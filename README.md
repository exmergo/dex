<img width="1280" height="563" alt="exmergo-dex-showcase" src="https://github.com/user-attachments/assets/9dd574c2-8598-47bc-ae90-7d5a3a4d2e18" />

**Built by [Exmergo](https://exmergo.com)** · AI Agents for Your Data Stack.

[![PyPI](https://img.shields.io/pypi/v/exmergo-dex-core?logo=pypi&logoColor=white&color=165dfc)](https://pypi.org/project/exmergo-dex-core/)
[![License](https://img.shields.io/badge/license-Apache--2.0-165dfc)](LICENSE)
[![ADE-bench](https://img.shields.io/badge/ADE--bench-76%25-33cf56)](benchmarks/ade_bench/README.md)
[![CI](https://github.com/exmergo/dex/actions/workflows/ci.yml/badge.svg)](https://github.com/exmergo/dex/actions/workflows/ci.yml)

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Exmergo-165dfc?logo=linkedin&logoColor=white)](https://www.linkedin.com/company/exmergo/)
[![X](https://img.shields.io/badge/Follow-%40exmergo-165dfc?logo=x&logoColor=white)](https://x.com/exmergo)

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

## Install (Any Agent)

Run this command in your terminal
```
npx skills install exmergo/dex
```

## `dex`: the agent-native analytics engineering toolkit

**`dex` is analytics engineering** for Claude Code and **any agent**: **data warehouse
exploration**, **dbt transformation** and **semantic modeling**, and **schema-drift
maintenance** on dbt. Point it at your warehouse (or a local DuckDB file) and your
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
  a PII-aware query firewall, persist a draft map. Fully read-only.

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


## Benchmark

On ADE-bench (75 analytics-engineering tasks: fix, build, and extend dbt
projects on DuckDB), `dex` reaches **76% task resolution with Claude Sonnet 5**,
at **2.5x lower cost than Claude Fable 5**.

<img width="719" height="283" alt="image" src="https://github.com/user-attachments/assets/9f8bca64-6508-4590-9fa7-bb1ac077263d" />


With `dex`, accuracy clusters tightly across models (72-76%) while cost does not,
so you can run an inexpensive model and still get top-tier results. Full
methodology, per-model cost, and the raw `results.json` for every run are in the
[benchmark README](benchmarks/ade_bench/README.md).

### On benchmarks

We publish these to be transparent, not to overclaim. A task-resolution score
measures whether tests pass; it does not measure what matters most in practice:
the experience of the human engineer working with the agent. Trust in a diff,
clarity of the proposed change, cost surfaced before spend, and sensitive data
kept out of context never show up in a pass rate. We optimize for that
experience first and treat the benchmark as a floor, not the goal.

## Connectors

- Cloud warehouse: **Snowflake**, **BigQuery**, **Databricks**.
- Embedded analytical: **DuckDB**.
- Operational database: **Postgres**.

<img width="840" height="153" alt="image" src="https://github.com/user-attachments/assets/82962530-4551-4d5b-b3b5-ae5ad1026f4d" />


Credentials are discovered, never asked for: BigQuery through Application
Default Credentials (`gcloud auth application-default login`), Snowflake
through `connections.toml`, `SNOWFLAKE_*` env, or a dbt profile, Databricks
through the SDK's unified chain (`databricks auth login`, `DATABRICKS_*` env,
or a dbt profile), Postgres through `pg_service.conf`, `DATABASE_URL`, the
`PG*` environment, or a dbt profile. Every scan is estimated and confirmed
before it spends, capped server-side (`maximum_bytes_billed` on BigQuery; a
per-statement statement timeout on Snowflake, Databricks, and Postgres, whose
budgets are warehouse-seconds with credits or DBUs alongside and
database-seconds respectively), and recorded in a local spend ledger.

### Upcoming Connectors

- Cloud warehouse: **AWS Redshift**, **Microsoft Fabric**

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
