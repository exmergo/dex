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
row-level PII-derived data. The same reasoning cuts the path through
``FILTER (WHERE ...)`` and ``COUNTIF``/``COUNT_IF``: their condition argument is
a filter, not a projected value, so it is never walked for taint -- only the
aggregate's own (measuring) output crosses the envelope. This is what lets
BigQuery, which has no ``FILTER (WHERE ...)`` clause, express a batched
filtered count as ``COUNTIF(cond)`` instead.

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
from typing import NamedTuple

import sqlglot
from sqlglot import expressions as exp

from ..cache import (
    CACHE_SCHEMA_VERSION,
    Dataset,
    DexCache,
    PIICategory,
    match_identifier,
)
from ..config import QueryLimits
from ..errors import DexError
from . import PII_BLOCK_CONFIDENCE
from .sql_guard import NotSelectOnlyError, assert_select_only
from .sql_guard import function_name as _func_name


class QueryRefusedError(DexError):
    """Raised when a query violates the firewall policy. The message always
    names the offending construct and the fix, so a refusal costs the agent one
    rewrite, not a debugging session."""


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
#
# count_if: sqlglot parses COUNTIF/COUNT_IF (BigQuery, Snowflake, DuckDB) into
# its own exp.CountIf node, normalized to "count_if" by sql_name() regardless
# of dialect. Its sole argument is grammatically always a boolean condition,
# never a value-carrying expression -- structurally the same as a
# FILTER (WHERE ...) clause, which already passes via the exp.Filter carve-out
# in _expr_taint. Listing it here (rather than a separate carve-out) skips its
# condition entirely, exactly as intended: the condition is a filter, not a
# projection.
_MEASURING = frozenset(
    {
        "count",
        "count_if",
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
        # sqlglot parses bool_and/bool_or into LogicalAnd/LogicalOr on every
        # dialect, and those canonical names are what reach this set, so the
        # spellings above alone never matched. every is Trino's bool_and.
        "logical_and",
        "logical_or",
        "every",
        "geometric_mean",
        "entropy",
        "kurtosis",
        "skewness",
        # ClickHouse spellings. sqlglot canonicalizes `uniq` to ApproxDistinct
        # and `countIf` to CountIf, both already above, but the rest of the
        # family parses to AnonymousAggFunc carrying its own lowered name.
        # Every one of these reduces a column to a single number: the distinct
        # sketches (uniq*), and the dispersion functions ClickHouse spells
        # without the underscore the ANSI names use. Deliberately absent, and
        # therefore carrying: `any`, `argMin`/`argMax`, `topK`, `groupArray`,
        # `median`/`quantile*` and every other order statistic, all of which
        # return values from rows.
        "uniqexact",
        "uniqcombined",
        "uniqcombined64",
        "uniqhll12",
        "uniqtheta",
        "uniqupto",
        "stddevpop",
        "stddevsamp",
        "varpop",
        "varsamp",
        "covarpop",
        "covarsamp",
    }
)

