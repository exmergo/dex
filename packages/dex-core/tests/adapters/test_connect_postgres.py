"""The Postgres adapter against the stateful fake connection: metadata is
free (pg_catalog lookups, no scans), every billed statement is estimated and
gated in database-seconds, and the budget binds at both the client (charge)
and the simulated server (statement_timeout)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("psycopg")

from fakes.postgres import FakePostgresTable, FakeResult

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.postgres import (
    _MIN_STATEMENT_SECONDS,
    _SCAN_BYTES_PER_SECOND,
    PostgresAdapter,
    PostgresConnectionError,
)
from exmergo_dex_core.config import PostgresTarget
from exmergo_dex_core.connect import (
    CredentialDiscoveryError,
    resolve_postgres_connection,
)
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
    target: PostgresTarget | None = None,
    scope_origin: str | None = None,
) -> PostgresAdapter:
    gate = CostGate(
        paradigm=Paradigm.DB_LOAD,
        ceiling=ceiling,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        confirmed=confirmed,
        connector="postgres",
        command="explore profile",
        record=record,
    )
    return PostgresAdapter(
        connection=connection,
        cost_gate=gate,
        target=target or PostgresTarget(),
        auth_method="database_url:password",
        scope_origin=scope_origin,
        clock=connection.clock,
    )


# --- metadata (free) ---------------------------------------------------------------


def test_capabilities_shape_and_free(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    caps = adapter.capabilities()
    assert caps["connector"] == "postgres"
    assert caps["dialect"] == "postgres"
    assert caps["read_only"] is True
    assert caps["session_read_only"] is True  # the SET took effect on the fake
    assert caps["paradigm"] == "db_load"
    assert caps["auth_method"] == "database_url:password"
    assert caps["database"] == "dexdb"
    assert caps["server_version"] == "16.4"
    assert caps["schema_count"] == 1  # shop
    assert caps["budget"]["ceiling_seconds"] == 600.0
    # Capabilities is a free probe: no data statement ran.
    assert fake_pg_connection.data_statements == []


def test_session_is_read_only_before_any_statement(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    adapter.list_objects()
    first = fake_pg_connection.statements[0].sql.lower()
    assert "set default_transaction_read_only = on" in first


def test_list_objects_uses_free_catalog_metadata_only(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "dexdb.shop.customers",
        "dexdb.shop.events",
    ]
    customers = next(o for o in objects if o.name == "customers")
    assert customers.row_count == 100
    assert customers.byte_size == 5_000_000_000
    assert customers.column_count == 4
    assert fake_pg_connection.data_statements == []


def test_unanalyzed_reltuples_reports_no_row_count(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    events_meta, _ = adapter.table_metadata("dexdb.shop.events")
    assert events_meta.row_count is None  # reltuples -1: never analyzed


def test_schema_allowlist_scopes_inventory(fake_pg_connection):
    fake_pg_connection.tables.append(
        FakePostgresTable(
            schema="other",
            name="noise",
            columns=[("id", "bigint", True)],
        )
    )
    adapter = make_adapter(fake_pg_connection, target=PostgresTarget(schemas=["shop"]))
    assert {o.schema for o in adapter.list_objects()} == {"shop"}


def test_views_carry_no_stored_size(fake_pg_connection):
    fake_pg_connection.tables.append(
        FakePostgresTable(
            schema="shop",
            name="v_totals",
            columns=[("total", "numeric", True)],
            kind="view",
            reltuples=-1.0,
            total_bytes=0,
        )
    )
    adapter = make_adapter(fake_pg_connection)
    meta, _ = adapter.table_metadata("dexdb.shop.v_totals")
    assert meta.object_type == "view"
    assert meta.row_count is None
    assert meta.byte_size is None


def test_unknown_object_names_the_fix(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    with pytest.raises(PostgresConnectionError, match=r"postgres\.schemas"):
        adapter.table_metadata("dexdb.shop.missing")


def test_get_adapter_constructs_postgres(fake_pg_connection):
    gate = CostGate(
        paradigm=Paradigm.DB_LOAD,
        ceiling=10.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="postgres",
    )
    adapter = get_adapter("postgres", connection=fake_pg_connection, cost_gate=gate)
    assert isinstance(adapter, PostgresAdapter)
    assert get_dialect("postgres") == "postgres"


# --- estimation (free) --------------------------------------------------------------


def test_profile_estimate_scales_with_bytes_and_batches(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    total, per_table = adapter.profile_estimate(["dexdb.shop.customers"])
    expected = 5_000_000_000 / _SCAN_BYTES_PER_SECOND  # one batch (4 columns)
    assert per_table["dexdb.shop.customers"] == pytest.approx(expected)
    assert total == pytest.approx(expected)
    # Estimation is free: metadata only, no scan ran.
    assert fake_pg_connection.data_statements == []


def _wide_blob_table() -> FakePostgresTable:
    # 50 plain columns plus one bytea column: without exclusion this is 2
    # batches (51 columns), with exclusion (the default) it's 1 (50 columns).
    columns = [(f"c_{i}", "integer", True) for i in range(50)]
    columns.append(("payload", "bytea", True))
    return FakePostgresTable(
        schema="raw",
        name="sessions",
        columns=columns,
        reltuples=100.0,
        total_bytes=5_000_000_000,
    )


def test_profile_estimate_accepts_include_blobs_without_crashing(fake_pg_connection):
    # Regression test: explore/commands.py::_profile_estimate always calls
    # adapter.profile_estimate(identifiers, include_blobs=...), so every
    # adapter with a profile_estimate must accept that kwarg.
    adapter = make_adapter(fake_pg_connection)
    adapter.profile_estimate(["dexdb.shop.customers"], include_blobs=set())


def test_profile_estimate_excludes_blob_columns_from_batch_count(fake_pg_connection):
    fake_pg_connection.tables.append(_wide_blob_table())
    adapter = make_adapter(fake_pg_connection)
    total, per_table = adapter.profile_estimate(["dexdb.raw.sessions"])
    expected = 1 * (5_000_000_000 / _SCAN_BYTES_PER_SECOND)  # 1 batch, blob excluded
    assert per_table["dexdb.raw.sessions"] == pytest.approx(expected)
    assert total == pytest.approx(expected)


def test_profile_estimate_include_blobs_override_adds_a_batch(fake_pg_connection):
    fake_pg_connection.tables.append(_wide_blob_table())
    adapter = make_adapter(fake_pg_connection)
    total, _per_table = adapter.profile_estimate(
        ["dexdb.raw.sessions"], include_blobs={"dexdb.raw.sessions.payload"}
    )
    expected = 2 * (5_000_000_000 / _SCAN_BYTES_PER_SECOND)  # 2 batches, 51 columns
    assert total == pytest.approx(expected)


def test_query_estimate_uses_the_free_planner(fake_pg_connection):
    fake_pg_connection.plan_costs = lambda sql: 640_000.0  # planner cost units
    adapter = make_adapter(fake_pg_connection)
    estimate = adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
    # 640k planner pages * 8192 bytes over the scan rate.
    assert estimate == pytest.approx(640_000.0 * 8192 / _SCAN_BYTES_PER_SECOND)
    explains = [
        s.sql for s in fake_pg_connection.statements if s.sql.startswith("EXPLAIN")
    ]
    assert len(explains) == 1
    assert explains[0].startswith("EXPLAIN (FORMAT JSON) ")
    assert fake_pg_connection.data_statements == []


def test_query_estimate_falls_back_to_table_bytes(fake_pg_connection):
    def broken_plan(sql: str) -> float:
        raise RuntimeError("no plan for you")

    fake_pg_connection.plan_costs = broken_plan
    adapter = make_adapter(fake_pg_connection)
    estimate = adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
    assert estimate == pytest.approx(5_000_000_000 / _SCAN_BYTES_PER_SECOND)


def test_query_estimate_floors_small_statements(fake_pg_connection):
    fake_pg_connection.plan_costs = lambda sql: 1.0
    adapter = make_adapter(fake_pg_connection)
    assert (
        adapter.query_estimate("SELECT count(*) FROM dexdb.shop.customers")
        == _MIN_STATEMENT_SECONDS
    )


def test_query_estimate_refuses_non_select(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    with pytest.raises(Exception, match="read-only"):
        adapter.query_estimate("DELETE FROM dexdb.shop.customers")


def test_describe_estimate_names_seconds_and_heuristic(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    payload = adapter.describe_estimate(12.5, {"dexdb.shop.customers": 12.5})
    assert payload["estimated_seconds"] == 12.5
    assert payload["estimate_quality"] == "heuristic"
    assert "--confirm --budget" in payload["hint"]
    assert "statement_timeout" in payload["hint"]
    assert payload["per_table_seconds"] == {"dexdb.shop.customers": 12.5}
    # No currency translation exists for db load.
    assert "estimated_usd" not in payload
    assert adapter.spend_display() == {}


# --- the cost gate binds ------------------------------------------------------------


def test_unconfirmed_scan_never_executes(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection, confirmed=False)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    with pytest.raises(ConfirmationRequiredError):
        adapter.column_aggregates("dexdb.shop.customers", columns)
    assert fake_pg_connection.data_statements == []


def test_over_ceiling_refuses_client_side(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection, ceiling=1.0)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("dexdb.shop.customers", columns)
    assert fake_pg_connection.data_statements == []


def test_every_billed_statement_is_server_capped(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 100, "nn_0": 100, "nn_1": 90, "nn_2": 80, "nn_3": 70}],
        seconds=1.0,
    )
    adapter = make_adapter(fake_pg_connection, ceiling=600.0)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns)
    billed = fake_pg_connection.data_statements
    assert billed, "the aggregate batch must have run"
    for statement in billed:
        assert statement.session_timeout_ms is not None
        assert statement.session_timeout_ms <= 600_000


def test_server_timeout_translates_to_over_ceiling(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(rows=[], seconds=999.0)
    adapter = make_adapter(fake_pg_connection, ceiling=200.0)
    with pytest.raises(OverCeilingError, match="statement_timeout"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=500.0,
        )
    # The killed statement still billed what ran (the timeout's worth).
    assert fake_pg_connection.clock.now == pytest.approx(200.0, abs=1.0)


def test_wall_clock_timeout_translates_to_timeout_error(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(rows=[], seconds=999.0)
    adapter = make_adapter(fake_pg_connection, ceiling=600.0)
    with pytest.raises(TimeoutError, match="narrow it"):
        adapter.run_query(
            "SELECT count(*) FROM dexdb.shop.customers",
            max_rows=10,
            timeout_seconds=30.0,
        )


def test_exhausted_budget_refuses_before_the_server(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 1}], seconds=99.4
    )
    adapter = make_adapter(fake_pg_connection, ceiling=100.0)
    adapter.run_query(
        "SELECT count(*) FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=500.0,
    )
    # 99.4 of 100 seconds billed; under one second remains.
    with pytest.raises(OverCeilingError, match="under one database-second"):
        adapter.run_query("SELECT 1", max_rows=10, timeout_seconds=500.0)


def test_actual_seconds_land_in_the_ledger(fake_pg_connection):
    entries: list[dict] = []
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=2.5
    )
    adapter = make_adapter(fake_pg_connection, record=entries.append)
    adapter.run_query(
        "SELECT count(*) AS n FROM dexdb.shop.customers",
        max_rows=10,
        timeout_seconds=30.0,
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["connector"] == "postgres"
    assert entry["billed_seconds"] == pytest.approx(2.5)
    assert "billed_bytes" not in entry
    assert entry["statement_sha256"]
    assert "SELECT" not in str(entry.values())


def test_session_ceiling_binds_across_commands(fake_pg_connection):
    adapter = make_adapter(
        fake_pg_connection,
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


# --- profiling ----------------------------------------------------------------------


def _aggregate_rows(sql: str) -> FakeResult:
    # COUNT(*) plus one nn_<i> per column; values keyed by alias.
    aliases = [part.split(" AS ")[-1] for part in sql.split(",")]
    row = {}
    for alias in aliases:
        alias = alias.split(" FROM ")[0].strip()
        row[alias] = 100 if alias == "n_total" else 90
    return FakeResult(rows=[row], seconds=0.5)


def test_aggregates_one_cheap_pass_no_count_distinct(fake_pg_connection):
    fake_pg_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, safe_min_max={"id"}
    )
    sql = fake_pg_connection.data_statements[0].sql
    assert "COUNT(*)" in sql
    assert "COUNT(DISTINCT" not in sql  # pg_stats carries distinct, not a scan
    by_name = {a.name: a for a in aggregates}
    # id: pg_stats n_distinct -1 scales by the exact row count from the batch.
    assert by_name["id"].distinct_count == 100
    assert by_name["id"].distinct_count_exact is False
    assert by_name["id"].is_unique is None  # estimates are never a proof
    # email: positive n_distinct used directly.
    assert by_name["email"].distinct_count == 90
    assert by_name["email"].null_fraction == pytest.approx(0.1)


def test_shape_stats_ride_the_scan_pass(fake_pg_connection):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[
            {
                "n_total": 100,
                "nn_0": 100,
                "nn_1": 90,
                "nn_2": 80,
                "nn_3": 70,
                "su_1": 0.8,
                "sp_1": 0.1,
                "st_1": 2.5,
            }
        ],
        seconds=0.5,
    )
    adapter = make_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, shape_stats={"email"}
    )
    sql = fake_pg_connection.data_statements[0].sql
    assert '"email" ~ \'' in sql
    for alias in ("su_1", "sp_1", "st_1"):
        assert f" AS {alias}" in sql
    assert assert_select_only(sql, dialect="postgres") == sql
    by_name = {a.name: a for a in aggregates}
    assert by_name["email"].upper_vocab_fraction == pytest.approx(0.8)
    assert by_name["email"].person_shape_fraction == pytest.approx(0.1)
    assert by_name["email"].avg_token_count == pytest.approx(2.5)
    # id was not requested: its shape fields stay None.
    assert by_name["id"].upper_vocab_fraction is None
    assert by_name["id"].person_shape_fraction is None
    assert by_name["id"].avg_token_count is None


def test_degraded_types_get_non_null_counts_only(fake_pg_connection):
    fake_pg_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, safe_min_max={"payload", "tags"}
    )
    sql = fake_pg_connection.data_statements[0].sql
    by_name = {a.name: a for a in aggregates}
    for degraded in ("payload", "tags"):  # jsonb and text[]
        assert by_name[degraded].distinct_count is None
        assert by_name[degraded].min_value is None
        assert f'MIN("{degraded}")' not in sql
        assert by_name[degraded].null_fraction is not None


def test_min_max_only_for_engine_cleared_columns(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[
            {
                "n_total": 100,
                "nn_0": 100,
                "mn_0": 1,
                "mx_0": 100,
                "nn_1": 90,
                "nn_2": 80,
                "nn_3": 70,
            }
        ],
        seconds=0.5,
    )
    adapter = make_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    aggregates = adapter.column_aggregates(
        "dexdb.shop.customers", columns, safe_min_max={"id"}
    )
    sql = fake_pg_connection.data_statements[0].sql
    assert 'MIN("id")' in sql
    assert 'MIN("email")' not in sql
    by_name = {a.name: a for a in aggregates}
    assert by_name["id"].min_value == 1
    assert by_name["email"].min_value is None


def test_stats_reads_never_select_value_columns(fake_pg_connection):
    fake_pg_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns)
    stats_reads = [s.sql for s in fake_pg_connection.statements if "pg_stats" in s.sql]
    assert stats_reads, "profiling must consult pg_stats"
    for sql in stats_reads:
        assert "most_common_vals" not in sql
        assert "histogram_bounds" not in sql


def test_missing_stats_notes_analyze(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 50, "nn_0": 50, "nn_1": 50}], seconds=0.5
    )
    adapter = make_adapter(fake_pg_connection, ceiling=2000.0)
    _meta, columns = adapter.table_metadata("dexdb.shop.events")
    aggregates = adapter.column_aggregates("dexdb.shop.events", columns)
    assert all(a.distinct_count is None for a in aggregates)
    assert any("ANALYZE" in note for note in adapter.table_notes("dexdb.shop.events"))


def test_exact_count_from_scan_supersedes_reltuples(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 137, "nn_0": 137, "nn_1": 137, "nn_2": 1, "nn_3": 1}],
        seconds=0.5,
    )
    adapter = make_adapter(fake_pg_connection)
    meta, columns = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 100  # the planner estimate
    adapter.column_aggregates("dexdb.shop.customers", columns)
    meta, _ = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 137  # the exact count the scan paid for


def test_sampling_above_threshold_notes_and_voids_uniqueness(fake_pg_connection):
    fake_pg_connection.row_resolver = _aggregate_rows
    adapter = make_adapter(
        fake_pg_connection,
        target=PostgresTarget(max_full_profile_bytes=1_000_000),
    )
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns)
    sql = fake_pg_connection.data_statements[0].sql
    assert "TABLESAMPLE SYSTEM" in sql
    notes = adapter.table_notes("dexdb.shop.customers")
    assert any("block sample" in note for note in notes)
    # A sampled COUNT(*) must not be cached as the exact row count.
    meta, _ = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 100


def test_exact_distinct_counts_ride_with_count_star(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n_total": 120, "d_0": 120}], seconds=0.5
    )
    adapter = make_adapter(fake_pg_connection)
    counts = adapter.exact_distinct_counts("dexdb.shop.customers", ["id"])
    assert counts == {"id": 120}
    sql = fake_pg_connection.data_statements[0].sql
    assert 'COUNT(DISTINCT "id")' in sql
    assert "COUNT(*)" in sql
    meta, _ = adapter.table_metadata("dexdb.shop.customers")
    assert meta.row_count == 120


def test_escalation_skipped_when_budget_cannot_cover(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection, ceiling=1.0)
    counts = adapter.exact_distinct_counts("dexdb.shop.customers", ["id"])
    assert counts == {}
    assert fake_pg_connection.data_statements == []
    notes = adapter.table_notes("dexdb.shop.customers")
    assert any("escalation skipped" in note for note in notes)


def test_distinct_combination_counts_batch_into_one_guarded_statement(
    fake_pg_connection,
):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"d_0": 97, "d_1": 100}], seconds=0.5
    )
    adapter = make_adapter(fake_pg_connection)
    counts = adapter.distinct_combination_counts(
        "dexdb.shop.customers", [["id", "email"], ["email", "id"]]
    )
    assert counts == {("id", "email"): 97, ("email", "id"): 100}
    stmts = fake_pg_connection.data_statements
    assert len(stmts) == 1
    sql = stmts[0].sql
    assert "SELECT DISTINCT" in sql
    # Postgres refuses an unaliased derived table; the portable shape carries
    # the alias everywhere.
    assert ") AS q_0" in sql
    assert assert_select_only(sql, dialect="postgres") == sql
    assert adapter.distinct_combination_counts("dexdb.shop.customers", []) == {}


def test_composite_probe_skipped_when_budget_cannot_cover(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection, ceiling=1.0)
    result = adapter.distinct_combination_counts(
        "dexdb.shop.customers", [["id", "email"]]
    )
    assert result == {}
    assert fake_pg_connection.data_statements == []
    assert any(
        "composite-key probe skipped" in note
        for note in adapter.table_notes("dexdb.shop.customers")
    )


# --- run_query ----------------------------------------------------------------------


def test_run_query_truncates_and_reports(fake_pg_connection):
    fake_pg_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": i} for i in range(5)], seconds=0.1
    )
    adapter = make_adapter(fake_pg_connection)
    result = adapter.run_query(
        "SELECT id AS n FROM dexdb.shop.customers",
        max_rows=3,
        timeout_seconds=30.0,
    )
    assert result.columns == ["n"]
    assert len(result.cells) == 3
    assert result.truncated is True


def test_run_query_rejects_writes(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    with pytest.raises(Exception, match="read-only"):
        adapter.run_query(
            "UPDATE dexdb.shop.customers SET email = 'x'",
            max_rows=10,
            timeout_seconds=30.0,
        )
    assert fake_pg_connection.data_statements == []


# --- credential discovery -----------------------------------------------------------


def test_discovery_prefers_config_service(tmp_path: Path):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text(
        "[analytics]\nhost=db.example.com\nport=5432\ndbname=shop\nuser=dex\n",
        encoding="utf-8",
    )
    params, method = resolve_postgres_connection(
        PostgresTarget(service="analytics"),
        {"PGSERVICEFILE": str(service_file), "DATABASE_URL": "postgres://x:y@h/d"},
        tmp_path,
    )
    assert params == {"service": "analytics"}
    assert method == "config_service:external"


def test_discovery_missing_service_names_the_fix(tmp_path: Path):
    with pytest.raises(CredentialDiscoveryError, match=r"pg_service\.conf"):
        resolve_postgres_connection(
            PostgresTarget(service="missing"),
            {"PGSERVICEFILE": str(tmp_path / "none.conf")},
            tmp_path,
        )


def test_discovery_database_url_classifies_password(tmp_path: Path):
    params, method = resolve_postgres_connection(
        PostgresTarget(),
        {"DATABASE_URL": "postgresql://dex:secret@localhost:5433/shop"},
        tmp_path,
    )
    assert params == {"conninfo": "postgresql://dex:secret@localhost:5433/shop"}
    assert method == "database_url:password"


def test_discovery_database_url_without_password_is_external(tmp_path: Path):
    _params, method = resolve_postgres_connection(
        PostgresTarget(),
        {"DATABASE_URL": "postgresql://dex@localhost/shop"},
        tmp_path,
    )
    assert method == "database_url:external"


def test_discovery_pg_env_lets_libpq_resolve(tmp_path: Path):
    params, method = resolve_postgres_connection(
        PostgresTarget(),
        {"PGHOST": "localhost", "PGDATABASE": "shop", "PGPASSWORD": "secret"},
        tmp_path,
    )
    assert params == {}  # libpq reads PG* natively
    assert method == "environment:password"


def test_discovery_pgservice_env(tmp_path: Path):
    _params, method = resolve_postgres_connection(
        PostgresTarget(), {"PGSERVICE": "analytics"}, tmp_path
    )
    assert method == "environment:service_file"


def test_discovery_config_target(tmp_path: Path):
    params, method = resolve_postgres_connection(
        PostgresTarget(host="db.internal", port=5433, dbname="shop", user="dex"),
        {},
        tmp_path,
    )
    assert params == {
        "host": "db.internal",
        "port": 5433,
        "dbname": "shop",
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
        "      type: postgres\n"
        "      host: db.example.com\n"
        "      port: 5432\n"
        "      user: dbt\n"
        "      password: hunter2\n"
        "      dbname: shop\n",
        encoding="utf-8",
    )
    params, method = resolve_postgres_connection(PostgresTarget(), {}, tmp_path)
    assert params["host"] == "db.example.com"
    assert params["dbname"] == "shop"
    assert method == "dbt_profile:password"


def test_discovery_failure_names_every_fix(tmp_path: Path):
    with pytest.raises(CredentialDiscoveryError) as excinfo:
        resolve_postgres_connection(PostgresTarget(), {}, tmp_path)
    message = str(excinfo.value)
    for fix in ("DATABASE_URL", "pg_service.conf", "profiles.yml", "postgres.host"):
        assert fix in message


def test_discovery_never_surfaces_a_password(tmp_path: Path):
    _params, method = resolve_postgres_connection(
        PostgresTarget(),
        {"DATABASE_URL": "postgresql://dex:supersecret@localhost/shop"},
        tmp_path,
    )
    assert "supersecret" not in method


# --- scope resolution: an entry that names nothing is refused, not dropped ------------


def test_nonexistent_schema_scope_is_refused_and_names_what_exists(fake_pg_connection):
    """Postgres was the worst of the connectors here: the allowlist was echoed
    back without ever asking the server, and the inventory filter then dropped
    the unmatched entry, so a typo simply returned nothing."""

    adapter = make_adapter(
        fake_pg_connection, target=PostgresTarget(schemas=["no_such_schema"])
    )
    with pytest.raises(PostgresConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "no_such_schema" in message
    assert "shop" in message  # the schemas that do exist
    assert "[from postgres.schemas in .dex/config.yml]" in message
    assert fake_pg_connection.data_statements == []


def test_scope_refusal_blames_the_flag_it_came_from(fake_pg_connection):
    adapter = make_adapter(
        fake_pg_connection,
        target=PostgresTarget(schemas=["nope"]),
        scope_origin="--scope",
    )
    with pytest.raises(PostgresConnectionError, match=r"\[from --scope\]"):
        adapter.list_objects()


def test_a_qualified_scope_is_refused_as_postgres_vocabulary(fake_pg_connection):
    """dbt refuses cross-database references outright, so a Postgres scope is
    always a bare schema in the connected database."""

    adapter = make_adapter(
        fake_pg_connection, target=PostgresTarget(schemas=["dexdb.shop"])
    )
    with pytest.raises(PostgresConnectionError, match="never a database or a table"):
        adapter.list_objects()


def test_a_valid_scope_still_bounds_the_inventory(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection, target=PostgresTarget(schemas=["shop"]))
    assert {o.schema for o in adapter.list_objects()} == {"shop"}
    assert fake_pg_connection.data_statements == []


# --- the dev-target preflight (free): the privilege, not the object -------------------


def _with_role(role):
    from fakes.postgres import FakePostgresConnection, FakePostgresTable

    return FakePostgresConnection(
        tables=[
            FakePostgresTable(
                schema="shop", name="customers", columns=[("id", "bigint", False)]
            )
        ],
        roles=[role],
        empty_schemas=["dbt_dev"],
    )


def test_a_writable_dev_schema_is_fine(fake_pg_connection):
    from fakes.postgres import FakeRole

    connection = _with_role(
        FakeRole(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE", "CREATE"}})
    )
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []
    assert connection.data_statements == []


def test_a_dev_schema_the_role_cannot_write_is_reported(fake_pg_connection):
    from fakes.postgres import FakeRole

    connection = _with_role(
        FakeRole(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE"}})
    )
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == [
        'CREATE on dev_schema "dbt_dev"'
    ]


def test_an_absent_dev_schema_is_fine_when_the_role_may_create_it(fake_pg_connection):
    """dbt creates the schema itself, so absence alone is not the failure."""

    from fakes.postgres import FakeRole

    connection = _with_role(FakeRole(name="dbt_dev", may_create_in_database=True))
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("not_there", role="dbt_dev") == []


def test_an_absent_dev_schema_the_role_cannot_create_is_reported(fake_pg_connection):
    from fakes.postgres import FakeRole

    connection = _with_role(FakeRole(name="dbt_dev", may_create_in_database=False))
    adapter = make_adapter(connection)
    assert adapter.missing_dev_namespaces("not_there", role="dbt_dev") == [
        'dev_schema "not_there"'
    ]


def test_the_privilege_is_asked_of_the_profile_role_not_the_connected_user(
    fake_pg_connection,
):
    """The whole reason the role is passed in: dex may read the warehouse as a
    read-only role while dbt builds as another, so asking about the connected
    user would refuse a build dbt could have run."""

    from fakes.postgres import FakeRole

    connection = _with_role(
        FakeRole(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE", "CREATE"}})
    )
    adapter = make_adapter(connection)
    # The writing role may build; the read-only role dex connects as may not, and
    # that is not a reason to refuse.
    assert adapter.missing_dev_namespaces("dbt_dev", role="dbt_dev") == []
    with pytest.raises(PostgresConnectionError, match="dex_ro"):
        adapter.missing_dev_namespaces("dbt_dev", role="dex_ro")


def test_a_profile_role_the_database_does_not_know_is_refused(fake_pg_connection):
    from fakes.postgres import FakeRole

    connection = _with_role(FakeRole(name="dbt_dev"))
    adapter = make_adapter(connection)
    with pytest.raises(PostgresConnectionError) as exc:
        adapter.missing_dev_namespaces("dbt_dev", role="ghost")
    assert "ghost" in str(exc.value)


def test_list_namespace_objects_lists_only_the_asked_schema(fake_pg_connection):
    adapter = make_adapter(fake_pg_connection)
    assert adapter.list_namespace_objects("shop") == ["customers", "events"]
    assert adapter.list_namespace_objects("not_there") == []
    assert fake_pg_connection.data_statements == []
