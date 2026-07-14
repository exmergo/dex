"""The Databricks adapter against the stateful fake pair: metadata is free
(Unity Catalog REST, no SQL session and no warehouse), every billed statement
is estimated and gated in warehouse-seconds, sizes are learned in-budget via
DESCRIBE DETAIL, and the budget binds at both the client (charge) and the
simulated server (STATEMENT_TIMEOUT)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("databricks.sql")

from fakes.databricks import FakeResult

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.databricks import (
    _DETAIL_SECONDS,
    _MIN_STATEMENT_SECONDS,
    _STARTUP_SERVERLESS_SECONDS,
    DatabricksAdapter,
    DatabricksConnectionError,
    warehouse_http_path,
    warehouse_id_of,
)
from exmergo_dex_core.config import DatabricksTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)


def make_adapter(
    fake,
    *,
    ceiling: float | None = 600.0,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: DatabricksTarget | None = None,
    scope_origin: str | None = None,
) -> DatabricksAdapter:
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        confirmed=confirmed,
        connector="databricks",
        command="explore profile",
        record=record,
    )
    return DatabricksAdapter(
        workspace=fake.workspace,
        sql_connect=fake.sql_connect,
        cost_gate=gate,
        target=target or DatabricksTarget(warehouse="fake-wh"),
        host="test.cloud.databricks.com",
        auth_method="default_profile:oauth_user",
        scope_origin=scope_origin,
        clock=fake.clock,
    )


def warm(fake) -> None:
    """Flip the fixture's warehouse to running so the startup floor is off."""

    fake.workspace.warehouse.state = "RUNNING"
    fake.connection.startup_pending = False


def data_statements(fake) -> list:
    return fake.connection.data_statements


# --- metadata (free, and never a SQL session) -----------------------------------------


def test_capabilities_shape_and_free(fake_databricks):
    adapter = make_adapter(fake_databricks)
    caps = adapter.capabilities()
    assert caps["connector"] == "databricks"
    assert caps["dialect"] == "databricks"
    assert caps["read_only"] is True
    assert caps["paradigm"] == "compute_time"
    assert caps["host"] == "test.cloud.databricks.com"
    assert caps["auth_method"] == "default_profile:oauth_user"
    assert caps["catalog_count"] == 1  # shop
    budget = caps["budget"]
    assert budget["ceiling_seconds"] == 600.0
    assert budget["warehouse"]["size"] == "2X-Small"
    assert budget["warehouse"]["serverless"] is True
    assert budget["warehouse"]["dbu_per_hour"] == 4.0
    # 600 warehouse-seconds on a 2X-Small at 4 DBU/h.
    assert budget["ceiling_dbus"] == pytest.approx(600 * 4 / 3600, abs=1e-6)
    # Capabilities is a free probe: REST metadata only, no SQL session opened.
    assert fake_databricks.connect_count == 0


def test_list_objects_uses_free_rest_metadata_only(fake_databricks):
    adapter = make_adapter(fake_databricks)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "shop.core.customers",
        "shop.core.events",
    ]
    customers = next(o for o in objects if o.name == "customers")
    # Unity Catalog has no free sizes: both stay None until an in-budget
    # DESCRIBE DETAIL learns them.
    assert customers.row_count is None
    assert customers.byte_size is None
    assert customers.column_count == 2
    assert fake_databricks.connect_count == 0


def test_catalog_allowlist_scopes_inventory(fake_databricks):
    from fakes.databricks import FakeDatabricksTable

    noise = FakeDatabricksTable(
        catalog="other", schema="stuff", name="noise", columns=[("id", "bigint", True)]
    )
    fake_databricks.workspace._tables.append(noise)
    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["shop.core"]),
    )
    assert all(o.identifier.startswith("shop.") for o in adapter.list_objects())


def test_columns_backfill_via_get_when_the_listing_omits_them(fake_databricks):
    # Shared/browse-only catalogs (samples) omit columns from tables.list;
    # the free per-table GET backfills them.
    fake_databricks.workspace.omit_list_columns = True
    adapter = make_adapter(fake_databricks)
    objects = adapter.list_objects()
    assert {o.name: o.column_count for o in objects} == {"customers": 2, "events": 3}
    calls = fake_databricks.workspace.metadata_calls
    assert any(call.startswith("tables.get:") for call in calls)
    assert fake_databricks.connect_count == 0


