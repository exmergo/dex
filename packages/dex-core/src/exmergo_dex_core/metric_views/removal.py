"""Turning the Metric View schema axis's removals into edits of the view files.

``maintain reconcile`` reads the removal each schema finding recorded (what goes
from one view, :mod:`.schema`) and proposes it as a `semantic databricks update`
plan. Never a write: the edits go through the same validation and plan store
`semantic databricks update` uses, so a reconciled view is read back by the
reader before anything is stored, and `transform apply` is what writes it.

**The edit is surgical.** Each removed field, measure or join is cut out of its
list by the span it occupies, and a ``format`` that contradicts the warehouse is
cut out of its member, so every other byte, comments included, stays as the
author left it. Read and checked as YAML 1.2, the way the reader reads it, so an
``on:`` key stays text. Every edit is checked by re-reading it: it must parse to
the original with exactly those entries gone. Anything that cannot be bounded
that way (a flow-style list, a ``format`` on the line of the dash that opens its
member) is not rewritten: the proposal is advisory instead, and says what to
remove.

**Advisory, never written:**

- a view whose ``source`` is gone, and a join whose ``source`` is gone: a
  vanished table is as often a rename to follow as an element to retire;
- a view ``filter`` reading a dropped column: the filter is the view's
  population, and dropping it changes which rows every measure counts;
- a query ``source`` reading a dropped column: no member removal repairs it;
- a removal a ``materialization`` names, which would leave it dangling;
- a removal that leaves the view no field and no measure, which Databricks
  refuses: removing a whole view is not something reconcile authors;
- a file that already holds a key Databricks refuses (``unknown_key``, or
  ``fields`` beside ``dimensions``): authoring refuses any edit to such a file.

Nothing here imports sqlglot: the removals arrive as plain data on the findings.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml

from ..yaml_spans import block_end, indent_of, sequence_item_span
from .loader import _Loader, read_yaml

if TYPE_CHECKING:
    from ..maintain.drift import DriftFinding
    from ..maintain.reconcile import Proposal
    from ..transform.plans import PlanEdit

__all__ = ["CODES", "apply_removal", "describe", "proposals_and_edits"]

#: Every finding code the schema channel produces, so reconcile can tell its
#: own. Here rather than beside the detector, which imports sqlglot.
CODES = frozenset(
    {
        "declared_column_missing",
        "declared_type_conflict",
        "declared_relation_missing",
        "declared_dependency_dropped",
    }
)

_ROUTE = "`semantic databricks update`"

# One change: cut ``[start, end)`` and write ``replacement`` there.
_Change = tuple[int, int, str]


def proposals_and_edits(
    findings: list[DriftFinding],
    contents: Mapping[str, str],
    strict: Mapping[str, list[str]],
    digests: Mapping[str, str] | None = None,
) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
    """One proposal per finding, and one edit per view file that can take it.

    ``digests`` is each view's content hash now, keyed by file: a removal
    computed for another version of the view (the view was edited after the
    report) is proposed as advisory, never cut, since cutting by name could
    remove a member the edit just repaired.

    ``contents`` is each configured file's current text, keyed as configured.
    ``strict`` names, per file, what it already holds that authoring refuses, so
    its proposals are advisory from the start.
    """

    from ..transform.plans import EditKind, PlanEdit

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    by_file: dict[str, list[DriftFinding]] = {}
    for finding in findings:
        by_file.setdefault(str(finding.data.get("model_file")), []).append(finding)

    for path, group in sorted(by_file.items()):
        # One view per file, so every finding of the file carries one removal.
        removal = next(
            (
                f.data.get("removal")
                for f in group
                if not (f.data.get("removal") or {}).get("whole_model")
            ),
            None,
        )
        whole = [f for f in group if (f.data.get("removal") or {}).get("whole_model")]
        proposals.extend(_advisory(f, path) for f in whole)
        live = [f for f in group if f not in whole]
        if not live or removal is None:
            continue
        detected = removal.get("view_sha256")
        if digests is not None and detected and digests.get(path) != detected:
            warnings.append(
                f"no edit proposed for {path}: the view changed since the drift "
                "report; re-run `maintain check` and reconcile again"
            )
            proposals.extend(
                _proposal(
                    f,
                    "advisory",
                    f"view '{f.data.get('semantic_model')}' ({path}) changed "
                    "since the drift report; re-run `maintain check`, then "
                    "reconcile again",
                )
                for f in live
            )
            continue
        if removal.get("held") or describe(removal) == "nothing":
            proposals.extend(_advisory(f, path) for f in live)
            continue

        text = contents.get(path)
        reason = None
        if text is None:
            reason = f"{path} is not one of the configured Metric View files"
        elif strict.get(path):
            reason = (
                f"{path} already holds what Databricks refuses "
                f"({', '.join(strict[path])}), and {_ROUTE} refuses any edit to a "
                "file that does; fix those first"
            )
        edited = apply_removal(text, removal) if reason is None and text else None
        if reason is None and (edited is None or edited == text):
            reason = (
                f"dex could not locate every entry to remove in {path} without "
                "rewriting the rest of the file"
            )
        if reason is not None:
            warnings.append(f"no edit proposed for {path}: {reason}")
            proposals.extend(
                _proposal(
                    finding,
                    "advisory",
                    f"remove {describe(removal)} from view "
                    f"'{finding.data.get('semantic_model')}' ({path}) with {_ROUTE}",
                )
                for finding in live
            )
            continue
        edits.append(
            PlanEdit(path=path, kind=EditKind.SEMANTIC_DOCUMENT, new_content=edited)
        )
        proposals.extend(
            _proposal(
                finding,
                "mechanical",
                f"remove {describe(removal)} from view "
                f"'{finding.data.get('semantic_model')}', everything that reads what "
                "the warehouse no longer has; review the diff before applying it"
                + (
                    ". If the warehouse type is the intended one, keep the cut "
                    "format; otherwise discard this plan and restore the column's "
                    "type"
                    if finding.code == "declared_type_conflict"
                    else ""
                ),
                paths=[path],
            )
            for finding in live
        )
    return proposals, edits, warnings


def describe(removal: Mapping[str, Any]) -> str:
    """The removal as a phrase: what goes, by kind."""

    parts = [
        f"{label} {', '.join(str(item) for item in items)}"
        for label, items in (
            ("field(s)", removal.get("fields") or []),
            ("measure(s)", removal.get("measures") or []),
            ("join(s)", removal.get("joins") or []),
            ("the format of", removal.get("formats") or []),
        )
        if items
    ]
    return "; ".join(parts) or "nothing"


def apply_removal(text: str, removal: Mapping[str, Any]) -> str | None:
    """``text`` with every removed entry cut out, or ``None`` if one cannot be.

    Every span is located against the file as parsed, never against a copy an
    earlier cut already pruned, so removals in one list (two sibling joins, a
    field and another field's ``format``) compose. Every requested entry has to
    be found exactly once, or nothing is proposed: a partial edit would read as
    a repair and still leave the view reading what is gone.
    """

    try:
        root = yaml.compose(text, Loader=_Loader)
        data = read_yaml(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return None
    expected = copy.deepcopy(data)
    changes: list[_Change] = []
    fields_key = str(removal.get("fields_key") or "fields")
    for key, names in (
        (fields_key, removal.get("fields") or []),
        ("measures", removal.get("measures") or []),
    ):
        if not names:
            continue
        selected = _by_name(names)
        planned = _cut(text, root, data, key, selected, len(names))
        if planned is None:
            return None
        changes.extend(planned)
        expected[key] = [item for item in expected[key] if not selected(item)]
    # Joins grouped by the list they sit in, so removing every sibling empties
    # that list once (`joins: []`) rather than leaving it null.
    siblings: dict[tuple[str, ...], list[str]] = {}
    for dotted in removal.get("joins") or []:
        path = tuple(dotted.split("."))
        siblings.setdefault(path[:-1], []).append(path[-1])
    for parent, names in siblings.items():
        planned = _cut_join(text, root, data, parent, names)
        if planned is None:
            return None
        changes.extend(planned)
        owner = _join_owner(expected, (*parent, names[0]))
        if owner is None:
            return None
        selected = _by_name(names)
        owner["joins"] = [item for item in owner["joins"] if not selected(item)]
    for name in removal.get("formats") or []:
        planned = _cut_format(text, root, data, fields_key, name)
        if planned is None:
            return None
        changes.extend(planned)
        for section in (fields_key, "measures"):
            for item in expected.get(section) or []:
                if _by_name([name])(item):
                    item.pop("format", None)
    if not changes:
        return text
    edited = text
    # Back to front, so every offset not yet applied still points into the
    # original text. No two overlap: each cuts inside one list or one member.
    for start, end, replacement in sorted(set(changes), reverse=True):
        edited = edited[:start] + replacement + edited[end:]
    try:
        reread = read_yaml(edited)
    except yaml.YAMLError:
        return None
    return edited if reread == expected else None


def _member_name(item: Any) -> str | None:
    """A member's name as Databricks reads it: as written, else derived from
    its ``expr`` by the reader's own rule."""

    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name
    expr = item.get("expr")
    if isinstance(expr, str) and expr.strip():
        from .validator import derived_name

        return derived_name(expr)
    return None


def _by_name(names: list[str]) -> Any:
    wanted = {n.lower() for n in names}

    def selected(item: Any) -> bool:
        name = _member_name(item)
        return name is not None and name.lower() in wanted

    return selected


def _child(node: yaml.MappingNode, key: str) -> tuple[yaml.Node, yaml.Node] | None:
    for key_node, value in node.value:
        if str(key_node.value) == key:
            return key_node, value
    return None


def _cut(
    text: str,
    owner: yaml.MappingNode,
    entry: Mapping[str, Any],
    key: str,
    selected: Any,
    wanted: int,
) -> list[_Change] | None:
    """The changes that drop every selected item of ``owner[key]``, located
    against ``entry`` as parsed, or None unless exactly ``wanted`` items are
    selected and each can be cut on its own lines."""

    found = _child(owner, key)
    items = entry.get(key)
    if found is None or not isinstance(items, list):
        return None
    key_node, sequence = found
    if not isinstance(sequence, yaml.SequenceNode) or sequence.flow_style:
        return None
    if len(items) != len(sequence.value):
        return None
    chosen = [i for i, item in enumerate(items) if selected(item)]
    if len(chosen) != wanted:
        return None
    changes: list[_Change] = []
    for index in chosen:
        span = sequence_item_span(text, sequence.value[index].start_mark.index)
        if span is None:
            return None
        changes.append((*span, ""))
    if len(chosen) == len(items):
        colon = text.find(":", key_node.end_mark.index)
        if colon < 0:
            return None
        changes.append((colon + 1, colon + 1, " []"))
    return changes


def _join_owner(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    """The mapping whose ``joins`` list holds the join at ``path``."""

    entry: Any = data
    for name in path[:-1]:
        items = entry.get("joins") if isinstance(entry, dict) else None
        if not isinstance(items, list):
            return None
        entry = next((i for i in items if _by_name([name])(i)), None)
    if not isinstance(entry, dict) or not isinstance(entry.get("joins"), list):
        return None
    return entry


def _cut_join(
    text: str,
    root: yaml.MappingNode,
    data: dict[str, Any],
    parent: tuple[str, ...],
    names: list[str],
) -> list[_Change] | None:
    """Cut the joins ``names`` out of the list under the join at ``parent``
    (``()`` for the view's own list)."""

    owner: yaml.MappingNode = root
    entry: Any = data
    for name in parent:
        found = _child(owner, "joins")
        items = entry.get("joins")
        if found is None or not isinstance(items, list):
            return None
        sequence = found[1]
        if not isinstance(sequence, yaml.SequenceNode):
            return None
        index = next(
            (i for i, item in enumerate(items) if _by_name([name])(item)), None
        )
        if index is None or not isinstance(sequence.value[index], yaml.MappingNode):
            return None
        owner, entry = sequence.value[index], items[index]
    return _cut(text, owner, entry, "joins", _by_name(names), len(names))


def _cut_format(
    text: str,
    root: yaml.MappingNode,
    data: dict[str, Any],
    fields_key: str,
    name: str,
) -> list[_Change] | None:
    """Cut the ``format`` key out of the member named ``name``."""

    for section in (fields_key, "measures"):
        found = _child(root, section)
        items = data.get(section)
        if found is None or not isinstance(items, list):
            continue
        sequence = found[1]
        if not isinstance(sequence, yaml.SequenceNode) or sequence.flow_style:
            continue
        if len(items) != len(sequence.value):
            return None
        for item, node in zip(items, sequence.value, strict=True):
            if not (_by_name([name])(item) and isinstance(node, yaml.MappingNode)):
                continue
            pair = _child(node, "format")
            if pair is None or "format" not in item:
                return None
            key_node = pair[0]
            line_start = text.rfind("\n", 0, key_node.start_mark.index) + 1
            if text[line_start : key_node.start_mark.index].strip():
                # `- format:` opens the member: cutting it would cut the dash.
                return None
            end = block_end(text, line_start, indent_of(text, line_start))
            return [(line_start, end, "")]
    return None


def _advisory(finding: DriftFinding, path: str) -> Proposal:
    """The proposal for a removal dex names and does not write."""

    removal = finding.data.get("removal") or {}
    name = finding.data.get("semantic_model")
    if removal.get("whole_model"):
        return _proposal(
            finding,
            "advisory",
            f"view '{name}' ({path}) reads a source that is gone. Point its "
            f"`source` at wherever the table went, or retire the view; dex does "
            "neither, because a vanished table is as often a rename as a "
            "retirement",
        )
    reasons = [_held_sentence(held) for held in removal.get("held") or []]
    then = (
        f" Then remove {describe(removal)} with {_ROUTE}"
        if describe(removal) != "nothing"
        else ""
    )
    return _proposal(
        finding,
        "advisory",
        f"view '{name}' ({path}): " + "; ".join(reasons) + "." + then,
    )


def _held_sentence(held: Mapping[str, Any]) -> str:
    reason = held.get("reason")
    if reason == "join_relation_missing":
        return (
            f"{held['element']} reads {held.get('identifier')}, which is gone; "
            "point its `source` at wherever the table went, or remove the join "
            "and what reads through it"
        )
    if reason == "filter_reads_dropped":
        return (
            f"the view's `filter` reads '{held.get('column')}', which is gone; "
            "dex does not change a view's population on its own, so rewrite the "
            "filter"
        )
    if reason == "source_query_reads_dropped":
        return (
            f"the query in {held['element']} reads '{held.get('column')}', which "
            "is gone; edit the query"
        )
    if reason == "filter_reads_removed_join":
        return (
            f"the view's `filter` reads through the join {held.get('join')}, "
            "which would be removed; dex does not change a view's population on "
            "its own, so rewrite the filter"
        )
    if reason == "materialization_reads_removed":
        return (
            "`materialization` lists "
            f"{', '.join(held.get('names') or [])}, which would be removed; take "
            "them out of the materialized views first"
        )
    if reason == "empties_view":
        return (
            "the view would be left with no field and no measure, which "
            "Databricks refuses; retire the view or repoint it"
        )
    return str(reason)


def _proposal(
    finding: DriftFinding, kind: str, action: str, *, paths: list[str] | None = None
) -> Proposal:
    from ..maintain.reconcile import Proposal

    return Proposal(
        axis=finding.axis,
        kind=kind,
        finding_code=finding.code,
        identifier=finding.identifier,
        column=finding.column,
        action=action,
        paths=paths or [],
    )
