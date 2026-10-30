"""The Snowflake adapter against the stateful fake connection: metadata is
free (SHOW commands, no warehouse), every billed statement is estimated and
gated in warehouse-seconds, and the budget binds at both the client (charge)
and the simulated server (statement timeout)."""

from __future__ import annotations

import os
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
from exmergo_dex_core.command_args import with_uptime
from exmergo_dex_core.config import SnowflakeTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    UPTIME_LINE,
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
    history=None,
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
        history=history,
    )
    return SnowflakeAdapter(
        connection=connection,
        cost_gate=gate,
        target=target or SnowflakeTarget(warehouse="DEX_WH"),
        account="TESTORG-TESTACCT",
        auth_method="named_connection:key_pair",
        scope_override=scope_override,
        clock=connection.clock,
        wall_clock=connection.clock.wall,
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


def test_a_database_scope_never_inventories_information_schema(fake_sf_connection):
    # SHOW ... IN DATABASE lists the database's INFORMATION_SCHEMA views beside
    # its own objects; profiling them would scan Snowflake's catalog as data.
    from fakes.snowflake import FakeSnowflakeTable

    fake_sf_connection.tables.append(
        FakeSnowflakeTable(
            database="SHOP",
            schema="INFORMATION_SCHEMA",
            name="COLUMNS",
            columns=[("COLUMN_NAME", "TEXT", True)],
            kind="view",
        )
    )
    adapter = make_adapter(
        fake_sf_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SHOP"]),
    )

    objects = adapter.list_objects()
    assert objects
    assert all(o.schema != "INFORMATION_SCHEMA" for o in objects)


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


def test_profile_estimate_is_heuristic_and_the_handshake_adds_the_resume_floor(
    fake_sf_connection,
):
    adapter = make_adapter(fake_sf_connection)
    total, per_table = adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"])
    # 5 GB over the conservative X-Small rate, one batch, and nothing else:
    # the warehouse's uptime belongs to the command, not to a statement.
    scan = per_table["SHOP.PUBLIC.CUSTOMERS"]
    assert scan > 1.0
    assert total == pytest.approx(scan)
    # DEX_WH is suspended, so what the caller is shown carries the 60-second
    # resume minimum on a line of its own.
    shown, lines, _uptime = with_uptime(adapter, total, per_table)
    assert shown == pytest.approx(scan + _RESUME_MINIMUM_SECONDS)
    assert lines[UPTIME_LINE] == pytest.approx(_RESUME_MINIMUM_SECONDS)
    assert data_statements(fake_sf_connection) == []


def test_a_running_warehouse_quotes_its_idle_tail_and_no_resume_minimum(
    fake_sf_connection,
):
    """Nothing resumes a running warehouse, but the last statement still
    leaves it idling for AUTO_SUSPEND, and that bills."""

    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouses[0].auto_suspend = 10
    adapter = make_adapter(fake_sf_connection)
    total, per_table = adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"])
    shown, _lines, _uptime = with_uptime(adapter, total, per_table)
    assert shown == pytest.approx(per_table["SHOP.PUBLIC.CUSTOMERS"] + 10)


def _wide_blob_table():
    from fakes.snowflake import FakeSnowflakeTable

    # 50 plain columns plus one BINARY column: without exclusion this is 2
    # batches (51 columns), with exclusion (the default) it's 1 (50 columns).
    columns = [(f"C_{i}", "FIXED", True) for i in range(50)]
    columns.append(("PAYLOAD", "BINARY", True))
    return FakeSnowflakeTable(
        database="SHOP",
        schema="PUBLIC",
        name="SESSIONS",
        columns=columns,
        rows=100,
        bytes=5_000_000_000,
    )


def test_profile_estimate_accepts_include_blobs_without_crashing(fake_sf_connection):
    # Regression test: explore/commands.py::_profile_estimate always calls
    # adapter.profile_estimate(identifiers, include_blobs=...), so every
    # adapter with a profile_estimate must accept that kwarg.
    adapter = make_adapter(fake_sf_connection)
    adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"], include_blobs=set())


def test_profile_estimate_excludes_blob_columns_from_batch_count(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.tables.append(_wide_blob_table())
    adapter = make_adapter(fake_sf_connection)
    total_excluded, per_excluded = adapter.profile_estimate(["SHOP.PUBLIC.SESSIONS"])
    total_included, per_included = adapter.profile_estimate(
        ["SHOP.PUBLIC.SESSIONS"], include_blobs={"shop.public.sessions.payload"}
    )
    # 1 batch (50 cols) vs 2 batches (51 cols).
    assert per_included["SHOP.PUBLIC.SESSIONS"] == pytest.approx(
        2 * per_excluded["SHOP.PUBLIC.SESSIONS"]
    )
    assert total_excluded == pytest.approx(per_excluded["SHOP.PUBLIC.SESSIONS"])
    assert total_included == pytest.approx(per_included["SHOP.PUBLIC.SESSIONS"])


def test_query_estimate_sums_referenced_tables(fake_sf_connection):
    fake_sf_connection.warehouses[0].state = "STARTED"
    adapter = make_adapter(fake_sf_connection)
    single = adapter.query_estimate('SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"')
    joined = adapter.query_estimate(
        'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS" c '
        'JOIN "SHOP"."PUBLIC"."EVENTS" e ON c."ID" = e."ID"'
    )
    assert joined > single > 0


def test_resume_floor_counts_once_across_many_estimates(fake_sf_connection):
    """A command that prices many statements resumes a suspended warehouse at
    most once, so summing per-statement estimates must not multiply the floor
    in (verified live: mutation coverage priced 39 statements and quoted 2,379
    seconds, of which 2,340 were floors). The estimators leave it out and the
    handshake adds it once."""

    adapter = make_adapter(fake_sf_connection)
    sql = 'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    first = adapter.query_estimate(sql)
    second = adapter.query_estimate(sql)
    assert first == pytest.approx(second)
    shown, _lines, _uptime = with_uptime(adapter, first + second, None)
    assert shown == pytest.approx(first + second + _RESUME_MINIMUM_SECONDS)


def test_a_discarded_estimate_does_not_take_the_uptime_quote(fake_sf_connection):
    """Issue #936: a phase priced and then thrown away (a profile with nothing
    stale) used to take the once-per-command overhead, so the next phase the
    caller saw quoted statement time alone and the run billed the tail."""

    adapter = make_adapter(fake_sf_connection)
    adapter.profile_estimate(["SHOP.PUBLIC.CUSTOMERS"])  # priced, never shown
    probe = adapter.query_estimate('SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"')
    shown, _lines, uptime = with_uptime(adapter, probe, None)
    assert shown == pytest.approx(probe + _RESUME_MINIMUM_SECONDS)
    # Quoted again until a gate admits it, then never again this command.
    assert with_uptime(adapter, probe, None)[0] == pytest.approx(shown)
    uptime.admit()
    assert with_uptime(adapter, probe, None)[0] == pytest.approx(probe)


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


# --- settlement: warehouse uptime, not statement time ---------------------------


def _one_query(adapter, **kwargs) -> None:
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
        **kwargs,
    )


def _settled(adapter) -> tuple[float, list[str]]:
    adapter.cost_gate.settle()
    return adapter.cost_gate.billed, adapter.cost_gate.settlement_notes


def test_a_command_that_resumed_the_warehouse_settles_its_uptime(fake_sf_connection):
    """A resume takes a moment of wall time and bills at least 60 seconds, and
    the warehouse then idles for AUTO_SUSPEND before it suspends. Statement
    time alone reported one second of that."""

    entries: list[dict] = []
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(
        fake_sf_connection,
        record=entries.append,
        target=SnowflakeTarget(warehouse="DEX_WH", credit_price_usd=3.0),
    )
    _one_query(adapter)
    assert entries[0]["billed_seconds"] == pytest.approx(1.0)

    billed, notes = _settled(adapter)
    # max(60, 1 second of uptime + the 60-second idle tail).
    assert billed == pytest.approx(61.0)
    uptime = entries[-1]
    assert uptime["entry"] == "settlement"
    assert uptime["billed_seconds"] == pytest.approx(60.0)
    assert uptime["statement_sha256"] is None
    assert uptime["reservation_id"] == entries[0]["reservation_id"]
    spend = adapter.cost_gate.spend_summary()
    assert spend["settled"] is True
    assert spend["unknown_settlement"] is False
    assert adapter.spend_display()["credits_billed"] == pytest.approx(
        61.0 * 1.0 / 3600, abs=1e-6
    )
    assert len(notes) == 1
    assert "This command resumed DEX_WH" in notes[0]
    assert "60-second AUTO_SUSPEND" in notes[0]
    assert "WAREHOUSE_METERING_HISTORY is the authority" in notes[0]


def test_a_running_warehouse_left_idle_pays_its_tail_and_no_resume_minimum(
    fake_sf_connection,
):
    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouses[0].auto_suspend = 10
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(11.0)
    assert "DEX_WH was already running" in notes[0]


def test_a_warehouse_other_queries_keep_busy_charges_no_idle_tail(
    fake_sf_connection,
):
    """The tail belongs to whoever leaves the warehouse idle, and a command
    that finishes while others are still running does not."""

    fake_sf_connection.warehouses[0].state = "STARTED"
    fake_sf_connection.warehouses[0].running = 2
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(1.0)
    assert "no idle tail is charged" in notes[0]


def test_a_statement_that_never_woke_the_warehouse_adds_no_uptime(
    fake_sf_connection,
):
    """A result-cache hit or a count served from metadata runs without a
    warehouse, so a suspended one stays suspended and bills nothing."""

    entries: list[dict] = []
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=0.2, wakes_warehouse=False
    )
    adapter = make_adapter(fake_sf_connection, record=entries.append)
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(0.2)
    assert notes == []
    assert len(entries) == 1


