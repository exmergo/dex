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

To try a change by hand rather than through the suite, generate the demo
warehouse in a scratch directory outside the checkout and drive the CLI against
it. It is the same seeded fixture the documentation quotes, so what you see there
is what a user sees:

```
mkdir /tmp/dex-scratch && cd /tmp/dex-scratch
uv run --project ~/path/to/dex/packages/dex-core python -m exmergo_dex_core demo
uv run --project ~/path/to/dex/packages/dex-core python -m exmergo_dex_core explore map
```

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

## Live Databricks integration tests

The same `tests/integration/` directory carries the Databricks suite:
connection discovery, the warehouse-seconds handshake with its DBU
translation, the over-ceiling refusal, a firewalled query, and a dbt build
into the scratch catalog. It reads the `samples` catalog (shared data, free
storage), bills warehouse time to the pinned warehouse only, and caps every
statement at `DEX_TEST_DATABRICKS_MAX_SECONDS` (default 60) via a server-side
`STATEMENT_TIMEOUT`, so a worst-case run costs cents. Databricks has no
resource-monitor analogue that hard-suspends compute, so the backstops are
the smallest warehouse size, the minimum auto-stop, the per-statement
timeout, and a budget alert in the account console.

One-time setup is automated by `scripts/setup_databricks_ci.sh` (run by a
maintainer who is a workspace admin and logged into the account console): the
`dex-ci` service principal, its account-level GitHub OIDC federation policy,
the dedicated 2X-Small serverless `DEX_CI` warehouse (minimum auto-stop, CAN
USE for the principal only), the `dex_ci` scratch catalog (write grants to
the principal only: the grant-level enforcement of "dex never writes outside
the dev target"; `samples` needs no grant, sample datasets are implicitly
readable), and the GitHub environment with its variables.

Run the suite locally against your own `databricks auth login` session:

```
DEX_TEST_DATABRICKS_WAREHOUSE=<warehouse-id> \
    DEX_TEST_DATABRICKS_CATALOG=dex_ci uv run pytest tests/integration -q -m databricks
```

In CI the same suite runs from `.github/workflows/integration.yml`,
authenticated via the federation policy (OIDC, no stored keys): the policy
accepts only GitHub OIDC tokens whose subject names this repository's
`databricks-integration` environment, whose deployment branch policy
restricts it to `main`. The job exchanges the GitHub token for a Databricks
OAuth token at `/oidc/v1/token` and hands it to the engine and dbt through
the ordinary `DATABRICKS_TOKEN` discovery path; the host, client id,
warehouse, and catalog are environment variables, not secrets, for the same
debuggability reason as the BigQuery job.

## Live Redshift integration tests

The same `tests/integration/` directory carries the Redshift suite:
connection discovery (the AWS chain against a pinned Serverless workgroup,
or the `REDSHIFT_*` environment), the compute-seconds handshake with the
Serverless wake-minimum floor, the over-ceiling refusal, PII
flag-not-surface, a firewalled query, and a dbt build into the dedicated dev
schema as the `dbt_dev` user.

One-time setup is automated by `scripts/setup_redshift_ci.sh` (run by a
maintainer with AWS admin credentials, psql, and gh): the smallest Serverless
namespace and workgroup (8 RPUs), a daily RPU-hours usage limit with a
query-deactivating breach action as the hard cost backstop, the seeded `app`
schema plus the `dex_ro` and `dbt_dev` users (the dbt user carries a durable
`statement_timeout`, the per-statement cap dex cannot inject through
dbt-redshift), an IAM role whose trust policy accepts only GitHub OIDC tokens
minted for this repo's `redshift-integration` environment, and the
environment with its variables and the rotated dbt password secret.

Run the suite locally with your own AWS credentials:

```
DEX_TEST_REDSHIFT_WORKGROUP=dex-ci DEX_TEST_REDSHIFT_DATABASE=dev \
    uv run pytest tests/integration -q -m redshift
```

The transform tests additionally need `DEX_TEST_REDSHIFT_HOST` and
`DEX_TEST_REDSHIFT_DEV_PASSWORD` (they exercise the env-var password
rendering; the engine itself stays keyless). In CI the suite runs from
`.github/workflows/integration.yml` on an assumed OIDC role: keyless, never
a merge gate.

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

## Live ClickHouse integration tests

