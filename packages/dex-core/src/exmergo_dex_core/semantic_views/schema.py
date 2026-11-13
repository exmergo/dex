"""Semantic Views held to the warehouse schema, on dex's own metadata (#1293).

The schema half of `maintain` under `semantic.vendor: snowflake`. Snowflake
compares a Semantic View against its tables only when one is created, and dex
never creates one, so the rules run here instead, on the datasets dex captured
through its own connection. Nothing here opens a connection or reads what an
account has deployed: it is handed the datasets, never an adapter.

What it asks of each logical table (its ``base_table``, or the one table a
``definition`` query reads, matched the way the active connector folds
identifiers):

- **Is the relation still there?** One the baseline listed and the warehouse no
  longer does is ``declared_relation_missing`` for the logical table. One
  neither lists is a note, not a finding: a view on a relation this connection
  was never scoped to has not lost anything.
- **Does every column an element reads still exist?** A dimension, time
  dimension, fact or metric, a relationship's columns on either side, a
  ``primary_key`` or ``unique_keys`` entry, and a ``definition`` query. A missing
  one is ``declared_column_missing``, naming the view, the element and the
  column.
- **Does the warehouse type still fit what is declared?** A time dimension on a
  column that is not temporal, a ``SUM`` or ``AVG`` over text, and a declared
  ``data_type`` in another family than the warehouse's are
  ``declared_type_conflict``. A warehouse type dex cannot place never conflicts
  (:mod:`..semantic_types`).
- **What else goes with it?** The view with those elements cut is read again by
  dex's reader, the same one authoring validates with, to a fixpoint: a member
  or metric that reads a removed one, a member that reads across a removed
  relationship, and a metric whose ``using`` names one. Those are
  ``declared_dependency_dropped``, one per view, each with the reader's reason.

A member the reader already refused (an expression it could not parse) is
named in a note as unchecked, never passed. Each finding carries the view's
removal (:func:`_removal`), which :mod:`.removal` turns into an edit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..adapters import fold_identifier
from ..cache import match_identifier
from ..maintain.drift import DriftFinding
from ..semantic_types import bucket
from .model import DERIVED, METRIC, TIME_DIMENSION, Member, SemanticView
from .removal import pruned

if TYPE_CHECKING:
    from ..cache import Dataset
    from .catalog import Resolution
    from .loader import LoadResult

__all__ = ["declared_schema_drift", "needed_kind", "view_digest"]

#: Aggregations (as :mod:`.lineage` names them) that add their column up, so a
#: text column cannot back one.
_SUMMING = frozenset({"sum", "average"})
#: How a finding spells an aggregation: the SQL function, not the catalog's word.
_SQL_NAMES = {"average": "AVG"}

#: How many times the reduced view is read again before dex stops looking for
#: more of the cascade. Each round removes at least one element, and a view
#: has a few dozen.
_ROUNDS = 64

_SECTION_OF = {
    "dimension": "dimensions",
    "time_dimension": "time_dimensions",
    "fact": "facts",
    "metric": "metrics",
    DERIVED: "metrics",
}

_Key = tuple[str | None, str]


def view_digest(view: SemanticView) -> str:
    """The view as parsed, hashed, so reconcile can tell a view edited since the
    report from the one the removal was computed for. Every key counts, a
    ``data_type`` included, which the fingerprint leaves out."""

    return hashlib.sha256(
        json.dumps(view.raw, sort_keys=True, default=str).encode()
    ).hexdigest()


def needed_kind(
    member: Member, warehouse_type: str | None, agg: str | None = None
) -> tuple[str, str] | None:
    """``(needed kind, what declares it)`` where the warehouse type plainly
    cannot carry what ``member`` declares about it, else None.

    ``agg`` is a table metric's aggregation, where it is one aggregate over one
    column. The declared ``data_type`` is judged separately
    (:func:`_data_type_conflict`), since it is cut rather than the member.
    """

    found = bucket(warehouse_type)
    if found is None:
        return None
    if member.kind == METRIC:
        if agg is not None and agg.lower() in _SUMMING and found == "text":
            return "numeric", _SQL_NAMES.get(agg.lower(), agg.upper())
        return None
    if member.kind == TIME_DIMENSION and found != "temporal":
        return "temporal", "a time dimension"
    return None


@dataclass
class _Live:
    """One relation as the warehouse holds it now."""

    identifier: str
    types: dict[str, str]
    folded: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.types:
            self.folded.setdefault(name.lower(), []).append(name)

    def find(self, column: str) -> str | None:
        """The warehouse's spelling of ``column``: exact, else the one column
        matching ignoring case, which is how Snowflake resolves an unquoted
        name."""

        if column in self.types:
            return column
        found = self.folded.get(column.lower(), [])
        return found[0] if len(found) == 1 else None


@dataclass
class _Cause:
    """Why one element goes, or what about it conflicts."""

    code: str
    column: str | None = None
    identifier: str | None = None
    warehouse_type: str | None = None
    needs: str | None = None
    declared: str | None = None
    reason: str | None = None


@dataclass
class _ViewState:
    """What goes from one view, and what dex will not cut on its own."""

    view: SemanticView
    members: dict[_Key, _Cause] = field(default_factory=dict)
    data_types: dict[_Key, _Cause] = field(default_factory=dict)
    relationships: dict[str, _Cause] = field(default_factory=dict)
    #: Conflicts reported and left for a person, never cut.
    advised: dict[_Key, _Cause] = field(default_factory=dict)
    #: Logical tables whose relation is gone, lowered, to the baseline's id.
    tables_gone: dict[str, str] = field(default_factory=dict)
    held: list[dict[str, Any]] = field(default_factory=list)


def _key(member: Member) -> _Key:
    return ((member.table or "").lower() or None, member.name.lower())


def _element(member: Member) -> str:
    """How a finding names a member, as the reader's diagnostics do."""

    if member.table is None:
        return f"metrics['{member.name}']"
    return f"tables['{member.table}'].{_SECTION_OF[member.kind]}['{member.name}']"


