"""The warehouse adapter protocol every connector implements.

One adapter per connector normalizes namespaces, carries the SQL dialect, owns
the per-connector cost strategy, and exposes a cheap-metadata path plus an
aggregate-profiling path. DuckDB is the only adapter with real logic today; the
cloud adapters are stubs. Keeping the surface here means the explore and transform
engines code against the protocol, not a specific warehouse.

The introspection types below carry only metadata and aggregates, so the
"profile, don't exfiltrate" guarantee holds by construction at the type level,
with one deliberate exception: :class:`QueryResult` can hold result cells, and it
exists only for agent-authored queries that have already passed the query
firewall (``guards/query_firewall.py``), which refuses any expression that would
carry values out of a PII-flagged or unprofiled column. Values reach a
``QueryResult`` only from profiled, PII-cleared columns, and the command layer
caps and truncates them before the envelope.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
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
    ``distinct_count`` is approximate (``approx_count_distinct``) for scale unless
    ``distinct_count_exact`` is set, in which case the engine escalated it to an
    exact ``COUNT(DISTINCT)`` and ``is_unique`` derived from it is a proof, not a
    signal.
    """

    name: str
    null_fraction: float | None
    distinct_count: int | None
    is_unique: bool | None
    min_value: object | None
    max_value: object | None
    distinct_count_exact: bool = False


@dataclass(frozen=True)
class QueryResult:
    """The result of one firewall-approved agent query, columnar.

    ``cells`` is a list of rows, each a list of JSON-safe scalars, deliberately
    NOT a list of dicts: the columnar shape is cheaper in tokens (no repeated
    keys) and keeps the envelope sanitizer's list-of-dicts raw-row rule intact as
    a backstop against accidental record dumps elsewhere. ``truncated`` is set by
    the adapter when the query produced more rows than requested.
    """

    columns: list[str]
    types: list[str]
    cells: list[list]
    truncated: bool


def scope_within(scope: str, committed: list[str]) -> bool:
    """Whether one scope entry lies inside a committed source allowlist.

    Every connector's scope entries are dotted namespace paths that grow coarse to
    fine (``project.dataset``, ``database.schema``, ``catalog.schema``), so
    containment is prefix containment on path segments: ``RAW.EVENTS`` is inside
    ``RAW``, and ``RAW`` is not inside ``RAW.EVENTS``. Comparison is
    case-insensitive because the connectors disagree about identifier case and a
    case mismatch must never read as an escape attempt.

    This is what makes ``--scope`` narrow-only. A committed allowlist is a cost
    boundary, so a per-command flag has to stay inside it.
    """

    entry = scope.strip().lower()
    return any(
        entry == c.strip().lower() or entry.startswith(c.strip().lower() + ".")
        for c in committed
    )


SUGGESTION_CAP = 12


def name_list(names: Iterable[str]) -> str:
    """Names for an error message, capped so a thousand-schema account cannot
    turn a one-line refusal into a page of stdout."""

    names = list(names)
    shown = names[:SUGGESTION_CAP]
    suffix = (
        f", and {len(names) - SUGGESTION_CAP} more"
        if len(names) > SUGGESTION_CAP
        else ""
    )
    return (", ".join(shown) + suffix) if shown else "(none)"


@contextmanager
def blame(origin: str, error: type[Exception]):
    """Attribute a scope failure to the thing the user has to go edit. A resolver
    does not know whether an entry came from the committed allowlist or from a
    flag, and the fix differs entirely.

    ``error`` is the connector's own exception class, so the re-raise stays the
    type that connector's callers already catch.
    """

    try:
        yield
    except error as exc:
        raise error(f"{exc} [from {origin}]") from exc


def json_safe(value: object | None) -> object | None:
    """Coerce a connector scalar to a JSON-serializable primitive for the envelope."""

    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value)


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

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact ``COUNT(DISTINCT)`` for the named columns, batched into as few
        statements as possible. The engine calls this only for columns whose
        approximate distinct landed within noise of the non-null count, so the
        spend is bounded and deliberate; a metered adapter never self-escalates."""
        ...

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT and return a columnar result.

        Callers MUST pass SQL that has already been through
        ``guards.query_firewall.inspect_query``; the adapter re-asserts
        SELECT-only as defense in depth but performs no PII policy of its own.
        Fetches at most ``max_rows`` rows and flags truncation."""
        ...

    def close(self) -> None: ...