def test_a_warehouse_that_already_suspended_again_still_bills_its_tail(
    fake_sf_connection,
):
    """Seen suspended after the command means the tail has already been
    billed, not that the warehouse never ran: ``resumed_on`` moved."""

    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    fake_sf_connection.warehouses[0].state = "SUSPENDED"
    billed, _notes = _settled(adapter)
    assert billed == pytest.approx(61.0)


def test_a_warehouse_that_never_suspends_settles_as_a_lower_bound(
    fake_sf_connection,
):
    fake_sf_connection.warehouses[0].auto_suspend = None
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(60.0)
    assert "AUTO_SUSPEND is off" in notes[0]
    assert "lower bound" in notes[0]


def test_a_warehouse_that_cannot_be_read_afterwards_is_charged_its_tail(
    fake_sf_connection,
):
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    fake_sf_connection.warehouses.clear()
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(61.0)
    assert "could not be read" in notes[0]


def test_uptime_is_booked_once_per_command_and_afresh_for_the_next(
    fake_sf_connection,
):
    """A held engine keeps its adapter and swaps the gate, and a resume the
    first command paid for must not be remembered by the second."""

    entries: list[dict] = []
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection, record=entries.append)
    _one_query(adapter)
    adapter.cost_gate.settle()
    adapter.cost_gate.settle()
    # The statement, the claim on the resume cycle, and the uptime.
    assert [e["billed_seconds"] for e in entries] == pytest.approx([1.0, 0.0, 60.0])

    adapter.cost_gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=600.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="snowflake",
        command="explore query",
        record=entries.append,
    )
    sql = 'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    # The warehouse is running now: the second command quotes the tail alone.
    assert with_uptime(adapter, adapter.query_estimate(sql), None)[0] == pytest.approx(
        adapter._statement_estimate(sql) + 60
    )
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(61.0)
    assert "DEX_WH was already running" in notes[0]


