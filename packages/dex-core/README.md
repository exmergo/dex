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

Connector client libraries live behind extras. DuckDB is an embedded data warehouse,
so you can start from there if you want to test Dex locally. We aim to support all major
data warehouses. Please suggest any missing connectors on [GitHub](https://github.com/exmergo/dex)!

```
exmergo-dex-core[duckdb]       # the on-ramp and the eval/benchmark engine
exmergo-dex-core[snowflake]
exmergo-dex-core[bigquery]
exmergo-dex-core[databricks]
exmergo-dex-core[redshift]
exmergo-dex-core[postgres]
exmergo-dex-core[clickhouse]
exmergo-dex-core[all]          # every optional capability at once
```

Two capabilities sit behind their own extras rather than a connector's:
`[semantic]` and `[semantic-api]` for the local and hosted semantic-layer query
backends, and `[cluster]` for `explore cluster`. `[all]` covers all of these too.

`[semantic-api]` is the one extra that stands completely alone: dbt Cloud owns the
warehouse connection and executes server-side, so a deployment that only queries a
hosted semantic layer needs no connector, no dbt-core, and no SQL parser. Every
other command validates SQL before running it, which is why the connector extras
carry the dialect engine; run one without a connector installed and dex refuses
with the install to use rather than guessing.

## First run, with nothing to point it at

`[duckdb]` is also what carries the on-ramp, so a fresh install has something to
work against before you have wired up a warehouse:

```
pip install "exmergo-dex-core[duckdb]"
dex demo
dex explore map
```

`dex demo` generates a small e-commerce warehouse (7 tables, 29,512 rows) in the
current directory and a `.dex/config.yml` beside it, so everything after it runs
with no flags. No credentials, no cloud account, no network. The data comes from a
pinned seed, so every run produces the same rows and the numbers quoted here are the
numbers you get.

It is seeded to be realistically broken rather than tidy: a key that lost its
uniqueness to a double-loaded batch, a key mixing two id schemes from a merged
catalogue, a join whose columns share a name and none of their values, a table an
interrupted load left empty, two columns whose declared type contradicts their
content, and personal data alongside two deliberate false positives. `explore map`
finds 6 PII columns, 5 joins, and 5 data-quality findings; `explore query "select
email from customers"` is refused, and the same count over the same column is not.

The generation is create-only: it writes a new file and refuses rather than replace
one, with no confirmation flag that can talk past that, and it will not write a
`.dex/config.yml` where a project already has one. `generate_demo_warehouse` is
public, so the Python API can build the same fixture.

## Two surfaces, one engine

### The Python API

```python
from exmergo_dex_core import DexEngine

with DexEngine(connector="duckdb", path="shop.duckdb") as eng:
    mapped = eng.map()
    rows = eng.query("select status, count(*) from orders group by status")
    print(eng.diagram().mermaid)  # the map as a Mermaid ER diagram
```

Methods return domain objects (`DexCache`, `Dataset`, `Snapshot`) and result
records carrying the counts, notes, and warnings that explain them. The stdout
envelope never crosses this boundary.

Nothing above touches disk. The default store keeps state in the process, so
importing this package cannot leave a `.dex/` directory in a consumer's repo;
pass `store=` for anything durable, or use `DexEngine.from_repo(repo_root)` to get
the CLI's behavior (config read from `.dex/config.yml`, and the backend that
config selects, which defaults to plain files under `.dex/`). The `Store` protocol
is public, so a host can back state with its own session store or database
instead, and a backend published as its own package is selectable by name from
`cache.backend` without a change to dex. See
[`references/storage.md`](../../references/storage.md).

[`examples/quickstart.py`](examples/quickstart.py) is the whole flow in one
runnable file: map a warehouse, read the inferred joins and the data-quality
findings, see PII flagged, ask a question, and watch the firewall refuse one it
should. It generates the same demo warehouse `dex demo` builds, into a throwaway
directory, so it runs anywhere and reports the same findings:

```
pip install "exmergo-dex-core[duckdb]"
python quickstart.py
```

The test suite runs that file against a freshly built wheel, so the usage
documented here is the usage that is verified.

Every guarantee below holds here too, because it is the same code. An
unconfirmed billed call raises `ConfirmationRequiredError` carrying the estimate
and the payload needed to re-issue; an over-ceiling one raises `OverCeilingError`
and cannot be confirmed through.

