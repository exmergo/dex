# Values-free mode

Some hosts need an agent to explore a warehouse (what exists, its shape, its
grain, how tables join, how many rows match a condition) without any value read
from a table reaching the model's context. The model provider may sit outside the
data boundary, or the findings may be posted somewhere more people can read than
can read the warehouse. Values-free mode is that guarantee, made by the engine
before the agent sees anything, rather than a filter a host applies afterwards. A
value the agent saw once can come back in its prose, and no code can tell which
words of a sentence are values, so filtering after the fact cannot give it.

The [PII policy](pii-policy.md) keeps personal data out of results by column.
This mode is broader and coarser: no cell value of any column, whatever it holds.

## Selecting it

The caller selects the mode, never the repository, and never the agent's SQL.

| Where | How | What it does |
|---|---|---|
| Library | `DexEngine(result_mode="values_free")` | pins the engine: every call runs in the mode, and a call asking for `result_mode="full"` is refused (`result_mode_pinned`) |
| Library | `engine.profile(..., result_mode="values_free")` | opts one call in on an engine built for full results |
| CLI | `--values-free` on any command | runs that command in the mode |
| CLI | `DEX_RESULT_MODE=values_free` in the environment | pins every command the process runs; there is no flag that turns it off |

`.dex/config.yml` cannot select the mode, in either direction. An agent may have
written that file, so a switch there would be a switch the agent controls. A
`result_mode` key in it is reported by `dex config validate` (kind
`caller_only_key`, not fatal) and rides every envelope as a warning, and
`dex config set result_mode ...` is refused. An unknown `DEX_RESULT_MODE` value
refuses every command (`reason: configuration`) rather than reading as full.

Every result produced in the mode says so: `data.result_mode` is
`"values_free"` on every CLI envelope the process prints, refusals and
`needs_confirmation` asks included, and `Result.result_mode` is
`ResultMode.VALUES_FREE` on every library result. A host asserts on it. Outside
the mode the key is absent, so a full-mode payload is unchanged.

## Which commands run

An allowlist, so a command added in a later release refuses until someone has
reviewed what it returns:

- `connect test`, `config get`, `config validate`
- `explore inventory`, `explore profile`, `explore relationships`, `explore map`,
  `explore diagram`
- `explore query`, including several statements, `--sql-file` and `--across`
- `explore semantic list` (it reads repository metadata, not the warehouse)

Everything else refuses before it opens a connection, with `reason: guard` and
`data.code: command_not_values_free`. That includes `explore cluster` (a small
cluster's centroid is one row's values), `explore semantic values` and
`explore semantic query` (their output is dimension values or group keys), every
`transform`, `semantic` and `maintain` command, `config set` and `config unset`,
and `demo`. On the library, every `DexEngine` method outside the reviewed set
raises `ValuesFreeError` with that code.

## What `profile` keeps and withholds

Kept: row counts, `null_fraction`, `distinct_count` and `distinct_count_exact`,
`is_unique`, `data_type`, `nullable`, candidate keys, grain, composite keys,
`key_evidence`, `findings`, `rank_score`, PII category and confidence, temporal
granularity, span in days, period and gap counts, and every elision count.

Withheld: `min_value`, `max_value` and `value_domain` on every column, numeric
ones included, because a maximum salary is a salary. They are absent from each
column rather than null, named once per dataset in `withheld_fields`, and kept out
of `suppressed_fields`, whose meaning ("null on every column shown") they do not
share. A column the default summary showed only because it had a value domain is
not shown in the mode, so which columns appear does not depend on what the cache
held.

The free-text notes (`data_quality`, `notes`, `findings[].detail`) are dex's own
sentences and carry counts, fractions, column names and fixes, never a value. A
BigQuery batch the server refused while profiling is noted with BigQuery's
structured reason code rather than its message, in every mode, because notes are
cached and copied into `explore map`.

`map`, `relationships`, `inventory` and `diagram` never carried a value; in the
mode they are stamped, and any profile they scan on the way is cached without
values. `MapResult.cache`, the cache handed back to a library caller, is a copy
with every value withheld.

## What `query` accepts

Only a statement whose every output column is an aggregate over the rows it
reads, returning one row:

- One plain `SELECT`. No `UNION`, `INTERSECT` or `EXCEPT` (`set_operation`).
- No `GROUP BY` keyed by a column (`group_by_column`): group keys come back as
  cells. `GROUP BY ()` and a string constant are fine; `GROUP BY 1` names a column
  by position, and `GROUP BY ALL`, `ROLLUP`, `CUBE` and `GROUPING SETS` key on
  columns.
- No `DISTINCT` (`distinct_projection`), `LIMIT`, `OFFSET` or `FETCH`
  (`limit_offset`), `ORDER BY` (`order_by`), `QUALIFY` (`qualify`), or window
  function (`window_function`).
- Every output column contains at least one aggregate on the allowlist
  (`no_aggregate` otherwise). The allowlist is the firewall's measuring
  aggregates: `COUNT` (including `COUNT(DISTINCT ...)`), `COUNT_IF`/`COUNTIF`,
  the approximate distinct counts, `SUM`, `AVG`, the `STDDEV` and `VARIANCE`
  family, `CORR`, `COVAR_*`, `BOOL_AND`, `BOOL_OR`, `ENTROPY`, `KURTOSIS`,
  `SKEWNESS`, and ClickHouse's `uniq*` family with its `-If`, `-OrNull`,
  `-OrDefault` and `-Distinct` combinators. `FILTER (WHERE ...)` is allowed.
