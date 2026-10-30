"""Normalizing a validated Cube model set into what dex reads.

Two channels from one validated set, as for Ossie and SLayer:

- :func:`semantic_catalog` builds the neutral read catalog `explore semantic
  list` renders.
- :func:`definitions` builds the declaration channel: declared keys and joins,
  and nothing else.

**How Cube maps, and where it does not.** dex's catalog is shaped by MetricFlow,
and Cube is shaped differently:

- A cube is a semantic model, on the relation its ``sql_table`` names. A ``sql``
  cube has one only when its query is a plain projection of one table.
- A view is a semantic model with no relation of its own: it is the governed
  public surface over its cubes, and each member it exposes keeps the cube member
  it is (``vendor_params.source``).
- A dimension is a dimension, queried as ``cube.member``. A time dimension's
  grains are Cube's built-in granularities plus its own custom ones.
- An aggregating measure (``count``, ``sum``, ``avg``, ...) is a catalog measure
  **and** a ``simple`` metric reading it, because Cube queries a measure
  directly. A measure with a ``rolling_window`` is ``cumulative``. A calculated
  measure (``number``, and ``string``, ``time``, ``boolean``) aggregates nothing
  itself: it is a metric over the measures its ``{member}`` references name, a
  ``ratio`` where it is exactly one divided by another, ``derived`` otherwise.
- ``primary_key`` and ``joins`` reach dex through the declaration channel. Cube
  has no entities, and none is synthesized: the gap is declared.
- Segments, pre-aggregations, access policies, hierarchies, folders, a view's
  default filters and a cube's refresh key have no home in the catalog, and are
  named in its notes rather than dropped silently.

**What is never read.** ``meta`` is free-form, and ``mask`` and access policies
govern values at query time; none of them is carried into the catalog, the
declarations or the fingerprint.

Nothing here imports sqlglot: the column and relation resolution it needs is
:mod:`.lineage`'s, passed in already computed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..project_definitions import (
    DeclaredCompositeKey,
    DeclaredForeignKey,
    DeclaredKey,
    DeclaredRelationship,
    ModelDeclaration,
    ProjectDefinitions,
)
from ..semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DimensionInfo,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
)
from ..semantic_declarations import read_native_declaration
from .model import MEASURE, Cube, Member, View
from .references import resolve, scan
from .validator import PREDEFINED_GRANULARITIES

if TYPE_CHECKING:
    from .lineage import CubeRelation
    from .loader import LoadResult

__all__ = [
    "AGGREGATIONS",
    "DECLARATION_SOURCE",
    "Resolution",
    "definitions",
    "member_token",
    "resolution",
    "semantic_catalog",
]

#: What `ProjectDefinitions` records as the channel a declaration came from.
DECLARATION_SOURCE = "cube"

#: Cube's aggregating measure types, as the catalog's aggregation vocabulary
#: spells them. `count_distinct_approx` keeps Cube's name: it is an estimate,
#: and calling it `count_distinct` would claim an exact count.
AGGREGATIONS: dict[str, str] = {
    "count": "count",
    "count_distinct": "count_distinct",
    "count_distinct_approx": "count_distinct_approx",
    "sum": "sum",
    "avg": "average",
    "min": "min",
    "max": "max",
}

_RATIO = re.compile(r"^\s*\(?\s*(\{[^{}]+\})\s*\)?\s*/\s*\(?\s*(\{[^{}]+\})\s*\)?\s*$")


def member_token(owner: str, member: str) -> str:
    """The reference a Cube query uses for a member: ``cube.member``."""

    return f"{owner}.{member}"


@dataclass
class Resolution:
    """Every relation, column and join pair the set resolves to, computed once.

    The catalog, the declarations and the fingerprint all read the same
    resolution, so whether a member links to a physical column is decided once
    whichever channel asks.
    """

    relations: dict[str, CubeRelation]
    #: `(cube, member) -> physical column`, against the cube's gate relation.
    columns: dict[tuple[str, str], str | None]
    #: `(cube, member) -> SQL over the cube's own columns`, for a computed one.
    expressions: dict[tuple[str, str], str | None]
    #: `(cube, join reached as) -> (pairs or None, note or None)`.
    pairs: dict[tuple[str, str], tuple[list[tuple[str, str]] | None, str | None]]

    def relation(self, cube: str) -> str | None:
        found = self.relations.get(cube)
        return found.relation if found else None

    def gate_relation(self, cube: str) -> str | None:
        found = self.relations.get(cube)
        return found.gate_relation if found else None

    def column(self, cube: str, member: str) -> str | None:
        """The column the catalog may claim: only where the cube has a relation."""

        return self.columns.get((cube, member)) if self.relation(cube) else None


def resolution(loaded: LoadResult, connector: str | None) -> Resolution:
    """Resolve the set, through :mod:`.lineage` (the one sqlglot importer)."""

    from ..adapters import fold_identifier
    from . import lineage

    def fold(column: str | None) -> str | None:
        # Lineage returns only unquoted names (a quoted one resolves to no
        # column), and an unquoted name is the column the warehouse folded it
        # to, the rule `sql_table` is already read by.
        return fold_identifier(connector, column) if column else None

    cubes = {cube.name: cube for cube in loaded.cubes}
    relations = {
        cube.name: lineage.cube_relation(cube, connector) for cube in loaded.cubes
    }
    columns: dict[tuple[str, str], str | None] = {}
    expressions: dict[tuple[str, str], str | None] = {}
    for cube in loaded.cubes:
        relation = relations[cube.name]
        for member in (*cube.dimensions, *cube.measures):
            column = fold(lineage.member_column(cube, member, cubes, relation))
            columns[(cube.name, member.name)] = column
            expressions[(cube.name, member.name)] = (
                None if column else _over_columns(cube, member, cubes, relation)
            )
    pairs: dict[tuple[str, str], tuple[list[tuple[str, str]] | None, str | None]] = {}
    for cube in loaded.cubes:
        for join in cube.joins:
            found, note = lineage.join_pairs(cube, join, cubes, relations, connector)
            pairs[(cube.name, join.reached_as)] = (
                [(fold(a) or a, fold(b) or b) for a, b in found] if found else found,
                note,
            )
    return Resolution(
        relations=relations, columns=columns, expressions=expressions, pairs=pairs
    )


def _over_columns(
    cube: Cube,
    member: Member,
    cubes: Mapping[str, Cube],
    relation: Any,
    visiting: frozenset[str] = frozenset(),
) -> str | None:
    """A computed member's SQL over its cube's own columns, for the PII gate.

    The gate parses it and screens every column it reads, rather than screening
    the member by its name. ``{CUBE}.x`` becomes ``x`` and a reference to another
    member of the cube becomes that member's own expression. Anything reaching
    another cube or a per-query value yields nothing: the gate then falls back to
    the name, and says so.
    """

    if member.sql is None or member.name in visiting or relation.projection:
        return None
    holes, problem = scan(member.sql)
    if problem:
        return None
    text = member.sql
    for hole in reversed(holes):
        resolved, _ = resolve(hole, cube, cubes)
        if resolved is None or resolved.via or resolved.cube != cube.name:
            return None
        if resolved.kind == "table":
            end = hole.end + 1 if text[hole.end : hole.end + 1] == "." else hole.end
            text = text[: hole.start] + text[end:]
            continue
        inner = cube.member(resolved.member or "")
        if inner is None or inner.kind == MEASURE:
            return None
        nested = _over_columns(cube, inner, cubes, relation, visiting | {member.name})
        if nested is None:
            return None
        text = text[: hole.start] + f"({nested})" + text[hole.end :]
    return text.replace("\\{", "{").replace("\\}", "}")


# --- the read catalog ----------------------------------------------------------


def semantic_catalog(
    loaded: LoadResult,
    resolved: Resolution,
    *,
    data_source: str = "default",
    notes: Sequence[str] = (),
) -> SemanticCatalogView:
    """The neutral read catalog for a validated set."""

    found: list[str] = list(notes)
    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    measures: list[MeasureInfo] = []
    metrics: list[MetricInfo] = []
    physical: dict[str, tuple[str, str]] = {}
    computed: dict[str, tuple[str, str]] = {}
    cubes = {cube.name: cube for cube in loaded.cubes}
    reach = _reachable(cubes)

    for cube in loaded.cubes:
        relation = resolved.relations[cube.name]
        if relation.note:
            found.append(relation.note)
        found.extend(_cube_notes(cube, data_source))
        time = _time_dimension(cube, found)
        models.append(
            SemanticModelInfo(
                name=cube.name,
                label=cube.title,
                description=cube.description,
                agg_time_dimension=time,
                relation=relation.relation,
            )
        )
        for member in cube.dimensions:
            token = member_token(cube.name, member.name)
            dimensions.append(_dimension(cube, member, token, resolved))
            _gate(cube.name, member.name, token, resolved, physical, computed)
        for member in cube.measures:
            token = member_token(cube.name, member.name)
            _gate(cube.name, member.name, token, resolved, physical, computed)
            if _aggregating(member):
                measures.append(
                    MeasureInfo(
                        name=member.name,
                        agg=AGGREGATIONS[member.type or ""],
                        expr=member.sql,
                        agg_time_dimension=time,
                        label=member.title,
                        description=member.description,
                        semantic_model=cube.name,
                        column=resolved.column(cube.name, member.name),
                    )
                )
        groupable = [
            member_token(owner, dimension.name)
            for owner in reach[cube.name]
            for dimension in cubes[owner].dimensions
        ]
        for member in cube.measures:
            metric, note = _metric(cube, member, cubes, groupable, time)
            metrics.append(metric)
            if note:
                found.append(note)

    for view in loaded.views:
        found.extend(_view_notes(view))
        view_metrics, view_dimensions, view_model = _view(
            view, cubes, resolved, physical, computed, found
        )
        models.append(view_model)
        dimensions.extend(view_dimensions)
        metrics.extend(view_metrics)

    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        measures=measures,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=found,
        physical_columns=physical,
        computed_columns=computed,
        compiled_only=list(loaded.compiled_only),
    )


def _gate(
    cube: str,
    member: str,
    token: str,
    resolved: Resolution,
    physical: dict[str, tuple[str, str]],
    computed: dict[str, tuple[str, str]],
) -> None:
    """Where the PII gate resolves ``token``: a column, or an expression over
    columns, on the cube's gate relation, which may be a partial name."""

    gate = resolved.gate_relation(cube)
    if gate is None:
        return
    column = resolved.columns.get((cube, member))
    if column:
        physical[token] = (gate, column)
    elif expression := resolved.expressions.get((cube, member)):
        computed[token] = (gate, expression)


