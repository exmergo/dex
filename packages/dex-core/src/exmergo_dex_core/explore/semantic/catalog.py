"""Catalog delivery: backend identity, scoping, budgeting, and serialization.

The neutral catalog model lives in :mod:`exmergo_dex_core.semantic_catalog`.
This module composes that model with facts about the backend and the command that
shaped it. Keeping the layers separate prevents CLI scope and payload-budget
state from leaking into the neutral domain model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ...semantic_catalog import SemanticCatalogView
from .backend import BackendDescriptor, descriptor_from_backend

MAX_SEMANTIC_MODELS = 50
MAX_METRICS = 60
MAX_DIMENSIONS = 150
MAX_ENTITIES = 50
MAX_MEASURES = 60
MAX_DIMENSIONS_PER_METRIC = 40

_ELIDED_KINDS = (
    "semantic_models",
    "metrics",
    "dimensions",
    "entities",
    "measures",
    "dimensions_per_metric",
)


def _element_data(element: Any) -> dict[str, Any]:
    """Serialize one sparse catalog element, omitting unset optional fields."""

    def prune(value: Any) -> Any:
        if isinstance(value, dict):
            kept = {key: prune(item) for key, item in value.items() if item is not None}
            return {key: item for key, item in kept.items() if item != {}}
        if isinstance(value, list):
            return [prune(item) for item in value]
        return value

    return prune(asdict(element))


def _elision_notes(
    elided: dict[str, int], limits: dict[str, int], per_metric: int
) -> list[str]:
    ways_out = (
        "narrow the question with --metric, --for-dimension or --search, or pass "
        "--full for the whole layer"
    )
    notes: list[str] = []
    if elided["semantic_models"]:
        notes.append(
            f"{elided['semantic_models']} semantic model(s) are not listed: the "
            f"catalog is capped at {limits['semantic_models']}. Every element still "
            "names its own semantic_model, so a metric can point at a model this "
            f"payload does not describe; {ways_out}"
        )
    if elided["metrics"]:
        notes.append(
            f"{elided['metrics']} metric(s) are not listed: the catalog is capped "
            f"at {limits['metrics']} metrics. This is not the layer's whole metric "
            f"set; {ways_out}"
        )
    if elided["dimensions"]:
        notes.append(
            f"{elided['dimensions']} dimension row(s) are not listed: the catalog "
            f"is capped at {limits['dimensions']}. A token named in a metric's "
            f"dimensions may therefore have no row of its own here; {ways_out}"
        )
    if elided["entities"]:
        notes.append(
            f"{elided['entities']} entity(ies) are not listed: the catalog is "
            f"capped at {limits['entities']}. The declared join graph is incomplete "
            f"in this payload; {ways_out}"
        )
    if elided["measures"]:
        notes.append(
            f"{elided['measures']} measure(s) are not listed: the catalog is "
            f"capped at {limits['measures']}. A metric's input_measures may name a "
            f"measure this payload does not describe; {ways_out}"
        )
    if elided["dimensions_per_metric"]:
        notes.append(
            f"{elided['dimensions_per_metric']} groupable token(s) are not listed "
            "across the metrics here: each metric's dimension list is capped at "
            f"{per_metric}, and elided_dimension_count on a metric says how many "
            "of its own are missing. A token absent from a capped list is not a "
            f"token the metric cannot be grouped by; {ways_out}"
        )
    return notes


def _metric_time_axis_notes(metrics: list[Any]) -> list[str]:
    """Describe multi-column metric time only for metrics in this response."""

    disagreeing = sorted(
        metric.name for metric in metrics if len(metric.time_axis or ()) > 1
    )
    if not disagreeing:
        return []
    return [
        f"{', '.join(disagreeing)} aggregate over more than one time column "
        "(see time_axis): grouping by metric_time uses each measure's own, so "
        "the parts of one number can be bucketed by different timestamps"
    ]


def _scoped_metric_notes(
    notes: list[str], before: list[Any], after: list[Any]
) -> list[str]:
    """Replace generated metric caveats after a catalog scope changes."""

    generated = set(_metric_time_axis_notes(before))
    return [
        *(note for note in notes if note not in generated),
        *_metric_time_axis_notes(after),
    ]


@dataclass
class SemanticCatalog:
    """A neutral catalog composed with backend and response-shaping metadata."""

    view: SemanticCatalogView
    descriptor: BackendDescriptor
    scoped_to: list[str] = field(default_factory=list)
    for_dimensions: list[str] = field(default_factory=list)
    searched_for: list[str] = field(default_factory=list)
    elided: dict[str, int] = field(default_factory=dict)

    @property
    def backend(self) -> str:
        return self.descriptor.name

    @property
    def vendor(self) -> str:
        return self.descriptor.vendor

    @property
    def deployment(self) -> str:
        return self.descriptor.deployment

    @property
    def execution(self) -> str:
        return self.descriptor.execution

    @property
    def unavailable(self) -> dict[str, list[str]]:
        return self.descriptor.catalog_gaps

    @classmethod
    def from_backend(cls, backend: Any, **fields: Any) -> SemanticCatalog:
        descriptor = descriptor_from_backend(backend)
        dimension_scope = fields.pop("dimension_scope", descriptor.dimension_scope)
        unavailable = fields.pop("unavailable", None)
        if unavailable is not None:
            descriptor = replace(descriptor, catalog_gaps=dict(unavailable))
        scope = {
            name: fields.pop(name, [])
            for name in ("scoped_to", "for_dimensions", "searched_for")
        }
        elided = fields.pop("elided", {})
        return cls(
            view=SemanticCatalogView(dimension_scope=dimension_scope, **fields),
            descriptor=descriptor,
            elided=elided,
            **scope,
        )

    @classmethod
    def from_view(
        cls, view: SemanticCatalogView, backend: Any, **fields: Any
    ) -> SemanticCatalog:
        notes = [*view.notes, *fields.pop("notes", [])]
        return cls(
            view=replace(view, notes=notes),
            descriptor=descriptor_from_backend(backend),
            **fields,
        )

    @property
    def semantic_models(self):
        return self.view.semantic_models

    @property
    def metrics(self):
        return self.view.metrics

    @property
    def dimensions(self):
        return self.view.dimensions

    @property
    def entities(self):
        return self.view.entities

    @property
    def measures(self):
        return self.view.measures

    @property
    def dimension_scope(self) -> str:
        return self.view.dimension_scope

    @property
    def notes(self) -> list[str]:
        return self.view.notes

    @notes.setter
    def notes(self, notes: list[str]) -> None:
        self.view = replace(self.view, notes=list(notes))

    def narrowed_to(self, metrics: list[str]) -> tuple[SemanticCatalog, list[str]]:
        view, unknown = self.view.narrowed_to(metrics)
        view = replace(
            view,
            notes=_scoped_metric_notes(self.notes, self.metrics, view.metrics),
        )
        return replace(self, view=view), unknown

    def matching(self, terms: list[str]) -> tuple[SemanticCatalog, list[str]]:
        view, unmatched = self.view.matching(terms)
        view = replace(
            view,
            notes=_scoped_metric_notes(self.notes, self.metrics, view.metrics),
        )
        return replace(self, view=view), unmatched

    def metrics_for_dimensions(
        self, dimensions: list[str]
    ) -> tuple[list[str], list[str]]:
        return self.view.metrics_for_dimensions(dimensions)

    def to_data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "backend": self.descriptor.name,
            "vendor": self.descriptor.vendor,
            "deployment": self.descriptor.deployment,
            "execution": self.descriptor.execution,
            "dimension_scope": self.dimension_scope,
        }
        if self.for_dimensions:
            payload["for_dimensions"] = self.for_dimensions
        if self.searched_for:
            payload["searched_for"] = self.searched_for
        if self.scoped_to:
            payload["scoped_to"] = self.scoped_to
        if self.descriptor.catalog_gaps:
            payload["unavailable"] = dict(self.descriptor.catalog_gaps)
        payload["elided"] = self.elided or dict.fromkeys(_ELIDED_KINDS, 0)
        payload.update(
            {
                "semantic_models": [
                    _element_data(item) for item in self.semantic_models
                ],
                "metrics": [_element_data(item) for item in self.metrics],
                "dimensions": [_element_data(item) for item in self.dimensions],
                "entities": [_element_data(item) for item in self.entities],
                "measures": [_element_data(item) for item in self.measures],
                "notes": self.notes,
            }
        )
        return payload

    def capped(
        self,
        *,
        full: bool = False,
        max_semantic_models: int = MAX_SEMANTIC_MODELS,
        max_metrics: int = MAX_METRICS,
        max_dimensions: int = MAX_DIMENSIONS,
        max_entities: int = MAX_ENTITIES,
        max_measures: int = MAX_MEASURES,
        max_dimensions_per_metric: int = MAX_DIMENSIONS_PER_METRIC,
    ) -> SemanticCatalog:
        limits = {
            "semantic_models": max_semantic_models,
            "metrics": max_metrics,
            "dimensions": max_dimensions,
            "entities": max_entities,
            "measures": max_measures,
        }
        elided = dict.fromkeys(_ELIDED_KINDS, 0)
        kept: dict[str, list[Any]] = {}
        for kind, limit in limits.items():
            elements = list(getattr(self.view, kind))
            kept[kind] = elements if full else elements[:limit]
            elided[kind] = len(elements) - len(kept[kind])

        metrics = []
        for metric in kept["metrics"]:
            dropped = (
                0
                if full
                else max(0, len(metric.dimensions) - max_dimensions_per_metric)
            )
            if dropped:
                elided["dimensions_per_metric"] += dropped
                metric = replace(
                    metric,
                    dimensions=metric.dimensions[:max_dimensions_per_metric],
                    elided_dimension_count=dropped,
                )
            metrics.append(metric)
        kept["metrics"] = metrics

        notes = [
            *self.notes,
            *_elision_notes(elided, limits, max_dimensions_per_metric),
        ]
        return replace(
            self,
            view=replace(self.view, **kept, notes=notes),
            elided=elided,
        )
