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


@pytest.fixture(autouse=True)
def non_interactive_credential():
    """A dbt build here must never depend on a human at a browser.

    `transform init` renders a user-OAuth connection as `auth_type: oauth`, and
    dbt-databricks then runs its own browser flow. In CI that never happens (the
    workflow exchanges the GitHub OIDC token and exports DATABRICKS_TOKEN, which
    dbt reads through the same env_var reference a PAT would), but a local run
    discovering a user-OAuth profile would sit for ten minutes waiting for a
    browser that no test harness will ever open, and then fail on a timeout that
    names none of this.

    So the requirement is stated rather than stumbled into: skip unless the
    discovered credential is one dbt can use unattended.
    """

    pytest.importorskip("databricks.sql")
    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.connect import resolve_databricks_connection

    try:
        _config, method = resolve_databricks_connection(
            DatabricksTarget(), os.environ, "."
        )
    except Exception as exc:  # no connection at all: the suite's own gate reports it
        pytest.skip(f"no Databricks connection discovered ({type(exc).__name__})")
    if method.rsplit(":", 1)[-1] == "oauth_user":
        pytest.skip(
            "dbt needs a credential it can use unattended; the discovered "
            "connection is user OAuth, which sends dbt to a browser. Export "
            "DATABRICKS_TOKEN (for example: databricks auth token -p <profile>)"
        )


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


def test_missing_dev_catalog_is_refused_before_the_cost_gate(
    tmp_path: Path, capsys, dbx_warehouse
):
    """dbt creates schemas but never catalogs, so a `dev_catalog` that does not
    exist dies inside the `create schema` dbt issues, naming neither the catalog
    nor the fix. The preflight is free (Unity Catalog REST, no SQL session) and
    runs before the confirm handshake, so this refuses without `--confirm` and
    without spend."""

    pytest.importorskip("dbt.adapters.databricks")
    root = str(tmp_path)
    seed_repo(tmp_path, dbx_warehouse, "dex_no_such_dev_catalog")

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert rc == 0, envelope

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "dex_no_such_dev_catalog" in error
    assert "CREATE CATALOG IF NOT EXISTS dex_no_such_dev_catalog" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()
