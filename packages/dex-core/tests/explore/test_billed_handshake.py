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

from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.config import BigQueryTarget, DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.explore import commands as explore_cmds
from exmergo_dex_core.guards.cost_guard import CostGate
from exmergo_dex_core.storage import FilesystemStore

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


def _near_unique_not_proven_resolver(sql: str):
    """Like `_aggregate_resolver`, but the exact-distinct escalation comes back
    just short of proving column 0 unique (99 of 100, not 100 of 100). Column 0
    stays a near-unique candidate without ever becoming a proven single-column
    key, so `_probe_composite_keys` is not short-circuited: the composite-key
    probe this fixture's tables can trigger actually runs, instead of being
    skipped the way a proven key would skip it."""

    values = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
        values[f"d_{i}"] = 99 if i == 0 else 40
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
    """Route the engine's one adapter funnel at a prebuilt adapter, reading the
    confirm/budget settings off the engine the way connect.py would off config.

    Patching ``DexEngine._adapter`` rather than ``connect.open_adapter`` is
    deliberate: it is the seam every command actually goes through, so a command
    that grew a second way to open a connection would fail here.
    """

    def install(fake_client, record=None):
        def opener(self, command=None, *, budget=None, confirmed=None):
            return _adapter(
                fake_client,
                confirmed=self.confirmed if confirmed is None else confirmed,
                budget=self.budget if budget is None else budget,
                record=record,
            )

        monkeypatch.setattr(DexEngine, "_adapter", opener)

    return install


def _engine(tmp_path: Path, **extra) -> DexEngine:
    return DexEngine(
        connector="bigquery",
        repo_root=str(tmp_path),
        store=FilesystemStore(tmp_path),
        config=DexConfig(connector="bigquery"),
        confirmed=extra.get("confirm", False),
        budget=extra.get("budget"),
    )


def _dispatch(tmp_path: Path, **extra):
    """One command end to end through the real router, which is also where an
    unmet confirmation becomes a needs_confirmation envelope."""

    from exmergo_dex_core.cli import dispatch

    return dispatch(_args(tmp_path, **extra), _engine(tmp_path, **extra))


