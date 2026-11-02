"""Turning the Cube schema axis's removals into edits of the model files.

``maintain reconcile`` reads the removal each schema finding recorded (what goes
from one cube or view, :mod:`.schema`) and proposes it as a `semantic cube
update` plan. Never a write: the edits go through the same validation and plan
store `semantic cube update` uses, and `transform apply` is what writes them.

**The edit is surgical.** Each removed dimension, measure, segment, join,
include or exclude is cut out of its list by the span it occupies, and every
other byte, other cubes, views and comments included, stays as the author left
it. A flow-style list of plain names (``includes: [revenue, status]``) is
rewritten in place with the survivors in their own spelling. Anything else
that cannot be bounded that way is not rewritten by re-serializing the file:
the proposal is advisory instead, and says what to remove. Every edit is checked
by re-reading it: it must parse to the original with exactly those entries gone.

**Advisory, never written:**

- a cube whose relation is gone: never removed here, because a vanished table is
  as often a rename to follow (repoint ``sql_table``) as a cube to retire;
- a view source whose join path walks a removed join: dropping a whole source
  changes what the view is, which is a decision rather than a repair;
- a view left with no sources: removing a whole view is not something reconcile
  authors, as with a whole cube;
- a file that already holds a key Cube does not declare: authoring refuses any
  edit to such a file (Cube's own validator would), so the proposal names it;
- a ``sql`` cube whose query selects the dropped column itself, which no member
  removal repairs.

Nothing here imports sqlglot: the removals arrive as plain data on the findings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import yaml

from ..yaml_spans import sequence_item_span
from .validator import canonical_key
from .yaml_items import element_node

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

_CUBE_SECTIONS = ("dimensions", "measures", "segments", "joins")

# One change: cut ``[start, end)`` and write ``replacement`` there.
_Change = tuple[int, int, str]


def proposals_and_edits(
    findings: list[DriftFinding],
    contents: Mapping[str, str],
    strict: Mapping[str, list[str]],
) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
    """One proposal per finding, and one edit per model file that can take it.

    ``contents`` is each configured YAML file's current text, keyed as
    configured. ``strict`` names, per file, the keys Cube does not declare that
    the file already holds: authoring refuses an edit to such a file, so its
    proposals are advisory from the start. A file is edited once however many
    cubes, views and findings point at it.
    """

    from ..transform.plans import EditKind, PlanEdit

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    by_file: dict[str, list[DriftFinding]] = {}
    for finding in findings:
        by_file.setdefault(str(finding.data.get("model_file")), []).append(finding)

    for path, group in sorted(by_file.items()):
        # One removal per element: every finding on it carries the same one.
        removals: dict[tuple[str, str], dict[str, Any]] = {}
        for finding in group:
            removal = finding.data.get("removal") or {}
            if removal.get("whole_model") or _held_back(removal):
                proposals.append(_advisory(finding, path))
                continue
            kind = "views" if removal.get("element") == "view" else "cubes"
            removals[(kind, str(finding.data.get("semantic_model")))] = removal
        live = [f for f in group if (_key(f)) in removals]
        if not live:
            continue

        text = contents.get(path)
        reason = None
        if text is None:
            reason = f"{path} is not one of the configured YAML model files"
        elif strict.get(path):
            reason = (
                f"{path} already declares key(s) Cube does not "
                f"({', '.join(strict[path])}), and `semantic cube update` refuses "
                "any edit to a file that does; remove them first"
            )
        edited = apply_removals(text, removals) if reason is None and text else None
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
                    f"remove {describe(removals[_key(finding)])} from "
                    f"{_element(finding)} ({path}) with `semantic cube update`",
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
                f"remove {describe(removals[_key(finding)])} from "
                f"{_element(finding)}, everything that reads what the warehouse "
                "no longer has; review the diff before applying it"
                + (
                    f". The join(s) to {', '.join(_removal_of(finding)['held_joins'])} "
                    "stay, because a view's join path walks them: re-route the "
                    "view first"
                    if _removal_of(finding).get("held_joins")
                    else ""
                )
                + (
                    ". If the warehouse type is the intended one, discard this "
                    "plan and change the member's type with `semantic cube "
                    "update` instead"
                    if finding.code == "declared_type_conflict"
                    else ""
                ),
                paths=[path],
            )
            for finding in live
        )
    return proposals, edits, warnings


def _removal_of(finding: DriftFinding) -> Mapping[str, Any]:
    return finding.data.get("removal") or {}


def _key(finding: DriftFinding) -> tuple[str, str]:
    removal = finding.data.get("removal") or {}
    kind = "views" if removal.get("element") == "view" else "cubes"
    return kind, str(finding.data.get("semantic_model"))


def _element(finding: DriftFinding) -> str:
    kind, name = _key(finding)
    return f"{kind[:-1]} '{name}'"


def _held_back(removal: Mapping[str, Any]) -> bool:
    """Whether this removal is one dex proposes but does not write: nothing in
    it is dex's to cut on its own."""

    if (
        removal.get("broken_sources")
        or removal.get("sql_reads_dropped")
        or removal.get("empties_view")
    ):
        return True
    return describe(removal) == "nothing"


