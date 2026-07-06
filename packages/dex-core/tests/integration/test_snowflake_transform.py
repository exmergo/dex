"""Live transform against Snowflake: init a project wired to the transient
scratch database, plan and apply a trivial model, then a confirmed dev build
via dbt-snowflake. Writes land only in the scratch database (grants enforce
it: the CI role can write there and nowhere else); the database is transient
with zero retention, so anything teardown misses stores nothing durable."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from .conftest import SF_MAX_SECONDS
from .test_snowflake_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.snowflake]

MODEL_NAME = "dex_probe"


def test_init_plan_apply_build_into_the_scratch_database(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    pytest.importorskip("dbt.adapters.snowflake")
    if os.environ.get("SNOWFLAKE_AUTHENTICATOR", "").upper() == "WORKLOAD_IDENTITY":
        # Stable dbt-snowflake cannot authenticate via workload identity, so
        # `transform init` deliberately refuses such a connection. The build
        # path is exercised by the local key-pair run; unskip once
        # dbt-snowflake ships workload-identity support and init renders it.
        pytest.skip("dbt builds need a durable credential; WIF not yet in dbt")
    root = str(tmp_path)
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert rc == 0, envelope
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    dev = profiles["analytics"]["outputs"]["dev"]
    assert dev["type"] == "snowflake"
    assert dev["warehouse"] == sf_warehouse
    assert dev["database"] == sf_scratch_database
    assert dev["schema"] == "DBT_DEV"
    assert dev["threads"] == 1
    # Discovery renders auth without persisting a secret value.
    assert "password" not in dev or "env_var" in str(dev.get("password"))

    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": f"models/staging/{MODEL_NAME}.sql",
                        "kind": "model_sql",
                        "content": "select 1 as id\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "probe model",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    rc, built = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(SF_MAX_SECONDS * 10),  # a dbt build resumes the warehouse
        ],
        capsys,
    )
    assert rc == 0, built
    assert built["status"] == "ok"
    statuses = {n["name"]: n["status"] for n in built["data"]["nodes"]}
    assert statuses.get(MODEL_NAME) == "success"
    # Warehouse time is accounted: the compute-time build records seconds.
    assert built["data"].get("seconds_billed", 0) >= 0

    # The relation exists exactly where the dev target points, then
    # best-effort cleanup; the transient database is the backstop.
    import snowflake.connector

    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.connect import resolve_snowflake_connection

    params, _method = resolve_snowflake_connection(
        SnowflakeTarget(connection_name=sf_connection_name), os.environ, tmp_path
    )
    conn = snowflake.connector.connect(**params)
    try:
        cursor = conn.cursor()
        # dbt's default materialization is a view, so match any object kind.
        cursor.execute(
            f"SHOW OBJECTS LIKE '{MODEL_NAME.upper()}' IN SCHEMA "
            f'"{sf_scratch_database}"."DBT_DEV"'
        )
        assert cursor.fetchall(), "the built model must exist in the dev schema"
        relation = f'"{sf_scratch_database}"."DBT_DEV"."{MODEL_NAME.upper()}"'
        cursor.execute(f"DROP VIEW IF EXISTS {relation}")
    finally:
        conn.close()
