"""explore profile --check-cumulative: cumulative and snapshot measure
detection (issue #219).

A running total or a point-in-time snapshot profiles identically to a
per-row increment (same type, null fraction, uniqueness, min/max), and
summing it across rows is a common and damaging misreading. The signal is
structural: within an entity ordered by a temporal column, such a measure
almost never decreases. These tests cover the pure eligibility functions,
the probe SQL and its transpile, and the end-to-end detection pipeline
against a real DuckDB table.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, PIICategory, PIIFlag
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.cumulative import (
    CumulativeCandidate,
    cumulative_measure_notes,
    find_candidate,
    measure_fractions,
    probe_sql,
    probe_statements,
)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def _col(name: str, data_type: str, **kwargs) -> ColumnProfile:
    return ColumnProfile(name=name, data_type=data_type, **kwargs)


# --- eligibility: entity / temporal / measure candidates ---------------------


def test_find_candidate_pairs_the_id_shaped_repeating_column_with_the_date():
    dataset = Dataset(
        identifier="db.main.balances",
        columns=[
            _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("balance", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
    )
    candidate, notes = find_candidate(dataset)
    assert candidate == CumulativeCandidate("account_id", "snapshot_date", ["balance"])
    assert notes == []


def test_find_candidate_excludes_an_auto_increment_id_from_measures():
    """A proven single-column key is never a measure, whatever it is named
    (the acceptance criterion): only the genuine running-total column is
    offered as a candidate measure."""

    dataset = Dataset(
        identifier="db.main.events",
        columns=[
            _col("event_id", "INTEGER", is_unique=True, null_fraction=0.0),
            _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("event_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("running_total", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
    )
    candidate, notes = find_candidate(dataset)
    assert candidate is not None
    assert candidate.entity == "account_id"
    assert candidate.measures == ["running_total"]
    assert notes == []


def test_find_candidate_does_not_exclude_a_column_for_pairing_into_a_composite_key():
    """An entity naturally pairs with its own evolving measure to look
    unique (a drifting balance rarely repeats per account), which is the
    coincidence a real running total or snapshot produces -- not evidence
    that the entity or measure fails its own eligibility. Regression for a
    bug where composite-key membership wrongly excluded both."""

    dataset = Dataset(
        identifier="db.main.balances",
        columns=[
            _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("balance", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
        composite_keys=[["account_id", "balance"]],
    )
    candidate, _notes = find_candidate(dataset)
    assert candidate is not None
    assert candidate.entity == "account_id"
    assert candidate.measures == ["balance"]


def test_find_candidate_prefers_the_pair_that_is_the_proven_composite_key():
    dataset = Dataset(
        identifier="db.main.balances",
        columns=[
            _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("region_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("as_of_ts", "TIMESTAMP", is_unique=False, null_fraction=0.0),
            _col("balance", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
        composite_keys=[["region_id", "as_of_ts"]],
    )
    candidate, notes = find_candidate(dataset)
    assert candidate is not None
    assert (candidate.entity, candidate.temporal) == ("region_id", "as_of_ts")
    assert any("tested only region_id x as_of_ts" in n for n in notes)
    assert "account_id" in notes[0] and "snapshot_date" in notes[0]


@pytest.mark.parametrize(
    ("columns", "expected_missing"),
    [
        (
            [_col("amount", "DOUBLE", is_unique=False, null_fraction=0.0)],
            "entity key or temporal column",
        ),
        (
            [
                _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
                _col("amount", "DOUBLE", is_unique=False, null_fraction=0.0),
            ],
            "temporal column",
        ),
        (
            [
                _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
                _col("amount", "DOUBLE", is_unique=False, null_fraction=0.0),
            ],
            "entity key",
        ),
    ],
)
def test_find_candidate_states_grammatically_correct_skip_when_a_shape_is_missing(
    columns, expected_missing
):
    dataset = Dataset(identifier="db.main.plain", columns=columns)
    candidate, notes = find_candidate(dataset)
    assert candidate is None
    assert len(notes) == 1
    assert f"no {expected_missing} found" in notes[0]
    # No article ever precedes the bare noun ("an entity key", "a temporal
    # column"): the missing-shape phrase must read as a plain noun list.
    assert "no an " not in notes[0]
    assert "no a " not in notes[0]


def test_find_candidate_skips_with_a_note_when_no_measure_column_is_eligible():
    dataset = Dataset(
        identifier="db.main.pairs",
        columns=[
            _col("account_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
        ],
    )
    candidate, notes = find_candidate(dataset)
    assert candidate is None
    assert any("no eligible numeric measure column" in n for n in notes)


def test_find_candidate_excludes_pii_columns_from_every_role():
    dataset = Dataset(
        identifier="db.main.pii",
        columns=[
            _col(
                "account_id",
                "INTEGER",
                is_unique=False,
                null_fraction=0.0,
                pii=PIIFlag(category=PIICategory.NAME, confidence=0.9),
            ),
            _col("snapshot_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("balance", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
    )
    candidate, notes = find_candidate(dataset)
    assert candidate is None
    assert any("no entity key found" in n for n in notes)


def test_find_candidate_excludes_an_ordinary_foreign_key_from_measures_by_shape():
    """An id-shaped column is never treated as a measure even when numeric
    and non-unique -- an ordinary foreign key, not a running total."""

    dataset = Dataset(
        identifier="db.main.orders",
        columns=[
            _col("customer_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("order_date", "DATE", is_unique=False, null_fraction=0.0),
            _col("product_id", "INTEGER", is_unique=False, null_fraction=0.0),
            _col("running_total", "DOUBLE", is_unique=False, null_fraction=0.0),
        ],
    )
    candidate, _notes = find_candidate(dataset)
    assert candidate is not None
    assert candidate.measures == ["running_total"]


# --- probe SQL: authored once, transpiled per dialect -------------------------


def test_probe_sql_never_projects_a_measure_value_only_lag_and_compare():
    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance", "mrr"])
    sql = probe_sql("db.main.balances", candidate)
    assert "LAG(" in sql
    assert "PARTITION BY" in sql and "ORDER BY" in sql
    # Only COUNT/CASE WHEN comparisons leave the WITH clause; no bare SELECT
    # of a measure column outside the lag computation.
    assert sql.count("COUNT(") == 4  # obs + dec, per measure
    assert '"balance"' in sql and '"mrr"' in sql


def test_probe_statements_transpiles_to_postgres_and_stays_select_only():
    import sqlglot

    from exmergo_dex_core.guards.sql_guard import assert_select_only

    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance"])
    dataset = Dataset(identifier="dexdb.app.balances", columns=[])
    statements = probe_statements([(dataset, candidate)], "postgres")
    assert len(statements) == 1
    sql = statements[0]
    assert_select_only(sql, dialect="postgres")
    parsed = sqlglot.parse_one(sql, read="postgres")
    assert parsed is not None
    assert "balances" in sql


def test_probe_statements_is_identity_on_duckdb():
    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance"])
    dataset = Dataset(identifier="db.main.balances", columns=[])
    [sql] = probe_statements([(dataset, candidate)], "duckdb")
    assert sql == probe_sql(dataset.identifier, candidate)


# --- measure_fractions / cumulative_measure_notes: pure aggregation ----------


class _StubAdapter:
    """Just the surface `measure_fractions` touches: one canned aggregate row."""

    dialect = "duckdb"

    def __init__(self, columns: list[str], row: list):
        self._columns = columns
        self._row = row

    def run_query(self, sql: str, *, max_rows: int, timeout_seconds: float):
        from exmergo_dex_core.adapters.base import QueryResult

        return QueryResult(
            columns=self._columns,
            types=["BIGINT"] * len(self._columns),
            cells=[self._row],
            truncated=False,
        )


def test_measure_fractions_omits_a_measure_with_zero_observations():
    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance", "delta"])
    adapter = _StubAdapter(
        columns=["obs_0", "dec_0", "obs_1", "dec_1"],
        row=[100, 3, 0, 0],
    )
    fractions = measure_fractions(adapter, "db.main.balances", candidate)
    assert set(fractions) == {"balance"}
    assert fractions["balance"] == (0.03, 100)


@pytest.mark.parametrize(
    ("fraction", "observations", "expect_note"),
    [
        (0.0, 780, True),  # never decreases, plenty of evidence
        (0.03, 100, True),  # a real snapshot can fall a little
        (0.05, 100, True),  # exactly at the ceiling still qualifies
        (0.051, 100, False),  # just over the ceiling: too many decreases
        (0.25, 1000, False),  # a genuine per-row increment
        (0.0, 19, False),  # not enough observations, even at zero decreases
    ],
)
def test_cumulative_measure_notes_decisions(fraction, observations, expect_note):
    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance"])
    notes = cumulative_measure_notes(candidate, {"balance": (fraction, observations)})
    assert bool(notes) is expect_note
    if expect_note:
        assert "balance" in notes[0]
        assert "cumulative" in notes[0] or "snapshot" in notes[0]
        # The rate is reported as a percentage; the raw fraction never appears.
        assert f"{fraction:.1%}" in notes[0]


def test_cumulative_measure_notes_silent_for_a_measure_with_no_evidence():
    candidate = CumulativeCandidate("account_id", "snapshot_date", ["balance"])
    assert cumulative_measure_notes(candidate, {}) == []


# --- end-to-end: a real DuckDB table, genuine cumulative vs. incrementing ----


def _build_account_activity(path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE account_activity ("
        "event_id INTEGER, account_id INTEGER, event_date DATE, "
        "balance DOUBLE, daily_txn DOUBLE)"
    )
    rows = []
    event_id = 1
    for account in range(1, 6):
        balance = 1000.0
        for day in range(160):  # 5 accounts * 160 days = 800 rows, 795 observations
            # A running total: goes up almost every day, one small dip.
            delta = -50.0 if day == 80 else 10.0
            balance += delta
            # A genuine per-row increment: naturally goes up and down.
            txn = 20.0 if day % 2 == 0 else -15.0
            event_date = date(2024, 1, 1) + timedelta(days=day)
            rows.append((event_id, account, event_date, balance, txn))
            event_id += 1
    conn.executemany("INSERT INTO account_activity VALUES (?, ?, ?, ?, ?)", rows)
    conn.close()


def test_end_to_end_flags_the_genuinely_cumulative_column_and_spares_the_increment(
    tmp_path: Path,
):
    path = tmp_path / "activity.duckdb"
    _build_account_activity(path)

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.explore.profile import profile as run_profile

    adapter = DuckDBAdapter(path)
    try:
        identifiers = [m.identifier for m in adapter.list_objects()]
        [dataset] = run_profile(adapter, identifiers)
        candidate, notes = find_candidate(dataset)
        assert candidate is not None
        assert notes == []
        assert set(candidate.measures) == {"balance", "daily_txn"}
        assert "event_id" not in candidate.measures

        fractions = measure_fractions(adapter, dataset.identifier, candidate)
        measure_notes = cumulative_measure_notes(candidate, fractions)
    finally:
        adapter.close()

    joined = " ".join(measure_notes)
    assert "balance" in joined and "looks cumulative" in joined
    assert "daily_txn" not in joined


def test_check_cumulative_flag_annotates_the_profile_and_persists_the_note(
    tmp_path: Path, capsys
):
    path = tmp_path / "activity.duckdb"
    _build_account_activity(path)

    data = _run(
        [
            "explore",
            "profile",
            "account_activity",
            "--check-cumulative",
            "--path",
            str(path),
        ],
        capsys,
    )["data"]
    [dataset] = data["datasets"]
    assert any("looks cumulative" in n for n in dataset["data_quality"])

    # Re-running plain `explore profile` (served from cache) still carries the
    # note: the check-cumulative phase persisted it, not just returned it.
    data = _run(
        ["explore", "profile", "account_activity", "--path", str(path)], capsys
    )["data"]
    [dataset] = data["datasets"]
    assert any("looks cumulative" in n for n in dataset["data_quality"])


def test_check_cumulative_is_silent_without_the_flag(tmp_path: Path, capsys):
    path = tmp_path / "activity.duckdb"
    _build_account_activity(path)

    data = _run(
        ["explore", "profile", "account_activity", "--path", str(path)], capsys
    )["data"]
    [dataset] = data["datasets"]
    assert not any("cumulative" in n for n in dataset["data_quality"])


def test_check_cumulative_persists_a_note_on_an_already_fresh_cached_dataset(
    tmp_path: Path, capsys
):
    """Regression: a dataset served from the freshness cache is a deep copy
    (`_split_fresh_stale.model_copy`), not the object the cache write carries
    forward. Appending a note only to that copy would show it in this run's
    payload but lose it the moment the cache saves -- exactly the case a
    plain profile first, then `--check-cumulative` second, hits."""

    path = tmp_path / "activity.duckdb"
    _build_account_activity(path)

    # Plain profile first: caches the object with no check-cumulative note.
    data = _run(
        ["explore", "profile", "account_activity", "--path", str(path)], capsys
    )["data"]
    assert data["cache_hit_count"] == 0
    assert data["profiled_count"] == 1

    # Second call is served from the freshness cache (profiled_count == 0),
    # which is exactly the path that lost the note.
    data = _run(
        [
            "explore",
            "profile",
            "account_activity",
            "--check-cumulative",
            "--path",
            str(path),
        ],
        capsys,
    )["data"]
    assert data["cache_hit_count"] == 1
    assert data["profiled_count"] == 0
    [dataset] = data["datasets"]
    assert any("looks cumulative" in n for n in dataset["data_quality"])

    # And it must have actually reached the persisted cache, not just this
    # run's in-memory payload.
    data = _run(
        ["explore", "profile", "account_activity", "--path", str(path)], capsys
    )["data"]
    [dataset] = data["datasets"]
    assert any("looks cumulative" in n for n in dataset["data_quality"])


def test_check_cumulative_run_twice_does_not_duplicate_the_note(tmp_path: Path, capsys):
    """Regression: running `--check-cumulative` again on a dataset a prior
    run already flagged must not pile up a second copy of the same
    sentence. The check is idempotent whether the dataset was freshly
    profiled this run or served from the freshness cache."""

    path = tmp_path / "activity.duckdb"
    _build_account_activity(path)

    argv = [
        "explore",
        "profile",
        "account_activity",
        "--check-cumulative",
        "--path",
        str(path),
    ]
    _run(argv, capsys)
    data = _run(argv, capsys)["data"]
    [dataset] = data["datasets"]
    cumulative_notes = [n for n in dataset["data_quality"] if "looks cumulative" in n]
    assert len(cumulative_notes) == 1

    # And the duplicate-free state is what actually persisted.
    data = _run(
        ["explore", "profile", "account_activity", "--path", str(path)], capsys
    )["data"]
    [dataset] = data["datasets"]
    cumulative_notes = [n for n in dataset["data_quality"] if "looks cumulative" in n]
    assert len(cumulative_notes) == 1
