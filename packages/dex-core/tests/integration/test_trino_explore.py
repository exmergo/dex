"""Live explore against Trino: free inventory across catalogs, the confirm
handshake priced from EXPLAIN (TYPE IO), a gated profile whose fractions must
match DuckDB's on the same data, the query firewall in Trino's idioms, and the
caps that only a live server can prove bind.

The target is the seeded pair from scripts/setup_trino_dev.sh, which CI runs
too: memory.app (the shared seed shape) and iceberg.app (Parquet with real
statistics, and a customer_profiles table whose history holds a dropped PII
column). Nothing here bills money; db-load gating is exercised for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import TRINO_MAX_SECONDS, trino_client
from .test_trino_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.trino]

BUDGET = TRINO_MAX_SECONDS


@pytest.fixture(autouse=True)
def _url(trino_url, monkeypatch):
    monkeypatch.setenv("TRINO_URL", trino_url)


def _map(tmp_path: Path, capsys, *extra: str) -> dict:
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            *extra,
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    return envelope


def _query(tmp_path: Path, capsys, sql: str) -> tuple[int, dict]:
    return run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            sql,
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )


def _cache(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".dex" / "cache.json").read_text())


def test_inventory_is_free_three_part_and_spans_catalogs(tmp_path: Path, capsys):
    seed_repo(tmp_path, catalogs=["memory.app", "iceberg.app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert rc == 0, envelope
    assert envelope["cost"]["estimate"] == 0.0
    objects = envelope["data"]["objects"]
    names = {o["identifier"] for o in objects}
    assert {
        "memory.app.customers",
        "memory.app.order_items",
        "iceberg.app.orders",
        "iceberg.app.customer_profiles",
    } <= names
    assert all(o["identifier"].count(".") == 2 for o in objects)
    assert not any("$" in name for name in names)
    view = next(o for o in objects if o["identifier"] == "memory.app.v_order_totals")
    assert view["object_type"] == "view"


def test_an_unconfirmed_map_asks_and_names_its_estimate_basis(tmp_path: Path, capsys):
    seed_repo(tmp_path, catalogs=["iceberg.app"], budget=BUDGET)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "explore", "map"], capsys)
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["cost"]["estimate"] > 0
    assert envelope["cost"]["paradigm"] == "db_load"
    payload = envelope["data"]
    assert payload["estimate_quality"] == "heuristic"
    # Iceberg writes Parquet statistics, so the quote comes from the plan.
    assert payload["estimate_basis"] == "explain_io"
    assert "query_max_run_time" in payload["hint"]
    assert not (tmp_path / ".dex" / "cache.json").exists()


def test_an_over_ceiling_map_refuses_and_spends_nothing(tmp_path: Path, capsys):
    seed_repo(tmp_path, catalogs=["memory.app"], budget=0.0001)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 1
    assert "ceiling" in envelope["errors"][0].lower()
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_a_confirmed_map_profiles_flags_pii_and_measures_exact_fractions(
    tmp_path: Path, capsys
):
    """Trino types a 1.0 literal as decimal(2,1) and AVG keeps the scale, so a
    fraction of 1/7 comes back as 0.1 unless the builders pass double literals.
    phone is NULL on exactly every seventh of 500 rows: 71 of them."""

    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUDGET)
    envelope = _map(tmp_path, capsys, "--verify")
    data = envelope["data"]
    assert data["object_count"] >= 7
    assert data["pii_column_count"] >= 4
    assert data["spend"]["seconds_billed"] > 0

    by_id = {d["identifier"]: d for d in _cache(tmp_path)["datasets"]}
    customers = by_id["memory.app.customers"]
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"]["category"] == "email"
    assert email.get("min_value") is None and email.get("max_value") is None

    phone = next(c for c in customers["columns"] if c["name"] == "phone")
    assert phone["null_fraction"] == pytest.approx(71 / 500, abs=1e-9)

    # The nested and json columns were profiled for nulls only, and did not take
    # the rest of the batch down with them.
    products = by_id["memory.app.products"]
    tags = next(c for c in products["columns"] if c["name"] == "tags")
    assert tags["null_fraction"] == 0.0
    assert tags.get("distinct_count") is None
    price = next(c for c in products["columns"] if c["name"] == "price")
    assert price["distinct_count"] is not None


def test_profiled_fractions_keep_their_precision(tmp_path: Path):
    """Trino types the builders' 1.0 as decimal(2,1) and AVG keeps that scale,
    so without double literals every fraction is rounded to one decimal place.
    customer_ref is numeric-only on 166 of 500 rows: 0.332, never 0.3."""

    seed_repo(tmp_path, catalogs=["memory.app"])
    adapter = _adapter(tmp_path, budget=BUDGET)
    try:
        _meta, columns = adapter.table_metadata("memory.app.customers")
        ref = [c for c in columns if c.name == "customer_ref"]
        (aggregate,) = adapter.column_aggregates(
            "memory.app.customers", ref, type_stats={"customer_ref"}
        )
    finally:
        adapter.close()
    assert aggregate.numeric_string_fraction == pytest.approx(166 / 500, abs=1e-9)


def test_orphan_verification_finds_the_seeded_orphans(tmp_path: Path, capsys):
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUDGET)
    _map(tmp_path, capsys, "--verify")
    edge = next(
        r
        for r in _cache(tmp_path)["relationships"]
        if r["from_dataset"] == "memory.app.order_items"
        and r["from_columns"] == ["product_id"]
        and r["to_dataset"] == "memory.app.products"
    )
    assert edge["verified"] is True
    assert edge["orphan_fraction"] == pytest.approx(40 / 5000, abs=0.0005)


def test_temporal_continuity_reports_the_seeded_gap(tmp_path: Path, capsys):
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "memory.app.events",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    events = next(
        d
        for d in _cache(tmp_path)["datasets"]
        if d["identifier"] == "memory.app.events"
    )
    occurred = next(c for c in events["columns"] if c["name"] == "occurred_at")
    assert occurred["temporal_granularity"] == "day"
    assert occurred["temporal_distinct_periods"] == 87
    assert occurred["temporal_largest_gap"] == 3
    recorded = next(c for c in events["columns"] if c["name"] == "recorded_at")
    assert recorded["temporal_granularity"] == "hour"


def test_the_query_firewall_admits_trino_idioms_and_refuses_what_it_must(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, catalogs=["memory.app", "iceberg.app"], budget=BUDGET)
    _map(tmp_path, capsys)

    admitted = [
        "SELECT city, count_if(phone IS NOT NULL) AS with_phone, count(*) AS n "
        "FROM memory.app.customers GROUP BY city ORDER BY n DESC",
        "SELECT k, count(*) AS n FROM memory.app.products "
        "CROSS JOIN UNNEST(CAST(json_parse(attrs) AS MAP(VARCHAR, JSON))) AS u(k, v) "
        "GROUP BY k ORDER BY k",
        "SELECT approx_distinct(email) AS people FROM memory.app.customers",
    ]
    for sql in admitted:
        rc, envelope = _query(tmp_path, capsys, sql)
        assert rc == 0, (sql, envelope)
        assert envelope["data"]["row_count"] > 0

    refused = {
        "SELECT email FROM memory.app.customers LIMIT 5": "email",
        "SELECT count(*) FROM app.customers": "fully qualified",
        "SELECT * FROM TABLE(memory.system.query(query => 'SELECT 1'))": (
            "function query is not permitted"
        ),
        'SELECT * FROM iceberg.app."orders$files"': "orders$files",
    }
    for sql, fragment in refused.items():
        rc, envelope = _query(tmp_path, capsys, sql)
        assert rc == 1, (sql, envelope)
        assert fragment in envelope["errors"][0], (sql, envelope["errors"])


def test_time_travel_to_a_dropped_pii_column_is_refused_on_real_history(
    tmp_path: Path, capsys, trino_url
):
    """The seed dropped customer_profiles.ssn after its first insert. Read as of
    that snapshot, the table still returns ssn with its values; the cache, built
    from the current schema, has never heard of the column."""

    cursor = trino_client(trino_url).cursor()
    cursor.execute(
        'SELECT snapshot_id FROM iceberg.app."customer_profiles$snapshots" '
        "WHERE summary['total-records'] = '300'"
    )
    (snapshot,) = cursor.fetchall()[0]
    cursor.execute(
        f"SELECT * FROM iceberg.app.customer_profiles FOR VERSION AS OF {snapshot} "  # noqa: S608
        "LIMIT 1"
    )
    cursor.fetchall()
    assert "ssn" in [d[0] for d in cursor.description], "the seed's history is gone"

    seed_repo(tmp_path, catalogs=["iceberg.app"], budget=BUDGET)
    _map(tmp_path, capsys)
    profiles = next(
        d
        for d in _cache(tmp_path)["datasets"]
        if d["identifier"] == "iceberg.app.customer_profiles"
    )
    assert "ssn" not in {c["name"] for c in profiles["columns"]}

    rc, envelope = _query(
        tmp_path,
        capsys,
        f"SELECT * FROM iceberg.app.customer_profiles FOR VERSION AS OF {snapshot}",  # noqa: S608
    )
    assert rc == 1
    assert "time travel is not permitted" in envelope["errors"][0]


def _adapter(tmp_path: Path, budget: float):
    from exmergo_dex_core.config import load_config
    from exmergo_dex_core.connect import open_adapter
    from exmergo_dex_core.storage import MemoryStore

    return open_adapter(
        config=load_config(tmp_path),
        store=MemoryStore(),
        budget=budget,
        confirmed=True,
        command="explore query",
    )


def test_the_run_time_cap_kills_a_statement_the_estimate_underpriced(tmp_path: Path):
    """A cross join of two small tables is quoted from their sizes and runs far
    longer than that. The server-side query_max_run_time is what stops it, at
    the budget, and what it ran for is still settled."""

    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    seed_repo(tmp_path, catalogs=["tpch.tiny"])
    adapter = _adapter(tmp_path, budget=3.0)
    try:
        with pytest.raises(OverCeilingError, match="query_max_run_time"):
            adapter.run_query(
                "SELECT count(*) FROM tpch.tiny.lineitem a "
                "CROSS JOIN tpch.tiny.lineitem b WHERE a.comment < b.comment",
                max_rows=10,
                timeout_seconds=600,
            )
        settled = adapter.cost_gate.spend_summary()["seconds_billed"]
        assert 1.0 <= settled <= 30.0
    finally:
        adapter.close()


def test_the_scan_cap_dex_sets_binds_on_iceberg(tmp_path: Path, monkeypatch):
    """The physical-bytes cap is derived from the estimate's own scan rate, which
    a laptop cannot outrun before the run-time cap fires. So the rate is lowered
    here and the statement goes straight through the billed door, past the
    charge the lowered rate would also inflate. What is proved is that the cap
    dex sets reaches the server and is enforced there on real Parquet, as a
    budget refusal."""

    from exmergo_dex_core.adapters import trino as trino_adapter
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    seed_repo(tmp_path, catalogs=["iceberg.app"])
    adapter = _adapter(tmp_path, budget=30.0)
    monkeypatch.setattr(trino_adapter, "_SCAN_BYTES_PER_SECOND", 100_000)
    sql = trino_adapter.guard(
        "SELECT count(*) FROM iceberg.app.orders a "
        "JOIN iceberg.app.orders b ON a.custkey = b.custkey "
        "WHERE a.comment < b.comment"
    )
    try:
        with pytest.raises(OverCeilingError, match="query_max_scan_physical_bytes"):
            adapter._run_rows(sql, timeout_seconds=600)
    finally:
        adapter.close()


def test_a_partial_fetch_is_cancelled_server_side(tmp_path: Path, trino_url):
    """Fetching stops at the row cap; the query would keep reading, and nothing
    would record it. The coordinator has to show it ended."""

    seed_repo(tmp_path, catalogs=["tpch.sf1"])
    adapter = _adapter(tmp_path, budget=BUDGET)
    try:
        result = adapter.run_query(
            "SELECT orderkey FROM tpch.sf1.lineitem", max_rows=5, timeout_seconds=600
        )
    finally:
        adapter.close()
    assert result.truncated is True
    cursor = trino_client(trino_url, user="admin").cursor()
    cursor.execute(
        "SELECT state, error_code FROM system.runtime.queries "
        "WHERE query = 'SELECT orderkey FROM tpch.sf1.lineitem' "
        "ORDER BY created DESC LIMIT 1"
    )
    state, error_code = cursor.fetchall()[0]
    assert state != "RUNNING"
    assert state == "FINISHED" or error_code == "USER_CANCELED"
