"""The last recorded status of each test, for ``maintain coverage`` (#1285).

A test that exists may never have run, may have been skipped, or may have
failed last time, so an inventory of configuration alone reads "has a test" as
"is protected". This reads the newest ``run_results.json`` a building dbt
command left (``build``, ``test``, ``run``, ``seed``, ``snapshot``), in the
directories ``transform build`` writes and reads: the project's own target path
and Dex's isolated ``<target-path>/dex/<dbt-version>/``, each with the ``cold/``
directory a ``--cold-deps`` build writes beside it. A ``compile``, ``parse`` or
``ls`` writes ``run_results.json`` too, where ``success`` means compiled, so it
is never read as a test status.

What is attributed, and what is not:

- Only a test the current manifest still has, enabled, gets a status. A result
  for a test since removed or disabled is dropped, and a test the run never
  reached is absent, which the caller reports ``not_run``, never ``pass``.
- A run whose invocation the dbt log beside it does not name is not attributed
  at all: those results came from somewhere other than the runs this project
  logged (copied artifacts, another checkout's target path), so no status from
  it describes this project's tests.
- The run is always dated (``generated_at``, ``age_seconds``) and never called
  recent or current: one run is evidence about the moment it ran.

Files only. Nothing here opens a connection, and nothing from a result row but
its id and status is read: no message, no compiled SQL, no failing rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..transform.artifact_runtime import BuildingRun, building_runs, run_is_logged

#: The statuses a test result is reported with, in the order counts print them.
STATUSES: tuple[str, ...] = ("pass", "fail", "warn", "error", "skipped", "not_run")

#: dbt's own statuses, normalized to the ones above. ``runtime error`` is the
#: spelling older releases wrote for an error.
_STATUS_MAP = {
    "pass": "pass",
    "fail": "fail",
    "warn": "warn",
    "error": "error",
    "runtime error": "error",
    "skipped": "skipped",
}


@dataclass
class LastRun:
    """Each test's status in the newest building run, and that run's identity."""

    #: Test and unit test unique id to ``pass``, ``fail``, ``warn``, ``error`` or
    #: ``skipped``. A test absent here did not run in that invocation.
    statuses: dict[str, str] = field(default_factory=dict)
    #: ``invocation_id``, ``generated_at``, ``age_seconds``, ``command``,
    #: ``artifact`` (``dex`` or ``project``) and ``cold``; ``None`` with no run.
    run: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def counts(statuses_for_resource: Iterable[str]) -> dict[str, int]:
    """Counts by status, every key present, in :data:`STATUSES` order."""

    tally = dict.fromkeys(STATUSES, 0)
    for status in statuses_for_resource:
        if status in tally:
            tally[status] += 1
    return tally


def _parse_time(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _current_tests(manifest: dict[str, Any]) -> set[str]:
    """Enabled test and unit test ids in the manifest the caller inventories."""

    ids: set[str] = set()
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        if (node.get("config") or {}).get("enabled") is False:
            continue
        ids.add(str(unique_id))
    for unique_id, node in (manifest.get("unit_tests") or {}).items():
        if (
            isinstance(node, dict)
            and (node.get("config") or {}).get("enabled") is False
        ):
            continue
        ids.add(str(unique_id))
    return ids


def last_run(
    project_dir: Path, manifest: dict[str, Any], *, now: datetime | None = None
) -> LastRun:
    """The newest building run's status for each test ``manifest`` still has."""

    project_dir = Path(project_dir).absolute()
    found = building_runs(project_dir)
    if not found:
        return LastRun(
            notes=[
                "no run_results.json from a dbt build, test or run was found in "
                "the project's target path or Dex's artifact directory, so every "
                "test is not_run: nothing records that it ever ran"
            ]
        )
    newest: BuildingRun = found[-1]
    invocation = newest.invocation_id
    run: dict[str, Any] = {
        "invocation_id": invocation,
        "generated_at": newest.generated_at or None,
        "command": newest.command,
        "artifact": newest.artifact,
        "cold": newest.cold,
    }
    when = _parse_time(newest.generated_at)
    if when is not None:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        run["age_seconds"] = max(0, int((moment - when).total_seconds()))

    result = LastRun(run=run)
    where = (
        "Dex's artifact directory"
        if newest.artifact == "dex"
        else ("the project's target path")
    )
    if newest.cold:
        where += " (a --cold-deps build)"

    logged = run_is_logged(project_dir, newest)
    if logged is False:
        result.warnings.append(
            f"the run_results.json in {where} names invocation {invocation}, "
            "which no dbt log beside it records, so it was written somewhere "
            "else (copied artifacts, another checkout) and no test status is "
            "taken from it: every test is reported not_run"
        )
        return result
    if logged is None and invocation:
        result.notes.append(
            "no dbt log was found to confirm the invocation that wrote "
            "run_results.json; its statuses are attributed as read"
        )

    current = _current_tests(manifest)
    dropped = 0
    unrecognized = 0
    for row in newest.document.get("results") or []:
        if not isinstance(row, dict) or not row.get("unique_id"):
            continue
        unique_id = str(row["unique_id"])
        if not unique_id.startswith(("test.", "unit_test.")):
            continue
        if unique_id not in current:
            dropped += 1
            continue
        status = _STATUS_MAP.get(str(row.get("status") or "").strip().lower())
        if status is None:
            unrecognized += 1
            continue
        result.statuses[unique_id] = status

    age = run.get("age_seconds")
    result.notes.append(
        f"test statuses are read from one dbt {run['command']} invocation "
        f"({invocation or 'with no recorded id'}) in {where}, generated at "
        f"{run['generated_at'] or 'an unrecorded time'}"
        + (f", {age} second(s) before this read" if age is not None else "")
        + "; it describes that run only. A test it did not run is not_run"
    )
    if dropped:
        result.notes.append(
            f"{dropped} test result(s) in that run belong to tests the project "
            "no longer has, or has since disabled, and are not attributed"
        )
    if unrecognized:
        result.notes.append(
            f"{unrecognized} test result(s) carried a status dex does not "
            "recognize and are reported not_run"
        )
    return result
