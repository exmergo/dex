"""The cost-before-spend handshake at the explore command layer: billed
connectors get needs_confirmation with a dry-run estimate; DuckDB stays
confirmation-free."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core import command_args
from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, DexStore, PIIFlag
from exmergo_dex_core.config import BigQueryTarget
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.explore import commands as explore_cmds
from exmergo_dex_core.guards.cost_guard import CostGate

MB = 1024 * 1024


def _aggregate_resolver(sql: str):
    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
        # The exact-distinct escalation statement (a near-unique id column
        # triggers it) reads d_<i> aliases.
        values[f"d_{i}"] = 100
    values["nonnull_fk"] = 100
    values["orphans"] = 0
    return [values]


def _adapter(fake_bq_client, *, confirmed: bool, budget: float | None, record=None):
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
        client=fake_bq_client,
        principal_type="user",
    )


def _args(tmp_path: Path, **extra) -> argparse.Namespace:
    base = {
        "connector": "bigquery",
        "path": None,
        "repo_root": str(tmp_path),
        "confirm": False,
        "budget": None,
        "group": "explore",
    }
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture
def route_adapter(monkeypatch):
    """Route command_args.open_from_args at a prebuilt adapter, reading the
    confirm/budget flags off the args namespace the way connect.py would."""

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


def test_unconfirmed_profile_returns_needs_confirmation(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_profile(
        _args(tmp_path, subcommand="profile", objects=["customers"])
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # The single below-floor batch is priced at the per-query billing minimum.
    assert envelope.cost.estimate == 10 * MB
    assert envelope.data["per_table_bytes"] == {"test-proj.shop.customers": 10 * MB}
    assert "--confirm" in envelope.data["hint"]
    # Nothing executed: only free metadata and dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_confirmed_profile_runs_and_stamps_spend(
    fake_bq_client, route_adapter, tmp_path
):
    entries: list[dict] = []
    fake_bq_client.row_resolver = _aggregate_resolver
    route_adapter(fake_bq_client, record=entries.append)
    envelope = explore_cmds.cmd_profile(
        _args(
            tmp_path,
            subcommand="profile",
            objects=["customers"],
            confirm=True,
            budget=float(100 * MB),
        )
    )
    assert envelope.status.value == "ok"
    assert envelope.data["datasets"][0]["identifier"] == "test-proj.shop.customers"
    assert envelope.cost.estimate == 10 * MB  # floored preflight estimate
    assert envelope.cost.ceiling == 100 * MB
    # The aggregate batch plus the exact-distinct escalation (optional spend
    # inside the confirmed budget): both scans land in the ledger.
    assert envelope.data["spend"]["bytes_billed"] == 10_000
    assert [e["billed_bytes"] for e in entries] == [5_000, 5_000]


def test_unconfirmed_map_estimates_selected_objects(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))
    assert envelope.status.value == "needs_confirmation"
    # customers and events each floor to the per-query minimum; logs.requests
    # needs a partition filter, so it contributes zero.
    assert envelope.cost.estimate == 2 * 10 * MB
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_unconfirmed_relationships_recommends_map(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_relationships(
        _args(tmp_path, subcommand="relationships")
    )
    assert envelope.status.value == "needs_confirmation"
    assert any("explore map" in note for note in envelope.data["notes"])


def _seed_query_cache(tmp_path: Path) -> None:
    store = DexStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.customers",
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER"),
                        ColumnProfile(
                            name="email",
                            data_type="STRING",
                            pii=PIIFlag(category="email", confidence=0.9),
                        ),
                    ],
                )
            ]
        )
    )


def test_unconfirmed_query_returns_estimate_and_logs(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_query_cache(tmp_path)
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_query(
        _args(
            tmp_path,
            subcommand="query",
            sql="SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        )
    )
    assert envelope.status.value == "needs_confirmation"
    # Single-table query, floored to the per-query billing minimum.
    assert envelope.cost.estimate == 10 * MB
    log_lines = (tmp_path / ".dex" / "queries.jsonl").read_text().splitlines()
    assert json.loads(log_lines[-1])["decision"] == "needs_confirmation"


def test_confirmed_query_runs_through_the_firewall(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_query_cache(tmp_path)
    fake_bq_client.row_resolver = lambda sql: [{"n": 100}]
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_query(
        _args(
            tmp_path,
            subcommand="query",
            sql="SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
            confirm=True,
            budget=float(100 * MB),
        )
    )
    assert envelope.status.value == "ok"
    assert envelope.data["cells"] == [[100]]
    assert envelope.data["spend"]["bytes_billed"] == 5_000
    # Two free dry-runs (the command estimate, then the per-statement charge
    # inside the adapter as defense in depth), then exactly one execution.
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True, True, False]


def test_duckdb_explore_stays_confirmation_free(duckdb_file: Path, capsys):
    # The regression guard for the free path: no gate, no handshake, free cost.
    from exmergo_dex_core.cli import main

    rc = main(["explore", "profile", "customers", "--path", str(duckdb_file)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["cost"]["paradigm"] == "free_local"
    assert "spend" not in payload["data"]
