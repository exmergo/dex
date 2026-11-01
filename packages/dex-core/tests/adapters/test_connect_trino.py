"""The Trino connector: metadata is free (coordinator information_schema reads,
no scans), every billed statement is estimated from a free non-executing
EXPLAIN (TYPE IO) and gated in cluster-seconds, and the budget binds at both
the client (charge) and the simulated server (query_max_run_time and
query_max_scan_physical_bytes, set through SET SESSION).

Everything here runs against the stateful fake (tests/fakes/trino.py):
deterministic, offline, free.
"""

from __future__ import annotations

import pytest

pytest.importorskip("trino")

from fakes.trino import (
    PERMISSION_DENIED,
    SYNTAX_ERROR,
    TABLE_NOT_FOUND,
    FakeResult,
    FakeTrinoConnection,
    FakeTrinoTable,
    server_error,
)

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.base import ColumnMeta
from exmergo_dex_core.adapters.trino import (
    TrinoAdapter,
    TrinoConnectionError,
    guard,
    is_degraded_type,
)
from exmergo_dex_core.config import TrinoTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.errors import ConnectorError, WarehouseQueryError
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)
from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError


def _gate(**kwargs) -> CostGate:
    defaults = {
        "paradigm": Paradigm.DB_LOAD,
        "connector": "trino",
        "command": "explore profile",
        "ceiling": 600.0,
        "confirmed": True,
        "session_ceiling": None,
        "session_spent": 0.0,
    }
    return CostGate(**{**defaults, **kwargs})


def make_adapter(
    connection: FakeTrinoConnection,
    *,
    ceiling: float | None = 600.0,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: TrinoTarget | None = None,
    scope_origin: str | None = None,
    owns_connection: bool = True,
    connection_warnings: list[str] | None = None,
) -> TrinoAdapter:
    return TrinoAdapter(
        connection=connection,
        cost_gate=_gate(
            ceiling=ceiling,
            confirmed=confirmed,
            session_ceiling=session_ceiling,
            session_spent=session_spent,
            record=record,
        ),
        target=target or TrinoTarget(),
        host="trino.example.internal",
        auth_method="environment:password",
        scope_origin=scope_origin,
        owns_connection=owns_connection,
        connection_warnings=connection_warnings,
        clock=connection.clock,
    )


# --- the type degrader ---------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "degraded"),
    [
        ("varchar", False),
        ("varchar(64)", False),
        ("char(10)", False),
        ("bigint", False),
        ("decimal(12,2)", False),
        ("timestamp(3) with time zone", False),
        ("uuid", False),
        ("array(varchar)", True),
        ("map(varchar, integer)", True),
        ("row(a bigint, b varchar)", True),
        ("json", True),
        ("varbinary", True),
        ("qdigest(bigint)", True),
        ("HyperLogLog", True),
        ("geometry", True),
    ],
)
def test_nested_and_special_types_are_degraded_before_any_statistic(declared, degraded):
    """array(varchar) contains VARCHAR and map(varchar, integer) contains INT,
    so the shared type predicates would ask for a regexp_like or an extreme over
    them, and one FUNCTION_NOT_FOUND kills the whole profiling batch."""

    assert is_degraded_type(declared) is degraded


def test_a_degraded_column_gets_only_a_non_null_count(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    tags = [c for c in columns if c.name == "tags"]
    sql, _plan = adapter._build_aggregate_sql(
        "memory.shop.customers",
        tags,
        safe={"tags"},
        shape={"tags"},
        type_req={"tags"},
        key_shape_req={"tags"},
        temporal_req=set(),
    )
    assert 'COUNT("tags") AS nn_0' in sql
    assert "approx_distinct" not in sql
    assert "regexp_like" not in sql
    assert "MIN(" not in sql


def test_exact_distinct_counts_skips_a_degraded_column(fake_trino_connection):
    """The engine also sends columns with no distinct count, which is what a
    degraded column reports, and one Trino cannot count would fail the scan."""

    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 50, "d_0": 50}]
    )
    adapter = make_adapter(fake_trino_connection)
    assert adapter.exact_distinct_counts("memory.shop.customers", ["id", "tags"]) == {
        "id": 50
    }
    statements = [
        q.sql for q in fake_trino_connection.data_queries if "COUNT(DISTINCT" in q.sql
    ]
    assert statements
    assert all('"tags"' not in sql for sql in statements)


def test_exact_distinct_counts_keeps_a_comparable_binary_column(fake_trino_connection):
    """A binary hash can be a declared grain, and Trino can count it."""

    fake_trino_connection.tables.append(
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="files",
            columns=[("hash", "varbinary", True)],
        )
    )
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 50, "d_0": 49}]
    )
    adapter = make_adapter(fake_trino_connection)
    assert adapter.exact_distinct_counts("memory.shop.files", ["hash"]) == {"hash": 49}


