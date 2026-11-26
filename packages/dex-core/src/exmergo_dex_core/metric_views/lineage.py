"""What a Metric View's expressions read: the one module that imports sqlglot.

Every expression in a Metric View is Databricks SQL whatever warehouse dex is
connected to, so it is parsed in the ``databricks`` dialect. What comes out is
names as written; :mod:`.catalog` folds them to the connector.

How a column is addressed, from the specification:

- in a field or a measure, ``source.<col>`` or a bare ``<col>`` reads the view's
  source, ``<join>.<col>`` reads a join, and a nested join is read through its
  whole path (``customer.nation.n_name``);
- in a join's ``on``, ``source.`` is the view's source, the join's own name (or
  an unqualified column) is the joined table, and a nested join names its parent
  by the parent's name;
- a measure reads another measure as ``MEASURE(<name>)``, with a name that is not
  an identifier in backticks.

Nothing here judges: it reports what an expression reads, or ``None`` where it
cannot be parsed, and the caller decides what that means.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .validator import AGGREGATE_FUNCTIONS

__all__ = [
    "Aggregate",
    "ColumnRef",
    "SourceRelation",
    "aggregate",
    "canonical",
    "column_refs",
    "filter_predicate",
    "is_query",
    "join_pairs",
    "measure_refs",
    "over_own_columns",
    "plain_column",
    "query_columns",
    "ratio_sides",
    "source_relation",
    "source_tables",
    "value_reads",
]

DIALECT = "databricks"

_QUERY = re.compile(r"^\s*\(?\s*(select|with)\b", re.IGNORECASE)
_NAME_PART = r"(?:`[^`]+`|[A-Za-z_][\w$-]*)"
_TABLE_NAME = re.compile(rf"^\s*{_NAME_PART}(?:\s*\.\s*{_NAME_PART}){{0,2}}\s*$")


@dataclass(frozen=True)
class ColumnRef:
    """One column an expression reads: the qualifier as written (empty for a bare
    column) and the column's name."""

    qualifier: tuple[str, ...]
    column: str


@dataclass(frozen=True)
class Aggregate:
    """A measure that is one aggregate: its aggregation, the column it reads
    where that is a bare column, and a ``FILTER (WHERE ...)`` predicate."""

    agg: str
    column: ColumnRef | None
    filter: str | None


@dataclass(frozen=True)
class SourceRelation:
    """What a view's or a join's ``source`` resolves to.

    ``relation`` is the table the catalog may claim, as written. ``gate`` is the
    table the PII gate may screen through even where the catalog claims none (a
    filtered projection reads that table's columns). ``columns`` maps an output
    name of a projection query to the column of the table it passes through.
    """

    relation: str | None
    gate: str | None
    columns: dict[str, str] | None = None
    note: str | None = None


def _parse(sql: str) -> exp.Expression | None:
    """``sql`` parsed in the Databricks dialect, or ``None`` where it does not parse."""
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except (ParseError, TokenError, ValueError):
        return None


def canonical(sql: str | None) -> str | None:
    """An expression as parsed Databricks SQL with unquoted identifiers folded,
    or its whitespace-collapsed, lower-cased text where it does not parse: the
    form two spellings of one expression share, so whitespace, keyword case and
    an unquoted name's case are never a difference (#1363)."""

    if sql is None:
        return None
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

    tree = _parse(sql)
    if tree is None:
        return " ".join(sql.split()).lower()
    return normalize_identifiers(tree, dialect=DIALECT).sql(dialect=DIALECT)


def _parts(node: exp.Expression) -> list[str] | None:
    """A column reference, through any ``Dot`` chain, as its parts."""

    if isinstance(node, exp.Column):
        return [part.name for part in node.parts]
    if isinstance(node, exp.Dot):
        inner = _parts(node.this)
        if inner is None or not isinstance(node.expression, exp.Identifier):
            return None
        return [*inner, node.expression.name]
    return None


def _is_measure_call(node: exp.Expression) -> bool:
    """Whether ``node`` is a ``MEASURE(...)`` call."""
    return isinstance(node, exp.Anonymous) and str(node.this).upper() == "MEASURE"


def column_refs(sql: str) -> list[ColumnRef] | None:
    """Every column ``sql`` reads, outside ``MEASURE()`` calls, or ``None``."""

    tree = _parse(sql)
    if tree is None:
        return None
    found: list[ColumnRef] = []

    def walk(node: exp.Expression) -> None:
        """Collect the columns under ``node``, skipping ``MEASURE()`` arguments."""
        if _is_measure_call(node):
            return
        parts = _parts(node)
        if parts is not None:
            found.append(ColumnRef(tuple(parts[:-1]), parts[-1]))
            return
        for child in node.iter_expressions():
            walk(child)

    walk(tree)
    return found


