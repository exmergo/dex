# The PII policy

dex flags personal data and never surfaces it. This file is the whole policy:
what a flag is, what each surface does with one, and how a human clears one.
Every other document links here rather than restating it.

How a flag is *produced* (the name-pattern table, the categories, the
value-shape statistics) belongs to [`methodology.md`](methodology.md). This file
owns what a flag *means* and who can clear it.

## What a flag is

A flag is recorded strictly as `(column, category, confidence)` with no example
value, and that triple is what propagates downstream into emitted dbt. Detection
runs on column names and aggregate shape, never by inspecting values, so a
column named `code` full of email addresses is not flagged anywhere in dex.

A flag is never removed by evidence. Value-shape statistics computed during
profiling move its confidence in both directions, and they fail closed: when the
evidence is missing or ambiguous the name-derived confidence stands, because
absence of evidence never weakens a flag. Only a human decision removes a flag,
and only through the config entries below.

## Presence versus threshold

This is the distinction that decides every surface's behavior, and it is why two
commands can disagree about the same column without either being wrong. Some
consumers act on a flag's *presence* at any confidence; others compare its
confidence against the blocking threshold.

| Surface | Gate | Effect |
|---|---|---|
| min and max suppression | presence, any confidence | the extremes are never computed, so no raw value leaves the engine |
| dbt `meta` stamping | presence, any confidence | the flag is stamped into model and column `config.meta` |
| cluster feature selection | presence, any confidence | flagged columns are excluded; naming one is opt-in and mean only |
| the query firewall | threshold | at or above, projection is refused; below, it runs with a warning |
| the seed header gate | threshold | a seed column at or above the threshold is refused |
| declaration population reporting | presence, any confidence | a flagged filter withholds its values and the entire free-text population rule |
| the semantic request gate | presence, any confidence | a flagged dimension refuses the whole command |
| warehouse error text | none: every column | the value a conversion failure quotes is masked (see [Warehouse error text](#warehouse-error-text)) |

A weak flag therefore still suppresses min and max and still stamps `meta`,
while allowing a query that projects the column. What a weak flag means is the
consumer's decision, and the consumers differ on purpose.

The stamp is `contains_pii: true` on the model, and `contains_pii: true` plus
`pii_category: <category>` on each flagged column, written under `config.meta`
because dbt deprecated a top-level `meta` and dbt Fusion rejects it. The key names
are unchanged from earlier releases, because tools outside dex read them. Readers
accept a stamp in either location, preferring `config.meta`, so a project
scaffolded by an older dex keeps working without a rewrite.

## The threshold

A flag at confidence **0.5** or above blocks projection. The threshold is a
hard-coded engine constant, uniform across categories, and deliberately not
configurable: a configurable threshold would let a one-line config edit quietly
widen the PII boundary. At today's base confidences everything blocks, so only a
flag de-rated by value-shape evidence at profile time falls below.

This section is the only place in the repository that states the number.

## The firewall rule

Agent-authored queries must be SELECTs or supported set operations. Query
bodies in CTEs, subqueries and set-operation branches follow the same rule.
Nested metadata statements such as `DESCRIBE` are refused before connection
discovery, profiling or execution, including in scalar expressions and unused
CTEs. The typed refusal names the unsupported shape and points to
`explore inventory` or `explore profile` for introspection. This also applies
to compile-time queries run through `transform plan`, where the refusal of a
`DESCRIBE`, `SUMMARIZE` or `information_schema` read names
`adapter.get_columns_in_relation` instead: dbt's column listing is answered from
metadata (names and types, no values), never run as SQL.

Output may carry values only from profiled columns whose flag is absent or below
the threshold. Every value path from a blocking column must pass through a
measuring aggregate (COUNT, APPROX_COUNT_DISTINCT, AVG, SUM, STDDEV, and the
like). Value-carrying aggregates (MIN, MAX, ANY_VALUE, STRING_AGG) do not
qualify, unknown functions fail closed, and `SELECT *` is refused when the
expansion includes a blocking column.

Filters, join conditions, GROUP BY and ORDER BY are unrestricted: values flow
in, not out. A count projects no column, which is why a filter over a flagged
column is still attributable.

Projecting a column whose flag sits below the threshold runs, with an envelope
warning naming the column, category, and confidence. Treat that warning as
information for the user, not an error to fix.

Unnested JSON and array outputs inherit the source column's flags.

## Protected values and transformation columns

A refusal to return protected values to an agent does not instruct it to remove,
redact or replace a column in a user's transformation. When the requested or
existing output contract includes that field, preserve its authorized pass-through
through the normal `transform plan` and `transform apply` path where supported,
then build on dev with verification. Validate column presence and types through
metadata and use only permitted aggregate evidence for row checks. There is no
need to retrieve an example of the protected value.

Intentional removal or redaction requires an applicable user instruction or
contract. Do not invent either as a workaround for an inspection restriction,
and keep protected examples out of assumptions, tests, logs and explanations.
Only a human may clear a PII flag.

Permission belongs to the command and operation being performed. A supported
model pass-through does not authorize exporting the field as a seed: the seed
header gate still applies because seed values enter the repository. If a
transformation itself is prohibited or unsupported, report that limitation
accurately and leave the guard in place. Never bypass a privacy or dev-target
guard or infer permission from another command's success.

## The stricter surface

Where a command's result *is* the values rather than an aggregate those values
slice, the gate moves from threshold to presence and from dropping a column to
refusing the command.

A metric query returns aggregates that a dimension merely groups, so a flagged
dimension can be dropped from the grouping and the query still answers
something. Listing a dimension's values cannot degrade that way, so **any flag
refuses the command outright**. The refusal names both clearing routes.

Evidence rules run in both directions: a dimension whose name reads innocuous is
refused when its resolved column is flagged, and a profiled, cleared column is
not re-blocked by a PII-shaped name. Where a dimension resolves to no physical
column, or to a relation that was never profiled, the name heuristic is the
fail-closed floor, so silence never clears. A result that ran on the floor alone
says so, naming each dimension that did.

A column a query reads without the caller naming it is screened as if the
caller had grouped by it. A time grouping (`metric_time`, or the column a SLayer
`--grain` buckets) is screened as each measure's time dimension. A token
carrying a grain suffix is screened as the dimension it buckets. A dimension
defined as an expression is screened by every column the expression reads, and
refused when dex cannot parse it. A SLayer saved measure named in a filter is
screened by the columns its formula reads.

Which evidence each backend can reach, and why a computed expression resolves to
no column, is in [`semantic-layer.md`](semantic-layer.md) and
[`ossie-compatibility.md`](ossie-compatibility.md).

## Clearing a flag

Two durable routes, both reviewable in git and both re-applied on every profile
so they survive re-profiling:

1. A `pii_overrides` entry in `.dex/config.yml`.
2. `config: {meta: {pii: false}}` on the dimension in the project, for a
   semantic layer.

An override takes effect at query time immediately, without re-profiling, and
leaves an audit trail in the cache recording which category the detector had
matched. `dex config set pii_overrides '<list>'` writes the first route as an
in-place edit, setting the list whole (existing entries included). It answers
with the diff and writes only on `--confirm`, and that confirmation is the
human's decision to clear the flag, never an agent's. **Never hand-edit `.dex/cache.json` to clear a flag**, and never
suggest weakening detection: neither survives the next profile, and neither is
reviewable.

A `pii_overrides` entry takes one of two mutually exclusive shapes, enforced at
load:

```yaml
pii_overrides:
  # Exact: one reviewed column, fully qualified with the cache's
  # connector-normalized identifier, so it can never silently widen to a
  # same-named column elsewhere.
  - column: MY_DB.PUBLIC.REGION.R_NAME
    reason: region label, not a person

  # Pattern: one reviewed decision about a structurally identical column that
  # exists by construction on many tables, where the exact form would cost one
  # entry per table per environment. Glob scope, case-insensitive.
  - column_name: document_name
    scope: analytics.cdc_*.*
    reason: resource path in this CDC export, not a person name
```

Both forms are typo-guarded at profile time. An exact entry naming a column its
table does not have warns, and so does a pattern entry whose scope matches
profiled tables when none of them carries the named column. A scope matching no
table yet stays silent, since new entities landing later under the same scope is
the point of the pattern form. Both `explore profile` and `explore map` emit
this warning with the same text, so a scheduled `map` is enough to catch a
rename that left an override pointing at nothing.

## Warehouse error text

A warehouse refuses a statement in its own words, and dex keeps them: the column
it could not find, the type it expected and the object that is gone are what
make a failure diagnosable. One family of refusals also quotes a value. A
conversion, parse or range failure names the input it could not handle, and that
input came from a row, possibly from a flagged column. An error is not a result
column, so the firewall never sees it.

So that value is masked and every other word is kept:

| Warehouse says | The envelope says |
|---|---|
| `Could not convert string 'org_cedar' to INT32` | `Could not convert string <value withheld> to INT32` |
| `Bad int64 value: org_cedar` | `Bad int64 value: <value withheld>` |
| `invalid input syntax for type integer: "org_cedar"` | `invalid input syntax for type integer: <value withheld>` |
| `Referenced column "nope" not found` | unchanged |

The rule holds on every connector and on every surface a warehouse's words can
reach: an error envelope (`explore query`, a profile scan), a statement in an
`explore query` batch and the query log, the notes, warnings and `suppressed`
reasons a check that could not run leaves behind, a record's `reason` and
`detail` (`join_coverage`, `parent_population`, `temporal_contracts`), a
dataset's `data_quality` in the cache, the messages and node evidence of a
`transform build` whose model failed on a cast, and the hosted semantic layer's
error. A plain join of a text key to an integer key is the common way to meet
it: the warehouse casts implicitly and fails on the first key that does not
convert, with no cast in the SQL.

Where a count or a conversion probe must carry nothing row-level at all (row
attribution, identifier conversions, a converted join key, `transform compare`),
the message is withheld and only the exception type is reported. In values-free
mode no warehouse words reach the caller at all: an error carries its class and
a stable code (see [`values-free-mode.md`](values-free-mode.md)), and a refused
join probe in `explore relationships` or `explore map` names the exception type
only.

The mask is a catalogue of the shapes each connector's conversion failures take,
held in `exmergo_dex_core.value_mask` and tested per connector. A message that
still reads as a conversion failure after the catalogue has every quoted segment
masked, so an unlisted shape over-masks rather than leaks; a shape that quotes
its value unquoted and is not listed would pass through, and finding one is a
bug to report.

## Broader than PII: values-free mode

This policy keeps personal data out of results column by column. A host that must
keep every value out, whatever the column holds, selects values-free mode instead,
which this policy does not replace: no `min_value`, `max_value` or value domain on
any column, aggregate-only queries, and errors as codes. See
[`values-free-mode.md`](values-free-mode.md).

## Where the policy is enforced

[`command-contract.md`](command-contract.md) carries the surfaces that gate a
statement: the query firewall, the auto-profile that makes a column adjudicable
at all, the seed header gate on values entering git, and row-attribution counts.
Profiling's own suppression and the diagram renderer are in
[`methodology.md`](methodology.md), `meta` stamping on emitted models is in
[`project.md`](project.md), and the per-backend request gate is in
[`semantic-layer.md`](semantic-layer.md).
