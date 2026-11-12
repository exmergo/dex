"""Dex's port of Snowflake's Semantic View YAML specification and validation
rules, for one view: the structure.

Snowflake publishes the specification
(https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)
and the validation rules
(https://docs.snowflake.com/en/user-guide/views-semantic/validation-rules) as
prose, and enforces them when ``SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML`` runs on
an account. dex reads the committed YAML without one, so the rules it relies on
are restated here, each as one row of :data:`RULES`, and each row was qualified
against Snowflake's own ``verify_only`` check on a live account. Where the
prose and the account disagree, the account wins, and the row says so:

- member names are unique **per logical table**, not across the view;
- a view of facts alone (no dimension, no metric) is accepted from YAML;
- ``access_modifier`` on a dimension is accepted and kept (read back on every
  dimension as ``public_access``), so ``private_access`` there only warns;
- a relationship whose right columns are no key is refused only where its left
  columns are one (one-to-many); where neither side is a key it is accepted as
  many-to-many, and only an expression reading across it is refused;
- a range relationship needs a ``distinct_range`` constraint on the referenced
  table, written under ``constraints``, which the specification does not
  document and Snowflake writes on read-back.

**What this module judges.** One view's structure: its name, its logical
tables and every key on them, keys and constraints, member lists, relationships
and derived metrics. What needs the expressions parsed (what each one reads,
aggregation, granularity, cycles, windows) is :mod:`.loader`'s, through
:mod:`.lineage`.

**Element versus view.** A problem in one member or relationship refuses that
element and keeps the rest. A problem in a logical table's own definition (its
name, base table or keys) refuses that table and its members, and a problem in
what the whole view rests on refuses the view.

Two files are refused outright, by name, as another format: a legacy Cortex
Analyst semantic model (its tables carry ``measures``), which Snowflake accepts
and silently drops parts of, and anything that is not a mapping.

Pure Python: no sqlglot, no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import DexError
from .model import (
    DERIVED,
    DIMENSION,
    FACT,
    METRIC,
    PRIVATE,
    PUBLIC,
    TIME_DIMENSION,
    ColumnPair,
    LogicalTable,
    Member,
    Relationship,
    SemanticView,
)

__all__ = [
    "CARRIED",
    "CORTEX_ONLY",
    "ERROR",
    "RULES",
    "UNSUPPORTED",
    "WARNING",
    "Finding",
    "check_view",
]

ERROR = "error"
WARNING = "warning"

#: Every rule this port applies, with the severity it carries while reading.
#: A warning is a definition Snowflake accepts and dex reports rather than
#: refuses, because it is still readable.
RULES: dict[str, str] = {
    "root": ERROR,
    "name": ERROR,
    "tables": ERROR,
    "unknown_key": WARNING,
    "legacy_semantic_model": ERROR,
    "table": ERROR,
    "base_table": ERROR,
    "type": ERROR,
    "member": ERROR,
    "duplicate_name": ERROR,
    "primary_key": ERROR,
    "unique_keys": ERROR,
    "constraints": ERROR,
    "access_modifier": ERROR,
    "dimension_access": WARNING,
    "relationship": ERROR,
    "self_relationship": ERROR,
    "relationship_cycle": ERROR,
    "one_to_many": ERROR,
    "many_to_many": WARNING,
    "asof": ERROR,
    "range": ERROR,
    "non_additive": ERROR,
    "using_relationships": ERROR,
    # Applied by the loader, over the files and the parsed expressions.
    "file_name": WARNING,
    "duplicate_view": ERROR,
    "unparsed_expression": WARNING,
    "reference": ERROR,
    "row_level": ERROR,
    "metric_aggregate": ERROR,
    "granularity": ERROR,
    "expression_cycle": ERROR,
    "derived_metric": ERROR,
    "window_reference": ERROR,
    "multiple_paths": ERROR,
}

#: Top-level keys only Cortex Analyst reads: carried, never modelled.
CORTEX_ONLY = (
    "verified_queries",
    "custom_instructions",
    "module_custom_instructions",
)
#: Top-level keys the catalog has no home for: carried and declared unsupported.
UNSUPPORTED = ("variables", "max_staleness", "tags", "materializations")
CARRIED = (*CORTEX_ONLY, *UNSUPPORTED)

_TOP_LEVEL = ("name", "description", "tables", "relationships", "metrics", *CARRIED)
_TABLE_KEYS = (
    "name",
    "description",
    "synonyms",
    "base_table",
    "primary_key",
    "unique_keys",
    "constraints",
    "dimensions",
    "time_dimensions",
    "facts",
    "metrics",
    "filters",
    "tags",
)
_COMMON_MEMBER_KEYS = (
    "name",
    "synonyms",
    "description",
    "expr",
    "data_type",
    "access_modifier",
    "labels",
    "tags",
)
_MEMBER_KEYS: dict[str, tuple[str, ...]] = {
    DIMENSION: (
        *_COMMON_MEMBER_KEYS,
        "unique",
        "is_enum",
        "sample_values",
        "cortex_search_service",
        "cortex_search_service_name",
    ),
    TIME_DIMENSION: (*_COMMON_MEMBER_KEYS, "unique", "sample_values"),
    FACT: (*_COMMON_MEMBER_KEYS, "sample_values"),
    METRIC: (
        *_COMMON_MEMBER_KEYS,
        "non_additive_dimensions",
        "using_relationships",
        "lod_dimension_type",
        "lod_dimensions",
    ),
}
_MEMBER_KEYS[DERIVED] = _MEMBER_KEYS[METRIC]
_LISTS = {
    "dimensions": DIMENSION,
    "time_dimensions": TIME_DIMENSION,
    "facts": FACT,
    "metrics": METRIC,
}
_RELATIONSHIP_KEYS = (
    "name",
    "left_table",
    "right_table",
    "relationship_columns",
    # Legacy Cortex Analyst keys a Semantic View does not need; accepted.
    "join_type",
    "relationship_type",
)
_PAIR_KEYS = ("left_column", "right_column", "type", "right_range")
_ACCESS = (PUBLIC, PRIVATE)
_SORT = ("ascending", "descending")
_NULLS = ("first", "last")


@dataclass(frozen=True)
class Finding:
    """One rule a view broke, at the element it broke it."""

    element: str
    rule: str
    message: str

    @property
    def severity(self) -> str:
        """The severity :data:`RULES` gives this finding's rule."""
        return RULES[self.rule]


