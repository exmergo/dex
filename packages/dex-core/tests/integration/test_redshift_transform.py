"""Live transform against the seeded Redshift workgroup: init renders a
secret-free dbt-redshift profile from the discovered connection, and a
confirmed build lands only in the dedicated dbt_dev schema as the dbt_dev
user, with actual compute-seconds recorded to the ledger. These tests use the
password path (a dedicated dbt_dev user from scripts/setup_redshift_ci.sh)
because it exercises the env_var rendering; the IAM profile path is dogfooded
manually and needs no durable credential."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from .conftest import RS_MAX_SECONDS, assert_unpivot_build, unpivot_fixture_edits
from .test_redshift_connect import run_cli

pytestmark = [pytest.mark.integration, pytest.mark.redshift]

# The knob is DEX_TEST_REDSHIFT_MAX_SECONDS (default 60): a dbt build wakes
# the workgroup (60s minimum) and runs a handful of tiny models, so its
# budget is a fixed multiple of the knob.
BUILD_BUDGET = RS_MAX_SECONDS * 5


@pytest.fixture
def dev_env(rs_host, rs_database, rs_dev_password, monkeypatch) -> None:
    """The REDSHIFT_* environment pointed at the dbt_dev user (builds need
    write access to the dev schema; dex_ro deliberately has none). The
    password travels via REDSHIFT_PASSWORD, exactly as the rendered profile
    expects."""

    if not rs_host or not rs_database or not rs_dev_password:
        pytest.skip(
            "set DEX_TEST_REDSHIFT_HOST, DEX_TEST_REDSHIFT_DATABASE, and "
            "DEX_TEST_REDSHIFT_DEV_PASSWORD (the dbt_dev user's password)"
        )
    monkeypatch.setenv("REDSHIFT_HOST", rs_host)
    monkeypatch.setenv("REDSHIFT_DATABASE", rs_database)
    monkeypatch.setenv("REDSHIFT_USER", "dbt_dev")
    monkeypatch.setenv("REDSHIFT_PASSWORD", rs_dev_password)


def seed_repo_password_path(
    root: Path, *, schemas: list[str], budget: float, dev_schema: str = "dbt_dev"
) -> None:
    """A config block without the workgroup pin, so discovery reads the
    REDSHIFT_* environment (the password path the rendered profile uses)."""

    config = {
        "connector": "redshift",
        "redshift": {"dev_schema": dev_schema, "schemas": schemas},
        "budget": {"ceiling": budget},
    }
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_init_and_build_write_only_the_dev_schema(tmp_path: Path, capsys, dev_env):
    seed_repo_password_path(tmp_path, schemas=["app"], budget=BUILD_BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "analytics",
            "--connector",
            "redshift",
        ],
        capsys,
    )
    assert rc == 0, envelope

    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    profile = yaml.safe_load(rendered)["analytics"]["outputs"]["dev"]
    assert profile["type"] == "redshift"
    assert profile["schema"] == "dbt_dev"
    assert profile["threads"] == 1
    # Never a secret in the rendered profile: the password field is an
    # env_var reference, not a value.
    assert profile["password"] == "{{ env_var('REDSHIFT_PASSWORD') }}"  # noqa: S105
    assert os.environ["REDSHIFT_PASSWORD"] not in rendered

    (tmp_path / "analytics" / "models" / "staging" / "stg_orders.sql").write_text(
        "SELECT id AS order_id, customer_id, status, total\nFROM app.orders\n",
        encoding="utf-8",
    )

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["success"] is True
    assert any(n["name"] == "stg_orders" for n in data["nodes"])
    # The build is priced upfront now: a heuristic compute-seconds estimate is
    # surfaced before the run (the 5x budget covers it comfortably).
    assert envelope["cost"]["estimate"] is not None
    assert envelope["cost"]["estimate"] > 0
    assert data["seconds_billed"] > 0
    assert any("statement_timeout" in w for w in envelope["warnings"])

    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.splitlines()[-1])
    assert entry["connector"] == "redshift"
    assert entry["command"] == "transform build"
    assert entry["billed_seconds"] > 0


def test_unpivot_json_object_macro_builds_live(tmp_path: Path, capsys, dev_env):
    """Also settles the two Redshift questions the macro carries: PartiQL
    UNPIVOT accepts the qualified column expression, and the AT key arrives as
    the plain string the accepted_values test compares against."""

    seed_repo_password_path(tmp_path, schemas=["app"], budget=BUILD_BUDGET)
    root = str(tmp_path)
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "redshift",
        ],
        capsys,
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
            unpivot_fixture_edits(lambda d: f"json_parse('{d}')", "cast(null as super)")
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
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 0, built
    assert_unpivot_build(built)


def test_an_unwritable_dev_schema_is_refused_before_the_cost_gate(
    tmp_path: Path, capsys, dev_env
):
    """dbt creates its dev schema, but only if the user may. The seeded
    dbt_dev user holds CREATE on the dbt_dev schema and nothing else, so a
    dev schema that does not exist yet cannot be created: the first build
    would die on a bare permission error naming neither the schema nor the
    grant. The preflight is cheap (catalog lookups and privilege predicates)
    and runs before the confirm handshake, so this refuses without
    `--confirm` and without spend."""

    pytest.importorskip("dbt.adapters.redshift")
    root = str(tmp_path)
    seed_repo_password_path(
        tmp_path,
        schemas=["app"],
        budget=BUILD_BUDGET,
        dev_schema="dex_absent_dev_schema",
    )

    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "redshift",
        ],
        capsys,
    )
    assert rc == 0, envelope

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "dex_absent_dev_schema" in error
    assert "may not create it" in error
    assert "CREATE SCHEMA IF NOT EXISTS dex_absent_dev_schema AUTHORIZATION" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()
