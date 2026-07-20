"""The local MetricFlow backend: query the dbt project's own semantic layer.

``list`` is a pure read-view over the compiled ``target/semantic_manifest.json``,
so it needs no extra and no warehouse connection: it is the discovery surface an
agent uses to find what it can query.

``query`` renders the metric SQL with MetricFlow's ``explain()`` through a
renderer-only ``SqlClient`` (MetricFlow never opens a connection or sees a
credential), then runs the rendered SQL through dex's own spine, in order: a PII
request-gate on the grouped and filtered dimensions (resolved to physical columns
and checked against the ``.dex/`` cache, with a name heuristic as the floor), a
relation pre-check against the cached inventory, a SELECT-only assertion, the
cost-before-spend handshake, and the connector. dex owns execution here, so the
full cost guard applies, unlike the hosted backend.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

from ... import command_args
from ... import envelope as env
from ...adapters import get_dialect
from ...cache import DexStore, match_identifier
from ...config import QueryLimits, pii_override_paths
from . import (
    PII_BLOCK_CONFIDENCE,
    DimensionInfo,
    EntityInfo,
    MetricInfo,
    SemanticBackendError,
    SemanticCatalog,
    SemanticQuery,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
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

    def __init__(
        self,
        project: Path,
        config,
        args,
        connector: str,
        limits: QueryLimits,
        repo_root: str | Path = ".",
    ) -> None:
        self._project = project
        self._config = config
        self._args = args
        self._connector = connector
        self._limits = limits
        self._repo_root = repo_root
        self._mf_engine = None
        self._dim_columns: dict[str, tuple[str, str]] | None = None

    @classmethod
    def from_args(cls, args, config, repo_root: str) -> LocalMetricFlowBackend:
        connector = getattr(args, "connector", None) or getattr(
            config, "connector", "duckdb"
        )
        limits = getattr(config, "query", None) or QueryLimits()
        return cls(
            command_args.project_dir(args),
            config,
            args,
            connector,
            limits,
            repo_root,
        )

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        from ... import dbt_project

        manifest = dbt_project._read_semantic_manifest(self._project)
        if manifest is None:
            raise SemanticBackendError(
                "no compiled semantic manifest at target/semantic_manifest.json; "
                "run `dbt parse` in the project so `explore semantic` can read it "
                "(or query a hosted deployment with --api)"
            )

        entities: dict[str, str] = {}
        dimensions: dict[str, str] = {}
        model_dims: dict[str, list[str]] = {}
        measure_model: dict[str, str] = {}
        for model in manifest.get("semantic_models") or []:
            model_name = model.get("name")
            primary = None
            for entity in model.get("entities") or []:
                entities.setdefault(
                    entity.get("name"), (entity.get("type") or "").lower()
                )
                if str(entity.get("type", "")).lower() == "primary":
                    primary = entity.get("name")
            qualified: list[str] = []
            for dim in model.get("dimensions") or []:
                # Entity-qualified name, the form a metric query groups by
                # (session__created_at). Cross-model joined dimensions resolve
                # only at query time, hence the catalog note below.
                name = f"{primary}__{dim.get('name')}" if primary else dim.get("name")
                qualified.append(name)
                dimensions.setdefault(name, (dim.get("type") or "").lower())
            model_dims[model_name] = qualified
            for measure in model.get("measures") or []:
                measure_model[measure.get("name")] = model_name
        dimensions.setdefault("metric_time", "time")

        metrics: list[MetricInfo] = []
        for metric in manifest.get("metrics") or []:
            params = metric.get("type_params") or {}
            owners: set[str] = set()
            for input_measure in params.get("input_measures") or []:
                measure_name = (
                    input_measure.get("name")
                    if isinstance(input_measure, dict)
                    else input_measure
                )
                owner = measure_model.get(measure_name)
                if owner:
                    owners.add(owner)
            metric_dims = {"metric_time"}
            for owner in owners:
                metric_dims.update(model_dims.get(owner, []))
            metrics.append(
                MetricInfo(
                    name=metric.get("name"),
                    type=(metric.get("type") or "").lower(),
                    label=metric.get("label"),
                    description=metric.get("description"),
                    dimensions=sorted(metric_dims),
                )
            )

        return SemanticCatalog(
            backend=self.name,
            metrics=metrics,
            dimensions=[
                DimensionInfo(name=n, type=t) for n, t in sorted(dimensions.items())
            ],
            entities=[EntityInfo(name=n, type=t) for n, t in sorted(entities.items())],
            notes=[
                "local list: a metric's dimensions are those of its owning "
                "semantic model(s), entity-qualified; dimensions reachable only "
                "through a join resolve at query time (or list with --api)"
            ],
        )

    # ---- query -------------------------------------------------------------

    def query(self, q: SemanticQuery) -> env.Envelope:
        if not q.metrics:
            return env.error("a metric query needs at least one --metric")

        cache = self._load_cache()

        # PII request-gate, before rendering. Catching a flagged dimension at the
        # request is cheaper and more precise than parsing rendered SQL.
        blocked = screen_dimension_refs(
            requested_dimension_refs(q), meta_lookup=self._cache_pii_lookup(cache)
        )
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            return env.error(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )

        try:
            sql = self._render(q)
        except SemanticBackendError as exc:  # missing extra or uncompiled manifest
            return env.error(str(exc))
        except Exception as exc:  # a MetricFlow resolution error (unknown metric,
            # unresolvable dimension) is the query's fault, not a crash: surface it.
            return env.error(
                f"could not resolve the metric query: {env.redact(str(exc))}"
            )

        from ...guards.sql_guard import NotSelectOnlyError, assert_select_only

        dialect = get_dialect(self._connector)
        # The rendered SQL bakes in the relation names the project was compiled
        # against, which routinely disagree with the connection when a manifest
        # was built elsewhere. Catch that here, with a precise message, instead of
        # letting the warehouse answer with a table-not-found after a billed job.
        mismatch = self._namespace_mismatch(sql, cache, dialect)
        if mismatch is not None:
            return env.error(mismatch)

        try:
            assert_select_only(sql, dialect=dialect)
        except NotSelectOnlyError as exc:
            return env.error(f"rendered metric SQL was not read-only: {exc}")

        adapter = command_args.open_from_args(self._args)
        try:
            estimate_fn = getattr(adapter, "query_estimate", None)
            estimate = estimate_fn(sql) if estimate_fn else 0.0
            unconfirmed = command_args.billed_handshake(
                "explore semantic query", adapter, estimate
            )
            if unconfirmed is not None:
                return unconfirmed
            result = adapter.run_query(
                sql,
                max_rows=self._limits.max_rows,
                timeout_seconds=self._limits.timeout_seconds,
            )
            data = cap_columnar(
                result.columns,
                result.types,
                result.cells,
                max_rows=self._limits.max_rows,
                max_cell_chars=self._limits.max_cell_chars,
                max_payload_bytes=self._limits.max_payload_bytes,
                truncated_by_source=result.truncated,
            )
            data["backend"] = self.name
            envelope = env.ok(data)
            command_args.stamp_spend(envelope, adapter)
            return envelope
        finally:
            adapter.close()

    def _load_cache(self):
        """The ``.dex/`` cache with config PII overrides applied in memory, or None.

        Absence is not fatal here (unlike ``explore query``, whose whole policy is
        the cache): the metric query still has the name heuristic and the semantic
        layer's own metadata, and the relation pre-check simply has no inventory to
        check against. A repo that never ran ``explore map`` can still query metrics.
        """

        try:
            cache = DexStore(self._repo_root).load_cache()
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
        """``entity-qualified dimension -> (relation, physical column)`` from the
        compiled manifest. Computed dimensions (an expression rather than a bare
        column) map to nothing: guessing a column out of an expression would make
        the gate over-claim, and the name heuristic still covers them."""

        from ... import dbt_project

        if self._dim_columns is not None:
            return self._dim_columns
        mapping: dict[str, tuple[str, str]] = {}
        manifest = dbt_project._read_semantic_manifest(self._project)
        for model in (manifest or {}).get("semantic_models") or []:
            node_relation = model.get("node_relation") or {}
            relation = node_relation.get("relation_name") or node_relation.get("alias")
            if not relation:
                continue
            relation = dbt_project._strip_relation_quoting(str(relation))
            primary = None
            for entity in model.get("entities") or []:
                if str(entity.get("type", "")).lower() == "primary":
                    primary = entity.get("name")
            for element in (model.get("dimensions") or []) + (
                model.get("entities") or []
            ):
                column = dbt_project.physical_column(element)
                if not column:
                    continue
                name = element.get("name")
                for token in {name, f"{primary}__{name}" if primary else name}:
                    if token:
                        mapping.setdefault(token, (relation, column))
        self._dim_columns = mapping
        return mapping

    def _namespace_mismatch(self, sql: str, cache, dialect: str) -> str | None:
        """A refusal message when the rendered SQL reads relations this connection
        does not have, else None.

        MetricFlow bakes ``node_relation.relation_name`` from the compiled manifest
        straight into the SQL, so a project compiled against another database (or a
        different dev target) renders relations that do not exist here. Without an
        inventory there is nothing to check against, and a relation that resolves by
        suffix is accepted: the cache is normalized per connector, so an exact
        string match would reject legitimate namespace spellings.
        """

        if cache is None or not cache.datasets:
            return None
        try:
            import sqlglot

            parsed = sqlglot.parse_one(sql, read=dialect)
        except Exception:
            return None  # unparseable SQL is the SELECT-only guard's problem
        from sqlglot import exp

        known = [dataset.identifier for dataset in cache.datasets]
        unknown: list[str] = []
        for table in parsed.find_all(exp.Table):
            parts = (table.args.get("catalog"), table.args.get("db"), table.this)
            name = ".".join(part.name for part in parts if part)
            if not name or not table.name:
                continue
            # Resolve the qualified name as written. Deliberately no bare-name
            # fallback: `match_identifier` matches any identifier ending in
            # `.orders`, so falling back would let a relation from another
            # database pass purely because a same-named table exists here, which
            # is exactly the mismatch this check exists to catch.
            if not match_identifier(name, known):
                unknown.append(name)
        if not unknown:
            return None
        named = ", ".join(sorted(set(unknown)))
        return (
            f"refused: the metric query reads {named}, which this connection does "
            "not have. The project was compiled against a different namespace than "
            "the one dex is connected to; re-run `dbt parse` against the target "
            "you are querying, or point dex at the connection the project was "
            "built for."
        )

    def _render(self, q: SemanticQuery) -> str:
        from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

        request = MetricFlowQueryRequest.create(
            metric_names=q.metrics,
            group_by_names=self._group_by_names(q) or None,
            where_constraints=q.where or None,
            order_by_names=q.order_by or None,
            limit=q.limit,
        )
        return self._engine().explain(request).sql_statement.sql

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

    def _engine(self):
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
            raise SemanticBackendError(
                f"no MetricFlow renderer for connector '{self._connector}'; local "
                "metric queries support duckdb, bigquery, snowflake, databricks, "
                "postgres, and redshift"
            )
        module_name, class_name, engine_name = spec
        from metricflow.protocols.sql_client import SqlEngine

        module = import_module(f"{_RENDER_ROOT}.{module_name}")
        renderer = getattr(module, class_name)()
        return _RendererOnlySqlClient(renderer, SqlEngine[engine_name])
