"""Connector adapters: DuckDB, BigQuery, Snowflake, Databricks, and Postgres.

``get_adapter`` is the single entry point so callers never import a connector
client directly; the client libraries stay behind their extras and are imported
only when their adapter is constructed. ``get_dialect`` exposes the SQLGlot
dialect without constructing anything (the query firewall needs it before a
connection exists).
"""

from __future__ import annotations

from typing import Any

# Connector name -> SQLGlot dialect. Kept here (not read off adapter classes)
# so resolving a dialect never imports a client library.
_DIALECTS = {
    "duckdb": "duckdb",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "postgres": "postgres",
}


def get_adapter(connector: str, **kwargs: Any):
    """Construct the adapter for ``connector``."""

    if connector == "duckdb":
        from .duckdb import DuckDBAdapter

        return DuckDBAdapter(**kwargs)
    if connector == "bigquery":
        from .bigquery import BigQueryAdapter

        return BigQueryAdapter(**kwargs)
    if connector == "snowflake":
        from .snowflake import SnowflakeAdapter

        return SnowflakeAdapter(**kwargs)
    if connector == "postgres":
        from .postgres import PostgresAdapter

        return PostgresAdapter(**kwargs)
    if connector == "databricks":
        from .databricks import DatabricksAdapter

        return DatabricksAdapter(**kwargs)
    raise ValueError(f"unknown connector '{connector}'")


def get_dialect(connector: str) -> str:
    """The SQLGlot dialect for ``connector``, defaulting to DuckDB for unknown
    names so parsing has a deterministic fallback."""

    return _DIALECTS.get(connector, "duckdb")
