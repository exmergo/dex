"""Explore: cumulative and snapshot measure detection (issue #219).

A numeric column holding a running total or a point-in-time snapshot (a
season-to-date score, a subscription's current MRR, an account balance, an
inventory level) profiles identically to one holding a per-row increment:
same type, same null fraction, same uniqueness, same min/max shape. Summing
it across rows is a common and severely damaging misreading, and it is
detectable from data.

The signal: within an entity (a repeating, id-shaped column that is not
itself the table's own key) ordered by a temporal column, a cumulative or
snapshot measure almost never decreases from one observation to the next. A
per-row increment, by contrast, has natural ups and downs. Measuring that
needs a real scan (a window function over the table), so this sits on the
gated side of the free-versus-gated split: opt-in via ``--check-cumulative``,
priced and confirmed the same way ``--verify`` prices relationship overlap
probes. The base profile always completes and is returned first; this runs
as a second, individually skippable phase against what the base scan already
proved, never blocking it.

Only fractions and observation counts ever leave the engine. The probe is
aggregate-only (``COUNT``/``CASE WHEN``); no row's measure value is ever
projected, only compared and counted.
"""

from __future__ import annotations

from typing import NamedTuple

from ..adapters.base import Adapter, is_temporal_type
from ..cache import ColumnProfile, Dataset
from .profile import is_numeric_type
from .relationships import _is_id_shaped

# A fraction of within-entity consecutive decreases at or below this, over
# enough observations, is read as "does not decrease" -- strong evidence of a
# cumulative or snapshot measure -- rather than "happened not to decrease
# yet". Small non-zero values still qualify: a snapshot can fall (a balance
# after a withdrawal, MRR after a downgrade) without being a per-row
# increment; what a genuine increment has that a snapshot does not is *many*
# decreases, not zero exactly.
_DECREASE_FRACTION_CEILING = 0.05
# Below this many within-entity observations there is not enough evidence
# either way; the column is skipped rather than reported on a handful of
# rows. Mirrors this codebase's other "not enough evidence, fail closed"
# floors (e.g. the temporal-alignment and heterogeneous-key checks).
_MIN_OBSERVATIONS = 20


class CumulativeCandidate(NamedTuple):
    """One (entity, temporal) pair this dataset's shape supports, and every
    eligible measure column paired with it. Only one pair is ever chosen per
    dataset; see :func:`find_candidate`."""

    entity: str
    temporal: str
    measures: list[str]


def _is_single_column_key(col: ColumnProfile) -> bool:
    return bool(col.is_unique) and col.null_fraction in (0.0, None)


def _is_entity_key(col: ColumnProfile) -> bool:
    """An id-shaped column that is *not itself, alone,* a proven key: a
    partition of exactly one row per key could never show a within-entity
    sequence, so a real entity key must repeat.

    Deliberately not excluded for merely appearing in some *composite* proven
    key: an entity column commonly pairs with the measure itself to look
    "unique" (a snapshot that keeps changing per entity rarely repeats a
    value), which is a coincidence of the data, not evidence the entity
    column fails to repeat on its own.
    """

    if col.pii is not None:
        return False
    if not _is_id_shaped(col.name):
        return False
    return not _is_single_column_key(col)


def _is_temporal_candidate(col: ColumnProfile) -> bool:
    return col.pii is None and is_temporal_type(col.data_type)


def _is_measure_candidate(col: ColumnProfile) -> bool:
    """A numeric, non-key, non-id-shaped, non-PII column: the acceptance
    criterion's auto-increment id is excluded here (it is a proven
    single-column key, whatever its name), and an ordinary foreign key is
    excluded by shape (id-shaped names are never measures). Not excluded for
    merely appearing in a composite key, for the same reason
    :func:`_is_entity_key` is not: pairing with the entity is the coincidence
    a real running total or snapshot produces, not disqualifying evidence.
    """

    if col.pii is not None:
        return False
    if not is_numeric_type(col.data_type):
        return False
    if _is_id_shaped(col.name):
        return False
    return not _is_single_column_key(col)


def find_candidate(dataset: Dataset) -> tuple[CumulativeCandidate | None, list[str]]:
    """The one (entity, temporal) pair this dataset's shape supports, paired
    with every eligible measure column, or ``None`` when an entity key or a
    temporal column is missing outright -- the acceptance criterion that a
    table lacking either is skipped, not silently reported as clean.

    Returns ``(candidate, notes)``: ``notes`` states the skip when there is
    one, and names any additional entity/temporal candidate this dataset had
    that was not tested, so narrowing to one pair is never silent.

    Where more than one entity or temporal candidate exists, the pair that is
    also the table's own proven composite key wins -- the strongest possible
    evidence that this table really is one row per entity per period.
    Failing that, the first of each, in column order.
    """

    entities = [c.name for c in dataset.columns if _is_entity_key(c)]
    temporals = [c.name for c in dataset.columns if _is_temporal_candidate(c)]

    if not entities or not temporals:
        missing = []
        if not entities:
            missing.append("entity key")
        if not temporals:
            missing.append("temporal column")
        return None, [
            "cumulative-measure check skipped: no "
            + " or ".join(missing)
            + " found; detecting a running total needs both"
        ]

    composite_pair = next(
        (
            (e, t)
            for key in dataset.composite_keys
            for e in entities
            for t in temporals
            if e in key and t in key
        ),
        None,
    )
    entity, temporal = composite_pair or (entities[0], temporals[0])

    measures = [
        c.name
        for c in dataset.columns
        if c.name not in (entity, temporal) and _is_measure_candidate(c)
    ]

    notes: list[str] = []
    skipped_entities = [e for e in entities if e != entity]
    skipped_temporals = [t for t in temporals if t != temporal]
    if skipped_entities or skipped_temporals:
        skipped = ", ".join(skipped_entities + skipped_temporals)
        notes.append(
            f"cumulative-measure check tested only {entity} x {temporal}; "
            f"also eligible but not tested: {skipped}"
        )
    if not measures:
        notes.append(
            f"cumulative-measure check skipped: {entity} x {temporal} has an "
            "entity key and a temporal column but no eligible numeric "
            "measure column"
        )
        return None, notes
    return CumulativeCandidate(entity, temporal, measures), notes


