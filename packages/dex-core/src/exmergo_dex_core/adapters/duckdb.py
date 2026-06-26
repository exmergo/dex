"""The DuckDB adapter: first-class product connector and the eval/benchmark
engine. One implementation, three uses.

DuckDB is always opened read-only and bounded by memory/thread limits rather than
cost, because the work is free and local. This is the only adapter with real logic
today; it is what makes the whole loop buildable with no cloud accounts and
deterministic in CI.
"""

from __future__ import annotations

from pathlib import Path

from ..envelope import Paradigm

# Conservative defaults so auto-invoked profiling cannot exhaust the machine.
# Overridable from .dex/config.yml.
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4


class DuckDBReadOnlyError(Exception):
    """Raised when a DuckDB path cannot be opened read-only.

    A read-only open is non-negotiable: rather than silently falling back to a
    writable connection, we fail loudly so the safety guarantee cannot erode.
    """


class DuckDBAdapter:
    """Holds a read-only DuckDB connection for the lifetime of one command.

    Opening a brand-new (nonexistent) file read-only fails in DuckDB, which is the
    correct behavior for dex: we attach to an existing analytical store, we never
    create one.
    """

    name = "duckdb"
    dialect = "duckdb"
    paradigm = Paradigm.FREE_LOCAL

    def __init__(
        self,
        path: str | Path,
        *,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        threads: int = DEFAULT_THREADS,
    ):
        self.path = str(path)
        self._memory_limit = memory_limit
        self._threads = threads
        self._conn = self._connect()

    def _connect(self):
        # Imported lazily so the base package import does not require the [duckdb]
        # extra; only this adapter pulls it in.
        import duckdb

        try:
            conn = duckdb.connect(
                self.path,
                read_only=True,
                config={
                    "memory_limit": self._memory_limit,
                    "threads": self._threads,
                },
            )
        except Exception as exc:  # duckdb raises various IO/Catalog errors
            raise DuckDBReadOnlyError(
                f"could not open '{self.path}' read-only: {exc}"
            ) from exc
        return conn

    def capabilities(self) -> dict[str, object]:
        version = self._conn.sql("SELECT version()").fetchone()[0]
        return {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "engine_version": version,
            "resource_bounds": {
                "memory_limit": self._memory_limit,
                "threads": self._threads,
            },
        }

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
