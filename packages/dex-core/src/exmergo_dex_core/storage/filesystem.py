"""Plain files under `.dex/` in the user's repo: the CLI's backend.

Layout (all non-secret, committed to the user's repo):

    .dex/config.yml     non-secret config (see config.py; not a store document)
    .dex/cache.json     the current DexCache (exploration artifacts)
    .dex/snapshot.json  the maintain baseline (see maintain/snapshot.py)
    .dex/drift.json     the last drift-detection report (see maintain/drift.py)
    .dex/queries.jsonl  the append-only `explore query` decision ledger
    .dex/spend.jsonl    the append-only billed-command ledger
    .dex/spend.lock     an empty file; the spend-admission lock is held on it
    .dex/plans/*.json   the stored transform plans

State on disk is what lets the CLI subcommands stay stateless: the agent
orchestrates multi-step flows by re-reading the cache and the dbt project between
process invocations, and the session budget survives across commands because the
spend ledger does. Both properties are why this, not
:class:`~.memory.MemoryStore`, is the CLI default.

This module is the only place in the engine that knows these file names.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..cache import DexCache
from ..errors import ConfigurationError
from .base import Document, StoreContext, spend_total

if TYPE_CHECKING:
    from ..maintain.drift import DriftReport
    from ..maintain.snapshot import Snapshot
    from ..transform.plans import EditKind, TransformPlan

DEX_DIR = ".dex"
CACHE_FILE = "cache.json"
SNAPSHOT_FILE = "snapshot.json"
DRIFT_FILE = "drift.json"
QUERIES_FILE = "queries.jsonl"
SPEND_FILE = "spend.jsonl"
SPEND_LOCK_FILE = "spend.lock"
PLANS_DIR = "plans"

# One re-entrant lock per resolved `.dex/` path, shared by every store instance
# addressing it. The OS lock below is what serializes separate processes; this is
# what serializes threads inside one, and it is not redundant with it: a POSIX
# `flock` is held by the open file description, so two threads opening the same
# lock file each get their own and neither waits for the other.
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(key: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


# Re-entrancy is tracked per thread rather than left to the RLock, because the OS
# lock is not re-entrant the way the RLock is: a second `flock` from the same
# process on a second descriptor for the same file blocks forever. So a nested
# acquisition takes the fast path and touches no descriptor at all.
_HELD = threading.local()


def _held_depths() -> dict[str, int]:
    depths = getattr(_HELD, "depths", None)
    if depths is None:
        depths = {}
        _HELD.depths = depths
    return depths


try:  # POSIX
    import fcntl

    def _lock_descriptor(handle) -> bool:
        """True when the exclusive lock was taken, False when someone holds it."""

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock_descriptor(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _lock_descriptor(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock_descriptor(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


_DOCUMENT_FILES = {
    Document.CACHE: CACHE_FILE,
    Document.SNAPSHOT: SNAPSHOT_FILE,
    Document.DRIFT: DRIFT_FILE,
    Document.QUERY_LOG: QUERIES_FILE,
    Document.SPEND_LOG: SPEND_FILE,
}


class FilesystemStore:
    """Reads and writes the `.dex/` state for a given repo root."""

    def __init__(self, repo_root: Path | str = "."):
        self.root = Path(repo_root)
        self.dex_dir = self.root / DEX_DIR
        self.plans_dir = self.dex_dir / PLANS_DIR

    @classmethod
    def from_context(cls, context: StoreContext) -> FilesystemStore:
        """Build from a :class:`~.base.StoreContext`: the path-shaped reference
        implementation of the construction contract.

        Both refusals below are refusals rather than quiet fallbacks, the same
        rule the connection seam applies: accepted-and-ignored is strictly worse
        than rejected, because the caller believes a setting took effect and
        nothing in the output says otherwise. Defaulting a missing root to the
        working directory would be the sharpest version of that, since it would
        write one project's exploration cache into whichever directory the
        process happened to start in.
        """

        if context.repo_root is None:
            raise ConfigurationError(
                "the filesystem store keeps state in a `.dex/` directory and so "
                "needs a repo root, and this context carries none. Point dex at a "
                "project (DexEngine.from_repo(repo_root), or the CLI's "
                "--repo-root), or select a backend that does not need one"
            )
        if context.options:
            raise ConfigurationError(
                "the filesystem store takes no options and this context carries "
                f"{', '.join(sorted(context.options))}. Its only input is the repo "
                "root; an option it silently ignored would look like a setting "
                "that took effect"
            )
        return cls(context.repo_root)

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        path = self.dex_dir / CACHE_FILE
        if not path.is_file():
            return None
        return DexCache.model_validate_json(path.read_text(encoding="utf-8"))

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        return self._write_document(Document.CACHE, cache.model_dump_json(indent=2))

    def load_snapshot(self) -> Snapshot | None:
        from ..maintain.snapshot import Snapshot

        path = self.dex_dir / SNAPSHOT_FILE
        if not path.is_file():
            return None
        return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def save_snapshot(self, snapshot: Snapshot) -> str:
        return self._write_document(
            Document.SNAPSHOT, snapshot.model_dump_json(indent=2)
        )

    def load_drift(self) -> DriftReport | None:
        from ..maintain.drift import DriftReport

        path = self.dex_dir / DRIFT_FILE
        if not path.is_file():
            return None
        return DriftReport.model_validate_json(path.read_text(encoding="utf-8"))

    def save_drift(self, report: DriftReport) -> str:
        return self._write_document(Document.DRIFT, report.model_dump_json(indent=2))

    # --- ledgers --------------------------------------------------------------

    def append_query_log(self, entry: dict) -> None:
        self._append(Document.QUERY_LOG, entry)

    def append_spend_log(self, entry: dict) -> None:
        self._append(Document.SPEND_LOG, entry)

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        path = self.dex_dir / SPEND_FILE
        if not path.is_file():
            return 0.0
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return spend_total(entries, cutoff_iso, field=field, connector=connector)

    def spend_entries(
        self,
        *,
        connector: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """The tail of the ledger, oldest first (see :class:`SpendHistory`).

        The file is read whole, as ``spend_since`` already reads it whole, and
        the tail is taken after filtering rather than before: slicing the raw
        lines first would return fewer than ``limit`` of the requested
        connector's entries whenever another connector shares the ledger, which
        is exactly the multi-connector project where the per-connector scoping
        matters most.
        """

        path = self.dex_dir / SPEND_FILE
        if not path.is_file():
            return []
        entries: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if connector is not None and entry.get("connector") != connector:
                continue
            entries.append(entry)
        return entries[-limit:] if limit > 0 else []

    @contextmanager
    def spend_lock(self, *, timeout: float = 30.0) -> Iterator[None]:
        """Serialize the spend admission across every process holding this root.

        Two locks, because the CLI runs one command per process while a host can
        run several engines in one. An advisory `flock` on `.dex/spend.lock`
        keeps processes apart; a per-path re-entrant lock keeps threads apart,
        and it is genuinely needed rather than belt-and-braces, since a POSIX
        lock belongs to the open file description and two threads opening the
        same file would each get their own and neither would wait.

        The lock file is created and never removed. Unlinking it is what would
        break this: the lock is on the inode, so a holder and a waiter that
        opened it either side of a delete would hold two different files and both
        proceed. An empty file per repo is cheaper than that failure.
        """

        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / SPEND_LOCK_FILE
        key = str(path)
        depths = _held_depths()
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        deadline = time.monotonic() + timeout
        if not _process_lock(key).acquire(timeout=max(timeout, 0.0)):
            raise TimeoutError(
                f"waited {timeout}s for the spend lock at {path} and another "
                "thread still holds it"
            )
        try:
            with path.open("a", encoding="utf-8") as handle:
                while not _lock_descriptor(handle):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"waited {timeout}s for the spend lock at {path} and "
                            "another process still holds it"
                        )
                    time.sleep(0.01)
                depths[key] = 1
                try:
                    yield
                finally:
                    depths[key] = 0
                    _unlock_descriptor(handle)
        finally:
            _process_lock(key).release()

    # --- plans ----------------------------------------------------------------

    def save_plan(self, plan: TransformPlan) -> str:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        path = self.plans_dir / f"{plan.plan_id}.json"
        path.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return str(path)

    def load_plan(self, plan_id: str) -> TransformPlan:
        from ..transform.plans import PlanNotFoundError, TransformPlan

        path = self.plans_dir / f"{plan_id}.json"
        if not path.is_file():
            raise PlanNotFoundError(
                f"no plan '{plan_id}' under {self.plans_dir}; run `transform plan` "
                "first or check the id"
            )
        return TransformPlan.model_validate_json(path.read_text(encoding="utf-8"))

    def list_plans(self) -> list[TransformPlan]:
        plans = self._read_plans()
        return sorted(plans, key=lambda p: p.created_at, reverse=True)

    def latest_plan(self, kind: EditKind | None = None) -> TransformPlan | None:
        candidates = [
            plan
            for plan in self._read_plans()
            if plan.applied_at is None
            and (kind is None or all(e.kind is kind for e in plan.edits))
        ]
        return max(candidates, key=lambda p: p.created_at, default=None)

    def _read_plans(self) -> list[TransformPlan]:
        from ..transform.plans import TransformPlan

        if not self.plans_dir.is_dir():
            return []
        return [
            TransformPlan.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.plans_dir.glob("*.json")
        ]

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        return str(self.dex_dir / _DOCUMENT_FILES[document])

    def plan_locator(self, plan_id: str) -> str:
        return str(self.plans_dir / f"{plan_id}.json")

    # --- shared write plumbing ------------------------------------------------

    def _write_document(self, document: Document, body: str) -> str:
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / _DOCUMENT_FILES[document]
        path.write_text(body + "\n", encoding="utf-8")
        return str(path)

    def _append(self, document: Document, entry: dict) -> None:
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / _DOCUMENT_FILES[document]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