def test_table_metadata_carries_unity_catalog_types(fake_databricks):
    adapter = make_adapter(fake_databricks)
    meta, columns = adapter.table_metadata("shop.core.events")
    assert meta.identifier == "shop.core.events"
    by_name = {c.name: c for c in columns}
    assert by_name["payload"].data_type == "struct<a:int,b:string>"
    assert by_name["labels"].data_type == "array<string>"
    id_col = next(
        c for c in adapter.table_metadata("shop.core.customers")[1] if c.name == "id"
    )
    assert id_col.nullable is False


# --- estimation (free) and the startup floor -----------------------------------------


def test_profile_estimate_is_a_floor_and_carries_the_startup_floor(fake_databricks):
    adapter = make_adapter(fake_databricks)
    total, per_table = adapter.profile_estimate(["shop.core.customers"])
    # No free size: one batch at the statement floor plus the DESCRIBE DETAIL
    # probe, plus the serverless wake (the fixture warehouse starts STOPPED).
    assert per_table["shop.core.customers"] == pytest.approx(
        _MIN_STATEMENT_SECONDS + _DETAIL_SECONDS
    )
    assert total == pytest.approx(
        _MIN_STATEMENT_SECONDS + _DETAIL_SECONDS + _STARTUP_SERVERLESS_SECONDS
    )
    assert fake_databricks.connect_count == 0


def test_no_startup_floor_on_a_running_warehouse(fake_databricks):
    warm(fake_databricks)
    adapter = make_adapter(fake_databricks)
    total, per_table = adapter.profile_estimate(["shop.core.customers"])
    assert total == pytest.approx(per_table["shop.core.customers"])


def test_query_estimate_is_the_floor_until_a_size_is_known(fake_databricks):
    warm(fake_databricks)
    adapter = make_adapter(fake_databricks)
    estimate = adapter.query_estimate("SELECT COUNT(*) FROM `shop`.`core`.`customers`")
    assert estimate == pytest.approx(_MIN_STATEMENT_SECONDS)


def test_describe_estimate_translates_to_dbus(fake_databricks):
    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", dbu_price_usd=0.7),
    )
    described = adapter.describe_estimate(360.0, {"shop.core.customers": 360.0})
    assert described["estimated_seconds"] == 360.0
    assert described["estimate_quality"] == "low"
    assert described["estimated_dbus"] == pytest.approx(0.4)
    assert described["estimated_usd"] == pytest.approx(0.28)
    assert described["per_table_seconds"] == {"shop.core.customers": 360.0}
    assert described["dbu_rate"]["approximate"] is True
    assert "--budget" in described["hint"] and "seconds" in described["hint"]
    assert any("no dry-run" in note for note in described["notes"])


# --- the billed door -----------------------------------------------------------------


def test_unconfirmed_execution_is_refused_before_any_run(fake_databricks):
    adapter = make_adapter(fake_databricks, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0


def test_over_ceiling_refused_client_side_without_execution(fake_databricks):
    # The floor plus the wake exceeds a 5-second ceiling.
    adapter = make_adapter(fake_databricks, ceiling=5.0)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0


def test_the_sql_session_opens_lazily_on_the_first_billed_statement(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_databricks)
    adapter.capabilities()
    adapter.list_objects()
    assert fake_databricks.connect_count == 0
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    assert fake_databricks.connect_count == 1
    # A second billed statement reuses the session.
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    assert fake_databricks.connect_count == 1


def test_no_pinned_warehouse_refuses_billed_work_with_the_fix(fake_databricks):
    adapter = make_adapter(fake_databricks, target=DatabricksTarget())
    with pytest.raises(DatabricksConnectionError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert "databricks.warehouse" in str(exc_info.value)
    assert fake_databricks.connect_count == 0


def test_missing_warehouse_refuses_with_the_fix(fake_databricks):
    adapter = make_adapter(
        fake_databricks, target=DatabricksTarget(warehouse="nope-wh")
    )
    with pytest.raises(DatabricksConnectionError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert "nope-wh" in str(exc_info.value)


def test_every_billed_statement_carries_the_remaining_budget_as_timeout(
    fake_databricks,
):
    fake_databricks.connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_databricks, ceiling=300.0)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=400,
    )
    executed = data_statements(fake_databricks)
    assert executed
    for statement in executed:
        assert statement.session_timeout is not None
        assert statement.session_timeout <= 300


def test_server_side_timeout_translates_when_the_estimate_drifts(fake_databricks):
    # The floor says seconds; the statement 'runs' far longer than the
    # remaining budget, and the simulated server kills it at the timeout.
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=10_000.0
    )
    adapter = make_adapter(fake_databricks, ceiling=200.0)
    with pytest.raises(OverCeilingError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=100_000,
        )
    assert "budget" in str(exc_info.value)


