"""The Snowflake adapter against the stateful fake connection: metadata is
free (SHOW commands, no warehouse), every billed statement is estimated and
gated in warehouse-seconds, and the budget binds at both the client (charge)
and the simulated server (statement timeout)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("snowflake.connector")

from fakes.snowflake import FakeResult

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.snowflake import (
    _RESUME_MINIMUM_SECONDS,
    SnowflakeAdapter,
    SnowflakeConnectionError,
)
from exmergo_dex_core.config import SnowflakeTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)


def make_adapter(
    connection,
    *,
    ceiling: float | None = 600.0,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: SnowflakeTarget | None = None,
    scope_override: list[str] | None = None,
) -> SnowflakeAdapter:
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        confirmed=confirmed,
        connector="snowflake",
        command="explore profile",
        record=record,
    )
    return SnowflakeAdapter(
        connection=connection,
        cost_gate=gate,
        target=target or SnowflakeTarget(warehouse="DEX_WH"),
        account="TESTORG-TESTACCT",
        auth_method="named_connection:key_pair",
        scope_override=scope_override,
        clock=connection.clock,
    )


def data_statements(connection) -> list:
    return connection.data_statements


# --- metadata (free) ---------------------------------------------------------------


def test_capabilities_shape_and_free(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection)
    caps = adapter.capabilities()
    assert caps["connector"] == "snowflake"
    assert caps["dialect"] == "snowflake"
    assert caps["read_only"] is True
    assert caps["paradigm"] == "compute_time"
    assert caps["account"] == "TESTORG-TESTACCT"
    assert caps["auth_method"] == "named_connection:key_pair"
    assert caps["database_count"] == 1  # SHOP
    budget = caps["budget"]
    assert budget["ceiling_seconds"] == 600.0
    assert budget["warehouse"]["size"] == "X-Small"
    assert budget["warehouse"]["credits_per_hour"] == 1.0
    # 600 warehouse-seconds on an X-Small is a sixth of a credit.
    assert budget["ceiling_credits"] == pytest.approx(600 / 3600, abs=1e-6)
    # Capabilities is a free probe: SHOW commands only, no data statement.
    assert data_statements(fake_sf_connection) == []


def test_list_objects_uses_free_show_metadata_only(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "SHOP.PUBLIC.CUSTOMERS",
        "SHOP.PUBLIC.EVENTS",
    ]
    customers = next(o for o in objects if o.name == "CUSTOMERS")
    assert customers.row_count == 100
    assert customers.byte_size == 5_000_000_000
    assert customers.column_count == 2
    assert data_statements(fake_sf_connection) == []


def test_database_allowlist_scopes_inventory(fake_sf_connection):
    from fakes.snowflake import FakeSnowflakeTable

    fake_sf_connection.tables.append(
        FakeSnowflakeTable(
            database="OTHER",
            schema="PUBLIC",
            name="NOISE",
            columns=[("ID", "FIXED", True)],
        )
    )
    adapter = make_adapter(
        fake_sf_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SHOP.PUBLIC"]),
    )
    assert {o.schema for o in adapter.list_objects()} == {"PUBLIC"}
    assert all(o.identifier.startswith("SHOP.") for o in adapter.list_objects())


def test_table_metadata_parses_show_columns_types(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection)
    meta, columns = adapter.table_metadata("SHOP.PUBLIC.EVENTS")
    assert meta.identifier == "SHOP.PUBLIC.EVENTS"
    by_name = {c.name: c for c in columns}
    assert by_name["PAYLOAD"].data_type == "VARIANT"
    assert by_name["LABELS"].data_type == "ARRAY"
    assert by_name["ID"].nullable is True
    id_col = next(
        c for c in adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")[1] if c.name == "ID"
    )
    assert id_col.nullable is False


# --- estimation (free) and the resume floor ------------------------------------------


def test_profile_estimate_is_heuristic_and_carries_the_resume_floor(
    fake_sf_connection,
):
    adapter = make_adapter(fake_sf_connection)
    total, per_table = adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"])
    # 5 GB over the conservative X-Small rate, one batch.
    scan = per_table["SHOP.PUBLIC.CUSTOMERS"]
    assert scan > 1.0
    # DEX_WH is suspended, so the total carries the 60-second resume minimum.
    assert total == pytest.approx(scan + _RESUME_MINIMUM_SECONDS)
    assert data_statements(fake_sf_connection) == []


def test_no_resume_floor_on_a_running_warehouse(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    adapter = make_adapter(fake_sf_connection)
    total, per_table = adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"])
    assert total == pytest.approx(per_table["SHOP.PUBLIC.CUSTOMERS"])


def test_query_estimate_sums_referenced_tables(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    adapter = make_adapter(fake_sf_connection)
    single = adapter.query_estimate('SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"')
    joined = adapter.query_estimate(
        'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS" c '
        'JOIN "SHOP"."PUBLIC"."EVENTS" e ON c."ID" = e."ID"'
    )
    assert joined > single > 0


def test_describe_estimate_translates_to_credits(fake_sf_connection):
    adapter = make_adapter(
        fake_sf_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", credit_price_usd=3.0),
    )
    described = adapter.describe_estimate(360.0, {"SHOP.PUBLIC.CUSTOMERS": 360.0})
    assert described["estimated_seconds"] == 360.0
    assert described["estimate_quality"] == "heuristic"
    assert described["estimated_credits"] == pytest.approx(0.1)
    assert described["estimated_usd"] == pytest.approx(0.3)
    assert described["per_table_seconds"] == {"SHOP.PUBLIC.CUSTOMERS": 360.0}
    assert "--budget" in described["hint"] and "seconds" in described["hint"]
    assert any("no dry-run" in note for note in described["notes"])


# --- the billed door -----------------------------------------------------------------


def test_unconfirmed_execution_is_refused_before_any_run(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert data_statements(fake_sf_connection) == []


def test_over_ceiling_refused_client_side_without_execution(fake_sf_connection):
    # The 50 GB events table estimates far beyond a 5-second ceiling.
    adapter = make_adapter(fake_sf_connection, ceiling=5.0)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."EVENTS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert data_statements(fake_sf_connection) == []


def test_billed_statements_run_on_the_pinned_warehouse_with_query_tag(
    fake_sf_connection,
):
    fake_sf_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_sf_connection)
    # The suspended warehouse's 60s resume counts toward the wall clock, so
    # the timeout must sit above it.
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
    )
    assert any('USE WAREHOUSE "DEX_WH"' in u for u in fake_sf_connection.used)
    assert fake_sf_connection.session_parameters["QUERY_TAG"] == "dex"


def test_no_pinned_warehouse_refuses_billed_work_with_the_fix(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection, target=SnowflakeTarget())
    with pytest.raises(SnowflakeConnectionError) as exc_info:
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert "snowflake.warehouse" in str(exc_info.value)
    assert data_statements(fake_sf_connection) == []


def test_missing_warehouse_refuses_with_the_fix(fake_sf_connection):
    adapter = make_adapter(
        fake_sf_connection, target=SnowflakeTarget(warehouse="NOPE_WH")
    )
    with pytest.raises(SnowflakeConnectionError) as exc_info:
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert "NOPE_WH" in str(exc_info.value)


def test_every_billed_statement_carries_the_remaining_budget_as_timeout(
    fake_sf_connection,
):
    fake_sf_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_sf_connection, ceiling=300.0)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
    )
    executed = data_statements(fake_sf_connection)
    assert executed
    for statement in executed:
        assert statement.session_timeout is not None
        assert statement.session_timeout <= 300


def test_server_side_timeout_translates_when_the_estimate_drifts(fake_sf_connection):
    from fakes.snowflake import FakeResult

    # The estimate says seconds; the statement 'runs' far longer than the
    # remaining budget, and the simulated server kills it at the timeout.
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=10_000.0
    )
    adapter = make_adapter(fake_sf_connection, ceiling=200.0)
    with pytest.raises(OverCeilingError) as exc_info:
        adapter.run_query(
            'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=100_000,
        )
    assert "budget" in str(exc_info.value)


def test_wall_timeout_translates_to_timeout_error(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=50.0
    )
    adapter = make_adapter(fake_sf_connection, ceiling=10_000.0)
    with pytest.raises(TimeoutError):
        adapter.run_query(
            'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=5,
        )


def test_actual_seconds_land_in_the_ledger(fake_sf_connection):
    entries: list[dict] = []
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=2.5
    )
    adapter = make_adapter(fake_sf_connection, record=entries.append)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=30,
    )
    assert len(entries) == 1
    assert entries[0]["billed_seconds"] == pytest.approx(2.5)
    assert entries[0]["connector"] == "snowflake"
    assert entries[0]["job_id"].startswith("fake-query")
    assert "SELECT" not in str(entries[0].values())


def test_resume_time_is_billed_to_the_first_statement(fake_sf_connection):
    entries: list[dict] = []
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection, record=entries.append)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
    )
    # The suspended warehouse resumed: 60s minimum + 1s of work on the clock.
    assert entries[0]["billed_seconds"] == pytest.approx(61.0)


def test_session_remainder_binds_across_commands(fake_sf_connection):
    adapter = make_adapter(
        fake_sf_connection,
        ceiling=600.0,
        session_ceiling=100.0,
        session_spent=99.5,
    )
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert data_statements(fake_sf_connection) == []


def test_run_query_truncates_and_shapes_columnar(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouse_resumes_pending = False
    fake_sf_connection.row_resolver = lambda sql: [{"id": i} for i in range(5)]
    adapter = make_adapter(fake_sf_connection)
    result = adapter.run_query(
        'SELECT "ID" AS id FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=3,
        timeout_seconds=30,
    )
    assert result.columns == ["id"]
    assert result.cells == [[0], [1], [2]]
    assert result.truncated is True


# --- profiling -----------------------------------------------------------------------


def _aggregate_resolver(sql: str):
    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
    return [values]


def test_column_aggregates_profile_scalar_columns(fake_sf_connection):
    fake_sf_connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_sf_connection, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "SHOP.PUBLIC.CUSTOMERS", columns, safe_min_max={"ID"}
        )
    }
    assert aggs["ID"].is_unique is True
    assert aggs["ID"].min_value == 1
    assert aggs["EMAIL"].min_value is None  # not in safe_min_max
    executed = data_statements(fake_sf_connection)
    assert len(executed) == 1
    assert "APPROX_COUNT_DISTINCT" in executed[0].sql


def test_shape_stats_ride_the_aggregate_batch(fake_sf_connection):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_sf_connection.row_resolver = lambda sql: [
        {
            "n_total": 100,
            "nn_0": 100,
            "nd_0": 100,
            "nn_1": 90,
            "nd_1": 40,
            "su_1": 0.8,
            "sp_1": 0.1,
            "st_1": 2.5,
        }
    ]
    adapter = make_adapter(fake_sf_connection, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "SHOP.PUBLIC.CUSTOMERS", columns, shape_stats={"EMAIL"}
        )
    }
    sql = data_statements(fake_sf_connection)[0].sql
    assert 'RLIKE("EMAIL", \'' in sql
    for alias in ("su_1", "sp_1", "st_1"):
        assert f" AS {alias}" in sql
    assert assert_select_only(sql, dialect="snowflake") == sql
    assert aggs["EMAIL"].upper_vocab_fraction == pytest.approx(0.8)
    assert aggs["EMAIL"].person_shape_fraction == pytest.approx(0.1)
    assert aggs["EMAIL"].avg_token_count == pytest.approx(2.5)
    # ID was not requested: its shape fields stay None.
    assert aggs["ID"].upper_vocab_fraction is None
    assert aggs["ID"].person_shape_fraction is None
    assert aggs["ID"].avg_token_count is None


def test_semi_structured_columns_degrade_to_non_null_counts(fake_sf_connection):
    fake_sf_connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_sf_connection, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.EVENTS")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "SHOP.PUBLIC.EVENTS", columns, safe_min_max=set()
        )
    }
    sql = data_statements(fake_sf_connection)[0].sql
    assert 'COUNT("PAYLOAD")' in sql
    assert 'APPROX_COUNT_DISTINCT("PAYLOAD")' not in sql
    assert 'APPROX_COUNT_DISTINCT("LABELS")' not in sql
    assert aggs["PAYLOAD"].distinct_count is None
    assert aggs["PAYLOAD"].null_fraction == 0.0


def test_sampling_kicks_in_above_the_threshold_and_voids_uniqueness(
    fake_sf_connection,
):
    fake_sf_connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(
        fake_sf_connection,
        ceiling=100_000.0,
        target=SnowflakeTarget(warehouse="DEX_WH", max_full_profile_bytes=1_000_000),
    )
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.EVENTS")
    aggs = adapter.column_aggregates("SHOP.PUBLIC.EVENTS", columns, safe_min_max=set())
    sql = data_statements(fake_sf_connection)[0].sql
    assert "SAMPLE SYSTEM" in sql
    assert all(a.is_unique is None for a in aggs)
    assert any(
        "block sample" in note for note in adapter.table_notes("SHOP.PUBLIC.EVENTS")
    )


def test_exact_distinct_counts_degrade_when_budget_cannot_cover(fake_sf_connection):
    fake_sf_connection.row_resolver = lambda sql: [{"d_0": 100}]
    adapter = make_adapter(fake_sf_connection, ceiling=100.0)
    adapter.cost_gate.charge(99.5)
    result = adapter.exact_distinct_counts("SHOP.PUBLIC.CUSTOMERS", ["ID"])
    assert result == {}
    assert any(
        "escalation skipped" in note
        for note in adapter.table_notes("SHOP.PUBLIC.CUSTOMERS")
    )
    assert data_statements(fake_sf_connection) == []


def test_distinct_combination_counts_batch_into_one_guarded_statement(
    fake_sf_connection,
):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_sf_connection.row_resolver = lambda sql: [{"d_0": 97, "d_1": 100}]
    adapter = make_adapter(fake_sf_connection, ceiling=100_000.0)
    counts = adapter.distinct_combination_counts(
        "SHOP.PUBLIC.CUSTOMERS", [["ID", "EMAIL"], ["EMAIL", "ID"]]
    )
    assert counts == {("ID", "EMAIL"): 97, ("EMAIL", "ID"): 100}
    stmts = data_statements(fake_sf_connection)
    assert len(stmts) == 1
    assert "SELECT DISTINCT" in stmts[0].sql
    assert assert_select_only(stmts[0].sql, dialect="snowflake") == stmts[0].sql
    assert adapter.distinct_combination_counts("SHOP.PUBLIC.CUSTOMERS", []) == {}


def test_distinct_combination_counts_degrade_when_budget_cannot_cover(
    fake_sf_connection,
):
    adapter = make_adapter(fake_sf_connection, ceiling=100.0)
    adapter.cost_gate.charge(99.5)
    result = adapter.distinct_combination_counts(
        "SHOP.PUBLIC.CUSTOMERS", [["ID", "EMAIL"]]
    )
    assert result == {}
    assert any(
        "composite-key probe skipped" in note
        for note in adapter.table_notes("SHOP.PUBLIC.CUSTOMERS")
    )
    assert data_statements(fake_sf_connection) == []


# --- factory and dialect --------------------------------------------------------


def test_get_adapter_wires_snowflake(fake_sf_connection):
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=600.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="snowflake",
    )
    adapter = get_adapter(
        "snowflake",
        connection=fake_sf_connection,
        cost_gate=gate,
        target=SnowflakeTarget(warehouse="DEX_WH"),
    )
    assert adapter.name == "snowflake"
    adapter.close()
    assert fake_sf_connection.closed is True


def test_get_dialect_resolves_snowflake():
    assert get_dialect("snowflake") == "snowflake"


# --- connection discovery (connect.py) ----------------------------------------------


def test_discovery_order_and_coarse_method(tmp_path: Path, monkeypatch):
    from exmergo_dex_core import connect as connect_mod
    from exmergo_dex_core.connect import resolve_snowflake_connection

    connections = {
        "dex-ci": {
            "account": "TESTORG-TESTACCT",
            "user": "DEX_DEV",
            "authenticator": "SNOWFLAKE_JWT",
            "private_key_file": "/keys/k.p8",
        },
        "other": {"account": "A", "user": "U", "password": "hunter2"},
    }
    monkeypatch.setattr(
        connect_mod, "_snowflake_connections", lambda env: dict(connections)
    )

    params, method = resolve_snowflake_connection(
        SnowflakeTarget(connection_name="dex-ci"), {}, tmp_path
    )
    assert params["account"] == "TESTORG-TESTACCT"
    assert method == "named_connection:key_pair"

    # The default connection is used when config pins nothing.
    connections["__default__"] = "other"
    _params, method = resolve_snowflake_connection(SnowflakeTarget(), {}, tmp_path)
    assert method == "default_connection:password"

    # Environment (the CI workload-identity path) when no toml store matches.
    monkeypatch.setattr(connect_mod, "_snowflake_connections", lambda env: {})
    env = {
        "SNOWFLAKE_ACCOUNT": "TESTORG-TESTACCT",
        "SNOWFLAKE_USER": "DEX_CI",
        "SNOWFLAKE_AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER": "OIDC",
        "SNOWFLAKE_TOKEN": "not-a-real-token",
    }
    params, method = resolve_snowflake_connection(SnowflakeTarget(), env, tmp_path)
    assert method == "environment:workload_identity"
    assert params["authenticator"] == "WORKLOAD_IDENTITY"
    assert params["workload_identity_provider"] == "OIDC"


def test_discovery_failure_names_the_fixes(tmp_path: Path, monkeypatch):
    from exmergo_dex_core import connect as connect_mod
    from exmergo_dex_core.connect import (
        CredentialDiscoveryError,
        resolve_snowflake_connection,
    )

    monkeypatch.setattr(connect_mod, "_snowflake_connections", lambda env: {})
    with pytest.raises(CredentialDiscoveryError) as exc_info:
        resolve_snowflake_connection(SnowflakeTarget(), {}, tmp_path)
    message = str(exc_info.value)
    assert "snow connection add" in message
    assert "SNOWFLAKE_ACCOUNT" in message

    with pytest.raises(CredentialDiscoveryError) as exc_info:
        resolve_snowflake_connection(
            SnowflakeTarget(connection_name="missing"), {}, tmp_path
        )
    assert "missing" in str(exc_info.value)


def test_discovery_falls_back_to_dbt_profiles(dbt_project_dir: Path, monkeypatch):
    from exmergo_dex_core import connect as connect_mod
    from exmergo_dex_core.connect import resolve_snowflake_connection

    monkeypatch.setattr(connect_mod, "_snowflake_connections", lambda env: {})
    profiles = dbt_project_dir / "profiles.yml"
    profiles.write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: snowflake\n"
        "      account: FROM-DBT\n"
        "      user: DBT_USER\n"
        "      private_key_path: /keys/dbt.p8\n"
        "      warehouse: WH\n"
        "      database: DB\n"
        "      schema: DEV\n",
        encoding="utf-8",
    )
    params, method = resolve_snowflake_connection(
        SnowflakeTarget(), {}, dbt_project_dir.parent
    )
    assert params["account"] == "FROM-DBT"
    assert params["private_key_file"] == "/keys/dbt.p8"
    assert method == "dbt_profile:key_pair"


def test_connections_toml_parsing(tmp_path: Path):
    from exmergo_dex_core.connect import _snowflake_connections

    home = tmp_path / "sfhome"
    home.mkdir()
    (home / "connections.toml").write_text(
        '[dex-ci]\naccount = "ORG-ACCT"\nuser = "DEX_DEV"\n', encoding="utf-8"
    )
    (home / "config.toml").write_text(
        'default_connection_name = "dex-ci"\n'
        '[connections.admin]\naccount = "ORG-ACCT"\nuser = "ADMIN"\n',
        encoding="utf-8",
    )
    connections = _snowflake_connections({"SNOWFLAKE_HOME": str(home)})
    assert connections["dex-ci"]["user"] == "DEX_DEV"
    assert connections["admin"]["user"] == "ADMIN"
    assert connections["__default__"] == "dex-ci"


# --- scope resolution: honored or named in an error, never dropped -----------------


@pytest.fixture
def multi_db_connection():
    """Two source databases that share a PUBLIC schema, plus an empty scratch
    database. Enough to exercise qualification, ambiguity, and the dev-target
    preflight, and modeled on the shape that made `--dataset` dangerous: one
    database holding schemas of wildly different size."""

    from fakes.snowflake import (
        FakeSnowflakeConnection,
        FakeSnowflakeTable,
        FakeWarehouse,
    )

    def table(database, schema, name, size):
        return FakeSnowflakeTable(
            database=database,
            schema=schema,
            name=name,
            columns=[("ID", "FIXED", False)],
            rows=10,
            bytes=size,
        )

    return FakeSnowflakeConnection(
        tables=[
            table("SAMPLE", "TPCH_SF1", "ORDERS", 1_000_000),
            table("SAMPLE", "TPCH_SF1", "CUSTOMER", 1_000_000),
            table("SAMPLE", "TPCDS_SF100TCL", "STORE_SALES", 900_000_000_000),
            table("SAMPLE", "PUBLIC", "SHARED", 1_000),
            table("RAW", "PUBLIC", "EVENTS", 1_000),
            table("RAW", "STAGING", "SEEDS", 1_000),
        ],
        warehouses=[FakeWarehouse(name="DEX_WH", size="X-Small", state="SUSPENDED")],
        empty_databases=["DBT_DEV"],
    )


def _scoped_identifiers(adapter) -> list[str]:
    return [o.identifier for o in adapter.list_objects()]


def test_qualified_scope_bounds_the_inventory(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["SAMPLE.TPCH_SF1"],
    )
    assert _scoped_identifiers(adapter) == [
        "SAMPLE.TPCH_SF1.CUSTOMER",
        "SAMPLE.TPCH_SF1.ORDERS",
    ]


def test_bare_schema_scope_qualifies_against_the_allowlist(multi_db_connection):
    """The exact spelling the field report used: `--scope TPCH_SF1`, no database.

    It must resolve to SAMPLE.TPCH_SF1 and bound the estimate to those two
    tables, never silently span the 900 GB TPCDS schema next door.
    """

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["TPCH_SF1"],
    )
    assert _scoped_identifiers(adapter) == [
        "SAMPLE.TPCH_SF1.CUSTOMER",
        "SAMPLE.TPCH_SF1.ORDERS",
    ]


def test_bare_database_scope_is_a_database_scope(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE", "RAW"]),
        scope_override=["RAW"],
    )
    assert _scoped_identifiers(adapter) == [
        "RAW.PUBLIC.EVENTS",
        "RAW.STAGING.SEEDS",
    ]


def test_nonexistent_schema_scope_is_refused_and_names_what_exists(multi_db_connection):
    """The bug from the field report: this used to be accepted and dropped, and
    the estimate silently covered the whole allowlist."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["__NONEXISTENT_SCHEMA__"],
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "__NONEXISTENT_SCHEMA__" in message
    assert "TPCH_SF1" in message and "TPCDS_SF100TCL" in message
    assert "[from --scope]" in message
    # Free: refusal never reaches a warehouse.
    assert data_statements(multi_db_connection) == []


