"""dex's port of Cube's model rules, for the fields dex reads.

Cube publishes no JSON Schema for its data model. The authoritative validator is
the schema compiler's Joi rules (`packages/cubejs-schema-compiler/src/compiler/
CubeValidator.ts`), applied after the YAML compiler camelizes every key. This
module ports those rules, pinned to the release
:data:`~.dependency.CUBE_VALIDATOR_RELEASE` names, for every field the catalog,
the declarations and the fingerprint read: names and identifiers, ``sql`` versus
``sql_table``, the member type enumerations, join relationships, granularities,
rolling windows, and a view's ``cubes``. Each rule is a row in a table here, so
authoring (#911) validates a prospective file with exactly these rules.

**What is a warning rather than a refusal.** Cube refuses any key its schema
does not declare. dex only warns: a key this port does not know may be one a
newer Cube added, and refusing a cube for it would hide every definition the
cube does carry. Authoring escalates the same finding to a refusal, because
there dex is the one writing the key.

Structure only, one element at a time. Whether a join's target exists, whether
a ``{member}`` resolves, and whether a view's ``join_path`` follows declared
joins are questions about the whole set, and :mod:`.loader` asks them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .model import (
    DIMENSION,
    MEASURE,
    SEGMENT,
    Cube,
    Join,
    Member,
    View,
    ViewInclude,
    ViewSource,
)

__all__ = [
    "DIMENSION_TYPES",
    "ERROR",
    "MEASURE_TYPES",
    "PREDEFINED_GRANULARITIES",
    "RELATIONSHIPS",
    "WARNING",
    "Finding",
    "canonical_key",
    "check_cube",
    "check_view",
    "is_identifier",
]

ERROR = "error"
WARNING = "warning"

_IDENTIFIER = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")

#: `BaseDimensionWithoutSubQuery.type`, plus the separate `switch` schema.
DIMENSION_TYPES = frozenset({"string", "number", "boolean", "time", "geo", "switch"})

#: `measureTypeWithCount`, snake_cased (Cube camelizes a type with an underscore,
#: so `count_distinct` and `countDistinct` are one type).
MEASURE_TYPES = frozenset(
    {
        "count",
        "number",
        "string",
        "boolean",
        "time",
        "sum",
        "avg",
        "min",
        "max",
        "count_distinct",
        "count_distinct_approx",
    }
)

#: `multiStageMeasureType`: the types a `multi_stage: true` measure may also take.
MULTI_STAGE_MEASURE_TYPES = MEASURE_TYPES | {"number_agg", "rank"}

#: Measure types whose `sql` is optional: a count counts rows without one.
_SQL_OPTIONAL_MEASURES = frozenset({"count"})

#: Every spelling `baseSchema.joins[].relationship` accepts, canonicalized.
RELATIONSHIPS: dict[str, str] = {
    "belongs_to": "many_to_one",
    "many_to_one": "many_to_one",
    "has_many": "one_to_many",
    "one_to_many": "one_to_many",
    "has_one": "one_to_one",
    "one_to_one": "one_to_one",
}

#: Cube's built-in time granularities, in the order a reader expects them.
PREDEFINED_GRANULARITIES = (
    "second",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "quarter",
    "year",
)

#: `rollingWindow.type`: `fixed` by default.
_ROLLING_WINDOW_TYPES = frozenset(
    {"fixed", "year_to_date", "quarter_to_date", "month_to_date", "to_date"}
)

_TIME_INTERVAL = re.compile(r"^(-?\d+) (minute|hour|day|week|month|quarter|year)s?$")
_GRANULARITY_INTERVAL = re.compile(
    r"^\d+\s+(second|minute|hour|day|week|month|quarter|year)s?"
    r"(\s\d+\s+(second|minute|hour|day|week|month|quarter|year)s?){0,7}$"
)

# The keys each level of Cube's schema declares, snake_cased. A key outside the
# set is a warning (see the module docstring).
_COMMON_KEYS = frozenset(
    {
        "name",
        "extends",
        "title",
        "sql_alias",
        "data_source",
        "description",
        "rewrite_queries",
        "shown",
        "public",
        "meta",
        "joins",
        "measures",
        "dimensions",
        "segments",
        "pre_aggregations",
        "access_policy",
        "hierarchies",
        "refresh_key",
    }
)
CUBE_KEYS = _COMMON_KEYS | {"sql", "sql_table", "calendar"}
VIEW_KEYS = _COMMON_KEYS | {
    "is_view",
    "view_group",
    "view_groups",
    "cubes",
    "folders",
    "default_filters",
}
_DIMENSION_KEYS = frozenset(
    {
        "name",
        "aliases",
        "type",
        "field_type",
        "values_as_segments",
        "primary_key",
        "shown",
        "public",
        "title",
        "description",
        "suggest_filter_values",
        "enable_suggestions",
        "links",
        "mask",
        "format",
        "currency",
        "meta",
        "order",
        "key",
        "key_reference",
        "values",
        "granularities",
        "sub_query",
        "propagate_filters_to_sub_query",
        "sql",
        "case",
        "multi_stage",
        "latitude",
        "longitude",
        "add_group_by",
        "filter",
        "time_shift",
    }
)
_MEASURE_KEYS = frozenset(
    {
        "name",
        "aliases",
        "format",
        "currency",
        "public",
        "visible",
        "shown",
        "cumulative",
        "mask",
        "filters",
        "title",
        "description",
        "rolling_window",
        "drill_member_references",
        "drill_members",
        "drill_filters",
        "meta",
        "type",
        "sql",
        "multi_stage",
        "case",
        "group_by",
        "reduce_by",
        "add_group_by",
        "filter",
        "grain",
        "time_shift",
        "order_by",
    }
)
_SEGMENT_KEYS = frozenset(
    {"name", "aliases", "sql", "title", "description", "meta", "shown", "public"}
)
_JOIN_KEYS = frozenset({"name", "alias", "sql", "relationship"})
_VIEW_CUBE_KEYS = frozenset(
    {"join_path", "prefix", "split", "alias", "includes", "excludes"}
)
_VIEW_INCLUDE_KEYS = frozenset(
    {"name", "alias", "title", "description", "format", "meta"}
)
_GRANULARITY_KEYS = frozenset({"name", "title", "interval", "origin", "offset", "sql"})

#: Cube-level keys the catalog has no home for, named so a caller is told which
#: parts of a cube the catalog does not carry rather than finding them missing.
UNMAPPED_KEYS = (
    "segments",
    "pre_aggregations",
    "access_policy",
    "hierarchies",
    "folders",
    "default_filters",
    "refresh_key",
)


@dataclass(frozen=True)
class Finding:
    """One problem with one element: where, which rule, how bad, and why."""

    element: str
    rule: str
    severity: str
    message: str


def is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


def canonical_key(key: str) -> str:
    """A key as snake_case, the spelling Cube's YAML documentation uses.

    Cube camelizes every key before validating, so either spelling is the same
    key to it; normalizing the other way keeps dex's tables readable.
    """

    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()


def _canonical(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {canonical_key(str(key)): value for key, value in raw.items()}


def _canonical_value(value: Any) -> Any:
    """An enumeration value as snake_case (`countDistinct` is `count_distinct`)."""

    return canonical_key(value) if isinstance(value, str) else value


class _Collector:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, element: str, rule: str, message: str) -> None:
        self.findings.append(Finding(element, rule, ERROR, message))

    def warning(self, element: str, rule: str, message: str) -> None:
        self.findings.append(Finding(element, rule, WARNING, message))

    def unknown(self, element: str, raw: Mapping[str, Any], known: frozenset) -> None:
        for key in raw:
            if key not in known:
                self.warning(
                    element,
                    "unknown_key",
                    f"`{key}` is not a key Cube {_release()} declares here; dex "
                    "ignores it, and Cube refuses a model carrying it",
                )

    @property
    def failed(self) -> bool:
        return any(f.severity == ERROR for f in self.findings)


def _release() -> str:
    from .dependency import CUBE_VALIDATOR_RELEASE

    return CUBE_VALIDATOR_RELEASE


def _string(
    found: _Collector, element: str, raw: Mapping[str, Any], key: str
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        found.error(
            element, "type", f"`{key}` has to be a string, got {type(value).__name__}"
        )
        return None
    return value


def _boolean(
    found: _Collector, element: str, raw: Mapping[str, Any], key: str
) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        # Joi's `.strict()`: the string "true" is not a boolean.
        found.error(element, "type", f"`{key}` has to be true or false, got {value!r}")
        return None
    return value


def _named_list(
    found: _Collector,
    element: str,
    raw: Mapping[str, Any],
    key: str,
    what: str,
) -> list[tuple[str, dict[str, Any]]]:
    """A YAML member list as `(name, entry)` pairs, refusing what Cube refuses.

    The YAML compiler requires each section to be a list (`yamlArrayToObj`),
    each entry to carry a `name`, and no name twice within one section.
    """

    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        found.error(element, "type", f"`{key}` must be defined as a list of {what}s")
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        at = f"{element}.{key}[{index}]"
        if not isinstance(entry, Mapping):
            found.error(at, "type", f"a {what} is a mapping with a `name`")
            continue
        canonical = _canonical(entry)
        name = canonical.get("name")
        if not isinstance(name, str) or not is_identifier(name):
            found.error(
                at,
                "name",
                f"a {what} needs a `name` that is an identifier "
                f"([_a-zA-Z][_a-zA-Z0-9]*), got {name!r}",
            )
            continue
        if name in seen:
            found.error(
                f"{element}.{key}['{name}']",
                "duplicate",
                f"{what} names are unique within a cube, and '{name}' is declared "
                "twice",
            )
            continue
        seen.add(name)
        entries.append((name, canonical))
    return entries


# --- members -------------------------------------------------------------------


def _dimension(found: _Collector, at: str, raw: dict[str, Any]) -> Member | None:
    found.unknown(at, raw, _DIMENSION_KEYS)
    kind = _canonical_value(raw.get("type"))
    if kind not in DIMENSION_TYPES:
        found.error(
            at,
            "dimension_type",
            f"`type` must be one of {', '.join(sorted(DIMENSION_TYPES))}, got "
            f"{raw.get('type')!r}",
        )
        return None
    sql = _string(found, at, raw, "sql")
    case = raw.get("case") is not None or (
        raw.get("latitude") is not None and raw.get("longitude") is not None
    )
    values: tuple[str, ...] = ()
    if kind == "switch":
        listed = raw.get("values")
        if (
            not isinstance(listed, list)
            or not listed
            or not all(isinstance(v, str) for v in listed)
        ):
            found.error(
                at, "switch_values", "a `switch` dimension needs a list of `values`"
            )
            return None
        values = tuple(listed)
    elif raw.get("values") is not None:
        found.error(at, "switch_values", "`values` is only valid on a switch dimension")
    elif sql is None and not case:
        found.error(
            at,
            "dimension_sql",
            "a dimension needs `sql`, a `case`, or (for geo) `latitude` and "
            "`longitude`",
        )
        return None
    granularities = _granularities(found, at, raw, kind)
    return Member(
        name=raw["name"],
        kind=DIMENSION,
        type=kind,
        sql=sql,
        title=_string(found, at, raw, "title"),
        description=_string(found, at, raw, "description"),
        primary_key=bool(_boolean(found, at, raw, "primary_key")),
        public=_public(found, at, raw),
        granularities=granularities,
        multi_stage=bool(_boolean(found, at, raw, "multi_stage")),
        case=case and sql is None,
        values=values,
        raw=raw,
    )


def _granularities(
    found: _Collector, at: str, raw: dict[str, Any], kind: str
) -> tuple[str, ...]:
    listed = raw.get("granularities")
    if listed is None:
        return ()
    if kind != "time":
        found.error(
            at, "granularities", "`granularities` is only valid on a time dimension"
        )
        return ()
    names: list[str] = []
    for name, entry in _named_list(found, at, raw, "granularities", "granularity"):
        where = f"{at}.granularities['{name}']"
        found.unknown(where, entry, _GRANULARITY_KEYS)
        interval, sql = entry.get("interval"), entry.get("sql")
        if sql is not None:
            if name.lower() not in PREDEFINED_GRANULARITIES:
                found.error(
                    where,
                    "granularity_sql",
                    "a granularity defined with `sql` must be named after a "
                    f"predefined granularity ({', '.join(PREDEFINED_GRANULARITIES)})"
                    "; define it with `interval` instead",
                )
                continue
        elif not isinstance(interval, str) or not _GRANULARITY_INTERVAL.match(interval):
            found.error(
                where,
                "granularity_interval",
                f"`interval` must read like `1 week` or `2 months`, got {interval!r}",
            )
            continue
        names.append(name)
    return tuple(names)


def _measure(found: _Collector, at: str, raw: dict[str, Any]) -> Member | None:
    found.unknown(at, raw, _MEASURE_KEYS)
    multi_stage = bool(_boolean(found, at, raw, "multi_stage"))
    allowed = MULTI_STAGE_MEASURE_TYPES if multi_stage else MEASURE_TYPES
    kind = _canonical_value(raw.get("type"))
    if kind not in allowed:
        found.error(
            at,
            "measure_type",
            f"`type` must be one of {', '.join(sorted(allowed))}, got "
            f"{raw.get('type')!r}",
        )
        return None
    sql = _string(found, at, raw, "sql")
    if sql is None and not multi_stage and kind not in _SQL_OPTIONAL_MEASURES:
        found.error(at, "measure_sql", f"a `{kind}` measure needs `sql`")
        return None
    filters: list[str] = []
    for index, entry in enumerate(raw.get("filters") or []):
        predicate = entry.get("sql") if isinstance(entry, Mapping) else None
        if not isinstance(predicate, str):
            found.error(
                f"{at}.filters[{index}]", "measure_filter", "a filter needs `sql`"
            )
            continue
        filters.append(predicate)
    rolling = _rolling_window(found, at, raw)
    return Member(
        name=raw["name"],
        kind=MEASURE,
        type=kind,
        sql=sql,
        title=_string(found, at, raw, "title"),
        description=_string(found, at, raw, "description"),
        public=_public(found, at, raw),
        filters=tuple(filters),
        rolling_window=rolling,
        multi_stage=multi_stage,
        case=raw.get("case") is not None and sql is None,
        raw=raw,
    )


def _rolling_window(
    found: _Collector, at: str, raw: dict[str, Any]
) -> dict[str, Any] | None:
    window = raw.get("rolling_window")
    if window is None:
        return None
    if not isinstance(window, Mapping):
        found.error(at, "rolling_window", "`rolling_window` is a mapping")
        return None
    window = _canonical(window)
    kind = _canonical_value(window.get("type") or "fixed")
    if kind not in _ROLLING_WINDOW_TYPES:
        found.error(
            at,
            "rolling_window",
            f"`rolling_window.type` must be one of "
            f"{', '.join(sorted(_ROLLING_WINDOW_TYPES))}, got {window.get('type')!r}",
        )
        return None
    if kind == "fixed":
        for side in ("trailing", "leading"):
            value = window.get(side)
            if (
                value is not None
                and value != "unbounded"
                and not (isinstance(value, str) and _TIME_INTERVAL.match(value))
            ):
                found.error(
                    at,
                    "rolling_window",
                    f"`rolling_window.{side}` must read like `7 days` or be "
                    f"`unbounded`, got {value!r}",
                )
                return None
    return {**window, "type": kind}


def _segment(found: _Collector, at: str, raw: dict[str, Any]) -> Member | None:
    found.unknown(at, raw, _SEGMENT_KEYS)
    sql = _string(found, at, raw, "sql")
    if sql is None:
        found.error(at, "segment_sql", "a segment needs `sql`")
        return None
    return Member(
        name=raw["name"],
        kind=SEGMENT,
        sql=sql,
        title=_string(found, at, raw, "title"),
        description=_string(found, at, raw, "description"),
        public=_public(found, at, raw),
        raw=raw,
    )


def _public(found: _Collector, at: str, raw: dict[str, Any]) -> bool | None:
    """`public`, or its deprecated spellings `shown` and (on measures) `visible`."""

    for key in ("public", "shown", "visible"):
        value = _boolean(found, at, raw, key)
        if value is not None:
            return value
    return None


def _members(
    found: _Collector,
    element: str,
    raw: dict[str, Any],
    key: str,
    what: str,
    build: Callable[[_Collector, str, dict[str, Any]], Member | None],
) -> list[Member]:
    members: list[Member] = []
    for name, entry in _named_list(found, element, raw, key, what):
        member = build(found, f"{element}.{key}['{name}']", entry)
        if member is not None:
            members.append(member)
    return members


# --- joins ---------------------------------------------------------------------


def _joins(found: _Collector, element: str, raw: dict[str, Any]) -> list[Join]:
    value = raw.get("joins")
    if value is None:
        return []
    if not isinstance(value, list):
        found.error(element, "type", "`joins` must be defined as a list")
        return []
    joins: list[Join] = []
    for index, entry in enumerate(value):
        at = f"{element}.joins[{index}]"
        if not isinstance(entry, Mapping):
            found.error(at, "type", "a join is a mapping with `name` and `sql`")
            continue
        entry = _canonical(entry)
        found.unknown(at, entry, _JOIN_KEYS)
        target = entry.get("name")
        if not isinstance(target, str) or not is_identifier(target):
            found.error(
                at,
                "name",
                f"a join's `name` is the cube it joins, an identifier; got {target!r}",
            )
            continue
        at = f"{element}.joins['{target}']"
        sql = entry.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            found.error(at, "join_sql", "a join needs `sql`")
            continue
        relationship = RELATIONSHIPS.get(_canonical_value(entry.get("relationship")))
        if relationship is None:
            found.error(
                at,
                "relationship",
                "`relationship` must be one of many_to_one, one_to_many or "
                "one_to_one (or the legacy belongs_to, has_many, has_one), got "
                f"{entry.get('relationship')!r}",
            )
            continue
        alias = entry.get("alias")
        if alias is not None and not is_identifier(alias):
            found.error(
                at, "alias", f"a join's `alias` is an identifier, got {alias!r}"
            )
            continue
        joins.append(
            Join(
                target=target,
                sql=sql,
                relationship=relationship,
                alias=alias,
                raw=entry,
            )
        )
    return joins


# --- cubes and views -----------------------------------------------------------


def _unmapped(raw: Mapping[str, Any]) -> list[str]:
    return [key for key in UNMAPPED_KEYS if raw.get(key)]


def check_cube(raw: Mapping[str, Any], file: str) -> tuple[Cube | None, list[Finding]]:
    """One cube, validated: the cube (or None if it is refused) and the findings.

    A cube using ``extends`` is never passed here: it is compiled-only, and the
    loader names it rather than validating half of it.
    """

    found = _Collector()
    raw = _canonical(raw)
    name = raw.get("name")
    if not isinstance(name, str) or not is_identifier(name):
        found.error(
            "cubes[?]",
            "name",
            f"a cube needs a `name` that is an identifier, got {name!r}",
        )
        return None, found.findings
    element = f"cubes['{name}']"
    found.unknown(element, raw, CUBE_KEYS)
    sql_table = _string(found, element, raw, "sql_table")
    sql = _string(found, element, raw, "sql")
    if (sql_table is None) == (sql is None):
        found.error(
            element,
            "sql_xor_sql_table",
            "a cube uses either `sql` or `sql_table`, not both and not neither",
        )
    cube = Cube(
        name=name,
        file=file,
        sql_table=sql_table,
        sql=sql,
        title=_string(found, element, raw, "title"),
        description=_string(found, element, raw, "description"),
        data_source=_string(found, element, raw, "data_source"),
        public=_public(found, element, raw),
        dimensions=_members(found, element, raw, "dimensions", "dimension", _dimension),
        measures=_members(found, element, raw, "measures", "measure", _measure),
        segments=_members(found, element, raw, "segments", "segment", _segment),
        joins=_joins(found, element, raw),
        unmapped=_unmapped(raw),
        raw=raw,
    )
    _cross_kind_duplicates(found, element, cube)
    return (None if found.failed else cube), found.findings


def _cross_kind_duplicates(found: _Collector, element: str, cube: Cube) -> None:
    """A name a dimension and a measure share resolves as whichever Cube reads
    first, so a `{member}` reference to it is ambiguous. Cube itself checks
    names per section only, so this is a warning."""

    seen: dict[str, str] = {}
    for member in (*cube.dimensions, *cube.measures, *cube.segments):
        if member.name in seen and seen[member.name] != member.kind:
            found.warning(
                f"{element}.{member.kind}s['{member.name}']",
                "ambiguous_member",
                f"'{member.name}' names both a {seen[member.name]} and a "
                f"{member.kind}, so a reference to it is ambiguous",
            )
        seen.setdefault(member.name, member.kind)


def check_view(raw: Mapping[str, Any], file: str) -> tuple[View | None, list[Finding]]:
    """One view's structure, validated. Its members are resolved by the loader,
    which has the cubes its join paths walk."""

    found = _Collector()
    raw = _canonical(raw)
    name = raw.get("name")
    if not isinstance(name, str) or not is_identifier(name):
        found.error(
            "views[?]",
            "name",
            f"a view needs a `name` that is an identifier, got {name!r}",
        )
        return None, found.findings
    element = f"views['{name}']"
    found.unknown(element, raw, VIEW_KEYS)
    for key in ("sql", "sql_table"):
        if raw.get(key) is not None:
            found.error(
                element, "view_sql", f"a view reads cubes, and takes no `{key}`"
            )
    sources: list[ViewSource] = []
    listed = raw.get("cubes") or []
    if not isinstance(listed, list):
        found.error(element, "type", "`cubes` must be defined as a list")
        listed = []
    for index, entry in enumerate(listed):
        source = _view_source(found, f"{element}.cubes[{index}]", entry)
        if source is not None:
            sources.append(source)
    for key in ("measures", "dimensions", "segments"):
        if raw.get(key):
            found.warning(
                element,
                "view_members",
                f"a view's own `{key}` are not read: dex reads a view as the "
                "members its `cubes` include",
            )
    view = View(
        name=name,
        file=file,
        title=_string(found, element, raw, "title"),
        description=_string(found, element, raw, "description"),
        public=_public(found, element, raw),
        sources=sources,
        unmapped=_unmapped(raw),
        raw=raw,
    )
    return (None if found.failed else view), found.findings


def _view_source(found: _Collector, at: str, entry: Any) -> ViewSource | None:
    if not isinstance(entry, Mapping):
        found.error(at, "type", "a view cube is a mapping with `join_path`")
        return None
    entry = _canonical(entry)
    found.unknown(at, entry, _VIEW_CUBE_KEYS)
    path = entry.get("join_path")
    segments = path.split(".") if isinstance(path, str) else []
    if not segments or not all(is_identifier(s) for s in segments):
        found.error(
            at,
            "join_path",
            f"`join_path` names cubes joined in order, like orders.customers; got "
            f"{path!r}",
        )
        return None
    at = f"{at.rsplit('[', 1)[0]}['{path}']"
    prefix = _boolean(found, at, entry, "prefix")
    split = _boolean(found, at, entry, "split")
    if prefix and split:
        found.error(
            at, "split_prefix", "`split` together with `prefix` is not supported"
        )
        return None
    alias = entry.get("alias")
    if alias is not None and not isinstance(alias, str):
        found.error(at, "alias", "`alias` is a string")
        return None
    includes = entry.get("includes")
    if includes == "*":
        parsed: tuple[ViewInclude, ...] | None = None
    elif isinstance(includes, list):
        members: list[ViewInclude] = []
        for item in includes:
            include = _view_include(found, at, item)
            if include is not None:
                members.append(include)
        parsed = tuple(members)
    else:
        found.error(
            at, "includes", '`includes` is required: "*" or a list of member names'
        )
        return None
    excludes = entry.get("excludes") or []
    if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
        found.error(at, "excludes", "`excludes` is a list of member names")
        return None
    for name in [*(i.name for i in parsed or ()), *excludes]:
        if "." in name:
            found.error(
                at,
                "member_path",
                f"paths are not allowed in a view's includes or excludes, and "
                f"'{name}' is one; name the cube in `join_path` instead",
            )
            return None
    return ViewSource(
        join_path=tuple(segments),
        includes=parsed,
        excludes=tuple(excludes),
        prefix=bool(prefix),
        alias=alias,
        split=bool(split),
    )


def _view_include(found: _Collector, at: str, item: Any) -> ViewInclude | None:
    if isinstance(item, str):
        return ViewInclude(name=item)
    if isinstance(item, Mapping):
        item = _canonical(item)
        found.unknown(at, item, _VIEW_INCLUDE_KEYS)
        name, alias = item.get("name"), item.get("alias")
        if isinstance(name, str) and (alias is None or is_identifier(alias)):
            return ViewInclude(
                name=name,
                alias=alias,
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                description=(
                    item.get("description")
                    if isinstance(item.get("description"), str)
                    else None
                ),
            )
    found.error(
        at,
        "includes",
        f"an include is a member name or a mapping with `name`: {item!r}",
    )
    return None
