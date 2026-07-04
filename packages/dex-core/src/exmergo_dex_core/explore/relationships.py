"""Explore: candidate keys, grain, and declared/inferred joins.

Inference is metadata-only: it reads the profiles already gathered (names, types,
uniqueness signals) and never scans data, which keeps it free at the cost of
confidence, so every inferred join carries a confidence the agent can weigh. The
one deliberate exception is the opt-in ``--verify`` pass
(:func:`verify_relationships`), which runs one bounded, engine-authored aggregate
probe per inferred join to measure the actual key overlap. Declared joins come
from the dbt project; absent one, they are simply empty (explore is designed to
work without a dbt project).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..adapters.base import Adapter
from ..cache import (
    ColumnProfile,
    Dataset,
    Relationship,
    RelationshipKind,
)
from .profile import NEAR_UNIQUE_RATIO

# Warehouse-layer prefixes stripped from a table name before entity matching, so
# RAW_HOSTS, stg_races, and dim_customers all match FKs named after the bare entity.
_LAYER_PREFIX = re.compile(r"^(raw|stg|src|dim|fct|fact|mart|int)_", re.IGNORECASE)


def _fk_stem(column_name: str) -> str | None:
    """The entity stem of an id-shaped column, or None if not id-shaped.

    Recognizes the three naming shapes seen in real warehouses: `customer_id` /
    `HOST_ID` (underscore, any case), camelCase `raceId`, and a trailing upper `ID`
    only when a separator precedes it (`HOSTID` stays ambiguous and is skipped).
    A bare `id` is a key, not a foreign key, so it has no stem.
    """

    if re.search(r"(?<=.)_id$", column_name, re.IGNORECASE):
        return column_name[:-3]
    if re.search(r"(?<=[a-z0-9])Id$", column_name):
        return column_name[:-2]
    return None


def _entity(table_name: str) -> str:
    """The entity a table represents: layer prefix stripped, singularized, lowered."""

    return _singularize(_LAYER_PREFIX.sub("", table_name)).lower()


def _is_id_shaped(column_name: str) -> bool:
    return column_name.lower() == "id" or _fk_stem(column_name) is not None


def candidate_keys(dataset: Dataset) -> list[list[str]]:
    """Single-column candidate keys: unique and non-null. Composite keys deferred.

    Uniqueness on near-unique columns is escalated to an exact COUNT(DISTINCT)
    at profile time (``distinct_count_exact``), so these are proven where it
    matters; a column whose uniqueness still rests on the approximate count is
    a signal, not a proof.
    """

    return [
        [col.name]
        for col in dataset.columns
        if col.is_unique and (col.null_fraction in (0.0, None))
    ]


def detect_grain(dataset: Dataset) -> list[str] | None:
    """The most likely grain: prefer an ``id`` / ``<entity>_id`` candidate key,
    else the unique column with the smallest cardinality. None if no key."""

    keys = candidate_keys(dataset)
    if not keys:
        return None
    entity = _entity(dataset.identifier.rsplit(".", 1)[-1])
    for key in keys:
        name = key[0].lower()
        if name in ("id", f"{entity}_id", f"{entity}id") or _is_id_shaped(key[0]):
            return key
    # Fall back to the lowest-cardinality unique column.
    by_card = sorted(
        keys,
        key=lambda k: _distinct_of(dataset, k[0]) or float("inf"),
    )
    return by_card[0]


def infer_relationships(datasets: list[Dataset]) -> list[Relationship]:
    """Infer many-to-one joins from column names, type compatibility, and the
    aggregate signals already profiled (uniqueness, distinct counts, min/max).

    A parent whose key is not unique still yields a join, at reduced confidence:
    suppressing it entirely would hide a real join behind a data-quality problem
    that :func:`data_quality_notes` reports separately.
    """

    keyed = {d.identifier: candidate_keys(d) for d in datasets}
    relationships: list[Relationship] = []

    for child in datasets:
        for col in child.columns:
            stem = _fk_stem(col.name)
            if stem is None:
                continue
            for parent in datasets:
                if parent.identifier == child.identifier:
                    continue
                match = _match_parent(col, stem, parent, keyed[parent.identifier])
                if match is not None:
                    to_columns, confidence = match
                    relationships.append(
                        Relationship(
                            from_dataset=child.identifier,
                            from_columns=[col.name],
                            to_dataset=parent.identifier,
                            to_columns=to_columns,
                            kind=RelationshipKind.INFERRED,
                            confidence=confidence,
                        )
                    )
    return relationships


def fk_candidate_count(datasets: list[Dataset]) -> int:
    """How many profiled columns look like foreign keys. Reported alongside the
    inference result so an empty relationships array is distinguishable from
    'nothing id-shaped to try'."""

    return sum(1 for d in datasets for c in d.columns if _fk_stem(c.name) is not None)


def data_quality_notes(dataset: Dataset) -> list[str]:
    """The interpretation an analyst would write from the aggregates already
    gathered: broken grain on the table's own key, and an unknown grain.

    Only the table's own key columns (bare ``id`` or ``<own entity>_id``) are
    checked for uniqueness; a repeated foreign key is the expected shape of a
    child table, not a defect.
    """

    notes: list[str] = []
    if not dataset.row_count:
        return notes

    entity = _entity(dataset.identifier.rsplit(".", 1)[-1])
    for col in dataset.columns:
        stem = _fk_stem(col.name)
        own_key = col.name.lower() == "id" or (
            stem is not None and _singularize(stem).lower() == entity
        )
        if not own_key or col.distinct_count is None:
            continue
        if (
            not col.distinct_count_exact
            and col.distinct_count >= NEAR_UNIQUE_RATIO * dataset.row_count
        ):
            # Within approximation noise of unique: unproven either way, so no
            # verdict. Exact counts always speak; a shortfall too large for
            # noise (an approx 500 distinct over 1,125 rows) still warns.
            continue
        if col.distinct_count < dataset.row_count:
            duplicates = dataset.row_count - col.distinct_count
            # An unescalated count is honest about being approximate.
            marker = "" if col.distinct_count_exact else "~"
            notes.append(
                f"{col.name} is not unique: {marker}{col.distinct_count} distinct "
                f"over {dataset.row_count} rows (~{duplicates} duplicate rows); "
                "joins on it will fan out"
            )

    if not candidate_keys(dataset):
        notes.append("no candidate key detected; grain unknown")
    return notes


def verify_relationships(
    adapter: Adapter,
    relationships: list[Relationship],
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Measure each inferred join with one overlap probe and adjust in place.

    The probe counts non-null foreign-key values and how many have no match in
    the parent (orphans). Full containment raises confidence; a high orphan rate
    is strong evidence the name-based guess was wrong and demotes it well below
    the emission threshold rather than deleting it, so the agent still sees what
    was tried. Aggregate counts only; no key value ever leaves the engine.
    """

    for rel in relationships:
        if rel.kind is not RelationshipKind.INFERRED:
            continue
        sql = _transpile_probe(_overlap_probe_sql(rel), adapter.dialect)
        result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
        values = dict(zip(result.columns, result.cells[0], strict=True))
        nonnull = int(values["nonnull_fk"] or 0)
        orphans = int(values["orphans"] or 0)

        rel.verified = True
        if nonnull == 0:
            rel.orphan_fraction = None
            continue
        fraction = orphans / nonnull
        rel.orphan_fraction = round(fraction, 4)

        confidence = rel.confidence or 0.5
        if fraction == 0.0:
            confidence += 0.1
        elif fraction <= 0.02:
            confidence += 0.05
        elif fraction >= 0.2:
            confidence -= 0.25
        else:
            confidence -= 0.1
        rel.confidence = round(min(0.95, max(0.05, confidence)), 4)


