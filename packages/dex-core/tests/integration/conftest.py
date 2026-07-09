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
"""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV = ("DEX_TEST_BQ_PROJECT", "DEX_TEST_BQ_DATASET")

MAX_BYTES = int(os.environ.get("DEX_TEST_BQ_MAX_BYTES", str(100 * 1024 * 1024)))

SF_MAX_SECONDS = float(os.environ.get("DEX_TEST_SNOWFLAKE_MAX_SECONDS", "60"))

DBX_MAX_SECONDS = float(os.environ.get("DEX_TEST_DATABRICKS_MAX_SECONDS", "60"))


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
def pg_dsn() -> str:
    return os.environ["DEX_TEST_PG_DSN"]


@pytest.fixture
def pg_dev_password() -> str | None:
    return os.environ.get("DEX_TEST_PG_DEV_PASSWORD")
