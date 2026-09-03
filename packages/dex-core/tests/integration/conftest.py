"""Live cloud integration: real credentials, real jobs, real (tiny) bills.

Each connector's tests are skipped unless its environment opts in, so the
default suite stays deterministic and free for contributors without cloud
accounts.

BigQuery (bills to DEX_TEST_BQ_PROJECT; every query capped at
DEX_TEST_BQ_MAX_BYTES, default 100 MB, so a worst-case bug costs cents; reads
public datasets, writes only to the TTL'd scratch dataset):

    gcloud auth application-default login
    DEX_TEST_BQ_PROJECT=<your-project> DEX_TEST_BQ_DATASET=dex_ci \
        uv run pytest tests/integration -q

Snowflake (bills warehouse time on the pinned X-Small; every statement capped
at DEX_TEST_SNOWFLAKE_MAX_SECONDS, default 60; reads SNOWFLAKE_SAMPLE_DATA,
writes only to the transient scratch database; the account-level resource
monitor is the hard backstop). Auth comes from a connections.toml entry
locally or SNOWFLAKE_* env in CI (scripts/setup_snowflake_ci.sh provisions
both):

    DEX_TEST_SNOWFLAKE_CONNECTION=dex-ci DEX_TEST_SNOWFLAKE_DATABASE=DEX_CI \
        uv run pytest tests/integration -q

Databricks (bills warehouse time on the pinned SQL warehouse; every statement
capped at DEX_TEST_DATABRICKS_MAX_SECONDS, default 60, enforced server-side by
STATEMENT_TIMEOUT; reads the samples catalog, writes only to the scratch
catalog). Auth is the SDK's unified chain: `databricks auth login` locally, or
DATABRICKS_HOST plus a credential in CI (scripts/setup_databricks_ci.sh
provisions the service principal and the OIDC federation policy):

    DEX_TEST_DATABRICKS_WAREHOUSE=<warehouse-id> \
        DEX_TEST_DATABRICKS_CATALOG=dex_ci uv run pytest tests/integration -q

Postgres (bills nothing: a local container; db-load gating is exercised for
real, statement timeouts and all). The DSN should be the read-only role from
scripts/postgres_seed.sql; transform additionally needs the dbt_dev role's
password in DEX_TEST_PG_DEV_PASSWORD (scripts/setup_postgres_dev.sh stands
the whole thing up):

    DEX_TEST_PG_DSN=postgresql://dex_ro:dex_ro@localhost:5433/dex_dogfood \
        DEX_TEST_PG_DEV_PASSWORD=dbt_dev uv run pytest tests/integration -q

Redshift (bills RPU time on the pinned Serverless workgroup; every confirmed
budget derives from DEX_TEST_REDSHIFT_MAX_SECONDS, default 60 -- small fixed
multiples covering the 60-second wake minimum plus the seeded scans -- and the
budget-derived statement_timeout caps each statement server-side; the
workgroup usage limit is the hard backstop). Auth is
the AWS default credential chain (`aws configure` locally, an assumed OIDC
role in CI; scripts/setup_redshift_ci.sh provisions the workgroup, the users,
and the usage limit); transform additionally needs the dbt_dev user's
password in DEX_TEST_REDSHIFT_DEV_PASSWORD unless the IAM profile path is in
use:

    DEX_TEST_REDSHIFT_WORKGROUP=dex-ci DEX_TEST_REDSHIFT_DATABASE=dev \
        uv run pytest tests/integration -q

ClickHouse (bills nothing: a local container; db-load gating is exercised for
real, server-side caps and all). The DSN should be the read-only user from
scripts/clickhouse_local_users.sql; transform additionally needs the dbt_dev user's
password in DEX_TEST_CH_DEV_PASSWORD. One script stands the whole thing up and
is the same one CI runs, so the local target and the CI target cannot drift:

    scripts/setup_clickhouse_dev.sh
    DEX_TEST_CH_DSN=clickhouse://dex_ro:dex_ro@localhost:8124/app \
        DEX_TEST_CH_DEV_PASSWORD=dbt_dev \
        uv run pytest tests/integration -q -m clickhouse

ClickHouse Cloud (bills the dedicated Scale service in AWS us-east-1; the
repository workflow performs the hard daily-usage preflight before any SQL).
scripts/setup_clickhouse_cloud_ci.sh provisions the two isolated databases,
least-privilege users, server-side limits, scoped usage reader, and protected
GitHub environment used by this suite:

    scripts/clickhouse_cloud/run_integration.sh
"""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV = ("DEX_TEST_BQ_PROJECT", "DEX_TEST_BQ_DATASET")

MAX_BYTES = int(os.environ.get("DEX_TEST_BQ_MAX_BYTES", str(100 * 1024 * 1024)))

SF_MAX_SECONDS = float(os.environ.get("DEX_TEST_SNOWFLAKE_MAX_SECONDS", "60"))