def probe_statements(relationships: list[Relationship], dialect: str) -> list[str]:
    """The exact SQL :func:`verify_relationships` will run, one statement per
    inferred join, in the adapter's dialect. Exists so a billed caller can
    dry-run the probes for a cost estimate before confirming the spend."""

    return [
        _transpile_probe(_overlap_probe_sql(rel), dialect)
        for rel in relationships
        if rel.kind is RelationshipKind.INFERRED
    ]


def _overlap_probe_sql(rel: Relationship) -> str:
    child = _quote_identifier(rel.from_dataset)
    parent = _quote_identifier(rel.to_dataset)
    fk = _quote_part(rel.from_columns[0])
    key = _quote_part(rel.to_columns[0])
    # Aggregate-only by construction: two counts, no value in the projection.
    # NOT EXISTS keeps the orphan count correct even when the parent key is not
    # unique (a join would fan out and inflate it). Deliberately portable SQL:
    # CASE inside COUNT rather than FILTER (which BigQuery lacks and sqlglot
    # does not rewrite), with the EXISTS in a subselect so every dialect plans
    # it as an anti-join.
    return (
        f"SELECT COUNT(probe.fk) AS nonnull_fk, "  # noqa: S608
        f"COUNT(CASE WHEN probe.orphan THEN 1 END) AS orphans FROM ("
        f"SELECT c.{fk} AS fk, "
        f"c.{fk} IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM {parent} p WHERE p.{key} = c.{fk}) AS orphan "
        f"FROM {child} c) probe"
    )


