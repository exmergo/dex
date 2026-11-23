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

from ..errors import DexError
from .observations import COMMIT_PATTERN

#: Which kind of reader answered.
HistoryKind = Literal["git", "mapped"]

_COMMIT = re.compile(COMMIT_PATTERN)

#: Seconds one ``git`` call may take before the history reads as unreadable.
GIT_TIMEOUT_SECONDS = 20.0


class HistoryUnreadableError(DexError, RuntimeError):
    """The history could not answer, as opposed to a commit absent from it.

    ``code`` is ``unreadable`` (no repository, no ``git``, a timeout) or
    ``shallow``: a shallow clone cut the history the answer needs, so "not an
    ancestor" or a range crossing the clone's boundary cannot be trusted.
    :func:`~.timeline.commit_range` reads them as ``history_unreadable`` and
    ``history_shallow``; a host calling a reader directly catches it.
    """

    def __init__(
        self, message: str, *, code: Literal["unreadable", "shallow"] = "unreadable"
    ) -> None:
        super().__init__(message)
        self.code = code


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

    Answers to :meth:`resolve` and :meth:`is_ancestor` are kept for the life
    of the instance, since the history they read does not change under one
    read; build a new instance to read a checkout again. The first time
    ``git`` cannot be reached at all (no repository, no ``git``, a timeout),
    every later call, cached or not, fails the same way at once rather than
    waiting on ``git`` again. A single command that fails is reported for that
    call only.

    In a shallow clone a boundary commit has no recorded parents, so a
    negative ancestry answer, or a range that reaches the boundary, may be an
    artefact of the clone: both raise :class:`HistoryUnreadableError` with
    ``code="shallow"`` rather than read as diverged history or a short range.
    """

    kind: HistoryKind = "git"

    def __init__(
        self, repo_root: str | os.PathLike[str], *, timeout: float = GIT_TIMEOUT_SECONDS
    ) -> None:
        self._root = Path(repo_root)
        self._timeout = timeout
        self._checked = False
        self._boundary: frozenset[str] = frozenset()
        self._failure: HistoryUnreadableError | None = None
        self._resolved: dict[str, str | None] = {}
        self._ancestry: dict[tuple[str, str], bool] = {}

    def _fail(self, message: str) -> HistoryUnreadableError:
        self._failure = HistoryUnreadableError(message)
        return self._failure

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        self._raise_if_failed()
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
            raise self._fail(type(exc).__name__) from None

    def _repository(self) -> None:
        self._raise_if_failed()
        if self._checked:
            return
        done = self._git("rev-parse", "--git-dir", "--is-shallow-repository")
        if done.returncode != 0:
            raise self._fail("not a git repository")
        if done.stdout.split()[-1:] == ["true"]:
            self._boundary = self._shallow_boundary()
        self._checked = True

    def _shallow_boundary(self) -> frozenset[str]:
        """The commits a shallow clone cut the history at (``.git/shallow``)."""

        done = self._git("rev-parse", "--path-format=absolute", "--git-path", "shallow")
        if done.returncode != 0:
            raise self._fail("shallow boundary unreadable")
        try:
            text = Path(done.stdout.strip()).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise self._fail("shallow boundary unreadable") from None
        return frozenset(s.lower() for s in text.split() if _COMMIT.fullmatch(s))

    @property
    def shallow(self) -> bool:
        """Whether the checkout is a shallow clone."""

        self._repository()
        return bool(self._boundary)

    def resolve(self, commit: str) -> str | None:
        name = _held(commit)
        self._raise_if_failed()
        if name in self._resolved:
            return self._resolved[name]
        self._repository()
        done = self._git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        resolved = done.stdout.strip() if done.returncode == 0 else ""
        self._resolved[name] = resolved if _COMMIT.fullmatch(resolved) else None
        return self._resolved[name]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        older, newer = _held(ancestor), _held(descendant)
        self._raise_if_failed()
        held = self._ancestry.get((older, newer))
        if held is None:
            self._repository()
            done = self._git("merge-base", "--is-ancestor", older, newer)
            if done.returncode not in (0, 1):
                raise HistoryUnreadableError("merge-base failed")
            held = self._ancestry[(older, newer)] = done.returncode == 0
        if not held and self._boundary:
            raise HistoryUnreadableError(
                "a shallow clone cannot show a commit is not an ancestor",
                code="shallow",
            )
        return held

    def independent(self, commits: Sequence[str]) -> tuple[str, ...]:
        names = list(dict.fromkeys(_held(c) for c in commits))
        self._raise_if_failed()
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
        if count == 0:
            return (), count
        # In a shallow clone, list the whole range: a boundary commit inside
        # it means the clone cut the range short.
        listing = [] if self._boundary else [f"--max-count={max(limit, 0)}"]
        if limit <= 0 and not self._boundary:
            return (), count
        listed = self._git("rev-list", "--topo-order", *listing, *spec)
        if listed.returncode != 0:
            raise HistoryUnreadableError("rev-list failed")
        every = [s for s in listed.stdout.split() if _COMMIT.fullmatch(s)]
        if self._boundary.intersection(every):
            raise HistoryUnreadableError(
                "the range reaches a shallow clone's boundary", code="shallow"
            )
        shas = tuple(every[: max(limit, 0)])
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