Three rules matter the moment a process serves more than one user, and all three
are in `DexEngine`'s docstring: scope one engine to one principal and one session,
know that an engine given an explicit `config=` never reads one from disk (so a
stray `.dex/config.yml` above the working directory cannot silently supply someone
else's connector, budget, or PII overrides), and supply the connection when the
request's identity is not the container's.

That last one is what makes per-end-user access control expressible. By default
dex discovers the credential from process-ambient state, which is right for one
person at a terminal and process-wide everywhere else. Pass a `ConnectionSource`
and the host owns authentication:

```python
from exmergo_dex_core import ConnectionSource, DexEngine

with DexEngine(
    connector="snowflake",
    config=cfg,
    store=store,
    connection=ConnectionSource(connect=lambda: user_conn),
) as eng:
    eng.inventory()
```

It is a zero-argument factory rather than a live connection, so a free metadata
command never opens a billed session. Two things stay dex's. The cost gate is
still built here from your `store`, so the per-command ceiling and the cumulative
session ceiling bind exactly as they do on a discovered connection; handing that
to an integrator would let a fumbled figure disarm the brake in the deployment
where a runaway agent loop costs the most. And dex closes nothing it reached
through the source, because the caller that opened a connection is the one still
holding it. Nothing is persisted either way: dex never stores, caches, or
refreshes a credential.

A hosted dbt Cloud Semantic Layer is a second service with its own credential, so
it has its own parameter. Non-secret coordinates go in the config, where they can
be committed; the service token never can, so it arrives separately:

```python
from exmergo_dex_core import DexConfig, DexEngine, SemanticSource

config = DexConfig(
    semantic={"backend": "dbt_cloud", "host": host, "environment_id": env_id}
)

with DexEngine(
    config=config,
    semantic_source=SemanticSource(token=lambda: token_for(user)),
) as eng:
    catalog = eng.semantic_list()
    result = eng.semantic_query("revenue", group_by=["metric_time__month"])
```

That is the one surface needing nothing on the filesystem at all: no dbt project,
no store, no connector, no credential file. The token callable runs once per
semantic command rather than once per HTTP request, so a metric query that polls
dbt Cloud while it runs costs you one token read. Note that dbt Cloud owns the
warehouse connection on this path and executes server-side, so dex's cost guard
cannot apply and every hosted result says so; the PII dimension gate still does.

### The command contract

Every subcommand prints exactly one sanitized JSON envelope to stdout and nothing
else; nothing reaches agent context except through that envelope. Credentials
never cross it, and data values cross only from profiled, PII-cleared columns,
bounded and capped by the query firewall. State persists in `.dex/`, so
subcommands are stateless and the agent orchestrates multi-step flows.

```
dex connect test --path data.duckdb
```

With nothing to point at yet, `dex demo` builds one and every command after it needs
no flags:

```
dex demo
dex connect test
```

The CLI is the API's first consumer rather than a parallel implementation: it
parses arguments, builds an engine, and wraps the result it gets back. See
[`references/command-contract.md`](../../references/command-contract.md) for the
full surface and the envelope spec.

## Status

Early and under active development; open issues on [GitHub](https://github.com/exmergo/dex)! Today the engine
runs Explore, Transform, and Maintain end to end on every connector: DuckDB,
BigQuery, Snowflake, Databricks, Amazon Redshift, Postgres, and ClickHouse
(self-hosted), through either the command contract or the Python API. One
exception, stated so it is never overclaimed: `explore semantic query --local`
renders through MetricFlow, which ships no ClickHouse renderer, so that one
capability refuses on ClickHouse by name rather than running.

### Commands

`demo`: generates a seeded local DuckDB warehouse and wires it up, so a first run
needs no warehouse and no credentials. One command, no network, and deterministic:
the row counts and column names the documentation quotes are the ones you get. It is
the only verb that creates a data file, and it is create-only by construction, so it
never opens, inspects, or replaces a warehouse you already have. The generator lives
on its own path, never through a connector, which is what keeps the read-only rule
true everywhere else.

`explore`: ranks what matters in an unfamiliar warehouse, profiles columns
selectively, flags PII, surfaces grain and data-quality warnings, infers joins
and verifies them with overlap probes (`--verify`), and executes agent-authored
ad-hoc SELECTs behind a PII-aware query firewall (`explore query`, which takes
several statements per call, or a `--sql-file`, and adjudicates each on its own),
all read-only. `explore diagram` serializes the map it built as a Mermaid
`erDiagram`, free and without opening a connection, drawing declared joins solid
and inferred joins dotted and claiming a cardinality only where the cache proved
one. It starts bare by default; with `--use-project` it reads an existing
dbt project, promoting declared `relationships` joins, honoring declared grain
and `unique` tests, and letting metric-backing models surface first in the
ranking. A repeatable `--scope` narrows the source scope per command without
writing back to `.dex/config.yml`. It also queries the dbt semantic layer
(`explore semantic list` / `query`): metric queries run either locally through
MetricFlow and dex's own cost handshake (`--local`), or against a hosted dbt Cloud
deployment (`--api`), where dbt Cloud executes server-side and every result warns
that dex's cost guard does not apply there.

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

Every connector below discovers its own credentials and never asks for a key or a
password, which is the right default for a CLI one person runs. A process serving
several end users supplies the connection instead (see the Python API above), and
dex still builds the cost gate, narrows scope inward only, and keeps the session
read-only. The connector extra is required either way, since each adapter reads
its driver's error types to translate refusals.

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

ClickHouse: the self-hosted analytical connector. Connects through discovered
credentials (`CLICKHOUSE_URL`, the `CLICKHOUSE_*` environment, a committed
non-secret target, or a dbt profile). Identifiers are two-part
`database.table`, because ClickHouse has no catalog level and dbt-clickhouse's
`schema:` is the ClickHouse database. Nothing is billed in dollars; the
guarded quantity is load on a server that is usually shared, so budgets are
**database-seconds** through the same confirm handshake. Estimates come from
the free, non-executing `EXPLAIN ESTIMATE`, which prices a statement after
primary-key pruning, with a `system.tables` fallback for the relations it does
not cover; the budget is hard-enforced anyway by a per-statement
`max_execution_time` **and** `max_bytes_to_read`, since time alone is checked
only at block boundaries. Settlement is free and exact: every response carries
the server's own elapsed time, so the ledger records what the server spent
rather than what the client waited. The session sends `readonly = 2` and
`allow_ddl = 0` on every statement, and dbt builds go to a dedicated dev
database via dbt-clickhouse, which the `[clickhouse]` extra carries, with the
ceiling injected through the profile's `custom_settings`. ClickHouse Cloud
bills compute-unit-hours, which dex does not yet model, and is refused at
connect rather than guarded in the wrong unit. See
[`references/clickhouse.md`](../../references/clickhouse.md).

## License

Apache-2.0.
