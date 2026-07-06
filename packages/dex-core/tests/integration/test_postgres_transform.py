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
    assert data["seconds_billed"] > 0
    assert any("statement_timeout" in w for w in envelope["warnings"])

    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.splitlines()[-1])
    assert entry["connector"] == "postgres"
    assert entry["command"] == "transform build"
    assert entry["billed_seconds"] > 0
