"""dbt's ``run_results.json`` test outcomes as incident observations (#1468).

The source adapter every dbt project has: it needs no warehouse connection and
no Elementary package. It hands :func:`~.incidents.fold_incidents` and
:func:`~.timeline.build_timeline` the same records every source hands them, one
:class:`~.observations.Observation` per subject and run and a
:class:`~.incidents.SourceHistory` naming the run read, under the source name
``dbt_run_results``. It opens and closes no incident: that is the fold's alone.

Which run it reads, by the rules ``maintain coverage`` reads last-run statuses
by (:mod:`.coverage_runs`, #1285):

- **The newest building run on disk that ran a test** (``build``, ``test``,
  ``run``, ``seed``, ``snapshot``), in the project's target path and Dex's
  isolated build directory, each with its ``cold/``; never a ``compile``,
  ``parse`` or ``ls``, whose ``success`` means compiled. A later ``dbt run`` or
  ``dbt seed`` holds no test result, so it observes nothing and is passed over,
  with a note: read, it would hide the failures of the build before it. The
  runs in those directories are not stitched into a history: they are separate
  invocations, often against separate targets, and each file holds one.
- **One invocation is all it holds.** So no failure has a known start: the run
  is the source's whole history and its retention bound, every interval before
  it reads unobserved, and the fold reports ``first_observed: unknown`` for
  every failure. The run read is never reported as the start of a failure.
- **A run this project did not log is refused.** When the dbt logs beside the
  file do not name its invocation, the file came from somewhere else (copied
  artifacts, another checkout), and nothing in it describes this project's
  tests. With no log to check, it is read, with a note saying so.

How it maps:

- Only a test the current manifest still has, enabled, is observed (data tests
  and unit tests alike); a result for one since removed or disabled is counted
  and dropped. A test the run did not reach has no observation, which the
  timeline reads as a run that did not check it.
- Statuses map as every source maps dbt's (:data:`~.observations.DBT_OUTCOMES`):
  ``pass`` good; ``fail``, ``error`` (``runtime error`` on older releases) and
  ``warn`` bad; ``skipped`` unobserved, never good. A status outside those is
  counted and gives no observation.
- The subject is the test's claim (``config.meta.dex.claim``) where it carries
  one, else its ``unique_id``. dbt writes one result per test, so the signal is
  always empty.
- The severity is ``warn`` on a warning; on a failure or an error, the token the
  test's ``config.severity`` sets in the manifest, where it is one.
- Rows repeating one test (a hand-merged file) are one observation, a failure
  over a warning over a pass over a skip, and counted.
- ``run_results.json`` records no commit, so every run's commit is ``unknown``.

No value crosses: from a result row it reads ``unique_id`` and ``status`` and
nothing else, never ``message``, ``compiled_code``, ``failures``,
``adapter_response``, ``relation_name`` or ``batch_results``. Files only:
nothing here opens a connection, runs dbt, or keeps state between calls.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..transform.artifact_runtime import BuildingRun, building_runs, run_is_logged
from ..transform.claims import manifest_claims
from .coverage_runs import DBT_STATUSES, current_tests, parse_time
from .incidents import SourceHistory
from .observations import (
    DBT_OUTCOMES,
    DBT_PRECEDENCE,
    SEVERITY_PATTERN,
    Observation,
    ObservedPoint,
    RunRef,
    Subject,
)

#: The source name every observation and run carries.
SOURCE = "dbt_run_results"

#: Whether the source was read, and the reason where it was not: ``missing``
#: (``no_run_results``: no building run on disk; ``no_test_results``: none of
#: them ran a test) or ``refused``
#: (``unlogged_invocation``: no dbt log beside the file names its invocation;
#: ``no_invocation_id``: the file names no usable invocation, and a run id is
#: not invented).
ReadStatus = Literal["read", "missing", "refused"]
ReasonCode = Literal[
    "no_run_results", "no_test_results", "unlogged_invocation", "no_invocation_id"
]

#: The longest id an observation holds (:class:`~.observations.Observation`).
_MAX_KEY_CHARS = 512
_MAX_RUN_CHARS = 128

_SEVERITY = re.compile(SEVERITY_PATTERN)


@dataclass(frozen=True)
class RunResultsCounts:
    """What one document held besides its observations, each counted."""

    #: Results for a test the manifest no longer has, or has disabled.
    dropped: int = 0
    #: Test results with a status the adapter does not map.
    unrecognized: int = 0
    #: Rows with no usable ``unique_id``, which no subject holds.
    unattributed: int = 0
    #: Rows that repeat a test already observed in the run.
    merged: int = 0
    #: Claims too long to hold, so the test is keyed by its ``unique_id``.
    claims_unread: int = 0


@dataclass(frozen=True)
class RunResultsRead:
    """What one call read, as the fold takes it, and how it got there.

    ``status`` and ``reason_code`` say whether the source was read; on
    ``missing`` and ``refused`` there are no observations and no history.
    ``run`` names the invocation found (``invocation_id``, ``generated_at``,
    ``command``, ``artifact``, ``cold`` and ``logged``), also when refused.
    """

    status: ReadStatus
    reason_code: ReasonCode | None = None
    observations: tuple[Observation, ...] = ()
    history: SourceHistory | None = None
    run: dict[str, Any] | None = None
    counts: RunResultsCounts = field(default_factory=RunResultsCounts)
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: The note in ``notes`` saying no failure has a known start, which holds of
    #: the file on its own. A caller that places the run in a longer history
    #: (``maintain incidents``, where Elementary recorded the same invocation)
    #: leaves it out there.
    unknown_start_note: str | None = None


@dataclass(frozen=True)
class ObservedRun:
    """One document, as the fold takes it: :func:`observe_run_results`'s result."""

    observations: tuple[Observation, ...]
    history: SourceHistory
    counts: RunResultsCounts


