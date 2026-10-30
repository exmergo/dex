"""A governed metric query, from dex's grammar to a Cube query and back.

Pure functions, no I/O: :mod:`.transport` asks the server, and
:class:`~..explore.semantic.cube.LocalCubeLayer` runs the order. What lives here
is everything that decides whether what came back can be executed by dex:

- :func:`to_cube_query` and :func:`to_cube_values_query` translate the request
  into Cube's query JSON over the members the repository's models declare;
- :func:`check_renderer` holds the server's ``/v1/meta`` to those models, member
  by member, so a server still serving yesterday's files is refused rather than
  trusted (Cube Core outside dev mode does not reload an edited model);
- :func:`check_plan` refuses a ``/v1/sql`` answer that is not one whole
  statement dex can run: several statements, a pre-aggregation or an external
  rollup, a lambda query, another data source;
- :func:`bind_params` writes Cube's parameters into the statement as literals of
  dex's own dialect, refusing a placeholder style that says the server renders
  for another warehouse;
- :func:`result_names` maps the aliases Cube emits back to the tokens the caller
  wrote.

**The CLI dialect.** Members are ``cube.member`` (or ``view.member``). Cube has
no ``metric_time``: a time grouping names its time dimension with the grain
suffix (``orders.ordered_at__month``), or names the dimension and passes
``--grain``. ``--where`` is Cube's JSON filter (:mod:`...cube_dialect`).
``--order-by`` takes only the query's own members, ``-`` for descending.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..cube_dialect import filter_refs, member_of, parse_where
from ..errors import DexError
from .model import MEASURE, Cube, Member
from .validator import PREDEFINED_GRANULARITIES

#: The connectors a Cube-rendered statement may run on in this release, each
#: with the placeholder style Cube renders for it and the quote its identifiers
#: carry. Both were recorded from Cube Core v1.8.14 (`unversioned-docs` spike);
#: a statement in another style was rendered for another warehouse.
CUBE_CONNECTORS: dict[str, tuple[str, str]] = {
    "duckdb": ("?", '"'),
    "postgres": ("$", '"'),
    "bigquery": ("?", "`"),
    "snowflake": ("?", '"'),
    "redshift": ("$", '"'),
}

#: Why each connector outside :data:`CUBE_CONNECTORS` is refused, by name.
UNSUPPORTED_CONNECTORS: dict[str, str] = {
    "databricks": "its rendered SQL has no reviewed golden yet",
    "clickhouse": "its rendered SQL has no reviewed golden yet",
    "trino": "its rendered SQL has no reviewed golden yet",
}

# Dialects whose string literals take a backslash escape, for the scanner that
# finds placeholders outside quoted text.
_BACKSLASH_ESCAPES = {"bigquery", "snowflake", "redshift"}


class CubeQueryError(DexError, ValueError):
    """A request this release cannot send to Cube, or a plan it cannot run."""


class CubeRefusalError(CubeQueryError):
    """A request understood and deliberately not run (``reason: guard``)."""


class StaleRendererError(CubeRefusalError):
    """The Cube server serves a different model than the repository's files."""


@dataclass(frozen=True)
class Target:
    """What a ``cube.member`` or ``view.member`` token is."""

    token: str
    kind: str
    member: Member
    #: The cube the member lives in: its own, or the one a view member exposes.
    cube: str
    #: ``cube.member`` behind a view member, None for a cube member.
    alias_of: str | None = None


@dataclass
class CubeRequest:
    """The Cube query, and what the layer needs to govern it."""

    query: dict[str, Any]
    #: Every member token the query names, for the stale check.
    members: list[str]
    #: The cubes those members live in, for the data-source check.
    cubes: list[str]
    #: ``member`` or ``member.grain`` as Cube names a result -> the caller's token.
    names: dict[str, str] = field(default_factory=dict)


