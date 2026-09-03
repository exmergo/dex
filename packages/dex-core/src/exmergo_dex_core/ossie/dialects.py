"""The Ossie dialect enum, partitioned once.

Ossie's `Dialect` enum mixes SQL dialects with expression languages that are not
SQL at all, and the distinction is load-bearing here in a way it is not for a
format that only stores expressions. dex *reads* an expression twice: to decide
whether a field resolves to a physical column, and to check that it parses. Both
readings are meaningless against MDX, and worse than meaningless against MAQL,
where a bare token that looks like a column identifier means something else
entirely.

So the partition lives in one module, shared by the validator and the catalog.
Two copies of it would eventually disagree, and the way they would disagree is
that one of them claims a physical column from a language dex cannot read.

Upstream's own `validation/validate.py` maintains the same partition, with the
same three non-SQL members. That is deliberate: dex ports upstream's judgment
about what a valid document is rather than forming its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = [
    "NON_SQL_DIALECTS",
    "PORTABLE_DIALECT",
    "SQL_DIALECTS",
    "connector_dialect",
    "select_expression",
    "sqlglot_dialect",
]

#: Ossie dialects dex may parse as SQL, and so may read a column out of.
SQL_DIALECTS: tuple[str, ...] = ("ANSI_SQL", "SNOWFLAKE", "DATABRICKS", "BIGQUERY")

#: Ossie dialects that are not SQL. Preserved verbatim, never parsed, never a
#: source of a physical column claim, and never reported as validated.
NON_SQL_DIALECTS: tuple[str, ...] = ("MDX", "TABLEAU", "MAQL")

#: The dialect an expression falls back to when nothing more specific applies.
PORTABLE_DIALECT = "ANSI_SQL"

# The Ossie dialect token a dex connector corresponds to. Four of dex's seven
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

    ``None`` means sqlglot's default, which is what `ANSI_SQL` parses as. Only
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
    2. the portable dialect;
    3. the first declared SQL dialect, in the document's own order.

    It never falls through to a non-SQL language. A fallback that could select
    MDX would hand a physical-column reader a string it cannot read, and the
    reader would either fail or, worse, succeed on a token that looks like an
    identifier and means something else.

    Step 2 is a list rather than a single name on purpose. The expression-language
    document is at Proposed Final and proposes a new default dialect, so when that
    lands it is prepended to ``_PORTABLE_CHAIN`` and nothing else here moves.
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


# The portable step of the selection chain, ordered most to least preferred. One
# member today; the slot ahead of it is reserved for the portable dialect the
# expression-language document proposes.
_PORTABLE_CHAIN: tuple[str, ...] = (PORTABLE_DIALECT,)


def _declared(expression: object) -> dict[str, str]:
    """Every ``dialect -> expression`` pair, in the document's own order.

    Shape-guarded rather than trusting, because the catalog reads documents the
    validator has already judged *and* the tier-1 declarations channel may not
    raise, so this has to survive being handed something malformed.
    """

    if not isinstance(expression, dict):
        return {}
    dialects = expression.get("dialects")
    if not isinstance(dialects, Sequence) or isinstance(dialects, str | bytes):
        return {}
    found: dict[str, str] = {}
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
