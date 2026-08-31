"""The ClickHouse connector: metadata is free (system-table lookups, no scans),
every billed statement is estimated from a free non-executing EXPLAIN ESTIMATE
and gated in database-seconds, and the budget binds at both the client (charge)
and the simulated server (max_execution_time and max_bytes_to_read).

Everything here runs against the stateful fake (tests/fakes/clickhouse.py):
deterministic, offline, free.
"""

from __future__ import annotations

import pytest

pytest.importorskip("clickhouse_connect")

from fakes.clickhouse import (
    ACCESS_DENIED,
    TIMEOUT_EXCEEDED,
    TOO_MANY_BYTES,
    UNKNOWN_TABLE,
    FakeClickHouseTable,
    FakeGrant,
    FakeResult,
    server_error,
)

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.base import ColumnMeta
from exmergo_dex_core.adapters.clickhouse import (
    ClickHouseAdapter,
    ClickHouseConnectionError,
    is_nullable_type,
    unwrap_type,
)
from exmergo_dex_core.config import ClickHouseTarget, DexConfig
from exmergo_dex_core.connect import new_cost_gate, paradigm_for
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
    utc_day_start,
)
from exmergo_dex_core.storage import MemoryStore


def _gate(**kwargs) -> CostGate:
    defaults = {
        "paradigm": Paradigm.DB_LOAD,
        "connector": "clickhouse",
        "command": "explore profile",
        "ceiling": 600.0,
        "confirmed": True,
        "session_ceiling": None,
        "session_spent": 0.0,
    }
    return CostGate(**{**defaults, **kwargs})


def make_adapter(
    connection,
    *,
    ceiling: float | None = 600.0,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: ClickHouseTarget | None = None,
    scope_origin: str | None = None,
) -> ClickHouseAdapter:
    target = target or ClickHouseTarget()
    gate = _gate(
        paradigm=(
            Paradigm.COMPUTE_TIME if target.deployment == "cloud" else Paradigm.DB_LOAD
        ),
        ceiling=ceiling,
        confirmed=confirmed,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        record=record,
    )
    return ClickHouseAdapter(
        connection=connection,
        cost_gate=gate,
        target=target,
        auth_method="environment:password",
        scope_origin=scope_origin,
    )


# --- the type unwrapper (nullability is a type, not a flag) --------------------


@pytest.mark.parametrize(
    ("declared", "inner", "nullable"),
    [
        ("String", "String", False),
        ("Nullable(String)", "String", True),
        ("LowCardinality(String)", "String", False),
        ("LowCardinality(Nullable(String))", "String", True),
        ("Nullable(LowCardinality(String))", "String", True),
        ("Array(String)", "Array(String)", False),
        ("Nullable(Decimal(10, 2))", "Decimal(10, 2)", True),
        ("DateTime64(3)", "DateTime64(3)", False),
    ],
)
def test_type_wrappers_are_peeled_in_either_nesting_order(declared, inner, nullable):
    """ClickHouse encodes nullability in the type, so system.columns has no
    is_nullable and every shared type predicate would otherwise be reading a
    constructor name."""

    assert unwrap_type(declared) == inner
    assert is_nullable_type(declared) is nullable


def test_a_low_cardinality_nullable_column_reports_as_nullable(
    fake_clickhouse_connection,
):
    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    city = next(c for c in columns if c.name == "city")
    assert city.data_type == "LowCardinality(Nullable(String))"
    assert city.nullable is True
    # And the raw connector spelling is preserved, not normalized away.
    assert next(c for c in columns if c.name == "id").nullable is False


# --- metadata (free) -----------------------------------------------------------


