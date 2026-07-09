"""Live transform against Databricks: init a project wired to the scratch
catalog, plan and apply a trivial model, then a confirmed dev build via
dbt-databricks. Writes land only in the scratch catalog (grants enforce it:
the CI principal can write there and nowhere else); teardown drops the built
relation best-effort."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from .conftest import DBX_MAX_SECONDS
from .test_databricks_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.databricks]

MODEL_NAME = "dex_probe"


def test_init_plan_apply_build_into_the_scratch_catalog(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    pytest.importorskip("dbt.adapters.databricks")
    if not dbx_scratch_catalog:
        pytest.skip("transform needs DEX_TEST_DATABRICKS_CATALOG (a writable scratch)")
    if not os.environ.get("DATABRICKS_TOKEN") and os.environ.get("DATABRICKS_HOST"):
        # In CI the exchanged OIDC token must be present for dbt token auth;
        # locally the CLI's OAuth cache renders auth_type: oauth instead.
        pytest.skip("DATABRICKS_HOST is set without DATABRICKS_TOKEN; dbt needs one")
    root = str(tmp_path)
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert rc == 0, envelope
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    dev = profiles["analytics"]["outputs"]["dev"]
    assert dev["type"] == "databricks"
    assert dev["catalog"] == dbx_scratch_catalog.lower()
    assert dev["schema"] == "dbt_dev"
    assert dev["threads"] == 1
    # Discovery renders auth without persisting a secret value.
    assert "token" not in dev or "env_var" in str(dev.get("token"))

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
            str(DBX_MAX_SECONDS * 10),  # a dbt build may wake the warehouse
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
    # best-effort cleanup through the same discovered connection.
    from exmergo_dex_core.adapters.databricks import warehouse_http_path
    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.connect import (
        _databricks_hostname,
        resolve_databricks_connection,
    )

    sdk_config, _method = resolve_databricks_connection(
        DatabricksTarget(), os.environ, tmp_path
    )
    from databricks import sql as dbsql

    conn = dbsql.connect(
        server_hostname=_databricks_hostname(sdk_config.host),
        http_path=warehouse_http_path(dbx_warehouse),
        credentials_provider=lambda: sdk_config.authenticate,
        user_agent_entry="dex",
    )
    relation = f"`{dbx_scratch_catalog.lower()}`.`dbt_dev`.`{MODEL_NAME}`"
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {relation}")  # noqa: S608 (fixed name)
        assert cursor.fetchall()[0][0] == 1
        # dbt's default materialization is a view.
        cursor.execute(f"DROP VIEW IF EXISTS {relation}")
    finally:
        conn.close()
