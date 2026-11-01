"""Cube's declarations held to the warehouse schema, on dex's own metadata.

The schema half of `maintain` under `semantic.vendor: cube`. Cube compares a
model against the warehouse only by compiling and running it inside a Cube
server, which dex never starts, so the rules run here instead, on the datasets
dex captured through its own connection. Nothing here opens a connection, starts
Node or reads a Cube server: it is handed the datasets, never an adapter.

What it asks of each cube on a physical relation (:mod:`.catalog`'s
resolution, read the way the active connector folds identifiers):

- **Is the relation still there?** One the baseline listed and the warehouse no
  longer does is ``declared_relation_missing``. One neither lists is a note,
  not a finding: a cube on a relation this connection was never scoped to (a
  derived cube still pointing at a dev schema, say) has not lost anything.
- **Does every column a member reads still exist?** A dimension or measure that
  is one column, and every column a computed member, a measure's filter or a
  segment reads, through :func:`.lineage.columns_read`. A missing one is
  ``declared_column_missing``. An expression dex cannot read is named in a note
  as unchecked, never passed.
- **Does the warehouse type still fit the member's type?** A ``time``
  dimension on a column that is not temporal, and a ``number`` dimension or a
  summing measure on text, are ``declared_type_conflict``. A warehouse type dex
  cannot place never conflicts (:mod:`..semantic_types`).
- **What else goes with it?** Everything that read a removed member or a
  removed join, to a fixpoint: another member, a join (its key columns, or its
  sql), a view's include or exclude. One ``declared_dependency_dropped`` per
  cube or view that loses something only that way.

Each finding carries the removal of its cube or view (``data.removal``), which
is what ``maintain reconcile`` turns into a `semantic cube update` plan
(:mod:`.removal`). A cube whose relation is gone is never removed there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..adapters import fold_identifier
from ..cache import match_identifier
from ..maintain.drift import DriftFinding
from ..semantic_types import bucket
from .model import DIMENSION, MEASURE, Cube, Member, View, ViewSource

if TYPE_CHECKING:
    from ..cache import Dataset
    from .catalog import Resolution
    from .lineage import Reads
    from .loader import LoadResult

__all__ = ["declared_schema_drift", "type_conflict"]

#: Measure types that add their column up, so a text column cannot back one.
_SUMMING = frozenset({"sum", "avg", "running_total"})


def type_conflict(member: Member, warehouse_type: str | None) -> str | None:
    """The kind of column ``member``'s type needs, where the warehouse's is
    plainly another, else None."""

    found = bucket(warehouse_type)
    if found is None:
        return None
    if member.kind == DIMENSION:
        if member.type == "time" and found != "temporal":
            return "temporal"
        if member.type == "number" and found == "text":
            return "numeric"
    if member.kind == MEASURE and member.type in _SUMMING and found == "text":
        return "numeric"
    return None


@dataclass
class _Live:
    """One cube's relation as the warehouse holds it now."""

    identifier: str
    types: dict[str, str]
    folded: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.types:
            self.folded.setdefault(name.lower(), []).append(name)

    def find(self, column: str) -> str | None:
        """The warehouse's spelling of ``column``: exact, else the one column
        matching case-insensitively, since a connector that folds unquoted
        names would otherwise report every column as missing."""

        if column in self.types:
            return column
        found = self.folded.get(column.lower(), [])
        return found[0] if len(found) == 1 else None


@dataclass
class _Cause:
    """Why one member goes: a column of its own, or what it read."""

    code: str
    column: str | None = None
    declared_type: str | None = None
    warehouse_type: str | None = None
    needs: str | None = None
    reason: str | None = None


@dataclass
class _State:
    cubes: dict[str, Cube]
    whole: dict[str, str] = field(default_factory=dict)
    members: dict[tuple[str, str], _Cause] = field(default_factory=dict)
    joins: dict[tuple[str, str], _Cause] = field(default_factory=dict)
    #: `sql` cubes whose own query selects a column the warehouse dropped.
    sql_reads: set[str] = field(default_factory=set)

    def gone(self, cube: str, member: str) -> bool:
        return cube in self.whole or (cube, member) in self.members

    def walks_removed(self, start: str, path: tuple[str, ...]) -> str | None:
        """The first removed hop of ``path`` walked from ``start``, or None."""

        cube = self.cubes.get(start)
        for segment in path:
            if cube is None:
                return None
            if (cube.name, segment) in self.joins:
                return f"{cube.name} -> {segment}"
            join = cube.join_to(segment)
            cube = self.cubes.get(join.target) if join else None
        return None