def _transpile_probe(sql: str, dialect: str) -> str:
    """Render the DuckDB-flavored probe in the active connector's dialect.

    The probe is authored once in DuckDB SQL (double-quoted identifiers,
    ``COUNT(*) FILTER``); sqlglot rewrites it per connector (BigQuery gets
    backticks and COUNTIF). Identity on DuckDB itself.
    """

    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


def _quote_identifier(identifier: str) -> str:
    return ".".join(_quote_part(p) for p in identifier.split("."))


def _quote_part(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def declared_relationships(repo_root: Path | str = ".") -> list[Relationship]:
    """Declared joins from the dbt project. Returns empty when there is no dbt
    project (the common explore-without-dbt case), which is not an error."""

    root = Path(repo_root)
    has_project = (root / "dbt_project.yml").is_file() or (
        root / "target" / "manifest.json"
    ).is_file()
    if not has_project:
        return []
    # Parsing declared relationships from the manifest lands with the dbt_project
    # reader (transform phase). Until then, a present-but-unparsed project yields
    # no declared joins rather than guessing.
    return []


def _match_parent(
    col: ColumnProfile,
    stem: str,
    parent: Dataset,
    parent_keys: list[list[str]],
) -> tuple[list[str], float] | None:
    parent_table = parent.identifier.rsplit(".", 1)[-1]
    stripped = _LAYER_PREFIX.sub("", parent_table)
    # Match the raw stripped name too, not just its singular: an already-singular
    # table like `status` would otherwise be mangled by the heuristic inflector.
    parent_entities = {stripped.lower(), _singularize(stripped).lower()}

    parent_cols = {c.name.lower(): c for c in parent.columns}
    parent_key_names = {k[0].lower() for k in parent_keys}
    fk = col.name.lower()
    stem_l = stem.lower()

    # Strongest: <entity>_id / <entity>Id pointing at the parent named <entity>,
    # joining to the parent's id-shaped key (`id`, `<entity>_id`, or `<entity>Id`).
    if stem_l in parent_entities or _singularize(stem).lower() in parent_entities:
        for target in ("id", f"{stem_l}_id", f"{stem_l}id"):
            pcol = parent_cols.get(target)
            if pcol is not None and _type_compatible(col.data_type, pcol.data_type):
                base = 0.85 if target in parent_key_names else 0.5
                return [pcol.name], _score(base, col, pcol)

    # Same-named foreign key shared by both tables (e.g. customer_id in both),
    # joining to the parent's key of that name.
    if fk in parent_cols and fk in parent_key_names:
        pcol = parent_cols[fk]
        if _type_compatible(col.data_type, pcol.data_type):
            return [pcol.name], _score(0.6, col, pcol)

    return None


def _score(base: float, child: ColumnProfile, parent: ColumnProfile) -> float:
    """Refine a name-derived confidence with the aggregates already profiled.

    Containment is the cheap value-overlap check: a true FK's distinct count
    cannot exceed its parent key's, and (for numerics) its range sits inside the
    parent's. Both signals come from the profile pass, so this stays free and
    metadata-only, with no extra queries.
    """

    confidence = base
    if child.distinct_count is not None and parent.distinct_count is not None:
        if child.distinct_count <= parent.distinct_count:
            confidence += 0.05
        else:
            confidence -= 0.15

    bounds = (child.min_value, child.max_value, parent.min_value, parent.max_value)
    if all(_is_number(v) for v in bounds):
        contained = (
            parent.min_value <= child.min_value and child.max_value <= parent.max_value
        )
        confidence += 0.05 if contained else -0.1

    return round(min(0.95, max(0.05, confidence)), 4)


def _is_number(value: object | None) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_compatible(a: str, b: str) -> bool:
    return _type_family(a) == _type_family(b)


def _type_family(data_type: str) -> str:
    upper = data_type.upper()
    if any(h in upper for h in ("INT", "HUGEINT", "DECIMAL", "NUMERIC")):
        return "integer"
    if any(h in upper for h in ("CHAR", "TEXT", "STRING", "VARCHAR", "UUID")):
        return "text"
    if any(h in upper for h in ("DOUBLE", "FLOAT", "REAL")):
        return "float"
    return upper


def _singularize(name: str) -> str:
    """Best-effort singular of a table name for entity matching (orders -> order).

    A heuristic, not a real inflector: it covers the common -s/-es/-ies plurals and
    deliberately leaves -ss words (address, class) untouched, since those are
    singular nouns that a naive trailing-s strip would corrupt. Irregular plurals
    (people, data) are not inverted; matching simply falls through for those.
    """

    lower = name.lower()
    if lower.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if lower.endswith(("ses", "xes", "zes", "ches", "shes")):
        return name[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return name[:-1]
    return name


def _distinct_of(dataset: Dataset, column: str) -> int | None:
    for col in dataset.columns:
        if col.name == column:
            return col.distinct_count
    return None
