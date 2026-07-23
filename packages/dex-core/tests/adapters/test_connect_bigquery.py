"""The BigQuery adapter against the stateful fake client: metadata is free,
every billed statement is dry-run first, and the budget binds at both the
client and the (simulated) server."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from google.cloud import bigquery

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter, BigQueryConnectionError
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)

MB = 1024 * 1024


def make_adapter(
    client,
    *,
    ceiling: float | None = 500 * MB,
    confirmed: bool = True,
    session_ceiling: float | None = None,
    session_spent: float = 0.0,
    record=None,
    target: BigQueryTarget | None = None,
    scope_origin: str | None = None,
) -> BigQueryAdapter:
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=ceiling,
        session_ceiling=session_ceiling,
        session_spent=session_spent,
        confirmed=confirmed,
        connector="bigquery",
        command="explore profile",
        record=record,
    )
    return BigQueryAdapter(
        project="test-proj",
        cost_gate=gate,
        target=target or BigQueryTarget(),
        client=client,
        principal_type="user",
        scope_origin=scope_origin,
    )


# --- metadata (free) -------------------------------------------------------------


def test_capabilities_shape(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    caps = adapter.capabilities()
    assert caps["connector"] == "bigquery"
    assert caps["dialect"] == "bigquery"
    assert caps["read_only"] is True
    assert caps["paradigm"] == "bytes_scanned"
    assert caps["project"] == "test-proj"
    assert caps["principal_type"] == "user"
    assert caps["dataset_count"] == 2  # shop + logs
    # Capabilities is a free probe: no queries were issued to compute it.
    assert fake_bq_client.query_calls == []


def test_list_objects_uses_free_api_metadata_only(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    objects = adapter.list_objects()
    assert [o.identifier for o in objects] == [
        "test-proj.logs.requests",
        "test-proj.shop.customers",
        "test-proj.shop.events",
    ]
    customers = next(o for o in objects if o.name == "customers")
    assert customers.row_count == 100
    assert customers.byte_size == 5_000
    assert customers.column_count == 2
    assert fake_bq_client.query_calls == []


def test_dataset_allowlist_filters_inventory(fake_bq_client):
    adapter = make_adapter(fake_bq_client, target=BigQueryTarget(datasets=["shop"]))
    assert {o.schema for o in adapter.list_objects()} == {"shop"}
    assert adapter.capabilities()["dataset_count"] == 1


def test_table_metadata_normalizes_nested_schema(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    meta, columns = adapter.table_metadata("test-proj.shop.events")
    assert meta.identifier == "test-proj.shop.events"
    by_name = {c.name: c for c in columns}
    assert by_name["payload"].data_type == "STRUCT"
    assert by_name["labels"].data_type == "ARRAY<STRING>"
    assert by_name["id"].nullable is True
    id_col = next(
        c
        for c in adapter.table_metadata("test-proj.shop.customers")[1]
        if c.name == "id"
    )
    assert id_col.nullable is False  # REQUIRED mode


def test_views_report_no_stored_row_count(fake_bq_client):
    from fakes.bigquery import FakeTable
    from google.cloud import bigquery

    view = FakeTable(
        project="test-proj",
        dataset_id="shop",
        table_id="recent_customers",
        schema=[bigquery.SchemaField("id", "INTEGER")],
        num_rows=0,
        num_bytes=0,
        table_type="VIEW",
    )
    fake_bq_client.tables[view.identifier] = view
    adapter = make_adapter(fake_bq_client)
    meta = next(o for o in adapter.list_objects() if o.name == "recent_customers")
    assert meta.object_type == "view"
    assert meta.row_count is None


# --- the billed door --------------------------------------------------------------


def test_dry_run_precedes_every_execution(fake_bq_client):
    fake_bq_client.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_bq_client)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True, False]


def test_estimate_floors_at_the_billing_minimum(fake_bq_client):
    # customers is 5000 bytes, well under BigQuery's 10 MiB per-table minimum;
    # the estimate reports what will be billed, not the raw scan, so the agent
    # budgets against a truthful number instead of hitting a rejection ladder.
    adapter = make_adapter(fake_bq_client)
    estimate = adapter.query_estimate(
        "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`"
    )
    assert estimate == 10 * MB
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True]


def test_estimate_is_the_raw_scan_when_it_exceeds_the_floor():
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    big = FakeTable(
        project="test-proj",
        dataset_id="shop",
        table_id="huge",
        schema=[bigquery.SchemaField("id", "INTEGER")],
        num_rows=1,
        num_bytes=20 * MB,
    )
    client = FakeBigQueryClient(project="test-proj", tables=[big])
    adapter = make_adapter(client)
    estimate = adapter.query_estimate("SELECT COUNT(*) FROM `test-proj`.`shop`.`huge`")
    assert estimate == 20 * MB  # above the floor, the raw dry-run figure wins


def test_query_estimate_floors_per_referenced_table(fake_bq_client):
    # A two-table join is billed at least the minimum per table, so the floor is
    # twice a single scan even though the raw dry-run of both is a few KB.
    adapter = make_adapter(fake_bq_client)
    estimate = adapter.query_estimate(
        "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers` c "
        "JOIN `test-proj`.`shop`.`events` e ON c.id = e.id"
    )
    assert estimate == 2 * 10 * MB


def test_describe_estimate_names_the_per_query_floor(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    described = adapter.describe_estimate(
        30 * MB, {"test-proj.shop.customers": 30 * MB}
    )
    assert described["estimated_bytes"] == 30 * MB
    assert described["per_table_bytes"] == {"test-proj.shop.customers": 30 * MB}
    assert "--budget" in described["hint"] and "bytes" in described["hint"]
    assert any("10,485,760" in note or "10 MB" in note for note in described["notes"])


def test_profile_estimate_floors_each_batch_at_the_billing_minimum(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    total, per_table = adapter.profile_estimate(["test-proj.shop.customers"])
    # One batch over one table (the raw 5000-byte scan floors to the minimum)
    # plus a floor reserved for each of the two possible escalation queries
    # (customers has 2 non-blob columns and 100 rows, so both are possible):
    # 10 MB aggregate + 10 MB near-unique reserve + 10 MB composite reserve.
    assert per_table["test-proj.shop.customers"] == 30 * MB
    assert total == 30 * MB


def test_every_executed_job_carries_maximum_bytes_billed(fake_bq_client):
    fake_bq_client.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_bq_client, ceiling=200 * MB)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    executed = [c for c in fake_bq_client.query_calls if not c.dry_run]
    assert executed
    for call in executed:
        assert call.job_config.maximum_bytes_billed == 200 * MB
        assert call.job_config.labels == {"app": "dex"}


def test_unconfirmed_execution_is_refused_before_any_run(fake_bq_client):
    adapter = make_adapter(fake_bq_client, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    # The free dry-run happened; nothing executed.
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True]


def test_over_ceiling_refused_client_side_without_execution(fake_bq_client):
    adapter = make_adapter(fake_bq_client, ceiling=1_000)  # below the 5000-byte scan
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_budget_below_the_billing_minimum_is_refused_with_the_math(fake_bq_client):
    adapter = make_adapter(fake_bq_client, ceiling=5 * MB)
    with pytest.raises(OverCeilingError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert "minimum" in str(exc_info.value)
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_server_side_cap_translates_when_the_estimate_drifts(fake_bq_client):
    # Dry-run underestimates 10x: the client-side gate passes, the (simulated)
    # server enforces maximum_bytes_billed, and the refusal is actionable.
    fake_bq_client.dry_run_underestimate = 0.1
    fake_bq_client.tables["test-proj.shop.customers"].num_bytes = 400 * MB
    adapter = make_adapter(fake_bq_client, ceiling=100 * MB)
    with pytest.raises(OverCeilingError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert "budget" in str(exc_info.value)


def test_timeout_cancels_the_job(fake_bq_client):
    fake_bq_client.result_error = TimeoutError("deadline")
    adapter = make_adapter(fake_bq_client)
    with pytest.raises(TimeoutError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=1,
        )
    assert "cancelled" in str(exc_info.value)
    assert fake_bq_client.cancelled_jobs


def test_run_query_truncates_and_shapes_columnar(fake_bq_client):
    fake_bq_client.row_resolver = lambda sql: [{"id": i} for i in range(5)]
    adapter = make_adapter(fake_bq_client)
    result = adapter.run_query(
        "SELECT id FROM `test-proj`.`shop`.`customers`",
        max_rows=3,
        timeout_seconds=30,
    )
    assert result.columns == ["id"]
    assert result.cells == [[0], [1], [2]]
    assert result.truncated is True


def test_billed_bytes_land_in_the_ledger(fake_bq_client):
    entries: list[dict] = []
    fake_bq_client.row_resolver = lambda sql: [{"n": 1}]
    adapter = make_adapter(fake_bq_client, record=entries.append)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    assert len(entries) == 1
    assert entries[0]["billed_bytes"] == 5_000
    assert entries[0]["connector"] == "bigquery"
    assert entries[0]["job_id"].startswith("fake-job")
    assert "SELECT" not in str(entries[0].values())


def test_session_remainder_binds_across_commands(fake_bq_client):
    # 5000-byte scan against a session budget with only 1000 bytes left today.
    adapter = make_adapter(
        fake_bq_client,
        ceiling=500 * MB,
        session_ceiling=100_000,
        session_spent=99_000,
    )
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


# --- profiling -------------------------------------------------------------------


def _aggregate_resolver(sql: str):
    # Answers any aggregate batch by alias; values chosen so id looks unique.
    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
    return [values]


def test_column_aggregates_profile_scalar_columns(fake_bq_client):
    fake_bq_client.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.shop.customers")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "test-proj.shop.customers", columns, safe_min_max={"id"}
        )
    }
    assert aggs["id"].is_unique is True
    assert aggs["id"].min_value == 1
    assert aggs["email"].min_value is None  # not in safe_min_max
    executed = [c.sql for c in fake_bq_client.query_calls if not c.dry_run]
    assert len(executed) == 1
    assert "APPROX_COUNT_DISTINCT" in executed[0]


def test_shape_stats_ride_the_aggregate_batch(fake_bq_client):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_bq_client.row_resolver = lambda sql: [
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
    adapter = make_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.shop.customers")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "test-proj.shop.customers", columns, shape_stats={"email"}
        )
    }
    sql = next(c.sql for c in fake_bq_client.query_calls if not c.dry_run)
    assert "REGEXP_CONTAINS(`email`, r'" in sql
    for alias in ("su_1", "sp_1", "st_1"):
        assert f" AS {alias}" in sql
    assert assert_select_only(sql, dialect="bigquery") == sql
    assert aggs["email"].upper_vocab_fraction == pytest.approx(0.8)
    assert aggs["email"].person_shape_fraction == pytest.approx(0.1)
    assert aggs["email"].avg_token_count == pytest.approx(2.5)
    # id was not requested: its shape fields stay None.
    assert aggs["id"].upper_vocab_fraction is None
    assert aggs["id"].person_shape_fraction is None
    assert aggs["id"].avg_token_count is None


def test_nested_and_repeated_columns_degrade_safely(fake_bq_client):
    fake_bq_client.row_resolver = _aggregate_resolver
    adapter = make_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.shop.events")
    aggs = {
        a.name: a
        for a in adapter.column_aggregates(
            "test-proj.shop.events", columns, safe_min_max=set()
        )
    }
    sql = next(c.sql for c in fake_bq_client.query_calls if not c.dry_run)
    # STRUCT: non-null count only, via COUNTIF; ARRAY: no aggregates at all.
    assert "COUNTIF(`payload` IS NOT NULL)" in sql
    assert "`labels`" not in sql
    assert aggs["payload"].distinct_count is None
    assert aggs["labels"].null_fraction is None


def test_partition_filter_tables_are_never_queried(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.logs.requests")
    aggs = adapter.column_aggregates(
        "test-proj.logs.requests", columns, safe_min_max=set()
    )
    assert all(a.null_fraction is None for a in aggs)
    assert fake_bq_client.query_calls == []
    assert any(
        "partition filter" in note
        for note in adapter.table_notes("test-proj.logs.requests")
    )


def test_profile_estimate_sums_free_dry_runs(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    total, per_table = adapter.profile_estimate(
        ["test-proj.shop.customers", "test-proj.shop.events", "test-proj.logs.requests"]
    )
    # Each queryable table is one below-floor batch (floors to the minimum)
    # plus a floor reserved for each of its two possible escalation queries
    # (both tables have >= 2 non-blob columns and > 0 rows): 30 MB apiece.
    assert per_table["test-proj.shop.customers"] == 30 * MB
    assert per_table["test-proj.shop.events"] == 30 * MB
    assert per_table["test-proj.logs.requests"] == 0.0  # unqueryable, skipped
    assert total == 60 * MB
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_profile_estimate_reserves_only_the_near_unique_floor_for_one_column(
    fake_bq_client,
):
    """A composite-key probe needs at least 2 columns to form a combination;
    a single-column table can never trigger one, so the estimate must not
    reserve for it (issue #107)."""

    from fakes.bigquery import FakeBigQueryClient, FakeTable

    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="single_col",
                schema=[bigquery.SchemaField("id", "INTEGER")],
                num_rows=100,
                num_bytes=5_000,
            )
        ],
    )
    adapter = make_adapter(client)
    total, per_table = adapter.profile_estimate(["test-proj.shop.single_col"])
    # 10 MB aggregate + 10 MB near-unique reserve only (no composite reserve).
    assert per_table["test-proj.shop.single_col"] == 20 * MB
    assert total == 20 * MB


