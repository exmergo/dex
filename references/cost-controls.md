# Cost controls

Nothing dex runs touches the warehouse without a ceiling. This file is the whole
cost guard: the handshake that surfaces a price before spending, the ledger that
records what was spent, the cumulative ceiling that binds a day's work, and the
one surface where no ceiling is possible. Every other document links here rather
than restating it. What a scan costs on a particular warehouse, and how that
figure is derived, belongs to each connector's own file; this file owns the
mechanism that is the same everywhere.

## What "budget" means here

Money, or load on a machine somebody pays for: `--budget`, `budget.ceiling` and
`budget.session_ceiling`. Two unrelated things are also called a budget and are
not governed here, because they protect agent context rather than spend:
`query.max_payload_bytes` and the semantic catalog's caps. `--confirm` likewise
carries a second meaning on the project commands, where it overwrites a stored
plan or resolves an apply conflict; that use is not a cost gate, and it lives in
[`dbt-project.md`](dbt-project.md).

## The handshake

`cost` is a preflight estimate, and it comes from free dry-runs. Any command
that would spend returns `needs_confirmation` unless given `--confirm`, plus a
`--budget` on billed connectors; DuckDB is free, so the confirm handshake alone
gates it. Re-issue the same command with `--confirm` and `--budget <magnitude>`
in the connector's unit, and the confirmed run re-checks every statement against
the budget with a server-side cap as backstop. An estimate over the ceiling is
refused outright; confirmation cannot override it.

`transform build --verify` prices its row counts into the same estimate as the
build itself, as a `(row counts)` entry in the per-table breakdown, so one
`--budget` covers both phases. Only a relation the warehouse keeps no row count
for costs anything, which is any view; a table's count is free metadata, and a
verdict resting on it is reported `exact: false` to say so. On a cold dev target
the counts cannot be priced before the build has written the relations, so a
note says so and they are priced again afterwards as a phase drawn against the
reservation the build is already holding.

**A priced phase the caller did not request is an offer, not a refusal.** When a
command's free half is a complete answer in its own right, the envelope is `ok`
and the price of the optional half sits in `data.offer`, carrying the same
estimate, breakdown and hint a refusal would. `maintain check` and
`maintain semantic` are the two. `cost.estimate` stays unset on an offer, so a
populated `cost.estimate` on an `ok` still means settled preflight for work that
ran, and `needs_confirmation` stays reserved for work the caller asked for and
has not authorized.

## Paradigms

`cost.paradigm` names the connector the command ran against, not what the
command happened to cost, so a free metadata command still reports the paradigm
a billed one would bill in. `free_local` is a positive claim that the connector
bills nothing; `null` means no connector was resolved.

| Connector | Paradigm | Binding unit | Cost model |
|---|---|---|---|
| BigQuery | `bytes_scanned` | bytes | [`bigquery.md`](bigquery.md) |
| Snowflake | `compute_time` | warehouse-seconds | [`snowflake.md`](snowflake.md) |
| Databricks | `compute_time` | warehouse-seconds | [`databricks.md`](databricks.md) |
| Redshift | `compute_time` | compute-seconds | [`redshift.md`](redshift.md) |
| Postgres | `db_load` | database-seconds | [`postgres.md`](postgres.md) |
| ClickHouse | `db_load`, or `compute_time` on Cloud | database-seconds | [`clickhouse.md`](clickhouse.md) |
| DuckDB | `free_local` | nothing to confirm | [`duckdb.md`](duckdb.md) |
| dbt Cloud Semantic Layer | `hosted` | no ceiling possible | see below |

What the envelope carries alongside the paradigm, including `estimate_quality`
and the `data.spend` key parity rule, is in
[`command-contract.md`](command-contract.md).

## The per-command ceiling and `--scope`

`--budget` sets the ceiling for one command and is refused when missing, because
nothing runs unbudgeted. `--scope` narrows the committed source allowlist for
one command, in each connector's own namespace vocabulary. Two rules make it a
cost control rather than a hint: a committed allowlist is a cost boundary, so
every `--scope` entry must resolve inside it and one reaching outside is
refused; and a scope is honored or named in an error, never dropped, so an entry
that names nothing refuses and lists what exists. It is never written back to
config. The per-connector vocabulary is in
[`command-contract.md`](command-contract.md).

## Pricing an auto-profile

`explore query` and `explore cluster` profile an object they name that the
connection has but the `.dex/` cache cannot adjudicate, because the firewall
cannot judge a column whose flags it does not have. That scan is billed, and it
is priced into the same handshake as the statements rather than added
afterward, so the estimate you confirm is the whole cost. A call carrying
several statements is quoted once for all of them, itemized per statement, and
an object two of them share is scanned once rather than twice. Resolving which
objects need it stays free: it is object listing and column metadata, the same
reads the inventory uses. `explore cluster` prices its profile first and gates
its sample mid-command, so a budget too small for the sample returns
`needs_confirmation` with the profile already saved rather than discarding it.

