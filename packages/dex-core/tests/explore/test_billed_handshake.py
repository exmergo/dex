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
from exmergo_dex_core.envelope import Paradigm, Reason
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


def _domain_eligible_resolver(sql: str):
    """Like `_near_unique_not_proven_resolver`, but every filler column
    (customers.plan_tier, orders.customer_id, orders.total) is also given a
    small, domain-eligible cardinality instead of the generic 40. Their
    value-domain probes are a genuinely different query shape -- a capped
    frequency list, not the scalar the other escalations return under the
    same `d_i`/`n_i` aliases -- so they are answered separately (keyed off
    `ARRAY_AGG`, the marker unique to that statement) rather than sharing
    the generic per-index values. This makes profiling this fixture's
    tables actually spend the new reserve instead of leaving it as unused
    padding, which is what the beyond-budget verify-checkpoint tests need."""

    values = _near_unique_not_proven_resolver(sql)[0]
    values["nd_1"] = 5  # orders.customer_id (customers.email stays PII-excluded
    values["nd_2"] = 5  # regardless); customers.plan_tier / orders.total
    if "ARRAY_AGG" in sql:
        for i in range(3):
            values[f"d_{i}"] = [{"v": f"v{j}", "c": 1} for j in range(5)]
            values[f"n_{i}"] = 5
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
    # floor reserved for each of the three possible escalation queries (2
    # columns, 100 rows): 10 + 10 + 10 + 10 = 40 MB.
    assert envelope.cost.estimate == 40 * MB
    assert envelope.data["per_table_bytes"] == {"test-proj.shop.customers": 40 * MB}
    # Three of those four floors are reserve, split out so the caller can see
    # whether a raised budget buys work or headroom (#299).
    assert envelope.data["reserved_bytes"] == 30 * MB
    assert envelope.data["reserved_queries"] == 3
    assert any("escalation reserve" in n for n in envelope.data["notes"])
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
    assert envelope.cost.estimate == 40 * MB  # floored preflight estimate + reserve
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
    # per escalation query their metadata leaves possible (40 MB for customers,
    # 30 MB for events, whose only distinct-countable column cannot form a
    # composite pair); logs.requests needs a partition filter, so it
    # contributes zero.
    assert envelope.cost.estimate == 70 * MB
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
    # partition-filtered to zero), so the estimate drops to that table's share
    # of the no-cache run.
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
    """A cache that already adjudicates `shop.customers`, so the query path prices
    the query alone.

    The column signature has to match what the fake warehouse reports, `id`
    REQUIRED included: a seeded profile that disagrees with the live schema is one
    the engine re-profiles before trusting it, which is the point of that check and
    would make this a test of the wrong thing.
    """

    store = FilesystemStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.customers",
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER", nullable=False),
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


@pytest.mark.parametrize(
    "sql",
    [
        # Issue #319: a hyphenated project id, both fully-quoted-per-part and
        # backtick-wrapped-as-one-identifier, plus the bare unquoted form
        # copied verbatim out of an `explore inventory` identifier or an
        # `explore query` `tables` entry. All three must parse in the
        # connector's own dialect and reach the cost handshake rather than
        # a "could not parse query" refusal on the hyphen.
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        "SELECT COUNT(*) AS n FROM `test-proj.shop.customers`",
        "SELECT COUNT(*) AS n FROM test-proj.shop.customers",
    ],
)
def test_a_hyphenated_project_id_parses_in_every_spelling(
    fake_bq_client, route_adapter, tmp_path, sql
):
    _seed_query_cache(tmp_path)
    route_adapter(fake_bq_client)
    envelope = _dispatch(tmp_path, subcommand="query", sql=sql)
    # A parse failure on the hyphen would come back as an `error` envelope
    # (`reason: guard`) naming a SQL parser, not this cost checkpoint.
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.estimate == 10 * MB


