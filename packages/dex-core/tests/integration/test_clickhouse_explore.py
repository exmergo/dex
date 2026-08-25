"""Live explore against ClickHouse: free inventory, the confirm handshake, a
gated profile with PII flags, the query firewall, and the two hazards that only
a live server can prove.

The target is the seeded container from scripts/setup_clickhouse_dev.sh, which
CI runs too. Nothing here bills money; db-load gating is exercised for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.cli import main  # noqa: F401  (imported for parity)

from .conftest import CH_MAX_SECONDS
from .test_clickhouse_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.clickhouse]

BUDGET = CH_MAX_SECONDS


@pytest.fixture(autouse=True)
def _dsn(ch_dsn, monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", ch_dsn)


def test_inventory_is_free_and_two_part(tmp_path: Path, capsys):
    seed_repo(tmp_path, databases=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert rc == 0, envelope
    assert envelope["cost"]["estimate"] == 0.0
    objects = envelope["data"]["objects"]
    assert objects
    for obj in objects:
        assert obj["identifier"].startswith("app.")
        assert obj["identifier"].count(".") == 1, "identifiers are database.table"
    names = {o["identifier"] for o in objects}
    assert {"app.customers", "app.orders", "app.events"} <= names
    # The view reports no row count rather than zero, so volume drift cannot
    # read it as an emptied table.
    view = next(o for o in objects if o["identifier"] == "app.v_order_totals")
    assert view["object_type"] == "view"
    assert view.get("row_count") is None


def test_an_unconfirmed_map_asks_before_it_scans(tmp_path: Path, capsys):
    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "explore", "map"], capsys)
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["cost"]["estimate"] > 0
    assert envelope["cost"]["paradigm"] == "db_load"
    payload = envelope["data"]
    assert payload["estimate_quality"] == "heuristic"
    assert "max_execution_time" in payload["hint"]
    assert not (tmp_path / ".dex" / "cache.json").exists()


def test_an_over_ceiling_map_refuses_and_spends_nothing(tmp_path: Path, capsys):
    seed_repo(tmp_path, databases=["app"], budget=0.0001)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 1
    assert "ceiling" in envelope["errors"][0].lower()
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_a_confirmed_map_profiles_flags_pii_and_records_spend(tmp_path: Path, capsys):
    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--verify",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["object_count"] >= 8
    assert data["pii_column_count"] >= 4
    assert data["spend"]["seconds_billed"] > 0

    import json

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text())
    by_id = {d["identifier"]: d for d in cache["datasets"]}

    # PII is flagged, never surfaced: the category and confidence cross, no value.
    customers = by_id["app.customers"]
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"]["category"] == "email"
    assert email["pii"]["confidence"] >= 0.5
    assert email.get("min_value") is None and email.get("max_value") is None

    # Nullability comes from the type constructor, since ClickHouse has no
    # is_nullable column. Getting this wrong is silent.
    phone = next(c for c in customers["columns"] if c["name"] == "phone")
    assert phone["data_type"].startswith("Nullable(")
    city = next(c for c in customers["columns"] if c["name"] == "city")
    assert city["data_type"] == "LowCardinality(Nullable(String))"
    assert city["null_fraction"] == 0.0


def test_orphan_verification_actually_finds_the_seeded_orphans(tmp_path: Path, capsys):
    """The sharpest ClickHouse-specific hazard, asserted against real data.

    ClickHouse defaults join_use_nulls to 0, which fills an unmatched LEFT JOIN
    row with the column type's default rather than NULL. The shared overlap
    probe counts orphans with `IS NULL`, so with the default this reports a
    perfectly clean join and maintain grain's join-fanout half never fires.

    The seed puts exactly 40 of 5000 order_items rows on products that do not
    exist, so the expected fraction is 0.008. A result of 0.0 here means the
    session setting was lost, not that the data is clean.
    """

    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--verify",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope

    import json

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text())
    edge = next(
        r
        for r in cache["relationships"]
        if r["from_dataset"] == "app.order_items"
        and r["from_columns"] == ["product_id"]
        and r["to_dataset"] == "app.products"
    )
    assert edge["verified"] is True
    assert edge["orphan_fraction"] == pytest.approx(0.008, abs=0.0005), (
        "0.0 here means join_use_nulls was lost: an unmatched LEFT JOIN row "
        "yielded the type default instead of NULL and every orphan vanished"
    )


def test_temporal_continuity_reports_the_seeded_gap(tmp_path: Path, capsys):
    """The second silent hazard. ClickHouse has no LAG, and the naive lagInFrame
    rewrite returns the type default past the frame edge, which makes the first
    row compare against the epoch and report a ~20,000 day gap.

    The seed removes exactly 3 consecutive days from a 90-day span, so the
    correct answers are 87 distinct days and a largest gap of 3, which is what
    DuckDB reports for the same shape.
    """

    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "app.events",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope

    import json

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text())
    events = next(d for d in cache["datasets"] if d["identifier"] == "app.events")
    occurred = next(c for c in events["columns"] if c["name"] == "occurred_at")
    assert occurred["temporal_granularity"] == "day"
    assert occurred["temporal_span"] == 90
    assert occurred["temporal_distinct_periods"] == 87
    assert occurred["temporal_missing_periods"] == 3
    assert occurred["temporal_largest_gap"] == 3, (
        "a gap of 20590 means lagInFrame returned the epoch for the first row; "
        "a gap of 0 means it returned the current row instead of the previous"
    )

    # And a DateTime column resolves to hour granularity. Its spelling contains
    # DATE and not TIMESTAMP, so the shared date-only check would otherwise
    # claim it and silently report only day and month, which reads as a clean
    # result rather than a skipped one.
    recorded = next(c for c in events["columns"] if c["name"] == "recorded_at")
    assert recorded["data_type"] == "DateTime"
    assert recorded["temporal_granularity"] == "hour"
    assert recorded["temporal_distinct_periods"] > 0


def test_the_query_firewall_admits_clickhouse_idioms_and_blocks_pii(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )

    # countIf and FINAL are what a ClickHouse user actually writes.
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT city, countIf(phone IS NOT NULL) AS with_phone, count() AS n "
            "FROM app.customers GROUP BY city ORDER BY n DESC",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["row_count"] > 0

    # A row-level projection of a flagged column stays refused.
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT email FROM app.customers LIMIT 5",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 1
    assert "email" in envelope["errors"][0]


def test_array_join_over_a_column_is_admitted(tmp_path: Path, capsys):
    """ClickHouse's unnest is ARRAY JOIN, which is a Join node rather than a
    FROM source, so the taint rule reaches it by a different path than every
    other dialect."""

    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT tag, count() AS n FROM app.products ARRAY JOIN tags AS tag "
            "GROUP BY tag ORDER BY n DESC",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["row_count"] > 0


def test_scope_cannot_widen_the_committed_allowlist_live(tmp_path: Path, capsys):
    seed_repo(tmp_path, databases=["app"], budget=BUDGET)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "--scope",
            "dbt_dev",
            "explore",
            "inventory",
        ],
        capsys,
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
