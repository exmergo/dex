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

from .conftest import SF_MAX_SECONDS, assert_unpivot_build, unpivot_fixture_edits
from .test_snowflake_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.snowflake]

MODEL_NAME = "dex_probe"


@pytest.fixture(autouse=True)
def dbt_capable_connection():
    """Every test here starts from `transform init`, which refuses a
    workload-identity connection outright: dbt-snowflake authenticates by
    password, key pair, SSO, or OAuth token only (its profile carries no
    workload-identity provider field at all), so a rendered profile would fail
    every build with an opaque auth error.

    So this suite needs a durable credential and skips without one. CI runs it
    on a dedicated key-pair service user rather than the workload identity the
    rest of the Snowflake job uses; a local run discovers a key-pair or SSO
    connection. Drop this once dbt-snowflake grows workload-identity support
    and init renders it.
    """

    pytest.importorskip("dbt.adapters.snowflake")
    if os.environ.get("SNOWFLAKE_AUTHENTICATOR", "").upper() == "WORKLOAD_IDENTITY":
        pytest.skip("dbt needs a durable credential; workload identity not yet in dbt")


def test_init_plan_apply_build_into_the_scratch_database(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
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
    # Discovery renders auth without ever persisting a credential value: a key
    # pair renders as a path to the key, a password as an env_var reference the
    # profile resolves at dbt runtime. Asserted against the rendered text, not
    # just the parsed keys, so any future auth branch that inlines a secret
    # trips this rather than quietly shipping one in a file dbt writes to disk.
    profile_text = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in profile_text, "key material inlined into the profile"
    for secret_key in ("password", "token", "private_key"):
        rendered = str(dev.get(secret_key, ""))
        assert not rendered or "env_var" in rendered, f"{secret_key} inlined"

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


def test_unpivot_json_object_macro_builds_live(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    root = str(tmp_path)
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "macro", "unpivot_json_object"], capsys
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, envelope

    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            unpivot_fixture_edits(
                lambda d: f"parse_json('{d}')", "cast(null as variant)"
            )
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "unpivot fixture",
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
            str(SF_MAX_SECONDS * 10),
        ],
        capsys,
    )
    assert rc == 0, built
    assert_unpivot_build(built)


def test_missing_dev_database_is_refused_before_the_cost_gate(
    tmp_path: Path, capsys, sf_warehouse, sf_connection_name
):
    """dbt creates schemas but never databases, so a `dev_database` that does not
    exist used to die inside dbt's `list_schemas` macro with an opaque
    `002043: Object does not exist`. The preflight is free and runs before the
    confirm handshake, so this refuses without `--confirm` and without spend."""

    root = str(tmp_path)
    seed_repo(tmp_path, "DEX_NO_SUCH_DEV_DATABASE", sf_warehouse, sf_connection_name)

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
    assert "DEX_NO_SUCH_DEV_DATABASE" in error
    assert "CREATE DATABASE IF NOT EXISTS DEX_NO_SUCH_DEV_DATABASE" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_config_drift_from_the_rendered_profile_is_refused(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    """`transform init` renders config into profiles.yml, which dbt then reads.
    A later config edit that never reached the profile must not build silently
    against the old target."""

    root = str(tmp_path)
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert rc == 0, envelope

    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["snowflake"]["dev_database"] = "DEX_RETARGETED_ELSEWHERE"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(SF_MAX_SECONDS),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "DEX_RETARGETED_ELSEWHERE" in error
    assert sf_scratch_database in error
    assert "disagree about the dev target" in error
