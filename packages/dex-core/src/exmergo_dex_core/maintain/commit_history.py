"""The repository's commit history, read to place a break between commits (#1467).

A coverage timeline (#1466) orders runs by invocation. A break sits between two
runs, and the commits those runs deployed are not always in the same order: a
rollback runs an older commit after a newer one, and two branches can deploy in
turn. The commits between two runs are therefore read from the repository's
history, never from the runs that happened in between.

:class:`CommitHistory` is the seam the timeline reads through. Two readers ship:

- :class:`GitHistory` asks ``git`` in a local checkout, read-only: it resolves
  an object name, tests ancestry, and lists the commits one commit has that a set
  of others does not (``git rev-list <bad> --not <good>...``, the candidate set
  ``git bisect`` would search).
- :class:`MappedHistory` holds a commit graph in memory, for a host with no
  checkout and for fixtures.

Every commit handed to a reader is held to
:data:`~.observations.COMMIT_PATTERN` first: commits are read from warehouse
rows, so one is never passed to ``git`` unless it is a hexadecimal object name.
A reader raises :class:`HistoryUnreadableError` when it cannot answer at all (no
repository, no ``git``, a timeout), which is not the same answer as "that
commit is not in the history".
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .observations import COMMIT_PATTERN

#: Which kind of reader answered.
HistoryKind = Literal["git", "mapped"]

_COMMIT = re.compile(COMMIT_PATTERN)

#: Seconds one ``git`` call may take before the history reads as unreadable.
GIT_TIMEOUT_SECONDS = 20.0


class HistoryUnreadableError(Exception):
    """The history could not be read at all (as opposed to a commit absent
    from it)."""


@runtime_checkable
class CommitHistory(Protocol):
    """What the timeline needs from a repository's history."""

    @property
    def kind(self) -> HistoryKind: ...

    def resolve(self, commit: str) -> str | None:
        """The full object name of ``commit``, or ``None`` where the history
        does not hold it (or an abbreviation is ambiguous)."""

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ``ancestor`` is reachable from ``descendant``; a commit is
        its own ancestor, as ``git merge-base --is-ancestor`` reads it. Both
        are full names :meth:`resolve` returned."""

    def independent(self, commits: Sequence[str]) -> tuple[str, ...]:
        """The commits none of the others is a descendant of."""

    def between(
        self, bad: str, goods: Sequence[str], limit: int
    ) -> tuple[tuple[str, ...], int]:
        """The commits reachable from ``bad`` and from none of ``goods``,
        newest first and at most ``limit`` of them, and how many there are."""


def _held(commit: str) -> str:
    if not _COMMIT.fullmatch(commit):
        raise ValueError("a commit must be a hexadecimal object name")
    return commit.lower()


class GitHistory:
    """The history of the ``git`` checkout at ``repo_root``, read-only.

    Each answer is one ``git`` call with an argument list (never a shell), a
    timeout, prompts off and the file-system monitor off, so no repository
    hook or configured command runs.
    """

    kind: HistoryKind = "git"

    def __init__(
        self, repo_root: str | os.PathLike[str], *, timeout: float = GIT_TIMEOUT_SECONDS
    ) -> None:
        self._root = Path(repo_root)
        self._timeout = timeout
        self._checked = False

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            return subprocess.run(  # noqa: S603
                ["git", "-C", str(self._root), "-c", "core.fsmonitor=false", *args],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise HistoryUnreadableError(type(exc).__name__) from None

    def _repository(self) -> None:
        if self._checked:
            return
        done = self._git("rev-parse", "--git-dir")
        if done.returncode != 0:
            raise HistoryUnreadableError("not a git repository")
        self._checked = True

    def resolve(self, commit: str) -> str | None:
        name = _held(commit)
        self._repository()
        done = self._git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if done.returncode != 0:
            return None
        resolved = done.stdout.strip()
        return resolved if _COMMIT.fullmatch(resolved) else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        older, newer = _held(ancestor), _held(descendant)
        self._repository()
        done = self._git("merge-base", "--is-ancestor", older, newer)
        if done.returncode in (0, 1):
            return done.returncode == 0
        raise HistoryUnreadableError("merge-base failed")

    def independent(self, commits: Sequence[str]) -> tuple[str, ...]:
        names = list(dict.fromkeys(_held(c) for c in commits))
        if len(names) <= 1:
            return tuple(names)
        self._repository()
        done = self._git("merge-base", "--independent", *names)
        if done.returncode != 0:
            raise HistoryUnreadableError("merge-base failed")
        kept = set(done.stdout.split())
        return tuple(n for n in names if n in kept)

    def between(
        self, bad: str, goods: Sequence[str], limit: int
    ) -> tuple[tuple[str, ...], int]:
        head = _held(bad)
        excluded = [_held(g) for g in goods]
        self._repository()
        spec = [head, "--not", *excluded] if excluded else [head]
        counted = self._git("rev-list", "--count", *spec)
        if counted.returncode != 0:
            raise HistoryUnreadableError("rev-list failed")
        try:
            count = int(counted.stdout.strip() or "0")
        except ValueError:
            raise HistoryUnreadableError("rev-list failed") from None
        if limit <= 0 or count == 0:
            return (), count
        listed = self._git("rev-list", "--topo-order", f"--max-count={limit}", *spec)
        if listed.returncode != 0:
            raise HistoryUnreadableError("rev-list failed")
        shas = tuple(s for s in listed.stdout.split() if _COMMIT.fullmatch(s))
        return shas, count


class MappedHistory:
    """A commit graph held in memory: each commit's parents.

    Names are full object names; :meth:`resolve` also takes an unambiguous
    prefix, as ``git`` does. A parent the mapping does not list as a commit is
    treated as a commit with no parents.
    """

    kind: HistoryKind = "mapped"

    def __init__(self, parents: Mapping[str, Iterable[str]]) -> None:
        graph: dict[str, tuple[str, ...]] = {}
        for commit, its_parents in parents.items():
            graph[_held(commit)] = tuple(_held(p) for p in its_parents)
        for its_parents in list(graph.values()):
            for parent in its_parents:
                graph.setdefault(parent, ())
        self._parents = graph

    def resolve(self, commit: str) -> str | None:
        name = _held(commit)
        if name in self._parents:
            return name
        matches = [c for c in self._parents if c.startswith(name)]
        return matches[0] if len(matches) == 1 else None

    def _ancestors(self, commit: str) -> set[str]:
        seen: set[str] = set()
        stack = [commit]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._parents.get(current, ()))
        return seen

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return _held(ancestor) in self._ancestors(_held(descendant))

    def independent(self, commits: Sequence[str]) -> tuple[str, ...]:
        names = list(dict.fromkeys(_held(c) for c in commits))
        return tuple(
            n
            for n in names
            if not any(other != n and n in self._ancestors(other) for other in names)
        )

    def between(
        self, bad: str, goods: Sequence[str], limit: int
    ) -> tuple[tuple[str, ...], int]:
        reachable = self._ancestors(_held(bad))
        for good in goods:
            reachable -= self._ancestors(_held(good))
        # Children before parents, as ``git rev-list --topo-order`` lists them.
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(commit: str) -> None:
            stack: list[tuple[str, bool]] = [(commit, False)]
            while stack:
                current, done = stack.pop()
                if done:
                    ordered.append(current)
                    continue
                if current in visited or current not in reachable:
                    continue
                visited.add(current)
                stack.append((current, True))
                stack.extend(
                    (parent, False)
                    for parent in reversed(self._parents.get(current, ()))
                )

        visit(_held(bad))
        ordered.reverse()
        return tuple(ordered[: max(limit, 0)]), len(reachable)


__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "CommitHistory",
    "GitHistory",
    "HistoryKind",
    "HistoryUnreadableError",
    "MappedHistory",
]
