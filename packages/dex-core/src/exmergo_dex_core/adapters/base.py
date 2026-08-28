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
from math import ceil
from typing import Protocol, runtime_checkable

from ..envelope import Paradigm
from ..errors import WarehouseQueryError


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
    #: Declared-type-vs-content statistics, computed only for columns the
    #: engine requested via ``type_stats`` (non-PII string/integer columns).
    #: Fractions and (for epoch) an exact min/max used only to translate into
    #: a calendar date; never a raw value. ``None`` means not computed (not
    #: requested, ineligible type, or the dialect could not).
    numeric_string_fraction: float | None = None
    integer_string_fraction: float | None = None
    iso_date_fraction: float | None = None
    iso_datetime_fraction: float | None = None
    slash_date_fraction: float | None = None
    slash_datetime_fraction: float | None = None
    #: Fraction of slash-date-shaped rows whose first/second component
    #: exceeds 12 -- a logical proof that component can't be a month, used
    #: to disambiguate %m/%d/%Y from %d/%m/%Y (see `type_contradiction_expressions`).
    slash_first_component_over_12_fraction: float | None = None
    slash_second_component_over_12_fraction: float | None = None
    epoch_seconds_fraction: float | None = None
    epoch_millis_fraction: float | None = None
    epoch_seconds_min_value: int | None = None
    epoch_seconds_max_value: int | None = None
    epoch_millis_min_value: int | None = None
    epoch_millis_max_value: int | None = None
    #: Heterogeneous-key-shape statistics, computed only for columns the
    #: engine requested via ``key_shape_stats`` (non-PII string columns).
    #: The numeric bucket reuses ``numeric_string_fraction`` above unchanged;
    #: ``hex_string_fraction`` explicitly excludes anything numeric already
    #: claimed, so the two never double-count the same value. ``None`` means
    #: not computed (not requested, ineligible type, or the dialect could
    #: not).
    uuid_string_fraction: float | None = None
    hex_string_fraction: float | None = None
    hex_string_min_length: int | None = None
    hex_string_max_length: int | None = None
    #: Temporal-continuity statistics, computed only for columns the engine
    #: requested via ``temporal_stats`` (non-PII date/timestamp columns).
    #: ``day_aligned_fraction``/``month_aligned_fraction`` decide the
    #: reported granularity; the three ``{day,month,hour}_distinct_periods``/
    #: ``{day,month,hour}_largest_gap`` pairs are computed for all three
    #: units unconditionally (the granularity is only known after these
    #: fractions come back, so the engine can't pick a unit before the scan
    #: runs) and the engine reads only the pair matching what it decided.
    #: Counts and one integer gap only; never a value. ``None`` means not
    #: computed (not requested, ineligible type, or the dialect could not).
    day_aligned_fraction: float | None = None
    month_aligned_fraction: float | None = None
    day_distinct_periods: int | None = None
    day_largest_gap: int | None = None
    month_distinct_periods: int | None = None
    month_largest_gap: int | None = None
    hour_distinct_periods: int | None = None
    hour_largest_gap: int | None = None


@dataclass(frozen=True)
class ValueDomainSample:
    """One column's raw value-domain probe result. ``values`` is already
    capped and ordered by frequency descending; ``total_distinct`` is the
    *exact* distinct-group count (never the approximate estimate), so the
    caller can compute an accurate elided count and, if the exact count
    turns out to exceed the cap after all (an approximate pre-check
    under-estimated), drop the domain rather than report a partial one.
    """

    values: list[tuple[object, int]]
    total_distinct: int