def test_capabilities_shape_and_free(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    caps = adapter.capabilities()
    assert caps["connector"] == "clickhouse"
    assert caps["dialect"] == "clickhouse"
    assert caps["read_only"] is True
    assert caps["session_read_only"] is True
    assert caps["paradigm"] == "db_load"
    assert caps["deployment"] == "self_hosted"
    assert caps["auth_method"] == "environment:password"
    assert fake_clickhouse_connection.data_queries == []


def test_session_read_only_is_reported_not_assumed(fake_clickhouse_connection):
    """The server's own `readonly` setting is what gets reported, so a session
    that is not actually read-only says so rather than being claimed."""

    fake_clickhouse_connection.readonly = "0"
    adapter = make_adapter(fake_clickhouse_connection)
    caps = adapter.capabilities()
    assert caps["read_only"] is True  # dex's own guards still hold
    assert caps["session_read_only"] is False


def test_inventory_is_two_part_and_free(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "shop.customers",
        "shop.events",
        "shop.order_events_raw",
    ]
    for obj in objects:
        assert obj.identifier.count(".") == 1, "ClickHouse identifiers are two-part"
    assert fake_clickhouse_connection.data_queries == []


def test_a_view_reports_no_row_count_rather_than_zero(fake_clickhouse_connection):
    """system.tables answers NULL where it cannot quickly determine a count, and
    ObjectMeta's optional row_count means exactly that. Reading it as 0 would
    make every view look like an emptied table to volume drift."""

    fake_clickhouse_connection.tables.append(
        FakeClickHouseTable(
            database="shop",
            name="v_totals",
            columns=[("order_id", "UInt64", False)],
            total_rows=None,
            total_bytes=None,
            engine="View",
        )
    )
    adapter = make_adapter(fake_clickhouse_connection)
    view = next(o for o in adapter.list_objects() if o.name == "v_totals")
    assert view.object_type == "view"
    assert view.row_count is None
    assert view.byte_size is None


def test_a_collapsing_engine_is_noted_so_duplicates_do_not_read_as_a_defect(
    fake_clickhouse_connection,
):
    """A ReplacingMergeTree keeps every version until a merge collapses them, so
    a duplicate count describes the stored parts rather than the modeled grain.
    Without the note a key_lost_uniqueness finding here is a permanent false
    alarm."""

    adapter = make_adapter(fake_clickhouse_connection)
    notes = adapter.table_notes("shop.order_events_raw")
    assert any("ReplacingMergeTree" in n for n in notes)
    assert any("FINAL" in n for n in notes)
    assert adapter.table_notes("shop.customers") == []


# --- estimation (free) ---------------------------------------------------------


def test_query_estimate_uses_the_free_non_executing_explain(
    fake_clickhouse_connection,
):
    """EXPLAIN ESTIMATE prices the statement after primary-key pruning and reads
    no data, so a filtered probe on a huge table is not quoted as a full scan."""

    adapter = make_adapter(fake_clickhouse_connection)
    fake_clickhouse_connection.estimate_rows = {"shop.events": 10}
    estimate = adapter.query_estimate("SELECT count() FROM shop.events")
    # 10 rows of a 50 GB / 1000 row table is nowhere near the whole relation.
    assert estimate < adapter._scan_seconds(50_000_000_000)
    assert fake_clickhouse_connection.data_queries == []


def test_estimate_basis_distinguishes_a_pruned_plan_from_a_whole_relation(
    fake_clickhouse_connection,
):
    """estimate_quality stays "heuristic" because seconds come from a throughput
    constant either way; what is exact rides one field down so a caller can tell
    the two apart without dex overclaiming the quote."""

    adapter = make_adapter(fake_clickhouse_connection)
    fake_clickhouse_connection.estimate_rows = {"shop.events": 10}
    _seconds, basis = adapter._statement_estimate("SELECT count() FROM shop.events")
    assert basis == "explain_estimate"

    fake_clickhouse_connection.estimate_rows = {}
    _seconds, basis = adapter._statement_estimate("SELECT count() FROM shop.events")
    assert basis == "system_tables"


def test_describe_estimate_is_honest_about_quality_and_names_the_caps(
    fake_clickhouse_connection,
):
    adapter = make_adapter(fake_clickhouse_connection)
    payload = adapter.describe_estimate(12.5, {"shop.events": 12.5})
    assert payload["estimated_seconds"] == 12.5
    assert payload["estimate_quality"] == "heuristic"
    assert payload["per_table_seconds"] == {"shop.events": 12.5}
    assert "max_execution_time" in payload["hint"]
    assert "max_bytes_to_read" in payload["hint"]
    assert "bills no dollars" in payload["notes"][0]


def test_spend_display_claims_no_currency(fake_clickhouse_connection):
    """Self-hosted ClickHouse bills nothing in dollars, so there is nothing to
    translate; the seconds in the spend summary are the whole story."""

    assert make_adapter(fake_clickhouse_connection).spend_display() == {}


def test_profile_estimate_accepts_include_blobs_without_crashing(
    fake_clickhouse_connection,
):
    """explore/commands.py always calls profile_estimate with include_blobs, so
    every adapter that has one must accept that kwarg."""

    adapter = make_adapter(fake_clickhouse_connection)
    total, per_table = adapter.profile_estimate(
        ["shop.customers"], include_blobs={"shop.customers.email"}
    )
    assert total > 0
    assert set(per_table) == {"shop.customers"}


# --- the cost gate binds -------------------------------------------------------


def test_unconfirmed_scan_never_executes(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection, confirmed=False)
    _meta, columns = adapter.table_metadata("shop.customers")
    with pytest.raises(ConfirmationRequiredError):
        adapter.column_aggregates("shop.customers", columns)
    assert fake_clickhouse_connection.data_queries == []


def test_confirmed_run_without_a_ceiling_is_refused(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection, ceiling=None)
    _meta, columns = adapter.table_metadata("shop.customers")
    with pytest.raises(Exception) as exc:
        adapter.column_aggregates("shop.customers", columns)
    assert "ceiling" in str(exc.value).lower() or "budget" in str(exc.value).lower()
    assert fake_clickhouse_connection.data_queries == []


def test_over_ceiling_refuses_client_side_without_executing(
    fake_clickhouse_connection,
):
    adapter = make_adapter(fake_clickhouse_connection, ceiling=0.001)
    _meta, columns = adapter.table_metadata("shop.events")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("shop.events", columns)
    assert fake_clickhouse_connection.data_queries == []


def test_every_billed_statement_carries_both_server_side_caps(
    fake_clickhouse_connection,
):
    """Time alone is not enough: max_execution_time is checked at block
    boundaries, so a single fast block can overshoot it. The byte cap, derived
    from the same throughput constant as the estimate, is what binds on a scan.
    """

    adapter = make_adapter(fake_clickhouse_connection, ceiling=600.0)
    _meta, columns = adapter.table_metadata("shop.customers")
    adapter.column_aggregates("shop.customers", columns)

    billed = fake_clickhouse_connection.data_queries
    assert billed, "the profiling scan should have issued at least one statement"
    for query in billed:
        assert query.settings["max_execution_time"] > 0
        assert query.settings["timeout_overflow_mode"] == "throw"
        assert query.settings["max_bytes_to_read"] > 0
        assert query.settings["read_overflow_mode"] == "throw"


def test_read_only_settings_ride_every_statement_free_or_billed(
    fake_clickhouse_connection,
):
    """readonly=2 and allow_ddl=0 are sent per statement rather than set once on
    the client, so a host that reuses a client it built cannot lose them.

    join_use_nulls=1 is here for a different and sharper reason: ClickHouse
    defaults it to 0, which fills an unmatched LEFT JOIN row with the column
    type's default instead of NULL, and the shared relationship overlap probe
    counts orphans with IS NULL. With the default, every inferred join reports
    perfectly clean and maintain grain's join-fanout half never fires.
    """

    adapter = make_adapter(fake_clickhouse_connection)
    adapter.capabilities()
    _meta, columns = adapter.table_metadata("shop.customers")
    adapter.column_aggregates("shop.customers", columns)

    assert fake_clickhouse_connection.queries
    for query in fake_clickhouse_connection.queries:
        assert query.settings["readonly"] == 2
        assert query.settings["allow_ddl"] == 0
        assert query.settings["join_use_nulls"] == 1


def test_a_sub_second_remainder_is_refused_rather_than_sent_as_zero(
    fake_clickhouse_connection,
):
    """max_execution_time = 0 means *no limit* in ClickHouse, and int() of a
    sub-second remainder is 0, so the truncation would silently remove the cap
    at exactly the moment the budget is nearly spent."""

    adapter = make_adapter(fake_clickhouse_connection, ceiling=600.0)
    # Settled spend, not a booking: the statement cap measures the ceiling
    # against what has actually been billed, which is what the server cap is
    # derived from.
    adapter.cost_gate.record_billed(599.5, job_id=None, statement="prior")
    with pytest.raises(OverCeilingError, match="under one database-second"):
        adapter.run_query("SELECT 1", max_rows=10, timeout_seconds=5)


def test_server_timeout_translates_to_over_ceiling(fake_clickhouse_connection):
    """The layer that binds when the estimate is wrong.

    The estimate is deliberately made to fit (a pruned plan reading almost
    nothing), so the client-side charge passes and the statement actually
    reaches the server, which is the only way to exercise the backstop. A
    statement killed by a cap dex derived from the budget has to read as a
    budget refusal, not as a server fault.
    """

    adapter = make_adapter(fake_clickhouse_connection, ceiling=5.0)
    fake_clickhouse_connection.estimate_rows = {"shop.events": 1}
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=99.0
    )
    with pytest.raises(OverCeilingError, match="max_execution_time"):
        adapter.run_query(
            "SELECT count() FROM shop.events", max_rows=10, timeout_seconds=90
        )


