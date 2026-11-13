"""Turning the Semantic View schema axis's removals into edits of the view files.

``maintain reconcile`` reads the removal each schema finding recorded (what goes
from one view, :mod:`.schema`, #1293) and proposes it as a `semantic snowflake
update` plan. Never a write: the edits go through the same validation and plan
store `semantic snowflake update` uses, so a reconciled view is read back by the
reader, strict rules included, before anything is stored, and `transform apply`
is what writes it.

**The edit is surgical.** Each removed dimension, time dimension, fact, metric,
derived metric or relationship is cut out of its list by the span it occupies,
and a ``data_type`` the warehouse contradicts is cut out of its member, so every
other byte, comments included, stays as the author left it. Every edit is
checked by re-reading it: it must parse to the original with exactly those
entries gone (:func:`pruned`). Anything that cannot be bounded that way (a
flow-style list, an aliased node, a ``data_type`` on the line of the dash that
opens its member) is not rewritten: the proposal is advisory instead, and says
what to remove.

**Advisory, never written:**

- a logical table whose ``base_table`` is gone: a vanished table is as often a
  rename to follow as a table to retire, and a whole logical table is never
  removed automatically;
- a ``primary_key`` or ``unique_keys`` entry naming a dropped column: the key is
  the table's grain and what its relationships rely on, so dex does not change
  it on its own;
- a ``base_table.definition`` query reading a dropped column: no member removal
  repairs the query;
- a removal the reader would refuse, or one that leaves the view no member,
  since removing a whole view is not something reconcile authors;
- a file that already holds what authoring refuses (an unknown key, a private
  dimension, an unresolved member): authoring refuses any edit to such a file.

Nothing here imports sqlglot: the removals arrive as plain data on the findings.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml

from ..yaml_spans import (
    block_end,
    indent_of,
    sequence_item_span,
    with_leading_comments,
)
from .loader import _Loader, read_yaml

if TYPE_CHECKING:
    from ..maintain.drift import DriftFinding
    from ..maintain.reconcile import Proposal
    from ..transform.plans import PlanEdit

__all__ = ["CODES", "apply_removal", "describe", "proposals_and_edits", "pruned"]

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

#: The member lists of a logical table, in the order the spec writes them.
SECTIONS = ("dimensions", "time_dimensions", "facts", "metrics")

_ROUTE = "`semantic snowflake update`"

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
    remove a member the edit just repaired. ``contents`` is each configured
    file's current text, keyed as configured. ``strict`` names, per file, what
    it already holds that authoring refuses.
    """

    from ..transform.plans import EditKind, PlanEdit

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    by_file: dict[str, list[DriftFinding]] = {}
    for finding in findings:
        if finding.data.get("advice"):
            # Reported and left for a person: nothing of it is cut.
            proposals.append(
                _proposal(finding, "advisory", str(finding.data["advice"]))
            )
            continue
        by_file.setdefault(str(finding.data.get("model_file")), []).append(finding)

    for path, group in sorted(by_file.items()):
        # One view per file, so every finding of the file carries one removal.
        removal = next(
            (f.data.get("removal") for f in group if f.data.get("removal")), None
        )
        if removal is None:
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
                for f in group
            )
            continue
        if removal.get("held") or describe(removal) == "nothing":
            proposals.extend(_advisory(f, path) for f in group)
            continue

        text = contents.get(path)
        reason = None
        if text is None:
            reason = f"{path} is not one of the configured Semantic View files"
        elif strict.get(path):
            reason = (
                f"{path} already holds what authoring refuses "
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
                for finding in group
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
                    ". If the warehouse type is the intended one, keep the cut; "
                    "otherwise discard this plan and restore the column's type"
                    if finding.code == "declared_type_conflict"
                    else ""
                ),
                paths=[path],
            )
            for finding in group
        )
    return proposals, edits, warnings


def describe(removal: Mapping[str, Any]) -> str:
    """The removal as a phrase: what goes, by kind."""

    members = [
        f"{m['table']}.{m['name']}" if m.get("table") else str(m["name"])
        for m in removal.get("members") or []
    ]
    data_types = [
        f"{m['table']}.{m['name']}" if m.get("table") else str(m["name"])
        for m in removal.get("data_types") or []
    ]
    parts = [
        f"{label} {', '.join(items)}"
        for label, items in (
            ("member(s)", members),
            ("relationship(s)", [str(r) for r in removal.get("relationships") or []]),
            ("the data_type of", data_types),
        )
        if items
    ]
    return "; ".join(parts) or "nothing"


