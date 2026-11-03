"""Turning SLayer's computed removals into edits of the model files.

``maintain reconcile`` reads the removals the schema axis recorded (what SLayer's
own rules would drop from each model) and proposes them as a `semantic slayer`
plan. Never a write: the edits go through the same validation and plan store
`semantic slayer update` uses, and `transform apply` is what writes them.

**The edit is surgical.** Each removed entry is cut out of the file by the span
it occupies, and every other byte, comments and formatting included, stays as
the author left it, so the diff shows the removal and nothing else. A removal
that cannot be located that way (a flow-style list, an entry sharing a line with
another) is not rewritten by re-serializing the whole file, which would bury it
in reformatting: the proposal is advisory instead, and says what to remove.

**A whole model is never removed here.** Deleting a model file is not something
`semantic slayer` authors, and a table that went away is as often a rename to
follow as a model to retire, so that proposal is always advisory.

Nothing here imports SLayer: the removals arrive as plain data on the findings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import yaml

from ..yaml_spans import sequence_item_span

if TYPE_CHECKING:
    from ..maintain.drift import DriftFinding
    from ..maintain.reconcile import Proposal
    from ..transform.plans import PlanEdit

__all__ = ["CODES", "apply_removal", "describe", "proposals_and_edits"]

#: Every finding code the schema channel produces, so reconcile can tell its
#: own. Here rather than beside the detector, which imports SLayer.
CODES = frozenset(
    {
        "declared_column_missing",
        "declared_type_conflict",
        "declared_relation_missing",
        "declared_dependency_dropped",
    }
)


def proposals_and_edits(
    findings: list[DriftFinding], contents: dict[str, str]
) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
    """One proposal per finding, and one edit per model file that can take it.

    ``contents`` is each configured model file's current text, keyed as
    configured. Findings for one model share one removal (SLayer computes it per
    model), so the file is edited once however many findings point at it.
    """

    from ..transform.plans import EditKind, PlanEdit

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    by_file: dict[str, list[DriftFinding]] = {}
    for finding in findings:
        by_file.setdefault(str(finding.data.get("model_file")), []).append(finding)

    for path, group in sorted(by_file.items()):
        removal = group[0].data.get("removal") or {}
        model = group[0].data.get("semantic_model")
        described = describe(removal)
        if removal.get("whole_model"):
            proposals.extend(
                _proposal(
                    finding,
                    "advisory",
                    f"SLayer would drop model '{model}' ({path}) whole. Remove "
                    "the file and its entry in semantic.slayer.models, or point "
                    "sql_table at wherever the table went; dex authors neither, "
                    "because a vanished table is as often a rename as a "
                    "retirement",
                )
                for finding in group
            )
            continue
        text = contents.get(path)
        edited = apply_removal(text, removal) if text is not None else None
        if edited is None or edited == text:
            reason = (
                f"{path} is not one of the configured model files"
                if text is None
                else f"dex could not locate every entry to remove in {path} "
                "without rewriting the rest of the file"
            )
            warnings.append(f"no edit proposed for SLayer model '{model}': {reason}")
            proposals.extend(
                _proposal(
                    finding,
                    "advisory",
                    f"remove {described} from SLayer model '{model}' ({path}) with "
                    "`semantic slayer update`",
                )
                for finding in group
            )
            continue
        edits.append(
            PlanEdit(
                path=path,
                kind=EditKind.SEMANTIC_DOCUMENT,
                new_content=edited,
            )
        )
        proposals.extend(
            _proposal(
                finding,
                "mechanical",
                f"remove {described} from SLayer model '{model}', the minimal "
                "removal SLayer's own validate-models rules compute for this "
                "schema; review the diff before applying it"
                + (
                    ". If the warehouse type is the intended one, discard this "
                    "plan and change the column's declared type with `semantic "
                    "slayer update` instead"
                    if finding.code == "declared_type_conflict"
                    else ""
                ),
                paths=[path],
            )
            for finding in group
        )
    return proposals, edits, warnings


def describe(removal: dict[str, Any]) -> str:
    """The removal as a phrase: what goes, by kind."""

    parts = [
        f"{label} {', '.join(str(item) for item in items)}"
        for label, items in (
            ("column(s)", removal.get("columns") or []),
            ("measure(s)", removal.get("measures") or []),
            ("aggregation(s)", removal.get("aggregations") or []),
            ("filter(s)", removal.get("filters") or []),
        )
        if items
    ]
    joins = [edge.get("target_model") for edge in removal.get("joins") or []]
    if joins:
        parts.append(f"join(s) to {', '.join(str(j) for j in joins)}")
    return "; ".join(parts) or "nothing"


def apply_removal(text: str, removal: dict[str, Any]) -> str | None:
    """``text`` with every removed entry cut out, or ``None`` if one cannot be.

    Checked by re-reading the result: it must parse to the original model with
    exactly those entries gone, or the edit is not offered.
    """

    try:
        root = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return None

    selections: dict[str, Callable[[Any], bool]] = {}
    if removal.get("columns"):
        names = set(removal["columns"])
        selections["columns"] = lambda e: isinstance(e, dict) and e.get("name") in names
    if removal.get("measures"):
        measures = set(removal["measures"])
        selections["measures"] = lambda e: (
            isinstance(e, dict) and e.get("name") in measures
        )
    if removal.get("aggregations"):
        aggregations = set(removal["aggregations"])
        selections["aggregations"] = lambda e: (
            isinstance(e, dict) and e.get("name") in aggregations
        )
    if removal.get("joins"):
        edges = list(removal["joins"])
        selections["joins"] = lambda e: any(_is_edge(e, edge) for edge in edges)
    if removal.get("filters"):
        filters = set(removal["filters"])
        selections["filters"] = lambda e: e in filters

    # Every change as (start, end, replacement): an item cut, or `[]` written
    # after the key of a list the removal empties.
    changes: list[tuple[int, int, str]] = []
    expected = dict(data)
    for key, selected in selections.items():
        located = _locate(text, root, key)
        if located is None:
            return None
        key_end, sequence = located
        items = data.get(key) or []
        if len(items) != len(sequence.value):
            return None
        keep = [item for item in items if not selected(item)]
        expected[key] = keep
        for item, node in zip(items, sequence.value, strict=True):
            if not selected(item):
                continue
            span = sequence_item_span(text, node.start_mark.index)
            if span is None:
                return None
            changes.append((*span, ""))
        if not keep and items:
            changes.append((key_end + 1, key_end + 1, " []"))

    if not changes:
        return text
    edited = text
    # Back to front, so every offset not yet applied still points into the
    # original text. No two changes overlap: a key's colon precedes its items.
    for start, end, replacement in sorted(changes, reverse=True):
        edited = edited[:start] + replacement + edited[end:]

    try:
        reread = yaml.safe_load(edited)
    except yaml.YAMLError:
        return None
    return edited if reread == expected else None


def _is_edge(entry: Any, edge: dict[str, Any]) -> bool:
    """SLayer's own join-edge match: target, the set of pairs, and the name."""

    if not isinstance(entry, dict) or entry.get("target_model") != edge.get(
        "target_model"
    ):
        return False
    pairs = {tuple(p) for p in entry.get("join_pairs") or [] if len(p) == 2}
    if pairs != {tuple(p) for p in edge.get("join_pairs") or []}:
        return False
    return edge.get("name") is None or entry.get("name") == edge.get("name")


def _locate(
    text: str, root: yaml.MappingNode, key: str
) -> tuple[int, yaml.SequenceNode] | None:
    """The offset of ``key``'s colon, and its block-style sequence."""

    for key_node, value in root.value:
        if getattr(key_node, "value", None) != key:
            continue
        if not isinstance(value, yaml.SequenceNode) or value.flow_style:
            return None
        colon = text.find(":", key_node.end_mark.index)
        return (colon, value) if colon >= 0 else None
    return None


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
