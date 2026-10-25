"""One statement per table for the drift probes on BigQuery (#275).

BigQuery bills a 10 MB minimum per statement and per table a statement
references, so a drift sweep that asked one table for its key distincts, its
composite grain, and its null fractions in three statements paid that minimum
three times for answers worth kilobytes. Batching folds them into one statement
per table. These tests hold the two things that change make worth anything: the
bill drops to one floor per table, and the findings are exactly the findings
the separate statements produced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("google.cloud.bigquery")

from google.cloud import bigquery

from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import CostGate
from exmergo_dex_core.maintain import drift as drift_mod
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

MB = 1024 * 1024
ORDERS = "test-proj.shop.orders"


def _client(*, num_bytes: int = 5_000):
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    table = FakeTable(
        project="test-proj",
        dataset_id="shop",
        table_id="orders",
        schema=[
            bigquery.SchemaField("id", "INTEGER"),
            bigquery.SchemaField("order_id", "INTEGER"),
            bigquery.SchemaField("line_number", "INTEGER"),
            bigquery.SchemaField("status", "STRING"),
            bigquery.SchemaField("note", "STRING"),
        ],
        num_rows=100,
        num_bytes=num_bytes,
    )
    client = FakeBigQueryClient(project="test-proj", tables=[table])
    client.row_resolver = _resolve
    return client


def _resolve(sql: str) -> list[dict]:
    """Answer the separate probes and the batched one with the same numbers: a
    key with 10 duplicates, a composite with 20, a status column whose distinct
    count moved, and a note column that is entirely NULL."""

    key, combination, status = 90, 80, 7
    if "kd_0" in sql:  # the batched statement
        row = _survey_row()
        row.update(kd_0=key, kd_1=status, cd_0=combination)
        return [row]
    if "n_total" in sql:  # a separate null survey
        return [_survey_row()]
    if "SELECT DISTINCT" in sql:  # a separate combination probe
        return [{"d_0": combination}]
    # A separate exact distinct count: the grain key, or the dimension column.
    return [{"d_0": status if "`status`" in sql else key}]


def _survey_row() -> dict:
    row = {"n_total": 100}
    for index in range(5):
        row[f"nn_{index}"] = 0 if index == 4 else 100
        row[f"nd_{index}"] = 50
    return row


def _adapter(client, *, budget: float = float(500 * MB), sample_bytes=None):
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=budget,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
        command="maintain",
    )
    return BigQueryAdapter(
        project="test-proj",
        cost_gate=gate,
        target=BigQueryTarget(max_full_profile_bytes=sample_bytes),
        client=client,
        principal_type="user",
    )


def _snapshot() -> Snapshot:
    now = datetime.now(UTC).isoformat()
    return Snapshot(
        created_at=now,
        connector="bigquery",
        warehouse=WarehouseBaseline(
            datasets=[
                Dataset(
                    identifier=ORDERS,
                    row_count=100,
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER"),
                        ColumnProfile(name="order_id", data_type="INTEGER"),
                        ColumnProfile(name="line_number", data_type="INTEGER"),
                        ColumnProfile(
                            name="status",
                            data_type="STRING",
                            distinct_count=5,
                            distinct_count_exact=True,
                        ),
                        ColumnProfile(name="note", data_type="STRING"),
                    ],
                    candidate_keys=[["id"], ["order_id", "line_number"]],
                    profiled_at=now,
                )
            ]
        ),
        warehouse_from="cache",
    )


def _cardinality_checks() -> list[drift_mod.CardinalityCheck]:
    return [
        drift_mod.CardinalityCheck(
            identifier=ORDERS,
            column="status",
            dimension="order_status",
            semantic_model="orders",
            baseline_distinct=5,
            baseline_exact=True,
        )
    ]


def _sweep(adapter, *, batch: bool):
    """What `maintain check` runs for the scanning axes, batched or not."""

    plan = drift_mod.grain_plan(adapter, _snapshot())
    checks = _cardinality_checks()
    batched = drift_mod.batched_probes(adapter, plan, checks) if batch else {}
    grain_total, _ = drift_mod.grain_estimate(adapter, plan, batched)
    card_total, _ = drift_mod.cardinality_estimate(adapter, checks, batched)
    probes = drift_mod.run_batched_probes(adapter, batched)
    findings = drift_mod.grain_drift(adapter, plan, probes=probes)
    findings += drift_mod.cardinality_drift(adapter, checks, None, probes)
    return findings, grain_total + card_total


def _executed(client) -> list[str]:
    return [call.sql for call in client.query_calls if not call.dry_run]


def _dump(findings) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings], sort_keys=True)


def test_batched_findings_are_byte_identical_to_the_separate_probes():
    separate_client, batched_client = _client(), _client()

    separate, _ = _sweep(_adapter(separate_client), batch=False)
    batched, _ = _sweep(_adapter(batched_client), batch=True)

    # Every probe kind contributed, so the comparison covers all of them.
    codes = {f.code for f in separate}
    assert {
        "key_lost_uniqueness",
        "fully_null_column",
        "dimension_cardinality_changed",
    } <= codes
    assert any(f.column == "order_id, line_number" for f in separate)
    assert _dump(batched) == _dump(separate)
    # Four statements (key, composite, null survey, dimension) became one.
    assert len(_executed(separate_client)) == 4
    assert len(_executed(batched_client)) == 1


def test_the_batched_estimate_is_one_floor_and_prices_what_runs():
    separate_client, batched_client = _client(), _client()

    _findings, separate_estimate = _sweep(_adapter(separate_client), batch=False)
    _findings, batched_estimate = _sweep(_adapter(batched_client), batch=True)

    # The separate probes floor three times and reserve a profile's
    # escalations on top; the batched statement floors once.
    assert batched_estimate == 10 * MB
    assert separate_estimate > 3 * batched_estimate
    # The statement the handshake priced is the statement that ran.
    (executed,) = _executed(batched_client)
    priced = [call.sql for call in batched_client.query_calls if call.dry_run]
    assert executed in priced


def test_a_table_with_one_probe_runs_what_it_ran_before():
    """Nothing to fold, so nothing changes: a table owing one statement is left
    to its own probe."""

    client = _client()
    adapter = _adapter(client)
    snap = _snapshot()
    snap.warehouse.datasets[0].candidate_keys = [["id"]]
    plan = drift_mod.grain_plan(adapter, snap)
    plan = plan._replace(null_checks=[])

    assert drift_mod.batched_probes(adapter, plan) == {}
    drift_mod.grain_drift(adapter, plan)
    (executed,) = _executed(client)
    assert "kd_0" not in executed


def test_a_sampled_table_keeps_its_null_survey_apart():
    """A table over `max_full_profile_bytes` is surveyed from a block sample,
    and the distinct counts must read every row, so the survey cannot share
    their statement. The rest still folds into one."""

    client = _client(num_bytes=50_000_000)
    adapter = _adapter(client, sample_bytes=1_000_000)

    findings, _ = _sweep(adapter, batch=True)

    executed = _executed(client)
    assert len(executed) == 2
    batched = [sql for sql in executed if "kd_0" in sql]
    survey = [sql for sql in executed if "TABLESAMPLE" in sql]
    assert len(batched) == 1 and len(survey) == 1
    assert "TABLESAMPLE" not in batched[0]
    assert "key_lost_uniqueness" in {f.code for f in findings}


def test_a_budget_that_cannot_cover_the_batch_falls_back_to_the_separate_probes():
    """Declining the batch must leave each probe its own degradation: the key
    check skips with its note rather than the whole table going unmeasured."""

    client = _client()
    adapter = _adapter(client, budget=float(5 * MB))

    plan = drift_mod.grain_plan(adapter, _snapshot())
    plan = plan._replace(null_checks=[])
    batched = drift_mod.batched_probes(adapter, plan)
    assert set(batched) == {ORDERS}

    assert drift_mod.run_batched_probes(adapter, batched) == {}
    drift_mod.grain_drift(adapter, plan, probes={})
    assert _executed(client) == []
    assert any(
        "distinct-count escalation skipped" in note
        for note in adapter.table_notes(ORDERS)
    )


EVENTS = "test-proj.shop.events"


def _events_client():
    """A table baselined with one tenant, where `day` then lost uniqueness and
    `(tenant, day)` still holds. Only the batched statement may run."""

    from fakes.bigquery import FakeBigQueryClient, FakeTable

    table = FakeTable(
        project="test-proj",
        dataset_id="shop",
        table_id="events",
        schema=[
            bigquery.SchemaField("id", "INTEGER"),
            bigquery.SchemaField("tenant", "STRING"),
            bigquery.SchemaField("day", "INTEGER"),
        ],
        num_rows=100,
        num_bytes=5_000,
    )
    client = FakeBigQueryClient(project="test-proj", tables=[table])

    def resolve(sql: str) -> list[dict]:
        assert "kd_0" in sql, f"only the batched statement should run: {sql}"
        row = {"n_total": 100, "kd_0": 50, "kd_1": 100, "cd_0": 100}
        for index in range(3):
            row[f"nn_{index}"] = 100
            row[f"nd_{index}"] = 50
        return [row]

    client.row_resolver = resolve
    return client


def _events_snapshot() -> Snapshot:
    now = datetime.now(UTC).isoformat()
    return Snapshot(
        created_at=now,
        connector="bigquery",
        warehouse=WarehouseBaseline(
            datasets=[
                Dataset(
                    identifier=EVENTS,
                    row_count=100,
                    columns=[
                        ColumnProfile(
                            name="id", data_type="INTEGER", distinct_count=100
                        ),
                        ColumnProfile(
                            name="tenant", data_type="STRING", distinct_count=1
                        ),
                        ColumnProfile(
                            name="day", data_type="INTEGER", distinct_count=100
                        ),
                    ],
                    candidate_keys=[["id"], ["day"]],
                    grain=["id"],
                    profiled_at=now,
                )
            ]
        ),
        warehouse_from="cache",
    )


def test_the_widening_of_a_lost_key_rides_the_tables_one_statement():
    """#780 on BigQuery: the combination that widens a lost incidental key is
    one more scalar subquery in the table's batched statement, so it adds no
    statement and no second billing floor."""

    client = _events_client()
    adapter = _adapter(client)
    plan = drift_mod.grain_plan(adapter, _events_snapshot())
    assert [combos for _ds, combos, _rows in plan.widening_checks] == [
        [["tenant", "day"]]
    ]

    batched = drift_mod.batched_probes(adapter, plan)
    assert batched[EVENTS].combinations == [["tenant", "day"]]
    estimate, _per_table = drift_mod.grain_estimate(adapter, plan, batched)
    assert estimate == 10 * MB

    findings = drift_mod.grain_drift(
        adapter, plan, probes=drift_mod.run_batched_probes(adapter, batched)
    )
    (finding,) = [f for f in findings if f.code == "key_lost_uniqueness"]
    assert finding.column == "day"
    assert finding.severity == "medium"
    assert finding.data["still_unique_as"] == ["tenant", "day"]
    # The grain's distinct count is a key the same statement already carried.
    assert finding.data["grain_holds"] is True
    assert len(_executed(client)) == 1
