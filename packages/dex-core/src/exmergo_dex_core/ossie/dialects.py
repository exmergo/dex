"""Ossie dialects, partitioned once.

Ossie's `Dialect` is a free-form string: any non-empty token is valid, and
`OSSIE_SQL_2026` is the one dialect the specification defines. The tokens mix
SQL dialects with expression languages that are not SQL at all, and the
distinction is load-bearing here in a way it is not for a format that only
stores expressions. dex *reads* an expression twice: to decide
whether a field resolves to a physical column, and to check that it parses. Both
readings are meaningless against MDX, and worse than meaningless against MAQL,
where a bare token that looks like a column identifier means something else
entirely.

So the partition lives in one module, shared by the validator and the catalog.
Two copies of it would eventually disagree, and the way they would disagree is
that one of them claims a physical column from a language dex cannot read.

Upstream's own `validation/validate.py` maintains the same partition: the SQL
dialects sqlglot parses, the registered non-SQL languages, and everything else,
which is opaque. That is deliberate: dex ports upstream's judgment about what a
valid document is rather than forming its own. dex reads only the SQL members;
any other token, registered or not, is preserved verbatim and never read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = [
    "NON_SQL_DIALECTS",
    "OSSIE_SQL",
    "PORTABLE_DIALECT",
    "SQL_DIALECTS",
    "connector_dialect",
    "declared_expressions",
    "select_expression",
    "sqlglot_dialect",
]

#: The one dialect the Ossie specification defines: its portable SQL expression
#: language. An expression's top-level `sql` is written in it.
OSSIE_SQL = "OSSIE_SQL_2026"

#: Ossie dialects dex may parse as SQL, and so may read a column out of. Every
#: other token is opaque: preserved verbatim, never parsed, never a source of a
#: physical column claim, and never reported as validated.
SQL_DIALECTS: tuple[str, ...] = (
    OSSIE_SQL,
    "ANSI_SQL",
    "SNOWFLAKE",
    "DATABRICKS",
    "BIGQUERY",
)

#: The non-SQL languages upstream's dialect registry (`extensions/dialects.md`)
#: names. Listed so a note can call them by what they are; an unregistered token
#: is treated exactly the same way, because `Dialect` is an open string.
NON_SQL_DIALECTS: tuple[str, ...] = (
    "MDX",
    "TABLEAU",
    "MAQL",
    "SIGMA",
    "THOUGHTSPOT",
    "DAX",
    "HOLISTICS_AQL",
)

#: The portable dialect ahead of which nothing but the connector's own ranks.
PORTABLE_DIALECT = OSSIE_SQL

# The Ossie dialect token a dex connector corresponds to. Five of dex's eight
# connectors have no token in Ossie's enum, and they are absent here rather than
# mapped to something close: DuckDB is not Databricks, and a wrong dialect is a
# worse answer than the portable one. They fall to PORTABLE_DIALECT, which is
# correct and is documented rather than left to be inferred.
_CONNECTOR_DIALECTS: Mapping[str, str] = {
    "snowflake": "SNOWFLAKE",
    "databricks": "DATABRICKS",
    "bigquery": "BIGQUERY",
}

# Ossie dialect -> sqlglot dialect, matching upstream's own map. `None` is
# sqlglot's dialect-agnostic default rather than "skip": ANSI_SQL *is* parsed.
_SQLGLOT_DIALECTS: Mapping[str, str | None] = {
    OSSIE_SQL: None,
    "ANSI_SQL": None,
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "BIGQUERY": "bigquery",
}


def connector_dialect(connector: str | None) -> str | None:
    """The Ossie dialect token for a dex connector, or ``None``."""

    return _CONNECTOR_DIALECTS.get((connector or "").strip().lower())


def sqlglot_dialect(dialect: str) -> str | None:
    """The sqlglot dialect for an Ossie SQL dialect.

    ``None`` means sqlglot's default, which is what `OSSIE_SQL_2026` and
    `ANSI_SQL` parse as, as upstream's validator parses them. Only
    call this for a member of :data:`SQL_DIALECTS`; a non-SQL language has no
    answer here and asking for one is the bug this module exists to prevent.
    """

    return _SQLGLOT_DIALECTS[dialect]


def select_expression(
    expression: object, connector: str | None = None
) -> tuple[str | None, str | None, dict[str, str]]:
    """The expression dex reads, the dialect it came from, and every declared one.

    Returns ``(expression, dialect, declared)``. ``declared`` is every dialect
    the document states, SQL and not, verbatim and unread: nothing is dropped,
    because the document is the source of truth and dex is one consumer of it.
    The first two are ``None`` when no SQL dialect is declared at all, which is a
    complete answer for a field expressed only in MDX.

    **Selection is deterministic given a document and a connector**, which
    matters because the choice decides which column a field links to and a
    catalog that varies between runs cannot be cached or compared. The chain:

    1. the active connector's own Ossie dialect, where it has one;
    2. `OSSIE_SQL_2026`, from the top-level `sql` first, since upstream makes
       it authoritative over an `OSSIE_SQL_2026` dialect entry;
    3. `ANSI_SQL`;
    4. the first other declared SQL dialect, in the document's own order.

    It never falls through to a non-SQL language. A fallback that could select
    MDX would hand a physical-column reader a string it cannot read, and the
    reader would either fail or, worse, succeed on a token that looks like an
    identifier and means something else.

    ``declared`` carries the top-level `sql` under `OSSIE_SQL_2026`, so a
    document that states only `sql:` reads exactly as one stating an
    `OSSIE_SQL_2026` dialect entry.
    """

    declared = _declared(expression)
    preferred = connector_dialect(connector)
    chain = [
        *([preferred] if preferred else []),
        *_PORTABLE_CHAIN,
        *(name for name in declared if name in SQL_DIALECTS),
    ]
    for name in chain:
        if name in declared and name in SQL_DIALECTS:
            return declared[name], name, declared
    return None, None, declared


# The portable steps of the selection chain, ordered most to least preferred.
_PORTABLE_CHAIN: tuple[str, ...] = (OSSIE_SQL, "ANSI_SQL")


def declared_expressions(expression: object) -> dict[str, str]:
    """Every ``dialect -> expression`` pair an expression object states.

    The top-level `sql` comes first, as `OSSIE_SQL_2026`, and outranks an
    `OSSIE_SQL_2026` entry in `dialects`; the rest follow in the document's own
    order. Shape-guarded rather than trusting, because the catalog reads
    documents the validator has already judged *and* the declarations channel
    may not raise, so this has to survive being handed something malformed.
    """

    return _declared(expression)


def _declared(expression: object) -> dict[str, str]:
    if not isinstance(expression, dict):
        return {}
    found: dict[str, str] = {}
    sql = expression.get("sql")
    if isinstance(sql, str) and sql:
        found[OSSIE_SQL] = sql
    dialects = expression.get("dialects")
    if not isinstance(dialects, Sequence) or isinstance(dialects, str | bytes):
        return found
    for entry in dialects:
        if not isinstance(entry, dict):
            continue
        name = entry.get("dialect")
        text = entry.get("expression")
        # A repeated dialect keeps the first, matching "the document's own
        # order" everywhere else here.
        if isinstance(name, str) and isinstance(text, str) and name not in found:
            found[name] = text
    return found