- Any other aggregate is refused (`aggregate_not_allowed`): `MIN`, `MAX`,
  `ANY_VALUE`, `ARG_MIN`/`ARG_MAX`, `MIN_BY`/`MAX_BY`, `MODE`, `FIRST`/`LAST`,
  `MEDIAN`, every quantile and percentile, `ARRAY_AGG`, `STRING_AGG`, `LISTAGG`,
  `LIST`, `APPROX_TOP_K`, `topK`, `groupArray`, and anything not yet reviewed.
- Outside its aggregates an output column may hold only numeric literals,
  arithmetic, comparisons and boolean logic, `CASE` whose branches obey the same
  rule, a cast to a numeric type, and `ROUND`, `ABS`, `COALESCE`, `NULLIF`,
  `GREATEST`, `LEAST`, `CEIL`, `FLOOR`, `SQRT`, `POWER`, `LN`, `LOG`, `EXP`,
  `SIGN`, `SAFE_DIVIDE`. A bare column is `bare_column` (so is `*`), a string,
  date or boolean literal is `literal_projection`, any other function is
  `function_not_allowed`.
- A scalar subquery in the select list must pass the same rule
  (`unaggregated_subquery`). Subqueries and CTEs in `FROM`, and everything in
  `WHERE`, `HAVING` and join conditions, are unrestricted: values flow into them,
  not out of them.

The rule reads only the parsed statement, so a refused shape opens no
connection, prices nothing and profiles nothing. The existing firewall and its
PII rule still apply on top of it.

What the warehouse returns is held to the same shape: a result with more than one
row (`multi_row_result`), a cell that is not a number or a boolean
(`non_numeric_result`; DuckDB's `AVG` over a timestamp is a timestamp), or a
result that would have been truncated to fit the payload budget
(`would_truncate`) is discarded, and the statement refused. Refuse, never
truncate: a host relies on a refusal being a refusal.

Several statements are judged one at a time, as always. A refused statement in a
batch has `status: refused`, its `code`, and no `cells`; its neighbors are
unaffected.

## Errors carry codes, not words

A warehouse's own error text can quote the value it failed on (`Could not convert
string 'abc' to INT32`, `invalid input syntax for type integer: "abc"`). In the
mode, an error reaching the caller carries its class (`reason`) and a stable code
(`data.code` on the envelope, `code` on a batch statement, `.code` or
`.values_free_code` on the exception):

| What failed | What the caller gets |
|---|---|
| A refusal of the mode's own rules | `ValuesFreeError`, `reason: guard`, the rule's code, the column or function it named |
| The warehouse refused a statement | `WarehouseQueryError` with its words replaced, its `reading` and `stage` kept, `reason: execution_failure`, `code: warehouse_refused` |
| An exception dex did not raise on purpose | `ValuesFreeError`, `reason: internal`, `code: unexpected_error`, no text of its own |
| Any other refusal dex wrote | unchanged words (they name objects, columns, numbers and fixes) and a code |

The replacement does not chain the original, so a traceback does not print the
words it exists to drop. The query ledger records the code in place of the words
too. It still records each statement's SQL, which is the agent's own text.

## The cache

A profile written in the mode keeps no value: the entry is marked
`values_withheld`, and a caller outside the mode treats it as stale and scans
again, so it is never served as a complete profile. A full profile already in the
cache is served to a caller in the mode with its values projected away, and is
left exactly as it was for the next full caller. Relationship inference runs
before values are withheld, so the confidences it reports are the same in either
mode; only a later run that reuses a withheld entry infers without its ranges.

## The conformance suite

The guarantee ships with an executable check, so a host can run it against the
release it pins:

```python
from exmergo_dex_core.values_free_conformance import ValuesFreeContract


class TestDexValuesFree(ValuesFreeContract):
    pass
```

Install `exmergo-dex-core[values-free-conformance]` (pytest and DuckDB). The suite
builds a small DuckDB warehouse whose every text, date, timestamp and numeric
value carries a distinctive sentinel, then asserts that no sentinel appears in
any result, envelope, refusal, warehouse error, cache entry the mode wrote, or
ledger row; that every accepted statement returns numbers and every refused one
its code and no cell; that neither mode's cache entries leak into the other; and
that no result carries a field it has not reviewed (`KNOWN_FIELDS`), so a field a
later release adds fails the suite until someone decides it is safe. Override
`make_engine` to build the engine the way your host does.

## What it does not claim

These are residual risks the mode names rather than closes. A host bounds them
with a cost ceiling it pins itself (`DEX_SESSION_CEILING_CAP`; see
[`cost-controls.md`](cost-controls.md#a-host-set-cap-on-the-daily-ceiling)) and a
query count per run.

- **Membership probing.** `select count(*) from t where email = 'a@b.c'` returns a
  number, and enough such probes test guesses about values.
- **Small counts.** A count of one identifies a row.
- **An aggregate over one row is that row's value.** `select avg(salary) from t
  where id = 42` is accepted and returns a salary.

## Population prose in native catalogs

Ossie, SLayer and Cube catalogs omit Dex-generated population sentences in every
result mode, including sentences in existing files. Catalog reads are
connectionless and cannot establish that population values are safe. The authored
description prefix is preserved when the generated suffix matches its structured
`dex.population` metadata. A stale or malformed suffix causes the description to
be withheld with a value-free note. This happens before catalog search. Files and
drift fingerprints are unchanged. Use `explore profile|map --use-project` for the
structured population, screened by the declaration withholding policy.
