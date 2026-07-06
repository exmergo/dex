"""Live `connect test` against Postgres: connection discovery, capabilities
envelope, and the sanitizer, end to end. Free: capabilities is one catalog
round-trip, no table scan. The target is the seeded container from
scripts/setup_postgres_dev.sh (or the CI service container), connected as the
read-only dex_ro role."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def seed_repo(
    root: Path,
    *,
    schemas: list[str] | None = None,
    budget: float | None = None,
    max_full_profile_bytes: int | None = None,
) -> None:
    postgres: dict = {"dev_schema": "dbt_dev"}
    if schemas is not None:
        postgres["schemas"] = schemas
    if max_full_profile_bytes is not None:
        postgres["max_full_profile_bytes"] = max_full_profile_bytes
    config: dict = {"connector": "postgres", "postgres": postgres}
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
    tmp_path: Path, capsys, pg_dsn, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    seed_repo(tmp_path)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "postgres"
    assert data["dialect"] == "postgres"
    assert data["read_only"] is True
    assert data["session_read_only"] is True
    assert data["paradigm"] == "db_load"
    assert data["schema_count"] >= 1
    assert envelope["cost"]["paradigm"] == "db_load"
    # The auth method is coarse; no identity, password, or DSN crosses.
    assert data["auth_method"].split(":")[0] in {
        "config_service",
        "database_url",
        "environment",
        "config_target",
        "dbt_profile",
    }
    payload = json.dumps(envelope)
    assert "dex_ro:" not in payload  # no user:password fragment
    assert not _identity_keys(data)


def _identity_keys(value, path="data") -> list[str]:
    """Every key in the payload that names an identity or credential."""

    hits: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if str(key).lower() in {"user", "username", "login", "login_name"}:
                hits.append(f"{path}.{key}")
            hits.extend(_identity_keys(sub, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            hits.extend(_identity_keys(sub, f"{path}[{i}]"))
    return hits
