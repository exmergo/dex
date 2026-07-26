"""Whether the dialect engine is installed, asked before it is needed.

sqlglot backs every guard that parses SQL, and it ships with the connector extras
rather than in the base install: a deployment that only queries a hosted semantic
layer over HTTP never sees a statement, so it should not have to carry a SQL
parser to get one metric. That makes "this install cannot guard SQL" a real state,
and a guarded command has to be able to say so cleanly instead of dying on an
import from a frame the caller did not write.

This module must stay importable with sqlglot absent, which is the entire reason
it is separate from the guards that use it.
"""

from __future__ import annotations


class DialectDependencyError(Exception):
    """The dialect engine (sqlglot, carried by every connector extra) is missing.

    Surfaced as a clean error envelope naming the install to fix, never as a
    fallback to a weaker check: a query dex cannot parse is a query dex cannot
    promise is read-only, so the only safe answer is to refuse.
    """


def ensure_available() -> None:
    """Raise :class:`DialectDependencyError` if the dialect engine is missing.

    Call this before importing the modules that parse SQL, so the failure names
    the fix. Mirrors :func:`exmergo_dex_core.explore.cluster.ensure_available` for
    scikit-learn, and like it runs before any connection is opened or any query is
    priced, so a missing extra costs nothing: no warehouse round-trip, no spend.
    """

    try:
        import sqlglot  # noqa: F401  (imported for availability, not for use)
    except ImportError as exc:
        raise DialectDependencyError(
            "this command validates SQL before it runs, which needs sqlglot. It "
            "ships with every connector extra, so reinstall the engine with the "
            "connector you use: exmergo-dex-core[duckdb] for the local on-ramp, or "
            "[snowflake], [bigquery], [databricks], [postgres], [redshift]. Only "
            "the hosted semantic layer (`explore semantic --api`, the "
            "[semantic-api] extra) runs without it, because dbt Cloud renders and "
            "executes that SQL rather than dex."
        ) from exc
