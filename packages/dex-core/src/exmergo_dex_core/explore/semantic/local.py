"""The local MetricFlow backend: query the dbt project's own semantic layer.

``list`` is a pure read-view over the compiled ``target/semantic_manifest.json``,
so it needs no extra and no warehouse connection: it is the discovery surface an
agent uses to find what it can query.

``query`` renders the metric SQL with MetricFlow's ``explain()`` through a
renderer-only ``SqlClient`` (MetricFlow never opens a connection or sees a
credential), then runs the rendered SQL through dex's own spine, in order: a PII
request-gate on the grouped and filtered dimensions (resolved to physical columns
and checked against the ``.dex/`` cache, with a name heuristic as the floor), a
SELECT-only assertion, a relation pre-check against the connection's own
inventory, the cost-before-spend handshake, and the connector. dex owns execution
here, so the full cost guard applies, unlike the hosted backend.

The SELECT-only assertion runs before the relation pre-check because the pre-check
may introspect the live connection: whatever else happens to a rendered statement,
it is proven read-only first.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import ClassVar

from ... import command_args
from ... import envelope as env
from ...adapters import get_dialect
from ...cache import match_identifier
from ...config import QueryLimits, pii_override_paths
from ..results import SemanticQueryResult
from . import (
    DIMENSIONS_PER_DECLARATION,
    EXECUTION_DEX,
    PII_BLOCK_CONFIDENCE,
    SemanticBackendError,
    SemanticCatalog,
    SemanticQuery,
    SemanticQueryRefusedError,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
)

# The one caveat a local catalog carries: a metric's groupable dimensions are
# computed per owning semantic model, single-hop, so a layer with joins has
# dimensions reachable at query time that this list does not name.
_LOCAL_LIST_NOTE = (
    "local list: a metric's dimensions are those of its owning semantic "
    "model(s), entity-qualified; dimensions reachable only through a join "
    "resolve at query time (or list with --api)"
)

_MISSING_EXTRA = (
    "local metric queries need the [semantic] extra: "
    "pip install 'exmergo-dex-core[semantic]'"
)

# dex connector -> (renderer submodule under _RENDER_ROOT, class, SqlEngine name).
# MetricFlow ships a renderer per dialect; the shim hands the engine the one
# matching the active connector so the SQL it renders is in that connector's dialect.
_RENDER_ROOT = "metricflow.sql.render"
_RENDERERS: dict[str, tuple[str, str, str]] = {
    "duckdb": ("duckdb_renderer", "DuckDbSqlPlanRenderer", "DUCKDB"),
    "bigquery": ("big_query", "BigQuerySqlPlanRenderer", "BIGQUERY"),
    "snowflake": ("snowflake", "SnowflakeSqlPlanRenderer", "SNOWFLAKE"),
    "databricks": ("databricks", "DatabricksSqlPlanRenderer", "DATABRICKS"),
    "postgres": ("postgres", "PostgresSqlPlanRenderer", "POSTGRES"),
    "redshift": ("redshift", "RedshiftSqlPlanRenderer", "REDSHIFT"),
}


class _RendererOnlySqlClient:
    """A MetricFlow ``SqlClient`` that can render but never execute. If MetricFlow
    calls anything execution-shaped, it raises: the mechanical form of "MetricFlow
    never reaches the warehouse". Only ``explain()`` (pure rendering) uses it."""

    def __init__(self, renderer, engine) -> None:
        self._renderer = renderer
        self._engine = engine

    @property
    def sql_plan_renderer(self):
        return self._renderer

    @property
    def sql_engine_type(self):
        return self._engine

    def render_bind_parameter_key(self, bind_parameter_key: str) -> str:
        return f":{bind_parameter_key}"

    def query(self, *args, **kwargs):
        raise RuntimeError("renderer-only SqlClient: execution is not permitted")

    def execute(self, *args, **kwargs):
        raise RuntimeError("renderer-only SqlClient: execution is not permitted")

    def dry_run(self, *args, **kwargs):
        raise RuntimeError("renderer-only SqlClient: execution is not permitted")

    def close(self) -> None:
        pass


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
    # One row per declaration, single-hop qualified, which is why this backend
    # reports fewer dimensions than the hosted one on an identical layer.
    dimension_scope = DIMENSIONS_PER_DECLARATION

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
        # The dex engine, not an adapter: the connection is opened through it on
        # the one billed path below, so this backend never becomes a second place
        # that discovers credentials or builds a cost gate. Named `_dex` because
        # "engine" already means the MetricFlow one throughout this module.
        self._dex = engine
        self._config = engine.config
        self._store = engine.store
        self._connector = connector
        self._limits = limits
        self._mf_engine = None
        self._dim_columns: dict[str, tuple[str, str]] | None = None
        self._catalog_view = None

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
        """

        return SemanticCatalog.from_view(
            self._semantic_view(), self, notes=[_LOCAL_LIST_NOTE]
        )

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

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")

        cache = self._load_cache()

        # PII request-gate, before rendering. Catching a flagged dimension at the
        # request is cheaper and more precise than parsing rendered SQL.
        blocked = screen_dimension_refs(
            requested_dimension_refs(q), meta_lookup=self._cache_pii_lookup(cache)
        )
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )

        try:
            sql = self._render(q)
        except SemanticBackendError:  # missing extra or uncompiled manifest
            raise
        except Exception as exc:  # a MetricFlow resolution error (unknown metric,
            # unresolvable dimension) is the query's fault, not a crash: surface it.
            raise SemanticBackendError(
                f"could not resolve the metric query: {env.redact(str(exc))}"
            ) from exc

        from ...guards.sql_guard import NotSelectOnlyError, assert_select_only

        dialect = get_dialect(self._connector)
        try:
            assert_select_only(sql, dialect=dialect)
        except NotSelectOnlyError as exc:
            raise SemanticQueryRefusedError(
                f"rendered metric SQL was not read-only: {exc}"
            ) from exc

        # One adapter for the whole command: the pre-check may introspect the
        # connection, and `_adapter` rebuilds the cost gate on every call, so
        # asking twice would settle and rebuild a gate for nothing.
        adapter = self._dex._adapter("explore semantic query")
        refusal, unprofiled = self._relation_precheck(
            sql,
            cache,
            dialect,
            lambda: [meta.identifier for meta in adapter.list_objects()],
        )
        if refusal is not None:
            raise SemanticBackendError(refusal)

        estimate_fn = getattr(adapter, "query_estimate", None)
        estimate = estimate_fn(sql) if estimate_fn else 0.0
        command_args.billed_handshake("explore semantic query", adapter, estimate)
        result = adapter.run_query(
            sql,
            max_rows=self._limits.max_rows,
            timeout_seconds=self._limits.timeout_seconds,
        )
        capped = cap_columnar(
            result.columns,
            result.types,
            result.cells,
            max_rows=self._limits.max_rows,
            max_cell_chars=self._limits.max_cell_chars,
            max_payload_bytes=self._limits.max_payload_bytes,
            truncated_by_source=result.truncated,
            extra_notes=_unprofiled_note(unprofiled),
        )
        record = SemanticQueryResult.from_capped(capped, backend=self)
        return command_args.stamp_spend(record, adapter)

    def _load_cache(self):
        """The exploration cache with config PII overrides applied in memory, or None.

        Absence is not fatal here (unlike ``explore query``, whose whole policy is
        the cache: it tracks PII taint through the projection of agent-authored SQL
        and cannot do that for a relation it has never seen). A metric query is
        governed at the request, by dimension name, before any SQL exists, so the
        name heuristic and the semantic layer's own metadata still bind, and the
        relation pre-check falls through to the live inventory. A repo that never
        ran ``explore map`` can still query metrics.
        """

        try:
            cache = self._store.load_cache()
        except Exception:
            return None
        if cache is None:
            return None
        overrides = pii_override_paths(getattr(self._config, "pii_overrides", []) or [])
        if not overrides:
            return cache
        from ..commands import _mask_overridden

        return _mask_overridden(cache, overrides)

    def _cache_pii_lookup(self, cache):
        """A ``dimension token -> {"pii": True}`` lookup backed by the cache.

        A semantic dimension (``session__is_deleted``) maps to a physical column on
        its owning model, so the token is resolved through the manifest to
        (relation, column) and that column's cached PII flag decides. This is the
        value-evidence-backed adjudication the profiler produced; the name
        heuristic in ``screen_dimension_refs`` remains the floor underneath it, so
        an unprofiled column is still caught by its name. Returns None for a token
        the cache cannot speak to, which leaves the heuristic in charge.
        """

        if cache is None:
            return None
        columns = self._dimension_columns()
        if not columns:
            return None
        known = [dataset.identifier for dataset in cache.datasets]

        def lookup(ref: str):
            target = columns.get(ref)
            if target is None:
                return None
            relation, column_name = target
            matches = match_identifier(relation, known)
            for dataset in cache.datasets:
                if dataset.identifier not in matches:
                    continue
                for column in dataset.columns:
                    if column.name.lower() != column_name.lower():
                        continue
                    flag = column.pii
                    if flag is not None and flag.confidence >= PII_BLOCK_CONFIDENCE:
                        return {"pii": True, "category": flag.category.value}
                    # Profiled and cleared (or a human override): authoritative,
                    # so say so rather than leaving it to the name heuristic.
                    return {"pii": False}
            return None

        return lookup

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
            try:
                self._dim_columns = dict(self._semantic_view().physical_columns)
            except SemanticBackendError:
                self._dim_columns = {}
        return self._dim_columns

    def _relation_precheck(
        self,
        sql: str,
        cache,
        dialect: str,
        live_identifiers: Callable[[], list[str]],
    ) -> tuple[str | None, list[str]]:
        """``(refusal message or None, relations with no profile)`` for the
        rendered SQL.

        MetricFlow bakes ``node_relation.relation_name`` from the compiled manifest
        straight into the SQL, so a project compiled against another database (or a
        different dev target) renders relations that do not exist here. Catching
        that is worth a precise message before the cost handshake rather than a
        table-not-found from the warehouse.

        The authority is the *connection*, not the ``.dex/`` cache. That cache
        records what has been profiled, which is a different question: a model
        ``transform build`` created minutes ago is in the warehouse and not in the
        cache, and refusing it made "build a model, then validate its metric"
        impossible without a profiling pass in between. So the cache is only a free
        fast path, and anything it cannot resolve is asked of the live inventory,
        the same authority ``explore profile`` resolves its arguments against.

        What the listing can and cannot settle is
        :func:`~...cache.relation_verdict`. An inventory that cannot be read at all
        settles nothing, and a relation that is genuinely absent still fails at
        planning without billing.

        Resolution is by suffix in both directions, because the cache and the
        inventory are namespace-normalized per connector and an exact string
        compare would reject legitimate spellings.
        """

        # Imported here, not at module scope: `explore semantic` is routed from
        # its own module so a remote-only install with no dialect engine can reach
        # the hosted backend, and a top-level guards import would undo that.
        from ...cache import relation_verdict
        from ...guards.sql_guard import referenced_relations

        relations = referenced_relations(sql, dialect=dialect)
        if not relations:
            return None, []

        datasets = list(cache.datasets) if cache is not None else []
        cached = [dataset.identifier for dataset in datasets]
        # Presence is not a profile: `explore map` writes inventory-only entries
        # with no columns, and those tell the PII gate nothing.
        profiled = {dataset.identifier for dataset in datasets if dataset.columns}

        unresolved: list[str] = []
        unprofiled: list[str] = []
        for name in relations:
            matches = match_identifier(name, cached)
            if not matches:
                unresolved.append(name)
                unprofiled.append(name)
            elif not any(match in profiled for match in matches):
                unprofiled.append(name)
        if not unresolved:
            return None, unprofiled

        try:
            live = live_identifiers()
        except Exception:
            return None, unprofiled

        verdicts: dict[str, list[str]] = {"foreign": [], "missing": []}
        for name in unresolved:
            if match_identifier(name, live):
                continue
            verdict = relation_verdict(name, live)
            if verdict is not None:
                verdicts[verdict].append(name)

        if verdicts["foreign"]:
            named = ", ".join(sorted(set(verdicts["foreign"])))
            return (
                f"refused: the metric query reads {named}, in a namespace this "
                "connection does not reach. The project was compiled against a "
                "different namespace than the one dex is connected to; re-run "
                "`dbt parse` against the target you are querying, or point dex at "
                "the connection the project was built for.",
                unprofiled,
            )
        if verdicts["missing"]:
            named = ", ".join(sorted(set(verdicts["missing"])))
            return (
                f"refused: the metric query reads {named}, which this connection "
                "does not have: its namespace was listed and the relation was not "
                "in it. Build the model into the target you are querying, or "
                "re-run `dbt parse` if the project was compiled against a "
                "different target.",
                unprofiled,
            )
        return None, unprofiled

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
        # MetricFlow spells a time grain into the token (metric_time__month); a
        # bare metric_time with --grain becomes that form. Other tokens pass through.
        names: list[str] = []
        for tok in q.group_by:
            if tok == "metric_time" and q.grain:
                names.append(f"metric_time__{q.grain.lower()}")
            else:
                names.append(tok)
        return names

    def _metricflow_engine(self):
        if self._mf_engine is not None:
            return self._mf_engine
        try:
            from metricflow.engine.metricflow_engine import MetricFlowEngine
            from metricflow_semantics.model.dbt_manifest_parser import (
                parse_manifest_from_dbt_generated_manifest,
            )
            from metricflow_semantics.model.semantic_manifest_lookup import (
                SemanticManifestLookup,
            )
        except ImportError as exc:
            raise SemanticBackendError(_MISSING_EXTRA) from exc

        from ... import dbt_project

        manifest_path = self._project / dbt_project.SEMANTIC_MANIFEST_PATH
        if not manifest_path.is_file():
            raise SemanticBackendError(
                "no compiled semantic manifest at target/semantic_manifest.json; "
                "run `dbt parse` in the project first"
            )
        manifest = parse_manifest_from_dbt_generated_manifest(
            manifest_path.read_text(encoding="utf-8")
        )
        lookup = SemanticManifestLookup(manifest)
        self._mf_engine = MetricFlowEngine(
            semantic_manifest_lookup=lookup, sql_client=self._sql_client()
        )
        return self._mf_engine

    def _sql_client(self) -> _RendererOnlySqlClient:
        spec = _RENDERERS.get(self._connector)
        if spec is None:
            # An inert capability declares itself rather than degrading:
            # falling back to another dialect's renderer would emit SQL that
            # parses and returns wrong numbers, which is the worst of the three
            # available behaviors. MetricFlow ships no ClickHouse renderer, so
            # the connector is named here rather than silently missing.
            supported = ", ".join(sorted(_RENDERERS))
            raise SemanticBackendError(
                f"no MetricFlow renderer for connector '{self._connector}'; "
                f"local metric queries support {supported}. dex will not "
                "render a metric through another dialect's renderer, because "
                "the SQL would run and the numbers would be wrong"
            )
        module_name, class_name, engine_name = spec
        from metricflow.protocols.sql_client import SqlEngine

        module = import_module(f"{_RENDER_ROOT}.{module_name}")
        renderer = getattr(module, class_name)()
        return _RendererOnlySqlClient(renderer, SqlEngine[engine_name])


# ---- the relation pre-check's plumbing --------------------------------------


def _unprofiled_note(relations: list[str]) -> list[str]:
    """The disclosure that a queried relation carries no profile.

    The PII request-gate adjudicates a dimension from its physical column's cached
    flag and falls back to the name heuristic when the cache cannot speak. That
    fallback is the fail-closed floor, not an equivalent, so a result whose
    relations were never profiled says which ones and how to fix it rather than
    letting the weaker screening pass unremarked.
    """

    if not relations:
        return []
    ordered = sorted(set(relations))
    return [
        "PII screening fell back to the name heuristic: the .dex/ cache holds no "
        f"profile for {', '.join(ordered)}. Run `explore profile "
        f"{' '.join(ordered)}` for value-evidence screening."
    ]
