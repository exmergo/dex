"""Live explore against public BigQuery data: free inventory, the confirm
handshake with a real dry-run estimate, the over-ceiling refusal (free), and a
firewalled query. Reads only bigquery-public-data; bills to the test project;
every scan is capped by the suite's byte ceiling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, DexStore

from .conftest import MAX_BYTES
from .test_bigquery_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]

SHAKESPEARE = "bigquery-public-data.samples.shakespeare"
# A deliberately large table (tens of GB): its dry-run estimate must blow any
# sane test budget, proving the refusal live at zero cost.
WIKIPEDIA = "bigquery-public-data.samples.wikipedia"


def test_inventory_is_free_and_ranked(tmp_path: Path, capsys, bq_project: str):
    seed_repo(tmp_path, bq_project)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert rc == 0, envelope
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert SHAKESPEARE in identifiers
    assert envelope["cost"]["paradigm"] == "bytes_scanned"
    assert envelope["cost"]["estimate"] in (0.0, None)


def test_profile_handshake_then_confirmed_run(tmp_path: Path, capsys, bq_project: str):
    seed_repo(tmp_path, bq_project)
    root = str(tmp_path)

    rc, first = run_cli(
        ["--repo-root", root, "explore", "profile", "shakespeare"], capsys
    )
    assert rc == 0
    assert first["status"] == "needs_confirmation"
    estimate = first["cost"]["estimate"]
    assert 0 < estimate <= MAX_BYTES, "shakespeare is a few MB"
    assert first["data"]["per_table_bytes"][SHAKESPEARE] == estimate

    rc, second = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "profile",
            "shakespeare",
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 0, second
    assert second["status"] == "ok"
    dataset = second["data"]["datasets"][0]
    assert dataset["identifier"] == SHAKESPEARE
    columns = {c["name"] for c in dataset["columns"]}
    assert {"word", "word_count", "corpus", "corpus_date"} <= columns
    # The estimate the agent confirmed is what the envelope reports; actual
    # spend (cache hits can make it 0) is in data.spend and the ledger.
    assert second["cost"]["estimate"] == estimate
    spend = second["data"]["spend"]
    assert spend["bytes_billed"] <= MAX_BYTES
    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    assert ledger, "billed commands always leave a ledger entry"


def test_over_ceiling_refusal_is_live_and_free(tmp_path: Path, capsys, bq_project: str):
    seed_repo(tmp_path, bq_project)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "wikipedia",
            "--confirm",
            "--budget",
            "1000",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "ceiling" in envelope["errors"][0]
    # Refused at the dry-run stage: no ledger, no spend.
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_firewalled_query_round_trip(tmp_path: Path, capsys, bq_project: str):
    seed_repo(tmp_path, bq_project)
    # The firewall's PII policy is computed from the cache; seed it directly
    # (the cache is a non-canonical artifact) so this test scans once, not twice.
    DexStore(tmp_path).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=SHAKESPEARE,
                    columns=[
                        ColumnProfile(name="word", data_type="STRING"),
                        ColumnProfile(name="word_count", data_type="INT64"),
                        ColumnProfile(name="corpus", data_type="STRING"),
                        ColumnProfile(name="corpus_date", data_type="INT64"),
                    ],
                )
            ]
        )
    )
    # Agent-shaped SQL over a fixed public table; the engine's query firewall
    # is exactly the layer under test here.
    sql = (
        "SELECT corpus, SUM(word_count) AS words "  # noqa: S608
        f"FROM `{'`.`'.join(SHAKESPEARE.split('.'))}` "
        "GROUP BY corpus ORDER BY words DESC LIMIT 5"
    )
    root = str(tmp_path)

    rc, first = run_cli(["--repo-root", root, "explore", "query", sql], capsys)
    assert rc == 0
    assert first["status"] == "needs_confirmation"

    rc, second = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "query",
            sql,
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 0, second
    assert second["status"] == "ok"
    assert second["data"]["columns"] == ["corpus", "words"]
    assert len(second["data"]["cells"]) == 5
    decisions = [
        json.loads(line)["decision"]
        for line in (tmp_path / ".dex" / "queries.jsonl").read_text().splitlines()
    ]
    assert decisions == ["needs_confirmation", "allowed"]


def test_unnest_of_a_function_derived_array_runs_live(
    tmp_path: Path, capsys, bq_project: str
):
    # The FROM-clause UNNEST the firewall now admits, exercised end to end on
    # BigQuery. The array derives from an allowlisted JSON function over a
    # literal, so the probe scans zero table bytes; a real column works the
    # same way (the unit suite covers taint inheritance).
    seed_repo(tmp_path, bq_project)
    DexStore(tmp_path).save_cache(DexCache(datasets=[]))
    root = str(tmp_path)

    sql = 'SELECT k FROM UNNEST(JSON_EXTRACT_ARRAY(\'["x","y","z"]\')) AS k'
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "query",
            sql,
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["row_count"] == 3

    # The smuggle shape is refused statically, before any job is created.
    bad = f"SELECT k FROM UNNEST((SELECT ARRAY_AGG(word) FROM `{SHAKESPEARE}`)) AS k"  # noqa:S608
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "explore",
            "query",
            bad,
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 1
    assert "query refused" in envelope["errors"][0]


# --- scope resolution against the live project (free: metadata GET, no query) ---------


def test_a_bogus_scope_is_refused_for_free(tmp_path: Path, capsys, bq_project):
    """The cost-safety bug: a scope that resolves to nothing used to reach
    list_tables and die on a raw google NotFound, naming neither the fix nor the
    datasets that do exist."""

    seed_repo(tmp_path, bq_project, datasets=["__no_such_dataset__"])
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "__no_such_dataset__" in error
    assert "[from bigquery.datasets in .dex/config.yml]" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_scope_cannot_widen_the_committed_allowlist_live(
    tmp_path: Path, capsys, bq_project
):
    seed_repo(tmp_path, bq_project, datasets=["bigquery-public-data.samples"])
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--scope",
            "bigquery-public-data.austin_bikeshare",
        ],
        capsys,
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()
