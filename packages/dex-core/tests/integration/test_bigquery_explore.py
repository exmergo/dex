"""Live explore against public BigQuery data: free inventory, the confirm
handshake with a real dry-run estimate, the over-ceiling refusal (free), a
firewalled query, temporal continuity over a DATETIME column, and key value
shapes. Reads bigquery-public-data plus three tiny tables this suite writes
into the scratch dataset; bills to the test project; every scan is capped by
the suite's byte ceiling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache
from exmergo_dex_core.storage import FilesystemStore

from .conftest import MAX_BYTES, assert_ok
from .test_bigquery_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]

SHAKESPEARE = "bigquery-public-data.samples.shakespeare"
# A deliberately large table (tens of GB): its dry-run estimate must blow any
# sane test budget, proving the refusal live at zero cost.
WIKIPEDIA = "bigquery-public-data.samples.wikipedia"
CONTINUITY_TABLE = "dex_temporal_continuity"


def test_inventory_is_free_and_ranked(tmp_path: Path, capsys, bq_project: str):
    seed_repo(tmp_path, bq_project)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory", "--rank"], capsys
    )
    assert_ok(rc, envelope)
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
            # This test wants the real schema profiling found, not the
            # finding-only default summary (#288): none of Shakespeare's four
            # columns carries a PII flag, a null fraction, key membership, or
            # a data-quality note, so the default summary would elide all of
            # them rather than the one this assertion used to catch missing.
            "--columns",
            "all",
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
    FilesystemStore(tmp_path).save_cache(
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
    FilesystemStore(tmp_path).save_cache(DexCache(datasets=[]))
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
    assert_ok(rc, envelope)
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


# --- temporal continuity over a written fixture (the one shape public data lacks) -----


@pytest.fixture
def bq_continuity_table(bq_project: str, bq_scratch_dataset: str):
    """One tiny table in the scratch dataset carrying a known hole at both
    grains, because no public dataset offers a DATETIME column with a hole
    whose size this suite can state.

    Forty-eight rows are generated and three are dropped, leaving 45.
    ``occurred_at`` advances one day per row from 2024-03-01, ``recorded_at``
    one hour per row from 2024-03-01 00:30 (never midnight, so the column is
    not day-aligned and hour is the grain the engine reports). The three
    dropped rows are a 3-day hole in one column and a 3-hour hole in the
    other; four later rows keep their date but carry a NULL ``recorded_at``,
    so the hour grain has a second, wider hole of 4 that the day grain does
    not. The two grains therefore report different numbers, and hour
    statistics that were really the day column's would not pass.

    Written and dropped by this fixture: the CI principal holds dataEditor on
    the scratch dataset and nowhere else, and its table TTL is the backstop for
    a crashed run.
    """

    from google.cloud import bigquery

    identifier = f"{bq_project}.{bq_scratch_dataset}.{CONTINUITY_TABLE}"
    client = bigquery.Client(project=bq_project)
    try:
        # DDL over generated literals: no table is read, so this scans nothing.
        client.query(
            f"CREATE OR REPLACE TABLE `{identifier}` AS SELECT "  # noqa: S608
            "DATE_ADD(DATE '2024-03-01', INTERVAL i DAY) AS occurred_at, "
            "IF(i BETWEEN 20 AND 23, NULL, DATETIME_ADD("
            "DATETIME '2024-03-01 00:30:00', INTERVAL i HOUR)) AS recorded_at "
            "FROM UNNEST(GENERATE_ARRAY(0, 47)) AS i "
            "WHERE i NOT IN (10, 11, 12)"
        ).result()
        yield identifier
    finally:
        client.delete_table(identifier, not_found_ok=True)
        client.close()


def test_temporal_continuity_reports_the_seeded_hour_grain_hole(
    tmp_path: Path,
    capsys,
    bq_project: str,
    bq_scratch_dataset: str,
    bq_continuity_table: str,
):
    """The hazard that reads as a clean result rather than a missing one.

    ``DATETIME`` contains the substring DATE and not TIMESTAMP, so the shared
    date-only check claimed it and every DATETIME column on BigQuery reported
    day and month continuity while silently never reporting an hour gap. This
    is BigQuery's own end-to-end proof that the hour grain is computed and
    correct, the assertion the ClickHouse suite already carries for
    ``DateTime`` and the connector that held the bug longest lacked.

    It also pins the other direction: a bare ``DATE`` column must still skip
    the hour grain, because ``DATE_TRUNC`` is the one truncation function
    BigQuery gives no HOUR unit, and asking for one is an error, not a wrong
    number.
    """

    seed_repo(tmp_path, bq_project, datasets=[f"{bq_project}.{bq_scratch_dataset}"])
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            CONTINUITY_TABLE,
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    dataset = envelope["data"]["datasets"][0]
    assert dataset["identifier"] == bq_continuity_table
    columns = {c["name"]: c for c in dataset["columns"]}

    recorded = columns["recorded_at"]
    assert recorded["data_type"] == "DATETIME"
    assert recorded["temporal_granularity"] == "hour", (
        "a granularity of day here means the date-only check claimed DATETIME "
        "again: the hour grain was never computed and its absence reads clean"
    )
    assert recorded["temporal_span"] == 48
    assert recorded["temporal_distinct_periods"] == 41
    assert recorded["temporal_missing_periods"] == 7
    assert recorded["temporal_largest_gap"] == 4

    occurred = columns["occurred_at"]
    assert occurred["data_type"] == "DATE"
    assert occurred["temporal_granularity"] == "day"
    assert occurred["temporal_span"] == 48
    assert occurred["temporal_distinct_periods"] == 45
    assert occurred["temporal_missing_periods"] == 3
    assert occurred["temporal_largest_gap"] == 3


# --- key value shapes over written fixtures -------------------------------------------

KEY_SHAPE_GEO = "dex_key_shape_geo"
KEY_SHAPE_SKUS = "dex_key_shape_skus"
# Five of the twenty (25% of rows) are spelt in the letters A to F only.
KEY_SHAPE_COUNTRIES = "AD BE CA DE EC US GB FR IT JP NL NO SE PL PT IN MX ZA KR NZ"


@pytest.fixture
def bq_key_shape_tables(bq_project: str, bq_scratch_dataset: str):
    """Two tiny tables for the key value-shape check, one per direction.

    ``dex_key_shape_geo`` is keyed by ``(day, country)`` over two-letter
    country codes, a fifth of them spelt only in A to F, so it must stay
    silent (#481). ``dex_key_shape_skus`` is the merged-catalogue key: 270
    numeric ids and 30 md5 hashes, so it must still report both shapes.

    Written and dropped by this fixture, under the same scratch-dataset rules
    as ``bq_continuity_table``.
    """

    from google.cloud import bigquery

    geo = f"{bq_project}.{bq_scratch_dataset}.{KEY_SHAPE_GEO}"
    skus = f"{bq_project}.{bq_scratch_dataset}.{KEY_SHAPE_SKUS}"
    codes = ", ".join(f"'{c}'" for c in KEY_SHAPE_COUNTRIES.split())
    client = bigquery.Client(project=bq_project)
    try:
        # DDL over generated literals: no table is read, so this scans nothing.
        client.query(
            f"CREATE OR REPLACE TABLE `{geo}` AS SELECT "  # noqa: S608
            "DATE_ADD(DATE '2026-09-01', INTERVAL d DAY) AS day, country "
            f"FROM UNNEST(GENERATE_ARRAY(0, 9)) AS d, UNNEST([{codes}]) AS country"
        ).result()
        client.query(
            f"CREATE OR REPLACE TABLE `{skus}` AS SELECT "  # noqa: S608
            "i AS product_id, IF(i <= 270, CAST(400000 + i AS STRING), "
            "TO_HEX(MD5(CONCAT('catalog-merge-', CAST(i AS STRING))))) AS sku "
            "FROM UNNEST(GENERATE_ARRAY(1, 300)) AS i"
        ).result()
        yield geo, skus
    finally:
        client.delete_table(geo, not_found_ok=True)
        client.delete_table(skus, not_found_ok=True)
        client.close()


def test_key_value_shapes_ignore_short_codes_and_still_catch_mixed_ids(
    tmp_path: Path,
    capsys,
    bq_project: str,
    bq_scratch_dataset: str,
    bq_key_shape_tables: tuple[str, str],
):
    """The key-shape check's regexes run inside BigQuery, so this is the only
    place their reading of the shared patterns is proven: a country-code key
    is one shape here, and a numeric key with md5 hashes merged into it is
    two, with the cast warning."""

    seed_repo(tmp_path, bq_project, datasets=[f"{bq_project}.{bq_scratch_dataset}"])
    datasets: dict[str, dict] = {}
    for table in (KEY_SHAPE_GEO, KEY_SHAPE_SKUS):
        rc, envelope = run_cli(
            [
                "--repo-root",
                str(tmp_path),
                "explore",
                "profile",
                table,
                "--confirm",
                "--budget",
                str(MAX_BYTES),
            ],
            capsys,
        )
        assert_ok(rc, envelope)
        (datasets[table],) = envelope["data"]["datasets"]

    geo = datasets[KEY_SHAPE_GEO]
    # The country column is a key member, so the check did look at it.
    assert set(geo["grain"]) == {"day", "country"}, geo["key_evidence"]
    geo_notes = " ".join(geo["data_quality"])
    assert "mixes value shapes" not in geo_notes, geo_notes
    sku_notes = " ".join(datasets[KEY_SHAPE_SKUS]["data_quality"])
    assert "sku is a candidate key but mixes value shapes" in sku_notes, sku_notes
    assert "90% numeric, 10% 32-character hexadecimal (md5-shaped)" in sku_notes
    assert "casting to a number or comparing numerically" in sku_notes


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
