"""The Metric View drift fingerprint: the maintain snapshot channel.

Builds the neutral `SemanticLayerSnapshot` from the validated set, reusing the
catalog's resolution (:class:`.catalog.Resolution`), so whether a field links to
a physical column is decided once, whichever channel asks.

- Each view is one semantic model on its source, with the fields and the
  aggregating measures that read that source mapped to the column behind each,
  and every other one mapped to None.
- Each joined table that is not another view's source is one semantic model of
  its own (``view.join.path``), with the fields that read it mapped to its
  columns and its ``rely.at_most_one_match`` key, so a column dropped from a
  joined table is reported against the table that lost it. A joined table that
  is another view's source is that view's model, which also carries the columns
  the reading view's fields and aggregates take from it, keyed
  ``reading_view.member``.
- Each measure is one metric named the way the catalog names it
  (``view.measure``), so a removed or rewritten one is its own finding.
- Each join with key pairs is one relationship with every ordered pair, oriented
  child to parent, so its arity is ``many_to_one``.

**Provenance.** ``vendor: databricks``, the Metric View YAML version as the
``format_version`` and the ported converter commit as the ``vendor_release``, so
a baseline from another reader or another spec version is reported as that
rather than as every view changed. Each file's own declared ``version`` is
recorded apart, under ``definition_versions``, so a view re-saved in another
version is reported as ``semantic_format_changed`` for that file while every
other view is still compared by content. The content hash covers the definition
as parsed, keys sorted.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from ..maintain.snapshot import (
    MetricDef,
    RelationshipDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
)
from . import catalog as catalog_mod
from .dependency import CONVERTER_COMMIT, METRIC_VIEW_SPEC_VERSION

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = ["semantic_layer"]


def semantic_layer(
    loaded: LoadResult, resolved: catalog_mod.Resolution
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated set."""

    notes = [*loaded.notes(), *resolved.notes]
    provenance = {
        "vendor": "databricks",
        "vendor_release": CONVERTER_COMMIT,
        "format_version": METRIC_VIEW_SPEC_VERSION,
        "definition_versions": dict(sorted(loaded.declared_versions.items())),
    }
    if loaded.empty:
        return SemanticLayerSnapshot(**provenance, declarations={}, notes=notes)

    joins = catalog_mod.declared_relationships(loaded, resolved)
    views = [view.name for view in loaded.views]
    keys: dict[str, list[list[str]]] = {}
    for declared in joins:
        if declared.join.at_most_one_match and declared.key_columns:
            listed = keys.setdefault(declared.key_model, [])
            if declared.key_columns not in listed:
                listed.append(list(declared.key_columns))

    models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    #: Columns a view's fields read from a table another view sits on, keyed by
    #: that view, so the table's own model carries them (qualified by the reading
    #: view, so they cannot collide with its own fields).
    borrowed: dict[str, dict[str, str | None]] = {}
    borrowed_measures: dict[str, dict[str, str | None]] = {}
    for view in loaded.views:
        dimensions: dict[str, str | None] = {}
        categorical: dict[str, str] = {}
        joined: dict[tuple[str, ...], dict[str, str | None]] = {}
        for member in view.fields:
            column = resolved.column(view.name, member.name)
            dimensions[member.name] = column
            if column and not member.is_time:
                categorical[member.name] = column
            here = resolved.members.get((view.name, member.name))
            if here is not None and here.owner:
                joined.setdefault(here.owner, {})[member.name] = here.column
        measures = {
            member.name: resolved.column(view.name, member.name)
            for member in view.measures
            if (here := resolved.members.get((view.name, member.name))) is not None
            and here.aggregate is not None
        }
        # An aggregate over a joined column (`SUM(lines.quantity)`) reads that
        # table, so it is fingerprinted there too, as a field read through a
        # join is.
        joined_measures: dict[tuple[str, ...], dict[str, str | None]] = {}
        for member in view.measures:
            here = resolved.members.get((view.name, member.name))
            if here is not None and here.aggregate is not None and here.owner:
                joined_measures.setdefault(here.owner, {})[member.name] = here.column
        models.append(
            SemanticModelDef(
                name=view.name,
                path=view.file,
                content_sha256=_hash(view.raw),
                relation=resolved.table(view.name).relation,
                dimensions=dimensions,
                categorical_dimensions=categorical,
                measures=measures,
                keys=keys.get(view.name, []),
                # A Metric View declares no key but `rely` (#1293).
                key_claims=["unique"] * len(keys.get(view.name, [])),
            )
        )
        for join in view.all_joins():
            name = f"{view.name}.{join.dotted}"
            table = resolved.table(view.name, join.path)
            other = next(
                (
                    v
                    for v in views
                    if v != view.name
                    and table.relation
                    and resolved.table(v).relation == table.relation
                ),
                None,
            )
            if other is not None:
                # That view's model fingerprints the table and its keys; it also
                # carries the columns this view's fields read from it, so a
                # column dropped there is still reported.
                borrowed.setdefault(other, {}).update(
                    {
                        catalog_mod.member_token(view.name, field): column
                        for field, column in joined.get(join.path, {}).items()
                    }
                )
                borrowed_measures.setdefault(other, {}).update(
                    {
                        catalog_mod.member_token(view.name, measure): column
                        for measure, column in joined_measures.get(
                            join.path, {}
                        ).items()
                    }
                )
                continue
            models.append(
                SemanticModelDef(
                    name=name,
                    path=view.file,
                    content_sha256=_hash(join.raw),
                    relation=table.relation,
                    dimensions=joined.get(join.path, {}),
                    measures=joined_measures.get(join.path, {}),
                    keys=keys.get(name, []),
                    key_claims=["unique"] * len(keys.get(name, [])),
                )
            )
        metrics.extend(
            MetricDef(
                name=catalog_mod.member_token(view.name, member.name),
                path=view.file,
                content_sha256=_hash(member.raw),
            )
            for member in view.measures
        )

    by_name = {model.name: model for model in models}
    for target, columns in borrowed.items():
        model = by_name[target]
        model.dimensions = {**model.dimensions, **columns}
    for target, columns in borrowed_measures.items():
        model = by_name[target]
        model.measures = {**model.measures, **columns}

    relationships = [
        RelationshipDef(
            name=declared.relationship.name or declared.view.name,
            path=declared.view.file,
            content_sha256=_hash(declared.join.raw),
            model=declared.relationship.model,
            to_model=declared.relationship.to_model,
            column_pairs=declared.relationship.column_pairs,
            cardinality="many_to_one",
        )
        for declared in joins
    ]
    return SemanticLayerSnapshot(
        semantic_models=models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        relationship_cardinality_captured=True,
        **provenance,
        declarations={},
        notes=notes,
    )


def _hash(definition: Any) -> str:
    """A content hash of one definition as parsed, keys sorted."""
    text = json.dumps(definition, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