def declared_schema_drift(
    loaded: LoadResult,
    resolved: Resolution,
    current: list[Dataset],
    baseline: list[Dataset],
    *,
    connector: str | None,
) -> tuple[list[DriftFinding], list[str]]:
    """Every cube and view the warehouse now contradicts, as findings."""

    from . import lineage

    notes: list[str] = []
    state = _State(cubes={cube.name: cube for cube in loaded.cubes})
    current_ids = [d.identifier for d in current]
    baseline_ids = [d.identifier for d in baseline]
    by_id = {d.identifier: d for d in current}

    def fold(column: str) -> str:
        return fold_identifier(connector, column) or column

    # What each member and join reads, for every cube, so a cascade reaches a
    # cube that was not itself compared.
    reads: dict[tuple[str, str], list[Reads | None]] = {}
    for cube in loaded.cubes:
        relation = resolved.relations[cube.name]
        for member in (*cube.dimensions, *cube.measures, *cube.segments):
            exprs = [member.sql, *member.filters]
            reads[(cube.name, member.name)] = [
                None
                if member.case
                else lineage.columns_read(sql, cube, state.cubes, relation, connector)
                for sql in exprs
                if sql is not None or member.case
            ]
    join_refs = {
        (cube.name, join.reached_as): lineage.columns_read(
            join.sql, cube, state.cubes, resolved.relations[cube.name], connector
        )
        for cube in loaded.cubes
        for join in cube.joins
    }

    live: dict[str, _Live] = {}
    for cube in loaded.cubes:
        relation = resolved.relation(cube.name)
        if relation is None:
            if cube.dimensions or cube.measures:
                notes.append(
                    f"cube '{cube.name}' carries no physical relation dex can "
                    "read, so its members were not compared to the warehouse"
                )
            continue
        matches = match_identifier(relation, current_ids)
        if len(matches) > 1:
            notes.append(
                f"cube '{cube.name}' reads {relation}, which matches several "
                f"warehouse objects ({', '.join(matches)}), so its members were "
                "not compared"
            )
            continue
        if not matches:
            gone = match_identifier(relation, baseline_ids)
            if len(gone) == 1:
                state.whole[cube.name] = gone[0]
            else:
                notes.append(
                    f"cube '{cube.name}' reads {relation}, which neither this "
                    "connection nor the baseline lists, so its members were not "
                    "compared. A relation outside the connection's scope (a dev "
                    "schema, say) is not reported as dropped"
                )
            continue
        dataset = by_id[matches[0]]
        if not dataset.columns:
            notes.append(
                f"dex has no column detail for {dataset.identifier}, so cube "
                f"'{cube.name}' was not compared against it"
            )
            continue
        live[cube.name] = _Live(
            identifier=dataset.identifier,
            types={c.name: c.data_type for c in dataset.columns},
        )

    # Members that lost a column of their own, or whose type no longer fits.
    for name, table in live.items():
        cube = state.cubes[name]
        unchecked: list[str] = []
        for member in (*cube.dimensions, *cube.measures, *cube.segments):
            found = reads[(name, member.name)]
            if any(r is None for r in found):
                unchecked.append(member.name)
            columns = sorted({fold(c) for r in found if r for c in r.columns})
            missing = [c for c in columns if table.find(c) is None]
            if missing:
                state.members[(name, member.name)] = _Cause(
                    code="declared_column_missing", column=missing[0]
                )
                projection = resolved.relations[name].projection
                if projection and set(missing) & {fold(c) for c in projection.values()}:
                    state.sql_reads.add(name)
                continue
            column = resolved.columns.get((name, member.name))
            if column is None:
                continue
            spelled = table.find(column)
            warehouse_type = table.types.get(spelled) if spelled else None
            needs = type_conflict(member, warehouse_type)
            if needs:
                state.members[(name, member.name)] = _Cause(
                    code="declared_type_conflict",
                    column=column,
                    declared_type=member.type,
                    warehouse_type=warehouse_type,
                    needs=needs,
                )
        if unchecked:
            notes.append(
                f"cube '{name}': dex could not read which columns "
                f"{', '.join(unchecked)} read (a per-query value, a reference "
                "into another cube's table, a case, or SQL it could not parse), "
                "so they were not checked against the warehouse"
            )

    # A join keyed on a column either side lost.
    for cube in loaded.cubes:
        if cube.name in state.whole:
            continue
        for join in cube.joins:
            pairs, _note = resolved.pairs.get(
                (cube.name, join.reached_as), (None, None)
            )
            for frm, to in pairs or []:
                lost = [
                    f"{side}.{col}"
                    for side, col in ((cube.name, frm), (join.target, to))
                    if side in live and live[side].find(col) is None
                ]
                if lost:
                    state.joins[(cube.name, join.reached_as)] = _Cause(
                        code="declared_dependency_dropped",
                        reason=(
                            f"it keys on {', '.join(lost)}, which the warehouse "
                            "no longer has"
                        ),
                    )
                    break

    _cascade(state, reads, join_refs)
    view_removals = {view.name: _view_removal(view, state) for view in loaded.views}
    walked = _walked_joins(loaded.views, state)

    findings: list[DriftFinding] = []
    for cube in loaded.cubes:
        findings.extend(_cube_findings(cube, state, live, walked))
    for view in loaded.views:
        removal = view_removals[view.name]
        if removal is not None:
            findings.append(_view_finding(view, removal))
    return findings, notes


