"""The Elementary incident reader (#1471): what it reads, what it refuses, and
the observations it hands the fold.

Each warehouse here is a DuckDB file holding the package's two tables, built
column for column from the recorded 0.26.1 layout, with rows written as the
package writes them. The tables the package itself builds are read back in
``tests/transform/test_elementary_fixture.py``, which needs the package.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.cache import PIIFlag
from exmergo_dex_core.guards.query_firewall import QueryRefusedError
from exmergo_dex_core.maintain import elementary_incidents as reader
from exmergo_dex_core.maintain.elementary_incidents import (
    ElementaryReadError,
    ReadBounds,
    plan_read,
    read_elementary,
)
from exmergo_dex_core.maintain.elementary_layouts import (
    ElementaryLayoutError,
    layout_for,
)
from exmergo_dex_core.maintain.incidents import fold_incidents
from exmergo_dex_core.maintain.observations import Signal, Subject
from exmergo_dex_core.maintain.timeline import Heartbeat, build_timeline
from exmergo_dex_core.values_free import ValuesFreeError

duckdb = pytest.importorskip("duckdb")

RELEASE = "0.26.1"
NAMESPACE = "dev.main_elementary"
START = datetime(2026, 11, 1, 0, 0, 0)

#: What dbt-duckdb builds each of the package's type names as.
_DUCKDB_TYPES = {
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "float": "DOUBLE",
    "long_string": "VARCHAR",
    "string": "VARCHAR",
}

TEST = "test.shop.not_null_orders_id.abc123"
ANOMALY = "test.shop.elementary_column_anomalies_orders.def456"
_ANOMALY = "anomaly_detection"


@dataclass(frozen=True)
class Run:
    """One invocation, as ``dbt_invocations`` holds it."""

    id: str
    started: datetime
    version: str = RELEASE
    commit: str | None = "a" * 40


@dataclass(frozen=True)
class Result:
    """One ``elementary_test_results`` row."""

    id: str
    run: str | None
    test: str = TEST
    status: str = "pass"
    test_type: str = "dbt_test"
    column: str | None = None
    metric: str | None = None
    severity: str | None = "ERROR"


def _hourly(count: int, *, skip: Iterable[int] = ()) -> list[Run]:
    skipped = set(skip)
    return [
        Run(f"inv-{n}", START + timedelta(hours=n))
        for n in range(1, count + 1)
        if n not in skipped
    ]


def _create(con, table: str) -> None:
    columns = ", ".join(
        f'"{name}" {_DUCKDB_TYPES[kind]}'
        for name, kind in layout_for(RELEASE).tables[table].columns
    )
    con.execute(f"CREATE TABLE main_elementary.{table} ({columns})")


def _warehouse(path: Path, runs: Sequence[Run], results: Sequence[Result]) -> Path:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA main_elementary")
    _create(con, "dbt_invocations")
    _create(con, "elementary_test_results")
    for run in runs:
        con.execute(
            "INSERT INTO main_elementary.dbt_invocations "
            "(invocation_id, run_started_at, generated_at, created_at, command, "
            "elementary_version, git_sha, target_name) "
            "VALUES (?, ?, ?, ?, 'build', ?, ?, 'dev')",
            [
                run.id,
                run.started.strftime("%Y-%m-%d %H:%M:%S"),
                run.started.strftime("%Y-%m-%d %H:%M:%S"),
                run.started + timedelta(minutes=5),
                run.version,
                run.commit,
            ],
        )
    starts = {run.id: run.started for run in runs}
    for result in results:
        at = starts.get(result.run, START) + timedelta(minutes=4)
        con.execute(
            "INSERT INTO main_elementary.elementary_test_results "
            "(id, invocation_id, test_unique_id, test_execution_id, column_name, "
            "test_type, test_sub_type, status, severity, detected_at, created_at, "
            "failures, result_rows, test_results_description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                result.id,
                result.run,
                result.test,
                f"{result.test}.{result.run}",
                result.column,
                result.test_type,
                result.metric,
                result.status,
                result.severity,
                at,
                at + timedelta(minutes=1),
                0 if result.status == "pass" else 1,
                '[{"email": "a@example.com"}]',
                "a value the test failed on",
            ],
        )
    con.close()
    return path


def _engine(path: Path, **kwargs) -> DexEngine:
    return DexEngine(connector="duckdb", path=str(path), **kwargs)


def _read(engine: DexEngine, *, since: datetime | None = None, **kwargs):
    plan = plan_read(engine, NAMESPACE, RELEASE, since=since)
    return read_elementary(engine, plan, **kwargs)


def _passing(runs: Sequence[Run], test: str = TEST) -> list[Result]:
    return [Result(f"r-{run.id}", run.id, test) for run in runs]


def _log(engine: DexEngine) -> list[dict]:
    return list(engine.store._queries)


# --- acceptance: runs the hooks did and did not record --------------------------


def _timeline(read):
    return build_timeline(
        reader.SOURCE,
        Heartbeat(basis="dbt_invocations", runs=read.history.runs),
        read.observations,
        retention_bound=read.history.retention_bound,
    )


def test_a_run_with_the_hooks_off_reads_unobserved(tmp_path: Path):
    """Run 3 ran with the package's hooks off, so it wrote neither table: the
    timeline reads the gap it leaves as a missed run."""

    runs = _hourly(6, skip=[3])
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs))))
    assert [ref.run for ref in read.history.runs] == [run.id for run in runs]
    missed = [e for e in _timeline(read).entries if e.kind == "missed"]
    assert [(e.after_run, e.before_run, e.reason) for e in missed] == [
        ("inv-2", "inv-4", "missed_run")
    ]


def test_the_same_source_with_the_hooks_on_observes_the_run(tmp_path: Path):
    runs = _hourly(6)
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs))))
    entries = _timeline(read).entries
    assert not [e for e in entries if e.kind == "missed"]
    [third] = [o for o in read.observations if o.run == "inv-3"]
    assert (third.outcome, third.sequence) == ("good", 3)


def test_a_run_with_dbt_s_own_materialization_is_observed(tmp_path: Path):
    """``handle_tests_results`` uploads a row for every test dbt ran even where
    the package's materialization cached nothing: no samples, no failed-row
    count, and still an outcome."""

    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="fail", severity="ERROR")]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    [only] = read.observations
    assert (only.outcome, only.run, only.severity) == ("bad", "inv-1", "ERROR")


def test_a_result_with_no_invocation_row_is_not_observed(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1"), Result("r-2", "inv-gone"), Result("r-3", None)]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    assert [o.run for o in read.observations] == ["inv-1"]
    assert read.measure.results_without_invocation == 2
    assert read.result_rows_read == 1


# --- acceptance: one observation per result id ------------------------------------


def test_two_rows_with_one_id_give_one_observation(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="fail")] * 2
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    assert len(read.observations) == 1
    assert (read.duplicate_ids, read.merged_rows) == (1, 0)


def test_two_anomaly_rows_for_two_columns_give_two_observations(tmp_path: Path):
    runs = _hourly(1)
    results = [
        Result("r-1", "inv-1", ANOMALY, "fail", _ANOMALY, "amount", "null_count"),
        Result("r-2", "inv-1", ANOMALY, "pass", _ANOMALY, "status", "null_count"),
    ]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    assert sorted((o.signal.column, o.outcome) for o in read.observations) == [
        ("amount", "bad"),
        ("status", "good"),
    ]
    assert {o.signal.metric for o in read.observations} == {"null_count"}


def test_a_column_level_generic_test_has_no_signal(tmp_path: Path):
    """dbt reports one result per test, so a column on a generic test's row
    would put one failure on two keys: this source's and dbt's."""

    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="fail", column="order_id")]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    [only] = read.observations
    assert only.signal == Signal()


def test_rows_one_subject_holds_in_one_run_are_one_outcome_a_failure_first(
    tmp_path: Path,
):
    """Two tests carrying one claim, or one anomaly metric on two dimension
    rows, give the fold one outcome per run: bad over good over unobserved."""

    runs = _hourly(1)
    other = "test.shop.renamed_check.ghi789"
    results = [
        Result("r-1", "inv-1", TEST, "pass"),
        Result("r-2", "inv-1", other, "fail"),
        Result("r-3", "inv-1", TEST, "skipped"),
    ]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, results))
    read = _read(engine, claims={TEST: "claim-1", other: "claim-1"})
    [only] = read.observations
    assert (only.subject, only.outcome, only.test) == (
        Subject(kind="claim", key="claim-1"),
        "bad",
        other,
    )
    assert read.merged_rows == 2


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("pass", "good"),
        ("fail", "bad"),
        ("error", "bad"),
        ("warn", "bad"),
        ("skipped", "unobserved"),
        ("PASS", "good"),
    ],
)
def test_statuses_map_as_dbt_s_do(tmp_path: Path, status: str, outcome: str):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status=status)]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    assert [o.outcome for o in read.observations] == [outcome]


# --- order and the history --------------------------------------------------------


def test_each_invocation_has_one_sequence_in_start_order_not_insert_order(
    tmp_path: Path,
):
    """Two runs that committed out of order: inv-late started first and was
    inserted last. The order is the starts', and each run has one sequence
    however many rows it holds."""

    early = Run("inv-late", START)
    late = Run("inv-early", START + timedelta(hours=1))
    path = _warehouse(tmp_path / "dev.duckdb", [late, early], [])
    con = duckdb.connect(str(path))
    con.execute(
        "UPDATE main_elementary.dbt_invocations SET created_at = ? "
        "WHERE invocation_id = 'inv-late'",
        [START + timedelta(hours=3)],
    )
    con.close()
    results = [
        Result("a", "inv-late", TEST),
        Result("b", "inv-late", ANOMALY, "fail", _ANOMALY, "amount", "row_count"),
        Result("c", "inv-early", TEST),
    ]
    _insert(path, results, {"inv-late": START, "inv-early": START})
    read = _read(_engine(path))
    assert [(ref.run, ref.sequence) for ref in read.history.runs] == [
        ("inv-late", 1),
        ("inv-early", 2),
    ]
    sequences = {(o.run, o.sequence) for o in read.observations}
    assert sequences == {("inv-late", 1), ("inv-early", 2)}
    assert read.history.retention_bound.run == "inv-late"


def _insert(path: Path, results: Sequence[Result], starts: dict[str, datetime]):
    con = duckdb.connect(str(path))
    for result in results:
        con.execute(
            "INSERT INTO main_elementary.elementary_test_results "
            "(id, invocation_id, test_unique_id, column_name, test_type, "
            "test_sub_type, status, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                result.id,
                result.run,
                result.test,
                result.column,
                result.test_type,
                result.metric,
                result.status,
                result.severity,
                starts.get(result.run or "", START),
            ],
        )
    con.close()


def test_each_run_carries_its_commit_and_its_start(tmp_path: Path):
    runs = [Run("inv-1", START, commit="b" * 40), Run("inv-2", START, commit=None)]
    runs[1] = Run("inv-2", START + timedelta(hours=1), commit=None)
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs))))
    assert [(r.run, r.commit) for r in read.history.runs] == [
        ("inv-1", "b" * 40),
        ("inv-2", None),
    ]
    assert read.history.runs[0].at == START.replace(tzinfo=UTC)
    assert read.commits_unread == 1


def test_the_reader_s_observations_fold_into_an_incident(tmp_path: Path):
    runs = _hourly(5)
    results = [
        Result(f"r-{n}", f"inv-{n}", status="fail" if n in (2, 3) else "pass")
        for n in range(1, 6)
    ]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    fold = fold_incidents(
        read.observations,
        sources=[read.history],
        now=START + timedelta(hours=6),
    )
    [incident] = fold.incidents
    assert incident.state == "closed"
    assert incident.first_observed != "unknown"
    assert incident.first_observed.run == "inv-2"
    assert incident.closed_by.run == "inv-5"


def test_a_window_reads_only_the_runs_inside_it(tmp_path: Path):
    runs = _hourly(4)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    read = _read(engine, since=START + timedelta(hours=3))
    assert [ref.run for ref in read.history.runs] == ["inv-3", "inv-4"]
    assert {o.run for o in read.observations} == {"inv-3", "inv-4"}
    assert read.windowed


# --- the firewall and the first read ---------------------------------------------


def test_the_first_read_profiles_the_tables_and_the_second_reuses_them(
    tmp_path: Path,
):
    runs = _hourly(2)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    first = _read(engine)
    assert first.first_read
    assert sorted(first.profiled) == [
        "dev.main_elementary.dbt_invocations",
        "dev.main_elementary.elementary_test_results",
    ]
    # DuckDB profiles for free, and the estimate says so rather than nothing.
    assert first.profile_estimate == 0.0
    second = _read(engine)
    assert (second.first_read, second.profiled, second.profile_estimate) == (
        False,
        (),
        None,
    )


def test_every_statement_reaches_the_query_ledger_and_none_reads_a_value(
    tmp_path: Path,
):
    runs = _hourly(2)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    _read(engine)
    allowed = [entry for entry in _log(engine) if entry["decision"] == "allowed"]
    assert len(allowed) == 5
    for entry in allowed:
        sql = entry["sql"].lower()
        for column in (
            "result_rows",
            "test_results_description",
            "test_results_query",
            "test_params",
        ):
            assert column not in sql, (column, sql)


def _flag(engine: DexEngine, table: str, column: str) -> None:
    cache = engine.store.load_cache()
    [dataset] = [d for d in cache.datasets if d.identifier.endswith(table)]
    [target] = [c for c in dataset.columns if c.name == column]
    target.pii = PIIFlag(category="name", confidence=0.95)
    engine.store.save_cache(cache)


def test_a_column_the_firewall_flags_is_never_read(tmp_path: Path):
    """A flag on a column the reader selects refuses the read, and the ledger
    holds no allowed statement that projects it."""

    runs = _hourly(2)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    _read(engine)
    _flag(engine, "elementary_test_results", "column_name")
    before = len(_log(engine))
    with pytest.raises(QueryRefusedError, match="column_name"):
        _read(engine)
    after = _log(engine)[before:]
    assert any(entry["decision"] == "refused" for entry in after)
    assert not [
        entry
        for entry in after
        if entry["decision"] == "allowed" and "column_name" in entry["sql"]
    ]


def test_values_free_mode_refuses_before_the_catalog_is_read(tmp_path: Path):
    runs = _hourly(1)
    engine = _engine(
        _warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)),
        result_mode="values_free",
    )
    with pytest.raises(ValuesFreeError):
        plan_read(engine, NAMESPACE, RELEASE)
    assert _log(engine) == []


# --- refusals ---------------------------------------------------------------------


def _refusal(engine: DexEngine, **kwargs) -> ElementaryReadError:
    with pytest.raises(ElementaryReadError) as caught:
        _read(engine, **kwargs)
    return caught.value


def test_a_release_with_no_layout_refuses_before_any_query(tmp_path: Path):
    runs = _hourly(1)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    with pytest.raises(ElementaryLayoutError) as caught:
        plan_read(engine, NAMESPACE, "0.23.0")
    assert caught.value.code == "elementary_release_untested"
    assert _log(engine) == []


def test_a_namespace_outside_the_inventory_refuses_as_missing_tables(tmp_path: Path):
    runs = _hourly(1)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    with pytest.raises(ElementaryLayoutError) as caught:
        plan_read(engine, "dev.prod_elementary", RELEASE)
    assert caught.value.code == "elementary_table_missing"
    assert _log(engine) == []


def test_no_invocation_is_refused_rather_than_read_as_a_clean_history(
    tmp_path: Path,
):
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", [], []))
    assert _refusal(engine).code == "elementary_no_invocations"
    # Refused on the count, so no statement read a row.
    assert _rows_read(engine) == 0


def test_a_window_holding_no_run_is_refused(tmp_path: Path):
    runs = _hourly(2)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    refusal = _refusal(engine, since=START + timedelta(days=30))
    assert refusal.code == "elementary_no_invocations"
    assert _rows_read(engine) == 0


def test_a_result_whose_run_the_invocation_read_missed_is_not_observed():
    """A run written between the invocation read and the result read: its rows
    are counted, never placed on a run the history does not hold."""

    held = reader._Invocation("inv-1", START.replace(tzinfo=UTC), None, None, RELEASE)
    built = reader._Observations([held], {})
    built.add(
        [
            ["r-1", "inv-1", TEST, None, "dbt_test", None, "pass", None],
            ["r-2", "inv-2", TEST, None, "dbt_test", None, "fail", None],
        ]
    )
    assert [o.run for o in built.observations()] == ["inv-1"]
    assert built.unplaced == 1


def test_more_invocations_than_the_bound_refuses_before_any_row_is_read(
    tmp_path: Path,
):
    runs = _hourly(3)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    refusal = _refusal(engine, bounds=ReadBounds(invocations=2))
    assert (refusal.code, refusal.counts) == (
        "elementary_read_too_large",
        {"dbt_invocations": 3},
    )
    assert _rows_read(engine) == 0


def test_more_result_rows_than_the_bound_refuses_before_any_row_is_read(
    tmp_path: Path,
):
    runs = _hourly(3)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    refusal = _refusal(engine, bounds=ReadBounds(result_rows=2))
    assert refusal.counts == {"elementary_test_results": 3}
    assert _rows_read(engine) == 0


def test_a_read_that_grew_past_its_bound_since_the_count_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Runs written between the count and the read: the read is capped at the
    bound and refuses rather than handing on the first rows."""

    runs = _hourly(3)
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    counted = reader.ReadMeasure(
        invocations=2, result_rows=2, results_without_invocation=0
    )
    monkeypatch.setattr(reader, "_measure", lambda *_args: counted)
    refusal = _refusal(engine, bounds=ReadBounds(invocations=2))
    assert refusal.code == "elementary_read_too_large"