class _RefusedError(DexError):
    """The element being read is refused; the finding already says why.

    A control-flow signal inside this module, caught before anything returns:
    a caller sees the finding, never this.
    """


Find = Callable[[str, str, str], None]


def _text(value: Any) -> str | None:
    """``value`` as stripped text, or ``None`` where it is not non-empty text."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _texts(
    value: Any, element: str, key: str, find: Find, *, refuse: bool = True
) -> tuple[str, ...]:
    """A list of text values, or a finding where it is not one."""
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(v for v in value if v.strip())
    find(element, "type", f"`{key}` is a list of text values")
    if refuse:
        raise _RefusedError
    return ()


def _unknown(raw: Mapping[str, Any], known: tuple[str, ...], element: str, find: Find):
    """A warning for every key this port does not know."""
    for key in raw:
        if key not in known:
            find(
                element,
                "unknown_key",
                f"`{key}` is not a key dex knows here; it is carried and not read",
            )


def check_view(raw: Any, file: str) -> tuple[SemanticView | None, list[Finding]]:
    """One view as dex reads it, or ``None`` with the findings that refused it."""

    findings: list[Finding] = []

    def find(element: str, rule: str, message: str) -> None:
        """Record one finding against this view."""
        findings.append(Finding(element, rule, message))

    if not isinstance(raw, Mapping):
        find(
            "(root)",
            "root",
            "a Semantic View file is one YAML mapping (name, tables, "
            f"relationships, metrics, ...), and this parsed as {type(raw).__name__}",
        )
        return None, findings
    if _legacy(raw):
        find(
            "(root)",
            "legacy_semantic_model",
            "this is a legacy Cortex Analyst semantic model (its tables declare "
            "`measures`), not a Semantic View. Snowflake creates a view from it "
            "and drops what a Semantic View does not read, so dex refuses it "
            "rather than read part of it: rename `measures` to `facts` (with "
            "`metrics` for the aggregates) to make it a Semantic View",
        )
        return None, findings
    _unknown(raw, _TOP_LEVEL, "(root)", find)

    name = _text(raw.get("name"))
    if name is None:
        find(
            "(root)",
            "name",
            "a Semantic View needs a `name`: it is the name Snowflake creates the "
            "view under",
        )
        return None, findings
    tables_raw = raw.get("tables")
    if not isinstance(tables_raw, list) or not tables_raw:
        find(
            "(root)",
            "tables",
            "a Semantic View needs at least one logical table under `tables`",
        )
        return None, findings

    tables: list[LogicalTable] = []
    seen_tables: set[str] = set()
    for position, entry in enumerate(tables_raw):
        try:
            table = _table(entry, position, find)
        except _RefusedError:
            continue
        if table.name.lower() in seen_tables:
            find(
                f"tables['{table.name}']",
                "duplicate_name",
                f"a second logical table named `{table.name}`; table names are "
                "unique in a view, ignoring case",
            )
            continue
        seen_tables.add(table.name.lower())
        tables.append(table)
    if not tables:
        return None, findings

    by_name = {t.name.lower(): t for t in tables}
    relationships = _relationships(raw.get("relationships"), by_name, find)
    derived = _derived(raw.get("metrics"), tables, find)
    tables = [_checked_members(t, by_name, relationships, find) for t in tables]
    view = SemanticView(
        name=name,
        file=file,
        description=_text(raw.get("description")),
        tables=tuple(tables),
        relationships=tuple(relationships),
        metrics=tuple(derived),
        carried={key: raw[key] for key in CARRIED if key in raw},
        raw=dict(raw),
    )
    return view, findings


def _legacy(raw: Mapping[str, Any]) -> bool:
    """Whether a document is a legacy Cortex Analyst semantic model."""
    tables = raw.get("tables")
    return isinstance(tables, list) and any(
        isinstance(t, Mapping) and "measures" in t for t in tables
    )


# --- logical tables -------------------------------------------------------------


def _table(entry: Any, position: int, find: Find) -> LogicalTable:
    """One logical table, or ``_RefusedError`` with the finding that refused it."""

    if not isinstance(entry, Mapping):
        find(f"tables[{position}]", "table", "a logical table is a mapping")
        raise _RefusedError
    name = _text(entry.get("name"))
    if name is None:
        find(f"tables[{position}]", "table", "a logical table needs a `name`")
        raise _RefusedError
    element = f"tables['{name}']"
    _unknown(entry, _TABLE_KEYS, element, find)

    base = entry.get("base_table")
    database = schema = table = definition = None
    if isinstance(base, Mapping) and _text(base.get("definition")):
        definition = _text(base.get("definition"))
    elif isinstance(base, Mapping):
        database, schema, table = (
            _text(base.get(part)) for part in ("database", "schema", "table")
        )
        missing = [
            part
            for part, value in (
                ("database", database),
                ("schema", schema),
                ("table", table),
            )
            if value is None
        ]
        if missing:
            find(
                element,
                "base_table",
                f"`base_table` names no {', '.join(missing)}: Snowflake needs the "
                "fully qualified table (database, schema, table) or a "
                "`definition` query",
            )
            raise _RefusedError
    else:
        find(
            element,
            "base_table",
            "a logical table needs a `base_table`: the fully qualified table "
            "(database, schema, table) or a `definition` query",
        )
        raise _RefusedError

    primary = _key(entry.get("primary_key"), element, "primary_key", find)
    uniques: list[tuple[str, ...]] = []
    raw_uniques = entry.get("unique_keys")
    if raw_uniques is not None and not isinstance(raw_uniques, list):
        find(element, "unique_keys", "`unique_keys` is a list of `{columns: [...]}`")
        raise _RefusedError
    for unique in raw_uniques or ():
        columns = _key(unique, element, "unique_keys", find)
        if columns is None:
            continue
        lowered = {c.lower() for c in columns}
        if primary is not None and lowered == {c.lower() for c in primary}:
            find(
                element,
                "unique_keys",
                f"unique key ({', '.join(columns)}) repeats the primary key; "
                "Snowflake refuses a table whose keys duplicate each other",
            )
            raise _RefusedError
        if any(lowered == {c.lower() for c in other} for other in uniques):
            find(
                element,
                "unique_keys",
                f"unique key ({', '.join(columns)}) is declared twice",
            )
            raise _RefusedError
        uniques.append(columns)

    ranges = _constraints(entry.get("constraints"), element, find)
    members: dict[str, list[Member]] = {}
    seen: set[str] = set()
    for key, kind in _LISTS.items():
        members[kind] = []
        listed = entry.get(key)
        if listed is None:
            continue
        if not isinstance(listed, list):
            find(element, "type", f"`{key}` is a list")
            continue
        for index, item in enumerate(listed):
            try:
                member = _member(item, kind, name, f"{element}.{key}", index, find)
            except _RefusedError:
                continue
            if member.name.lower() in seen:
                find(
                    f"{element}.{key}['{member.name}']",
                    "duplicate_name",
                    f"`{member.name}` is already a member of `{name}`; names are "
                    "unique within a logical table (dimensions, time dimensions, "
                    "facts and metrics together), ignoring case",
                )
                continue
            seen.add(member.name.lower())
            members[kind].append(member)

    filters = entry.get("filters")
    return LogicalTable(
        name=name,
        database=database,
        schema=schema,
        table=table,
        definition=definition,
        description=_text(entry.get("description")),
        synonyms=_texts(entry.get("synonyms"), element, "synonyms", find),
        primary_key=primary,
        unique_keys=tuple(uniques),
        ranges=ranges,
        dimensions=tuple(members[DIMENSION]),
        time_dimensions=tuple(members[TIME_DIMENSION]),
        facts=tuple(members[FACT]),
        metrics=tuple(members[METRIC]),
        filters=tuple(f for f in filters or () if isinstance(f, Mapping)),
        raw=dict(entry),
    )


def _key(value: Any, element: str, rule: str, find: Find) -> tuple[str, ...] | None:
    """A ``{columns: [...]}`` key, or ``None`` where none is declared."""
    if value is None:
        return None
    columns = value.get("columns") if isinstance(value, Mapping) else None
    if (
        not isinstance(columns, list)
        or not columns
        or not all(_text(c) for c in columns)
    ):
        find(element, rule, f"`{rule}` is `{{columns: [<column>, ...]}}`")
        raise _RefusedError
    if len({c.strip().lower() for c in columns}) != len(columns):
        find(element, rule, f"`{rule}` names one column twice")
        raise _RefusedError
    return tuple(c.strip() for c in columns)


def _constraints(value: Any, element: str, find: Find) -> tuple[tuple[str, str], ...]:
    """Each ``distinct_range`` constraint as ``(start_column, end_column)``.

    The specification does not document ``constraints``; it is what Snowflake
    reads ``CONSTRAINT ... DISTINCT RANGE BETWEEN s AND e EXCLUSIVE`` as, and
    writes on read-back.
    """

    if value is None:
        return ()
    if not isinstance(value, list):
        find(element, "constraints", "`constraints` is a list")
        raise _RefusedError
    found: list[tuple[str, str]] = []
    for entry in value:
        rng = entry.get("distinct_range") if isinstance(entry, Mapping) else None
        start = _text(rng.get("start_column")) if isinstance(rng, Mapping) else None
        end = _text(rng.get("end_column")) if isinstance(rng, Mapping) else None
        if start is None or end is None:
            find(
                element,
                "constraints",
                "a constraint is `{name, distinct_range: {start_column, end_column}}`",
            )
            raise _RefusedError
        if start.lower() == end.lower():
            find(
                element,
                "range",
                f"a distinct range starts and ends on one column (`{start}`); an "
                "interval needs two",
            )
            raise _RefusedError
        found.append((start, end))
    return tuple(found)


def _member(
    item: Any, kind: str, table: str | None, element: str, index: int, find: Find
) -> Member:
    """One member, or ``_RefusedError`` with the finding that refused it."""

    if not isinstance(item, Mapping):
        find(f"{element}[{index}]", "member", "a member is a mapping")
        raise _RefusedError
    name = _text(item.get("name"))
    if name is None:
        find(f"{element}[{index}]", "member", "a member needs a `name`")
        raise _RefusedError
    here = f"{element}['{name}']"
    _unknown(item, _MEMBER_KEYS[kind], here, find)
    expr = _text(item.get("expr"))
    if expr is None:
        find(here, "member", "a member needs an `expr`")
        raise _RefusedError

    access = item.get("access_modifier", PUBLIC)
    if access not in _ACCESS:
        find(
            here,
            "access_modifier",
            f"`access_modifier` is `{PUBLIC}` or `{PRIVATE}`, not `{access}`",
        )
        raise _RefusedError
    if access == PRIVATE and kind in (DIMENSION, TIME_DIMENSION):
        find(
            here,
            "dimension_access",
            "Snowflake documents `access_modifier` for facts and metrics only, and "
            "a dimension as always public; it accepts and keeps `private_access` "
            "here, and dex reads it as written",
        )

    samples = item.get("sample_values")
    labels = _texts(item.get("labels"), here, "labels", find)
    non_additive = _non_additive(item.get("non_additive_dimensions"), here, find)
    return Member(
        kind=kind,
        name=name,
        expr=expr,
        table=table,
        description=_text(item.get("description")),
        synonyms=_texts(item.get("synonyms"), here, "synonyms", find),
        data_type=_text(item.get("data_type")),
        access=access,
        is_enum=item.get("is_enum") is True,
        sample_value_count=len(samples) if isinstance(samples, list) else 0,
        non_additive=non_additive,
        using_relationships=_texts(
            item.get("using_relationships"), here, "using_relationships", find
        ),
        labels=labels,
        raw=dict(item),
    )


def _non_additive(value: Any, element: str, find: Find) -> tuple[dict[str, Any], ...]:
    """The ``non_additive_dimensions`` entries, checked for shape."""
    if value is None:
        return ()
    if not isinstance(value, list):
        find(element, "non_additive", "`non_additive_dimensions` is a list")
        raise _RefusedError
    found: list[dict[str, Any]] = []
    for entry in value:
        if not (
            isinstance(entry, Mapping)
            and _text(entry.get("table"))
            and _text(entry.get("dimension"))
        ):
            find(
                element,
                "non_additive",
                "a non-additive dimension is `{table, dimension, sort_direction, "
                "null_order}`",
            )
            raise _RefusedError
        if (
            entry.get("sort_direction", "ascending") not in _SORT
            or entry.get("null_order", "first") not in _NULLS
        ):
            find(
                element,
                "non_additive",
                "`sort_direction` is ascending or descending, and `null_order` "
                "first or last",
            )
            raise _RefusedError
        found.append(dict(entry))
    return tuple(found)


def _checked_members(
    table: LogicalTable,
    tables: Mapping[str, LogicalTable],
    relationships: list[Relationship],
    find: Find,
) -> LogicalTable:
    """The table with each metric whose ``non_additive_dimensions`` or
    ``using_relationships`` names nothing the view declares refused."""

    names = {r.name.lower() for r in relationships}
    kept: list[Member] = []
    for metric in table.metrics:
        element = f"tables['{table.name}'].metrics['{metric.name}']"
        if not _references_resolve(metric, tables, names, element, find):
            continue
        kept.append(metric)
    return LogicalTable(**{**table.__dict__, "metrics": tuple(kept)})


def _references_resolve(
    metric: Member,
    tables: Mapping[str, LogicalTable],
    relationships: set[str],
    element: str,
    find: Find,
) -> bool:
    """Whether a metric's non-additive dimensions and relationship names resolve."""
    for entry in metric.non_additive:
        target = tables.get(str(entry["table"]).lower())
        member = target.member(str(entry["dimension"])) if target else None
        if member is None or member.kind not in (DIMENSION, TIME_DIMENSION):
            find(
                element,
                "non_additive",
                f"non-additive dimension `{entry['table']}.{entry['dimension']}` is "
                "not a dimension the view declares",
            )
            return False
    for name in metric.using_relationships:
        if name.lower() not in relationships:
            find(
                element,
                "using_relationships",
                f"`using_relationships` names `{name}`, which is not a relationship "
                "the view declares",
            )
            return False
    return True


