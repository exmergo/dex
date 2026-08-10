"""Connector adapters: DuckDB, BigQuery, Snowflake, Databricks, Postgres, and
Redshift.

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
    "redshift": "redshift",
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
    if connector == "redshift":
        from .redshift import RedshiftAdapter

        return RedshiftAdapter(**kwargs)
    raise ValueError(f"unknown connector '{connector}'")


def get_dialect(connector: str) -> str:
    """The SQLGlot dialect for ``connector``, defaulting to DuckDB for unknown
    names so parsing has a deterministic fallback."""

    return _DIALECTS.get(connector, "duckdb")


# Connectors that bill nothing for a scan. Kept beside the dialect map and for
# the same reason: a command deciding whether an optional scan is free enough to
# run unasked must not have to construct an adapter (and open a connection) to
# find out. An unknown name is treated as billed, so the cautious answer is the
# default one.
_FREE_CONNECTORS = frozenset({"duckdb"})


def is_free_connector(connector: str) -> bool:
    """Whether ``connector`` bills nothing, resolved without constructing anything."""

    return connector in _FREE_CONNECTORS