## The spend ledger

`.dex/spend.jsonl` is one JSON object per line, appended and never rewritten.
Every row carries the same keys, `null` where one does not apply, so the file
parses to a stable schema and no reader has to interpret an absent key.

| key | what it holds |
|---|---|
| `at` | UTC ISO-8601 stamp of the write |
| `connector` | the connector that billed. Several can share one file, so filter on it before summing |
| `command` | the command that wrote the row |
| `entry` | the kind: `reservation`, `settlement` or `release`, and nothing else |
| `reservation_id` | ties one command's rows together. `null` on a `transform build` settlement, which settles outside any gate because dbt runs the statements |
| `billed_bytes` or `billed_seconds` | the magnitude, in the connector's unit. Signed |
| `estimate` | the whole-command preflight figure the settlement was admitted on. `null` on the other two kinds, and on a settlement whose pricing degraded to no estimate at all |
| `job_id` | the warehouse's identifier for the job, where it has one |
| `statement_sha256` | a hash of the statement. Never its text, never a value |

**Settled spend is the `entry == "settlement"` filter**, summed over one
connector's unit. That is not the same number as `session_spent_today` whenever
a command is in flight: a reservation is positive and its release is the same
magnitude negative, so the three kinds net to actual spend once a command has
settled, and until then the day's total legitimately reads higher by the
headroom being held. Sum what you are given; clamping the negative would leave a
release uncancelled.

**A ledger holding settlements alone is not a ledger missing rows.** A
reservation exists to be seen by a concurrent command settling against
`budget.session_ceiling`, so a project that never set a daily cap has nothing to
protect and writes none. **A row with no `entry` at all** was written by a dex
older than v1.5.1 and is a settlement; filter on `entry` being `"settlement"` or
absent to include them.

**The ledger is a dependency of billing, not of every command.** Billed
admission reads it and fails closed if it cannot, with a named refusal
(`reason: guard`) that says nothing ran, so re-issuing is safe. Settlement
tolerates a read failure instead, so a backend that goes away mid-command does
not turn a command that already ran into a refusal. Writing a store that serves
this contract is [`storage.md`](storage.md).

## The cumulative ceiling

`budget.session_ceiling` binds per UTC day and across commands that overlap in
time, not only across commands that follow one another. An admitted command
books its estimate against the day's headroom before it runs and releases the
unspent part when it settles, so a second command issued while the first is
running is measured against what is genuinely left. Three consequences a caller
can see:

- `cost.ceiling` on a refusal reflects headroom another command is holding, so
  two runs of the same command can be refused against different numbers.
- `session_spent_today` counts held headroom, so it reads higher than settled
  spend while another billed command runs and can briefly exceed the ceiling
  without anything having overspent. Run commands one at a time and it is
  exactly settled spend.
- A command killed outright leaves its estimate booked until the UTC rollover.
  Every softer exit, including an interrupt, releases.

Unlike `budget.ceiling`, a missing `budget.session_ceiling` only warns, because
refusing would break every project that never set one. Config is read from
`<repo_root>/.dex/config.yml` and does not inherit, so a second repo root has its
own budget or none.

## The one-time ask

That warning is accurate and it is also the default state of every new project,
so on its own it would repeat on every billed command, which is the condition
under which warnings stop being read. Instead, the first billed command in a
project with no recorded decision returns `needs_confirmation` naming a
`suggested_session_ceiling` (five times that command's own estimate, in the
connector's unit, as a starting point) and a `session_ceiling_hint` spelling out
both answers. Answer with `--session-ceiling <value>` to set one or
`--no-session-ceiling` to record that the project runs unbounded. Either answer
is written to `.dex/config.yml` and nothing asks again in that project.

The ask is the last check before spend, so an unanswered one has run nothing,
booked no headroom, and never reached the ledger. The unconfirmed cost ask that
precedes it carries the suggestion in `notes`, so one re-run can answer both.
Three cases are never asked: a project that already set the ceiling, one that
recorded a decline, and a config-free ad-hoc read, which has no committed file
to record an answer in. A decline loosens nothing: the warning still fires and
now names the decline, so a reader can tell a settled choice from a project that
was never asked.

## The one surface with no cost guard

The hosted dbt Cloud Semantic Layer (`explore semantic query --api`) is the one
place dex cannot enforce a ceiling. dbt Cloud owns the warehouse connection and
executes the query server-side, so no dry-run estimate and no server-side cap
are possible from dex. That backend therefore does not ask for `--confirm`,
because a confirmation dex could not back with a ceiling would be dishonest. It
reports `cost.paradigm: hosted` with no estimate and no ceiling, and states on
every result that the cost guard is unavailable and spend is governed by the dbt
Cloud environment. Do not present a hosted result as cost-guarded, and do not
read the absence of an estimate as "it was free".

The local backend (`--local`) executes through dex's own connector and keeps the
full handshake. Which axis decides this is in
[`semantic-layer.md`](semantic-layer.md).
