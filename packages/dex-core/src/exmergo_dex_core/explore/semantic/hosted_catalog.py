"""Decode dbt Cloud GraphQL catalog payloads into dex's neutral domain model.

The hosted backend owns request orchestration. This module owns the deliberately
lossy boundary between the vendor response and :mod:`semantic_catalog`, including
the fields that dbt Cloud cannot supply. Keeping that boundary pure makes catalog
shape changes testable without credentials, polling, or a backend instance.
"""

from __future__ import annotations

from typing import Any

from ... import metricflow_dialect
from ...semantic_catalog import (
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticModelInfo,
    column_reference,
    derive_entity_type,
    merge_element_fields,
)
from .catalog import SemanticCatalog, _metric_time_axis_notes

HOSTED_COST_WARNING = (
    "cost guard unavailable on the hosted semantic layer: dbt Cloud owns the "
    "warehouse connection and executes this query server-side, so dex applies no "
    "cost estimate or ceiling here. Spend is governed by the dbt Cloud "
    "environment's own limits, not by dex."
)

HOSTED_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": [
        "label",
        "description",
        "model_ref",
        "agg_time_dimension",
        "primary_entity",
        "relation",
    ],
    "entities": ["label"],
    "measures": ["label", "description"],
}

# One document and one round trip. Every field here has been verified against
# dbt Cloud's schema; one speculative field makes GraphQL reject the whole list.
HOSTED_CATALOG_FIELDS = (
    "name type label description "
    "queryableGranularities queryableTimeGranularities requiresMetricTime "
    "filter { whereSqlTemplate } "
    "typeParams { measure { name } inputMeasures { name } "
    "numerator { name } denominator { name } expr "
    "window { count granularity } grainToDate "
    "metrics { name offsetWindow { count granularity } } } "
    "measures { name agg expr aggTimeDimension } "
    "semanticModels { name } "
    "dimensions { name type label description expr semanticModel { name } "
    "queryableGranularities queryableTimeGranularities } "
    "entities { name type description expr role semanticModel { name } }"
)

_HOSTED_RELATION_NOTE = (
    "hosted list: the dbt Cloud Semantic Layer API exposes no physical relation "
    "on a semantic model (its SemanticModel type carries only a name), so every "
    "element here names the column behind it and none of them can say which table "
    "that column is in. List with --local for the relations, which is also what "
    "connects a metric to the objects `explore map` and `explore profile` describe"
)
_HOSTED_ENTITY_LABEL_NOTE = (
    "hosted list: the dbt Cloud Semantic Layer API exposes no label on entities "
    "(its Entity type has no such field), so an entity here carries a "
    "description at most; list with --local to read the labels the dbt project "
    "declares"
)
_HOSTED_REACH_NOTE = (
    "hosted list: every element is reached through a metric, so a measure, "
    "entity declaration or semantic model that no metric draws on is absent; "
    "list with --local for the layer as the project declares it"
)

_GRAIN_FIELDS = ("queryableTimeGranularities", "queryableGranularities")


def hosted_grains(payload: dict[str, Any]) -> list[str] | None:
    """Return the grains the API reported, preserving an answered empty list."""

    if not any(field in payload for field in _GRAIN_FIELDS):
        return None
    return metricflow_dialect.order_grains(
        [value for field in _GRAIN_FIELDS for value in payload.get(field) or []]
    )


