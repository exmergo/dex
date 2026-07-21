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

A FROM clause may unnest, in each dialect's native idiom (UNNEST, LATERAL
FLATTEN, LATERAL VIEW EXPLODE, set-returning functions, PartiQL navigation and
UNPIVOT ... AT), provided the unnested value derives from columns of tables the
query already reads: either a bare column, or an allowlisted JSON/array
function over such columns. Tables, subqueries, literals, and generators
inside an unnest stay refused; they are where the arbitrary-read and
row-synthesis risks live. Every column an unnest produces (values, keys,
paths, offsets) inherits the taint of the columns it derives from, so PII
cannot be laundered through a reshape.

The firewall also clamps LIMIT (token protection, enforced here rather than
trusted to agent frugality) and records which cache tables the query touches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp

from ..cache import CACHE_SCHEMA_VERSION, Dataset, DexCache, match_identifier
from ..config import QueryLimits
from .sql_guard import NotSelectOnlyError, assert_select_only


class QueryRefusedError(Exception):
    """Raised when a query violates the firewall policy. The message always
    names the offending construct and the fix, so a refusal costs the agent one
    rewrite, not a debugging session."""


# A flag at or above this confidence blocks projection; below it, the query runs
# and the envelope carries a warning naming the column, category, and number.
# Hard-coded engine policy, uniform across categories: a configurable threshold
# would let a one-line config edit quietly widen the PII boundary. At today's
# base confidences everything blocks; only evidence-de-rated flags fall below.
PII_BLOCK_CONFIDENCE = 0.5


@dataclass(frozen=True)
class InspectedQuery:
    """A query the firewall approved: the (possibly rewritten) SQL, the row cap
    the caller must enforce when fetching, whether the engine imposed that cap
    (only then is a result at the cap reported as truncated), the cache
    identifiers the query reads, and per-column warnings for projections of
    sub-threshold PII flags (built from column names, categories, and numbers
    only, never values)."""

    sql: str
    row_cap: int
    capped_by_engine: bool
    tables: list[str]
    warnings: list[str] = field(default_factory=list)


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

# JSON/array functions whose result a FROM clause may unnest, per dialect:
# key enumeration (JSON_KEYS, OBJECT_KEYS, jsonb_object_keys), array-element
# extraction (JSON_EXTRACT_ARRAY, FLATTEN, explode, jsonb_array_elements), and
# the parse/navigation calls needed to reach an array inside a document
# (PARSE_JSON, from_json, ->). Matching is by sqlglot expression class where a
# canonical class exists and by lowered Anonymous name otherwise; sql_name() is
# unreliable for matching (JSONKeysAtDepth renders as j_s_o_n_keys_at_depth).
# Deliberately absent: generators (GENERATE_ARRAY and friends synthesize rows
# from nothing, the row-explosion shape), string splitters (not needed for
# JSON exploration; extendable if demand appears), and every aggregate.
# Redshift's native idioms (PartiQL navigation, UNPIVOT ... AT) take bare
# columns rather than function calls, so its entry is empty and the
# bare-column rule in _unnest_source carries it.
_UNNEST_FUNCS: dict[str, tuple[tuple[type, ...], frozenset[str]]] = {
    "bigquery": (
        (
            exp.JSONKeysAtDepth,
            exp.JSONExtractArray,
            exp.JSONValueArray,
            exp.JSONExtract,
            exp.ParseJSON,
        ),
        frozenset(),
    ),
    "snowflake": (
        (exp.Explode, exp.JSONKeys, exp.ParseJSON, exp.JSONExtract),
        frozenset(),
    ),
    "databricks": (
        (exp.Explode, exp.JSONKeys, exp.JSONExtractScalar, exp.ParseJSON),
        frozenset({"from_json", "variant_explode", "try_parse_json"}),
    ),
    "postgres": (
        (exp.JSONExtract, exp.JSONExtractScalar),
        frozenset(
            {
                "json_object_keys",
                "jsonb_object_keys",
                "json_each",
                "jsonb_each",
                "json_each_text",
                "jsonb_each_text",
                "json_array_elements",
                "jsonb_array_elements",
                "json_array_elements_text",
                "jsonb_array_elements_text",
            }
        ),
    ),
    "redshift": ((), frozenset()),
    "duckdb": (
        (exp.JSONKeys, exp.JSONExtract, exp.JSONExtractScalar, exp.ParseJSON),
        frozenset({"json_each"}),
    ),
}

