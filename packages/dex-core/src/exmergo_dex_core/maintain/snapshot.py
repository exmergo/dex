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
    """

    files: dict[str, str] = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)
    model_paths: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceTable] = Field(default_factory=list)
    model_sources: dict[str, list[str]] = Field(default_factory=dict)
    model_refs: dict[str, list[str]] = Field(default_factory=dict)
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
    """

    semantic_models: list[SemanticModelDef] = Field(default_factory=list)
    metrics: list[MetricDef] = Field(default_factory=list)
    relationships: list[RelationshipDef] = Field(default_factory=list)
    relationships_and_keys_captured: bool = False
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
    for parsed, path in yaml_documents(view):
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
    )


def semantic_layer_snapshot(view: DbtProjectView) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the project's YAML files."""

    semantic_models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    for kind, entry, path in semantic_yaml_entries(view):
        if kind == "semantic_model":
            semantic_models.append(_semantic_model_def(entry, path))
        else:
            metrics.append(_metric_def(entry, path))
    return SemanticLayerSnapshot(semantic_models=semantic_models, metrics=metrics)


# RETRO: Compatibility names: the JSON field is intentionally still `semantic_layer`.
SemanticLayer = SemanticLayerSnapshot
semantic_layer = semantic_layer_snapshot


# --- helpers -----------------------------------------------------------------


def _definition_hash(entry: dict[str, Any]) -> str:
    return content_hash(json.dumps(entry, sort_keys=True, default=str))


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