def test_profile_estimate_skips_the_reserve_for_a_provably_empty_table(fake_bq_client):
    """Neither escalation query ever runs against zero rows (both bail out on
    a falsy row count), so a table known to be empty at estimate time needs no
    reserve (issue #107)."""

    from fakes.bigquery import FakeBigQueryClient, FakeTable

    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="empty_table",
                schema=[
                    bigquery.SchemaField("id", "INTEGER"),
                    bigquery.SchemaField("email", "STRING"),
                ],
                num_rows=0,
                num_bytes=0,
            )
        ],
    )
    adapter = make_adapter(client)
    total, per_table = adapter.profile_estimate(["test-proj.shop.empty_table"])
    # Aggregate batch floor only; no escalation reserve for a known-empty table.
    assert per_table["test-proj.shop.empty_table"] == 10 * MB
    assert total == 10 * MB


def test_profile_estimate_still_reserves_for_a_view_with_unknown_row_count(
    fake_bq_client,
):
    """A view's row count is never known before the aggregate that reveals it
    (BigQuery reports no stored row count for a view), so 'unknown' must still
    reserve for escalation rather than being mistaken for 'empty' (issue #107).
    """

    from fakes.bigquery import FakeBigQueryClient, FakeTable

    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="a_view",
                schema=[
                    bigquery.SchemaField("id", "INTEGER"),
                    bigquery.SchemaField("email", "STRING"),
                ],
                num_rows=12345,  # ignored for a view; _object_meta nulls it out
                num_bytes=0,
                table_type="VIEW",
            )
        ],
    )
    adapter = make_adapter(client)
    total, per_table = adapter.profile_estimate(["test-proj.shop.a_view"])
    # Full reserve still applies despite the unknown (None) row count.
    assert per_table["test-proj.shop.a_view"] == 30 * MB
    assert total == 30 * MB


