"""Diagnosis's pure half (#1475): the range, the snapshots that bound it, the
window no recorded state covers, and the verdict.

Each incident here is folded from observations by the one lifecycle rule
(:func:`fold_incidents`) on a declared heartbeat, so its onset is the break the
coverage timeline (#1466) reads, as ``maintain diagnose`` receives it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from exmergo_dex_core.maintain import diagnosis as diag
from exmergo_dex_core.maintain.incidents import (
    Observation,
    RunRef,
    SourceHistory,
    Subject,
    fold_incidents,
)
from exmergo_dex_core.maintain.timeline import (
    Break,
    CommitRange,
    CommitRef,
    RunPoint,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
SUBJECT = Subject.of("test.shop.unique_stg_order_items_order_item_id.abc")


def _at(hour: int) -> datetime:
    return START + timedelta(hours=hour)


def _onset(states: dict[int, str]) -> Break:
    """The onset of the one incident a series folds into.

    ``states`` maps a run number (its hour) to ``good``, ``bad`` or
    ``hooks_off``; every run is on the source's heartbeat.
    """

    runs = tuple(
        RunRef(
            run=f"inv-{n}",
            sequence=n,
            at=_at(n),
            unobserved="hooks_off" if state == "hooks_off" else None,
        )
        for n, state in sorted(states.items())
    )
    observations = [
        Observation(
            subject=SUBJECT,
            run=f"inv-{n}",
            sequence=n,
            at=_at(n),
            source="elementary",
            outcome=state,
            test=SUBJECT.key,
        )
        for n, state in sorted(states.items())
        if state in {"good", "bad"}
    ]
    fold = fold_incidents(
        observations,
        sources=[SourceHistory(name="elementary", runs=runs)],
        now=_at(max(states)),
    )
    [incident] = fold.incidents
    assert incident.onset is not None
    return incident.onset


def _series(*hours: float) -> diag.Series:
    return diag.Series(
        "read",
        points=tuple(
            diag.SnapshotPoint(
                commit=f"{n:040x}", created_at=_at(0) + timedelta(hours=h)
            )
            for n, h in enumerate(hours, start=1)
        ),
    )


# --- acceptance: an unobserved run widens the range -------------------------------


def test_a_run_that_recorded_nothing_widens_the_range_back_to_the_last_pass():
    onset = _onset({1: "good", 2: "hooks_off", 3: "bad"})
    span = diag.bisect(onset)

    assert span.after != "unknown"
    assert span.after.run == "inv-1"
    assert span.through.run == "inv-3"
    assert span.unobserved_runs == 1
    assert span.reasons == ("hooks_off",)
    assert span.span_seconds == 2 * 3600


def test_the_same_run_observed_passing_narrows_the_range():
    onset = _onset({1: "good", 2: "good", 3: "bad"})
    span = diag.bisect(onset)

    assert span.after != "unknown"
    assert span.after.run == "inv-2"
    assert span.unobserved_runs == 0
    assert span.reasons == ()
    assert span.span_seconds == 3600


def test_the_widened_range_is_bounded_by_an_earlier_snapshot():
    # One snapshot before run 1, one between runs 1 and 2. The unobserved run
    # leaves the last observed pass at run 1, so the snapshot between runs 1
    # and 2 is not before it: the bound reaches back to the earlier snapshot.
    series = _series(0.5, 1.5)
    widened = diag.bound(
        series, diag.bisect(_onset({1: "good", 2: "hooks_off", 3: "bad"}))
    )
    narrowed = diag.bound(series, diag.bisect(_onset({1: "good", 2: "good", 3: "bad"})))

    assert widened.before is not None and narrowed.before is not None
    assert widened.before.created_at == _at(0) + timedelta(hours=0.5)
    assert narrowed.before.created_at == _at(0) + timedelta(hours=1.5)


# --- the snapshots that bound the break, and the window nothing saw ---------------


def test_with_no_series_the_whole_break_is_the_unseen_window():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    bounds = diag.bound(diag.Series("read"), span)
    [window] = diag.unseen(span, bounds)

    assert bounds.series == "none"
    assert bounds.before is None and bounds.after is None
    assert window.reason == "no_snapshot_series"
    assert window.from_at == _at(1)
    assert window.to_at == _at(2)


def test_with_a_series_the_bounding_snapshots_are_named_and_the_window_between():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    bounds = diag.bound(_series(0.5, 3.0), span)
    [window] = diag.unseen(span, bounds)

    assert bounds.series == "read"
    assert bounds.count == 2
    assert bounds.before is not None and bounds.after is not None
    assert bounds.before.created_at == _at(0) + timedelta(hours=0.5)
    assert bounds.after.created_at == _at(3)
    assert window.reason == "between_snapshots"
    assert (window.from_at, window.to_at) == (
        bounds.before.created_at,
        bounds.after.created_at,
    )


def test_no_later_snapshot_leaves_the_window_open_to_the_live_warehouse():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    bounds = diag.bound(_series(0.5), span)
    [window] = diag.unseen(span, bounds)

    assert bounds.after is None
    assert window.to_at is None


def test_a_series_with_no_snapshot_before_the_last_pass_says_so():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    bounds = diag.bound(_series(5.0), span)
    [window] = diag.unseen(span, bounds)

    assert bounds.before is None
    assert window.reason == "no_snapshot_before"


def test_a_failure_with_no_prior_pass_adds_the_window_before_retention():
    span = diag.bisect(_onset({1: "bad"}))
    windows = diag.unseen(span, diag.bound(diag.Series("read"), span))

    assert span.after == "unknown"
    assert [w.reason for w in windows] == ["before_retention", "no_snapshot_series"]
    assert windows[0].from_at is None


def test_an_unreadable_series_is_not_read_as_no_series():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    bounds = diag.bound(diag.Series("unreadable", reason="no git"), span)

    assert bounds.series == "unreadable"
    assert bounds.reason == "no git"


# --- the verdict ------------------------------------------------------------------


def _candidate(status: str, kind: str = "duplicate_load") -> diag.Candidate:
    return diag.Candidate(owner="loader", kind=kind, status=status)  # type: ignore[arg-type]


def test_no_confirmed_candidate_is_no_cause_and_names_none():
    result = diag.diagnosis(
        subject=SUBJECT.key,
        tests=[SUBJECT.key],
        onset=_onset({1: "good", 2: "bad"}),
        series=diag.Series("read"),
        candidates=[_candidate("ruled_out")],
        next_steps=[{"command": "dex transform plan", "purpose": "x"}],
    )

    assert result.verdict == "no_cause"
    assert result.causes == ()
    assert [c.kind for c in result.ruled_out] == ["duplicate_load"]
    # The project and the owners this release does not check are listed, so
    # no_cause never reads as having ruled them out.
    assert {c.owner for c in result.not_checked} == {
        "project",
        "seed",
        "package",
        "schedule",
    }
    assert result.unseen
    # Nothing is handed to transform without a confirmed cause.
    assert result.next_steps == ()


def test_one_confirmed_candidate_is_the_cause():
    result = diag.diagnosis(
        subject=SUBJECT.key,
        tests=[SUBJECT.key],
        onset=_onset({1: "good", 2: "bad"}),
        series=diag.Series("read"),
        candidates=[_candidate("confirmed"), _candidate("ruled_out", "other")],
    )

    assert result.verdict == "cause"
    assert [c.kind for c in result.causes] == ["duplicate_load"]


def test_two_confirmed_candidates_are_both_named_and_neither_preferred():
    verdict, confirmed, _, _ = diag.decide(
        [_candidate("confirmed", "a"), _candidate("confirmed", "b")]
    )

    assert verdict == "several"
    assert [c.kind for c in confirmed] == ["a", "b"]


# --- the project, read off the commit range ---------------------------------------


def _range(status: str, **extra) -> diag.DiagnosisRange:
    through = RunPoint(run="inv-2", sequence=2, at=_at(2), commit="b" * 40)
    ref = CommitRef(commit="b" * 40, run="inv-2", sequence=2, basis="git_sha")
    return diag.DiagnosisRange(
        after=RunPoint(run="inv-1", sequence=1, at=_at(1)),
        through=through,
        commits=CommitRange(status=status, through=ref, **extra),  # type: ignore[arg-type]
    )


def test_the_failing_commit_having_passed_earlier_rules_the_project_out():
    candidate = diag.project_candidate(_range("same_commit"))

    assert candidate.status == "ruled_out"
    assert candidate.reason == "same_commit"
    assert candidate.check == "onset.commits"


@pytest.mark.parametrize("status", ["ordered", "unknown", "not_ordered"])
def test_any_other_commit_range_leaves_the_project_not_checked(status: str):
    candidate = diag.project_candidate(_range(status, count=3))

    assert candidate.status == "not_checked"


def test_with_no_history_read_the_project_is_not_checked():
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    candidate = diag.project_candidate(span)

    assert candidate.status == "not_checked"
    assert candidate.reason == "no_commit_history"


# --- a snapshot is placed by when its warehouse state was measured ----------------


def test_a_snapshot_is_placed_by_its_measurement_not_its_writing():
    # Written after the failure, measured before the last pass: it bounds the
    # break from before, and is not the later record of the warehouse.
    span = diag.bisect(_onset({1: "good", 2: "bad"}))
    point = diag.SnapshotPoint(
        commit="1" * 40,
        created_at=_at(3),
        measured_at=_at(0) + timedelta(minutes=30),
    )
    bounds = diag.bound(diag.Series("read", points=(point,)), span)
    [window] = diag.unseen(span, bounds)

    assert bounds.before == point
    assert bounds.after is None
    assert window.from_at == point.measured_at


def test_a_carried_forward_warehouse_keeps_the_time_it_was_measured():
    from exmergo_dex_core.maintain.snapshot import Snapshot
    from exmergo_dex_core.maintain.snapshot_series import _measured

    first = Snapshot(created_at=_at(1).isoformat(), warehouse_from="metadata")
    carried = first.model_copy(update={"created_at": _at(5).isoformat()})
    points = [
        diag.SnapshotPoint(commit="1" * 40, created_at=_at(1)),
        diag.SnapshotPoint(commit="2" * 40, created_at=_at(5)),
    ]
    placed = _measured(points, {"1" * 40: first, "2" * 40: carried})

    assert [p.placed_at() for p in placed] == [_at(1), _at(1)]


def test_a_cache_pinned_warehouse_is_placed_at_the_cache_time():
    from exmergo_dex_core.maintain.snapshot import Snapshot
    from exmergo_dex_core.maintain.snapshot_series import _measured

    pinned = Snapshot(
        created_at=_at(5).isoformat(),
        warehouse_from="cache",
        cache_updated_at=_at(2).isoformat(),
    )
    [placed] = _measured(
        [diag.SnapshotPoint(commit="1" * 40, created_at=_at(5))], {"1" * 40: pinned}
    )

    assert placed.placed_at() == _at(2)
