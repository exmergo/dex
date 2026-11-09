"""Column names a warehouse's SQL reserves, which dbt must quote (issue #651).

A source column named ``window`` profiles and plans cleanly on DuckDB, then the
first build fails inside a generic test, because dbt writes the column bare and
``window`` is reserved there. ``quote: true`` on the column is the fix, so
``explore`` notes such columns and ``transform plan`` warns on a test that
needs it.

The word list is sqlglot's per-dialect ``Generator.RESERVED_KEYWORDS``, the set
sqlglot itself quotes an identifier on, not the tokenizer's keywords, which also
hold non-reserved words like ``date`` and would flag ordinary columns. sqlglot
keeps no list for Postgres, Snowflake, Databricks or ClickHouse, so on those
nothing is flagged.
"""

from __future__ import annotations

from collections.abc import Iterable


def reserved_column_names(dialect: str, names: Iterable[str]) -> list[str]:
    """The names in ``names`` that ``dialect`` reserves, in their given order."""

    # Imported here: plan-time callers reach this only on a dbt project, and
    # the base install carries no sqlglot (see `guards.dialect`).
    from sqlglot.dialects.dialect import Dialect

    reserved = Dialect.get_or_raise(dialect).generator_class.RESERVED_KEYWORDS
    return [name for name in names if name.lower() in reserved]