def test_a_byte_cap_hit_reads_as_a_budget_refusal(fake_clickhouse_connection):
    """The byte cap is the one that actually binds on a fast scan, because
    max_execution_time is only checked at block boundaries. Same setup as the
    timeout test: the estimate fits, the statement runs, the server stops it."""

    adapter = make_adapter(fake_clickhouse_connection, ceiling=5.0)
    fake_clickhouse_connection.estimate_rows = {"shop.events": 1}
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=0.1, read_bytes=10**15
    )
    with pytest.raises(OverCeilingError, match="max_bytes_to_read"):
        adapter.run_query(
            "SELECT count() FROM shop.events", max_rows=10, timeout_seconds=90
        )


def test_actual_seconds_come_from_the_server_summary_and_land_in_the_ledger(
    fake_clickhouse_connection,
):
    """Settlement is free and server-side: the response's own summary header
    carries elapsed nanoseconds, so no second query and no query_log poll is
    needed, and the recorded seconds are what the server spent rather than what
    the client waited."""

    entries = []
    adapter = make_adapter(fake_clickhouse_connection, record=entries.append)
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.25, read_rows=500, read_bytes=4096
    )
    adapter.run_query(
        "SELECT count() FROM shop.events", max_rows=10, timeout_seconds=90
    )

    assert entries, "the billed statement should have produced a ledger entry"
    entry = entries[-1]
    assert entry["billed_seconds"] == pytest.approx(1.25)
    assert "statement_sha256" in entry
    assert "SELECT" not in str(entry.values())


def test_session_ceiling_binds_across_commands(fake_clickhouse_connection):
    adapter = make_adapter(
        fake_clickhouse_connection,
        ceiling=600.0,
        session_ceiling=10.0,
        session_spent=9.9,
    )
    _meta, columns = adapter.table_metadata("shop.events")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("shop.events", columns)
    assert fake_clickhouse_connection.data_queries == []


# --- profiling -----------------------------------------------------------------


def test_column_aggregates_profile_scalar_columns(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 100, "nn_0": 100, "d_0": 100, "nn_1": 98, "d_1": 98}],
        seconds=0.2,
    )
    aggregates = adapter.column_aggregates("shop.customers", columns[:2])
    by_name = {a.name: a for a in aggregates}
    assert by_name["id"].null_fraction == 0.0
    assert by_name["id"].distinct_count == 100
    # uniq() is a HyperLogLog sketch, never a uniqueness verdict on its own.
    assert by_name["id"].is_unique is None
    assert by_name["id"].distinct_count_exact is False


def test_degraded_types_get_only_a_non_null_count(fake_clickhouse_connection):
    """Array/Map/Tuple columns cannot be ordered or counted distinct
    meaningfully, and the check must see through the Nullable/LowCardinality
    wrappers to notice."""

    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    tags = next(c for c in columns if c.name == "tags")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.customers", [tags], set(), set(), set(), set(), set()
    )
    assert 'COUNT("tags")' in sql
    assert "uniq(" not in sql
    assert "MIN(" not in sql


def test_the_aggregate_batch_is_select_only_and_uses_clickhouse_idioms(
    fake_clickhouse_connection,
):
    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.customers",
        columns,
        {"created_at"},
        {"email"},
        set(),
        set(),
        {"created_at"},
    )
    assert sql.strip().upper().startswith("SELECT")
    # ClickHouse's own spellings, not a transpiled ANSI approximation.
    assert "match(toString(" in sql
    assert "dateTrunc(" in sql
    assert "lagInFrame(toNullable(" in sql


def test_the_lag_idiom_carries_a_null_default_and_the_full_frame(
    fake_clickhouse_connection,
):
    """ClickHouse has no LAG. lagInFrame returns the *type default* rather than
    NULL past the frame edge, so without toNullable(..., NULL) the first row
    compares against the epoch and every temporal column reports a ~20,000 day
    gap; and it respects the window frame, so it needs the explicit full frame
    to behave like standard LAG. Verified live against a column with a known
    3-day hole, which reports 3 with this idiom and 20590 without it.
    """

    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.events")
    occurred = next(c for c in columns if c.name == "occurred_at")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.events", [occurred], set(), set(), set(), set(), {"occurred_at"}
    )
    assert "lagInFrame(toNullable(period), 1, NULL)" in sql
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING" in sql


def test_a_date_time_column_still_gets_hour_granularity(fake_clickhouse_connection):
    """`DateTime` contains the substring DATE and not TIMESTAMP, so the shared
    date-only check would claim it and silently skip the hour grain. That is a
    clean-looking report rather than a missing one."""

    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    created = next(c for c in columns if c.name == "created_at")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.customers", [created], set(), set(), set(), set(), {"created_at"}
    )
    assert "tp_h_0" in sql, "hour-grain continuity should be computed for DateTime"
    assert "tp_d_0" in sql


def test_sampling_is_refused_out_loud_when_the_table_has_no_sampling_key(
    fake_clickhouse_connection,
):
    """ClickHouse can only SAMPLE where the table declared a sampling expression
    in its MergeTree key, which most do not. Honoring the threshold silently
    would produce a full scan the user believed was sampled."""

    target = ClickHouseTarget(max_full_profile_bytes=1000)
    adapter = make_adapter(fake_clickhouse_connection, target=target)
    assert adapter._sample_ratio("shop.events", 50_000_000_000) is None
    notes = adapter.table_notes("shop.events")
    assert any("no sampling key" in n for n in notes)
    assert any("in full" in n for n in notes)