def _dimension(
    cube: Cube, member: Member, token: str, resolved: Resolution
) -> DimensionInfo:
    is_time = member.type == "time"
    return DimensionInfo(
        name=token,
        type="time" if is_time else "categorical",
        label=member.title,
        description=member.description,
        definition=member.name,
        semantic_model=cube.name,
        # Empty is the positive statement that a categorical dimension has no
        # grain; a time one has every built-in granularity and its own.
        queryable_granularities=_grains(member) if is_time else [],
        column=resolved.column(cube.name, member.name),
        vendor_params=_member_params(member),
    )


def _grains(member: Member) -> list[str]:
    return [*PREDEFINED_GRANULARITIES, *member.granularities]


def _member_params(member: Member) -> dict[str, Any]:
    """What Cube states about a member that the neutral model has no field for."""

    params: dict[str, Any] = {"type": member.type}
    if member.sql is not None:
        params["sql"] = member.sql
    if member.primary_key:
        params["primary_key"] = True
    if member.public is False:
        params["public"] = False
    if member.granularities:
        params["granularities"] = list(member.granularities)
    if member.values:
        params["values"] = list(member.values)
    if member.case:
        params["case"] = True
    if member.filters:
        params["filters"] = list(member.filters)
    if member.rolling_window:
        params["rolling_window"] = dict(member.rolling_window)
    if member.multi_stage:
        params["multi_stage"] = True
    return params