def _cascade(
    state: _State,
    reads: Mapping[tuple[str, str], list[Reads | None]],
    join_refs: Mapping[tuple[str, str], Reads | None],
) -> None:
    """Remove what read something removed, until nothing more goes."""

    def broken(owner: str, found: list[Reads | None]) -> str | None:
        for r in found:
            for ref in r.members if r else ():
                hop = state.walks_removed(owner, ref.via)
                if hop:
                    return (
                        f"it reaches {ref.cube}.{ref.member} through the join "
                        f"{hop}, which goes"
                    )
                if ref.cube and ref.member and state.gone(ref.cube, ref.member):
                    return f"it reads {ref.cube}.{ref.member}, which goes"
        return None

    changed = True
    while changed:
        changed = False
        for cube in state.cubes.values():
            if cube.name in state.whole:
                continue
            for join in cube.joins:
                key = (cube.name, join.reached_as)
                if key in state.joins:
                    continue
                reason = (
                    f"its target cube '{join.target}' reads a relation that is gone"
                    if join.target in state.whole
                    else broken(cube.name, [join_refs.get(key)])
                )
                if reason:
                    state.joins[key] = _Cause(
                        code="declared_dependency_dropped", reason=reason
                    )
                    changed = True
            for member in (*cube.dimensions, *cube.measures, *cube.segments):
                key = (cube.name, member.name)
                if key in state.members:
                    continue
                reason = broken(cube.name, list(reads.get(key, [])))
                if reason:
                    state.members[key] = _Cause(
                        code="declared_dependency_dropped", reason=reason
                    )
                    changed = True


