"""Whether a build actually validated the change, as distinct from whether it ran.

``BuildResult.success`` is dbt's process outcome, which is the right meaning for
a command line and the wrong one for a host deciding whether a change is safe to
promote. A build whose selection matched no nodes at all exits zero. So does a
build of a model the change never touched. So does a build whose every node was
skipped because a parent failed somewhere the selector excluded. All three report
success, and none of them validated anything.

:class:`BuildOutcome` is the field that tells them apart, and everything else here
is the evidence behind it: which nodes actually executed and with what status,
what the selection asked for against what it got, which of the nodes the change
*required* were covered, and digests over the artifacts the answer was read from
so a host can tell that the grounding it admitted and the build it later ran
describe the same tree.

``success`` and ``summary`` are untouched. A consumer reading either keeps
reading exactly what it read before; this sits beside them.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..edits import EditOp
from .plans import EditKind, PlanEdit

#: dbt node statuses that mean the node did not produce a good relation. `fail`
#: is a test that returned rows; `error` and `runtime error` are a node that
#: raised. Kept as data because dbt has spelled the second one both ways.
FAILED_STATUSES = frozenset({"error", "runtime error", "fail", "failed"})

#: Statuses that mean the node was not attempted. `skipped` is dbt's own word for
#: a node whose parent failed, which is exactly the case a caller must not read
#: as a pass.
SKIPPED_STATUSES = frozenset({"skipped"})

#: Edit kinds that name a buildable node by their own path. Everything else
#: either documents nodes (a schema.yml), points at one (a semantic YAML), or
#: can affect any node at all (a macro, the project config).
_NODE_KINDS = frozenset({EditKind.MODEL_SQL, EditKind.SNAPSHOT_SQL, EditKind.SEED_CSV})


class BuildOutcome(str, Enum):
    """What this build establishes about the change, in one word.

    Ordered by precedence in :func:`_outcome`, and the order is the argument:
    a build that ran nothing cannot have validated anything, a build that failed
    is failed whatever it covered, and a build that succeeded against artifacts
    older than the source tree validated a project that is no longer there.
    """

    #: dbt produced no results at all: it never reached node execution.
    NOT_RUN = "not_run"
    #: The selection matched no nodes. Exits zero, validates nothing.
    EMPTY_SELECTION = "empty_selection"
    #: At least one node errored or a test returned rows.
    FAILED = "failed"
    #: Every node was skipped, which usually means a parent failed elsewhere.
    SKIPPED = "skipped"
    #: Nodes ran, and not one of them was a node this change required.
    UNRELATED = "unrelated"
    #: Some of the required nodes ran and some did not.
    PARTIAL = "partial"
    #: Everything required ran and passed, but not against what the plan said.
    #: Either the compiled artifacts predate the model sources, or a file the
    #: plan wrote no longer holds the content the plan wrote, which means the
    #: build validated a tree the plan does not describe.
    STALE = "stale"
    #: Everything required ran and passed, against current artifacts.
    VALIDATED = "validated"


class BuildNode(BaseModel):
    """One node dbt executed, and what became of it."""

    unique_id: str
    name: str
    resource_type: str | None = None
    status: str = "unknown"
    execution_time: float | None = None
    materialization: str | None = None
    relation: str | None = None
    message: str | None = None


class BuildSelection(BaseModel):
    """What the run was asked to build against what it built.

    ``complete`` is ``None`` rather than ``False`` wherever the selector uses
    dbt's graph operators or method selectors, because dex does not evaluate
    dbt's selector language and guessing would be worse than declining. ``empty``
    needs no such caveat: nothing ran, and that is the same fact whatever the
    selector meant.
    """

    requested: str | None = None
    matched: list[str] = Field(default_factory=list)
    empty: bool = True
    complete: bool | None = None
    unmatched: list[str] = Field(default_factory=list)


class PlanDrift(BaseModel):
    """A file the plan wrote whose content on disk is no longer what it wrote.

    Reachable in exactly the situation a host has to catch: the change was
    applied, something else changed the file, and the build then validated a tree
    the plan does not describe. Neither hash is a secret and both are already in
    the plan document, so reporting the pair costs nothing and saves a caller
    re-deriving it.
    """

    path: str
    expected_sha256: str | None = None
    found_sha256: str | None = None


class BuildCoverage(BaseModel):
    """Which of the nodes this change required were actually built.

    ``basis`` says how the requirement was derived, because the derivation is not
    uniform: a model edit names its own node, a schema.yml names the nodes it
    documents, and a semantic YAML names the transformation model its semantic
    model sits on. An edit that can affect any node at all (a macro, the project
    config) contributes no requirement and says so here rather than silently
    contributing nothing.
    """

    required: list[str] = Field(default_factory=list)
    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)


class BuildDigests(BaseModel):
    """Fingerprints of what this build read and what it was asked to validate."""

    manifest_sha256: str | None = None
    run_results_sha256: str | None = None
    source_digest: str | None = None
    plan_digest: str | None = None


class BuildEvidence(BaseModel):
    """Everything a host needs to decide whether this build validated the change."""

    outcome: BuildOutcome = BuildOutcome.NOT_RUN
    invocation: dict[str, Any] = Field(default_factory=dict)
    nodes: list[BuildNode] = Field(default_factory=list)
    selection: BuildSelection = Field(default_factory=BuildSelection)
    coverage: BuildCoverage | None = None
    digests: BuildDigests = Field(default_factory=BuildDigests)
    generated: list[str] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)
    #: Why the outcome is `stale`, where it is. Always present so an empty list
    #: is the positive statement that every file the plan wrote still holds what
    #: the plan wrote.
    plan_drift: list[PlanDrift] = Field(default_factory=list)
    stale_artifacts: bool | None = None

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "invocation": self.invocation,
            "selection": self.selection.model_dump(mode="json"),
            "digests": self.digests.model_dump(mode="json"),
            "generated": self.generated,
            "errors": self.errors,
            "stale_artifacts": self.stale_artifacts,
            "plan_drift": [d.model_dump(mode="json") for d in self.plan_drift],
            "nodes": [n.model_dump(mode="json", exclude_none=True) for n in self.nodes],
        }
        # Absent rather than empty when no plan was named: an empty coverage
        # block reads as "nothing required was built", which is the opposite of
        # "nobody asked what was required".
        if self.coverage is not None:
            payload["coverage"] = self.coverage.model_dump(mode="json")
        return payload


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def required_nodes(edits: list[PlanEdit], project: Path) -> tuple[list[str], list[str]]:
    """The nodes a plan's edits require a build to exercise, and how each was derived.

    Read from the edits themselves rather than from a compiled artifact, so this
    answers for a plan that has been applied but never built, which is the state
    a sandbox is in when it decides what to select.
    """

    from ..dbt_project import node_name

    required: set[str] = set()
    basis: list[str] = []
    for edit in edits:
        if edit.op is EditOp.DELETE:
            # A removed node cannot be built, and requiring it would make every
            # deletion look uncovered forever.
            basis.append(f"{edit.path}: a deletion requires no node")
            continue
        if edit.kind in _NODE_KINDS:
            name = node_name(edit.path)
            required.add(name)
            basis.append(f"{edit.path}: builds '{name}'")
            continue
        if edit.kind in {EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML}:
            named = _names_in_yaml(edit.new_content or "")
            if named:
                required.update(named)
                basis.append(f"{edit.path}: documents {', '.join(sorted(named))}")
            else:
                basis.append(f"{edit.path}: names no buildable node")
            continue
        basis.append(
            f"{edit.path}: a {edit.kind.value} edit can affect any node, so it "
            "requires none in particular"
        )
    return sorted(required), basis


def _names_in_yaml(content: str) -> set[str]:
    """Buildable node names a schema or semantic YAML points at.

    A semantic model's ``model:`` is read through its ``ref()`` because that is
    how the layer names the transformation model underneath it, which is the
    link that makes a semantic change checkable against a build at all.
    """

    from ..dbt_project import REF_PATTERN

    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return set()
    if not isinstance(document, dict):
        return set()

    names: set[str] = set()
    for key in ("models", "seeds", "snapshots"):
        for entry in document.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(entry["name"])
    for entry in document.get("semantic_models") or []:
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if isinstance(model, str):
            match = REF_PATTERN.search(model)
            if match:
                names.add(match.group(1))
    return names


def plan_drift(edits: list[PlanEdit], project: Path) -> list[PlanDrift]:
    """Files the plan wrote that no longer hold what it wrote.

    The reachable half of staleness, and the half a host actually needs: a build
    that succeeded against a tree somebody changed after the plan landed has
    validated something other than the change under review. Compares each upsert
    edit's content hash against the file now on disk; a delete has nothing to
    compare, and a file that is simply gone is reported as such rather than
    skipped.
    """

    from ..edits import content_hash

    drift: list[PlanDrift] = []
    for edit in edits:
        if edit.op is not EditOp.UPSERT or edit.new_content is None:
            continue
        target = project / edit.path
        found = (
            content_hash(target.read_text(encoding="utf-8"))
            if target.is_file()
            else None
        )
        expected = content_hash(edit.new_content)
        if found != expected:
            drift.append(
                PlanDrift(path=edit.path, expected_sha256=expected, found_sha256=found)
            )
    return drift


def _outcome(
    ran: bool,
    nodes: list[BuildNode],
    coverage: BuildCoverage | None,
    stale: bool | None,
    drift: list[PlanDrift],
) -> BuildOutcome:
    if not ran:
        return BuildOutcome.NOT_RUN
    if not nodes:
        return BuildOutcome.EMPTY_SELECTION
    statuses = {node.status.lower() for node in nodes}
    if statuses & FAILED_STATUSES:
        return BuildOutcome.FAILED
    if statuses and statuses <= SKIPPED_STATUSES:
        return BuildOutcome.SKIPPED
    if coverage is not None and coverage.required:
        if not coverage.covered:
            return BuildOutcome.UNRELATED
        if coverage.missing:
            return BuildOutcome.PARTIAL
    if drift or stale:
        return BuildOutcome.STALE
    return BuildOutcome.VALIDATED


#: Selector syntax dex does not evaluate. Their presence is what makes
#: `selection.complete` unanswerable rather than false.
_SELECTOR_OPERATORS = ("+", "@", "*", ":", ",")


def build_evidence(
    project: Path | str,
    *,
    target: str,
    select: str | None = None,
    edits: list[PlanEdit] | None = None,
    plan_digest: str | None = None,
    source_digest: str | None = None,
) -> BuildEvidence:
    """Read dbt's own artifacts and say what this build established.

    Reads ``target/run_results.json`` and ``target/manifest.json`` rather than
    scraping log text, exactly as :func:`~.build._summarize` does, and reads them
    separately from it on purpose: ``summary`` is a released payload shape and
    nothing here may move it.
    """

    from ..dbt_project import load as load_project
    from ..dbt_project import manifest_freshness, strip_relation_quoting

    root = Path(project)
    run_results_path = root / "target" / "run_results.json"
    manifest_path = root / "target" / "manifest.json"

    results: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    document: dict[str, Any] = {}
    ran = run_results_path.is_file()
    if ran:
        try:
            document = json.loads(run_results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
            ran = False
        results = [r for r in document.get("results") or [] if isinstance(r, dict)]
        metadata = document.get("metadata") or {}

    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    manifest_nodes = manifest.get("nodes") or {}

    nodes: list[BuildNode] = []
    generated: list[str] = []
    errors: list[dict[str, str]] = []
    for result in results:
        unique_id = str(result.get("unique_id") or "")
        node = manifest_nodes.get(unique_id) or {}
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        relation = node.get("relation_name")
        status = str(result.get("status") or "unknown")
        # The manifest's own `name` where there is one. A test's unique_id ends
        # in a disambiguating hash (`test.pkg.not_null_orders_id.3249b83c15`),
        # so the last segment is a checksum rather than a name a reader can look
        # up, and the fallback only fires for a node the manifest does not carry.
        built = BuildNode(
            unique_id=unique_id,
            name=str(node.get("name") or unique_id.split(".")[-1]),
            resource_type=node.get("resource_type"),
            status=status,
            execution_time=result.get("execution_time"),
            materialization=(config or {}).get("materialized"),
            relation=strip_relation_quoting(relation)
            if isinstance(relation, str) and relation
            else None,
            # dbt's message on a failure is the actionable half; it is bounded
            # here for the same reason build messages are, since a traceback in a
            # payload is a payload nobody reads.
            message=(
                str(result.get("message"))[:400] if result.get("message") else None
            ),
        )
        nodes.append(built)
        if status.lower() in FAILED_STATUSES:
            errors.append({"node": built.name, "message": built.message or status})
        elif built.relation and built.resource_type in {"model", "snapshot", "seed"}:
            generated.append(built.relation)

    matched = [n.name for n in nodes]
    selection = BuildSelection(
        requested=select,
        matched=matched,
        empty=not matched,
        complete=_selection_complete(select, matched),
        unmatched=_unmatched(select, matched),
    )

    coverage = None
    drift: list[PlanDrift] = []
    if edits is not None:
        drift = plan_drift(edits, root)
        required, basis = required_nodes(edits, root)
        covered = [name for name in required if name in matched]
        coverage = BuildCoverage(
            required=required,
            covered=covered,
            missing=[name for name in required if name not in matched],
            basis=basis,
        )

    stale: bool | None = None
    try:
        stale = manifest_freshness(load_project(root)).get("stale")
    except Exception:
        stale = None

    return BuildEvidence(
        outcome=_outcome(ran, nodes, coverage, stale, drift),
        invocation={
            "invocation_id": metadata.get("invocation_id"),
            "generated_at": metadata.get("generated_at"),
            "started_at": metadata.get("invocation_started_at"),
            "dbt_version": (metadata.get("dbt_version") or None),
            "command": "build",
            "target": target,
            "select": select,
            # dbt writes this at the top of run_results, not inside `metadata`.
            "elapsed_seconds": (
                float(elapsed)
                if isinstance(elapsed := document.get("elapsed_time"), (int, float))
                else None
            ),
        },
        nodes=nodes,
        selection=selection,
        coverage=coverage,
        digests=BuildDigests(
            manifest_sha256=_file_digest(manifest_path),
            run_results_sha256=_file_digest(run_results_path),
            source_digest=source_digest,
            plan_digest=plan_digest,
        ),
        generated=sorted(set(generated)),
        errors=errors,
        plan_drift=drift,
        stale_artifacts=stale,
    )


def _selection_complete(select: str | None, matched: list[str]) -> bool | None:
    """Whether everything the selector literally named was built.

    ``None`` for a selector dex does not evaluate. dbt's selector language has
    graph operators, method selectors, and unions, and reimplementing it here
    would be a second resolver that disagrees with dbt's, which is the specific
    thing this contract exists to avoid.
    """

    if select is None:
        return bool(matched)
    if any(op in select for op in _SELECTOR_OPERATORS) or " " in select.strip():
        names = [part for part in select.split() if part]
        if any(any(op in name for op in _SELECTOR_OPERATORS) for name in names):
            return None
        return all(name in matched for name in names)
    return select in matched


def _unmatched(select: str | None, matched: list[str]) -> list[str]:
    """Literal names in the selector that no node answered to."""

    if select is None:
        return []
    names = [part for part in select.split() if part]
    if any(any(op in name for op in _SELECTOR_OPERATORS) for name in names):
        return []
    return [name for name in names if name not in matched]