def index(cubes: Sequence[Cube], views: Sequence[Any]) -> dict[str, Target]:
    """Every queryable token in the repository's models."""

    found: dict[str, Target] = {}
    for cube in cubes:
        for member in (*cube.dimensions, *cube.measures):
            token = f"{cube.name}.{member.name}"
            found[token] = Target(token, member.kind, member, cube.name)
    for view in views:
        for exposed in view.members:
            token = f"{view.name}.{exposed.name}"
            found[token] = Target(
                token, exposed.kind, exposed.member, exposed.cube, exposed.source
            )
    return found


def grains_of(target: Target) -> tuple[str, ...]:
    return (*PREDEFINED_GRANULARITIES, *target.member.granularities)


def split_time(token: str, members: Mapping[str, Target]) -> tuple[str, str | None]:
    """``orders.ordered_at__month`` as ``(orders.ordered_at, month)``.

    A suffix is a grain only where the head is a time dimension that has it, so
    a member whose own name holds ``__`` is never cut in two.
    """

    if "__" in token:
        head, tail = token.rsplit("__", 1)
        target = members.get(head)
        if (
            target is not None
            and target.member.type == "time"
            and tail.lower() in [grain.lower() for grain in grains_of(target)]
        ):
            return head, tail.lower()
    return token, None


def _require(token: str, members: Mapping[str, Target], what: str) -> Target:
    target = members.get(token)
    if target is None:
        raise CubeQueryError(
            f"no such {what} in this Cube layer: {token}. List what it exposes with "
            "`explore semantic list`; a member is named cube.member (orders.status)"
        )
    return target


def to_cube_query(
    q: Any,
    members: Mapping[str, Target],
    *,
    max_rows: int,
) -> CubeRequest:
    """The Cube query for a :class:`~..explore.semantic.model.SemanticQuery`."""

    request = CubeRequest(query={}, members=[], cubes=[])
    measures: list[str] = []
    for token in q.metrics:
        target = _require(token, members, "metric")
        if target.kind != MEASURE:
            raise CubeQueryError(
                f"{token} is a dimension, not a measure; pass it to --group-by"
            )
        measures.append(token)
        request.names[token] = token

    grain = (q.grain or "").lower() or None
    dimensions: list[str] = []
    times: list[dict[str, str]] = []
    for token in q.group_by:
        name, suffix = split_time(token, members)
        target = _require(name, members, "dimension")
        if target.kind == MEASURE:
            raise CubeQueryError(f"{name} is a measure; pass it as a metric")
        if target.member.type != "time":
            dimensions.append(name)
            request.names[name] = token
            continue
        if suffix and grain and suffix != grain:
            raise CubeQueryError(
                f"{token} buckets by {suffix} and --grain asks for {grain}; name one"
            )
        bucket = suffix or grain
        if bucket is None:
            dimensions.append(name)
            request.names[name] = token
            continue
        if bucket not in [g.lower() for g in grains_of(target)]:
            raise CubeQueryError(
                f"unknown time grain '{bucket}' for {name}; Cube reports "
                f"{', '.join(grains_of(target))}"
            )
        times.append({"dimension": name, "granularity": bucket})
        request.names[f"{name}.{bucket}"] = token if suffix else f"{name}__{bucket}"
    if grain and not times:
        raise CubeQueryError(
            f"--grain {grain} needs a time dimension to bucket, and Cube has no "
            "metric_time: name it in --group-by with the grain suffix "
            f"(orders.ordered_at__{grain})"
        )

    filters = parse_where(list(q.where)) if q.where else []
    for item in _leaves(filters):
        _require(member_of(item) or "", members, "member")
    filters = _date_ranges(filters, times)

    # What an --order-by token may name, as written -> the member Cube orders by.
    # A bucketed time dimension is ordered by the dimension itself.
    own = {token: token for token in (*measures, *dimensions)}
    for entry in times:
        name = entry["dimension"]
        own[name] = name
        own[f"{name}__{entry['granularity']}"] = name
    order: list[list[str]] = []
    for raw in q.order_by:
        descending = raw.startswith("-")
        token = raw[1:] if descending else raw
        member = own.get(token)
        if member is None:
            raise CubeQueryError(
                f"--order-by {token} is not one of this query's own metrics or "
                "grouped members; ordering by anything else would read a member "
                "nothing screened"
            )
        order.append([member, "desc" if descending else "asc"])

    limit = q.limit if q.limit and q.limit <= max_rows else max_rows + 1
    query: dict[str, Any] = {"measures": measures, "limit": limit, "timezone": "UTC"}
    if dimensions:
        query["dimensions"] = dimensions
    if times:
        query["timeDimensions"] = times
    if filters:
        query["filters"] = filters
    if order:
        query["order"] = order
    request.query = query
    named = [
        *measures,
        *dimensions,
        *(entry["dimension"] for entry in times),
        *(filter_refs(filters) or []),
    ]
    request.members = list(dict.fromkeys(named))
    request.cubes = list(dict.fromkeys(members[t].cube for t in request.members))
    return request