def _cube_findings(
    cube: Cube,
    state: _State,
    live: Mapping[str, _Live],
    walked: set[tuple[str, str]],
) -> list[DriftFinding]:
    if cube.name in state.whole:
        identifier = state.whole[cube.name]
        return [
            DriftFinding(
                axis="schema",
                code="declared_relation_missing",
                identifier=identifier,
                severity="high",
                detail=(
                    f"cube '{cube.name}' reads {identifier}, which was in the "
                    "baseline and is gone from the warehouse, so every member of "
                    "the cube is unqueryable"
                ),
                data={
                    "semantic_model": cube.name,
                    "model_file": cube.file,
                    "removal": {"whole_model": True},
                },
            )
        ]

    removal = _cube_removal(cube, state, walked)
    if removal is None:
        return []
    identifier = live[cube.name].identifier if cube.name in live else None
    context = {"semantic_model": cube.name, "model_file": cube.file, "removal": removal}
    consequence = _consequence(removal)
    findings: list[DriftFinding] = []
    cascaded: list[str] = []
    for member in (*cube.dimensions, *cube.measures, *cube.segments):
        cause = state.members.get((cube.name, member.name))
        if cause is None:
            continue
        if cause.code == "declared_dependency_dropped":
            cascaded.append(f"{member.name}: {cause.reason}")
            continue
        findings.append(
            _member_finding(cube, member, cause, identifier, context, consequence)
        )
    for join in cube.joins:
        cause = state.joins.get((cube.name, join.reached_as))
        if cause is not None:
            cascaded.append(f"join {join.reached_as}: {cause.reason}")
    if cascaded:
        findings.append(
            DriftFinding(
                axis="schema",
                code="declared_dependency_dropped",
                identifier=identifier,
                severity="medium",
                detail=(
                    f"cube '{cube.name}' would lose part of its definition because "
                    f"something it depends on was dropped ({'; '.join(cascaded)})"
                ),
                data={**context, "reasons": cascaded},
            )
        )
    return findings


def _member_finding(
    cube: Cube,
    member: Member,
    cause: _Cause,
    identifier: str | None,
    context: dict[str, Any],
    consequence: str,
) -> DriftFinding:
    data = {**context, "declared_member": member.name, "member_kind": member.kind}
    if cause.code == "declared_column_missing":
        return DriftFinding(
            axis="schema",
            code="declared_column_missing",
            identifier=identifier,
            column=cause.column,
            severity="high",
            detail=(
                f"cube '{cube.name}' declares {member.kind} '{member.name}' reading "
                f"column '{cause.column}', which {identifier} no longer has"
                f"{consequence}"
            ),
            data=data,
        )
    return DriftFinding(
        axis="schema",
        code="declared_type_conflict",
        identifier=identifier,
        column=cause.column,
        severity="high",
        detail=(
            f"cube '{cube.name}' declares {member.kind} '{member.name}' as "
            f"{cause.declared_type}, and {identifier}.{cause.column} is "
            f"{cause.warehouse_type}, which is not {cause.needs}{consequence}"
        ),
        data={
            **data,
            "declared_type": cause.declared_type,
            "warehouse_type": cause.warehouse_type,
        },
    )


def _walked_joins(views: list[View], state: _State) -> set[tuple[str, str]]:
    """Every ``(cube, join)`` hop some view's join path walks."""

    walked: set[tuple[str, str]] = set()
    for view in views:
        for source in view.sources:
            cube = state.cubes.get(source.join_path[0])
            for segment in source.join_path[1:]:
                if cube is None:
                    break
                walked.add((cube.name, segment))
                join = cube.join_to(segment)
                cube = state.cubes.get(join.target) if join else None
    return walked


def _cube_removal(
    cube: Cube, state: _State, walked: set[tuple[str, str]]
) -> dict[str, Any] | None:
    """What goes from one cube, by section, or None if nothing does.

    A join a view's path walks is ``held_joins`` rather than ``joins``: removing
    it would break the view, whose sources dex does not rewrite, so it stays
    until the view is re-routed.
    """

    sections = {
        "dimensions": [
            m.name for m in cube.dimensions if (cube.name, m.name) in state.members
        ],
        "measures": [
            m.name for m in cube.measures if (cube.name, m.name) in state.members
        ],
        "segments": [
            m.name for m in cube.segments if (cube.name, m.name) in state.members
        ],
    }
    removed = [
        j.reached_as for j in cube.joins if (cube.name, j.reached_as) in state.joins
    ]
    sections["joins"] = [j for j in removed if (cube.name, j) not in walked]
    held = [j for j in removed if (cube.name, j) in walked]
    if not any(sections.values()) and not held:
        return None
    removal: dict[str, Any] = {"whole_model": False, "element": "cube", **sections}
    if held:
        removal["held_joins"] = held
    if cube.name in state.sql_reads:
        removal["sql_reads_dropped"] = True
    return removal


