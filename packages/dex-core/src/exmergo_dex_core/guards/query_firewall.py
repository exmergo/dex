"""The query firewall: the policy gate for agent-authored SQL.

`explore query` lets the agent write its own SELECTs; this module decides whether
one may run. The engine never generates these queries, it only refuses or bounds
them. The policy: a query's output may carry values only from columns dex has
profiled and not flagged as PII. Formally, every value path from a PII-flagged
(or unprofiled) column to a projection root must pass through a *measuring*
aggregate (COUNT, AVG, SUM, ...), whose output is a statistic rather than a
value. Value-carrying aggregates (MIN, MAX, ANY_VALUE, STRING_AGG, ...) do not
cut the path, and unknown functions are treated as carrying, so the gate fails
closed.

Resolution happens against the `.dex/` cache, which is what makes the policy
computable: the cache holds the PII flags. No cache, or a table or column the
cache does not know, refuses with the fix ("profile it first"). Filters, join
conditions, GROUP BY and ORDER BY are unrestricted: values flow into them, not
out of them. Row-level projections of a flagged column (including comparisons
like ``name LIKE 'A%'``) are refused; per-row predicates over PII are still
row-level PII-derived data.

The firewall also clamps LIMIT (token protection, enforced here rather than
trusted to agent frugality) and records which cache tables the query touches.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp

from ..cache import Dataset, DexCache, match_identifier
from ..config import QueryLimits
from .sql_guard import NotSelectOnlyError, assert_select_only


class QueryRefusedError(Exception):
    """Raised when a query violates the firewall policy. The message always
    names the offending construct and the fix, so a refusal costs the agent one
    rewrite, not a debugging session."""


@dataclass(frozen=True)
class InspectedQuery:
    """A query the firewall approved: the (possibly rewritten) SQL, the row cap
    the caller must enforce when fetching, whether the engine imposed that cap
    (only then is a result at the cap reported as truncated), and the cache
    identifiers the query reads."""

    sql: str
    row_cap: int
    capped_by_engine: bool
    tables: list[str]


# Aggregates whose output is a statistic, not a value: they cut the value path
# from a flagged column to the projection. MIN/MAX/ANY_VALUE/MODE/percentiles
# and the collecting aggregates (STRING_AGG, ARRAY_AGG, LIST, HISTOGRAM) all
# return actual values, so they are deliberately absent.
_MEASURING = frozenset(
    {
        "count",
        "approx_distinct",
        "approx_count_distinct",
        "avg",
        "sum",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "variance_pop",
        "var_pop",
        "var_samp",
        "corr",
        "covar_pop",
        "covar_samp",
        "bool_and",
        "bool_or",
        "entropy",
        "kurtosis",
        "skewness",
    }
)

# Roots an agent query may have. Stricter than sql_guard's read-only set:
# DESCRIBE/PRAGMA are introspection with their own commands (inventory/profile),
# and letting them through here would bypass the cache gate.
_QUERY_ROOTS = tuple(
    c
    for c in (
        exp.Select,
        getattr(exp, "SetOperation", None),
        getattr(exp, "Union", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "Except", None),
    )
    if isinstance(c, type)
)

# A derived source (CTE, subquery, set operation) is represented by its output
# taints: output column name (lowered) -> the flagged columns whose values can
# reach it. A physical source is the cached Dataset itself.
_Outputs = dict[str, set[str]]
_Source = Dataset | dict


def inspect_query(
    sql: str,
    cache: DexCache,
    limits: QueryLimits,
    *,
    dialect: str = "duckdb",
) -> InspectedQuery:
    """Approve (and bound) an agent query, or raise :class:`QueryRefusedError`."""

    try:
        assert_select_only(sql, dialect=dialect)
    except NotSelectOnlyError as exc:
        raise QueryRefusedError(str(exc)) from exc
    except sqlglot.errors.ParseError as exc:
        raise QueryRefusedError(f"could not parse query: {exc}") from exc

    root = sqlglot.parse_one(sql, dialect=dialect)
    if not isinstance(root, _QUERY_ROOTS):
        raise QueryRefusedError(
            f"only SELECT queries may run here, got {type(root).__name__}; use "
            "`explore inventory` / `explore profile` for introspection"
        )

    known = {d.identifier: d for d in cache.datasets}
    tables: set[str] = set()
    outputs = _query_outputs(root, {}, known, tables)

    offending = sorted({flag for taint in outputs.values() for flag in taint})
    if offending:
        raise QueryRefusedError(
            "the projection would carry values from PII-flagged column(s): "
            + "; ".join(offending)
            + ". Use a measuring aggregate over them (COUNT, "
            "APPROX_COUNT_DISTINCT, AVG(LENGTH(...))), or drop them from the "
            "output. PII values never cross the envelope."
        )

    new_sql, row_cap, capped = _apply_limit(root, limits.max_rows, dialect)
    return InspectedQuery(
        sql=new_sql,
        row_cap=row_cap,
        capped_by_engine=capped,
        tables=sorted(tables),
    )


# --- taint analysis ------------------------------------------------------------


def _query_outputs(
    node: exp.Expression,
    env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
) -> _Outputs:
    """Output taints of a whole query node (SELECT or a set operation)."""

    if isinstance(node, exp.Select):
        return _select_outputs(node, env, known, tables)
    if isinstance(node, _QUERY_ROOTS):  # set operation: UNION/INTERSECT/EXCEPT
        left = _query_outputs(node.left, env, known, tables)
        right = _query_outputs(node.right, env, known, tables)
        # Column names come from the left side; taints merge positionally, and a
        # length mismatch (invalid SQL anyway) merges everything conservatively.
        left_items = list(left.items())
        right_taints = list(right.values())
        if len(left_items) != len(right_taints):
            everything = set().union(*left.values(), *right.values())
            return {name: set(everything) for name, _ in left_items}
        return {
            name: taint | right_taints[i] for i, (name, taint) in enumerate(left_items)
        }
    if isinstance(node, exp.Subquery):
        return _query_outputs(node.this, env, known, tables)
    raise QueryRefusedError(f"unsupported query shape: {type(node).__name__}")


def _select_outputs(
    select: exp.Select,
    outer_env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
) -> _Outputs:
    env = dict(outer_env)
    for cte in select.ctes:
        env[cte.alias_or_name.lower()] = _query_outputs(cte.this, env, known, tables)

    sources = _resolve_sources(select, env, known, tables)

    outputs: _Outputs = {}
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            for alias, source in sources.items():
                outputs.update(_expand_star(source, alias))
            continue
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            alias = projection.table.lower()
            if alias not in sources:
                raise QueryRefusedError(f"unknown table or alias '{projection.table}'")
            outputs.update(_expand_star(sources[alias], alias))
            continue
        name = (projection.alias_or_name or projection.sql()).lower()
        outputs[name] = _expr_taint(projection, sources, known, tables)
    return outputs


def _resolve_sources(
    select: exp.Select,
    env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
) -> dict[str, _Source]:
    source_nodes: list[exp.Expression] = []
    # sqlglot renamed the arg key "from" to "from_" between major versions.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        source_nodes.append(from_clause.this)
    source_nodes.extend(join.this for join in select.args.get("joins") or [])

    sources: dict[str, _Source] = {}
    for node in source_nodes:
        alias = node.alias_or_name.lower()
        if isinstance(node, exp.Table):
            dotted = ".".join(p for p in (node.catalog, node.db, node.name) if p)
            if node.name.lower() in env and not (node.catalog or node.db):
                sources[alias] = env[node.name.lower()]  # a CTE shadows tables
                continue
            sources[alias] = _resolve_dataset(dotted, known, tables)
        elif isinstance(node, exp.Subquery):
            merged = env | sources
            sources[alias] = _query_outputs(node.this, merged, known, tables)
        else:
            raise QueryRefusedError(
                f"unsupported FROM source: {type(node).__name__}; query cached "
                "tables and views only"
            )
    return sources


def _resolve_dataset(
    dotted: str, known: dict[str, Dataset], tables: set[str]
) -> Dataset:
    matches = match_identifier(dotted, list(known))
    if not matches:
        raise QueryRefusedError(
            f"'{dotted}' is not in the .dex cache; run `explore map` (or "
            f"`explore profile {dotted}`) first so its columns and PII flags "
            "are known"
        )
    if len(matches) > 1:
        raise QueryRefusedError(
            f"'{dotted}' is ambiguous in the cache: {', '.join(matches)}; qualify it"
        )
    dataset = known[matches[0]]
    if not dataset.columns:
        raise QueryRefusedError(
            f"'{dataset.identifier}' is inventoried but not profiled; run "
            f"`explore profile {dotted}` or `explore map --full` first"
        )
    tables.add(dataset.identifier)
    return dataset


def _expand_star(source: _Source, alias: str) -> _Outputs:
    if isinstance(source, Dataset):
        expanded: _Outputs = {}
        for col in source.columns:
            expanded[col.name.lower()] = _column_taint(source, col.name)
        return expanded
    return {name: set(taint) for name, taint in source.items()}


def _column_taint(dataset: Dataset, column_name: str) -> set[str]:
    for col in dataset.columns:
        if col.name.lower() == column_name.lower():
            if col.pii is None:
                return set()
            table = dataset.identifier.rsplit(".", 1)[-1]
            return {f"{table}.{col.name} ({col.pii.category.value})"}
    raise QueryRefusedError(
        f"column '{column_name}' is not in the profiled cache for "
        f"'{dataset.identifier}'; the cache may be stale, re-run `explore map`"
    )


def _expr_taint(
    node: exp.Expression,
    sources: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
) -> set[str]:
    if isinstance(node, exp.Column):
        return _resolve_column(node, sources)
    if isinstance(node, exp.Star):
        # A bare * as a function argument: conservatively the union of every
        # source column (COUNT(*) never gets here; COUNT is cut first).
        taint: set[str] = set()
        for alias, source in sources.items():
            for sub in _expand_star(source, alias).values():
                taint |= sub
        return taint
    if isinstance(node, exp.Subquery) or (
        isinstance(node, exp.Select) and node.parent is not None
    ):
        inner = node.this if isinstance(node, exp.Subquery) else node
        outputs = _query_outputs(inner, dict(sources), known, tables)
        return set().union(*outputs.values()) if outputs else set()
    if isinstance(node, exp.Filter):
        # FILTER (WHERE ...) is a filter: values flow in, not out.
        return _expr_taint(node.this, sources, known, tables)
    if isinstance(node, exp.Window):
        # PARTITION BY / ORDER BY route rows; only the function's output crosses.
        return _expr_taint(node.this, sources, known, tables)
    if isinstance(node, exp.Func) and _func_name(node) in _MEASURING:
        return set()
    # Everything else (carrying/unknown functions, operators, CASE, casts)
    # passes taint through from all children: fail closed.
    taint = set()
    for child in node.iter_expressions():
        taint |= _expr_taint(child, sources, known, tables)
    return taint


def _resolve_column(node: exp.Column, sources: dict[str, _Source]) -> set[str]:
    name = node.name
    qualifier = node.table.lower() if node.table else ""

    if qualifier:
        if qualifier not in sources:
            raise QueryRefusedError(
                f"unknown table or alias '{node.table}' for column "
                f"'{node.table}.{name}'"
            )
        return _source_column_taint(sources[qualifier], name)

    hits = [source for source in sources.values() if _source_has_column(source, name)]
    if not hits:
        raise QueryRefusedError(
            f"column '{name}' is not in any queried table's profile; check the "
            "name, or re-run `explore map` if the cache is stale"
        )
    if len(hits) > 1:
        raise QueryRefusedError(f"column '{name}' is ambiguous; qualify it")
    return _source_column_taint(hits[0], name)


def _source_has_column(source: _Source, name: str) -> bool:
    if isinstance(source, Dataset):
        return any(c.name.lower() == name.lower() for c in source.columns)
    return name.lower() in source


def _source_column_taint(source: _Source, name: str) -> set[str]:
    if isinstance(source, Dataset):
        return _column_taint(source, name)
    taint = source.get(name.lower())
    if taint is None:
        raise QueryRefusedError(f"column '{name}' does not exist in the subquery")
    return set(taint)


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return node.sql_name().lower()


# --- LIMIT clamp ---------------------------------------------------------------


def _apply_limit(
    root: exp.Expression, max_rows: int, dialect: str
) -> tuple[str, int, bool]:
    """Bound the result: keep an agent LIMIT at or under the cap (the agent chose
    the bound, so a full result is not 'truncated'); otherwise set the engine cap
    plus one row, so the caller can detect and report truncation."""

    existing: int | None = None
    limit = root.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal):
        try:
            existing = int(limit.expression.name)
        except ValueError:
            existing = None

    if existing is not None and existing <= max_rows:
        return root.sql(dialect=dialect), existing, False

    root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows + 1)))
    return root.sql(dialect=dialect), max_rows, True
