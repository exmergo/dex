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

BigQuery bills a 10 MB minimum per query; a remaining budget below that is
refused with the math rather than letting the job fail server-side. Query-cache
hits bill zero and are recorded as such.

## Profiling behavior

- Aggregates only (`COUNT`, `APPROX_COUNT_DISTINCT`, `MIN`/`MAX` on safe
  columns), batched to keep statements bounded.
- `RECORD`/`STRUCT` columns get a non-null count only; `REPEATED` (ARRAY)
  columns get no aggregates (they cannot be NULL and distinct counts are
  invalid on them); `JSON`/`GEOGRAPHY` are treated as nested.
- Tables that require a partition filter are never scanned: they get a
  metadata-only profile plus a data-quality note.
- With `bigquery.max_full_profile_bytes` set, larger tables are profiled from
  a `TABLESAMPLE SYSTEM` block sample, flagged as approximate, and uniqueness
  is not judged.
- Exact distinct-count escalation (the uniqueness proof) spends only within
  the already-confirmed budget and degrades to approximate verdicts when the
  remaining budget cannot cover it.

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
server-side; `transform build` has no upfront estimate (dbt has no dry-run)
but still requires `--confirm` and a `--budget`, and its billed bytes land in
the spend ledger.

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