def _clusterable_client():
    """A fake warehouse whose `customers` carries numeric non-key columns.

    The shared fixture's table is id and email, which clustering has nothing to
    cluster on. Building the client here rather than widening the shared one keeps
    every other test's view of that table exactly as it was.
    """

    from fakes.bigquery import FakeBigQueryClient, FakeTable
    from google.cloud import bigquery

    return FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="customers",
                schema=[
                    bigquery.SchemaField("id", "INTEGER", mode="REQUIRED"),
                    bigquery.SchemaField("amount", "INTEGER"),
                    bigquery.SchemaField("score", "FLOAT"),
                ],
                num_rows=100,
                num_bytes=5_000,
            )
        ],
    )


def _seed_cluster_cache(tmp_path: Path) -> None:
    """A profile of `shop.customers` matching what `_clusterable_client` reports.

    Signature-exact for the same reason as `_seed_query_cache`: a seeded profile
    that disagrees with the live schema is re-profiled before it is trusted.
    """

    store = FilesystemStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.customers",
                    row_count=100,
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER", nullable=False),
                        ColumnProfile(name="amount", data_type="INTEGER"),
                        ColumnProfile(name="score", data_type="FLOAT"),
                    ],
                )
            ]
        )
    )


def test_unconfirmed_cluster_returns_needs_confirmation(route_adapter, tmp_path):
    """Clustering scans the feature columns, so on a billed connector it takes
    the same cost-before-spend handshake: an estimate and needs_confirmation,
    with nothing executed."""

    pytest.importorskip("sklearn")
    _seed_cluster_cache(tmp_path)
    fake_bq_client = _clusterable_client()
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


def test_a_batch_is_priced_once_and_itemized_per_statement(
    fake_bq_client, route_adapter, tmp_path
):
    """One question, one number. A caller confirming a batch is confirming all of
    it, so the ceiling binds on the sum rather than on whichever statement asked
    first."""

    _seed_query_cache(tmp_path)
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="query",
        sql=[
            "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
            "SELECT COUNT(DISTINCT id) AS n FROM `test-proj`.`shop`.`customers`",
        ],
    )
    assert envelope.status.value == "needs_confirmation"
    # Two statements, each floored to the per-query billing minimum, quoted as one.
    assert envelope.cost.estimate == 20 * MB
    assert envelope.data["per_table_bytes"] == {
        "(statement 1)": 10 * MB,
        "(statement 2)": 10 * MB,
    }
    # The ask is as findable per statement in the ledger as a spend would be.
    log_lines = (tmp_path / ".dex" / "queries.jsonl").read_text().splitlines()
    decisions = [json.loads(line) for line in log_lines[-2:]]
    assert [d["decision"] for d in decisions] == ["needs_confirmation"] * 2
    assert [d["batch_index"] for d in decisions] == [0, 1]


def test_a_confirmed_batch_runs_every_statement_off_one_handshake(
    fake_bq_client, route_adapter, tmp_path
):
    _seed_query_cache(tmp_path)
    fake_bq_client.row_resolver = lambda sql: [{"n": 100}]
    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="query",
        sql=[
            "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
            "SELECT COUNT(DISTINCT id) AS n FROM `test-proj`.`shop`.`customers`",
        ],
        confirm=True,
        budget=float(100 * MB),
    )
    assert envelope.status.value == "ok"
    assert [r["cells"] for r in envelope.data["results"]] == [[[100]], [[100]]]
    # Two command-level dry-runs (one estimate per statement, summed into a single
    # handshake), then per statement the adapter's own defense-in-depth dry-run
    # followed by its execution. One handshake, two runs.
    assert [c.dry_run for c in fake_bq_client.query_calls] == [
        True,
        True,
        True,
        False,
        True,
        False,
    ]
    assert envelope.data["spend"]["bytes_billed"] == 10_000


