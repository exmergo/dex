"""A coverage timeline that never reads an unobserved interval as good (#1466).

A run that did not check a test looks the same as a run in which it passed,
unless something says the run happened and what it covered. The timeline says
so: it lays one source's runs out in invocation order from a named
**heartbeat**, marks every interval that recorded no outcome as unobserved and
why, and, per subject, reads the series of observed outcomes into the subject's
"last verified" point and the **breaks** a diagnosis bisects over.

An interval is unobserved when:

- ``missed_run``: the heartbeat has a gap longer than the expected schedule
  allows, so dbt did not run (or ran with the package's hooks off, which writes
  no ``dbt_invocations`` row either);
- ``hooks_off`` / ``uploads_off``: the adapter knows the run happened (the
  orchestrator lists it) and that the package's hooks did not run, or ran with
  the test-results upload off;
- ``snapshot_missed``: on the snapshot series, a snapshot the schedule expected
  is missing;
- ``retention``: before the oldest point the source still holds;
- ``not_checked``: the run recorded outcomes, and none for this check (it was
  deselected, skipped, or did not exist yet).

dbt's own test materialization is not among them: Elementary's
``handle_tests_results`` uploads a row for every test dbt ran, built from the
test's definition where the package's materialization cached nothing (read at
Elementary e3ab23d8, not run), so a test with an observation is observed.

The heartbeat is ``dbt_invocations`` (one row per run). With the invocation
upload off (``disable_dbt_invocation_autoupload``) there is none, and
:func:`choose_heartbeat` falls back to the orchestrator's run history, then the
``maintain snapshot`` series (coarser: one point per snapshot), then the runs
the observations name, and says which it used and why.

Order is the caller's invocation ``sequence``, never a clock: sources stamp time
with different clocks. Each run carries the commit its invocation records, or
``unknown``. Ordering by commit is #1467.

Pure logic: nothing here opens a connection, reads a file or runs dbt.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .observations import (
    UNKNOWN,
    Observation,
    ObservedPoint,
    RunRef,
    Signal,
    Subject,
    Value,
    median_gap,
    utc,
)

#: Where the timeline's runs came from. ``source_runs`` is a run list a caller
#: handed in with no named heartbeat, ``observations`` the runs the
#: observations themselves name; neither can show a run that recorded nothing.
HeartbeatBasis = Literal[
    "dbt_invocations", "orchestrator", "snapshots", "source_runs", "observations"
]

#: Why the heartbeat is not ``dbt_invocations``.
FallbackReason = Literal["invocation_upload_off", "no_invocations"]

#: The ``disable_dbt_invocation_autoupload`` setting, as the resolver read it.
Upload = Literal["on", "off", "unknown"]

#: Why an interval recorded no outcome for a check.
UnobservedReason = Literal[
    "missed_run",
    "snapshot_missed",
    "hooks_off",
    "uploads_off",
    "retention",
    "not_checked",
]

#: The three states, never two.
PointState = Literal["observed_good", "observed_bad", "unobserved"]

#: A gap more than this many expected gaps holds at least one missed run.
DEFAULT_GAP_TOLERANCE = 1.5

#: With a derived schedule, the shortest gap read as a missed run: a burst of
#: builds run by hand seconds apart is not a schedule, and an hourly job is the
#: tightest one dbt projects commonly keep. A declared schedule has no floor.
DEFAULT_GAP_FLOOR_SECONDS = 3600.0


class Heartbeat(Value):
    """The run series the timeline is laid on, and why it is that one."""

    basis: HeartbeatBasis
    runs: tuple[RunRef, ...] = ()
    invocation_upload: Upload = "unknown"
    fallback_reason: FallbackReason | None = None

    @property
    def granularity(self) -> Literal["run", "snapshot"]:
        return "snapshot" if self.basis == "snapshots" else "run"


def choose_heartbeat(
    *,
    invocation_upload: bool | None,
    invocations: Iterable[RunRef] = (),
    orchestrator: Iterable[RunRef] = (),
    snapshots: Iterable[RunRef] = (),
) -> Heartbeat:
    """The heartbeat to lay a timeline on.

    ``invocation_upload`` is whether the package uploads ``dbt_invocations``
    (``ElementaryRuntime.uploads["invocations"]``; ``None`` where the var is
    unknown). With it on, or unknown and rows present, the invocations are the
    heartbeat. Otherwise the orchestrator's run history, then the snapshot
    series, then nothing (the observations' own runs), with the reason named.
    """

    upload: Upload = (
        "unknown" if invocation_upload is None else "on" if invocation_upload else "off"
    )
    rows = tuple(invocations)
    if invocation_upload is not False and rows:
        return Heartbeat(basis="dbt_invocations", runs=rows, invocation_upload=upload)
    reason: FallbackReason = (
        "invocation_upload_off" if invocation_upload is False else "no_invocations"
    )
    for basis, runs in (("orchestrator", orchestrator), ("snapshots", snapshots)):
        held = tuple(runs)
        if held:
            return Heartbeat(
                basis=basis,  # type: ignore[arg-type]
                runs=held,
                invocation_upload=upload,
                fallback_reason=reason,
            )
    return Heartbeat(
        basis="observations", invocation_upload=upload, fallback_reason=reason
    )


class GapSchedule(Value):
    """The expected gap between heartbeat points.

    ``declared`` where the caller states it, ``derived`` (the median gap between
    dated heartbeat points) with three or more of them, else ``unknown``, in
    which case no gap is read as a missed run. ``missed_after_seconds`` is the
    gap beyond which a missed run is read.
    """

    basis: Literal["declared", "derived", "unknown"]
    gap_seconds: float | None = None
    points: int = 0
    missed_after_seconds: float | None = None


class TimelineEntry(Value):
    """One run, one interval of missed runs, or the time before retention."""

    kind: Literal["run", "missed", "retention"]
    run: str | None = None
    sequence: int | None = None
    at: datetime | None = None
    #: The commit the run's invocation records, or ``unknown``.
    commit: str | None = None
    #: Whether the run is a heartbeat point, rather than one only an
    #: observation names.
    in_heartbeat: bool = True
    #: Whether the run recorded test outcomes at all.
    recorded: bool = True
    reason: UnobservedReason | None = None
    #: On a missed interval, the runs on either side; on the retention entry,
    #: the first run held.
    after_run: str | None = None
    before_run: str | None = None
    after_sequence: int | None = None
    before_sequence: int | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    #: On a missed interval, the runs the schedule expected in it (at least 1).
    missed_runs: int | None = None


class Timeline(Value):
    """One source's runs in invocation order, with every unobserved interval."""

    source: str
    heartbeat: HeartbeatBasis
    granularity: Literal["run", "snapshot"]
    invocation_upload: Upload
    fallback_reason: FallbackReason | None = None
    order: Literal["invocation"] = "invocation"
    schedule: GapSchedule
    retention_bound: ObservedPoint | Literal["unknown"]
    entries: tuple[TimelineEntry, ...] = ()


def build_timeline(
    source: str,
    heartbeat: Heartbeat,
    observations: Iterable[Observation] = (),
    *,
    retention_bound: ObservedPoint | None = None,
    expected_gap_seconds: float | None = None,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    gap_floor_seconds: float = DEFAULT_GAP_FLOOR_SECONDS,
) -> Timeline:
    """Lay ``source``'s runs on ``heartbeat``, marking every unobserved interval.

    The runs are the heartbeat's, plus any run ``source``'s observations name
    that the heartbeat lacks. ``expected_gap_seconds`` declares the schedule;
    left out, it is derived from the heartbeat's dated points and a gap is read
    as missed only past the larger of ``gap_tolerance`` expected gaps and
    ``gap_floor_seconds``. ``retention_bound`` is the oldest point the source
    holds; left out, the first run stands in.
    """

    if gap_tolerance <= 1:
        raise ValueError("gap_tolerance must be greater than 1")
    if gap_floor_seconds < 0:
        raise ValueError("gap_floor_seconds must not be negative")
    if expected_gap_seconds is not None and expected_gap_seconds <= 0:
        raise ValueError("expected_gap_seconds must be positive")

    own = [o for o in observations if o.source == source]
    named_by_observations = heartbeat.basis == "observations"
    runs: dict[str, TimelineEntry] = {}
    for ref in heartbeat.runs:
        runs[ref.run] = TimelineEntry(
            kind="run",
            run=ref.run,
            sequence=ref.sequence,
            at=ref.at,
            commit=ref.commit or UNKNOWN,
            recorded=ref.unobserved is None,
            reason=ref.unobserved,
        )
    for observation in sorted(own, key=lambda o: (o.sequence, o.run)):
        if observation.run in runs:
            entry = runs[observation.run]
            if entry.commit == UNKNOWN and observation.commit:
                runs[observation.run] = entry.model_copy(
                    update={"commit": observation.commit}
                )
            continue
        runs[observation.run] = TimelineEntry(
            kind="run",
            run=observation.run,
            sequence=observation.sequence,
            at=observation.at,
            commit=observation.commit or UNKNOWN,
            in_heartbeat=named_by_observations,
        )
    ordered = sorted(runs.values(), key=lambda e: (e.sequence or 0, e.run or ""))
    beats = [e for e in ordered if e.in_heartbeat]

    if expected_gap_seconds is not None:
        schedule = GapSchedule(
            basis="declared",
            gap_seconds=float(expected_gap_seconds),
            points=sum(1 for e in beats if e.at is not None),
            missed_after_seconds=gap_tolerance * expected_gap_seconds,
        )
    else:
        gap, points = median_gap(e.at for e in beats)
        schedule = (
            GapSchedule(basis="unknown", points=points)
            if gap is None or gap <= 0
            else GapSchedule(
                basis="derived",
                gap_seconds=gap,
                points=points,
                missed_after_seconds=max(gap_tolerance * gap, gap_floor_seconds),
            )
        )

    missed_reason: UnobservedReason = (
        "snapshot_missed" if heartbeat.basis == "snapshots" else "missed_run"
    )
    missed_before: dict[str, TimelineEntry] = {}
    if schedule.missed_after_seconds is not None and schedule.gap_seconds:
        # Gaps are located over every dated run, so a run an observation names
        # splits a heartbeat interval rather than being counted as missed.
        dated = [(e, utc(e.at)) for e in ordered if e.at is not None]
        for (earlier, early_at), (later, late_at) in itertools.pairwise(dated):
            span = (late_at - early_at).total_seconds()
            if span <= schedule.missed_after_seconds:
                continue
            missed_before[later.run or ""] = TimelineEntry(
                kind="missed",
                reason=missed_reason,
                recorded=False,
                in_heartbeat=False,
                after_run=earlier.run,
                before_run=later.run,
                after_sequence=earlier.sequence,
                before_sequence=later.sequence,
                from_at=earlier.at,
                to_at=later.at,
                missed_runs=_missed_runs(span, schedule),
            )

    entries: list[TimelineEntry] = []
    if ordered:
        first = ordered[0]
        entries.append(
            TimelineEntry(
                kind="retention",
                reason="retention",
                recorded=False,
                in_heartbeat=False,
                before_run=first.run,
                before_sequence=first.sequence,
                to_at=retention_bound.at if retention_bound is not None else first.at,
            )
        )
    for entry in ordered:
        gap_entry = missed_before.get(entry.run or "")
        if gap_entry is not None:
            entries.append(gap_entry)
        entries.append(entry)

    bound: ObservedPoint | Literal["unknown"]
    if retention_bound is not None:
        bound = retention_bound
    elif ordered:
        bound = ObservedPoint(run=ordered[0].run or "", at=ordered[0].at, source=source)
    else:
        bound = UNKNOWN
    return Timeline(
        source=source,
        heartbeat=heartbeat.basis,
        granularity=heartbeat.granularity,
        invocation_upload=heartbeat.invocation_upload,
        fallback_reason=heartbeat.fallback_reason,
        schedule=schedule,
        retention_bound=bound,
        entries=tuple(entries),
    )


class RunPoint(Value):
    """A run in the timeline: its id, place in invocation order, time and
    commit (``unknown`` where the invocation records none)."""

    run: str
    sequence: int
    at: datetime | None = None
    commit: str = UNKNOWN


class TimelineSegment(Value):
    """Consecutive intervals in one state for the same reason.

    ``runs`` counts the runs in a ``runs`` segment and estimates the missed
    runs in a ``missed`` one; a ``retention`` segment has no count.
    """

    kind: Literal["runs", "missed", "retention"] = "runs"
    state: PointState
    reason: UnobservedReason | None = None
    from_run: str | None = None
    to_run: str | None = None
    runs: int | None = None
    #: On a ``missed`` segment, the runs either side; on ``retention``, the
    #: first run held.
    after_run: str | None = None
    before_run: str | None = None


class Break(Value):
    """Where a check went from good to bad: after the last observed-good run,
    up to and including the first observed-bad run.

    ``after`` is ``unknown`` where no observed pass precedes the failure inside
    what the sources hold. The counts are the unobserved intervals between the
    two, which widen the range rather than split it.
    """

    after: RunPoint | Literal["unknown"]
    through: RunPoint
    unobserved_runs: int = 0
    missed_runs: int = 0
    reasons: tuple[UnobservedReason, ...] = ()


class LatestPoint(Value):
    """The check's state at the timeline's latest run."""

    run: str
    sequence: int
    state: PointState
    reason: UnobservedReason | None = None


class SubjectHistory(Value):
    """The series of observed outcomes for one subject and signal.

    ``last_verified`` is its latest observed-good run: a deselected test, a
    missed run or a run with uploads off leaves it where it was.
    """

    subject: Subject
    signal: Signal
    source: str
    heartbeat: HeartbeatBasis
    segments: tuple[TimelineSegment, ...] = ()
    last_verified: RunPoint | Literal["unknown"] = UNKNOWN
    last_observed: RunPoint | Literal["unknown"] = UNKNOWN
    latest: LatestPoint | None = None
    breaks: tuple[Break, ...] = ()


@dataclass(frozen=True)
class _Point:
    """One interval of one check's history, in timeline order."""

    kind: Literal["run", "missed", "retention"]
    state: PointState
    reason: UnobservedReason | None
    order: tuple[float, int]
    entry: TimelineEntry
    point: RunPoint | None = None


def _merge(items: Iterable[Observation]) -> dict[int, tuple[str, Observation]]:
    """Each sequence's outcome: a failure over a pass over nothing."""

    rank = {"bad": 0, "good": 1}
    merged: dict[int, tuple[str, Observation]] = {}
    for observation in items:
        if observation.outcome == "unobserved":
            continue
        held = merged.get(observation.sequence)
        if held is None or rank[observation.outcome] < rank[held[0]]:
            merged[observation.sequence] = (observation.outcome, observation)
    return merged


def _points(timeline: Timeline, items: Iterable[Observation]) -> list[_Point]:
    outcomes = _merge(items)
    on_axis: set[int] = set()
    points: list[_Point] = []
    for entry in timeline.entries:
        if entry.kind == "retention":
            points.append(
                _Point(
                    "retention", "unobserved", "retention", (float("-inf"), 0), entry
                )
            )
            continue
        if entry.kind == "missed":
            points.append(
                _Point(
                    "missed",
                    "unobserved",
                    entry.reason,
                    (float(entry.before_sequence or 0), 0),
                    entry,
                )
            )
            continue
        if entry.sequence is None or entry.run is None:
            continue
        on_axis.add(entry.sequence)
        held = outcomes.get(entry.sequence)
        commit = entry.commit if entry.commit and entry.commit != UNKNOWN else None
        if held is not None and commit is None:
            commit = held[1].commit
        point = RunPoint(
            run=entry.run,
            sequence=entry.sequence,
            at=entry.at,
            commit=commit or UNKNOWN,
        )
        if held is not None:
            state: PointState = "observed_bad" if held[0] == "bad" else "observed_good"
            points.append(
                _Point("run", state, None, (float(entry.sequence), 1), entry, point)
            )
        else:
            reason: UnobservedReason = entry.reason or "not_checked"
            order = (float(entry.sequence), 1)
            points.append(_Point("run", "unobserved", reason, order, entry, point))
    for sequence, (outcome, observation) in outcomes.items():
        if sequence in on_axis:
            continue
        point = RunPoint(
            run=observation.run,
            sequence=sequence,
            at=observation.at,
            commit=observation.commit or UNKNOWN,
        )
        entry = TimelineEntry(
            kind="run",
            run=observation.run,
            sequence=sequence,
            at=observation.at,
            commit=point.commit,
            in_heartbeat=False,
        )
        state = "observed_bad" if outcome == "bad" else "observed_good"
        points.append(_Point("run", state, None, (float(sequence), 1), entry, point))
    points = _split_missed(points, timeline.schedule)
    points.sort(key=lambda p: p.order)
    return points


def _missed_runs(span_seconds: float, schedule: GapSchedule) -> int:
    """The runs a span between two observed runs held, by the schedule."""

    if (
        schedule.missed_after_seconds is None
        or not schedule.gap_seconds
        or span_seconds <= schedule.missed_after_seconds
    ):
        return 0
    return max(1, round(span_seconds / schedule.gap_seconds) - 1)


def _split_missed(points: list[_Point], schedule: GapSchedule) -> list[_Point]:
    """Split each missed interval at the runs another source observed inside
    it: those runs happened, so only the stretches between them that the
    schedule says held runs stay missed (#1466 review)."""

    off_axis = sorted(
        (p for p in points if p.kind == "run" and not p.entry.in_heartbeat),
        key=lambda p: p.order,
    )
    if not off_axis:
        return points
    kept: list[_Point] = []
    for p in points:
        entry = p.entry
        after, before = entry.after_sequence, entry.before_sequence
        inside = (
            [
                q
                for q in off_axis
                if q.point is not None and after < q.point.sequence < before
            ]
            if p.kind == "missed" and after is not None and before is not None
            else []
        )
        if not inside:
            kept.append(p)
            continue
        bounds = [(after, entry.after_run, entry.from_at)]
        bounds += [
            (q.point.sequence, q.point.run, q.point.at) for q in inside if q.point
        ]
        bounds.append((before, entry.before_run, entry.to_at))
        for (lo, lo_run, lo_at), (hi, hi_run, hi_at) in itertools.pairwise(bounds):
            if lo_at is None or hi_at is None:
                continue
            count = _missed_runs((utc(hi_at) - utc(lo_at)).total_seconds(), schedule)
            if count == 0:
                continue
            piece = entry.model_copy(
                update={
                    "after_run": lo_run,
                    "before_run": hi_run,
                    "after_sequence": lo,
                    "before_sequence": hi,
                    "from_at": lo_at,
                    "to_at": hi_at,
                    "missed_runs": count,
                }
            )
            kept.append(
                _Point("missed", "unobserved", p.reason, (float(hi or 0), 0), piece)
            )
    return kept


def _segments(points: Iterable[_Point]) -> tuple[TimelineSegment, ...]:
    segments: list[TimelineSegment] = []
    for p in points:
        if p.kind == "retention":
            segments.append(
                TimelineSegment(
                    kind="retention",
                    state="unobserved",
                    reason="retention",
                    before_run=p.entry.before_run,
                )
            )
            continue
        if p.kind == "missed":
            segments.append(
                TimelineSegment(
                    kind="missed",
                    state="unobserved",
                    reason=p.reason,
                    runs=p.entry.missed_runs,
                    after_run=p.entry.after_run,
                    before_run=p.entry.before_run,
                )
            )
            continue
        if p.point is None:
            continue
        last = segments[-1] if segments else None
        if (
            last is not None
            and last.kind == "runs"
            and last.state == p.state
            and last.reason == p.reason
        ):
            segments[-1] = last.model_copy(
                update={"to_run": p.point.run, "runs": (last.runs or 0) + 1}
            )
        else:
            segments.append(
                TimelineSegment(
                    state=p.state,
                    reason=p.reason,
                    from_run=p.point.run,
                    to_run=p.point.run,
                    runs=1,
                )
            )
    return tuple(segments)


def _breaks(points: Iterable[_Point]) -> tuple[Break, ...]:
    breaks: list[Break] = []
    last_good: RunPoint | None = None
    previous: PointState | None = None
    unobserved = missed = 0
    reasons: set[str] = set()
    for p in points:
        if p.state == "unobserved":
            if p.kind == "missed":
                missed += p.entry.missed_runs or 0
            elif p.kind == "run":
                unobserved += 1
            if p.reason is not None:
                reasons.add(p.reason)
            continue
        if p.point is None:
            continue
        if p.state == "observed_bad" and previous != "observed_bad":
            breaks.append(
                Break(
                    after=last_good if last_good is not None else UNKNOWN,
                    through=p.point,
                    unobserved_runs=unobserved,
                    missed_runs=missed,
                    reasons=tuple(sorted(reasons)),  # type: ignore[arg-type]
                )
            )
        if p.state == "observed_good":
            last_good = p.point
        previous = p.state
        unobserved = missed = 0
        reasons = set()
    return tuple(breaks)


def place_break(
    timeline: Timeline,
    observations: Iterable[Observation],
    through: int,
) -> Break | None:
    """The break whose failure is the run at sequence ``through``.

    ``after`` is the latest observed pass from any source before it, even in a
    run another source failed: the lifecycle closes an incident on the opening
    source's passes alone, so a merged run can hide the pass that ended the
    previous incident. ``observations`` are one subject and signal's.
    """

    items = list(observations)
    goods = [o for o in items if o.outcome == "good" and o.sequence < through]
    after_seq = max((o.sequence for o in goods), default=None)
    points = _points(timeline, items)
    by_seq = {
        p.point.sequence: p.point
        for p in points
        if p.kind == "run" and p.point is not None
    }
    through_point = by_seq.get(through)
    if through_point is None:
        return None
    low = (float("-inf"), -1) if after_seq is None else (float(after_seq), 1)
    high = (float(through), 1)
    unobserved = missed = 0
    reasons: set[str] = set()
    for p in points:
        if not low < p.order < high or p.state != "unobserved":
            continue
        if p.kind == "missed":
            missed += p.entry.missed_runs or 0
        elif p.kind == "run":
            unobserved += 1
        if p.reason is not None:
            reasons.add(p.reason)
    after = by_seq.get(after_seq) if after_seq is not None else None
    return Break(
        after=after if after is not None else UNKNOWN,
        through=through_point,
        unobserved_runs=unobserved,
        missed_runs=missed,
        reasons=tuple(sorted(reasons)),  # type: ignore[arg-type]
    )


def subject_history(
    timeline: Timeline,
    subject: Subject,
    signal: Signal,
    observations: Iterable[Observation],
) -> SubjectHistory:
    """One subject and signal's history on ``timeline``.

    ``observations`` may come from more than one source observing the same
    invocations (the same ``sequence``): within one run a failure is read
    over a pass. A run only another source observed is added in its place.
    """

    items = [o for o in observations if o.subject == subject and o.signal == signal]
    points = _points(timeline, items)
    runs = [p for p in points if p.kind == "run" and p.point is not None]
    good = [p.point for p in runs if p.state == "observed_good"]
    seen = [p.point for p in runs if p.state != "unobserved"]
    # The latest run, on the heartbeat or not: a run only an observation
    # names (no invocation row) still happened, and ``last_verified`` may
    # already point at it.
    latest = None
    tail = runs[-1] if runs else None
    if tail is not None and tail.point is not None:
        latest = LatestPoint(
            run=tail.point.run,
            sequence=tail.point.sequence,
            state=tail.state,
            reason=tail.reason,
        )
    return SubjectHistory(
        subject=subject,
        signal=signal,
        source=timeline.source,
        heartbeat=timeline.heartbeat,
        segments=_segments(points),
        last_verified=good[-1] if good and good[-1] is not None else UNKNOWN,
        last_observed=seen[-1] if seen and seen[-1] is not None else UNKNOWN,
        latest=latest,
        breaks=_breaks(points),
    )


def subject_histories(
    timeline: Timeline, observations: Iterable[Observation]
) -> tuple[SubjectHistory, ...]:
    """Every subject and signal the observations name, on ``timeline``."""

    items = list(observations)
    keys: dict[tuple[Subject, Signal], None] = {}
    for observation in items:
        keys.setdefault((observation.subject, observation.signal), None)
    ordered = sorted(
        keys,
        key=lambda k: (k[0].kind, k[0].key, k[1].column or "", k[1].metric or ""),
    )
    return tuple(subject_history(timeline, s, g, items) for s, g in ordered)


def window_segments(
    timeline: Timeline,
    items: Iterable[Observation],
    start: int,
    end: int | None,
) -> tuple[TimelineSegment, ...]:
    """The segments from run ``start`` through run ``end`` (or the latest),
    missed intervals between two runs of the window included."""

    kept: list[_Point] = []
    for p in _points(timeline, items):
        if p.kind == "retention":
            continue
        if p.kind == "missed":
            after = p.entry.after_sequence
            before = p.entry.before_sequence
            if after is None or before is None or after < start:
                continue
            if end is not None and before > end:
                continue
            kept.append(p)
            continue
        if p.point is None:
            continue
        if p.point.sequence < start or (end is not None and p.point.sequence > end):
            continue
        kept.append(p)
    return _segments(kept)


__all__ = [
    "DEFAULT_GAP_FLOOR_SECONDS",
    "DEFAULT_GAP_TOLERANCE",
    "Break",
    "FallbackReason",
    "GapSchedule",
    "Heartbeat",
    "HeartbeatBasis",
    "LatestPoint",
    "PointState",
    "RunPoint",
    "SubjectHistory",
    "Timeline",
    "TimelineEntry",
    "TimelineSegment",
    "UnobservedReason",
    "Upload",
    "build_timeline",
    "choose_heartbeat",
    "place_break",
    "subject_histories",
    "subject_history",
    "window_segments",
]
