"""Metric Views held to the warehouse schema, on dex's own metadata (#1259).

The schema half of `maintain` under `semantic.vendor: databricks`. Databricks
checks a Metric View against its tables only when the view is created or
queried, in a workspace dex never deploys to, so the rules run here instead, on
the datasets dex captured through its own connection. Nothing here opens a
connection or reads what a workspace has deployed: it is handed the datasets,
never an adapter.

What it asks of each view, for its ``source`` and every join's ``source`` (the
relation :mod:`.catalog` resolved, matched the way the active connector folds
identifiers):

- **Is the relation still there?** A view's source the baseline listed and the
  warehouse no longer does is ``declared_relation_missing`` for the whole view;
  a join's, for that join. One neither lists is a note, not a finding: a view on
  a relation this connection was never scoped to has not lost anything.
- **Does every column an element reads still exist?** A field, a measure, the
  view's ``filter``, a join's ``on`` or ``using``, and a query ``source``'s own
  columns, each through :mod:`.lineage`. A missing one is
  ``declared_column_missing``, naming the view, the element and the column. An
  expression dex cannot read is named in a note as unchecked, never passed.
- **Does the warehouse type still fit the declared ``format``?** A ``date`` or
  ``date_time`` format on a column that is not temporal, a number, currency,
  percentage or byte format on text, and a ``SUM`` or ``AVG`` over text are
  ``declared_type_conflict``. A warehouse type dex cannot place never conflicts
  (:mod:`..semantic_types`).
- **What else goes with it?** Everything that read a removed element, to a
  fixpoint: a field reading an earlier field, a measure reading a field or
  another measure through ``MEASURE()``, a window ordered by a field, a field or
  measure reading through a removed join, and a join nested under one. One
  ``declared_dependency_dropped`` per view that loses something only that way.

Each finding carries its view's removal (``data.removal``), which ``maintain
reconcile`` turns into a `semantic databricks update` plan (:mod:`.removal`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..adapters import fold_identifier
from ..cache import match_identifier
from ..maintain.drift import DriftFinding
from ..semantic_types import bucket
from .model import FIELD, MEASURE, Join, Member, MetricView

if TYPE_CHECKING:
    from ..cache import Dataset
    from .catalog import Resolution, Table
    from .loader import LoadResult

__all__ = ["declared_schema_drift", "needed_kind", "view_digest"]

_SOURCE: tuple[str, ...] = ()

#: Format types that display a number, so a text column cannot carry one.
_NUMERIC_FORMATS = frozenset({"number", "currency", "percentage", "byte"})
#: Format types that display a date or a timestamp.
_TEMPORAL_FORMATS = frozenset({"date", "date_time"})
#: Aggregations that add their column up, so a text column cannot back one.
_SUMMING = frozenset({"sum", "avg", "mean"})
#: Aggregations whose result is a value of the column itself, so the measure's
#: ``format`` describes the column's type.
_PASSING = frozenset({"min", "max", "any_value", "first", "last", "median", "mode"})


def view_digest(view: MetricView) -> str:
    """The fingerprint's content hash of one view as parsed."""

    from .snapshot import _hash

    return _hash(view.raw)