def probe_sql(identifier: str, candidate: CumulativeCandidate) -> str:
    """Aggregate-only SQL (DuckDB-flavored; transpiled per dialect by
    :func:`probe_statements`) computing, for every measure in one pass, the
    fraction of within-entity consecutive observations where the value
    decreased. Never projects a measure's own value: each is only compared,
    inside a ``CASE WHEN``, to its own lag."""

    table = _quote_identifier(identifier)
    entity = _quote_part(candidate.entity)
    temporal = _quote_part(candidate.temporal)

    lag_cols = []
    counts = []
    for i, measure in enumerate(candidate.measures):
        m = _quote_part(measure)
        cur, prev = f"m_{i}", f"p_{i}"
        lag_cols.append(
            f"{m} AS {cur}, LAG({m}) OVER (PARTITION BY {entity} "
            f"ORDER BY {temporal}) AS {prev}"
        )
        counts.append(
            f"COUNT(CASE WHEN {prev} IS NOT NULL AND {cur} IS NOT NULL "
            f"THEN 1 END) AS obs_{i}, "
            f"COUNT(CASE WHEN {prev} IS NOT NULL AND {cur} IS NOT NULL "
            f"AND {cur} < {prev} THEN 1 END) AS dec_{i}"
        )

    return (
        "WITH lagged AS (SELECT "  # noqa: S608
        + ", ".join(lag_cols)
        + f" FROM {table}) SELECT "
        + ", ".join(counts)
        + " FROM lagged"
    )


def probe_statements(
    datasets_and_candidates: list[tuple[Dataset, CumulativeCandidate]], dialect: str
) -> list[str]:
    """The exact SQL :func:`measure_fractions` will run, one statement per
    dataset, in the adapter's dialect. Exists so a billed caller can dry-run
    the probes for a cost estimate before confirming the spend."""

    return [
        _transpile_probe(probe_sql(dataset.identifier, candidate), dialect)
        for dataset, candidate in datasets_and_candidates
    ]


def measure_fractions(
    adapter: Adapter,
    identifier: str,
    candidate: CumulativeCandidate,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, tuple[float, int]]:
    """Run one probe and return, per measure, ``(decrease_fraction,
    observations)``. A measure with zero within-entity observations (every
    row is its entity's first, or every prior value was null) is omitted
    rather than reported at a meaningless 0/0."""

    sql = _transpile_probe(probe_sql(identifier, candidate), adapter.dialect)
    result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
    values = dict(zip(result.columns, result.cells[0], strict=True))
    fractions: dict[str, tuple[float, int]] = {}
    for i, measure in enumerate(candidate.measures):
        obs = int(values[f"obs_{i}"] or 0)
        if obs <= 0:
            continue
        decreases = int(values[f"dec_{i}"] or 0)
        fractions[measure] = (decreases / obs, obs)
    return fractions


def cumulative_measure_notes(
    candidate: CumulativeCandidate, fractions: dict[str, tuple[float, int]]
) -> list[str]:
    """A data-quality observation per measure that never (or almost never)
    decreases within an entity over time: the consequence named, never a
    value. Silent for a measure with too little evidence, or whose decreases
    are too common to read as a running total."""

    notes = []
    for measure in candidate.measures:
        result = fractions.get(measure)
        if result is None:
            continue
        fraction, observations = result
        if observations < _MIN_OBSERVATIONS or fraction > _DECREASE_FRACTION_CEILING:
            continue
        notes.append(
            f"{measure} looks cumulative or a point-in-time snapshot: over "
            f"{observations} consecutive observations within "
            f"{candidate.entity}, ordered by {candidate.temporal}, only "
            f"{fraction:.1%} decreased; summing it across rows multiplies "
            "the true value -- take the latest value per entity, or "
            "difference consecutive values to recover the increment"
        )
    return notes


def _transpile_probe(sql: str, dialect: str) -> str:
    """Render the DuckDB-flavored probe in the active connector's dialect.
    Identity on DuckDB itself, matching ``relationships._transpile_probe``."""

    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


def _quote_identifier(identifier: str) -> str:
    return ".".join(_quote_part(p) for p in identifier.split("."))


def _quote_part(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'
