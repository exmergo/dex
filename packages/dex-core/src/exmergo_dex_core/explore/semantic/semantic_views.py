"""The local Snowflake Semantic View layer: the Semantic View files in this
repository, read.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the two governed entry points a later release fills in.

**The catalog needs no Snowflake connection.** ``explore semantic list`` reads
the repository's Semantic View YAML statically, and the declaration channel
carries each primary and unique key and each relationship to ``explore
relationships|map|profile --use-project``.

**Querying is not available yet.** A governed Semantic View query is rendered
as a ``SEMANTIC_VIEW(...)`` clause and executed by dex through the Snowflake
connector, and it is a later release's (#1290). Until then ``query`` refuses by
name, and ``values`` runs the values PII gate first, so a PII dimension is
refused as PII before it is refused for want of a runtime: the order the
governed path will keep.
"""

from __future__ import annotations

from typing import Any

from ...config import QueryLimits
from ...errors import ProjectError
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog
from .policy import resolve_values_request, screen_values_request

__all__ = ["SEMANTIC_VIEW_CATALOG_GAPS", "LocalSemanticViewLayer"]

# Fields of the neutral catalog a Semantic View has nowhere to put, per element
# kind.
#
# A Semantic View element carries a description and synonyms, and no display
# label. A logical table sits on a relation rather than a dbt model, and names no
# default time dimension. Semantic Views have no time grains: a time bucket is a
# dimension's own SQL, so no dimension or metric names a queryable grain or a
# time axis, and no measure names the time dimension it aggregates over. An
# entity is a table's primary key, which carries no label or description of its
# own. A metric has no filter apart from its own expression (a table's `filters`
# are Cortex Analyst's, not a metric's).
SEMANTIC_VIEW_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "agg_time_dimension"],
    "metrics": ["label", "filter", "time_axis", "queryable_granularities"],
    "dimensions": ["label", "queryable_granularities"],
    "measures": ["label", "agg_time_dimension"],
    "entities": ["label", "description"],
}

_NO_RUNTIME = (
    "governed Semantic View queries (`SELECT * FROM SEMANTIC_VIEW(<view> "
    "DIMENSIONS ... METRICS ...)`, executed by dex through the Snowflake connector "
    "under the cost guard) are not available in this release (#1290)"
)


class LocalSemanticViewLayer:
    """The Snowflake Semantic View files committed to this repository."""

    name = "local"
    vendor = "snowflake"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = SEMANTIC_VIEW_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=SEMANTIC_VIEW_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        """Wrap a Semantic View source; an engine, where given, backs the PII gate."""
        self._source = source
        # Optional: reading the catalog needs no engine. With one, the values
        # gate reads the profiled cache rather than the name alone.
        self._dex = engine
        self._view: Any = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalSemanticViewLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Semantic View files needs a repository to read them from: "
                "they are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        """The catalog `explore semantic list` renders."""
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """Each key-equality relationship, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Each primary and unique key, single-column and composite. A Semantic
        View is never the transformation project, so this is the only route its
        keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def declared_models(self) -> tuple[dict[str, Any], list[str]]:
        """Each definition's declaration, with what could not be read. A
        Semantic View has no metadata block a dex declaration could live in, so
        this is empty by construction."""

        defs = self._source.declared_definitions()
        return defs.model_declarations, defs.declaration_notes

    def _catalog_view(self):
        """The source's catalog, read once; an unreadable layer is a backend error."""
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
        """None: a Semantic View names no time axis for a metric to bucket."""

        return None

    def query(self, _q: Any) -> Any:
        """Refused by name until #1290."""
        raise SemanticBackendError(
            f"{_NO_RUNTIME}. Read the layer with `explore semantic list`, then "
            "query the relations it names with `explore query`, which runs under "
            "the firewall and the cost guard"
        )

    def values(self, dimension: str, metrics: list[str]) -> Any:
        """Refused, after the values gate: a PII dimension is refused as PII."""

        view = self._catalog_view()
        request = resolve_values_request(
            view,
            dimension,
            metrics,
            grains=(),
            spelling="named view.table.dimension (star.orders.order_status)",
        )
        screen_values_request(request.name, meta_lookup=self._pii_lookup())
        raise SemanticBackendError(
            f"{_NO_RUNTIME}, and reading a dimension's values is one of them. "
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
            recompile_hint="correct the logical table's `base_table`",
            computed_columns=lambda: dict(self._catalog_view().computed_columns),
        )
        return governed.pii_lookup(governed.load_cache())
