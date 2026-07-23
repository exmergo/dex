"""The cost-before-spend handshake at the explore command layer: billed
connectors get needs_confirmation with a dry-run estimate; DuckDB stays
confirmation-free."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
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


def _fresh_bq_dataset(identifier: str, columns, *, now: str) -> Dataset:
    """A same-connector prior profile stamped just now, so skip-if-cached treats
    it as fresh. Column signatures mirror the fake BigQuery schema exactly so the
    pre-profile metadata check finds no drift."""

    return Dataset(
        identifier=identifier,
        row_count=100,
        columns=columns,
        candidate_keys=[["id"]],
        grain=["id"],
        profiled_at=now,
    )


def _seed_bq_map_cache(tmp_path: Path, *, identifiers: set[str]) -> None:
    """Seed a bigquery-connector cache holding fresh profiles for the named
    objects (schema-matching the fake client's tables)."""

    now = datetime.now(UTC).isoformat()
    catalog = {
        "test-proj.shop.customers": [
            ColumnProfile(name="id", data_type="INTEGER", nullable=False),
            ColumnProfile(name="email", data_type="STRING", nullable=True),
        ],
        "test-proj.shop.events": [
            ColumnProfile(name="id", data_type="INTEGER", nullable=True),
            ColumnProfile(name="payload", data_type="STRUCT", nullable=True),
            ColumnProfile(name="labels", data_type="ARRAY<STRING>", nullable=True),
        ],
        "test-proj.logs.requests": [
            ColumnProfile(name="day", data_type="DATE", nullable=True),
        ],
    }
    cache = DexCache(
        datasets=[
            _fresh_bq_dataset(identifier, catalog[identifier], now=now)
            for identifier in identifiers
        ]
    )
    cache.provenance.connector = "bigquery"
    DexStore(tmp_path).save_cache(cache)


def test_unconfirmed_map_excludes_fresh_cached_objects(
    fake_bq_client, route_adapter, tmp_path
):
    """A fresh cached profile for customers is excluded from the preflight
    estimate and its per-table breakdown; only the stale events is priced."""

    _seed_bq_map_cache(tmp_path, identifiers={"test-proj.shop.customers"})
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))
    assert envelope.status.value == "needs_confirmation"
    # Only events is priced now (customers is fresh-cached, requests is
    # partition-filtered to zero), so the estimate halves versus the no-cache run.
    assert envelope.cost.estimate == 10 * MB
    assert "test-proj.shop.customers" not in envelope.data["per_table_bytes"]
    assert any("fresh-cached" in note for note in envelope.data["notes"])
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_fully_fresh_map_needs_no_confirmation(fake_bq_client, route_adapter, tmp_path):
    """When every object is fresh-cached there is nothing to scan: the billed
    handshake is skipped entirely and the run completes without confirmation."""

    _seed_bq_map_cache(
        tmp_path,
        identifiers={
            "test-proj.shop.customers",
            "test-proj.shop.events",
            "test-proj.logs.requests",
        },
    )
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))
    assert envelope.status.value == "ok"
    assert envelope.data["profiled_count"] == 0
    assert envelope.data["cache_hit_count"] == 3
    # No estimate and no scan: not even a dry-run job was issued.
    assert fake_bq_client.query_calls == []


def test_scoped_map_carries_forward_out_of_scope_dataset_profiles(
    fake_bq_client, tmp_path, monkeypatch
):
    """Regression for #111: a prior cache spanning three datasets, re-mapped
    with --scope narrowed to just one of them, must not silently drop the
    other two datasets' profiles from the cache."""

    _seed_bq_map_cache(
        tmp_path,
        identifiers={
            "test-proj.shop.customers",
            "test-proj.shop.events",
            "test-proj.logs.requests",
        },
    )

    def scoped_opener(args):
        gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=getattr(args, "budget", None),
            session_ceiling=None,
            session_spent=0.0,
            confirmed=getattr(args, "confirm", False),
            connector="bigquery",
            command="explore",
        )
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=gate,
            # Simulates --scope logs: this run's inventory only ever sees the
            # logs dataset, never shop.customers or shop.events.
            target=BigQueryTarget(datasets=["logs"]),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(command_args, "open_from_args", scoped_opener)
    envelope = explore_cmds.cmd_map(_args(tmp_path, subcommand="map"))

    assert envelope.status.value == "ok"
    assert envelope.data["out_of_scope_carried_count"] == 2
    assert any(
        "outside this run's --scope/--dataset" in note
        for note in envelope.data["notes"]
    )
    cache = DexStore(tmp_path).load_cache()
    identifiers = {d.identifier for d in cache.datasets}
    assert identifiers == {
        "test-proj.shop.customers",
        "test-proj.shop.events",
        "test-proj.logs.requests",
    }


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