def test_exact_distinct_counts_runs_nothing_for_only_degraded_columns(
    fake_trino_connection,
):
    adapter = make_adapter(fake_trino_connection)
    assert adapter.exact_distinct_counts("memory.shop.customers", ["tags"]) == {}
    assert not [
        q for q in fake_trino_connection.data_queries if "COUNT(DISTINCT" in q.sql
    ]


# --- the guard -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT id FROM shop.customers", "not fully qualified"),
        ("SELECT id FROM customers", "not fully qualified"),
        (
            "SELECT * FROM TABLE(pg.system.query(query => 'SELECT 1'))",
            # The base guard's external-read denylist names `query` before this
            # adapter's table-function rule is reached; same decision, same reason.
            "query is not permitted",
        ),
        ('SELECT "$path" FROM memory.shop.customers', "hidden column"),
        ('SELECT * FROM lake.app."orders$files"', "metadata table"),
        ("SELECT * FROM lake.app.orders FOR VERSION AS OF 3", "time travel"),
        ("DESCRIBE memory.shop.customers", "DESCRIBE"),
        ("EXPLAIN ANALYZE SELECT 1", "SELECT"),
        ("CALL lake.system.rollback_to_snapshot('app', 'orders', 1)", "SELECT"),
        ("SET SESSION query_max_run_time = '1d'", "SELECT"),
        ("RESET SESSION query_max_run_time", "SELECT"),
        ("USE memory.shop", "SELECT"),
        ("ANALYZE lake.app.orders", "SELECT"),
        ("CREATE TABLE memory.shop.x AS SELECT 1 AS a", "SELECT"),
        (
            "INSERT INTO memory.shop.customers SELECT * FROM memory.shop.customers",
            "SELECT",
        ),
    ],
)
def test_the_guard_refuses(sql: str, fragment: str):
    with pytest.raises(NotSelectOnlyError, match=fragment):
        guard(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM memory.shop.customers",
        'SELECT "id" FROM "memory"."shop"."customers"',
        "WITH recent AS (SELECT id FROM memory.shop.customers) SELECT id FROM recent",
        "SELECT c.id FROM memory.shop.customers c "
        "JOIN memory.shop.events e ON e.id = c.id",
        "SELECT k FROM memory.shop.events "
        "CROSS JOIN UNNEST(CAST(json_parse(payload) AS ARRAY(JSON))) AS u(k)",
        "SELECT COUNT(*) FROM memory.shop.customers TABLESAMPLE BERNOULLI (1.5)",
    ],
)
def test_the_guard_admits_the_sibling_of_every_refusal(sql: str):
    assert guard(sql) == sql


# --- metadata (free) -----------------------------------------------------------


def test_capabilities_shape_and_free(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    caps = adapter.capabilities()
    assert caps["connector"] == "trino"
    assert caps["dialect"] == "trino"
    assert caps["read_only"] is True
    assert caps["paradigm"] == "db_load"
    assert caps["deployment"] == "self_hosted"
    assert caps["auth_method"] == "environment:password"
    assert caps["server_version"] == "483"
    assert caps["host"] == "trino.example.internal"
    assert fake_trino_connection.data_queries == []


def test_session_read_only_is_reported_false_rather_than_assumed(fake_trino_connection):
    """Trino has no read-only session mode a client can hold, so the honest
    answer is no; read-only rests on the guard and the cluster's access control."""

    caps = make_adapter(fake_trino_connection).capabilities()
    assert caps["read_only"] is True
    assert caps["session_read_only"] is False


def test_connection_warnings_ride_connect_test(fake_trino_connection):
    warning = "TRINO_URL names a catalog and schema; dex ignores both"
    caps = make_adapter(
        fake_trino_connection, connection_warnings=[warning]
    ).capabilities()
    assert caps["warnings"] == [warning]


def test_inventory_is_three_part_free_and_excludes_metadata(fake_trino_connection):
    fake_trino_connection.tables.append(
        FakeTrinoTable(catalog="memory", schema="shop", name="orders$files", columns=[])
    )
    adapter = make_adapter(fake_trino_connection)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "memory.shop.customers",
        "memory.shop.events",
        "memory.shop.v_totals",
        "pg.public.accounts",
    ]
    assert all(o.identifier.count(".") == 2 for o in objects)
    assert all(o.row_count is None for o in objects)
    view = next(o for o in objects if o.name == "v_totals")
    assert view.object_type == "view"
    assert fake_trino_connection.data_queries == []


def test_no_session_catalog_is_ever_relied_on(fake_trino_connection):
    """Every statement the adapter issues names its catalog: the metadata reads
    go to <catalog>.information_schema, and nothing sends USE."""

    adapter = make_adapter(fake_trino_connection)
    adapter.list_objects()
    for query in fake_trino_connection.queries:
        assert not query.sql.upper().startswith("USE")
        if "information_schema" in query.sql:
            assert '".information_schema' in query.sql


