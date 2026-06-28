"""Explore: column profiling and PII detection.

Understanding is built from SQL aggregates, never from raw rows in context
(Principle 2). PII is recorded as (column, category, confidence) with no example
value (Principle 6), and min/max are surfaced only for columns where the extreme
value is not itself sensitive: numeric and temporal types that carry no PII flag.
For any string column, or any column flagged PII, min/max are suppressed at the
source so the value never leaves the engine.

A categorical sketch (the most-frequent values with counts) is surfaced for short,
low-cardinality, non-PII text columns so the agent can see a column's actual
categories without raw rows. Value frequencies are aggregates, not rows; the gate
is layered (PII flag, a stricter deny-list, a value-length cap, a cardinality cap)
so it never rests on name-based detection alone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal

from ..adapters.base import Adapter, ColumnAggregate
from ..cache import ColumnProfile, Dataset, PIICategory, PIIFlag, ValueCount

# Name patterns mapped to a PII category and a base confidence. Matched on the
# lowercased column name with word-ish boundaries so "email" hits but "emailable"
# does not over-trigger. Detection never inspects values, only names and types.
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

_NUMERIC_HINTS = ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "HUGEINT")
_TEMPORAL_HINTS = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
_BOOLEAN_HINTS = ("BOOL",)

_MAX_CONFIDENCE = 0.95

# Value sketching surfaces real cell values (aggregated), so it is gated harder
# than min/max. A column qualifies only if non-PII, not on this deny-list, short-
# valued, and low-cardinality. The deny-list is deliberately broader and blunter
# (substring match) than the PII patterns: a missed name here surfaces up to K real
# values, not one, so it also catches special-category data (gender, ethnicity,
# religion, health) and free-text-ish names (note, comment, description) that the
# PII patterns do not, plus bare identity names ("name") the PII patterns require
# a prefix for.
_SKETCH_DENY = re.compile(
    r"name|email|phone|address|gender|sex|ethnic|race|religi|diagnos|marital|"
    r"national|citizen|disab|orientation|politic|biometric|health|medical|patient|"
    r"comment|note|desc|reason|remark|message|body|content|summary|title|subject",
    re.IGNORECASE,
)
# A column reads as categorical only below this many distinct values; above it, the
# sketch is neither useful nor bounded.
_CATEGORICAL_MAX_DISTINCT = 50
# Values longer than this read as free text (and a likely PII carrier), never a
# category. A structural, name-independent backstop to the deny-list.
_MAX_CATEGORICAL_VALUE_LEN = 64


def detect_pii(column_name: str, data_type: str) -> PIIFlag | None:
    """Classify a column as PII from its name (and, loosely, type). Never a value.

    Returns the first matching category with a base confidence; aggregate signals
    refine the confidence later in :func:`_refine_confidence`.
    """

    name = column_name.lower()
    for pattern, category, confidence in _PII_PATTERNS:
        if pattern.search(name):
            return PIIFlag(category=category, confidence=confidence)
    return None


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


def is_sketch_name_safe(column_name: str, pii: PIIFlag | None) -> bool:
    """Name/PII half of the value-sketch gate: a column may be sketched only if it
    carries no PII flag and its name is not on the stricter sketch deny-list. The
    cardinality, length, and type checks are applied separately from the aggregate
    results (text columns carry a ``max_length``; non-text columns do not)."""

    if pii is not None:
        return False
    return _SKETCH_DENY.search(column_name) is None


def profile(adapter: Adapter, identifiers: list[str]) -> list[Dataset]:
    """Profile each object into a Dataset of aggregate-derived ColumnProfiles."""

    datasets: list[Dataset] = []
    for identifier in identifiers:
        meta, columns = adapter.table_metadata(identifier)

        # Decide min/max safety BEFORE querying, from name + type, so the adapter
        # never even computes a suppressed extreme.
        prelim_pii = {c.name: detect_pii(c.name, c.data_type) for c in columns}
        safe = {
            c.name for c in columns if is_min_max_safe(c.data_type, prelim_pii[c.name])
        }

        aggregates = {
            a.name: a
            for a in adapter.column_aggregates(identifier, columns, safe_min_max=safe)
        }

        # Second pass: value-sketch only columns that pass every gate. Eligibility
        # needs distinct_count and max_length (known only after the aggregate pass),
        # so this cannot fold into the first pass. max_length being present means the
        # adapter classified the column as text (the type half of the gate).
        sketch_cols = [
            c
            for c in columns
            if is_sketch_name_safe(c.name, prelim_pii[c.name])
            and (agg := aggregates.get(c.name)) is not None
            and agg.max_length is not None
            and agg.max_length <= _MAX_CATEGORICAL_VALUE_LEN
            and agg.distinct_count is not None
            and 1 <= agg.distinct_count <= _CATEGORICAL_MAX_DISTINCT
        ]
        sketches = (
            adapter.column_top_values(
                identifier, sketch_cols, k=_CATEGORICAL_MAX_DISTINCT
            )
            if sketch_cols
            else {}
        )

        profiles: list[ColumnProfile] = []
        data_quality: list[str] = []
        if meta.row_count == 0:
            data_quality.append("empty table (no rows)")

        for col in columns:
            agg = aggregates.get(col.name)
            pii = _refine_confidence(prelim_pii[col.name], agg)
            sketch = sketches.get(col.name)
            top_values = (
                [ValueCount(value=str(_json_safe(v)), count=c) for v, c in sketch]
                if sketch
                else None
            )
            profiles.append(
                ColumnProfile(
                    name=col.name,
                    data_type=col.data_type,
                    nullable=col.nullable,
                    null_fraction=agg.null_fraction if agg else None,
                    distinct_count=agg.distinct_count if agg else None,
                    is_unique=agg.is_unique if agg else None,
                    min_value=_json_safe(agg.min_value) if agg else None,
                    max_value=_json_safe(agg.max_value) if agg else None,
                    pii=pii,
                    top_values=top_values,
                )
            )

        datasets.append(
            Dataset(
                identifier=identifier,
                object_type=meta.object_type,
                row_count=meta.row_count,
                byte_size=meta.byte_size,
                columns=profiles,
                data_quality=data_quality,
            )
        )
    return datasets


def _refine_confidence(
    pii: PIIFlag | None, aggregate: ColumnAggregate | None
) -> PIIFlag | None:
    """Nudge PII confidence using aggregate signals (never raw values)."""

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
    return PIIFlag(category=pii.category, confidence=round(confidence, 4))


def _json_safe(value: object | None) -> object | None:
    """Coerce a DuckDB scalar to a JSON-serializable primitive for the envelope."""

    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value)