def test_the_statement_timeout_holds_back_the_uptime_still_owed(fake_sf_connection):
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    adapter = make_adapter(fake_sf_connection, ceiling=200.0)
    _one_query(adapter)
    # 200 confirmed, less the 60 seconds the warehouse will bill once the
    # statement is done (resumed, then idle until AUTO_SUSPEND).
    assert fake_sf_connection.session_parameters["STATEMENT_TIMEOUT_IN_SECONDS"] == 140


def test_a_budget_under_the_idle_tail_refuses_before_anything_runs(
    fake_sf_connection,
):
    fake_sf_connection.warehouses[0].state = "STARTED"
    adapter = make_adapter(fake_sf_connection, ceiling=30.0)
    with pytest.raises(OverCeilingError):
        _one_query(adapter)
    assert data_statements(fake_sf_connection) == []


def test_the_estimate_says_what_its_uptime_line_is(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection)
    estimate, _lines, _uptime = with_uptime(
        adapter,
        adapter.query_estimate('SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'),
        None,
    )
    notes = adapter.describe_estimate(estimate)["notes"]
    assert any(
        "DEX_WH is suspended, so the first statement resumes it" in n for n in notes
    )
    assert any("idles 60 seconds (AUTO_SUSPEND)" in n for n in notes)


def test_capabilities_report_auto_suspend_and_warn_when_it_is_off(
    fake_sf_connection,
):
    caps = make_adapter(fake_sf_connection).capabilities()
    assert caps["budget"]["warehouse"]["auto_suspend_seconds"] == 60
    assert caps["budget"]["warehouse"]["state"] == "SUSPENDED"
    assert "warnings" not in caps

    fake_sf_connection.warehouses[0].auto_suspend = None
    caps = make_adapter(fake_sf_connection).capabilities()
    assert caps["budget"]["warehouse"]["auto_suspend_seconds"] is None
    assert any("AUTO_SUSPEND is off on DEX_WH" in w for w in caps["warnings"])


# --- issue #664: suspend after the command, cycle-aware accounting ------------------


def _ledgered(connection, entries: list[dict], *, suspend: bool = False, **kwargs):
    """One command's adapter on a ledger every command in the test shares, the
    way consecutive dex commands share `.dex/spend.jsonl`."""

    return make_adapter(
        connection,
        record=entries.append,
        history=lambda: list(entries),
        target=SnowflakeTarget(warehouse="DEX_WH", suspend_after_command=suspend),
        **kwargs,
    )


def _claim(entries: list[dict]) -> dict:
    """The latest claim on a resume cycle in the ledger."""

    return [e for e in entries if e.get("warehouse_claim")][-1]["warehouse_claim"]


def _suspends(connection) -> list[str]:
    return [
        s.sql
        for s in connection.statements
        if s.sql.strip().upper().startswith("ALTER WAREHOUSE")
    ]