def test_an_invocation_read_that_comes_back_empty_refuses():
    with pytest.raises(ElementaryReadError) as caught:
        reader._invocations([], RELEASE)
    assert caught.value.code == "elementary_no_invocations"


def _rows_read(engine: DexEngine) -> int:
    """How many allowed statements read rows rather than a count."""

    return sum(
        1
        for entry in _log(engine)
        if entry["decision"] == "allowed" and "count(" not in entry["sql"].lower()
    )


@pytest.mark.parametrize(
    ("invocations", "result_rows"),
    [(0, 1), (1, 0), (reader.MAX_INVOCATIONS + 1, 1), (1, reader.MAX_RESULT_ROWS + 1)],
)
def test_bounds_narrow_and_never_widen(invocations: int, result_rows: int):
    with pytest.raises(ValueError, match="must be between"):
        ReadBounds(invocations=invocations, result_rows=result_rows)


def test_a_run_written_by_another_release_refuses(tmp_path: Path):
    runs = [Run("inv-1", START, version="0.23.0"), Run("inv-2", START, version="x y")]
    runs[1] = Run("inv-2", START + timedelta(hours=1), version="not a version!")
    runs.append(Run("inv-3", START + timedelta(hours=2)))
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    refusal = _refusal(engine)
    assert refusal.code == "elementary_rows_untested_release"
    assert refusal.counts == {"0.23.0": 1, "an unreadable value": 1}
    assert "not a version!" not in str(refusal)


