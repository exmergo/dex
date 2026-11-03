# exmergo-dex-core

**Analytics engineering for your coding agent, with guardrails and lifecycle built into the engine.**

Dex helps an agent understand a warehouse, build dbt models and a semantic layer,
and keep them coherent as the data changes. `exmergo-dex-core` is the Python
engine behind [Dex](https://github.com/exmergo/dex), built by
[Exmergo](https://www.exmergo.com). It provides both the `dex` CLI and a typed
Python API. The agent skills call this same engine; the rules live in code.

- **Explore:** rank tables, find their grain, flag personal data, verify joins,
  and answer questions through a query firewall.
- **Transform:** author and refactor dbt models, tests, documentation, and
  semantic definitions as reviewable plans. Build on a dev target.
- **Maintain:** detect schema, volume, grain, and semantic drift against a
  saved baseline, then propose the edits that reconcile it.

[Documentation](https://docs.exmergo.com/dex) ·
[Source](https://github.com/exmergo/dex) ·
[Changelog](https://github.com/exmergo/dex/blob/main/CHANGELOG.md) ·
[Issues](https://github.com/exmergo/dex/issues)

## Try it in three commands

Requires Python 3.11 or later; Python 3.11–3.14 are supported. Use pip 26.2.2+
or uv 0.13.4+ to install.

```bash
pip install "exmergo-dex-core[duckdb]"
dex demo
dex explore map
```

After installation, this runs locally with no credentials, cloud account, or
network. Run it in a scratch directory: `dex demo` creates `dex_demo.duckdb`
(7 e-commerce tables, 29,512 rows) and `.dex/config.yml`. It refuses to overwrite
an existing warehouse and leaves an existing project config untouched.

The demo has defects on purpose. The map reports 5 inferred joins, 6 PII-flagged
columns, and 6 data-quality findings. For example, a double-loaded batch makes
`order_items.order_item_id` unique for only 92.9% of rows. Dex reports the
duplicates and leaves the grain unknown.

Dig into the evidence:

```bash
dex explore profile order_items products
dex explore relationships --verify
dex explore query "select status, count(*) as orders from orders group by status order by status"
```

Verification also catches a misleading join: `web_events.customer_id` shares a
name with the CRM key, but none of its values match. Try the query firewall:

```bash
dex explore query "select email from customers"
dex explore query "select count(distinct email) as customers from customers"
```

The first query is refused because it projects a blocking PII column. The second
returns a count. Each command prints one JSON envelope, including refusals.
DuckDB is free and local; scans on other connectors first return a cost estimate.

## Why an engine

A coding agent can write SQL. It still needs a way to choose useful tables from
thousands, establish keys rather than guess them, control scan costs, and keep
definitions consistent across sessions.

Dex supplies that process and persists its working state in `.dex/`. Your
repository remains the source of truth for two independent layers: the dbt
transformation project and the semantic layer. Ossie and SLayer repositories can
have a semantic layer without a dbt project.

The CLI and Python API run the same implementation. Source reads pass through
the SQL and cost guards; project changes go through plans and diffs; warehouse
builds are confined to dev targets.

## Install for your warehouse

Install the extra for your connector. It includes the warehouse client, SQL
parser, and dbt adapter needed for the full workflow:

```bash
pip install "exmergo-dex-core[snowflake]"
# Or: uv add "exmergo-dex-core[snowflake]"
```

| Connector / extra | Credentials discovered from | Scan budget unit |
| --- | --- | --- |
| [DuckDB](https://docs.exmergo.com/dex/connectors/duckdb) / `duckdb` | Local file path; no credentials | Free and local |
| [Snowflake](https://docs.exmergo.com/dex/connectors/snowflake) / `snowflake` | `connections.toml`, `SNOWFLAKE_*`, or a dbt profile | Warehouse-seconds, with credits |
| [BigQuery](https://docs.exmergo.com/dex/connectors/bigquery) / `bigquery` | Application Default Credentials | Bytes scanned |
| [Databricks](https://docs.exmergo.com/dex/connectors/databricks) / `databricks` | SDK unified auth, `DATABRICKS_*`, or a dbt profile | Warehouse-seconds, with DBUs |
| [Amazon Redshift](https://docs.exmergo.com/dex/connectors/redshift) / `redshift` | AWS credential chain, `REDSHIFT_*`, or a dbt profile | Compute-seconds, with RPU-hours |
| [Postgres](https://docs.exmergo.com/dex/connectors/postgres) / `postgres` | `pg_service.conf`, `DATABASE_URL`, `PG*`, or a dbt profile | Database-seconds |
| [ClickHouse](https://docs.exmergo.com/dex/connectors/clickhouse) / `clickhouse` | `CLICKHOUSE_URL`, `CLICKHOUSE_*`, or a dbt profile | Database-seconds; compute-seconds on Cloud |
| [Trino](https://docs.exmergo.com/dex/connectors/trino) / `trino` | `TRINO_URL`, `TRINO_*`, or a dbt profile | Cluster-seconds |

ClickHouse supports self-hosted and Cloud deployments. Trino supports
self-hosted clusters, Starburst Enterprise, and Trino on EMR; Starburst Galaxy
is currently refused.

Additional capabilities have their own extras:

| Extra | Use it for |
| --- | --- |
| `semantic` | Local dbt metric queries through MetricFlow, and schema validation when authoring dbt semantic YAML |
| `semantic-api` | Hosted dbt Cloud Semantic Layer discovery and queries |
| `ossie` | Reading, validating, authoring, and maintaining native Apache Ossie documents |
| `slayer` | SLayer model files, validated against one pinned SLayer release |
| `cube` | Reading and authoring plain-YAML Cube model files, with no Node and no Cube server |
| `cluster` | Bounded k-means exploration with `explore cluster` |
| `all` | Every connector and optional capability above; a substantially larger install |

Combine extras as needed, for example `exmergo-dex-core[duckdb,semantic]`.
A bare `pip install exmergo-dex-core` provides the API and configuration tools
without a warehouse client. Hosted dbt Cloud and repository-only Ossie workflows
need no connector extra. Local dbt catalog discovery can read compiled artifacts
without MetricFlow; local metric execution needs `semantic`.

### Point Dex at an existing project

Run commands from your project directory. For an existing DuckDB file, create
`.dex/config.yml` with:

```yaml
connector: duckdb
duckdb:
  path: /absolute/path/to/shop.duckdb
```

Then check the configuration and connection before scanning:

```bash
dex config validate
dex connect test
dex explore inventory --rank
```

For a one-off connection check, use `dex connect test --path shop.duckdb`.
Warehouse connectors take `--connector` and repeatable `--scope` flags; scope
can narrow a configured source allowlist, never widen it. Follow the connector
links above for target settings and authentication setup. Credentials belong in
your environment or credential provider, never in `.dex/config.yml`.

`config get` explains effective settings and their sources. `config set` and
`config unset` propose a diff and write only with `--confirm`, preserving comments
and key order. These commands work without opening a warehouse connection.

## Use the Python API

Drive the same workflow without parsing CLI output. This example creates the
demo in a temporary directory and keeps engine state in memory:

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from exmergo_dex_core import DexEngine, generate_demo_warehouse

with TemporaryDirectory() as directory:
    warehouse = generate_demo_warehouse(Path(directory) / "shop.duckdb")
    with DexEngine(connector="duckdb", path=str(warehouse.path)) as engine:
        mapped = engine.map()
        for dataset in mapped.cache.datasets:
            print(dataset.identifier, dataset.grain, dataset.data_quality)

        result = engine.query(
            "select status, count(*) as orders from orders "
            "group by status order by status"
        )
        print(result.columns, result.cells)
        print(engine.diagram().mermaid)
```

Methods return typed result records containing domain objects such as `DexCache`,
`Dataset`, and `Snapshot`, plus notes, warnings, and cost information.
`map()` returns a `MapResult`; its `.cache` holds the datasets and relationships.
The CLI envelope is a transport format, separate from these Python results.

The default `MemoryStore` writes no `.dex/` files. To use an existing repository's
config and durable state, opt in explicitly:

```python
with DexEngine.from_repo("/path/to/project") as engine:
    catalog = engine.semantic_list()
    checked = engine.verify()
```

An explicit `config=DexConfig(...)` is never supplemented from disk. Pass
`store=` to supply durable storage yourself. An unconfirmed billed call raises
`ConfirmationRequiredError` carrying the estimate; exceeding a ceiling raises
`OverCeilingError`. The engine enforces the same guards as the CLI.

The [runnable Python quickstart](https://github.com/exmergo/dex/blob/main/packages/dex-core/examples/quickstart.py)
also demonstrates PII refusals and cache reuse. It is checked against the built
wheel in the package's tests.

## The workflow

### Explore: establish what the data means

Start with a ranked inventory, profile the useful tables, then measure the joins:

```bash
dex explore inventory --rank --limit 20
dex explore profile orders customers
dex explore relationships --verify
dex explore map --use-project
dex explore diagram
```

Profiles report row counts, nulls, ranked candidate keys, grain, PII flags, and
data-quality findings. Key evidence explains why a candidate was accepted or
suppressed. `--use-project` adds declared grain, relationships, and semantic
context from the repository, while keeping declarations distinct from measured
evidence.

`map` combines inventory, selective profiling, and join inference into a saved
map. It bounds both the work and the response; skipped objects and elided fields
are counted. `diagram` reads that cache without connecting and returns Mermaid
ER text under `data.mermaid`. Declared joins are solid, inferred joins dotted,
and cardinality appears only where measured.

Use `explore query` for ad-hoc SELECTs. Results are capped and returned as
`columns`, `types`, and `cells`, with `shape: "columnar"`. An unprofiled table is
profiled on demand before its columns can pass the firewall; on a metered
connector that profiling is included in the estimate. `--no-auto-profile`
requires an existing profile instead. Pass several SQL arguments or a
`--sql-file` to batch questions; each statement gets its own verdict.

[Explore reference](https://docs.exmergo.com/dex/cli/explore)

### Transform: review, apply, then build

The agent authors SQL and YAML. Dex validates that content, computes diffs, and
stores a plan. It also generates staging scaffolds from profiled tables and
plans refactors across the project's references.

In an existing dbt project, the sequence is:

```bash
dex transform plan "add order staging" --scaffold orders
dex transform plans
dex transform apply <plan-id>
dex transform build --target dev --verify
```

Review the first command's diffs before applying the returned plan ID. Applying
writes project files; building runs dbt separately. The build above runs directly
on DuckDB. On a metered connector it first returns an estimate and needs the
cost authorization described below.

For authored changes, use `transform plan "<intent>" --edits-file edits.json`.
The payload contains an `edits` array; each edit names a project-relative `path`,
its `kind` (such as `model_sql` or `schema_yml`), and the file `content`.
Deletes use `op: "delete"`. A file changed since planning causes a conflict;
Dex withholds the plan rather than silently overwriting that edit.

Other useful entry points:

- `transform init "<name>" --connector <connector>` bootstraps a dbt project and
  dev profile when none exists.
- `transform references <name>` finds uses across SQL, YAML, macros, and packages,
  reporting any limits to its analysis.
- `transform rename`, `remove`, and `place` turn refactors into stored plans.
- `semantic define|update|plan` authors dbt semantic YAML; `semantic ossie`,
  `semantic slayer` and `semantic cube` author native documents. Apply all of them with
  `transform apply`.

[Transform reference](https://docs.exmergo.com/dex/cli/transform) ·
[Edit payloads](https://github.com/exmergo/dex/blob/main/skills/transform/references/edit-kinds.md)

### Maintain: catch assumptions that stopped being true

Save a baseline once the current state is understood. Compare future runs to it:

```bash
dex maintain snapshot
dex maintain check
dex maintain reconcile
```

Dex checks schema changes, volume shifts, broken grain, and semantic definition
drift, and ranks findings by their impact on downstream models and metrics.
`reconcile` proposes mechanical fixes and advisory changes as plans for review.
`maintain verify` checks the project as it stands without needing a baseline.

The transformation project and semantic layer are fingerprinted independently,
so a semantic-only repository can still have a baseline. Metadata checks stay
free; grain and dimension-cardinality probes need scans. `maintain check` can
return free findings with an optional paid follow-up under `data.offer`.

[Maintain reference](https://docs.exmergo.com/dex/cli/maintain)

## Semantic layers

Choose the layer in `.dex/config.yml` using `semantic.vendor` and
`semantic.deployment`. Authoring, discovery, and drift detection use that choice.

| Layer | Configuration | Discovery and execution |
| --- | --- | --- |
| dbt, local | `vendor: dbt`, `deployment: local` | Reads compiled semantic artifacts; MetricFlow renders metric SQL and Dex executes it through its connector and cost guard |
| dbt Cloud | `vendor: dbt`, `deployment: dbt_cloud` | Reads and queries the hosted Semantic Layer API; dbt Cloud owns execution |
| Apache Ossie | `vendor: ossie`, `deployment: local` | Reads native repository documents; catalog, authoring, and drift checks, with no query runtime |
| SLayer | `vendor: slayer`, `deployment: local` | Reads configured model files; SLayer renders SQL and Dex executes it through its connector and cost guard |
| Cube | `vendor: cube`, `deployment: local` | Reads and authors configured plain-YAML model files statically; catalog, authoring, and drift checks, with no query runtime yet |

Discover the catalog before choosing a metric or filter. On a dbt layer, for
example, with a declared `revenue` metric:

```bash
dex explore semantic list
dex explore semantic list --metric revenue
dex explore semantic query revenue --group-by metric_time__month
```

The catalog connects metrics to their semantic models and physical relations
where available. `--search` narrows by names and descriptions;
`--for-dimension` finds metrics that can share a grouping. Before writing a
filter, use `explore semantic values <dimension>` to discover its allowed values.
SLayer uses tokens such as `orders.revenue` and `orders.status`.

Limits are explicit: Ossie defines no portable query runtime or metric
groupability, so metric queries, dimension values, and `--for-dimension` are
refused. Hosted dbt Cloud exposes no physical relations and cannot be governed
by Dex's scan budget. Local dbt metric queries refuse on ClickHouse because
MetricFlow has no ClickHouse renderer. SLayer files must match the pinned
release's format; Dex refuses rather than migrate them implicitly.

[Semantic layer guide](https://docs.exmergo.com/dex/semantic-layers/overview) ·
[Configuration and query contract](https://github.com/exmergo/dex/blob/main/references/semantic-layer.md)

## Guardrails and their boundaries

**Source data stays read-only.** Dex validates its source SQL as SELECT-only;
agent-authored queries pass through the firewall. Confirmed dbt builds write to
dev targets only, and production-looking targets are refused. `demo` is a
separate, create-only generator. Trino has no read-only session mode, so its
server-side boundary depends on a read-only principal as well as Dex's SQL guard.

**Personal data is screened before projection.** Profiling records PII category
and confidence without example values. The firewall blocks projections at the
blocking threshold; weaker flags warn. Semantic dimension queries reject a flag
at any confidence. Detection uses column names and aggregate shape, so it cannot
identify every sensitive column. Only a human-reviewed override clears a flag.
See the [PII policy](https://docs.exmergo.com/dex/guardrails/pii-policy).

**Scans are priced and bounded.** Metered commands first return
`needs_confirmation` with an estimate. Once authorized, reissue with `--confirm`
and an approved `--budget` in the connector's unit. A per-command ceiling bounds
each call, and a cumulative daily ceiling bounds repeated calls. When the daily
ceiling is unset, Dex asks for one or an explicit decision to run without it.
Settled spend is ledgered.
BigQuery uses dry-run estimates and a bytes-billed cap. Time-based connectors
use estimates and server-side limits; ClickHouse and Trino also cap bytes read.
Self-hosted databases are gated too, because load on a production database has
a cost even without a cloud bill.

An agent may continue within budgets committed before the session. Warehouse
contents, query results, and file text cannot authorize new spend or a higher
ceiling. A host can pin `DEX_SESSION_CEILING_CAP` or the Python
`session_ceiling_cap` argument so repository changes cannot raise its bound.
Hosted dbt Cloud queries are the exception: Dex cannot estimate or cap their
execution, and every result warns about that limit.
See [cost controls](https://docs.exmergo.com/dex/guardrails/cost-controls).

**Project changes stay reviewable.** Planning writes no project changes.
Application checks the planned file state before writing. Config changes also
return diffs before confirmation. Your repository owns the definitions;
`.dex/` holds working state.

### When the model must see no table values

Values-free mode withholds all column values, including non-PII minima, maxima,
and value domains. Queries must return one row of allowlisted measuring
aggregates; grouped values and value-carrying aggregates are refused.

```bash
DEX_RESULT_MODE=values_free dex explore map
dex explore query "select count(*) as orders from orders" --values-free
```

Set `DEX_RESULT_MODE=values_free` in the host environment to pin the mode for all
invocations; `--values-free` selects it for one call. Python hosts use
`DexEngine(result_mode="values_free")`.
The repository cannot turn it off. Results carry `result_mode: "values_free"`;
unreviewed commands refuse, and warehouse errors use stable codes instead of
potentially value-bearing messages.

This mode permits counts and statistics, so it does not prevent membership
probing or inference from small populations. The
[mode contract](https://docs.exmergo.com/dex/guardrails/values-free-mode)
documents the command allowlist, residual risks, and shipped conformance suite.

## Embed Dex in a service

Scope one engine and its state to one principal and one session. For per-user
warehouse identity, pass `ConnectionSource(connect=...)`, a zero-argument factory
that returns the user's connection. Dex opens it lazily, retains its own cost
gate, and leaves caller-supplied connections for the caller to close. Keep
persisted stores isolated by the same tenancy boundary.

A hosted dbt Cloud token has its own provider:

```python
from exmergo_dex_core import DexConfig, DexEngine, SemanticSource

# host, environment_id, and token_for_user come from your application.
config = DexConfig(
    semantic={
        "vendor": "dbt",
        "deployment": "dbt_cloud",
        "host": host,
        "environment_id": environment_id,
    }
)

with DexEngine(
    config=config,
    semantic_source=SemanticSource(token=token_for_user),
) as engine:
    catalog = engine.semantic_list()
```

This path needs only `exmergo-dex-core[semantic-api]`: no warehouse connector,
local dbt project, or credential file. The token callable runs once per semantic
command. dbt Cloud owns query execution and spend.

For applications that split planning, offline application, and building across
processes, Dex can export plans with content hashes, verify a host-pinned digest,
report dependencies and executable content, and return typed build evidence.
Use that evidence to distinguish a validated change from a dbt run that merely
exited successfully.

[Host integration](https://docs.exmergo.com/dex/reference/host-integration) ·
[Storage backends](https://docs.exmergo.com/dex/reference/storage-backends) ·
[Project formats](https://docs.exmergo.com/dex/reference/project-formats)

## Drive Dex from an agent

For a coding agent, install the skills from a terminal:

```bash
npx skills add exmergo/dex
# Codex: npx skills add exmergo/dex --agent codex
```

In Claude Code, run `/plugin marketplace add exmergo/exmergo-agent-plugins`, then
`/plugin install dex@exmergo`. Skills need `uv` on `PATH` and an agent with a
local shell. Install uv yourself with `brew install uv` or `pipx install uv`;
the agent does not install it. Direct Python and `dex` use do not require uv
when the package is already installed with pip.

The skills' pinned wrappers select connector extras at runtime. To pay the cold
install cost ahead of a session or in CI, run from a checkout:

```bash
uv run --no-project --script skills/explore/scripts/run.py --warm
```

Add `--connector snowflake` to warm a named connector before config exists.
Agents without skills can follow
[AGENTS.md](https://github.com/exmergo/dex/blob/main/AGENTS.md) and call `dex` or
`python -m exmergo_dex_core` directly.

Every command emits one sanitized JSON object to stdout; diagnostics go to
stderr. Read these fields before deciding the next step:

| Field | Meaning |
| --- | --- |
| `status` | `ok`, `needs_confirmation`, `error`, or `not_implemented` |
| `data` | Command result, including notes, truncation indicators, and spend where relevant |
| `connection` | Resolved connector, non-secret target coordinates, and resolution source |
| `cost` | Estimate, ceiling, paradigm, unit, and estimate quality |
| `warnings`, `errors`, `reason` | Caveats and typed failure category; `reason` is populated for errors |
| `diffs` | Proposed file changes for review |

Check elision counts and `truncated` before treating an answer as complete.
For query batches, inspect each entry in `data.results`; individual statements
can be refused while their neighbors succeed. `data.offer` is optional further
work, whereas `needs_confirmation` means the requested operation is waiting.

[Full command and envelope contract](https://docs.exmergo.com/dex/reference/command-contract)

## What to keep in git

Commit your project and semantic definitions. Treat `.dex/` as working state,
and check whether an artifact can contain warehouse values before publishing it.

| Artifact | Contents and commit guidance |
| --- | --- |
| `.dex/config.yml` | Non-secret targets, limits, and reviewed PII overrides. Commit it; credentials are refused. |
| `.dex/snapshot.json` | Drift baseline, useful to version like a lockfile. A snapshot from a populated cache can include non-PII value domains and min/max; review before publishing. A metadata-only baseline has schema and volume only. |
| `.dex/cache.json` | Profiles, joins, and non-PII value domains. Keep private unless those values may be public. |
| `.dex/dex.db` | Optional SQLite state backend; treat it with the same caution as the file cache. |
| `.dex/plans/*.json` | Proposed project file contents; optional audit trail. |
| `.dex/queries.jsonl` | SQL ledger; query literals may contain values. Usually ignore. |
| `.dex/drift.json` | Generated drift report; regenerate rather than commit. |
| `.dex/spend.jsonl` | Local spend ledger with counts, job IDs, and hashes; no SQL values or credentials. Usually ignore. |

For a repository that must not publish warehouse values, start with:

```gitignore
.dex/cache.json
.dex/dex.db
.dex/dex.db-journal
.dex/queries.jsonl
.dex/drift.json
.dex/spend.jsonl
.dex/spend.lock
```

Review `snapshot.json` separately; ignoring the cache does not remove values
already copied into a baseline.

## Examples, benchmarks, and support

[github-repo-analytics](https://github.com/exmergo/github-repo-analytics) is a
working analytics project whose models and metrics are built and maintained
with Dex. Its repository includes an audit trail of the engine's decisions.

The repository publishes raw runs for two analytics-engineering benchmarks:
[data-eng-bench](https://github.com/exmergo/dex/blob/main/benchmarks/data-eng-bench/README.md)
and [ADE-bench](https://github.com/exmergo/dex/blob/main/benchmarks/ade_bench/README.md).
Those reports describe the setup, results, and limitations.

The community-maintained [dagster-dex](https://github.com/catincloud-labs/dagster-dex)
integration lets Dex explore and maintain a Dagster asset graph through the
project-format interface.

[Dex Enterprise](https://www.exmergo.com/dex/enterprise) runs the workflow in your
own CI, turning tickets and scheduled drift checks into draft pull requests.
Everything in this repository remains Apache-2.0.

For setup and command details, use the [documentation](https://docs.exmergo.com/dex).
Report bugs or request connectors on [GitHub](https://github.com/exmergo/dex/issues),
or join the [community](https://www.exmergo.com/discord). To contribute, see
[CONTRIBUTING.md](https://github.com/exmergo/dex/blob/main/CONTRIBUTING.md)
and the [code of conduct](https://github.com/exmergo/dex/blob/main/CODE_OF_CONDUCT.md).

Apache-2.0. See the [license](https://github.com/exmergo/dex/blob/main/LICENSE)
and [NOTICE](https://github.com/exmergo/dex/blob/main/NOTICE). Exmergo's
distribution, documentation, support, and trademarks are covered by the
[terms of service](https://www.exmergo.com/terms/dex), and Dex sends Exmergo
nothing, as the [privacy policy](https://www.exmergo.com/privacy/dex) explains.
