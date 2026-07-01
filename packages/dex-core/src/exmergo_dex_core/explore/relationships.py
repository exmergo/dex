"""Explore: candidate keys, grain, and declared/inferred joins.

All inference here is metadata-only: it reads the profiles already gathered (names,
types, uniqueness signals) and never scans data to verify referential integrity.
That keeps relationship inference free and read-only, at the cost of confidence,
so every inferred join carries a confidence the agent can weigh. Declared joins
come from the dbt project; absent one, they are simply empty (explore is designed
to work without a dbt project).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..cache import (
    ColumnProfile,
    Dataset,
    Relationship,
    RelationshipKind,
)

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

    Uniqueness rests on an approximate distinct count, so these are candidates the
    engine and agent treat as signals, not proven primary keys.
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
        if col.distinct_count < dataset.row_count:
            duplicates = dataset.row_count - col.distinct_count
            notes.append(
                f"{col.name} is not unique: {col.distinct_count} distinct over "
                f"{dataset.row_count} rows (~{duplicates} duplicate rows); "
                "joins on it will fan out"
            )

    if not candidate_keys(dataset):
        notes.append("no candidate key detected; grain unknown")
    return notes


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