# --- relationships ----------------------------------------------------------------


def _relationships(
    value: Any, tables: Mapping[str, LogicalTable], find: Find
) -> list[Relationship]:
    """Every relationship that holds, refusing the ones that do not."""

    if value is None:
        return []
    if not isinstance(value, list):
        find("(root)", "relationship", "`relationships` is a list")
        return []
    found: list[Relationship] = []
    seen: set[str] = set()
    for position, entry in enumerate(value):
        try:
            relationship = _relationship(entry, position, tables, find)
        except _RefusedError:
            continue
        if relationship.name.lower() in seen:
            find(
                f"relationships['{relationship.name}']",
                "duplicate_name",
                f"a second relationship named `{relationship.name}`",
            )
            continue
        seen.add(relationship.name.lower())
        found.append(relationship)
    return _without_cycles(found, find)


def _relationship(
    entry: Any, position: int, tables: Mapping[str, LogicalTable], find: Find
) -> Relationship:
    """One relationship, or ``_RefusedError`` with the finding that refused it."""

    if not isinstance(entry, Mapping):
        find(
            f"relationships[{position}]", "relationship", "a relationship is a mapping"
        )
        raise _RefusedError
    name = _text(entry.get("name"))
    if name is None:
        find(
            f"relationships[{position}]",
            "relationship",
            "a relationship needs a `name`",
        )
        raise _RefusedError
    element = f"relationships['{name}']"
    _unknown(entry, _RELATIONSHIP_KEYS, element, find)
    left_name, right_name = (
        _text(entry.get("left_table")),
        _text(entry.get("right_table")),
    )
    for side, value in (("left_table", left_name), ("right_table", right_name)):
        if value is None or value.lower() not in tables:
            find(
                element,
                "relationship",
                f"`{side}` is `{value}`, which is not a logical table of this view"
                if value
                else f"a relationship needs a `{side}`",
            )
            raise _RefusedError
    left, right = tables[left_name.lower()], tables[right_name.lower()]  # type: ignore[union-attr]
    if left.name.lower() == right.name.lower():
        find(
            element,
            "self_relationship",
            f"relates `{left.name}` to itself; Snowflake refuses a self-reference",
        )
        raise _RefusedError

    pairs = _pairs(entry.get("relationship_columns"), element, right, find)
    relationship = Relationship(
        name=name,
        left_table=left.name,
        right_table=right.name,
        columns=pairs,
        raw=dict(entry),
    )
    _arity(relationship, left, right, element, find)
    return relationship


