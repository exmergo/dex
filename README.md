<img width="1280" height="563" alt="exmergo-dex-showcase" src="https://github.com/user-attachments/assets/9dd574c2-8598-47bc-ae90-7d5a3a4d2e18" />

*Developed by Exmergo*

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
- **Transform** the dbt project: author dbt models (staging to marts) with tests
  and docs, and the semantic layer on top (entities, dimensions, measures,
  metrics) as dbt semantic models (MetricFlow YAML), with a free Viz preview.
  Validated against a dev target, cost-guarded.
- **Maintain** the project as it drifts: diff the warehouse and dbt against the
  last snapshot, surface schema, volume, grain, and definition drift ranked by
  blast radius, and propose edits.

## Connectors

- Cloud warehouse: **Snowflake**, **BigQuery**.
- Embedded analytical: **DuckDB**.

Cloud credentials are discovered, never asked for: BigQuery through
Application Default Credentials (`gcloud auth application-default login`),
Snowflake through `connections.toml`, `SNOWFLAKE_*` env, or a dbt profile.
Every scan is estimated and confirmed before it spends, capped server-side
(`maximum_bytes_billed` on BigQuery; a per-statement statement timeout on
Snowflake, where budgets are warehouse-seconds with credits shown alongside),
and recorded in a local spend ledger.

### Upcoming Connectors

- Cloud warehouse: **Databricks**, **AWS Redshift**
- Operational database: **PostgreSQL**

## The `exmergo-dex-core` package

`dex` also bundles the `exmergo-dex-core` Python package.  
This is the reusable and agent friendly package through which `dex` runs
its commands.

You can install it yourself in your projects

1. pip
```
pip install exmergo-dex-core
```

2. uv
```
uv add exmergo-dex-core
```

More info in the package's [`README.md`](packages/dex-core/README.md)

## Agent References

- Engine: `packages/dex-core/` (PyPI: `exmergo-dex-core`, Apache-2.0).
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
