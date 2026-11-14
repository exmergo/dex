"""What a Semantic View's expressions read: the one module that imports sqlglot.

Every expression in a Semantic View is Snowflake SQL, so it is parsed in the
``snowflake`` dialect. What comes out is names as written; :mod:`.loader` and
:mod:`.catalog` resolve them against the view.

How an expression addresses what it reads, from the specification and the
validation rules:

- a bare name reads its own logical table: a member of that name where there is
  one (other than the member being defined), else a column of the base table;
- ``<table>.<name>`` reads the named logical table: on its own table a member or
  a column, on another table a member only (a base column of another table is
  refused by Snowflake as an invalid identifier);
- a derived metric reads table metrics as ``<table>.<metric>``, and other
  derived metrics by their bare name.

Nothing here judges: it reports what an expression reads, how deeply each read
is aggregated, and whether it is a window function, or ``None`` where the
expression cannot be parsed. The caller decides what that means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

__all__ = [
    "AGGREGATIONS",
    "DIALECT",
    "Aggregate",
    "Read",
    "aggregate",
    "aggregates",
    "definition_reads",
    "definition_table",
    "over_columns",
    "plain",
    "ratio_sides",
    "reads",
    "window",
]

DIALECT = "snowflake"

_OVER = re.compile(r"\bover\s*\(", re.IGNORECASE)

#: The catalog's aggregation vocabulary, per sqlglot node.
AGGREGATIONS: dict[type, str] = {
    exp.Sum: "sum",
    exp.Count: "count",
    exp.Avg: "average",
    exp.Min: "min",
    exp.Max: "max",
    exp.Median: "median",
    exp.ApproxDistinct: "count_distinct_approx",
}


@dataclass(frozen=True)
class Read:
    """One name an expression reads: its qualifier as written (``None`` for a
    bare name), the name, and how many aggregate functions enclose it."""

    qualifier: str | None
    name: str
    depth: int
    #: Whether the name was written quoted, which keeps its spelling.
    quoted: bool = False


@dataclass(frozen=True)
class Aggregate:
    """An expression that is one aggregate over one name: its aggregation and
    the name it reads, where the argument is a bare name."""

    agg: str
    read: Read | None


def _parse(sql: str) -> exp.Expression | None:
    """``sql`` parsed in the Snowflake dialect, or ``None`` where it does not parse."""
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except (ParseError, TokenError, ValueError):
        return None


def _unwrap(tree: exp.Expression | None) -> exp.Expression | None:
    """``tree`` without enclosing parentheses."""
    while isinstance(tree, exp.Paren):
        tree = tree.this
    return tree


def _read(node: exp.Column) -> Read | None:
    """A column node as a :class:`Read`, or ``None`` where it has more than a
    table qualifier (a three-part name is no member reference)."""

    if node.args.get("db") or node.args.get("catalog"):
        return None
    depth = 0
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.AggFunc):
            depth += 1
        parent = parent.parent
    identifier = node.this
    quoted = bool(getattr(identifier, "quoted", False))
    return Read(
        qualifier=node.table or None, name=node.name, depth=depth, quoted=quoted
    )


def reads(sql: str) -> list[Read] | None:
    """Every name ``sql`` reads, or ``None`` where it does not parse or reads a
    name no Semantic View reference takes (a three-part name): such a read is
    unresolved, never dropped, so no rule passes for want of a read."""

    tree = _parse(sql)
    if tree is None:
        return None
    found: list[Read] = []
    for node in tree.find_all(exp.Column):
        if isinstance(node.this, exp.Star):
            continue
        read = _read(node)
        if read is None:
            return None
        found.append(read)
    return found


def aggregates(sql: str) -> bool | None:
    """Whether ``sql`` contains an aggregate function, or ``None`` where it does
    not parse."""
    tree = _parse(sql)
    if tree is None:
        return None
    return any(isinstance(node, exp.AggFunc) for node in tree.walk())


def window(sql: str) -> bool:
    """Whether ``sql`` is a window function. Read from the parse where there is
    one, and from an ``OVER (`` otherwise, so an expression sqlglot cannot read
    is still not treated as an ordinary aggregate."""

    tree = _parse(sql)
    if tree is None:
        return bool(_OVER.search(sql))
    return tree.find(exp.Window) is not None


def plain(sql: str) -> Read | None:
    """``sql`` as one name and nothing else, or ``None``."""

    tree = _unwrap(_parse(sql))
    if isinstance(tree, exp.Column) and not isinstance(tree.this, exp.Star):
        return _read(tree)
    return None


def ratio_sides(sql: str) -> tuple[Read, Read] | None:
    """``a / b`` with each side one name and nothing else, or ``None``."""

    tree = _unwrap(_parse(sql))
    if not isinstance(tree, exp.Div):
        return None
    sides = [_unwrap(tree.this), _unwrap(tree.expression)]
    if not all(isinstance(s, exp.Column) for s in sides):
        return None
    reads = [_read(s) for s in sides]  # type: ignore[arg-type]
    if None in reads:
        return None
    return reads[0], reads[1]  # type: ignore[return-value]


def aggregate(sql: str) -> Aggregate | None:
    """``sql`` as one aggregate over one argument, or ``None``.

    ``COUNT(DISTINCT x)`` is ``count_distinct``. The name is carried only where
    the argument is a bare name: a conditional sum reads several, and naming one
    would be a guess.
    """

    tree = _unwrap(_parse(sql))
    kind = AGGREGATIONS.get(type(tree)) if tree is not None else None
    if kind is None:
        return None
    argument = tree.this
    if tree.args.get("expressions"):
        # `COUNT(a, b)` reads more than one column, so it names none.
        return Aggregate(agg=kind, read=None)
    if isinstance(tree, exp.Count) and isinstance(argument, exp.Distinct):
        kind = "count_distinct"
        argument = argument.expressions[0] if len(argument.expressions) == 1 else None
    argument = _unwrap(argument)
    if isinstance(argument, exp.Column) and not isinstance(argument.this, exp.Star):
        return Aggregate(agg=kind, read=_read(argument))
    return Aggregate(agg=kind, read=None)


def over_columns(sql: str, own: str, columns: dict[str, str]) -> str | None:
    """``sql`` rewritten over its table's base columns: every name it reads
    (bare or qualified by ``own``) replaced by ``columns[name.lower()]``, or
    ``None`` where it reads another qualifier, a name ``columns`` does not map,
    or does not parse.

    What the PII gate parses to screen every column a computed dimension reads,
    against the one relation they all sit on, members followed to the columns
    behind them.
    """

    tree = _parse(sql)
    if tree is None:
        return None
    for node in tree.find_all(exp.Column):
        if node.table and node.table.lower() != own.lower():
            return None
        if node.name.lower() not in columns:
            return None

    def rewrite(node: exp.Expression) -> exp.Expression:
        """A column as its base column, unqualified; any other node as it is."""
        if isinstance(node, exp.Column):
            return exp.column(columns[node.name.lower()])
        return node

    return tree.transform(rewrite).sql(dialect=DIALECT)


def definition_table(sql: str) -> tuple[str | None, str | None]:
    """A ``base_table.definition`` query as the one table it projects, or
    ``(None, reason)``.

    Only a plain projection of one table stands for that table: no filter,
    join, grouping, ``DISTINCT``, ``LIMIT``, ``WITH``, aggregate or window (any
    of which holds other rows than the table does), and every output a column of
    the table under its own name (a renamed or computed output is not that
    table's column).
    """

    tree = _unwrap(_parse(sql))
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        return None, "its `definition` could not be read as one SELECT"
    source = tree.args.get("from_") or tree.args.get("from")
    single = source.this if source is not None else None
    reasons = [
        what
        for what, present in (
            ("a join", bool(tree.args.get("joins"))),
            ("a WHERE", tree.args.get("where") is not None),
            ("a GROUP BY", tree.args.get("group") is not None),
            ("a HAVING", tree.args.get("having") is not None),
            ("a QUALIFY", tree.args.get("qualify") is not None),
            ("DISTINCT", tree.args.get("distinct") is not None),
            ("a LIMIT", tree.args.get("limit") is not None),
            (
                "a WITH clause",
                tree.args.get("with_") is not None or tree.args.get("with") is not None,
            ),
        )
        if present
    ]
    if not isinstance(single, exp.Table):
        reasons.append("no single table in FROM")
    if any(
        isinstance(node, (exp.AggFunc, exp.Window))
        for projection in tree.expressions
        for node in projection.walk()
    ):
        reasons.append("an aggregate or window in its select list")
    for projection in tree.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star):
            continue
        if not isinstance(inner, exp.Column) or (
            projection.alias_or_name.lower() != inner.name.lower()
        ):
            reasons.append(
                "a column it renames or computes, which a member would read as "
                "a column of the table"
            )
            break
    if reasons:
        return None, (
            f"its `definition` query has {', '.join(reasons)}, so no one table "
            "stands behind it"
        )
    return ".".join(part.name for part in single.parts), None


def definition_reads(sql: str) -> tuple[str, list[str]] | None:
    """The one table a ``base_table.definition`` query reads, and every column
    of it the query names, or ``None`` where that cannot be said (#1293).

    Any shape of query over one table qualifies, a filter or a grouping
    included, since every column it names is a column of that table. ``None``
    for a query that does not parse, reads more than one table (a join, a
    subquery, a ``WITH`` clause), or names a column through a qualifier other
    than that table's. A name in ``ORDER BY``, ``GROUP BY`` or ``HAVING`` that
    is also an output alias is left out, since Snowflake may read it as the
    alias.
    """

    tree = _unwrap(_parse(sql))
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        return None
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or any(True for _ in tree.find_all(exp.Subquery, exp.CTE)):
        return None
    (table,) = tables
    names = {part.name.lower() for part in table.parts[-1:]}
    if table.alias:
        names.add(table.alias.lower())
    aliases = {
        projection.alias.lower()
        for projection in tree.expressions
        if isinstance(projection, exp.Alias)
    }
    found: list[str] = []
    for node in tree.find_all(exp.Column):
        if isinstance(node.this, exp.Star):
            continue
        if node.args.get("db") or node.args.get("catalog"):
            return None
        if node.table and node.table.lower() not in names:
            return None
        if not node.table and node.name.lower() in aliases:
            clause = node.find_ancestor(exp.Order, exp.Group, exp.Having)
            if clause is not None:
                continue
        if node.name.lower() not in {f.lower() for f in found}:
            found.append(node.name)
    return ".".join(part.name for part in table.parts), found
