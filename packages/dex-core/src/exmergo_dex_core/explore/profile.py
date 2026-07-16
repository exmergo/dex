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
from dataclasses import replace
from datetime import UTC, datetime

from ..adapters.base import Adapter, ColumnAggregate, json_safe
from ..cache import ColumnProfile, Dataset, PIICategory, PIIFlag
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
_COMPOSITE_PAIR_CAP = 3

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

_NUMERIC_HINTS = ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "HUGEINT")
_TEMPORAL_HINTS = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
_BOOLEAN_HINTS = ("BOOL",)
_STRING_HINTS = ("CHAR", "TEXT", "STRING", "VARCHAR")

_MAX_CONFIDENCE = 0.95

# Splits camelCase boundaries so patterns written against snake_case also match
# camelCase warehouses ("firstName" -> "first_name", "reviewerName" -> "reviewer_name").
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalize(column_name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", column_name).lower()


def _is_string_type(data_type: str) -> bool:
    upper = data_type.upper()
    return any(h in upper for h in _STRING_HINTS)


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


def detect_pii(column_name: str, data_type: str) -> PIIFlag | None:
    """Classify a column as PII from its name (and, loosely, type). Never a value.

    Returns the first matching category with a base confidence; aggregate signals
    refine the confidence later in :func:`_refine_confidence`. The exact-token
    patterns apply to any type; the weaker generic-name and free-text patterns
    apply only to string columns (a numeric `comments` count is not PII).
    """

    name = _normalize(column_name)
    for pattern, category, confidence in _PII_PATTERNS:
        if pattern.search(name):
            return PIIFlag(category=category, confidence=confidence)

    if _is_string_type(data_type):
        match = _GENERIC_NAME.search(name)
        if match is not None:
            qualifier = name[: match.start()].rstrip("_").rsplit("_", 1)[-1]
            if qualifier not in _NONPERSON_NAME_QUALIFIERS:
                return PIIFlag(category=PIICategory.NAME, confidence=0.6)
        if _FREE_TEXT.search(name):
            return PIIFlag(category=PIICategory.FREE_TEXT, confidence=0.5)
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


def profile(
    adapter: Adapter,
    identifiers: list[str],
    *,
    progress: ProgressReporter | None = None,
) -> list[Dataset]:
    """Profile each object into a Dataset of aggregate-derived ColumnProfiles.

    An optional ``progress`` reporter emits a throttled stderr line per object so
    a long run is visibly distinguishable from a hung one; ``None`` (the default)
    keeps existing callers silent and unchanged.
    """

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
        # Re-read the metadata after the aggregate scan: adapters whose
        # inventory row counts are planner estimates (Postgres reltuples)
        # upgrade to the exact COUNT(*) the scan just paid for, so uniqueness
        # proofs and the dataset row count are exact. Free everywhere (cached
        # or trivially cheap on the other adapters).
        meta, _ = adapter.table_metadata(identifier)
        aggregates = _escalate_near_unique(
            adapter, identifier, meta.row_count, aggregates
        )
        composite_keys = _probe_composite_keys(
            adapter, identifier, meta.row_count, aggregates
        )

        profiles: list[ColumnProfile] = []
        data_quality: list[str] = []
        if meta.row_count == 0:
            data_quality.append("empty table (no rows)")

        # Adapters that degrade a profile (partition-filter tables, block
        # sampling, skipped escalations) explain themselves through this
        # duck-typed hook, so the limitation reads as a data-quality note
        # instead of silently thinner numbers.
        notes_for = getattr(adapter, "table_notes", None)
        if notes_for is not None:
            data_quality.extend(notes_for(identifier))

        for col in columns:
            agg = aggregates.get(col.name)
            pii = _refine_confidence(prelim_pii[col.name], agg)
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
                )
            )

        datasets.append(
            Dataset(
                identifier=identifier,
                object_type=meta.object_type,
                row_count=meta.row_count,
                byte_size=meta.byte_size,
                columns=profiles,
                composite_keys=composite_keys,
                data_quality=data_quality,
                profiled_at=datetime.now(UTC).isoformat(),
            )
        )
        if progress is not None:
            progress.advance()
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
    even when technically unique). Bounded to ``_COMPOSITE_PAIR_CAP`` pairs in
    one batched adapter call; a pair is proven when its exact combination
    count equals the row count. Adapters without
    ``distinct_combination_counts`` degrade to no composite keys.
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
    from .relationships import _is_id_shaped

    ranked: list[tuple[int, int, tuple[str, str]]] = []
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            product = a.distinct_count * b.distinct_count
            n_approx = sum(1 for m in (a, b) if not m.distinct_count_exact)
            if product < row_count * NEAR_UNIQUE_RATIO**n_approx:
                continue
            id_shaped = sum(1 for m in (a, b) if _is_id_shaped(m.name))
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
    chosen = [list(pair) for _ids, _product, pair in ranked[:_COMPOSITE_PAIR_CAP]]
    exact = combo_counts(identifier, chosen)
    return [combo for combo in chosen if exact.get(tuple(combo)) == row_count]


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