def test_unconfirmed_profile_returns_needs_confirmation(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = _dispatch(tmp_path, subcommand="profile", objects=["customers"])
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # The single below-floor batch floors to the per-query minimum, plus a
    # floor reserved for each of the two possible escalation queries (2
    # columns, 100 rows): 10 + 10 + 10 = 30 MB.
    assert envelope.cost.estimate == 30 * MB
    assert envelope.data["per_table_bytes"] == {"test-proj.shop.customers": 30 * MB}
    assert "--confirm" in envelope.data["hint"]
    # Nothing executed: only free metadata and dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_confirmed_profile_runs_and_stamps_spend(
    fake_bq_client, route_adapter, tmp_path
):
    entries: list[dict] = []
    fake_bq_client.row_resolver = _aggregate_resolver
    route_adapter(fake_bq_client, record=entries.append)
    envelope = _dispatch(
        tmp_path,
        subcommand="profile",
        objects=["customers"],
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert envelope.data["datasets"][0]["identifier"] == "test-proj.shop.customers"
    assert envelope.cost.estimate == 30 * MB  # floored preflight estimate + reserve
    assert envelope.cost.ceiling == 100 * MB
    # The aggregate batch plus the exact-distinct escalation (optional spend
    # inside the confirmed budget): both scans land in the ledger.
    assert envelope.data["spend"]["bytes_billed"] == 10_000
    assert [e["billed_bytes"] for e in entries] == [5_000, 5_000]


def test_unconfirmed_map_estimates_selected_objects(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = _dispatch(tmp_path, subcommand="map")
    assert envelope.status.value == "needs_confirmation"
    # customers and events each floor to the per-query minimum plus a floor
    # per possible escalation query (30 MB apiece); logs.requests needs a
    # partition filter, so it contributes zero.
    assert envelope.cost.estimate == 2 * 30 * MB
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
    FilesystemStore(tmp_path).save_cache(cache)


def test_unconfirmed_map_excludes_fresh_cached_objects(
    fake_bq_client, route_adapter, tmp_path
):
    """A fresh cached profile for customers is excluded from the preflight
    estimate and its per-table breakdown; only the stale events is priced."""

    _seed_bq_map_cache(tmp_path, identifiers={"test-proj.shop.customers"})
    route_adapter(fake_bq_client)
    envelope = _dispatch(tmp_path, subcommand="map")
    assert envelope.status.value == "needs_confirmation"
    # Only events is priced now (customers is fresh-cached, requests is
    # partition-filtered to zero), so the estimate halves versus the no-cache run.
    assert envelope.cost.estimate == 30 * MB
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
    envelope = _dispatch(tmp_path, subcommand="map")
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

    def scoped_opener(self, command=None, *, budget=None, confirmed=None):
        gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=self.budget if budget is None else budget,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=self.confirmed if confirmed is None else confirmed,
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

    monkeypatch.setattr(DexEngine, "_adapter", scoped_opener)
    envelope = _dispatch(tmp_path, subcommand="map")

    assert envelope.status.value == "ok"
    assert envelope.data["out_of_scope_carried_count"] == 2
    assert any(
        "outside this run's --scope/--dataset" in note
        for note in envelope.data["notes"]
    )
    cache = FilesystemStore(tmp_path).load_cache()
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
    envelope = _dispatch(tmp_path, subcommand="relationships")
    assert envelope.status.value == "needs_confirmation"
    assert any("explore map" in note for note in envelope.data["notes"])


def _seed_query_cache(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path)
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
    envelope = _dispatch(
        tmp_path,
        subcommand="query",
        sql="SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
    )
    assert envelope.status.value == "needs_confirmation"
    # Single-table query, floored to the per-query billing minimum.
    assert envelope.cost.estimate == 10 * MB
    log_lines = (tmp_path / ".dex" / "queries.jsonl").read_text().splitlines()
    assert json.loads(log_lines[-1])["decision"] == "needs_confirmation"


def _seed_cluster_cache(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path)
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
    envelope = _dispatch(tmp_path, subcommand="cluster", object="customers")
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # Two queries price into this estimate: the feature sample, and its
    # companion null-count query (#160, so dropped_null_rows is measured, not
    # structurally zero). Each floors independently to the per-query billing
    # minimum.
    assert envelope.cost.estimate == 20 * MB
    assert any("sampl" in note for note in envelope.data.get("notes", []))
    # Nothing executed: only free dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_confirmed_query_runs_through_the_firewall(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_query_cache(tmp_path)
    fake_bq_client.row_resolver = lambda sql: [{"n": 100}]
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="query",
        sql="SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        confirm=True,
        budget=float(100 * MB),
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
    envelope = _dispatch(
        tmp_path,
        subcommand="map",
        verify=True,
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert "phase" not in envelope.data
    assert _probe_executed(fk_bq_client)
    cache = FilesystemStore(tmp_path).load_cache()
    assert cache.relationships and cache.relationships[0].verified


def test_verify_beyond_budget_checkpoints_before_any_probe(
    fk_bq_client, route_adapter, tmp_path
):
    # 60 MB matches profile_estimate()'s worst-case total exactly (two tables,
    # each floored aggregate batch plus both possible escalation reserves: 30
    # MB apiece), so the initial handshake just barely passes. A near-unique
    # column that escalates without fully proving uniqueness (unlike the
    # shared resolver's exact d_i == nd_i) leaves the composite-key probe
    # un-short-circuited, so both escalations this fixture's estimate reserved
    # for actually run and get charged -- leaving no headroom for the
    # two-table verify probe, which floors to another 20 MB.
    fk_bq_client.row_resolver = _near_unique_not_proven_resolver
    route_adapter(fk_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="map",
        verify=True,
        confirm=True,
        budget=float(60 * MB),
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["phase"] == "verify"
    assert envelope.data["candidate_count"] == 1
    assert envelope.data["object_count"] == 2
    assert envelope.data["per_table_bytes"] == {"(join overlap probes)": 20 * MB}
    assert "--budget" in envelope.data["hint"]
    # The raised estimate is the whole-command total the re-run needs.
    assert envelope.cost.estimate > envelope.cost.ceiling
    assert envelope.cost.ceiling == 60 * MB
    # No probe was billed; profiling spend is reported on the checkpoint.
    assert not _probe_executed(fk_bq_client)
    assert envelope.data["spend"]["bytes_billed"] > 0
    # The map itself completed and persisted: profiles and the unverified
    # relationship are in the cache, and the summary rides along.
    assert envelope.data["relationship_count"] == 1
    assert Path(envelope.data["cache_path"]).exists()
    cache = FilesystemStore(tmp_path).load_cache()
    assert cache.relationships and not cache.relationships[0].verified
    assert any("unverified" in note for note in envelope.data["notes"])


def test_relationships_verify_beyond_budget_checkpoints(
    fk_bq_client, route_adapter, tmp_path
):
    fk_bq_client.row_resolver = _near_unique_not_proven_resolver
    route_adapter(fk_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="relationships",
        verify=True,
        confirm=True,
        budget=float(60 * MB),
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["phase"] == "verify"
    assert envelope.data["command"] == "explore relationships"
    assert not _probe_executed(fk_bq_client)
    assert not any(
        "verified" in note and "overlap" in note for note in envelope.data["notes"]
    )
    cache = FilesystemStore(tmp_path).load_cache()
    assert cache.relationships and not cache.relationships[0].verified


def test_verify_with_no_candidates_skips_the_checkpoint(
    fake_bq_client, route_adapter, tmp_path
):
    # The shared fixture's tables share no foreign-key stem, so inference finds
    # nothing to probe and the confirmed budget alone carries the run.
    fake_bq_client.row_resolver = _aggregate_resolver
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="map",
        verify=True,
        confirm=True,
        budget=float(100 * MB),
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
    envelope = _dispatch(
        tmp_path,
        subcommand="map",
        verify=True,
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert any("1 of 1" in w and "budget exhausted" in w for w in envelope.warnings)
    cache = FilesystemStore(tmp_path).load_cache()
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

    from exmergo_dex_core import command_args

    pending = command_args.verify_handshake(
        "explore map", StubAdapter(), 5.0, candidate_count=3, object_count=2
    )
    # A request, not a raise: the profiles this phase follows are already paid for.
    assert pending is not None
    assert pending.data["estimated_seconds"] == 5.0
    assert pending.data["per_table_seconds"] == {"(join overlap probes)": 5.0}
    assert pending.data["candidate_count"] == 3
    assert pending.data["object_count"] == 2
    assert "notes" in pending.data
    assert pending.cost.estimate == 13.0


def test_duckdb_map_verify_stays_confirmation_free(duckdb_file: Path, capsys):
    from exmergo_dex_core.cli import main

    rc = main(["explore", "map", "--verify", "--path", str(duckdb_file)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["cost"]["paradigm"] == "free_local"
    assert "phase" not in payload["data"]


# --- what a refusal reports ------------------------------------------------------
#
# A refusal is the most consequential message the cost guard emits, and it used
# to arrive with an empty cost block whose paradigm defaulted to `free_local`: a
# spend refusal on a metered connector positively asserting that nothing was
# going to be spent. These pin that every cost-guard refusal carries the gate's
# own cost, so the structured field agrees with the prose beside it.


def test_over_ceiling_refusal_reports_the_metered_paradigm(
    fake_bq_client, route_adapter, tmp_path
):
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="profile",
        objects=["customers"],
        confirm=True,
        budget=float(MB),
    )
    assert envelope.status.value == "error"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    # The two numbers the prose names, structured: what was asked for and what
    # was allowed. A caller sizes the re-run from these without parsing text.
    assert envelope.cost.estimate == 30 * MB
    assert envelope.cost.ceiling == MB
    assert "exceeds the ceiling" in envelope.errors[0]
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_no_ceiling_refusal_reports_the_metered_paradigm(
    fake_bq_client, route_adapter, tmp_path
):
    # Confirmed but unbudgeted: nothing executes unbudgeted, and the refusal
    # still has to say which unit the missing budget would have been in.
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path, subcommand="profile", objects=["customers"], confirm=True
    )
    assert envelope.status.value == "error"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    assert envelope.cost.ceiling is None
    assert "no ceiling set" in envelope.errors[0]


def test_budget_exhaustion_carries_both_the_ceiling_and_the_spend(
    fake_bq_client, route_adapter, tmp_path, monkeypatch
):
    """Partial completion reports what it cost *and* what it was allowed to cost.

    The gap between the two is the whole message: a caller deciding how much to
    raise the budget by needs both numbers.
    """

    from exmergo_dex_core.cli import dispatch
    from exmergo_dex_core.explore import commands as cmds

    def exhausted(args, engine):
        adapter = engine._adapter("explore profile")
        adapter.cost_gate.charge(4 * MB)
        adapter.cost_gate.record_billed(5_000)
        raise cmds._budget_exhausted(FilesystemStore(tmp_path), adapter, [], 3)

    route_adapter(fake_bq_client)
    monkeypatch.setattr(cmds, "cmd_profile", exhausted)
    envelope = dispatch(
        _args(tmp_path, subcommand="profile", confirm=True, budget=float(10 * MB)),
        _engine(tmp_path, confirm=True, budget=float(10 * MB)),
    )
    assert envelope.status.value == "error"
    assert envelope.cost.paradigm is Paradigm.BYTES_SCANNED
    assert envelope.cost.estimate == 4 * MB
    assert envelope.cost.ceiling == 10 * MB
    assert envelope.data["spend"]["bytes_billed"] == 5_000


# --- the cumulative cap that was never set ---------------------------------------
#
# `effective_ceiling()` returns the tighter of the per-command and the remaining
# session bound, and `None` only when neither is set. So a config with `ceiling`
# set and `session_ceiling` unset runs every billed command with no daily cap,
# and from outside that is indistinguishable from a cap that bound. A host
# running a second repo root found seven builds settled against nothing, by
# reading the ledger rather than by being told.


def _session_warning(warnings) -> list[str]:
    return [w for w in warnings if "budget.session_ceiling" in w]


def test_the_handshake_says_when_no_cumulative_cap_is_set(
    fake_bq_client, route_adapter, tmp_path
):
    # The handshake is where a caller picks a budget, so it is the last useful
    # moment to say that the budget they pick bounds one command and not the day.
    route_adapter(fake_bq_client)
    envelope = _dispatch(tmp_path, subcommand="profile", objects=["customers"])
    assert envelope.status.value == "needs_confirmation"
    assert len(_session_warning(envelope.warnings)) == 1


def test_a_settled_billed_run_says_when_no_cumulative_cap_is_set(
    fake_bq_client, route_adapter, tmp_path
):
    fake_bq_client.row_resolver = _aggregate_resolver
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="profile",
        objects=["customers"],
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert len(_session_warning(envelope.warnings)) == 1


def test_a_run_under_a_cumulative_cap_stays_quiet(
    monkeypatch, fake_bq_client, tmp_path
):
    from exmergo_dex_core.engine import DexEngine

    def opener(self, command=None, *, budget=None, confirmed=None):
        adapter = _adapter(fake_bq_client, confirmed=True, budget=float(100 * MB))
        adapter.cost_gate.session_ceiling = float(500 * MB)
        return adapter

    monkeypatch.setattr(DexEngine, "_adapter", opener)
    fake_bq_client.row_resolver = _aggregate_resolver
    envelope = _dispatch(
        tmp_path,
        subcommand="profile",
        objects=["customers"],
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert _session_warning(envelope.warnings) == []


def test_duckdb_never_warns_about_a_cumulative_cap(duckdb_file: Path, capsys):
    # Nothing bills, so there is no daily spend for a cap to bound.
    from exmergo_dex_core.cli import main

    assert main(["explore", "profile", "customers", "--path", str(duckdb_file)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert _session_warning(payload["warnings"]) == []