def needed_kind(
    member: Member, warehouse_type: str | None, agg: str | None = None
) -> tuple[str, str] | None:
    """``(needed kind, what declares it)`` where the warehouse type plainly
    cannot carry what ``member`` declares about it, else None.

    ``agg`` is a measure's aggregation, where it is one aggregate over one
    column; a field passes None. What declares it is ``format`` or the
    aggregation, for the finding's sentence.
    """

    found = bucket(warehouse_type)
    if found is None:
        return None
    if member.kind == MEASURE and agg is not None:
        if agg.lower() in _SUMMING and found == "text":
            return "numeric", agg.upper()
        if agg.lower() not in _PASSING:
            return None
    kind = (member.format or {}).get("type")
    if kind in _TEMPORAL_FORMATS and found != "temporal":
        return "temporal", f"format {kind}"
    if kind in _NUMERIC_FORMATS and found == "text":
        return "numeric", f"format {kind}"
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
        matching ignoring case, which is how Databricks resolves a name."""

        if column in self.types:
            return column
        found = self.folded.get(column.lower(), [])
        return found[0] if len(found) == 1 else None


@dataclass
class _Cause:
    """Why one element goes: a column of its own, or what it read."""

    code: str
    column: str | None = None
    identifier: str | None = None
    warehouse_type: str | None = None
    needs: str | None = None
    declared: str | None = None
    reason: str | None = None


@dataclass
class _Reads:
    """What one element reads: columns by owner, fields, measures, and whether
    any of it could not be read."""

    columns: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    fields: set[str] = field(default_factory=set)
    measures: set[str] = field(default_factory=set)
    owners: set[tuple[str, ...]] = field(default_factory=set)
    unread: bool = False


@dataclass
class _ViewState:
    """What goes from one view, and what dex will not cut on its own."""

    view: MetricView
    whole: str | None = None
    members: dict[str, _Cause] = field(default_factory=dict)
    formats: dict[str, _Cause] = field(default_factory=dict)
    joins: dict[tuple[str, ...], _Cause] = field(default_factory=dict)
    held: list[dict[str, Any]] = field(default_factory=list)

    def member_gone(self, name: str) -> bool:
        return name.lower() in self.members

    def path_gone(self, path: tuple[str, ...]) -> tuple[str, ...] | None:
        """The removed join ``path`` reads through, or None."""

        lowered = tuple(p.lower() for p in path)
        for gone in self.joins:
            if lowered[: len(gone)] == gone:
                return gone
        return None


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

    notes: list[str] = []
    current_ids = [d.identifier for d in current]
    baseline_ids = [d.identifier for d in baseline]
    by_id = {d.identifier: d for d in current}

    def fold(column: str) -> str:
        return fold_identifier(connector, column) or column

    findings: list[DriftFinding] = []
    for view in loaded.views:
        state = _ViewState(view=view)
        tables: dict[tuple[str, ...], _Live] = {}
        unread: list[str] = []
        unchecked_tables: set[tuple[str, ...]] = set()

        # Each table the view reads: its source and every join's.
        owners: list[tuple[tuple[str, ...], str, str]] = [
            (_SOURCE, "source", view.source)
        ]
        owners += [(j.path, f"joins['{j.dotted}']", j.source) for j in view.all_joins()]
        for path, element, written in owners:
            table = resolved.table(view.name, path)
            physical = table.relation or table.gate
            what = "its source" if path == _SOURCE else f"join '{'.'.join(path)}'"
            if physical is None:
                notes.append(
                    f"view '{view.name}': {what} carries no physical relation dex "
                    "can read, so what reads it was not compared to the warehouse"
                )
                unchecked_tables.add(path)
                continue
            matches = match_identifier(physical, current_ids)
            if len(matches) > 1:
                notes.append(
                    f"view '{view.name}': {what} reads {physical}, which matches "
                    f"several warehouse objects ({', '.join(matches)}), so what "
                    "reads it was not compared"
                )
                unchecked_tables.add(path)
                continue
            if not matches:
                gone = match_identifier(physical, baseline_ids)
                if len(gone) == 1:
                    if path == _SOURCE:
                        state.whole = gone[0]
                    else:
                        state.joins[_lower(path)] = _Cause(
                            code="declared_relation_missing", identifier=gone[0]
                        )
                else:
                    notes.append(
                        f"view '{view.name}': {what} reads {physical}, which "
                        "neither this connection nor the baseline lists, so what "
                        "reads it was not compared. A relation outside the "
                        "connection's scope is not reported as dropped"
                    )
                    unchecked_tables.add(path)
                continue
            dataset = by_id[matches[0]]
            if not dataset.columns:
                notes.append(
                    f"dex has no column detail for {dataset.identifier}, so view "
                    f"'{view.name}' was not compared against it"
                )
                unchecked_tables.add(path)
                continue
            live = _Live(
                identifier=dataset.identifier,
                types={c.name: c.data_type for c in dataset.columns},
            )
            tables[path] = live
            # A query source reads columns of its table before anything in the
            # view does; one it lost breaks the source itself.
            if table.relation is None or table.columns is not None:
                missing = _query_columns_missing(
                    written, live, lineage, f"view '{view.name}': {element}", notes
                )
                if missing:
                    state.held.append(
                        {
                            "reason": "source_query_reads_dropped",
                            "element": element,
                            "column": missing,
                            "identifier": live.identifier,
                        }
                    )
        if state.whole is not None:
            findings.append(_whole_finding(view, state.whole))
            continue

        def column_cause(
            path: tuple[str, ...],
            column: str,
            view: MetricView = view,
            tables: Mapping[tuple[str, ...], _Live] = tables,
        ) -> tuple[_Cause | None, bool]:
            """Why ``column`` of the table at ``path`` is gone, and whether it
            could be checked at all."""

            table = resolved.table(view.name, path)
            through = _through(table, fold(column))
            live = tables.get(path)
            if live is None or through is None:
                return None, False
            if live.find(through) is None:
                return (
                    _Cause(
                        code="declared_column_missing",
                        column=through,
                        identifier=live.identifier,
                    ),
                    True,
                )
            return None, True

        # The view's filter: a population, never cut by dex.
        filter_reads = _Reads()
        if view.filter:
            reads = filter_reads = _reads(view, view.filter, None, lineage)
            for owner, column in reads.columns:
                cause, _checked = column_cause(owner, column)
                if cause is not None:
                    state.held.append(
                        {
                            "reason": "filter_reads_dropped",
                            "element": "filter",
                            "column": cause.column,
                            "identifier": cause.identifier,
                        }
                    )
                    break
            if reads.unread:
                unread.append("filter")

        # Joins keyed on a column either side lost.
        for join in view.all_joins():
            if _lower(join.path) in state.joins:
                continue
            for side, column in _join_columns(view, join, lineage):
                if side is None:
                    unread.append(f"joins['{join.dotted}']")
                    break
                cause, _checked = column_cause(side, column)
                if cause is not None:
                    state.joins[_lower(join.path)] = cause
                    break

        # Fields and measures that lost a column of their own, or whose type no
        # longer fits what they declare.
        reads_of: dict[str, _Reads] = {}
        earlier: list[str] = []
        for member in (*view.fields, *view.measures):
            reads = _reads(
                view,
                member.expr,
                member,
                lineage,
                fields=earlier
                if member.kind == FIELD
                else [m.name for m in view.fields],
            )
            reads_of[member.name.lower()] = reads
            if member.kind == FIELD:
                earlier.append(member.name)
            if reads.unread:
                unread.append(_element(view, member))
            for owner, column in reads.columns:
                cause, _checked = column_cause(owner, column)
                if cause is not None:
                    state.members[member.name.lower()] = cause
                    break
            if member.name.lower() in state.members:
                continue
            conflict = _type_conflict(view, member, resolved, tables, fold, lineage)
            if conflict is not None:
                if conflict.declared and conflict.declared.startswith("format"):
                    state.formats[member.name.lower()] = conflict
                else:
                    state.members[member.name.lower()] = conflict

        _cascade(state, reads_of)
        # A filter reading through a join that goes would be left dangling.
        for owner in sorted(filter_reads.owners):
            gone = state.path_gone(owner)
            if gone is not None and not any(
                h["reason"] == "filter_reads_dropped" for h in state.held
            ):
                state.held.append(
                    {
                        "reason": "filter_reads_removed_join",
                        "element": "filter",
                        "join": ".".join(gone),
                    }
                )
                break
        if unread:
            notes.append(
                f"view '{view.name}': dex could not read which columns "
                f"{', '.join(sorted(set(unread)))} read (SQL it could not parse, or "
                "a qualifier naming no join), so they were not checked against "
                "the warehouse"
            )
        findings.extend(_view_findings(view, state, tables))
    return findings, notes


def _lower(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p.lower() for p in path)


def _through(table: Table, column: str) -> str | None:
    """A column of a source, through a projection query's output names."""

    if table.columns is None:
        return column
    lowered = {k.lower(): v for k, v in table.columns.items()}
    return lowered.get(column.lower())


