"""Connector-aware cost gating across the paradigms: bytes-scanned (BigQuery),
compute-time (Snowflake, Databricks), DB load (Postgres), and resource bounds
(DuckDB). Cost is surfaced as a preflight estimate before any spend; nothing runs
without a ceiling. DuckDB resource bounds are enforced by the adapter today; the
billed-paradigm logic is not yet implemented.
"""

from __future__ import annotations

from typing import Any


def preflight(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError
