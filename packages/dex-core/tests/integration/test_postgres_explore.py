"""Live explore against the seeded Postgres: free inventory, the strict
db-load handshake, PII flag-not-surface, relationship inference on the
deliberately undeclared foreign key, and the query firewall. Loads nothing
beyond the confirmed budgets; every scanning statement is capped server-side
by the budget-derived statement_timeout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_postgres_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture(autouse=True)
def _dsn_env(pg_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_dsn)


def test_inventory_is_free_and_scoped(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory"], capsys
    )
    assert rc == 0, envelope
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert any(i.endswith("app.customers") for i in identifiers)
    assert all(".app." in i for i in identifiers)
    # Free: no confirmation was required and nothing was billed.
    assert envelope["status"] == "ok"
    assert "spend" not in envelope["data"]


def test_unconfirmed_profile_returns_the_handshake(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    _rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "profile", "app.customers"],
        capsys,
    )
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["estimated_seconds"] > 0
    assert envelope["data"]["estimate_quality"] == "heuristic"
    assert "--confirm --budget" in envelope["data"]["hint"]


def test_over_ceiling_refusal_is_live_and_loads_nothing(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "app.events",
            "--confirm",
            "--budget",
            "0.01",
        ],
        capsys,
    )
    assert rc == 1
    assert "exceeds the ceiling" in envelope["errors"][0]


def test_confirmed_map_profiles_flags_pii_and_infers_the_missing_fk(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["profiled_count"] >= 6
    assert data["pii_column_count"] >= 3  # email, names, phone, address
    assert data["spend"]["seconds_billed"] > 0

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text(encoding="utf-8"))
    # The deliberately undeclared FK is inferred from names and profiles.
    assert any(
        r["from_dataset"].endswith("app.order_items")
        and r["from_columns"] == ["product_id"]
        and r["to_dataset"].endswith("app.products")
        for r in cache["relationships"]
    )
    # PII is flagged with min/max suppressed: no example value in the cache.
    customers = next(
        d for d in cache["datasets"] if d["identifier"].endswith("app.customers")
    )
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"]["category"] == "email"
    assert email["min_value"] is None and email["max_value"] is None
    assert "@example.com" not in json.dumps(cache)


def test_query_firewall_allows_measuring_and_refuses_pii_values(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    rc, _ = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 0

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT status, count(*) AS n FROM app.orders GROUP BY status",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["row_count"] >= 1

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT email FROM app.customers LIMIT 5",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert rc == 1
    assert "PII" in envelope["errors"][0]