def _aggregating(member: Member) -> bool:
    return member.type in AGGREGATIONS and not member.multi_stage


def _time_dimension(cube: Cube, notes: list[str]) -> str | None:
    """The cube's one time dimension, the axis its measures aggregate over.

    Cube has no default time dimension: a query names the time dimension it
    buckets by. With exactly one there is no choice to make. With several there
    is, and naming one would be choosing for the caller, so none is named and a
    note says why.
    """

    times = [m.name for m in cube.dimensions if m.type == "time"]
    if len(times) == 1:
        return times[0]
    if len(times) > 1:
        notes.append(
            f"cube '{cube.name}' has {len(times)} time dimensions "
            f"({', '.join(times)}) and Cube declares no default, so its measures "
            "name no time axis: a query names the one it buckets by"
        )
    return None


def _reachable(cubes: Mapping[str, Cube]) -> dict[str, list[str]]:
    """Each cube and every cube its declared joins reach, in walk order.

    Directed, the way Cube's join graph is: a cube joining another can be
    grouped by the other's dimensions, and not the reverse unless that cube
    declares the join back.
    """

    reach: dict[str, list[str]] = {}
    for name in cubes:
        seen = [name]
        frontier = [name]
        while frontier:
            current = cubes[frontier.pop(0)]
            for join in current.joins:
                if join.target in cubes and join.target not in seen:
                    seen.append(join.target)
                    frontier.append(join.target)
        reach[name] = seen
    return reach