def test_wall_timeout_translates_to_timeout_error(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=50.0
    )
    adapter = make_adapter(fake_databricks, ceiling=10_000.0)
    with pytest.raises(TimeoutError):
        adapter.run_query(
            "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=5,
        )


def test_actual_seconds_land_in_the_ledger(fake_databricks):
    entries: list[dict] = []
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=2.5
    )
    adapter = make_adapter(fake_databricks, record=entries.append)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    assert len(entries) == 1
    assert entries[0]["billed_seconds"] == pytest.approx(2.5)
    assert entries[0]["connector"] == "databricks"
    assert entries[0]["job_id"].startswith("fake-query")
    assert "SELECT" not in str(entries[0].values())


def test_wake_time_is_billed_to_the_first_statement(fake_databricks):
    entries: list[dict] = []
    fake_databricks.connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_databricks, record=entries.append)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=200,
    )
    # The stopped warehouse woke: 10s of simulated wake + 1s of work.
    assert entries[0]["billed_seconds"] == pytest.approx(11.0)


def test_session_remainder_binds_across_commands(fake_databricks):
    adapter = make_adapter(
        fake_databricks,
        ceiling=600.0,
        session_ceiling=100.0,
        session_spent=99.5,
    )
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0


def test_run_query_truncates_and_shapes_columnar(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: [{"id": i} for i in range(5)]
    adapter = make_adapter(fake_databricks)
    result = adapter.run_query(
        "SELECT `id` AS id FROM `shop`.`core`.`customers`",
        max_rows=3,
        timeout_seconds=30,
    )
    assert result.columns == ["id"]
    assert result.cells == [[0], [1], [2]]
    assert result.truncated is True


# --- in-budget size refinement --------------------------------------------------------


def test_describe_detail_refines_sizes_inside_the_confirmed_budget(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("shop.core.customers")
    adapter.column_aggregates("shop.core.customers", columns, safe_min_max=set())
    # The probe ran, and the learned size now sharpens later estimates: 5 GB
    # over the 2X-Small scan rate is ~95 s, far above the 5 s floor.
    assert any(
        s.sql.upper().startswith("DESCRIBE DETAIL")
        for s in fake_databricks.connection.statements
    )
    meta, _ = adapter.table_metadata("shop.core.customers")
    assert meta.byte_size == 5_000_000_000
    assert adapter.query_estimate(
        "SELECT COUNT(*) FROM `shop`.`core`.`customers`"
    ) == pytest.approx(5_000_000_000 / (50 * 1024 * 1024))


def test_aggregates_capture_the_exact_row_count(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("shop.core.customers")
    adapter.column_aggregates("shop.core.customers", columns, safe_min_max=set())
    meta, _ = adapter.table_metadata("shop.core.customers")
    # n_total from the aggregate batch, not DESCRIBE DETAIL's numRows: the
    # engine's uniqueness escalation needs the exact count.
    assert meta.row_count == 100


def test_refused_describe_detail_degrades_to_the_floor_with_a_note(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.detail_error = True
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("shop.core.customers")
    adapter.column_aggregates("shop.core.customers", columns, safe_min_max=set())
    assert any(
        "size probe unavailable" in note
        for note in adapter.table_notes("shop.core.customers")
    )
    meta, _ = adapter.table_metadata("shop.core.customers")
    assert meta.byte_size is None


# --- profiling -----------------------------------------------------------------------


def _aggregate_resolver(sql: str):
    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
    return [values]


def test_column_aggregates_profile_scalar_columns(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("shop.core.customers")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "shop.core.customers", columns, safe_min_max={"id"}
        )
    }
    assert aggs["id"].is_unique is True
    assert aggs["id"].min_value == 1
    assert aggs["email"].min_value is None  # not in safe_min_max
    executed = data_statements(fake_databricks)
    assert len(executed) == 1
    assert "APPROX_COUNT_DISTINCT" in executed[0].sql


def test_nested_columns_degrade_to_non_null_counts(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    _meta, columns = adapter.table_metadata("shop.core.events")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "shop.core.events", columns, safe_min_max=set()
        )
    }
    sql = data_statements(fake_databricks)[0].sql
    assert "COUNT(`payload`)" in sql
    assert "APPROX_COUNT_DISTINCT(`payload`)" not in sql
    assert "APPROX_COUNT_DISTINCT(`labels`)" not in sql
    assert aggs["payload"].distinct_count is None
    assert aggs["payload"].null_fraction == 0.0


def test_sampling_kicks_in_above_the_threshold_and_voids_uniqueness(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = _aggregate_resolver
    adapter = make_adapter(
        fake_databricks,
        ceiling=100_000.0,
        target=DatabricksTarget(warehouse="fake-wh", max_full_profile_bytes=1_000_000),
    )
    _meta, columns = adapter.table_metadata("shop.core.events")
    aggs = adapter.column_aggregates("shop.core.events", columns, safe_min_max=set())
    sql = data_statements(fake_databricks)[0].sql
    assert "TABLESAMPLE" in sql
    assert all(a.is_unique is None for a in aggs)
    assert any("sample" in note for note in adapter.table_notes("shop.core.events"))


def test_exact_distinct_counts_degrade_when_budget_cannot_cover(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: [{"d_0": 100}]
    adapter = make_adapter(fake_databricks, ceiling=100.0)
    adapter.cost_gate.charge(99.5)
    result = adapter.exact_distinct_counts("shop.core.customers", ["id"])
    assert result == {}
    assert any(
        "escalation skipped" in note
        for note in adapter.table_notes("shop.core.customers")
    )
    assert data_statements(fake_databricks) == []


def test_distinct_combination_counts_batch_into_one_guarded_statement(
    fake_databricks,
):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: [{"d_0": 97, "d_1": 100}]
    adapter = make_adapter(fake_databricks, ceiling=100_000.0)
    counts = adapter.distinct_combination_counts(
        "shop.core.customers", [["id", "email"], ["email", "id"]]
    )
    assert counts == {("id", "email"): 97, ("email", "id"): 100}
    stmts = data_statements(fake_databricks)
    assert len(stmts) == 1
    assert "SELECT DISTINCT" in stmts[0].sql
    assert assert_select_only(stmts[0].sql, dialect="databricks") == stmts[0].sql
    assert adapter.distinct_combination_counts("shop.core.customers", []) == {}


def test_distinct_combination_counts_degrade_when_budget_cannot_cover(
    fake_databricks,
):
    warm(fake_databricks)
    adapter = make_adapter(fake_databricks, ceiling=100.0)
    adapter.cost_gate.charge(99.5)
    result = adapter.distinct_combination_counts(
        "shop.core.customers", [["id", "email"]]
    )
    assert result == {}
    assert any(
        "composite-key probe skipped" in note
        for note in adapter.table_notes("shop.core.customers")
    )
    assert data_statements(fake_databricks) == []


# --- factory, dialect, and warehouse pin forms ----------------------------------------


def test_get_adapter_wires_databricks(fake_databricks):
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=600.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="databricks",
    )
    adapter = get_adapter(
        "databricks",
        workspace=fake_databricks.workspace,
        sql_connect=fake_databricks.sql_connect,
        cost_gate=gate,
        target=DatabricksTarget(warehouse="fake-wh"),
    )
    assert adapter.name == "databricks"
    adapter.close()  # no session was ever opened, so nothing to close
    assert fake_databricks.connection.closed is False


def test_close_closes_an_opened_session(fake_databricks):
    warm(fake_databricks)
    fake_databricks.connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_databricks)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    adapter.close()
    assert fake_databricks.connection.closed is True


def test_get_dialect_resolves_databricks():
    assert get_dialect("databricks") == "databricks"


def test_warehouse_pin_accepts_id_or_http_path():
    assert warehouse_http_path("abc123") == "/sql/1.0/warehouses/abc123"
    assert (
        warehouse_http_path("/sql/1.0/warehouses/abc123")
        == "/sql/1.0/warehouses/abc123"
    )
    assert warehouse_id_of("abc123") == "abc123"
    assert warehouse_id_of("/sql/1.0/warehouses/abc123") == "abc123"


# --- connection discovery (connect.py) ----------------------------------------------


def _isolate_databricks_env(monkeypatch, tmp_path: Path) -> None:
    """Point every Databricks discovery source at empty test-local state so a
    developer's real ~/.databrickscfg or env can never leak into a test, and
    keep the SDK offline: ``Config.__init__`` unconditionally fetches
    ``/.well-known/databricks-config`` from the configured host, and the fake
    test host resolves (wildcard DNS), so without this no-op each discovery
    test burns the SDK's multi-minute retry budget against a live endpoint."""

    from databricks.sdk.core import Config

    monkeypatch.setattr(Config, "_resolve_host_metadata", lambda self: None)
    for var in (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "databrickscfg"))


def test_named_profile_discovery_and_coarse_method(tmp_path: Path, monkeypatch):
    from exmergo_dex_core.connect import resolve_databricks_connection

    _isolate_databricks_env(monkeypatch, tmp_path)
    (tmp_path / "databrickscfg").write_text(
        "[dex-ci]\nhost = https://test.cloud.databricks.com\n"
        "token = not-a-real-token\n",
        encoding="utf-8",
    )
    cfg, method = resolve_databricks_connection(
        DatabricksTarget(profile="dex-ci"), {}, tmp_path
    )
    assert "test.cloud.databricks.com" in cfg.host
    assert method == "named_profile:token"


def test_environment_discovery_is_the_ci_path(tmp_path: Path, monkeypatch):
    from exmergo_dex_core.connect import resolve_databricks_connection

    _isolate_databricks_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://test.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "not-a-real-token")
    env = {"DATABRICKS_HOST": "https://test.cloud.databricks.com"}
    cfg, method = resolve_databricks_connection(DatabricksTarget(), env, tmp_path)
    assert method == "environment:token"
    assert "test.cloud.databricks.com" in cfg.host


def test_default_profile_discovery(tmp_path: Path, monkeypatch):
    from exmergo_dex_core.connect import resolve_databricks_connection

    _isolate_databricks_env(monkeypatch, tmp_path)
    (tmp_path / "databrickscfg").write_text(
        "[DEFAULT]\nhost = https://test.cloud.databricks.com\n"
        "token = not-a-real-token\n",
        encoding="utf-8",
    )
    env = {"DATABRICKS_CONFIG_FILE": str(tmp_path / "databrickscfg")}
    _cfg, method = resolve_databricks_connection(DatabricksTarget(), env, tmp_path)
    assert method == "default_profile:token"


def test_cli_settings_pointer_counts_as_a_default_profile(tmp_path: Path, monkeypatch):
    # The newer databricks CLI writes no [DEFAULT] section; it records the
    # default profile under [__settings__], which discovery must honor.
    from exmergo_dex_core.connect import _databrickscfg_default_exists

    config_file = tmp_path / "databrickscfg"
    config_file.write_text(
        "[__settings__]\ndefault_profile = me\n"
        "[me]\nhost = https://test.cloud.databricks.com\n",
        encoding="utf-8",
    )
    assert _databrickscfg_default_exists({"DATABRICKS_CONFIG_FILE": str(config_file)})
    config_file.write_text("[me]\nhost = https://x\n", encoding="utf-8")
    assert not _databrickscfg_default_exists(
        {"DATABRICKS_CONFIG_FILE": str(config_file)}
    )


def test_discovery_failure_names_the_fixes(tmp_path: Path, monkeypatch):
    from exmergo_dex_core.connect import (
        CredentialDiscoveryError,
        resolve_databricks_connection,
    )

    _isolate_databricks_env(monkeypatch, tmp_path)
    env = {"DATABRICKS_CONFIG_FILE": str(tmp_path / "databrickscfg")}
    with pytest.raises(CredentialDiscoveryError) as exc_info:
        resolve_databricks_connection(DatabricksTarget(), env, tmp_path)
    message = str(exc_info.value)
    assert "databricks auth login" in message
    assert "DATABRICKS_HOST" in message

    with pytest.raises(CredentialDiscoveryError) as exc_info:
        resolve_databricks_connection(
            DatabricksTarget(profile="missing"), env, tmp_path
        )
    assert "missing" in str(exc_info.value)


def test_discovery_falls_back_to_dbt_profiles(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    from exmergo_dex_core.connect import resolve_databricks_connection

    _isolate_databricks_env(monkeypatch, tmp_path)
    profiles = dbt_project_dir / "profiles.yml"
    profiles.write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: databricks\n"
        "      host: from-dbt.cloud.databricks.com\n"
        "      token: not-a-real-token\n"
        "      http_path: /sql/1.0/warehouses/abc\n"
        "      schema: dev\n",
        encoding="utf-8",
    )
    env = {"DATABRICKS_CONFIG_FILE": str(tmp_path / "databrickscfg")}
    cfg, method = resolve_databricks_connection(
        DatabricksTarget(), env, dbt_project_dir.parent
    )
    assert "from-dbt.cloud.databricks.com" in cfg.host
    assert method == "dbt_profile:token"


# --- scope resolution: an entry that names nothing is refused, not dropped ------------


def test_nonexistent_catalog_scope_is_refused_and_names_what_exists(fake_databricks):
    """The cost-safety bug: a scope that resolves to nothing used to yield an
    empty inventory, so the user scoped to nothing and was never told."""

    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["no_such_catalog"]),
    )
    with pytest.raises(DatabricksConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "no_such_catalog" in message
    assert "shop" in message  # the catalogs that do exist
    assert "[from databricks.catalogs in .dex/config.yml]" in message
    assert data_statements(fake_databricks) == []
    assert fake_databricks.connect_count == 0  # never woke the warehouse


def test_nonexistent_schema_scope_is_refused_and_names_what_exists(fake_databricks):
    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["shop.nope"]),
        scope_origin="--scope",
    )
    with pytest.raises(DatabricksConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "shop.nope" in message
    assert "catalog shop has no schema nope" in message
    assert "core" in message  # the schemas that do exist there
    # The blame names the flag the entry actually came from, not the config file
    # it was copied over: the two have entirely different fixes.
    assert "[from --scope]" in message
    assert data_statements(fake_databricks) == []


def test_a_table_shaped_scope_is_refused(fake_databricks):
    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["shop.core.customers"]),
    )
    with pytest.raises(DatabricksConnectionError, match="never a table"):
        adapter.list_objects()


def test_scope_resolution_is_free_and_cached(fake_databricks):
    """Free (REST only) and one round-trip per command: the estimate pass and the
    confirmed run share the resolution."""

    adapter = make_adapter(
        fake_databricks,
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["shop.core"]),
    )
    adapter.list_objects()
    adapter.list_objects()
    calls = fake_databricks.workspace.metadata_calls
    assert calls.count("catalogs.list") == 1
    assert data_statements(fake_databricks) == []


# --- the dev-target preflight (free) --------------------------------------------------


def test_missing_dev_catalog_is_reported(fake_databricks):
    adapter = make_adapter(fake_databricks)
    assert adapter.missing_dev_namespaces("not_there") == ['dev_catalog "not_there"']
    assert data_statements(fake_databricks) == []
    assert fake_databricks.connect_count == 0


def test_existing_but_empty_dev_catalog_is_fine(fake_databricks):
    """dbt creates the schema; only the catalog has to pre-exist. An empty scratch
    catalog is exactly the state before a first build."""

    fake_databricks.workspace._empty_catalogs.append("dex_dev")
    adapter = make_adapter(fake_databricks)
    assert adapter.missing_dev_namespaces("dex_dev") == []
    assert data_statements(fake_databricks) == []
