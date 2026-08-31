"""Explore: column profiling and PII detection.

Understanding is built from SQL aggregates, never from raw rows in context
(Principle 2). PII is recorded as (column, category, confidence) with no example
value (Principle 6), and min/max are surfaced only for columns where the extreme
value is not itself sensitive: numeric and temporal types that carry no PII flag.
For any string column, or any column flagged PII, min/max are suppressed at the
source so the value never leaves the engine.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime

from ..adapters.base import (
    VALUE_DOMAIN_CAP,
    VALUE_DOMAIN_MAX_FRACTION,
    Adapter,
    ColumnAggregate,
    is_blob_type,
    is_integer_type,
    is_temporal_type,
    json_safe,
)
from ..adapters.base import is_string_type as _base_is_string_type
from ..cache import (
    ColumnProfile,
    Dataset,
    PIICategory,
    PIIFlag,
    ValueCount,
    ValueDomain,
)
from ..config import PIIOverrideMatcher
from ..errors import WarehouseQueryError
from ..progress import ProgressReporter

# approx_count_distinct error observed in practice reaches ~14% in both
# directions at tens of thousands of rows (27,044 approx on 26,599 unique;
# 39,134 approx on 45,000 distinct), far beyond HLL's nominal ~2%. The
# threshold is one-sided with margin: any overshoot is consistent with a truly
# unique column (an exact count can never exceed the non-null count), and
# undershoot is covered down to 75% of non-null. Shared with
# relationships.data_quality_notes, which must not call a column non-unique
# from an approximation inside this band.
NEAR_UNIQUE_RATIO = 0.75

# All near-unique columns of one table escalate in a single batched
# COUNT(DISTINCT) statement; the cap bounds the width of that statement.
_EXACT_DISTINCT_CAP = 8

# Composite-key probes are capped much tighter than single-column escalation:
# each pair costs a full two-column DISTINCT scan (not one cheap aggregate),
# and the ranking puts a real grain in the first slots when one exists.
_COMPOSITE_PAIR_CAP = 5

# A later pair that shares a column with a better-ranked one is treated as the
# same hypothesis tried with different filler ("does this dimension have *a*
# partner?") when its product is within this multiple of the better-ranked
# pair's -- not a genuinely different candidate. Keeping the ratio bounded
# (rather than 1:1) is what sends several near-identical junk pairs to the back
# together; a pair whose product diverges meaningfully (a real competing
# hypothesis, not filler) keeps its place even if it reuses a column. This only
# orders candidates. It decides nothing about which ones get probed while the
# cap has room, because a discarded candidate cannot be the grain.
_COMPOSITE_REDUNDANCY_RATIO = 3.0

# Name patterns mapped to a PII category and a base confidence. Matched on the
# snake-normalized column name (camelCase is split first, so "firstName" matches
# the same as "first_name") with word-ish boundaries so "email" hits but
# "emailable" does not over-trigger. Detection never inspects values, only names
# and types.
_PII_PATTERNS: list[tuple[re.Pattern[str], PIICategory, float]] = [
    (re.compile(r"(^|_)(e_?mail|email_address)(_|$)"), PIICategory.EMAIL, 0.9),
    (re.compile(r"(^|_)(phone|mobile|cell|fax|msisdn)(_|$)"), PIICategory.PHONE, 0.8),
    (
        re.compile(
            r"(^|_)(first_?name|last_?name|full_?name|surname|given_?name|fname|lname)(_|$)"
        ),
        PIICategory.NAME,
        0.75,
    ),
    (
        re.compile(r"(^|_)(address|addr|street|city|zip|postal|postcode)(_|$)"),
        PIICategory.ADDRESS,
        0.7,
    ),
    (
        re.compile(r"(^|_)(ssn|nino|tax_?id|passport|national_?id|tin)(_|$)"),
        PIICategory.GOVERNMENT_ID,
        0.9,
    ),
    (
        re.compile(r"(^|_)(iban|card|cc_?num|account_?no|routing|cvv|salary)(_|$)"),
        PIICategory.FINANCIAL,
        0.8,
    ),
    (
        re.compile(r"(^|_)(password|passwd|secret|api_?key|access_?token)(_|$)"),
        PIICategory.CREDENTIAL,
        0.95,
    ),
    (
        re.compile(r"(^|_)(lat|lng|latitude|longitude|geo|coordinates)(_|$)"),
        PIICategory.LOCATION,
        0.6,
    ),
    (
        re.compile(r"(^|_)(dob|birth_?date|date_of_birth)(_|$)|(^|_)birth(_|$)"),
        PIICategory.DOB,
        0.85,
    ),
]

# Generic person-name detection: any bare `name` or `*_name` column. Weaker than
# the exact person tokens above (a `name` could be a product), so it carries a
# lower confidence, applies only to string columns, and a denylist of clearly
# technical/organizational qualifiers stops the obvious false positives. The
# denylist errs toward flagging: a false positive merely suppresses a min/max,
# while a false negative reads as an all-clear on sensitive data.
_GENERIC_NAME = re.compile(r"(^|_)name(_|$)")
_NONPERSON_NAME_QUALIFIERS = frozenset(
    {
        "table",
        "column",
        "file",
        "schema",
        "db",
        "database",
        "model",
        "type",
        "product",
        "brand",
        "company",
        "app",
        "service",
    }
)

# Free-text fields carry PII in their values regardless of the column name, so
# any comment/notes/message-shaped string column is flagged FREE_TEXT.
_FREE_TEXT = re.compile(
    r"(^|_)(comments?|notes?|message|body|feedback|review_text|bio|about)(_|$)"
)

# A column name that promises exactly two states (issue #218): the `is_`/`has_`
# prefixes and `_flag`/`_yn`/`_ind` suffixes a data-quality check reads as
# "boolean, by convention" even on a connector with no native BOOLEAN type.
# Matched as a whole underscore-delimited token, same discipline as the PII
# patterns above, so "is_active" matches but "history"/"island" do not.
_BOOLEAN_NAME = re.compile(r"(^|_)(is|has|flag|yn|ind)(_|$)")

# "FIXED" is Snowflake's SHOW COLUMNS token for every integer and NUMBER type
# (surfaced by snowflake._render_type); without it no Snowflake integer column
# reads as numeric, and the type-aware PII gates below would be inert there.
_NUMERIC_HINTS = (
    "INT",
    "DECIMAL",
    "NUMERIC",
    "DOUBLE",
    "FLOAT",
    "REAL",
    "HUGEINT",
    "FIXED",
)
_TEMPORAL_HINTS = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
_BOOLEAN_HINTS = ("BOOL",)

_MAX_CONFIDENCE = 0.95

# Confidence levels for the generic `*_name` flag and its value-shape verdicts.
# The generic flag starts at 0.6 (weaker than an exact person token at 0.75);
# shape evidence computed in the profiling scan can corroborate it up to the
# exact-token level or de-rate it to 0.3, below the query firewall's blocking
# threshold. Evidence moves confidence in both directions and its absence moves
# nothing (fail closed: an unverifiable flag keeps blocking).
_GENERIC_NAME_CONFIDENCE = 0.6
_SHAPE_PERSON_CONFIDENCE = 0.75
_SHAPE_REFERENCE_CONFIDENCE = 0.3

# Shape-rule thresholds. A column where half the values look like "Given
# Surname" is treated as person names. The reference verdict requires person
# shape to be essentially absent AND one of: a tiny closed all-caps vocabulary
# (the R_NAME/N_NAME shape; the distinct cap keeps a large all-caps
# customer-name column blocked, since an all-caps person name defeats the
# person-shape check), long multi-token labels (person names essentially never
# average 3.5+ tokens; part and product descriptions do), or a closed
# enumeration regardless of case or token count (weekday/month names, status
# codes: neither all-caps nor multi-token, so the two rules above miss them,
# but cardinality alone is conclusive).
_PERSON_FRACTION_RAISE = 0.5
_PERSON_FRACTION_ABSENT = 0.05
_UPPER_VOCAB_FRACTION = 0.9
_UPPER_VOCAB_MAX_DISTINCT = 32
_LABEL_AVG_TOKENS = 3.5

# A closed enumeration: distinct count small in absolute terms AND as a
# fraction of non-null rows, so a genuinely small table of distinct people
# (few rows, each one a different name) is not cleared by the same rule -- a
# small table has a HIGH distinct/row fraction; an enumeration column on a
# real fact table has a tiny one.
_ENUM_MAX_DISTINCT = 32
_ENUM_MAX_DISTINCT_FRACTION = 0.01

# Splits camelCase boundaries so patterns written against snake_case also match
# camelCase warehouses ("firstName" -> "first_name", "reviewerName" -> "reviewer_name").
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalize(column_name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", column_name).lower()


def _is_string_type(data_type: str) -> bool:
    # Delegates to adapters.base so profile.py's request-set construction and
    # each adapter's SQL-branch decision (#204) classify a declared type the
    # same way, single-sourced.
    return _base_is_string_type(data_type)


def is_numeric_type(data_type: str) -> bool:
    """Whether a connector's raw column type is numeric (integer, decimal, or
    float). Substring-matched against ``_NUMERIC_HINTS`` so it holds across
    dialects (DuckDB BIGINT/HUGEINT, BigQuery INT64/FLOAT64/NUMERIC, Snowflake
    NUMBER, Postgres double precision). Booleans and temporals are excluded
    first, because ``INTERVAL`` and ``BOOL`` would otherwise match the ``INT``
    hint. Shared so numeric-feature selection for `explore cluster` and min/max
    safety stay single-sourced on one hint set."""

    upper = data_type.upper()
    if any(h in upper for h in _BOOLEAN_HINTS + _TEMPORAL_HINTS):
        return False
    return any(h in upper for h in _NUMERIC_HINTS)


# Per-category structural type gate. Type evidence is known before any scan, so
# an impossible pairing (an EMAIL on an integer) suppresses the flag outright at
# classification time rather than being re-scored later. The gate is
# per-category impossibility, never blanket: several categories legitimately
# live on non-string columns (zip as INT64, ssn as INT64, salary as NUMERIC,
# lat/lng as FLOAT, dob as DATE) and must keep flagging there.
def _type_permits(category: PIICategory, data_type: str) -> bool:
    """Whether ``category`` can structurally sit on a column of ``data_type``.

    Built on the existing type predicates; adds no fourth type-hint tuple.

    - EMAIL / NAME / FREE_TEXT: string only (an integer cannot hold an address
      or a person name; this matches the pre-existing generic-name/free-text
      gate, extended to the exact-token patterns).
    - PHONE: anything but boolean or temporal (phone-as-INT is bad practice but
      real, so a numeric phone still flags).
    - every other category (ADDRESS, GOVERNMENT_ID, FINANCIAL, LOCATION, DOB,
      CREDENTIAL): unchanged, permitted on any type.
    """

    if category in {PIICategory.EMAIL, PIICategory.NAME, PIICategory.FREE_TEXT}:
        return _is_string_type(data_type)
    if category is PIICategory.PHONE:
        upper = data_type.upper()
        return not any(h in upper for h in _BOOLEAN_HINTS + _TEMPORAL_HINTS)
    return True


# Aggregate-name suffixes. A non-string column whose name ends in one of these
# is a derived statistic (a COUNT/SUM/AVG), not a value, so it is suppressed even
# for categories the type gate above still permits on numbers (ssn_count,
# zip_count, salary_sum). The conjunction with non-string type is what keeps this
# safe: a string column named `email_count` is odd enough to stay flagged.
#
# Deliberately excluded, and do not "complete" this list: `_num` (phone_num,
# account_num, cc_num are values, and cc_num is already a FINANCIAL token) and
# `_total` (salary_total reads as a value as readily as an aggregate).
_AGGREGATE_SUFFIXES = ("_count", "_cnt", "_sum", "_avg", "_pct", "_ratio")


def _is_derived_statistic(name: str, data_type: str) -> bool:
    """A non-string column whose normalized name ends in an aggregate suffix."""

    return not _is_string_type(data_type) and name.endswith(_AGGREGATE_SUFFIXES)


def detect_pii(column_name: str, data_type: str) -> PIIFlag | None:
    """Classify a column as PII from its name (and, loosely, type). Never a value.

    Returns the first matching category with a base confidence; aggregate signals
    refine the confidence later in :func:`_refine_confidence`. Each match is
    gated by :func:`_type_permits` (a category impossible on the column's type is
    skipped) and :func:`_is_derived_statistic` (an aggregate-suffixed numeric
    column is a count, not a value); the weaker generic-name and free-text
    patterns remain string-only.
    """

    flag, _generic = classify_pii(column_name, data_type)
    return flag


def classify_pii(column_name: str, data_type: str) -> tuple[PIIFlag | None, bool]:
    """The detector plus a marker for the generic `*_name` match.

    The marker says the flag is **provisional**: it is the one match
    :func:`profile` refines against value shape, up to person-name confidence or
    down to reference-vocabulary confidence. A caller that cannot run that
    refinement (nothing that reads names without values can) needs to know it is
    holding an unrefined guess rather than a verdict, which is why this is
    public and :func:`detect_pii` is the front door for callers that do not
    care. It is deliberately a return value and never persisted: keying
    eligibility on the flag itself (e.g. its confidence value) would couple the
    shape rules to the base-confidence table.
    """

    name = _normalize(column_name)
    for pattern, category, confidence in _PII_PATTERNS:
        if pattern.search(name):
            # A type-forbidden or derived-statistic match continues past this
            # pattern rather than returning None, so a later pattern still gets a
            # fair look (e.g. `phone_count` on a numeric column short-circuits
            # nothing).
            if not _type_permits(category, data_type):
                continue
            if _is_derived_statistic(name, data_type):
                continue
            return PIIFlag(category=category, confidence=confidence), False

    if _is_string_type(data_type):
        match = _GENERIC_NAME.search(name)
        if match is not None:
            qualifier = name[: match.start()].rstrip("_").rsplit("_", 1)[-1]
            if qualifier not in _NONPERSON_NAME_QUALIFIERS:
                flag = PIIFlag(
                    category=PIICategory.NAME, confidence=_GENERIC_NAME_CONFIDENCE
                )
                return flag, True
        if _FREE_TEXT.search(name):
            return PIIFlag(category=PIICategory.FREE_TEXT, confidence=0.5), False
    return None, False


def is_min_max_safe(data_type: str, pii: PIIFlag | None) -> bool:
    """min/max may be surfaced only for non-PII numeric / temporal / boolean
    columns. A min/max of a string is a raw value; a min/max of a PII column is a
    sensitive value. Both are suppressed."""

    if pii is not None:
        return False
    upper = data_type.upper()
    return (
        any(h in upper for h in _NUMERIC_HINTS)
        or any(h in upper for h in _TEMPORAL_HINTS)
        or any(h in upper for h in _BOOLEAN_HINTS)
    )


# A detector reports only once it clears this fraction of non-null values,
# matching the codebase's general under-report-over-over-report posture: a
# false negative here just costs one more `explore query`.
_TYPE_CONTRADICTION_MATCH_THRESHOLD = 0.95


def _type_contradiction_notes(
    col_name: str, data_type: str, agg: ColumnAggregate | None
) -> list[str]:
    """Data-quality notes for a declared type that contradicts its column's
    content (#204): a string holding dates/timestamps, epoch-shaped numbers,
    or numeric-only text. Fractions, pattern/format names, and (for epoch) a
    translated calendar date only -- never a raw value. Fail closed: a
    missing fraction (not requested, ineligible type, PII-flagged, or the
    dialect could not) yields no note for that detector.
    """

    if agg is None:
        return []
    notes: list[str] = []

    for fraction, fmt in (
        (agg.iso_date_fraction, "%Y-%m-%d"),
        (agg.iso_datetime_fraction, "%Y-%m-%d %H:%M:%S"),
    ):
        if fraction is not None and fraction >= _TYPE_CONTRADICTION_MATCH_THRESHOLD:
            notes.append(_temporal_note(col_name, data_type, fraction, fmt))

    slash_note = _slash_date_note(col_name, data_type, agg)
    if slash_note is not None:
        notes.append(slash_note)

    if (
        agg.numeric_string_fraction is not None
        and agg.numeric_string_fraction >= _TYPE_CONTRADICTION_MATCH_THRESHOLD
    ):
        notes.append(_numeric_string_note(col_name, data_type, agg))

    epoch_note = _epoch_note(col_name, data_type, agg)
    if epoch_note is not None:
        notes.append(epoch_note)

    return notes


def _temporal_note(col_name: str, data_type: str, fraction: float, fmt: str) -> str:
    return (
        f"{col_name} is declared {data_type} but {fraction:.0%} of values match "
        f"the {fmt} date format; consider casting with that format"
    )


def _slash_date_note(col_name: str, data_type: str, agg: ColumnAggregate) -> str | None:
    """Disambiguate %m/%d/%Y from %d/%m/%Y, which share one regex shape.

    A component that exceeds 12 anywhere in the data is a logical proof that
    component cannot be a month -- not a confidence bar, a single
    counterexample is conclusive -- so `> 0` decides this, never the 0.95
    match threshold used everywhere else in this module.
    """

    date_frac = agg.slash_date_fraction or 0.0
    datetime_frac = agg.slash_datetime_fraction or 0.0
    total = date_frac + datetime_frac  # mutually exclusive shapes: sum is coverage
    if total < _TYPE_CONTRADICTION_MATCH_THRESHOLD:
        return None
    has_time = datetime_frac > date_frac

    first_over = (agg.slash_first_component_over_12_fraction or 0.0) > 0
    second_over = (agg.slash_second_component_over_12_fraction or 0.0) > 0
    if first_over and second_over:
        return None  # contradiction: no single format order fits every row

    def fmt(day_first: bool) -> str:
        base = "%d/%m/%Y" if day_first else "%m/%d/%Y"
        return f"{base} %H:%M:%S" if has_time else base

    if first_over:  # first component can't be a month (>12): must be the day
        return _temporal_note(col_name, data_type, total, fmt(day_first=True))
    if second_over:  # second component can't be a month: must be MDY
        return _temporal_note(col_name, data_type, total, fmt(day_first=False))

    # Neither component ever exceeds 12: the order cannot be told apart from
    # the data alone -- say so rather than guessing.
    return (
        f"{col_name} is declared {data_type} and {total:.0%} of values match a "
        f"slash-separated date shape, but the day/month order is ambiguous "
        f"({fmt(day_first=True)} and {fmt(day_first=False)} both fit every "
        "value); casting either way risks a silent swap"
    )


def _numeric_string_note(col_name: str, data_type: str, agg: ColumnAggregate) -> str:
    integer_fraction = agg.integer_string_fraction or 0.0
    if integer_fraction >= _TYPE_CONTRADICTION_MATCH_THRESHOLD:
        shape = "every value fits an integer"
    else:
        shape = (
            f"only {integer_fraction:.0%} of values fit an integer "
            "(the rest carry a decimal point)"
        )
    return (
        f"{col_name} is declared {data_type} but {agg.numeric_string_fraction:.0%} "
        f"of values are numeric-only strings; {shape}"
    )


def _epoch_note(col_name: str, data_type: str, agg: ColumnAggregate) -> str | None:
    seconds = agg.epoch_seconds_fraction or 0.0
    millis = agg.epoch_millis_fraction or 0.0
    if (
        seconds < _TYPE_CONTRADICTION_MATCH_THRESHOLD
        and millis < _TYPE_CONTRADICTION_MATCH_THRESHOLD
    ):
        return None
    if seconds >= millis:
        unit, fraction = "seconds", seconds
        lo, hi = agg.epoch_seconds_min_value, agg.epoch_seconds_max_value
    else:
        unit, fraction = "milliseconds", millis
        lo, hi = agg.epoch_millis_min_value, agg.epoch_millis_max_value
    if lo is None or hi is None:
        return None  # evidence incomplete: fail closed, no unverifiable claim

    divisor = 1000 if unit == "milliseconds" else 1
    lo_date = datetime.fromtimestamp(lo / divisor, tz=UTC).date().isoformat()
    hi_date = datetime.fromtimestamp(hi / divisor, tz=UTC).date().isoformat()
    return (
        f"{col_name} is declared {data_type} but {fraction:.0%} of values fall "
        f"in the plausible unix-epoch-{unit} range; implied date range "
        f"{lo_date} to {hi_date}"
    )


# Below this, a second shape reads as a handful of typos/outliers, not a
# second real population; the issue's own worked example floors here at 10%,
# so 5% catches a genuine minority scheme with margin to spare while staying
# on this codebase's side of under- rather over-reporting.
_HETEROGENEOUS_KEY_MIN_SHARE = 0.05

# Friendly names for the hash lengths this shape recurs as in practice;
# anything else is reported by its bare length instead of guessing further.
_HEX_LENGTH_NAMES = {32: "md5", 40: "sha1", 64: "sha256"}


def _is_candidate_key(
    col_name: str, agg: ColumnAggregate | None, composite_members: set[str]
) -> bool:
    """A single-column proven key, or a member of a proven composite key --
    the same predicate `relationships.candidate_keys` applies to
    `ColumnProfile` after the fact, evaluated here instead against the live
    aggregate because that's the only place `key_shape` fractions exist.
    """

    if col_name in composite_members:
        return True
    if agg is None:
        return False
    return bool(agg.is_unique) and agg.null_fraction in (0.0, None)


def _hex_shape_label(agg: ColumnAggregate) -> str:
    lo, hi = agg.hex_string_min_length, agg.hex_string_max_length
    if lo is None or hi is None:
        return "hexadecimal"
    if lo == hi:
        name = _HEX_LENGTH_NAMES.get(lo)
        return (
            f"{lo}-character hexadecimal ({name}-shaped)"
            if name
            else f"{lo}-character hexadecimal"
        )
    return f"hexadecimal ({lo}-{hi} characters, mixed length)"


def _heterogeneous_key_note(col_name: str, agg: ColumnAggregate | None) -> str | None:
    """A candidate-key column that mixes two or more value shapes (numeric,
    UUID, fixed-length hex, or an unclassified remainder) in a meaningful
    share each -- the shape that survives a partial migration or a merged
    upstream, where a downstream cast or numeric comparison silently drops
    the group it can't parse. Fractions and a length-derived shape label
    only; never a value.
    """

    if agg is None:
        return None
    numeric = agg.numeric_string_fraction or 0.0
    uuid = agg.uuid_string_fraction or 0.0
    hexed = agg.hex_string_fraction or 0.0
    other = max(0.0, 1.0 - numeric - uuid - hexed)
    buckets = [
        ("numeric", numeric),
        ("uuid", uuid),
        (_hex_shape_label(agg), hexed),
        ("other", other),
    ]
    present = [
        (name, frac) for name, frac in buckets if frac >= _HETEROGENEOUS_KEY_MIN_SHARE
    ]
    if len(present) < 2:
        return None
    parts = ", ".join(f"{frac:.0%} {name}" for name, frac in present)
    return (
        f"{col_name} is a candidate key but mixes value shapes: {parts}; "
        "casting to a number or comparing numerically will silently drop "
        "the non-numeric group(s)"
    )


def _boolean_shaped_flag_note(
    col_name: str,
    data_type: str,
    agg: ColumnAggregate | None,
    domain: ValueDomain | None,
) -> str | None:
    """A column named like a two-valued flag (``is_*``, ``has_*``, ``*_flag``,
    ``*_yn``, ``*_ind``) whose non-null content holds more than two distinct
    values -- a mixed-encoding defect invisible to anyone who does not ask for
    the domain (issue #218). Firing does not require telling a data-quality
    defect apart from a genuine third state; the tool cannot make that call,
    and naming the count (and the values, when known) is what lets the caller
    make it instead.

    A genuinely ``BOOLEAN``-typed column can only ever hold two values (plus
    null) by construction and is excluded outright, whatever its name.

    Where the value domain (#203) is available, the note names the
    encodings and their counts. Where it is not, because the column failed
    one of that feature's own eligibility gates (PII-flagged, a candidate
    key, or too many distinct values to report), the note still fires on the
    count alone: the tool not being able to name the values is not evidence
    there are only two of them.
    """

    if not _BOOLEAN_NAME.search(_normalize(col_name)):
        return None
    if any(hint in data_type.upper() for hint in _BOOLEAN_HINTS):
        return None
    if agg is None or agg.distinct_count is None or agg.distinct_count <= 2:
        return None

    if domain is not None and domain.values:
        total = len(domain.values) + domain.elided
        encodings = ", ".join(f"{v.value}={v.count}" for v in domain.values)
        if domain.elided:
            encodings += f", +{domain.elided} more"
        return (
            f"{col_name} looks boolean-shaped by name but holds {total} "
            f"distinct non-null values: {encodings}; a two-valued read of "
            "it will misclassify some rows"
        )
    marker = "" if agg.distinct_count_exact else "~"
    return (
        f"{col_name} looks boolean-shaped by name but holds "
        f"{marker}{agg.distinct_count} distinct non-null values; a "
        "two-valued read of it will misclassify some rows"
    )


def _parse_temporal(value: object) -> date | datetime:
    """Normalize a temporal min/max back to a comparable object. Adapters
    disagree about when they call ``json_safe`` on these two fields (see the
    module docstring's note on #206): DuckDB/BigQuery hand back a native
    ``date``/``datetime``, Snowflake/Databricks/Redshift/Postgres an ISO
    8601 ``str``. Both must resolve to the same shape before date
    arithmetic, since subtracting a ``date`` from a ``datetime`` raises."""

    if isinstance(value, str):
        return (
            datetime.fromisoformat(value)
            if ("T" in value or " " in value)
            else date.fromisoformat(value)
        )
    return value  # already date or datetime


def _temporal_granularity(agg: ColumnAggregate) -> str:
    """Day is the default; month wins only when the data is clearly at that
    grain (values sit on month boundaries), and hour is reported instead of
    day when day-truncation would actually lose information (real
    sub-day variation, so a bare DATE column -- always day-aligned by
    construction -- never lands here)."""

    day_aligned = agg.day_aligned_fraction or 0.0
    month_aligned = agg.month_aligned_fraction or 0.0
    if day_aligned < _TYPE_CONTRADICTION_MATCH_THRESHOLD:
        return "hour"
    if month_aligned >= _TYPE_CONTRADICTION_MATCH_THRESHOLD:
        return "month"
    return "day"


def _temporal_continuity(
    agg: ColumnAggregate | None,
) -> tuple[str, int, int, int, int] | None:
    """Span, distinct periods, missing periods, and the largest gap, at the
    detected granularity. Neutral by design: a genuinely sparse column (an
    event timestamp on a rare event) reports large numbers here without
    being characterized as broken -- interpretation belongs to the caller,
    including a future drift-sweep detector this only lands the statistics
    for (issue #226, not built here). ``None`` when there isn't enough
    evidence (no aggregate, no min/max, or the adapter could not compute
    the granularity-matched pair)."""

    if agg is None or agg.min_value is None or agg.max_value is None:
        return None
    # Not a temporal-eligible column at all (no alignment evidence was ever
    # requested for it): min/max exist but are not dates, so there is
    # nothing to do date arithmetic on.
    if agg.day_aligned_fraction is None and agg.month_aligned_fraction is None:
        return None
    granularity = _temporal_granularity(agg)
    lo, hi = _parse_temporal(agg.min_value), _parse_temporal(agg.max_value)
    if granularity == "month":
        span = (hi.year - lo.year) * 12 + (hi.month - lo.month) + 1
        distinct_periods = agg.month_distinct_periods
        largest_gap = agg.month_largest_gap
    elif granularity == "hour":
        span = int((hi - lo).total_seconds() // 3600) + 1
        distinct_periods = agg.hour_distinct_periods
        largest_gap = agg.hour_largest_gap
    else:
        span = (hi - lo).days + 1
        distinct_periods = agg.day_distinct_periods
        largest_gap = agg.day_largest_gap
    if distinct_periods is None:
        return None
    missing = max(0, span - distinct_periods)
    return granularity, span, distinct_periods, missing, largest_gap or 0


def profile(
    adapter: Adapter,
    identifiers: list[str],
    *,
    progress: ProgressReporter | None = None,
    on_complete: Callable[[Dataset], None] | None = None,
    pii_overrides: PIIOverrideMatcher | None = None,
    include_blobs: set[str] | None = None,
) -> list[Dataset]:
    """Profile each object into a Dataset of aggregate-derived ColumnProfiles.

    ``progress``, when supplied, is advanced once per profiled object so a long
    run emits periodic ``profiled N/M objects`` lines to stderr.

    ``on_complete`` is invoked with each raw Dataset as soon as it is fully
    profiled, so callers can checkpoint budget-paid work before a later object's
    cost gate can abort the run. It fires *after* the object is appended and only
    for fully-profiled objects, never a half-scanned one.

    ``pii_overrides`` holds lowered ``identifier.column`` paths a human has
    reviewed as not PII (from `.dex/config.yml`). An overridden column's flag is
    suppressed at the source, before min/max safety and shape-stat eligibility
    are decided, and the suppressed category is recorded on the profile as the
    audit trail. Re-applied on every profile, which is what makes the override
    durable: the cache is overwritten wholesale by re-profiling.

    ``include_blobs`` holds lowered ``identifier.column`` paths a human has opted
    back into profiling despite being blob-typed (from `.dex/config.yml`). Blob
    columns (BYTES/BLOB/bytea/BINARY, scalar or repeated) are excluded from the
    aggregate scan by default: their profile can only ever be a null fraction
    and a distinct estimate, yet a columnar engine bills for the whole column
    once it is referenced at all, so scanning them by default trades most of a
    table's scan cost for stats that rarely inform a decision.
    """

    override_paths = pii_overrides or set()
    blob_paths = include_blobs or set()
    datasets: list[Dataset] = []
    for identifier in identifiers:
        meta, columns = adapter.table_metadata(identifier)

        # Decide min/max safety BEFORE querying, from name + type, so the adapter
        # never even computes a suppressed extreme.
        classified = {c.name: classify_pii(c.name, c.data_type) for c in columns}
        prelim_pii = {name: flag for name, (flag, _g) in classified.items()}
        overridden: dict[str, PIICategory] = {}
        for c in columns:
            flag = prelim_pii[c.name]
            if flag is not None and f"{identifier}.{c.name}".lower() in override_paths:
                overridden[c.name] = flag.category
                prelim_pii[c.name] = None
        safe = {
            c.name for c in columns if is_min_max_safe(c.data_type, prelim_pii[c.name])
        }
        # Shape evidence is bought only for generic-name flags that still stand:
        # exact person tokens need no corroboration, and a human-cleared column
        # deserves no scan spend.
        shape = {
            name
            for name, (flag, generic) in classified.items()
            if generic and prelim_pii[name] is not None
        }
        # Declared-type-vs-content checks (#204) run only for non-PII
        # string/integer columns: PII at any confidence is excluded, full
        # stop, the same rule is_min_max_safe and value-domain eligibility
        # already use.
        type_stats = {
            c.name
            for c in columns
            if prelim_pii[c.name] is None
            and (_is_string_type(c.data_type) or is_integer_type(c.data_type))
        }
        # Heterogeneous-key-shape checks (#205) are string-only, unlike
        # type_stats above: a UUID/hex check is meaningless on a column SQL
        # already stores as a number.
        key_shape_stats = {
            c.name
            for c in columns
            if prelim_pii[c.name] is None and _is_string_type(c.data_type)
        }
        # Temporal-continuity checks (#206): non-PII date/timestamp columns
        # only, same eligibility rule as above.
        temporal_stats = {
            c.name
            for c in columns
            if prelim_pii[c.name] is None and is_temporal_type(c.data_type)
        }

        # Blob-type columns can only ever yield a null fraction and a distinct
        # estimate, yet a columnar engine bills for the whole column once it is
        # referenced at all, so they are dropped from the scan before it is
        # built (unless a human opted a specific one back in via config).
        blob_excluded = [
            c.name
            for c in columns
            if is_blob_type(c.data_type)
            and f"{identifier}.{c.name}".lower() not in blob_paths
        ]
        scan_columns = [c for c in columns if c.name not in blob_excluded]

        # The adapter knows what the server said and this loop knows which
        # object it said it about, so a server-side refusal picks up the
        # identifier on its way past: an envelope reading "the warehouse
        # refused a statement dex built" is only actionable when it names the
        # object, and a map run has a dozen of them in flight.
        try:
            aggregates = {
                a.name: a
                for a in adapter.column_aggregates(
                    identifier,
                    scan_columns,
                    safe_min_max=safe,
                    shape_stats=shape,
                    type_stats=type_stats,
                    key_shape_stats=key_shape_stats,
                    temporal_stats=temporal_stats,
                )
            }
            # Re-read the metadata after the aggregate scan: adapters whose
            # inventory row counts are planner estimates (Postgres reltuples)
            # upgrade to the exact COUNT(*) the scan just paid for, so
            # uniqueness proofs and the dataset row count are exact. Free
            # everywhere (cached or trivially cheap on the other adapters).
            meta, _ = adapter.table_metadata(identifier)
            aggregates = _escalate_near_unique(
                adapter, identifier, meta.row_count, aggregates
            )
            composite_keys = _probe_composite_keys(
                adapter, identifier, meta.row_count, aggregates
            )
            value_domains = _probe_value_domains(
                adapter,
                identifier,
                meta.row_count,
                aggregates,
                prelim_pii,
                composite_keys,
            )
        except WarehouseQueryError as exc:
            raise exc.naming(identifier) from exc
        composite_members = {c for pair in composite_keys for c in pair}

        profiles: list[ColumnProfile] = []
        data_quality: list[str] = []
        if meta.row_count == 0:
            data_quality.append("empty table (no rows)")
        if blob_excluded:
            data_quality.append(
                f"excluded {len(blob_excluded)} blob-type column(s) from "
                f"profiling scan ({', '.join(sorted(blob_excluded))}): "
                "BYTES/BLOB-shaped columns only ever yield a null fraction and "
                "a distinct estimate, and a columnar engine bills for the whole "
                "column once referenced; add a blob_overrides entry in "
                ".dex/config.yml to include one"
            )

        # Adapters that degrade a profile (partition-filter tables, block
        # sampling, skipped escalations) explain themselves through this
        # duck-typed hook, so the limitation reads as a data-quality note
        # instead of silently thinner numbers.
        notes_for = getattr(adapter, "table_notes", None)
        if notes_for is not None:
            data_quality.extend(notes_for(identifier))

        for col in columns:
            agg = aggregates.get(col.name)
            pii = _refine_confidence(
                prelim_pii[col.name],
                agg,
                generic=col.name in shape,
                row_count=meta.row_count,
            )
            if prelim_pii[col.name] is None:
                data_quality.extend(
                    _type_contradiction_notes(col.name, col.data_type, agg)
                )
                if _is_candidate_key(col.name, agg, composite_members):
                    key_note = _heterogeneous_key_note(col.name, agg)
                    if key_note is not None:
                        data_quality.append(key_note)
                flag_note = _boolean_shaped_flag_note(
                    col.name, col.data_type, agg, value_domains.get(col.name)
                )
                if flag_note is not None:
                    data_quality.append(flag_note)
            continuity = (
                _temporal_continuity(agg) if col.name in temporal_stats else None
            )
            profiles.append(
                ColumnProfile(
                    name=col.name,
                    data_type=col.data_type,
                    nullable=col.nullable,
                    null_fraction=agg.null_fraction if agg else None,
                    distinct_count=agg.distinct_count if agg else None,
                    distinct_count_exact=agg.distinct_count_exact if agg else False,
                    is_unique=agg.is_unique if agg else None,
                    min_value=json_safe(agg.min_value) if agg else None,
                    max_value=json_safe(agg.max_value) if agg else None,
                    pii=pii,
                    pii_overridden=overridden.get(col.name),
                    value_domain=value_domains.get(col.name),
                    temporal_granularity=continuity[0] if continuity else None,
                    temporal_span=continuity[1] if continuity else None,
                    temporal_distinct_periods=continuity[2] if continuity else None,
                    temporal_missing_periods=continuity[3] if continuity else None,
                    temporal_largest_gap=continuity[4] if continuity else None,
                )
            )

        ds = Dataset(
            identifier=identifier,
            object_type=meta.object_type,
            row_count=meta.row_count,
            byte_size=meta.byte_size,
            columns=profiles,
            composite_keys=composite_keys,
            data_quality=data_quality,
            profiled_at=datetime.now(UTC).isoformat(),
        )
        if progress is not None:
            progress.advance()
        datasets.append(ds)
        if on_complete is not None:
            on_complete(ds)
    return datasets


def _escalate_near_unique(
    adapter: Adapter,
    identifier: str,
    row_count: int | None,
    aggregates: dict[str, ColumnAggregate],
) -> dict[str, ColumnAggregate]:
    """Replace approximate distinct counts with exact ones on columns that look
    unique within approximation noise, so uniqueness verdicts (and the grain and
    data-quality notes derived from them) rest on proof, not on HLL error.

    Bounded: at most ``_EXACT_DISTINCT_CAP`` columns per table (the smallest
    approx-to-non-null gaps win), all in one batched adapter call. An adapter
    without ``exact_distinct_counts`` degrades to the approximate signals.
    """

    if not row_count:
        return aggregates
    exact_counts = getattr(adapter, "exact_distinct_counts", None)
    if exact_counts is None:
        return aggregates

    candidates: list[tuple[int, str, int]] = []
    for agg in aggregates.values():
        if agg.distinct_count is None or agg.distinct_count_exact:
            continue
        non_null = (
            round((1 - agg.null_fraction) * row_count)
            if agg.null_fraction is not None
            else row_count
        )
        if non_null <= 0:
            continue
        if agg.distinct_count >= NEAR_UNIQUE_RATIO * non_null:
            gap = abs(non_null - agg.distinct_count)
            candidates.append((gap, agg.name, non_null))
    if not candidates:
        return aggregates

    candidates.sort(key=lambda c: c[0])
    chosen = candidates[:_EXACT_DISTINCT_CAP]
    exact = exact_counts(identifier, [name for _gap, name, _nn in chosen])

    escalated = dict(aggregates)
    for _gap, name, non_null in chosen:
        if name not in exact:
            continue
        escalated[name] = replace(
            aggregates[name],
            distinct_count=exact[name],
            is_unique=(exact[name] == non_null == row_count),
            distinct_count_exact=True,
        )
    return escalated


def _probe_composite_keys(
    adapter: Adapter,
    identifier: str,
    row_count: int | None,
    aggregates: dict[str, ColumnAggregate],
) -> list[list[str]]:
    """Prove 2-column keys on tables where no single column is one: the shape
    of a fact table, whose grain is exactly what a profile must answer.

    A pair can only be a key if the product of its members' distinct counts
    reaches the row count, so pairs are pruned on that necessary condition
    (relaxed per approximate member to absorb HLL undershoot) and ranked:
    id-shaped members first (real grains are key-shaped), smallest product
    next (a minimal grain sits just above the row count; a pair of two
    near-unique columns lands near rows squared and is analytically useless
    even when technically unique).

    A pair sharing a column with a better-ranked one at a product within
    ``_COMPOSITE_REDUNDANCY_RATIO`` of it is the same hypothesis tried with
    different filler, so it goes behind every pair that is not. **Behind, not
    away.** That preference is only worth anything while the cap binds, and
    discarding a ranked candidate with slots still free buys nothing and can
    cost the grain outright: on a fact table the pair a two-id parent pair
    blocks is exactly the parent-plus-line grain, which is the one shape this
    probe exists to find. So the cap is filled from the preferred pairs first
    and the demoted ones after, and the survivors go back into rank order.

    Bounded to ``_COMPOSITE_PAIR_CAP`` pairs in one batched adapter call; a
    pair is proven when its exact combination count equals the row count.
    Proven pairs come back smallest product first, which is the minimal grain
    among them: once a pair is proven, id-shapedness has nothing left to say,
    and a superkey of two id columns that happen to be unique together must not
    outrank the tighter key, because callers read the first entry as the grain.
    Adapters without ``distinct_combination_counts`` degrade to no composite
    keys, and one that narrows the probe to what its budget covers leaves the
    pairs it dropped simply unproven.
    """

    if not row_count:
        return []
    combo_counts = getattr(adapter, "distinct_combination_counts", None)
    if combo_counts is None:
        return []
    for agg in aggregates.values():
        if agg.is_unique and agg.null_fraction in (0.0, None):
            return []  # a proven single-column key makes the probe waste

    pool = [
        agg
        for agg in aggregates.values()
        if agg.distinct_count and not agg.is_unique and agg.null_fraction in (0.0, None)
    ]
    if len(pool) < 2:
        return []

    # Imported here: relationships imports NEAR_UNIQUE_RATIO from this module,
    # so a module-level import would be circular.
    from .relationships import is_id_shaped

    ranked: list[tuple[int, int, tuple[str, str]]] = []
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            product = a.distinct_count * b.distinct_count
            n_approx = sum(1 for m in (a, b) if not m.distinct_count_exact)
            if product < row_count * NEAR_UNIQUE_RATIO**n_approx:
                continue
            id_shaped = sum(1 for m in (a, b) if is_id_shaped(m.name))
            # Members ordered by descending cardinality so the key reads
            # parent-then-line, e.g. (L_ORDERKEY, L_LINENUMBER).
            members = sorted(
                (a.name, b.name),
                key=lambda n: (-(aggregates[n].distinct_count or 0), n),
            )
            ranked.append((-id_shaped, product, (members[0], members[1])))
    if not ranked:
        return []

    ranked.sort()
    preferred: list[tuple[int, int, tuple[str, str]]] = []
    demoted: list[tuple[int, int, tuple[str, str]]] = []
    best_product_for: dict[str, int] = {}
    for entry in ranked:
        _ids, product, pair = entry
        if any(
            col in best_product_for
            and product <= best_product_for[col] * _COMPOSITE_REDUNDANCY_RATIO
            for col in pair
        ):
            demoted.append(entry)  # same anchor, interchangeable filler
            continue
        preferred.append(entry)
        for col in pair:
            best_product_for.setdefault(col, product)

    selected = sorted((preferred + demoted)[:_COMPOSITE_PAIR_CAP])
    exact = combo_counts(identifier, [list(pair) for _ids, _product, pair in selected])
    proven = sorted(
        (product, pair)
        for _ids, product, pair in selected
        if exact.get(pair) == row_count
    )
    return [list(pair) for _product, pair in proven]


def _probe_value_domains(
    adapter: Adapter,
    identifier: str,
    row_count: int | None,
    aggregates: dict[str, ColumnAggregate],
    prelim_pii: dict[str, PIIFlag | None],
    composite_keys: list[list[str]],
) -> dict[str, ValueDomain]:
    """Report the value domain of a column with no PII flag at any
    confidence, a distinct count that clears both the absolute cap and the
    row-relative fraction, and that is not a candidate key -- so a caller
    can write a correct CASE expression without a raw SQL client, the one
    place the PII policy and query firewall don't apply.

    Eligibility is decided from the approximate pre-scan distinct count (cost
    control: an obviously-too-wide column is never even queried), but the
    reported domain is built from the exact count the probe itself returns.
    When HLL under-estimated and the true count is still within the
    row-relative fraction, the result is capped to the top values by
    frequency with the remainder counted as ``elided`` rather than dropped;
    only a true count that breaks the fraction bar (a near-key column the
    approximation missed) is dropped entirely.

    ``prelim_pii``, not the post-``_refine_confidence`` flag: refinement
    only adjusts confidence, never presence, so the two are equivalent for
    an any-confidence presence check, and ``prelim_pii`` is already computed
    before the aggregate scan even runs.
    """

    domain_fn = getattr(adapter, "value_domain_counts", None)
    if domain_fn is None or not row_count:
        return {}

    composite_members = {c for pair in composite_keys for c in pair}
    eligible = []
    for name, agg in aggregates.items():
        if prelim_pii.get(name) is not None:
            continue  # flagged at any confidence: never a domain
        if name in composite_members:
            continue
        if agg.is_unique and agg.null_fraction in (0.0, None):
            continue  # single-column key
        if not agg.distinct_count or agg.distinct_count > VALUE_DOMAIN_CAP:
            continue
        non_null = row_count * (1 - (agg.null_fraction or 0.0))
        if non_null <= 0 or agg.distinct_count > non_null * VALUE_DOMAIN_MAX_FRACTION:
            continue
        eligible.append(name)
    if not eligible:
        return {}

    samples = domain_fn(identifier, eligible, limit=VALUE_DOMAIN_CAP)
    domains: dict[str, ValueDomain] = {}
    for name, sample in samples.items():
        agg = aggregates[name]
        non_null = row_count * (1 - (agg.null_fraction or 0.0))
        if (
            non_null <= 0
            or sample.total_distinct > non_null * VALUE_DOMAIN_MAX_FRACTION
        ):
            # The approximate pre-check under-estimated the true fraction:
            # this is really a near-key column, so report no domain at all
            # rather than a capped slice of one.
            continue
        # Sorted here, not trusted from the adapter: not every dialect's
        # array-aggregate reliably preserves the inner subquery's ORDER BY
        # once it's collected into a single value, so frequency order is
        # guaranteed at this one point instead of per-dialect.
        ordered = sorted(sample.values, key=lambda pair: pair[1], reverse=True)
        domains[name] = ValueDomain(
            values=[ValueCount(value=json_safe(v), count=c) for v, c in ordered],
            elided=max(0, sample.total_distinct - len(sample.values)),
        )
    return domains


def _refine_confidence(
    pii: PIIFlag | None,
    aggregate: ColumnAggregate | None,
    *,
    generic: bool = False,
    row_count: int | None = None,
) -> PIIFlag | None:
    """Nudge PII confidence using aggregate signals (never raw values).

    ``generic`` marks the flag as the generic `*_name` match, the only one whose
    confidence the value-shape rules may move. The flag itself is never removed:
    a de-rated flag stays recorded at reference-data confidence, and what to do
    with a weak flag is the consumer's decision (the query firewall blocks at
    its threshold; min/max suppression and dbt meta stay presence-based).
    ``row_count`` backs the closed-enumeration shape rule; ``None`` (unknown
    row count) simply leaves that rule unable to fire, same as any other
    missing evidence.
    """

    if pii is None or aggregate is None:
        return pii
    confidence = pii.confidence
    # A near-unique column strengthens identity-like categories (emails, ids).
    if aggregate.is_unique and pii.category in {
        PIICategory.EMAIL,
        PIICategory.GOVERNMENT_ID,
        PIICategory.FINANCIAL,
    }:
        confidence = min(_MAX_CONFIDENCE, confidence + 0.05)
    # Very low cardinality on a location/address name reads as reference data.
    if (
        aggregate.distinct_count is not None
        and aggregate.distinct_count <= 5
        and pii.category in {PIICategory.LOCATION, PIICategory.ADDRESS}
    ):
        confidence = max(0.1, confidence - 0.3)
    if generic and pii.category is PIICategory.NAME:
        confidence = _shape_verdict(confidence, aggregate, row_count)
    return PIIFlag(category=pii.category, confidence=round(confidence, 4))


def _shape_verdict(
    confidence: float, aggregate: ColumnAggregate, row_count: int | None = None
) -> float:
    """Map value-shape evidence to a generic-name confidence.

    Fail closed: whenever the evidence is missing or ambiguous the name-derived
    confidence stands unchanged, which keeps the column blocked. Only the
    provably non-person shapes de-rate, and a person-shaped distribution
    corroborates up to the exact-token level.
    """

    person = aggregate.person_shape_fraction
    if person is None:
        return confidence
    if person >= _PERSON_FRACTION_RAISE:
        return _SHAPE_PERSON_CONFIDENCE
    if person > _PERSON_FRACTION_ABSENT:
        return confidence
    upper = aggregate.upper_vocab_fraction
    if (
        upper is not None
        and upper >= _UPPER_VOCAB_FRACTION
        and aggregate.distinct_count is not None
        and aggregate.distinct_count <= _UPPER_VOCAB_MAX_DISTINCT
    ):
        return _SHAPE_REFERENCE_CONFIDENCE
    tokens = aggregate.avg_token_count
    if tokens is not None and tokens >= _LABEL_AVG_TOKENS:
        return _SHAPE_REFERENCE_CONFIDENCE
    if _is_closed_enumeration(aggregate, row_count):
        return _SHAPE_REFERENCE_CONFIDENCE
    return confidence


def _is_closed_enumeration(aggregate: ColumnAggregate, row_count: int | None) -> bool:
    """A small closed set of repeated values (day/month names, status codes,
    plan tiers): distinct count small in absolute terms and, more importantly,
    small as a fraction of non-null rows. The fraction is what keeps a
    genuinely small table of distinct people from clearing here: few rows,
    each a different name, is a small distinct count but a high fraction."""

    distinct = aggregate.distinct_count
    if distinct is None or distinct > _ENUM_MAX_DISTINCT or not row_count:
        return False
    non_null = (
        round((1 - aggregate.null_fraction) * row_count)
        if aggregate.null_fraction is not None
        else row_count
    )
    if not non_null:
        return False
    return (distinct / non_null) <= _ENUM_MAX_DISTINCT_FRACTION
