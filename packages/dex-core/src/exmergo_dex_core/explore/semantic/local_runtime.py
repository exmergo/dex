"""MetricFlow loading and renderer-only SQL client construction."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from .backend import SemanticBackendError

_RENDER_ROOT = "metricflow.sql.render"
_RENDERERS: dict[str, tuple[str, str, str]] = {
    "duckdb": ("duckdb_renderer", "DuckDbSqlPlanRenderer", "DUCKDB"),
    "bigquery": ("big_query", "BigQuerySqlPlanRenderer", "BIGQUERY"),
    "snowflake": ("snowflake", "SnowflakeSqlPlanRenderer", "SNOWFLAKE"),
    "databricks": ("databricks", "DatabricksSqlPlanRenderer", "DATABRICKS"),
    "postgres": ("postgres", "PostgresSqlPlanRenderer", "POSTGRES"),
    "redshift": ("redshift", "RedshiftSqlPlanRenderer", "REDSHIFT"),
}


class RendererOnlySqlClient:
    """A MetricFlow SQL client that can render and can never execute."""

    def __init__(self, renderer: Any, engine: Any) -> None:
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


def sql_client(connector: str) -> RendererOnlySqlClient:
    """Build the MetricFlow renderer matching dex's active connector."""

    spec = _RENDERERS.get(connector)
    if spec is None:
        supported = ", ".join(sorted(_RENDERERS))
        raise SemanticBackendError(
            f"no MetricFlow renderer for connector '{connector}'; local metric "
            f"queries support {supported}. dex will not render a metric through "
            "another dialect's renderer, because the SQL would run and the "
            "numbers would be wrong"
        )
    module_name, class_name, engine_name = spec
    from metricflow.protocols.sql_client import SqlEngine

    module = import_module(f"{_RENDER_ROOT}.{module_name}")
    renderer = getattr(module, class_name)()
    return RendererOnlySqlClient(renderer, SqlEngine[engine_name])


def metricflow_engine(
    project: Path, connector: str, *, missing_extra_message: str
) -> Any:
    """Load a compiled semantic manifest into a renderer-only MetricFlow engine."""

    try:
        from metricflow.engine.metricflow_engine import MetricFlowEngine
        from metricflow_semantics.model.dbt_manifest_parser import (
            parse_manifest_from_dbt_generated_manifest,
        )
        from metricflow_semantics.model.semantic_manifest_lookup import (
            SemanticManifestLookup,
        )
    except ImportError as exc:
        raise SemanticBackendError(missing_extra_message) from exc

    from ... import dbt_project

    manifest_path = project / dbt_project.SEMANTIC_MANIFEST_PATH
    if not manifest_path.is_file():
        raise SemanticBackendError(
            "no compiled semantic manifest at target/semantic_manifest.json; "
            "run `dbt parse` in the project first"
        )
    manifest = parse_manifest_from_dbt_generated_manifest(
        manifest_path.read_text(encoding="utf-8")
    )
    return MetricFlowEngine(
        semantic_manifest_lookup=SemanticManifestLookup(manifest),
        sql_client=sql_client(connector),
    )