def _members(view: SemanticView) -> list[Member]:
    return [*(m for t in view.tables for m in t.members()), *view.metrics]


def declared_schema_drift(
    loaded: LoadResult,
    resolved: Resolution,
    current: list[Dataset],
    baseline: list[Dataset],
    *,
    connector: str | None,
) -> tuple[list[DriftFinding], list[str]]:
    """Every view the warehouse now contradicts, as findings."""

    from . import lineage
    from .loader import resolve_read

    notes: list[str] = []
    current_ids = [d.identifier for d in current]
    baseline_ids = [d.identifier for d in baseline]
    by_id = {d.identifier: d for d in current}

    def fold(column: str) -> str:
        return fold_identifier(connector, column) or column

    findings: list[DriftFinding] = []
    for view in loaded.views:
        state = _ViewState(view=view)
        live_tables: dict[str, _Live] = {}
        # Tables whose members read a query's outputs rather than the table's
        # own columns, so only the query is compared.
        outputs_only: set[str] = set()

        for table in view.tables:
            lowered = table.name.lower()
            what = f"table '{table.name}'"
            physical = resolved.relation(view, table.name) or resolved.gate(
                view, table.name
            )
            definition_reads: list[str] | None = None
            if table.definition is not None:
                found = lineage.definition_reads(table.definition)
                if found is None:
                    notes.append(
                        f"view '{view.name}': {what}: dex could not read which "
                        "table and columns its `definition` query reads, so it was "
                        "not compared to the warehouse"
                    )
                    continue
                name, definition_reads = found
                if physical is None:
                    outputs_only.add(lowered)
                    physical = name
            if physical is None:
                notes.append(
                    f"view '{view.name}': {what} carries no physical relation dex "
                    "can read, so what reads it was not compared to the warehouse"
                )
                continue
            matches = match_identifier(physical, current_ids)
            if len(matches) > 1:
                notes.append(
                    f"view '{view.name}': {what} reads {physical}, which matches "
                    f"several warehouse objects ({', '.join(matches)}), so what "
                    "reads it was not compared"
                )
                continue
            if not matches:
                gone = match_identifier(physical, baseline_ids)
                if len(gone) == 1:
                    state.tables_gone[lowered] = gone[0]
                    state.held.append(
                        {
                            "reason": "table_relation_missing",
                            "element": f"tables['{table.name}']",
                            "identifier": gone[0],
                        }
                    )
                else:
                    notes.append(
                        f"view '{view.name}': {what} reads {physical}, which "
                        "neither this connection nor the baseline lists, so what "
                        "reads it was not compared. A relation outside the "
                        "connection's scope is not reported as dropped"
                    )
                continue
            dataset = by_id[matches[0]]
            if not dataset.columns:
                notes.append(
                    f"dex has no column detail for {dataset.identifier}, so view "
                    f"'{view.name}' {what} was not compared against it"
                )
                continue
            live = _Live(
                identifier=dataset.identifier,
                types={c.name: c.data_type for c in dataset.columns},
            )
            live_tables[lowered] = live
            for column in definition_reads or []:
                if live.find(fold(column)) is None:
                    state.held.append(
                        {
                            "reason": "definition_reads_dropped",
                            "element": f"tables['{table.name}'].base_table.definition",
                            "column": fold(column),
                            "identifier": live.identifier,
                        }
                    )
                    break
            # The keys: the table's grain, never cut by dex.
            for element, columns in [
                *(
                    [(f"tables['{table.name}'].primary_key", table.primary_key)]
                    if table.primary_key and lowered not in outputs_only
                    else []
                ),
                *(
                    (f"tables['{table.name}'].unique_keys[{i}]", key)
                    for i, key in enumerate(table.unique_keys)
                    if lowered not in outputs_only
                ),
            ]:
                for column in columns:
                    if live.find(fold(column)) is None:
                        state.held.append(
                            {
                                "reason": "key_reads_dropped",
                                "element": element,
                                "column": fold(column),
                                "identifier": live.identifier,
                            }
                        )
                        break

        # Members: a column of their own table they read and it lost, then a
        # type that no longer fits what they declare.
        for table in view.tables:
            lowered = table.name.lower()
            live = live_tables.get(lowered)
            if live is None or lowered in outputs_only:
                continue
            for member in table.members():
                for read in lineage.reads(member.expr) or []:
                    target = resolve_read(view, member, read.qualifier, read.name)
                    if target.column is None:
                        continue
                    if live.find(fold(target.column)) is None:
                        state.members[_key(member)] = _Cause(
                            code="declared_column_missing",
                            column=fold(target.column),
                            identifier=live.identifier,
                        )
                        break
                if _key(member) in state.members:
                    continue
                conflict = _type_conflict(view, member, resolved, live, fold)
                if conflict is not None and member.kind == TIME_DIMENSION:
                    # Snowflake creates and queries it, returning the column's
                    # values: wrong, but answering, so it is not cut (#1293).
                    state.advised[_key(member)] = conflict
                    continue
                if conflict is not None:
                    state.members[_key(member)] = conflict
                    continue
                conflict = _data_type_conflict(view, member, resolved, live, fold)
                if conflict is not None:
                    state.data_types[_key(member)] = conflict

        # Relationships keyed on a column either side lost.
        for rel in view.relationships:
            if {rel.left_table.lower(), rel.right_table.lower()} & set(
                state.tables_gone
            ):
                continue
            sides = [(rel.left_table, c) for c in rel.left_columns] + [
                (rel.right_table, c) for c in rel.right_columns
            ]
            for table_name, column in sides:
                live = live_tables.get(table_name.lower())
                if live is None or table_name.lower() in outputs_only:
                    continue
                if live.find(fold(column)) is None:
                    state.relationships[rel.name.lower()] = _Cause(
                        code="declared_column_missing",
                        column=fold(column),
                        identifier=live.identifier,
                    )
                    break

        _cascade(state)
        unread = sorted(
            {
                d.element
                for d in loaded.diagnostics
                if d.file == view.file and d.rule == "unparsed_expression"
            }
        )
        if unread:
            notes.append(
                f"view '{view.name}': dex could not read which columns "
                f"{', '.join(unread)} read (SQL it could not parse), so they were "
                "not checked against the warehouse"
            )
        findings.extend(_view_findings(state, live_tables))
    return findings, notes


