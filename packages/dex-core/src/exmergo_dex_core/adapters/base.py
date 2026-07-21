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

from collections.abc import Callable, Iterable
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
    #: Value-shape statistics, computed only for columns the engine requested via
    #: ``shape_stats`` (name-flagged generic-name columns). Numeric fractions and
    #: averages derived in-engine from regex predicates inside aggregates; never
    #: values. ``None`` means not computed (not requested, non-string, degraded,
    #: or the dialect could not), which the engine treats as absent evidence.
    upper_vocab_fraction: float | None = None
    person_shape_fraction: float | None = None
    avg_token_count: float | None = None


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


# Substring hints for arbitrary binary-blob column types across dialects
# (BigQuery BYTES, DuckDB BLOB, Postgres bytea, Snowflake/Databricks
# BINARY/VARBINARY). Matched the same way as profile.is_numeric_type, so a
# repeated spelling (ARRAY<BYTES>, BLOB[]) is caught by the same substring
# search without a separate case.
_BLOB_HINTS = ("BYTES", "BLOB", "BYTEA", "BINARY")


def is_blob_type(data_type: str) -> bool:
    """Whether a connector's raw column type is an arbitrary binary blob, scalar
    or repeated. A blob column's profile can only ever be a null fraction and a
    distinct estimate, yet a columnar engine bills for the whole column once it
    is referenced by any aggregate at all -- so ``explore profile`` excludes
    these columns from its scan by default (see ``explore.profile.profile``)."""

    upper = data_type.upper()
    return any(h in upper for h in _BLOB_HINTS)


def distinct_combination_sql(
    table_sql: str,
    combinations: list[list[str]],
    quote_ident: Callable[[str], str],
) -> str:
    """One statement counting each column combination's distinct tuples, one
    scalar subquery per combination, results read back by alias ``d_{i}``.

    The subquery form is the one shape every supported dialect accepts
    (BigQuery has no multi-argument COUNT(DISTINCT); DuckDB needs a struct
    variant); derived tables are aliased because Postgres requires it.
    ``table_sql`` and the identifiers must already be quoted/escaped by the
    calling adapter, which also guards the result as a read-only SELECT.
    """

    parts = [
        "(SELECT COUNT(*) FROM (SELECT DISTINCT "  # noqa: S608
        + ", ".join(quote_ident(name) for name in combo)
        + f" FROM {table_sql}) AS q_{i}) AS d_{i}"
        for i, combo in enumerate(combinations)
    ]
    return f"SELECT {', '.join(parts)}"


# Value-shape regex patterns, shared by every adapter so the shape evidence the
# engine reasons over means the same thing on every connector. Both are plain
# POSIX-class regexes (no \d, no lookaround) so they parse in every supported
# engine's regex flavor, and both anchor in the pattern because some predicates
# (Databricks RLIKE, Postgres ~) match substrings.
#
# UPPER_VOCAB: values that are entirely upper-case tokens (spaces/hyphens
# allowed), the signature of a closed reference vocabulary like region or nation
# labels ("MIDDLE EAST"). PERSON_SHAPE: exactly two capitalized words, the
# given/surname shape; deliberately not "two or more tokens", which would
# misread multi-word labels ("Australian Grand Prix") as person-shaped.
UPPER_VOCAB_PATTERN = "^[A-Z]+([ -][A-Z]+)*$"
PERSON_SHAPE_PATTERN = "^[A-Z][a-z]+ [A-Z][a-z]+$"


def shape_stat_expressions(
    qcol: str,
    i: int,
    regexp_predicate: Callable[[str, str], str],
) -> list[str]:
    """The three value-shape aggregate expressions for one column, read back by
    aliases ``su_{i}`` / ``sp_{i}`` / ``st_{i}``.

    Results are numeric fractions and an average token count, never values. The
    CASE has no ELSE so a NULL input yields NULL, which AVG skips: nulls never
    dilute the fraction denominators. ``qcol`` must already be quoted/escaped by
    the calling adapter, and ``regexp_predicate(qcol, pattern)`` renders that
    dialect's full-match predicate (anchors ride in the pattern).
    """

    def fraction(pattern: str, alias: str) -> str:
        predicate = regexp_predicate(qcol, pattern)
        return (
            f"AVG(CASE WHEN {predicate} THEN 1.0 "
            f"WHEN {qcol} IS NOT NULL THEN 0.0 END) AS {alias}"
        )

    token_count = f"LENGTH({qcol}) - LENGTH(REPLACE({qcol}, ' ', '')) + 1"
    return [
        fraction(UPPER_VOCAB_PATTERN, f"su_{i}"),
        fraction(PERSON_SHAPE_PATTERN, f"sp_{i}"),
        f"AVG({token_count}) AS st_{i}",
    ]


def shape_stat_value(
    values: dict[str, object], alias: str, wanted: bool
) -> float | None:
    """Read one shape statistic back from an alias/value row, ``None`` when it
    was not requested or the engine returned NULL (e.g. an all-NULL column)."""

    if not wanted:
        return None
    value = values.get(alias)
    return float(value) if value is not None else None


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
        shape_stats: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        """Profile every column of one object in as few aggregate queries as
        possible. ``safe_min_max`` is the set of column names for which min/max may
        be computed; all others get ``None`` so values never leave the engine.
        ``shape_stats`` is the set of string column names for which the value-shape
        fractions are computed (in the same scan); all others keep them ``None``."""
        ...

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact ``COUNT(DISTINCT)`` for the named columns, batched into as few
        statements as possible. The engine calls this only for columns whose
        approximate distinct landed within noise of the non-null count, so the
        spend is bounded and deliberate; a metered adapter never self-escalates."""
        ...

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count for each column combination, all in one
        statement. The engine calls this only when no single-column key was
        proven, with a small ranked set of combinations, so the spend is
        bounded and deliberate; a metered adapter that cannot cover the scan
        within the confirmed budget returns ``{}`` and explains itself through
        a table note instead of self-escalating."""
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