def _query_columns_missing(
    source: str, live: _Live, lineage: Any, element: str, notes: list[str]
) -> str | None:
    """The first column a query ``source`` reads that its table lost, or None.

    A table name reads nothing of its own. A query over one table reads every
    column it names, whatever the qualifier, since one table stands behind it.
    """

    if not lineage.is_query(source):
        return None
    reads, ambiguous = lineage.query_columns(source) or ([], [])
    for column in reads:
        if live.find(column) is None:
            return column
    notes.extend(
        f"{element}: the query's `{column}` in GROUP BY or HAVING is both an "
        f"alias and a name {live.identifier} no longer has, so whether it read "
        "the table was not checked"
        for column in ambiguous
        if live.find(column) is None
    )
    return None


def _owner(view: MetricView, qualifier: tuple[str, ...]) -> tuple[str, ...] | None:
    lowered = _lower(qualifier)
    if lowered in ((), ("source",)):
        return _SOURCE
    join = view.join(qualifier)
    return join.path if join else None


def _reads(
    view: MetricView,
    sql: str,
    member: Member | None,
    lineage: Any,
    fields: list[str] | None = None,
) -> _Reads:
    """What ``sql`` reads: physical columns by owning join path, the fields and
    measures it names, and whether any part could not be read."""

    found = _Reads()
    refs = lineage.column_refs(sql)
    if refs is None:
        found.unread = True
        return found
    named = {f.lower(): f for f in fields or ()}
    for ref in refs:
        if not ref.qualifier and ref.column.lower() in named:
            found.fields.add(ref.column.lower())
            continue
        owner = _owner(view, ref.qualifier)
        if owner is None:
            found.unread = True
            continue
        found.owners.add(_lower(owner))
        found.columns.append((owner, ref.column))
    if member is not None and member.kind == MEASURE:
        measures = lineage.measure_refs(sql)
        if measures is None:
            found.unread = True
        else:
            found.measures.update(m.strip("`").lower() for m in measures)
        for spec in member.window:
            order = spec.get("order") if isinstance(spec, Mapping) else None
            if isinstance(order, str):
                found.fields.add(order.strip("`").lower())
    return found


