"""Where a derived column belongs: the lowest common ancestor, and the reasoning.

When a derived column has to appear in several models, where it is *defined* is a
graph question with a right answer. Defining it once in the lowest point of the
lineage that every target descends from, and threading it down, keeps one copy of
the derivation. Defining it in each target duplicates the logic, and the copies
drift apart the first time one of them is corrected and the others are not.

Callers routinely ask for the outcome ("make these two marts carry a
`geo_segment`") without naming the location, so the location is the part dex
decides and the part it has to justify.

**The reasoning is the product, not the plan.** "Propose, do not impose" only
means anything if the proposal can be argued with, so this reports which ancestor
it chose, why that one is the lowest, which targets descend from it, and what the
pass-through chain is. ``--explain`` returns exactly that and stores nothing.

**What counts as eligible is deliberately narrow.** An ancestor qualifies only if
it already projects every input the derivation reads. dex will not go hunting
further upstream to pull an input down, because one placement request would then
become an unbounded rewrite of the graph above it. Where the lowest common
ancestor is ineligible, it is reported by name with what it is missing, which is
the fact a caller needs in order to disagree.

Where there is no common ancestor at all, or the lowest is ineligible, or two
incomparable candidates tie, the answer is the per-target definition with the
reason stated. That is the worse outcome, and saying so is better than doing it
quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import expressions as exp

from ..dbt_project import DbtProjectView, node_files, node_name
from ..errors import RequestError
from ..references import ReferenceIndex
from .plans import EditKind, PlanEdit
from .rewrite import RewriteError, output_columns, project_column_in_sql

if TYPE_CHECKING:
    from pathlib import Path

    from ..adapters.project import PlacingProject


class PlacementRefusedError(RequestError):
    """A placement dex will not guess at. Always names what is missing."""


@dataclass
class Placement:
    """Where a column should be defined, why, and the edits that put it there.

    ``strategy`` is ``common_ancestor`` or ``per_target``, and the difference
    matters enough to be a field rather than something inferred from whether
    ``ancestor`` is set: the fallback is a worse outcome that dex chose
    deliberately, and a caller skimming the payload has to see which one they got.
    """

    column: str
    expression: str
    inputs: list[str]
    targets: list[str]
    strategy: str
    ancestor: str | None
    reasoning: list[str] = field(default_factory=list)
    chain: dict[str, list[str]] = field(default_factory=dict)
    edits: list[PlanEdit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def intent(self) -> str:
        where = self.ancestor if self.strategy == "common_ancestor" else "each target"
        return f"define {self.column} in {where} and carry it to " + ", ".join(
            self.targets
        )


def derivation_inputs(expression: str) -> list[str]:
    """The column names one derivation reads.

    Parsed out of the expression rather than taken from the caller, because two
    sources for the same fact is one source too many: an input list that disagrees
    with the expression using it produces a confidently wrong ancestor and there
    is no way for dex to notice.
    """

    try:
        parsed = sqlglot.parse_one(expression, read="duckdb")
    except Exception as exc:
        raise PlacementRefusedError(
            f"dex could not read '{expression}' as a SQL expression ({exc}). "
            "Pass the expression as it would appear in a SELECT list, without "
            "the alias"
        ) from exc
    if parsed is None:
        raise PlacementRefusedError("the derivation expression is empty")
    names = sorted({column.name.lower() for column in parsed.find_all(exp.Column)})
    if not names:
        raise PlacementRefusedError(
            f"'{expression}' reads no columns, so every model in the project could "
            "host it equally and there is no placement question to answer. Define "
            "it wherever it belongs"
        )
    return names


def place(
    view: DbtProjectView,
    project_dir: Path,
    column: str,
    targets: list[str],
    expression: str,
    *,
    placement: PlacingProject | None = None,
) -> Placement:
    """Choose where ``column`` is defined and build the edits that thread it down.

    Pure: writes nothing. ``placement`` is the project format answering where an
    edit lands, the way ``maintain reconcile`` asks it; the ``ref()`` graph and
    the SQL itself stay dbt's, which is what they are.
    """

    if len(targets) < 2:
        raise PlacementRefusedError(
            "name at least two target models: placing a column shared by one "
            "model is not a graph question, and the answer is that model"
        )
    inputs = derivation_inputs(expression)
    index = ReferenceIndex(view)
    nodes = {node_name(path): path for path in node_files(view)}

    missing = [target for target in targets if target not in nodes]
    if missing:
        raise PlacementRefusedError(
            f"not a model in this project: {', '.join(sorted(missing))}. "
            "`transform place` threads a column through models it can read"
        )

    ancestry = {target: index.ancestors_of(target) for target in targets}
    common = set.intersection(*ancestry.values())
    reasoning: list[str] = []

    candidate, why = _lowest_eligible(common, index, view, nodes, inputs, reasoning)
    if candidate is None:
        return _per_target(
            view, column, targets, expression, inputs, nodes, why, placement
        )

    chain = {target: _path_between(index, candidate, target) for target in targets}
    reasoning.append(
        f"'{candidate}' is the lowest model every target descends from that "
        f"already projects {', '.join(inputs)}"
    )
    for target in targets:
        hops = chain[target]
        reasoning.append(
            f"{target} reaches it through {' -> '.join(hops)}"
            if len(hops) > 1
            else f"{target} is '{candidate}' itself"
        )

    edits = _thread(
        view, project_dir, column, expression, candidate, chain, nodes, placement
    )
    return Placement(
        column=column,
        expression=expression,
        inputs=inputs,
        targets=targets,
        strategy="common_ancestor",
        ancestor=candidate,
        reasoning=reasoning,
        chain=chain,
        edits=edits.edits,
        notes=edits.notes,
    )


def _lowest_eligible(
    common: set[str],
    index: ReferenceIndex,
    view: DbtProjectView,
    nodes: dict[str, str],
    inputs: list[str],
    reasoning: list[str],
) -> tuple[str | None, str]:
    """The lowest common ancestor that projects every input, and why not otherwise.

    "Lowest" is the graph's own ordering rather than a depth count: a common
    ancestor is lowest when no *other* common ancestor descends from it. Two
    candidates surviving that means the ancestors fork and rejoin, and neither is
    below the other; dex says so rather than picking by an arbitrary tiebreak.
    """

    if not common:
        return None, "the targets share no model in their lineage"

    lowest = [
        node for node in common if not (index.descendants_of(node) - {node}) & common
    ]
    if not lowest:
        return None, "the targets' shared lineage has no lowest point dex could find"
    if len(lowest) > 1:
        return None, (
            "the targets share more than one lowest common ancestor "
            f"({', '.join(sorted(lowest))}) and neither is below the other"
        )

    candidate = lowest[0]
    projected = _projected(view, nodes, candidate)
    if projected is None:
        reasoning.append(
            f"'{candidate}' is the lowest common ancestor, but it projects a "
            "star, so dex cannot prove it produces the inputs"
        )
        return None, (
            f"'{candidate}' is the lowest common ancestor but projects `select *`, "
            "so dex cannot confirm it produces "
            f"{', '.join(inputs)}. Name its columns explicitly and re-run"
        )
    absent = [name for name in inputs if name not in projected]
    if absent:
        return None, (
            f"'{candidate}' is the lowest common ancestor but does not project "
            f"{', '.join(absent)}. dex will not pull an input down from further "
            "upstream, because one placement would then rewrite the graph above "
            "it. Add those columns to it first, and this becomes the answer"
        )
    return candidate, ""


def _projected(
    view: DbtProjectView, nodes: dict[str, str], node: str
) -> set[str] | None:
    path = nodes.get(node)
    if path is None or not path.endswith(".sql"):
        return None
    try:
        return output_columns(view.files[path].content, path)
    except RewriteError:
        return None


def _path_between(index: ReferenceIndex, ancestor: str, target: str) -> list[str]:
    """One ``ref()`` path from ``ancestor`` down to ``target``, ancestor first.

    The shortest one, found by walking up from the target, which is the direction
    the graph is stored in. Where several paths of the same length exist the
    column arrives by whichever is threaded; the others read it from a model that
    already has it, so a shortest path is a complete answer rather than an
    arbitrary one.
    """

    if ancestor == target:
        return [ancestor]
    frontier = [[target]]
    seen = {target}
    while frontier:
        route = frontier.pop(0)
        for parent in sorted(index.parents_of(route[-1])):
            if parent == ancestor:
                return list(reversed([*route, parent]))
            if parent not in seen:
                seen.add(parent)
                frontier.append([*route, parent])
    return [ancestor, target]


@dataclass
class _Threaded:
    edits: list[PlanEdit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _thread(
    view: DbtProjectView,
    project_dir: Path,
    column: str,
    expression: str,
    ancestor: str,
    chain: dict[str, list[str]],
    nodes: dict[str, str],
    placement: PlacingProject | None,
) -> _Threaded:
    """Define the column at ``ancestor`` and carry it down every chain.

    Each model is edited once however many chains pass through it, because an
    edit is a whole-file proposal. The ancestor gets the derivation; every model
    below it gets the bare column, which is the pass-through.
    """

    out = _Threaded()
    ordered: list[str] = []
    for hops in chain.values():
        for hop in hops:
            if hop not in ordered:
                ordered.append(hop)

    for node in ordered:
        path = _edit_path(placement, nodes, node)
        if path is None or path not in view.files:
            out.notes.append(
                f"{node}: this project format has nowhere for dex to write a "
                "model edit, so the column was not threaded through it"
            )
            continue
        source = expression if node == ancestor else column
        try:
            result = project_column_in_sql(
                view.files[path].content, path, source, column
            )
        except RewriteError as exc:
            raise PlacementRefusedError(str(exc)) from exc
        if result.star:
            out.notes.append(
                f"{path}: projects `select *`, so it already carries '{column}' "
                "through and needed no edit"
            )
            continue
        if result.changed == 0:
            out.notes.append(f"{path}: already projects '{column}'")
            continue
        out.edits.append(
            PlanEdit(path=path, new_content=result.content, kind=EditKind.MODEL_SQL)
        )

    # Documented at the ends only: the ancestor defines the column and each
    # target consumes it, so those are the two places a declared column contract
    # is worth keeping current. The hops between carry it and declare nothing.
    out.edits.extend(_document(view, [ancestor, *chain], column, expression))
    return out


def _document(
    view: DbtProjectView, nodes: list[str], column: str, expression: str
) -> list[PlanEdit]:
    """A ``schema.yml`` column entry for each node, one edit per file.

    Accumulated per file rather than per node, because the ancestor and its
    targets routinely share one ``schema.yml`` and an edit is a whole-file
    proposal: emitting one per node would leave every entry after the first
    pinned against content the previous edit had not written.

    A model with no ``columns:`` block is skipped. A model that declares no
    contract has none to keep current, and inventing one would make
    ``column_contract_warnings`` start reporting on every column it never
    declared.
    """

    by_path: dict[str, list[tuple[int, str]]] = {}
    seen: set[str] = set()
    for node in nodes:
        if node in seen:
            continue
        seen.add(node)
        path = _schema_path(view, node)
        if path is None:
            continue
        content = view.files[path].content
        anchor = _columns_anchor(content, node)
        if anchor is None or f"      - name: {column}\n" in content:
            continue
        entry = (
            f"      - name: {column}\n        description: derived as `{expression}`\n"
        )
        by_path.setdefault(path, []).append((anchor, entry))

    edits = []
    for path, insertions in sorted(by_path.items()):
        content = view.files[path].content
        for anchor, entry in sorted(insertions, reverse=True):
            content = content[:anchor] + entry + content[anchor:]
        edits.append(PlanEdit(path=path, new_content=content, kind=EditKind.SCHEMA_YML))
    return edits


def _columns_anchor(content: str, node: str) -> int | None:
    """Where a new column entry goes inside ``node``'s ``columns:`` block.

    A text insertion rather than a YAML round trip, for the reason every rewrite
    in this package is: re-serialising a ``schema.yml`` reflows the whole file and
    drops the comments, and the diff stops showing what changed.
    """

    from .rewrite import yaml_blocks

    blocks = [
        block
        for block in yaml_blocks(content)
        if block.form == "yaml_column" and block.owner == node
    ]
    if not blocks:
        return None
    return max(block.span[1] for block in blocks)


def _edit_path(
    placement: PlacingProject | None, nodes: dict[str, str], node: str
) -> str | None:
    """Where a model edit for ``node`` lands, asking the format when there is one.

    A format is asked about the *warehouse table* the model is built from, which
    is the vocabulary ``edit_path`` is defined in; where the format declines or
    answers a path the project does not have, the model's own file is the answer
    dex already knows to be right.
    """

    known = nodes.get(node)
    if placement is None:
        return known
    answered = placement.edit_path(EditKind.MODEL_SQL, node)
    return answered if answered is not None and known is None else known


def _schema_path(view: DbtProjectView, node: str) -> str | None:
    """The YAML file that documents ``node``, if any documents it."""

    from .rewrite import yaml_blocks

    for path in sorted(view.files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        for block in yaml_blocks(view.files[path].content):
            if block.form == "yaml_model_entry" and block.name == node:
                return path
    return None


def _per_target(
    view: DbtProjectView,
    column: str,
    targets: list[str],
    expression: str,
    inputs: list[str],
    nodes: dict[str, str],
    reason: str,
    placement: PlacingProject | None,
) -> Placement:
    """The fallback: define the column in each target, and say why.

    Proposed rather than refused, because the caller asked for an outcome and this
    does reach it. Named as the worse answer, because the copies will drift and
    somebody should know that before applying it.
    """

    out = _Threaded()
    for target in targets:
        path = _edit_path(placement, nodes, target)
        if path is None or path not in view.files:
            out.notes.append(f"{target}: dex has nowhere to write this model's edit")
            continue
        try:
            result = project_column_in_sql(
                view.files[path].content, path, expression, column
            )
        except RewriteError as exc:
            raise PlacementRefusedError(str(exc)) from exc
        if result.changed:
            out.edits.append(
                PlanEdit(path=path, new_content=result.content, kind=EditKind.MODEL_SQL)
            )
        elif result.star:
            out.notes.append(f"{path}: projects `select *`; add the column by hand")

    return Placement(
        column=column,
        expression=expression,
        inputs=inputs,
        targets=targets,
        strategy="per_target",
        ancestor=None,
        reasoning=[
            reason,
            f"so '{column}' is defined separately in each of "
            f"{', '.join(targets)}. That duplicates the derivation, and the "
            "copies will drift the first time one of them is corrected. If you "
            "would rather not, the fix named above is what makes the shared "
            "definition possible",
        ],
        chain={target: [target] for target in targets},
        edits=out.edits,
        notes=out.notes,
    )