def plain_column(sql: str) -> ColumnRef | None:
    """``sql`` as one column and nothing else, or ``None``."""

    tree = _parse(sql)
    while isinstance(tree, exp.Paren):
        tree = tree.this
    parts = _parts(tree) if tree is not None else None
    return ColumnRef(tuple(parts[:-1]), parts[-1]) if parts else None


def measure_refs(sql: str) -> list[str] | None:
    """The measures ``sql`` reads through ``MEASURE()``, as written, or ``None``."""

    tree = _parse(sql)
    if tree is None:
        return None
    names: list[str] = []
    for node in tree.walk():
        if _is_measure_call(node):
            args = node.expressions
            parts = _parts(args[0]) if len(args) == 1 else None
            if parts is None or len(parts) != 1:
                return None
            names.append(parts[0])
    return names


def ratio_sides(sql: str) -> tuple[str, str] | None:
    """``MEASURE(a) / MEASURE(b)``, exactly, as its two sides."""

    tree = _parse(sql)
    while isinstance(tree, exp.Paren):
        tree = tree.this
    if not isinstance(tree, exp.Div):
        return None
    sides: list[str] = []
    for side in (tree.this, tree.expression):
        while isinstance(side, exp.Paren):
            side = side.this
        if not _is_measure_call(side) or len(side.expressions) != 1:
            return None
        parts = _parts(side.expressions[0])
        if not parts or len(parts) != 1:
            return None
        sides.append(parts[0])
    return sides[0], sides[1]


_AGG_NODES: dict[type, str] = {
    exp.Sum: "sum",
    exp.Count: "count",
    exp.Avg: "avg",
    exp.Min: "min",
    exp.Max: "max",
    exp.Median: "median",
    exp.ApproxDistinct: "approx_count_distinct",
}


def aggregate(sql: str) -> Aggregate | None:
    """``sql`` as one aggregate over one argument, or ``None``.

    ``COUNT(DISTINCT x)`` is ``count_distinct``; a ``FILTER (WHERE ...)`` is
    carried as the predicate. The column is named only where the argument is a
    bare column: a conditional sum reads several, and naming one would be a
    guess.
    """

    tree = _parse(sql)
    while isinstance(tree, exp.Paren):
        tree = tree.this
    predicate: str | None = None
    if isinstance(tree, exp.Filter):
        where = tree.expression
        predicate = (
            where.this.sql(dialect=DIALECT) if isinstance(where, exp.Where) else None
        )
        tree = tree.this
    kind = _AGG_NODES.get(type(tree)) if tree is not None else None
    if kind is None:
        return None
    argument = tree.this
    agg = AGGREGATE_FUNCTIONS[kind]
    if isinstance(tree, exp.Count) and isinstance(argument, exp.Distinct):
        agg = "count_distinct"
        argument = argument.expressions[0] if len(argument.expressions) == 1 else None
    parts = _parts(argument) if argument is not None else None
    column = ColumnRef(tuple(parts[:-1]), parts[-1]) if parts else None
    return Aggregate(agg=agg, column=column, filter=predicate)


def is_query(source: str) -> bool:
    """Whether a ``source`` is a query rather than a table name."""

    return bool(_QUERY.match(source))


def query_columns(source: str) -> tuple[list[str], list[str]] | None:
    """The physical columns a query ``source`` reads, and the names whose
    reading is ambiguous, or None where it does not parse.

    A column in the select list or ``WHERE`` is a read of the table even when an
    alias shares its name (``old AS old``). An unqualified name in ``ORDER BY``
    that the select list names as an alias is that alias. In ``GROUP BY`` and
    ``HAVING`` Databricks resolves a table column before an alias, so a name
    that is both is a read only while the table has it: such a name is returned
    as ambiguous, for the caller to name as unchecked when it is missing rather
    than to call it dropped.
    """

    tree = _parse(source)
    if tree is None:
        return None
    select = tree
    while isinstance(select, exp.Subquery):
        select = select.this
    aliases = (
        {
            p.alias.lower()
            for p in select.expressions
            if isinstance(p, exp.Alias) and p.alias
        }
        if isinstance(select, exp.Select)
        else set()
    )
    found: list[str] = []
    ambiguous: list[str] = []
    for node in tree.find_all(exp.Column):
        name = node.name
        if node.table or name.lower() not in aliases:
            found.append(name)
        elif node.find_ancestor(exp.Group, exp.Having) is not None:
            ambiguous.append(name)
        elif node.find_ancestor(exp.Order) is None:
            found.append(name)
    return found, ambiguous


