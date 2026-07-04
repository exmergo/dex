"""Live BigQuery integration: real ADC, real jobs, real (tiny) bills.

Skipped unless the environment opts in, so the default suite stays
deterministic and free for contributors without GCP. To run locally:

    gcloud auth application-default login
    DEX_TEST_BQ_PROJECT=<your-project> DEX_TEST_BQ_DATASET=dex_ci \
        uv run pytest tests/integration -q

Billing goes to DEX_TEST_BQ_PROJECT. Every query is capped at
DEX_TEST_BQ_MAX_BYTES (default 100 MB), so a worst-case bug costs cents; the
suite reads public datasets (bigquery-public-data) and writes only to the
scratch dataset, which should be provisioned with a default table TTL:

    bq mk --dataset --default_table_expiration 86400 <project>:dex_ci
"""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV = ("DEX_TEST_BQ_PROJECT", "DEX_TEST_BQ_DATASET")

MAX_BYTES = int(os.environ.get("DEX_TEST_BQ_MAX_BYTES", str(100 * 1024 * 1024)))


@pytest.fixture(autouse=True)
def _require_bigquery_env():
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
