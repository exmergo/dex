"""Narrow live contract for the dedicated ClickHouse Cloud service.

The free container suite remains exhaustive. This file covers only the Cloud
boundary that a container cannot: deployment corroboration, live capacity,
compute-unit translation, control-plane agreement, server-enforced identity
isolation, and one real dbt build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

from .conftest import integration_budget

pytestmark = [pytest.mark.integration, pytest.mark.clickhouse_cloud]


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    return rc, json.loads(out)


def _seed_repo(root: Path) -> None:
    max_seconds = float(os.environ["DEX_TEST_CH_CLOUD_MAX_SECONDS"])
    config = {
        "connector": "clickhouse",
        "budget": integration_budget(max_seconds),
        "clickhouse": {
            "host": os.environ["DEX_TEST_CH_CLOUD_HOST"],
            "port": int(os.environ["DEX_TEST_CH_CLOUD_PORT"]),
            "database": os.environ["DEX_TEST_CH_CLOUD_DATABASE"],
            "user": "dex_ci_ro",
            "secure": True,
            "databases": [os.environ["DEX_TEST_CH_CLOUD_DATABASE"]],
            "dev_database": os.environ["DEX_TEST_CH_CLOUD_DEV_DATABASE"],
            "deployment": "cloud",
            "compute_unit_price_usd": float(
                os.environ["DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD"]
            ),
        },
    }
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.fixture
def cloud_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_repo(tmp_path)
    monkeypatch.setenv("CLICKHOUSE_URL", os.environ["DEX_TEST_CH_CLOUD_DSN"])
    return tmp_path


def _assert_sanitized(envelope: dict) -> None:
    payload = json.dumps(envelope)
    for env_name in (
        "DEX_TEST_CH_CLOUD_DSN",
        "DEX_TEST_CH_CLOUD_DEV_PASSWORD",
        "DEX_TEST_CH_CLOUD_API_KEY",
        "DEX_TEST_CH_CLOUD_API_SECRET",
    ):
        value = os.environ.get(env_name)
        if value and value in payload:
            pytest.fail(f"envelope leaked {env_name}")
    if re.search(r"https?://[^/@\s:]+:[^/@\s]+@", payload):
        pytest.fail("envelope leaked URL userinfo")
    for key in ("password", "api_secret", "api_key", "dsn"):
        assert f'"{key}"' not in payload.lower()


def _assert_ok(rc: int, envelope: dict) -> None:
    assert rc == 0, (
        f"errors={envelope.get('errors')} reason={envelope.get('reason')} "
        f"warnings={envelope.get('warnings')}"
    )


def test_connect_reports_cloud_capacity_and_matches_control_plane(
    cloud_repo: Path, capsys
):
    argv = ["--repo-root", str(cloud_repo), "connect", "test"]
    for attempt in range(3):
        rc, envelope = _run(argv, capsys)
        if rc == 0 or attempt == 2:
            break
        time.sleep(1)
    _assert_ok(rc, envelope)
    data = envelope["data"]
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert data["paradigm"] == "compute_time"
    assert data["deployment"] == "cloud"
    assert data["read_only"] is True and data["session_read_only"] is True
    compute = data["compute"]
    assert compute["replica_count"] >= 1
    assert compute["total_memory_gib"] > 0
    assert compute["compute_units_per_hour"] == pytest.approx(
        compute["total_memory_gib"] / 8
    )
    assert compute["approximate"] is True

    control_env = os.environ.copy()
    control_env["CLICKHOUSE_CLOUD_API_KEY"] = os.environ["DEX_TEST_CH_CLOUD_API_KEY"]
    control_env["CLICKHOUSE_CLOUD_API_SECRET"] = os.environ[
        "DEX_TEST_CH_CLOUD_API_SECRET"
    ]
    clickhousectl = shutil.which("clickhousectl")
    assert clickhousectl is not None
    result = subprocess.run(  # noqa: S603 - fixed executable, explicit argv
        [
            clickhousectl,
            "cloud",
            "service",
            "get",
            os.environ["DEX_TEST_CH_CLOUD_SERVICE_ID"],
            "--org-id",
            os.environ["DEX_TEST_CH_CLOUD_ORG_ID"],
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=control_env,
    )
    service = json.loads(result.stdout)
    scaling = service["currentScaling"]
    expected_replicas = scaling["effectiveMinReplicas"]
    assert scaling["effectiveMaxReplicas"] == expected_replicas
    assert compute["replica_count"] == expected_replicas
    assert compute["total_memory_gib"] == pytest.approx(
        expected_replicas * scaling["effectiveMinReplicaMemoryGb"]
    )
    _assert_sanitized(envelope)


def test_unconfirmed_then_confirmed_profile_translates_and_ledgers_cloud_spend(
    cloud_repo: Path, capsys
):
    budget = os.environ["DEX_TEST_CH_CLOUD_MAX_SECONDS"]
    argv = [
        "--repo-root",
        str(cloud_repo),
        "explore",
        "profile",
        f"{os.environ['DEX_TEST_CH_CLOUD_DATABASE']}.customers",
        "--budget",
        budget,
    ]
    rc, request = _run(argv, capsys)
    _assert_ok(rc, request)
    assert request["status"] == "needs_confirmation", request.get("status")
    assert request["cost"]["paradigm"] == "compute_time"
    assert request["data"]["estimated_compute_unit_hours"] > 0
    assert request["data"]["estimated_usd"] > 0
    assert not (cloud_repo / ".dex" / "cache.json").exists()

    rc, envelope = _run([*argv, "--confirm"], capsys)
    _assert_ok(rc, envelope)
    spend = envelope["data"]["spend"]
    assert spend["seconds_billed"] > 0
    assert spend["compute_unit_hours_billed"] > 0
    assert spend["usd_billed"] >= 0
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert (cloud_repo / ".dex" / "cache.json").exists()
    _assert_sanitized(envelope)


def test_read_identity_is_server_enforced(cloud_repo: Path):
    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import DatabaseError

    try:
        client = clickhouse_connect.get_client(dsn=os.environ["DEX_TEST_CH_CLOUD_DSN"])
    except Exception as exc:
        pytest.fail(f"read identity connection failed: {type(exc).__name__}")
    app = os.environ["DEX_TEST_CH_CLOUD_DATABASE"]
    with pytest.raises(DatabaseError):
        client.command(
            f"INSERT INTO {app}.signups VALUES (1, 'blocked', now())"  # noqa: S608
        )
    with pytest.raises(DatabaseError):
        client.command(f"CREATE TABLE {app}.blocked (id UInt8) ENGINE=Memory")


def test_minimal_dbt_build_writes_only_to_cloud_dev_database(
    cloud_repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    app = os.environ["DEX_TEST_CH_CLOUD_DATABASE"]
    dev = os.environ["DEX_TEST_CH_CLOUD_DEV_DATABASE"]
    budget = os.environ["DEX_TEST_CH_CLOUD_MAX_SECONDS"]

    rc, envelope = _run(
        [
            "--repo-root",
            str(cloud_repo),
            "transform",
            "init",
            "chcloud",
            "--connector",
            "clickhouse",
        ],
        capsys,
    )
    _assert_ok(rc, envelope)
    project = cloud_repo / "chcloud"
    profile_path = project / "profiles.yml"
    profile = profile_path.read_text(encoding="utf-8")
    profile_path.write_text(
        re.sub(r"^(\s*user:).*$", r"\1 dex_ci_dbt", profile, count=1, flags=re.M),
        encoding="utf-8",
    )
    models = project / "models" / "staging"
    (models / "cloud_sources.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {"name": "app", "schema": app, "tables": [{"name": "orders"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (models / "cloud_orders.sql").write_text(
        "select id, customer_id, status, total, ordered_at\n"
        "from {{ source('app', 'orders') }}\nlimit 10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "CLICKHOUSE_PASSWORD", os.environ["DEX_TEST_CH_CLOUD_DEV_PASSWORD"]
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(cloud_repo),
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            "cloud_orders",
            "--confirm",
            "--budget",
            budget,
        ],
        capsys,
    )
    _assert_ok(rc, envelope)
    assert envelope["data"]["success"] is True
    assert envelope["data"]["spend"]["compute_unit_hours_billed"] >= 0
    _assert_sanitized(envelope)

    import clickhouse_connect

    dbt_client = clickhouse_connect.get_client(
        host=os.environ["DEX_TEST_CH_CLOUD_HOST"],
        port=int(os.environ["DEX_TEST_CH_CLOUD_PORT"]),
        username="dex_ci_dbt",
        password=os.environ["DEX_TEST_CH_CLOUD_DEV_PASSWORD"],
        database=dev,
        secure=True,
    )
    assert (
        dbt_client.query(
            "SELECT count() FROM system.tables WHERE database = {db:String} "
            "AND name = 'cloud_orders'",
            parameters={"db": dev},
        ).result_rows[0][0]
        == 1
    )
    assert (
        dbt_client.query(
            "SELECT count() FROM system.tables WHERE database = {db:String} "
            "AND name = 'cloud_orders'",
            parameters={"db": app},
        ).result_rows[0][0]
        == 0
    )