def _type_conflict(
    view: SemanticView,
    member: Member,
    resolved: Resolution,
    live: _Live,
    fold: Any,
) -> _Cause | None:
    """A conflict between what ``member`` is and its column's type."""

    found = resolved.member(view, member)
    if not found.column:
        return None
    spelled = live.find(fold(found.column))
    if spelled is None:
        return None
    warehouse_type = live.types.get(spelled)
    agg = found.aggregate.agg if found.aggregate is not None else None
    needs = needed_kind(member, warehouse_type, agg)
    if needs is None:
        return None
    return _Cause(
        code="declared_type_conflict",
        column=spelled,
        identifier=live.identifier,
        warehouse_type=warehouse_type,
        needs=needs[0],
        declared=needs[1],
    )


def _data_type_conflict(
    view: SemanticView,
    member: Member,
    resolved: Resolution,
    live: _Live,
    fold: Any,
) -> _Cause | None:
    """A row-level member's declared ``data_type`` in another family than its
    column's warehouse type. A metric's is left alone: an aggregate's type is
    not its column's."""

    if not member.row_level or not member.data_type:
        return None
    found = resolved.member(view, member)
    if not found.column:
        return None
    spelled = live.find(fold(found.column))
    if spelled is None:
        return None
    warehouse_type = live.types.get(spelled)
    declared, actual = bucket(member.data_type), bucket(warehouse_type)
    if declared is None or actual is None or declared == actual:
        return None
    return _Cause(
        code="declared_type_conflict",
        column=spelled,
        identifier=live.identifier,
        warehouse_type=warehouse_type,
        needs=declared,
        declared=f"data_type {member.data_type}",
    )


