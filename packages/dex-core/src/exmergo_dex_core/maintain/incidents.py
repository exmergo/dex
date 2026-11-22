"""Test failures as incidents: one value type and one lifecycle rule (#1465).

Every source of test outcomes (dbt's ``run_results.json``, Elementary's
``elementary_test_results``) is turned by its adapter into the same
:class:`Observation` records, and :func:`fold_incidents` is the only thing
that turns those into :class:`Incident` records. Two sources therefore can
never open or close an incident by different rules.

The rule, per subject and signal, with observations in invocation order:

- An observed failure from any source opens an incident, or extends the open
  one. The source whose failure opened it is the incident's opening source;
  where several fail in that run, the one named first in ``sources`` opens it.
- ``N`` consecutive observed passes from the opening source close it (``N`` is
  set per signal, default 2), so a test that flaps stays one incident. A pass
  from any other source is attached as evidence and never counts toward ``N``.
  Each source has one outcome per run, a failure over a pass, so ``N`` counts
  runs rather than rows.
- An unobserved interval (a run that did not check the test, a run with the
  package's hooks off, a skipped test) neither opens nor closes an incident,
  and does not break a run of passes either: ``N`` counts observed passes.
- At call time, an open incident whose check stopped running ends as
  ``no_longer_observed``, never as ``closed``: its subject is no longer among
  the current ones (removed or disabled), or its opening source has not
  observed it for longer than the expected schedule allows. The schedule is
  derived from the history itself (the median gap between observations) and
  reported as derived; with too little history it is ``unknown`` and nothing
  is ended for staleness.

Each incident's ``coverage`` and ``onset`` are read off its opening source's
coverage timeline (:mod:`.timeline`, #1466), so a missed run, a run with the
package's hooks or uploads off and a run that did not check the test each read
unobserved, with the reason, and the onset is the break a diagnosis bisects.

Three states, never two: an interval is observed good, observed bad, or
unobserved, and nothing here reads an unobserved one as good. An incident's
first failure is ``unknown`` unless an observed pass precedes it, because a
failure the sources hold no start for may have begun before their retention.

No value crosses into an incident. An observation carries ids, statuses, a
severity token and times, and its model refuses any other field, so an adapter
cannot pass a result's message, compiled SQL or failing rows through it.

Pure logic: nothing here opens a connection, reads a file or runs dbt, and no
incident state is kept between calls.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from .observations import (
    SOURCE_PATTERN,
    UNKNOWN,
    Observation,
    ObservedPoint,
    Outcome,
    RunRef,
    RunUnobserved,
    Signal,
    Subject,
    median_gap,
    utc,
)
from .observations import Value as _Value
from .timeline import (
    Break,
    Heartbeat,
    HeartbeatBasis,
    Timeline,
    TimelineSegment,
    build_timeline,
    place_break,
    window_segments,
)

#: Where an incident stands at call time.
State = Literal["open", "closed", "no_longer_observed"]

#: Passes from the opening source that close an incident when nothing else is set.
DEFAULT_CLOSE_AFTER = 2

#: How many expected gaps may pass without an observation before a check is
#: read as no longer running.
DEFAULT_STALE_AFTER_GAPS = 3.0

#: The shortest allowance a check is given before it reads as no longer
#: running, whatever its history says: a burst of builds run by hand seconds
#: apart is not a schedule, and an hourly job is the tightest one dbt projects
#: commonly keep.
DEFAULT_STALE_FLOOR_SECONDS = 3600.0

#: Kept under its #1465 name: a coverage segment is a timeline segment.
CoverageSegment = TimelineSegment

#: Why :func:`fold_incidents` refused its input.
FoldInputCode = Literal["run_sequence_conflict", "opener_tie_undeclared"]


class FoldInputError(ValueError):
    """Input the fold refuses rather than read in an order nobody declared.

    ``run_sequence_conflict``: one run id carries two ``sequence`` values
    across the observations, the sources' runs and the timelines, so the
    invocation order is not one order. ``opener_tie_undeclared``: two sources
    failed in the run that opens an incident and ``sources`` gives no
    precedence between them. ``run`` names the run, and ``sequences`` or
    ``sources`` what disagreed.
    """

    def __init__(
        self,
        code: FoldInputCode,
        message: str,
        *,
        run: str,
        sequences: tuple[int, ...] = (),
        sources: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.run = run
        self.sequences = sequences
        self.sources = sources


class SourceHistory(_Value):
    """What one source holds: its runs (the heartbeat) and its retention bound.

    ``runs`` lets coverage name the runs that did not observe a subject, and
    lets the schedule be derived from the source when a subject has too little
    history of its own. ``retention_bound`` is the oldest point the source still
    holds, as read at call time; left out, the oldest run stands in.
    """

    name: str = Field(pattern=SOURCE_PATTERN)
    runs: tuple[RunRef, ...] = ()
    retention_bound: ObservedPoint | None = None


class IncidentObservation(_Value):
    """One observation an incident rests on. ``evidence`` marks one from a
    source other than the opening source that disagrees with it: attached,
    never counted."""

    run: str
    at: datetime | None = None
    commit: str | None = None
    source: str
    outcome: Literal["good", "bad"]
    severity: str | None = None
    test: str
    evidence: bool = False


class Coverage(_Value):
    """Which intervals of the incident's window the opening source observed.

    Laid on the opening source's timeline (#1466): ``heartbeat`` names the run
    series it was read from, and a run that recorded nothing, a run that did
    not check this test and a gap the schedule says held runs are each an
    ``unobserved`` segment with its reason, never a pass.
    """

    source: str
    heartbeat: HeartbeatBasis = "observations"
    segments: tuple[TimelineSegment, ...] = ()


class Schedule(_Value):
    """The expected gap between observations, derived from the history.

    ``basis`` is ``derived`` (from the subject's own observations, or from the
    source's runs, which ``series`` names) or ``unknown`` where fewer than three
    dated points exist, in which case nothing is ended for staleness.
    """

    basis: Literal["derived", "unknown"]
    series: Literal["subject", "source"] | None = None
    source: str
    median_gap_seconds: float | None = None
    points: int = 0
    #: How long without an observation reads as no longer running: the
    #: larger of ``stale_after_gaps`` expected gaps and the floor.
    allowance_seconds: float | None = None


class Incident(_Value):
    """One failure of one subject and signal, from first to latest observation."""

    subject: Subject
    signal: Signal
    #: The test ``unique_id`` values seen, in order: more than one where the
    #: test was renamed while its claim stayed.
    tests: tuple[str, ...]
    first_observed: ObservedPoint | Literal["unknown"]
    last_observed: ObservedPoint
    observations: tuple[IncidentObservation, ...]
    #: The latest failure's severity, in its source's words.
    severity: str | None = None
    opening_source: str
    coverage: Coverage
    #: Where the check broke: after the last observed-good run before the
    #: first failure, up to and including that failure, with the unobserved
    #: intervals between them (#1466). ``after`` is ``unknown`` exactly where
    #: ``first_observed`` is.
    onset: Break | None = None
    #: The oldest point each source holds, or ``unknown``.
    retention_bound: dict[str, ObservedPoint | Literal["unknown"]]
    state: State
    #: The pass that closed it, on a closed incident.
    closed_by: ObservedPoint | None = None
    #: Why a ``no_longer_observed`` incident ended: ``removed`` (its subject is
    #: no longer a current one) or ``stale`` (not observed for longer than the
    #: schedule allows).
    ended_reason: Literal["removed", "stale"] | None = None
    #: The schedule its staleness was judged against, on an incident still
    #: open or ended at call time.
    schedule: Schedule | None = None


class IncidentFold(_Value):
    """What :func:`fold_incidents` returns: the incidents, and the schedule
    derived for each source."""

    incidents: tuple[Incident, ...] = ()
    schedules: dict[str, Schedule] = Field(default_factory=dict)


IncidentKey = tuple[Subject, Signal]


@dataclass(frozen=True)
class _Allowance:
    """How many expected gaps, and at least how long, before a check is stale."""

    gaps: float
    floor_seconds: float


@dataclass
class _Open:
    """The incident being built while the series is walked."""

    opening: Observation
    observations: list[Observation] = field(default_factory=list)
    evidence: set[int] = field(default_factory=set)
    streak: int = 0
    first_known: bool = False
    closed_by: Observation | None = None


def fold_incidents(
    observations: Iterable[Observation],
    *,
    sources: Iterable[SourceHistory] = (),
    close_after: int = DEFAULT_CLOSE_AFTER,
    close_after_by: Mapping[IncidentKey, int] | None = None,
    current_subjects: Iterable[Subject] | None = None,
    now: datetime | None = None,
    stale_after_gaps: float = DEFAULT_STALE_AFTER_GAPS,
    stale_floor_seconds: float = DEFAULT_STALE_FLOOR_SECONDS,
    timelines: Mapping[str, Timeline] | None = None,
) -> IncidentFold:
    """Fold observed outcomes into incidents by the one lifecycle rule.

    ``close_after_by`` sets ``N`` for one subject and signal, over the
    ``close_after`` default. ``current_subjects``, where given, is every subject
    whose check still exists; an open incident on any other ends
    ``no_longer_observed``. ``now`` is the call time staleness is judged at,
    and an open incident ends ``stale`` once its opening source has not
    observed it for the larger of ``stale_after_gaps`` expected gaps and
    ``stale_floor_seconds``. ``timelines``, keyed by source name, lays each
    incident's coverage on its opening source's timeline (#1466), so a missed
    run, a run with hooks or uploads off and a run that did not check the test
    each read unobserved with their reason; a source with no timeline gets one
    built from the runs it names, with no named heartbeat.

    The order of ``sources`` is the precedence between them: where two sources
    fail in the run that opens an incident, the one named first opens it. A tie
    between sources ``sources`` does not name, and a run id carrying two
    ``sequence`` values, are refused with :class:`FoldInputError`.
    """

    if close_after < 1:
        raise ValueError("close_after must be at least 1")
    if stale_after_gaps <= 0:
        raise ValueError("stale_after_gaps must be positive")
    if stale_floor_seconds < 0:
        raise ValueError("stale_floor_seconds must not be negative")
    allowance = _Allowance(stale_after_gaps, stale_floor_seconds)
    overrides = dict(close_after_by or {})
    for key, value in overrides.items():
        if value < 1:
            raise ValueError(f"close_after_by[{key!r}] must be at least 1")
    moment = utc(now) if now is not None else datetime.now(UTC)
    current = None if current_subjects is None else set(current_subjects)

    observed = list(observations)
    declared = list(sources)
    histories = {history.name: history for history in declared}
    precedence: dict[str, int] = {}
    for rank, history in enumerate(declared):
        precedence.setdefault(history.name, rank)
    laid = dict(timelines or {})
    _check_run_sequences(observed, declared, laid)
    for name, timeline in laid.items():
        if name != timeline.source:
            raise ValueError(
                f"timelines[{name!r}] is the timeline of {timeline.source!r}"
            )
        held = histories.get(name)
        if held is None or not held.runs:
            histories[name] = SourceHistory(
                name=name,
                runs=_heartbeat_runs(timeline),
                retention_bound=(
                    held.retention_bound
                    if held is not None and held.retention_bound is not None
                    else timeline.retention_bound
                    if timeline.retention_bound != UNKNOWN
                    else None
                ),
            )
    runs_by_source = _runs_by_source(observed, histories)
    for name, runs in runs_by_source.items():
        if name not in laid:
            history = histories.get(name)
            named = history is not None and bool(history.runs)
            basis: HeartbeatBasis = "source_runs" if named else "observations"
            laid[name] = build_timeline(
                name,
                Heartbeat(basis=basis, runs=tuple(runs)),
                observed,
                retention_bound=None if history is None else history.retention_bound,
            )
    schedules = {
        name: _schedule([run.at for run in runs], name, "source", allowance)
        for name, runs in sorted(runs_by_source.items())
    }
    retention = _retention(runs_by_source, histories)

    series: dict[IncidentKey, list[Observation]] = {}
    for observation in observed:
        series.setdefault((observation.subject, observation.signal), []).append(
            observation
        )

    ranked: list[tuple[tuple[int, str, str, str, str], Incident]] = []
    for key, items in series.items():
        threshold = overrides.get(key, close_after)
        by_source: dict[str, list[Observation]] = {}
        for observation in sorted(items, key=_order):
            by_source.setdefault(observation.source, []).append(observation)
        for built in _walk(items, threshold, precedence):
            subject, signal = key
            ranked.append(
                (
                    (
                        built.opening.sequence,
                        subject.kind,
                        subject.key,
                        signal.column or "",
                        signal.metric or "",
                    ),
                    _incident(
                        key,
                        built,
                        by_source,
                        laid,
                        items,
                        retention,
                        schedules,
                        current,
                        moment,
                        allowance,
                    ),
                )
            )
    ranked.sort(key=lambda pair: pair[0])
    incidents = [incident for _, incident in ranked]
    return IncidentFold(incidents=tuple(incidents), schedules=schedules)


_OUTCOME_RANK = {"bad": 0, "good": 1, "unobserved": 2}


def _check_run_sequences(
    observed: Iterable[Observation],
    sources: Iterable[SourceHistory],
    timelines: Mapping[str, Timeline],
) -> None:
    """Refuse a run id that carries two ``sequence`` values anywhere.

    A run id names one invocation, which every source sees at one place in
    invocation order. Two places for one run mean an adapter numbered its runs
    on its own, and reading them as given would order the sources' outcomes
    against each other wrongly.
    """

    seen: dict[str, tuple[int, str]] = {}

    def hold(run: str, sequence: int, where: str) -> None:
        held = seen.setdefault(run, (sequence, where))
        if held[0] != sequence:
            raise FoldInputError(
                "run_sequence_conflict",
                f"run {run!r} is sequence {held[0]} in {held[1]} and "
                f"{sequence} in {where}: one run has one place in invocation "
                "order, so number every source's runs from the same series",
                run=run,
                sequences=(held[0], sequence),
                sources=tuple(dict.fromkeys((held[1], where))),
            )

    for observation in observed:
        hold(observation.run, observation.sequence, observation.source)
    for history in sources:
        for ref in history.runs:
            hold(ref.run, ref.sequence, history.name)
    for name, timeline in timelines.items():
        for entry in timeline.entries:
            if entry.kind != "run" or entry.run is None or entry.sequence is None:
                continue
            hold(entry.run, entry.sequence, name)


def _order(observation: Observation) -> tuple[int, int, str, str]:
    """Invocation order; within one run a failure is read before a pass, so a
    source that disagrees in the same run is evidence, not the opener. The
    source and test names only keep the order stable: which source opens a tie is
    :func:`_opener`'s, by the declared precedence."""

    rank = _OUTCOME_RANK[observation.outcome]
    return (observation.sequence, rank, observation.source, observation.test)


def _first_per_source(failures: list[Observation]) -> list[Observation]:
    """Each source's first failure in one run: the candidates to open it."""

    kept: dict[str, Observation] = {}
    for observation in failures:
        kept.setdefault(observation.source, observation)
    return list(kept.values())


def _counts_as_pass(
    observation: Observation, failed: set[str], counted: set[str]
) -> bool:
    """Whether an opening-source pass adds to ``N``: once per run, and never in
    a run the same source failed (a failure over a pass, as
    ``timeline._merge`` merges a run). Two rows for one key in one run (two
    tests carrying one claim, or an adapter that did not deduplicate) are
    one observed run, so ``N`` counts runs. Every row is still recorded."""

    if observation.source in failed or observation.source in counted:
        return False
    counted.add(observation.source)
    return True


def _opener(
    candidates: list[Observation], precedence: Mapping[str, int]
) -> Observation:
    """The failure that opens an incident when sources fail in the same run:
    the source declared first in ``sources``. Refused where a tied source is
    not declared, since no order between them is given."""

    if len(candidates) == 1:
        return candidates[0]
    names = sorted(o.source for o in candidates)
    undeclared = [name for name in names if name not in precedence]
    if undeclared:
        raise FoldInputError(
            "opener_tie_undeclared",
            f"run {candidates[0].run!r} failed in {', '.join(names)}, and "
            f"{', '.join(undeclared)} has no precedence: name every source "
            "that can fail in one run in `sources`, highest precedence first",
            run=candidates[0].run,
            sources=tuple(names),
        )
    return min(candidates, key=lambda o: precedence[o.source])


def _walk(
    items: list[Observation], threshold: int, precedence: Mapping[str, int]
) -> list[_Open]:
    """The incidents one subject and signal's series holds, oldest first."""

    built: list[_Open] = []
    current: _Open | None = None
    seen_good = False
    ordered = sorted(items, key=_order)
    for _, run in itertools.groupby(ordered, key=lambda o: o.sequence):
        outcomes = list(run)
        failures = [o for o in outcomes if o.outcome == "bad"]
        failed = {o.source for o in failures}
        counted: set[str] = set()
        if failures:
            if current is None:
                opening = _opener(_first_per_source(failures), precedence)
                current = _Open(opening=opening, first_known=seen_good)
                failures = [opening, *(o for o in failures if o is not opening)]
            current.observations.extend(failures)
            current.streak = 0
        for observation in (o for o in outcomes if o.outcome == "good"):
            if current is None:
                seen_good = True
                continue
            current.observations.append(observation)
            if observation.source != current.opening.source:
                current.evidence.add(len(current.observations) - 1)
                continue
            if not _counts_as_pass(observation, failed, counted):
                continue
            current.streak += 1
            if current.streak >= threshold:
                current.closed_by = observation
                built.append(current)
                current = None
                seen_good = True
    if current is not None:
        built.append(current)
    return built


def _incident(
    key: IncidentKey,
    built: _Open,
    by_source: Mapping[str, list[Observation]],
    timelines: Mapping[str, Timeline],
    items: list[Observation],
    retention: Mapping[str, ObservedPoint | Literal["unknown"]],
    schedules: Mapping[str, Schedule],
    current: set[Subject] | None,
    moment: datetime,
    allowance: _Allowance,
) -> Incident:
    subject, signal = key
    opener = built.opening.source
    failures = [o for o in built.observations if o.outcome == "bad"]
    latest = failures[-1]
    tests: list[str] = []
    for observation in built.observations:
        if observation.test not in tests:
            tests.append(observation.test)
    records = tuple(
        IncidentObservation(
            run=o.run,
            at=o.at,
            commit=o.commit,
            source=o.source,
            outcome=o.outcome,
            severity=o.severity,
            test=o.test,
            evidence=index in built.evidence,
        )
        for index, o in enumerate(built.observations)
    )
    end_sequence = built.closed_by.sequence if built.closed_by is not None else None
    timeline = timelines[opener]
    coverage = Coverage(
        source=opener,
        heartbeat=timeline.heartbeat,
        segments=window_segments(
            timeline, by_source.get(opener, []), built.opening.sequence, end_sequence
        ),
    )
    onset = place_break(timeline, items, built.opening.sequence)

    state: State = "closed" if built.closed_by is not None else "open"
    ended_reason: Literal["removed", "stale"] | None = None
    schedule: Schedule | None = None
    if state == "open":
        own = [o for o in by_source.get(opener, []) if o.outcome != "unobserved"]
        schedule = _schedule([o.at for o in own], opener, "subject", allowance)
        if schedule.basis == "unknown":
            schedule = schedules.get(opener, schedule)
        if current is not None and subject not in current:
            state, ended_reason = "no_longer_observed", "removed"
        else:
            last_at = _latest_time(own)
            if (
                schedule.allowance_seconds is not None
                and last_at is not None
                and (moment - last_at).total_seconds() > schedule.allowance_seconds
            ):
                state, ended_reason = "no_longer_observed", "stale"

    return Incident(
        subject=subject,
        signal=signal,
        tests=tuple(tests),
        first_observed=_point(built.opening) if built.first_known else UNKNOWN,
        last_observed=_point(latest),
        observations=records,
        severity=latest.severity,
        opening_source=opener,
        coverage=coverage,
        onset=onset,
        retention_bound=dict(retention),
        state=state,
        closed_by=_point(built.closed_by) if built.closed_by is not None else None,
        ended_reason=ended_reason,
        schedule=schedule,
    )


def _point(observation: Observation) -> ObservedPoint:
    return ObservedPoint(
        run=observation.run, at=observation.at, source=observation.source
    )


def _runs_by_source(
    observed: list[Observation], histories: Mapping[str, SourceHistory]
) -> dict[str, list[RunRef]]:
    """Each source's runs in invocation order: its declared heartbeat, else
    the runs its observations name."""

    runs: dict[str, dict[str, RunRef]] = {}
    for name, history in histories.items():
        runs[name] = {run.run: run for run in history.runs}
    for observation in observed:
        if histories.get(observation.source) and histories[observation.source].runs:
            continue
        runs.setdefault(observation.source, {}).setdefault(
            observation.run,
            RunRef(
                run=observation.run,
                sequence=observation.sequence,
                at=observation.at,
                commit=observation.commit,
            ),
        )
    return {
        name: sorted(by_run.values(), key=lambda run: (run.sequence, run.run))
        for name, by_run in runs.items()
    }


def _retention(
    runs_by_source: Mapping[str, list[RunRef]],
    histories: Mapping[str, SourceHistory],
) -> dict[str, ObservedPoint | Literal["unknown"]]:
    bounds: dict[str, ObservedPoint | Literal["unknown"]] = {}
    for name in sorted(set(runs_by_source) | set(histories)):
        history = histories.get(name)
        if history is not None and history.retention_bound is not None:
            bounds[name] = history.retention_bound
            continue
        runs = runs_by_source.get(name) or []
        bounds[name] = (
            ObservedPoint(run=runs[0].run, at=runs[0].at, source=name)
            if runs
            else UNKNOWN
        )
    return bounds


def _run_unobserved(reason: str | None) -> RunUnobserved | None:
    if reason == "hooks_off":
        return "hooks_off"
    if reason == "uploads_off":
        return "uploads_off"
    return None


def _heartbeat_runs(timeline: Timeline) -> tuple[RunRef, ...]:
    """The timeline's heartbeat runs, as the runs its source holds."""

    return tuple(
        RunRef(
            run=entry.run,
            sequence=entry.sequence,
            at=entry.at,
            commit=None if entry.commit in (None, UNKNOWN) else entry.commit,
            unobserved=_run_unobserved(entry.reason),
        )
        for entry in timeline.entries
        if entry.kind == "run"
        and entry.in_heartbeat
        and entry.run is not None
        and entry.sequence is not None
    )


def _schedule(
    times: Iterable[datetime | None],
    source: str,
    series: Literal["subject", "source"],
    allowance: _Allowance,
) -> Schedule:
    """The median gap between dated points, or ``unknown`` with too few."""

    median, points = median_gap(times)
    if median is None:
        return Schedule(basis="unknown", source=source, points=points)
    return Schedule(
        basis="derived",
        series=series,
        source=source,
        median_gap_seconds=median,
        points=points,
        allowance_seconds=max(allowance.gaps * median, allowance.floor_seconds),
    )


def _latest_time(items: Iterable[Observation]) -> datetime | None:
    dated = [utc(o.at) for o in items if o.at is not None]
    return max(dated) if dated else None


__all__ = [
    "DEFAULT_CLOSE_AFTER",
    "DEFAULT_STALE_AFTER_GAPS",
    "DEFAULT_STALE_FLOOR_SECONDS",
    "UNKNOWN",
    "Break",
    "Coverage",
    "CoverageSegment",
    "FoldInputCode",
    "FoldInputError",
    "Incident",
    "IncidentFold",
    "IncidentObservation",
    "Observation",
    "ObservedPoint",
    "Outcome",
    "RunRef",
    "RunUnobserved",
    "Schedule",
    "Signal",
    "SourceHistory",
    "State",
    "Subject",
    "fold_incidents",
]