def test_a_free_door_never_inherits_a_billed_cap(fake_trino_connection):
    """The caps live in the session, so a door that did not restate its own
    would run under whatever the last statement set. A metadata read after a
    tight billed statement must not be killed by that statement's cap."""

    adapter = make_adapter(fake_trino_connection, ceiling=30.0)
    adapter.run_query(
        "SELECT COUNT(*) FROM memory.shop.customers", max_rows=10, timeout_seconds=60
    )
    adapter.forget_metadata()
    adapter.list_objects()
    metadata = [
        q for q in fake_trino_connection.queries if "information_schema" in q.sql
    ]
    assert metadata
    for query in metadata:
        assert query.properties == {"query_max_run_time": "60s"}


# --- estimation (free) ---------------------------------------------------------


def test_query_estimate_uses_the_free_non_executing_explain(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.explain_override = {"memory.shop.events": 10_000_000}
    estimate = adapter.query_estimate("SELECT COUNT(*) FROM memory.shop.events")
    # 10 MB of a 50 GB table is nowhere near the whole relation.
    assert estimate < 1.0
    assert fake_trino_connection.data_queries == []
    assert any(
        q.sql.startswith("EXPLAIN (TYPE IO, FORMAT JSON)")
        for q in fake_trino_connection.queries
    )


def test_a_table_without_statistics_is_priced_at_the_floor_and_said_so(
    fake_trino_connection,
):
    """A federated catalog answers NaN; that table is a guess, and the handshake
    has to say which part of the quote is one."""

    adapter = make_adapter(fake_trino_connection)
    adapter.query_estimate("SELECT COUNT(*) FROM pg.public.accounts")
    payload = adapter.describe_estimate(5.0)
    assert payload["estimate_basis"] == "floor"
    assert any("pg.public.accounts" in n for n in payload["notes"])
    assert any("no statistics" in n for n in adapter.table_notes("pg.public.accounts"))


def test_a_table_with_statistics_is_priced_from_the_plan(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    adapter.query_estimate("SELECT COUNT(*) FROM memory.shop.events")
    assert adapter.describe_estimate(1.0)["estimate_basis"] == "explain_io"


def test_the_plan_s_predicate_values_never_reach_the_handshake(fake_trino_connection):
    """EXPLAIN (TYPE IO) carries the pushed-down predicate ranges, which are
    literal values from the statement. Only the estimates are read."""

    adapter = make_adapter(fake_trino_connection)
    total, per_table = adapter.profile_estimate(
        ["memory.shop.customers", "pg.public.accounts"]
    )
    payload = adapter.describe_estimate(total, per_table)
    rendered = repr(payload) + repr(adapter.table_notes("memory.shop.customers"))
    assert "secret@example.com" not in rendered


def test_describe_estimate_is_honest_about_quality_and_names_the_caps(
    fake_trino_connection,
):
    payload = make_adapter(fake_trino_connection).describe_estimate(12.5, {"x": 12.5})
    assert payload["estimate_quality"] == "heuristic"
    assert payload["per_table_seconds"] == {"x": 12.5}
    assert "query_max_run_time" in payload["hint"]
    assert "query_max_scan_physical_bytes" in payload["hint"]
    assert "cluster-seconds" in payload["notes"][0]


def test_spend_display_claims_no_currency(fake_trino_connection):
    assert make_adapter(fake_trino_connection).spend_display() == {}


def test_profile_estimate_prices_the_columns_the_run_reads(fake_trino_connection):
    """Column pruning counts: a blob column left out of the scan is left out of
    the plan that prices it, and opting it back in changes what is explained."""

    fake_trino_connection.tables.append(
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="files",
            columns=[("id", "bigint", True), ("body", "varbinary", True)],
            estimate_bytes=1_000_000,
        )
    )
    adapter = make_adapter(fake_trino_connection)
    adapter.profile_estimate(["memory.shop.files"])
    explained = [
        q.sql for q in fake_trino_connection.queries if q.sql.startswith("EXPLAIN")
    ]
    assert '"body"' not in explained[-1]
    adapter.forget_metadata()
    adapter.profile_estimate(
        ["memory.shop.files"], include_blobs={"memory.shop.files.body"}
    )
    explained = [
        q.sql for q in fake_trino_connection.queries if q.sql.startswith("EXPLAIN")
    ]
    assert '"body"' in explained[-1]


# --- the cost gate binds -------------------------------------------------------


def test_unconfirmed_scan_never_executes(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, confirmed=False)
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    with pytest.raises(ConfirmationRequiredError):
        adapter.column_aggregates("memory.shop.customers", columns)
    assert fake_trino_connection.data_queries == []


def test_confirmed_run_without_a_ceiling_is_refused(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=None)
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    with pytest.raises(Exception) as exc:
        adapter.column_aggregates("memory.shop.customers", columns)
    assert "--budget" in str(exc.value) or "ceiling" in str(exc.value)
    assert fake_trino_connection.data_queries == []


def test_over_ceiling_cannot_be_confirmed_through(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=1.0)
    _meta, columns = adapter.table_metadata("memory.shop.events")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("memory.shop.events", columns)
    assert fake_trino_connection.data_queries == []


def test_every_billed_statement_runs_under_both_server_caps(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=600.0)
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    adapter.column_aggregates("memory.shop.customers", columns)
    data = fake_trino_connection.data_queries
    assert data
    for query in data:
        run_time = int(query.properties["query_max_run_time"].removesuffix("s"))
        assert 1 <= run_time <= 600
        assert query.properties["query_max_scan_physical_bytes"].endswith("B")


def test_the_caps_wind_down_to_what_remains_of_the_budget(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=100.0)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=40.0
    )
    adapter.run_query(
        "SELECT 1 AS n FROM memory.shop.customers", max_rows=10, timeout_seconds=600
    )
    adapter.run_query(
        "SELECT 1 AS n FROM memory.shop.customers", max_rows=10, timeout_seconds=600
    )
    first, second = fake_trino_connection.data_queries
    assert int(first.properties["query_max_run_time"].removesuffix("s")) > int(
        second.properties["query_max_run_time"].removesuffix("s")
    )