def _join_columns(
    view: MetricView, join: Join, lineage: Any
) -> list[tuple[tuple[str, ...] | None, str]]:
    """Each column a join's condition reads, with the join path owning it
    (``()`` for the source), or None as the owner where it cannot be read."""

    if join.using:
        both: list[tuple[tuple[str, ...] | None, str]] = [
            (join.parent, c) for c in join.using
        ]
        return both + [(join.path, c) for c in join.using]
    refs = lineage.column_refs(join.on or "")
    if refs is None:
        return [(None, "")]
    own = {(join.name.lower(),), _lower(join.path), ()}
    parent_names = (
        {_lower(join.parent), _lower(join.parent[-1:])}
        if join.parent
        else {("source",)}
    )
    found: list[tuple[tuple[str, ...] | None, str]] = []
    for ref in refs:
        qualifier = _lower(ref.qualifier)
        if qualifier in parent_names:
            found.append((join.parent, ref.column))
        elif qualifier in own:
            found.append((join.path, ref.column))
        else:
            owner = _owner(view, ref.qualifier)
            found.append((owner, ref.column))
    return found


def _type_conflict(
    view: MetricView,
    member: Member,
    resolved: Resolution,
    tables: Mapping[tuple[str, ...], _Live],
    fold: Any,
    lineage: Any,
) -> _Cause | None:
    """A conflict between what ``member`` declares and its column's type."""

    found = resolved.members.get((view.name, member.name))
    if found is None or found.owner is None or not found.column:
        return None
    live = tables.get(found.owner)
    if live is None:
        return None
    spelled = live.find(found.column)
    if spelled is None:
        return None
    warehouse_type = live.types.get(spelled)
    agg = found.aggregate.agg if found.aggregate is not None else None
    if member.kind == MEASURE and agg is None:
        return None
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