def _metric(
    cube: Cube,
    member: Member,
    cubes: Mapping[str, Cube],
    groupable: list[str],
    time: str | None,
) -> tuple[MetricInfo, str | None]:
    token = member_token(cube.name, member.name)
    base: dict[str, Any] = {
        "name": token,
        "label": member.title,
        "description": member.description,
        "dimensions": list(groupable),
        "filter": _filter(member.filters),
        "vendor_params": _member_params(member),
    }
    axis = cube.member(time) if time else None
    timed: dict[str, Any] = {
        "time_axis": [time] if time else None,
        "queryable_granularities": _grains(axis) if axis else None,
    }
    if _aggregating(member):
        return (
            MetricInfo(
                type="cumulative" if member.rolling_window else "simple",
                semantic_models=[cube.name],
                input_measures=[member.name],
                composition=MetricComposition(measure=member.name),
                **timed,
                **base,
            ),
            None,
        )

    inputs, aggregates, note = _inputs(cube, member, cubes)
    owners = list(dict.fromkeys([cube.name, *(owner for owner, _ in aggregates)]))
    ratio = _RATIO.match(member.sql or "")
    sides = _ratio_sides(cube, member, cubes) if ratio else None
    composition = (
        MetricComposition(numerator=sides[0], denominator=sides[1])
        if sides
        else MetricComposition(expr=member.sql, input_metrics=inputs or None)
    )
    measures = list(dict.fromkeys(name for _, name in aggregates))
    same_axis = all(owner == cube.name for owner, _ in aggregates)
    return (
        MetricInfo(
            type="ratio" if sides else "derived",
            semantic_models=owners,
            input_measures=measures or None,
            composition=composition,
            **(timed if same_axis else {}),
            **base,
        ),
        note,
    )


def _inputs(
    cube: Cube, member: Member, cubes: Mapping[str, Cube]
) -> tuple[list[str], list[tuple[str, str]], str | None]:
    """A calculated measure's input metrics, and the aggregating measures under
    them, followed through other calculated measures."""

    inputs: list[str] = []
    aggregates: list[tuple[str, str]] = []
    pending = [(cube, member)]
    seen: set[tuple[str, str]] = set()
    while pending:
        owner, current = pending.pop(0)
        holes, problem = scan(current.sql or "")
        if problem:
            return inputs, aggregates, None
        for hole in holes:
            resolved, _ = resolve(hole, owner, cubes)
            if resolved is None or resolved.kind != "member":
                continue
            target = cubes.get(resolved.cube or "")
            found = target.member(resolved.member or "") if target else None
            if target is None or found is None or found.kind != MEASURE:
                continue
            key = (target.name, found.name)
            if key in seen:
                continue
            seen.add(key)
            if current is member:
                inputs.append(member_token(*key))
            if _aggregating(found):
                aggregates.append(key)
            else:
                pending.append((target, found))
    note = None
    if not inputs:
        note = (
            f"measure '{cube.name}.{member.name}' is a calculated `{member.type}` "
            "measure that references no other measure, so the measures it is made "
            "of cannot be named"
        )
    return inputs, aggregates, note


def _ratio_sides(
    cube: Cube, member: Member, cubes: Mapping[str, Cube]
) -> tuple[str, str] | None:
    """``{a} / {b}``, with both sides measures, as their metric tokens."""

    holes, problem = scan(member.sql or "")
    if problem or len(holes) != 2:
        return None
    sides: list[str] = []
    for hole in holes:
        resolved, _ = resolve(hole, cube, cubes)
        if resolved is None or resolved.kind != "member":
            return None
        target = cubes.get(resolved.cube or "")
        found = target.member(resolved.member or "") if target else None
        if target is None or found is None or found.kind != MEASURE:
            return None
        sides.append(member_token(target.name, found.name))
    return sides[0], sides[1]


def _filter(filters: Sequence[str]) -> str | None:
    """A measure's `filters`, which Cube ANDs, as one predicate."""

    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return " AND ".join(f"({f})" for f in filters)