def test_a_tighter_wall_clock_wins_over_the_budget(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=600.0)
    adapter.run_query(
        "SELECT COUNT(*) FROM memory.shop.customers", max_rows=10, timeout_seconds=30
    )
    (query,) = fake_trino_connection.data_queries
    assert query.properties["query_max_run_time"] == "30s"


def test_a_sub_second_remainder_is_refused_rather_than_sent_as_zero(
    fake_trino_connection,
):
    """'0s' is not "unlimited" on Trino, it fails the statement at once; the
    gate refuses below a second, so it is never rendered."""

    adapter = make_adapter(
        fake_trino_connection, ceiling=600.0, session_ceiling=10.0, session_spent=9.5
    )
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM memory.shop.customers",
            max_rows=10,
            timeout_seconds=60,
        )
    assert fake_trino_connection.data_queries == []


def test_the_run_time_cap_kills_and_settles_what_ran(fake_trino_connection):
    """The server kills the statement at the cap; the client's stats never saw
    it finish, so it settles on the adapter's clock, which ran to the kill."""

    adapter = make_adapter(fake_trino_connection, ceiling=20.0)
    # Quoted cheap, runs long: the estimate was wrong, which is what the cap is for.
    fake_trino_connection.explain_override = {"memory.shop.events": 1_000_000}
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=500.0
    )
    with pytest.raises(OverCeilingError, match="query_max_run_time"):
        adapter.run_query(
            "SELECT 1 AS n FROM memory.shop.events", max_rows=10, timeout_seconds=600
        )
    settled = adapter.cost_gate.spend_summary()["seconds_billed"]
    assert 1.0 <= settled <= 20.0


def test_the_scan_cap_is_a_budget_refusal(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, ceiling=600.0)
    fake_trino_connection.explain_override = {"memory.shop.events": 1_000_000}
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=1.0, physical_bytes=10**15
    )
    with pytest.raises(OverCeilingError, match="query_max_scan_physical_bytes"):
        adapter.run_query(
            "SELECT 1 AS n FROM memory.shop.events", max_rows=10, timeout_seconds=600
        )


def test_a_finished_statement_settles_the_server_s_elapsed_with_its_query_id(
    fake_trino_connection,
):
    entries: list = []
    adapter = make_adapter(fake_trino_connection, record=entries.append)
    fake_trino_connection.explain_override = {"memory.shop.events": 1_000_000}
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=2.5
    )
    adapter.run_query(
        "SELECT 1 AS n FROM memory.shop.events", max_rows=10, timeout_seconds=600
    )
    assert adapter.cost_gate.spend_summary()["seconds_billed"] == pytest.approx(2.5)
    settlements = [e for e in entries if e.get("entry") == "settlement"]
    assert (
        settlements
        and settlements[-1]["job_id"] == fake_trino_connection.data_queries[-1].query_id
    )


# --- profiling ---------------------------------------------------------------


def test_profiling_fractions_use_double_literals(fake_trino_connection):
    """1.0 is decimal(2,1) on Trino and AVG keeps the scale, which would round
    every fraction to one decimal place."""

    adapter = make_adapter(fake_trino_connection)
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    email = [c for c in columns if c.name == "email"]
    sql, _plan = adapter._build_aggregate_sql(
        "memory.shop.customers",
        email,
        safe=set(),
        shape={"email"},
        type_req={"email"},
        key_shape_req={"email"},
        temporal_req=set(),
    )
    assert "THEN 1e0 " in sql
    assert "THEN 1.0 " not in sql
    assert "regexp_like(" in sql
    assert "approx_distinct(" in sql


