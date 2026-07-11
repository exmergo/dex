"""The dev-target preflight: config that looks live is live, or says so.

Drift (`.dex/config.yml` edited after `transform init` rendered `profiles.yml`)
and absence (a dev database dbt cannot create for itself) are both refusals, both
free, and both happen before the cost gate. The drift half needs no connection at
all, which is why most of this file is pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.config import (
    DexConfig,
    DuckDBTarget,
    SnowflakeTarget,
)
from exmergo_dex_core.transform import dev_target


def _write_profile(project: Path, output: str, target: str = "dev") -> None:
    (project / "profiles.yml").write_text(
        f"dex_test:\n  target: {target}\n  outputs:\n    {target}:\n{output}",
        encoding="utf-8",
    )


def _snowflake_profile(project: Path, **overrides: str) -> None:
    fields = {
        "type": "snowflake",
        "account": "ORG-ACCT",
        "user": "DEX",
        "warehouse": "DEX_WH",
        "database": "DBT_DEV",
        "schema": "DBT_DEV",
        # Quoted exactly as `transform init` renders it: unquoted Jinja is not
        # valid YAML, for dex or for dbt.
        "password": "'{{ env_var(''SNOWFLAKE_PASSWORD'') }}'",
        **overrides,
    }
    _write_profile(project, "".join(f"      {k}: {v}\n" for k, v in fields.items()))


def _snowflake_config(**overrides) -> DexConfig:
    fields = {"warehouse": "DEX_WH", "dev_database": "DBT_DEV", "dev_schema": "DBT_DEV"}
    fields.update(overrides)
    return DexConfig(connector="snowflake", snowflake=SnowflakeTarget(**fields))


@pytest.fixture(autouse=True)
def no_warehouse(monkeypatch):
    """No connection reachable by default, so the drift half is exercised alone
    and no unit test can wander onto a real account. Tests that need a warehouse
    replace this with a fake adapter."""

    def unreachable(**_kwargs):
        raise RuntimeError("no connection discovered")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", unreachable)


# --- drift: config and profile must agree ------------------------------------------


def test_retargeted_dev_database_is_refused_naming_both_values(
    dbt_project_dir: Path, tmp_path: Path
):
    """The field report: edit `snowflake.dev_database`, get a green build against
    the old database. Now it refuses, and the message names both sides."""

    _snowflake_profile(dbt_project_dir)
    config = _snowflake_config(dev_database="DBT_DEV_DOES_NOT_EXIST")

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert "snowflake.dev_database: DBT_DEV_DOES_NOT_EXIST" in message
    assert "profiles.yml dev.database: DBT_DEV" in message
    assert ".dex/config.yml" in message and "analytics/profiles.yml" in message


def test_matching_config_and_profile_pass(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir)
    # No connection is configured, so the existence half degrades to a note.
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_snowflake_identifier_case_is_not_drift(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir, database="DBT_DEV")
    config = _snowflake_config(dev_database="dbt_dev")
    dev_target.check(dbt_project_dir, "dev", config, tmp_path)


def test_a_drifted_warehouse_is_refused(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir, warehouse="DEX_WH")
    config = _snowflake_config(warehouse="BIG_WH")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "snowflake.warehouse: BIG_WH" in str(exc.value)


def test_an_unset_config_field_is_not_drift(dbt_project_dir: Path, tmp_path: Path):
    """A field the user never set was never a claim about the dev target, so a
    profile that names something there cannot have drifted away from it."""

    _snowflake_profile(dbt_project_dir, schema="ANALYTICS_DEV")
    config = DexConfig(
        connector="snowflake",
        snowflake=SnowflakeTarget(warehouse="DEX_WH", dev_database="DBT_DEV"),
    )
    dev_target.check(dbt_project_dir, "dev", config, tmp_path)


def test_a_switched_connector_is_refused(dbt_project_dir: Path, tmp_path: Path):
    """The shared fixture renders a duckdb profile; config says snowflake."""

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    message = str(exc.value)
    assert "disagree about the connector" in message
    assert "connector: snowflake" in message
    assert "type: duckdb" in message


def test_an_unreadable_profile_is_not_a_refusal(tmp_path: Path):
    """Absence of evidence stays absence of evidence: a project dex cannot read
    is dbt's problem to report, not a preflight refusal."""

    warnings = dev_target.check(tmp_path, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_a_malformed_profile_is_not_a_refusal(dbt_project_dir: Path, tmp_path: Path):
    """Unquoted Jinja is invalid YAML. dbt will say so; the preflight must not
    crash with a parser traceback on the way there."""

    _write_profile(dbt_project_dir, "      password: {{ env_var('X') }}\n")
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_drift_message_never_carries_a_credential(
    dbt_project_dir: Path, tmp_path: Path
):
    """The profile holds a password reference, a user, and an account. Only
    namespace identifiers may reach the message that goes into the envelope."""

    _snowflake_profile(dbt_project_dir)
    config = _snowflake_config(dev_database="OTHER_DB")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    for secret in ("password", "SNOWFLAKE_PASSWORD", "ORG-ACCT", "user"):
        assert secret not in message


# --- existence: duckdb keeps the behavior it always had -----------------------------


def test_duckdb_missing_file_with_sources_refuses(
    dbt_project_dir: Path, tmp_path: Path
):
    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n",
        encoding="utf-8",
    )
    config = DexConfig(connector="duckdb")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "seed" in str(exc.value)