def test_sampling_is_honored_where_a_sampling_key_exists(fake_clickhouse_connection):
    fake_clickhouse_connection.table("shop.events").sampling_key = "cityHash64(id)"
    target = ClickHouseTarget(max_full_profile_bytes=1_000_000_000)
    adapter = make_adapter(fake_clickhouse_connection, target=target)
    ratio = adapter._sample_ratio("shop.events", 50_000_000_000)
    assert ratio is not None and 0 < ratio < 1
    assert any("SAMPLE" in n for n in adapter.table_notes("shop.events"))


def test_a_sampled_profile_reports_no_extremes(fake_clickhouse_connection):
    """A sample cannot judge min/max for the whole table, so they are withheld
    rather than reported from a fraction of the rows."""

    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    plan = [(0, columns[0], True, True, False, False, False, False)]
    values = {"n_total": 100, "nn_0": 100, "d_0": 100, "mn_0": 1, "mx_0": 100}
    [sampled] = ClickHouseAdapter._read_aggregates(values, plan, sampled=True)
    [full] = ClickHouseAdapter._read_aggregates(values, plan, sampled=False)
    assert sampled.min_value is None and sampled.max_value is None
    assert full.min_value == 1 and full.max_value == 100


def test_batched_probes_skip_when_the_budget_cannot_cover_them(
    fake_clickhouse_connection,
):
    """A metered adapter never self-escalates past its ceiling; it degrades and
    says so."""

    adapter = make_adapter(fake_clickhouse_connection, ceiling=600.0)
    adapter.cost_gate.charge(599.0)
    assert adapter.exact_distinct_counts("shop.events", ["id"]) == {}
    assert adapter.distinct_combination_counts("shop.events", [["id", "payload"]]) == {}
    assert adapter.value_domain_counts("shop.events", ["payload"], limit=5) == {}
    notes = adapter.table_notes("shop.events")
    assert any("escalation skipped" in n for n in notes)
    assert fake_clickhouse_connection.data_queries == []


def test_exact_distinct_counts_use_uniq_exact(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 1000, "d_0": 1000}], seconds=0.2
    )
    counts = adapter.exact_distinct_counts("shop.events", ["id"])
    assert counts == {"id": 1000}
    issued = fake_clickhouse_connection.data_queries[-1].sql
    assert "uniqExact(" in issued
    assert "uniq(" not in issued.replace("uniqExact(", "")


# --- run_query -----------------------------------------------------------------


def test_run_query_truncates_and_shapes_columnar(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"city": f"c{i}", "n": i} for i in range(5)], seconds=0.1
    )
    result = adapter.run_query(
        "SELECT city, count() AS n FROM shop.customers GROUP BY city",
        max_rows=3,
        timeout_seconds=30,
    )
    assert result.columns == ["city", "n"]
    assert len(result.cells) == 3
    assert result.truncated is True
    # Columnar by construction: a list of dicts would trip the envelope's
    # raw-row rule.
    assert isinstance(result.cells[0], list)