DBX_MAX_SECONDS = float(os.environ.get("DEX_TEST_DATABRICKS_MAX_SECONDS", "60"))

RS_MAX_SECONDS = float(os.environ.get("DEX_TEST_REDSHIFT_MAX_SECONDS", "60"))

CH_MAX_SECONDS = float(os.environ.get("DEX_TEST_CH_MAX_SECONDS", "60"))


def integration_budget(ceiling: float | None = None) -> dict[str, float | bool]:
    """Budget config for one ephemeral live-test project.

    Every integration test gets a fresh ``tmp_path`` and already runs behind a
    per-command ceiling plus the connector's external CI backstop. Record that
    these disposable projects deliberately have no cross-command ceiling so the
    one-time production prompt does not replace the behavior under test.
    """

    budget: dict[str, float | bool] = {"session_ceiling_declined": True}
    if ceiling is not None:
        budget["ceiling"] = ceiling
    return budget


def assert_ok(rc: int, envelope: dict) -> dict:
    """Assert a live command succeeded, and say what the warehouse said if it
    did not. Returns the envelope, so it reads inline.

    The obvious spelling is ``assert rc == 0, envelope``, and it is why a live
    Redshift failure cost sixteen CI runs to diagnose instead of five minutes
    (#310): pytest elides the middle of a long assertion message, and the
    middle of a profiling envelope is exactly where ``errors`` sits, so the
    server's own message never once reached a CI log. Putting the errors
    first, alone, makes the surviving line the one worth reading.
    """

    assert rc == 0, (
        f"errors={envelope.get('errors')} reason={envelope.get('reason')} "
        f"warnings={envelope.get('warnings')}"
    )
    return envelope


def _snowflake_enabled() -> bool:
    return bool(
        os.environ.get("DEX_TEST_SNOWFLAKE_DATABASE")
        and (
            os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION")
            or (
                os.environ.get("SNOWFLAKE_ACCOUNT") and os.environ.get("SNOWFLAKE_USER")
            )
        )
    )


@pytest.fixture(autouse=True)
def _require_cloud_env(request):
    if request.node.get_closest_marker("snowflake"):
        if not _snowflake_enabled():
            pytest.skip(
                "Snowflake integration disabled: set DEX_TEST_SNOWFLAKE_DATABASE "
                "plus DEX_TEST_SNOWFLAKE_CONNECTION (or SNOWFLAKE_ACCOUNT/USER)"
            )
        pytest.importorskip("snowflake.connector")
        return
    if request.node.get_closest_marker("databricks"):
        if not os.environ.get("DEX_TEST_DATABRICKS_WAREHOUSE"):
            pytest.skip(
                "Databricks integration disabled: set "
                "DEX_TEST_DATABRICKS_WAREHOUSE (and authenticate via "
                "`databricks auth login` or DATABRICKS_HOST plus a credential)"
            )
        pytest.importorskip("databricks.sql")
        return
    if request.node.get_closest_marker("postgres"):
        if not os.environ.get("DEX_TEST_PG_DSN"):
            pytest.skip(
                "Postgres integration disabled: set DEX_TEST_PG_DSN (run "
                "scripts/setup_postgres_dev.sh for a seeded local container)"
            )
        pytest.importorskip("psycopg")
        return
    if request.node.get_closest_marker("redshift"):
        if not (
            os.environ.get("DEX_TEST_REDSHIFT_WORKGROUP")
            or os.environ.get("DEX_TEST_REDSHIFT_HOST")
        ):
            pytest.skip(
                "Redshift integration disabled: set DEX_TEST_REDSHIFT_WORKGROUP "
                "(IAM via the AWS credential chain) or DEX_TEST_REDSHIFT_HOST "
                "plus REDSHIFT_* credentials"
            )
        pytest.importorskip("redshift_connector")
        return
    if request.node.get_closest_marker("clickhouse_cloud"):
        required = (
            "DEX_TEST_CH_CLOUD_ORG_ID",
            "DEX_TEST_CH_CLOUD_SERVICE_ID",
            "DEX_TEST_CH_CLOUD_HOST",
            "DEX_TEST_CH_CLOUD_PORT",
            "DEX_TEST_CH_CLOUD_DATABASE",
            "DEX_TEST_CH_CLOUD_DEV_DATABASE",
            "DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD",
            "DEX_TEST_CH_CLOUD_MAX_SECONDS",
            "DEX_TEST_CH_CLOUD_DSN",
            "DEX_TEST_CH_CLOUD_DEV_PASSWORD",
            "DEX_TEST_CH_CLOUD_API_KEY",
            "DEX_TEST_CH_CLOUD_API_SECRET",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            pytest.skip(
                "ClickHouse Cloud integration disabled: required values are "
                f"missing: {', '.join(missing)}"
            )
        pytest.importorskip("clickhouse_connect")
        return
    if request.node.get_closest_marker("clickhouse"):
        if not os.environ.get("DEX_TEST_CH_DSN"):
            pytest.skip(
                "ClickHouse integration disabled: set DEX_TEST_CH_DSN (run "
                "scripts/setup_clickhouse_dev.sh for a seeded local container)"
            )
        pytest.importorskip("clickhouse_connect")
        return
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"BigQuery integration disabled: set {', '.join(missing)}")
    pytest.importorskip("google.cloud.bigquery")


