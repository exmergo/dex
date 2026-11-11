"""The local Databricks Metric View layer: the Metric View files in this
repository, read.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the two governed entry points a later release fills in.

**The catalog needs no workspace.** ``explore semantic list`` reads the
repository's Metric View YAML statically, and the declaration channel carries
each join and each ``rely.at_most_one_match`` key to ``explore
relationships|map|profile --use-project``.

**Querying is not available yet.** A governed Metric View query is SQL over the
view with ``MEASURE()``, executed by dex through the Databricks connector, and it
is the next release's (#964). Until then ``query`` refuses by name, and
``values`` runs the values PII gate first, so a PII field is refused as PII
before it is refused for want of a runtime: the order the governed path will
keep.
"""

from __future__ import annotations

from typing import Any

from ...config import QueryLimits
from ...errors import ProjectError
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog
from .policy import resolve_values_request, screen_values_request

__all__ = ["METRIC_VIEW_CATALOG_GAPS", "LocalMetricViewLayer"]

# Fields of the neutral catalog a Metric View has nowhere to put, per element
# kind.
#
# A view carries no display name of its own and sits on a relation rather than
# a dbt model; it declares no entities, its joins and keys reaching dex through
# the declaration channel instead. Metric Views have no time grains: a time
# bucket is a field's own SQL (`DATE_TRUNC('MONTH', o_orderdate)`), so no field
# or metric names a queryable grain or a time axis, and no measure names the
# time dimension it aggregates over.
METRIC_VIEW_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "agg_time_dimension", "primary_entity"],
    "metrics": ["time_axis", "queryable_granularities"],
    "dimensions": ["queryable_granularities"],
    "measures": ["agg_time_dimension"],
    "entities": ["name", "type", "label", "description", "roles"],
}

_NO_RUNTIME = (
    "governed Metric View queries (`SELECT ... MEASURE(m) ... FROM <view>`, "
    "executed by dex through the Databricks connector under the cost guard) are "
    "not available in this release (#964)"
)


class LocalMetricViewLayer:
    """The Databricks Metric View files committed to this repository."""

    name = "local"
    vendor = "databricks"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = METRIC_VIEW_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=METRIC_VIEW_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        self._source = source
        # Optional: reading the catalog needs no engine. With one, the values
        # gate reads the profiled cache rather than the name alone.
        self._dex = engine
        self._view: Any = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalMetricViewLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Metric View files needs a repository to read them from: "
                "they are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """Each join with key pairs, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Each ``rely.at_most_one_match`` key, on the joined table. A Metric
        View is never the transformation project, so this is the only route its
        keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def declared_models(self) -> tuple[dict[str, Any], list[str]]:
        """Each definition's declaration, with what could not be read. A Metric
        View has no metadata block a declaration could live in, so this is empty
        by construction."""

        defs = self._source.declared_definitions()
        return defs.model_declarations, defs.declaration_notes

    def _catalog_view(self):
        if self._view is None:
            try:
                self._view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._view

    # ---- query -------------------------------------------------------------

    def filter_refs(self, _clauses: list[str]) -> list[str] | None:
        """None: no filter dialect is read yet, so a filtered query is refused
        rather than screened by half."""

        return None

    def time_axis_refs(self, _metrics: list[str]) -> list[str] | None:
        """None: a Metric View names no time axis for a metric to bucket."""

        return None

    def query(self, _q: Any) -> Any:
        raise SemanticBackendError(
            f"{_NO_RUNTIME}. Read the layer with `explore semantic list`, then "
            "query the relations it names with `explore query`, which runs under "
            "the firewall and the cost guard"
        )

    def values(self, dimension: str, metrics: list[str]) -> Any:
        """Refused, after the values gate: a PII field is refused as PII."""

        view = self._catalog_view()
        request = resolve_values_request(
            view,
            dimension,
            metrics,
            grains=(),
            spelling="named view.field (orders.order_status)",
        )
        screen_values_request(request.name, meta_lookup=self._pii_lookup())
        raise SemanticBackendError(
            f"{_NO_RUNTIME}, and reading a field's values is one of them. "
            f"{request.token} sits on a column the catalog names: profile that "
            "relation with `explore profile` instead"
        )

    def _pii_lookup(self):
        """The cache-backed PII lookup when an engine is present, else None (the
        name heuristic alone, which still refuses an obviously personal name)."""

        if self._dex is None:
            return None
        from .governed import GovernedSql

        governed = GovernedSql(
            self._dex,
            self._dex.connector or getattr(self._dex.config, "connector", "") or "",
            getattr(self._dex.config, "query", None) or QueryLimits(),
            lambda: dict(self._catalog_view().physical_columns),
            recompile_hint="correct the Metric View's `source`",
            computed_columns=lambda: dict(self._catalog_view().computed_columns),
        )
        return governed.pii_lookup(governed.load_cache())
