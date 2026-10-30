"""The local SLayer layer: the SLayer model files in this repository, queried.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the governed metric query.

**SLayer renders, dex executes.** ``query`` follows the local MetricFlow
backend's order, because the safety contract is the order:

1. the PII request-gate on the grouped dimensions, on the time dimension a
   ``--grain`` buckets, and on every column a ``--where`` clause names, read
   with SLayer's own filter grammar (:mod:`...slayer_dialect`) and looked up in
   the ``.dex/`` cache through the catalog's ``physical_columns``, with the name
   heuristic as the floor and a note on any token that had only the floor;
2. the grain check;
3. translation into a ``SlayerQuery`` (:mod:`...slayer.translate`), which
   refuses whatever v1 does not map;
4. a dry-run render (:mod:`...slayer.render`), over the models dex already
   validated, in the dialect of dex's own connector;
5. the governed spine (:mod:`.governed`): SELECT-only, the relation pre-check,
   the cost handshake, dex's connector, the columnar cap.

SLayer never opens a datasource, never reads a credential, and never executes
under dex: it is handed a query and returns the SQL it would run, and that SQL
reaches the warehouse only through the spine. So ``execution`` is ``dex`` and
no cost-guard-unavailable warning applies. The result's columns carry the names
SLayer itself would report (``orders.status``) on every connector, including
BigQuery, where the statement has to emit ``orders___status``.

``values`` runs the same spine behind a different front: the dimension token is
resolved against the catalog (an unknown one is refused by name), the values gate
(:func:`.policy.screen_values_request`) refuses a PII dimension outright, and the
render is a dimension-only query with no measure, rooted at the dimension's own
model or, with ``--metric``, at that metric's model, which ``scoped_to`` reports.
The values come back capped like any query result. SLayer's own value profiling
(``update_column_sampled``, the samples it caches in a model file) is never
reached: the storage SLayer renders against refuses every write.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from ... import envelope as env
from ...config import QueryLimits
from ...errors import ProjectError
from ..results import SemanticQueryResult, SemanticValuesResult
from .backend import (
    EXECUTION_DEX,
    BackendDescriptor,
    SemanticBackendError,
    SemanticQueryRefusedError,
)
from .catalog import SemanticCatalog
from .governed import GovernedSql
from .model import SemanticQuery
from .policy import (
    name_only_note,
    requested_dimension_refs,
    resolve_values_request,
    screen_dimension_refs,
    screen_values_request,
    sub_threshold_note,
    unadjudicated_refs,
    validate_grain,
)

# Fields of the neutral catalog SLayer has nowhere to put, per element kind, so
# a caller can tell a structural absence from a field the author left blank.
#
# Two whole kinds are absent. SLayer declares no entities: its joins and keys
# reach dex through the declaration channel instead. And it declares no
# measures in the catalog's sense: a saved `ModelMeasure` is a formula with no
# aggregation of its own, so it is listed as a metric, and a column becomes an
# aggregation input only inside a query.
SLAYER_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "primary_entity"],
    "metrics": ["input_measures", "queryable_granularities"],
    "dimensions": ["queryable_granularities"],
    "entities": ["name", "type", "label", "description", "roles"],
    "measures": [
        "name",
        "agg",
        "expr",
        "agg_time_dimension",
        "label",
        "description",
        "semantic_model",
        "column",
    ],
}

# A relation that resolves to the wrong namespace is a model whose `sql_table`
# names another target, since SLayer bakes it into the SQL verbatim.
_RECOMPILE_HINT = "correct the SLayer model's `sql_table`"


class LocalSlayerLayer:
    """The SLayer model files committed to this repository."""

    name = "local"
    vendor = "slayer"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = SLAYER_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=SLAYER_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        self._source = source
        # The dex engine, which the governed spine opens the connection through.
        # Optional, because reading the catalog needs no connection and a caller
        # that only lists (a contract test, a host) should not have to build one.
        self._dex = engine
        self._view: Any = None
        self._governed: GovernedSql | None = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalSlayerLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading SLayer models needs a repository to read them from: "
                "they are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """SLayer's joins, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """SLayer's declared keys. SLayer is never the transformation project,
        so this is the only route its keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def _catalog_view(self):
        """The layer's catalog, read once per command: the listing, the PII
        gate's column resolution and the translation all want the same view."""

        if self._view is None:
            try:
                self._view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._view

    # ---- query -------------------------------------------------------------

    def filter_refs(
        self, clauses: list[str], *, source_model: str | None = None
    ) -> list[str] | None:
        """The ``model.column`` tokens these SLayer filter clauses name, or None.

        A SLayer filter is read relative to the model its query is rooted at: a
        bare column belongs to that model, and a dotted one is walked through its
        declared joins. So without ``source_model`` there is nothing to resolve a
        reference against, and the answer is None, which the gate reads as a
        filter it cannot screen. Never raises, for the same reason.
        """

        if source_model is None:
            return None
        try:
            from ...slayer_dialect import filter_refs

            models = {
                loaded.model.name: loaded.model
                for loaded in self._source.validated_models()
            }
        except Exception:
            return None
        return filter_refs(clauses, source_model, models=models)

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        from ...slayer.render import slayer_type
        from ...slayer.translate import source_model, to_slayer_query

        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")
        # Before anything is read: a connector SLayer would render in its
        # Postgres fallback is a configuration problem no query can fix.
        slayer_type(self._connector())
        governed = self._governed_sql()
        view = self._catalog_view()
        cache = governed.load_cache()

        # A filter is read against the model the query is rooted at, and
        # `--grain` buckets that model's time dimension, so either needs the
        # root first. A set of metrics that names no single root is refused by
        # name here, rather than as a filter the gate could not read, which would
        # send the caller after the wrong problem. Nothing is rendered either way.
        root = source_model(q.metrics, view) if q.where or q.grain else None

        # PII request-gate, before rendering. `--grain` groups by the root's time
        # dimension as surely as `--group-by` would, so that column is screened
        # too. A root with no time dimension adds nothing: translation refuses
        # the grain, before anything renders.
        refs = requested_dimension_refs(
            q,
            filter_refs=partial(
                self.filter_refs, source_model=root.name if root else None
            ),
        )
        if q.grain and root is not None:
            refs = list(dict.fromkeys([*refs, *(self.time_axis_refs(q.metrics) or [])]))
        lookup = governed.pii_lookup(cache)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )

        # The grain's shape, after the gate. SLayer declares no grains per metric
        # (`queryable_granularities` is a declared gap), so which grains a model
        # can be bucketed at is the translation's check: its default time
        # dimension, at one of SLayer's built-in granularities.
        q = replace(q, grain=validate_grain(q.grain, available=None))
        rendered = self._render(to_slayer_query(q, view), "the metric query")

        return governed.run(
            rendered.sql,
            cache,
            backend=self,
            command="explore semantic query",
            result_type=SemanticQueryResult,
            extra_notes=[
                *name_only_note(unadjudicated_refs(refs, meta_lookup=lookup)),
                *sub_threshold_note(refs, meta_lookup=lookup),
            ],
            column_names=rendered.result_keys,
        )

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
        """The ``model.column`` a ``--grain`` on these metrics buckets.

        The root model's ``default_time_dimension``, which is what SLayer groups
        by when a query names a grain, so the gate screens it as a grouped
        column. None where the metrics name no model with one, which is also a
        query translation refuses before anything renders.
        """

        from ...semantic_catalog import time_axis_tokens

        try:
            return time_axis_tokens(self._catalog_view(), metrics)
        except SemanticBackendError:
            return None

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One dimension's value domain, rendered by SLayer and executed here.

        The token is resolved against the catalog first, so a misspelling is
        refused by name, and the values gate runs before anything renders: this
        command's whole output is values, so a PII dimension has no reduced
        answer to fall back to. Then a dimension-only render and the spine.
        """

        from ...slayer.render import slayer_type
        from ...slayer.translate import GRAINS, to_slayer_values_query

        slayer_type(self._connector())
        governed = self._governed_sql()
        view = self._catalog_view()
        request = resolve_values_request(
            view,
            dimension,
            metrics,
            grains=GRAINS,
            spelling="named model.column (orders.status)",
        )
        cache = governed.load_cache()
        notes = screen_values_request(
            request.name, meta_lookup=governed.pii_lookup(cache)
        )

        slayer_query = to_slayer_values_query(request, view)
        rendered = self._render(slayer_query, f"the values of {request.token}")

        return governed.run(
            rendered.sql,
            cache,
            backend=self,
            command="explore semantic values",
            result_type=SemanticValuesResult,
            extra_notes=notes,
            column_names=rendered.result_keys,
            dimension=request.token,
            scoped_to=request.metrics,
        )

    def _render(self, slayer_query: Any, what: str) -> Any:
        """The ``RenderedQuery`` SLayer makes of ``slayer_query``, as a dry run."""

        from ...slayer.render import SlayerRenderer

        try:
            return SlayerRenderer(
                self._source.validated_models(), connector=self._connector()
            ).render(slayer_query)
        except SemanticBackendError:
            raise
        except Exception as exc:  # a SLayer resolution error is the query's
            # fault, not a crash: surface it, redacted, as MetricFlow's is.
            raise SemanticBackendError(
                f"could not resolve {what}: {env.redact(str(exc))}"
            ) from exc

    def _connector(self) -> str | None:
        if self._dex is not None:
            return self._dex.connector or getattr(self._dex.config, "connector", None)
        return getattr(self._source, "connector", None)

    def _governed_sql(self) -> GovernedSql:
        if self._governed is None:
            if self._dex is None:
                raise SemanticBackendError(
                    "a SLayer metric query runs through dex's own connection, so "
                    "build the layer from an engine (LocalSlayerLayer.from_engine)"
                )
            limits = getattr(self._dex.config, "query", None) or QueryLimits()
            self._governed = GovernedSql(
                self._dex,
                self._connector() or "",
                limits,
                self._physical_columns,
                recompile_hint=_RECOMPILE_HINT,
                computed_columns=self._computed_columns,
            )
        return self._governed

    def _physical_columns(self) -> dict[str, tuple[str, str]]:
        """``model.column -> (relation, physical column)``, from the catalog.

        A layer that cannot answer leaves the gate on the name heuristic, the
        fail-closed floor an unprofiled relation already gets.
        """

        try:
            return dict(self._catalog_view().physical_columns)
        except SemanticBackendError:
            return {}

    def _computed_columns(self) -> dict[str, tuple[str, str]]:
        """``model.column -> (relation, SQL)`` for a column defined by ``sql``.

        Read by the gate for the columns the expression reads, rather than
        screened on the column's name alone.
        """

        try:
            return dict(self._catalog_view().computed_columns)
        except SemanticBackendError:
            return {}
