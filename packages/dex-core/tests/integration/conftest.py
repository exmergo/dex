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
"""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV = ("DEX_TEST_BQ_PROJECT", "DEX_TEST_BQ_DATASET")

MAX_BYTES = int(os.environ.get("DEX_TEST_BQ_MAX_BYTES", str(100 * 1024 * 1024)))

SF_MAX_SECONDS = float(os.environ.get("DEX_TEST_SNOWFLAKE_MAX_SECONDS", "60"))


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