def _advisory(finding: DriftFinding, path: str) -> Proposal:
    """The advisory proposal when a drift finding requires manual intervention."""

    removal = finding.data.get("removal") or {}
    name = finding.data.get("semantic_model")
    if removal.get("whole_model"):
        action = (
            f"cube '{name}' ({path}) reads a relation that is gone. Point its "
            "sql_table at wherever the table went, or remove the cube and what "
            "reads it with `semantic cube update`; dex does neither, because a "
            "vanished table is as often a rename as a retirement"
        )
    elif removal.get("broken_sources"):
        action = (
            f"view '{name}' ({path}) has source(s) "
            f"{', '.join(removal['broken_sources'])} whose join path walks a join "
            "that goes. Re-route or remove them with `semantic cube update`; dex "
            "does not drop a view's source on its own"
        )
    elif removal.get("empties_view"):
        action = (
            f"view '{name}' ({path}) would be left with no sources. "
            "Add another source, or remove the view with `semantic cube update`; "
            "dex does not drop a whole view on its own"
        )
    elif removal.get("held_joins") and describe(removal) == "nothing":
        action = (
            f"cube '{name}' ({path}) has join(s) to "
            f"{', '.join(removal['held_joins'])} that read something the "
            "warehouse no longer has, and a view's join path walks them. "
            "Re-route the view, then remove the join(s) with `semantic cube update`"
        )
    else:
        action = (
            f"cube '{name}' ({path}) is defined by a sql query that itself "
            "selects the dropped column. Edit the query, then remove "
            f"{describe(removal)} with `semantic cube update`"
        )
    return _proposal(finding, "advisory", action)


def describe(removal: Mapping[str, Any]) -> str:
    """The removal as a phrase: what goes, by kind."""

    parts = [
        f"{label} {', '.join(str(item) for item in items)}"
        for label, items in (
            ("dimension(s)", removal.get("dimensions") or []),
            ("measure(s)", removal.get("measures") or []),
            ("segment(s)", removal.get("segments") or []),
            ("join(s) to", removal.get("joins") or []),
            (
                "source(s)",
                [s["join_path"] for s in removal.get("removed_sources") or []],
            ),
            (
                "include(s)",
                [n for i in removal.get("includes") or [] for n in i["names"]],
            ),
            (
                "exclude(s)",
                [n for e in removal.get("excludes") or [] for n in e["names"]],
            ),
        )
        if items
    ]
    return "; ".join(parts) or "nothing"


def apply_removals(
    text: str, removals: Mapping[tuple[str, str], Mapping[str, Any]]
) -> str | None:
    """``text`` with every removed entry of every element cut out, or ``None``
    if one cannot be.

    ``removals`` is keyed ``(section, name)``: ``("cubes", "orders")``.
    """

    try:
        root = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    changes: list[_Change] = []
    for (section, name), removal in removals.items():
        node = element_node(root, name, section)
        entry = _entry(data, section, name)
        if node is None or entry is None:
            return None
        planned = (
            _view_changes(text, node, entry, removal)
            if section == "views"
            else _cube_changes(text, node, entry, removal)
        )
        if planned is None:
            return None
        changes.extend(planned)
    if not changes:
        return text
    edited = text
    # Back to front, so every offset not yet applied still points into the
    # original text. No two overlap: each cuts inside one list.
    for start, end, replacement in sorted(changes, reverse=True):
        edited = edited[:start] + replacement + edited[end:]
    try:
        reread = yaml.safe_load(edited)
    except yaml.YAMLError:
        return None
    return edited if reread == data else None


def apply_removal(
    text: str, element: str, name: str, removal: Mapping[str, Any]
) -> str | None:
    """One element's removal; see :func:`apply_removals`."""

    return apply_removals(text, {(element, name): removal})


def _entry(data: dict[str, Any], section: str, name: str) -> dict[str, Any] | None:
    for entry in data.get(section) or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _cube_changes(
    text: str,
    node: yaml.MappingNode,
    entry: dict[str, Any],
    removal: Mapping[str, Any],
) -> list[_Change] | None:
    changes: list[_Change] = []
    for section in _CUBE_SECTIONS:
        names = set(removal.get(section) or [])
        if not names:
            continue
        by_alias = section == "joins"

        def selected(
            item: Any, names: set[str] = names, by_alias: bool = by_alias
        ) -> bool:
            if not isinstance(item, dict):
                return False
            name = (item.get("alias") or item.get("name")) if by_alias else None
            return (name if by_alias else item.get("name")) in names

        planned = _cut(text, node, entry, section, selected)
        if planned is None:
            return None
        changes.extend(planned)
    return changes


def _source_path(item: Mapping[str, Any]) -> str | None:
    """The join path of one view source, recognizing canonical join_path/joinPath."""

    for k, v in item.items():
        if canonical_key(str(k)) == "join_path" and v is not None:
            return str(v)
    return None


