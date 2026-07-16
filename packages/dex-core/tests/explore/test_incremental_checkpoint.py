"""Incremental cache persistence for billed profiling runs.

When a billed connector exhausts its budget partway through a profiling pass, the
cost gate raises mid-run. Every explore command that profiles (`map`,
`relationships`, `profile`) must still leave the objects already paid for in
`.dex/cache.json`, and report how many of how many objects were saved. A fully
successful run overwrites those checkpoints with the authoritative composed cache
(relationships + ranking + carry-forward). DuckDB, being free, never checkpoints.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from google.cloud import bigquery

from exmergo_dex_core import command_args
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.cache import DexStore
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.explore import commands as explore_cmds
from exmergo_dex_core.guards.cost_guard import CostGate, OverCeilingError

MB = 1024 * 1024


def _aggregate_resolver(sql: str):
    """Answer the column-aggregate / exact-distinct SQL with unique-looking
    numbers (mirrors test_billed_handshake's resolver), enough for a full
    profile of a small table to complete and be checkpointed."""

    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
        values[f"d_{i}"] = 100
    values["nonnull_fk"] = 100
    values["orphans"] = 0
    return [values]


@pytest.fixture
def two_table_client():
    """A deterministic two-table billed warehouse: `customers` (a clean surrogate
    key) and `orders` (a customer_id foreign key), both cheap and queryable so a
    map/relationships/profile pass profiles exactly two objects in a known order."""

    from fakes.bigquery import FakeBigQueryClient, FakeTable

    tables = [
        FakeTable(
            project="test-proj",
            dataset_id="shop",
            table_id="customers",
            schema=[
                bigquery.SchemaField("id", "INTEGER", mode="REQUIRED"),
                bigquery.SchemaField("email", "STRING"),
            ],
            num_rows=100,
            num_bytes=5_000,
        ),
        FakeTable(
            project="test-proj",
            dataset_id="shop",
            table_id="orders",
            schema=[
                bigquery.SchemaField("id", "INTEGER", mode="REQUIRED"),
                bigquery.SchemaField("customer_id", "INTEGER"),
            ],
            num_rows=100,
            num_bytes=5_000,
        ),
    ]
    client = FakeBigQueryClient(project="test-proj", tables=tables)
    client.row_resolver = _aggregate_resolver
    return client


def _install(
    monkeypatch, client, *, confirmed=True, budget=float(100 * MB), record=None
):
    def opener(args):
        gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=budget,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=confirmed,
            connector="bigquery",
            command="explore",
            record=record,
        )
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=gate,
            target=BigQueryTarget(),
            client=client,
            principal_type="user",
        )

    monkeypatch.setattr(command_args, "open_from_args", opener)


def _raise_on_nth_object(monkeypatch, n: int):
    """Make the (n-th) object's column_aggregates raise OverCeilingError, exactly
    as the cost gate's charge() would when the accumulated estimate crosses the
    ceiling. Because charge() fires before the Dataset is appended, the objects
    before the n-th are fully profiled and checkpointed; the n-th is not."""

    original = BigQueryAdapter.column_aggregates
    state = {"count": 0}

    def wrapped(self, identifier, columns, *, safe_min_max=None, shape_stats=None):
        state["count"] += 1
        if state["count"] >= n:
            raise OverCeilingError("simulated ceiling crossed mid-run")
        return original(
            self,
            identifier,
            columns,
            safe_min_max=safe_min_max,
            shape_stats=shape_stats,
        )

    monkeypatch.setattr(BigQueryAdapter, "column_aggregates", wrapped)
    return state


def _args(tmp_path: Path, **extra) -> argparse.Namespace:
    base = {
        "connector": "bigquery",
        "path": None,
        "repo_root": str(tmp_path),
        "confirm": True,
        "budget": float(100 * MB),
        "group": "explore",
    }
    base.update(extra)
    return argparse.Namespace(**base)


# --- 1. mid-run budget exhaustion leaves a partial cache ---------------------


def test_map_mid_run_exhaustion_saves_partial_cache(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)
    _raise_on_nth_object(monkeypatch, 2)  # object 1 completes, object 2 trips the gate

    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))

    assert envelope.status.value == "error"
    message = envelope.errors[0]
    assert "1 of 2" in message
    assert ".dex" in message and "cache.json" in message
    # Spend was stamped after the adapter closed (the finally ran).
    assert "spend" in envelope.data

    cache = DexStore(tmp_path).load_cache()
    assert cache is not None
    assert len(cache.datasets) == 1
    # The paid-for profile is real: columns are present on the checkpointed object.
    assert cache.datasets[0].columns


def test_relationships_mid_run_exhaustion_saves_partial_cache(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)
    _raise_on_nth_object(monkeypatch, 2)

    envelope = explore_cmds.cmd_relationships(
        _args(tmp_path, subcommand="relationships")
    )

    assert envelope.status.value == "error"
    assert "1 of 2" in envelope.errors[0]
    cache = DexStore(tmp_path).load_cache()
    assert cache is not None and len(cache.datasets) == 1
    assert cache.datasets[0].columns


def test_profile_mid_run_exhaustion_saves_partial_cache(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)
    _raise_on_nth_object(monkeypatch, 2)

    envelope = explore_cmds.cmd_profile(
        _args(tmp_path, subcommand="profile", objects=["customers", "orders"])
    )

    assert envelope.status.value == "error"
    assert "1 of 2" in envelope.errors[0]
    cache = DexStore(tmp_path).load_cache()
    assert cache is not None and len(cache.datasets) == 1
    assert cache.datasets[0].columns


# --- 2. first-object failure saves nothing -----------------------------------


def test_map_first_object_failure_saves_nothing(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)
    _raise_on_nth_object(monkeypatch, 1)  # the first object trips the gate

    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))

    assert envelope.status.value == "error"
    message = envelope.errors[0]
    assert "no partial profiles were saved" in message
    assert "cache.json" not in message  # no path when nothing was written
    assert not (tmp_path / ".dex" / "cache.json").exists()


def test_profile_first_object_failure_saves_nothing(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)
    _raise_on_nth_object(monkeypatch, 1)

    envelope = explore_cmds.cmd_profile(
        _args(tmp_path, subcommand="profile", objects=["customers", "orders"])
    )

    assert envelope.status.value == "error"
    assert "no partial profiles were saved" in envelope.errors[0]
    assert not (tmp_path / ".dex" / "cache.json").exists()


# --- 3. a successful billed run overwrites checkpoints with the composed cache -


def test_successful_map_overwrites_checkpoints_with_composed_cache(
    two_table_client, monkeypatch, tmp_path
):
    _install(monkeypatch, two_table_client)  # no wrap: the run completes

    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))

    assert envelope.status.value == "ok"
    cache = DexStore(tmp_path).load_cache()
    assert cache is not None
    assert len(cache.datasets) == 2
    # Ranking and relationship inference ran: signals a checkpoint never carries.
    assert all(d.rank_score is not None for d in cache.datasets)
    assert cache.relationships  # orders.customer_id -> customers.id


# --- 4. the free DuckDB path never checkpoints -------------------------------


def test_duckdb_map_does_not_checkpoint(airbnb_duckdb, monkeypatch, tmp_path):
    """DuckDB has no cost gate, so the checkpointer is never built: a single
    authoritative save at the end, exactly as before this change."""

    saves: list[int] = []
    original = DexStore.save_cache

    def counting_save(self, cache, *, now=None):
        saves.append(len(cache.datasets))
        return original(self, cache, now=now)

    monkeypatch.setattr(DexStore, "save_cache", counting_save)

    from exmergo_dex_core.cli import main

    rc = main(
        [
            "explore",
            "map",
            "--full",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    # Exactly one save — the final authoritative one — with no per-object writes.
    assert len(saves) == 1
