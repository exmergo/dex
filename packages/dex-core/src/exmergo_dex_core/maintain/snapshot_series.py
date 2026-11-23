"""The committed ``maintain snapshot`` series, read from the repository (#1475).

``.dex/snapshot.json`` is committed like a lockfile, so every baseline anyone
vouched for is a version of that file in the repository's history. The series
is those versions, each with the commit that recorded it and the time its
baseline was captured. On a project's first day there is none, and diagnosis
says so rather than reading the absence as a clean history.

Read-only ``git``, as :class:`~.commit_history.GitHistory` runs it: one
argument list per call (no shell), a timeout, prompts off and the file-system
monitor off. A version that does not parse as a snapshot is skipped and
counted; the series is capped at :data:`MAX_SNAPSHOTS` versions, newest first.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .commit_history import GIT_TIMEOUT_SECONDS
from .diagnosis import Series, SnapshotPoint
from .snapshot import Snapshot

#: The most committed versions one diagnosis reads, newest first.
MAX_SNAPSHOTS = 50

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class SeriesRead:
    """The series as read, and the snapshot each point carries."""

    series: Series
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    skipped: int = 0
    elided: int = 0


def read_series(
    dex_dir: str | os.PathLike[str],
    *,
    limit: int = MAX_SNAPSHOTS,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> SeriesRead:
    """The committed versions of ``snapshot.json`` in ``dex_dir``.

    ``unreadable`` where ``git`` cannot answer (no repository, no ``git``);
    ``read`` with no points where the file was never committed.
    """

    root = Path(dex_dir)
    if not root.is_dir():
        return SeriesRead(
            Series("read", reason="no .dex directory, so no snapshot was committed")
        )
    head = _git(root, timeout, "rev-parse", "--verify", "--quiet", "HEAD")
    if head is not None and head.returncode == 1 and not head.stdout.strip():
        # A repository with no commit yet holds no committed snapshot either.
        repository = _git(root, timeout, "rev-parse", "--git-dir")
        if repository is not None and repository.returncode == 0:
            return SeriesRead(
                Series(
                    "read",
                    reason="the repository has no commit yet, so no maintain "
                    "snapshot baseline is committed",
                )
            )
    # A commit that deleted the file recorded no baseline, so it is not listed.
    listed = _git(
        root, timeout, "log", "--format=%H", "--diff-filter=d", "--", "snapshot.json"
    )
    if listed is None or listed.returncode != 0:
        return SeriesRead(
            Series(
                "unreadable",
                reason="the repository's history could not be read, so the "
                "committed snapshots are not known",
            )
        )
    commits = [c for c in listed.stdout.split() if _COMMIT.fullmatch(c)]
    elided = max(len(commits) - limit, 0)
    points: list[SnapshotPoint] = []
    snapshots: dict[str, Snapshot] = {}
    skipped = 0
    for commit in commits[:limit]:
        shown = _git(root, timeout, "show", f"{commit}:./snapshot.json")
        if shown is None or shown.returncode != 0:
            skipped += 1
            continue
        try:
            snapshot = Snapshot.model_validate_json(shown.stdout)
            created = datetime.fromisoformat(snapshot.created_at)
        except ValueError:
            skipped += 1
            continue
        points.append(SnapshotPoint(commit=commit, created_at=created))
        snapshots[commit] = snapshot
    if not points:
        reason = (
            "no maintain snapshot baseline is committed to the repository"
            if not commits
            else "no committed version of .dex/snapshot.json parses as a snapshot"
        )
        return SeriesRead(Series("read", reason=reason), skipped=skipped)
    points.reverse()
    points = _measured(points, snapshots)
    return SeriesRead(
        Series("read", points=tuple(points)),
        snapshots=snapshots,
        skipped=skipped,
        elided=elided,
    )


def _measured(
    points: list[SnapshotPoint], snapshots: dict[str, Snapshot]
) -> list[SnapshotPoint]:
    """Each point with the time its warehouse state was measured, oldest first.

    A warehouse block identical to the previous snapshot's was carried forward
    (``maintain snapshot --project-only``), so it keeps that snapshot's time. A
    block pinned from the exploration cache was measured when the cache was
    (``cache_updated_at``). Otherwise the snapshot measured it when written.
    """

    placed: list[SnapshotPoint] = []
    previous: Snapshot | None = None
    for point in points:
        snapshot = snapshots[point.commit]
        measured: datetime | None = None
        if (
            previous is not None
            and placed
            and snapshot.warehouse == previous.warehouse
            and snapshot.cache_updated_at == previous.cache_updated_at
        ):
            measured = placed[-1].placed_at()
        elif snapshot.warehouse_from == "cache" and snapshot.cache_updated_at:
            try:
                measured = datetime.fromisoformat(snapshot.cache_updated_at)
            except ValueError:
                measured = None
        placed.append(point.model_copy(update={"measured_at": measured}))
        previous = snapshot
    return placed


def _git(
    root: Path, timeout: float, *args: str
) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


__all__ = ["MAX_SNAPSHOTS", "SeriesRead", "read_series"]