@pytest.fixture
def tail_600(fake_sf_connection):
    """Snowflake's default AUTO_SUSPEND, where a tail is ten resume minimums,
    and one-second statements."""

    fake_sf_connection.warehouses[0].auto_suspend = 600
    fake_sf_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0
    )
    return fake_sf_connection


def test_a_command_that_woke_the_warehouse_suspends_it_and_pays_no_tail(tail_600):
    entries: list[dict] = []
    adapter = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    _one_query(adapter)
    billed, notes = _settled(adapter)

    # One second of work, floored at the 60-second resume minimum, no tail.
    assert billed == pytest.approx(_RESUME_MINIMUM_SECONDS)
    assert tail_600.warehouses[0].state == "SUSPENDED"
    assert _suspends(tail_600) == ['ALTER WAREHOUSE "DEX_WH" SUSPEND']
    assert "suspend_after_command" in notes[0]
    assert "no idle tail is charged" in notes[0]
    claim = _claim(entries)
    assert claim["warehouse"] == "DEX_WH"
    assert claim["suspend"] == "suspended"
    assert claim["auto_suspend"] == 600


def test_suspend_is_off_by_default_and_never_issued(tail_600):
    entries: list[dict] = []
    adapter = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(adapter)
    billed, _notes = _settled(adapter)
    assert billed == pytest.approx(601.0)
    assert _suspends(tail_600) == []
    assert tail_600.warehouses[0].state == "STARTED"
    assert _claim(entries)["suspend"] is None


def test_a_warehouse_that_was_already_running_is_never_suspended(tail_600):
    tail_600.warehouses[0].state = "STARTED"
    adapter = _ledgered(tail_600, [], suspend=True, ceiling=1200.0)
    _one_query(adapter)
    billed, _notes = _settled(adapter)
    assert _suspends(tail_600) == []
    assert billed == pytest.approx(601.0)


def test_a_warehouse_other_queries_are_using_is_never_suspended(tail_600):
    adapter = _ledgered(tail_600, [], suspend=True, ceiling=1200.0)
    _one_query(adapter)
    tail_600.warehouses[0].running = 1
    billed, notes = _settled(adapter)
    assert _suspends(tail_600) == []
    assert tail_600.warehouses[0].state == "STARTED"
    assert billed == pytest.approx(_RESUME_MINIMUM_SECONDS)
    assert "Other queries were running" in notes[0]


def test_a_resume_that_was_not_this_commands_is_never_suspended(tail_600):
    """The warehouse suspended and another session woke it again while this
    command ran: ``resumed_on`` moved, but not during this command's first
    statement, so the warehouse is theirs."""

    from fakes.snowflake import _iso

    adapter = _ledgered(tail_600, [], suspend=True, ceiling=1200.0)
    _one_query(adapter)
    tail_600.warehouses[0].resumed_on = _iso(tail_600.clock.wall() + 3600)
    _settled(adapter)
    assert _suspends(tail_600) == []


def test_without_operate_the_suspend_is_skipped_and_the_tail_is_charged(tail_600):
    tail_600.warehouses[0].operate_granted = False
    entries: list[dict] = []
    adapter = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    _one_query(adapter)
    billed, notes = _settled(adapter)
    assert billed == pytest.approx(601.0)
    assert tail_600.warehouses[0].state == "STARTED"
    assert any("GRANT OPERATE ON WAREHOUSE DEX_WH" in n for n in notes)
    assert _claim(entries)["suspend"] == "refused_grant"


def test_the_quote_counts_on_a_suspend_only_once_one_has_worked(tail_600):
    """The first command cannot know the grant holds, so it quotes the tail
    and settles what the suspend did; the next one quotes the resume minimum."""

    entries: list[dict] = []
    sql = 'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    first = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    work = first.query_estimate(sql)
    assert with_uptime(first, work, None)[0] == pytest.approx(work + 600)
    _one_query(first)
    _settled(first)

    second = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    shown = with_uptime(second, work, None)[0]
    assert shown == pytest.approx(work + _RESUME_MINIMUM_SECONDS)
    notes = second.describe_estimate(shown)["notes"]
    assert any("no idle tail is quoted" in n for n in notes)

    # A refusal recorded since puts the tail back in the quote.
    tail_600.warehouses[0].operate_granted = False
    _one_query(second)
    _settled(second)
    # Its tail runs out and the warehouse suspends on its own.
    tail_600.clock.now += 700.0
    tail_600.warehouses[0].state = "SUSPENDED"
    third = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    assert with_uptime(third, work, None)[0] == pytest.approx(work + 600)