def test_temporal_gaps_diff_the_earlier_period_first(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    _meta, columns = adapter.table_metadata("memory.shop.events")
    occurred = [c for c in columns if c.name == "occurred_at"]
    sql, _plan = adapter._build_aggregate_sql(
        "memory.shop.events",
        occurred,
        safe=set(),
        shape=set(),
        type_req=set(),
        key_shape_req=set(),
        temporal_req={"occurred_at"},
    )
    assert "date_diff('day', prev_period, period)" in sql
    assert "LAG(period) OVER (ORDER BY period)" in sql
    assert "'hour'" not in sql  # a date has no hour grain


def test_a_large_table_is_sampled_with_bernoulli_and_says_what_it_saves(
    fake_trino_connection,
):
    adapter = make_adapter(
        fake_trino_connection,
        target=TrinoTarget(max_full_profile_bytes=1_000_000_000),
    )
    _meta, columns = adapter.table_metadata("memory.shop.events")
    adapter.column_aggregates("memory.shop.events", columns)
    (query,) = fake_trino_connection.data_queries
    assert "TABLESAMPLE BERNOULLI (" in query.sql
    notes = adapter.table_notes("memory.shop.events")
    assert any("reduces CPU rather than the scan" in n for n in notes)


def test_a_full_scan_records_the_exact_row_count(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 42, **{f"nn_{i}": 42 for i in range(5)}}]
    )
    _meta, columns = adapter.table_metadata("memory.shop.customers")
    adapter.column_aggregates("memory.shop.customers", columns)
    meta, _ = adapter.table_metadata("memory.shop.customers")
    assert meta.row_count == 42


def test_exact_distinct_counts_escalate_inside_the_budget(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 10, "d_0": 10}]
    )
    assert adapter.exact_distinct_counts("memory.shop.customers", ["id"]) == {"id": 10}
    assert 'COUNT(DISTINCT "id")' in fake_trino_connection.data_queries[-1].sql


def test_value_domain_reads_ordered_pairs(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"d_0": [["NL", "3"], ["DE", "1"]], "n_0": 2}]
    )
    domain = adapter.value_domain_counts("memory.shop.customers", ["email"], limit=5)
    assert domain["email"].values == [("NL", 3), ("DE", 1)]
    assert domain["email"].total_distinct == 2


# --- run_query ---------------------------------------------------------------


def test_run_query_truncates_and_cancels_what_it_did_not_fetch(fake_trino_connection):
    """Fetching stops early; the query would keep reading server-side, and
    nothing would ever record it."""

    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"id": i} for i in range(50)]
    )
    result = adapter.run_query(
        "SELECT id FROM memory.shop.customers", max_rows=5, timeout_seconds=60
    )
    assert result.truncated is True
    assert len(result.cells) == 5
    assert fake_trino_connection.cancelled == [
        fake_trino_connection.data_queries[-1].query_id
    ]