def test_a_cold_batch_prices_the_shared_scan_and_every_statement_together(
    fake_bq_client, route_adapter, tmp_path
):
    """Nothing is seeded, so the objects have to be profiled first. Two statements
    over the same cold table are quoted one scan, not two."""

    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="query",
        sql=[
            "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
            "SELECT COUNT(DISTINCT id) AS n FROM `test-proj`.`shop`.`customers`",
        ],
    )
    assert envelope.status.value == "needs_confirmation"
    itemized = envelope.data["per_table_bytes"]
    assert [key for key in itemized if key.startswith("(statement")] == [
        "(statement 1)",
        "(statement 2)",
    ]
    assert len([key for key in itemized if key.startswith("test-proj")]) == 1
    assert any("no usable profile" in note for note in envelope.data["notes"])


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
                bigquery.SchemaField("plan_tier", "STRING"),
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
    # 80 MB is profile_estimate()'s worst-case total for two tables (each
    # floored aggregate batch plus all three possible escalation reserves: 40
    # MB apiece), so the initial handshake passes. A near-unique column that
    # escalates without fully proving uniqueness (unlike the shared
    # resolver's exact d_i == nd_i) leaves the composite-key probe
    # un-short-circuited, and every filler column is domain-eligible, so
    # every escalation this fixture's estimate reserved for actually runs and
    # gets charged: 2 tables x (exact-distinct + combination) = 4 charges,
    # plus 3 value-domain probes (customers.plan_tier, orders.customer_id,
    # orders.total) = 7 charges x ~10 MB =~ 73 MB profiling, which alone
    # already leaves no room for the two-table verify probe's 20 MB floor.
    fk_bq_client.row_resolver = _domain_eligible_resolver
    route_adapter(fk_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="map",
        verify=True,
        confirm=True,
        budget=float(80 * MB),
    )
    assert envelope.status.value == "needs_confirmation"
    assert envelope.data["phase"] == "verify"
    assert envelope.data["candidate_count"] == 1
    assert envelope.data["object_count"] == 2
    assert envelope.data["per_table_bytes"] == {"(join overlap probes)": 20 * MB}
    assert "--budget" in envelope.data["hint"]
    # The raised estimate is the whole-command total the re-run needs.
    assert envelope.cost.estimate > envelope.cost.ceiling
    assert envelope.cost.ceiling == 80 * MB
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
    # The verify phase prices overlap probes, which carry no escalation
    # reserve. The profile estimate earlier in this same command did, and
    # attributing that one to this number would describe the wrong estimate.
    assert "reserved_bytes" not in envelope.data
    assert not any("escalation reserve" in n for n in envelope.data["notes"])


