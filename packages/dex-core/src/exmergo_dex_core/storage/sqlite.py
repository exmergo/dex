"""A single `.dex/dex.db` file: the opt-in durable backend.

:class:`~.filesystem.FilesystemStore` is right for the CLI because persistence is
git: a loose JSON file diffs cleanly in a pull request. Some hosts do not want
that trade. A long-running local session, or a library caller that wants state to
survive a process restart without writing anything the user has to `git add` or
`.gitignore`, wants one file it can point a backup or a `.gitignore` line at and
forget.

This backend trades the reviewable-diff property for that: everything lands in
one SQLite file, `.dex/dex.db`, with real tables and an index on the spend
ledger's `at`/`connector` columns, so :meth:`spend_since` is a bounded query
rather than a scan of every line ever appended. Nothing here is committed to the
user's repo the way `.dex/cache.json` is; `.dex/dex.db` (and the `-journal` file
that appears mid-write) belong in `.gitignore`, the way any local database file
does.

Selected the way :class:`~.filesystem.FilesystemStore` is, by naming it in
`.dex/config.yml` (`cache.backend: sqlite`) or on the command line
(`--cache-backend sqlite`); `filesystem` stays the default. See
``references/storage.md``.
"""

from __future__ import annotations

import json
import sqlite3
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
DB_FILE = "dex.db"

# This backend's own table-schema version, tracked via SQLite's `user_version`
# pragma. Deliberately unrelated to `cache.py`'s `CACHE_SCHEMA_VERSION`: that one
# versions the `DexCache` document the engine reads out of any backend, and
# `base.py` is explicit that a store never polices it. This one versions the
# *tables* below, and it is this module's alone to read and migrate. There is no
# migration to write yet because 1 is the only version that has ever shipped; the
# branch below is where the next one lands.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    kind TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    connector TEXT,
    entry TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_log_at ON spend_log(at);
