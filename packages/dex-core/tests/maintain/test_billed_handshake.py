"""The per-axis cost model on a billed connector: schema and volume stay free,
grain (and check's scanning phase) goes through the confirm handshake, and the
free phase of a two-phase command completes before any confirmation."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core import command_args
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexStore
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    CeilingRequiredError,
    CostGate,
    OverCeilingError,
)
from exmergo_dex_core.maintain import commands as maintain_cmds
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

MB = 1024 * 1024


def _adapter(fake_bq_client, *, confirmed: bool, budget: float | None, record=None):
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=budget,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="bigquery",
        command="maintain",
        record=record,
    )
    return BigQueryAdapter(
        project="test-proj",
        cost_gate=gate,
        target=BigQueryTarget(),
        client=fake_bq_client,
        principal_type="user",
    )


def _args(tmp_path: Path, subcommand: str, **extra) -> argparse.Namespace:
    base = {
        "connector": "bigquery",
        "path": None,
        "repo_root": str(tmp_path),
        "confirm": False,
        "budget": None,
        "group": "maintain",
        "subcommand": subcommand,
        "objects": [],
    }
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture
def route_adapter(monkeypatch):
    def install(fake_client, record=None):
        def opener(args):
            return _adapter(
                fake_client,
                confirmed=getattr(args, "confirm", False),
                budget=getattr(args, "budget", None),
                record=record,
            )

        monkeypatch.setattr(command_args, "open_from_args", opener)

    return install


def _seed_snapshot(tmp_path: Path, *, extra_baseline_column: bool = False) -> None:
    """A BigQuery-shaped baseline: customers with a proven unique key (the
    grain target), optionally with a column the fake's current schema lacks
    (an induced schema drift)."""

    columns = [
        ColumnProfile(
            name="id",
            data_type="INTEGER",
            nullable=False,
            null_fraction=0.0,
            distinct_count=100,
            distinct_count_exact=True,
            is_unique=True,
        ),
        ColumnProfile(name="email", data_type="STRING"),
    ]
    if extra_baseline_column:
        columns.append(ColumnProfile(name="phone", data_type="STRING"))
    now = datetime.now(UTC).isoformat()
    DexStore(tmp_path).save_snapshot(
        Snapshot(
            created_at=now,
            connector="bigquery",
            warehouse=WarehouseBaseline(
                datasets=[
                    Dataset(
                        identifier="test-proj.shop.customers",
                        row_count=100,
                        byte_size=5_000,
                        columns=columns,
                        candidate_keys=[["id"]],
                        grain=["id"],
                        profiled_at=now,
                    )
                ]
            ),
            warehouse_from="cache",
        )
    )


def test_schema_and_volume_run_free_on_bigquery(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path, extra_baseline_column=True)
    route_adapter(fake_bq_client)

    envelope = maintain_cmds.cmd_schema(_args(tmp_path, "schema"))
    assert envelope.status.value == "ok"
    dropped = [f for f in envelope.data["findings"] if f["code"] == "column_dropped"]
    assert dropped and dropped[0]["column"] == "phone"
    # Structural detection is metadata-only: not even a dry-run job was issued.
    assert fake_bq_client.query_calls == []

    envelope = maintain_cmds.cmd_volume(_args(tmp_path, "volume"))
    assert envelope.status.value == "ok"
    assert fake_bq_client.query_calls == []


def test_unconfirmed_grain_returns_the_dry_run_estimate(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path)
    route_adapter(fake_bq_client)

    envelope = maintain_cmds.cmd_grain(_args(tmp_path, "grain"))
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # The single-table distinct-count scan floors to the per-query minimum.
    assert envelope.cost.estimate == 10 * MB
    assert envelope.data["per_table_bytes"] == {"test-proj.shop.customers": 10 * MB}
    assert "--confirm" in envelope.data["hint"]
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_confirmed_grain_scans_within_budget_and_ledgers(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path)
    entries: list[dict] = []
    fake_bq_client.row_resolver = lambda sql: [{"d_0": 90}]
    route_adapter(fake_bq_client, record=entries.append)

    envelope = maintain_cmds.cmd_grain(
        _args(tmp_path, "grain", confirm=True, budget=float(100 * MB))
    )
    assert envelope.status.value == "ok"
    finding = envelope.data["findings"][0]
    assert finding["code"] == "key_lost_uniqueness"
    assert finding["data"]["distinct_count"] == 90
    assert finding["data"]["row_count"] == 100
    assert envelope.data["spend"]["bytes_billed"] == 5_000
    assert [e["billed_bytes"] for e in entries] == [5_000]
    # The command estimate and the adapter's per-statement charge are free
    # dry-runs; exactly one execution follows.
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True, True, False]


def test_confirmed_grain_without_a_budget_is_refused(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path)
    route_adapter(fake_bq_client)
    with pytest.raises(CeilingRequiredError):
        maintain_cmds.cmd_grain(_args(tmp_path, "grain", confirm=True))
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_over_ceiling_grain_cannot_be_confirmed_through(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path)
    route_adapter(fake_bq_client)
    with pytest.raises(OverCeilingError):
        maintain_cmds.cmd_grain(_args(tmp_path, "grain", confirm=True, budget=1_000.0))
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_unconfirmed_check_is_two_phase(fake_bq_client, route_adapter, tmp_path):
    _seed_snapshot(tmp_path, extra_baseline_column=True)
    route_adapter(fake_bq_client)

    envelope = maintain_cmds.cmd_check(_args(tmp_path, "check"))
    assert envelope.status.value == "needs_confirmation"
    # Phase one is complete and returned: the free axes' findings ride along
    # with the estimate for the scanning axes.
    codes = {f["code"] for f in envelope.data["findings"]}
    assert "column_dropped" in codes
    assert envelope.data["estimated_bytes"] == 10 * MB
    assert any("free" in note for note in envelope.data["notes"])
    assert all(c.dry_run for c in fake_bq_client.query_calls)

    # The free axes are already persisted for reconcile; grain waits.
    report = DexStore(tmp_path).load_drift()
    assert "schema" in report.axes and "grain" not in report.axes


def test_confirmed_check_completes_the_scanning_axes(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_snapshot(tmp_path)
    fake_bq_client.row_resolver = lambda sql: [{"d_0": 100}]
    route_adapter(fake_bq_client)

    envelope = maintain_cmds.cmd_check(
        _args(tmp_path, "check", confirm=True, budget=float(100 * MB))
    )
    assert envelope.status.value == "ok"
    assert envelope.data["axes"]["grain"] == 0
    report = DexStore(tmp_path).load_drift()
    assert {"schema", "volume", "grain"} <= set(report.axes)


def _seed_two_keyed_tables(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()

    def keyed(identifier: str, rows: int, byte_size: int) -> Dataset:
        return Dataset(
            identifier=identifier,
            row_count=rows,
            byte_size=byte_size,
            columns=[
                ColumnProfile(
                    name="id",
                    data_type="INTEGER",
                    nullable=False,
                    null_fraction=0.0,
                    distinct_count=rows,
                    distinct_count_exact=True,
                    is_unique=True,
                )
            ],
            candidate_keys=[["id"]],
            grain=["id"],
            profiled_at=now,
        )

    DexStore(tmp_path).save_snapshot(
        Snapshot(
            created_at=now,
            connector="bigquery",
            warehouse=WarehouseBaseline(
                datasets=[
                    keyed("test-proj.shop.customers", 100, 5_000),
                    keyed("test-proj.shop.events", 1_000, 50_000),
                ]
            ),
            warehouse_from="cache",
        )
    )


def test_check_fanout_estimate_reflects_per_query_floor(
    fake_bq_client, route_adapter, tmp_path
):
    """A fan-out check over two tables estimates 2x the per-query floor, not the
    few KB of raw scan; confirming with exactly that budget then runs without the
    per-statement floor rejecting a statement (the reject-ladder the estimate
    used to cause)."""

    _seed_two_keyed_tables(tmp_path)
    route_adapter(fake_bq_client)

    unconfirmed = maintain_cmds.cmd_check(_args(tmp_path, "check"))
    assert unconfirmed.status.value == "needs_confirmation"
    assert unconfirmed.data["estimated_bytes"] == 2 * 10 * MB

    fake_bq_client.row_resolver = lambda sql: [{"d_0": 100}]
    route_adapter(fake_bq_client)
    confirmed = maintain_cmds.cmd_check(
        _args(
            tmp_path,
            "check",
            confirm=True,
            budget=float(unconfirmed.data["estimated_bytes"]),
        )
    )
    assert confirmed.status.value == "ok"
