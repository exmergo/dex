"""Connector adapters. DuckDB is real today; the cloud adapters are stubs.

``get_adapter`` is the single entry point so callers never import a connector
client directly; the client libraries stay behind their extras and are imported
only when their adapter is constructed.
"""

from __future__ import annotations

from typing import Any


def get_adapter(connector: str, **kwargs: Any):
    """Construct the adapter for ``connector``. Only DuckDB is wired in v0.1."""

    if connector == "duckdb":
        from .duckdb import DuckDBAdapter

        return DuckDBAdapter(**kwargs)
    if connector in {"snowflake", "bigquery", "databricks", "postgres"}:
        raise NotImplementedError(f"the '{connector}' adapter is not yet implemented")
    raise ValueError(f"unknown connector '{connector}'")