def test_a_command_inside_the_tail_a_previous_one_paid_pays_the_gap_and_its_span(
    tail_600,
):
    """Two commands thirty seconds apart: the first paid a 600-second tail the
    warehouse never idled through, so the second owes only what it adds to
    that claim, and the pair settles one tail between them."""

    entries: list[dict] = []
    first = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(first)  # t = 0 .. 1
    assert _settled(first)[0] == pytest.approx(601.0)

    tail_600.clock.now += 30.0
    second = _ledgered(tail_600, entries, ceiling=1200.0)
    sql = 'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    work = second.query_estimate(sql)
    assert with_uptime(second, work, None)[0] == pytest.approx(work + 30.0)
    notes = second.describe_estimate(work + 30.0)["notes"]
    assert any("a previous dex command already paid for" in n for n in notes)
    _one_query(second)  # t = 31 .. 32
    billed, settle_notes = _settled(second)
    assert billed == pytest.approx(31.0)  # the 30-second gap plus its own second
    assert "same resume cycle already paid" in settle_notes[0]
    # Uptime 0 .. 632 is exactly what the two commands settled together.
    assert 601.0 + billed == pytest.approx(32.0 + 600.0)


def test_a_command_after_the_claimed_tail_ran_out_pays_its_own_tail(tail_600):
    """Other users kept the warehouse up past the tail dex paid for: dex claims
    its own statement time plus one tail, never their uptime."""

    entries: list[dict] = []
    first = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(first)
    _settled(first)
    tail_600.clock.now += 900.0
    second = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(second)
    assert _settled(second)[0] == pytest.approx(601.0)


def test_a_new_resume_cycle_ignores_the_previous_cycles_claim(tail_600):
    entries: list[dict] = []
    first = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(first)
    _settled(first)
    tail_600.warehouses[0].state = "SUSPENDED"
    tail_600.clock.now += 30.0
    second = _ledgered(tail_600, entries, ceiling=1200.0)
    _one_query(second)
    assert _settled(second)[0] == pytest.approx(601.0)


def test_capabilities_recommend_a_short_auto_suspend_without_changing_it(tail_600):
    caps = make_adapter(tail_600).capabilities()
    assert caps["budget"]["warehouse"]["suspend_after_command"] == {
        "enabled": False,
        "last_outcome": None,
    }
    (warning,) = caps["warnings"]
    assert "idles 600 seconds" in warning
    assert "AUTO_SUSPEND = 60" in warning
    assert "`dex config set snowflake.suspend_after_command true`" in warning
    assert "`GRANT OPERATE ON WAREHOUSE DEX_WH TO ROLE <dex role>`" in warning
    assert "never changes AUTO_SUSPEND" in warning
    assert not any("OPERATE" in g for g in caps["required_grants"])
    assert _suspends(tail_600) == []


@pytest.mark.parametrize(
    ("role", "spelled"),
    [("DEX_READER", "DEX_READER"), ("analytics team", '"analytics team"')],
)
def test_the_tail_advice_names_the_session_role_in_the_grant(tail_600, role, spelled):
    """The login response carries the role, so the grant to copy is complete."""

    tail_600.role = role
    (warning,) = make_adapter(tail_600).capabilities()["warnings"]
    assert f"`GRANT OPERATE ON WAREHOUSE DEX_WH TO ROLE {spelled}`" in warning


def test_capabilities_report_the_last_suspend_and_name_a_missing_grant(tail_600):
    entries: list[dict] = []
    tail_600.warehouses[0].operate_granted = False
    adapter = _ledgered(tail_600, entries, suspend=True, ceiling=1200.0)
    _one_query(adapter)
    _settled(adapter)

    caps = _ledgered(tail_600, entries, suspend=True).capabilities()
    assert caps["budget"]["warehouse"]["suspend_after_command"] == {
        "enabled": True,
        "last_outcome": "refused_grant",
    }
    assert any("OPERATE" in g for g in caps["required_grants"])
    assert any("GRANT OPERATE ON WAREHOUSE DEX_WH" in w for w in caps["warnings"])


def test_the_estimate_names_the_fix_when_the_tail_dominates(tail_600):
    adapter = make_adapter(tail_600, ceiling=1200.0)
    work = adapter.query_estimate(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    )
    shown, lines, _ = with_uptime(adapter, work, {"probe": work})
    notes = adapter.describe_estimate(shown, lines)["notes"]
    advice = next(n for n in notes if "own work is about" in n)
    assert "600-second AUTO_SUSPEND" in advice
    assert "AUTO_SUSPEND = 60" in advice
    assert "never changes AUTO_SUSPEND" in advice


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


def test_distinct_combination_counts_narrow_to_what_the_budget_covers(
    fake_sf_connection,
):
    """Every combination is another scan, so a budget can cover some of the
    probe and not all of it. The pairs arrive best-ranked first, so spending on
    the affordable prefix keeps the grain reachable where refusing the whole
    probe would have thrown it away; the note names what went unasked."""

    fake_sf_connection.row_resolver = lambda sql: [{"d_0": 97, "d_1": 100}]
    adapter = make_adapter(fake_sf_connection, ceiling=100.0)
    meta, _ = adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")
    unit = adapter._scan_seconds(meta.byte_size)
    # Room for two of the four scans asked for.
    adapter.cost_gate.charge(100.0 - 2.5 * unit)

    pairs = [["ID", "EMAIL"], ["EMAIL", "ID"], ["ID", "PLAN"], ["EMAIL", "PLAN"]]
    counts = adapter.distinct_combination_counts("SHOP.PUBLIC.CUSTOMERS", pairs)

    assert counts == {("ID", "EMAIL"): 97, ("EMAIL", "ID"): 100}
    assert any(
        "composite-key probe narrowed to 2 of 4 candidate pairs" in note
        for note in adapter.table_notes("SHOP.PUBLIC.CUSTOMERS")
    )
    stmts = data_statements(fake_sf_connection)
    assert len(stmts) == 1
    assert stmts[0].sql.count("SELECT DISTINCT") == 2


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


