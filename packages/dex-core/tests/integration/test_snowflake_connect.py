"""Live `connect test` against Snowflake: connection discovery, capabilities
envelope, and the sanitizer, end to end. Free: capabilities issues SHOW
commands only, which run on the cloud-services layer with no warehouse."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.snowflake]

SAMPLE_SCOPE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1"


def seed_repo(
    root: Path,
    scratch_database: str,
    warehouse: str,
    connection_name: str | None,
    *,
    databases: list[str] | None = None,
    budget: float | None = None,
) -> None:
    snowflake: dict = {
        "warehouse": warehouse,
        "databases": databases if databases is not None else [SAMPLE_SCOPE],
        "dev_database": scratch_database,
    }
    if connection_name:
        snowflake["connection_name"] = connection_name
    config: dict = {"connector": "snowflake", "snowflake": snowflake}
    if budget is not None:
        config["budget"] = {"ceiling": budget}
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line on stdout"
    return rc, json.loads(out)


def test_connect_test_discovers_connection_and_reports_read_only(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "snowflake"
    assert data["dialect"] == "snowflake"
    assert data["read_only"] is True
    assert data["paradigm"] == "compute_time"
    assert data["budget"]["warehouse"]["name"].upper() == sf_warehouse.upper()
    assert data["budget"]["warehouse"]["credits_per_hour"] is not None
    assert envelope["cost"]["paradigm"] == "compute_time"
    # The auth method is coarse; no identity, password, or key crosses.
    payload = json.dumps(envelope)
    assert data["auth_method"].split(":")[0] in {
        "named_connection",
        "default_connection",
        "environment",
        "dbt_profile",
    }
    user = os.environ.get("SNOWFLAKE_USER", "\x00never")
    assert user not in payload