def to_cube_values_query(
    dimension: str,
    grain: str | None,
    metrics: Sequence[str],
    members: Mapping[str, Target],
    *,
    max_rows: int,
) -> CubeRequest:
    """A dimension-only query: the values present for ``dimension``.

    Rooted at the dimension's own cube, so the answer is the column's domain;
    with ``metrics``, the measures are added and the answer is the values
    present for them, which ``scoped_to`` reports.
    """

    target = _require(dimension, members, "dimension")
    if target.kind == MEASURE:
        raise CubeQueryError(f"{dimension} is a measure, which has no value domain")
    request = CubeRequest(query={}, members=[dimension], cubes=[target.cube])
    query: dict[str, Any] = {"limit": max_rows + 1, "timezone": "UTC"}
    if grain:
        if target.member.type != "time" or grain not in [
            g.lower() for g in grains_of(target)
        ]:
            reported = (
                ", ".join(grains_of(target))
                if target.member.type == "time"
                else "none for a dimension that is not a time"
            )
            raise CubeQueryError(
                f"{dimension} has no grain '{grain}'; Cube reports {reported}"
            )
        query["timeDimensions"] = [{"dimension": dimension, "granularity": grain}]
        request.names[f"{dimension}.{grain}"] = f"{dimension}__{grain}"
    else:
        query["dimensions"] = [dimension]
        request.names[dimension] = dimension
    measures: list[str] = []
    for token in metrics:
        metric = _require(token, members, "metric")
        if metric.kind != MEASURE:
            raise CubeQueryError(f"--metric {token} is a dimension, not a measure")
        measures.append(token)
        request.names[token] = token
    if measures:
        query["measures"] = measures
        request.members += measures
        request.cubes = list(
            dict.fromkeys([*request.cubes, *(members[t].cube for t in measures)])
        )
    request.query = query
    return request