def _cascade(state: _ViewState, reads_of: Mapping[str, _Reads]) -> None:
    """Remove what read something removed, until nothing more goes."""

    view = state.view
    changed = True
    while changed:
        changed = False
        for join in view.all_joins():
            path_key = _lower(join.path)
            if path_key in state.joins:
                continue
            gone = state.path_gone(join.parent) if join.parent else None
            if gone is not None:
                state.joins[path_key] = _Cause(
                    code="declared_dependency_dropped",
                    reason=f"it hangs off the join {'.'.join(gone)}, which goes",
                )
                changed = True
        for member in (*view.fields, *view.measures):
            key = member.name.lower()
            if key in state.members:
                continue
            reads = reads_of.get(key)
            if reads is None:
                continue
            reason = None
            for owner in reads.owners:
                gone = state.path_gone(owner)
                if gone is not None:
                    reason = f"it reads through the join {'.'.join(gone)}, which goes"
                    break
            if reason is None:
                lost = sorted(n for n in reads.fields if state.member_gone(n))
                if lost:
                    reason = f"it reads the field {lost[0]}, which goes"
            if reason is None:
                lost = sorted(n for n in reads.measures if state.member_gone(n))
                if lost:
                    reason = f"it reads MEASURE({lost[0]}), which goes"
            if reason is not None:
                state.members[key] = _Cause(
                    code="declared_dependency_dropped", reason=reason
                )
                changed = True


def _element(view: MetricView, member: Member) -> str:
    """How the finding names a member: ``fields['status']``, ``measures['revenue']``."""

    section = view.fields_key if member.kind == FIELD else "measures"
    return f"{section}['{member.name}']"


def _whole_finding(view: MetricView, identifier: str) -> DriftFinding:
    return DriftFinding(
        axis="schema",
        code="declared_relation_missing",
        identifier=identifier,
        severity="high",
        detail=(
            f"view '{view.name}' reads {identifier} as its source, which was in "
            "the baseline and is gone from the warehouse, so every field and "
            "measure of the view is unqueryable"
        ),
        data={
            "semantic_model": view.name,
            "model_file": view.file,
            "element": "source",
            "removal": {"whole_model": True},
        },
    )


def _removal(state: _ViewState) -> dict[str, Any] | None:
    """What goes from one view, by section, or None if nothing does."""

    view = state.view
    fields = [m.name for m in view.fields if m.name.lower() in state.members]
    measures = [m.name for m in view.measures if m.name.lower() in state.members]
    # Only the topmost removed join is cut: a nested one goes with its parent.
    joins = [
        j.dotted
        for j in view.all_joins()
        if _lower(j.path) in state.joins
        and not (j.parent and state.path_gone(j.parent))
    ]
    formats = [
        m.name
        for m in (*view.fields, *view.measures)
        if m.name.lower() in state.formats and m.name.lower() not in state.members
    ]
    held = list(state.held)
    for path, cause in state.joins.items():
        if cause.code == "declared_relation_missing":
            held.append(
                {
                    "reason": "join_relation_missing",
                    "element": f"joins['{'.'.join(path)}']",
                    "identifier": cause.identifier,
                }
            )
    removed = {n.lower() for n in (*fields, *measures)}
    if view.materialization and removed:
        named = _materialized_names(view.materialization)
        if named & removed:
            held.append(
                {
                    "reason": "materialization_reads_removed",
                    "element": "materialization",
                    "names": sorted(named & removed),
                }
            )
    if removed and len(removed) >= len(view.fields) + len(view.measures):
        held.append({"reason": "empties_view", "element": "(root)"})
    if not (fields or measures or joins or formats or held):
        return None
    removal: dict[str, Any] = {
        "whole_model": False,
        "element": "view",
        "fields_key": view.fields_key,
        # The view as detected, so reconcile can tell a view edited since the
        # report from the one the removal was computed for.
        "view_sha256": view_digest(view),
        "fields": fields,
        "measures": measures,
        "joins": joins,
        "formats": formats,
    }
    if held:
        removal["held"] = held
    return removal


def _materialized_names(materialization: Mapping[str, Any]) -> set[str]:
    """Every member name a materialized view lists, lower-cased."""

    names: set[str] = set()
    for entry in materialization.get("materialized_views") or []:
        if not isinstance(entry, Mapping):
            continue
        for key in ("dimensions", "measures"):
            for name in entry.get(key) or []:
                if isinstance(name, str):
                    names.add(name.strip("`").lower())
    return names