def _blob_fake_client():
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    tables = [
        FakeTable(
            project="test-proj",
            dataset_id="raw",
            table_id="sessions",
            schema=[
                bigquery.SchemaField("id", "INTEGER"),
                bigquery.SchemaField("payload", "BYTES"),
            ],
            num_rows=100,
            num_bytes=5_000,
        ),
    ]
    return FakeBigQueryClient(project="test-proj", tables=tables)


def test_profile_estimate_excludes_blob_columns_by_default():
    client = _blob_fake_client()
    adapter = make_adapter(client)
    adapter.profile_estimate(["test-proj.raw.sessions"])
    dry_run_sql = next(c.sql for c in client.query_calls if c.dry_run)
    assert "`payload`" not in dry_run_sql


def test_profile_estimate_include_blobs_override_restores_the_column():
    client = _blob_fake_client()
    adapter = make_adapter(client)
    adapter.profile_estimate(
        ["test-proj.raw.sessions"],
        include_blobs={"test-proj.raw.sessions.payload"},
    )
    dry_run_sql = next(c.sql for c in client.query_calls if c.dry_run)
    assert "`payload`" in dry_run_sql


def test_exact_distinct_counts_degrade_when_budget_cannot_cover(fake_bq_client):
    fake_bq_client.row_resolver = lambda sql: [{"d_0": 100}]
    adapter = make_adapter(fake_bq_client, ceiling=100 * MB)
    # Consume nearly the whole budget so the escalation cannot fit.
    adapter.cost_gate.charge(100 * MB - 1_000)
    result = adapter.exact_distinct_counts("test-proj.shop.customers", ["id"])
    assert result == {}
    assert any(
        "escalation skipped" in note
        for note in adapter.table_notes("test-proj.shop.customers")
    )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_distinct_combination_counts_batch_into_one_guarded_statement(fake_bq_client):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    fake_bq_client.row_resolver = lambda sql: [{"d_0": 90, "d_1": 100}]
    adapter = make_adapter(fake_bq_client)
    counts = adapter.distinct_combination_counts(
        "test-proj.shop.customers", [["id", "email"], ["email", "id"]]
    )
    assert counts == {("id", "email"): 90, ("email", "id"): 100}
    billed = [c for c in fake_bq_client.query_calls if not c.dry_run]
    assert len(billed) == 1
    sql = billed[0].sql
    assert "SELECT DISTINCT" in sql
    assert assert_select_only(sql, dialect="bigquery") == sql
    assert adapter.distinct_combination_counts("test-proj.shop.customers", []) == {}


