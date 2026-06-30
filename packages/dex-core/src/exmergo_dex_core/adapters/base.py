"""The warehouse adapter protocol every connector implements.

One adapter per connector normalizes namespaces, carries the SQL dialect, owns
the per-connector cost strategy, and exposes a cheap-metadata path plus an
aggregate-profiling path. DuckDB is the only adapter with real logic today; the
cloud adapters are stubs. Keeping the surface here means the explore and transform
engines code against the protocol, not a specific warehouse.

The introspection types below carry only metadata and aggregates. None of them
has a field that can hold a raw row, so the "profile, don't exfiltrate" guarantee
holds by construction at the type level: whatever an adapter returns to the engine
is already an aggregate or a flag, never row values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..envelope import Paradigm


@dataclass(frozen=True)
class ObjectMeta:
    """Cheap, scan-free facts about one warehouse object (table or view).

    ``row_count`` is an estimate at inventory time (no scan); an exact count is
    fetched lazily only when an object is profiled. ``byte_size`` is left ``None``
    where a connector has no cheap per-object byte size (DuckDB), rather than
    fabricating a misleading number.
    """

    identifier: str
    object_type: str
    schema: str
    name: str
    row_count: int | None
    byte_size: int | None
    column_count: int


@dataclass(frozen=True)
class ColumnMeta:
    """A column's catalog metadata: name, raw connector type, nullability, order."""

    name: str
    data_type: str
    nullable: bool
    ordinal: int


@dataclass(frozen=True)
class ColumnAggregate:
    """Aggregate-derived facts about one column. Built from SQL aggregates only.

    ``min_value`` / ``max_value`` are populated by the adapter only for columns the
    engine has marked safe (numeric / temporal, non-PII); for everything else they
    stay ``None`` so a sensitive or free-text value never crosses the boundary.
    ``distinct_count`` is approximate (``approx_count_distinct``) for scale, so
    ``is_unique`` derived from it is a signal, not a proof.
    """

    name: str
    null_fraction: float | None
    distinct_count: int | None
    is_unique: bool | None
    min_value: object | None
    max_value: object | None


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

    def list_objects(self, *, include_views: bool = True) -> list[ObjectMeta]:
        """Landscape pass: every object's cheap metadata in one round-trip, no
        per-object scans. Backs ``explore inventory``."""
        ...

    def table_metadata(self, identifier: str) -> tuple[ObjectMeta, list[ColumnMeta]]:
        """One object's metadata plus its columns. The ``ObjectMeta`` here carries
        an exact ``row_count`` (one cheap aggregate), unlike the estimate from
        ``list_objects``."""
        ...

    def column_aggregates(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        safe_min_max: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        """Profile every column of one object in as few aggregate queries as
        possible. ``safe_min_max`` is the set of column names for which min/max may
        be computed; all others get ``None`` so values never leave the engine."""
        ...

    def close(self) -> None: ...