def _cascade(state: _ViewState) -> None:
    """Remove what the reader would refuse once the direct removals are made,
    until nothing more goes.

    The reduced view is written out and read again by the same reader
    authoring validates with, so the cascade is exactly what Snowflake's rules,
    as dex ports them, refuse: a member reading a removed member, a member
    reading across a relationship that went, a metric whose ``using`` names it.
    """

    from .loader import dump_yaml, validate_view_contents

    view = state.view
    if state.tables_gone or not (
        state.members or state.relationships or state.data_types
    ):
        return
    before = {_key(m): m for m in _members(view)}
    relationships = {r.name.lower() for r in view.relationships}
    reads = _member_reads(view)
    for _round in range(_ROUNDS):
        # A member reading a removed member first: once that member goes, the
        # reader takes the bare name for a base column of the same name, which
        # it cannot tell is gone too.
        added = True
        while added:
            added = False
            for key in before:
                if key in state.members:
                    continue
                lost = next((k for k in reads.get(key, ()) if k in state.members), None)
                if lost is not None:
                    state.members[key] = _Cause(
                        code="declared_dependency_dropped",
                        reason=f"it reads `{_token(before[lost])}`, which goes",
                    )
                    added = True
        removal = _removal_lists(state)
        text = dump_yaml(pruned(view.raw, removal))
        result = validate_view_contents({view.file: text})
        reread = next(iter(result.views), None)
        if reread is None:
            message = next(
                (d.message for d in result.errors), "the reader refused the view"
            )
            state.held.append({"reason": "would_not_validate", "message": message})
            return
        why = {d.element: d.message for d in result.errors}
        after = {_key(m) for m in _members(reread)}
        added = False
        for key, member in before.items():
            if key in after or key in state.members:
                continue
            state.members[key] = _Cause(
                code="declared_dependency_dropped",
                reason=why.get(
                    _element(member), "the reader refuses it once the rest goes"
                ),
            )
            added = True
        kept = {r.name.lower() for r in reread.relationships}
        for name in relationships - kept - set(state.relationships):
            state.relationships[name] = _Cause(
                code="declared_dependency_dropped",
                reason=why.get(
                    f"relationships['{name}']",
                    "the reader refuses it once the rest goes",
                ),
            )
            added = True
        if not added:
            break
    if before and set(before) <= set(state.members):
        state.held.append({"reason": "empties_view", "element": "(root)"})