The same `tests/integration/` directory carries the ClickHouse suite:
connection discovery, the database-seconds handshake, the over-ceiling
refusal, PII flag-not-surface, relationship inference on the seeded orphans,
temporal continuity over a deliberate gap, a firewalled query using ClickHouse
idioms (`countIf`, `FINAL`, `ARRAY JOIN`), the dev-target grant preflight, and
a dbt build into the dedicated dev database. Like the Postgres suite it bills
nothing and needs no account: the target is a local Docker container.

Unlike Postgres it has only one seeding path, and CI uses it too. The seed is
multi-statement and the ClickHouse HTTP interface refuses multi-statement
bodies, so it has to go through `clickhouse-client` inside the container, which
means the same script serves both places:

```
scripts/setup_clickhouse_dev.sh
DEX_TEST_CH_DSN=clickhouse://dex_ro:dex_ro@localhost:8124/app \
    DEX_TEST_CH_DEV_PASSWORD=dbt_dev \
    uv run pytest tests/integration -q -m clickhouse
scripts/setup_clickhouse_dev.sh --down
```

There is no `setup_clickhouse_ci.sh`; there is nothing to provision.

Two assertions in that suite are load-bearing and worth understanding before
changing the seed. The seed puts exactly 40 of 5,000 `order_items` rows on
products that do not exist, so an orphan probe reporting zero has lost the
`join_use_nulls` session setting rather than found clean data. And
`events.occurred_at` is missing exactly three consecutive days out of ninety,
so a continuity check reporting no gap has the window function wrong. Both
failures are silent by nature: they produce a clean result rather than an
error.

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

## Writing a storage backend

`.dex/` state lives behind a `Store` protocol, and the contract is public so a
backend does not have to live in this repository. The usual reason to write one is
a process serving several end users, which needs state federated per user in its
own datastore rather than in one repo directory.

Implement the tier your host actually uses (`ExploreStore` is six methods;
`MaintainStore` and `Store` add to it), then prove it with the suite dex ships:

```
pip install "exmergo-dex-core[storage-conformance]"
```

```python
from exmergo_dex_core.storage.conformance import ExploreStoreContract


class TestMyStore(ExploreStoreContract):
    def make_store(self, key):
        return MyStore(tenant=key)
```

pytest collects the inherited tests and runs the whole contract against your
backend, isolation assertions included. Every assertion gets its own key, so a
backend whose instances share state per key, which is every durable one, needs no
reset hook to keep one assertion's writes out of the next.
`references/storage.md` covers the tiers, the contracts that are not obvious from
the signatures, and which calls need nothing on the filesystem.

If your backend can be reached by more than one command at a time, also
implement `spend_lock` and mix in `SpendLockContract`. It is the one optional
capability whose absence costs correctness: without it two overlapping billed
commands are admitted against the same headroom and `budget.session_ceiling`
does not bind. dex says so on every billed result rather than assuming it, so a
backend without one is honest rather than silently broken.

Backends contributed here run the same suite: see
`packages/dex-core/tests/storage/test_parity.py`, which is deliberately the same
three lines a third party writes.

## Writing a project format

The source of truth lives behind a tiered protocol in `adapters/project.py`, and
that contract is public too. The usual reason to write one is that your models are
not a dbt project: an orchestrated asset graph, SQLMesh, or a semantic layer that
owns its own definitions still knows which tables it builds, at what grain, and how
they relate.

Implement the tier your format can answer (`ExploreProject` is one method;
`MaintainProject` adds the two snapshot layers; `EditableProject` adds the write
path), then prove it with the suite dex ships:

```
pip install "exmergo-dex-core[project-conformance]"
```

```python
from exmergo_dex_core.adapters.conformance import ExploreProjectContract


class TestMyProject(ExploreProjectContract):
    def make_project(self):
        return MyProject(nothing_declared())
```

The assertion worth the most is behavioural rather than structural: `definitions()`
must not raise, on an absent project, an ambiguous one, or a source that will not
parse. Explore runs against warehouses with no project at all, so a format that
raises there turns an ordinary state into an outage. Override
`make_unreadable_project()` to get those assertions running against your format
instead of skipped.