def test_run_query_rejects_writes(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    for sql in (
        "INSERT INTO shop.customers VALUES (1)",
        "ALTER TABLE shop.customers DROP COLUMN email",
        "DROP TABLE shop.customers",
        "SELECT 1; DROP TABLE shop.customers",
        "OPTIMIZE TABLE shop.customers FINAL",
    ):
        with pytest.raises(Exception):
            adapter.run_query(sql, max_rows=10, timeout_seconds=30)
    assert fake_clickhouse_connection.data_queries == []


# --- error translation ---------------------------------------------------------


def test_server_errors_translate_to_named_refusals(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)

    def raiser(code, message):
        def _resolve(sql):
            raise server_error(code, message)

        return _resolve

    fake_clickhouse_connection.row_resolver = raiser(UNKNOWN_TABLE, "Unknown table")
    with pytest.raises(ClickHouseConnectionError, match="does not exist"):
        adapter.run_query("SELECT 1 FROM shop.nope", max_rows=1, timeout_seconds=30)

    fake_clickhouse_connection.row_resolver = raiser(
        ACCESS_DENIED, "Not enough privileges"
    )
    with pytest.raises(ClickHouseConnectionError, match="grant"):
        adapter.run_query(
            "SELECT 1 FROM shop.customers", max_rows=1, timeout_seconds=30
        )


def test_an_unreachable_server_is_a_connector_error_not_a_policy_refusal(
    fake_clickhouse_connection,
):
    """A network blip and a permanent refusal are the retry-versus-stop
    decision, so they must not share a type."""

    from exmergo_dex_core.errors import ConnectorError

    adapter = make_adapter(fake_clickhouse_connection)
    fake_clickhouse_connection.unreachable = True
    with pytest.raises(ConnectorError):
        adapter.list_objects()


# --- factory and dialect -------------------------------------------------------


def test_get_adapter_constructs_clickhouse(fake_clickhouse_connection):
    adapter = get_adapter(
        "clickhouse",
        connection=fake_clickhouse_connection,
        cost_gate=_gate(),
    )
    assert isinstance(adapter, ClickHouseAdapter)
    assert adapter.paradigm is Paradigm.DB_LOAD


def test_get_dialect_resolves_clickhouse():
    assert get_dialect("clickhouse") == "clickhouse"


def test_a_host_supplied_client_is_not_closed(fake_clickhouse_connection):
    adapter = ClickHouseAdapter(
        connection=fake_clickhouse_connection, cost_gate=_gate(), owns_connection=False
    )
    adapter.close()
    assert fake_clickhouse_connection.closed is False

    adapter = ClickHouseAdapter(
        connection=fake_clickhouse_connection, cost_gate=_gate(), owns_connection=True
    )
    adapter.close()
    assert fake_clickhouse_connection.closed is True


# --- the deployment gate -------------------------------------------------------


def test_clickhouse_paradigm_is_dynamic_only_when_effective_config_is_available():
    self_hosted = DexConfig(
        connector="clickhouse", clickhouse=ClickHouseTarget(deployment="self_hosted")
    )
    cloud = DexConfig(
        connector="clickhouse", clickhouse=ClickHouseTarget(deployment="cloud")
    )
    assert paradigm_for("clickhouse") is Paradigm.DB_LOAD
    assert paradigm_for("clickhouse", self_hosted) is Paradigm.DB_LOAD
    assert paradigm_for("clickhouse", cloud) is Paradigm.COMPUTE_TIME

    store = MemoryStore()
    gate = new_cost_gate("clickhouse", cloud, store, budget=60, confirmed=True)
    assert gate.paradigm is Paradigm.COMPUTE_TIME
    assert gate.cost().paradigm is Paradigm.COMPUTE_TIME
    gate.record_billed(2.5, statement="SELECT 1")
    assert store.spend_since(
        utc_day_start(), field="billed_seconds", connector="clickhouse"
    ) == pytest.approx(2.5)


def test_a_cloud_deployment_is_corroborated_and_reports_live_capacity(
    fake_clickhouse_connection,
):
    fake_clickhouse_connection.cloud_mode = "1"
    fake_clickhouse_connection.capacity_rows = [
        {
            "replica": "r1",
            "memory_bytes": 8 * 1024**3,
            "expected_replicas": 2,
        },
        {
            "replica": "r2",
            "memory_bytes": 16 * 1024**3,
            "expected_replicas": 2,
        },
    ]
    adapter = make_adapter(
        fake_clickhouse_connection,
        ceiling=60.0,
        target=ClickHouseTarget(deployment="cloud", compute_unit_price_usd=0.29846),
    )
    caps = adapter.capabilities()
    assert adapter.paradigm is Paradigm.COMPUTE_TIME
    assert caps["paradigm"] == "compute_time"
    assert caps["deployment"] == "cloud"
    assert caps["compute"] == {
        "replica_count": 2,
        "total_memory_gib": 24.0,
        "compute_units_per_hour": 3.0,
        "source": (
            "system.asynchronous_metrics.CGroupMemoryTotal across "
            "clusterAllReplicas(default)"
        ),
        "approximate": True,
    }
    assert caps["budget"]["ceiling_compute_unit_hours"] == 0.05
    assert fake_clickhouse_connection.data_queries == []


def test_a_self_hosted_endpoint_declared_cloud_is_refused(
    fake_clickhouse_connection,
):
    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(deployment="cloud")
    )
    with pytest.raises(ClickHouseConnectionError, match="cloud_mode != 1"):
        adapter.capabilities()


def test_a_cloud_endpoint_declared_self_hosted_is_refused(fake_clickhouse_connection):
    """The deployment is a committed declaration, never a sniff; the server
    check exists only to catch a declaration that does not match reality, which
    is exactly the case where guarding in the wrong unit would be silent."""

    fake_clickhouse_connection.cloud_mode = "1"
    adapter = make_adapter(fake_clickhouse_connection)
    with pytest.raises(ClickHouseConnectionError, match="cloud_mode"):
        adapter.capabilities()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "replica": "r1",
                "memory_bytes": 8 * 1024**3,
                "expected_replicas": 2,
            }
        ],
        [
            {
                "replica": "r1",
                "memory_bytes": "bad",
                "expected_replicas": 1,
            }
        ],
        [
            {
                "replica": "r1",
                "memory_bytes": 8 * 1024**3,
                "expected_replicas": 1,
            },
            {
                "replica": "r1",
                "memory_bytes": 8 * 1024**3,
                "expected_replicas": 1,
            },
        ],
    ],
)
def test_cloud_capacity_fails_closed_on_empty_partial_or_malformed_facts(
    fake_clickhouse_connection, rows
):
    fake_clickhouse_connection.cloud_mode = "1"
    fake_clickhouse_connection.capacity_rows = rows
    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(deployment="cloud")
    )
    with pytest.raises(ClickHouseConnectionError, match="capacity could not be proved"):
        adapter.capabilities()