# Output columns a lateral/flatten construct produces when the query does not
# alias them: Snowflake's FLATTEN emits a fixed six-column shape; the other
# explode-style constructs emit some subset of these depending on the input
# (array vs map, with or without position). Over-listing is safe: a name that
# does not really exist can only cause an ambiguity refusal or a live query
# error, never a leak, because every listed name carries the full taint.
_FLATTEN_COLUMNS = ("seq", "key", "path", "index", "value", "this")
_EXPLODE_COLUMNS = ("col", "key", "value", "pos")

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
# reach it, each a (label, confidence) pair. Presence taints; whether a taint
# blocks or merely warns is decided once, at the projection root, against
# PII_BLOCK_CONFIDENCE. A physical source is the cached Dataset itself.
_Taint = tuple[str, float]
_Outputs = dict[str, set[_Taint]]
_Source = Dataset | dict

# Warehouse-layer prefixes stripped before entity matching, so stg_products and
# products share the entity "product" when suggesting an unflagged twin column.
_LAYER_PREFIX = re.compile(r"^(raw|stg|src|dim|fct|fact|mart|int)_", re.IGNORECASE)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STRING_TYPE_HINTS = ("CHAR", "TEXT", "STRING", "VARCHAR")


def _normalize_name(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _is_string_type(data_type: str) -> bool:
    upper = data_type.upper()
    return any(hint in upper for hint in _STRING_TYPE_HINTS)


def _entity_of(table: str) -> str:
    """The entity a table name denotes: layer prefix stripped, naively
    singularized (products -> product), lowered."""

    stripped = _LAYER_PREFIX.sub("", table).lower()
    if stripped.endswith("s") and not stripped.endswith("ss"):
        stripped = stripped[:-1]
    return stripped


def recovery_hints(offending: list[str], cache: DexCache) -> list[str]:
    """Unflagged string columns that plausibly carry the same readable value as a
    refused PII column, so a refusal points at a lawful alternative instead of a
    dead end (for example ``inventory_items.product_name`` for a flagged
    ``products.name``). Column names only; no value is ever read. The flag itself
    is never weakened, only the guidance around it."""

    suggestions: set[str] = set()
    for entry in offending:
        table_col = entry.split(" (", 1)[0]
        table, _, column = table_col.rpartition(".")
        entity = _entity_of(table)
        base = _normalize_name(column)
        if not entity or not base:
            continue
        target = f"{entity}_{base}"
        for dataset in cache.datasets:
            short = dataset.identifier.rsplit(".", 1)[-1]
            for col in dataset.columns:
                # A sub-threshold flag is a lawful projection target, so it
                # qualifies as a twin (its projection warns, never blocks).
                blocked = (
                    col.pii is not None and col.pii.confidence >= PII_BLOCK_CONFIDENCE
                )
                if blocked or not _is_string_type(col.data_type):
                    continue
                normalized = _normalize_name(col.name)
                tokens = set(normalized.split("_"))
                if normalized == target or {entity, base} <= tokens:
                    suggestions.add(f"{short}.{col.name}")
    return sorted(suggestions)


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
    outputs = _query_outputs(root, {}, known, tables, dialect)

    flagged = {taint for taints in outputs.values() for taint in taints}
    offending = sorted(
        {label for label, conf in flagged if conf >= PII_BLOCK_CONFIDENCE}
    )
    if offending:
        message = (
            "the projection would carry values from PII-flagged column(s): "
            + "; ".join(offending)
            + ". Use a measuring aggregate over them (COUNT, "
            "APPROX_COUNT_DISTINCT, AVG(LENGTH(...))), or drop them from the "
            "output. PII values never cross the envelope."
        )
        hints = recovery_hints(offending, cache)
        if hints:
            message += (
                " An unflagged column may carry the same readable value: "
                + ", ".join(hints)
                + "."
            )
        message += (
            " A column reviewed as not PII can be cleared durably with a "
            "pii_overrides entry in .dex/config.yml."
        )
        if cache.schema_version < CACHE_SCHEMA_VERSION and any(
            "(name)" in label for label in offending
        ):
            message += (
                " This cache predates value-shape profiling; re-profile the "
                "table to refine name flags with value-shape evidence."
            )
        raise QueryRefusedError(message)

    warnings = [
        f"{label} is PII-flagged at confidence {conf:g}, below the "
        f"{PII_BLOCK_CONFIDENCE:g} block threshold, so the projection ran. If "
        "its values are personal data, drop the column; if it is reviewed as "
        "not PII, record a pii_overrides entry in .dex/config.yml."
        for label, conf in sorted(flagged)
    ]

    new_sql, row_cap, capped = _apply_limit(root, limits.max_rows, dialect)
    return InspectedQuery(
        sql=new_sql,
        row_cap=row_cap,
        capped_by_engine=capped,
        tables=sorted(tables),
        warnings=warnings,
    )


# --- taint analysis ------------------------------------------------------------


def _query_outputs(
    node: exp.Expression,
    env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> _Outputs:
    """Output taints of a whole query node (SELECT or a set operation)."""

    # sqlglot attaches a WITH clause to the query root. For a plain query that
    # root is a Select, but for UNION/INTERSECT/EXCEPT it is the set operation.
    # Register CTEs here so both sides of any query shape resolve the same local
    # relations. Process them in order because later CTEs may reference earlier
    # ones.
    local_env = dict(env)
    for cte in node.ctes:
        local_env[cte.alias_or_name.lower()] = _query_outputs(
            cte.this, local_env, known, tables, dialect
        )

    if isinstance(node, exp.Select):
        return _select_outputs(node, local_env, known, tables, dialect)
    if isinstance(node, _QUERY_ROOTS):  # set operation: UNION/INTERSECT/EXCEPT
        left = _query_outputs(node.left, local_env, known, tables, dialect)
        right = _query_outputs(node.right, local_env, known, tables, dialect)
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
        return _query_outputs(node.this, local_env, known, tables, dialect)
    raise QueryRefusedError(f"unsupported query shape: {type(node).__name__}")


def _select_outputs(
    select: exp.Select,
    outer_env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> _Outputs:
    env = dict(outer_env)

    sources = _resolve_sources(select, env, known, tables, dialect)

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
        outputs[name] = _expr_taint(projection, sources, known, tables, dialect)
    return outputs


def _resolve_sources(
    select: exp.Select,
    env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> dict[str, _Source]:
    source_nodes: list[exp.Expression] = []
    # sqlglot renamed the arg key "from" to "from_" between major versions.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        source_nodes.append(from_clause.this)
    source_nodes.extend(join.this for join in select.args.get("joins") or [])
    # LATERAL VIEW (Databricks/Hive) hangs off the select, not the join list.
    source_nodes.extend(select.args.get("laterals") or [])

    sources: dict[str, _Source] = {}
    for node in source_nodes:
        alias = node.alias_or_name.lower()
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            dotted = ".".join(p for p in (node.catalog, node.db, node.name) if p)
            if node.name.lower() in env and not (node.catalog or node.db):
                sources[alias] = env[node.name.lower()]  # a CTE shadows tables
                continue
            sources[alias] = _resolve_dataset(dotted, known, tables)
        elif isinstance(node, exp.Subquery):
            merged = env | sources
            sources[alias] = _query_outputs(node.this, merged, known, tables, dialect)
        elif isinstance(
            node, (exp.Unnest, exp.Lateral, exp.TableFromRows, exp.Table)
        ) or (isinstance(node, exp.Pivot) and node.args.get("unpivot")):
            merged = env | sources
            key, outputs = _unnest_source(node, merged, known, tables, dialect)
            if key in sources:
                raise QueryRefusedError(
                    f"duplicate source alias '{key}'; give each unnest its own alias"
                )
            sources[key] = outputs
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


def _column_taint(dataset: Dataset, column_name: str) -> set[_Taint]:
    for col in dataset.columns:
        if col.name.lower() == column_name.lower():
            if col.pii is None:
                return set()
            table = dataset.identifier.rsplit(".", 1)[-1]
            label = f"{table}.{col.name} ({col.pii.category.value})"
            return {(label, col.pii.confidence)}
    raise QueryRefusedError(
        f"column '{column_name}' is not in the profiled cache for "
        f"'{dataset.identifier}'; the cache may be stale, re-run `explore map`"
    )


def _expr_taint(
    node: exp.Expression,
    sources: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> set[_Taint]:
    if isinstance(node, exp.Column):
        return _resolve_column(node, sources)
    if isinstance(node, exp.Star):
        # A bare * as a function argument: conservatively the union of every
        # source column (COUNT(*) never gets here; COUNT is cut first).
        taint: set[_Taint] = set()
        for alias, source in sources.items():
            for sub in _expand_star(source, alias).values():
                taint |= sub
        return taint
    if isinstance(node, exp.Subquery) or (
        isinstance(node, exp.Select) and node.parent is not None
    ):
        inner = node.this if isinstance(node, exp.Subquery) else node
        outputs = _query_outputs(inner, dict(sources), known, tables, dialect)
        return set().union(*outputs.values()) if outputs else set()
    if isinstance(node, exp.Filter):
        # FILTER (WHERE ...) is a filter: values flow in, not out.
        return _expr_taint(node.this, sources, known, tables, dialect)
    if isinstance(node, exp.Window):
        # PARTITION BY / ORDER BY route rows; only the function's output crosses.
        return _expr_taint(node.this, sources, known, tables, dialect)
    if isinstance(node, exp.Func) and _func_name(node) in _MEASURING:
        return set()
    # Everything else (carrying/unknown functions, operators, CASE, casts)
    # passes taint through from all children: fail closed.
    taint = set()
    for child in node.iter_expressions():
        taint |= _expr_taint(child, sources, known, tables, dialect)
    return taint


def _unnest_source(
    node: exp.Expression,
    sources: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> tuple[str, _Outputs]:
    """Admit a FROM-clause unnest construct, or refuse it.

    Handles every dialect's native spelling: UNNEST (BigQuery, DuckDB,
    Postgres, and Redshift's PartiQL navigation, which parses as an Unnest of
    a bare column), LATERAL / LATERAL VIEW over explode-style calls (Snowflake
    FLATTEN, Databricks explode and variant_explode), TABLE(FLATTEN(...)),
    set-returning functions as a FROM item (Postgres), and Redshift's
    UNPIVOT ... AT. The policy is uniform: each unnested expression must be a
    bare column of an already-resolved source or an allowlisted JSON/array
    call whose subtree reads nothing but such columns. Returns the alias to
    register and the output columns, every one carrying the union of the
    inner expressions' taints.
    """

    if isinstance(node, exp.Unnest):
        inner = list(node.expressions)
        table_alias = node.args.get("alias")
        offset = node.args.get("offset")
    elif isinstance(node, (exp.Lateral, exp.TableFromRows)):
        inner = [node.this]
        table_alias = node.args.get("alias")
        offset = None
    elif isinstance(node, exp.Table):
        inner = [node.this]
        table_alias = node.args.get("alias")
        offset = node.args.get("ordinality")
    else:  # Pivot(unpivot=True): UNPIVOT <col> AS <value> AT <key>
        at_index = node.this
        if not isinstance(at_index, exp.AtIndex):
            raise QueryRefusedError(
                "unsupported FROM source: Pivot; query cached tables and views only"
            )
        value_expr = at_index.this
        value_name = ""
        if isinstance(value_expr, exp.Alias):
            value_name = value_expr.alias
            value_expr = value_expr.this
        key_name = at_index.expression.name if at_index.expression is not None else ""
        taint = _unnest_taint([value_expr], sources, known, tables, dialect)
        outputs = {
            name.lower(): set(taint)
            for name in (value_name or "value", key_name or "key")
        }
        return (value_name or key_name or "unpivot").lower(), outputs

    if not inner:
        raise QueryRefusedError(
            "unnest has nothing to unnest; give it a column of a queried "
            "table or a permitted JSON/array function over one"
        )
    taint = _unnest_taint(inner, sources, known, tables, dialect)

    # Output naming. Explicit column aliases win; otherwise the construct's
    # own convention applies: a lone table alias doubles as the column name
    # (BigQuery UNNEST(...) AS k, Postgres jsonb_object_keys(...) AS k), and
    # explode-style constructs emit their fixed columns.
    alias_name = ""
    columns: list[str] = []
    if table_alias is not None:
        this = table_alias.args.get("this")
        # BigQuery synthesizes a _tN table alias for UNNEST(...) AS k; the
        # user-visible name is the column alias, not the synthetic one.
        if isinstance(this, exp.Identifier) and not re.fullmatch(r"_t\d+", this.name):
            alias_name = this.name
        columns = [c.name for c in table_alias.columns]
    if not columns:
        if isinstance(node, (exp.Lateral, exp.TableFromRows)):
            fixed = _FLATTEN_COLUMNS if dialect == "snowflake" else _EXPLODE_COLUMNS
            columns = list(fixed)
        elif isinstance(node, exp.Table):
            columns = [alias_name, "key", "value"] if alias_name else ["key", "value"]
        elif alias_name:
            columns = [alias_name]
        elif len(inner) == 1 and dialect == "duckdb":
            columns = ["unnest"]  # DuckDB's default name for a bare UNNEST
        else:
            raise QueryRefusedError(
                "alias the unnest so its output has a column name, e.g. "
                "UNNEST(...) AS u(k)"
            )
    if isinstance(offset, exp.Expression):
        columns.append(offset.name or "offset")
    elif offset:
        columns.append("offset" if isinstance(node, exp.Unnest) else "ordinality")

    key = (alias_name or columns[0]).lower()
    return key, {name.lower(): set(taint) for name in columns if name}


def _unnest_taint(
    inner: list[exp.Expression],
    sources: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> set[_Taint]:
    """Validate the expressions a FROM-clause unnest reads and return their
    combined taint. A bare column may be dotted past two parts (Redshift
    PartiQL navigates into SUPER: t.doc.items); the leftmost identifier is the
    source alias and the next one is the profiled column, so navigation
    inherits that column's taint."""

    classes, names = _UNNEST_FUNCS.get(dialect, ((), frozenset()))
    taint: set[_Taint] = set()
    for expr in inner:
        if isinstance(expr, exp.Column):
            parts = [p.name for p in expr.parts]
            if len(parts) == 1:
                taint |= _resolve_column(expr, sources)
                continue
            qualifier = parts[0].lower()
            if qualifier not in sources:
                raise QueryRefusedError(
                    f"unknown table or alias '{parts[0]}' for unnest of '{expr.sql()}'"
                )
            taint |= _source_column_taint(sources[qualifier], parts[1])
            continue
        allowed_call = isinstance(expr, classes) or (
            isinstance(expr, exp.Anonymous) and str(expr.this).lower() in names
        )
        if not allowed_call:
            permitted = sorted({c.__name__ for c in classes} | set(names))
            raise QueryRefusedError(
                f"unnest of {type(expr).__name__} is not permitted; unnest a "
                "column of a queried table directly, or through one of: "
                + ", ".join(permitted)
            )
        for sub in expr.walk():
            if isinstance(sub, (exp.Select, exp.Subquery, exp.Table)):
                raise QueryRefusedError(
                    "unnest must not read a table or subquery; unnest a "
                    "column of an already-queried table, directly or through "
                    "a permitted JSON/array function"
                )
            if isinstance(sub, exp.Func) and not isinstance(sub, exp.Cast):
                sub_allowed = isinstance(sub, classes) or (
                    isinstance(sub, exp.Anonymous) and str(sub.this).lower() in names
                )
                if not sub_allowed:
                    raise QueryRefusedError(
                        f"unnest argument calls {type(sub).__name__}, which is "
                        "not a permitted JSON/array function here"
                    )
        taint |= _expr_taint(expr, sources, known, tables, dialect)
    return taint


def _resolve_column(node: exp.Column, sources: dict[str, _Source]) -> set[_Taint]:
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


def _source_column_taint(source: _Source, name: str) -> set[_Taint]:
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
