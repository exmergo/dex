"""SLayer's own drift fingerprint: the maintain snapshot channel.

Builds the neutral `SemanticLayerSnapshot` from the validated SLayer models, the
way `ossie.snapshot` does from Ossie documents. It reuses `catalog.py`'s column,
key, and join resolution rather than re-deriving them, so whether a field links
to a physical column is decided by one piece of logic whether a caller reads the
catalog or fingerprints it.

Each model is one semantic model, each saved measure one metric (named the way
the catalog names it), and each join one relationship with every ordered pair.
There is no transform half: SLayer builds nothing, and its ``sql_table`` already
is the relation the semantic model records.

**What the content hash covers.** The model as SLayer validated it, minus the
values SLayer caches from the warehouse (``sampled``, ``sampled_values``,
``distinct_count``). Those change whenever SLayer re-profiles, and a re-profile
reported as a definition change would bury the changes someone made.

**What the hash depends on.** It hashes the model as the pinned release dumps
it, so a release that adds a field or changes a default changes every hash
without anyone touching a file. The fingerprint therefore records the vendor,
the SLayer release and the model format version beside the hashes, and the
semantic axis reports a change in any of them as that, before it compares a
single definition.

**Joins keep their declared arity.** Each relationship carries the cardinality
oriented child to parent, the direction the relationship is written in, and
the layer says it captured cardinality, so a join declaring none reaches the
grain axis as unchecked rather than as holding.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from ..maintain.snapshot import (
    DeclarationFingerprint,
    MetricDef,
    RelationshipDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
    native_declaration_fingerprint,
)
from . import catalog as catalog_mod

if TYPE_CHECKING:
    from ..project_definitions import DeclaredRelationship
    from .loader import LoadedModel, LoadResult

__all__ = ["semantic_layer"]

# Column fields that are a cache of warehouse values rather than a definition.
_CACHED = frozenset({"sampled", "sampled_values", "distinct_count"})


def semantic_layer(
    loaded: LoadResult, *, connector: str | None
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated models."""

    if not loaded.models:
        return SemanticLayerSnapshot(
            **_provenance(), declarations={}, notes=loaded.notes()
        )

    by_name = {entry.model.name: entry.model for entry in loaded.models}
    relations: dict[str, str] = {}
    for entry in loaded.models:
        relation, _ = catalog_mod._relation(entry.model, connector)
        if relation:
            relations[entry.model.name] = relation

    semantic_models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    relationships: list[RelationshipDef] = []
    declarations: dict[str, DeclarationFingerprint] = {}
    notes: list[str] = list(loaded.notes())
    for entry in loaded.models:
        model = entry.model
        declared = native_declaration_fingerprint(
            *catalog_mod.declaration_inputs(model, connector), path=entry.file
        )
        if declared is not None:
            declarations[model.name] = declared
        # A model declaring no column, measure, or join says nothing semantic
        # about its relation yet: the relation itself is a schema-axis fact, and
        # recording it here would make a bare table reference indistinguishable
        # from a populated model in the added/removed/changed diff. Ossie's
        # fingerprint draws the same line for a dataset with no fields.
        if model.columns or model.measures or model.joins:
            semantic_models.append(_semantic_model_def(entry, relations))
        metrics.extend(_metric_defs(entry))
        for join in entry.model.joins:
            declared, note = catalog_mod.declared_relationship(
                entry.model, join, by_name, relations
            )
            if note:
                notes.append(note)
            if declared is not None:
                relationships.append(_relationship_def(declared, join, entry.file))

    return SemanticLayerSnapshot(
        semantic_models=semantic_models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        relationship_cardinality_captured=True,
        **_provenance(),
        declarations=declarations,
        notes=notes,
    )


def _provenance() -> dict[str, str]:
    """Which reader produced the hashes, and against which format."""

    from .dependency import SLAYER_VERSION
    from .loader import FORMAT_VERSION

    return {
        "vendor": "slayer",
        "vendor_release": SLAYER_VERSION,
        "format_version": str(FORMAT_VERSION),
    }


def _semantic_model_def(
    entry: LoadedModel, relations: dict[str, str]
) -> SemanticModelDef:
    model = entry.model
    relation = relations.get(model.name)
    dimensions: dict[str, str | None] = {}
    categorical: dict[str, str] = {}
    for col in model.columns:
        column = catalog_mod.physical_column(col) if relation else None
        dimensions[col.name] = column
        if column and catalog_mod._type(col) not in catalog_mod._TEMPORAL:
            categorical[col.name] = column
    return SemanticModelDef(
        name=model.name,
        path=entry.file,
        content_sha256=_definition_hash(_definition(model)),
        relation=relation,
        dimensions=dimensions,
        categorical_dimensions=categorical,
        keys=catalog_mod.declared_key_columns(model),
    )


def _metric_defs(entry: LoadedModel) -> list[MetricDef]:
    """Each saved measure, so a removed or rewritten one is its own finding.

    ``input_measures`` and ``input_metrics`` stay empty: a SLayer formula
    composes aggregations over columns, not named measures dex can resolve, and
    an inferred composition would be a MetricFlow-shaped fact the file never
    states.
    """

    model = entry.model
    return [
        MetricDef(
            name=catalog_mod.dimension_token(model.name, str(measure.name)),
            path=entry.file,
            content_sha256=_definition_hash(
                measure.model_dump(mode="json", exclude_none=True)
            ),
        )
        for measure in model.measures
    ]


def _relationship_def(
    declared: DeclaredRelationship, join: Any, path: str
) -> RelationshipDef:
    """One join, named by the author where they named it.

    A synthesized name otherwise, from the endpoints and pairs, because this is
    the identity drift is diffed by and an absent one would drop the row from
    that diff.
    """

    name = declared.name or (
        f"{declared.model}->{declared.to_model}:"
        + ",".join(f"{frm}={to}" for frm, to in declared.column_pairs)
    )
    declared_cardinality = catalog_mod._cardinality(join)
    entry = {
        "model": declared.model,
        "to_model": declared.to_model,
        "column_pairs": [list(pair) for pair in declared.column_pairs],
        "join_type": str(getattr(join.join_type, "value", join.join_type)),
        "cardinality": declared_cardinality,
    }
    return RelationshipDef(
        name=name,
        path=path,
        content_sha256=_definition_hash(entry),
        model=declared.model,
        to_model=declared.to_model,
        column_pairs=declared.column_pairs,
        # `declared_relationship` already turned a one_to_many join round so
        # the row reads child to parent; the arity turns with it.
        cardinality=(
            "many_to_one"
            if declared_cardinality == "one_to_many"
            else declared_cardinality
        ),
    )


def _definition(model: Any) -> dict[str, Any]:
    """The model as validated, without the warehouse values SLayer caches."""

    data = model.model_dump(mode="json", exclude_none=True)
    for column in data.get("columns") or []:
        for name in _CACHED:
            column.pop(name, None)
    return data


def _definition_hash(entry: Any) -> str:
    text = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
