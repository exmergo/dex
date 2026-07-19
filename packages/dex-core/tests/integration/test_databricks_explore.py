"""Live explore against the Databricks samples catalog: free inventory, the
confirm handshake with the floor seconds estimate (DBUs alongside), the
over-ceiling refusal (free), and a firewalled query. Reads only the samples
catalog; warehouse time bills to the pinned SQL warehouse, capped per
statement by STATEMENT_TIMEOUT wound to the budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, DexStore

from .conftest import DBX_MAX_SECONDS
from .test_databricks_connect import SAMPLE_SCOPE, run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.databricks]

REGION = f"{SAMPLE_SCOPE}.region"  # 5 rows: the cheapest possible scan
# samples.tpch.lineitem is tens of GB across SF variants; profiling it under a
# 2-second budget must refuse at the floor estimate, live, at zero cost.
HUGE = f"{SAMPLE_SCOPE}.lineitem"


def test_inventory_is_free_and_ranked(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert rc == 0, envelope
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert REGION in identifiers
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert envelope["cost"]["estimate"] in (0.0, None)


def test_profile_handshake_then_confirmed_run(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    root = str(tmp_path)

    rc, first = run_cli(["--repo-root", root, "explore", "profile", "region"], capsys)
    assert rc == 0
    assert first["status"] == "needs_confirmation"
    estimate = first["cost"]["estimate"]
    assert estimate > 0
    assert first["data"]["estimate_quality"] == "low"
    assert "seconds" in first["data"]["hint"]
    # The DBU translation rides alongside the binding seconds figure.
    assert first["data"]["estimated_dbus"] > 0
    assert first["data"]["per_table_seconds"][REGION] > 0

    # The floor plus the wake is honest but small; the warehouse may still be
    # starting, so give the run real headroom.
    budget = max(estimate * 3, DBX_MAX_SECONDS)
    rc, second = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "profile",
            "region",
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
    assert {"r_regionkey", "r_name", "r_comment"} <= columns
    # The estimate the agent confirmed is what the envelope reports; actual
    # spend (wall seconds) is in data.spend and the ledger.
    assert second["cost"]["estimate"] == pytest.approx(estimate, rel=0.5)
    spend = second["data"]["spend"]
    assert spend["seconds_billed"] >= 0
    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    assert ledger, "billed commands always leave a ledger entry"


def test_over_ceiling_refusal_is_live_and_free(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "lineitem",
            "--confirm",
            "--budget",
            "2",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "ceiling" in envelope["errors"][0]
    # Refused at the estimate stage: no ledger, no spend, and no SQL session.
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_firewalled_query_round_trip(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    # The firewall's PII policy is computed from the cache; seed it directly
    # (the cache is a non-canonical artifact) so this test scans once, not twice.
    DexStore(tmp_path).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=REGION,
                    columns=[
                        ColumnProfile(name="r_regionkey", data_type="bigint"),
                        ColumnProfile(name="r_name", data_type="string"),
                        ColumnProfile(name="r_comment", data_type="string"),
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
            # Covers the wake when the warehouse is cold: the estimate honestly
            # includes it, so the budget must too.
            str(DBX_MAX_SECONDS + 60),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["cells"] == [[5]]
    assert envelope["data"]["spend"]["seconds_billed"] >= 0


def test_lateral_view_explode_runs_live(
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    # The LATERAL VIEW EXPLODE shape the firewall now admits, end to end over
    # the 5-row region sample: each region row fans out to the two keys of a
    # literal JSON object through an allowlisted function.
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    DexStore(tmp_path).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=REGION,
                    columns=[
                        ColumnProfile(name="r_regionkey", data_type="bigint"),
                        ColumnProfile(name="r_name", data_type="string"),
                        ColumnProfile(name="r_comment", data_type="string"),
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
            f"SELECT k, COUNT(*) AS n FROM {REGION} "  # noqa: S608
            'LATERAL VIEW EXPLODE(json_object_keys(\'{"a": 1, "b": 2}\')) x AS k '
            "GROUP BY k ORDER BY k",
            "--confirm",
            "--budget",
            str(DBX_MAX_SECONDS + 60),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["cells"] == [["a", 5], ["b", 5]]


# --- scope resolution against the live workspace (free: Unity Catalog REST) -----------


def test_a_bogus_scope_is_refused_for_free(tmp_path: Path, capsys, dbx_warehouse):
    """The cost-safety bug: a scope that resolves to nothing used to yield an
    empty inventory, so the user scoped to nothing and was never told."""

    seed_repo(tmp_path, dbx_warehouse, None, catalogs=["samples"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--scope", "samples.__nope__"],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "__nope__" in error
    # The refusal names what does exist, and the flag it came from.
    assert "nyctaxi" in error
    assert "[from --scope]" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_a_bogus_committed_catalog_is_refused_for_free(
    tmp_path: Path, capsys, dbx_warehouse
):
    seed_repo(tmp_path, dbx_warehouse, None, catalogs=["__no_such_catalog__"])
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 1
    error = envelope["errors"][0]
    assert "__no_such_catalog__" in error
    assert "[from databricks.catalogs in .dex/config.yml]" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_scope_cannot_widen_the_committed_allowlist_live(
    tmp_path: Path, capsys, dbx_warehouse
):
    """The committed allowlist is a cost boundary: it holds this run to one
    sample schema, and a flag must not be able to reach a different one (nyctaxi
    exists, which is the point: the refusal is the boundary, not the typo)."""

    seed_repo(tmp_path, dbx_warehouse, None, catalogs=[SAMPLE_SCOPE])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--scope", "samples.nyctaxi"],
        capsys,
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()