def _source_includes(item: Mapping[str, Any]) -> set[str]:
    """The member names explicitly included in one view source."""

    raw = item.get("includes")
    if not isinstance(raw, list):
        return set()
    return {
        i.get("name") if isinstance(i, dict) else str(i)
        for i in raw
        if not isinstance(i, str) or i != "*"
    }


def _view_changes(
    text: str,
    node: yaml.MappingNode,
    entry: dict[str, Any],
    removal: Mapping[str, Any],
) -> list[_Change] | None:
    """The changes that apply includes/excludes or source cuts to one view."""

    sources = _child(node, "cubes")
    listed = entry.get("cubes")
    if sources is None or not isinstance(listed, list):
        return None
    changes: list[_Change] = []
    wanted = [("includes", i) for i in removal.get("includes") or []] + [
        ("excludes", e) for e in removal.get("excludes") or []
    ]
    for key, target in wanted:
        names = set(target["names"])
        target_source_names = (
            set(target["source_names"]) if "source_names" in target else None
        )
        matched = [
            (source_node, source)
            for source_node, source in zip(sources.value, listed, strict=True)
            if isinstance(source, dict)
            and _source_path(source) == target["join_path"]
            and source.get("alias") == target.get("alias")
            and (
                target_source_names is None
                or _source_includes(source) == target_source_names
            )
        ]
        if len(matched) != 1 or not isinstance(matched[0][0], yaml.MappingNode):
            return None
        source_node, source = matched[0]

        def selected(item: Any, names: set[str] = names) -> bool:
            """Whether this item is one of the members named for removal."""

            name = item.get("name") if isinstance(item, dict) else item
            return name in names

        planned = _cut(text, source_node, source, key, selected)
        if planned is None:
            return None
        changes.extend(planned)
    removed = removal.get("removed_sources") or []
    if removed:
        counts: dict[tuple[str, Any, frozenset[str]], int] = {}
        for target in removed:
            group_key = (
                target["join_path"],
                target.get("alias"),
                frozenset(target.get("names") or []),
            )
            counts[group_key] = counts.get(group_key, 0) + 1

        to_cut: list[dict[str, Any]] = []
        for (join_path, alias, target_names), count in counts.items():
            matched_sources = [
                source
                for source in listed
                if isinstance(source, dict)
                and _source_path(source) == join_path
                and source.get("alias") == alias
                and (not target_names or _source_includes(source) == target_names)
            ]
            if len(matched_sources) != count:
                return None
            to_cut.extend(matched_sources)

        def source_selected(item: Any) -> bool:
            """Whether this item is one of the matched sources to cut."""

            return any(item is m for m in to_cut)

        planned = _cut(text, node, entry, "cubes", source_selected)
        if planned is None:
            return None
        changes.extend(planned)
    return changes


def _child(node: yaml.MappingNode, key: str) -> yaml.SequenceNode | None:
    """Find the child sequence node matching a canonical key."""

    for key_node, value in node.value:
        if canonical_key(str(key_node.value)) == key and isinstance(
            value, yaml.SequenceNode
        ):
            return value
    return None


def _cut(
    text: str,
    owner: yaml.MappingNode,
    entry: dict[str, Any],
    key: str,
    selected: Callable[[Any], bool],
) -> list[_Change] | None:
    """The changes that drop every selected item of ``owner[key]``, applied to
    ``entry`` as well, so the caller's parsed copy is what the edit must read as.
    """

    for key_node, sequence in owner.value:
        if canonical_key(str(key_node.value)) != key:
            continue
        written = str(key_node.value)
        items = entry.get(written)
        if not isinstance(sequence, yaml.SequenceNode) or not isinstance(items, list):
            return None
        if len(items) != len(sequence.value):
            return None
        keep = [item for item in items if not selected(item)]
        if len(keep) == len(items):
            return []
        entry[written] = keep
        if sequence.flow_style:
            if not all(isinstance(n, yaml.ScalarNode) for n in sequence.value):
                return None
            kept = [
                text[n.start_mark.index : n.end_mark.index]
                for item, n in zip(items, sequence.value, strict=True)
                if not selected(item)
            ]
            return [
                (
                    sequence.start_mark.index,
                    sequence.end_mark.index,
                    "[" + ", ".join(kept) + "]",
                )
            ]
        changes: list[_Change] = []
        for item, item_node in zip(items, sequence.value, strict=True):
            if not selected(item):
                continue
            span = sequence_item_span(text, item_node.start_mark.index)
            if span is None:
                return None
            changes.append((*span, ""))
        if not keep:
            colon = text.find(":", key_node.end_mark.index)
            if colon < 0:
                return None
            changes.append((colon + 1, colon + 1, " []"))
        return changes
    # Nothing written under that key: nothing of it to remove.
    return []


def _proposal(
    finding: DriftFinding, kind: str, action: str, *, paths: list[str] | None = None
) -> Proposal:
    """Construct a proposal for a schema drift finding."""

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