def test_distinct_combination_counts_degrade_when_budget_cannot_cover(fake_bq_client):
    adapter = make_adapter(fake_bq_client, ceiling=100 * MB)
    adapter.cost_gate.charge(100 * MB - 1_000)
    result = adapter.distinct_combination_counts(
        "test-proj.shop.customers", [["id", "email"]]
    )
    assert result == {}
    assert any(
        "composite-key probe skipped" in note
        for note in adapter.table_notes("test-proj.shop.customers")
    )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_sampling_kicks_in_above_the_threshold_and_voids_uniqueness(fake_bq_client):
    fake_bq_client.row_resolver = _aggregate_resolver
    adapter = make_adapter(
        fake_bq_client, target=BigQueryTarget(max_full_profile_bytes=10_000)
    )
    _meta, columns = adapter.table_metadata("test-proj.shop.events")
    aggs = adapter.column_aggregates(
        "test-proj.shop.events", columns, safe_min_max=set()
    )
    sql = next(c.sql for c in fake_bq_client.query_calls if not c.dry_run)
    assert "TABLESAMPLE SYSTEM" in sql
    assert all(a.is_unique is None for a in aggs)
    assert any(
        "block sample" in note for note in adapter.table_notes("test-proj.shop.events")
    )


# --- factory and dialect ----------------------------------------------------------


