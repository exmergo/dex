"""Ossie's own tier-2 fingerprint: the maintain snapshot channel (#409).

Builds the same format-neutral shapes `maintain.snapshot` defines for dbt
(`TransformLayer`, `SemanticLayerSnapshot`), from Ossie's own validated
documents. This module never imports `dbt_project` or anything MetricFlow
shaped, and `maintain.snapshot` never imports this one: the two formats read
their own sources into one shared shape rather than one depending on the
other's reader.

Reuses `catalog.py`'s column-resolution and relationship-resolution helpers
rather than re-deriving them, so a field's link to a physical column, and a
relationship's ordered pairs, are decided by exactly one piece of logic
whether a caller reads the catalog or fingerprints it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..maintain.snapshot import (
    MetricDef,
    RelationshipDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
    TransformLayer,
)
from ..project_definitions import DeclaredRelationship
from . import catalog as catalog_mod
from .loader import LoadResult


def _content_hash(text: str) -> str:
    """A definition's fingerprint. Deliberately not `dbt_project.content_hash`:
    importing a one-line sha256 wrapper across the format boundary would still
    be a dependency on the dbt module, which #409's own constraint refuses."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _definition_hash(entry: Any) -> str:
    return _content_hash(json.dumps(entry, sort_keys=True, default=str))


def transform_layer(repo_root: Path, files: list[str]) -> TransformLayer:
    """The document set's own fingerprint: file hashes and nothing else.

    ``models``, ``model_paths``, ``sources``, ``model_sources``, and
    ``model_refs`` all stay empty: Ossie documents declare no build step, and
    a dataset's source is already the physical relation its semantic model
    records on ``SemanticModelDef.relation`` (#409), not a name a
    transformation project resolves later the way a dbt ``ref()``/``source()``
    does. The note says this once here rather than leaving five empty
    collections to be misread as "this format has none of these to declare".
    """

    hashed: dict[str, str] = {}
    for name in files:
        try:
            hashed[name] = _content_hash((repo_root / name).read_text(encoding="utf-8"))
        except OSError:
            # Absent or unreadable: declared_definitions()/semantic_layer()
            # already carry the diagnostic naming this file, so a missing
            # hash here is that same fact, not a second one to explain.
            continue
    return TransformLayer(
        files=hashed,
        notes=[
            "native Ossie documents declare no build step, so `models` and "
            "`model_refs` stay empty: a dataset's source is already the "
            "physical relation its semantic model records, not a name a "
            "transformation project resolves later"
        ],
    )


def semantic_layer(
    loaded: LoadResult, *, connector: str | None
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated documents."""

    if not loaded.documents:
        return SemanticLayerSnapshot(notes=loaded.notes())

    semantic_models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    relationships: list[RelationshipDef] = []
    notes: list[str] = list(loaded.notes())

    for document in loaded.documents:
        for model in document.data.get("semantic_model") or []:
            model_name = model.get("name")
            by_name: dict[str, str] = {}
            relations: dict[str, str] = {}
            for dataset in model.get("datasets") or []:
                dataset_name = dataset.get("name")
                qualified = f"{model_name}.{dataset_name}"
                by_name[str(dataset_name)] = qualified
                sm_def, sm_notes = _semantic_model_def(
                    dataset, qualified, document.file, connector
                )
                # A dataset with no fields and no keys has nothing semantic
                # declared about it yet: a bare relation reference is a
                # schema-axis fact, not a semantic-model definition, and
                # recording one here would make an unadorned dataset
                # indistinguishable from a real, populated one for the
                # generic added/removed/changed diff.
                if sm_def.dimensions or sm_def.keys:
                    semantic_models.append(sm_def)
                    notes.extend(sm_notes)
                if sm_def.relation:
                    relations[qualified] = sm_def.relation

            for rel in model.get("relationships") or []:
                declared, note = catalog_mod._declared_relationship(
                    rel, by_name, relations
                )
                if note:
                    notes.append(note)
                if declared is not None:
                    relationships.append(_relationship_def(declared, document.file))

            metrics.extend(
                _metric_def(metric, document.file)
                for metric in model.get("metrics") or []
                if isinstance(metric, dict) and isinstance(metric.get("name"), str)
            )

    return SemanticLayerSnapshot(
        semantic_models=semantic_models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        notes=notes,
    )


def _semantic_model_def(
    dataset: dict[str, Any], qualified: str, path: str, connector: str | None
) -> tuple[SemanticModelDef, list[str]]:
    """One dataset as a semantic model, plus any linkage notes its fields raised."""

    dataset_name = dataset.get("name")
    relation = catalog_mod._relation(dataset.get("source"), connector)
    dimensions: dict[str, str | None] = {}
    notes: list[str] = []
    for field_ in dataset.get("fields") or []:
        if not isinstance(field_, dict) or not isinstance(field_.get("name"), str):
            continue
        dimension, note = catalog_mod._dimension(
            field_, dataset_name, qualified, connector, relation
        )
        dimensions[str(field_["name"])] = dimension.column
        if note:
            notes.append(note)

    keys: list[list[str]] = []
    for declared in (dataset.get("primary_key"), *(dataset.get("unique_keys") or [])):
        columns = [c for c in (declared or []) if isinstance(c, str) and c]
        if columns:
            keys.append(columns)

    return (
        SemanticModelDef(
            name=qualified,
            path=path,
            content_sha256=_definition_hash(dataset),
            relation=relation,
            dimensions=dimensions,
            keys=keys,
        ),
        notes,
    )


def _relationship_def(declared: DeclaredRelationship, path: str) -> RelationshipDef:
    name = declared.name or (
        f"{declared.model}->{declared.to_model}:"
        + ",".join(f"{frm}={to}" for frm, to in declared.column_pairs)
    )
    entry = {
        "model": declared.model,
        "to_model": declared.to_model,
        "column_pairs": [list(pair) for pair in declared.column_pairs],
    }
    return RelationshipDef(
        name=name,
        path=path,
        content_sha256=_definition_hash(entry),
        model=declared.model,
        to_model=declared.to_model,
        column_pairs=declared.column_pairs,
    )


def _metric_def(metric: dict[str, Any], path: str) -> MetricDef:
    # `input_measures`/`input_metrics` stay empty: Ossie metrics carry lineage
    # (which datasets an expression touches), not measure/metric composition,
    # and inventing either here would be a MetricFlow-shaped fact this format
    # never states.
    return MetricDef(
        name=str(metric["name"]),
        path=path,
        content_sha256=_definition_hash(metric),
    )