def decode_hosted_catalog(data: dict[str, Any], backend: Any) -> SemanticCatalog:
    """Turn one hosted metrics response into the backend-neutral catalog."""

    metrics: list[MetricInfo] = []
    dimensions: dict[str, dict[str, Any]] = {}
    roles: dict[str, dict[str, EntityRole]] = {}
    entity_words: dict[str, str | None] = {}
    measures: dict[str, MeasureInfo] = {}
    models: set[str] = set()

    for metric in data.get("metrics") or []:
        owners = [
            model["name"]
            for model in metric.get("semanticModels") or []
            if isinstance(model, dict) and model.get("name")
        ]
        models.update(owners)

        for dimension in metric.get("dimensions") or []:
            owner = _model_name(dimension.get("semanticModel"))
            if owner:
                models.add(owner)
            bare = _bare_dimension(dimension.get("name"), owner)
            merge_element_fields(
                dimensions,
                dimension.get("name"),
                {
                    "type": dimension.get("type"),
                    "label": dimension.get("label"),
                    "description": dimension.get("description"),
                    "definition": bare,
                    "semantic_model": owner,
                    "queryable_granularities": hosted_grains(dimension),
                    "column": column_reference(dimension.get("expr"), bare),
                },
            )

        for entity in metric.get("entities") or []:
            name = entity.get("name")
            owner = _model_name(entity.get("semanticModel"))
            if not name:
                continue
            if owner:
                models.add(owner)
            roles.setdefault(name, {}).setdefault(
                owner or "",
                EntityRole(
                    semantic_model=owner or "",
                    type=str(entity.get("type") or "").lower(),
                    expr=entity.get("expr"),
                    role=entity.get("role"),
                    description=entity.get("description"),
                    column=column_reference(entity.get("expr"), name),
                ),
            )
            if entity_words.get(name) is None:
                entity_words[name] = entity.get("description")

        for measure in metric.get("measures") or []:
            name = measure.get("name")
            if not name or name in measures:
                continue
            measures[name] = MeasureInfo(
                name=name,
                agg=str(measure.get("agg") or "").lower() or None,
                expr=measure.get("expr"),
                agg_time_dimension=measure.get("aggTimeDimension"),
                column=column_reference(measure.get("expr"), name),
                semantic_model=owners[0] if len(owners) == 1 else None,
            )

        metrics.append(_metric_info(metric, owners))

    notes = [
        _HOSTED_REACH_NOTE,
        _HOSTED_RELATION_NOTE,
        *_metric_time_axis_notes(metrics),
    ]
    if roles:
        notes.append(_HOSTED_ENTITY_LABEL_NOTE)
    return SemanticCatalog.from_backend(
        backend,
        semantic_models=[SemanticModelInfo(name=name) for name in sorted(models)],
        metrics=metrics,
        dimensions=[
            DimensionInfo(
                name=name,
                type=fields.get("type") or "",
                label=fields.get("label"),
                description=fields.get("description"),
                definition=fields.get("definition"),
                semantic_model=fields.get("semantic_model"),
                queryable_granularities=fields.get("queryable_granularities"),
                column=fields.get("column"),
            )
            for name, fields in sorted(dimensions.items())
        ],
        entities=[
            EntityInfo(
                name=name,
                type=derive_entity_type(list(declared.values())),
                description=entity_words.get(name),
                roles=[declared[key] for key in sorted(declared)],
            )
            for name, declared in sorted(roles.items())
        ],
        measures=[measures[name] for name in sorted(measures)],
        notes=notes,
    )


def _model_name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def _bare_dimension(token: str | None, semantic_model: str | None) -> str | None:
    if not token or not semantic_model:
        return None
    _, separator, tail = token.rpartition("__")
    return tail if separator else token


def _metric_info(payload: dict[str, Any], owners: list[str]) -> MetricInfo:
    params = payload.get("typeParams")
    params = params if isinstance(params, dict) else {}

    def named(value: Any) -> str | None:
        return value.get("name") if isinstance(value, dict) else None

    def window(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        count, granularity = value.get("count"), value.get("granularity")
        if count is None and granularity is None:
            return None
        return {"count": count, "granularity": granularity}

    input_measures = [
        name
        for name in (named(value) for value in params.get("inputMeasures") or [])
        if name is not None
    ]
    input_metrics = [
        name
        for name in (named(value) for value in params.get("metrics") or [])
        if name is not None
    ]

    vendor: dict[str, Any] = {}
    if (cumulative := window(params.get("window"))) is not None:
        vendor["window"] = cumulative
    if params.get("grainToDate"):
        vendor["grain_to_date"] = params["grainToDate"]
    offsets = {
        value["name"]: offset
        for value in params.get("metrics") or []
        if isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and (offset := window(value.get("offsetWindow"))) is not None
    }
    if offsets:
        vendor["offset_windows"] = offsets
    if payload.get("requiresMetricTime"):
        vendor["requires_metric_time"] = True

    time_axis = sorted(
        {
            measure["aggTimeDimension"]
            for measure in payload.get("measures") or []
            if isinstance(measure, dict) and measure.get("aggTimeDimension")
        }
    )
    metric_filter = payload.get("filter")
    return MetricInfo(
        name=payload.get("name"),
        type=(payload.get("type") or "").lower(),
        label=payload.get("label"),
        description=payload.get("description"),
        dimensions=[
            dimension.get("name") for dimension in payload.get("dimensions") or []
        ],
        semantic_models=owners or None,
        input_measures=input_measures or None,
        time_axis=time_axis or None,
        queryable_granularities=hosted_grains(payload),
        composition=MetricComposition(
            measure=named(params.get("measure")),
            numerator=named(params.get("numerator")),
            denominator=named(params.get("denominator")),
            expr=params.get("expr"),
            input_metrics=input_metrics or None,
        ),
        filter=(
            metric_filter.get("whereSqlTemplate")
            if isinstance(metric_filter, dict)
            else None
        ),
        vendor_params=vendor or None,
    )