def test_value_domain_counts_batch_into_one_guarded_statement(fake_sf_connection):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_sf_connection.row_resolver = lambda sql: [
        {
            "d_0": [{"v": "prod", "c": 60}, {"v": "dev", "c": 40}],
            "n_0": 2,
            "d_1": [{"v": "x", "c": 100}],
            "n_1": 1,
        }
    ]
    adapter = make_adapter(fake_sf_connection, ceiling=100_000.0)
    result = adapter.value_domain_counts(
        "SHOP.PUBLIC.CUSTOMERS", ["ENV_TIER", "FLAG"], limit=25
    )
    assert result["ENV_TIER"].values == [("prod", 60), ("dev", 40)]
    assert result["ENV_TIER"].total_distinct == 2
    assert result["FLAG"].values == [("x", 100)]
    stmts = data_statements(fake_sf_connection)
    assert len(stmts) == 1
    assert "ARRAY_AGG" in stmts[0].sql
    assert assert_select_only(stmts[0].sql, dialect="snowflake") == stmts[0].sql
    assert adapter.value_domain_counts("SHOP.PUBLIC.CUSTOMERS", [], limit=25) == {}


def test_value_domain_counts_degrade_when_budget_cannot_cover(fake_sf_connection):
    adapter = make_adapter(fake_sf_connection, ceiling=100.0)
    adapter.cost_gate.charge(99.5)
    result = adapter.value_domain_counts(
        "SHOP.PUBLIC.CUSTOMERS", ["ENV_TIER"], limit=25
    )
    assert result == {}
    assert any(
        "value-domain probe skipped" in note
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


@pytest.mark.skipif(os.name != "posix", reason="mode bits are synthetic on Windows")
@pytest.mark.parametrize("source", ["named_connection", "environment"])
def test_a_key_file_other_accounts_can_read_is_refused_with_the_fix(
    tmp_path: Path, monkeypatch, source: str
):
    """A group- or world-readable private key is refused, the way ssh refuses one.

    Using it silently lends dex's authority to a key anyone on the machine can
    already copy. The message has to carry the path and the `chmod` that fixes
    it, because the person reading it is the one who can run that command. The
    same file at 0600 passes, so the check is the mode and nothing else.
    """

    from exmergo_dex_core import connect as connect_mod
    from exmergo_dex_core.connect import (
        CredentialDiscoveryError,
        resolve_snowflake_connection,
    )

    key = tmp_path / "rsa_key.p8"
    key.write_text("not a real key", encoding="utf-8")
    key.chmod(0o644)
    if source == "named_connection":
        connections = {
            "dex-ci": {"account": "A", "user": "U", "private_key_file": str(key)}
        }
        target, env = SnowflakeTarget(connection_name="dex-ci"), {}
    else:
        connections = {}
        target = SnowflakeTarget()
        env = {
            "SNOWFLAKE_ACCOUNT": "A",
            "SNOWFLAKE_USER": "U",
            "SNOWFLAKE_PRIVATE_KEY_FILE": str(key),
        }
    monkeypatch.setattr(connect_mod, "_snowflake_connections", lambda env: connections)

    with pytest.raises(CredentialDiscoveryError) as exc_info:
        resolve_snowflake_connection(target, env, tmp_path)
    message = str(exc_info.value)
    assert str(key) in message
    assert "0644" in message
    assert f"chmod 600 {key}" in message

    key.chmod(0o600)
    params, method = resolve_snowflake_connection(target, env, tmp_path)
    assert params["private_key_file"] == str(key)
    assert method.endswith(":key_pair")


def test_a_key_path_that_does_not_resolve_is_left_to_the_connector(
    tmp_path: Path, monkeypatch
):
    """Only an existing file is checked. A path that does not resolve (a typo, an
    unrendered dbt `env_var()`) is not a permissions problem, and the connector's
    own error names it better than a mode check could."""

    from exmergo_dex_core import connect as connect_mod
    from exmergo_dex_core.connect import resolve_snowflake_connection

    connections = {
        "dex-ci": {
            "account": "A",
            "user": "U",
            "private_key_file": str(tmp_path / "absent.p8"),
        }
    }
    monkeypatch.setattr(connect_mod, "_snowflake_connections", lambda env: connections)
    params, _method = resolve_snowflake_connection(
        SnowflakeTarget(connection_name="dex-ci"), {}, tmp_path
    )
    assert params["private_key_file"] == str(tmp_path / "absent.p8")


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


def test_list_namespace_objects_lists_tables_and_views(multi_db_connection):
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
    listed = adapter.list_namespace_objects("raw", "staging_dev")
    assert listed == ["V_LEFTOVER"]
    assert data_statements(multi_db_connection) == []


def test_list_namespace_objects_reads_an_absent_schema_as_empty(multi_db_connection):
    adapter = make_adapter(multi_db_connection)
    assert adapter.list_namespace_objects("RAW", "NOT_THERE") == []
    assert adapter.list_namespace_objects("NO_SUCH_DB", "STAGING_DEV") == []
    assert data_statements(multi_db_connection) == []


def test_the_aggregate_supplies_the_count_show_tables_does_not_maintain(
    fake_sf_connection,
):
    """The same gap as issue #375's, on the sibling connector nobody reported.

    SHOW TABLES maintains no row count for a view, and the aggregate's own
    COUNT(*) was read and discarded, so a view's count never arrived and the
    exact-distinct escalation, the composite-key probe and the grain verdict
    could never run against one. It now supersedes the SHOW figure the way it
    already does on Postgres and Redshift.
    """

    from fakes.snowflake import FakeSnowflakeTable

    fake_sf_connection.tables.append(
        FakeSnowflakeTable(
            database="SHOP",
            schema="PUBLIC",
            name="V_CUSTOMERS",
            columns=[("ID", "FIXED", False)],
            kind="view",
        )
    )
    fake_sf_connection.row_resolver = lambda sql: [
        {"N_TOTAL": 42, "NN_0": 42, "ND_0": 42}
    ]
    adapter = make_adapter(fake_sf_connection)
    meta, columns = adapter.table_metadata("SHOP.PUBLIC.V_CUSTOMERS")
    assert meta.row_count is None
    adapter.column_aggregates("SHOP.PUBLIC.V_CUSTOMERS", columns)
    refreshed, _ = adapter.table_metadata("SHOP.PUBLIC.V_CUSTOMERS")
    assert refreshed.row_count == 42


# --- a warehouse that changes mid-command, and the dev target ----------------------
#
# A build is the one command where the warehouse changes under the adapter: dbt
# writes between the pricing pass and the verification that judges its output.
# The dev namespace dbt writes into is read for that command only, and may not
# exist until dbt creates it.


def _shows(connection, prefix: str) -> list[str]:
    return [s.sql for s in connection.statements if s.sql.upper().startswith(prefix)]


def _dev_table(schema: str = "DBT_DEV", name: str = "DIM_CUSTOMERS", rows: int = 4):
    from fakes.snowflake import FakeSnowflakeTable

    return FakeSnowflakeTable(
        database="RAW",
        schema=schema,
        name=name,
        columns=[("ID", "FIXED", False)],
        rows=rows,
        bytes=1_000,
    )


def test_forgetting_metadata_lists_what_was_written_since(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
    )
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"]

    multi_db_connection.tables.append(_dev_table(schema="PUBLIC", name="ORDERS"))
    multi_db_connection.tables[4].rows = 99
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"], "cached"

    adapter.forget_metadata()
    listed = {o.identifier: o.row_count for o in adapter.list_objects()}
    assert listed == {"RAW.PUBLIC.EVENTS": 99, "RAW.PUBLIC.ORDERS": 4}
    # dbt creates schemas, never a database, so the database list is kept.
    assert len(_shows(multi_db_connection, "SHOW DATABASES")) == 1
    assert data_statements(multi_db_connection) == []


def test_forgetting_metadata_keeps_the_session_and_the_uptime_quote(
    fake_sf_connection,
):
    """What a forget keeps is what the command has paid for: re-preparing the
    session or re-quoting the uptime would bill a second time for a warehouse
    the build already woke."""

    fake_sf_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_sf_connection)
    sql = 'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"'
    first = with_uptime(adapter, adapter.query_estimate(sql), None)[0]
    adapter.run_query(sql, max_rows=10, timeout_seconds=200)
    warm = with_uptime(adapter, adapter.query_estimate(sql), None)[0]
    assert first == pytest.approx(warm + 60)

    adapter.forget_metadata()
    assert with_uptime(adapter, adapter.query_estimate(sql), None)[0] == pytest.approx(
        warm
    )
    adapter.run_query(sql, max_rows=10, timeout_seconds=200)
    assert len([u for u in fake_sf_connection.used if "USE WAREHOUSE" in u]) == 1