# A column's value domain is reported only when its distinct count clears BOTH
# bars: small absolutely (the cap) and small relative to the table (the
# fraction), so a tiny table's near-key column does not qualify on the absolute
# count alone. Deliberately conservative: this codebase's general posture is to
# under-report rather than over-report, and a false negative here just costs one
# more `explore query`.
#
# They live beside the sample type rather than in `explore.profile` because a
# metered adapter has to reserve for the probe *before* profiling runs, and the
# two sides would drift apart the moment one of them moved. VALUE_DOMAIN_MIN_ROWS
# is what the fraction implies rather than a threshold of its own: a domain needs
# at least one distinct value, so a table below it can never clear the fraction
# and never produces a probe worth reserving for.
VALUE_DOMAIN_CAP = 25
VALUE_DOMAIN_MAX_FRACTION = 0.10
VALUE_DOMAIN_MIN_ROWS = ceil(1 / VALUE_DOMAIN_MAX_FRACTION)


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


# How much of a driver's error text survives into the envelope. Generous
# enough for any real server message, short enough that a driver which appends
# the whole statement (or a stack of context lines) cannot turn one refusal
# into a wall of stdout.
_SERVER_DETAIL_CAP = 400


def warehouse_refusal(message: str, *, code: str | None = None) -> WarehouseQueryError:
    """The typed error for one server-side statement failure.

    Every adapter funnels through here so the envelope reads the same whichever
    warehouse said no, and so the server's words get the same trim: first line
    only (drivers append the statement, a caret diagram, or their whole error
    payload after it) and capped. ``code`` is the connector's own error code
    where it has one, which is what a caller looking the failure up needs.
    """

    first = next((ln.strip() for ln in message.splitlines() if ln.strip()), "")
    detail = first or "the server gave no message"
    if len(detail) > _SERVER_DETAIL_CAP:
        detail = detail[:_SERVER_DETAIL_CAP].rstrip() + "..."
    return WarehouseQueryError(f"{detail} [{code}]" if code else detail)


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


COMPOSITE_PROBE_SKIPPED_NOTE = (
    "composite-key probe skipped: the remaining budget could not cover the "
    "extra scan; grain stays unknown"
)


def composite_probe_narrowed_note(probed: int, asked: int) -> str:
    """The table note for a probe the budget could only partly cover. It has to
    say what went unasked, because the caller's alternative reading of a missing
    composite key is that the warehouse answered and there was none."""

    return (
        f"composite-key probe narrowed to {probed} of {asked} candidate pairs: "
        "the remaining budget could not cover the rest; a grain outside the "
        "pairs probed stays unknown"
    )


def affordable_combinations(
    combinations: list[list[str]],
    estimate_for: Callable[[list[list[str]]], float],
    try_charge: Callable[[float], bool],
    *,
    floor: float | None = None,
) -> tuple[list[list[str]], str | None]:
    """The longest prefix of ``combinations`` the cost gate will cover, paired
    with the table note the caller owes its reader (None when the whole list
    fits).

    The probe list arrives best-ranked first, so when the budget cannot cover
    all of it, narrowing is the honest degradation and refusing is not: refusing
    gives up the grain entirely, while the affordable prefix is the part most
    likely to hold it. Every metered adapter reaches this the same way, which is
    why the search and both notes live here rather than six times over.

    ``estimate_for`` prices one prefix in the adapter's own paradigm. ``floor``
    is the per-statement billing minimum where the adapter has one: a refusal
    already at the floor cannot be rescued by a shorter prefix, so the search
    stops instead of re-pricing prefixes that cannot cost less, which on
    BigQuery is the difference between one dry run and five.

    Descending attempts are safe because ``CostGate.charge`` raises out of
    ``preflight`` before it accumulates an estimate or widens a reservation, so
    a refused ``try_charge`` leaves the gate exactly as it found it.
    """

    for count in range(len(combinations), 0, -1):
        prefix = combinations[:count]
        estimate = estimate_for(prefix)
        if try_charge(estimate):
            if count == len(combinations):
                return prefix, None
            return prefix, composite_probe_narrowed_note(count, len(combinations))
        if floor is not None and estimate <= floor:
            break
    return [], COMPOSITE_PROBE_SKIPPED_NOTE


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