def _has_aggregate(select: exp.Select) -> bool:
    """Whether the select list aggregates its rows, outside window functions.

    A window function keeps the row count, so the windowed function itself
    does not count; neither does an aggregate inside a subquery, which
    aggregates that query's rows rather than this one's. But an aggregate
    nested inside a windowed function's arguments, its FILTER or WITHIN
    GROUP operands, or its window specification aggregates the rows before
    the window runs, so it does count.
    """

    pending = list(select.expressions)
    while pending:
        node = pending.pop()
        if isinstance(node, (exp.Subquery, exp.Select)):
            continue
        if isinstance(node, exp.Window):
            pending.extend(_window_parts(node))
            continue
        if isinstance(node, exp.AggFunc):
            return True
        pending.extend(node.iter_expressions())
    return False


def _window_parts(window: exp.Window) -> list[exp.Expression]:
    """The parts of a window function that may hide an aggregate.

    The windowed function itself is left out: it keeps the row count, so it
    does not count. What is checked is its arguments, its FILTER predicate
    and WITHIN GROUP operand, and its window specification (partition and
    order), where a nested aggregate would run before the window. Null
    treatment modifiers (IGNORE NULLS, RESPECT NULLS) wrap the windowed
    function and are unwrapped first, so the function itself is still left
    out rather than counted as an aggregate.
    """

    func = window.this
    parts: list[exp.Expression] = []
    while isinstance(
        func, (exp.Filter, exp.WithinGroup, exp.IgnoreNulls, exp.RespectNulls)
    ):
        if isinstance(func, (exp.Filter, exp.WithinGroup)) and (
            func.expression is not None
        ):
            parts.append(func.expression)
        func = func.this
    if func is not None:
        parts.extend(func.iter_expressions())
    parts.extend(
        child for child in window.iter_expressions() if child is not window.this
    )
    return parts


