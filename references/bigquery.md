# Connector: BigQuery

The first billed cloud connector. Namespace: `project.dataset.table`. Cost
paradigm: **bytes scanned**. Read-only against data, enforced in depth.

## Authentication: discover, don't ask

Auth is Application Default Credentials (ADC), never a prompted or pasted key.
The engine discovers credentials at runtime; if none exist it tells you the
fix:

```
gcloud auth application-default login
```

Service accounts work through `GOOGLE_APPLICATION_CREDENTIALS`, Workload
Identity Federation, impersonation (`--impersonate-service-account`), or the
metadata server; whatever `google.auth.default()` resolves. The GCP project
resolves in this order:

1. `bigquery.project` in `.dex/config.yml`
2. `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT`
3. the ADC default project
4. the `project` of a `type: bigquery` target in a discovered dbt `profiles.yml`

Only the principal's coarse type (user, service account, impersonated,
federated) is ever surfaced; its identity and any key material never cross the
envelope.

## Config

```yaml
# .dex/config.yml
connector: bigquery
bigquery:
  project: my-project            # billing/quota project jobs run in
  datasets:                      # source allowlist; empty means every dataset
    - raw                        #   bare names resolve against `project`
    - bigquery-public-data.samples   # qualified names read another project
  location: EU                   # optional job-location override
  dev_dataset: dbt_dev           # where dbt dev builds write (never a source)
  max_full_profile_bytes: null   # opt-in TABLESAMPLE threshold for huge tables
budget:
  ceiling: 1000000000            # per-command bytes (1 GB); --budget overrides
  session_ceiling: 10000000000   # cumulative bytes per UTC day
```

## Cost model: preflight before spend, capped at the server

- **Free:** `connect test`, `explore inventory`, all schema and row/byte-count
  metadata (API calls, never `INFORMATION_SCHEMA`, which bills a 10 MB minimum
  per query), and every dry-run.
- **Billed:** profiling aggregates, `explore query`, relationship verification
  probes, and `transform build`.

`explore query` and `explore cluster` profile an object they name that this connection has but the `.dex/` cache cannot adjudicate. That scan is billed, and it is priced into the same handshake as the statements rather than added afterward, so the estimate you confirm is the whole cost. A call carrying several statements is quoted once for all of them, itemized per statement, and an object two of them share is scanned once rather than twice. Resolving which objects need it stays free: it is object listing and column metadata, the same reads the inventory uses. Pass `--no-auto-profile` (or set `auto_profile: false` in `.dex/config.yml`) to be refused instead.

Every billed command is estimated first with free dry-runs. Without
`--confirm` it returns a `needs_confirmation` envelope carrying the byte
estimate (per table where relevant); re-issue with `--confirm` and
`--budget <bytes>`. Nothing executes unconfirmed or without a ceiling, and an estimate
over the ceiling is refused outright (confirmation cannot override it).

On the confirmed run, every statement is dry-run again and charged against the
budget, and every job carries a server-side `maximum_bytes_billed` cap, so a
drifting estimate cannot overrun the budget. Billed bytes are appended to
`.dex/spend.jsonl` (byte counts, job ids, and statement hashes; never SQL text
or values), and `budget.session_ceiling` binds cumulatively against that
ledger per UTC day.

The ledger gates billing and nothing else. A gate is built whenever a BigQuery
connection is assembled, free commands included, but the day's spend is read only
where it is needed: billed admission reads it and refuses if it cannot (a named
`reason: guard` refusal saying nothing ran), settlement tolerates a failure, and a
free command never reaches it. So a store keeping the ledger somewhere that can be
unreachable does not put `explore inventory` behind it. `connect test` is the one
free exception, because reporting the budget is its job: it takes one guarded read
and reports `budget.session_spent_today: null` when the ledger cannot be reached.

BigQuery bills a 10 MB minimum per query; a remaining budget below that is
refused with the math rather than letting the job fail server-side. Query-cache
hits bill zero and are recorded as such.

### What a profile estimate is made of

A profile's cost is not one query per table. After the aggregate scan, a
profile may issue up to three more queries against the same table: an exact
distinct count for a near-unique column, a value-domain probe for a
low-cardinality one, and a composite-key probe. Which of them run depends on
the aggregate scan's own approximate results, so none can be dry-run before it,
and the estimate holds one 10 MB floor apiece instead. A reserve is dropped only
where an object's metadata already rules the query out: a table known to hold no
rows, a table of nested or repeated columns only (no approximate distinct, which
every probe starts from), a table too small for a value domain, or one with too
few countable columns to form a composite pair.

An object BigQuery keeps no row count for, meaning every view and every external
table, reserves all three. Unknown is not empty: the count arrives inside the
aggregate scan, so every probe can run, and at estimate time there is no number
yet to narrow the hold with. That makes an external table's estimate four 10 MB
floors rather than one, which is worth knowing before pointing a profile at a
lakehouse of them. The reserve is released rather than spent when a probe does not
run, so it costs headroom for the length of the command, not money.

The reserve scales with object count rather than data size, so on a warehouse of
many small tables it can be most of the number. Both the `needs_confirmation`
payload and the over-ceiling refusal split it out, in prose and in
`reserved_bytes` / `reserved_queries`, so a raised budget is a decision rather
than a guess:

```
220,200,960 bytes of this estimate is escalation reserve: 21 queries at
BigQuery's 10,485,760-byte per-query minimum, held for probes a profile may add
after its aggregate scan and may never issue. The remaining 533,991,980 bytes
is dry-run scan
```

A mid-command verify checkpoint prices overlap probes, which carry no reserve,
so it reports none rather than repeating the profile's.

## Profiling behavior

- Aggregates only (`COUNT`, `APPROX_COUNT_DISTINCT`, `MIN`/`MAX` on safe
  columns), batched to keep statements bounded.
- `RECORD`/`STRUCT` columns get a non-null count only; `REPEATED` (ARRAY)
  columns get no aggregates (they cannot be NULL and distinct counts are
  invalid on them); `JSON`/`GEOGRAPHY` are treated as nested.
- Tables that require a partition filter are never scanned: they get a
  metadata-only profile plus a data-quality note.
- A row count is read from the metadata only for a base table, which is the only
  kind BigQuery maintains one for. A view, a materialized view, an external table
  and a snapshot all report `num_rows` as `0` whatever they hold, so that zero is
  read as unknown rather than as empty, and the real count comes from the
  aggregate scan's own `COUNT(*)`, which is already paid for. Until an object is
  profiled its count and byte size are therefore `null`, which ranks it as if it
  were small: profile it to rank it on its size. `empty table (no rows)` is
  reported from the aggregate's zero, so it means the object is empty rather than
  uncounted, and `maintain volume` names the objects it could not compare instead
  of returning no finding for them.
- With `bigquery.max_full_profile_bytes` set, larger tables are profiled from
  a `TABLESAMPLE SYSTEM` block sample, flagged as approximate, and uniqueness
  is not judged.
- Exact distinct-count escalation (the uniqueness proof), the composite-key
  probe, and the value-domain probe each spend only within the
  already-confirmed budget, and degrade (to an approximate verdict, no
  composite key, or no reported domain) plus a table note when the remaining
  budget cannot cover them. The composite-key probe degrades in two steps: it
  first narrows to the best-ranked candidate pairs the budget can cover, and
  only skips outright when it cannot cover one. Either way the note says which
  it was, because a missing composite key otherwise reads as a warehouse that
  answered and had none.
- The reserve holds one query minimum for the composite-key probe however many
  pairs it carries, so on a wide table the probe's dry run can price above what
  the estimate held for it. The per-statement gate, not the reserve, is what
  bounds that: the probe is priced against the live remaining budget before it
  runs, and narrows or skips rather than exceeding it.

## Read-only, in depth

BigQuery has no read-only connection mode, so the layers are: every statement
passes the SELECT-only guard in the BigQuery dialect (scripting, DML, DDL,
`EXPORT DATA`, `CALL`, and multi-statement input are refused); the adapter
calls no mutating client API; and the recommended grants are read-only:

- `roles/bigquery.dataViewer` on the datasets dex explores
- `roles/bigquery.jobUser` on the billing project
- `roles/bigquery.dataEditor` on the dedicated dev dataset only (for
  `transform build`)

## dbt

The `[bigquery]` extra carries `dbt-bigquery`. Running
`transform init --connector bigquery`
renders a single `dev` target with `method: oauth` (ADC; no secret
is ever written), pointed at `bigquery.dev_dataset` (default `dbt_dev`), and
refuses a dev dataset that is also a source. When `budget.ceiling` is set, the
profile carries `maximum_bytes_billed` so every statement dbt runs is capped
server-side. `transform build` surfaces an upfront byte estimate: it runs a free
`dbt compile`, dry-runs each compiled node the same way `explore` prices a query,
and sums the result (downstream nodes whose dev inputs are not built yet cannot
be dry-run, so on a cold target the total is a partial floor). It still requires
`--confirm` and a `--budget`, and its billed bytes land in the spend ledger.

With `--layered-schemas`, the scaffolded `generate_schema_name` override makes
each layer build into its own sibling dataset in the profile's project
(`staging_dev`, `intermediate_dev`, `marts_dev` on the `dev` target);
dbt-bigquery creates them on first build. Init's content preflight lists each
target dataset through the free `tables.list` metadata API (never
`INFORMATION_SCHEMA`, which bills a minimum per query) and warns when one
already holds tables or views.

## JSON quirks

Two BigQuery behaviors cost real debugging time when modeling JSON with
dynamic (data-dependent) keys, the shape NoSQL-sourced CDC exports land in:

- A JSON function's path argument must be a compile-time literal; building it
  per row (string concatenation into `JSON_QUERY`) is rejected at compile
  time. The subscript operator on the JSON value (`doc[key_expr]`) accepts a
  computed key.
- `JSON_KEYS` recurses into nested objects by default, silently returning
  nested field names alongside the real top-level keys; pass an explicit
  depth (`JSON_KEYS(doc, 1)`) to stop it. The symptom of the default is
  quiet: extra rows that orphan a downstream join.

The shipped `unpivot_json_object` macro (`transform macro unpivot_json_object`)
bakes both fixes in; prefer it to hand-rolling this pattern.