# ClickHouse aggregate combinators that wrap a base aggregate without changing
# whether it measures: `-If` appends a condition, which is a filter exactly as
# `FILTER (WHERE ...)` is; `-OrNull` and `-OrDefault` only change what an empty
# input returns; `-Distinct` de-duplicates the input. So `sumIf` measures
# because `sum` does, and `maxIf` carries because `max` does. Combinators that
# change the *shape* of the result are deliberately absent, so `-Array`,
# `-Map`, `-ForEach`, `-State`, `-Merge` and `-Resample` all fall through to
# the fail-closed carrying rule.
_AGG_COMBINATORS = ("ifornull", "ifordefault", "if", "ornull", "ordefault", "distinct")

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
        # Newer sqlglot releases parse from_json as FromJson rather than
        # Anonymous. Keep both forms without requiring the new class on
        # older supported releases.
        tuple(
            cls
            for cls in (
                exp.Explode,
                exp.JSONKeys,
                exp.JSONExtractScalar,
                exp.ParseJSON,
                getattr(exp, "FromJson", None),
            )
            if isinstance(cls, type)
        ),
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
    # ClickHouse stores JSON as String and navigates it with the JSONExtract*
    # family; mapKeys/mapValues reach into a Map column. arrayJoin is the
    # function spelling of ARRAY JOIN and parses to exp.Explode.
    "clickhouse": (
        (exp.Explode, exp.JSONExtract, exp.JSONExtractScalar, exp.ParseJSON),
        frozenset(
            {
                "jsonextractkeysandvaluesraw",
                "jsonextractkeys",
                "jsonextractraw",
                "jsonextractarrayraw",
                "mapkeys",
                "mapvalues",
                "arrayzip",
            }
        ),
    ),
    "duckdb": (
        (exp.JSONKeys, exp.JSONExtract, exp.JSONExtractScalar, exp.ParseJSON),
        frozenset({"json_each"}),
    ),
    # Trino unnests arrays and maps, so a JSON document is reached by parsing it
    # and casting it to one: the canonical idiom is
    # UNNEST(CAST(json_parse(c) AS ARRAY(JSON))), which is why a top-level Cast
    # is admitted here (a nested one already is, everywhere). map_keys,
    # map_values and map_entries reach into a map column. sequence and split
    # are deliberately absent: they synthesize rows from a scalar.
    "trino": (
        tuple(
            cls
            for cls in (
                exp.Cast,
                exp.JSONExtract,
                exp.JSONExtractScalar,
                exp.ParseJSON,
                getattr(exp, "MapKeys", None),
            )
            if isinstance(cls, type)
        ),
        frozenset({"map_keys", "map_values", "map_entries"}),
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
# reach it. Presence taints; whether a taint blocks or merely warns is decided
# once, at the projection root, against PII_BLOCK_CONFIDENCE. A physical source
# is the cached Dataset itself.
#
# `identifier` and `column` (alongside the display `label` and `confidence`)
# are what let a blocking refusal name the exact `pii_overrides` entry that
# would clear the column (issue #217): the label alone is the short table name
# only, which is not enough to build a fully-qualified override safely (two
# datasets can share a short name), so the fully-qualified identifier has to
# ride along on the taint itself rather than being reconstructed later from a
# string two datasets could both match.
class _Taint(NamedTuple):
    label: str
    confidence: float
    identifier: str
    column: str
    category: PIICategory


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


def _recurs_elsewhere(cache: DexCache, taint: _Taint) -> bool:
    """Whether another dataset in the cache also flags a same-named column at
    the same category, which is the evidence that a pattern override (a scope
    glob, not one entry per table) is worth suggesting rather than a guess at
    how far a naming convention actually reaches."""

    return any(
        dataset.identifier != taint.identifier
        and any(
            col.name.lower() == taint.column.lower()
            and col.pii is not None
            and col.pii.category == taint.category
            for col in dataset.columns
        )
        for dataset in cache.datasets
    )


def _override_suggestion(taint: _Taint, cache: DexCache) -> str:
    """The exact ``pii_overrides`` entry that would clear ``taint``'s column,
    ready to paste into ``.dex/config.yml`` (issue #217).

    Column identity only, in flow-mapping YAML so it fits inline in the
    refusal's own prose: a fully-qualified path for the exact form, a bare
    name and scope for the pattern form. Never a value: the taint carries
    only what :class:`~..cache.PIIFlag` itself does (category and
    confidence), the same structural guarantee that flag makes.

    The pattern form is offered only when :func:`_recurs_elsewhere` finds the
    same column name flagged at the same category on another dataset; the
    suggested scope is that dataset's own schema (its identifier with the
    table segment replaced by ``*``), never wider than what was actually
    observed, so a caller who wants it to reach further widens the glob
    themselves rather than dex guessing a blast radius nothing here proves.
    """

    exact = f"`- {{column: {taint.identifier}.{taint.column}}}`"
    clause = f"for {taint.label}: {exact}"
    if _recurs_elsewhere(cache, taint):
        scope = taint.identifier.rsplit(".", 1)[0] + ".*"
        pattern = f"`- {{column_name: {taint.column}, scope: {scope}}}`"
        clause += f" (or, since it recurs across tables, {pattern})"
    return clause


# The table arguments that turn a read of a table into a read of its past.
# sqlglot keeps FOR VERSION/TIMESTAMP/SYSTEM_TIME AS OF (Trino, Databricks,
# BigQuery) in `version`, AT/BEFORE (Snowflake) and AT (VERSION => ...) (DuckDB)
# in `when`, and Snowflake's CHANGES stream in `changes`. Matching the node shape
# rather than the dialect is what lets a newly added dialect inherit the refusal.
_HISTORICAL_READ_ARGS = ("version", "when", "changes")


def assert_query_shape(sql: str, *, dialect: str = "duckdb") -> exp.Expression:
    """Prove a statement is one read-only SELECT and return its parsed root.

    Split out of :func:`inspect_query` so a caller that needs to touch the live
    connection before the cache can adjudicate (resolving a relation, profiling
    it) can prove the statement read-only first. Whatever else happens to an
    agent-authored statement, it is never the reason dex introspected a
    warehouse on its behalf.

    Cache-independent by construction, which is what makes it safe to run early;
    everything that needs the cache stays in :func:`inspect_query`. Time travel
    is refused here too: it needs no cache to recognize, and no relation should be
    profiled on behalf of a statement that was always going to be refused.
    """

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
    # Refused wherever it appears, a WHERE subquery included, because the cache
    # and every PII verdict in it describe each table as it is now. A past
    # snapshot keeps the schema it had then, so a column dropped since (and so
    # never profiled) comes back from SELECT *, and a change stream returns rows
    # the current table no longer holds.
    for table in root.find_all(exp.Table):
        if any(table.args.get(key) for key in _HISTORICAL_READ_ARGS):
            name = ".".join(p for p in (table.catalog, table.db, table.name) if p)
            raise QueryRefusedError(
                f"time travel is not permitted: {name} is read as of an earlier "
                "version or point in time, or as a change stream. The exploration "
                "cache and its PII verdicts describe the table as it is now, and a "
                "past snapshot can carry columns or values that were never "
                "profiled. Query the current table"
            )
    return root


def inspect_query(
    sql: str,
    cache: DexCache,
    limits: QueryLimits,
    *,
    dialect: str = "duckdb",
) -> InspectedQuery:
    """Approve (and bound) an agent query, or raise :class:`QueryRefusedError`."""

    root = assert_query_shape(sql, dialect=dialect)

    known = {d.identifier: d for d in cache.datasets}
    tables: set[str] = set()
    outputs = _query_outputs(root, {}, known, tables, dialect)

    flagged = {taint for taints in outputs.values() for taint in taints}
    blocking = sorted(
        (t for t in flagged if t.confidence >= PII_BLOCK_CONFIDENCE),
        key=lambda t: t.label,
    )
    offending = [t.label for t in blocking]
    if offending:
        message = (
            "the projection would carry values from PII-flagged column(s): "
            + "; ".join(offending)
            + ". Use a measuring aggregate over them (COUNT, COUNTIF, "
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
            "pii_overrides entry in .dex/config.yml, e.g. "
            + "; ".join(_override_suggestion(t, cache) for t in blocking)
            + "."
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
        f"{t.label} is PII-flagged at confidence {t.confidence:g}, below the "
        f"{PII_BLOCK_CONFIDENCE:g} block threshold, so the projection ran. If "
        "its values are personal data, drop the column; if it is reviewed as "
        "not PII, record a pii_overrides entry in .dex/config.yml."
        for t in sorted(flagged, key=lambda t: t.label)
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
    correlated: dict[str, _Source] | None = None,
) -> _Outputs:
    """Output taints of a whole query node (SELECT or a set operation).

    ``correlated`` is the enclosing query's sources, for a subquery that may
    name them (``WHERE x IN (SELECT t.id FROM u)``). They resolve a column the
    subquery's own sources do not have, and never shadow one they do.
    """

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
        return _select_outputs(node, local_env, known, tables, dialect, correlated)
    if isinstance(node, _QUERY_ROOTS):  # set operation: UNION/INTERSECT/EXCEPT
        left = _query_outputs(node.left, local_env, known, tables, dialect, correlated)
        right = _query_outputs(
            node.right, local_env, known, tables, dialect, correlated
        )
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
        return _query_outputs(node.this, local_env, known, tables, dialect, correlated)
    raise QueryRefusedError(f"unsupported query shape: {type(node).__name__}")


#: Select arguments that hold the query's own sources or projections, which the
#: projection path already resolves. Every other argument (WHERE, HAVING,
#: QUALIFY, PREWHERE, GROUP BY, ORDER BY, LIMIT, ...) is searched for subqueries.
_SOURCE_AND_PROJECTION_ARGS = frozenset(
    {"expressions", "from", "from_", "joins", "laterals", "with"}
)


def _clause_queries(node: exp.Expression):
    """The outermost queries under ``node``: each one resolves its own nested
    subqueries, so descending further would resolve them twice."""

    if isinstance(node, (exp.Subquery, *_QUERY_ROOTS)):
        yield node
        return
    for child in node.iter_expressions():
        yield from _clause_queries(child)


def _resolve_clause_subqueries(
    select: exp.Select,
    sources: dict[str, _Source],
    env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
    correlated: dict[str, _Source] | None,
) -> None:
    """Resolve every subquery outside the projection against the cache.

    A subquery under WHERE, EXISTS, IN or a join's ON projects nothing out of
    the statement, but it still reads: a table nobody profiled, used as a
    filter, is an oracle over values whose PII flags were never checked, and a
    table function there reaches whatever it reaches. So each one goes through
    the same source resolution as the projection path, with the same refusals,
    and its tables join the ones the relation check and the cost estimate see.
    Its taint is discarded, because none of its values leave the statement.
    """

    clauses: list[exp.Expression] = []
    for key, value in select.args.items():
        if key in _SOURCE_AND_PROJECTION_ARGS or value is None:
            continue
        values = value if isinstance(value, list) else [value]
        clauses.extend(v for v in values if isinstance(v, exp.Expression))
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            clauses.append(on)

    scope = {**(correlated or {}), **sources}
    for clause in clauses:
        for query in _clause_queries(clause):
            _query_outputs(query, env | sources, known, tables, dialect, scope)


def _select_outputs(
    select: exp.Select,
    outer_env: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
    correlated: dict[str, _Source] | None = None,
) -> _Outputs:
    env = dict(outer_env)

    sources = _resolve_sources(select, env, known, tables, dialect)
    _resolve_clause_subqueries(select, sources, env, known, tables, dialect, correlated)
    if correlated:
        # A correlated subquery's projection may name the enclosing query's
        # columns. SQL resolves a name in the innermost scope first, so the
        # enclosing sources are a fallback, never a peer.
        sources = _ScopedSources(sources, outer=correlated)

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
    # ClickHouse's ARRAY JOIN is a join by grammar and an unnest by meaning: it
    # expands one row per element of an array *already read from the tables in
    # scope*, so it reshapes without reaching anything new. It is collected
    # separately because its node is a bare aliased expression rather than one
    # of the FROM-source classes, and because the taint rule differs: the
    # produced column inherits whatever the array it came from carries.
    array_joins = [
        join
        for join in select.args.get("joins") or []
        if str(join.args.get("kind") or "").upper() == "ARRAY"
    ]
    source_nodes.extend(
        join.this for join in select.args.get("joins") or [] if join not in array_joins
    )
    # LATERAL VIEW (Databricks/Hive) hangs off the select, not the join list.
    source_nodes.extend(select.args.get("laterals") or [])

    sources: dict[str, _Source] = {}
    for raw in source_nodes:
        # ClickHouse's FROM <table> FINAL wraps the table in exp.Final. It is a
        # read modifier (collapse the merge tree's duplicate rows before
        # reading), so it changes which rows a query sees and nothing about
        # which columns it may project. Unwrapping here rather than adding a
        # branch keeps every rule below reading the table it always did;
        # without it an ordinary ClickHouse query is refused as an unsupported
        # FROM source.
        node = raw.this if isinstance(raw, exp.Final) else raw
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
        elif isinstance(node, exp.Values):
            sources[alias] = _values_source(node)
        else:
            raise QueryRefusedError(
                f"unsupported FROM source: {type(node).__name__}; query cached "
                "tables and views only"
            )
    for join in array_joins:
        merged = env | sources
        key, outputs = _array_join_source(join.this, merged, known, tables, dialect)
        if key in sources:
            raise QueryRefusedError(
                f"duplicate source alias '{key}'; give each ARRAY JOIN its own alias"
            )
        sources[key] = outputs
    return sources


def _values_source(node: exp.Values) -> _Outputs:
    """Admit an inline ``VALUES`` list as a FROM source, or refuse it.

    A list of literal rows (``cross join (values (7), (28)) as w (window_days)``)
    is self-contained: it reads no relation and carries no profile, so it needs
    nothing from the cache and its columns carry no taint. What it must not be
    is a side door. A row that reads a column or a subquery would reach data by
    a path the resolution above never saw, so the rows must be plain literals (not
    even a constant expression), and the columns must be named so every reference
    to them resolves by name rather than by a dialect's positional default
    (``col0`` here, ``column1`` there).
    """

    table_alias = node.args.get("alias")
    columns = [c.name.lower() for c in table_alias.columns] if table_alias else []
    if not columns:
        raise QueryRefusedError(
            "alias the VALUES list and name its columns, e.g. "
            "(VALUES (7), (28)) AS windows (window_days)"
        )
    for row in node.expressions:
        for cell in row.expressions if isinstance(row, exp.Tuple) else [row]:
            # Parentheses and a minus on a number still wrap a literal, and
            # ClickHouse parses each cell as a one-element tuple.
            negated = False
            while isinstance(cell, (exp.Neg, exp.Paren)) or (
                isinstance(cell, exp.Tuple) and len(cell.expressions) == 1
            ):
                negated = negated or isinstance(cell, exp.Neg)
                cell = cell.expressions[0] if isinstance(cell, exp.Tuple) else cell.this
            if not isinstance(cell, (exp.Literal, exp.Null, exp.Boolean)) or (
                negated and not cell.is_number
            ):
                raise QueryRefusedError(
                    "VALUES rows must be plain literals; an expression, column, "
                    "subquery or relation inside one is not allowed"
                )
    return {name: set() for name in columns}


def _array_join_source(
    node: exp.Expression,
    sources: dict[str, _Source],
    known: dict[str, Dataset],
    tables: set[str],
    dialect: str,
) -> tuple[str, _Outputs]:
    """Admit one ClickHouse ``ARRAY JOIN`` element, or refuse it.

    The rule is the one every other dialect's unnest already follows: an
    unnest reshapes what the query can already read, and cannot launder. So the
    expression must derive from columns of tables in scope (bare, or through a
    JSON/array function this dialect allows), and the column it produces
    inherits the full taint of whatever it derived from. A PII-flagged array
    exploded into rows is still PII, one value per row instead of one array per
    row, which is if anything the more exposed shape.
    """

    if not isinstance(node, exp.Alias):
        raise QueryRefusedError(
            "alias the ARRAY JOIN so its output has a column name, e.g. "
            "ARRAY JOIN tags AS tag"
        )
    inner = node.this
    # An unnest reshapes what the query can already read; it is not a second
    # way to reach something. A subquery or a bare relation inside one is a
    # laundering path (the taint of a value read through a nested SELECT is not
    # the taint the outer resolution computed), and a literal or generator
    # synthesizes rows from nothing. Every other dialect's unnest refuses these
    # in `_unnest_source`; ARRAY JOIN reaches this code by a different route and
    # owes the same refusal.
    if any(inner.find(cls) for cls in (exp.Subquery, exp.Select, exp.Table)):
        raise QueryRefusedError(
            "ARRAY JOIN over a subquery or relation is not allowed; unnest a "
            "column of a table the query already reads"
        )
    if not inner.find(exp.Column):
        raise QueryRefusedError(
            "ARRAY JOIN over a literal or generated array is not allowed; "
            "unnest a column of a table the query already reads"
        )
    _classes, names = _UNNEST_FUNCS.get(dialect, ((), frozenset()))
    if isinstance(inner, exp.Func):
        name = _func_name(inner)
        if not (isinstance(inner, _classes) if _classes else False) and (
            name not in names
        ):
            raise QueryRefusedError(
                f"ARRAY JOIN over '{name}' is not allowed; unnest a column of a "
                "table the query already reads, bare or through an allowed "
                "JSON/array function"
            )
    taint = _expr_taint(inner, sources, known, tables, dialect)
    return node.alias.lower(), {node.alias.lower(): taint}


def _resolve_dataset(
    dotted: str, known: dict[str, Dataset], tables: set[str]
) -> Dataset:
    matches = match_identifier(dotted, list(known))
    if not matches:
        raise QueryRefusedError(
            f"'{dotted}' is not in the exploration cache; run `explore map` (or "
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
            return {
                _Taint(
                    label,
                    col.pii.confidence,
                    dataset.identifier,
                    col.name,
                    col.pii.category,
                )
            }
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
    if isinstance(node, exp.Func) and _measures(node):
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
        _refuse_table_function(node, dialect)
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


def _refuse_table_function(node: exp.Table, dialect: str) -> None:
    """Refuse ``TABLE(fn(...))`` by name.

    sqlglot parses the standard table-function call as a table whose body is an
    anonymous ``TABLE`` call. It was already refused as an unnest of nothing
    permitted, which is the right decision for the wrong reason: the refusal has
    to say what was actually asked. On Trino the table functions include
    ``system.query``, which sends its argument to a federated database as opaque
    SQL, and the polymorphic ones that reshape a whole relation, so none of them
    is a source the cache profiled. A Postgres set-returning function in FROM is
    a different shape (the function itself is the table body) and is not
    touched here.
    """

    body = node.this
    if not (isinstance(body, exp.Anonymous) and str(body.this).upper() == "TABLE"):
        return
    called = body.expressions[0].sql(dialect=dialect) if body.expressions else ""
    name = called.split("(", 1)[0] or "TABLE(...)"
    raise QueryRefusedError(
        f"table function {name} is not permitted: TABLE(...) returns rows the "
        "exploration cache never profiled, and some table functions pass their "
        "argument to another database unread. Query cached tables and views, or "
        "unnest a column of one with UNNEST(...)"
    )


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


class _ScopedSources(dict):
    """A subquery's own sources, with the enclosing query's behind them."""

    def __init__(self, own: dict[str, _Source], *, outer: dict[str, _Source]):
        super().__init__(own)
        self.outer = outer


def _resolve_column(node: exp.Column, sources: dict[str, _Source]) -> set[_Taint]:
    name = node.name
    qualifier = node.table.lower() if node.table else ""
    outer = getattr(sources, "outer", None)

    if qualifier:
        if qualifier not in sources and outer is not None:
            return _resolve_column(node, outer)
        if qualifier not in sources:
            raise QueryRefusedError(
                f"unknown table or alias '{node.table}' for column "
                f"'{node.table}.{name}'"
            )
        return _source_column_taint(sources[qualifier], name)

    hits = [source for source in sources.values() if _source_has_column(source, name)]
    if not hits and outer is not None:
        return _resolve_column(node, outer)
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


def _measures(node: exp.Func) -> bool:
    """Whether a function cuts the value path from its arguments to its result.

    A named measuring aggregate does. So does a ClickHouse combinator over one,
    which is why the suffixes are peeled here rather than enumerated as names:
    the family is open (`sumIfOrNull`), and enumerating it would leave the next
    spelling refused for no reason while stripping unknown suffixes would leave
    it accepted for no reason. Peeling only the suffixes that provably preserve
    the base's measuring property keeps the fail-closed default intact.
    """

    name = _func_name(node)
    if name in _MEASURING:
        return True
    if not isinstance(node, exp.CombinedAggFunc):
        return False
    peeled = True
    while peeled:
        peeled = False
        for suffix in _AGG_COMBINATORS:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                peeled = True
                break
    return name in _MEASURING


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