@pytest.fixture
def bq_project() -> str:
    return os.environ["DEX_TEST_BQ_PROJECT"]


@pytest.fixture
def bq_scratch_dataset() -> str:
    return os.environ["DEX_TEST_BQ_DATASET"]


@pytest.fixture
def sf_scratch_database() -> str:
    return os.environ["DEX_TEST_SNOWFLAKE_DATABASE"]


@pytest.fixture
def sf_connection_name() -> str | None:
    return os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION")


@pytest.fixture
def sf_warehouse() -> str:
    return os.environ.get("DEX_TEST_SNOWFLAKE_WAREHOUSE", "DEX_CI_WH")


@pytest.fixture
def dbx_warehouse() -> str:
    return os.environ["DEX_TEST_DATABRICKS_WAREHOUSE"]


@pytest.fixture
def dbx_scratch_catalog() -> str | None:
    # Needed by transform only; explore reads samples and writes nothing.
    return os.environ.get("DEX_TEST_DATABRICKS_CATALOG")


@pytest.fixture
def rs_workgroup() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_WORKGROUP")


@pytest.fixture
def rs_host() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_HOST")


@pytest.fixture
def rs_database() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_DATABASE")


@pytest.fixture
def rs_dev_password() -> str | None:
    # Needed by transform's password path only; IAM profiles need nothing.
    return os.environ.get("DEX_TEST_REDSHIFT_DEV_PASSWORD")


@pytest.fixture
def pg_dsn() -> str:
    return os.environ["DEX_TEST_PG_DSN"]


@pytest.fixture
def pg_dev_password() -> str | None:
    return os.environ.get("DEX_TEST_PG_DEV_PASSWORD")


# --- the unpivot_json_object live fixture ----------------------------------------
#
# One payload shape shared by every connector's macro build test: a staging
# model inlining three rows (two JSON documents whose values are themselves
# objects, one NULL), the unpivot mart, and the schema tests that pin the
# nested-key failure mode (a surfaced nested key like `role` fails
# accepted_values; the NULL row must yield no rows at all).

UNPIVOT_DOC_A = (
    '{"rel_a": {"role": "admin", "since": 2020}, "rel_b": {"role": "viewer"}}'
)
UNPIVOT_DOC_B = '{"rel_c": {"role": "editor"}}'


def unpivot_fixture_edits(wrap, null_expr: str) -> dict:
    """The edits payload, parameterized by the connector's JSON idiom:
    ``wrap`` renders a JSON document literal into the column's type and
    ``null_expr`` is a typed NULL for it."""

    return {
        "edits": [
            {
                "path": "models/staging/stg_entities.sql",
                "kind": "model_sql",
                "content": (
                    f"select 1 as id, {wrap(UNPIVOT_DOC_A)} as attributes\n"
                    f"union all select 2, {wrap(UNPIVOT_DOC_B)}\n"
                    f"union all select 3, {null_expr}\n"
                ),
            },
            {
                "path": "models/marts/entity_relations.sql",
                "kind": "model_sql",
                "content": (
                    "select id, key as related_id, value as attrs\n"
                    "from (\n"
                    "  {{ unpivot_json_object(relation=ref('stg_entities'),"
                    " json_column='attributes', passthrough=['id']) }}\n"
                    ")\n"
                ),
            },
            {
                "path": "models/marts/entity_relations.yml",
                "kind": "schema_yml",
                "content": (
                    "version: 2\n"
                    "models:\n"
                    "  - name: entity_relations\n"
                    "    columns:\n"
                    "      - name: related_id\n"
                    "        data_tests:\n"
                    "          - not_null\n"
                    "          - accepted_values:\n"
                    "              values: ['rel_a', 'rel_b', 'rel_c']\n"
                ),
            },
        ]
    }


def assert_unpivot_build(built: dict) -> None:
    """The build envelope assertions shared by every connector: models built,
    every schema test passed (accepted_values is the nested-key tripwire)."""

    assert built["status"] == "ok", built
    statuses = {n["name"]: n["status"] for n in built["data"]["nodes"]}
    assert statuses.get("stg_entities") == "success"
    assert statuses.get("entity_relations") == "success"
    assert all(s in {"success", "pass"} for s in statuses.values()), statuses


@pytest.fixture
def ch_dsn() -> str:
    return os.environ["DEX_TEST_CH_DSN"]


@pytest.fixture
def ch_dev_password() -> str:
    return os.environ.get("DEX_TEST_CH_DEV_PASSWORD", "dbt_dev")