def _invocation(document: Mapping[str, Any]) -> str | None:
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("invocation_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_RUN_CHARS:
        return None
    return value


def _generated_at(document: Mapping[str, Any]) -> str:
    metadata = document.get("metadata")
    value = metadata.get("generated_at") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, str) else ""


def _started_at(document: Mapping[str, Any]) -> str | None:
    metadata = document.get("metadata")
    value = (
        metadata.get("invocation_started_at") if isinstance(metadata, Mapping) else None
    )
    # Echoed only where it reads as a time, as dbt types it (an optional
    # datetime); never replaced by `generated_at`, which is the run's end.
    if not isinstance(value, str) or parse_time(value.strip()) is None:
        return None
    return value.strip()


def _severities(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Each test's ``config.severity`` where it is a token, by ``unique_id``."""

    found: dict[str, str] = {}
    nodes = manifest.get("nodes")
    if not isinstance(nodes, Mapping):
        return found
    for unique_id, node in nodes.items():
        if not isinstance(node, Mapping) or node.get("resource_type") != "test":
            continue
        config = node.get("config")
        value = config.get("severity") if isinstance(config, Mapping) else None
        if isinstance(value, str) and _SEVERITY.match(value.strip()):
            found[str(unique_id)] = value.strip()
    return found


def observe_run_results(
    document: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    claims: Mapping[str, str] | None = None,
    sequence: int = 1,
) -> ObservedRun | None:
    """Turn one parsed ``run_results.json`` into observations and a history.

    ``manifest`` is the project's current compiled manifest: it decides which
    tests still exist and carries each one's claim and severity. ``claims``
    stands in for the claims read from it. ``sequence`` is the run's place in
    invocation order: alone the run is the only one, and folded beside another
    source that observed the same invocation (Elementary's ``dbt_invocations``
    carries dbt's ``invocation_id``), the caller passes the place that source
    gave it, since the fold refuses one run at two places. Returns ``None``
    where the document names no usable ``invocation_id``. Pure: reads nothing
    but its arguments.
    """

    invocation = _invocation(document)
    if invocation is None:
        return None
    at = parse_time(_generated_at(document))
    current = current_tests(dict(manifest))
    claimed = dict(manifest_claims(manifest) if claims is None else claims)
    severities = _severities(manifest)

    dropped = unrecognized = unattributed = merged = claims_unread = 0
    kept: dict[str, str] = {}
    results = document.get("results")
    for row in results if isinstance(results, list) else ():
        if not isinstance(row, Mapping):
            unattributed += 1
            continue
        raw_id = row.get("unique_id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            unattributed += 1
            continue
        unique_id = raw_id.strip()
        if not unique_id.startswith(("test.", "unit_test.")):
            continue
        if len(unique_id) > _MAX_KEY_CHARS:
            unattributed += 1
            continue
        if unique_id not in current:
            dropped += 1
            continue
        raw_status = row.get("status")
        status = DBT_STATUSES.get(
            raw_status.strip().lower() if isinstance(raw_status, str) else ""
        )
        if status is None or status not in DBT_OUTCOMES:
            unrecognized += 1
            continue
        held = kept.get(unique_id)
        if held is not None:
            merged += 1
            if DBT_PRECEDENCE[status] >= DBT_PRECEDENCE[held]:
                continue
        kept[unique_id] = status

    observations: list[Observation] = []
    for unique_id in sorted(kept):
        status = kept[unique_id]
        claim = claimed.get(unique_id)
        if claim is not None and len(claim) > _MAX_KEY_CHARS:
            claims_unread += 1
            claim = None
        severity: str | None = None
        if status == "warn":
            severity = "warn"
        elif DBT_OUTCOMES[status] == "bad":
            severity = severities.get(unique_id)
        observations.append(
            Observation(
                subject=Subject.of(unique_id, claim),
                test=unique_id,
                outcome=DBT_OUTCOMES[status],
                source=SOURCE,
                run=invocation,
                sequence=sequence,
                at=at,
                severity=severity,
            )
        )
    observations.sort(key=lambda o: (o.subject.kind, o.subject.key, o.test))
    history = SourceHistory(
        name=SOURCE,
        runs=(RunRef(run=invocation, sequence=sequence, at=at),),
        retention_bound=ObservedPoint(run=invocation, at=at, source=SOURCE),
    )
    return ObservedRun(
        observations=tuple(observations),
        history=history,
        counts=RunResultsCounts(
            dropped=dropped,
            unrecognized=unrecognized,
            unattributed=unattributed,
            merged=merged,
            claims_unread=claims_unread,
        ),
    )


def _ran_tests(document: Mapping[str, Any]) -> bool:
    """Whether a run holds a result for any test or unit test."""

    results = document.get("results")
    return any(
        isinstance(row, Mapping)
        and isinstance(row.get("unique_id"), str)
        and row["unique_id"].strip().startswith(("test.", "unit_test."))
        for row in (results if isinstance(results, list) else ())
    )


def _where(run: BuildingRun) -> str:
    where = (
        "Dex's artifact directory"
        if run.artifact == "dex"
        else "the project's target path"
    )
    return where + (" (a --cold-deps build)" if run.cold else "")


def read_run_results(
    project_dir: Path | str,
    manifest: Mapping[str, Any],
    *,
    claims: Mapping[str, str] | None = None,
    sequence: int = 1,
) -> RunResultsRead:
    """Read the newest building run that tested as the ``dbt_run_results`` source.

    A later run that ran no test (a ``dbt run``, a ``dbt seed``) is passed over
    and named in a note: it observes nothing, and reading it would hide the
    outcomes of the last run that did test.

    ``manifest`` is the project's current compiled manifest. ``claims`` stands
    in for the claim ids read from it (:func:`~..transform.claims.manifest_claims`).
    ``sequence`` is the run's place in invocation order, as
    :func:`observe_run_results` takes it.
    """

    project = Path(project_dir).absolute()
    found = building_runs(project)
    if not found:
        return RunResultsRead(
            status="missing",
            reason_code="no_run_results",
            notes=(
                "no run_results.json from a dbt build, test, run, seed or "
                "snapshot was found in the project's target path or Dex's "
                "artifact directory, so dbt's run results hold no test outcome",
            ),
        )
    tested = [run for run in found if _ran_tests(run.document)]
    if not tested:
        return RunResultsRead(
            status="missing",
            reason_code="no_test_results",
            notes=(
                f"{len(found)} run_results.json file(s) from a building dbt "
                "command were found, and none holds a test result (a dbt run "
                "or seed runs no test), so dbt's run results hold no test outcome",
            ),
        )
    newest = tested[-1]
    passed_over = found[found.index(newest) + 1 :]
    logged = run_is_logged(project, newest)
    run: dict[str, Any] = {
        "invocation_id": newest.invocation_id or None,
        "generated_at": newest.generated_at or None,
        # When the invocation started, where dbt recorded it: another source
        # orders runs by their start, and `generated_at` is the run's end.
        "invocation_started_at": _started_at(newest.document),
        "command": newest.command,
        "artifact": newest.artifact,
        "cold": newest.cold,
        "logged": logged,
    }
    where = _where(newest)
    if logged is False:
        return RunResultsRead(
            status="refused",
            reason_code="unlogged_invocation",
            run=run,
            warnings=(
                f"the run_results.json in {where} names invocation "
                f"{newest.invocation_id}, which no dbt log beside it records, so "
                "it was written somewhere else (copied artifacts, another "
                "checkout) and no test outcome is read from it",
            ),
        )
    observed = observe_run_results(
        newest.document, manifest, claims=claims, sequence=sequence
    )
    if observed is None:
        return RunResultsRead(
            status="refused",
            reason_code="no_invocation_id",
            run=run,
            warnings=(
                f"the run_results.json in {where} names no usable invocation_id, "
                "so its results cannot be placed as a run and none is read",
            ),
        )

    unknown_start = (
        f"test outcomes are read from one dbt {newest.command} invocation "
        f"({newest.invocation_id}) in {where}, generated at "
        f"{newest.generated_at or 'an unrecorded time'}. One invocation is all "
        "run_results.json holds, so no failure has a known start "
        "(first_observed: unknown), and every interval before it is unobserved"
    )
    notes = [unknown_start]
    if passed_over:
        commands = sorted({later.command for later in passed_over})
        notes.append(
            f"{len(passed_over)} later run(s) ({', '.join(commands)}) ran no "
            "test, so they are not read: a run with no test result observes "
            "nothing, and reading it would hide the outcomes of the last run "
            "that tested"
        )
    if logged is None:
        notes.append(
            "no dbt log was found to confirm the invocation that wrote "
            "run_results.json; its outcomes are read as found"
        )
    counts = observed.counts
    if counts.dropped:
        notes.append(
            f"{counts.dropped} test result(s) belong to tests the project no "
            "longer has, or has since disabled, and are not observed"
        )
    if counts.unrecognized:
        notes.append(
            f"{counts.unrecognized} test result(s) carried a status dex does "
            "not map and are not observed"
        )
    if counts.unattributed:
        notes.append(
            f"{counts.unattributed} result row(s) named no usable unique_id "
            "and are not observed"
        )
    if counts.merged:
        notes.append(
            f"{counts.merged} result row(s) repeated a test already read in "
            "the run; each test keeps one outcome, a failure over a pass"
        )
    if counts.claims_unread:
        notes.append(
            f"{counts.claims_unread} test(s) carry a claim id too long to "
            "hold and are keyed by their unique_id"
        )
    return RunResultsRead(
        status="read",
        observations=observed.observations,
        history=observed.history,
        run=run,
        counts=counts,
        notes=tuple(notes),
        unknown_start_note=unknown_start,
    )