def pruned(raw: Mapping[str, Any], removal: Mapping[str, Any]) -> dict[str, Any]:
    """``raw`` (a view as parsed) with exactly the removal's entries gone.

    What :func:`apply_removal` checks its edit against, and what the detector
    re-reads to find what else the reader would then refuse.
    """

    expected: dict[str, Any] = copy.deepcopy(dict(raw))
    for (table, section), names in _grouped(removal.get("members") or []).items():
        owner = expected if table is None else _table_entry(expected, table)
        if owner is None or not isinstance(owner.get(section), list):
            continue
        selected = _by_name(names)
        owner[section] = [item for item in owner[section] if not selected(item)]
    relationships = removal.get("relationships") or []
    if relationships and isinstance(expected.get("relationships"), list):
        selected = _by_name(list(relationships))
        expected["relationships"] = [
            item for item in expected["relationships"] if not selected(item)
        ]
    for entry in removal.get("data_types") or []:
        owner = (
            expected
            if entry.get("table") is None
            else _table_entry(expected, entry["table"])
        )
        for item in (owner or {}).get(entry["section"]) or []:
            if _by_name([entry["name"]])(item):
                item.pop("data_type", None)
    return expected


def apply_removal(text: str, removal: Mapping[str, Any]) -> str | None:
    """``text`` with every removed entry cut out, or ``None`` if one cannot be.

    Every span is located against the file as parsed, so removals in one list
    compose. Every requested entry has to be found exactly once, or nothing is
    proposed: a partial edit would read as a repair and still leave the view
    reading what is gone.
    """

    try:
        root = yaml.compose(text, Loader=_Loader)
        data = read_yaml(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return None
    changes: list[_Change] = []
    for (table, section), names in _grouped(removal.get("members") or []).items():
        located = _owner(root, data, table)
        if located is None:
            return None
        planned = _cut(text, *located, section, _by_name(names), len(names))
        if planned is None:
            return None
        changes.extend(planned)
    relationships = list(removal.get("relationships") or [])
    if relationships:
        planned = _cut(
            text,
            root,
            data,
            "relationships",
            _by_name(relationships),
            len(relationships),
        )
        if planned is None:
            return None
        changes.extend(planned)
    for entry in removal.get("data_types") or []:
        located = _owner(root, data, entry.get("table"))
        if located is None:
            return None
        planned = _cut_key(text, *located, entry["section"], entry["name"], "data_type")
        if planned is None:
            return None
        changes.extend(planned)
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
    return edited if reread == pruned(data, removal) else None


def _grouped(
    members: list[Mapping[str, Any]],
) -> dict[tuple[str | None, str], list[str]]:
    """Removed members by the list they sit in."""

    grouped: dict[tuple[str | None, str], list[str]] = {}
    for member in members:
        table = member.get("table")
        grouped.setdefault(
            (str(table) if table else None, str(member["section"])), []
        ).append(str(member["name"]))
    return grouped


def _by_name(names: list[str]) -> Any:
    wanted = {n.lower() for n in names}

    def selected(item: Any) -> bool:
        name = item.get("name") if isinstance(item, dict) else None
        return isinstance(name, str) and name.lower() in wanted

    return selected


def _table_entry(data: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for item in data.get("tables") or []:
        if _by_name([table])(item):
            return item
    return None


def _child(node: yaml.MappingNode, key: str) -> tuple[yaml.Node, yaml.Node] | None:
    for key_node, value in node.value:
        if str(key_node.value) == key:
            return key_node, value
    return None


def _inside(node: yaml.Node, container: yaml.Node) -> bool:
    """Whether ``node`` is written inside ``container``: an aliased node's marks
    point at its anchor, and is never edited in place."""

    return (
        container.start_mark.index <= node.start_mark.index
        and node.end_mark.index <= container.end_mark.index
    )


def _owner(
    root: yaml.MappingNode, data: dict[str, Any], table: str | None
) -> tuple[yaml.MappingNode, dict[str, Any]] | None:
    """The mapping holding the lists of ``table``, node and data, or the view's
    root for a derived metric (``table`` None)."""

    if table is None:
        return root, data
    found = _child(root, "tables")
    items = data.get("tables")
    if found is None or not isinstance(items, list):
        return None
    sequence = found[1]
    if (
        not isinstance(sequence, yaml.SequenceNode)
        or sequence.flow_style
        or len(sequence.value) != len(items)
    ):
        return None
    chosen = [i for i, item in enumerate(items) if _by_name([table])(item)]
    if len(chosen) != 1:
        return None
    node = sequence.value[chosen[0]]
    if (
        not isinstance(node, yaml.MappingNode)
        or node.flow_style
        or not _inside(node, sequence)
    ):
        return None
    return node, items[chosen[0]]


def _cut(
    text: str,
    owner: yaml.MappingNode,
    entry: Mapping[str, Any],
    key: str,
    selected: Any,
    wanted: int,
) -> list[_Change] | None:
    """The changes that drop every selected item of ``owner[key]``, or None
    unless exactly ``wanted`` items are selected and each can be cut on its own
    lines."""

    found = _child(owner, key)
    items = entry.get(key)
    if found is None or not isinstance(items, list):
        return None
    key_node, sequence = found
    if (
        not isinstance(sequence, yaml.SequenceNode)
        or sequence.flow_style
        or not _inside(sequence, owner)
        or len(items) != len(sequence.value)
    ):
        return None
    chosen = [i for i, item in enumerate(items) if selected(item)]
    if len(chosen) != wanted:
        return None
    changes: list[_Change] = []
    for index in chosen:
        span = sequence_item_span(text, sequence.value[index].start_mark.index)
        if span is None:
            return None
        # A comment written directly above an item, at its column, is about
        # that item, and would otherwise be left describing its neighbour.
        changes.append((with_leading_comments(text, span[0]), span[1], ""))
    if len(chosen) == len(items):
        colon = text.find(":", key_node.end_mark.index)
        if colon < 0:
            return None
        changes.append((colon + 1, colon + 1, " []"))
    return changes


def _cut_key(
    text: str,
    owner: yaml.MappingNode,
    entry: Mapping[str, Any],
    section: str,
    name: str,
    key: str,
) -> list[_Change] | None:
    """Cut ``key`` out of the member ``name`` of ``owner[section]``."""

    found = _child(owner, section)
    items = entry.get(section)
    if found is None or not isinstance(items, list):
        return None
    sequence = found[1]
    if (
        not isinstance(sequence, yaml.SequenceNode)
        or sequence.flow_style
        or len(items) != len(sequence.value)
    ):
        return None
    for item, node in zip(items, sequence.value, strict=True):
        if not (_by_name([name])(item) and isinstance(node, yaml.MappingNode)):
            continue
        if node.flow_style:
            return None
        pair = _child(node, key)
        if pair is None or key not in item:
            return None
        key_node = pair[0]
        line_start = text.rfind("\n", 0, key_node.start_mark.index) + 1
        if text[line_start : key_node.start_mark.index].strip():
            # `- data_type:` opens the member: cutting it would cut the dash.
            return None
        end = block_end(text, line_start, indent_of(text, line_start))
        return [(line_start, end, "")]
    return None


def _advisory(finding: DriftFinding, path: str) -> Proposal:
    """The proposal for a removal dex names and does not write."""

    removal = finding.data.get("removal") or {}
    name = finding.data.get("semantic_model")
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
    if reason == "table_relation_missing":
        return (
            f"{held['element']} reads {held.get('identifier')}, which is gone; "
            "point its `base_table` at wherever the table went, or remove the "
            "logical table and what reads it. dex removes no logical table on its "
            "own, because a vanished table is as often a rename as a retirement"
        )
    if reason == "key_reads_dropped":
        return (
            f"{held['element']} names '{held.get('column')}', which is gone; a key "
            "is the table's grain and what its relationships rely on, so dex does "
            "not change it on its own: change the key, or restore the column"
        )
    if reason == "definition_reads_dropped":
        return (
            f"the query in {held['element']} reads '{held.get('column')}', which "
            "is gone; edit the query"
        )
    if reason == "empties_view":
        return (
            "the view would be left with no dimension, fact or metric; retire the "
            "view or repoint it"
        )
    if reason == "would_not_validate":
        return (
            "the view would not read back once the removal is made "
            f"({held.get('message')}); edit it by hand"
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