def _cube_notes(cube: Cube, data_source: str) -> list[str]:
    notes: list[str] = []
    if cube.unmapped:
        notes.append(
            f"cube '{cube.name}' declares {', '.join(cube.unmapped)}, which dex's "
            "catalog has no home for, so they are not listed"
        )
    if cube.public is False:
        notes.append(
            f"cube '{cube.name}' is not public: Cube's API does not expose it, and "
            "it is listed here because it is still a definition in the repository"
        )
    hidden = [m.name for m in (*cube.dimensions, *cube.measures) if m.public is False]
    if hidden:
        notes.append(
            f"cube '{cube.name}': {', '.join(hidden)} "
            f"{'is' if len(hidden) == 1 else 'are'} not public, so Cube's API does "
            "not expose them (vendor_params.public is false)"
        )
    if (cube.data_source or "default") != data_source:
        notes.append(
            f"cube '{cube.name}' is on Cube data source "
            f"'{cube.data_source or 'default'}', not '{data_source}' "
            "(semantic.cube.data_source), so it does not sit on the warehouse dex "
            "is connected to"
        )
    if any(m.multi_stage for m in cube.measures):
        notes.append(
            f"cube '{cube.name}' has multi-stage measures, which Cube computes over "
            "other queries' results; each is listed as a derived metric with its "
            "multi-stage settings in vendor_params"
        )
    return notes


def _view_notes(view: View) -> list[str]:
    notes: list[str] = []
    if view.unmapped:
        notes.append(
            f"view '{view.name}' declares {', '.join(view.unmapped)}, which dex's "
            "catalog has no home for, so they are not listed"
        )
    if view.public is False:
        notes.append(f"view '{view.name}' is not public: Cube's API does not expose it")
    return notes


def _view(
    view: View,
    cubes: Mapping[str, Cube],
    resolved: Resolution,
    physical: dict[str, tuple[str, str]],
    computed: dict[str, tuple[str, str]],
    notes: list[str],
) -> tuple[list[MetricInfo], list[DimensionInfo], SemanticModelInfo]:
    """A view: its members, each the cube member it exposes."""

    dimensions: list[DimensionInfo] = []
    tokens: list[str] = []
    times: list[str] = []
    for exposed in view.members_of("dimension"):
        token = member_token(view.name, exposed.name)
        cube = cubes[exposed.cube]
        row = _dimension(cube, exposed.member, token, resolved)
        dimensions.append(
            DimensionInfo(
                name=token,
                type=row.type,
                label=exposed.title or row.label,
                description=exposed.description or row.description,
                definition=exposed.name,
                semantic_model=view.name,
                queryable_granularities=row.queryable_granularities,
                # A view has no relation, so it claims no column; the PII gate
                # still reaches the cube column the member exposes.
                column=None,
                vendor_params={**(row.vendor_params or {}), "source": exposed.source},
            )
        )
        tokens.append(token)
        if row.type == "time":
            times.append(exposed.name)
        _gate(exposed.cube, exposed.member.name, token, resolved, physical, computed)

    metrics: list[MetricInfo] = []
    for exposed in view.members_of("measure"):
        cube = cubes[exposed.cube]
        source, _ = _metric(cube, exposed.member, cubes, tokens, None)
        token = member_token(view.name, exposed.name)
        _gate(exposed.cube, exposed.member.name, token, resolved, physical, computed)
        metrics.append(
            MetricInfo(
                name=token,
                type=source.type,
                label=exposed.title or source.label,
                description=exposed.description or source.description,
                dimensions=list(tokens),
                semantic_models=list(
                    dict.fromkeys([view.name, *(source.semantic_models or [])])
                ),
                input_measures=source.input_measures,
                composition=source.composition,
                filter=source.filter,
                vendor_params={
                    **(source.vendor_params or {}),
                    "source": exposed.source,
                },
            )
        )
    if len(times) > 1:
        notes.append(
            f"view '{view.name}' exposes {len(times)} time dimensions "
            f"({', '.join(times)}), so it names no default time dimension"
        )
    model = SemanticModelInfo(
        name=view.name,
        label=view.title,
        description=view.description,
        agg_time_dimension=times[0] if len(times) == 1 else None,
    )
    return metrics, dimensions, model


# --- the declaration channel ---------------------------------------------------