def test_get_adapter_wires_bigquery(fake_bq_client):
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=100 * MB,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
    )
    adapter = get_adapter(
        "bigquery",
        project="test-proj",
        cost_gate=gate,
        client=fake_bq_client,
    )
    assert adapter.name == "bigquery"
    adapter.close()
    assert fake_bq_client.closed is True


def test_get_dialect_resolves_without_clients():
    assert get_dialect("bigquery") == "bigquery"
    assert get_dialect("duckdb") == "duckdb"
    assert get_dialect("nonsense") == "duckdb"


# --- project resolution (connect.py) -----------------------------------------------


def test_project_resolution_order(tmp_path: Path):
    from exmergo_dex_core.connect import resolve_bigquery_project

    target = BigQueryTarget(project="explicit")
    env = {"GOOGLE_CLOUD_PROJECT": "from-env"}
    assert (
        resolve_bigquery_project(target, env, "from-adc", repo_root=tmp_path)
        == "explicit"
    )
    assert (
        resolve_bigquery_project(BigQueryTarget(), env, "from-adc", repo_root=tmp_path)
        == "from-env"
    )
    assert (
        resolve_bigquery_project(BigQueryTarget(), {}, "from-adc", repo_root=tmp_path)
        == "from-adc"
    )
    assert (
        resolve_bigquery_project(BigQueryTarget(), {}, None, repo_root=tmp_path) is None
    )


def test_project_falls_back_to_dbt_profiles(dbt_project_dir: Path):
    from exmergo_dex_core.connect import resolve_bigquery_project

    profiles = dbt_project_dir / "profiles.yml"
    profiles.write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: from-dbt\n"
        "      dataset: dbt_dev\n",
        encoding="utf-8",
    )
    resolved = resolve_bigquery_project(
        BigQueryTarget(), {}, None, repo_root=dbt_project_dir.parent
    )
    assert resolved == "from-dbt"


def test_connect_test_parses_project_and_dataset_flags():
    # The first-attempt flags a user reaches for must not be rejected as
    # unrecognized args (the discoverability defect).
    from exmergo_dex_core.cli import _build_parser

    args = _build_parser().parse_args(
        [
            "connect",
            "test",
            "--connector",
            "bigquery",
            "--project",
            "p",
            "--dataset",
            "d1",
            "--dataset",
            "d2",
        ]
    )
    assert args.project == "p"
    assert args.dataset == ["d1", "d2"]