def _view_findings(
    view: MetricView, state: _ViewState, tables: Mapping[tuple[str, ...], _Live]
) -> list[DriftFinding]:
    removal = _removal(state)
    if removal is None:
        return []
    source = tables.get(_SOURCE)
    context = {"semantic_model": view.name, "model_file": view.file, "removal": removal}
    consequence = _consequence(removal)
    findings: list[DriftFinding] = []
    cascaded: list[str] = []

    for held in removal.get("held") or []:
        if held["reason"] == "source_query_reads_dropped":
            findings.append(
                _column_finding(
                    view,
                    held["element"],
                    "the query",
                    held["column"],
                    held["identifier"],
                    context,
                    consequence,
                )
            )
        elif held["reason"] == "filter_reads_dropped":
            findings.append(
                _column_finding(
                    view,
                    "filter",
                    "the filter",
                    held["column"],
                    held["identifier"],
                    context,
                    consequence,
                )
            )
    for join in view.all_joins():
        cause = state.joins.get(_lower(join.path))
        if cause is None:
            continue
        element = f"joins['{join.dotted}']"
        if cause.code == "declared_column_missing":
            findings.append(
                _column_finding(
                    view,
                    element,
                    f"join '{join.dotted}'",
                    cause.column,
                    cause.identifier,
                    context,
                    consequence,
                )
            )
        elif cause.code == "declared_relation_missing":
            findings.append(
                DriftFinding(
                    axis="schema",
                    code="declared_relation_missing",
                    identifier=cause.identifier,
                    severity="high",
                    detail=(
                        f"view '{view.name}' joins {cause.identifier} as "
                        f"'{join.dotted}', which was in the baseline and is gone "
                        "from the warehouse, so the join and everything read "
                        f"through it is unqueryable{consequence}"
                    ),
                    data={**context, "element": element},
                )
            )
        else:
            cascaded.append(f"join {join.dotted}: {cause.reason}")
    for member in (*view.fields, *view.measures):
        key = member.name.lower()
        element = _element(view, member)
        cause = state.members.get(key) or state.formats.get(key)
        if cause is None:
            continue
        if cause.code == "declared_dependency_dropped":
            cascaded.append(f"{element}: {cause.reason}")
        elif cause.code == "declared_column_missing":
            findings.append(
                _column_finding(
                    view,
                    element,
                    f"{member.kind} '{member.name}'",
                    cause.column,
                    cause.identifier,
                    {
                        **context,
                        "declared_member": member.name,
                        "member_kind": member.kind,
                    },
                    consequence,
                )
            )
        else:
            findings.append(_type_finding(view, member, element, cause, context))
    if cascaded:
        findings.append(
            DriftFinding(
                axis="schema",
                code="declared_dependency_dropped",
                identifier=source.identifier if source else None,
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
    view: MetricView,
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
    view: MetricView,
    member: Member,
    element: str,
    cause: _Cause,
    context: dict[str, Any],
) -> DriftFinding:
    format_only = bool(cause.declared and cause.declared.startswith("format"))
    # Databricks refuses `CREATE VIEW` on a format its column cannot carry
    # (COLUMN_FORMAT_INCOMPATIBLE_WITH_COLUMN_TYPE), so either kind breaks the
    # view, not only its display.
    then = (
        ", which Databricks refuses when the view is created; the format would "
        "be removed and the member kept"
        if format_only
        else "; the measure would be removed"
    )
    return DriftFinding(
        axis="schema",
        code="declared_type_conflict",
        identifier=cause.identifier,
        column=cause.column,
        severity="high",
        detail=(
            f"view '{view.name}' {element} declares {cause.declared} over "
            f"{cause.identifier}.{cause.column}, which is {cause.warehouse_type}, "
            f"not {cause.needs}{then}"
        ),
        data={
            **context,
            "element": element,
            "declared_member": member.name,
            "member_kind": member.kind,
            "declared": cause.declared,
            "warehouse_type": cause.warehouse_type,
            "needs": cause.needs,
        },
    )


def _consequence(removal: Mapping[str, Any]) -> str:
    """What else goes from the same view, for a finding's sentence."""

    parts = [
        f"{label} {', '.join(items)}"
        for label, items in (
            ("fields", removal.get("fields") or []),
            ("measures", removal.get("measures") or []),
            ("joins", removal.get("joins") or []),
        )
        if items
    ]
    return f"; the view would lose {'; '.join(parts)}" if parts else ""