def test_nonexistent_qualified_schema_names_the_database_and_its_schemas(
    multi_db_connection,
):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["SAMPLE.__NOPE__"],
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    assert "database SAMPLE has no schema __NOPE__" in str(exc.value)


def test_nonexistent_database_scope_is_refused(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH"),
        scope_override=["__NO_DB__.PUBLIC"],
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    assert "names no database this role can see" in str(exc.value)
    assert "SAMPLE" in str(exc.value)


def test_ambiguous_bare_schema_demands_qualification(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE", "RAW"]),
        scope_override=["PUBLIC"],
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "ambiguous" in message
    assert "qualify it as <database>.PUBLIC" in message


def test_scope_cannot_widen_the_committed_allowlist(multi_db_connection):
    """The cost boundary: `snowflake.databases` is committed to the repo, so a
    per-command flag must not reach the 900 GB schema it deliberately excludes."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE.TPCH_SF1"]),
        scope_override=["SAMPLE.TPCDS_SF100TCL"],
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "outside the committed allowlist" in message
    assert "never widens" in message
    assert data_statements(multi_db_connection) == []


def test_committed_allowlist_entry_that_does_not_exist_is_blamed_on_config(
    multi_db_connection,
):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE.__GONE__"]),
    )
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    assert "[from snowflake.databases in .dex/config.yml]" in str(exc.value)


def test_connect_test_validates_the_scope_for_free(multi_db_connection):
    """capabilities() resolves scopes, so `connect test --scope <bad>` fails
    before any command that would spend."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["__NOPE__"],
    )
    with pytest.raises(SnowflakeConnectionError):
        adapter.capabilities()
    assert data_statements(multi_db_connection) == []


def test_information_schema_is_never_a_scope(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["INFORMATION_SCHEMA"],
    )
    with pytest.raises(SnowflakeConnectionError):
        adapter.list_objects()


# --- the dev-target preflight (free) ----------------------------------------------


def test_missing_dev_database_is_reported(multi_db_connection):
    adapter = make_adapter(multi_db_connection)
    assert adapter.missing_dev_namespaces("NOT_THERE") == ['dev_database "NOT_THERE"']
    assert data_statements(multi_db_connection) == []


def test_existing_but_empty_dev_database_is_fine(multi_db_connection):
    """dbt creates the schema; only the database has to pre-exist. An empty
    scratch database is exactly the state before a first build."""

    adapter = make_adapter(multi_db_connection)
    assert adapter.missing_dev_namespaces("DBT_DEV") == []
    assert data_statements(multi_db_connection) == []


def test_bare_schema_on_a_wide_open_account_asks_for_qualification(monkeypatch):
    """Qualifying a bare schema costs one free SHOW SCHEMAS per candidate
    database. On an account with hundreds, dex asks rather than round-trips."""

    from fakes.snowflake import (
        FakeSnowflakeConnection,
        FakeSnowflakeTable,
        FakeWarehouse,
    )

    connection = FakeSnowflakeConnection(
        tables=[
            FakeSnowflakeTable(
                database=f"DB{i:03d}",
                schema="RAW",
                name="T",
                columns=[("ID", "FIXED", True)],
            )
            for i in range(30)
        ],
        warehouses=[FakeWarehouse(name="DEX_WH")],
    )
    adapter = make_adapter(connection, scope_override=["RAW"])
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "30 databases" in message
    assert "qualify it as <database>.RAW" in message


def test_dev_namespace_objects_lists_tables_and_views(multi_db_connection):
    from fakes.snowflake import FakeSnowflakeTable

    multi_db_connection.tables.append(
        FakeSnowflakeTable(
            database="RAW",
            schema="STAGING_DEV",
            name="V_LEFTOVER",
            columns=[("ID", "FIXED", False)],
            kind="view",
        )
    )
    adapter = make_adapter(multi_db_connection)
    # Case-folded like every Snowflake identifier, and views count as content.
    listed = adapter.dev_namespace_objects("raw", "staging_dev")
    assert listed == ["V_LEFTOVER"]
    assert data_statements(multi_db_connection) == []


def test_dev_namespace_objects_reads_an_absent_schema_as_empty(multi_db_connection):
    adapter = make_adapter(multi_db_connection)
    assert adapter.dev_namespace_objects("RAW", "NOT_THERE") == []
    assert adapter.dev_namespace_objects("NO_SUCH_DB", "STAGING_DEV") == []
    assert data_statements(multi_db_connection) == []