CREATE INDEX IF NOT EXISTS idx_spend_log_connector_at ON spend_log(connector, at);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_applied_created ON plans(applied_at, created_at);
"""

# One re-entrant lock per resolved database path, mirroring FilesystemStore's
# `_PROCESS_LOCKS`: it is what serializes threads in this process wanting the
# spend lock, kept separate from the cross-process mechanism below because a
# `BEGIN IMMEDIATE` on one connection says nothing about a second connection on
# the same thread trying the same thing.
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()

# Re-entrancy for one holder is tracked here rather than left to the RLock alone,
# for the same reason FilesystemStore tracks it: the *cross-process* half of the
# lock is a held SQLite transaction, and a second `BEGIN IMMEDIATE` from the same
# connection on the same thread would not be a nested acquisition, it would be an
# error. A nested call takes the fast path below and never touches the connection
# at all.
_HELD = threading.local()


def _process_lock(key: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _held_depths() -> dict[str, int]:
    depths = getattr(_HELD, "depths", None)
    if depths is None:
        depths = {}
        _HELD.depths = depths
    return depths


class SqliteStore:
    """Reads and writes `.dex/dex.db` for a given repo root."""

    def __init__(self, repo_root: Path | str = "."):
        self.root = Path(repo_root)
        self.dex_dir = self.root / DEX_DIR
        self.path = self.dex_dir / DB_FILE
        self._conn: sqlite3.Connection | None = None
        # Guards every use of `_conn`: the sqlite3 module documents a connection
        # as safe to share across threads only if the caller serializes access to
        # it, which this backend is (a host can call any store method from any
        # thread, the same as the other two backends allow).
        self._conn_lock = threading.Lock()
        # A second, dedicated connection to the same file, opened only inside
        # `spend_lock`. Kept apart from `_conn` so holding the lock's write
        # transaction open across the caller's block never blocks an unrelated
        # `load_cache`/`save_cache` on `_conn` from another thread; the lock's
        # scope is the ledger, not the whole store, the same rule
        # FilesystemStore's separate `spend.lock` file follows.
        self._lock_conn: sqlite3.Connection | None = None

    @classmethod
    def from_context(cls, context: StoreContext) -> SqliteStore:
        """Build from a :class:`~.base.StoreContext`.

        Same two refusals as :meth:`~.filesystem.FilesystemStore.from_context`,
        for the same reason: accepted-and-ignored is worse than rejected.
        """

        if context.repo_root is None:
            raise ConfigurationError(
                "the sqlite store keeps state in a single `.dex/dex.db` file and "
                "so needs a repo root, and this context carries none. Point dex "
                "at a project (DexEngine.from_repo(repo_root), or the CLI's "
                "--repo-root), or select a backend that does not need one"
            )
        if context.options:
            raise ConfigurationError(
                "the sqlite store takes no options and this context carries "
                f"{', '.join(sorted(context.options))}. Its only input is the "
                "repo root; an option it silently ignored would look like a "
                "setting that took effect"
            )
        return cls(context.repo_root)

    # --- connection -----------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """The shared connection, opening and migrating the file on first use.

        Nothing touches disk until the first document, ledger, or plan
        operation: `locator()` answers from `self.path` alone, and a store built
        but never used leaves no `.dex/` directory, the same promise
        FilesystemStore makes.
        """

        if self._conn is None:
            self.dex_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                conn.commit()
            elif version > _SCHEMA_VERSION:
                conn.close()
                raise ConfigurationError(
                    f"{self.path} was written by a newer dex (schema version "
                    f"{version}) than this one knows how to read (version "
                    f"{_SCHEMA_VERSION}). Upgrade dex to open it, or point "
                    "cache.backend at a fresh sqlite file"
                )
            # version == _SCHEMA_VERSION: already current. A version between 1
            # and _SCHEMA_VERSION - 1 would migrate here; none exist yet, because
            # 1 is the only version that has ever shipped.
            self._conn = conn
        return self._conn

    def _lock_connection(self) -> sqlite3.Connection:
        if self._lock_conn is None:
            self.dex_dir.mkdir(parents=True, exist_ok=True)
            self._lock_conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
        return self._lock_conn

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        body = self._load_document(Document.CACHE)
        return None if body is None else DexCache.model_validate_json(body)

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        return self._save_document(Document.CACHE, cache.model_dump_json())

    def load_snapshot(self) -> Snapshot | None:
        from ..maintain.snapshot import Snapshot

        body = self._load_document(Document.SNAPSHOT)
        return None if body is None else Snapshot.model_validate_json(body)

    def save_snapshot(self, snapshot: Snapshot) -> str:
        return self._save_document(Document.SNAPSHOT, snapshot.model_dump_json())

    def load_drift(self) -> DriftReport | None:
        from ..maintain.drift import DriftReport

        body = self._load_document(Document.DRIFT)
        return None if body is None else DriftReport.model_validate_json(body)

    def save_drift(self, report: DriftReport) -> str:
        return self._save_document(Document.DRIFT, report.model_dump_json())

    def _load_document(self, document: Document) -> str | None:
        with self._conn_lock:
            row = (
                self._connection()
                .execute("SELECT body FROM documents WHERE kind = ?", (document.value,))
                .fetchone()
            )
        return None if row is None else row[0]

    def _save_document(self, document: Document, body: str) -> str:
        with self._conn_lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO documents(kind, body) VALUES (?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET body = excluded.body",
                (document.value, body),
            )
            conn.commit()
        return self.locator(document)

    # --- ledgers --------------------------------------------------------------

    def append_query_log(self, entry: dict) -> None:
        with self._conn_lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO query_log(entry) VALUES (?)", (json.dumps(entry),)
            )
            conn.commit()

    def append_spend_log(self, entry: dict) -> None:
        at = entry.get("at")
        connector = entry.get("connector")
        with self._conn_lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO spend_log(at, connector, entry) VALUES (?, ?, ?)",
                (
                    at if isinstance(at, str) else None,
                    connector if isinstance(connector, str) else None,
                    json.dumps(entry),
                ),
            )
            conn.commit()

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        # The index on (connector, at) and on (at) alone lets SQLite narrow to the
        # qualifying rows directly rather than reading every entry ever appended,
        # which is the whole point: FilesystemStore's equivalent reads and
        # json.loads()s the entire ledger file on every call. The final
        # filter-and-sum still goes through spend_total, in Python, so a
        # malformed field value or a stamp SQL cannot interpret is skipped the
        # same way every backend skips it, rather than one bad row failing the
        # whole query.
        query = (
            "SELECT entry FROM spend_log WHERE at >= ? AND (? IS NULL OR connector = ?)"
        )
        with self._conn_lock:
            rows = (
                self._connection()
                .execute(query, (cutoff_iso, connector, connector))
                .fetchall()
            )
        entries = []
        for (raw,) in rows:
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return spend_total(entries, cutoff_iso, field=field, connector=connector)

    @contextmanager
    def spend_lock(self, *, timeout: float = 30.0) -> Iterator[None]:
        """Serialize the spend admission across every process holding this file.

        The cross-process half is a `BEGIN IMMEDIATE` transaction on a dedicated
        connection, held open for the duration of the caller's block: SQLite
        grants that at most one connection to a file at a time, so a second
        process's `BEGIN IMMEDIATE` on the same file blocks (up to
        `busy_timeout`) until this one commits. The in-process half is the same
        `_process_lock`/depth-tracking pattern FilesystemStore uses, needed for
        the same reason: the OS-level primitive underneath is not itself
        re-entrant, so a nested acquisition on one thread has to take a fast path
        that never touches the connection at all.
        """

        key = str(self.path)
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
                f"waited {timeout}s for the spend lock on {self.path} and "
                "another thread still holds it"
            )
        try:
            conn = self._lock_connection()
            remaining = max(deadline - time.monotonic(), 0.0)
            conn.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise TimeoutError(
                    f"waited {timeout}s for the spend lock on {self.path} and "
                    "another process still holds it"
                ) from exc
            depths[key] = 1
            try:
                yield
            finally:
                depths[key] = 0
                conn.execute("COMMIT")
        finally:
            _process_lock(key).release()

    # --- plans ----------------------------------------------------------------

    def save_plan(self, plan: TransformPlan) -> str:
        with self._conn_lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO plans(plan_id, created_at, applied_at, body) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET "
                "created_at = excluded.created_at, "
                "applied_at = excluded.applied_at, "
                "body = excluded.body",
                (
                    plan.plan_id,
                    plan.created_at,
                    plan.applied_at,
                    plan.model_dump_json(),
                ),
            )
            conn.commit()
        return self.plan_locator(plan.plan_id)

    def load_plan(self, plan_id: str) -> TransformPlan:
        from ..transform.plans import PlanNotFoundError, TransformPlan

        with self._conn_lock:
            row = (
                self._connection()
                .execute("SELECT body FROM plans WHERE plan_id = ?", (plan_id,))
                .fetchone()
            )
        if row is None:
            raise PlanNotFoundError(
                f"no plan '{plan_id}' in {self.path}; run `transform plan` first "
                "or check the id"
            )
        return TransformPlan.model_validate_json(row[0])

    def list_plans(self) -> list[TransformPlan]:
        from ..transform.plans import TransformPlan

        with self._conn_lock:
            rows = (
                self._connection()
                .execute("SELECT body FROM plans ORDER BY created_at DESC")
                .fetchall()
            )
        return [TransformPlan.model_validate_json(row[0]) for row in rows]

    def latest_plan(self, kind: EditKind | None = None) -> TransformPlan | None:
        from ..transform.plans import TransformPlan

        with self._conn_lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT body FROM plans WHERE applied_at IS NULL "
                    "ORDER BY created_at DESC"
                )
                .fetchall()
            )
        for (raw,) in rows:
            plan = TransformPlan.model_validate_json(raw)
            if kind is None or all(edit.kind is kind for edit in plan.edits):
                return plan
        return None

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        return f"{self.path}#{document.value}"

    def plan_locator(self, plan_id: str) -> str:
        return f"{self.path}#plans/{plan_id}"
