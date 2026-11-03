"""The known-good baseline that drift is measured against: `.dex/snapshot.json`.

The snapshot is a frozen fingerprint of two worlds at a moment the user vouched
for. The warehouse side pins the explore cache wholesale (datasets with column
profiles, grain verdicts, and verified relationships), so the grain baseline is
the exact-distinct verdicts explore already computed and snapshotting from a
cache opens no connection and spends nothing. Without a cache, a metadata-only
baseline is captured directly (free on every connector); that covers the schema
and volume axes but leaves no grain or cardinality baseline until `explore map`
runs.

The project side is fingerprinted per layer rather than via the compiled
manifest: the transformation layer as file hashes, model names, and declared
sources; the semantic layer as named definitions, each with a content hash and
the physical columns it references. Fingerprinting the definitions themselves
(not dbt's serialization of them) keeps the baseline stable across dbt versions
and independent of whether the project was last compiled.

Like the cache, the snapshot is never truth: the dbt project stays canonical,
and deleting the snapshot loses only the baseline. No data value is stored here;
cardinality baselines are distinct counts, and min/max stay whatever the profile
pass deemed safe to record.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..adapters.base import Adapter
from ..cache import ColumnProfile, Dataset, DexCache, Relationship, tool_version
from ..dbt_project import (
    REF_PATTERN,
    SOURCE_PATTERN,
    DbtProjectView,
    content_hash,
    metric_inputs,
    node_files,
    node_name,
    physical_column,
    semantic_yaml_entries,
    yaml_documents,
)
from ..guards.pii_meta import entry_meta

#: The ``config.meta`` namespace a declaration lives under. The same string as
#: ``transform.declarations.DEX_META_KEY``, restated rather than imported so
#: reading a baseline does not load the declaration writer.
DEX_META_KEY = "dex"

SNAPSHOT_SCHEMA_VERSION = 1

#: Snapshot schema versions this engine can read.
#:
#: The field was stamped on every write from the beginning and read by nothing,
#: so it could not have told anyone anything. This is the set that gives it a
#: meaning, and the policy is REFUSE rather than migrate: a baseline is cheap to
#: regenerate (`maintain snapshot`), the alternative needs a migration function
#: per version that must itself be right about a document nobody has looked at
#: in a while, and a wrongly-migrated baseline is worse than an absent one
#: because drift measured against it looks like a result.
#:
#: There is only one version today, so nothing is refused yet. When a second
#: arrives, add it here if and only if this engine can genuinely read it; adding
#: it to keep an old baseline working is how the field stops meaning anything
#: again.
#:
#: A set rather than the ``<`` comparison the query firewall uses on
#: ``CACHE_SCHEMA_VERSION``, deliberately. That one degrades: an old cache is
#: still usable, so the firewall adds a hint and carries on. A baseline is not
#: usable-but-thin in the same way. It is the thing every axis measures against,
#: so a version this engine does not understand has no degraded reading, only a
#: wrong one, and membership says exactly that where ``<`` would also have to
#: pick a side on newer-than-this-engine.
#:
#: The check that reads this runs on a *parsed* ``Snapshot``, so it names a
#: version only for a document the current model still validates. A future
#: version that also changed shape arrives as a parse failure and is refused
#: with the same remedy and no version named. Widening that would take a
#: contract change on ``Store``, which owns the raw document.
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({SNAPSHOT_SCHEMA_VERSION})


class WarehouseBaseline(BaseModel):
    """The warehouse as last mapped: what schema/volume/grain drift diffs against."""

    datasets: list[Dataset] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    def without_column_detail(self) -> list[str]:
        """Identifiers whose columns this baseline never captured.

        **An empty column list means *unknown*, not *empty*.** Every warehouse
        object has columns, so a baseline holding none for one of them recorded
        an absence of evidence, never evidence of absence. ``explore map``
        profiles the top-ranked objects and enters the rest as metadata alone,
        so on a warehouse past the rank cutoff most of a pinned baseline can
        land here.

        The distinction is load-bearing for the schema axis: diffing live
        columns against an unknown set would report every column of every
        unprofiled object as newly added. It also bounds what the grain and
        cardinality axes can see, since neither has keys to probe without a
        profile. One definition, because the pin-time warning and the detector
        have to agree on which objects are thin.
        """

        return sorted(d.identifier for d in self.datasets if not d.columns)


class SourceTable(BaseModel):
    """One declared source table: a contract the warehouse must keep honoring.

    ``path`` is provenance for the ``dangling_source`` finding: the file an
    analyst would open to fix the declaration. It is ``None`` for a project
    format that declares its sources somewhere other than a file of their own,
    and nothing ever opens it. Inventing a plausible path instead would attach a
    file that is not there to a high-severity finding, which is the trap
    :class:`SemanticModelDef` avoids one field over by mapping unresolvable
    columns to ``None``.
    """

    source_name: str
    schema_name: str | None = None
    table: str
    columns: list[str] = Field(default_factory=list)
    path: str | None = None


class DeclarationFingerprint(BaseModel):
    """What one model declares about its meaning, as one hash per field (#497).

    A declaration (#491) is the model's ``description`` and its
    ``config.meta.dex`` (grain, population, assumptions), and per column the
    column's ``description`` and its own ``config.meta.dex`` (role, additivity,
    unit, and the rest). ``fields`` and ``columns`` map each field that is set to
    a hash of its canonical form, so drift can name the field that changed
    without the baseline holding the declaration itself: a population filter's
    values are authored text, but they are still values, and the snapshot holds
    none (see the module docstring).

    Every key under ``meta.dex`` is a field, not only the ones #491 defines, so a
    declaration field added later is fingerprinted without a change here. A
    column with nothing declared is absent from ``columns`` rather than present
    and empty, and a model with nothing declared anywhere is absent from
    :attr:`TransformLayer.declarations`.

    ``path`` is the YAML file the entry was read from: provenance for the
    finding, never opened, and excluded from comparison so moving an entry to
    another file is not a change to what it says.
    """

    path: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, dict[str, str]] = Field(default_factory=dict)


class TransformLayer(BaseModel):
    """The transformation layer's fingerprint: file hashes, model names, and the
    source declarations that bind the project to warehouse objects.

    ``model_sources`` and ``model_refs`` record each model's ``source()`` and
    ``ref()`` calls (source entries as ``source_name.table``), which is how a
    warehouse-level finding is traced to the models it lands on without
    re-reading the project at detection time.

    ``model_paths`` maps each name in ``models`` to the file that builds it, so
    :func:`~.drift.transform_drift` can look up that model's entry in ``files``
    without guessing: a model and its schema YAML routinely share a filename
    stem (``stg_orders.sql`` next to ``stg_orders.yml``), so recovering the path
    from the name by stem alone would risk hashing the wrong file. A baseline
    pinned before this field existed has an empty ``model_paths``, and a model
    missing from it is a model ``transform_drift`` cannot content-diff, not one
    it reports changed.

    ``notes`` is how a project format says what it could not supply, the way
    ``ProjectDefinitions.notes`` does on the declarations channel. A format whose
    layer is faithful but narrower than a dbt project's (no file hashes because it
    has no files, say) records that here, and the maintain commands fold it into
    their warnings. It is informational only: no detector reads it, and it is
    excluded from every comparison, so a changed note is never drift. Anything dex
    must *decide* from belongs in a project tier, which is checkable, rather than
    in prose the engine would have to trust.

    ``declarations`` is each declared model's :class:`DeclarationFingerprint`.
    ``None`` means *not captured*: every baseline pinned before #497, and any
    format that does not read declarations. ``{}`` means captured, and nothing
    is declared. The two are kept apart for the reason
    ``SemanticLayerSnapshot.relationships_and_keys_captured`` gives: read as
    empty, an old baseline would report every declaration in the project as
    newly added, or a project with none as clean, when the axis was never
    checked at all.
    """

    files: dict[str, str] = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)
    model_paths: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceTable] = Field(default_factory=list)
    model_sources: dict[str, list[str]] = Field(default_factory=dict)
    model_refs: dict[str, list[str]] = Field(default_factory=dict)
    declarations: dict[str, DeclarationFingerprint] | None = None
    notes: list[str] = Field(default_factory=list)


class SemanticModelDef(BaseModel):
    """One semantic model, reduced to what drift detection needs: a content hash
    for definition changes, the dbt model it sits on, and each entity,
    dimension, and measure mapped to the physical column it references.

    A column is recorded only when the reference resolves to a single physical
    column (a bare-identifier ``expr``, or a name with no ``expr``, which dbt
    treats as the column itself); computed expressions map to ``None``.
    Guessing columns out of expressions would turn every refactor into a false
    dangling-ref finding. ``path`` is ``None`` on the same principle: it is
    provenance carried on the ``definition_changed`` finding, absent for a format
    whose definitions are objects rather than files, and never opened.

    ``relation`` and ``keys`` are additive, #409 fields (empty/``None`` for
    every baseline pinned before this field existed, and for dbt, which has no
    populated analogue for either): a format whose semantic model *is* a
    direct relation reference, rather than a name a transformation project
    later builds, records that relation here so drift can match it straight
    against the warehouse without a ``model_ref``. ``keys`` is every column
    combination the format declares independently unique, one list per
    declaration regardless of arity, so a single-column and a composite key
    are recorded the same way instead of one being disguised as several
    single ones.
    """

    name: str
    path: str | None = None
    content_sha256: str
    model_ref: str | None = None
    relation: str | None = None
    entities: dict[str, str | None] = Field(default_factory=dict)
    dimensions: dict[str, str | None] = Field(default_factory=dict)
    categorical_dimensions: dict[str, str] = Field(default_factory=dict)
    measures: dict[str, str | None] = Field(default_factory=dict)
    keys: list[list[str]] = Field(default_factory=list)

    def referenced_columns(self) -> set[str]:
        return {
            column
            for mapping in (self.entities, self.dimensions, self.measures)
            for column in mapping.values()
            if column is not None
        } | {column for combo in self.keys for column in combo}

    def structural_columns(self) -> set[str]:
        """Columns whose loss breaks the model as a whole (entities,
        dimensions, and declared keys), as opposed to a measure column that
        breaks only the measures on it."""

        return {
            column
            for mapping in (self.entities, self.dimensions)
            for column in mapping.values()
            if column is not None
        } | {column for combo in self.keys for column in combo}


class MetricDef(BaseModel):
    """One metric: a content hash plus the measures and metrics it draws from,
    so warehouse drift can be traced through measures up to the metrics it
    ultimately biases.

    ``path`` follows :class:`SemanticModelDef`: provenance for
    ``definition_changed``, ``None`` where the definition has no file, never
    opened."""

    name: str
    path: str | None = None
    content_sha256: str
    input_measures: list[str] = Field(default_factory=list)
    input_metrics: list[str] = Field(default_factory=list)


class RelationshipDef(BaseModel):
    """One declared join between two semantic models, with its full ordered
    column pairs (#409).

    Composite and single-column joins are recorded the same way: dropping a
    pair's second column here would be the exact partial-edge risk #408
    refused at the read-catalog layer, one level down. It sits beside
    :class:`SemanticModelDef` rather than on it because a join names two
    models, not one.

    ``name`` is never ``None``: a format whose declarations are themselves
    unnamed (Ossie's relationships are optional to name) synthesizes a stable
    one from the endpoints and columns, because this is the identity
    ``semantic_free_drift`` diffs added/removed/changed by, and an identity
    that is absent for some rows would silently exclude them from that diff.
    """

    name: str
    path: str | None = None
    content_sha256: str
    model: str
    to_model: str
    column_pairs: list[tuple[str, str]] = Field(default_factory=list)
    #: The declared arity read in this row's own direction, ``model`` to
    #: ``to_model``: ``many_to_one``, ``one_to_one`` or ``many_to_many``, or
    #: ``None`` where nothing was declared. A format that declares a join the
    #: other way round (``one_to_many``) is recorded oriented child to parent,
    #: so the grain axis has one rule to read rather than four. Meaningful only
    #: where the layer sets ``relationship_cardinality_captured``.
    cardinality: str | None = None


class OpaqueDef(BaseModel):
    """A definition only the vendor's compiler can read, fingerprinted by bytes.

    Some formats hold definitions dex reads no structure out of (a Cube JS
    model, a Jinja-templated file, an ``extends``). They are still part of the
    layer, so the fingerprint records them by their content: a change is then
    reported as a change, never read as nothing having moved, and never
    compared structurally either. ``name`` identifies the definition (a file,
    or ``file: element`` where only one element is opaque); ``reason`` says why
    dex does not read it.
    """

    name: str
    path: str
    reason: str
    content_sha256: str


class SemanticLayerSnapshot(BaseModel):
    """The semantic layer's fingerprint: every named definition the project holds.

    ``notes`` carries the same meaning as on :class:`TransformLayer`, and for the
    same reason: this is a return type a non-dbt project format fills in, and a
    layer that is narrower than a dbt one needs somewhere to say so that travels
    with the value rather than sitting beside it.

    ``relationships_and_keys_captured`` is #409's answer to the hazard its own
    fields create: ``relationships`` and every ``SemanticModelDef.keys`` entry
    are additive fields a baseline pinned before they existed carries empty,
    and empty there is indistinguishable from "this layer declares no
    relationships or keys" -- which would read as a clean bill rather than as
    "not checked". This flag is what ``semantic_free_drift`` reads instead of
    trusting the emptiness: ``False`` (the default, so an old baseline and a
    format that has never populated these -- dbt included, which has no
    composite-relationship or multi-column-key concept at the semantic-model
    level -- both land here) skips every relationship/key finding rather than
    reporting false drift or a false clean bill; only a layer that set it
    ``True`` on both sides of a comparison is one the two new checks run
    against.

    ``relationship_cardinality_captured`` is the same idea for
    ``RelationshipDef.cardinality``: a layer that sets it declares that a
    ``None`` there means the author declared no arity, which the grain axis
    reports as unchecked, rather than that the format has nowhere to declare
    one.

    ``vendor``, ``vendor_release`` and ``format_version`` say which reader
    produced the fingerprint and against which version of the vendor's own
    format. The content hashes are hashes of what that reader produced, so two
    fingerprints from different readers, or from one reader across a release
    that changed its model, are not comparable definition by definition, and
    ``semantic_free_drift`` reports the change itself instead of every model
    in the layer as changed. ``None`` is "not recorded" (every baseline pinned
    before these fields existed), and an unrecorded side is never read as a
    change. Excluded from every content hash.
    """

    semantic_models: list[SemanticModelDef] = Field(default_factory=list)
    metrics: list[MetricDef] = Field(default_factory=list)
    relationships: list[RelationshipDef] = Field(default_factory=list)
    relationships_and_keys_captured: bool = False
    relationship_cardinality_captured: bool = False
    vendor: str | None = None
    vendor_release: str | None = None
    format_version: str | None = None
    #: Definitions recorded by their bytes alone, and whether this layer records
    #: them: a baseline pinned before they were captured carries the list empty,
    #: which must not read as every one of them being new (#912).
    opaque_definitions: list[OpaqueDef] = Field(default_factory=list)
    opaque_captured: bool = False
    notes: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    """The whole baseline in `.dex/snapshot.json`.

    ``warehouse_from`` records how the warehouse side was captured ("cache" or
    "metadata"), because a metadata-only baseline cannot back the grain or
    cardinality axes and `check` must say so instead of reporting a clean bill.
    """

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    created_at: str
    connector: str | None = None
    tool_version: str | None = Field(default_factory=tool_version)
    warehouse: WarehouseBaseline = Field(default_factory=WarehouseBaseline)
    warehouse_from: str = "cache"
    cache_updated_at: str | None = None
    transform_layer: TransformLayer | None = None
    semantic_layer: SemanticLayerSnapshot | None = None


def warehouse_from_cache(cache: DexCache) -> WarehouseBaseline:
    """Pin the explore cache as the warehouse baseline, verbatim."""

    return WarehouseBaseline(
        datasets=[d.model_copy(deep=True) for d in cache.datasets],
        relationships=[r.model_copy(deep=True) for r in cache.relationships],
    )


def warehouse_from_metadata(adapter: Adapter) -> WarehouseBaseline:
    """A metadata-only baseline captured directly: names, types, nullability,
    and row/byte counts, with no aggregate scans. Free on every connector, but
    it carries no uniqueness or cardinality verdicts, so the grain and
    cardinality axes have nothing to diff against."""

    datasets: list[Dataset] = []
    for listed in adapter.list_objects():
        meta, columns = adapter.table_metadata(listed.identifier)
        datasets.append(
            Dataset(
                identifier=meta.identifier,
                object_type=meta.object_type,
                row_count=meta.row_count,
                byte_size=meta.byte_size,
                columns=[
                    ColumnProfile(
                        name=col.name, data_type=col.data_type, nullable=col.nullable
                    )
                    for col in columns
                ],
            )
        )
    return WarehouseBaseline(datasets=datasets)


def transform_layer(view: DbtProjectView) -> TransformLayer:
    """Fingerprint the transformation layer from the project view.

    ``models`` is every node the project builds and names after its file: a
    model, a snapshot, or a seed. It reads that from ``node_files`` rather than
    from every ``.sql`` in the view, which is what keeps a macro (jinja, builds
    nothing, ``ref()``-able by no one) from counting as a model and a snapshot
    or seed from being missed now that both are loaded.

    ``files`` stays the whole editable surface: it is a change fingerprint of
    what a human can edit, not a node list. ``model_paths`` is the narrower
    index from a node's name back to the one entry in ``files`` that builds
    it, which is what lets :func:`~.drift.transform_drift` diff a model's
    content hash across snapshots without guessing at a path from the name
    alone.
    """

    models: list[str] = []
    model_paths: dict[str, str] = {}
    model_sources: dict[str, list[str]] = {}
    model_refs: dict[str, list[str]] = {}
    for path, source in node_files(view).items():
        model = node_name(path)
        models.append(model)
        model_paths[model] = path
        source_calls = sorted(
            {
                f"{name}.{table}"
                for name, table in SOURCE_PATTERN.findall(source.content)
            }
        )
        ref_calls = sorted(set(REF_PATTERN.findall(source.content)) - {model})
        if source_calls:
            model_sources[model] = source_calls
        if ref_calls:
            model_refs[model] = ref_calls
    models.sort()
    sources: list[SourceTable] = []
    documents = yaml_documents(view)
    for parsed, path in documents:
        for src in parsed.get("sources") or []:
            if not isinstance(src, dict) or not src.get("name"):
                continue
            for table in src.get("tables") or []:
                if not isinstance(table, dict) or not table.get("name"):
                    continue
                sources.append(
                    SourceTable(
                        source_name=src["name"],
                        schema_name=src.get("schema"),
                        table=table["name"],
                        columns=[
                            col["name"]
                            for col in table.get("columns") or []
                            if isinstance(col, dict) and col.get("name")
                        ],
                        path=path,
                    )
                )
    return TransformLayer(
        files={path: source.sha256 for path, source in view.files.items()},
        models=models,
        model_paths=model_paths,
        sources=sources,
        model_sources=model_sources,
        model_refs=model_refs,
        declarations=declaration_fingerprints(documents),
    )


def declaration_fingerprints(
    documents: list[tuple[dict[str, Any], str]],
) -> dict[str, DeclarationFingerprint]:
    """Each declared model's per-field fingerprint, from the project's YAML.

    Read from every ``models:`` entry, where #491 renders a declaration, through
    the same ``meta`` precedence dbt applies (``config.meta`` over a top-level
    ``meta``), so a key a human moved is read where they put it. A model entry
    that appears in two files (which dbt refuses) is read from the first path in
    sorted order, so the fingerprint does not depend on file-system order.
    """

    out: dict[str, DeclarationFingerprint] = {}
    for parsed, path in sorted(documents, key=lambda document: document[1]):
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            if entry["name"] in out:
                continue
            fields = _declared_fields(entry)
            columns: dict[str, dict[str, str]] = {}
            for column in entry.get("columns") or []:
                if not isinstance(column, dict) or not isinstance(
                    column.get("name"), str
                ):
                    continue
                column_fields = _declared_fields(column)
                if column_fields:
                    columns[column["name"]] = column_fields
            if fields or columns:
                out[entry["name"]] = DeclarationFingerprint(
                    path=path, fields=fields, columns=columns
                )
    return out


def semantic_layer_snapshot(view: DbtProjectView) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the project's YAML files."""

    semantic_models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    for kind, entry, path in semantic_yaml_entries(view):
        if kind == "semantic_model":
            semantic_models.append(_semantic_model_def(entry, path))
        else:
            metrics.append(_metric_def(entry, path))
    return SemanticLayerSnapshot(
        semantic_models=semantic_models, metrics=metrics, vendor="dbt"
    )


# RETRO: Compatibility names: the JSON field is intentionally still `semantic_layer`.
SemanticLayer = SemanticLayerSnapshot
semantic_layer = semantic_layer_snapshot


# --- helpers -----------------------------------------------------------------


def _definition_hash(entry: dict[str, Any]) -> str:
    return content_hash(json.dumps(entry, sort_keys=True, default=str))


def _declared_fields(entry: dict[str, Any]) -> dict[str, str]:
    """One model or column entry's declared fields, each hashed canonically.

    The ``description`` plus every key under ``meta.dex``. A field written but
    empty (``description: ""``, ``grain: []``) declares nothing and is left out,
    which is how #493's ``declared`` reader treats it too.
    """

    stated: dict[str, Any] = {}
    description = entry.get("description")
    if isinstance(description, str) and description.strip():
        stated["description"] = description
    dex = entry_meta(entry).get(DEX_META_KEY)
    if isinstance(dex, dict):
        for key, value in dex.items():
            if value not in (None, "", [], {}):
                stated[str(key)] = value
    return {
        field: content_hash(
            json.dumps(_canonical(field, value), sort_keys=True, default=str)
        )
        for field, value in stated.items()
    }


#: Lists whose order says nothing. A grain is a set of columns, and a population
#: is the conjunction of its filters, each over a set of values; reordering any
#: of them in the YAML restates the same declaration. Every other list keeps its
#: order, because dex cannot know an order is meaningless in a field it does not
#: define.
_UNORDERED = {("grain",), ("population", "filters"), ("assumptions",)}
_UNORDERED_VALUES = {"includes", "excludes"}


def _canonical(field: str, value: Any, path: tuple[str, ...] = ()) -> Any:
    """``value`` with formatting and ordering that carry no meaning removed.

    Whitespace inside a string is collapsed, so a description reflowed from a
    plain scalar into a folded block (``>``) is not a change. Mapping order is
    already discarded by the ``sort_keys`` dump that follows.
    """

    path = path or (field,)
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {
            str(key): _canonical(field, item, (*path, str(key)))
            for key, item in value.items()
        }
    if isinstance(value, list):
        items = [_canonical(field, item, path) for item in value]
        if path in _UNORDERED or path[-1] in _UNORDERED_VALUES:
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return items
    return value


def _semantic_model_def(entry: dict[str, Any], path: str) -> SemanticModelDef:
    model_match = REF_PATTERN.search(str(entry.get("model", "")))
    dimensions = [d for d in entry.get("dimensions") or [] if isinstance(d, dict)]
    measures = [m for m in entry.get("measures") or [] if isinstance(m, dict)]
    entities = [e for e in entry.get("entities") or [] if isinstance(e, dict)]

    def mapping_of(entries: list[dict[str, Any]]) -> dict[str, str | None]:
        return {
            e["name"]: physical_column(e)
            for e in entries
            if isinstance(e.get("name"), str)
        }

    return SemanticModelDef(
        name=entry["name"],
        path=path,
        content_sha256=_definition_hash(entry),
        model_ref=model_match.group(1) if model_match else None,
        entities=mapping_of(entities),
        dimensions=mapping_of(dimensions),
        categorical_dimensions={
            name: column
            for name, column in mapping_of(
                [
                    d
                    for d in dimensions
                    if str(d.get("type", "")).lower() == "categorical"
                ]
            ).items()
            if column is not None
        },
        measures=mapping_of(measures),
    )


def _metric_def(entry: dict[str, Any], path: str) -> MetricDef:
    measures, metrics = metric_inputs(entry)
    return MetricDef(
        name=entry["name"],
        path=path,
        content_sha256=_definition_hash(entry),
        input_measures=measures,
        input_metrics=metrics,
    )