def test_run_query_that_fits_cancels_nothing(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.row_resolver = lambda sql: FakeResult(rows=[{"id": 1}])
    result = adapter.run_query(
        "SELECT id FROM memory.shop.customers", max_rows=5, timeout_seconds=60
    )
    assert result.truncated is False
    assert fake_trino_connection.cancelled == []


def test_run_query_refuses_a_partial_name_before_anything_runs(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    with pytest.raises(NotSelectOnlyError, match=r"catalog\.schema\.table"):
        adapter.run_query(
            "SELECT id FROM shop.customers", max_rows=5, timeout_seconds=60
        )
    assert fake_trino_connection.data_queries == []


# --- error translation -------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected", "fragment"),
    [
        (PERMISSION_DENIED, TrinoConnectionError, "access control"),
        (TABLE_NOT_FOUND, TrinoConnectionError, "does not exist"),
        (SYNTAX_ERROR, WarehouseQueryError, "SYNTAX_ERROR"),
    ],
)
def test_server_refusals_are_named(fake_trino_connection, error, expected, fragment):
    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.refusals['FROM "memory"'] = server_error(error, "nope")
    fake_trino_connection.refusals["FROM memory"] = server_error(error, "nope")
    with pytest.raises(expected, match=fragment):
        adapter.run_query(
            "SELECT id FROM memory.shop.customers", max_rows=5, timeout_seconds=60
        )


def test_an_unreachable_cluster_is_a_connector_error(fake_trino_connection):
    fake_trino_connection.unreachable = True
    with pytest.raises(ConnectorError, match="could not be reached"):
        make_adapter(fake_trino_connection).capabilities()


# --- factory, dialect, and the connection's owner ------------------------------


def test_get_adapter_and_dialect(fake_trino_connection):
    adapter = get_adapter("trino", connection=fake_trino_connection, cost_gate=_gate())
    assert isinstance(adapter, TrinoAdapter)
    assert get_dialect("trino") == "trino"


def test_a_host_supplied_connection_is_not_closed(fake_trino_connection):
    make_adapter(fake_trino_connection, owns_connection=False).close()
    assert fake_trino_connection.closed is False
    make_adapter(fake_trino_connection).close()
    assert fake_trino_connection.closed is True


# --- the deployment gate ----------------------------------------------------


def test_galaxy_is_refused_by_the_adapter_too(fake_trino_connection):
    with pytest.raises(TrinoConnectionError, match="galaxy"):
        make_adapter(fake_trino_connection, target=TrinoTarget(deployment="galaxy"))


def test_a_mismatched_gate_is_refused(fake_trino_connection):
    with pytest.raises(ValueError, match="db_load"):
        TrinoAdapter(
            connection=fake_trino_connection,
            cost_gate=_gate(paradigm=Paradigm.COMPUTE_TIME),
        )


# --- scope resolution: refused, not dropped -----------------------------------


def test_a_catalog_allowlist_scopes_inventory(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection, target=TrinoTarget(catalogs=["pg"]))
    assert [o.identifier for o in adapter.list_objects()] == ["pg.public.accounts"]


def test_a_schema_scope_narrows_to_one_schema(fake_trino_connection):
    adapter = make_adapter(
        fake_trino_connection, target=TrinoTarget(catalogs=["memory.shop"])
    )
    assert {o.identifier.rsplit(".", 1)[0] for o in adapter.list_objects()} == {
        "memory.shop"
    }


def test_a_nonexistent_catalog_scope_is_refused_and_names_what_exists(
    fake_trino_connection,
):
    adapter = make_adapter(fake_trino_connection, target=TrinoTarget(catalogs=["nope"]))
    with pytest.raises(TrinoConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "names no catalog" in message and "memory" in message
    assert "[from trino.catalogs in .dex/config.yml]" in message
    assert fake_trino_connection.data_queries == []


def test_a_nonexistent_schema_scope_is_refused(fake_trino_connection):
    adapter = make_adapter(
        fake_trino_connection,
        target=TrinoTarget(catalogs=["memory.nope"]),
        scope_origin="--scope",
    )
    with pytest.raises(TrinoConnectionError, match="catalog memory has no schema nope"):
        adapter.list_objects()


def test_a_table_shaped_scope_is_refused(fake_trino_connection):
    adapter = make_adapter(
        fake_trino_connection, target=TrinoTarget(catalogs=["memory.shop.customers"])
    )
    with pytest.raises(TrinoConnectionError, match="never a table"):
        adapter.list_objects()


def test_scope_resolution_is_free_and_cached(fake_trino_connection):
    adapter = make_adapter(
        fake_trino_connection, target=TrinoTarget(catalogs=["memory"])
    )
    adapter.list_objects()
    adapter.list_objects()
    catalog_reads = [
        q for q in fake_trino_connection.queries if "system.metadata.catalogs" in q.sql
    ]
    assert len(catalog_reads) == 1
    assert fake_trino_connection.data_queries == []


def test_the_dev_target_is_read_only_when_it_exists(fake_trino_connection):
    adapter = make_adapter(
        fake_trino_connection, target=TrinoTarget(catalogs=["memory"])
    )
    adapter.read_dev_target("lake.dbt_dev")
    assert ("lake", "dbt_dev") in adapter._scopes()
    adapter.read_dev_target("lake.missing")
    assert all(c != "lake" for c, _ in adapter._scopes())


# --- the dev-target preflight -------------------------------------------------


def test_a_writable_dev_catalog_passes(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    assert adapter.missing_dev_namespaces("lake.dbt_dev", role="dbt_dev") == []
    # A missing schema is dbt's to create.
    assert adapter.missing_dev_namespaces("lake.brand_new", role="dbt_dev") == []


def test_a_read_only_dev_catalog_is_refused(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    (missing,) = adapter.missing_dev_namespaces("tpch.dbt_dev", role="dbt_dev")
    assert "tpch catalog, which is read-only" in missing


def test_a_missing_dev_catalog_is_refused_because_dbt_cannot_create_one(
    fake_trino_connection,
):
    adapter = make_adapter(fake_trino_connection)
    (missing,) = adapter.missing_dev_namespaces("nope.dbt_dev", role="dbt_dev")
    assert "dbt cannot create a catalog" in missing


def test_a_memory_dev_catalog_passes_with_a_durability_note(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    assert adapter.missing_dev_namespaces("memory.dbt_dev", role="dbt_dev") == []
    assert any("restart" in n for n in adapter.table_notes("memory.dbt_dev.*"))


def test_list_namespace_objects(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    assert adapter.list_namespace_objects("memory.shop") == [
        "customers",
        "events",
        "v_totals",
    ]
    assert adapter.list_namespace_objects("nope.shop") == []
    assert fake_trino_connection.data_queries == []


# --- identifiers ----------------------------------------------------------------


def test_identifiers_are_quoted_per_part(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    assert adapter._quote("memory.shop.customers") == '"memory"."shop"."customers"'
    with pytest.raises(ValueError, match=r"catalog\.schema\.table"):
        adapter._quote("shop.customers")


def test_an_unknown_object_names_the_fix(fake_trino_connection):
    with pytest.raises(TrinoConnectionError, match=r"trino\.catalogs"):
        make_adapter(fake_trino_connection).table_metadata("memory.shop.nope")


# --- a warehouse that changes mid-command ---------------------------------------


def test_forget_metadata_relists_after_a_build(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    assert "memory.shop.new_model" not in {o.identifier for o in adapter.list_objects()}
    fake_trino_connection.tables.append(
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="new_model",
            columns=[("id", "bigint", True)],
        )
    )
    adapter.forget_metadata()
    assert "memory.shop.new_model" in {o.identifier for o in adapter.list_objects()}


def test_column_meta_reads_nullability(fake_trino_connection):
    adapter = make_adapter(fake_trino_connection)
    _meta, columns = adapter.table_metadata("pg.public.accounts")
    assert columns == [
        ColumnMeta(name="id", data_type="integer", nullable=False, ordinal=0),
        ColumnMeta(name="owner", data_type="varchar(64)", nullable=True, ordinal=1),
    ]


# --- connection discovery (discover, don't ask) ---------------------------------


def _resolve(env: dict, target: TrinoTarget | None = None, repo_root="."):
    from exmergo_dex_core.connect import resolve_trino_connection

    return resolve_trino_connection(target or TrinoTarget(), env, repo_root)


def test_trino_url_is_first_and_its_path_is_ignored_out_loud():
    params, method, warnings = _resolve(
        {
            "TRINO_URL": "http://dex_ro@localhost:8085/memory/app",
            "TRINO_HOST": "other",
            "TRINO_USER": "someone",
        }
    )
    assert method == "environment:dsn"
    assert (params["host"], params["port"], params["user"]) == (
        "localhost",
        8085,
        "dex_ro",
    )
    assert params["http_scheme"] == "http"
    assert params["password"] is None
    assert warnings and "never sets a session catalog" in warnings[0]


def test_trino_url_takes_only_its_own_credential():
    """TRINO_PASSWORD is what the dex-rendered dbt profile reads for the writing
    identity; the URL is dex's read identity, so the two never mix."""

    params, _method, _warnings = _resolve(
        {"TRINO_URL": "trino://dex_ro@localhost:8085", "TRINO_PASSWORD": "dbt-secret"}
    )
    assert params["password"] is None
    assert params["http_scheme"] == "http"


def test_a_credential_defaults_the_scheme_to_https():
    params, method, _ = _resolve(
        {"TRINO_HOST": "trino.internal", "TRINO_USER": "dex", "TRINO_PASSWORD": "pw"}
    )
    assert method == "environment:password"
    assert (params["http_scheme"], params["port"]) == ("https", 443)


def test_a_credential_over_plain_http_is_refused_with_the_fix():
    from exmergo_dex_core.connect import CredentialDiscoveryError

    with pytest.raises(CredentialDiscoveryError, match="TRINO_HTTP_SCHEME=https"):
        _resolve(
            {
                "TRINO_HOST": "trino.internal",
                "TRINO_USER": "dex",
                "TRINO_JWT_TOKEN": "tok",
                "TRINO_HTTP_SCHEME": "http",
            }
        )


def test_the_environment_carries_a_jwt():
    params, method, _ = _resolve(
        {"TRINO_HOST": "trino.internal", "TRINO_USER": "dex", "TRINO_JWT_TOKEN": "tok"}
    )
    assert method == "environment:jwt"
    assert params["jwt_token"] == "tok"  # noqa: S105


def test_the_committed_target_takes_a_password_from_the_environment():
    params, method, _ = _resolve(
        {"TRINO_PASSWORD": "pw"},
        TrinoTarget(host="trino.internal", port=8443, user="dex", http_scheme="https"),
    )
    assert method == "config_target:password"
    assert (params["host"], params["port"], params["http_scheme"]) == (
        "trino.internal",
        8443,
        "https",
    )


def test_no_user_is_refused_by_name():
    from exmergo_dex_core.connect import CredentialDiscoveryError

    with pytest.raises(CredentialDiscoveryError, match="TRINO_USER"):
        _resolve({"TRINO_HOST": "localhost"})


def _profiles(project_dir, output: dict) -> None:
    import yaml

    (project_dir / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\n", encoding="utf-8"
    )
    (project_dir / "profiles.yml").write_text(
        yaml.safe_dump({"p": {"target": "dev", "outputs": {"dev": output}}}),
        encoding="utf-8",
    )


def test_a_dbt_profile_is_the_last_resort_with_env_var_expanded(tmp_path):
    _profiles(
        tmp_path,
        {
            "type": "trino",
            "method": "ldap",
            "host": "trino.internal",
            "port": 443,
            "user": "dbt_dev",
            "password": "{{ env_var('TRINO_PASSWORD') }}",
            "database": "lake",
            "schema": "dbt_dev",
        },
    )
    params, method, _ = _resolve({"TRINO_PASSWORD": "pw"}, repo_root=tmp_path)
    assert method == "dbt_profile:password"
    assert (params["host"], params["user"], params["password"]) == (
        "trino.internal",
        "dbt_dev",
        "pw",
    )


@pytest.mark.parametrize("method", ["oauth", "kerberos", "certificate"])
def test_an_interactive_or_ticketed_profile_method_is_refused_by_name(tmp_path, method):
    from exmergo_dex_core.connect import CredentialDiscoveryError

    _profiles(
        tmp_path,
        {"type": "trino", "method": method, "host": "trino.internal", "user": "dbt"},
    )
    with pytest.raises(CredentialDiscoveryError, match=f"method: {method}"):
        _resolve({}, repo_root=tmp_path)


def test_nothing_discoverable_names_every_route():
    from exmergo_dex_core.connect import CredentialDiscoveryError

    with pytest.raises(CredentialDiscoveryError) as exc:
        _resolve({}, repo_root="/nonexistent-dex-test-root")
    message = str(exc.value)
    for route in ("TRINO_URL", "TRINO_HOST", "trino.host", "profiles.yml"):
        assert route in message


# --- the deployment axis, before anything opens ---------------------------------


def test_galaxy_is_compute_time_and_self_hosted_is_db_load():
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.connect import paradigm_for

    assert paradigm_for("trino") is Paradigm.DB_LOAD
    galaxy = DexConfig(connector="trino", trino=TrinoTarget(deployment="galaxy"))
    assert paradigm_for("trino", galaxy) is Paradigm.COMPUTE_TIME


def test_galaxy_is_refused_before_any_discovery_or_connection(monkeypatch):
    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.errors import ConfigurationError
    from exmergo_dex_core.storage import MemoryStore

    def unreachable(*_args, **_kwargs):
        raise AssertionError("discovery ran for a refused deployment")

    monkeypatch.setattr(connect_mod, "resolve_trino_connection", unreachable)
    config = DexConfig(connector="trino", trino=TrinoTarget(deployment="galaxy"))
    with pytest.raises(ConfigurationError, match="Starburst Galaxy"):
        connect_mod.open_adapter(config=config, store=MemoryStore())


def test_a_credit_price_under_self_hosted_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="credit_price_usd"):
        TrinoTarget(credit_price_usd=0.5)
    assert (
        TrinoTarget(deployment="galaxy", credit_price_usd=0.5).credit_price_usd == 0.5
    )


def test_a_self_hosted_declaration_pointing_at_galaxy_is_refused():
    with pytest.raises(ValueError, match="Starburst Galaxy"):
        TrinoTarget(host="acme-prod.trino.galaxy.starburst.io")


def test_a_galaxy_host_from_the_environment_is_refused_too(monkeypatch):
    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.errors import ConfigurationError
    from exmergo_dex_core.storage import MemoryStore

    monkeypatch.setenv(
        "TRINO_URL", "https://me%40acme.com:pw@acme-prod.trino.galaxy.starburst.io:443"
    )
    with pytest.raises(ConfigurationError, match="Starburst Galaxy"):
        connect_mod.open_adapter(
            config=DexConfig(connector="trino", trino=TrinoTarget()),
            store=MemoryStore(),
        )


def test_an_unknown_deployment_is_refused():
    with pytest.raises(ValueError, match="self_hosted, galaxy"):
        TrinoTarget(deployment="cloud")


def test_a_statement_that_cannot_be_planned_is_priced_at_the_minimum(
    fake_trino_connection,
):
    """A build's tests read the model the build creates, which does not exist
    when it is priced, so EXPLAIN fails. That is priced at the minimum, as every
    connector prices a cold dev target, rather than at the stats-less table
    floor, and the note says the estimate is a lower bound for it."""

    adapter = make_adapter(fake_trino_connection)
    fake_trino_connection.refusals["EXPLAIN"] = server_error(
        TABLE_NOT_FOUND, "Table 'memory.dbt_dev.stg_orders' does not exist"
    )
    seconds = adapter.query_estimate("SELECT count(*) FROM memory.dbt_dev.stg_orders")
    assert seconds == pytest.approx(0.5)
    payload = adapter.describe_estimate(seconds)
    assert payload["estimate_basis"] == "floor"
    assert any("lower bound" in n for n in payload["notes"])
    assert not any("their catalogs report no statistics" in n for n in payload["notes"])
