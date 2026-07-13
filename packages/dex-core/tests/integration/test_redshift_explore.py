"""Live explore against the seeded Redshift workgroup: cheap inventory, the
strict compute-time handshake with the Serverless wake floor, PII
flag-not-surface, and the query firewall. Spends nothing beyond the confirmed
budgets; every scanning statement is capped server-side by the
budget-derived statement_timeout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import RS_MAX_SECONDS
from .test_redshift_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.redshift]

# The knob is DEX_TEST_REDSHIFT_MAX_SECONDS (default 60): confirmed budgets
# are small fixed multiples of it, covering the Serverless wake minimum (60s)
# plus the scans over the small seeded schema.
MAP_BUDGET = RS_MAX_SECONDS * 4
QUERY_BUDGET = RS_MAX_SECONDS + 60


def test_inventory_is_ungated_and_scoped(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory"], capsys
    )
    assert rc == 0, envelope
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert any(i.endswith("app.customers") for i in identifiers)
    assert all(".app." in i for i in identifiers)
    # Metadata runs immediately: no confirmation was required, nothing gated.
    assert envelope["status"] == "ok"
    assert "spend" not in envelope["data"]


def test_unconfirmed_profile_returns_the_handshake_with_the_wake_floor(
    tmp_path: Path, capsys, rs_workgroup
):
    seed_repo(tmp_path, schemas=["app"])
    _rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "profile", "app.customers"],
        capsys,
    )
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["estimate_quality"] == "heuristic"
    assert "--confirm --budget" in envelope["data"]["hint"]
    if rs_workgroup:
        # Serverless: the 60-second wake minimum is floored in exactly once.
        assert envelope["data"]["estimated_seconds"] >= 60.0
        assert any("wake minimum" in note for note in envelope["data"]["notes"])
    else:
        assert envelope["data"]["estimated_seconds"] > 0


def test_over_ceiling_refusal_is_live_and_spends_nothing(tmp_path: Path, capsys):
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
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_confirmed_map_profiles_flags_pii_and_records_spend(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"], budget=MAP_BUDGET)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["profiled_count"] >= 4
    assert data["pii_column_count"] >= 1  # the seeded email at minimum
    assert data["spend"]["seconds_billed"] > 0

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text(encoding="utf-8"))
    # PII is flagged with min/max suppressed: no example value in the cache.
    customers = next(
        d for d in cache["datasets"] if d["identifier"].endswith("app.customers")
    )
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"]["category"] == "email"
    assert email["min_value"] is None and email["max_value"] is None
    assert "@example.com" not in json.dumps(cache)


def test_query_firewall_allows_measuring_and_refuses_pii_values(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"], budget=MAP_BUDGET)
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
            str(QUERY_BUDGET),
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
            str(QUERY_BUDGET),
        ],
        capsys,
    )
    assert rc == 1
    assert "PII" in envelope["errors"][0]


# --- scope resolution against the live database (one catalog SELECT) -----------------


def test_a_bogus_scope_is_refused_for_free(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["__no_such_schema__"])
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "__no_such_schema__" in error
    # The refusal names what does exist, which a silent empty inventory never did.
    assert "app" in error
    assert "[from redshift.schemas in .dex/config.yml]" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_scope_cannot_widen_the_committed_allowlist_live(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--scope", "public"], capsys
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()
