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
    physical_column,
    semantic_yaml_entries,
    yaml_documents,
)

SNAPSHOT_SCHEMA_VERSION = 1


class WarehouseBaseline(BaseModel):
    """The warehouse as last mapped: what schema/volume/grain drift diffs against."""

    datasets: list[Dataset] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class SourceTable(BaseModel):
    """One declared dbt source table: a contract the warehouse must keep honoring."""

    source_name: str
    schema_name: str | None = None
    table: str
    columns: list[str] = Field(default_factory=list)
    path: str


class TransformLayer(BaseModel):
    """The transformation layer's fingerprint: file hashes, model names, and the
    source declarations that bind the project to warehouse objects.

    ``model_sources`` and ``model_refs`` record each model's ``source()`` and
    ``ref()`` calls (source entries as ``source_name.table``), which is how a
    warehouse-level finding is traced to the models it lands on without
    re-reading the project at detection time.
    """

    files: dict[str, str] = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)
    sources: list[SourceTable] = Field(default_factory=list)
    model_sources: dict[str, list[str]] = Field(default_factory=dict)
    model_refs: dict[str, list[str]] = Field(default_factory=dict)


class SemanticModelDef(BaseModel):
    """One semantic model, reduced to what drift detection needs: a content hash
    for definition changes, the dbt model it sits on, and each entity,
    dimension, and measure mapped to the physical column it references.

    A column is recorded only when the reference resolves to a single physical
    column (a bare-identifier ``expr``, or a name with no ``expr``, which dbt
    treats as the column itself); computed expressions map to ``None``.
    Guessing columns out of expressions would turn every refactor into a false
    dangling-ref finding.
    """

    name: str
    path: str
    content_sha256: str
    model_ref: str | None = None
    entities: dict[str, str | None] = Field(default_factory=dict)
    dimensions: dict[str, str | None] = Field(default_factory=dict)
    categorical_dimensions: dict[str, str] = Field(default_factory=dict)
    measures: dict[str, str | None] = Field(default_factory=dict)

    def referenced_columns(self) -> set[str]:
        return {
            column
            for mapping in (self.entities, self.dimensions, self.measures)
            for column in mapping.values()
            if column is not None
        }

    def structural_columns(self) -> set[str]:
        """Columns whose loss breaks the model as a whole (entities and
        dimensions), as opposed to a measure column that breaks only the
        measures on it."""

        return {
            column
            for mapping in (self.entities, self.dimensions)
            for column in mapping.values()
            if column is not None
        }


class MetricDef(BaseModel):
    """One metric: a content hash plus the measures and metrics it draws from,
    so warehouse drift can be traced through measures up to the metrics it
    ultimately biases."""

    name: str
    path: str
    content_sha256: str
    input_measures: list[str] = Field(default_factory=list)
    input_metrics: list[str] = Field(default_factory=list)


class SemanticLayer(BaseModel):
    semantic_models: list[SemanticModelDef] = Field(default_factory=list)
    metrics: list[MetricDef] = Field(default_factory=list)


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
    semantic_layer: SemanticLayer | None = None


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
    """Fingerprint the transformation layer from the project view."""

    models: list[str] = []
    model_sources: dict[str, list[str]] = {}
    model_refs: dict[str, list[str]] = {}
    for path, source in view.files.items():
        if not path.endswith(".sql"):
            continue
        model = path.rsplit("/", 1)[-1][: -len(".sql")]
        models.append(model)
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
        sources=sources,
        model_sources=model_sources,
        model_refs=model_refs,
    )


def semantic_layer(view: DbtProjectView) -> SemanticLayer:
    """Fingerprint the semantic layer from the project's YAML files."""

    semantic_models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    for kind, entry, path in semantic_yaml_entries(view):
        if kind == "semantic_model":
            semantic_models.append(_semantic_model_def(entry, path))
        else:
            metrics.append(_metric_def(entry, path))
    return SemanticLayer(semantic_models=semantic_models, metrics=metrics)


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
