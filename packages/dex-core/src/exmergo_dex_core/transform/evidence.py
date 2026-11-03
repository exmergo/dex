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
from pydantic import BaseModel, Field, model_validator

from ..edits import EditOp
from .plans import EditKind, PlanEdit, PlanError

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
    #: A plan was named, but no buildable requirement could be derived for its
    #: edits (a metric whose measure cannot be located, a macro, a project-config
    #: change). Distinct from ``validated`` with an empty required list, which
    #: previously over-claimed coverage.
    COVERAGE_UNDETERMINED = "coverage_undetermined"
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
    model sits on — including following a metric to its measure and that measure
    to its model. An edit that can affect any node at all (a macro, the project
    config), or a metric whose measure cannot be located, contributes no
    requirement, sets ``undetermined``, and says so here rather than silently
    contributing nothing.
    """

    required: list[str] = Field(default_factory=list)
    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)
    #: True when at least one edit could not derive a buildable requirement and
    #: ``required`` is therefore empty. Hosts must not read an empty required list
    #: as "nothing to check" / validated.
    undetermined: bool = False


class BuildDigests(BaseModel):
    """Fingerprints of what this build read and what it was asked to validate."""

    manifest_sha256: str | None = None
    run_results_sha256: str | None = None
    source_digest: str | None = None
    plan_digest: str | None = None


class BuildEvidenceError(PlanError, ValueError):
    """A build evidence document was unreadable or contradicts its fields."""


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

    def __getitem__(self, key: str) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in type(self).model_fields

    def get(self, key: str, default: Any = None) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        return default

    @model_validator(mode="after")
    def _validate_outcome(self) -> BuildEvidence:
        recomputed = recompute_outcome(self)
        if self.outcome != recomputed:
            raise BuildEvidenceError(
                f"outcome '{self.outcome.value}' contradicts evidence fields "
                f"(recomputes to '{recomputed.value}')"
            )
        return self

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


def required_nodes(
    edits: list[PlanEdit], project: Path
) -> tuple[list[str], list[str], bool]:
    """The nodes a plan's edits require a build to exercise, and how each was derived.

    Read from the edits themselves rather than from a compiled artifact, so this
    answers for a plan that has been applied but never built, which is the state
    a sandbox is in when it decides what to select.

    Returns ``(required, basis, undetermined)``. ``undetermined`` is true when
    every concrete requirement is empty because at least one edit could not name
    a buildable node (an unresolved metric, a macro, a project-config change).
    """

    from ..dbt_project import node_name

    required: set[str] = set()
    basis: list[str] = []
    undetermined_edits = False
    # Measure -> transformation model, union of plan edits and on-disk YAML.
    measure_owners = _measure_owners(edits, project)
    metric_defs = _metric_definitions(edits, project)

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
            named, detail, unresolved = _requirements_from_yaml(
                edit.new_content or "",
                measure_owners=measure_owners,
                metric_defs=metric_defs,
            )
            if named:
                required.update(named)
                basis.append(f"{edit.path}: {detail}")
            elif unresolved:
                undetermined_edits = True
                basis.append(f"{edit.path}: {detail}")
            else:
                undetermined_edits = True
                basis.append(f"{edit.path}: names no buildable node")
            continue
        undetermined_edits = True
        basis.append(
            f"{edit.path}: a {edit.kind.value} edit can affect any node, so it "
            "requires none in particular"
        )
    undetermined = undetermined_edits and not required
    return sorted(required), basis, undetermined


def _ref_model(value: Any) -> str | None:
    from ..dbt_project import REF_PATTERN

    if not isinstance(value, str):
        return None
    match = REF_PATTERN.search(value)
    return match.group(1) if match else None


def _measure_owners(edits: list[PlanEdit], project: Path) -> dict[str, str]:
    """Map measure name -> transformation model from plan edits then on-disk YAML."""

    owners: dict[str, str] = {}

    def absorb(document: Any) -> None:
        if not isinstance(document, dict):
            return
        for entry in document.get("semantic_models") or []:
            if not isinstance(entry, dict):
                continue
            model = _ref_model(entry.get("model"))
            if not model:
                continue
            for measure in entry.get("measures") or []:
                if isinstance(measure, dict) and isinstance(measure.get("name"), str):
                    owners[measure["name"]] = model

    for edit in edits:
        if edit.kind is not EditKind.SEMANTIC_YML or not edit.new_content:
            continue
        try:
            absorb(yaml.safe_load(edit.new_content))
        except yaml.YAMLError:
            continue

    if project.is_dir():
        for path in project.rglob("*"):
            if path.suffix not in {".yml", ".yaml"} or not path.is_file():
                continue
            try:
                absorb(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                continue
    return owners


def _metric_definitions(
    edits: list[PlanEdit], project: Path
) -> dict[str, dict[str, Any]]:
    """Metric name -> raw YAML entry, plan edits overriding on-disk files."""

    defs: dict[str, dict[str, Any]] = {}

    def absorb(document: Any) -> None:
        if not isinstance(document, dict):
            return
        for entry in document.get("metrics") or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                defs[entry["name"]] = entry
        # create_metric: true measures are addressable as metrics.
        for entry in document.get("semantic_models") or []:
            if not isinstance(entry, dict):
                continue
            for measure in entry.get("measures") or []:
                if (
                    isinstance(measure, dict)
                    and measure.get("create_metric")
                    and isinstance(measure.get("name"), str)
                ):
                    defs.setdefault(
                        measure["name"],
                        {
                            "name": measure["name"],
                            "type": "simple",
                            "type_params": {"measure": measure["name"]},
                        },
                    )

    if project.is_dir():
        for path in project.rglob("*"):
            if path.suffix not in {".yml", ".yaml"} or not path.is_file():
                continue
            try:
                absorb(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                continue
    for edit in edits:
        if edit.kind is not EditKind.SEMANTIC_YML or not edit.new_content:
            continue
        try:
            absorb(yaml.safe_load(edit.new_content))
        except yaml.YAMLError:
            continue
    return defs


def _grounded_measures(
    metric_name: str,
    metric_defs: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> list[str]:
    from ..dbt_project import metric_inputs

    if seen is None:
        seen = set()
    if metric_name in seen:
        return []
    seen.add(metric_name)
    entry = metric_defs.get(metric_name)
    if entry is None:
        return []
    measures, metrics = metric_inputs(entry)
    grounded = list(measures)
    for name in metrics:
        grounded.extend(_grounded_measures(name, metric_defs, seen))
    return grounded


def _requirements_from_yaml(
    content: str,
    *,
    measure_owners: dict[str, str],
    metric_defs: dict[str, dict[str, Any]],
) -> tuple[set[str], str, bool]:
    """Buildable node names a schema or semantic YAML requires.

    Returns ``(names, detail, unresolved)``. ``unresolved`` is true when the
    document named metrics (or similar) whose measures could not be located, so
    the caller can mark coverage undetermined rather than silently empty.
    """

    from ..dbt_project import REF_PATTERN

    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return set(), "names no buildable node", True
    if not isinstance(document, dict):
        return set(), "names no buildable node", True

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

    unresolved_metrics: list[str] = []
    metric_models: set[str] = set()
    for entry in document.get("metrics") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        metric_name = entry["name"]
        # Prefer the edit's own body for this metric's inputs.
        local_defs = dict(metric_defs)
        local_defs[metric_name] = entry
        grounded = _grounded_measures(metric_name, local_defs)
        if not grounded:
            unresolved_metrics.append(metric_name)
            continue
        found_any = False
        for measure in grounded:
            owner = measure_owners.get(measure)
            if owner:
                metric_models.add(owner)
                found_any = True
        if not found_any:
            unresolved_metrics.append(metric_name)

    names.update(metric_models)
    if names and not unresolved_metrics:
        detail = f"documents {', '.join(sorted(names))}"
        return names, detail, False
    if names and unresolved_metrics:
        detail = (
            f"documents {', '.join(sorted(names))}; unresolved metrics: "
            + ", ".join(unresolved_metrics)
        )
        return names, detail, False
    if unresolved_metrics:
        detail = "names metrics whose measures could not be located: " + ", ".join(
            unresolved_metrics
        )
        return set(), detail, True
    return set(), "names no buildable node", True


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
    if coverage is not None and coverage.undetermined and not coverage.required:
        return BuildOutcome.COVERAGE_UNDETERMINED
    if drift or stale:
        return BuildOutcome.STALE
    return BuildOutcome.VALIDATED


def recompute_outcome(evidence: BuildEvidence | dict[str, Any]) -> BuildOutcome:
    """Recompute what build outcome an evidence document actually establishes.

    Uses the same precedence and rules as :func:`_outcome`, reading from the
    carried nodes, coverage, plan drift, and artifacts freshness.
    """
    if isinstance(evidence, dict):
        nodes_data = evidence.get("nodes") or []
        nodes = [
            BuildNode.model_validate(n) if isinstance(n, dict) else n
            for n in nodes_data
        ]
        coverage_data = evidence.get("coverage")
        coverage = (
            BuildCoverage.model_validate(coverage_data)
            if isinstance(coverage_data, dict)
            else coverage_data
        )
        stale = evidence.get("stale_artifacts")
        drift_data = evidence.get("plan_drift") or []
        drift = [
            PlanDrift.model_validate(d) if isinstance(d, dict) else d
            for d in drift_data
        ]
        invocation_data = evidence.get("invocation") or {}
        inv_id = (
            invocation_data.get("invocation_id")
            if isinstance(invocation_data, dict)
            else getattr(invocation_data, "invocation_id", None)
        )
        elapsed = (
            invocation_data.get("elapsed_seconds")
            if isinstance(invocation_data, dict)
            else getattr(invocation_data, "elapsed_seconds", None)
        )
        digests_data = evidence.get("digests") or {}
        run_results_sha = (
            digests_data.get("run_results_sha256")
            if isinstance(digests_data, dict)
            else getattr(digests_data, "run_results_sha256", None)
        )
    else:
        nodes = evidence.nodes
        coverage = evidence.coverage
        stale = evidence.stale_artifacts
        drift = evidence.plan_drift
        inv_id = evidence.invocation.get("invocation_id")
        elapsed = evidence.invocation.get("elapsed_seconds")
        run_results_sha = (
            evidence.digests.run_results_sha256
            if evidence.digests is not None
            else None
        )

    ran = bool(nodes) or bool(run_results_sha) or bool(inv_id) or (elapsed is not None)
    return _outcome(ran, nodes, coverage, stale, drift)


def verify_build_evidence(
    document: BuildEvidence | dict[str, Any] | str | bytes,
    *,
    expect_outcome: BuildOutcome | str | None = None,
) -> BuildEvidence:
    """Verify that build evidence is internally consistent and not contradictory.

    Validates that the outcome the document claims is the outcome its carried
    nodes, coverage, drift and artifact freshness actually justify, matching
    :func:`recompute_outcome`.

    If ``expect_outcome`` is given, asserts that the verified outcome matches
    that expected verdict.
    """
    if isinstance(document, (str, bytes)):
        try:
            parsed = json.loads(document)
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildEvidenceError(
                f"not a readable build-evidence document: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise BuildEvidenceError("build-evidence document must be a JSON object")
        document = parsed

    if isinstance(document, dict):
        try:
            evidence = BuildEvidence.model_validate(document)
        except Exception as exc:
            msg = str(exc)
            if hasattr(exc, "errors") and exc.errors():
                first = exc.errors()[0]
                if "ctx" in first and "error" in first["ctx"]:
                    msg = str(first["ctx"]["error"])
            raise BuildEvidenceError(f"invalid build-evidence document: {msg}") from exc
    elif isinstance(document, BuildEvidence):
        evidence = document
        recomputed = recompute_outcome(evidence)
        if evidence.outcome != recomputed:
            raise BuildEvidenceError(
                f"outcome '{evidence.outcome.value}' contradicts evidence fields "
                f"(recomputes to '{recomputed.value}')"
            )
    else:
        raise BuildEvidenceError(
            f"expected BuildEvidence, dict, str or bytes, got {type(document).__name__}"
        )

    if expect_outcome is not None:
        expected = BuildOutcome(expect_outcome)
        if evidence.outcome != expected:
            raise BuildEvidenceError(
                f"build outcome '{evidence.outcome.value}' is not the expected "
                f"'{expected.value}'"
            )

    return evidence


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
    artifact_directory: Path | str | None = None,
    expected_invocation_id: str | None = None,
    full_refresh: bool = False,
) -> BuildEvidence:
    """Read dbt's own artifacts and say what this build established.

    Reads ``run_results.json`` and ``manifest.json`` from ``artifact_directory``
    (by default the newest on disk, see
    :func:`~.artifact_runtime.readable_artifact_dir`) rather than scraping log
    text, exactly as :func:`~.build._summarize` does, and reads them
    separately from it on purpose. ``summary`` is a released payload shape
    and nothing here may move it.

    ``expected_invocation_id``, when set, refuses artifacts whose invocation id
    is a different id. A missing file has no id to disagree with and stays
    ``not_run`` rather than a successful completion.

    ``full_refresh`` records under ``invocation`` that the build ran with
    ``--full-refresh`` (issue #1015).
    """

    from ..dbt_project import load as load_project
    from ..dbt_project import (
        manifest_freshness,
        manifest_node_label,
        node_name,
        strip_relation_quoting,
    )
    from .artifact_runtime import (
        artifact_paths,
        artifact_producer,
        assert_same_invocation,
        readable_artifact_dir,
    )

    root = Path(project)
    if artifact_directory is None:
        chosen = readable_artifact_dir(root)
    else:
        chosen = Path(artifact_directory)
        if not chosen.is_absolute():
            chosen = root / chosen
    paths = artifact_paths(chosen)
    run_results_path = paths.run_results
    manifest_path = paths.manifest

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
    # Every name a built node answers to, not only its label: a selector or an
    # edit can name a versioned model by its unversioned name (which selects
    # every version) or by the file it is defined in (`dim_customers_v2.sql`),
    # and each of those did build this node.
    answers: set[str] = set()
    generated: list[str] = []
    errors: list[dict[str, str]] = []
    for result in results:
        unique_id = str(result.get("unique_id") or "")
        node = manifest_nodes.get(unique_id) or {}
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        relation = node.get("relation_name")
        status = str(result.get("status") or "unknown")
        built = BuildNode(
            unique_id=unique_id,
            name=manifest_node_label(unique_id, node or None),
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
        answers.add(built.name)
        if isinstance(node.get("name"), str):
            answers.add(node["name"])
        if node.get("resource_type") == "model" and isinstance(
            node.get("original_file_path"), str
        ):
            answers.add(node_name(node["original_file_path"]))
        if status.lower() in FAILED_STATUSES:
            errors.append({"node": built.name, "message": built.message or status})
        elif built.relation and built.resource_type in {"model", "snapshot", "seed"}:
            generated.append(built.relation)

    matched = [n.name for n in nodes]
    selection = BuildSelection(
        requested=select,
        matched=matched,
        empty=not matched,
        complete=_selection_complete(select, answers),
        unmatched=_unmatched(select, answers),
    )

    coverage = None
    drift: list[PlanDrift] = []
    if edits is not None:
        drift = plan_drift(edits, root)
        required, basis, undetermined = required_nodes(edits, root)
        covered = [name for name in required if name in answers]
        coverage = BuildCoverage(
            required=required,
            covered=covered,
            missing=[name for name in required if name not in answers],
            basis=basis,
            undetermined=undetermined,
        )

    stale: bool | None = None
    try:
        stale = manifest_freshness(load_project(root)).get("stale")
    except Exception:
        stale = None

    producer = artifact_producer(
        project_root=root,
        target=target,
        artifact_directory=paths.directory,
        metadata=metadata if isinstance(metadata, dict) else {},
        project_name=(
            str(manifest.get("metadata", {}).get("project_name"))
            if isinstance(manifest.get("metadata"), dict)
            and manifest.get("metadata", {}).get("project_name")
            else None
        ),
        adapter_version=(
            str(metadata["adapter_version"])
            if isinstance(metadata, dict) and metadata.get("adapter_version")
            else None
        ),
    )
    assert_same_invocation(
        expected_invocation_id,
        (
            str(metadata["invocation_id"])
            if isinstance(metadata, dict) and metadata.get("invocation_id")
            else None
        ),
    )

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
            "full_refresh": full_refresh,
            "artifact_producer": producer.as_dict(),
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
            run_results_sha256=_file_digest(run_results_path) if ran else None,
            source_digest=source_digest,
            plan_digest=plan_digest,
        ),
        generated=sorted(set(generated)),
        errors=errors,
        plan_drift=drift,
        stale_artifacts=stale,
    )


def _selection_complete(select: str | None, answers: set[str]) -> bool | None:
    """Whether everything the selector literally named was built.

    ``None`` for a selector dex does not evaluate. dbt's selector language has
    graph operators, method selectors, and unions, and reimplementing it here
    would be a second resolver that disagrees with dbt's, which is the specific
    thing this contract exists to avoid.
    """

    if select is None:
        return bool(answers)
    if any(op in select for op in _SELECTOR_OPERATORS) or " " in select.strip():
        names = [part for part in select.split() if part]
        if any(any(op in name for op in _SELECTOR_OPERATORS) for name in names):
            return None
        return all(name in answers for name in names)
    return select in answers


def _unmatched(select: str | None, answers: set[str]) -> list[str]:
    """Literal names in the selector that no node answered to."""

    if select is None:
        return []
    names = [part for part in select.split() if part]
    if any(any(op in name for op in _SELECTOR_OPERATORS) for name in names):
        return []
    return [name for name in names if name not in answers]