def test_cloud_capacity_denial_fails_closed(fake_clickhouse_connection):
    fake_clickhouse_connection.cloud_mode = "1"
    fake_clickhouse_connection.capacity_denied = True
    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(deployment="cloud")
    )
    with pytest.raises(ClickHouseConnectionError, match="capacity could not be proved"):
        adapter.profile_estimate(["shop.events"])


def test_cloud_estimate_and_spend_translate_compute_and_usd(
    fake_clickhouse_connection,
):
    fake_clickhouse_connection.cloud_mode = "1"
    fake_clickhouse_connection.capacity_rows = [
        {
            "replica": "r1",
            "memory_bytes": 8 * 1024**3,
            "expected_replicas": 2,
        },
        {
            "replica": "r2",
            "memory_bytes": 8 * 1024**3,
            "expected_replicas": 2,
        },
    ]
    adapter = make_adapter(
        fake_clickhouse_connection,
        target=ClickHouseTarget(deployment="cloud", compute_unit_price_usd=0.29846),
    )
    estimate = adapter.describe_estimate(60.0, {"shop.events": 60.0})
    assert estimate["estimated_compute_unit_hours"] == pytest.approx(0.033333)
    assert estimate["estimated_usd"] == pytest.approx(0.0099)
    assert estimate["compute_unit_rate"]["compute_units_per_hour"] == 2.0
    assert "approximate" in estimate["notes"][0]

    adapter.cost_gate.record_billed(30.0, statement="SELECT 1")
    assert adapter.spend_display() == {
        "compute_unit_hours_billed": pytest.approx(0.016667),
        "usd_billed": pytest.approx(0.005),
    }

    unpriced = make_adapter(
        fake_clickhouse_connection,
        target=ClickHouseTarget(deployment="cloud"),
    )
    estimate_without_price = unpriced.describe_estimate(60.0)
    assert estimate_without_price["estimated_compute_unit_hours"] > 0
    assert "estimated_usd" not in estimate_without_price
    unpriced.cost_gate.record_billed(30.0, statement="SELECT 1")
    assert "usd_billed" not in unpriced.spend_display()


def test_a_compute_unit_price_under_self_hosted_is_refused_not_ignored():
    """Accepted-and-ignored is worse than rejected: a price per compute unit
    under a paradigm that counts seconds would read as a configured dollar
    translation and produce none."""

    with pytest.raises(ValueError, match="deployment: cloud"):
        ClickHouseTarget(deployment="self_hosted", compute_unit_price_usd=0.3)

    # And it is accepted under the deployment it belongs to, so the config
    # surface is already its final shape.
    assert ClickHouseTarget(deployment="cloud", compute_unit_price_usd=0.3)


def test_an_unknown_deployment_is_refused():
    with pytest.raises(ValueError, match="self_hosted, cloud"):
        ClickHouseTarget(deployment="onprem")


# --- scope resolution: refused, not dropped ------------------------------------


def test_a_nonexistent_scope_is_refused_and_names_what_exists(
    fake_clickhouse_connection,
):
    """A scope that resolves to nothing and silently falls back is a cost-safety
    bug: the estimate the user confirms would cover tables they never named."""

    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(databases=["nope"])
    )
    with pytest.raises(ClickHouseConnectionError, match="shop"):
        adapter.list_objects()


def test_a_scope_refusal_blames_where_the_entry_came_from(fake_clickhouse_connection):
    adapter = make_adapter(
        fake_clickhouse_connection,
        target=ClickHouseTarget(databases=["nope"]),
        scope_origin="--scope",
    )
    with pytest.raises(ClickHouseConnectionError, match=r"\[from --scope\]"):
        adapter.list_objects()


def test_a_dotted_scope_is_refused_as_too_many_parts(fake_clickhouse_connection):
    adapter = make_adapter(
        fake_clickhouse_connection,
        target=ClickHouseTarget(databases=["shop.customers"]),
    )
    with pytest.raises(ClickHouseConnectionError, match="too many parts"):
        adapter.list_objects()


def test_a_valid_scope_still_bounds_the_inventory(fake_clickhouse_connection):
    fake_clickhouse_connection.tables.append(
        FakeClickHouseTable(
            database="other", name="t", columns=[("id", "UInt64", True)], total_rows=1
        )
    )
    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(databases=["shop"])
    )
    assert all(o.identifier.startswith("shop.") for o in adapter.list_objects())


# --- the dev-target preflight --------------------------------------------------


