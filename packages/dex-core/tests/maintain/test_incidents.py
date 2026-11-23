"""The incident model and its one lifecycle rule (#1465).

Each acceptance criterion is a pair: a case built to break the rule and one
built to hold it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from exmergo_dex_core.maintain.incidents import (
    UNKNOWN,
    FoldInputError,
    Observation,
    ObservedPoint,
    RunRef,
    Signal,
    SourceHistory,
    Subject,
    fold_incidents,
)
from exmergo_dex_core.maintain.timeline import Heartbeat, build_timeline

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
TEST = "test.shop.not_null_orders_order_id.abc123"
CLAIMED = Subject.of(TEST, "4f1c2a9be0d3c711")
BY_ID = Subject.of(TEST)
DBT = "dbt_run_results"
ELEMENTARY = "elementary"


def _obs(
    sequence: int,
    outcome: str,
    *,
    source: str = DBT,
    subject: Subject = BY_ID,
    test: str = TEST,
    signal: Signal | None = None,
    severity: str | None = None,
) -> Observation:
    return Observation(
        subject=subject,
        signal=signal or Signal(),
        test=test,
        outcome=outcome,
        source=source,
        run=f"inv-{sequence}",
        sequence=sequence,
        at=T0 + timedelta(hours=sequence),
        commit=f"{sequence:07x}",
        severity=severity,
    )


def _series(outcomes: str, **kwargs) -> list[Observation]:
    """``"gbbu"`` is good, bad, bad, unobserved on runs 1, 2, 3, 4."""

    names = {"g": "good", "b": "bad", "u": "unobserved"}
    return [
        _obs(index, names[letter], **kwargs)
        for index, letter in enumerate(outcomes, start=1)
    ]


def _now(after: int) -> datetime:
    """Just after run ``after``, so nothing reads as stale."""

    return T0 + timedelta(hours=after, minutes=5)


# -- one failure, one incident -------------------------------------------------


def test_three_consecutive_failures_are_one_incident() -> None:
    fold = fold_incidents(_series("gbbb"), now=_now(4))
    assert len(fold.incidents) == 1
    incident = fold.incidents[0]
    assert incident.state == "open"
    assert [o.run for o in incident.observations] == ["inv-2", "inv-3", "inv-4"]
    assert incident.last_observed.run == "inv-4"


def test_a_single_pass_between_failures_does_not_close_it() -> None:
    fold = fold_incidents(_series("gbgb"), now=_now(4))
    assert len(fold.incidents) == 1
    assert fold.incidents[0].state == "open"


def test_n_passes_between_failures_make_two_incidents() -> None:
    fold = fold_incidents(_series("gbggb"), now=_now(5))
    assert [i.state for i in fold.incidents] == ["closed", "open"]
    closed, reopened = fold.incidents
    assert closed.closed_by == ObservedPoint(
        run="inv-4", at=T0 + timedelta(hours=4), source=DBT
    )
    assert reopened.first_observed != UNKNOWN
    assert reopened.first_observed.run == "inv-5"


def test_n_is_set_per_signal() -> None:
    column = Signal(column="amount", metric="null_count")
    series = _series("bgg", signal=column) + _series("bgg")
    fold = fold_incidents(series, close_after_by={(BY_ID, column): 3}, now=_now(3))
    by_signal = {i.signal: i.state for i in fold.incidents}
    assert by_signal == {column: "open", Signal(): "closed"}


def test_close_after_must_be_positive() -> None:
    with pytest.raises(ValueError, match="close_after"):
        fold_incidents([], close_after=0)


# -- N counts runs, not rows ---------------------------------------------------


def test_two_passing_rows_in_one_run_are_one_pass() -> None:
    # Two tests carry one claim, or an adapter did not deduplicate: run 2
    # holds two rows. One run of passes is not N = 2.
    series = [_obs(1, "bad"), _obs(2, "good"), _obs(2, "good")]
    fold = fold_incidents(series, now=_now(2))
    assert [i.state for i in fold.incidents] == ["open"]
    assert [o.outcome for o in fold.incidents[0].observations] == [
        "bad",
        "good",
        "good",
    ]


def test_every_failing_row_in_one_run_stays_on_the_incident() -> None:
    warn = _obs(1, "bad", test="t1", severity="warn")
    error = _obs(1, "bad", test="t2", severity="error")
    for series in ([warn, error], [error, warn]):
        (incident,) = fold_incidents(series, now=_now(1)).incidents
        assert incident.tests == ("t1", "t2")
        assert len(incident.observations) == 2
        assert incident.severity == "error"


def test_the_closing_runs_other_test_stays_on_the_incident() -> None:
    series = [
        _obs(1, "bad", test="test.a"),
        _obs(2, "good", test="test.a"),
        _obs(2, "good", test="test.b"),
    ]
    (incident,) = fold_incidents(series, close_after=1, now=_now(2)).incidents
    assert incident.state == "closed"
    assert incident.closed_by.run == "inv-2"
    assert incident.tests == ("test.a", "test.b")
    assert len(incident.observations) == 3


def test_a_duplicate_pass_in_the_closing_run_stays_on_the_incident() -> None:
    series = [_obs(1, "bad"), _obs(2, "good"), _obs(3, "good"), _obs(3, "good")]
    (incident,) = fold_incidents(series, now=_now(3)).incidents
    assert incident.state == "closed"
    assert incident.closed_by.run == "inv-3"
    assert [o.run for o in incident.observations] == [
        "inv-1",
        "inv-2",
        "inv-3",
        "inv-3",
    ]


def test_another_sources_pass_in_the_closing_run_is_evidence() -> None:
    series = [
        _obs(1, "bad", source=DBT),
        _obs(2, "good", source=DBT),
        _obs(2, "good", source=ELEMENTARY),
    ]
    (incident,) = fold_incidents(series, close_after=1, now=_now(2)).incidents
    assert incident.state == "closed"
    assert [(o.source, o.evidence) for o in incident.observations] == [
        (DBT, False),
        (DBT, False),
        (ELEMENTARY, True),
    ]


def test_a_run_that_failed_is_not_also_counted_as_a_pass() -> None:
    series = [_obs(1, "bad"), _obs(2, "bad"), _obs(2, "good"), _obs(3, "good")]
    fold = fold_incidents(series, now=_now(3))
    assert [i.state for i in fold.incidents] == ["open"]
    closing = fold_incidents([*series, _obs(4, "good")], now=_now(4))
    assert [i.state for i in closing.incidents] == ["closed"]
    assert closing.incidents[0].closed_by.run == "inv-4"


# -- the check stops running ---------------------------------------------------


def test_a_removed_test_ends_no_longer_observed() -> None:
    fold = fold_incidents(_series("gbb"), current_subjects=[], now=_now(3))
    incident = fold.incidents[0]
    assert incident.state == "no_longer_observed"
    assert incident.ended_reason == "removed"
    assert incident.closed_by is None


def test_a_test_still_running_keeps_its_incident_open() -> None:
    fold = fold_incidents(_series("gbb"), current_subjects=[BY_ID], now=_now(3))
    assert fold.incidents[0].state == "open"
    assert fold.incidents[0].ended_reason is None


def test_a_source_that_stops_uploading_ends_it_no_longer_observed() -> None:
    # Hourly runs; nothing for a day.
    fold = fold_incidents(_series("gbbb"), now=_now(4) + timedelta(days=1))
    incident = fold.incidents[0]
    assert incident.state == "no_longer_observed"
    assert incident.ended_reason == "stale"
    assert incident.schedule is not None
    assert incident.schedule.basis == "derived"
    assert incident.schedule.series == "subject"
    assert incident.schedule.median_gap_seconds == 3600
    assert incident.schedule.allowance_seconds == 3 * 3600


def test_a_source_on_schedule_keeps_it_open() -> None:
    fold = fold_incidents(_series("gbbb"), now=_now(4) + timedelta(hours=2))
    assert fold.incidents[0].state == "open"


def test_a_deselected_test_is_judged_on_the_sources_schedule() -> None:
    # The test has two observations, too few for its own schedule; the source
    # kept running hourly without it for ten hours.
    runs = tuple(
        RunRef(run=f"inv-{n}", sequence=n, at=T0 + timedelta(hours=n))
        for n in range(1, 13)
    )
    fold = fold_incidents(
        _series("gb"),
        sources=[SourceHistory(name=DBT, runs=runs)],
        now=_now(12),
    )
    incident = fold.incidents[0]
    assert incident.state == "no_longer_observed"
    assert incident.schedule is not None and incident.schedule.series == "source"
    assert [s.state for s in incident.coverage.segments] == [
        "observed_bad",
        "unobserved",
    ]


def _burst(seconds_apart: int) -> list[Observation]:
    """Builds run by hand a few seconds apart, failing each time."""

    return [
        _obs(n, "bad").model_copy(
            update={"at": T0 + timedelta(seconds=n * seconds_apart)}
        )
        for n in range(1, 6)
    ]


def test_a_burst_of_hand_runs_is_not_read_as_a_schedule() -> None:
    fold = fold_incidents(_burst(4), now=T0 + timedelta(minutes=10))
    incident = fold.incidents[0]
    assert incident.state == "open"
    assert incident.schedule is not None
    assert incident.schedule.median_gap_seconds == 4
    assert incident.schedule.allowance_seconds == 3600


def test_a_burst_followed_by_hours_of_silence_still_ends_stale() -> None:
    fold = fold_incidents(_burst(4), now=T0 + timedelta(hours=2))
    assert fold.incidents[0].state == "no_longer_observed"
    assert fold.incidents[0].ended_reason == "stale"


def test_the_staleness_floor_is_settable() -> None:
    fold = fold_incidents(
        _burst(4), now=T0 + timedelta(minutes=10), stale_floor_seconds=0
    )
    assert fold.incidents[0].state == "no_longer_observed"
    with pytest.raises(ValueError, match="stale_floor_seconds"):
        fold_incidents([], stale_floor_seconds=-1)


def test_a_dst_repeated_wall_clock_hour_stays_two_instants() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    times = [
        datetime(2020, 11, 1, 0, 30, tzinfo=zone),
        datetime(2020, 11, 1, 1, 30, tzinfo=zone),
        datetime(2020, 11, 1, 1, 30, fold=1, tzinfo=zone),
    ]
    series = [
        _obs(n, "bad").model_copy(update={"at": at})
        for n, at in enumerate(times, start=1)
    ]
    fold = fold_incidents(series, now=datetime(2020, 11, 2, 12, tzinfo=UTC))
    incident = fold.incidents[0]
    assert incident.schedule is not None and incident.schedule.points == 3
    assert incident.state == "no_longer_observed"


def test_too_little_history_never_ends_an_incident_for_staleness() -> None:
    fold = fold_incidents(_series("b"), now=_now(1) + timedelta(days=30))
    incident = fold.incidents[0]
    assert incident.state == "open"
    assert incident.schedule is not None and incident.schedule.basis == "unknown"
    assert fold.schedules[DBT].basis == "unknown"


def test_a_resumed_gap_is_an_unobserved_interval_not_an_ending() -> None:
    series = [*_series("gbb"), _obs(100, "bad"), _obs(101, "bad"), _obs(102, "bad")]
    fold = fold_incidents(series, now=_now(102))
    assert len(fold.incidents) == 1
    assert fold.incidents[0].state == "open"


# -- unobserved intervals ------------------------------------------------------


@pytest.mark.parametrize("hooks_off", ["absent", "unobserved"])
def test_a_run_with_hooks_off_neither_closes_nor_reopens(hooks_off: str) -> None:
    series = _series("gbub")
    if hooks_off == "absent":
        series = [o for o in series if o.sequence != 3]
    fold = fold_incidents(series, now=_now(4))
    assert len(fold.incidents) == 1
    incident = fold.incidents[0]
    assert incident.state == "open"
    assert [o.run for o in incident.observations] == ["inv-2", "inv-4"]


def test_an_unobserved_run_between_passes_does_not_break_the_streak() -> None:
    fold = fold_incidents(_series("gbgug"), now=_now(5))
    assert fold.incidents[0].state == "closed"
    assert fold.incidents[0].closed_by.run == "inv-5"


def test_coverage_names_the_unobserved_runs_of_the_window() -> None:
    runs = tuple(
        RunRef(run=f"inv-{n}", sequence=n, at=T0 + timedelta(hours=n))
        for n in range(1, 6)
    )
    series = [o for o in _series("gbubb") if o.sequence != 3]
    fold = fold_incidents(
        series, sources=[SourceHistory(name=DBT, runs=runs)], now=_now(5)
    )
    segments = fold.incidents[0].coverage.segments
    assert [(s.state, s.from_run, s.to_run, s.runs) for s in segments] == [
        ("observed_bad", "inv-2", "inv-2", 1),
        ("unobserved", "inv-3", "inv-3", 1),
        ("observed_bad", "inv-4", "inv-5", 2),
    ]


# -- sources that disagree -----------------------------------------------------


def test_another_source_passing_attaches_evidence_and_leaves_it_open() -> None:
    series = _series("gbbb", source=ELEMENTARY) + _series("ggg", source=DBT)[1:]
    fold = fold_incidents(series, now=_now(4))
    assert len(fold.incidents) == 1
    incident = fold.incidents[0]
    assert incident.state == "open"
    assert incident.opening_source == ELEMENTARY
    evidence = [o for o in incident.observations if o.evidence]
    assert [(o.source, o.outcome) for o in evidence] == [
        (DBT, "good"),
        (DBT, "good"),
    ]


def test_the_opening_source_passing_n_times_closes_it() -> None:
    series = _series("gbgg", source=ELEMENTARY) + _series("gggg", source=DBT)
    fold = fold_incidents(series, now=_now(4))
    assert [i.state for i in fold.incidents] == ["closed"]
    assert fold.incidents[0].closed_by.source == ELEMENTARY


def test_a_disagreement_in_the_same_run_opens_on_the_failure() -> None:
    series = [_obs(1, "good", source=DBT), _obs(1, "bad", source=ELEMENTARY)]
    fold = fold_incidents(series, now=_now(1))
    incident = fold.incidents[0]
    assert incident.opening_source == ELEMENTARY
    assert [o.evidence for o in incident.observations] == [False, True]


def test_a_failure_from_another_source_extends_and_resets_the_streak() -> None:
    series = [
        _obs(1, "bad", source=ELEMENTARY),
        _obs(2, "good", source=ELEMENTARY),
        _obs(3, "bad", source=DBT),
        _obs(4, "good", source=ELEMENTARY),
    ]
    fold = fold_incidents(series, now=_now(4))
    assert len(fold.incidents) == 1
    assert fold.incidents[0].state == "open"


# -- one invocation order for every source -------------------------------------


def _at_run(run: str, sequence: int, outcome: str, source: str) -> Observation:
    return _obs(sequence, outcome, source=source).model_copy(update={"run": run})


def test_one_run_numbered_twice_across_sources_is_refused() -> None:
    # Elementary holds three invocations; the dbt adapter numbered the one it
    # read as 1, though it is Elementary's third.
    series = [
        _at_run("inv-A", 1, "bad", ELEMENTARY),
        _at_run("inv-B", 2, "good", ELEMENTARY),
        _at_run("inv-C", 3, "good", ELEMENTARY),
        _at_run("inv-C", 1, "bad", DBT),
    ]
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(series, now=_now(3))
    assert refused.value.code == "run_sequence_conflict"
    assert refused.value.run == "inv-C"
    assert refused.value.sequences == (3, 1)


def test_a_run_numbered_twice_by_a_sources_runs_is_refused() -> None:
    history = SourceHistory(name=DBT, runs=(RunRef(run="inv-2", sequence=7, at=T0),))
    with pytest.raises(FoldInputError, match="inv-2") as refused:
        fold_incidents(_series("bb"), sources=[history], now=_now(2))
    assert refused.value.code == "run_sequence_conflict"


def test_a_run_numbered_twice_by_a_timeline_is_refused() -> None:
    timeline = build_timeline(
        DBT,
        Heartbeat(basis="source_runs", runs=(RunRef(run="inv-1", sequence=9),)),
    )
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(_series("bb"), timelines={DBT: timeline}, now=_now(2))
    assert refused.value.code == "run_sequence_conflict"
    assert refused.value.sequences == (1, 9)


def test_one_sequence_holding_two_runs_across_sources_is_refused() -> None:
    # dbt numbered its one invocation 1, which is Elementary's inv-A: the
    # fold would read dbt's pass as evidence on inv-A's run.
    series = [
        _at_run("inv-A", 1, "bad", ELEMENTARY),
        _at_run("inv-B", 2, "good", ELEMENTARY),
        _at_run("inv-D", 3, "good", ELEMENTARY),
        _at_run("inv-C", 1, "good", DBT),
    ]
    ranked = [SourceHistory(name=ELEMENTARY), SourceHistory(name=DBT)]
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(series, sources=ranked, now=_now(3))
    assert refused.value.code == "sequence_run_conflict"
    assert refused.value.runs == ("inv-A", "inv-C")
    assert refused.value.sequences == (1,)
    assert refused.value.sources == (ELEMENTARY, DBT)


def test_a_sequence_a_sources_runs_give_another_run_is_refused() -> None:
    history = SourceHistory(name=DBT, runs=(RunRef(run="inv-X", sequence=2, at=T0),))
    with pytest.raises(FoldInputError, match="inv-X") as refused:
        fold_incidents(_series("bb"), sources=[history], now=_now(2))
    assert refused.value.code == "sequence_run_conflict"


def test_a_sequence_a_timeline_gives_another_run_is_refused() -> None:
    timeline = build_timeline(
        DBT,
        Heartbeat(basis="source_runs", runs=(RunRef(run="inv-X", sequence=1),)),
    )
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(_series("bb"), timelines={DBT: timeline}, now=_now(2))
    assert refused.value.code == "sequence_run_conflict"
    assert refused.value.runs == ("inv-1", "inv-X")


def _opener_tie() -> list[Observation]:
    # dbt read one invocation; Elementary holds that one and three more.
    return [_obs(5, "bad", source=DBT)] + [
        _obs(n, outcome, source=ELEMENTARY)
        for n, outcome in [(5, "bad"), (6, "good"), (7, "good"), (8, "good")]
    ]


def test_a_tie_between_undeclared_sources_is_refused() -> None:
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(_opener_tie(), now=_now(8))
    assert refused.value.code == "opener_tie_undeclared"
    assert refused.value.sources == (DBT, ELEMENTARY)


def test_a_tie_with_one_undeclared_source_is_refused() -> None:
    with pytest.raises(FoldInputError) as refused:
        fold_incidents(
            _opener_tie(), sources=[SourceHistory(name=ELEMENTARY)], now=_now(8)
        )
    assert refused.value.code == "opener_tie_undeclared"
    assert refused.value.sources == (DBT, ELEMENTARY)


def test_the_source_named_first_opens_a_tie_and_can_close_it() -> None:
    ranked = [SourceHistory(name=ELEMENTARY), SourceHistory(name=DBT)]
    fold = fold_incidents(_opener_tie(), sources=ranked, now=_now(8))
    (incident,) = fold.incidents
    assert incident.opening_source == ELEMENTARY
    assert incident.state == "closed"
    assert incident.closed_by.run == "inv-7"
    assert [(o.source, o.evidence) for o in incident.observations[:2]] == [
        (ELEMENTARY, False),
        (DBT, False),
    ]


def test_a_tie_is_settled_by_the_declared_order_not_the_names() -> None:
    ranked = [SourceHistory(name=DBT), SourceHistory(name=ELEMENTARY)]
    fold = fold_incidents(_opener_tie(), sources=ranked, now=_now(8))
    assert fold.incidents[0].opening_source == DBT
    swapped = fold_incidents(_opener_tie(), sources=ranked[::-1], now=_now(8))
    assert swapped.incidents[0].opening_source == ELEMENTARY


def test_sources_failing_together_after_the_opener_need_no_precedence() -> None:
    series = [
        _obs(1, "bad", source=ELEMENTARY),
        _obs(2, "bad", source=ELEMENTARY),
        _obs(2, "bad", source=DBT),
    ]
    fold = fold_incidents(series, now=_now(2))
    assert fold.incidents[0].opening_source == ELEMENTARY


# -- first observed ------------------------------------------------------------


def test_a_first_failure_before_every_sources_retention_is_unknown() -> None:
    fold = fold_incidents(_series("bbb"), now=_now(3))
    incident = fold.incidents[0]
    assert incident.first_observed == UNKNOWN
    assert incident.retention_bound[DBT] == ObservedPoint(
        run="inv-1", at=T0 + timedelta(hours=1), source=DBT
    )


def test_a_series_holding_its_first_failure_reports_that_run() -> None:
    fold = fold_incidents(_series("gbbb"), now=_now(4))
    first = fold.incidents[0].first_observed
    assert first != UNKNOWN
    assert first.run == "inv-2"


def test_a_pass_only_another_source_holds_still_dates_the_onset() -> None:
    series = [_obs(1, "good", source=ELEMENTARY), _obs(2, "bad", source=DBT)]
    fold = fold_incidents(series, now=_now(2))
    assert fold.incidents[0].first_observed.run == "inv-2"


def test_a_declared_retention_bound_is_reported_as_read() -> None:
    bound = ObservedPoint(run="inv-0", at=T0, source=ELEMENTARY)
    fold = fold_incidents(
        _series("bb", source=ELEMENTARY),
        sources=[SourceHistory(name=ELEMENTARY, retention_bound=bound)],
        now=_now(2),
    )
    assert fold.incidents[0].retention_bound == {ELEMENTARY: bound}


# -- subjects ------------------------------------------------------------------


def test_a_renamed_claimed_test_stays_one_incident() -> None:
    series = [
        _obs(1, "bad", subject=CLAIMED, test=TEST),
        _obs(2, "bad", subject=CLAIMED, test="test.shop.order_id_present.f00"),
    ]
    fold = fold_incidents(series, now=_now(2))
    assert len(fold.incidents) == 1
    assert fold.incidents[0].tests == (TEST, "test.shop.order_id_present.f00")
    assert fold.incidents[0].subject.kind == "claim"


def test_a_claim_and_a_unique_id_spelled_alike_never_collide() -> None:
    alike = Subject(kind="claim", key=TEST)
    series = [_obs(1, "bad", subject=BY_ID), _obs(1, "bad", subject=alike)]
    fold = fold_incidents(series, now=_now(1))
    assert {i.subject.kind for i in fold.incidents} == {"claim", "unique_id"}


def test_each_column_and_metric_is_its_own_signal() -> None:
    amount = Signal(column="amount", metric="null_count")
    status = Signal(column="status", metric="null_count")
    series = [_obs(1, "bad", signal=amount), _obs(1, "good", signal=status)]
    fold = fold_incidents(series, now=_now(1))
    assert [i.signal for i in fold.incidents] == [amount]


def test_severity_is_kept_in_the_sources_words() -> None:
    series = [
        _obs(1, "bad", source=ELEMENTARY, severity="ERROR"),
        _obs(2, "bad", source=DBT, severity="warn"),
    ]
    fold = fold_incidents(series, now=_now(2))
    incident = fold.incidents[0]
    assert incident.severity == "warn"
    assert [o.severity for o in incident.observations] == ["ERROR", "warn"]


# -- no value crosses ----------------------------------------------------------

MARKER = "SECRET_VALUE_7f3a"


def _adapter(row: dict) -> Observation:
    """What an adapter does: read ids, a status, times, and nothing else."""

    return Observation(
        subject=Subject.of(row["unique_id"], row.get("claim")),
        test=row["unique_id"],
        outcome={"pass": "good", "fail": "bad"}[row["status"]],
        source=DBT,
        run=row["invocation_id"],
        sequence=row["sequence"],
        at=row["generated_at"],
    )


def test_an_incidents_serialized_form_holds_no_input_value() -> None:
    rows = [
        {
            "unique_id": TEST,
            "status": status,
            "invocation_id": f"inv-{n}",
            "sequence": n,
            "generated_at": T0 + timedelta(hours=n),
            "message": f"Got 3 results: {MARKER}",
            "failures": [{"order_id": MARKER}],
            "compiled_code": f"select '{MARKER}'",
        }
        for n, status in enumerate(["pass", "fail", "fail"], start=1)
    ]
    fold = fold_incidents([_adapter(row) for row in rows], now=_now(3))
    assert len(fold.incidents) == 1
    assert MARKER not in json.dumps(fold.model_dump(mode="json"))


def test_an_observation_refuses_a_field_that_could_carry_a_value() -> None:
    with pytest.raises(ValidationError):
        Observation(
            subject=BY_ID,
            test=TEST,
            outcome="bad",
            source=DBT,
            run="inv-1",
            sequence=1,
            message=MARKER,
        )


def test_a_severity_that_is_free_text_is_refused() -> None:
    with pytest.raises(ValidationError):
        _obs(1, "bad", severity=f"fail: order_id = '{MARKER}'")
