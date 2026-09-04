"""Validate native Ossie references using stored exploration evidence only.

The cache is evidence, not the source of truth.  A positive inventory or column
profile can therefore prove that a reference exists, and a complete inventory
of one namespace can prove that a relation does not.  Every other absence is an
unknown, never a refusal.  Nothing in this module accepts an adapter or opens a
connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..adapters import normalize_relation
from ..cache import DexCache, match_identifier
from ..semantic_catalog import column_reference
from .dialects import select_expression
from .loader import LoadedDocument


@dataclass
class CacheValidation:
    """What the cache proved, could not settle, or contradicted."""

    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked_relations: int = 0
    checked_columns: int = 0


def validate_cached_references(
    documents: list[LoadedDocument],
    cache: DexCache | None,
    *,
    connector: str | None,
) -> CacheValidation:
    """Check cache-provable sources and columns without opening a connector."""

    result = CacheValidation()
    if cache is None:
        result.notes.append(
            "cache validation could not check physical Ossie references because "
            "there is no exploration cache; they remain unknown and no warehouse "
            "connection was opened"
        )
        return result

    cached_connector = cache.provenance.connector
    if connector is None or (
        cached_connector is not None and cached_connector != connector
    ):
        expected = connector or "an unconfigured connector"
        actual = cached_connector or "an unknown connector"
        result.notes.append(
            f"cache validation did not use the exploration cache for {actual} "
            f"against {expected}; physical references remain unknown and no "
            "warehouse connection was opened"
        )
        return result

    by_identifier = {dataset.identifier: dataset for dataset in cache.datasets}
    identifiers = list(by_identifier)
    inventoried = {
        namespace.lower() for namespace in cache.provenance.inventory_namespaces
    }
    unknown_relations: set[str] = set()
    unprofiled: set[str] = set()
    opaque_sources = 0
    computed_fields = 0

    for document in documents:
        for model_index, model in enumerate(document.data.get("semantic_model") or []):
            model_name = str(model.get("name"))
            datasets = model.get("datasets") or []
            endpoint_columns = _endpoint_columns(model)

            for dataset_index, dataset in enumerate(datasets):
                dataset_name = str(dataset.get("name"))
                location = (
                    f"{document.file}: semantic_model[{model_index}] "
                    f"'{model_name}' dataset[{dataset_index}] '{dataset_name}'"
                )
                relation = normalize_relation(connector, dataset.get("source"))
                if relation is None:
                    opaque_sources += 1
                    continue

                matches = match_identifier(relation, identifiers)
                if len(matches) != 1:
                    namespace = relation.rpartition(".")[0].lower()
                    if not matches and namespace in inventoried:
                        result.errors.append(
                            f"{location} names source '{relation}', but the cached "
                            f"inventory for namespace '{namespace}' does not contain it"
                        )
                    else:
                        unknown_relations.add(relation)
                    continue

                cached = by_identifier[matches[0]]
                result.checked_relations += 1
                references, skipped = _column_references(
                    dataset,
                    endpoint_columns.get(dataset.get("name"), []),
                    connector=connector,
                )
                computed_fields += skipped
                if not cached.columns:
                    if references:
                        unprofiled.add(cached.identifier)
                    continue

                known_columns = {column.name.lower() for column in cached.columns}
                for column, role in references:
                    result.checked_columns += 1
                    if column.lower() not in known_columns:
                        result.errors.append(
                            f"{location} {role} names column '{column}', but the "
                            f"cached profile for '{cached.identifier}' does not "
                            "contain it"
                        )

    if unknown_relations:
        result.notes.append(
            "cache validation could not settle direct source(s) outside a "
            "completely inventoried namespace: " + ", ".join(sorted(unknown_relations))
        )
    if unprofiled:
        result.notes.append(
            "cache validation found relation(s) whose columns are unprofiled, so "
            "their field, key, and relationship-column references remain unknown: "
            + ", ".join(sorted(unprofiled))
        )
    if opaque_sources:
        result.notes.append(
            f"cache validation skipped {opaque_sources} query-backed, quoted, or "
            "otherwise opaque dataset source(s); dex does not guess a physical relation"
        )
    if computed_fields:
        result.notes.append(
            f"cache validation skipped {computed_fields} computed or non-SQL field "
            "expression(s); dex validates only a direct physical column reference"
        )
    result.notes.append(
        f"cache validation checked {result.checked_relations} direct relation(s) "
        f"and {result.checked_columns} physical column reference(s) using stored "
        "evidence only; no warehouse connection was opened"
    )
    return result


def _endpoint_columns(model: dict[str, Any]) -> dict[Any, list[tuple[str, str]]]:
    columns: dict[Any, list[tuple[str, str]]] = {}
    for relationship in model.get("relationships") or []:
        name = str(relationship.get("name"))
        for side, key in (("from", "from_columns"), ("to", "to_columns")):
            dataset = relationship.get(side)
            columns.setdefault(dataset, []).extend(
                (str(column), f"relationship '{name}' {key}")
                for column in relationship.get(key) or []
            )
    return columns


def _column_references(
    dataset: dict[str, Any],
    endpoints: list[tuple[str, str]],
    *,
    connector: str,
) -> tuple[list[tuple[str, str]], int]:
    references = list(endpoints)
    skipped = 0
    for field_ in dataset.get("fields") or []:
        expression, _dialect, _declared = select_expression(
            field_.get("expression"), connector
        )
        column = column_reference(expression, None) if expression else None
        if column is None:
            skipped += 1
        else:
            references.append((column, f"field '{field_.get('name')}'"))
    references.extend(
        (str(column), "primary_key")
        for column in dataset.get("primary_key") or []
    )
    for key_index, key in enumerate(dataset.get("unique_keys") or []):
        references.extend(
            (str(column), f"unique_keys[{key_index}]") for column in key
        )
    # The same physical column may be a field, key, and endpoint.  Each role is
    # useful in a refusal, but duplicate declarations of one role are only noise.
    return list(dict.fromkeys(references)), skipped
