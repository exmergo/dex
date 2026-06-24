"""The warehouse adapter protocol every connector implements.

One adapter per connector normalizes namespaces, carries the SQL dialect, owns
the per-connector cost strategy, and exposes a cheap-metadata path. DuckDB is the
only adapter with real logic today; the cloud adapters are stubs. Keeping the
surface here means the explore and transform engines code against the protocol,
not a specific warehouse.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..envelope import Paradigm


@runtime_checkable
class Adapter(Protocol):
    """Behavioral contract for a connector adapter.

    Connection state lives inside the adapter instance (class DI): it holds the
    open handle and the raw-data access, so nothing leaks past the engine. The
    agent only ever sees the sanitized envelope.
    """

    #: Stable connector name, e.g. "duckdb", "snowflake".
    name: str
    #: SQLGlot dialect name for SQL generation/parsing.
    dialect: str
    #: Cost paradigm this connector bills under.
    paradigm: Paradigm

    def capabilities(self) -> dict[str, object]:
        """Cheap, read-only probe: what this connection can do, its dialect, and
        that it is read-only. Backs ``dex connect test``."""
        ...

    def close(self) -> None:
        ...
