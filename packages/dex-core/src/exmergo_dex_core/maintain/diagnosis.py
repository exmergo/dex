"""From an incident to a cause: the pure half of ``dex maintain diagnose`` (#1475).

An incident (#1465) says a check started failing; its ``onset`` (#1466) is the
break a diagnosis bisects: after the last observed-good run, up to and
including the first observed-bad run. This module reads that break into the
range a cause must lie in, places it against the committed ``maintain snapshot``
series where there is one, names the window no evidence covers, and turns the
candidates a check confirmed or ruled out into one answer.

Four rules hold throughout:

- **An unobserved run is never read as good.** It widens the range rather than
  splitting it: the range runs back to the last *observed* pass, so the
  bounding snapshot is the last one at or before that pass, never one beside a
  run that recorded nothing.
- **A cause is confirmed by a check Dex already has**, named in the answer
  (:attr:`Candidate.check` and :attr:`Candidate.finding`). This module writes
  no detector: it is handed each candidate's result.
- **No cause is an answer.** Where no candidate is confirmed the verdict is
  ``no_cause``, with what was ruled out and the window that could not be seen.
  The most likely candidate is never named as the cause.
- **The fix is not written here.** A confirmed cause carries the ``transform``
  step that would repair it, as the rest of ``maintain`` hands repairs on.

Pure logic: nothing here opens a connection, reads a file or runs git.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .observations import UNKNOWN, Value, utc
from .timeline import Break, CommitRange, RunPoint, UnobservedReason

#: Who owns a cause: the loader (a source changed), a seed, the project (a
#: model or test changed), a package (its version changed), or the schedule
#: (one model built before another it reads had landed).
Owner = Literal["loader", "seed", "project", "package", "schedule"]

#: Where a candidate stands after its check.
CandidateStatus = Literal["confirmed", "ruled_out", "not_checked"]

#: The answer: one confirmed cause, more than one, or none.
Verdict = Literal["cause", "several", "no_cause"]

#: Why the snapshot series could not bound the break.
SeriesStatus = Literal["read", "none", "unreadable"]

#: Why an unseen window holds no evidence.
UnseenReason = Literal[
    #: No committed snapshot recorded the warehouse at all, so its state across
    #: the break was never seen: a check confirms the cause now, not when it
    #: entered.
    "no_snapshot_series",
    #: The snapshots that bound the break are the nearest records of the
    #: warehouse; between them nothing recorded it.
    "between_snapshots",
    #: No snapshot was committed at or before the last observed pass.
    "no_snapshot_before",
    #: No observed pass precedes the failure in what the sources hold, so the
    #: break begins before their retention.
    "before_retention",
]


class SnapshotPoint(Value):
    """One committed ``maintain snapshot`` baseline: the commit that recorded it
    and the time the baseline was captured (its ``created_at``)."""

    commit: str
    created_at: datetime


class DiagnosisRange(Value):
    """The range a cause must lie in, read off the incident's onset.

    ``after`` is the last observed-good run (``unknown`` where none precedes
    the failure), ``through`` the first observed-bad one. The unobserved and
    missed runs between them are why the range is as wide as it is: each widens
    it, none splits it. ``commits`` is the commit range the break spans where
    the repository's history was read (#1467).
    """

    after: RunPoint | Literal["unknown"]
    through: RunPoint
    unobserved_runs: int = 0
    missed_runs: int = 0
    reasons: tuple[UnobservedReason, ...] = ()
    #: Seconds from ``after`` to ``through``; ``None`` where either end has no
    #: time.
    span_seconds: float | None = None
    commits: CommitRange | None = None


class SnapshotBounds(Value):
    """The committed snapshots that bound the break, as ``maintain check`` reads
    a change against the snapshot it was measured from.

    ``before`` is the newest snapshot captured at or before the range's
    ``after`` run; the cause's check measures against it. ``after`` is the
    oldest captured at or after the ``through`` run, the nearest later record
    of the warehouse; ``None`` with a ``before`` means the check measured the
    live warehouse instead. ``series`` says whether there was a series to read.
    """

    series: SeriesStatus
    reason: str | None = None
    count: int = 0
    before: SnapshotPoint | None = None
    after: SnapshotPoint | None = None


class UnseenWindow(Value):
    """An interval no recorded state of the warehouse covers. ``None`` at
    either end is open: before retention, or up to the live warehouse."""

    from_at: datetime | None = None
    to_at: datetime | None = None
    reason: UnseenReason


class Candidate(Value):
    """One candidate cause and what its check said.

    ``check`` names the existing check that confirmed or ruled it out, and
    ``finding`` the finding code it reported (or would have reported).
    ``evidence`` holds the counts the check measured, never a value from the
    data. ``reason`` says why a candidate was ruled out or not checked.
    """

    owner: Owner
    kind: str
    relation: str | None = None
    columns: tuple[str, ...] = ()
    status: CandidateStatus
    check: str | None = None
    finding: str | None = None
    reason: str | None = None
    detail: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Diagnosis(Value):
    """One incident diagnosed: the range, the snapshots that bound it, the
    window no evidence covers, and every candidate with its check's answer.

    ``causes`` holds every confirmed candidate; ``verdict`` is ``cause`` with
    exactly one, ``several`` with more, and ``no_cause`` with none, in which
    case ``ruled_out`` and ``unseen`` are the answer. ``next_steps`` hands a
    confirmed cause's repair to ``transform``; nothing here writes it.
    """

    subject: str
    tests: tuple[str, ...]
    verdict: Verdict
    causes: tuple[Candidate, ...] = ()
    ruled_out: tuple[Candidate, ...] = ()
    not_checked: tuple[Candidate, ...] = ()
    range: DiagnosisRange
    snapshots: SnapshotBounds
    unseen: tuple[UnseenWindow, ...] = ()
    next_steps: tuple[dict[str, str], ...] = ()


def bisect(onset: Break) -> DiagnosisRange:
    """The range a cause must lie in: the incident's onset as it stands.

    The onset already runs back to the last *observed* pass, so a run that
    recorded nothing between it and the failure is counted here as a reason the
    range is wide, never as a pass that would narrow it.
    """

    after = onset.after
    through = onset.through
    span: float | None = None
    if after != UNKNOWN and after.at is not None and through.at is not None:
        span = (utc(through.at) - utc(after.at)).total_seconds()
    return DiagnosisRange(
        after=after,
        through=through,
        unobserved_runs=onset.unobserved_runs,
        missed_runs=onset.missed_runs,
        reasons=onset.reasons,
        span_seconds=span,
        commits=onset.commits,
    )


@dataclass(frozen=True)
class Series:
    """The committed snapshot series as read: its points oldest first, or why
    there is none."""

    status: SeriesStatus
    points: tuple[SnapshotPoint, ...] = ()
    reason: str | None = None


def bound(series: Series, span: DiagnosisRange) -> SnapshotBounds:
    """The snapshots that bound ``span``: the newest captured at or before its
    last observed pass, and the oldest captured at or after its first failure.

    With no observed pass (``after`` unknown) or no time on it, no snapshot can
    be placed before the break: the cause's check then has no measured before.
    """

    if series.status != "read" or not series.points:
        return SnapshotBounds(
            series="none" if series.status == "read" else series.status,
            reason=series.reason
            or "no maintain snapshot baseline is committed to the repository",
            count=0,
        )
    points = sorted(series.points, key=lambda p: (utc(p.created_at), p.commit))
    before: SnapshotPoint | None = None
    after_at = span.after.at if span.after != UNKNOWN else None
    if after_at is not None:
        limit = utc(after_at)
        earlier = [p for p in points if utc(p.created_at) <= limit]
        before = earlier[-1] if earlier else None
    later: SnapshotPoint | None = None
    if span.through.at is not None:
        limit = utc(span.through.at)
        later = next((p for p in points if utc(p.created_at) >= limit), None)
    return SnapshotBounds(series="read", count=len(points), before=before, after=later)


def unseen(span: DiagnosisRange, bounds: SnapshotBounds) -> tuple[UnseenWindow, ...]:
    """The windows no recorded state of the warehouse covers.

    With no series, the whole break: the warehouse was never recorded across
    it, so a check confirms a cause as present now and cannot say when inside
    the break it entered. With one, the stretch between the bounding snapshots
    (open-ended to the live warehouse where no later snapshot exists), which is
    as finely as the evidence resolves the break. Before an unknown ``after``,
    the time before the sources' retention.
    """

    windows: list[UnseenWindow] = []
    after_at = span.after.at if span.after != UNKNOWN else None
    if span.after == UNKNOWN:
        windows.append(
            UnseenWindow(from_at=None, to_at=span.through.at, reason="before_retention")
        )
    if bounds.series != "read":
        windows.append(
            UnseenWindow(
                from_at=after_at, to_at=span.through.at, reason="no_snapshot_series"
            )
        )
        return tuple(windows)
    if bounds.before is None:
        windows.append(
            UnseenWindow(
                from_at=after_at, to_at=span.through.at, reason="no_snapshot_before"
            )
        )
        return tuple(windows)
    windows.append(
        UnseenWindow(
            from_at=bounds.before.created_at,
            to_at=bounds.after.created_at if bounds.after is not None else None,
            reason="between_snapshots",
        )
    )
    return tuple(windows)


def decide(
    candidates: Iterable[Candidate],
) -> tuple[
    Verdict, tuple[Candidate, ...], tuple[Candidate, ...], tuple[Candidate, ...]
]:
    """The verdict and the candidates split by status.

    Only a confirmed candidate is a cause. Several confirmed are each named,
    and none is preferred; none confirmed is ``no_cause``, whatever was left
    unchecked.
    """

    held = list(candidates)
    confirmed = tuple(c for c in held if c.status == "confirmed")
    ruled_out = tuple(c for c in held if c.status == "ruled_out")
    not_checked = tuple(c for c in held if c.status == "not_checked")
    verdict: Verdict = (
        "no_cause" if not confirmed else "cause" if len(confirmed) == 1 else "several"
    )
    return verdict, confirmed, ruled_out, not_checked


def project_candidate(span: DiagnosisRange) -> Candidate:
    """The project as a cause, read off the commit range the break spans.

    Ruled out where the failing commit itself passed earlier (``same_commit``):
    no project file changed across the break. Otherwise not checked here: a
    model or test changed in the range is confirmed by ``maintain verify`` on
    the changed model, which #1476 proves.
    """

    commits = span.commits
    if commits is None:
        return Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            reason="no_commit_history",
            detail="the repository's history was not read, so the commits the "
            "break spans are not known",
        )
    if commits.status == "same_commit":
        return Candidate(
            owner="project",
            kind="project_change",
            status="ruled_out",
            check="onset.commits",
            reason="same_commit",
            detail=f"commit {commits.through.commit} passed in an earlier run and "
            "failed in this one, so no project file changed across the break",
            evidence={"commit": commits.through.commit},
        )
    evidence: dict[str, Any] = {"status": commits.status}
    if commits.count is not None:
        evidence["count"] = commits.count
    if commits.reason is not None:
        evidence["reason"] = commits.reason
    return Candidate(
        owner="project",
        kind="project_change",
        status="not_checked",
        reason="kind_not_proven_here",
        detail="a model or test changed in the break's commit range is confirmed "
        "by maintain verify on the changed model, which this release does not "
        "run for diagnosis",
        evidence=evidence,
    )


#: The owners whose cause kinds this release does not check, each named so a
#: ``no_cause`` answer never reads as having ruled them out.
_UNCHECKED_OWNERS: tuple[tuple[Owner, str, str], ...] = (
    ("seed", "seed_edited", "a seed's file hash at the two bounding runs"),
    ("package", "package_upgraded", "the package versions at the two bounding runs"),
    (
        "schedule",
        "built_before_parent",
        "both models' run times, from dbt_invocations or the orchestrator",
    ),
)


def unchecked_owners() -> tuple[Candidate, ...]:
    """A ``not_checked`` candidate for each owner this release does not check."""

    return tuple(
        Candidate(
            owner=owner,
            kind=kind,
            status="not_checked",
            reason="kind_not_proven_here",
            detail=f"confirmed by {evidence}, which this release does not read",
        )
        for owner, kind, evidence in _UNCHECKED_OWNERS
    )


def diagnosis(
    *,
    subject: str,
    tests: Sequence[str],
    onset: Break,
    series: Series,
    candidates: Iterable[Candidate],
    next_steps: Iterable[dict[str, str]] = (),
) -> Diagnosis:
    """One incident's diagnosis from its onset, the series and the candidates
    its checks answered. ``candidates`` are the checked ones; the project and
    the owners this release does not check are added here."""

    span = bisect(onset)
    bounds = bound(series, span)
    held = [*candidates, project_candidate(span), *unchecked_owners()]
    verdict, confirmed, ruled_out, not_checked = decide(held)
    return Diagnosis(
        subject=subject,
        tests=tuple(tests),
        verdict=verdict,
        causes=confirmed,
        ruled_out=ruled_out,
        not_checked=not_checked,
        range=span,
        snapshots=bounds,
        unseen=unseen(span, bounds),
        next_steps=tuple(next_steps) if confirmed else (),
    )


__all__ = [
    "Candidate",
    "CandidateStatus",
    "Diagnosis",
    "DiagnosisRange",
    "Owner",
    "Series",
    "SeriesStatus",
    "SnapshotBounds",
    "SnapshotPoint",
    "UnseenReason",
    "UnseenWindow",
    "Verdict",
    "bisect",
    "bound",
    "decide",
    "diagnosis",
    "project_candidate",
    "unchecked_owners",
    "unseen",
]