def test_missing_dev_privileges_are_named_with_the_grant_that_fixes_them(
    fake_clickhouse_connection,
):
    """dbt-clickhouse creates the dev database itself, so its absence is not the
    failure; the privilege to create it is."""

    adapter = make_adapter(fake_clickhouse_connection)
    missing = adapter.missing_dev_namespaces("dbt_dev", role="dex_ro")
    assert missing
    assert "CREATE DATABASE" in missing[0]
    assert "dbt_dev" in missing[0]


def test_a_role_holding_the_privileges_is_cleared(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []


def test_an_unreadable_grant_table_abstains_rather_than_guessing(
    fake_clickhouse_connection,
):
    """ClickHouse shows another user's grants only to a caller holding SHOW
    ACCESS, so 'no verdict' is a real state. A preflight that guesses would
    either refuse a build that works or pass one that cannot."""

    fake_clickhouse_connection.grants_readable = False
    adapter = make_adapter(fake_clickhouse_connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []
    notes = adapter.table_notes("dbt_dev.*")
    assert any("could not be read" in n for n in notes)


def test_a_partial_grant_read_may_clear_but_never_refuses(fake_clickhouse_connection):
    """Direct grants readable, role membership not. A privilege held through an
    invisible role looks exactly like a missing one, so the partial read is only
    ever allowed to clear a target."""

    fake_clickhouse_connection.role_grants_readable = False
    adapter = make_adapter(fake_clickhouse_connection)

    # dbt_dev holds them directly: cleared, confidently.
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []

    # dex_ro does not, but the answer may be hiding in a role dex cannot see,
    # so this abstains with a note rather than refusing the build.
    assert adapter.missing_dev_namespaces("dbt_dev", role="dex_ro") == []
    assert any("only be read in part" in n for n in adapter.table_notes("dbt_dev.*"))


def test_a_privilege_held_through_a_role_clears_the_target(
    fake_clickhouse_connection,
):
    fake_clickhouse_connection.grants = [
        FakeGrant(
            user_name="", role_name="writer", access_type="CREATE", database="dbt_dev"
        ),
        FakeGrant(
            user_name="", role_name="writer", access_type="INSERT", database="dbt_dev"
        ),
        FakeGrant(
            user_name="", role_name="writer", access_type="SELECT", database="dbt_dev"
        ),
    ]
    adapter = make_adapter(fake_clickhouse_connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="someone") == []


def test_list_namespace_objects_reads_only_the_asked_database(
    fake_clickhouse_connection,
):
    adapter = make_adapter(fake_clickhouse_connection)
    assert adapter.list_namespace_objects("shop") == [
        "customers",
        "events",
        "order_events_raw",
    ]
    assert adapter.list_namespace_objects("absent") == []
    assert fake_clickhouse_connection.data_queries == []


# --- identifiers ---------------------------------------------------------------


def test_identifiers_are_two_part_and_a_three_part_one_is_refused():
    assert ClickHouseAdapter._split("shop.customers") == ("shop", "customers")
    with pytest.raises(ValueError, match=r"database\.table"):
        ClickHouseAdapter._split("cat.shop.customers")


def test_a_bare_table_reference_completes_against_the_connected_database(
    fake_clickhouse_connection,
):
    adapter = make_adapter(
        fake_clickhouse_connection, target=ClickHouseTarget(database="shop")
    )
    assert adapter._referenced_tables("SELECT 1 FROM customers") == {"shop.customers"}
    assert adapter._referenced_tables("SELECT 1 FROM shop.events") == {"shop.events"}


def test_quoting_survives_an_embedded_quote(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    assert adapter._quote('shop.we"ird') == '"shop"."we""ird"'


def test_column_meta_ordinals_follow_position(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    assert [c.ordinal for c in columns] == list(range(len(columns)))
    assert isinstance(columns[0], ColumnMeta)


def test_an_unknown_object_names_the_two_part_grammar(fake_clickhouse_connection):
    adapter = make_adapter(fake_clickhouse_connection)
    with pytest.raises(ClickHouseConnectionError, match="two parts"):
        adapter.table_metadata("shop.absent")


def test_timeout_codes_that_are_not_budget_bound_read_as_a_timeout(
    fake_clickhouse_connection,
):
    """A wall-clock limit tighter than the budget is the caller's own bound, so
    hitting it is a TimeoutError telling them to narrow the query, not a budget
    refusal telling them to raise --budget."""

    adapter = make_adapter(fake_clickhouse_connection, ceiling=None, confirmed=True)
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=99.0
    )
    with pytest.raises(TimeoutError, match="narrow it"):
        adapter._run_rows("SELECT 1", timeout_seconds=5)
    assert TIMEOUT_EXCEEDED and TOO_MANY_BYTES  # codes are the fake's, not invented
