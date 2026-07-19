"""Live explore against Snowflake sample data: free inventory, the confirm
handshake with a heuristic seconds estimate (credits alongside), the
over-ceiling refusal (free), and a firewalled query. Reads only
SNOWFLAKE_SAMPLE_DATA; warehouse time bills to the pinned X-Small, capped per
statement by the suite's second ceiling."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, DexStore

from .conftest import SF_MAX_SECONDS
from .test_snowflake_connect import SAMPLE_SCOPE, run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.snowflake]

REGION = f"{SAMPLE_SCOPE}.REGION"  # 5 rows: the cheapest possible scan
# Deliberately large (TPCH SF1000 lineitem is ~170 GB): its heuristic estimate
# must blow any sane test budget, proving the refusal live at zero cost.
HUGE_SCOPE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000"
HUGE = f"{HUGE_SCOPE}.LINEITEM"


def test_inventory_is_free_and_ranked(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert rc == 0, envelope
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert REGION in identifiers
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert envelope["cost"]["estimate"] in (0.0, None)


def test_profile_handshake_then_confirmed_run(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    root = str(tmp_path)

    rc, first = run_cli(["--repo-root", root, "explore", "profile", "REGION"], capsys)
    assert rc == 0
    assert first["status"] == "needs_confirmation"
    estimate = first["cost"]["estimate"]
    assert estimate > 0
    assert first["data"]["estimate_quality"] == "heuristic"
    assert "seconds" in first["data"]["hint"]
    # The credit translation rides alongside the binding seconds figure.
    assert first["data"]["estimated_credits"] > 0
    assert first["data"]["per_table_seconds"][REGION] > 0

    budget = max(estimate * 2, SF_MAX_SECONDS)
    rc, second = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "profile",
            "REGION",
            "--confirm",
            "--budget",
            str(budget),
        ],
        capsys,
    )
    assert rc == 0, second
    assert second["status"] == "ok"
    dataset = second["data"]["datasets"][0]
    assert dataset["identifier"] == REGION
    columns = {c["name"] for c in dataset["columns"]}
    assert {"R_REGIONKEY", "R_NAME", "R_COMMENT"} <= columns
    # The standing name over-flag, live: R_NAME (5 distinct all-caps region
    # labels) keeps its flag but de-rates below the firewall threshold on
    # value-shape evidence computed in the same scan.
    by_name = {c["name"]: c for c in dataset["columns"]}
    r_name = by_name["R_NAME"]
    assert r_name["pii"]["category"] == "name"
    assert r_name["pii"]["confidence"] < 0.5
    # The estimate the agent confirmed is what the envelope reports; actual
    # spend (wall seconds) is in data.spend and the ledger.
    assert second["cost"]["estimate"] == pytest.approx(estimate, rel=0.5)
    spend = second["data"]["spend"]
    assert spend["seconds_billed"] >= 0
    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    assert ledger, "billed commands always leave a ledger entry"


def test_over_ceiling_refusal_is_live_and_free(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(
        tmp_path,
        sf_scratch_database,
        sf_warehouse,
        sf_connection_name,
        databases=[HUGE_SCOPE],
    )
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "LINEITEM",
            "--confirm",
            "--budget",
            "2",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "ceiling" in envelope["errors"][0]
    # Refused at the estimate stage: no ledger, no spend.
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_firewalled_query_round_trip(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    # The firewall's PII policy is computed from the cache; seed it directly
    # (the cache is a non-canonical artifact) so this test scans once, not twice.
    DexStore(tmp_path).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=REGION,
                    columns=[
                        ColumnProfile(name="R_REGIONKEY", data_type="FIXED"),
                        ColumnProfile(name="R_NAME", data_type="TEXT"),
                        ColumnProfile(name="R_COMMENT", data_type="TEXT"),
                    ],
                )
            ]
        )
    )
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            f"SELECT COUNT(*) AS n FROM {REGION}",  # noqa: S608 (test SQL, fixed table)
            "--confirm",
            "--budget",
            # Covers the 60s resume minimum when the warehouse is cold: the
            # estimate honestly includes it, so the budget must too.
            str(SF_MAX_SECONDS + 90),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["cells"] == [[5]]
    assert envelope["data"]["spend"]["seconds_billed"] >= 0


def test_flatten_of_a_parsed_json_literal_runs_live(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    # The FROM-clause FLATTEN the firewall now admits, exercised end to end.
    # The input derives from an allowlisted function over a literal, so no
    # table is read; a VARIANT column works the same way (the unit suite
    # covers taint inheritance).
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    DexStore(tmp_path).save_cache(DexCache(datasets=[]))
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT f.key AS k FROM TABLE(FLATTEN(input => "
            'PARSE_JSON(\'{"a": 1, "b": 2}\'))) f ORDER BY k',
            "--confirm",
            "--budget",
            str(SF_MAX_SECONDS + 90),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert [row[0] for row in envelope["data"]["cells"]] == ["a", "b"]


def test_scope_bounds_the_estimate_to_the_named_schema(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    """A scope that is honored bounds the spend. The committed allowlist here is
    the whole sample database, which spans four orders of magnitude in table size;
    `--scope TPCH_SF1` must quote an estimate for those eight tables alone.
    """

    seed_repo(
        tmp_path,
        sf_scratch_database,
        sf_warehouse,
        sf_connection_name,
        databases=["SNOWFLAKE_SAMPLE_DATA"],
    )
    root = str(tmp_path)
    rc, scoped = run_cli(
        ["--repo-root", root, "explore", "map", "--scope", "TPCH_SF1"], capsys
    )
    assert rc == 0, scoped
    assert scoped["status"] == "needs_confirmation"
    per_table = scoped["data"]["per_table_seconds"]
    assert {ident.split(".")[1] for ident in per_table} == {"TPCH_SF1"}
    assert len(per_table) == 8

    rc, unscoped = run_cli(["--repo-root", root, "explore", "map"], capsys)
    assert rc == 0, unscoped
    # The whole point: scoping is worth orders of magnitude, and before this was
    # honored both calls returned the same estimate.
    assert scoped["cost"]["estimate"] < unscoped["cost"]["estimate"] / 1000
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_bogus_scope_is_refused_for_free(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(
        tmp_path,
        sf_scratch_database,
        sf_warehouse,
        sf_connection_name,
        databases=["SNOWFLAKE_SAMPLE_DATA"],
    )
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--scope",
            "__NONEXISTENT_SCHEMA__",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "__NONEXISTENT_SCHEMA__" in error
    # The refusal names what does exist, which the raw 002043 never did.
    assert "TPCH_SF1" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_scope_cannot_widen_the_committed_allowlist_live(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    """The committed allowlist keeps the ~170 GB SF1000 schema out of reach; a
    flag must not be able to pull it back in."""

    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--scope", HUGE_SCOPE], capsys
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_bigquery_flags_are_refused_on_snowflake(
    tmp_path: Path, capsys, sf_scratch_database, sf_warehouse, sf_connection_name
):
    seed_repo(tmp_path, sf_scratch_database, sf_warehouse, sf_connection_name)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--dataset", "TPCH_SF1"],
        capsys,
    )
    assert rc == 1
    assert "--dataset" in envelope["errors"][0]
    assert "--scope" in envelope["errors"][0]
