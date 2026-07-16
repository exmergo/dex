# exmergo-dex-core

The portable, Apache-2.0 analytics-engineering engine behind
[Dex](https://github.com/exmergo/dex). All non-trivial logic lives here; the
Claude Code skills and the cross-agent `AGENTS.md` are thin wrappers that drive it
through one stable command contract.

Dex is the agent-native analytics engineering toolkit: explore an unfamiliar
warehouse, transform raw data into clean dbt models and a semantic layer on top,
and maintain all of it as the data underneath changes. Read-only against your data;
every change is a reviewable diff.

## Install

```
pip install "exmergo-dex-core"
```

Connector client libraries live behind extras. DuckDB is an in-memory data warehouse,
so you can start from there if you want to test Dex locally. We aim to support all major
data warehouses. Please suggest any missing connectors on [GitHub](https://github.com/exmergo/dex)!

```
exmergo-dex-core[duckdb]       # the on-ramp and the eval/benchmark engine
exmergo-dex-core[snowflake]
exmergo-dex-core[bigquery]
exmergo-dex-core[databricks]
exmergo-dex-core[redshift]
exmergo-dex-core[postgres]
exmergo-dex-core[all]          # every connector at once
```

## The command contract

Every subcommand prints exactly one sanitized JSON envelope to stdout and nothing
else; nothing reaches agent context except through that envelope. Credentials
never cross it, and data values cross only from profiled, PII-cleared columns,
bounded and capped by the query firewall. State persists in `.dex/`, so
subcommands are stateless and the agent orchestrates multi-step flows.

```
dex connect test --path data.duckdb
```

See [`references/command-contract.md`](../../references/command-contract.md) for
the full surface and the envelope spec.

## Status

Early and under active development; open issues on [GitHub](https://github.com/exmergo/dex)! Today the engine
runs Explore, Transform, and Maintain end to end on every connector: DuckDB,
BigQuery, Snowflake, Databricks, Amazon Redshift, and Postgres.

### Commands

`explore`: ranks what matters in an unfamiliar warehouse, profiles columns
selectively, flags PII, surfaces grain and data-quality warnings, infers joins
and verifies them with overlap probes (`--verify`), and executes agent-authored
ad-hoc SELECTs behind a PII-aware query firewall (`explore query`), all
read-only. It starts bare by default; with `--use-project` it reads an existing
dbt project, promoting declared `relationships` joins, honoring declared grain
and `unique` tests, and letting metric-backing models surface first in the
ranking. A repeatable `--scope` narrows the source scope per command without
writing back to `.dex/config.yml`.

`transform`: bootstraps a dbt project where none exists (`transform init`, with an
explicit connector, never a default), turns agent-authored edits and
deterministic staging scaffolds into reviewable, conflict-checked diffs
(`transform plan` / `apply`, with human edits authoritative on conflict), runs
gated dev-target-only builds with cost surfaced before any spend
(`transform build`), and authors the semantic layer as MetricFlow-validated dbt
semantic models (`semantic define|update|plan`, applied with `transform apply`).

`maintain`: detects drift against the `.dex/` snapshot on four axes and proposes
the fix: schema (structure), volume (freshness), grain (uniqueness and fanout),
and semantic (definitions, dangling references, and dimension cardinality).
`maintain check` sweeps all of them, ranked by blast radius; `reconcile`
proposes reviewable diffs tagged mechanical or advisory, applied through
`transform apply`. Detection is read-only on every connector; on billed
connectors the metadata axes (schema, volume, references) stay free while the
scanning axes (grain, dimension cardinality) take the `--confirm --budget`
handshake, so `check` is two-phase.

### Connectors

BigQuery: connects through Application Default Credentials
(`gcloud auth application-default login`; dex discovers credentials, it never
asks for keys). Metadata is free; every scan is dry-run first, returned as a
`needs_confirmation` estimate, and runs only with `--confirm --budget <bytes>`,
capped server-side by `maximum_bytes_billed` and recorded in a local
`.dex/spend.jsonl` ledger. dbt builds go to a dedicated dev dataset via
dbt-bigquery, which the `[bigquery]` extra carries. See
[`references/bigquery.md`](../../references/bigquery.md).

Snowflake: connects through discovered credentials (`connections.toml`,
`SNOWFLAKE_*` env, or a dbt profile; dex never asks for or persists a
password). The cost inversion from BigQuery: metadata is free (SHOW commands,
no warehouse), while scans bill warehouse time, so budgets are
**warehouse-seconds** with credits shown alongside. Estimates are an honestly
labeled heuristic (Snowflake has no dry-run), floored by the 60-second resume
minimum on a cold warehouse; the budget is hard-enforced anyway by a
per-statement server-side `STATEMENT_TIMEOUT_IN_SECONDS`, and actual seconds
land in the same `.dex/spend.jsonl` ledger. Billed work runs only on the
warehouse the config pins. dbt builds go to a dedicated dev database.schema
via dbt-snowflake, which the `[snowflake]` extra carries. See
[`references/snowflake.md`](../../references/snowflake.md).

Databricks: the lakehouse connector. Connects through the Databricks SDK's
unified auth chain (`databricks auth login`, `DATABRICKS_*` env, or a dbt
profile; dex never asks for or persists a token). Metadata is free through
the Unity Catalog REST API, and the SQL session opens lazily on the first
billed statement, so free commands never touch (or wake) the warehouse.
Budgets are **warehouse-seconds** with DBUs shown alongside. Estimates start
as an honestly labeled floor (no dry-run, no free table sizes) and refine
in-budget via `DESCRIBE DETAIL`; the budget is hard-enforced anyway by a
per-statement server-side `STATEMENT_TIMEOUT`, and actual seconds land in the
same `.dex/spend.jsonl` ledger. Billed work runs only on the SQL warehouse
the config pins. dbt builds go to a dedicated dev catalog.schema via
dbt-databricks, which the `[databricks]` extra carries. See
[`references/databricks.md`](../../references/databricks.md).

Amazon Redshift: Serverless-first and provisioned-compatible. Connects through
the AWS default credential chain (a pinned Serverless `workgroup` or provisioned
`cluster_identifier` mints IAM temporary database credentials), the `REDSHIFT_*`
environment, the committed non-secret target (password via `REDSHIFT_PASSWORD`),
or a dbt profile; dex never asks for or persists a password. Metadata comes from
the Postgres catalog (`pg_class` merged with `SVV_TABLE_INFO` and `SVV_COLUMNS`,
so empty tables still appear). The guarded quantity is compute time, so budgets
are **compute-seconds** with RPU-hours shown alongside (dollars when
`redshift.rpu_price_usd` is set), floored once by the 60-second Serverless wake
minimum; the budget is hard-enforced by a per-statement server-side
`statement_timeout`, and actual seconds land in the same `.dex/spend.jsonl`
ledger. Profiling uses `HLL(...)` approximate distincts with exact escalation
in-budget; the session is read-only at the server. dbt builds go to a dedicated
dev schema via dbt-redshift, which the `[redshift]` extra carries. See
[`references/redshift.md`](../../references/redshift.md).

PostgreSQL: the operational-database connector. Connects through discovered
credentials (`pg_service.conf`, `DATABASE_URL`, the `PG*` environment, or a
dbt profile; dex never asks for or persists a password). Nothing is billed in
dollars; the guarded quantity is load on what is often a production primary,
so budgets are **database-seconds** through the same confirm handshake. Query
estimates come from the genuinely free planner preflight (`EXPLAIN`), profile
estimates from relation sizes, both labeled heuristic; the budget is
hard-enforced anyway by a per-statement server-side `statement_timeout`, and
actual seconds land in the same ledger. The session is read-only at the
server (`default_transaction_read_only = on`), profiling leans on the
planner's own statistics instead of scanning distincts, and dbt builds go to
a dedicated dev schema via dbt-postgres, which the `[postgres]` extra
carries, with the ceiling injected as a statement timeout through
`PGOPTIONS`. See [`references/postgres.md`](../../references/postgres.md).

## License

Apache-2.0.
