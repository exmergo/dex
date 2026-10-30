"""The local MetricFlow backend: query the dbt project's own semantic layer.

``list`` is a pure read-view over the compiled ``target/semantic_manifest.json``,
so it needs no extra and no warehouse connection: it is the discovery surface an
agent uses to find what it can query.

``query`` puts a PII request-gate on the grouped and filtered dimensions (resolved
to physical columns and checked against the ``.dex/`` cache, with a name heuristic
as the floor) and checks the grain, then renders the metric SQL with MetricFlow's
``explain()`` through a renderer-only ``SqlClient`` (MetricFlow never opens a
connection or sees a credential). The rendered SQL is handed to dex's governed
spine (:mod:`.governed`), which owns the order everything after rendering runs in.
dex owns execution here, so the full cost guard applies, unlike the hosted backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from ... import envelope as env
from ... import metricflow_dialect
from ...config import QueryLimits
from ...metricflow_dialect import METRIC_TIME
from ...semantic_catalog import time_axis_tokens
from ..results import SemanticQueryResult, SemanticValuesResult
from . import (
    DIMENSIONS_PER_DECLARATION,
    EXECUTION_DEX,
    BackendDescriptor,
    SemanticBackendError,
    SemanticCatalog,
    SemanticQuery,
    SemanticQueryRefusedError,
    ValuesRequest,
    queryable_grains,
    requested_dimension_refs,
    resolve_values_request,
    screen_dimension_refs,
    screen_values_request,
    unadjudicated_refs,
    validate_grain,
    values_reach_note,
)
from .governed import GovernedSql
from .governed import unprofiled_note as _unprofiled_note  # noqa: F401  (re-export)
from .local_runtime import metricflow_engine
from .policy import name_only_note, sub_threshold_note

# Said only when the join graph could not be resolved, which is an install that
# picked no extras. A list that names a metric's single-hop dimensions and reads
# as complete is worse than an acknowledged gap: the dimension it drops is often
# the one the metric's own description tells a caller to group by.
_UNRESOLVED_JOINS_NOTE = (
    "local list: a metric's dimensions are those of its owning semantic model(s), "
    "entity-qualified single-hop, because the join graph was not resolved (the "
    "[semantic] extra is not installed, or this compiled manifest is not one its "
    "resolver reads), so dimensions reachable only through a join are missing from "
    "these lists. Install or upgrade that extra for the resolved lists, or list "
    "with --api"
)

_RECOMPILE_HINT = "re-run `dbt parse`"

_MISSING_EXTRA = (
    "local metric queries need the [semantic] extra: "
    "pip install 'exmergo-dex-core[semantic]'"
)


class LocalMetricFlowBackend:
    name = "local"
    vendor = "dbt"
    deployment = "local"
    # dex renders the metric SQL and runs it through its own connector, so the
    # full cost guard applies here and the cost comes from the adapter, not from
    # `cost_posture`.
    execution = EXECUTION_DEX
    # Nothing structurally missing: a project read carries every field the
    # catalog defines, which is the half of the asymmetry worth stating plainly.
    catalog_gaps: ClassVar[dict[str, list[str]]] = {}
    # The floor, not the claim. What one dimension row actually is depends on
    # whether the project read could resolve the join graph, so the catalog carries
    # what that read reported and this stands in only for a caller that asks the
    # backend without one.
    dimension_scope = DIMENSIONS_PER_DECLARATION
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=catalog_gaps,
        dimension_scope=dimension_scope,
    )

    def __init__(
        self,
        project: Path,
        engine,
        connector: str,
        limits: QueryLimits,
        project_format=None,
    ) -> None:
        # The directory, for MetricFlow, which reads the compiled artifact itself.
        self._project = project
        # The project *format*, for everything dex reads: the catalog and the PII
        # gate's column resolution both come from it, and injecting it is what
        # keeps this backend from knowing that the format on the other side is
        # dbt. Falls back to the engine's, which is the ordinary path.
        self._format = project_format
        # The dex engine, which the governed spine opens the connection through.
        # Named `_dex` because "engine" already means the MetricFlow one
        # throughout this module.
        self._dex = engine
        self._store = engine.store
        self._connector = connector
        self._mf_engine = None
        self._dim_columns: dict[str, tuple[str, str]] | None = None
        self._dim_expressions: dict[str, tuple[str, str]] | None = None
        self._catalog_view = None
        # Everything that happens to the SQL once MetricFlow has rendered it.
        self._governed = GovernedSql(
            engine,
            connector,
            limits,
            self._dimension_columns,
            recompile_hint=_RECOMPILE_HINT,
            computed_columns=self._dimension_expressions,
        )

    @classmethod
    def from_engine(cls, engine) -> LocalMetricFlowBackend:
        from ...errors import ProjectError

        config = engine.config
        connector = engine.connector or getattr(config, "connector", "duckdb")
        limits = getattr(config, "query", None) or QueryLimits()
        try:
            project = engine.project_dir()
        except (ValueError, ProjectError) as exc:
            # This backend is the default, so a deployment with no dbt project on
            # disk lands here without asking to. It used to surface the raw refusal
            # from `require_repo_root`, which is a bare ValueError and says nothing
            # about the backend that actually needs the project, so a host embedding
            # the engine got a stack trace where `resolve_backend` promises a
            # SemanticBackendError. Name the choice instead.
            raise SemanticBackendError(
                f"the local semantic backend needs a dbt project on disk ({exc}). "
                "A deployment without one queries a hosted dbt Cloud Semantic Layer "
                "instead: set `semantic.deployment: dbt_cloud` in config (or pass "
                "--api), which needs no project and no local credential"
            ) from exc
        return cls(project, engine, connector, limits, engine.project_format())

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        """The catalog, read through the project seam rather than parsed here.

        The project format owns the reduction from whatever it holds on disk into
        the neutral catalog, so this backend adds only what it knows: its own
        provenance, and the caveat about how a metric's groupable dimensions were
        computed.

        That caveat is conditional now, because the read is. A project read that
        resolved the join graph carries one row per queryable path and needs no
        caveat; one that could not says so, and says it in ``dimension_scope`` as
        data before it says it in a note.
        """

        view = self._semantic_view()
        unresolved = view.dimension_scope == DIMENSIONS_PER_DECLARATION
        return SemanticCatalog.from_view(
            view, self, notes=[_UNRESOLVED_JOINS_NOTE] if unresolved else []
        )

    def declared_relationships(self) -> list[Any]:
        """MetricFlow entity joins are derived from the catalog by explore."""

        return []

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """dbt's own declared keys already reach grain through its project
        format's ``definitions()``; stating them here too would double them."""

        return [], []

    def _semantic_view(self):
        """The project's semantic catalog, read once per command.

        The engine builds a fresh project per call on purpose (a project is a read
        of files that ``transform apply`` rewrites), so the memo lives here: the
        catalog read and the PII gate's column resolution want the same view, and
        parsing it twice for one command would be work for nothing.
        """

        from ...adapters.project import (
            SemanticCatalogProject,
            semantic_catalog_gap,
        )
        from ...errors import ProjectError

        if self._catalog_view is not None:
            return self._catalog_view
        project = self._format or self._dex.project_format()
        if not isinstance(project, SemanticCatalogProject):
            raise SemanticBackendError(semantic_catalog_gap(project))
        try:
            self._catalog_view = project.semantic_catalog()
        except ProjectError as exc:
            raise SemanticBackendError(
                f"{exc} (or query a hosted deployment with --api)"
            ) from exc
        return self._catalog_view

    # ---- query -------------------------------------------------------------

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        """The dimensions and entities these filter clauses name.

        MetricFlow renders the clause, so it is read with MetricFlow's own filter
        grammar. The gate screens what this returns, so a clause dex could not read
        would be a clause dex could not screen.
        """

        return metricflow_dialect.filter_refs(clauses)

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")

        cache = self._load_cache()

        # PII request-gate, before rendering. Catching a flagged dimension at the
        # request is cheaper and more precise than parsing rendered SQL. A token
        # carrying a grain is screened as the dimension it buckets, and
        # `metric_time` as the time dimensions of the metrics queried, since
        # those are the columns the result groups by.
        refs = requested_dimension_refs(q, filter_refs=self.filter_refs)
        aliases = self._screening_aliases(refs, q.metrics)
        lookup = self._cache_pii_lookup(cache)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup, aliases=aliases)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )

        # Checked against the grains the project declares for these metrics, and
        # only after the gate: a query that would disclose PII is refused for that
        # reason whatever else is wrong with it. Costs nothing, because the catalog
        # this reads is the one the command already parsed.
        q = replace(q, grain=validate_grain(q.grain, available=self._grains(q.metrics)))

        try:
            sql = self._render(q)
        except SemanticBackendError:  # missing extra or uncompiled manifest
            raise
        except Exception as exc:  # a MetricFlow resolution error (unknown metric,
            # unresolvable dimension) is the query's fault, not a crash: surface it.
            raise SemanticBackendError(
                f"could not resolve the metric query: {env.redact(str(exc))}"
            ) from exc

        return self._governed.run(
            sql,
            cache,
            backend=self,
            command="explore semantic query",
            result_type=SemanticQueryResult,
            extra_notes=[
                *name_only_note(
                    unadjudicated_refs(refs, meta_lookup=lookup, aliases=aliases)
                ),
                *sub_threshold_note(refs, meta_lookup=lookup, aliases=aliases),
            ],
        )

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
        """The dimension tokens ``metric_time`` resolves to for these metrics.

        Each input measure's agg time dimension, on its own semantic model, as
        the token a caller would group that dimension by: the gate screens those
        in place of ``metric_time``, which names no column of its own. None where
        the project cannot place every one of them, which leaves ``metric_time``
        on the name heuristic and the result saying so.
        """

        view = self._readable_view()
        return time_axis_tokens(view, metrics) if view is not None else None

    def _screening_aliases(
        self, refs: list[str], metrics: list[str]
    ) -> dict[str, list[str]]:
        """What each requested token is screened as, where that is not itself."""

        view = self._readable_view()
        grains = tuple(
            dict.fromkeys(
                grain
                for element in (
                    (*view.metrics, *view.dimensions) if view is not None else ()
                )
                for grain in (element.queryable_granularities or ())
            )
        )
        aliases: dict[str, list[str]] = {}
        for ref in refs:
            name, _grain = metricflow_dialect.split_grain(ref, grains=grains or None)
            if name == METRIC_TIME:
                axis = self.time_axis_refs(metrics)
                if axis:
                    aliases[ref] = axis
            elif name != ref:
                aliases[ref] = [name]
        return aliases

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One dimension's value domain, rendered by MetricFlow and executed here.

        Everything after the render is the governed spine a metric query runs
        through, because this is the same kind of statement reaching the same
        warehouse. What differs is in front of it, and it is resolved against the
        project's own catalog so a token the layer does not have is refused by name
        rather than surfacing as a MetricFlow resolution error about something else.
        """

        request = resolve_values_request(self._semantic_view(), dimension, metrics)
        cache = self._load_cache()
        notes = screen_values_request(
            request.name, meta_lookup=self._cache_pii_lookup(cache)
        )

        used, sql = self._render_values(request)
        if used and not request.metrics:
            notes.append(values_reach_note(request.token, used, request.reachable))
        return self._governed.run(
            sql,
            cache,
            backend=self,
            command="explore semantic values",
            result_type=SemanticValuesResult,
            extra_notes=notes,
            dimension=request.token,
            scoped_to=used,
        )

    def _render_values(self, request: ValuesRequest) -> tuple[list[str], str]:
        """``(the metrics the rendering used, the SQL)``, cheapest form first.

        A dimension of one semantic model renders as a distinct scan of that model
        alone, which is the cheapest and most direct answer to "what can I filter
        to". A dimension reached through a join has no such rendering: MetricFlow
        refuses a distinct-values query it cannot reach without a measure, and the
        hosted layer refuses the same request for the same reason. What makes it
        answerable is a metric that reaches it, which turns the statement into a
        join and an aggregate.

        So the cheap form is tried first and the scoped form is the fallback rather
        than the default. Rendering costs nothing (no connection is opened and
        nothing is priced until the caller's handshake), so the second attempt is
        free, and the metric it settled on travels back to be reported: the values
        of a dimension *for a metric* can be narrower than the column's own domain,
        and a caller must not have to guess which of the two it is holding.
        """

        attempts: list[list[str]] = [request.metrics] if request.metrics else [[]]
        if not request.metrics and request.reachable:
            attempts.append(request.reachable[:1])
        failure: Exception | None = None
        for used in attempts:
            try:
                return used, self._render_dimension_values(request.token, used)
            except SemanticBackendError:  # missing extra or uncompiled manifest
                raise
            except Exception as exc:
                failure = exc
        raise SemanticBackendError(
            f"could not resolve the values of {request.token}: "
            f"{env.redact(str(failure))}"
        ) from failure

    def _render_dimension_values(self, dimension: str, metrics: list[str]) -> str:
        return (
            self._metricflow_engine()
            .explain_get_dimension_values(
                metric_names=metrics or None, get_group_by_values=dimension
            )
            .sql_statement.sql
        )

    # The spine's cache, PII lookup and relation pre-check, reachable here so a
    # caller holding this backend (a test pinning the gate) need not know the
    # spine is a separate object.

    def _load_cache(self):
        return self._governed.load_cache()

    def _cache_pii_lookup(self, cache):
        return self._governed.pii_lookup(cache)

    def _relation_precheck(
        self,
        sql: str,
        cache,
        dialect: str,
        live_identifiers: Callable[[], list[str]],
    ) -> tuple[str | None, list[str]]:
        return self._governed.relation_precheck(sql, cache, dialect, live_identifiers)

    def _dimension_columns(self) -> dict[str, tuple[str, str]]:
        """``dimension token -> (relation, physical column)``, from the project.

        The format resolves this, not this backend: it is the one place that knows
        which relation a semantic model sits on and which column each element
        references. A token whose reference is a computed expression is absent
        rather than guessed, because guessing a column out of an expression would
        make the PII gate over-claim; the name heuristic still covers it.

        A project that cannot answer at all leaves the gate on that heuristic
        alone, which is the fail-closed floor and the same posture an unprofiled
        relation already gets.
        """

        if self._dim_columns is None:
            view = self._readable_view()
            self._dim_columns = dict(view.physical_columns) if view else {}
        return self._dim_columns

    def _dimension_expressions(self) -> dict[str, tuple[str, str]]:
        """``dimension token -> (relation, expression)`` for computed elements.

        The tokens :meth:`_dimension_columns` leaves out because their reference
        is an expression: the gate reads the expression for the columns it reads
        rather than screening the token's name alone.
        """

        if self._dim_expressions is None:
            view = self._readable_view()
            self._dim_expressions = dict(view.computed_columns) if view else {}
        return self._dim_expressions

    def _readable_view(self):
        """The catalog, or None where the project cannot be read at all.

        For the gate's evidence only: a project that cannot answer leaves the
        gate on the name heuristic, the fail-closed floor, and the command
        refuses for the unreadable project a step later, when it renders.
        """

        from ...errors import DexError

        try:
            return self._semantic_view()
        except (SemanticBackendError, DexError):
            return None

    def _render(self, q: SemanticQuery) -> str:
        from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

        request = MetricFlowQueryRequest.create(
            metric_names=q.metrics,
            group_by_names=self._group_by_names(q) or None,
            where_constraints=q.where or None,
            order_by_names=q.order_by or None,
            limit=q.limit,
        )
        return self._metricflow_engine().explain(request).sql_statement.sql

    def _group_by_names(self, q: SemanticQuery) -> list[str]:
        # MetricFlow spells a time grain into the token, which is the dialect's
        # business rather than this backend's.
        return [metricflow_dialect.spell_grain(tok, q.grain) for tok in q.group_by]

    def _grains(self, metrics: list[str]) -> list[str] | None:
        """The grains every one of these metrics can be queried at, per the project.

        None where the project could not be read at all, which leaves MetricFlow to
        refuse an impossible grain on its own authority. That is the same posture
        the hosted backend takes when the layer does not answer: dex declines to
        refuse a grain the layer never spoke about.
        """

        try:
            view = self._semantic_view()
        except SemanticBackendError:
            return None
        return queryable_grains(
            metrics,
            {
                metric.name: metric.queryable_granularities
                for metric in view.metrics
                if metric.queryable_granularities is not None
            },
        )

    def _metricflow_engine(self):
        if self._mf_engine is not None:
            return self._mf_engine
        self._mf_engine = metricflow_engine(
            self._project,
            self._connector,
            missing_extra_message=_MISSING_EXTRA,
        )
        return self._mf_engine
