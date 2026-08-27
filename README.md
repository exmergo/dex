<img width="1280" height="563" alt="exmergo-dex-showcase" src="https://github.com/user-attachments/assets/9dd574c2-8598-47bc-ae90-7d5a3a4d2e18" />

**Built by [Exmergo](https://exmergo.com)** · AI Agents for Your Data Stack.

[![PyPI](https://img.shields.io/pypi/v/exmergo-dex-core?logo=pypi&logoColor=white&color=165dfc)](https://pypi.org/project/exmergo-dex-core/)
[![License](https://img.shields.io/badge/license-Apache--2.0-165dfc)](LICENSE)
[![data-eng-bench](https://img.shields.io/badge/data--eng--bench-57%25-33cf56)](benchmarks/data-eng-bench/README.md)
[![ADE-bench](https://img.shields.io/badge/ADE--bench-76%25-33cf56)](benchmarks/ade_bench/README.md)
[![CI](https://github.com/exmergo/dex/actions/workflows/ci.yml/badge.svg)](https://github.com/exmergo/dex/actions/workflows/ci.yml)

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Exmergo-165dfc?logo=linkedin&logoColor=white)](https://www.linkedin.com/company/exmergo/)
[![X](https://img.shields.io/badge/Follow-%40exmergo-165dfc?logo=x&logoColor=white)](https://x.com/exmergo)

## Install (Any Agent)

Run this command in your terminal
```
npx skills add exmergo/dex
```

## Install (Claude Code)

Run these commands **inside Claude Code** one at a time
```
/plugin marketplace add exmergo/exmergo-agent-plugins
```
```
/plugin install dex@exmergo
```

Update later with `/plugin marketplace update exmergo`. The skills appear as
`/dex:explore`, `/dex:transform`, and `/dex:maintain` and auto-trigger on matching
intent.

## `dex`: the agent-native analytics engineering toolkit

**`dex` is analytics engineering** for Claude Code and **any agent**: **data warehouse
exploration**, **dbt transformation** and **semantic modeling**, and **schema-drift
maintenance** on dbt. Point it at your warehouse (or a local DuckDB file, or the one
`dex demo` generates for you) and your
dbt project; it learns the landscape, writes and refactors your dbt transformations
and semantic models, and tells you what to fix when anything drifts. The dbt
project is the source of truth; every change is a reviewable diff. Read-only
against your data.

**It closes the gap a general coding agent still has**: agents re-learn the schema
each session, have no strategy for thousands of tables, are blind to warehouse
cost, will pull sensitive data into context, do not treat a dbt project as a
first-class object, and have no concept of a semantic model to keep coherent over
time. `dex` owns exactly that loop.

## The loop

**Explore. Transform. Maintain. (ETM)**

- **Explore** an unfamiliar warehouse: rank what matters, profile selectively,
  infer and verify joins, answer ad-hoc questions with guarded SQL probes behind
  a PII-aware query firewall, read the semantic layer as the object graph it is
  (semantic models, metrics with their composition, measures, dimensions, and the
  declared join graph) and query its metrics (locally via
  MetricFlow or against a hosted dbt Cloud deployment), and render the map as a
  Mermaid ER diagram that never claims a cardinality the data has not proven.
  Persist a draft map. Fully read-only.

<img width="522" height="343" alt="image" src="https://github.com/user-attachments/assets/7f16b370-66ed-4596-ae01-041cf3db3525" />

  
- **Transform** the dbt project: author dbt models (staging to marts) with tests
  and docs, and the semantic layer on top (entities, dimensions, measures,
  metrics) as dbt semantic models (MetricFlow YAML), with a free Viz preview.
  Validated against a dev target, cost-guarded.

<img width="504" height="271" alt="image" src="https://github.com/user-attachments/assets/fda40e48-b481-424c-adc7-d79c0ede346b" />

  
- **Maintain** the project as it drifts: diff the warehouse and dbt against the
  last snapshot, surface schema, volume, grain, and definition drift ranked by
  blast radius, and propose edits.

<img width="484" height="344" alt="image" src="https://github.com/user-attachments/assets/ff714eaf-f0b2-46d6-8a4b-c69791740f18" />

## Try it in three commands, on your laptop

No warehouse, no credentials, no cloud account, no network. `dex demo` generates a
small e-commerce DuckDB warehouse locally and points the following commands at it.

```
pip install "exmergo-dex-core[duckdb]"
dex demo
dex explore map
```

`dex demo` writes two files in the directory you are standing in, and refuses rather
than overwrite anything: `dex_demo.duckdb` (7 tables, 29,512 rows) and a
`.dex/config.yml` so everything after it runs with no flags. The data is generated
from a pinned seed, so what you see is what is written here.

It is seeded to be **realistically broken**, because a first run that reports a clean
bill of health teaches you nothing. `explore map` flags 6 columns as personal data,
infers 5 joins, and reports 5 data-quality findings. Then:

```
dex explore profile order_items products
dex explore relationships --verify
dex explore query "select email from customers"
```

- **A broken grain.** `order_item_id is not unique: 13000 distinct over 14000 rows`,
  because a batch was loaded twice. Any join on it silently fans out.
- **A key that mixes id schemes.** `sku` is `90% numeric, 10% 32-character
  hexadecimal (md5-shaped)`, from a merged catalogue. Cast it to a number and you
  drop 10% of your rows without an error.
- **A join that looks right and is not.** `web_events.customer_id` shares the CRM's
  column name and type, so it is inferred; verification finds **100% of values have
  no match**, so the inference collapses instead of shipping a join that returns all
  NULLs and looks like it worked.
- **A refusal.** The query firewall declines to project `customers.email` into
  context. `select count(distinct email) from customers` runs, because a statistic
  is not a value.
- Plus a table an interrupted load left empty, two columns whose declared type
  contradicts their content, and two PII **false positives** on a distribution
  centre's city and coordinates, which are a designed behaviour and worth meeting
  early.

DuckDB is free and local, so nothing here asks you to confirm a spend. On BigQuery
or Snowflake the same commands return an estimate first and run only once you agree
to it.

## Prerequisite: `uv`

dex installs and runs its engine through [`uv`](https://docs.astral.sh/uv/), so you
need it on your `PATH` before either install below. Neither Claude Code nor the
plugin installs it for you.
```
curl -LsSf https://astral.sh/uv/install.sh | sh
```
`brew install uv` and `pipx install uv` work too. Nothing else is required: `uv`
supplies the Python and the engine, with the connector extra chosen for you at
runtime.

## Benchmarks

We run `dex` on two public analytics-engineering benchmarks. Every run's raw
per-task results are committed, including the ones that flatter us least.

### data-eng-bench (Snowflake, 103 tasks)

Each task hands the agent a **2,356-model dbt project on a 489 MB DuckDB
warehouse** and a prescriptive ticket, then runs a hidden pytest suite after a
cold `dbt run`. Scoring is binary and total: one failed assertion is a zero.

`dex` + **Claude Sonnet 5** resolves **59 of 103 tasks (57.3%)**, with dex firing
on **98% of trials**. That is nominally the highest published Sonnet 5 figure and
statistically indistinguishable from the 56.6% Snowflake published for both Claude
Code and their own CoCo harness, since 0.7 points on 103 tasks is less than one
task. Read it as parity, not as a win.

Because the reward is all-or-nothing, we also publish assertion-level results:
**89.7% of assertions pass** (task-weighted), and 19 of the 44 unresolved tasks
missed by exactly one assertion. Full methodology and the per-check record are in
the [data-eng-bench README](benchmarks/data-eng-bench/README.md).

### ADE-bench (dbt Labs, 75 tasks)

Fix, build, and extend dbt projects on DuckDB. `dex` + **Claude Sonnet 5** reaches
**76% task resolution**, at **2.5x lower cost than Claude Fable 5**.

<img width="719" height="283" alt="image" src="https://github.com/user-attachments/assets/9f8bca64-6508-4590-9fa7-bb1ac077263d" />


With `dex`, accuracy clusters tightly across models (72-76%) while cost does not,
so you can run an inexpensive model and still get top-tier results. One honest
caveat on this one: dex was actually invoked in only 15 of the 75 Sonnet 5 trials,
and on those 15 it netted a single extra task, so the 76% is mostly a statement
about the model rather than about `dex`. Per-model cost and the raw `results.json`
for every run are in the [ADE-bench README](benchmarks/ade_bench/README.md).

### On benchmarks

We publish these to be transparent, not to overclaim. A task-resolution score
measures whether tests pass; it does not measure what matters most in practice:
the experience of the human engineer working with the agent. Trust in a diff,
clarity of the proposed change, cost surfaced before spend, and sensitive data
kept out of context never show up in a pass rate. We optimize for that
experience first and treat these scores as guide posts, not as the goal.

Two habits follow from that. We report **how often `dex` actually ran** next to
the accuracy, because a score with the tool firing on 98% of trials and the same
score with it firing on 20% are different claims and only the first says anything
about `dex`. And we do not turn a gap smaller than the noise into a headline: a
single run, one attempt per task, tells you roughly where a setup stands, not that
it is better than the one a point below it.

## Connectors

- Cloud warehouse: **Snowflake**, **BigQuery**, **Databricks**, **Amazon Redshift** (Serverless-first).
- Self-hosted analytical: **ClickHouse**.
- Embedded analytical: **DuckDB**.
- Operational database: **Postgres**.

<img width="1162" height="225" alt="image" src="https://github.com/user-attachments/assets/32d2311b-b85e-41a5-8431-4edb1f928346" />

Credentials are discovered, never asked for: BigQuery through Application
Default Credentials (`gcloud auth application-default login`), Snowflake
through `connections.toml`, `SNOWFLAKE_*` env, or a dbt profile, Databricks
through the SDK's unified chain (`databricks auth login`, `DATABRICKS_*` env,
or a dbt profile), Redshift through the AWS credential chain (a pinned
Serverless workgroup mints IAM temporary database credentials) or `REDSHIFT_*`
env, Postgres through `pg_service.conf`, `DATABASE_URL`, the `PG*`
environment, or a dbt profile, ClickHouse through `CLICKHOUSE_URL`, the
`CLICKHOUSE_*` environment, or a dbt profile. Every scan is estimated and
confirmed before it spends, capped server-side (`maximum_bytes_billed` on
BigQuery; a per-statement statement timeout on Snowflake, Databricks, Redshift,
and Postgres; `max_execution_time` plus `max_bytes_to_read` on ClickHouse,
whose budgets are warehouse-seconds with credits or DBUs alongside,
compute-seconds with RPU-hours alongside, and database-seconds for the last
two), and recorded in a local spend ledger.

The two self-hosted connectors bill no dollars, and dex still gates them: an
unbounded scan on a production Postgres primary or a shared ClickHouse cluster
is a real cost even when nothing appears on an invoice.

### Upcoming Connectors

- Cloud warehouse: **Trino**, **Azure Synapse**, **Microsoft Fabric**, **ClickHouse Cloud**

## The `exmergo-dex-core` package

`dex` also bundles the `exmergo-dex-core` Python package.  
This is the reusable and agent-friendly package that contains all the core
explore, transform, and maintain logic. This also holds connectors and the
write logic for .dex/ which stores cache, snapshots, and query billing logs. 

You can install it yourself in your projects:

```
pip install exmergo-dex-core
```

or

```
uv add exmergo-dex-core
```

### Use it as a library

The engine has a programmatic API, so you can drive the same loop from Python
instead of shelling out to the CLI and parsing JSON:

```python
from exmergo_dex_core import DexEngine

with DexEngine(connector="duckdb", path="shop.duckdb") as eng:
    mapped = eng.map()                  # a DexCache, not a JSON blob
    rows = eng.query("select status, count(*) from orders group by status")

print(mapped.relationship_count, rows.cells)
```

Methods return domain objects and result records, never the CLI's stdout
envelope. Nothing above writes to disk: the default store keeps state in the
process, so importing the package cannot leave a `.dex/` directory in your repo.
`DexEngine.from_repo("path/to/repo")` opts in to a project on disk, which is exactly
what the CLI itself does.

Every guarantee the CLI makes holds here too, because it is the same code: reads
are read-only, cost is surfaced before any spend (an unconfirmed billed call
raises `ConfirmationRequiredError` carrying the estimate), and PII stays flagged
rather than surfaced.

A process serving more than one end user can pass a `ConnectionSource` so each
request reaches the warehouse as its own principal rather than as the container,
and a `SemanticSource` to do the same for a hosted dbt Cloud Semantic Layer token.
The host owns authentication; dex still builds the cost gate from your store, so
the session budget binds either way.

More info in the package's [`README.md`](packages/dex-core/README.md)

## Agent References

- Cross-agent contract: [`AGENTS.md`](AGENTS.md).
- References (connectors, the contract, the canonical model, evaluation):
  [`references/`](references/).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup, the Ruff lint and
format workflow, and the pre-commit hook. Every pull request into `main` must
pass the Lint workflow and CI before it can merge.

## Community

Connect with the Analytics Engineering Community (Data Engineers welcome as well!) 
and discover how Exmergo brings AI Agents to Your Data Stack.

- 🌟 [Star Us on GitHub](https://github.com/exmergo/dex/)
- 🔗 [Follow Us on LinkedIn](https://www.linkedin.com/company/exmergo/)
- 🐦 [Follow Us on Twitter](https://x.com/exmergo)
- 🔨 [Follow Us on GitHub](https://github.com/exmergo/)

## License

Apache-2.0.