def is_string_type(data_type: str) -> bool:
    upper = data_type.upper()
    return any(h in upper for h in ("CHAR", "TEXT", "STRING", "VARCHAR"))


def is_integer_type(data_type: str) -> bool:
    """``"INT"`` substring after excluding boolean/temporal (``INTERVAL``
    contains ``"INT"``). Deliberately excludes Snowflake's ``"FIXED"`` (its
    ``SHOW COLUMNS`` reports no scale, so ``NUMBER(38,0)`` and ``NUMBER(10,2)``
    render identically) and every ``DECIMAL``/``NUMERIC``/``DOUBLE``/``FLOAT``/
    ``REAL`` spelling -- a Snowflake integer stored as ``NUMBER`` is a known,
    accepted false negative for the type-contradiction epoch check, consistent
    with this codebase's under-report-over-over-report posture elsewhere."""

    upper = data_type.upper()
    if any(h in upper for h in ("BOOL", "DATE", "TIME", "TIMESTAMP", "INTERVAL")):
        return False
    return "INT" in upper


def is_temporal_type(data_type: str) -> bool:
    """A column with a date component, eligible for span/gap analysis --
    excludes a bare time-of-day (``TIME``, no date to span) and
    ``INTERVAL`` (a duration, not a point in time). ``TIMESTAMP`` matches
    on its own substring, not the generic ``"TIME"`` check that would also
    catch the bare time-of-day type."""

    upper = data_type.upper()
    if "INTERVAL" in upper:
        return False
    return "DATE" in upper or "TIMESTAMP" in upper


def is_date_only_type(data_type: str) -> bool:
    """A bare calendar date with no time-of-day component. Hour granularity
    is meaningless on one (there is nothing to truncate to an hour), and on
    BigQuery ``DATE_TRUNC`` does not even accept an ``HOUR`` unit -- so
    every adapter skips the hour-grain computation for this shape rather
    than relying on another dialect's implicit date-to-timestamp cast.

    ``DATETIME`` is excluded alongside ``TIMESTAMP`` because it *is* a
    timestamp under a different spelling (BigQuery ``DATETIME``, ClickHouse
    ``DateTime``/``DateTime64``) and the bare ``"DATE" in upper`` test would
    otherwise claim it. Getting that wrong is silent: the column keeps its
    day and month continuity and simply never reports an hour gap, which
    reads as a clean result rather than a skipped one."""

    upper = data_type.upper()
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        return False
    return "DATE" in upper


TEMPORAL_UNITS_WITH_TIME = ("day", "month", "hour")
TEMPORAL_UNITS_DATE_ONLY = ("day", "month")


def temporal_units_for(data_type: str) -> tuple[str, ...]:
    """Which granularities are worth computing for one column's declared
    type: hour is skipped for a bare date (see `is_date_only_type`)."""

    if is_date_only_type(data_type):
        return TEMPORAL_UNITS_DATE_ONLY
    return TEMPORAL_UNITS_WITH_TIME


# Declared-type-vs-content patterns (issue #204). No `\d`, no lookaround: the
# shared regex predicates must parse identically across every dialect's regex
# engine (see each adapter's `_regexp_predicate`).
NUMERIC_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"
INTEGER_PATTERN = r"^-?[0-9]+$"
ISO_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
ISO_DATETIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}$"
# Zero-padded 2-digit components only, deliberately: this shape matches both
# %m/%d/%Y and %d/%m/%Y identically, which is what makes the SUBSTR-position
# extraction below reliable (a 1-digit field would shift the fixed offsets).
SLASH_DATE_PATTERN = r"^[0-9]{2}/[0-9]{2}/[0-9]{4}$"
SLASH_DATETIME_PATTERN = r"^[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}$"
# Length-bounded, NOT the general NUMERIC_PATTERN: a CAST gated behind this
# predicate can never overflow BIGINT/INT64 (max ~9.2e18, 19 digits) on any
# dialect, unlike an unbounded numeric string (e.g. a 20-digit surrogate id).
EPOCH_SECONDS_SHAPE_PATTERN = r"^-?[0-9]{1,10}$"
EPOCH_MILLIS_SHAPE_PATTERN = r"^-?[0-9]{1,13}$"