def _pairs(
    value: Any, element: str, right: LogicalTable, find: Find
) -> tuple[ColumnPair, ...]:
    """A relationship's column pairs, with the ASOF and range rules applied."""

    if not isinstance(value, list) or not value:
        find(
            element,
            "relationship",
            "a relationship needs `relationship_columns`: a list of "
            "`{left_column, right_column}`",
        )
        raise _RefusedError
    pairs: list[ColumnPair] = []
    for entry in value:
        if not isinstance(entry, Mapping) or not _text(entry.get("left_column")):
            find(element, "relationship", "each column pair needs a `left_column`")
            raise _RefusedError
        _unknown(entry, _PAIR_KEYS, element, find)
        kind = entry.get("type", "equal")
        left = _text(entry.get("left_column"))
        if kind == "range":
            rng = entry.get("right_range")
            start = _text(rng.get("start_column")) if isinstance(rng, Mapping) else None
            end = _text(rng.get("end_column")) if isinstance(rng, Mapping) else None
            if start is None or end is None:
                find(
                    element,
                    "range",
                    "a range pair needs `right_range: {start_column, end_column}`",
                )
                raise _RefusedError
            if start.lower() == end.lower():
                find(
                    element,
                    "range",
                    f"the range starts and ends on one column (`{start}`); an "
                    "interval needs two",
                )
                raise _RefusedError
            if (start.lower(), end.lower()) not in {
                (s.lower(), e.lower()) for s, e in right.ranges
            }:
                find(
                    element,
                    "range",
                    f"joins to the interval ({start}, {end}) of `{right.name}`, which "
                    "declares no matching `constraints: [{distinct_range: "
                    "{start_column, end_column}}]`; Snowflake refuses a range "
                    "relationship without one",
                )
                raise _RefusedError
            pairs.append(ColumnPair(left, None, "range", (start, end)))  # type: ignore[arg-type]
            continue
        if kind not in ("equal", "asof"):
            find(element, "relationship", f"`type` is asof or range, not `{kind}`")
            raise _RefusedError
        right_column = _text(entry.get("right_column"))
        if right_column is None:
            find(element, "relationship", "each column pair needs a `right_column`")
            raise _RefusedError
        pairs.append(ColumnPair(left, right_column, kind))  # type: ignore[arg-type]
    if sum(pair.kind == "asof" for pair in pairs) > 1:
        find(
            element,
            "asof",
            "more than one ASOF column; a relationship has at most one",
        )
        raise _RefusedError
    seen = [p.right.lower() for p in pairs if p.right]
    if len(set(seen)) != len(seen):
        find(element, "relationship", "a referenced column is named twice")
        raise _RefusedError
    return tuple(pairs)