def test_a_dev_target_that_does_not_exist_yet_reads_as_empty(multi_db_connection):
    """A first build: dbt creates the dev schema, so before it runs there is
    nothing there to read and nothing wrong. Refusing here priced every node
    of the build at nothing, and judged none of what it wrote."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
    )
    adapter.read_dev_target("RAW.DBT_DEV")
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"]

    multi_db_connection.tables.append(_dev_table())
    adapter.forget_metadata()
    assert _scoped_identifiers(adapter) == [
        "RAW.DBT_DEV.DIM_CUSTOMERS",
        "RAW.PUBLIC.EVENTS",
    ]
    assert data_statements(multi_db_connection) == []


def test_a_dev_target_in_a_database_that_does_not_exist_reads_as_empty(
    multi_db_connection,
):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
    )
    adapter.read_dev_target("NO_SUCH_DB.DBT_DEV")
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"]


def test_an_existing_empty_dev_target_is_read(multi_db_connection):
    multi_db_connection.empty_schemas.add("RAW.DBT_DEV")
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
    )
    adapter.read_dev_target("raw.dbt_dev")
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"]
    assert 'SHOW TABLES IN SCHEMA "RAW"."DBT_DEV"' in _shows(
        multi_db_connection, "SHOW TABLES"
    )


def test_a_dev_target_inside_a_committed_database_is_read_once(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW"]),
    )
    adapter.read_dev_target("RAW.STAGING")
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS", "RAW.STAGING.SEEDS"]
    assert not any("IN SCHEMA" in s for s in _shows(multi_db_connection, "SHOW"))


def test_an_empty_allowlist_reads_the_dev_target_once(multi_db_connection):
    adapter = make_adapter(multi_db_connection)
    adapter.read_dev_target("RAW.STAGING")
    identifiers = _scoped_identifiers(adapter)
    assert identifiers.count("RAW.STAGING.SEEDS") == 1
    assert not any("IN SCHEMA" in s for s in _shows(multi_db_connection, "SHOW"))


def test_a_scope_flag_cannot_name_the_dev_target(multi_db_connection):
    """`--scope` narrows the committed allowlist and nothing else, so it can no
    more reach the dev namespace than any other one outside the allowlist."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
        scope_override=["RAW.STAGING"],
    )
    adapter.read_dev_target("RAW.STAGING")
    with pytest.raises(SnowflakeConnectionError, match="never widens"):
        adapter.list_objects()