def test_project_and_dataset_flags_override_the_config_target(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import exmergo_dex_core.connect as connect_mod

    captured: dict = {}
    monkeypatch.setattr(
        connect_mod, "_default_credentials", lambda: (None, None, "user")
    )

    def fake_get_adapter(name, **kwargs):
        captured.update(kwargs)
        captured["name"] = name
        return SimpleNamespace(name=name, close=lambda: None)

    monkeypatch.setattr(connect_mod, "get_adapter", fake_get_adapter)

    connect_mod.open_adapter(
        connector="bigquery",
        project="cli-proj",
        datasets=["ds_a", "ds_b"],
        repo_root=tmp_path,  # no .dex/config.yml here
    )
    assert captured["name"] == "bigquery"
    assert captured["project"] == "cli-proj"
    assert captured["target"].project == "cli-proj"
    assert captured["target"].datasets == ["ds_a", "ds_b"]


# --- scope resolution: an entry that names nothing is refused, not dropped ------------


def test_nonexistent_dataset_scope_is_refused_and_names_what_exists(fake_bq_client):
    """The cost-safety bug: a scope that resolves to nothing used to reach
    list_tables and die on a raw google NotFound, naming neither the fix nor the
    datasets that do exist."""

    adapter = make_adapter(
        fake_bq_client,
        target=BigQueryTarget(datasets=["no_such_dataset"]),
    )
    with pytest.raises(BigQueryConnectionError) as exc:
        adapter.list_objects()
    message = str(exc.value)
    assert "no_such_dataset" in message
    assert "shop" in message and "logs" in message  # the datasets that do exist
    assert "[from bigquery.datasets in .dex/config.yml]" in message
    assert fake_bq_client.query_calls == []


def test_scope_refusal_blames_the_flag_it_came_from(fake_bq_client):
    """`--dataset` and `--scope` both scope BigQuery, and narrow_target copies
    either one over the committed allowlist, so the adapter is told which."""

    adapter = make_adapter(
        fake_bq_client,
        target=BigQueryTarget(datasets=["nope"]),
        scope_origin="--dataset",
    )
    with pytest.raises(BigQueryConnectionError, match=r"\[from --dataset\]"):
        adapter.list_objects()


def test_a_table_shaped_scope_is_refused(fake_bq_client):
    adapter = make_adapter(
        fake_bq_client,
        target=BigQueryTarget(datasets=["test-proj.shop.customers"]),
    )
    with pytest.raises(BigQueryConnectionError, match="never a table"):
        adapter.list_objects()


def test_a_valid_scope_still_bounds_the_inventory(fake_bq_client):
    adapter = make_adapter(fake_bq_client, target=BigQueryTarget(datasets=["shop"]))
    assert [o.identifier for o in adapter.list_objects()] == [
        "test-proj.shop.customers",
        "test-proj.shop.events",
    ]
    assert fake_bq_client.query_calls == []


# --- the dev-target preflight (free) --------------------------------------------------


def test_a_missing_dev_dataset_is_reported_not_raised(fake_bq_client):
    """dbt-bigquery creates its dev dataset (CREATE SCHEMA IF NOT EXISTS), so an
    absent one is the normal state before a first build: the caller warns rather
    than refusing, unlike Snowflake and Databricks."""

    adapter = make_adapter(fake_bq_client)
    assert adapter.missing_dev_namespaces("dbt_dev") == [
        'dev_dataset "test-proj.dbt_dev"'
    ]
    assert fake_bq_client.query_calls == []


def test_an_existing_dev_dataset_is_fine(fake_bq_client):
    fake_bq_client.empty_datasets.add("test-proj.dbt_dev")
    adapter = make_adapter(fake_bq_client)
    assert adapter.missing_dev_namespaces("dbt_dev") == []
    assert fake_bq_client.query_calls == []


def test_an_unreachable_dev_project_is_raised(fake_bq_client):
    """What dbt cannot create for itself: it makes datasets, never projects. The
    listing that distinguishes the two is lazy in the real client, so a project
    that is merely never iterated would read as a healthy one."""

    adapter = make_adapter(fake_bq_client)
    with pytest.raises(BigQueryConnectionError) as exc:
        adapter.missing_dev_namespaces("no-such-project.dbt_dev")
    message = str(exc.value)
    assert "no-such-project" in message
    assert "dbt creates datasets but never projects" in message
    assert fake_bq_client.query_calls == []


def test_list_namespace_objects_lists_one_dataset_from_metadata(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    assert adapter.list_namespace_objects("shop") == ["customers", "events"]
    # Bare names qualify against the adapter's project; absence reads as empty.
    assert adapter.list_namespace_objects("not_there") == []
    assert fake_bq_client.query_calls == []
