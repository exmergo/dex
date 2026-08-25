"""Live `connect test` against ClickHouse: connection discovery, capabilities
envelope, and the sanitizer, end to end. Free: capabilities is one system-table
round-trip, no table scan. The target is the seeded container from
scripts/setup_clickhouse_dev.sh (which CI runs too), connected as the read-only
dex_ro user."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.clickhouse]


def seed_repo(
    root: Path,
    *,
    databases: list[str] | None = None,
    budget: float | None = None,
    max_full_profile_bytes: int | None = None,
) -> None:
    clickhouse: dict = {"dev_database": "dbt_dev"}
    if databases is not None:
        clickhouse["databases"] = databases
    if max_full_profile_bytes is not None:
        clickhouse["max_full_profile_bytes"] = max_full_profile_bytes
    config: dict = {"connector": "clickhouse", "clickhouse": clickhouse}
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
    tmp_path: Path, capsys, ch_dsn, monkeypatch
):
    monkeypatch.setenv("CLICKHOUSE_URL", ch_dsn)
    seed_repo(tmp_path)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "clickhouse"
    assert data["dialect"] == "clickhouse"
    assert data["read_only"] is True
    # Reported from the server's own `readonly` setting, not assumed.
    assert data["session_read_only"] is True
    assert data["paradigm"] == "db_load"
    assert data["deployment"] == "self_hosted"
    assert data["database_count"] >= 1
    assert envelope["cost"]["paradigm"] == "db_load"
    # The auth method is coarse; no identity, password, or DSN crosses.
    assert data["auth_method"].split(":")[0] in {
        "environment",
        "config_target",
        "dbt_profile",
    }
    payload = json.dumps(envelope)
    assert "dex_ro:" not in payload  # no user:password fragment
    assert not _identity_keys(data)


def test_a_bogus_scope_is_refused_for_free_and_names_what_exists(
    tmp_path: Path, capsys, ch_dsn, monkeypatch
):
    """Scope resolution runs before anything is estimated, so a typo costs
    nothing and says what would have worked."""

    monkeypatch.setenv("CLICKHOUSE_URL", ch_dsn)
    seed_repo(tmp_path)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "--scope", "nope", "connect", "test"], capsys
    )
    assert rc == 1
    message = envelope["errors"][0]
    assert "nope" in message
    assert "app" in message, "the refusal should list the databases that do exist"


def test_a_write_is_refused_by_the_server_not_only_by_the_guard(ch_dsn):
    """Read-only in depth: the SELECT-only guard refuses first, but the session
    the adapter opens is itself read-only, so even a statement that somehow got
    past the guard cannot mutate anything.

    Asserted against the live server because `readonly = 2` is a claim about the
    server's behavior, not about dex's code.
    """

    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import DatabaseError

    client = clickhouse_connect.get_client(dsn=ch_dsn, settings={"readonly": 2})
    for statement in (
        "INSERT INTO app.signups VALUES (1, 'a', now())",
        "CREATE TABLE app.should_not_exist (a UInt8) ENGINE = Memory",
        "DROP TABLE app.signups",
        "ALTER TABLE app.customers DROP COLUMN email",
    ):
        with pytest.raises(DatabaseError):
            client.command(statement)


def test_explain_estimate_is_permitted_under_read_only(ch_dsn):
    """The whole free-estimate design rests on this: EXPLAIN ESTIMATE executes
    nothing, so a read-only session must be allowed to ask for it. If a server
    refused it, every estimate would silently degrade to the whole-relation
    fallback."""

    import clickhouse_connect

    client = clickhouse_connect.get_client(dsn=ch_dsn, settings={"readonly": 2})
    # A bare count() is answered from part metadata without reading anything, so
    # it legitimately estimates no rows; the statement has to actually read a
    # column for there to be an estimate at all. That distinction is exactly why
    # the adapter keeps a system.tables fallback and reports estimate_basis.
    result = client.query("EXPLAIN ESTIMATE SELECT uniq(session_id) FROM app.events")
    assert result.column_names == ("database", "table", "parts", "rows", "marks")
    assert result.result_rows, "a scanning statement should return an estimate row"
    row = result.result_rows[0]
    assert row[0] == "app" and row[1] == "events"
    assert row[3] > 0, "the estimate should name a row count"


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