def test_a_scope_flag_keeps_the_dev_target(multi_db_connection):
    """A verified build narrowed to some sources still judges what it wrote."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE"]),
        scope_override=["SAMPLE.TPCH_SF1"],
    )
    adapter.read_dev_target("RAW.STAGING")
    assert _scoped_identifiers(adapter) == [
        "RAW.STAGING.SEEDS",
        "SAMPLE.TPCH_SF1.CUSTOMER",
        "SAMPLE.TPCH_SF1.ORDERS",
    ]


def test_a_committed_entry_that_names_nothing_is_still_refused(multi_db_connection):
    """The dev target is the one lenient entry; the allowlist beside it is not."""

    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["SAMPLE.__GONE__"]),
    )
    adapter.read_dev_target("RAW.DBT_DEV")
    with pytest.raises(SnowflakeConnectionError) as exc:
        adapter.list_objects()
    assert "[from snowflake.databases in .dex/config.yml]" in str(exc.value)


def test_changing_the_dev_target_forgets_what_was_listed(multi_db_connection):
    adapter = make_adapter(
        multi_db_connection,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=["RAW.PUBLIC"]),
    )
    adapter.read_dev_target("RAW.STAGING")
    assert "RAW.STAGING.SEEDS" in _scoped_identifiers(adapter)
    adapter.read_dev_target(None)
    assert _scoped_identifiers(adapter) == ["RAW.PUBLIC.EVENTS"]


def test_a_host_cap_binds_over_a_higher_committed_session_ceiling(fake_sf_connection):
    """#787: the cap is folded in where every gate is built, so a Snowflake
    command whose file allows the day far more still meets the host's bound."""

    from exmergo_dex_core.config import Budget, DexConfig
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.session_cap import SessionCeilingCap
    from exmergo_dex_core.storage import MemoryStore

    config = DexConfig(
        connector="snowflake",
        budget=Budget(ceiling=600.0, session_ceiling=1_000_000.0),
    ).with_session_ceiling_cap(SessionCeilingCap(0.5, "environment"))
    adapter = make_adapter(fake_sf_connection)
    adapter.cost_gate = new_cost_gate(
        "snowflake", config, MemoryStore(), confirmed=True, command="explore query"
    )
    with pytest.raises(OverCeilingError, match=r"DEX_SESSION_CEILING_CAP=0\.5"):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert data_statements(fake_sf_connection) == []


def test_a_utc_session_is_pinned_only_when_asked(fake_sf_connection):
    fake_sf_connection.row_resolver = lambda sql: FakeResult(rows=[{"n": 1}])
    adapter = make_adapter(fake_sf_connection)
    _one_query(adapter)
    assert "TIMEZONE" not in fake_sf_connection.session_parameters

    adapter.pin_utc_session()
    _one_query(adapter)
    assert fake_sf_connection.session_parameters["TIMEZONE"] == "UTC"