def _seed_cluster_cache(tmp_path: Path) -> None:
    store = DexStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.customers",
                    row_count=100,
                    columns=[
                        ColumnProfile(name="amount", data_type="INTEGER"),
                        ColumnProfile(name="score", data_type="FLOAT64"),
                    ],
                )
            ]
        )
    )


def test_unconfirmed_cluster_returns_needs_confirmation(
    fake_bq_client, route_adapter, tmp_path
):
    """Clustering scans the feature columns, so on a billed connector it takes
    the same cost-before-spend handshake: an estimate and needs_confirmation,
    with nothing executed."""

    pytest.importorskip("sklearn")
    _seed_cluster_cache(tmp_path)
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_cluster(
        _args(tmp_path, subcommand="cluster", object="customers")
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # The single-table feature scan floors to the per-query billing minimum.
    assert envelope.cost.estimate == 10 * MB
    assert any("sampl" in note for note in envelope.data.get("notes", []))
    # Nothing executed: only free dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


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


# --- the verify-phase checkpoint -------------------------------------------------
#
# Verify probes can only be priced after profiling finds the candidate joins, so
# the confirm handshake cannot cover them upfront. These tests pin the second,
# headroom-gated checkpoint: a budget that covers profiling and the probes runs
# in one pass; one that does not gets needs_confirmation after profiling, with
# the profiles and unverified relationships already persisted.


@pytest.fixture
def fk_bq_client():
    """A fake warehouse where inference finds a candidate join: orders carries
    a customer_id foreign key into customers, whose id the aggregate resolver
    reports as unique. Local to these tests so the shared fixture's estimate
    assertions stay untouched."""

    bigquery = pytest.importorskip("google.cloud.bigquery")
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
                bigquery.SchemaField("total", "NUMERIC"),
            ],
            num_rows=100,
            num_bytes=5_000,
        ),
    ]
    client = FakeBigQueryClient(project="test-proj", tables=tables)
    client.row_resolver = _aggregate_resolver
    return client


def _probe_executed(client) -> bool:
    return any(not c.dry_run and "nonnull_fk" in c.sql for c in client.query_calls)


def test_verify_within_budget_runs_in_one_pass(fk_bq_client, route_adapter, tmp_path):
    route_adapter(fk_bq_client)
    envelope = explore_cmds.cmd_map(
        _args(
            tmp_path,
            subcommand="map",
            verify=True,
            confirm=True,
            budget=float(100 * MB),
        )
    )
    assert envelope.status.value == "ok"
    assert "phase" not in envelope.data
    assert _probe_executed(fk_bq_client)
    cache = DexStore(tmp_path).load_cache()
    assert cache.relationships and cache.relationships[0].verified