def test_relationships_verify_beyond_budget_checkpoints(
    fk_bq_client, route_adapter, tmp_path
):
    # See test_verify_beyond_budget_checkpoints_before_any_probe: every
    # filler column's value-domain probe is what actually spends the new
    # reserve rather than leaving it as unused padding.
    fk_bq_client.row_resolver = _domain_eligible_resolver
    route_adapter(fk_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="relationships",
        verify=True,
        confirm=True,
        budget=float(80 * MB),
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
    assert envelope.cost.estimate == 40 * MB
    assert envelope.cost.ceiling == MB
    assert "exceeds the ceiling" in envelope.errors[0]
    assert all(c.dry_run for c in fake_bq_client.query_calls)
    # #170: a machine-readable reason alongside the prose.
    assert envelope.reason is Reason.GUARD


def test_over_ceiling_refusal_attributes_the_estimate_it_refused_on(
    fake_bq_client, route_adapter, tmp_path
):
    """Three quarters of this estimate is escalation reserve, and a refusal
    that quotes only the total leaves the operator to reconstruct the split
    from the spend ledger to find out whether the number grew because the
    warehouse did (issue #299)."""

    route_adapter(fake_bq_client)
    envelope = _dispatch(
        tmp_path,
        subcommand="profile",
        objects=["customers"],
        confirm=True,
        budget=float(MB),
    )
    assert envelope.status.value == "error"
    message = envelope.errors[0]
    assert "exceeds the ceiling" in message
    # 3 of the 4 floors in customers' 40 MB estimate are reserve.
    assert "31,457,280 bytes of this estimate is escalation reserve" in message
    assert "3 queries" in message
    assert "10,485,760 bytes is dry-run scan" in message
    # Still a refusal that spends nothing and cannot be confirmed through.
    assert envelope.cost.estimate == 40 * MB
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
    # #170: a machine-readable reason alongside the prose.
    assert envelope.reason is Reason.GUARD


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


# --- a statement the server refused (#310) ----------------------------------------
#
# Three defects met on this path and each hid the next: profiling built a CAST
# Redshift refused, the driver's exception escaped untranslated so the envelope
# said `internal` with no reason and no object, and the seconds already billed
# were reported nowhere, so a failed metered command read as a free one. The
# first is fixed in the SQL (see test_safety_spine); these pin the envelope the
# other two produce, which is what makes any future one diagnosable from stdout.


def test_a_refused_statement_reports_its_reason_its_object_and_its_spend(
    fake_bq_client, monkeypatch, tmp_path, capsys
):
    from google.api_core import exceptions as api_exceptions

    from exmergo_dex_core.cli import main
    from exmergo_dex_core.engine import DexEngine

    def opener(self, command=None, *, budget=None, confirmed=None):
        # Cached on the engine the way the real funnel does it: the settlement
        # that reads spend back on the way out reads the engine's held adapter,
        # so an opener that handed out a fresh one per call would test nothing.
        if self._adapter_instance is None:
            self._adapter_instance = _adapter(
                fake_bq_client, confirmed=True, budget=float(100 * MB)
            )
        return self._adapter_instance

    monkeypatch.setattr(DexEngine, "_adapter", opener)
    fake_bq_client.row_resolver = _aggregate_resolver

    # The first object profiles and bills; the second meets a server refusal,
    # which is the ordinary shape of this failure (one bad column in one table
    # of many, on a run that has already spent).
    original = BigQueryAdapter.column_aggregates

    def refuse_the_second(self, identifier, columns, **kwargs):
        if identifier.endswith("events"):
            fake_bq_client.result_error = api_exceptions.BadRequest(
                "Bad int64 value: pending"
            )
        return original(self, identifier, columns, **kwargs)

    monkeypatch.setattr(BigQueryAdapter, "column_aggregates", refuse_the_second)

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "explore",
            "profile",
            "customers",
            "events",
            "--confirm",
            "--budget",
            str(float(100 * MB)),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["status"] == "error"
    # Classified: the server ran the statement and refused it, which is not the
    # `internal` an untyped driver exception used to fall through to.
    assert payload["reason"] == Reason.EXECUTION_FAILURE.value
    error = payload["errors"][0]
    assert "Bad int64 value" in error  # the server's own diagnosis, verbatim
    assert "test-proj.shop.events" in error  # and which object it was about
    # Metered and failed is not the same as free: the first object's scan was
    # billed and the envelope says so.
    assert payload["data"]["spend"]["bytes_billed"] > 0
    assert payload["cost"]["paradigm"] == Paradigm.BYTES_SCANNED.value


def test_a_refusal_that_never_reached_the_warehouse_reports_no_spend(
    fake_bq_client, monkeypatch, tmp_path, capsys
):
    """The other side of it: a command refused before the first statement
    spent nothing, and a spend block of zeroes on that envelope would read as a
    claim about money where silence is the honest answer."""

    from exmergo_dex_core.cli import main
    from exmergo_dex_core.engine import DexEngine

    def opener(self, command=None, *, budget=None, confirmed=None):
        if self._adapter_instance is None:
            self._adapter_instance = _adapter(
                fake_bq_client, confirmed=True, budget=float(1)
            )
        return self._adapter_instance

    monkeypatch.setattr(DexEngine, "_adapter", opener)
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "explore",
            "profile",
            "customers",
            "--confirm",
            "--budget",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["reason"] == Reason.GUARD.value
    assert "spend" not in payload["data"]