def _member_reads(view: SemanticView) -> dict[_Key, list[_Key]]:
    """Each member to the members its expression reads, and the dimensions a
    metric is non-additive by."""

    from . import lineage
    from .loader import resolve_read

    found: dict[_Key, list[_Key]] = {}
    for member in _members(view):
        targets = [
            resolve_read(view, member, read.qualifier, read.name)
            for read in lineage.reads(member.expr) or []
        ]
        found[_key(member)] = [
            _key(t.member) for t in targets if t.member is not None
        ] + [
            (str(entry["table"]).lower(), str(entry["dimension"]).lower())
            for entry in member.non_additive
        ]
    return found


def _token(member: Member) -> str:
    return f"{member.table}.{member.name}" if member.table else member.name


def _removal_lists(state: _ViewState) -> dict[str, Any]:
    """The removal's entries, by list, in declaration order."""

    view = state.view
    members = [
        {
            "table": m.table,
            "section": _SECTION_OF[m.kind],
            "name": m.name,
        }
        for m in _members(view)
        if _key(m) in state.members
    ]
    data_types = [
        {"table": m.table, "section": _SECTION_OF[m.kind], "name": m.name}
        for m in _members(view)
        if _key(m) in state.data_types and _key(m) not in state.members
    ]
    relationships = [
        r.name for r in view.relationships if r.name.lower() in state.relationships
    ]
    return {
        "members": members,
        "relationships": relationships,
        "data_types": data_types,
    }


def _removal(state: _ViewState) -> dict[str, Any] | None:
    """What goes from one view, by list, or None if nothing does."""

    lists = _removal_lists(state)
    held = list(state.held)
    if not (any(lists.values()) or held):
        return None
    removal: dict[str, Any] = {
        "whole_model": False,
        "element": "view",
        "view_sha256": view_digest(state.view),
        **lists,
    }
    if held:
        removal["held"] = held
    return removal


def _view_findings(
    state: _ViewState, live_tables: Mapping[str, _Live]
) -> list[DriftFinding]:
    view = state.view
    removal = _removal(state)
    if removal is None and not state.advised:
        return []
    context: dict[str, Any] = {"semantic_model": view.name, "model_file": view.file}
    if removal is not None:
        context["removal"] = removal
    consequence = _consequence(removal or {})
    findings: list[DriftFinding] = []
    cascaded: list[str] = []

    for held in (removal or {}).get("held") or []:
        reason = held.get("reason")
        if reason == "table_relation_missing":
            findings.append(
                DriftFinding(
                    axis="schema",
                    code="declared_relation_missing",
                    identifier=held["identifier"],
                    severity="high",
                    detail=(
                        f"view '{view.name}' {held['element']} reads "
                        f"{held['identifier']}, which was in the baseline and is "
                        "gone from the warehouse, so the logical table and "
                        "everything read from it is unqueryable"
                    ),
                    data={**context, "element": held["element"]},
                )
            )
        elif reason in ("key_reads_dropped", "definition_reads_dropped"):
            what = "the key" if reason == "key_reads_dropped" else "the query"
            findings.append(
                _column_finding(
                    view,
                    held["element"],
                    what,
                    held["column"],
                    held["identifier"],
                    context,
                    consequence,
                )
            )
    for rel in view.relationships:
        cause = state.relationships.get(rel.name.lower())
        if cause is None:
            continue
        element = f"relationships['{rel.name}']"
        if cause.code == "declared_column_missing":
            findings.append(
                _column_finding(
                    view,
                    element,
                    f"relationship '{rel.name}'",
                    cause.column,
                    cause.identifier,
                    context,
                    consequence,
                )
            )
        else:
            cascaded.append(f"{element}: {cause.reason}")
    for member in _members(view):
        element = _element(member)
        cause = (
            state.members.get(_key(member))
            or state.data_types.get(_key(member))
            or state.advised.get(_key(member))
        )
        if cause is None:
            continue
        member_context = {
            **context,
            "declared_member": member.name,
            "member_kind": member.kind,
        }
        if cause.code == "declared_dependency_dropped":
            cascaded.append(f"{element}: {cause.reason}")
        elif cause.code == "declared_column_missing":
            findings.append(
                _column_finding(
                    view,
                    element,
                    f"{member.kind.replace('_', ' ')} '{member.name}'",
                    cause.column,
                    cause.identifier,
                    member_context,
                    consequence,
                )
            )
        else:
            findings.append(_type_finding(view, member, element, cause, member_context))
    if cascaded:
        first = next(iter(live_tables.values()), None)
        findings.append(
            DriftFinding(
                axis="schema",
                code="declared_dependency_dropped",
                identifier=first.identifier if first else None,
                severity="medium",
                detail=(
                    f"view '{view.name}' would lose part of its definition because "
                    f"something it depends on was dropped ({'; '.join(cascaded)})"
                ),
                data={**context, "reasons": cascaded},
            )
        )
    return findings


