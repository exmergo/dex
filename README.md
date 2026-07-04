# dex: the agent-native analytics engineering toolkit

**Explore. Transform. Maintain. (ETM)**

dex is analytics engineering for Claude Code and any agent: data warehouse
exploration, dbt transformation and semantic modeling, and schema-drift
maintenance on dbt. Point it at your warehouse (or a local DuckDB file) and your
dbt project; it learns the landscape, writes and refactors your dbt transformations
and semantic models, and tells you what to fix when anything drifts. The dbt
project is the source of truth; every change is a reviewable diff. Read-only
against your data.

It closes the gap a general coding agent still has: agents re-learn the schema
each session, have no strategy for thousands of tables, are blind to warehouse
cost, will pull sensitive data into context, do not treat a dbt project as a
first-class object, and have no concept of a semantic model to keep coherent over
time. dex owns exactly that loop.

## The loop

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

## Install (Claude Code)

```
/plugin marketplace add exmergo/exmergo-agent-plugins
/plugin install dex@exmergo
```

Update later with `/plugin marketplace update exmergo`. The skills appear as
`/dex:explore`, `/dex:transform`, and `/dex:maintain` and auto-trigger on matching
intent.

## Connectors

Cloud warehouse: **BigQuery** (live), **Snowflake**, **Databricks**. Operational
database: **PostgreSQL**. Embedded analytical: **DuckDB** (the zero-credential
on-ramp, and the engine behind the eval and benchmark suites). Each client library
is behind an optional extra, so the DuckDB on-ramp installs only `duckdb` and
`sqlglot`; BigQuery installs with `exmergo-dex-core[bigquery]`. To pull every
connector at once, install `exmergo-dex-core[all]`.

BigQuery authenticates through Application Default Credentials (run
`gcloud auth application-default login`); dex discovers credentials, it never
asks for keys. Every scan is estimated with a free dry-run and confirmed before
it spends, capped server-side with `maximum_bytes_billed`, and recorded in a
local spend ledger.

## Status

**v0.1 is the full ETM loop on DuckDB**, with no cloud credentials required:
explore, transform, and now **maintain** (drift detection and reconcile across
schema, volume, grain, and semantic axes). **Explore, transform, and maintain
also run on BigQuery**, the first cloud connector: credential discovery via ADC,
bytes-scanned cost guards with a confirm-before-spend handshake, and
dev-dataset-only dbt builds via dbt-bigquery. Snowflake, Databricks, and
PostgreSQL land next as v0.2 completes; published benchmark scores (ADE-bench
uplift and cost/turn efficiency, Spider2.0-DBT) land with v0.3.

## Beyond Claude Code

The engine is portable. `AGENTS.md` documents how any coding agent (Codex, Gemini
CLI, Cursor, and others) drives the same `exmergo-dex-core` engine through its
command contract, with identical guardrails. Claude Code is the first-class,
evaluated path; other agents are supported and become eval-gated as the benchmarks
land.

## Cross-agent and engine

- Engine: `packages/dex-core/` (PyPI: `exmergo-dex-core`, Apache-2.0).
- Cross-agent contract: [`AGENTS.md`](AGENTS.md).
- References (connectors, the contract, the canonical model, evaluation):
  [`references/`](references/).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup, the Ruff lint and
format workflow, and the pre-commit hook. Every pull request into `main` must
pass the Lint workflow and CI before it can merge.

## Maintainers: post-scaffold runbook

A few post-scaffold steps need accounts or network. Run them with the appropriate
credentials:

- **GitHub repo metadata:** set the repo **description** to the keyword sentence
  at the top of this README, and add **Topics**: `analytics-engineering`, `dbt`,
  `claude-code`, `text-to-sql`, `semantic-layer`, `duckdb`, `snowflake`,
  `bigquery`, `databricks`, `data-engineering`, `agent`, `metricflow`,
  `schema-drift`, `data-contracts`. This is where discovery lives, not the slug.
- **TestPyPI dry-run:** `scripts/testpypi_dry_run.sh` proves the publish-and-pin
  loop before automation.
- **PyPI Trusted Publishing (both projects):** configure a pending publisher for
  `exmergo-dex-core` (owner `exmergo`, repo `dex`, workflow `release.yml`,
  environment `pypi`) and a second for `dex-core` with the **same values except
  environment `pypi-stub`**. The environments must differ: PyPI rejects two
  pending publishers that share an identical config. Create both environments in
  the repo's GitHub settings. No API tokens are stored.
- **Anti-squat `dex-core` stub:** published automatically by the
  `reserve-dex-core` job in `release.yml` from `packages/dex-core-stub/`,
  idempotently via `uv publish --check-url`. It claims the name on the first
  tagged release and is a no-op after. For protection before that release, you
  can publish the stub once by hand; the CI job then simply skips it.
- **ADE-bench spike:** stand up ADE-bench locally on DuckDB against the no-plugin
  baseline to confirm the runner before depending on it (the exact command is in
  `benchmarks/ade_bench/README.md`).
- **Marketplace entry:** at v0.1 ship time, add the `dex` entry to the
  `exmergo/exmergo-agent-plugins` catalog with a pinned `ref`.
- **BigQuery integration CI:** one-time GCP and GitHub setup (Workload
  Identity Federation, a scoped service account, the `dex_ci` scratch dataset,
  and the `gcp-integration` environment with its variables), automated by
  `scripts/setup_bigquery_ci.sh`; background in `CONTRIBUTING.md` under "Live
  BigQuery integration tests".

## License

Apache-2.0.