def test_reading_from_after_an_upgrade_passes(tmp_path: Path):
    old = Run("inv-old", START - timedelta(hours=2), version="0.23.0")
    runs = [old, *_hourly(2)]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs)))
    read = _read(engine, since=START)
    assert [ref.run for ref in read.history.runs] == ["inv-1", "inv-2"]


def test_a_status_the_reader_does_not_map_refuses(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="runtime error")]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, results))
    refusal = _refusal(engine)
    assert (refusal.code, refusal.counts) == (
        "elementary_status_unknown",
        {"runtime error": 1},
    )


def test_an_unreadable_status_is_counted_never_quoted(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="failed on 'jane@example.com'")]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, results))
    refusal = _refusal(engine)
    assert refusal.counts == {"an unreadable value": 1}
    assert "example.com" not in str(refusal)


def test_one_id_with_two_records_refuses(tmp_path: Path):
    runs = _hourly(1)
    results = [
        Result("r-1", "inv-1", status="pass"),
        Result("r-1", "inv-1", status="fail"),
    ]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, results))
    assert _refusal(engine).code == "elementary_result_id_conflict"


def test_one_invocation_with_two_starts_refuses(tmp_path: Path):
    runs = [Run("inv-1", START), Run("inv-1", START + timedelta(hours=1))]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, []))
    assert _refusal(engine).code == "elementary_invocation_repeated"