def _column_finding(
    view: SemanticView,
    element: str,
    what: str,
    column: str | None,
    identifier: str | None,
    context: dict[str, Any],
    consequence: str,
) -> DriftFinding:
    return DriftFinding(
        axis="schema",
        code="declared_column_missing",
        identifier=identifier,
        column=column,
        severity="high",
        detail=(
            f"view '{view.name}' {element} ({what}) reads column '{column}', which "
            f"{identifier} no longer has{consequence}"
        ),
        data={**context, "element": element},
    )


def _type_finding(
    view: SemanticView,
    member: Member,
    element: str,
    cause: _Cause,
    context: dict[str, Any],
) -> DriftFinding:
    # What Snowflake does with each, probed on an account (#1293): it ignores
    # a declared `data_type` and records the column's own, it creates and
    # queries a time dimension over text and returns the text, and it creates
    # a SUM over text and fails every query that reads it.
    advice: str | None = None
    if cause.declared and cause.declared.startswith("data_type"):
        severity = "low"
        then = (
            ". Snowflake ignores a declared data_type and records the column's "
            "own when the view is created, so the file disagrees with what the "
            "account holds; the data_type would be removed and the member kept"
        )
    elif member.kind == TIME_DIMENSION:
        severity = "medium"
        then = (
            ". Snowflake creates the view and answers queries with the column's "
            "values as the time dimension, so it is not removed"
        )
        advice = (
            f"view '{view.name}' {element} is a time dimension over "
            f"{cause.identifier}.{cause.column}, which is {cause.warehouse_type}: "
            "cast its `expr` to a date or timestamp, or make it a dimension, with "
            "`semantic snowflake update`; or restore the column's type. dex does "
            "not remove a member Snowflake still answers for"
        )
    else:
        severity = "high"
        then = (
            ". Snowflake creates the view and fails every query that reads it; "
            f"the {member.kind.replace('_', ' ')} would be removed"
        )
    return DriftFinding(
        axis="schema",
        code="declared_type_conflict",
        identifier=cause.identifier,
        column=cause.column,
        severity=severity,
        detail=(
            f"view '{view.name}' {element} declares {cause.declared} over "
            f"{cause.identifier}.{cause.column}, which is {cause.warehouse_type}, "
            f"not {cause.needs}{then}"
        ),
        data={
            **context,
            "element": element,
            "declared": cause.declared,
            "warehouse_type": cause.warehouse_type,
            "needs": cause.needs,
            **({"advice": advice} if advice else {}),
        },
    )


def _consequence(removal: Mapping[str, Any]) -> str:
    """What else goes from the same view, for a finding's sentence."""

    members = [
        f"{m['table']}.{m['name']}" if m.get("table") else str(m["name"])
        for m in removal.get("members") or []
    ]
    parts = [
        f"{label} {', '.join(items)}"
        for label, items in (
            ("members", members),
            ("relationships", [str(r) for r in removal.get("relationships") or []]),
        )
        if items
    ]
    return f"; the view would lose {'; '.join(parts)}" if parts else ""
