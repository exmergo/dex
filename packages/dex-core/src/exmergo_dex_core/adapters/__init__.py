"""Connector adapters: DuckDB, BigQuery, Snowflake, Databricks, Postgres,
Redshift, and ClickHouse.

``get_adapter`` is the single entry point so callers never import a connector
client directly; the client libraries stay behind their extras and are imported
only when their adapter is constructed. ``get_dialect``, ``is_free_connector``
and ``parse_relation`` answer from name-keyed tables here without constructing
anything, because their callers need an answer before a connection exists (the
query firewall needs a dialect; a project format needs to know whether an
authored string is a relation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    "clickhouse": "clickhouse",
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
    if connector == "clickhouse":
        from .clickhouse import ClickHouseAdapter

        return ClickHouseAdapter(**kwargs)
    raise ValueError(f"unknown connector '{connector}'")


def get_dialect(connector: str) -> str:
    """The SQLGlot dialect for ``connector``.

    Raises rather than defaulting to DuckDB on an unrecognized name: a silent
    fallback here means every subsequent SQL parse silently reads the wrong
    dialect (a hyphenated BigQuery project id parses as subtraction, for
    example), producing a confusing parse-error refusal that never mentions
    the real cause. ``get_adapter`` already raises on the same condition; this
    matches it instead of being the one caller that stays lenient.
    """

    try:
        return _DIALECTS[connector]
    except KeyError:
        raise ValueError(f"unknown connector '{connector}'") from None


# Connectors that bill nothing for a scan. Kept beside the dialect map and for
# the same reason: a command deciding whether an optional scan is free enough to
# run unasked must not have to construct an adapter (and open a connection) to
# find out. An unknown name is treated as billed, so the cautious answer is the
# default one.
_FREE_CONNECTORS = frozenset({"duckdb"})


def is_free_connector(connector: str) -> bool:
    """Whether ``connector`` bills nothing, resolved without constructing anything."""

    return connector in _FREE_CONNECTORS


# --- Relation identifiers --------------------------------------------------
#
# What counts as *one relation* differs per connector, and a caller that has to
# decide without opening a connection needs the rule rather than the adapter.
# The one caller today is a project format reading an authored source string
# (Apache Ossie documents a dataset's source as "database.schema.table **or
# query**" with no portable discriminator), and the answer decides whether a
# declaration reaches the physical map at all: accept a query as a relation and
# a SQL string becomes false physical evidence at the PII gate.
#
# Kept here, beside `_DIALECTS`, and for the same stated reason: resolving this
# must never import a client library. It is also deliberately not a member on
# `Adapter`, which is a runtime_checkable Protocol -- a new member there demotes
# every host-supplied adapter that has not grown it, to state a requirement that
# is about a name rather than about a connection.
#
# It is NOT the adapters' `_split`. That one splits an identifier dex already
# resolved from the warehouse's own catalog, so it checks arity and nothing
# else; handed `select x from a.b.c` it returns three parts and reports success.
# Here the string is authored by a human in a file, so the check has to be an
# acceptance test rather than a split.


@dataclass(frozen=True)
class _RelationRule:
    """One connector's rules for an unquoted, fully qualified relation name."""

    #: How many dot-separated parts a fully qualified name has.
    parts: int
    #: How this connector folds an unquoted identifier when it resolves it.
    #: `None` preserves the authored spelling.
    fold: str | None
    #: Parts that accept a hyphen, by index. BigQuery project ids routinely
    #: carry one and dex's own inventory reports them that way, so rejecting a
    #: hyphen would leave every hyphenated BigQuery project unlinkable. It is
    #: per-part rather than per-connector because a hyphen is legal in the
    #: project id and not in the dataset or table beneath it.
    hyphenated_parts: frozenset[int] = frozenset()


# ClickHouse is two parts because it has no catalog level: dbt-clickhouse's
# `schema:` *is* the ClickHouse database, and the adapter's own `_split`
# documents at length why dex does not synthesize a third component.
#
# Folding is what the warehouse does to an unquoted identifier, and it is why a
# quoted one is rejected outright below rather than unwrapped: an unquoted
# `Orders` and a quoted `"Orders"` name the same relation on DuckDB, different
# relations on Snowflake, and dex has no way to tell which the author meant.
_RELATION_RULES: dict[str, _RelationRule] = {
    "duckdb": _RelationRule(parts=3, fold=None),
    "bigquery": _RelationRule(parts=3, fold=None, hyphenated_parts=frozenset({0})),
    "snowflake": _RelationRule(parts=3, fold="upper"),
    "databricks": _RelationRule(parts=3, fold="lower"),
    "postgres": _RelationRule(parts=3, fold="lower"),
    "redshift": _RelationRule(parts=3, fold="lower"),
    "clickhouse": _RelationRule(parts=2, fold=None),
}

# An unquoted identifier part, anchored. Deliberately narrow: a leading letter or
# underscore then word characters, which is the intersection every connector
# accepts unquoted. A part needing anything else needs quoting, and a quoted part
# is not accepted here at all.
_RELATION_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# The same, plus the hyphen a BigQuery project id may carry. Still anchored, so
# it admits a hyphen inside a name and never a leading or trailing one, and never
# the ` - ` of a subtraction.
_HYPHENATED_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*[A-Za-z0-9_$]|[A-Za-z_]")


def parse_relation(connector: str, identifier: str) -> tuple[str, ...] | None:
    """``identifier``'s parts if it is one relation on ``connector``, else None.

    ``None`` is the answer for anything that is not exactly one fully qualified,
    unquoted relation name: a query, an expression, a quoted or backtick-quoted
    part, whitespace anywhere, an empty part, or the wrong number of parts for
    this connector. It is a *conservative* acceptor, and every rejection it makes
    is a physical link a caller then does not claim.

    A partially qualified name is rejected too. dex identifies relations fully
    qualified everywhere else, so a bare `orders` cannot be resolved here without
    inventing the two parts in front of it.
    """

    rule = _RELATION_RULES.get(connector)
    if rule is None:
        raise ValueError(f"unknown connector '{connector}'")
    if not isinstance(identifier, str):
        return None
    parts = identifier.split(".")
    if len(parts) != rule.parts:
        return None
    for index, part in enumerate(parts):
        pattern = _HYPHENATED_PART if index in rule.hyphenated_parts else _RELATION_PART
        if not pattern.fullmatch(part):
            return None
    return tuple(parts)


def normalize_relation(connector: str, identifier: str) -> str | None:
    """``identifier`` spelled the way ``connector`` resolves it, or None.

    The companion to :func:`parse_relation`, applying the connector's folding so
    an authored `main.Orders` matches the `main.orders` the cache and
    ``explore inventory`` carry. ``None`` for anything the acceptor rejects, so
    one call answers both "is this a relation" and "what is it called".
    """

    parts = parse_relation(connector, identifier)
    if parts is None:
        return None
    rule = _RELATION_RULES[connector]
    if rule.fold == "upper":
        parts = tuple(part.upper() for part in parts)
    elif rule.fold == "lower":
        parts = tuple(part.lower() for part in parts)
    return ".".join(parts)
