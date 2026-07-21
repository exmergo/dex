"""Live transform against the seeded Postgres: init renders a secret-free
dbt-postgres profile from the discovered connection, and a confirmed build
lands only in the dedicated dbt_dev schema as the dbt_dev role, with actual
database-seconds recorded to the ledger and the ceiling injected as a
statement_timeout via PGOPTIONS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .conftest import assert_unpivot_build, unpivot_fixture_edits
from .test_postgres_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def dev_dsn(pg_dsn, pg_dev_password, monkeypatch) -> str:
    """The pg_dsn rewritten to the dbt_dev role (builds need write access to
    the dev schema; dex_ro deliberately has none). Password travels via
    PGPASSWORD, exactly as the rendered profile expects."""

    if not pg_dev_password:
        pytest.skip("set DEX_TEST_PG_DEV_PASSWORD (the dbt_dev role's password)")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    fields = {
        k: v for k, v in conninfo_to_dict(pg_dsn).items() if k not in ("password",)
    }
    fields["user"] = "dbt_dev"
    dsn = make_conninfo(**fields)
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("PGPASSWORD", pg_dev_password)
    return dsn


def test_init_and_build_write_only_the_dev_schema(
    tmp_path: Path, capsys, dev_dsn, pg_dev_password
):
    seed_repo(tmp_path, schemas=["app"], budget=300)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert rc == 0, envelope

    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    profile = yaml.safe_load(rendered)["analytics"]["outputs"]["dev"]
    assert profile["type"] == "postgres"
    assert profile["schema"] == "dbt_dev"
    assert profile["threads"] == 1
    # Never a secret in the rendered profile: the password field is an
    # env_var reference, not a value. (A substring check on the password
    # would false-positive here: the seeded dev password equals the role and
    # schema name, which legitimately appear in the profile.)
    assert profile["password"] == "{{ env_var('PGPASSWORD', '') }}"  # noqa: S105

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
            "300",
        ],
        capsys,
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["success"] is True
    assert any(n["name"] == "stg_orders" for n in data["nodes"])
    # The build is priced upfront now: a database-seconds estimate from the free
    # EXPLAIN planner preflight is surfaced before the run.
    assert envelope["cost"]["estimate"] is not None
    assert envelope["cost"]["estimate"] > 0
    assert data["seconds_billed"] > 0
    assert any("statement_timeout" in w for w in envelope["warnings"])

    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.splitlines()[-1])
    assert entry["connector"] == "postgres"
    assert entry["command"] == "transform build"
    assert entry["billed_seconds"] > 0


def test_unpivot_json_object_macro_builds_live(tmp_path: Path, capsys, dev_dsn):
    seed_repo(tmp_path, schemas=["app"], budget=300)
    root = str(tmp_path)
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
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
        json.dumps(unpivot_fixture_edits(lambda d: f"'{d}'::jsonb", "null::jsonb")),
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
        ["--repo-root", root, "transform", "build", "--confirm", "--budget", "300"],
        capsys,
    )
    assert rc == 0, built
    assert_unpivot_build(built)


def test_an_unwritable_dev_schema_is_refused_before_the_cost_gate(
    tmp_path: Path, capsys, dev_dsn
):
    """dbt creates its dev schema, but only if the role may. The seeded dbt_dev
    role holds CREATE on the dbt_dev schema and nothing else, so a dev schema that
    does not exist yet cannot be created: the first build would die on a bare
    permission error naming neither the schema nor the grant. The preflight is
    free (catalog lookups and privilege predicates) and runs before the confirm
    handshake, so this refuses without `--confirm` and without load."""

    pytest.importorskip("dbt.adapters.postgres")
    root = str(tmp_path)
    seed_repo(tmp_path, schemas=["app"], budget=300)
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["postgres"]["dev_schema"] = "dex_absent_dev_schema"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
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