def _date_ranges(
    filters: list[dict[str, Any]], times: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Move a top-level ``inDateRange`` on a bucketed time dimension into that
    dimension's ``dateRange``, and return the filters that remain.

    The same condition either way for an ordinary measure, since the top-level
    filters and the ``dateRange`` are all ANDed. Not for a rolling window: Cube
    builds its time series from the ``dateRange``, and given only a filter it
    spans every bucket in the table, which is a different answer. A range on a
    time dimension that is not grouped, or one inside an ``or``, stays a filter.
    """

    bucketed = {entry["dimension"]: entry for entry in times}
    kept: list[dict[str, Any]] = []
    for item in filters:
        entry = bucketed.get(member_of(item) or "")
        values = item.get("values")
        if (
            entry is not None
            and "dateRange" not in entry
            and item.get("operator") == "inDateRange"
            and isinstance(values, list)
            and len(values) in (1, 2)
        ):
            entry["dateRange"] = values[0] if len(values) == 1 else list(values)
            continue
        kept.append(item)
    return kept


def _leaves(filters: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for item in filters:
        if not isinstance(item, dict):
            continue
        for key in ("and", "or"):
            if isinstance(item.get(key), list):
                yield from _leaves(item[key])
                break
        else:
            yield item


# ---- the renderer is not a reviewed artifact ----------------------------------------


def _norm(sql: str | None) -> str:
    """A member's SQL as both sides spell it: the YAML compiler wraps it in a
    template literal (``\\`total\\```) and writes ``{CUBE}`` as ``${CUBE}``."""

    text = (sql or "").strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1]
    text = text.replace("${", "{")
    return re.sub(r"\s+", " ", text).strip()


def check_renderer(
    meta: Mapping[str, Any],
    tokens: Sequence[str],
    members: Mapping[str, Target],
    *,
    where: str,
    cubes: Mapping[str, Cube] | None = None,
) -> None:
    """Refuse unless the server's model agrees with the repository's on every
    member the query names: present, the same kind, the same aggregation or
    type, the same SQL and filters, the same custom granularities, and, for a
    view member, the same cube member behind it.

    With ``cubes``, the members those reach through ``{references}`` are
    compared too, since a calculated measure whose own SQL is unchanged still
    means something else when a measure it reads changed. A referenced member
    Cube's API does not expose (a primary key, a ``public: false`` member) has
    nothing to compare against and is skipped; a named one must be served.
    """

    served: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for element in meta.get("cubes") or []:
        for kind, key in (("measure", "measures"), ("dimension", "dimensions")):
            for entry in element.get(key) or []:
                if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                    served[entry["name"]] = (kind, entry)

    def stale(token: str, why: str) -> StaleRendererError:
        return StaleRendererError(
            f"refused: the Cube server at {where} serves a different model than "
            f"this repository: {token} {why}. A Cube server outside dev mode does "
            "not reload an edited model file, so restart it on the repository's "
            "current files (or point semantic.cube.url at one that is)"
        )

    reached = referenced(tokens, members, cubes) if cubes else []
    for token in reached:
        target = members.get(token)
        if target is None or token in tokens:
            continue
        if token not in served and (
            target.member.primary_key or target.member.public is False
        ):
            continue
        _compare(token, target.member, served.get(token), stale)

    for token in tokens:
        target = members[token]
        found = served.get(token)
        if found is None:
            raise stale(token, "is not in the server's /v1/meta")
        kind, entry = found
        if kind != target.kind:
            raise stale(token, f"is a {kind} there and a {target.kind} here")
        if target.alias_of is not None:
            if entry.get("aliasMember") not in (None, target.alias_of):
                raise stale(
                    token,
                    f"exposes {entry.get('aliasMember')} there and "
                    f"{target.alias_of} here",
                )
            behind = members.get(target.alias_of)
            if behind is not None and target.alias_of not in tokens:
                _compare(
                    target.alias_of, behind.member, served.get(target.alias_of), stale
                )
            continue
        _compare(token, target.member, found, stale)


def referenced(
    tokens: Sequence[str], members: Mapping[str, Target], cubes: Mapping[str, Cube]
) -> list[str]:
    """Every cube member ``tokens`` reach through ``{references}`` in their SQL
    and filters, followed transitively, in the order first reached."""

    from .references import resolve, scan

    found: list[str] = []
    pending = [members[t] for t in tokens if t in members]
    seen: set[tuple[str, str]] = set()
    while pending:
        target = pending.pop(0)
        cube = cubes.get(target.cube)
        if cube is None or (cube.name, target.member.name) in seen:
            continue
        seen.add((cube.name, target.member.name))
        for sql in (target.member.sql or "", *target.member.filters):
            holes, problem = scan(sql)
            if problem:
                continue
            for hole in holes:
                resolved, _ = resolve(hole, cube, cubes)
                if resolved is None or resolved.kind != "member":
                    continue
                token = f"{resolved.cube}.{resolved.member}"
                if token in members and token not in found:
                    found.append(token)
                    pending.append(members[token])
    return found


def _compare(token: str, member: Member, found: Any, stale: Any) -> None:
    if found is None:
        raise stale(token, "is not in the server's /v1/meta")
    kind, entry = found
    if kind == MEASURE:
        if entry.get("aggType") not in (None, member.type):
            raise stale(
                token,
                f"aggregates as {entry.get('aggType')} there and {member.type} here",
            )
        served_filters = sorted(_norm(f.get("sql")) for f in entry.get("filters") or [])
        if served_filters != sorted(_norm(f) for f in member.filters):
            raise stale(token, "has different filters there")
    elif entry.get("type") not in (None, member.type):
        raise stale(token, f"is a {entry.get('type')} there and a {member.type} here")
    if (
        "sql" in entry
        and member.sql is not None
        and _norm(entry.get("sql")) != _norm(member.sql)
    ):
        raise stale(token, "has different SQL there")
    if member.type == "time":
        served_grains = sorted(
            g.get("name")
            for g in entry.get("granularities") or []
            if isinstance(g, Mapping)
        )
        if served_grains != sorted(member.granularities):
            raise stale(token, "has different custom granularities there")


# ---- the plan has to be the whole answer ---------------------------------------------


@dataclass(frozen=True)
class Plan:
    sql: str
    params: list[Any]
    aliases: dict[str, str]


def check_plan(body: Any, *, data_source: str) -> Plan:
    """The one statement a ``/v1/sql`` answer carries, or refuse."""

    plan = body.get("sql") if isinstance(body, Mapping) else None
    if isinstance(body, list) or not isinstance(plan, Mapping):
        raise CubeRefusalError(
            "refused: Cube planned this query as more than one statement "
            "(a compareDateRange, or a blended query), which dex cannot run as one "
            "governed SELECT"
        )
    rendered = plan.get("sql")
    if (
        not isinstance(rendered, list)
        or len(rendered) != 2
        or not isinstance(rendered[0], str)
        or not isinstance(rendered[1], list)
    ):
        raise CubeQueryError("Cube's /v1/sql answer carries no [sql, params] pair")
    used = [
        str(p.get("preAggregationId") or p.get("tableName") or "?")
        for p in plan.get("preAggregations") or []
        if isinstance(p, Mapping)
    ]
    if used or plan.get("preAggregations"):
        raise CubeRefusalError(
            "refused: Cube answered this query from a pre-aggregation "
            f"({', '.join(used) or 'unnamed'}), a rollup table Cube builds and "
            "refreshes itself. Dex runs only SQL over the tables the models name, "
            "so it cannot use it. Query members the rollup does not cover, or remove "
            "the pre-aggregation from the models dex reads"
        )
    if plan.get("external"):
        raise CubeRefusalError(
            "refused: Cube planned this query against an external store (Cube "
            "Store), not the warehouse dex is connected to"
        )
    if plan.get("lambdaQueries"):
        raise CubeRefusalError(
            "refused: Cube planned this query as a lambda pre-aggregation, which "
            "unions a rollup with live data inside Cube"
        )
    served = plan.get("dataSource") or body.get("dataSource") or "default"
    if served != data_source:
        raise CubeRefusalError(
            f"refused: Cube planned this query on data source '{served}', and dex "
            f"is connected to '{data_source}' (semantic.cube.data_source)"
        )
    aliases = plan.get("aliasNameToMember") or {}
    return Plan(
        sql=rendered[0],
        params=list(rendered[1]),
        aliases={str(k): str(v) for k, v in aliases.items()},
    )


def check_data_sources(
    cubes: Sequence[str], declared: Mapping[str, str | None], data_source: str
) -> None:
    """Every cube a query reaches must be on the data source dex is connected to."""

    others = [
        f"{name} ({declared.get(name) or 'default'})"
        for name in cubes
        if (declared.get(name) or "default") != data_source
    ]
    if others:
        raise CubeRefusalError(
            f"refused: this query reaches {', '.join(others)}, on another Cube "
            f"data source than '{data_source}' (semantic.cube.data_source), the "
            "warehouse dex is connected to; this release runs one data source"
        )


# ---- binding Cube's parameters into dex's dialect ------------------------------------


def _placeholders(sql: str, dialect: str) -> list[tuple[int, int, str]]:
    """``(start, end, text)`` of every ``?`` and ``$n`` outside quoted text.

    A scanner rather than sqlglot's tokenizer, which reads ``?::timestamptz`` as
    something other than a placeholder in some dialects; Cube's output uses no
    dollar quoting, so quotes and comments are all there is to step over.
    """

    found: list[tuple[int, int, str]] = []
    backslash = dialect in _BACKSLASH_ESCAPES
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char in "'\"`":
            j = i + 1
            while j < n:
                if backslash and char == "'" and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == char:
                    if j + 1 < n and sql[j + 1] == char:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if char == "?":
            found.append((i, i + 1, "?"))
        elif char == "$":
            match = re.match(r"\$(\d+)", sql[i:])
            if match:
                found.append((i, i + match.end(), match.group(0)))
                i += match.end()
                continue
        i += 1
    return found


def _literal(value: Any, dialect: str) -> str:
    from .lineage import string_literal

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return string_literal(value, dialect)
    raise CubeQueryError(
        f"Cube rendered a parameter of type {type(value).__name__}, which dex "
        "cannot write as a literal"
    )


def bind_params(sql: str, params: Sequence[Any], connector: str, dialect: str) -> str:
    """The statement with Cube's parameters written in as ``dialect`` literals.

    dex runs one statement with no bind variables, so each placeholder becomes a
    literal of its own value, quoted by the dialect's own rules. The style and
    the identifier quote are checked against what Cube renders for
    ``connector``: a statement in another style was rendered for another
    warehouse, and would run here and mean something else.
    """

    style, quote = CUBE_CONNECTORS[connector]
    other = "`" if quote == '"' else '"'
    if re.search(rf"\bAS\s+{re.escape(other)}", sql) and not re.search(
        rf"\bAS\s+{re.escape(quote)}", sql
    ):
        raise CubeRefusalError(_dialect_mismatch(connector))
    marks = _placeholders(sql, dialect)
    if any(text[0] != style for _, _, text in marks):
        raise CubeRefusalError(_dialect_mismatch(connector))
    if style == "?":
        if len(marks) != len(params):
            raise CubeQueryError(
                f"Cube rendered {len(marks)} placeholder(s) for {len(params)} "
                "parameter(s); dex will not guess which value goes where"
            )
        values = list(params)
    else:
        indices = [int(text[1:]) for _, _, text in marks]
        if any(not 1 <= index <= len(params) for index in indices) or (
            params and set(indices) != set(range(1, len(params) + 1))
        ):
            raise CubeQueryError(
                "Cube's numbered placeholders do not match its parameters; dex will "
                "not guess which value goes where"
            )
        values = [params[index - 1] for index in indices]
    out: list[str] = []
    cursor = 0
    for (start, end, _), value in zip(marks, values, strict=True):
        out.append(sql[cursor:start])
        out.append(_literal(value, dialect))
        cursor = end
    out.append(sql[cursor:])
    return "".join(out)


def _dialect_mismatch(connector: str) -> str:
    return (
        f"refused: the Cube server rendered SQL for another warehouse than "
        f"{connector}, dex's connector. Run the Cube server with the database type "
        f"of the warehouse dex queries (CUBEJS_DB_TYPE={connector})"
    )


def result_names(
    aliases: Mapping[str, str], names: Mapping[str, str]
) -> dict[str, str]:
    """Cube's result alias -> the token the caller wrote, keyed case-insensitively
    too, for a warehouse that folds a quoted alias (Redshift)."""

    mapped: dict[str, str] = {}
    for alias, member in aliases.items():
        token = names.get(member)
        if token is None:
            continue
        mapped[alias] = token
        mapped.setdefault(alias.lower(), token)
        mapped.setdefault(alias.upper(), token)
    return mapped
