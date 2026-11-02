"""What a build established, in a block that survives a truncated read (#855).

A build payload grows with the project: ``data.nodes`` and
``data.evidence.nodes`` hold one entry per node dbt ran, and a project of a few
hundred staging models pushes everything that matters (the deliverable that was
skipped, the check that did not run, the finding that contradicts the change)
past the point where an agent harness cuts the output off. The verdict was
always in the payload; it was in the wrong place.

:class:`BuildCompletion` is the bounded answer, derived only from evidence the
build already produced: :class:`~.evidence.BuildEvidence`, the verification
sweep, and the manifest's dependency edges. It leads ``data`` and is held to
:data:`COMPLETION_BUDGET_BYTES` serialized, with every cut counted under
``elided``, so a reader who stops after the first few kilobytes still learns
whether the change's deliverables were built, what was checked, what was not,
and where the rest of the evidence is.

It keeps four answers apart that a green exit code runs together:

* ``process``: what dbt reported (its exit, its node counts);
* ``deliverables``: which of the nodes the named change required were built,
  failed, skipped, missing or stale, and ``coverage: unknown`` where no change
  was named, rather than an empty list that reads as "nothing was required";
* ``verification`` and ``obligations``: what the sweep checked, what it found,
  what it suppressed, and which declared obligations stayed unchecked, so an
  empty findings list never stands in for coverage;
* ``assurance``: whether anything checked a built deliverable's rows, since a
  model that built with no test and no declared fact is only build-validated.

Nothing here decides a status or refuses anything. ``success`` and
``outcome`` keep their meanings; this reports what they rest on.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from .evidence import FAILED_STATUSES, SKIPPED_STATUSES, BuildEvidence, BuildNode

__all__ = [
    "COMPLETION_BUDGET_BYTES",
    "BuildCompletion",
    "NameList",
    "node_answers",
    "summarize_completion",
]

#: The serialized size the block is held to. Chosen so the whole block fits in
#: the first few kilobytes of the envelope, which is where a `head -c` or a
#: sliced print stops reading.
COMPLETION_BUDGET_BYTES = 4096

#: How many names one list shows before the rest are counted rather than named.
_NAMES = 5
#: How long one dbt message may run in the block. The full message is in
#: ``data.evidence.nodes``.
_MESSAGE = 160

#: Where the unbounded evidence is, in reading order.
_DETAILS = ["data.evidence", "data.verification", "data.nodes"]

#: dbt statuses that say a test passed, and so that something checked the rows
#: of the model it reads.
_PASSING_TEST = frozenset({"pass", "success"})

#: Resource types whose node builds a relation a deliverable can be.
_BUILDABLE = frozenset({"model", "seed", "snapshot"})

#: The longest selector the block repeats; the full one is under
#: ``data.evidence.selection.requested``.
_SELECTOR = 200

#: The record lists a verification payload carries, each judged per model.
_RECORD_KINDS = (
    "join_coverage",
    "parent_population",
    "temporal_contracts",
    "retirement",
)

#: The deliverable lists that say the change is not done. Each keeps at least
#: one name however tight the budget.
_UNFINISHED = frozenset({"failed", "skipped", "missing", "stale"})

_PREDATES = (
    "not rebuilt by this run; any relation it has in the dev target predates "
    "this run and was not verified by it"
)


class NameList(BaseModel):
    """A count, the first few names, and how many more were not named."""

    count: int = 0
    names: list[str] = Field(default_factory=list)
    elided: int = 0


class ChangeRef(BaseModel):
    """The change this build was asked to validate, if one was named."""

    plan_id: str | None = None
    source: Literal["for_plan", "for_plan_file"] | None = None
    selector: str | None = None


class Blocked(BaseModel):
    """A required deliverable that did not build, and what stopped it."""

    deliverable: str
    status: str
    cause: str | None = None


class Deliverables(BaseModel):
    """Which of the nodes the change required this run actually built.

    ``coverage`` is ``unknown`` where no plan was named, because without one
    nothing says which nodes were the point of the build; ``undetermined``
    where a plan was named but its edits name no buildable node (a macro, the
    project config). Neither is a statement that nothing was required.
    """

    coverage: Literal["known", "unknown", "undetermined"] = "unknown"
    required: int = 0
    built: NameList = Field(default_factory=NameList)
    failed: NameList = Field(default_factory=NameList)
    skipped: NameList = Field(default_factory=NameList)
    missing: NameList = Field(default_factory=NameList)
    stale: NameList = Field(default_factory=NameList)
    blocked: list[Blocked] = Field(default_factory=list)
    note: str | None = None


class Assurance(BaseModel):
    """Whether anything checked the rows of what was built.

    ``asserted`` is a deliverable a passing test reads, or one a verification
    record judged ``satisfied``. ``build_only`` built and nothing checked it:
    the build says dbt ran its SQL, not that its answer is right.
    """

    basis: Literal["required", "built_models"] = "built_models"
    asserted: NameList = Field(default_factory=NameList)
    #: Built, and a data test or a verification record on it failed.
    failed_checks: NameList = Field(default_factory=NameList)
    build_only: NameList = Field(default_factory=NameList)


class Blocker(BaseModel):
    node: str
    status: str
    #: ``project`` or ``package``, and how it relates to the named plan; the
    #: repair routes are under ``data.prerequisites`` (#860).
    owner: str | None = None
    package: str | None = None
    plan_relation: str | None = None
    message: str | None = None


class FindingRef(BaseModel):
    code: str
    identifier: str
    severity: str


class VerificationSummary(BaseModel):
    """What the sweep did, in counts, with the full answer under
    ``data.verification``."""

    ran: bool = False
    reason: str | None = None
    finding_count: int = 0
    findings_dropped: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    top: list[FindingRef] = Field(default_factory=list)
    suppressed: list[str] = Field(default_factory=list)
    offered: bool = False


class Obligation(BaseModel):
    model: str
    kind: str
    status: str
    #: The column an identifier-conversion obligation is about (#861).
    column: str | None = None
    #: The finding a contradicted record raised (`join_orphans`, ...), so the
    #: block says what was wrong without a trip to ``data.verification``.
    finding: str | None = None
    reason_code: str | None = None


class Obligations(BaseModel):
    """The declared contracts the sweep held the build to, by status.

    ``checked`` is false when the sweep did not run, so "no contradicted
    obligation" is never read off a run that judged none.
    """

    checked: bool = False
    by_status: dict[str, int] = Field(default_factory=dict)
    contradicted: list[Obligation] = Field(default_factory=list)
    unchecked: list[Obligation] = Field(default_factory=list)
    no_declared_columns: NameList = Field(default_factory=NameList)
    #: Required models whose declared population is a sentence: nothing can run
    #: it, as distinct from a parent or filter a record above measured (#1029).
    prose_population: NameList = Field(default_factory=NameList)
    #: Identifier conversions the plan added that change which entities a
    #: model holds with nothing declared recording it (`undeclared`), or that
    #: dex could not measure (`unchecked`), as the plan recorded them (#861).
    identifier_conversions: list[Obligation] | None = None


class ColdSummary(BaseModel):
    """What ``--cold-deps`` established, in counts (#860)."""

    ran: bool = False
    reinstalled: bool = False
    installed_edits_lost: int = 0
    paths: list[str] = Field(default_factory=list)
    not_compared: list[str] = Field(default_factory=list)
    reason: str | None = None


class ComparisonState(BaseModel):
    """One rewritten model's refactor comparison, as the plan recorded it."""

    model: str
    status: str
    static_attribution: str | None = None
    reason_code: str | None = None
    compared_at: str | None = None


class BuildCompletion(BaseModel):
    """The bounded lead of a build payload. See the module docstring."""

    outcome: str
    process: dict[str, Any] = Field(default_factory=dict)
    change: ChangeRef = Field(default_factory=ChangeRef)
    deliverables: Deliverables = Field(default_factory=Deliverables)
    blockers: list[Blocker] = Field(default_factory=list)
    assurance: Assurance = Field(default_factory=Assurance)
    verification: VerificationSummary = Field(default_factory=VerificationSummary)
    obligations: Obligations = Field(default_factory=Obligations)
    refactor_comparison: list[ComparisonState] | None = None
    cold: ColdSummary | None = None
    details: list[str] = Field(default_factory=lambda: list(_DETAILS))
    elided: dict[str, int] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def size(self) -> int:
        return len(json.dumps(self.data()).encode("utf-8"))


def node_answers(node: BuildNode) -> set[str]:
    """Every name a required deliverable may use for this node.

    The node's label, its manifest name read off the unique id, and for a
    versioned model the file stem dbt's convention gives it
    (``dim_customers.v2`` is defined in ``dim_customers_v2.sql``), which is
    what :func:`~.evidence.required_nodes` derives from an edit's path.
    """

    answers = {node.name}
    parts = node.unique_id.split(".")
    # Only a node that builds a relation can stand for a deliverable: a unit
    # test's id carries its model's name too, and running it builds nothing.
    if parts[0] not in _BUILDABLE:
        return set()
    if len(parts) >= 3:
        answers.add(parts[2])
        if parts[0] == "model" and len(parts) == 4 and parts[3].startswith("v"):
            answers.add(f"{parts[2]}_{parts[3]}")
    if "." in node.name:
        base, _, version = node.name.partition(".")
        answers.add(f"{base}_{version}")
    return answers


def _names(values: Iterable[str], limit: int = _NAMES) -> NameList:
    listed = list(dict.fromkeys(values))
    return NameList(
        count=len(listed), names=listed[:limit], elided=max(0, len(listed) - limit)
    )


def _status_kind(status: str) -> str:
    lowered = status.lower()
    if lowered in FAILED_STATUSES:
        return "failed"
    if lowered in SKIPPED_STATUSES:
        return "skipped"
    return "built"


def _tests_on(graph: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Each node's data tests, from the tests' own dependency edges."""

    found: dict[str, list[str]] = {}
    for unique_id, parents in graph.items():
        if unique_id.startswith("test."):
            for parent in parents:
                found.setdefault(parent, []).append(unique_id)
    return found


def _failed_ancestor(
    unique_id: str,
    graph: Mapping[str, list[str]],
    status_of: Mapping[str, str],
    label_of: Mapping[str, str],
    tests_on: Mapping[str, list[str]] | None = None,
) -> str | None:
    """The node that actually failed behind a skip, walked through skipped
    parents, the way the verify sweep's build-status half names it.

    ``dbt build`` also skips a model's children when a data test on that model
    fails, and the test is not their parent, so a parent's failed tests are
    checked as well: it is the most common skip after a model error.
    """

    tests_on = tests_on or {}
    seen: set[str] = set()
    pending = [unique_id]
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for parent in graph.get(current, []):
            status = status_of.get(parent, "").lower()
            if status in FAILED_STATUSES:
                return label_of.get(parent, parent)
            for test in tests_on.get(parent, []):
                if status_of.get(test, "").lower() in FAILED_STATUSES:
                    return label_of.get(test, test)
            if status in SKIPPED_STATUSES:
                pending.append(parent)
    return None


def _deliverables(
    evidence: BuildEvidence,
    graph: Mapping[str, list[str]],
) -> tuple[Deliverables, list[str], list[str]]:
    """The deliverables block, the names of what built, and the failed nodes
    that caused a required deliverable not to build (blockers to rank first)."""

    status_of = {n.unique_id: n.status for n in evidence.nodes}
    label_of = {n.unique_id: n.name for n in evidence.nodes}
    tests_on = _tests_on(graph)
    built_models = [
        n.name
        for n in evidence.nodes
        if n.unique_id.startswith("model.") and _status_kind(n.status) == "built"
    ]
    coverage = evidence.coverage
    stale_paths = [d.path for d in evidence.plan_drift]
    stale_names = list(stale_paths)
    note: str | None = None
    if evidence.stale_artifacts:
        stale_names.append("(compiled artifacts older than the model sources)")

    if coverage is None:
        return (
            Deliverables(
                coverage="unknown",
                stale=_names(stale_names),
                note=(
                    "no plan was named, so nothing says which nodes this build was "
                    "for; pass --for-plan <id> (or --for-plan-file) to learn whether "
                    "the change's deliverables were built"
                ),
            ),
            built_models,
            [],
        )
    if coverage.undetermined and not coverage.required:
        return (
            Deliverables(
                coverage="undetermined",
                stale=_names(stale_names),
                note=(
                    "the plan's edits name no buildable node (a macro, the project "
                    "config, or a metric whose model could not be located), so no "
                    "deliverable could be checked"
                ),
            ),
            built_models,
            [],
        )

    built: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    blocked: list[Blocked] = []
    causes: list[str] = []
    missing: list[str] = []
    for name in coverage.required:
        matches = [n for n in evidence.nodes if name in node_answers(n)]
        if not matches:
            missing.append(name)
            continue
        kinds = {_status_kind(n.status) for n in matches}
        node = matches[0]
        if "failed" in kinds:
            failed.append(name)
            failing = next(n for n in matches if _status_kind(n.status) == "failed")
            blocked.append(Blocked(deliverable=name, status=failing.status))
            causes.append(failing.name)
        elif "skipped" in kinds:
            skipped.append(name)
            cause = _failed_ancestor(
                node.unique_id, graph, status_of, label_of, tests_on
            )
            blocked.append(Blocked(deliverable=name, status=node.status, cause=cause))
            if cause:
                causes.append(cause)
        else:
            built.append(name)
    if failed or skipped or missing:
        note = _PREDATES
    return (
        Deliverables(
            coverage="known",
            required=len(coverage.required),
            built=_names(built),
            failed=_names(failed),
            skipped=_names(skipped),
            missing=_names(missing),
            stale=_names(stale_names),
            blocked=blocked,
            note=note,
        ),
        built if coverage.required else built_models,
        causes,
    )


def _cold_summary(report: Mapping[str, Any] | None) -> ColdSummary | None:
    if report is None:
        return None
    edits = report.get("installed_edits") or {}
    groups = [edits.get(key) or {} for key in ("changed", "added", "removed")]
    return ColdSummary(
        ran=bool(report.get("ran")),
        reinstalled=bool(report.get("reinstalled")),
        installed_edits_lost=sum(int(g.get("count") or 0) for g in groups),
        paths=[path for g in groups for path in g.get("paths") or []][:_NAMES],
        not_compared=list(edits.get("not_compared") or [])[:_NAMES],
        reason=report.get("reason"),
    )


def _blockers(
    evidence: BuildEvidence,
    causes: list[str],
    prerequisites: Iterable[Mapping[str, Any]] = (),
) -> list[Blocker]:
    """Every node that failed, the ones that stopped a deliverable first."""

    diagnosed = {entry.get("unique_id"): entry for entry in prerequisites}

    failed = [n for n in evidence.nodes if _status_kind(n.status) == "failed"]
    rank = {name: index for index, name in enumerate(causes)}
    failed.sort(key=lambda n: (rank.get(n.name, len(rank)), n.name))
    return [
        Blocker(
            node=n.name,
            status=n.status,
            owner=diagnosed.get(n.unique_id, {}).get("owner"),
            package=diagnosed.get(n.unique_id, {}).get("package"),
            plan_relation=diagnosed.get(n.unique_id, {}).get("plan_relation"),
            message=(n.message[:_MESSAGE] if n.message else None),
        )
        for n in failed
    ]


def _checked(
    evidence: BuildEvidence,
    graph: Mapping[str, list[str]],
    verification: Mapping[str, Any] | None,
) -> tuple[set[str], set[str]]:
    """Models whose built rows something in this run checked: those every
    check passed on, and those a check failed on.

    Data tests and verification records read the built relation. A unit test
    reads fixtures, so it vouches for the SQL and not for the rows, and is not
    counted here.
    """

    label_of = {n.unique_id: n.name for n in evidence.nodes}
    passed: set[str] = set()
    failed: set[str] = set()
    for node in evidence.nodes:
        if not node.unique_id.startswith("test."):
            continue
        status = node.status.lower()
        if status in _PASSING_TEST:
            target = passed
        elif status in FAILED_STATUSES or status == "warn":
            target = failed
        else:
            continue
        for parent in graph.get(node.unique_id, []):
            if parent.startswith("model."):
                target.add(label_of.get(parent) or parent.split(".")[2])
    if verification and verification.get("ran"):
        for kind in _RECORD_KINDS:
            for record in verification.get(kind) or []:
                if not record.get("model"):
                    continue
                if record.get("status") == "satisfied":
                    passed.add(str(record["model"]))
                elif record.get("status") == "contradicted":
                    failed.add(str(record["model"]))
    return passed - failed, failed


def _verification(
    verification: Mapping[str, Any] | None, dropped: int, offered: bool
) -> VerificationSummary:
    if not verification:
        return VerificationSummary(ran=False, reason="not reported by this build")
    if not verification.get("ran"):
        return VerificationSummary(
            ran=False, reason=verification.get("reason"), offered=offered
        )
    findings = list(verification.get("findings") or [])
    severities = Counter(str(f.get("severity") or "unknown") for f in findings)
    return VerificationSummary(
        ran=True,
        finding_count=len(findings) + dropped,
        findings_dropped=dropped,
        by_severity=dict(sorted(severities.items())),
        top=[
            FindingRef(
                code=str(f.get("code") or ""),
                identifier=str(f.get("identifier") or ""),
                severity=str(f.get("severity") or ""),
            )
            for f in findings[:_NAMES]
        ],
        suppressed=sorted((verification.get("suppressed") or {}).keys()),
        offered=offered,
    )


def _obligations(verification: Mapping[str, Any] | None) -> Obligations:
    if not verification or not verification.get("ran"):
        return Obligations(checked=False)
    by_status: Counter[str] = Counter()
    contradicted: list[Obligation] = []
    unchecked: list[Obligation] = []
    for kind in _RECORD_KINDS:
        for record in verification.get(kind) or []:
            status = str(record.get("status") or "unknown")
            by_status[status] += 1
            entry = Obligation(
                model=str(record.get("model") or ""),
                kind=kind,
                status=status,
                finding=record.get("finding"),
                reason_code=record.get("reason_code"),
            )
            if status == "contradicted":
                contradicted.append(entry)
            elif status == "unchecked":
                unchecked.append(entry)
    contract = verification.get("column_contract") or {}
    for item in contract.get("unchecked") or []:
        by_status["unchecked"] += 1
        unchecked.append(
            Obligation(
                model=str(item.get("model") or ""),
                kind="column_contract",
                status="unchecked",
                reason_code=item.get("reason_code"),
            )
        )
    return Obligations(
        checked=True,
        by_status=dict(sorted(by_status.items())),
        contradicted=contradicted,
        unchecked=unchecked,
        no_declared_columns=_names(contract.get("no_declared_columns") or []),
    )


def summarize_completion(
    evidence: BuildEvidence,
    verification: Mapping[str, Any] | None,
    *,
    process: Mapping[str, Any] | None = None,
    change: ChangeRef | None = None,
    graph: Mapping[str, list[str]] | None = None,
    comparisons: list[ComparisonState] | None = None,
    findings_dropped: int = 0,
    verification_offered: bool = False,
    prerequisites: list[dict[str, Any]] | None = None,
    cold: Mapping[str, Any] | None = None,
    prose_population: Iterable[str] = (),
    identifier_conversions: Mapping[str, list[Mapping[str, Any]]] | None = None,
    budget: int = COMPLETION_BUDGET_BYTES,
) -> BuildCompletion:
    """Reduce a build's evidence to the bounded block that leads its payload.

    ``verification`` is the sweep's payload as ``data.verification`` carries it.
    ``graph`` maps each node's unique id to the unique ids it depends on, read
    from the manifest; without it a skip's cause and a test's model go
    unnamed, and nothing else changes. ``process`` is dbt's own figures
    (``success``, ``returncode``), which this reports and never reinterprets.

    The block is trimmed to ``budget`` bytes serialized, in a fixed order
    (messages, then lists, then names), and every cut is counted under
    ``elided``. ``prerequisites`` is :func:`~.prerequisites.diagnose`'s answer and
    ``cold`` is what ``--cold-deps`` reported, both summarized here with the rest
    under their own keys in ``data``.
    """

    graph = graph or {}
    deliverables, built_names, causes = _deliverables(evidence, graph)
    blockers = _blockers(evidence, causes, prerequisites or ())
    asserted, failed_checks = _checked(evidence, graph, verification)
    statuses = Counter(n.status.lower() for n in evidence.nodes)
    completion = BuildCompletion(
        outcome=evidence.outcome.value,
        process={
            **{k: v for k, v in (process or {}).items() if v is not None},
            "node_counts": dict(sorted(statuses.items())),
        },
        change=_bounded_change(
            change or ChangeRef(selector=evidence.selection.requested)
        ),
        deliverables=deliverables,
        blockers=blockers,
        assurance=Assurance(
            basis="required" if deliverables.coverage == "known" else "built_models",
            asserted=_names(n for n in built_names if n in asserted),
            failed_checks=_names(n for n in built_names if n in failed_checks),
            build_only=_names(
                n for n in built_names if n not in asserted and n not in failed_checks
            ),
        ),
        verification=_verification(
            verification, findings_dropped, verification_offered
        ),
        obligations=_obligations(verification),
        refactor_comparison=comparisons,
        cold=_cold_summary(cold),
    )
    completion.obligations.prose_population = _names(prose_population)
    completion.obligations.identifier_conversions = (
        _conversion_obligations(identifier_conversions, evidence) or None
    )
    if prerequisites:
        completion.details = ["data.prerequisites", *completion.details]
    if cold is not None:
        completion.details = ["data.cold_verification", *completion.details]
    if verification_offered:
        completion.details = [*completion.details, "data.offer"]
    _fit(completion, budget)
    return completion


def _conversion_obligations(
    recorded: Mapping[str, list[Mapping[str, Any]]] | None,
    evidence: BuildEvidence,
) -> list[Obligation]:
    """The plan's identifier conversions that are not settled, on the models
    the change requires (every recorded model where coverage is unknown)."""

    if not recorded:
        return []
    coverage = evidence.coverage
    required = set(coverage.required) if coverage is not None else None
    out = []
    for model, conversions in sorted(recorded.items()):
        if required and model not in required:
            continue
        for conversion in conversions:
            status = str(conversion.get("status") or "unknown")
            if status in ("lossless", "declared"):
                continue
            out.append(
                Obligation(
                    model=model,
                    kind="identifier_conversion",
                    status=status,
                    column=conversion.get("column"),
                    reason_code=conversion.get("reason_code"),
                )
            )
    return out


def _bounded_change(change: ChangeRef) -> ChangeRef:
    selector = change.selector
    if selector is not None and len(selector) > _SELECTOR:
        return change.model_copy(update={"selector": selector[:_SELECTOR] + "..."})
    return change


def _fit(completion: BuildCompletion, budget: int) -> None:
    """Trim the block to ``budget`` bytes, counting every cut.

    Fixed order, cheapest information first: dbt's messages (the full text is
    in ``data.evidence.nodes``), then the per-item lists beyond their first
    entry, then names in every list. Counts are never cut, so whatever is left
    still says how much there was.
    """

    elided: Counter[str] = Counter()

    def cap_list(owner: Any, field: str, keep: int, key: str) -> None:
        items = getattr(owner, field)
        if items is not None and len(items) > keep:
            elided[key] += len(items) - keep
            setattr(owner, field, items[:keep])

    cap_list(completion, "blockers", _NAMES, "blockers")
    cap_list(completion.deliverables, "blocked", _NAMES, "deliverables.blocked")
    cap_list(completion.obligations, "contradicted", _NAMES, "obligations.contradicted")
    cap_list(completion.obligations, "unchecked", _NAMES, "obligations.unchecked")
    cap_list(
        completion.obligations,
        "identifier_conversions",
        _NAMES,
        "obligations.identifier_conversions",
    )
    if completion.refactor_comparison is not None:
        cap_list(completion, "refactor_comparison", _NAMES, "refactor_comparison")
    completion.elided = dict(elided)
    if completion.size() <= budget:
        return

    for blocker in completion.blockers:
        if blocker.message:
            blocker.message = None
            elided["blockers.message"] += 1
    completion.elided = dict(elided)
    if completion.size() <= budget:
        return

    for keep in (3, 1, 0):
        cap_list(completion, "blockers", keep, "blockers")
        cap_list(
            completion.obligations, "contradicted", keep, "obligations.contradicted"
        )
        cap_list(completion.obligations, "unchecked", keep, "obligations.unchecked")
        cap_list(
            completion.obligations,
            "identifier_conversions",
            max(keep, 1),
            "obligations.identifier_conversions",
        )
        cap_list(completion.verification, "top", keep, "verification.top")
        # The first blocked deliverable is the last thing to go: it is the
        # answer to "is the change done", which is what the block is for.
        cap_list(
            completion.deliverables, "blocked", max(keep, 1), "deliverables.blocked"
        )
        if completion.refactor_comparison is not None:
            cap_list(completion, "refactor_comparison", keep, "refactor_comparison")
        if completion.cold is not None:
            # The count stays, and it is what says the installed tree lost edits.
            cap_list(completion.cold, "paths", keep, "cold.paths")
            cap_list(completion.cold, "not_compared", keep, "cold.not_compared")
        for owner, fields in (
            (
                completion.deliverables,
                ("built", "failed", "skipped", "missing", "stale"),
            ),
            (completion.assurance, ("asserted", "failed_checks", "build_only")),
            (completion.obligations, ("no_declared_columns", "prose_population")),
        ):
            for field in fields:
                listed: NameList = getattr(owner, field)
                floor = max(keep, 1) if field in _UNFINISHED else keep
                if len(listed.names) > floor:
                    listed.elided += len(listed.names) - floor
                    elided["names"] += len(listed.names) - floor
                    listed.names = listed.names[:floor]
        completion.elided = dict(elided)
        if completion.size() <= budget:
            return
