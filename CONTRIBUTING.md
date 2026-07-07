# Contributing to dex

Thanks for contributing. This guide covers local setup and the checks every
pull request must pass.

## Development setup

The engine lives in `packages/dex-core/` and is managed with
[uv](https://docs.astral.sh/uv/). From that directory, sync the runtime plus the
DuckDB on-ramp, the BigQuery client (its unit tests run offline against a fake
but need the library's types), and the dev tools:

```
cd packages/dex-core
uv sync --extra duckdb --extra bigquery --extra dev
```

Run the test suite with:

```
uv run pytest
```

Everything is deterministic and free: no cloud account is needed. The live
cloud integration tests under `tests/integration/` collect as skipped with
the enabling variables named in the skip reason.

## Live BigQuery integration tests

`tests/integration/` runs the real loop against BigQuery: ADC discovery, the
confirm-before-spend handshake with genuine dry-run estimates, a firewalled
query, and a dbt build into a scratch dataset. It reads public data
(`bigquery-public-data`), bills to your test project, and caps every query at
`DEX_TEST_BQ_MAX_BYTES` (default 100 MB), so a worst-case run costs cents.

One-time setup in your GCP project (`scripts/setup_bigquery_ci.sh` automates
all of this plus the CI wiring below; the manual steps follow for reference):

```
# Scratch dataset dbt builds into; the 24h table TTL makes crashed runs self-clean.
bq mk --dataset --location=US --default_table_expiration=86400 <project>:dex_ci

# The principal running the tests needs, at minimum:
#   roles/bigquery.jobUser on the project (run query jobs; billing lands there)
#   roles/bigquery.dataEditor on dex_ci ONLY (never project-wide: this is the
#   IAM enforcement of "dex never writes outside the dev dataset")
```

Then authenticate with ADC and run the suite:

```
gcloud auth application-default login
DEX_TEST_BQ_PROJECT=<project> DEX_TEST_BQ_DATASET=dex_ci uv run pytest tests/integration -q
```

In CI the same suite runs from `.github/workflows/integration.yml`,
authenticated via Workload Identity Federation (OIDC, no stored keys): a
Workload Identity Pool with a GitHub OIDC provider whose attribute condition
pins it to this repository (`attribute.repository == "exmergo/dex"`; without
that condition any repo could mint tokens against the pool), and a service
account holding the two roles above plus `roles/iam.workloadIdentityUser` for
the repository's principalSet. The job runs in the `gcp-integration` GitHub
environment, whose deployment branch policy restricts it to `main` (so a
workflow modified on a branch cannot claim it), and reads the pool path,
service account, and project from that environment's variables
`GCP_WIF_PROVIDER`, `GCP_INTEGRATION_SA`, and `DEX_TEST_BQ_PROJECT`. They are
variables, not secrets, on purpose: with WIF there is no credential to hide,
the values are identifiers, and unmasked values make auth failures debuggable.
The workflow is deliberately not a merge or release gate; forks skip it and
can point the suite at their own project with the same environment variables.

## Live Snowflake integration tests

The same `tests/integration/` directory carries the Snowflake suite:
connection discovery, the warehouse-seconds handshake with its credit
translation, the over-ceiling refusal, a firewalled query, and a dbt build
into the scratch database. It reads `SNOWFLAKE_SAMPLE_DATA` (shared data,
free storage), bills warehouse time to the pinned X-Small only, and caps
every statement at `DEX_TEST_SNOWFLAKE_MAX_SECONDS` (default 60), so a
worst-case run costs cents; the account's resource monitor is the hard
monthly backstop no bug can outspend.

One-time setup is automated by `scripts/setup_snowflake_ci.sh` (run by a
maintainer with an ACCOUNTADMIN `snow` connection): the `DEX_CI_WH` X-Small
warehouse (60s auto-suspend, statement timeout, resource monitor), the
transient zero-retention `DEX_CI` database, the least-privilege `DEX_CI_ROLE`
(read samples, write scratch only: the grant-level enforcement of "dex never
writes outside the dev target"), a workload-identity CI user, a key-pair dev
user with a local `dex-ci` connection, and the GitHub environment with its
variables.

Run the suite locally against the `dex-ci` connection:

```
DEX_TEST_SNOWFLAKE_CONNECTION=dex-ci DEX_TEST_SNOWFLAKE_DATABASE=DEX_CI \
    uv run pytest tests/integration -q -m snowflake
```

In CI the same suite runs from `.github/workflows/integration.yml`,
authenticated via Snowflake workload identity federation (OIDC, no stored
keys): the `DEX_CI` service user's `WORKLOAD_IDENTITY` accepts only GitHub
OIDC tokens whose subject names this repository's `snowflake-integration`
environment, whose deployment branch policy restricts it to `main`. The job
mints the token itself and hands it to the connector through the ordinary
`SNOWFLAKE_*` discovery path; `DEX_TEST_SNOWFLAKE_ACCOUNT` and
`DEX_TEST_SNOWFLAKE_USER` are environment variables, not secrets, for the
same debuggability reason as the BigQuery job.

## Live PostgreSQL integration tests

The same `tests/integration/` directory carries the Postgres suite:
connection discovery, the database-seconds handshake, the over-ceiling
refusal, PII flag-not-surface, relationship inference on a deliberately
undeclared foreign key, a firewalled query, and a dbt build into the
dedicated dev schema. Unlike the cloud suites it bills nothing and needs no
cloud account: the target is a local Docker container seeded from
`scripts/postgres_seed.sql`, with a read-only `dex_ro` role for the engine
and a `dbt_dev` role that can write only the dev schema.

Stand the container up and run the suite locally:

```
scripts/setup_postgres_dev.sh
DEX_TEST_PG_DSN=postgresql://dex_ro:dex_ro@localhost:5433/dex_dogfood \
    DEX_TEST_PG_DEV_PASSWORD=dbt_dev uv run pytest tests/integration -q -m postgres
```

In CI the same suite runs from `.github/workflows/integration.yml` against a
`postgres:16` service container seeded from the same SQL: free, keyless, and
fork-runnable, kept in the integration workflow for pattern parity with the
cloud connectors rather than as a cost decision. There is no
`setup_postgres_ci.sh`; there is nothing to provision.

## Agent evals (`evals/`)

The Tier-2 agent-eval harness lives at the repo root in `evals/`, separate from
the engine: it drives a concrete agent (Claude today) to test the skills, so it
sits with the skills it tests, not in the published `exmergo-dex-core` wheel.

It is **stdlib only by design**: no `pyproject.toml`, no `uv.lock`, no
third-party runtime dependency. Run its deterministic core tests with:

```
uvx pytest evals
```

Run a skill's live suite (needs the `claude` CLI on PATH and the plugin
installed) with `python -m evals skills/<skill>`. If a future backend needs a
real Python dependency, promote `evals/` to its own uv project at that point and
not before. See `evals/README.md` for the rationale and full usage.

## Linting and formatting (Ruff)

We use [Ruff](https://docs.astral.sh/ruff/) as both the linter and the
formatter. A single `ruff.toml` at the repo root governs the whole tree
(`packages/`, `skills/`, and `scripts/`), so run Ruff from the repo root:

```
uvx ruff check .          # lint
uvx ruff check --fix .    # lint and auto-fix
uvx ruff format .         # format in place
```

### Set up the pre-commit hook

The fastest way to stay green is to let Ruff run automatically before each
commit. Install [pre-commit](https://pre-commit.com/) once, then enable the
hook in your clone:

```
uv tool install pre-commit   # or: pipx install pre-commit
pre-commit install
```

From then on, `ruff check --fix` and `ruff format` run on your staged files at
commit time. To check every file on demand:

```
pre-commit run --all-files
```

## The linter must pass before a PR can merge

Every push to `main` and every pull request into `main` runs the **Lint**
workflow (`.github/workflows/lint.yml`), which fails the build if
`ruff check` reports any issue or if `ruff format --check` finds unformatted
code. Open PRs cannot merge until this check is green, alongside the existing
CI (tests, safety spine, and the em-dash prose check). Run Ruff or the
pre-commit hook locally before you push so the gate passes on the first try.

## Prose is em-dash free

All shipped prose in this repo avoids em dashes (an Exmergo brand rule),
enforced in CI. Before committing Markdown or text, you can check it locally:

```
python3 scripts/check_no_em_dashes.py path/to/file.md
```

## Keeping the Ruff version in sync

The pinned Ruff version appears in three places that must move together when you
bump it:

- `.github/workflows/lint.yml` (the `uvx ruff@<version>` calls)
- `.pre-commit-config.yaml` (the `rev:` tag)
- `packages/dex-core/pyproject.toml` (the `ruff==<version>` pin in the `dev` extra)


## Maintainers

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
- **Repo traffic history:** the `repo-stats.yml` workflow snapshots clone and
  view counts nightly into a `github-repo-stats` branch (GitHub's traffic API
  only retains 14 days). It needs a fine-grained PAT scoped to this repo with
  Administration: read and Contents: read/write, stored as the `GHRS_TOKEN`
  secret; the job fails silently when the PAT expires, so rotate it on schedule.
- **BigQuery integration CI:** one-time GCP and GitHub setup (Workload
  Identity Federation, a scoped service account, the `dex_ci` scratch dataset,
  and the `gcp-integration` environment with its variables), automated by
  `scripts/setup_bigquery_ci.sh`; background in `CONTRIBUTING.md` under "Live
  BigQuery integration tests".
- **Snowflake integration CI:** one-time Snowflake and GitHub setup (a
  workload-identity service user pinned to this repo's
  `snowflake-integration` environment, a least-privilege role, the pinned
  X-Small `DEX_CI_WH` warehouse with a resource-monitor backstop, the
  transient `DEX_CI` scratch database, and a key-pair dev user with a local
  `dex-ci` connection for running the live suite while developing), automated
  by `scripts/setup_snowflake_ci.sh`.