EPOCH_SECONDS_LOW = 946684800  # 2000-01-01T00:00:00Z
EPOCH_SECONDS_HIGH = 4102444800  # 2100-01-01T00:00:00Z
EPOCH_MILLIS_LOW = EPOCH_SECONDS_LOW * 1000
EPOCH_MILLIS_HIGH = EPOCH_SECONDS_HIGH * 1000

# Canonical dashed form only (8-4-4-4-12 hex groups). A UUID stripped of its
# dashes is indistinguishable from a same-length hex string, so that form
# falls through to HEX_PATTERN below rather than being claimed here.
UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Charset-only: length is measured separately (MIN/MAX in
# key_shape_expressions), not baked into the pattern, since a real hash
# column is fixed-length but this predicate must still catch a *mixed*-length
# hex column so there is something to report on.
HEX_PATTERN = r"^[0-9a-fA-F]+$"

# Friendly names for the hash lengths this shape recurs as in practice;
# anything else is reported by its bare length instead of guessing further.
_HEX_LENGTH_NAMES = {32: "md5", 40: "sha1", 64: "sha256"}

# What a shape-gated CAST reads on every row the shape predicate rejects, so
# the cast's argument is digit-only for the whole column and the cast is total
# (see `type_contradiction_expressions`). It has to parse as an integer on
# every dialect and be *rejected* by every predicate built on top of the cast:
# the epoch ranges start in the year 2000 and the slash-component test asks for
# > 12, so zero is evidence of nothing and a row that reaches the cast only
# because the cast is unconditional can never be counted.
_CAST_SENTINEL = "'0'"


def type_contradiction_expressions(
    qcol: str,
    i: int,
    *,
    is_string: bool,
    is_integer: bool,
    regexp_predicate: Callable[[str, str], str],
    bigint_type: str,
) -> list[str]:
    """Declared-type-vs-content aggregate expressions for one column.

    Every CAST here is **total**: its argument is a CASE that yields a
    digit-only string on every row of the column (the value where a
    length-bounded shape predicate matches, ``_CAST_SENTINEL`` where it does
    not), so the cast cannot raise whatever rows the dialect decides to
    evaluate it for, and the length bound means it can never overflow
    BIGINT/INT64 either.

    The obvious shape is the opposite one, and it is a bug (#310). Guarding
    the cast *inside* a CASE branch (``CASE WHEN <shape> THEN CAST(col AS
    BIGINT) END``) reads as safe and is safe on Postgres, but Redshift
    evaluates a branch's cast for rows the WHEN never selects, so a single
    non-numeric string killed the whole profiling statement server-side with
    ``Invalid digit, Value 'p', Pos 0, Type: Long``. The SQL standard does not
    promise the lazy evaluation that guard needs, no offline test can catch a
    dialect that disagrees, and so nothing here depends on it: correctness
    comes from the cast's argument being castable for every row, which is a
    property of the expression rather than of the engine's evaluation order.

    What the sentinel cannot express is the difference between "not shaped"
    and "shaped but out of range", and the fractions' denominators are exactly
    that distinction (``ts_ep_s_{i}`` is the in-range share *of the
    epoch-shaped rows*, not of the column). That comes from a second,
    uncast expression whose NULL-ness is the denominator test, which is where
    the CASE-returns-NULL trick belongs: it carries a string, so it can raise
    nothing.

    Only fractions and translated-to-date integers (read back and converted to
    a calendar date by the caller) ever leave the engine through this path.
    ``qcol`` must already be quoted/escaped by the calling adapter. Returns
    ``[]`` for a column that is neither string- nor integer-typed (nothing to
    check).
    """

    def fraction(value_expr: str, condition: str, alias: str) -> str:
        return (
            f"AVG(CASE WHEN {condition} THEN 1.0 "
            f"WHEN {value_expr} IS NOT NULL THEN 0.0 END) AS {alias}"
        )

    def plain_fraction(pattern: str, alias: str) -> str:
        return fraction(qcol, regexp_predicate(qcol, pattern), alias)

    def shaped(predicate: str) -> str:
        """The column itself where the shape matches, NULL everywhere else:
        the denominator test, uncast and so incapable of raising."""

        return f"CASE WHEN {predicate} THEN {qcol} END"

    def total_cast(predicate: str, inner: str) -> str:
        """``inner`` as an integer, with the sentinel standing in wherever the
        shape predicate does not match, so every row casts digits."""

        return (
            f"CAST(CASE WHEN {predicate} THEN {inner} "
            f"ELSE {_CAST_SENTINEL} END AS {bigint_type})"
        )

    exprs: list[str] = []
    if is_string:
        exprs += [
            plain_fraction(NUMERIC_PATTERN, f"ts_ns_{i}"),
            plain_fraction(INTEGER_PATTERN, f"ts_is_{i}"),
            plain_fraction(ISO_DATE_PATTERN, f"ts_iso_d_{i}"),
            plain_fraction(ISO_DATETIME_PATTERN, f"ts_iso_dt_{i}"),
        ]
        slash_date_pred = regexp_predicate(qcol, SLASH_DATE_PATTERN)
        slash_datetime_pred = regexp_predicate(qcol, SLASH_DATETIME_PATTERN)
        exprs += [
            fraction(qcol, slash_date_pred, f"ts_sl_d_{i}"),
            fraction(qcol, slash_datetime_pred, f"ts_sl_dt_{i}"),
        ]
        slash_either = f"({slash_date_pred} OR {slash_datetime_pred})"
        slash_shaped = shaped(slash_either)
        first_val = total_cast(slash_either, f"SUBSTR({qcol}, 1, 2)")
        second_val = total_cast(slash_either, f"SUBSTR({qcol}, 4, 2)")
        exprs += [
            fraction(slash_shaped, f"{first_val} > 12", f"ts_sl1_{i}"),
            fraction(slash_shaped, f"{second_val} > 12", f"ts_sl2_{i}"),
        ]
        seconds_shape = regexp_predicate(qcol, EPOCH_SECONDS_SHAPE_PATTERN)
        millis_shape = regexp_predicate(qcol, EPOCH_MILLIS_SHAPE_PATTERN)
        seconds_shaped = shaped(seconds_shape)
        millis_shaped = shaped(millis_shape)
        seconds_val = total_cast(seconds_shape, qcol)
        millis_val = total_cast(millis_shape, qcol)
    elif is_integer:
        # Already numeric: no CAST to make total, no overflow surface, and
        # every non-null row is in the denominator.
        seconds_shaped = millis_shaped = seconds_val = millis_val = qcol
    else:
        return []

    seconds_cond = f"{seconds_val} BETWEEN {EPOCH_SECONDS_LOW} AND {EPOCH_SECONDS_HIGH}"
    millis_cond = f"{millis_val} BETWEEN {EPOCH_MILLIS_LOW} AND {EPOCH_MILLIS_HIGH}"
    exprs += [
        fraction(seconds_shaped, seconds_cond, f"ts_ep_s_{i}"),
        fraction(millis_shaped, millis_cond, f"ts_ep_ms_{i}"),
        # The MIN/MAX branch value is the total cast, not the shaped string:
        # the range condition already excludes the sentinel (zero is not a
        # plausible epoch), so a dialect that evaluates the branch for every
        # row computes a cast that cannot raise and reports only in-range rows.
        f"MIN(CASE WHEN {seconds_cond} THEN {seconds_val} END) AS ts_ep_s_mn_{i}",
        f"MAX(CASE WHEN {seconds_cond} THEN {seconds_val} END) AS ts_ep_s_mx_{i}",
        f"MIN(CASE WHEN {millis_cond} THEN {millis_val} END) AS ts_ep_ms_mn_{i}",
        f"MAX(CASE WHEN {millis_cond} THEN {millis_val} END) AS ts_ep_ms_mx_{i}",
    ]
    return exprs


def type_contradiction_aggregate_kwargs(
    values: dict[str, object], i: int, wanted: bool
) -> dict[str, float | int | None]:
    """Every type-contradiction field for one column, ready to splat into a
    ``ColumnAggregate(...)`` call:
    ``**type_contradiction_aggregate_kwargs(values, i, wants_type)``."""

    def frac(alias: str) -> float | None:
        v = values.get(alias) if wanted else None
        return float(v) if v is not None else None

    def epoch(alias: str) -> int | None:
        v = values.get(alias) if wanted else None
        return int(v) if v is not None else None

    return {
        "numeric_string_fraction": frac(f"ts_ns_{i}"),
        "integer_string_fraction": frac(f"ts_is_{i}"),
        "iso_date_fraction": frac(f"ts_iso_d_{i}"),
        "iso_datetime_fraction": frac(f"ts_iso_dt_{i}"),
        "slash_date_fraction": frac(f"ts_sl_d_{i}"),
        "slash_datetime_fraction": frac(f"ts_sl_dt_{i}"),
        "slash_first_component_over_12_fraction": frac(f"ts_sl1_{i}"),
        "slash_second_component_over_12_fraction": frac(f"ts_sl2_{i}"),
        "epoch_seconds_fraction": frac(f"ts_ep_s_{i}"),
        "epoch_millis_fraction": frac(f"ts_ep_ms_{i}"),
        "epoch_seconds_min_value": epoch(f"ts_ep_s_mn_{i}"),
        "epoch_seconds_max_value": epoch(f"ts_ep_s_mx_{i}"),
        "epoch_millis_min_value": epoch(f"ts_ep_ms_mn_{i}"),
        "epoch_millis_max_value": epoch(f"ts_ep_ms_mx_{i}"),
    }


def key_shape_expressions(
    qcol: str, i: int, regexp_predicate: Callable[[str, str], str]
) -> list[str]:
    """Heterogeneous-key-shape aggregate expressions for one column.

    Every non-null value falls into exactly one of numeric / uuid / hex /
    other by construction: the hex bucket explicitly excludes anything the
    numeric pattern already claimed (a pure-digit string like ``"123456"``
    is valid input to a hex-charset pattern too), which is what keeps
    ``numeric_string_fraction`` directly reusable here unchanged and keeps
    the two buckets from double-counting the same value. Plain boolean
    predicates ANDed together cast nothing, so the total-CAST discipline
    ``type_contradiction_expressions`` has to keep does not apply here and
    nothing in these expressions can raise on any dialect, whatever it
    chooses to evaluate. ``qcol`` must already be quoted/escaped by the
    calling adapter.
    """

    numeric_pred = regexp_predicate(qcol, NUMERIC_PATTERN)
    hex_pred = regexp_predicate(qcol, HEX_PATTERN)
    hex_not_numeric = f"({hex_pred} AND NOT {numeric_pred})"
    return [
        f"AVG(CASE WHEN {regexp_predicate(qcol, UUID_PATTERN)} THEN 1.0 "
        f"WHEN {qcol} IS NOT NULL THEN 0.0 END) AS ks_uuid_{i}",
        f"AVG(CASE WHEN {hex_not_numeric} THEN 1.0 "
        f"WHEN {qcol} IS NOT NULL THEN 0.0 END) AS ks_hex_{i}",
        f"MIN(CASE WHEN {hex_not_numeric} THEN LENGTH({qcol}) END) AS ks_hexmn_{i}",
        f"MAX(CASE WHEN {hex_not_numeric} THEN LENGTH({qcol}) END) AS ks_hexmx_{i}",
    ]


def key_shape_aggregate_kwargs(
    values: dict[str, object], i: int, wanted: bool
) -> dict[str, float | int | None]:
    """Every key-shape field for one column, ready to splat into a
    ``ColumnAggregate(...)`` call: ``**key_shape_aggregate_kwargs(values, i,
    wants_key_shape)``."""

    def frac(alias: str) -> float | None:
        v = values.get(alias) if wanted else None
        return float(v) if v is not None else None

    def length(alias: str) -> int | None:
        v = values.get(alias) if wanted else None
        return int(v) if v is not None else None

    return {
        "uuid_string_fraction": frac(f"ks_uuid_{i}"),
        "hex_string_fraction": frac(f"ks_hex_{i}"),
        "hex_string_min_length": length(f"ks_hexmn_{i}"),
        "hex_string_max_length": length(f"ks_hexmx_{i}"),
    }


_TEMPORAL_UNIT_ALIAS = {"day": "d", "month": "m", "hour": "h"}


def temporal_alignment_expressions(
    qcol: str, i: int, date_trunc: Callable[[str, str], str]
) -> list[str]:
    """Fraction of non-null values that are already truncated to day/month --
    the evidence `explore.profile._temporal_granularity` decides the
    reported granularity from. A column whose values are all exactly
    midnight is day-aligned; one whose values are all the 1st of the month
    is also month-aligned."""

    def fraction(condition: str, alias: str) -> str:
        return (
            f"AVG(CASE WHEN {condition} THEN 1.0 "
            f"WHEN {qcol} IS NOT NULL THEN 0.0 END) AS {alias}"
        )

    return [
        fraction(f"{qcol} = {date_trunc(qcol, 'day')}", f"tc_da_{i}"),
        fraction(f"{qcol} = {date_trunc(qcol, 'month')}", f"tc_ma_{i}"),
    ]


def default_lag_expr(value: str, order_by: str) -> str:
    """The standard-SQL previous-row window expression, used by every dialect
    whose ``LAG`` returns NULL past the frame edge.

    ClickHouse has no ``LAG`` at all, and its ``lagInFrame`` substitute returns
    the *type default* rather than NULL for the first row, so a connector whose
    lag idiom differs supplies its own (see `temporal_continuity_sql`)."""

    return f"LAG({value}) OVER (ORDER BY {order_by})"


def temporal_continuity_sql(
    qcol: str,
    i: int,
    unit: str,
    table_sql: str,
    date_trunc: Callable[[str, str], str],
    date_diff: Callable[[str, str, str], str],
    lag_expr: Callable[[str, str], str] = default_lag_expr,
) -> list[str]:
    """Distinct-period count and largest gap for one column at one
    granularity, as two scalar subqueries spliced into the caller's flat
    SELECT (the same "subquery inside the SELECT list" shape
    ``distinct_combination_sql`` already uses for composite-key probes).

    The gap is measured between consecutive *present* periods via the
    caller's ``lag_expr`` (standard ``LAG`` for every dialect that has one),
    diffed by the caller's own date-diff idiom (there is no universal
    ``DATEDIFF`` across dialects -- Postgres has none), minus one: a diff of
    1 between neighbors means no missing period between them, so the
    reported gap is the count of missing periods in the widest run, not the
    raw diff. ``COALESCE(..., 0)`` covers both the single-period and the
    empty-column case, where the inner aggregate has nothing (or only a NULL
    first-row lag) to compare.

    **The first row's lag must be NULL, not a zero value.** ``MAX`` skips
    NULL, so a NULL lag drops the meaningless first-row comparison; a lag
    that yields the type default instead (ClickHouse ``lagInFrame`` without
    an explicit NULL default returns the epoch) makes every column report a
    gap of ~20,000 days, and one that silently returns the *current* row
    makes every column report no gap at all. Both look like a working
    detector. Any adapter overriding ``lag_expr`` owes a live test against a
    column with a known hole.

    Only two integers ever leave the engine through this path.
    """

    # Interpolated parts are a quoted/escaped column (by the calling
    # adapter), fixed aggregate keywords, and the caller's own quoted table
    # identifier -- never a value; the caller guards the assembled statement
    # as read-only SELECT before it runs.
    alias = _TEMPORAL_UNIT_ALIAS[unit]
    periods = (
        f"(SELECT DISTINCT {date_trunc(qcol, unit)} AS period "  # noqa: S608
        f"FROM {table_sql} WHERE {qcol} IS NOT NULL)"
    )
    lagged = (
        f"(SELECT period, {lag_expr('period', 'period')} AS prev_period "  # noqa: S608
        f"FROM {periods} AS periods_{alias}_{i})"
    )
    gap_expr = date_diff(unit, "period", "prev_period")
    return [
        f"(SELECT COUNT(*) FROM {periods} AS count_{alias}_{i}) AS tp_{alias}_{i}",  # noqa: S608
        f"(SELECT COALESCE(MAX({gap_expr} - 1), 0) FROM {lagged} "  # noqa: S608
        f"AS gaps_{alias}_{i}) AS tg_{alias}_{i}",
    ]


def temporal_continuity_aggregate_kwargs(
    values: dict[str, object], i: int, wanted: bool
) -> dict[str, float | int | None]:
    """Every temporal-continuity field for one column, ready to splat into a
    ``ColumnAggregate(...)`` call:
    ``**temporal_continuity_aggregate_kwargs(values, i, wants_temporal)``."""

    def frac(alias: str) -> float | None:
        v = values.get(alias) if wanted else None
        return float(v) if v is not None else None

    def count(alias: str) -> int | None:
        v = values.get(alias) if wanted else None
        return int(v) if v is not None else None

    return {
        "day_aligned_fraction": frac(f"tc_da_{i}"),
        "month_aligned_fraction": frac(f"tc_ma_{i}"),
        "day_distinct_periods": count(f"tp_d_{i}"),
        "day_largest_gap": count(f"tg_d_{i}"),
        "month_distinct_periods": count(f"tp_m_{i}"),
        "month_largest_gap": count(f"tg_m_{i}"),
        "hour_distinct_periods": count(f"tp_h_{i}"),
        "hour_largest_gap": count(f"tg_h_{i}"),
    }


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
        type_stats: set[str] | None = None,
        key_shape_stats: set[str] | None = None,
        temporal_stats: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        """Profile every column of one object in as few aggregate queries as
        possible. ``safe_min_max`` is the set of column names for which min/max may
        be computed; all others get ``None`` so values never leave the engine.
        ``shape_stats`` is the set of string column names for which the value-shape
        fractions are computed (in the same scan); all others keep them ``None``.
        ``type_stats`` is the set of non-PII string/integer column names for which
        the declared-type-vs-content fractions (#204) are computed, same scan.
        ``key_shape_stats`` is the set of non-PII string column names for which
        the heterogeneous-key-shape fractions (#205) are computed, same scan.
        ``temporal_stats`` is the set of non-PII date/timestamp column names for
        which the temporal-continuity fractions/counts (#206) are computed, same
        scan."""
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

    def value_domain_counts(
        self, identifier: str, columns: list[str], *, limit: int
    ) -> dict[str, ValueDomainSample]:
        """Top ``limit`` values by frequency for each named column, plus each
        column's exact distinct-group count, batched into as few statements
        as possible. The engine calls this only for columns already screened
        as non-PII and low-cardinality, so the spend is bounded and
        deliberate; a metered adapter that cannot cover the scan within the
        confirmed budget returns ``{}`` and explains itself through a table
        note, same as ``distinct_combination_counts``. Optional: an adapter
        that does not implement it simply reports no value domain, exactly
        like an adapter that predates this capability."""
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