def _view_removal(view: View, state: _State) -> dict[str, Any] | None:
    """What goes from one view: includes and excludes naming a removed member,
    members a ``"*"`` source loses on its own, and sources whose path breaks."""

    includes: list[dict[str, Any]] = []
    excludes: list[dict[str, Any]] = []
    lost: list[str] = []
    broken: list[str] = []
    removed_sources: list[dict[str, Any]] = []
    for source in view.sources:
        path = ".".join(source.join_path)
        hop = state.walks_removed(source.join_path[0], source.join_path[1:])
        if hop or any(c in state.whole for c in _path_cubes(source, state)):
            broken.append(path)
            continue
        target = _target(source, state)
        if target is None:
            continue
        coords = {"join_path": path, "alias": source.alias}
        if source.everything:
            lost.extend(f"{target}.{m}" for (c, m) in state.members if c == target)
            names_ex = [n for n in source.excludes if state.gone(target, n)]
            if names_ex:
                excludes.append({**coords, "names": names_ex})
        else:
            names = [
                i.name for i in source.includes or () if state.gone(target, i.name)
            ]
            if names and len(names) == len(source.includes or ()):
                removed_sources.append({**coords, "names": names})
            else:
                if names:
                    includes.append({**coords, "names": names})
                names_ex = [n for n in source.excludes if state.gone(target, n)]
                if names_ex:
                    excludes.append({**coords, "names": names_ex})
    if not (includes or excludes or lost or broken or removed_sources):
        return None
    emptied_view = bool(
        view.sources and len(removed_sources) + len(broken) >= len(view.sources)
    )
    return {
        "whole_model": False,
        "element": "view",
        "includes": includes,
        "excludes": excludes,
        "lost": sorted(lost),
        "broken_sources": broken,
        "removed_sources": removed_sources,
        "empties_view": emptied_view,
    }


def _path_cubes(source: ViewSource, state: _State) -> list[str]:
    reached = [source.join_path[0]]
    cube = state.cubes.get(source.join_path[0])
    for segment in source.join_path[1:]:
        join = cube.join_to(segment) if cube else None
        if join is None:
            break
        reached.append(join.target)
        cube = state.cubes.get(join.target)
    return reached


def _target(source: ViewSource, state: _State) -> str | None:
    reached = _path_cubes(source, state)
    return reached[-1] if len(reached) == len(source.join_path) else None


def _view_finding(view: View, removal: dict[str, Any]) -> DriftFinding:
    parts = [
        (
            f"the source(s) "
            f"{', '.join(s['join_path'] for s in removal['removed_sources'])}, "
            "whose includes all go"
        )
        if removal.get("removed_sources")
        else "",
        f"includes {', '.join(n for i in removal['includes'] for n in i['names'])}"
        if removal["includes"]
        else "",
        f"excludes {', '.join(n for e in removal['excludes'] for n in e['names'])}"
        if removal["excludes"]
        else "",
        f'members {", ".join(removal["lost"])} through `includes: "*"`'
        if removal["lost"]
        else "",
        f"the source(s) {', '.join(removal['broken_sources'])}, whose join path "
        "walks something that goes"
        if removal["broken_sources"]
        else "",
    ]
    return DriftFinding(
        axis="schema",
        code="declared_dependency_dropped",
        identifier=None,
        severity="medium",
        detail=(
            f"view '{view.name}' would lose "
            + "; ".join(p for p in parts if p)
            + ", because the cube members they expose were dropped"
        ),
        data={
            "semantic_model": view.name,
            "model_file": view.file,
            "removal": removal,
        },
    )


def _consequence(removal: Mapping[str, Any]) -> str:
    """What else goes from the same cube, for a finding's sentence."""

    parts = [
        f"{label} {', '.join(items)}"
        for label, items in (
            ("dimensions", removal.get("dimensions") or []),
            ("measures", removal.get("measures") or []),
            ("segments", removal.get("segments") or []),
        )
        if items
    ]
    if removal.get("joins"):
        parts.append(f"joins to {', '.join(removal['joins'])}")
    return f"; the cube would lose {'; '.join(parts)}" if parts else ""