def source_relation(source: str) -> SourceRelation:
    """A ``source`` as the relation behind it.

    A table name (up to three parts) is that relation. A query is one only where
    it is a plain projection of one table: its columns are traced through, and a
    ``WHERE`` keeps the table for the PII gate and claims no relation, since the
    view then holds fewer rows than the table. Anything else (a join, an
    aggregate, a set operation) has no relation of its own.
    """

    if _TABLE_NAME.match(source) and not _QUERY.match(source):
        return SourceRelation(relation=source.strip(), gate=source.strip())
    if not _QUERY.match(source):
        return SourceRelation(
            None,
            None,
            note=f"source `{_short(source)}` is neither a table name nor a query",
        )
    tree = _parse(source)
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        return SourceRelation(
            None, None, note=f"source query `{_short(source)}` could not be read"
        )
    table = tree.args.get("from_") or tree.args.get("from")
    single = table.this if table is not None else None
    reasons = [
        name
        for name, present in (
            ("a join", bool(tree.args.get("joins"))),
            ("a GROUP BY", tree.args.get("group") is not None),
            ("an aggregate", _has_aggregate(tree)),
            ("a HAVING", tree.args.get("having") is not None),
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
    if reasons:
        return SourceRelation(
            None,
            None,
            note=(
                f"source query `{_short(source)}` has {', '.join(reasons)}, so no "
                "one table stands behind the view"
            ),
        )
    name = ".".join(part.name for part in single.parts)
    columns: dict[str, str] = {}
    for projection in tree.expressions:
        if isinstance(projection, exp.Star):
            continue
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        parts = _parts(inner)
        if parts:
            columns[projection.alias_or_name] = parts[-1]
    star = any(isinstance(p, exp.Star) for p in tree.expressions)
    filtered = tree.args.get("where") is not None
    if filtered:
        return SourceRelation(
            None,
            name,
            columns=None if star else columns,
            note=(
                f"source query `{_short(source)}` filters `{name}`, so the view "
                "holds fewer rows than that table and claims no relation; its "
                "columns are still screened as that table's"
            ),
        )
    return SourceRelation(relation=name, gate=name, columns=None if star else columns)


def join_pairs(
    on: str | None,
    using: tuple[str, ...] | None,
    join_name: str,
    join_path: tuple[str, ...],
    parent_names: tuple[tuple[str, ...], ...],
) -> tuple[list[tuple[str, str]] | None, str | None]:
    """A join's condition as ``(parent column, joined column)`` pairs, in order.

    ``parent_names`` are the qualifiers that name the parent side (``source``
    for a top-level join; the parent's name and its whole path for a nested
    one). Only a conjunction of equalities between one parent column and one
    joined column is a key join; anything else names no pairs, and says why.
    """

    if using:
        return [(column, column) for column in using], None
    tree = _parse(on or "")
    if tree is None:
        return None, f"join `{'.'.join(join_path)}`: its `on` could not be read"
    own = {(join_name.lower(),), tuple(p.lower() for p in join_path), ()}
    parents = {tuple(p.lower() for p in names) for names in parent_names}
    pairs: list[tuple[str, str]] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            pending.extend([node.expression, node.this])
            continue
        if not isinstance(node, exp.EQ):
            return None, (
                f"join `{'.'.join(join_path)}`: `{_short(on or '')}` is not only "
                "equalities between the two sides, so no key pairs are declared"
            )
        sides = [_parts(node.this), _parts(node.expression)]
        if not all(sides):
            return None, (
                f"join `{'.'.join(join_path)}`: `{node.sql(dialect=DIALECT)}` "
                "compares something other than two columns"
            )
        left, right = (
            (tuple(p.lower() for p in s[:-1]), s[-1])
            for s in sides  # type: ignore[index]
        )
        if left[0] in parents and right[0] in own:
            pairs.append((left[1], right[1]))
        elif right[0] in parents and left[0] in own:
            pairs.append((right[1], left[1]))
        else:
            return None, (
                f"join `{'.'.join(join_path)}`: `{node.sql(dialect=DIALECT)}` does "
                "not compare a column of the parent with one of the joined table"
            )
    return pairs, None


def _short(sql: str, limit: int = 60) -> str:
    """``sql`` on one line, cut to ``limit`` characters for a note."""
    text = " ".join(sql.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def over_own_columns(sql: str, owners: tuple[tuple[str, ...], ...]) -> str | None:
    """``sql`` with every column's qualifier removed, where each one is one of
    ``owners`` (the qualifiers naming one relation) or none; otherwise ``None``.

    What the PII gate parses to screen every column a computed field reads,
    against the one relation they all sit on.
    """

    tree = _parse(sql)
    if tree is None:
        return None
    allowed = {tuple(p.lower() for p in owner) for owner in owners} | {()}
    for node in tree.walk():
        if _is_measure_call(node) or isinstance(node, exp.Dot):
            return None
        if isinstance(node, exp.Column):
            qualifier = tuple(part.name.lower() for part in node.parts[:-1])
            if qualifier not in allowed:
                return None

    def strip(node: exp.Expression) -> exp.Expression:
        """A column without its qualifier; any other node as it is."""
        if isinstance(node, exp.Column) and node.table:
            return exp.column(node.this.copy())
        return node

    return tree.transform(strip).sql(dialect=DIALECT)


# --- a governed query (#1362) ------------------------------------------------------

#: The functions that return one of their input values (or a list or a sample of
#: them) rather than a number computed from them, as sqlglot names each in the
#: Databricks dialect: ``collect_list`` reads as ``array_agg``, ``max_by`` as
#: ``arg_max``, ``percentile_approx`` as ``approx_quantile``, ``string_agg`` and
#: ``listagg`` as ``group_concat``. A measure built on one returns a column's
#: values as surely as grouping by the column, so the PII gate screens what it
#: reads.
VALUE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "min",
        "max",
        "median",
        "mode",
        "any_value",
        "anyvalue",
        "first",
        "last",
        "first_value",
        "firstvalue",
        "last_value",
        "lastvalue",
        "array_agg",
        "arrayagg",
        "collect_list",
        "collect_set",
        "array_unique_agg",
        "arrayuniqueagg",
        "array_union_agg",
        "arrayunionagg",
        "arg_max",
        "argmax",
        "arg_min",
        "argmin",
        "max_by",
        "min_by",
        "quantile",
        "percentile",
        "percentile_cont",
        "percentilecont",
        "percentile_disc",
        "percentiledisc",
        "approx_quantile",
        "approxquantile",
        "percentile_approx",
        "approx_percentile",
        "approx_top_k",
        "approxtopk",
        "group_concat",
        "groupconcat",
        "string_agg",
        "listagg",
        "histogram_numeric",
    }
)


def value_reads(sql: str) -> list[ColumnRef] | None:
    """The columns ``sql`` reads inside a function that returns one of its input
    values (:data:`VALUE_FUNCTIONS`), or ``None`` where it does not parse.

    A ``MEASURE()`` call inside one reads another measure, which the caller walks
    on its own; its argument is not a column.
    """

    tree = _parse(sql)
    if tree is None:
        return None
    found: list[ColumnRef] = []
    for node in tree.walk():
        if not isinstance(node, exp.Func) or _is_measure_call(node):
            continue
        names = {(node.sql_name() or "").lower(), type(node).__name__.lower()}
        if isinstance(node, exp.Anonymous):
            # A function sqlglot has no node for keeps its own name.
            names.add(str(node.name or "").lower())
        if not names & VALUE_FUNCTIONS:
            continue
        refs = column_refs(node.sql(dialect=DIALECT))
        if refs is None:
            return None
        found.extend(refs)
    return list(dict.fromkeys(found))


#: What a ``--where`` predicate may not hold: a query, an aggregate (a measure's
#: work), a window, a placeholder or parameter bound after the gate, or a star.
_FILTER_FORBIDDEN = (
    exp.Select,
    exp.Subquery,
    exp.AggFunc,
    exp.Window,
    exp.Placeholder,
    exp.Parameter,
    exp.Star,
)


def filter_predicate(clause: str, resolve: Callable[[list[str]], str]) -> str | None:
    """A ``--where`` clause re-rendered over the view's field names, or ``None``
    where it cannot be read: it does not parse, is not a predicate, reads no
    column, or holds what :data:`_FILTER_FORBIDDEN` lists.

    ``resolve`` takes each column's parts as written (``["status"]``, or
    ``["orders", "status"]`` with the view in front) and returns the field's
    name; it raises to refuse a name a filter may not read. Each field is
    rendered quoted, so a name the dialect reserves still reads as the field.
    A ``MEASURE()`` call is the caller's to refuse by name before this runs.
    """

    if not clause or "{" in clause or "}" in clause or ";" in clause:
        return None
    tree = _parse(clause)
    if tree is None or isinstance(tree, exp.Query | exp.Command):
        return None
    if tree.find(*_FILTER_FORBIDDEN) is not None:
        return None
    if any(isinstance(node, exp.Dot) for node in tree.walk()):
        return None
    columns = list(tree.find_all(exp.Column))
    if not columns:
        return None
    for column in columns:
        parts = [p for p in (column.catalog, column.db, column.table) if p]
        if len(parts) > 1:
            return None
        name = resolve([*parts, column.name])
        column.replace(exp.column(exp.to_identifier(name, quoted=True)))
    return tree.sql(dialect=DIALECT)


def source_tables(source: str) -> list[str] | None:
    """Every table a ``source`` reads, as written without backticks, or ``None``
    where it cannot be read.

    A table name is itself. A query is every table it names, a table in a
    subquery or a join included, and a name a ``WITH`` in scope defines excluded:
    what Databricks scans to answer the view, which is what a statement over it
    is priced by. A one-part name is a CTE only where a ``WITH`` enclosing it
    defines one of that name; a CTE in a subquery is not visible outside it, so
    a table of the same name there is still read.
    """

    if _TABLE_NAME.match(source) and not _QUERY.match(source):
        return [source.replace("`", "").strip()]
    if not _QUERY.match(source):
        return None
    tree = _parse(source)
    if tree is None:
        return None
    found: list[str] = []
    for table in tree.find_all(exp.Table):
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        if not parts or (len(parts) == 1 and _cte_in_scope(table, parts[0])):
            continue
        name = ".".join(parts)
        if name not in found:
            found.append(name)
    return found or None


def _cte_in_scope(table: exp.Table, name: str) -> bool:
    """Whether a ``WITH`` lexically enclosing ``table`` defines ``name``.

    Walked outward from the reference: a query's ``WITH`` is visible to the
    query's body and every subquery in it, and within one ``WITH`` a CTE sees
    those listed before it (and itself, where the ``WITH`` is recursive).
    """

    lowered = name.lower()
    child: exp.Expression = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            ctes = list(node.expressions)
            index = next((i for i, c in enumerate(ctes) if c is child), len(ctes))
            visible = ctes[: index + 1] if node.args.get("recursive") else ctes[:index]
            if any(cte.alias_or_name.lower() == lowered for cte in visible):
                return True
        else:
            for value in node.args.values():
                if (
                    isinstance(value, exp.With)
                    and value is not child
                    and any(
                        cte.alias_or_name.lower() == lowered
                        for cte in value.expressions
                    )
                ):
                    return True
        child, node = node, node.parent
    return False
