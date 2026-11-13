"""The Semantic View drift fingerprint: the maintain snapshot channel.

Builds the neutral `SemanticLayerSnapshot` from the validated set, reusing the
catalog's resolution (:class:`.catalog.Resolution`), so whether a member links
to a physical column is decided once, whichever channel asks.

- The view's own keys (description, `variables`, `max_staleness`, tags) are one
  definition named after the view, with no relation.
- Each logical table is one semantic model on its base table, named as the
  catalog names it, with its dimensions, time dimensions and facts mapped to the
  column behind each (``None`` for a computed one), each aggregating table metric
  to the column it aggregates, and its primary and unique keys.
- Each table metric and derived metric is one metric, named as the catalog names
  it, so a removed or rewritten one is its own finding.
- Each key-equality relationship is one relationship with every ordered pair,
  child (left) to parent (right), with its arity: ``many_to_one``, or
  ``one_to_one`` where the left columns are a key too, or ``many_to_many`` where
  the right ones are not. ASOF and range relationships are fingerprinted with
  their equality pairs and no arity, so ``maintain grain`` names them unchecked.

**Identities are lower case**: every model, member, metric and relationship
name, so a read-back (which upper-cases them) is the same definition, not a
removal and an addition.

**A canonical form, so a read-back matches its file.** Snowflake's
``SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`` returns a view with every identifier in
upper case, its lists sorted, ``access_modifier: public_access`` and the column's
``data_type`` written on every member, ``sample_values`` re-typed, and empty
``synonyms`` and ``description`` on a derived metric. None of that changes the
definition, so the hash covers a canonical form: names, columns and table parts
folded to lower case (Snowflake identifiers are case-insensitive), lists of named
elements keyed by name, the defaulted values dropped, and ``data_type`` and
``sample_values`` left out (a column type is the warehouse's, held to it by the
schema axis; a sample is data, not definition). Expressions are hashed as
written.

**Provenance.** ``vendor: snowflake`` and dex's own rule-set revision as the
``format_version`` (the format has no ``version`` key), so a baseline from
another reader or another rule set is reported as that rather than as every
definition changed.
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
from .dependency import SEMANTIC_VIEW_FORMAT_VERSION
from .model import DIMENSION, METRIC

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = ["canonical", "semantic_layer"]

#: Keys whose value is an identifier, compared ignoring case.
_IDENTIFIERS = frozenset(
    {
        "name",
        "left_table",
        "right_table",
        "left_column",
        "right_column",
        "start_column",
        "end_column",
        "table",
        "dimension",
        "database",
        "schema",
    }
)
#: Keys whose value is a list of identifiers.
_IDENTIFIER_LISTS = frozenset({"columns", "using_relationships"})
#: Keys left out of the hash: filled in or re-typed by Snowflake on read-back,
#: and no part of the definition (an enum's `sample_values` excepted).
_IGNORED = frozenset({"data_type", "sample_values"})
#: The view-level keys fingerprinted as the view's own definition.
_VIEW_KEYS = ("description", "variables", "max_staleness", "tags")


def canonical(value: Any, key: str | None = None) -> Any:
    """``value`` in the form the fingerprint hashes (see the module docstring)."""

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "sample_values" and value.get("is_enum") is True:
                # An enum's values are its complete allowed set: a definition,
                # hashed (never stored), and compared as text, since read-back
                # re-types them.
                out[k] = sorted(str(item) for item in v or ())
                continue
            if k in _IGNORED:
                continue
            if k == "access_modifier" and v == "public_access":
                continue
            if k in ("synonyms", "description") and v in ([], "", None):
                continue
            out[k] = canonical(v, k)
        return out
    if isinstance(value, list):
        if key in _IDENTIFIER_LISTS:
            return [str(v).lower() for v in value]
        items = [canonical(v) for v in value]
        if items and all(isinstance(i, dict) and "name" in i for i in items):
            # A structured name (a tag's `{database, schema, tag}`) is keyed by
            # its sorted form, so reordering its fields is not a change.
            return {
                json.dumps(i["name"], sort_keys=True)
                if isinstance(i["name"], dict)
                else str(i["name"]): i
                for i in items
            }
        return items
    if key in _IDENTIFIERS and isinstance(value, str):
        return value.lower()
    return value


def semantic_layer(
    loaded: LoadResult, resolved: catalog_mod.Resolution
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated set."""

    notes = [*loaded.notes(), *resolved.notes]
    provenance = {
        "vendor": "snowflake",
        "format_version": SEMANTIC_VIEW_FORMAT_VERSION,
    }
    if loaded.empty:
        return SemanticLayerSnapshot(**provenance, declarations={}, notes=notes)

    models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    declarations: dict[str, DeclarationFingerprint] = {}
    for view in loaded.views:
        # The view's own keys (its description, `variables` and their defaults,
        # `max_staleness`, tags) change what it answers without touching a
        # table, so they are a definition of their own, named after the view.
        # The Cortex Analyst keys are left out: Snowflake rewrites them on
        # read-back and no query reads them.
        own = {k: view.raw[k] for k in _VIEW_KEYS if k in view.raw}
        models.append(
            SemanticModelDef(
                name=view.name.lower(), path=view.file, content_sha256=_hash(own)
            )
        )
        for table in view.tables:
            dimensions: dict[str, str | None] = {}
            categorical: dict[str, str] = {}
            measures: dict[str, str | None] = {}
            for member in table.members():
                here = resolved.member(view, member)
                name = member.name.lower()
                if member.kind == METRIC:
                    if here.aggregate is not None:
                        measures[name] = here.column
                    continue
                dimensions[name] = here.column
                if member.kind == DIMENSION and here.column:
                    categorical[name] = here.column
            keys = [[resolved.fold(c) for c in key] for key in table.declared_keys()]
            # The primary key first, then each `unique_keys` entry: only the
            # first is the grain (#1293).
            claims = ["primary"] * bool(table.primary_key) + ["unique"] * len(
                table.unique_keys
            )
            model_name = resolved.model(view, table.name)
            if table.primary_key:
                # The whole declaration a Semantic View carries: no metadata
                # key exists, so the primary key is what `explore --use-project`
                # reads back as the grain (see `catalog.definitions`).
                declared = native_declaration_fingerprint(
                    [resolved.fold(c) for c in table.primary_key],
                    None,
                    [],
                    path=view.file,
                )
                if declared is not None:
                    declarations[model_name] = declared
            models.append(
                SemanticModelDef(
                    name=model_name.lower(),
                    path=view.file,
                    content_sha256=_hash(table.raw),
                    relation=resolved.relation(view, table.name),
                    dimensions=dimensions,
                    categorical_dimensions=categorical,
                    measures=measures,
                    keys=keys,
                    key_claims=claims,
                )
            )
            metrics.extend(
                MetricDef(
                    name=resolved.token(view, member).lower(),
                    path=view.file,
                    content_sha256=_hash(member.raw),
                    input_measures=_lower(
                        [member.name]
                        if resolved.member(view, member).aggregate is not None
                        else catalog_mod.underlying(view, member, resolved)
                    ),
                )
                for member in table.metrics
            )
        metrics.extend(
            MetricDef(
                name=resolved.token(view, member).lower(),
                path=view.file,
                content_sha256=_hash(member.raw),
                input_measures=_lower(catalog_mod.underlying(view, member, resolved)),
                input_metrics=_lower(catalog_mod.metric_inputs(view, member, resolved)),
            )
            for member in view.metrics
        )

    relationships = [
        RelationshipDef(
            name=(declared.declared.name or declared.relationship.name).lower(),
            path=declared.view.file,
            content_sha256=_hash(declared.relationship.raw),
            model=declared.declared.model.lower(),
            to_model=declared.declared.to_model.lower(),
            column_pairs=declared.declared.column_pairs,
            cardinality=declared.cardinality,
        )
        for declared in catalog_mod.declared_relationships(loaded, resolved)
    ]
    for view in loaded.views:
        for rel in view.relationships:
            if rel.equi:
                continue
            relationships.append(
                RelationshipDef(
                    name=f"{view.name}.{rel.name}".lower(),
                    path=view.file,
                    content_sha256=_hash(rel.raw),
                    model=resolved.model(view, rel.left_table).lower(),
                    to_model=resolved.model(view, rel.right_table).lower(),
                    column_pairs=[
                        (resolved.fold(p.left), resolved.fold(p.right))  # type: ignore[arg-type]
                        for p in rel.columns
                        if p.kind == "equal"
                    ],
                    cardinality=None,
                )
            )
    return SemanticLayerSnapshot(
        semantic_models=models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        relationship_cardinality_captured=True,
        **provenance,
        declarations=declarations,
        notes=notes,
    )


def _lower(names: list[str]) -> list[str]:
    """Names as the fingerprint identifies them: lower case, since Snowflake
    identifiers are case-insensitive and its read-back upper-cases them."""
    return [name.lower() for name in names]


def _hash(definition: Any) -> str:
    """A content hash of one definition's canonical form, keys sorted."""
    text = json.dumps(canonical(definition), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
