"""The Redshift adapter against the stateful fake connection: metadata is
cheap (pg_catalog and SVV lookups, no scans), every billed statement is
estimated and gated in compute-seconds, the Serverless wake minimum is floored
into estimates exactly once per command, and the budget binds at both the
client (charge) and the simulated server (statement_timeout)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("redshift_connector")

from fakes.redshift import (
    FakeRedshiftConnection,
    FakeRedshiftTable,
    FakeResult,
    FakeUser,
)

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.redshift import (
    _MIN_STATEMENT_SECONDS,
    _WAKE_MINIMUM_SECONDS,
    RedshiftAdapter,
    RedshiftConnectionError,
)
from exmergo_dex_core.config import RedshiftTarget
from exmergo_dex_core.connect import (
    CredentialDiscoveryError,
    resolve_redshift_connection,
)
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)

SERVERLESS_8_RPU = {
    "kind": "serverless",
    "workgroup": "dex-wg",
    "base_capacity_rpus": 8.0,
}
PROVISIONED = {"kind": "provisioned", "workgroup": None, "base_capacity_rpus": None}

# The fixture's shop.customers is 5000 MB; at the 8-RPU reference rate
# (50 MB/s) one aggregate batch over it estimates exactly 100 seconds.
CUSTOMERS_SCAN_SECONDS = 100.0


def make_adapter(
    connection,
    *,
    ceiling: float | None = 600.0,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: RedshiftTarget | None = None,
    compute: dict | None = None,
    scope_origin: str | None = None,
) -> RedshiftAdapter:
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        confirmed=confirmed,
        connector="redshift",
        command="explore profile",
        record=record,
    )
    return RedshiftAdapter(
        connection=connection,
        cost_gate=gate,
        target=target or RedshiftTarget(),
        compute=compute if compute is not None else dict(SERVERLESS_8_RPU),
        auth_method="iam_serverless:default_chain",
        scope_origin=scope_origin,
        clock=connection.clock,
    )


# --- metadata (cheap) ---------------------------------------------------------------


def test_capabilities_shape_and_free(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    caps = adapter.capabilities()
    assert caps["connector"] == "redshift"
    assert caps["dialect"] == "redshift"
    assert caps["read_only"] is True
    assert caps["session_read_only"] is True  # the SET took effect on the fake
    assert caps["paradigm"] == "compute_time"
    assert caps["auth_method"] == "iam_serverless:default_chain"
    assert caps["database"] == "dexdb"
    assert caps["server_version"] == "Redshift 1.0.12345"
    assert caps["schema_count"] == 1  # shop
    assert caps["compute"] == SERVERLESS_8_RPU
    assert caps["budget"]["ceiling_seconds"] == 600.0
    assert caps["budget"]["ceiling_rpu_hours"] == pytest.approx(600.0 * 8 / 3600)
    # Serverless honesty: metadata is billable activity when compute is idle.
    assert any("60-second minimum" in w for w in caps["warnings"])
    # Capabilities is a cheap probe: no data statement ran.
    assert fake_redshift_connection.data_statements == []


def test_session_is_read_only_and_tagged_before_any_statement(
    fake_redshift_connection,
):
    adapter = make_adapter(fake_redshift_connection)
    adapter.list_objects()
    prepared = [s.sql.lower() for s in fake_redshift_connection.statements[:2]]
    assert any("set default_transaction_read_only = on" in sql for sql in prepared)
    assert any("set query_group = 'dex'" in sql for sql in prepared)


def test_declined_session_read_only_is_tolerated_and_reported():
    connection = FakeRedshiftConnection(
        tables=[
            FakeRedshiftTable(
                schema="shop", name="t", columns=[("id", "bigint", True)], size_mb=1
            )
        ],
        reject_read_only=True,
    )
    adapter = make_adapter(connection)
    caps = adapter.capabilities()
    # The refusal is not fatal (SELECT-only and grants still hold); it is honest.
    assert caps["session_read_only"] is False


def test_list_objects_uses_cheap_catalog_metadata_only(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "dexdb.shop.customers",
        "dexdb.shop.events",
        "dexdb.shop.signups",
    ]
    customers = next(o for o in objects if o.name == "customers")
    assert customers.row_count == 100
    assert customers.byte_size == 5_000 * 1024 * 1024
    assert customers.column_count == 3
    assert fake_redshift_connection.data_statements == []


def test_empty_table_omitted_by_svv_table_info_still_appears(fake_redshift_connection):
    """SVV_TABLE_INFO omits tables holding no data; the pg_class census is the
    inventory of record precisely so those tables do not silently vanish."""

    adapter = make_adapter(fake_redshift_connection)
    signups, _ = adapter.table_metadata("dexdb.shop.signups")
    assert signups.row_count == 0
    assert signups.byte_size == 0


def test_schema_allowlist_scopes_inventory(fake_redshift_connection):
    fake_redshift_connection.tables.append(
        FakeRedshiftTable(
            schema="other",
            name="noise",
            columns=[("id", "bigint", True)],
            size_mb=1,
        )
    )
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(schemas=["shop"])
    )
    assert {o.schema for o in adapter.list_objects()} == {"shop"}


def test_the_resolved_scope_is_pushed_into_the_census_sql(fake_redshift_connection):
    """A one-schema scope in a wide warehouse filters server-side: the three
    censuses must not transfer every other schema's catalog rows just to drop
    them client-side."""

    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(schemas=["shop"])
    )
    adapter.list_objects()
    census = [
        s.sql
        for s in fake_redshift_connection.statements
        if "pg_class" in s.sql or "svv_table_info" in s.sql or "svv_columns" in s.sql
    ]
    assert len(census) == 3
    assert all("IN ('shop')" in sql for sql in census)


def test_views_carry_no_stored_size(fake_redshift_connection):
    fake_redshift_connection.tables.append(
        FakeRedshiftTable(
            schema="shop",
            name="v_totals",
            columns=[("total", "numeric", True)],
            kind="view",
        )
    )
    adapter = make_adapter(fake_redshift_connection)
    meta, _ = adapter.table_metadata("dexdb.shop.v_totals")
    assert meta.object_type == "view"
    assert meta.row_count is None
    assert meta.byte_size is None


def test_materialized_view_backing_tables_are_hidden(fake_redshift_connection):
    fake_redshift_connection.tables.append(
        FakeRedshiftTable(
            schema="shop",
            name="mv_tbl__totals__0",
            columns=[("total", "numeric", True)],
            size_mb=1,
        )
    )
    adapter = make_adapter(fake_redshift_connection)
    assert "dexdb.shop.mv_tbl__totals__0" not in {
        o.identifier for o in adapter.list_objects()
    }


def test_unknown_object_names_the_fix(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    with pytest.raises(RedshiftConnectionError, match=r"redshift\.schemas"):
        adapter.table_metadata("dexdb.shop.missing")


def test_get_adapter_constructs_redshift(fake_redshift_connection):
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=10.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="redshift",
    )
    adapter = get_adapter(
        "redshift", connection=fake_redshift_connection, cost_gate=gate
    )
    assert isinstance(adapter, RedshiftAdapter)
    assert get_dialect("redshift") == "redshift"


def test_no_sampling_knob_exists():
    """Redshift has no TABLESAMPLE, so a sampled-profiling threshold would be
    a lie; the budget is the only bound."""

    assert "max_full_profile_bytes" not in RedshiftTarget.model_fields


# --- estimation (cheap; feeds the confirm handshake) ---------------------------------


def test_profile_estimate_scales_with_bytes_and_floors_the_wake_minimum(
    fake_redshift_connection,
):
    adapter = make_adapter(fake_redshift_connection)
    total, per_table = adapter.profile_estimate(["dexdb.shop.customers"])
    assert per_table["dexdb.shop.customers"] == pytest.approx(CUSTOMERS_SCAN_SECONDS)
    assert total == pytest.approx(CUSTOMERS_SCAN_SECONDS + _WAKE_MINIMUM_SECONDS)
    # Estimation is cheap: metadata only, no scan ran.
    assert fake_redshift_connection.data_statements == []


def test_no_wake_floor_on_provisioned(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, compute=dict(PROVISIONED))
    total, _ = adapter.profile_estimate(["dexdb.shop.customers"])
    assert total == pytest.approx(CUSTOMERS_SCAN_SECONDS)


def test_estimate_scales_with_base_capacity(fake_redshift_connection):
    """A 16-RPU workgroup scans (an estimated) twice as fast as the 8-RPU
    reference, so estimated seconds halve while estimated RPU-hours hold."""

    adapter = make_adapter(
        fake_redshift_connection,
        compute={"kind": "serverless", "workgroup": "big", "base_capacity_rpus": 16.0},
    )
    _total, per_table = adapter.profile_estimate(["dexdb.shop.customers"])
    assert per_table["dexdb.shop.customers"] == pytest.approx(
        CUSTOMERS_SCAN_SECONDS / 2
    )


def test_query_estimate_sums_referenced_tables(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    estimate = adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
    assert estimate == pytest.approx(CUSTOMERS_SCAN_SECONDS + _WAKE_MINIMUM_SECONDS)


def test_estimate_floor_counts_once_across_many_estimates(fake_redshift_connection):
    """A command that sweeps many statements (maintain probes one per table)
    wakes compute at most once, so summing per-statement estimates must not
    multiply the floor in (verified live: a twelve-probe sweep quoted 738
    seconds of which 720 were floors)."""

    adapter = make_adapter(fake_redshift_connection)
    first = adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
    second = adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
    assert first == pytest.approx(CUSTOMERS_SCAN_SECONDS + _WAKE_MINIMUM_SECONDS)
    assert second == pytest.approx(CUSTOMERS_SCAN_SECONDS)


def test_query_estimate_qualifies_two_part_names(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, compute=dict(PROVISIONED))
    estimate = adapter.query_estimate("SELECT count(*) FROM shop.customers")
    assert estimate == pytest.approx(CUSTOMERS_SCAN_SECONDS)


def test_query_estimate_floors_small_statements(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, compute=dict(PROVISIONED))
    assert (
        adapter.query_estimate("SELECT count(*) FROM dexdb.shop.not_registered")
        == _MIN_STATEMENT_SECONDS
    )


def test_query_estimate_refuses_non_select(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    with pytest.raises(Exception, match="read-only"):
        adapter.query_estimate("DELETE FROM dexdb.shop.customers")


def test_describe_estimate_names_seconds_rpu_and_the_floor(fake_redshift_connection):
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(rpu_price_usd=0.36)
    )
    payload = adapter.describe_estimate(160.0, {"dexdb.shop.customers": 100.0})
    assert payload["estimated_seconds"] == 160.0
    assert payload["estimate_quality"] == "heuristic"
    assert "--confirm --budget" in payload["hint"]
    assert "statement_timeout" in payload["hint"]
    assert payload["per_table_seconds"] == {"dexdb.shop.customers": 100.0}
    assert any("wake minimum" in note for note in payload["notes"])
    assert payload["estimated_rpu_hours"] == pytest.approx(160.0 * 8 / 3600, abs=1e-6)
    assert payload["rpu_rate"]["base_capacity_rpus"] == 8.0
    assert payload["rpu_rate"]["approximate"] is True
    assert payload["estimated_usd"] == pytest.approx(160.0 * 8 / 3600 * 0.36, abs=1e-4)


def test_no_rpu_translation_without_capacity_or_on_provisioned(
    fake_redshift_connection,
):
    unknown = make_adapter(
        fake_redshift_connection,
        compute={"kind": "serverless", "workgroup": "wg", "base_capacity_rpus": None},
    )
    assert "estimated_rpu_hours" not in unknown.describe_estimate(60.0)
    assert unknown.spend_display() == {}
    provisioned = make_adapter(fake_redshift_connection, compute=dict(PROVISIONED))
    assert "estimated_rpu_hours" not in provisioned.describe_estimate(60.0)
    assert provisioned.spend_display() == {}


def test_spend_display_translates_actual_seconds(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=9.0
    )
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(rpu_price_usd=0.36)
    )
    adapter.run_query(
        "SELECT count(*) AS n FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=30.0,
    )
    display = adapter.spend_display()
    assert display["rpu_hours_billed"] == pytest.approx(9.0 * 8 / 3600)
    assert display["usd_billed"] == pytest.approx(9.0 * 8 / 3600 * 0.36, abs=1e-4)


# --- the cost gate binds ------------------------------------------------------------


def test_unconfirmed_scan_never_executes(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, confirmed=False)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    with pytest.raises(ConfirmationRequiredError):
        adapter.column_aggregates("dexdb.shop.customers", columns)
    assert fake_redshift_connection.data_statements == []


def test_over_ceiling_refuses_client_side(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, ceiling=1.0)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("dexdb.shop.customers", columns)
    assert fake_redshift_connection.data_statements == []


def test_every_billed_statement_is_server_capped(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[
            {
                "n_total": 100,
                "nn_0": 100,
                "nd_0": 90,
                "nn_1": 90,
                "nd_1": 80,
                "nn_2": 70,
            }
        ],
        seconds=1.0,
    )
    adapter = make_adapter(fake_redshift_connection, ceiling=600.0)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns)
    billed = fake_redshift_connection.data_statements
    assert billed, "the aggregate batch must have run"
    for statement in billed:
        assert statement.session_timeout_ms is not None
        assert statement.session_timeout_ms <= 600_000


def test_server_timeout_translates_to_over_ceiling(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[], seconds=999.0
    )
    adapter = make_adapter(fake_redshift_connection, ceiling=200.0)
    with pytest.raises(OverCeilingError, match="statement_timeout"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=500.0,
        )
    # The killed statement still billed what ran (the timeout's worth).
    assert fake_redshift_connection.clock.now == pytest.approx(200.0, abs=1.0)


def test_wall_clock_timeout_translates_to_timeout_error(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[], seconds=999.0
    )
    adapter = make_adapter(fake_redshift_connection, ceiling=600.0)
    with pytest.raises(TimeoutError, match="narrow it"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )


def test_a_wlm_cancel_is_not_blamed_on_the_budget(fake_redshift_connection):
    """WLM query-monitoring rules and admin kills also say "canceled"; only a
    statement_timeout kill (SQLSTATE 57014) may be translated into budget
    advice, or the user is told to raise --budget for a refusal it cannot fix."""

    from redshift_connector import error as rs_errors

    def resolve(sql):
        raise rs_errors.ProgrammingError(
            {
                "S": "ERROR",
                "C": "XX000",
                "M": "Query (12345) canceled by WLM query monitoring rule",
            }
        )

    fake_redshift_connection.row_resolver = resolve
    adapter = make_adapter(fake_redshift_connection, ceiling=600.0)
    with pytest.raises(rs_errors.ProgrammingError, match="WLM"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )


def test_a_dropped_connection_still_records_billed_seconds(
    fake_redshift_connection,
):
    """A statement that dies mid-flight (network drop, interrupt) billed the
    seconds that ran; the ledger and session ceiling must see them even though
    the exception is not a ProgrammingError."""

    from redshift_connector import error as rs_errors

    clock = fake_redshift_connection.clock

    def resolve(sql):
        clock.now += 50.0
        raise rs_errors.InterfaceError("connection reset by peer")

    fake_redshift_connection.row_resolver = resolve
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(rpu_price_usd=0.36)
    )
    with pytest.raises(rs_errors.InterfaceError):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=120.0,
        )
    display = adapter.spend_display()
    assert display["rpu_hours_billed"] == pytest.approx(50.0 * 8 / 3600)


def test_exhausted_budget_refuses_before_the_server(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 1}], seconds=99.4
    )
    adapter = make_adapter(
        fake_redshift_connection, ceiling=100.0, compute=dict(PROVISIONED)
    )
    adapter.run_query(
        "SELECT count(*) FROM dexdb.shop.not_registered",
        max_rows=10,
        timeout_seconds=500.0,
    )
    # 99.4 of 100 seconds billed; under one second remains.
    with pytest.raises(OverCeilingError, match="under one compute-second"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.not_registered",
            max_rows=10,
            timeout_seconds=500.0,
        )


def test_actual_seconds_land_in_the_ledger(fake_redshift_connection):
    entries: list[dict] = []
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=2.5
    )
    adapter = make_adapter(fake_redshift_connection, record=entries.append)
    adapter.run_query(
        "SELECT count(*) AS n FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=30.0,
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["connector"] == "redshift"
    assert entry["billed_seconds"] == pytest.approx(2.5)
    assert "billed_bytes" not in entry
    assert entry["statement_sha256"]
    assert "SELECT" not in str(entry.values())


def test_session_ceiling_binds_across_commands(fake_redshift_connection):
    adapter = make_adapter(
        fake_redshift_connection,
        ceiling=None,
        session_ceiling=100.0,
        session_spent=99.5,
    )
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )


def test_wake_floor_is_charged_exactly_once_per_command(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=0.1
    )
    # customers estimates 100s; the floor adds 60 once. Two queries fit a 270s
    # ceiling only if the second one does not carry the floor again
    # (100 + 60 + 100 = 260 <= 270; a re-floored 320 would refuse).
    adapter = make_adapter(fake_redshift_connection, ceiling=270.0)
    for _ in range(2):
        adapter.run_query(
            "SELECT count(*) AS n FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )
    # And the floor was genuinely charged the first time: the same single query
    # is refused when the ceiling covers the scan but not the floor.
    strict = make_adapter(fake_redshift_connection, ceiling=155.0)
    with pytest.raises(OverCeilingError):
        strict.run_query(
            "SELECT count(*) AS n FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )


# --- profiling ----------------------------------------------------------------------


def _aggregate_rows(sql: str) -> FakeResult:
    # COUNT(*) plus per-column aggregates; values keyed by alias.
    aliases = [part.split(" AS ")[-1] for part in sql.split(",")]
    row = {}
    for alias in aliases:
        alias = alias.split(" FROM ")[0].strip()
        row[alias] = 100 if alias == "n_total" else 90
    return FakeResult(rows=[row], seconds=0.5)


def test_aggregates_use_hll_distinct_in_one_pass(fake_redshift_connection):
    fake_redshift_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_redshift_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, safe_min_max={"id"}
    )
    sql = fake_redshift_connection.data_statements[0].sql
    assert "COUNT(*)" in sql
    # HLL(), not APPROXIMATE COUNT(DISTINCT): Redshift caps the latter at 3
    # per statement (verified live), so a wide batch must use the former.
    assert 'HLL("id")' in sql
    assert "COUNT(DISTINCT" not in sql
    by_name = {a.name: a for a in aggregates}
    assert by_name["id"].distinct_count == 90
    assert by_name["id"].distinct_count_exact is False
    assert by_name["id"].is_unique is None  # an approximation is never a proof
    assert by_name["email"].null_fraction == pytest.approx(0.1)


def test_degraded_types_get_non_null_counts_only(fake_redshift_connection):
    fake_redshift_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_redshift_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, safe_min_max={"payload"}
    )
    sql = fake_redshift_connection.data_statements[0].sql
    by_name = {a.name: a for a in aggregates}
    # payload is SUPER: no distinct, no min/max, only the non-null count.
    assert by_name["payload"].distinct_count is None
    assert by_name["payload"].min_value is None
    assert 'MIN("payload")' not in sql
    assert 'HLL("payload")' not in sql
    assert by_name["payload"].null_fraction is not None


def test_min_max_only_for_engine_cleared_columns(fake_redshift_connection):
    fake_redshift_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_redshift_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns, safe_min_max={"id"})
    sql = fake_redshift_connection.data_statements[0].sql
    assert 'MIN("id")' in sql
    assert 'MIN("email")' not in sql


def test_exact_count_from_scan_supersedes_tbl_rows(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[
            {"n_total": 137, "nn_0": 137, "nd_0": 137, "nn_1": 1, "nd_1": 1, "nn_2": 1}
        ],
        seconds=0.5,
    )
    adapter = make_adapter(fake_redshift_connection)
    meta, columns = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 100  # the SVV_TABLE_INFO estimate
    adapter.column_aggregates("dexdb.shop.customers", columns)
    meta, _ = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 137  # the exact count the scan paid for


def test_exact_distinct_counts_ride_with_count_star(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 120, "d_0": 120}], seconds=0.5
    )
    adapter = make_adapter(fake_redshift_connection)
    counts = adapter.exact_distinct_counts("dexdb.shop.customers", ["id"])
    assert counts == {"id": 120}
    sql = fake_redshift_connection.data_statements[0].sql
    assert 'COUNT(DISTINCT "id")' in sql
    assert "COUNT(*)" in sql
    meta, _ = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 120


def test_escalation_skipped_when_budget_cannot_cover(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, ceiling=1.0)
    counts = adapter.exact_distinct_counts("dexdb.shop.customers", ["id"])
    assert counts == {}
    assert fake_redshift_connection.data_statements == []
    notes = adapter.table_notes("dexdb.shop.customers")
    assert any("escalation skipped" in note for note in notes)


def test_distinct_combination_counts_batch_into_one_guarded_statement(
    fake_redshift_connection,
):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"d_0": 97, "d_1": 100}], seconds=0.5
    )
    adapter = make_adapter(fake_redshift_connection, ceiling=100_000.0)
    counts = adapter.distinct_combination_counts(
        "dexdb.shop.customers", [["id", "email"], ["email", "id"]]
    )
    assert counts == {("id", "email"): 97, ("email", "id"): 100}
    stmts = fake_redshift_connection.data_statements
    assert len(stmts) == 1
    sql = stmts[0].sql
    assert "SELECT DISTINCT" in sql
    assert ") AS q_0" in sql
    assert assert_select_only(sql, dialect="redshift") == sql
    assert adapter.distinct_combination_counts("dexdb.shop.customers", []) == {}


def test_composite_probe_skipped_when_budget_cannot_cover(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection, ceiling=1.0)
    result = adapter.distinct_combination_counts(
        "dexdb.shop.customers", [["id", "email"]]
    )
    assert result == {}
    assert fake_redshift_connection.data_statements == []
    assert any(
        "composite-key probe skipped" in note
        for note in adapter.table_notes("dexdb.shop.customers")
    )


def test_composite_probe_carries_the_wake_floor_when_it_bills_first(
    fake_redshift_connection,
):
    """Like the exact-distinct escalation, the composite probe can be a
    command's first billed statement, so the pending wake minimum rides its
    charge (and stays pending on refusal). One probe over customers estimates
    scan (100s) + floor (60s)."""

    def resolve(sql):
        if "SELECT DISTINCT" in sql:
            return FakeResult(rows=[{"d_0": 100}], seconds=0.1)
        return FakeResult(rows=[{"n": 1}], seconds=0.1)

    fake_redshift_connection.row_resolver = resolve

    # Covers the scan but not scan + floor: the probe must refuse rather than
    # under-charge its way in, and the floor stays pending.
    strict = make_adapter(fake_redshift_connection, ceiling=155.0)
    assert (
        strict.distinct_combination_counts("dexdb.shop.customers", [["id", "email"]])
        == {}
    )
    assert fake_redshift_connection.data_statements == []
    assert strict._wake_floor_pending is True

    # Once charged, the floor is consumed: probe (100 + 60) plus a follow-up
    # query (100, floorless) fit a 270s ceiling only if the second statement
    # does not carry the floor again.
    adapter = make_adapter(fake_redshift_connection, ceiling=270.0)
    assert adapter.distinct_combination_counts(
        "dexdb.shop.customers", [["id", "email"]]
    ) == {("id", "email"): 100}
    adapter.run_query(
        "SELECT count(*) AS n FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=30.0,
    )


def test_escalation_carries_the_wake_floor_when_it_bills_first(
    fake_redshift_connection,
):
    """The exact-distinct scan can be a command's first billed statement
    (maintain grain runs key checks before any probe), so the pending wake
    minimum rides its charge and is consumed for the statements that follow;
    a refused charge leaves the floor pending."""

    def resolve(sql):
        if "COUNT(DISTINCT" in sql:
            return FakeResult(rows=[{"n_total": 120, "d_0": 120}], seconds=0.1)
        return FakeResult(rows=[{"n": 1}], seconds=0.1)

    fake_redshift_connection.row_resolver = resolve

    # The ceiling covers the scan (100s) but not scan + floor (160s): the
    # escalation must refuse rather than under-charge its way in.
    strict = make_adapter(fake_redshift_connection, ceiling=155.0)
    assert strict.exact_distinct_counts("dexdb.shop.customers", ["id"]) == {}
    assert fake_redshift_connection.data_statements == []

    # And once charged, the floor is consumed: escalation (100 + 60) plus a
    # follow-up query (100, floorless) fit a 270s ceiling only if the second
    # statement does not carry the floor again.
    adapter = make_adapter(fake_redshift_connection, ceiling=270.0)
    assert adapter.exact_distinct_counts("dexdb.shop.customers", ["id"]) == {"id": 120}
    adapter.run_query(
        "SELECT count(*) AS n FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=30.0,
    )


# --- run_query ----------------------------------------------------------------------


def test_run_query_truncates_and_reports(fake_redshift_connection):
    fake_redshift_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": i} for i in range(5)], seconds=0.1
    )
    adapter = make_adapter(fake_redshift_connection)
    result = adapter.run_query(
        "SELECT id AS n FROM dexdb.shop.customers",
        max_rows=3,
        timeout_seconds=30.0,
    )
    assert result.columns == ["n"]
    assert len(result.cells) == 3
    assert result.truncated is True


def test_run_query_rejects_writes(fake_redshift_connection):
    adapter = make_adapter(fake_redshift_connection)
    with pytest.raises(Exception, match="read-only"):
        adapter.run_query(
            "UPDATE dexdb.shop.customers SET email = 'x'",
            max_rows=10,
            timeout_seconds=30.0,
        )
    assert fake_redshift_connection.data_statements == []


# --- credential discovery -----------------------------------------------------------


class _StubServerlessClient:
    def __init__(self, *, base_capacity=8, endpoint=True, db_name="dexdb"):
        self._base_capacity = base_capacity
        self._endpoint = endpoint
        self._db_name = db_name

    def get_workgroup(self, workgroupName):  # noqa: N803 (boto3's spelling)
        workgroup = {
            "workgroupName": workgroupName,
            "namespaceName": "dex-ns",
            "status": "AVAILABLE",
            "baseCapacity": self._base_capacity,
        }
        if self._endpoint:
            workgroup["endpoint"] = {
                "address": (
                    f"{workgroupName}.123456789012.eu-central-1"
                    ".redshift-serverless.amazonaws.com"
                ),
                "port": 5439,
            }
        return {"workgroup": workgroup}

    def get_namespace(self, namespaceName):  # noqa: N803 (boto3's spelling)
        return {"namespace": {"namespaceName": namespaceName, "dbName": self._db_name}}


def _stub_boto3(monkeypatch, client: _StubServerlessClient):
    class _Session:
        def __init__(self, *, profile_name=None, region_name=None):
            self.profile_name = profile_name
            self.region_name = region_name

        def client(self, service):
            assert service == "redshift-serverless"
            return client

    import boto3

    monkeypatch.setattr(boto3, "Session", _Session)


def test_discovery_prefers_the_pinned_workgroup(monkeypatch, tmp_path: Path):
    _stub_boto3(monkeypatch, _StubServerlessClient())
    params, method, compute = resolve_redshift_connection(
        RedshiftTarget(workgroup="dex-wg", aws_profile="dex"),
        {"REDSHIFT_HOST": "ignored.example.com"},
        tmp_path,
    )
    assert params["iam"] is True
    assert params["host"].endswith(".redshift-serverless.amazonaws.com")
    assert params["port"] == 5439
    assert params["database"] == "dexdb"  # discovered from the namespace
    assert params["profile"] == "dex"
    assert method == "iam_serverless:profile"
    assert compute == {
        "kind": "serverless",
        "workgroup": "dex-wg",
        "base_capacity_rpus": 8.0,
    }


def test_discovery_workgroup_without_endpoint_names_the_state(
    monkeypatch, tmp_path: Path
):
    _stub_boto3(monkeypatch, _StubServerlessClient(endpoint=False))
    with pytest.raises(CredentialDiscoveryError, match="AVAILABLE"):
        resolve_redshift_connection(RedshiftTarget(workgroup="dex-wg"), {}, tmp_path)


def test_discovery_workgroup_failure_names_every_fix(monkeypatch, tmp_path: Path):
    from botocore.exceptions import BotoCoreError

    class _Broken:
        def get_workgroup(self, workgroupName):  # noqa: N803 (boto3's spelling)
            raise BotoCoreError()

    _stub_boto3(monkeypatch, _Broken())
    with pytest.raises(CredentialDiscoveryError) as excinfo:
        resolve_redshift_connection(RedshiftTarget(workgroup="dex-wg"), {}, tmp_path)
    message = str(excinfo.value)
    for fix in ("aws configure", "AWS_*", "GetWorkgroup", "GetCredentials"):
        assert fix in message


def test_discovery_cluster_requires_dbname_and_user(tmp_path: Path):
    with pytest.raises(CredentialDiscoveryError, match=r"redshift\.dbname"):
        resolve_redshift_connection(
            RedshiftTarget(cluster_identifier="prod-cluster"), {}, tmp_path
        )


def test_discovery_cluster_params(tmp_path: Path):
    params, method, compute = resolve_redshift_connection(
        RedshiftTarget(
            cluster_identifier="prod-cluster",
            dbname="shop",
            user="dex_ro",
            region="eu-central-1",
        ),
        {},
        tmp_path,
    )
    assert params == {
        "iam": True,
        "cluster_identifier": "prod-cluster",
        "database": "shop",
        "db_user": "dex_ro",
        "region": "eu-central-1",
    }
    assert method == "iam_cluster:default_chain"
    assert compute["kind"] == "provisioned"


def test_discovery_env_redshift_host(tmp_path: Path):
    params, method, compute = resolve_redshift_connection(
        RedshiftTarget(),
        {
            "REDSHIFT_HOST": "cluster.abc.eu-central-1.redshift.amazonaws.com",
            "REDSHIFT_DATABASE": "shop",
            "REDSHIFT_USER": "dex_ro",
            "REDSHIFT_PASSWORD": "secret",
        },
        tmp_path,
    )
    assert params["host"] == "cluster.abc.eu-central-1.redshift.amazonaws.com"
    assert params["database"] == "shop"
    assert params["password"] == "secret"  # noqa: S105 (a fixture, not a credential)
    assert method == "environment:password"
    assert compute["kind"] == "provisioned"


def test_discovery_env_serverless_host_flags_serverless(tmp_path: Path):
    _params, _method, compute = resolve_redshift_connection(
        RedshiftTarget(),
        {
            "REDSHIFT_HOST": (
                "wg.123456789012.eu-central-1.redshift-serverless.amazonaws.com"
            ),
            "REDSHIFT_DATABASE": "shop",
        },
        tmp_path,
    )
    assert compute == {
        "kind": "serverless",
        "workgroup": None,
        "base_capacity_rpus": None,
    }


def test_discovery_config_target(tmp_path: Path):
    params, method, _compute = resolve_redshift_connection(
        RedshiftTarget(host="db.internal", port=5440, dbname="shop", user="dex"),
        {},
        tmp_path,
    )
    assert params == {
        "host": "db.internal",
        "port": 5440,
        "database": "shop",
        "user": "dex",
    }
    assert method == "config_target:external"


def test_discovery_dbt_profile_fallback(tmp_path: Path):
    project = tmp_path / "analytics"
    project.mkdir()
    (project / "dbt_project.yml").write_text(
        "name: analytics\nversion: '1.0.0'\nprofile: analytics\n", encoding="utf-8"
    )
    (project / "profiles.yml").write_text(
        "analytics:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: redshift\n"
        "      host: wg.example.redshift-serverless.amazonaws.com\n"
        "      port: 5439\n"
        "      user: dbt\n"
        "      password: hunter2\n"
        "      dbname: shop\n",
        encoding="utf-8",
    )
    params, method, compute = resolve_redshift_connection(
        RedshiftTarget(), {}, tmp_path
    )
    assert params["host"] == "wg.example.redshift-serverless.amazonaws.com"
    assert params["database"] == "shop"
    assert method == "dbt_profile:password"
    assert compute["kind"] == "serverless"


def _write_dbt_profile(tmp_path: Path, output_lines: str) -> None:
    project = tmp_path / "analytics"
    project.mkdir()
    (project / "dbt_project.yml").write_text(
        "name: analytics\nversion: '1.0.0'\nprofile: analytics\n", encoding="utf-8"
    )
    (project / "profiles.yml").write_text(
        "analytics:\n  target: dev\n  outputs:\n    dev:\n" + output_lines,
        encoding="utf-8",
    )


def test_discovery_renders_the_profiles_env_var_password_like_dbt_would(
    tmp_path: Path,
):
    """dex's own transform init writes the password as an env_var reference;
    discovering that profile must resolve the reference, not send the literal
    Jinja template to the driver as a password."""

    _write_dbt_profile(
        tmp_path,
        "      type: redshift\n"
        "      host: db.internal\n"
        "      user: dbt\n"
        "      password: \"{{ env_var('REDSHIFT_PASSWORD') }}\"\n"
        "      dbname: shop\n",
    )
    params, method, _compute = resolve_redshift_connection(
        RedshiftTarget(), {"REDSHIFT_PASSWORD": "s3cret"}, tmp_path
    )
    assert params["password"] == "s3cret"  # noqa: S105 (a test-only stand-in)
    assert method == "dbt_profile:password"


def test_discovery_skips_a_profile_whose_template_cannot_render(tmp_path: Path):
    """With the referenced env var unset, the output cannot connect as
    discovered: discovery falls through to the failure that names the fix
    instead of attempting auth with a Jinja literal."""

    _write_dbt_profile(
        tmp_path,
        "      type: redshift\n"
        "      host: db.internal\n"
        "      user: dbt\n"
        "      password: \"{{ env_var('REDSHIFT_PASSWORD') }}\"\n"
        "      dbname: shop\n",
    )
    with pytest.raises(CredentialDiscoveryError, match="REDSHIFT_PASSWORD"):
        resolve_redshift_connection(RedshiftTarget(), {}, tmp_path)


def test_discovery_skips_an_iam_profile_output(tmp_path: Path):
    """A method: iam output mints credentials at dbt runtime; it carries
    nothing durable for a native connection, so discovery must not treat its
    host as a password-path candidate."""

    _write_dbt_profile(
        tmp_path,
        "      type: redshift\n"
        "      method: iam\n"
        "      host: wg.example.redshift-serverless.amazonaws.com\n"
        "      user: iam\n"
        "      dbname: shop\n",
    )
    with pytest.raises(CredentialDiscoveryError):
        resolve_redshift_connection(RedshiftTarget(), {}, tmp_path)


def test_discovery_failure_names_every_fix(tmp_path: Path):
    with pytest.raises(CredentialDiscoveryError) as excinfo:
        resolve_redshift_connection(RedshiftTarget(), {}, tmp_path)
    message = str(excinfo.value)
    for fix in (
        "redshift.workgroup",
        "redshift.cluster_identifier",
        "REDSHIFT_HOST",
        "REDSHIFT_PASSWORD",
        "profiles.yml",
    ):
        assert fix in message


def test_discovery_never_surfaces_a_password(tmp_path: Path):
    _params, method, _compute = resolve_redshift_connection(
        RedshiftTarget(),
        {
            "REDSHIFT_HOST": "h.example.com",
            "REDSHIFT_DATABASE": "shop",
            "REDSHIFT_PASSWORD": "supersecret",
        },
        tmp_path,
    )
    assert "supersecret" not in method


# --- scope resolution: an entry that names nothing is refused, not dropped ------------


def test_nonexistent_schema_scope_is_refused_and_names_what_exists(
    fake_redshift_connection,
):
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(schemas=["no_such_schema"])
    )
    with pytest.raises(RedshiftConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "no_such_schema" in message
    assert "shop" in message  # the schemas that do exist
    assert "[from redshift.schemas in .dex/config.yml]" in message
    assert fake_redshift_connection.data_statements == []


def test_scope_refusal_blames_the_flag_it_came_from(fake_redshift_connection):
    adapter = make_adapter(
        fake_redshift_connection,
        target=RedshiftTarget(schemas=["nope"]),
        scope_origin="--scope",
    )
    with pytest.raises(RedshiftConnectionError, match=r"\[from --scope\]"):
        adapter.list_objects()


def test_a_qualified_scope_is_refused_as_redshift_vocabulary(fake_redshift_connection):
    """dbt refuses cross-database references outright, so a Redshift scope is
    always a bare schema in the connected database."""

    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(schemas=["dexdb.shop"])
    )
    with pytest.raises(RedshiftConnectionError, match="never a database or a table"):
        adapter.list_objects()


def test_a_valid_scope_still_bounds_the_inventory(fake_redshift_connection):
    adapter = make_adapter(
        fake_redshift_connection, target=RedshiftTarget(schemas=["shop"])
    )
    assert {o.schema for o in adapter.list_objects()} == {"shop"}
    assert fake_redshift_connection.data_statements == []


# --- the dev-target preflight (cheap): the privilege, not the object ------------------


def _with_user(user: FakeUser) -> FakeRedshiftConnection:
    return FakeRedshiftConnection(
        tables=[
            FakeRedshiftTable(
                schema="shop",
                name="customers",
                columns=[("id", "bigint", False)],
                size_mb=1,
            )
        ],
        users=[user],
        empty_schemas=["dbt_dev"],
    )


def test_a_writable_dev_schema_is_fine():
    connection = _with_user(
        FakeUser(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE", "CREATE"}})
    )
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []
    assert connection.data_statements == []


def test_a_dev_schema_the_user_cannot_write_is_reported():
    connection = _with_user(
        FakeUser(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE"}})
    )
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == [
        'CREATE on dev_schema "dbt_dev"'
    ]


def test_an_absent_dev_schema_is_fine_when_the_user_may_create_it():
    """dbt creates the schema itself, so absence alone is not the failure."""

    connection = _with_user(FakeUser(name="dbt_dev", may_create_in_database=True))
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("not_there", role="dbt_dev") == []


def test_an_absent_dev_schema_the_user_cannot_create_is_reported():
    connection = _with_user(FakeUser(name="dbt_dev", may_create_in_database=False))
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("not_there", role="dbt_dev") == [
        'dev_schema "not_there"'
    ]


def test_a_profile_user_the_database_does_not_know_is_refused():
    connection = _with_user(FakeUser(name="dbt_dev"))
    adapter = make_adapter(connection)
    with pytest.raises(RedshiftConnectionError) as exc:
        adapter.missing_dev_namespaces("dbt_dev", role="ghost")
    assert "ghost" in str(exc.value)