def test_verify_beyond_budget_checkpoints_before_any_probe(
    fk_bq_client, route_adapter, tmp_path
):
    # 20 MB covers the profiling handshake exactly (two tables floored to the
    # per-query minimum each) but leaves no headroom for the two-table probe,
    # which floors to another 20 MB.
    route_adapter(fk_bq_client)
    envelope = explore_cmds.cmd_map(
        _args(
            tmp_path,
            subcommand="map",
            verify=True,
            confirm=True,
            budget=float(20 * MB),
        )
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["phase"] == "verify"
    assert envelope.data["candidate_count"] == 1
    assert envelope.data["object_count"] == 2
    assert envelope.data["per_table_bytes"] == {"(join overlap probes)": 20 * MB}
    assert "--budget" in envelope.data["hint"]
    # The raised estimate is the whole-command total the re-run needs.
    assert envelope.cost.estimate > envelope.cost.ceiling
    assert envelope.cost.ceiling == 20 * MB
    # No probe was billed; profiling spend is reported on the checkpoint.
    assert not _probe_executed(fk_bq_client)
    assert envelope.data["spend"]["bytes_billed"] > 0
    # The map itself completed and persisted: profiles and the unverified
    # relationship are in the cache, and the summary rides along.
    assert envelope.data["relationship_count"] == 1
    assert Path(envelope.data["cache_path"]).exists()
    cache = DexStore(tmp_path).load_cache()
    assert cache.relationships and not cache.relationships[0].verified
    assert any("unverified" in note for note in envelope.data["notes"])


def test_relationships_verify_beyond_budget_checkpoints(
    fk_bq_client, route_adapter, tmp_path
):
    route_adapter(fk_bq_client)
    envelope = explore_cmds.cmd_relationships(
        _args(
            tmp_path,
            subcommand="relationships",
            verify=True,
            confirm=True,
            budget=float(20 * MB),
        )
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["phase"] == "verify"
    assert envelope.data["command"] == "explore relationships"
    assert not _probe_executed(fk_bq_client)
    assert not any(
        "verified" in note and "overlap" in note for note in envelope.data["notes"]
    )
    cache = DexStore(tmp_path).load_cache()
    assert cache.relationships and not cache.relationships[0].verified


def test_verify_with_no_candidates_skips_the_checkpoint(
    fake_bq_client, route_adapter, tmp_path
):
    # The shared fixture's tables share no foreign-key stem, so inference finds
    # nothing to probe and the confirmed budget alone carries the run.
    fake_bq_client.row_resolver = _aggregate_resolver
    route_adapter(fake_bq_client)
    envelope = explore_cmds.cmd_map(
        _args(
            tmp_path,
            subcommand="map",
            verify=True,
            confirm=True,
            budget=float(20 * MB),
        )
    )
    assert envelope.status.value == "ok"
    assert "phase" not in envelope.data


def test_mid_verify_budget_exhaustion_degrades_to_a_warning(
    fk_bq_client, route_adapter, tmp_path, monkeypatch
):
    # Estimate drift can still trip the per-statement gate after the phase
    # checkpoint passed; the map is complete, so the run finishes with a
    # warning instead of the generic error it used to die with.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    def exhaust(adapter, relationships, *, timeout_seconds=None, progress=None):
        relationships[0].verified = True
        raise OverCeilingError("drifted past the ceiling")

    monkeypatch.setattr(explore_cmds.rel_mod, "verify_relationships", exhaust)
    route_adapter(fk_bq_client)
    envelope = explore_cmds.cmd_map(
        _args(
            tmp_path,
            subcommand="map",
            verify=True,
            confirm=True,
            budget=float(100 * MB),
        )
    )
    assert envelope.status.value == "ok"
    assert any("1 of 1" in w and "budget exhausted" in w for w in envelope.warnings)
    cache = DexStore(tmp_path).load_cache()
    assert cache.relationships


def test_verify_handshake_uses_the_adapters_estimate_description():
    # Connectors that speak credits/seconds describe their own estimate; the
    # checkpoint payload keeps that shape and overlays the phase fields.
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=10.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="snowflake",
        command="explore map",
    )
    gate.charge(8.0)

    class StubAdapter:
        cost_gate = gate

        def describe_estimate(self, estimate, per_table):
            return {
                "estimated_seconds": estimate,
                "per_table_seconds": per_table,
                "notes": ["seconds are a coarse translation"],
            }

    envelope = command_args.verify_handshake(
        "explore map", StubAdapter(), 5.0, candidate_count=3, object_count=2
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["estimated_seconds"] == 5.0
    assert envelope.data["per_table_seconds"] == {"(join overlap probes)": 5.0}
    assert envelope.data["candidate_count"] == 3
    assert envelope.data["object_count"] == 2
    assert "notes" in envelope.data
    assert envelope.cost.estimate == 13.0


def test_duckdb_map_verify_stays_confirmation_free(duckdb_file: Path, capsys):
    from exmergo_dex_core.cli import main

    rc = main(["explore", "map", "--verify", "--path", str(duckdb_file)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["cost"]["paradigm"] == "free_local"
    assert "phase" not in payload["data"]
