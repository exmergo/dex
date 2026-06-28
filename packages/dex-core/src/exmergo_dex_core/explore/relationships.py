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

_ID_SUFFIX = re.compile(r"_id$", re.IGNORECASE)


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
    entity = _singularize(dataset.identifier.rsplit(".", 1)[-1]).lower()
    for key in keys:
        name = key[0].lower()
        if name == "id" or name == f"{entity}_id" or _ID_SUFFIX.search(name):
            return key
    # Fall back to the lowest-cardinality unique column.
    by_card = sorted(
        keys,
        key=lambda k: _distinct_of(dataset, k[0]) or float("inf"),
    )
    return by_card[0]


def infer_relationships(datasets: list[Dataset]) -> list[Relationship]:
    """Infer many-to-one joins from column names, type compatibility, and which
    side carries a candidate key (the parent)."""

    keyed = {d.identifier: candidate_keys(d) for d in datasets}
    relationships: list[Relationship] = []

    for child in datasets:
        for col in child.columns:
            if not _ID_SUFFIX.search(col.name):
                continue
            for parent in datasets:
                if parent.identifier == child.identifier:
                    continue
                match = _match_parent(col, parent, keyed[parent.identifier])
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
    parent: Dataset,
    parent_keys: list[list[str]],
) -> tuple[list[str], float] | None:
    parent_entity = _singularize(parent.identifier.rsplit(".", 1)[-1]).lower()
    fk = col.name.lower()
    expected_fk = f"{parent_entity}_id"

    parent_cols = {c.name.lower(): c for c in parent.columns}
    parent_key_names = {k[0].lower() for k in parent_keys}

    # Strongest: <parent>_id pointing at the parent's unique id / <parent>_id.
    for target in ("id", expected_fk):
        if fk == expected_fk and target in parent_cols:
            pcol = parent_cols[target]
            if _type_compatible(col.data_type, pcol.data_type):
                confidence = 0.85 if target in parent_key_names else 0.6
                return [pcol.name], confidence

    # Same-named foreign key shared by both tables (e.g. customer_id in both),
    # joining to the parent's key of that name.
    if fk in parent_cols and fk in parent_key_names:
        pcol = parent_cols[fk]
        if _type_compatible(col.data_type, pcol.data_type):
            return [pcol.name], 0.6

    return None


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
