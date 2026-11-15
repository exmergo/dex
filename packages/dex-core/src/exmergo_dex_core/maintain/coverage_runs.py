"""The last recorded status of each test and source freshness check, for
``maintain coverage`` (#1285, #1347).

A test that exists may never have run, may have been skipped, or may have
failed last time, so an inventory of configuration alone reads "has a test" as
"is protected". This reads the newest ``run_results.json`` a building dbt
command left (``build``, ``test``, ``run``, ``seed``, ``snapshot``) and, apart
from it, the newest ``sources.json`` a ``dbt source freshness`` run left, in
the directories ``transform build`` writes and reads: the project's own target
path and Dex's isolated ``<target-path>/dex/<dbt-version>/``, each with the
``cold/`` directory a ``--cold-deps`` build writes beside it. A ``compile``,
``parse`` or ``ls`` writes ``run_results.json`` too, where ``success`` means
compiled, so it is never read as a test status.

What is attributed, and what is not:

- Only a test or source the current manifest still has, enabled, gets a status.
  A result for a test or source since removed or disabled is dropped, and one
  the run never reached is absent, which the caller reports ``not_run``, never
  ``pass``.
- A run whose invocation the dbt log beside it does not name is not attributed
  at all: those results came from somewhere other than the runs this project
  logged (copied artifacts, another checkout's target path), so no status from
  it describes this project.
- Each run is always dated (``generated_at``, ``age_seconds``) and never called
  recent or current: one run is evidence about the moment it ran.

Files only. Nothing here opens a connection, and nothing from a result row but
its id and status is read: no message, no ``max_loaded_at``, no compiled SQL,
no failing rows.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..transform.artifact_runtime import isolated_artifact_dir, shared_target_dir

#: The statuses a test result is reported with, in the order counts print them.
STATUSES: tuple[str, ...] = ("pass", "fail", "warn", "error", "skipped", "not_run")

#: The statuses a source freshness result is reported with (#1347).
FRESHNESS_STATUSES: tuple[str, ...] = ("pass", "warn", "error", "not_run")

#: dbt commands that execute tests. Anything else that writes run_results.json
#: (a compile, which Dex runs to price a build) says nothing about a test.
_BUILDING_COMMANDS = frozenset({"build", "run", "seed", "snapshot", "test"})

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

#: dbt's ``sources.json`` statuses, normalized (#1347). ``runtime error`` is the
#: status dbt writes when the freshness query itself failed to execute.
_FRESHNESS_STATUS_MAP = {
    "pass": "pass",
    "warn": "warn",
    "error": "error",
    "runtime error": "error",
}

#: The header line dbt's text log writes at the start of each invocation:
#: ``============================== 10:00:00.000 | <uuid> ===...``.
_TEXT_HEADER = re.compile(r"=+ .*\| ([0-9a-f-]{36}) =+")
_JSON_INVOCATION = re.compile(r'"invocation_id"\s*:\s*"([0-9a-f-]{36})"')


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


@dataclass
class _Candidate:
    document: dict[str, Any]
    generated_at: str
    mtime: float
    directory: Path
    artifact: str
    cold: bool


def _candidates(
    project_dir: Path,
    filename: str = "run_results.json",
    *,
    building_only: bool = True,
) -> list[_Candidate]:
    roots: list[tuple[Path, str]] = [(shared_target_dir(project_dir), "project")]
    isolated = isolated_artifact_dir(project_dir)
    if isolated is not None:
        roots.append((isolated, "dex"))
    found: list[_Candidate] = []
    for root, artifact in roots:
        for directory, cold in ((root, False), (root / "cold", True)):
            path = directory / filename
            try:
                mtime = path.stat().st_mtime
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            if building_only:
                which = str((document.get("args") or {}).get("which") or "build")
                if which not in _BUILDING_COMMANDS:
                    continue
            meta = document.get("metadata") or {}
            found.append(
                _Candidate(
                    document=document,
                    generated_at=str(meta.get("generated_at") or ""),
                    mtime=mtime,
                    directory=directory,
                    artifact=artifact,
                    cold=cold,
                )
            )
    return found


def _log_files(project_dir: Path, directory: Path) -> list[Path]:
    """The dbt logs that could record the invocation that wrote ``directory``.

    A build Dex runs in the project writes the project's log (``logs/`` under
    the project, or ``DBT_LOG_PATH``); one it runs detached, in a copy, writes
    ``logs/`` beside its artifacts.
    """

    paths = [directory / "logs" / "dbt.log"]
    configured = os.environ.get("DBT_LOG_PATH")
    if configured:
        base = Path(configured).expanduser()
        paths.append((base if base.is_absolute() else project_dir / base) / "dbt.log")
    paths.append(project_dir / "logs" / "dbt.log")
    # dbt rotates its log, so an older invocation can sit in dbt.log.1 and on.
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        found.extend(sorted(path.parent.glob("dbt.log.*")))
    return list(dict.fromkeys(found))


def _logged_invocations(path: Path) -> set[str]:
    found: set[str] = set()
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "invocation_id" in line:
                    found.update(_JSON_INVOCATION.findall(line))
                elif line.startswith("="):
                    match = _TEXT_HEADER.search(line)
                    if match:
                        found.add(match.group(1))
    except OSError:
        return set()
    return found


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


def _current_sources(manifest: dict[str, Any]) -> set[str]:
    """Enabled source ids in the manifest the caller inventories."""

    ids: set[str] = set()
    for unique_id, node in (manifest.get("sources") or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in (None, "source"):
            continue
        if (node.get("config") or {}).get("enabled") is False:
            continue
        ids.add(str(unique_id))
    return ids


def last_run(
    project_dir: Path, manifest: dict[str, Any], *, now: datetime | None = None
) -> LastRun:
    """The newest building run's status for each test ``manifest`` still has."""

    project_dir = Path(project_dir).absolute()
    found = _candidates(project_dir)
    if not found:
        return LastRun(
            notes=[
                "no run_results.json from a dbt build, test or run was found in "
                "the project's target path or Dex's artifact directory, so every "
                "test is not_run: nothing records that it ever ran"
            ]
        )
    # ISO timestamps sort as text; a run with none sorts first, then by mtime.
    newest = max(found, key=lambda run: (run.generated_at, run.mtime))
    meta = newest.document.get("metadata") or {}
    invocation = str(meta["invocation_id"]) if meta.get("invocation_id") else None
    run: dict[str, Any] = {
        "invocation_id": invocation,
        "generated_at": newest.generated_at or None,
        "command": str((newest.document.get("args") or {}).get("which") or "build"),
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

    logs = _log_files(project_dir, newest.directory)
    if invocation and logs:
        logged: set[str] = set()
        for path in logs:
            logged |= _logged_invocations(path)
        if invocation not in logged:
            result.warnings.append(
                f"the run_results.json in {where} names invocation {invocation}, "
                "which no dbt log beside it records, so it was written somewhere "
                "else (copied artifacts, another checkout) and no test status is "
                "taken from it: every test is reported not_run"
            )
            return result
    elif invocation and not logs:
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


def freshness_run(
    project_dir: Path, manifest: dict[str, Any], *, now: datetime | None = None
) -> LastRun:
    """The newest ``sources.json`` status for each source ``manifest`` still has."""

    project_dir = Path(project_dir).absolute()
    found = _candidates(project_dir, "sources.json", building_only=False)
    if not found:
        return LastRun(
            notes=[
                "no sources.json from `dbt source freshness` was found in "
                "the project's target path or Dex's artifact directory, so "
                "every source freshness check is not_run: nothing records "
                "that it ever ran"
            ]
        )
    # ISO timestamps sort as text; a run with none sorts first, then by mtime.
    newest = max(found, key=lambda run: (run.generated_at, run.mtime))
    meta = newest.document.get("metadata") or {}
    invocation = str(meta["invocation_id"]) if meta.get("invocation_id") else None
    run: dict[str, Any] = {
        "invocation_id": invocation,
        "generated_at": newest.generated_at or None,
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
        else "the project's target path"
    )
    if newest.cold:
        where += " (a --cold-deps build)"

    logs = _log_files(project_dir, newest.directory)
    if invocation and logs:
        logged: set[str] = set()
        for path in logs:
            logged |= _logged_invocations(path)
        if invocation not in logged:
            result.warnings.append(
                f"the sources.json in {where} names invocation {invocation}, "
                "which no dbt log beside it records, so it was written somewhere "
                "else (copied artifacts, another checkout) and no freshness "
                "status is taken from it: every source freshness check is "
                "reported not_run"
            )
            return result
    elif invocation and not logs:
        result.notes.append(
            "no dbt log was found to confirm the invocation that wrote "
            "sources.json; its statuses are attributed as read"
        )

    current = _current_sources(manifest)
    dropped = 0
    unrecognized = 0
    for row in newest.document.get("results") or []:
        if not isinstance(row, dict) or not row.get("unique_id"):
            continue
        unique_id = str(row["unique_id"])
        if not unique_id.startswith("source."):
            continue
        if unique_id not in current:
            dropped += 1
            continue
        status = _FRESHNESS_STATUS_MAP.get(str(row.get("status") or "").strip().lower())
        if status is None:
            unrecognized += 1
            continue
        result.statuses[unique_id] = status

    age = run.get("age_seconds")
    result.notes.append(
        "source freshness statuses are read from one `dbt source freshness` "
        f"invocation ({invocation or 'with no recorded id'}) in {where}, "
        f"generated at {run['generated_at'] or 'an unrecorded time'}"
        + (f", {age} second(s) before this read" if age is not None else "")
        + "; it describes that run only. A source it did not check is not_run"
    )
    if dropped:
        result.notes.append(
            f"{dropped} freshness result(s) in that run belong to sources the "
            "project no longer has, or has since disabled, and are not "
            "attributed"
        )
    if unrecognized:
        result.notes.append(
            f"{unrecognized} freshness result(s) carried a status dex does not "
            "recognize and are reported not_run"
        )
    return result
