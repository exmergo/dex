"""The BigQuery adapter against the stateful fake client: metadata is free,
every billed statement is dry-run first, and the budget binds at both the
client and the (simulated) server."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core.adapters import get_adapter, get_dialect
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
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


def test_estimate_is_the_dry_run_figure(fake_bq_client):
    adapter = make_adapter(fake_bq_client)
    estimate = adapter.query_estimate(
        "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`"
    )
    assert estimate == 5_000  # the table's num_bytes, priced by the fake
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True]


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
    assert per_table["test-proj.shop.customers"] == 5_000
    assert per_table["test-proj.shop.events"] == 50_000
    assert per_table["test-proj.logs.requests"] == 0.0  # unqueryable, skipped
    assert total == 55_000
    assert all(c.dry_run for c in fake_bq_client.query_calls)


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


def test_remaining_cloud_connectors_still_stub():
    for connector in ("snowflake", "databricks", "postgres"):
        with pytest.raises(NotImplementedError):
            get_adapter(connector)


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