def test_one_invocation_written_twice_alike_is_one_run(tmp_path: Path):
    runs = [Run("inv-1", START), Run("inv-1", START)]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, _passing(runs[:1])))
    read = _read(engine)
    assert [ref.run for ref in read.history.runs] == ["inv-1"]


def test_an_unreadable_start_refuses(tmp_path: Path):
    path = _warehouse(tmp_path / "dev.duckdb", _hourly(1), [])
    con = duckdb.connect(str(path))
    con.execute("UPDATE main_elementary.dbt_invocations SET run_started_at = 'soon'")
    con.close()
    assert _refusal(_engine(path)).code == "elementary_invocation_unreadable"


def test_an_id_too_long_to_hold_refuses_rather_than_being_cut(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", test="test." + "x" * 600)]
    engine = _engine(_warehouse(tmp_path / "dev.duckdb", runs, results))
    assert _refusal(engine).code == "elementary_id_too_long"


def test_a_result_with_no_test_id_is_counted_not_observed(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1"), Result("r-2", "inv-1", test="")]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    assert (len(read.observations), read.unattributed) == (1, 1)


def test_a_severity_that_is_not_a_token_is_dropped_and_counted(tmp_path: Path):
    runs = _hourly(1)
    results = [Result("r-1", "inv-1", status="fail", severity="failed: 42 rows!")]
    read = _read(_engine(_warehouse(tmp_path / "dev.duckdb", runs, results)))
    [only] = read.observations
    assert (only.severity, read.severities_unread) == (None, 1)