def _arity(
    relationship: Relationship,
    left: LogicalTable,
    right: LogicalTable,
    element: str,
    find: Find,
) -> None:
    """Refuse a one-to-many relationship; warn on a many-to-many one.

    A range pair joins to an interval its constraint makes distinct, so it is not
    held to a key here.
    """

    if any(pair.kind == "range" for pair in relationship.columns):
        return
    if right.is_key(relationship.right_columns):
        return
    if left.is_key(relationship.left_columns):
        find(
            element,
            "one_to_many",
            f"its left columns ({', '.join(relationship.left_columns)}) are a key "
            f"of `{left.name}` and its right columns "
            f"({', '.join(relationship.right_columns)}) are no key of "
            f"`{right.name}`: a one-to-many relationship, which Snowflake refuses. "
            "Declare it the other way round, many-to-one",
        )
        raise _RefusedError
    find(
        element,
        "many_to_many",
        f"its right columns ({', '.join(relationship.right_columns)}) are no key "
        f"of `{right.name}`, and its left columns are none of `{left.name}`: "
        "Snowflake accepts it as many-to-many, and refuses any expression that "
        "reads across it",
    )


def _without_cycles(
    relationships: list[Relationship], find: Find
) -> list[Relationship]:
    """The relationships less every one on a cycle, which Snowflake refuses."""

    edges: dict[str, set[str]] = {}
    for rel in relationships:
        edges.setdefault(rel.left_table.lower(), set()).add(rel.right_table.lower())

    def reaches(start: str, goal: str) -> bool:
        pending, seen = [start], set()
        while pending:
            current = pending.pop()
            if current == goal:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(edges.get(current, ()))
        return False

    kept: list[Relationship] = []
    for rel in relationships:
        if reaches(rel.right_table.lower(), rel.left_table.lower()):
            find(
                f"relationships['{rel.name}']",
                "relationship_cycle",
                f"`{rel.left_table}` and `{rel.right_table}` reach each other through "
                "the view's relationships; Snowflake refuses a cycle",
            )
            continue
        kept.append(rel)
    return kept


# --- derived metrics --------------------------------------------------------------


def _derived(value: Any, tables: list[LogicalTable], find: Find) -> list[Member]:
    """The view-level (derived) metrics, refusing one named like a table metric."""

    if value is None:
        return []
    if not isinstance(value, list):
        find("(root)", "derived_metric", "`metrics` is a list")
        return []
    table_metrics = {m.name.lower() for t in tables for m in t.metrics}
    found: list[Member] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        try:
            member = _member(item, DERIVED, None, "metrics", index, find)
        except _RefusedError:
            continue
        element = f"metrics['{member.name}']"
        if member.name.lower() in table_metrics:
            find(
                element,
                "derived_metric",
                f"`{member.name}` is also the name of a table metric; Snowflake "
                "refuses a derived metric that shares one",
            )
            continue
        if member.name.lower() in seen:
            find(element, "duplicate_name", f"a second derived metric `{member.name}`")
            continue
        seen.add(member.name.lower())
        found.append(member)
    return found