def definitions(
    loaded: LoadResult,
    resolved: Resolution,
    *,
    notes: Sequence[str] = (),
    present: bool = True,
) -> ProjectDefinitions:
    """Declared keys and joins, for grain detection and the relationship channel."""

    keys: list[DeclaredKey] = []
    composite: list[DeclaredCompositeKey] = []
    foreign: list[DeclaredForeignKey] = []
    relationships: list[DeclaredRelationship] = []
    declarations: dict[str, ModelDeclaration] = {}
    unread: list[str] = []
    found: list[str] = list(notes)
    relations = {
        cube.name: relation
        for cube in loaded.cubes
        if (relation := resolved.relation(cube.name))
    }

    for cube in loaded.cubes:
        columns = declared_key_columns(cube, resolved)
        if len(columns) == 1:
            keys.append(
                DeclaredKey(
                    model=cube.name,
                    relation=relations.get(cube.name),
                    column=columns[0],
                    unique=True,
                    primary=True,
                    source=DECLARATION_SOURCE,
                )
            )
        elif columns:
            composite.append(
                DeclaredCompositeKey(
                    model=cube.name,
                    relation=relations.get(cube.name),
                    columns=columns,
                    claim="primary",
                    source=DECLARATION_SOURCE,
                )
            )
        declaration, declaration_notes = read_native_declaration(
            cube.name,
            relations.get(cube.name),
            columns,
            cube.raw.get("meta"),
            [
                (m.name, resolved.columns.get((cube.name, m.name)), m.raw.get("meta"))
                for m in [*cube.dimensions, *cube.measures]
            ],
            source=DECLARATION_SOURCE,
        )
        unread.extend(declaration_notes)
        if declaration is not None:
            declarations[cube.name] = declaration
        for join in cube.joins:
            declared, note = declared_relationship(cube, join, resolved, relations)
            if note:
                found.append(note)
            if declared is None:
                continue
            relationships.append(declared)
            if len(declared.column_pairs) == 1:
                ((column, to_column),) = declared.column_pairs
                foreign.append(
                    DeclaredForeignKey(
                        model=declared.model,
                        relation=declared.relation,
                        column=column,
                        to_model=declared.to_model,
                        to_relation=declared.to_relation,
                        to_column=to_column,
                        source=DECLARATION_SOURCE,
                    )
                )

    if loaded.cubes:
        found.append(
            "a Cube cube declares the relation it reads and builds none, so it "
            "contributes no built-relation names and cannot tell explore that a "
            "warehouse relation is unaccounted for"
        )
    return ProjectDefinitions(
        present=present,
        relationship_source=DECLARATION_SOURCE if loaded.cubes else None,
        semantic_source=DECLARATION_SOURCE if loaded.cubes else None,
        foreign_keys=foreign,
        declared_relationships=relationships,
        declared_keys=keys,
        declared_composite_keys=composite,
        source_relations=relations,
        model_declarations=declarations,
        declaration_notes=unread,
        notes=found,
    )


def declared_key_columns(cube: Cube, resolved: Resolution) -> list[str]:
    """The cube's primary key as physical columns, in declared order.

    Several ``primary_key`` dimensions are one composite key, never several
    single ones, which would claim each member unique on its own. A key member
    with no physical column behind it cannot be checked against the warehouse,
    so a key containing one is left out rather than recorded against a guess.
    """

    columns = [
        resolved.columns.get((cube.name, m.name))
        for m in cube.dimensions
        if m.primary_key
    ]
    if not columns or not all(columns):
        return []
    return [str(c) for c in columns]


def declared_relationship(
    cube: Cube,
    join: Any,
    resolved: Resolution,
    relations: Mapping[str, str],
) -> tuple[DeclaredRelationship | None, str | None]:
    """One join with every ordered pair, written child to parent.

    The joining cube is the child (``many_to_one``, and ``one_to_one`` as
    written), except where the author declared ``one_to_many``, which says the
    joining cube is the one side.
    """

    pairs, note = resolved.pairs.get((cube.name, join.reached_as), (None, None))
    if pairs is None:
        return None, note
    child, parent = cube.name, join.target
    if join.relationship == "one_to_many":
        child, parent = parent, child
        pairs = [(to, frm) for frm, to in pairs]
    return (
        DeclaredRelationship(
            model=child,
            relation=relations.get(child),
            to_model=parent,
            to_relation=relations.get(parent),
            column_pairs=pairs,
            source=DECLARATION_SOURCE,
            name=join.alias,
        ),
        None,
    )