def test_duckdb_missing_file_without_sources_only_warns(
    dbt_project_dir: Path, tmp_path: Path
):
    warnings = dev_target.check(
        dbt_project_dir, "dev", DexConfig(connector="duckdb"), tmp_path
    )
    assert len(warnings) == 1
    assert "dbt will create an empty one" in warnings[0]


def test_duckdb_path_drift_is_refused(dbt_project_dir: Path, tmp_path: Path):
    config = DexConfig(
        connector="duckdb", duckdb=DuckDBTarget(path="/elsewhere.duckdb")
    )
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "duckdb.path: /elsewhere.duckdb" in str(exc.value)


# --- existence: snowflake, through the fake ------------------------------------------


def test_missing_snowflake_dev_database_refuses_with_the_create_statement(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import FakeSnowflakeConnection, FakeWarehouse

    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    connection = FakeSnowflakeConnection(
        warehouses=[FakeWarehouse(name="DEX_WH")], empty_databases=["SOMETHING_ELSE"]
    )

    def fake_open_adapter(**_kwargs):
        return SnowflakeAdapter(
            connection=connection,
            cost_gate=CostGate(
                paradigm=Paradigm.COMPUTE_TIME,
                ceiling=None,
                session_ceiling=None,
                session_spent=0.0,
                confirmed=False,
                connector="snowflake",
            ),
            target=SnowflakeTarget(warehouse="DEX_WH"),
            clock=connection.clock,
        )

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", fake_open_adapter)
    _snowflake_profile(dbt_project_dir)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    message = str(exc.value)
    assert 'dev_database "DBT_DEV" does not exist' in message
    assert "CREATE DATABASE IF NOT EXISTS DBT_DEV;" in message
    assert "dbt creates schemas but never databases" in message
    # The preflight is free: it never reaches a warehouse.
    assert connection.data_statements == []


def test_an_unopenable_connection_degrades_to_a_note(
    dbt_project_dir: Path, tmp_path: Path
):
    """dex discovers its own connection while dbt reads profiles.yml; the two can
    legitimately differ, so the preflight must never break a build dbt could run.
    The note names the failure class, so a defect here cannot hide as a shrug."""

    _snowflake_profile(dbt_project_dir)
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert len(warnings) == 1
    assert "could not preflight the dev database" in warnings[0]
    assert "RuntimeError" in warnings[0]