Mix in the contracts covering what your format *declares*, beside the one for your
tier. `DeclaringProjectContract` checks that a declared key and a declared join
arrive, and carries two further hooks worth supplying: a composite key of more than
two columns (a format that special-cases the pair passes a two-column fixture), and
a join whose two ends are spelled differently (if both ends of your fixture share a
name, an implementation that mirrors one side onto the other satisfies it exactly).
`SemanticProjectContract` checks that each semantic field keeps the warehouse column
behind it, which is the one no tier assertion can see: the tier contract only looks
at an *empty* semantic layer, and a format that reads every field name and drops the
columns maps everything to `None`, which validates, serializes, and compares clean
forever while the drift check silently never runs. It checks your format reaches
tier 2 before any of that, so mixing it in beside the tier-1 contract fails with a
sentence naming the tier rather than with a missing-attribute error.

Mix `ProjectFactoryContract` in front of your tier contract if dex will build your
format from a name rather than be handed an instance, which is what a host reaching
dex as a subprocess needs. Naming one is the same shape as naming a storage backend:

```yaml
project:
  format: mypkg.projects:my_project
  options:
    graph: orders
```

A shipped name, a dotted path, or an entry point under `exmergo_dex_core.projects`,
with shipped names always winning so an install can never silently redirect which
models a repo is reasoned about.

Declining `EditableProject` is a supported answer, not a gap. The clearest case is a
project reduced from a running graph, where the reduction is not the source of truth:
the code that produced the graph is, so an edit written into the reduction is
overwritten on the next run. `maintain reconcile` reads that declaration, and your
format gets advisory proposals and no stored plan, by contract rather than by luck.

Decide it by asking which artifact an edit would land in, not where your project came
from. Those questions come apart more often than the graph example suggests: an asset
graph carries neither column names nor join keys, so a format over one reads its
declared keys, joins and semantics from somewhere else, and that somewhere is usually
a hand-authored file that nothing regenerates. Such a file is a real source of truth
and it is the shape `reconcile` already proposes edits to, so a format holding one may
serve this tier for that channel while still refusing to author a model.
`references/project.md` covers the tiers, the construction contract, and the rules
that are not obvious from the signatures.

Formats contributed here run the same suite: see
`packages/dex-core/tests/adapters/test_project_parity.py`.

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

## The Mermaid syntax canary

`explore diagram` emits Mermaid text. dex never renders it, so nothing in
`pyproject.toml` pins the syntax dialect it has to stay compatible with, and the
`mermaid-syntax` CI job holds that contract instead: it renders a fixture corpus
from the engine and parses each one with the real `mermaid` package from npm. It is
advisory, like `sqlglot-canary`, because a Mermaid release breaking our dialect
should be loud without blocking an unrelated merge.

To reproduce it locally you need Node. Install outside the checkout so a Python
repo does not acquire a 160MB `node_modules` (it is gitignored either way):

```bash
mkdir -p /tmp/mermaid-canary && cd /tmp/mermaid-canary
npm install mermaid@^11 jsdom@^26
cp "$OLDPWD/scripts/check_mermaid_syntax.mjs" .

cd "$OLDPWD"
uv run --project packages/dex-core python scripts/dump_mermaid_fixtures.py /tmp/mermaid-fixtures
cd /tmp/mermaid-canary && node check_mermaid_syntax.mjs /tmp/mermaid-fixtures
```

If you change `explore/diagram.py`, run this. A construct that parses in the
Mermaid you happen to have may still be outside the conservative `erDiagram`
subset the module commits to, and the corpus is what tells you.

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
- **Redshift integration CI:** one-time AWS and GitHub setup (the smallest
  Serverless workgroup with a daily RPU-hours usage limit, the seeded `app`
  schema with the `dex_ro`/`dbt_dev` users, an OIDC-trusted IAM role pinned
  to this repo's `redshift-integration` environment, and the environment
  with its variables and the rotated dbt password secret), automated by
  `scripts/setup_redshift_ci.sh`; background in `CONTRIBUTING.md` under
  "Live Redshift integration tests".
- **Databricks integration CI:** one-time Databricks and GitHub setup (the
  `dex-ci` service principal with an account-level GitHub OIDC federation
  policy pinned to this repo's `databricks-integration` environment, the
  dedicated 2X-Small serverless `DEX_CI` warehouse, the `dex_ci` scratch
  catalog, and the environment with its variables), automated by
  `scripts/setup_databricks_ci.sh`; background in `CONTRIBUTING.md` under
  "Live Databricks integration tests". The budget-alert backstop is manual
  (account console, Usage > Budgets).
