"""`explore cluster` end to end: the cache gate, feature selection (numeric,
non-PII, non-key), the dialect-aware sample query, and an aggregates-only
envelope that carries cluster sizes and centroids but never a row.

DuckDB is the executed connector (free, in-process, deterministic); the
sample-SQL builder is unit-tested string-for-string across every dialect."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cache import ColumnProfile, Dataset, Relationship
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore import cluster as cluster_mod
from exmergo_dex_core.explore import commands
from exmergo_dex_core.guards.sql_guard import assert_select_only

_HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None
requires_sklearn = pytest.mark.skipif(
    not _HAS_SKLEARN, reason="needs the [cluster] extra (scikit-learn)"
)


@pytest.fixture
def clusterable_duckdb(tmp_path: Path) -> Path:
    """Three well-separated numeric blobs (spend, visits) around (10,1),
    (50,5), (90,9), 100 rows each, plus columns that must NOT be auto-selected
    as features: a unique id (a key), a person name (PII + string), and lat/lng
    (numeric but PII-flagged LOCATION). Deterministic: no randomness, so k-means
    with a fixed seed recovers the three groups exactly."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "customers.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE customers AS
        SELECT
            i AS id,
            ('name_' || i) AS full_name,
            CASE i % 3 WHEN 0 THEN 10.0 WHEN 1 THEN 50.0 ELSE 90.0 END
                + (i % 5) * 0.1 AS spend,
            CASE i % 3 WHEN 0 THEN 1.0 WHEN 1 THEN 5.0 ELSE 9.0 END
                + (i % 5) * 0.1 AS visits,
            40.0 + (i % 3) AS lat,
            -74.0 - (i % 3) AS lng
        FROM range(300) t(i)
        """
    )
    conn.close()
    return path


def _cluster(db: Path, repo: Path, *args: str, capsys, expect_error: bool = False):
    rc = main(
        [
            "explore",
            "cluster",
            "customers",
            *args,
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ]
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    if expect_error:
        assert rc == 1 and payload["status"] == "error", payload
    else:
        assert rc == 0 and payload["status"] == "ok", payload
    return payload


def _mapped_repo(db: Path, tmp_path: Path, capsys) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["explore", "map", "--path", str(db), "--repo-root", str(repo)])
    capsys.readouterr()  # drain the map envelope
    assert rc == 0
    return repo


def _cluster_table(db: Path, repo: Path, table: str, *args: str, capsys):
    """`_cluster` for a table other than `customers`."""

    rc = main(
        [
            "explore",
            "cluster",
            table,
            *args,
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0 and payload["status"] == "ok", payload
    return payload


@pytest.fixture
def sampled_duckdb(tmp_path: Path) -> Path:
    """A table comfortably over the 20000-row sample cap, so the sample clause
    actually draws (a table under the cap is read whole and would be trivially
    reproducible, proving nothing). Three blobs, deterministic."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "wide.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE wide AS
        SELECT
            CASE i % 3 WHEN 0 THEN 10.0 WHEN 1 THEN 50.0 ELSE 90.0 END
                + (i % 7) * 0.1 AS spend,
            CASE i % 3 WHEN 0 THEN 1.0 WHEN 1 THEN 5.0 ELSE 9.0 END
                + (i % 7) * 0.1 AS visits
        FROM range(60000) t(i)
        """
    )
    conn.close()
    return path


@pytest.fixture
def outlier_duckdb(tmp_path: Path) -> Path:
    """One dense blob plus 3 extreme rows in 3000: the shape that makes k-means
    peel off a sub-1% cluster and report a high silhouette for it."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "spikes.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE spikes AS
        SELECT
            CASE WHEN i < 3 THEN 500000.0 ELSE 10.0 + (i % 5) * 0.1 END AS amount,
            CASE WHEN i < 3 THEN 400000.0 ELSE 5.0 + (i % 5) * 0.1 END AS duration
        FROM range(3000) t(i)
        """
    )
    conn.close()
    return path


# --- the cache gate ----------------------------------------------------------


@requires_sklearn
def test_cluster_without_cache_is_refused_with_the_fix(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys, expect_error=True)
    assert "explore map" in payload["errors"][0]
    assert not (repo / ".dex").exists(), "a refused gate writes nothing"


def test_missing_cluster_extra_is_a_clean_error(
    clusterable_duckdb: Path, tmp_path: Path, capsys, monkeypatch
):
    """A missing scikit-learn surfaces as an actionable error envelope, not a
    crash, and does so before any warehouse work (the cache gate ran, so this is
    the fail-fast the engine promises). Forced regardless of what is installed."""

    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)

    def _boom() -> None:
        raise cluster_mod.ClusterDependencyError("scikit-learn is not installed")

    monkeypatch.setattr(cluster_mod, "ensure_available", _boom)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys, expect_error=True)
    assert "scikit-learn" in payload["errors"][0]


# --- auto k-selection recovers the three blobs -------------------------------


@requires_sklearn
def test_auto_cluster_recovers_three_even_blobs(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys)
    data = payload["data"]

    assert data["object"].endswith(".customers")
    assert data["k"] == 3
    assert data["k_selection"] == "silhouette"
    # Auto-selection keeps only the two real numeric features.
    assert data["features"] == ["spend", "visits"]
    assert data["standardized"] is True
    assert data["n_samples"] == 300
    assert sorted(c["size"] for c in data["clusters"]) == [100, 100, 100]
    assert data["silhouette"] > 0.5
    # The sweep it chose from is reported for transparency.
    assert {row["k"] for row in data["k_sweep"]} >= {2, 3}

    # Centroids land near the three seeded centers, in some order.
    spends = sorted(round(c["centroid"]["spend"]) for c in data["clusters"])
    assert spends == [10, 50, 90]


@requires_sklearn
def test_cluster_envelope_is_aggregates_only(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    """No raw-row payload survives to the boundary: every cluster is a dict of
    aggregates and the envelope passes the sanitizer."""

    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys)
    data = payload["data"]
    # No forbidden raw-row key names anywhere in the payload.
    for banned in ("rows", "records", "sample_rows", "raw", "preview_rows"):
        assert banned not in data
    # A centroid is a dict of feature -> mean, not a row of values.
    for cluster in data["clusters"]:
        assert set(cluster["centroid"]) == {"spend", "visits"}
    # The sanitizer accepts it (the release-blocking safety guarantee).
    env.sanitize(env.ok(data))


# --- explicit k --------------------------------------------------------------


@requires_sklearn
def test_explicit_k_is_honored(clusterable_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, "-k", "2", capsys=capsys)
    data = payload["data"]
    assert data["k"] == 2
    assert data["k_selection"] == "explicit"
    assert len(data["clusters"]) == 2
    assert data["k_sweep"] == []


# --- feature selection -------------------------------------------------------


@requires_sklearn
def test_auto_selection_excludes_pii_and_keys_with_notes(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys)
    notes = " ".join(payload["data"]["notes"])
    assert "PII" in notes and "lat" in notes and "lng" in notes
    assert "unique-key" in notes and "id" in notes
    # The string name column never even reaches numeric consideration.
    assert "full_name" not in payload["data"]["features"]


@requires_sklearn
def test_explicit_features_may_opt_in_pii_and_report_only_its_mean(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(
        clusterable_duckdb, repo, "--features", "spend,visits,lat,lng", capsys=capsys
    )
    data = payload["data"]
    assert data["features"] == ["spend", "visits", "lat", "lng"]
    notes = " ".join(data["notes"])
    assert "PII-flagged feature" in notes and "mean" in notes
    # lat/lng appear only as a centroid mean, never as a raw value.
    for cluster in data["clusters"]:
        assert "lat" in cluster["centroid"] and "lng" in cluster["centroid"]
    env.sanitize(env.ok(data))


@requires_sklearn
def test_non_numeric_feature_is_refused(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(
        clusterable_duckdb,
        repo,
        "--features",
        "full_name",
        capsys=capsys,
        expect_error=True,
    )
    assert "not numeric" in payload["errors"][0]


@requires_sklearn
def test_unknown_feature_is_refused(clusterable_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(
        clusterable_duckdb, repo, "--features", "nope", capsys=capsys, expect_error=True
    )
    assert "not among the profiled columns" in payload["errors"][0]


def _numeric(name: str, **kw) -> ColumnProfile:
    return ColumnProfile(name=name, data_type="INTEGER", **kw)


def _fact(*columns: ColumnProfile) -> Dataset:
    return Dataset(identifier="db.main.lap_times", columns=list(columns))


def _joins_out(*columns: str) -> list[Relationship]:
    return [
        Relationship(
            from_dataset="db.main.lap_times",
            from_columns=[col],
            to_dataset="db.main.dim",
            to_columns=[col],
        )
        for col in columns
    ]


def test_foreign_keys_are_excluded_even_when_not_named_like_one():
    """The join is the evidence, not the name: a fact table is mostly foreign
    keys plus a few measures, and clustering on the keys just partitions
    surrogate ranges. `region` joins out, so it is a key however it is spelled."""

    dataset = _fact(
        _numeric("region"), _numeric("lap"), _numeric("position"), _numeric("ms")
    )
    features, notes = commands._select_cluster_features(
        dataset, None, 10, _joins_out("region")
    )

    assert features == ["lap", "position", "ms"]
    joined = " ".join(notes)
    assert "foreign-key" in joined and "region" in joined


def test_foreign_key_may_be_opted_back_in_by_name():
    dataset = _fact(_numeric("region"), _numeric("lap"), _numeric("ms"))
    features, _ = commands._select_cluster_features(
        dataset, ["region", "lap"], 10, _joins_out("region")
    )
    assert features == ["region", "lap"]


def test_key_shaped_names_are_excluded_when_no_relationships_are_cached():
    """`explore cluster` is gated on a cache that `explore profile <object>`
    alone can write, and that path infers no joins. The name fallback covers it,
    and says so, rather than silently clustering on the keys."""

    dataset = _fact(
        _numeric("driverId"), _numeric("product_id"), _numeric("lap"), _numeric("ms")
    )
    features, notes = commands._select_cluster_features(dataset, None, 10, [])

    assert features == ["lap", "ms"]
    joined = " ".join(notes)
    assert "named like a key" in joined
    assert "explore relationships" in joined, "the fallback names its own upgrade"


@pytest.mark.parametrize(
    "name",
    [
        "id",
        "ID",
        "raceId",
        "product_id",
        "HOST_ID",
        "entity_uuid",  # an integer entity key, seen in the wild
        "customer_key",  # a Kimball surrogate key
        "orderGuid",
    ],
)
def test_key_spellings_are_all_recognized(name: str):
    dataset = _fact(_numeric(name), _numeric("lap"), _numeric("ms"))
    features, _ = commands._select_cluster_features(dataset, None, 10, [])
    assert features == ["lap", "ms"], f"{name} should read as a key"


@pytest.mark.parametrize(
    "name",
    ["grid", "paid", "valid", "void", "bid_amount", "monkey", "turkey", "keyword"],
)
def test_measures_that_merely_contain_a_key_word_are_kept(name: str):
    """The boundary is the whole point of the name rule: `grid` (starting grid
    position) and `monkey` are real measures, and a bare endswith('id') or
    endswith('key') would eat them."""

    dataset = _fact(_numeric(name), _numeric("lap"), _numeric("ms"))
    features, _ = commands._select_cluster_features(dataset, None, 10, [])
    assert name in features


def test_too_few_features_says_which_exclusion_caused_it():
    """A bare count is unactionable; the error carries the exclusions so the
    caller can see that --features is the way back in."""

    dataset = _fact(_numeric("driverId"), _numeric("lap"))
    with pytest.raises(ValueError) as excinfo:
        commands._select_cluster_features(dataset, None, 10, _joins_out("driverId"))

    message = str(excinfo.value)
    assert "needs at least 2" in message
    assert "foreign-key" in message and "driverId" in message


# --- the free path stays confirmation-free -----------------------------------


@requires_sklearn
def test_duckdb_cluster_stays_confirmation_free(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys)
    assert payload["cost"]["paradigm"] == "free_local"
    assert "spend" not in payload["data"]


# --- the dialect-aware sample-SQL builder (pure; no warehouse, no sklearn) ----


def test_sample_sql_duckdb_uses_trailing_using_sample():
    sql, method = cluster_mod.build_sample_sql(
        "db.main.customers",
        ["spend", "visits"],
        dialect="duckdb",
        sample_rows=100,
        row_count=1000,
    )
    assert sql.rstrip().endswith("USING SAMPLE 100 ROWS")
    assert '"spend"' in sql and '"visits"' in sql
    assert '"spend" IS NOT NULL' in sql and '"visits" IS NOT NULL' in sql
    assert '"db"."main"."customers"' in sql
    assert "USING SAMPLE" in method
    # It must survive the SELECT-only guard the adapter re-asserts.
    assert_select_only(sql, dialect="duckdb")


@pytest.mark.parametrize(
    "dialect,marker",
    [
        ("snowflake", "SAMPLE (100 ROWS)"),
        ("databricks", "TABLESAMPLE (100 ROWS)"),
        ("bigquery", "TABLESAMPLE SYSTEM (10.0 PERCENT)"),
        ("postgres", "TABLESAMPLE SYSTEM (10.0)"),
        ("redshift", "ORDER BY RANDOM() LIMIT 100"),
    ],
)
def test_sample_sql_per_dialect_sampling(dialect: str, marker: str):
    sql, _method = cluster_mod.build_sample_sql(
        "db.sch.customers",
        ["spend", "visits"],
        dialect=dialect,
        sample_rows=100,
        row_count=1000,
    )
    assert marker in sql, sql
    assert "IS NOT NULL" in sql
    assert_select_only(sql, dialect=dialect)


def test_sample_sql_skips_percent_sampling_when_row_count_unknown():
    sql, method = cluster_mod.build_sample_sql(
        "db.sch.customers",
        ["spend", "visits"],
        dialect="bigquery",
        sample_rows=100,
        row_count=None,
    )
    assert "TABLESAMPLE" not in sql
    assert "no sample clause" in method
    assert_select_only(sql, dialect="bigquery")


# --- a seeded sample draws the same rows twice -------------------------------


def test_duckdb_seeded_sample_emits_a_repeatable_reservoir():
    """DuckDB rejects REPEATABLE on the bare `USING SAMPLE n ROWS`, so the seeded
    form has to spell out reservoir(...). Pinned because the two spellings look
    interchangeable and only one parses."""

    sql, method = cluster_mod.build_sample_sql(
        "db.main.customers",
        ["spend", "visits"],
        dialect="duckdb",
        sample_rows=100,
        row_count=1000,
        seed=7,
    )
    assert sql.rstrip().endswith("USING SAMPLE reservoir(100 ROWS) REPEATABLE (7)")
    assert "repeatable seed 7" in method
    assert_select_only(sql, dialect="duckdb")
    assert cluster_mod.sample_is_repeatable("duckdb", 7) is True


def test_seed_is_omitted_where_the_dialect_cannot_honor_it():
    """No seed clause is invented for an engine that has none, and the engine
    reports the sample as not repeatable rather than implying otherwise."""

    for dialect in ("snowflake", "bigquery", "postgres", "redshift", "databricks"):
        sql, _ = cluster_mod.build_sample_sql(
            "db.sch.customers",
            ["spend", "visits"],
            dialect=dialect,
            sample_rows=100,
            row_count=1000,
            seed=7,
        )
        assert "REPEATABLE" not in sql.upper(), dialect
        assert cluster_mod.sample_is_repeatable(dialect, 7) is False, dialect
        assert_select_only(sql, dialect=dialect)


def test_a_null_seed_opts_out_of_repeatability():
    sql, _ = cluster_mod.build_sample_sql(
        "db.main.customers",
        ["spend", "visits"],
        dialect="duckdb",
        sample_rows=100,
        row_count=1000,
        seed=None,
    )
    assert "REPEATABLE" not in sql.upper()
    assert cluster_mod.sample_is_repeatable("duckdb", None) is False


@requires_sklearn
def test_two_identical_runs_return_identical_clusters(
    sampled_duckdb: Path, tmp_path: Path, capsys
):
    """The regression this exists for: with the sample unseeded, the same command
    on the same table returned a different k run to run, because a re-drawn
    sample is a different dataset. Uses a table well over the sample cap, so a
    sample is genuinely drawn rather than the whole table read."""

    repo = _mapped_repo(sampled_duckdb, tmp_path, capsys)
    first = _cluster_table(sampled_duckdb, repo, "wide", capsys=capsys)["data"]
    second = _cluster_table(sampled_duckdb, repo, "wide", capsys=capsys)["data"]

    assert first["sample_repeatable"] is True
    assert first["k"] == second["k"]
    assert first["silhouette"] == second["silhouette"]
    assert first["clusters"] == second["clusters"]


@requires_sklearn
def test_an_unrepeatable_sample_says_so(sampled_duckdb: Path, tmp_path: Path, capsys):
    """With the seed off, the result is still valid but no longer comparable
    across runs, and the envelope has to admit that rather than let a reader
    diff two runs and think the data moved."""

    repo = _mapped_repo(sampled_duckdb, tmp_path, capsys)
    (repo / ".dex" / "config.yml").write_text("cluster:\n  sample_seed: null\n")
    payload = _cluster_table(sampled_duckdb, repo, "wide", capsys=capsys)

    assert payload["data"]["sample_repeatable"] is False
    assert "not reproducible" in " ".join(payload["data"]["notes"])


# --- a tiny cluster is an outlier pocket, not a segment -----------------------


@requires_sklearn
def test_a_degenerate_cluster_is_called_out(
    outlier_duckdb: Path, tmp_path: Path, capsys
):
    """A handful of extreme rows split off as their own cluster and drive the
    silhouette up, which reads as a confident segmentation when it is really
    outlier detection. The score alone cannot tell those apart, so the note
    must."""

    repo = _mapped_repo(outlier_duckdb, tmp_path, capsys)
    data = _cluster_table(outlier_duckdb, repo, "spikes", capsys=capsys)["data"]

    tiny = [c for c in data["clusters"] if c["fraction"] < 0.01]
    assert tiny, "fixture must produce a sub-1% cluster"
    assert data["silhouette"] > 0.7, "and it must look confident"
    note = " ".join(data["notes"])
    assert "outlier pocket rather than a segment" in note
    assert "inflates" in note


@requires_sklearn
def test_even_clusters_are_not_called_degenerate(
    clusterable_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(clusterable_duckdb, tmp_path, capsys)
    payload = _cluster(clusterable_duckdb, repo, capsys=capsys)
    assert "outlier pocket" not in " ".join(payload["data"]["notes"])


# --- the pure clustering engine ---------------------------------------------


@requires_sklearn
def test_cluster_features_drops_null_rows_and_counts_them():
    cells = [[1.0, 1.0], [1.1, 0.9], [None, 5.0], [50.0, 50.0], [51.0, 49.0]]
    result = cluster_mod.cluster_features(
        ["a", "b"],
        cells,
        k=2,
        k_min=2,
        k_max=8,
        silhouette_sample=5000,
        random_state=0,
    )
    assert result.dropped_null_rows == 1
    assert result.n_samples == 4
    assert result.k == 2


@requires_sklearn
def test_cluster_features_refuses_too_few_rows():
    with pytest.raises(cluster_mod.ClusterError):
        cluster_mod.cluster_features(
            ["a", "b"],
            [[1.0, 1.0]],
            k=2,
            k_min=2,
            k_max=8,
            silhouette_sample=5000,
            random_state=0,
        )


@requires_sklearn
def test_cluster_features_refuses_k_over_distinct_points():
    cells = [[1.0, 1.0], [1.0, 1.0], [2.0, 2.0], [2.0, 2.0]]
    with pytest.raises(cluster_mod.ClusterError):
        cluster_mod.cluster_features(
            ["a", "b"],
            cells,
            k=5,
            k_min=2,
            k_max=8,
            silhouette_sample=5000,
            random_state=0,
        )
