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
from itertools import zip_longest
from typing import Any, Literal

from pydantic import BaseModel, Field

from .evidence import (
    BUILT_STATUS,
    FAILED_STATUSES,
    SKIPPED_STATUSES,
    BuildEvidence,
    BuildNode,
    BuildOutcome,
    status_kind,
)

__all__ = [
    "BUILT_UNVERIFIED",
    "COMPLETION_BUDGET_BYTES",
    "BuildCompletion",
    "FailedCheck",
    "FailedChecks",
    "NameList",
    "Unverified",
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
    "population_delta",
    "retirement",
    "toggle",
    "model_versions",
)

#: The deliverable lists that say the change is not done. Each keeps at least
#: one name however tight the budget.
_UNFINISHED = frozenset({"failed", "skipped", "unrecognized", "missing", "stale"})

_PREDATES = (
    "not rebuilt by this run; any relation it has in the dev target predates "
    "this run and was not verified by it"
)


_UNRECOGNIZED_NOTE = (
    "reported a status dex does not recognize (a microbatch model's partial "
    "success, say); this run may have written part of its relation, and nothing "
    "established that it is complete"
)


class NameList(BaseModel):
    """A count, the first few names, and how many more were not named."""

    count: int = 0
    names: list[str] = Field(default_factory=list)
    elided: int = 0


class FailedCheck(BaseModel):
    """One check that failed on a built model, and which kind it was (#1162).

    ``kind`` is ``data_test`` for a dbt data test, named in ``name``, or the
    verification record list that judged it ``contradicted``
    (``join_coverage``, ``parent_population``, ...), with the ``finding`` it
    raised and, for a join, the ``join`` as the model writes it. A join
    measured against a default threshold is then not read as a failed test.
    """

    model: str
    kind: str
    name: str | None = None
    finding: str | None = None
    join: str | None = None


class FailedChecks(NameList):
    """The models a check failed on, and each failed check, kind first."""

    checks: list[FailedCheck] = Field(default_factory=list)


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
    #: Ran with a status dex does not recognize (``partial success``,
    #: ``no-op``), so it is not counted as built and is not a failure (#1105).
    #: Present only when there is one, so the bounded block is unchanged for
    #: every build dbt reports in its usual words.
    unrecognized: NameList | None = None
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
    #: Built, and a data test or a verification record on it failed; each
    #: failure is under ``checks`` with its kind (#1162).
    failed_checks: FailedChecks = Field(default_factory=FailedChecks)
    build_only: NameList = Field(default_factory=NameList)


class Blocker(BaseModel):
    node: str
    status: str
    #: ``project`` or ``package``, and how it relates to the named plan; the
    #: repair routes are under ``data.prerequisites`` (#860).
    owner: str | None = None
    package: str | None = None
    plan_relation: str | None = None
    previous_status: str | None = None
    message: str | None = None
    #: The one command that repairs it, kept ahead of the cut a truncating
    #: reader makes (#1229): `repair: dex transform repair <node>` for a
    #: package model the project can override, `repair: transform plan on
    #: <path>` for the project's own.
    next: str | None = None


class FindingRef(BaseModel):
    code: str
    identifier: str
    severity: str
    #: ``introduced``, ``unattributed`` or ``existing`` on a ``--for-plan``
    #: build (#1230); absent otherwise.
    attribution: str | None = None


class Unverified(BaseModel):
    """One reason a build that validated is still not a finished change (#1230).

    ``reason`` is ``comparison_not_run`` (a rewrite applied uncompared while
    its baseline was ready), ``undeclared_population_change`` (a population
    change no assumption records and apply did not accept),
    ``introduced_high_finding`` (a high-severity finding this plan introduced)
    or ``unattributed_high_finding`` (a high-severity finding on a model the
    plan edited that no baseline places before the plan).
    """

    model: str
    reason: str
    #: The change-scope kind, or the finding code, the reason is about.
    detail: str | None = None
    column: str | None = None


class VerificationSummary(BaseModel):
    """What the sweep did, in counts, with the full answer under
    ``data.verification``."""

    ran: bool = False
    reason: str | None = None
    #: On a ``--for-plan`` build (#1230): the findings this plan introduced,
    #: ahead of ``top``, and every finding counted by attribution. The baseline
    #: they were judged against is under ``data.verification.attribution``.
    introduced: list[FindingRef] | None = None
    by_attribution: dict[str, int] | None = None
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
    #: The column an identifier-conversion or change-scope obligation is about
    #: (#861, #1115).
    column: str | None = None
    #: What a change-scope obligation changed (`column_removed`,
    #: `aggregate_changed`, `predicate_added`, ...) (#1115).
    change: str | None = None
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
    #: Populations measured against a drafted parent whose counts ask which
    #: rows the model is for, with no assumption recording the answer (#1228):
    #: orphan keys, missing parents, or parents kept with no children.
    needs_decision: list[Obligation] | None = None
    no_declared_columns: NameList = Field(default_factory=NameList)
    #: Required models whose declared population is a sentence: nothing can run
    #: it, as distinct from a parent or filter a record above measured (#1029).
    prose_population: NameList = Field(default_factory=NameList)
    #: Identifier conversions the plan added that change which entities a
    #: model holds with nothing declared recording it (`undeclared`), or that
    #: dex could not measure (`unchecked`), as the plan recorded them (#861).
    identifier_conversions: list[Obligation] | None = None
    #: Changes the plan made beside its request with no assumption recording
    #: them (`undeclared`: a population change, a removed output, a changed
    #: cast, aggregate or NULL treatment), or an edit dex could not read
    #: (`unchecked`), as the plan recorded them (#1115).
    change_scope: list[Obligation] | None = None
    #: Each required incremental model the plan edited, and whether a unit
    #: test overriding ``is_incremental`` to true passed in this build
    #: (``satisfied``), failed (``contradicted``), or never ran (``unchecked``:
    #: ``full_refresh_only`` where the build only full-refreshed the model,
    #: ``no_incremental_test`` otherwise). A full refresh runs only the full
    #: program, so it never answers the next incremental run (#1130).
    incremental_replay: list[Obligation] | None = None
    #: Each required model the plan rewrote, held to its recorded
    #: ``transform compare`` verdict: ``equivalent`` or ``as_expected`` is
    #: ``satisfied``, ``different`` or ``unexpected`` is ``contradicted``, and
    #: no verdict (``not_compared``) or ``not_checked`` is ``unchecked``. Only a
    #: stored plan records verdicts, so a ``--for-plan-file`` build carries no
    #: such key (#1157).
    comparison: list[Obligation] | None = None
    #: A package override `transform repair` drafted, held to what a repair has
    #: to show (#1229): `repair_built`, `repair_tests` (the package's own tests,
    #: run on the override), `repair_cold_deps` (only a `--cold-deps` build that
    #: lost no installed edit satisfies it), `repair_row_count` (no row
    #: finding on the override, whose draft moves no rows), and
    #: `repair_conversion` while a conversion stayed unresolved.
    repair: list[Obligation] | None = None


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
    #: The latest build among the compared model's upstream models (#1114): its
    #: invocation, when it ran, and whether it was a `--cold-deps` build. One
    #: run; other inputs may come from earlier builds.
    inputs_built_by: dict[str, Any] | None = None
    #: Whether the plan's comparison offer found the baseline ready to compare
    #: (#1114); recorded at plan time, ``None`` on a plan stored before it.
    baseline_ready: bool | None = None


class BuildCompletion(BaseModel):
    """The bounded lead of a build payload. See the module docstring."""

    outcome: str
    #: Why a build whose evidence reads ``validated`` is ``built_unverified``
    #: (#1230): each unfinished part of the change, by model. Absent when none.
    unverified: list[Unverified] | None = None
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
    """``built``, ``failed``, ``skipped`` or ``unrecognized`` for one node.

    Built is the one status the coverage partition counts, so a deliverable
    and ``coverage.built`` can never disagree (#1105). A passing test is
    ``succeeded`` in the evidence, and never stands for a deliverable here.
    """

    kind = status_kind(status)
    if kind == "succeeded":
        return "built" if status.lower() == BUILT_STATUS else "succeeded"
    return kind


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
    unrecognized: list[str] = []
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
        elif kinds - {"built"}:
            # Every match has to have built: a versioned requirement with one
            # version on an unrecognized status did not build as a whole.
            unrecognized.append(name)
            odd = next(n for n in matches if _status_kind(n.status) != "built")
            blocked.append(Blocked(deliverable=name, status=odd.status))
        else:
            built.append(name)
    notes = [_PREDATES] if failed or skipped or missing else []
    if unrecognized:
        notes.append(_UNRECOGNIZED_NOTE)
    if notes:
        note = "; ".join(notes)
    return (
        Deliverables(
            coverage="known",
            required=len(coverage.required),
            built=_names(built),
            failed=_names(failed),
            skipped=_names(skipped),
            unrecognized=_names(unrecognized) if unrecognized else None,
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
            previous_status=(
                diagnosed.get(n.unique_id, {}).get("previous_run", {}).get("status")
            ),
            message=(n.message[:_MESSAGE] if n.message else None),
            next=diagnosed.get(n.unique_id, {}).get("next"),
        )
        for n in failed
    ]


def _checked(
    evidence: BuildEvidence,
    graph: Mapping[str, list[str]],
    verification: Mapping[str, Any] | None,
) -> tuple[set[str], set[str], dict[str, list[FailedCheck]]]:
    """Models whose built rows something in this run checked: those every
    check passed on, those a check failed on, and each model's failed checks
    with their kind.

    Data tests and verification records read the built relation. A unit test
    reads fixtures, so it vouches for the SQL and not for the rows, and is not
    counted here.
    """

    label_of = {n.unique_id: n.name for n in evidence.nodes}
    passed: set[str] = set()
    failed: set[str] = set()
    failures: dict[str, list[FailedCheck]] = {}
    for node in evidence.nodes:
        if not node.unique_id.startswith("test."):
            continue
        status = node.status.lower()
        failing = status in FAILED_STATUSES or status == "warn"
        if not failing and status not in _PASSING_TEST:
            continue
        for parent in graph.get(node.unique_id, []):
            if not parent.startswith("model."):
                continue
            model = label_of.get(parent) or parent.split(".")[2]
            if not failing:
                passed.add(model)
                continue
            failed.add(model)
            failures.setdefault(model, []).append(
                FailedCheck(model=model, kind="data_test", name=node.name)
            )
    if verification and verification.get("ran"):
        for kind in _RECORD_KINDS:
            for record in verification.get(kind) or []:
                if not record.get("model"):
                    continue
                model = str(record["model"])
                if record.get("status") == "satisfied":
                    passed.add(model)
                elif record.get("status") == "contradicted":
                    failed.add(model)
                    join = record.get("join")
                    failures.setdefault(model, []).append(
                        FailedCheck(
                            model=model,
                            kind=kind,
                            finding=record.get("finding"),
                            join=_bounded(join) if isinstance(join, str) else None,
                        )
                    )
    return passed - failed, failed, failures


def _bounded(text: str, limit: int = _MESSAGE) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


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
    attribution = verification.get("attribution")

    def ref(f: Mapping[str, Any]) -> FindingRef:
        return FindingRef(
            code=str(f.get("code") or ""),
            identifier=str(f.get("identifier") or ""),
            severity=str(f.get("severity") or ""),
            attribution=f.get("attribution"),
        )

    introduced = None
    by_attribution = None
    if isinstance(attribution, Mapping):
        # Every introduced finding, the ones past the findings cap too: the
        # sweep lists them ahead of the rest, so the cap cuts existing first.
        introduced = [
            FindingRef(
                code=str(f.get("code") or ""),
                identifier=str(f.get("identifier") or ""),
                severity=str(f.get("severity") or ""),
            )
            for f in verification.get("introduced") or []
        ][:_NAMES]
        # Counted before the findings cap, so a cut finding is still counted.
        by_attribution = dict(sorted((attribution.get("counts") or {}).items()))
    return VerificationSummary(
        ran=True,
        introduced=introduced,
        by_attribution=by_attribution,
        finding_count=len(findings) + dropped,
        findings_dropped=dropped,
        by_severity=dict(sorted(severities.items())),
        top=[ref(f) for f in findings[:_NAMES]],
        suppressed=sorted((verification.get("suppressed") or {}).keys()),
        offered=offered,
    )


def _obligations(verification: Mapping[str, Any] | None) -> Obligations:
    if not verification or not verification.get("ran"):
        return Obligations(checked=False)
    by_status: Counter[str] = Counter()
    contradicted: list[Obligation] = []
    unchecked: list[Obligation] = []
    needs_decision: list[Obligation] = []
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
            elif status == "needs_decision":
                needs_decision.append(entry)
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
        needs_decision=needs_decision or None,
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
    change_scope: Mapping[str, list[Mapping[str, Any]]] | None = None,
    incremental_replay: Mapping[str, Mapping[str, Any]] | None = None,
    repair: Mapping[str, Any] | None = None,
    population_accepted: Mapping[str, Any] | None = None,
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
    under their own keys in ``data``. ``incremental_replay`` maps each
    incremental model the plan edited to ``tests`` (the unique ids of its unit
    tests that override ``is_incremental`` to true) and ``full_refreshed``
    (whether this build full-refreshed it) (#1130).

    ``population_accepted`` is what ``transform apply
    --accept-population-change`` recorded on the plan: the reason and the
    population changes it accepted (#1230). ``outcome`` is
    ``built_unverified`` where the evidence reads ``validated`` and any reason
    under ``unverified`` stands; the evidence's own outcome is unchanged.
    """

    from .scope_gate import accepted_key

    graph = graph or {}
    deliverables, built_names, causes = _deliverables(evidence, graph)
    blockers = _blockers(evidence, causes, prerequisites or ())
    asserted, failed_checks, failures = _checked(evidence, graph, verification)
    failed_names = list(dict.fromkeys(n for n in built_names if n in failed_checks))
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
            failed_checks=FailedChecks(
                **_names(failed_names).model_dump(),
                checks=_one_per_model_first(failed_names, failures),
            ),
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
    accepted = {
        accepted_key(entry)
        for entry in (population_accepted or {}).get("changes") or []
        if isinstance(entry, Mapping)
    }
    completion.obligations.change_scope = (
        _scope_obligations(change_scope, evidence, accepted) or None
    )
    completion.obligations.incremental_replay = (
        _replay_obligations(incremental_replay, evidence) or None
    )
    completion.obligations.comparison = (
        _comparison_obligations(comparisons, evidence) or None
    )
    completion.obligations.repair = (
        _repair_obligations(repair, evidence, graph, verification, cold) or None
    )
    completion.unverified = (
        _unverified(completion, comparisons, verification, evidence) or None
    )
    if completion.unverified and completion.outcome == BuildOutcome.VALIDATED.value:
        completion.outcome = BUILT_UNVERIFIED
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


def _scope_obligations(
    recorded: Mapping[str, list[Mapping[str, Any]]] | None,
    evidence: BuildEvidence,
    accepted: Iterable[tuple[str, str, str]] = (),
) -> list[Obligation]:
    """The plan's change-scope entries no assumption settles, on the models the
    change requires (every recorded model where coverage is unknown).

    A population change ``transform apply --accept-population-change`` accepted
    is ``accepted`` rather than ``undeclared`` (#1230): settled by a recorded
    decision, and still listed so the decision stays in view.
    """

    from .scope_gate import accepted_key, is_population_change

    if not recorded:
        return []
    coverage = evidence.coverage
    required = set(coverage.required) if coverage is not None else None
    taken = set(accepted)
    out = []
    for model, changes in sorted(recorded.items()):
        if required and model not in required:
            continue
        for change in changes:
            status = str(change.get("status") or "unknown")
            if status == "declared":
                continue
            if (
                status == "undeclared"
                and is_population_change(change)
                and accepted_key({**change, "model": model}) in taken
            ):
                status = "accepted"
            out.append(
                Obligation(
                    model=model,
                    kind="change_scope",
                    status=status,
                    column=change.get("column"),
                    change=change.get("kind"),
                )
            )
    return out


#: A unit test's status in run results that means it ran and passed.
_PASSED = frozenset({"pass", "success"})
#: Statuses that mean it ran and did not pass: the build evidence's own set.
_FAILED = FAILED_STATUSES


def _required(evidence: BuildEvidence) -> set[str] | None:
    """The models the change requires, or None where coverage is unknown."""

    coverage = evidence.coverage
    return set(coverage.required) if coverage is not None else None


def _replay_obligations(
    recorded: Mapping[str, Mapping[str, Any]] | None,
    evidence: BuildEvidence,
) -> list[Obligation]:
    """Whether this build ran each edited incremental model's next run (#1130).

    The only evidence that the ``is_incremental()`` branch works is a unit test
    that overrides it to true. A failing one is ``contradicted``, even beside a
    passing one, and otherwise a passing one is ``satisfied``. With none in the
    build the obligation is ``unchecked``, and the reason says whether a full
    refresh is what stood in for it.
    """

    if not recorded:
        return []
    required = _required(evidence)
    statuses = {node.unique_id: node.status.lower() for node in evidence.nodes}
    out = []
    for model, facts in sorted(recorded.items()):
        answers = set(facts.get("answers") or ()) | {model}
        if required and not answers & required:
            continue
        ran = [statuses[uid] for uid in facts.get("tests") or () if uid in statuses]
        # A failing test wins over a passing one: the branch is wrong for the
        # case that test pins, whatever another test shows.
        if any(status in _FAILED for status in ran):
            out.append(
                Obligation(
                    model=model,
                    kind="incremental_replay",
                    status="contradicted",
                    finding="incremental_test_failed",
                )
            )
        elif any(status in _PASSED for status in ran):
            out.append(
                Obligation(model=model, kind="incremental_replay", status="satisfied")
            )
        else:
            out.append(
                Obligation(
                    model=model,
                    kind="incremental_replay",
                    status="unchecked",
                    reason_code=(
                        "full_refresh_only"
                        if facts.get("full_refreshed")
                        else "no_incremental_test"
                    ),
                )
            )
    return out


#: A recorded comparison verdict, as the obligation it settles (#1157).
_COMPARISON_STATUS = {
    "equivalent": ("satisfied", None),
    "as_expected": ("satisfied", None),
    "different": ("contradicted", "different"),
    "unexpected": ("contradicted", "unexpected_change"),
}


def _comparison_obligations(
    comparisons: list[ComparisonState] | None,
    evidence: BuildEvidence,
) -> list[Obligation]:
    """Each required rewritten model, held to its recorded comparison (#1157).

    A rewrite that was never compared is ``unchecked`` (``not_compared``),
    whether or not it was meant to change rows: an intended change is compared
    with ``--expect``, and ``different`` is a defect until an expectation says
    which difference was meant.
    """

    if not comparisons:
        return []
    required = _required(evidence)
    # A comparison of another rewrite in the plan (a mart downstream of a
    # rewritten intermediate) may be what checks the results the request
    # keeps; the uncompared model stays open, but says so (#1049).
    elsewhere = any(state.status != "not_compared" for state in comparisons)
    out = []
    for state in comparisons:
        if required and state.model not in required:
            continue
        status, finding = _COMPARISON_STATUS.get(state.status, ("unchecked", None))
        out.append(
            Obligation(
                model=state.model,
                kind="comparison",
                status=status,
                finding=finding,
                reason_code=(
                    None
                    if status != "unchecked"
                    else (
                        ("compared_elsewhere" if elsewhere else "not_compared")
                        if state.status == "not_compared"
                        else state.reason_code or state.status
                    )
                ),
            )
        )
    return out


#: The outcome of a build that validated with part of the change unfinished
#: (#1230). Between ``validated`` and a failure: dbt built every deliverable,
#: and something the change owed is still open. ``failed`` stays reserved for
#: a build that failed.
BUILT_UNVERIFIED = "built_unverified"


def _unverified(
    completion: BuildCompletion,
    comparisons: list[ComparisonState] | None,
    verification: Mapping[str, Any] | None,
    evidence: BuildEvidence,
) -> list[Unverified]:
    """What keeps a validated build of a named change from being finished.

    Read from the obligations already summarized and the sweep's attribution,
    so it can never disagree with them. Only a build for a named plan has a
    change to be unfinished.
    """

    from .scope_gate import is_population_change

    if completion.deliverables.coverage != "known":
        return []
    ready = {state.model: state.baseline_ready for state in comparisons or []}
    # `compared_elsewhere` is a comparison of another rewrite in the plan,
    # which may be what checks the results this one keeps (#1049), so only a
    # plain `not_compared` counts, and only where the baseline was ready.
    out = [
        Unverified(model=item.model, reason="comparison_not_run")
        for item in completion.obligations.comparison or []
        if item.reason_code == "not_compared" and ready.get(item.model) is True
    ]
    out.extend(
        Unverified(
            model=item.model,
            reason="undeclared_population_change",
            detail=item.change,
            column=item.column,
        )
        for item in completion.obligations.change_scope or []
        if item.status == "undeclared" and is_population_change({"kind": item.change})
    )
    if not verification or not verification.get("ran"):
        return out
    out.extend(
        Unverified(
            model=str(ref.get("identifier") or ""),
            reason="introduced_high_finding",
            detail=ref.get("code"),
            column=ref.get("column"),
        )
        for ref in verification.get("introduced") or []
        if ref.get("severity") == "high"
    )
    # Without a baseline, a high finding on a model the plan edited cannot be
    # shown to predate it; the plan's own models are where that matters.
    required = _required(evidence) or set()
    out.extend(
        Unverified(
            model=str(finding["identifier"]),
            reason="unattributed_high_finding",
            detail=finding.get("code"),
            column=finding.get("column"),
        )
        for finding in verification.get("findings") or []
        if finding.get("attribution") == "unattributed"
        and finding.get("severity") == "high"
        and finding.get("identifier") in required
    )
    # One entry per model and reason: the findings themselves are listed
    # under `verification`, and the block's bytes are better spent there.
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in out:
        if (item.model, item.reason) not in seen:
            seen.add((item.model, item.reason))
            unique.append(item)
    return unique


_ROW_FINDINGS = frozenset({"row_loss", "row_fanout"})


def _repair_obligations(
    repair: Mapping[str, Any] | None,
    evidence: BuildEvidence,
    graph: Mapping[str, list[str]] | None,
    verification: Mapping[str, Any] | None,
    cold: Mapping[str, Any] | None,
) -> list[Obligation]:
    """What the build shows about a drafted package override (#1229).

    The override is the project's model of the package model's name; the
    package's tests attach to it, since dbt gives it the package's YAML entry.
    """

    if not repair:
        return []
    # One plan can repair several package models; each is held on its own.
    # A model whose every check holds is one `repair: satisfied` entry, and an
    # open one lists only what is open, so the bounded block keeps every model.
    if repair.get("nodes"):
        out: list[Obligation] = []
        for record in repair["nodes"]:
            if not isinstance(record, Mapping):
                continue
            held = _repair_obligations(record, evidence, graph, verification, cold)
            if held and all(o.status == "satisfied" for o in held):
                out.append(
                    Obligation(model=held[0].model, kind="repair", status="satisfied")
                )
            else:
                out.extend(o for o in held if o.status != "satisfied")
        return out
    if not repair.get("node"):
        return []
    name = str(repair["node"])
    package = str(repair.get("package") or "")
    node = next(
        (
            n
            for n in evidence.nodes
            if n.unique_id.startswith("model.")
            and n.name == name
            and n.unique_id.split(".")[1] != package
        ),
        None,
    )

    def obligation(kind: str, status: str, **extra: Any) -> Obligation:
        """One repair obligation on this override."""

        return Obligation(model=name, kind=kind, status=status, **extra)

    out: list[Obligation] = []
    status = node.status.lower() if node is not None else None
    if status is None:
        out.append(obligation("repair_built", "unchecked", reason_code="not_built"))
    elif status in _PASSED:
        out.append(obligation("repair_built", "satisfied"))
    elif status in _FAILED:
        out.append(
            obligation("repair_built", "contradicted", finding="override_failed")
        )
    else:
        out.append(obligation("repair_built", "unchecked", reason_code=status))

    statuses = {n.unique_id: n.status.lower() for n in evidence.nodes}
    tests = [
        uid
        for uid, parents in (graph or {}).items()
        if uid.startswith(("test.", "unit_test."))
        and node is not None
        and node.unique_id in parents
        and uid in statuses
    ]
    ran = [statuses[uid] for uid in tests]
    if any(s in _FAILED for s in ran):
        out.append(
            obligation("repair_tests", "contradicted", finding="package_test_failed")
        )
    elif any(s in _PASSED for s in ran):
        out.append(obligation("repair_tests", "satisfied"))
    else:
        out.append(obligation("repair_tests", "unchecked", reason_code="no_tests"))

    if cold is None:
        out.append(obligation("repair_cold_deps", "unchecked", reason_code="not_cold"))
    elif not cold.get("reinstalled"):
        out.append(
            obligation("repair_cold_deps", "unchecked", reason_code="not_reinstalled")
        )
    else:
        edits = cold.get("installed_edits") or {}
        lost = sum(
            int((edits.get(part) or {}).get("count") or 0)
            for part in ("changed", "added", "removed")
        )
        out.append(
            obligation("repair_cold_deps", "contradicted", finding="installed_edits")
            if lost
            else obligation("repair_cold_deps", "satisfied")
        )

    findings = (verification or {}).get("findings") or []
    row = next(
        (
            f
            for f in findings
            if isinstance(f, Mapping)
            and f.get("code") in _ROW_FINDINGS
            and f.get("identifier") == name
        ),
        None,
    )
    if row is not None:
        out.append(
            obligation("repair_row_count", "contradicted", finding=str(row["code"]))
        )
    elif not (verification or {}).get("ran"):
        out.append(
            obligation(
                "repair_row_count", "unchecked", reason_code="verification_not_run"
            )
        )
    elif status not in _PASSED:
        out.append(obligation("repair_row_count", "unchecked", reason_code="not_built"))
    elif repair.get("population") != "kept":
        out.append(
            obligation(
                "repair_row_count", "unchecked", reason_code="population_unchecked"
            )
        )
    else:
        out.append(obligation("repair_row_count", "satisfied"))

    # An `unbound` draft is checked by the build itself: once the override
    # builds, its types bound; until then the conversion stays open.
    if repair.get("status") == "unresolved" or (
        repair.get("status") == "unbound" and status not in _PASSED
    ):
        out.append(
            obligation(
                "repair_conversion", "unchecked", reason_code=str(repair["status"])
            )
        )
    return out


def _one_per_model_first(
    names: list[str], failures: Mapping[str, list[FailedCheck]]
) -> list[FailedCheck]:
    """Each model's failed checks, taken breadth first in ``names`` order.

    The list is capped, and every cut keeps a prefix. Taking each model's first
    check before anyone's second means a prefix that covers the first ``n``
    names gives each of them a kind, instead of one model with many failures
    filling the list.
    """

    rounds = zip_longest(*(failures.get(n, []) for n in names))
    return [check for round_ in rounds for check in round_ if check is not None]


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
    if completion.unverified is not None:
        cap_list(completion, "unverified", _NAMES, "unverified")
    cap_list(completion.deliverables, "blocked", _NAMES, "deliverables.blocked")
    cap_list(completion.obligations, "contradicted", _NAMES, "obligations.contradicted")
    cap_list(completion.obligations, "unchecked", _NAMES, "obligations.unchecked")
    cap_list(
        completion.obligations, "needs_decision", _NAMES, "obligations.needs_decision"
    )
    cap_list(
        completion.obligations,
        "identifier_conversions",
        _NAMES,
        "obligations.identifier_conversions",
    )
    for listed in ("change_scope", "incremental_replay", "comparison", "repair"):
        cap_list(completion.obligations, listed, _NAMES, f"obligations.{listed}")
    if completion.refactor_comparison is not None:
        cap_list(completion, "refactor_comparison", _NAMES, "refactor_comparison")
    cap_list(
        completion.assurance.failed_checks,
        "checks",
        _NAMES,
        "assurance.failed_checks.checks",
    )
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
            "needs_decision",
            max(keep, 1),
            "obligations.needs_decision",
        )
        cap_list(
            completion.obligations,
            "identifier_conversions",
            max(keep, 1),
            "obligations.identifier_conversions",
        )
        for listed in ("change_scope", "incremental_replay", "comparison", "repair"):
            cap_list(
                completion.obligations, listed, max(keep, 1), f"obligations.{listed}"
            )
        cap_list(completion.verification, "top", keep, "verification.top")
        # What the plan introduced and why the change is unfinished are the
        # answer the block is for, so each keeps one entry to the end (#1230).
        if completion.verification.introduced is not None:
            cap_list(
                completion.verification,
                "introduced",
                max(keep, 1),
                "verification.introduced",
            )
        if completion.unverified is not None:
            cap_list(completion, "unverified", max(keep, 1), "unverified")
        cap_list(
            completion.assurance.failed_checks,
            "checks",
            keep,
            "assurance.failed_checks.checks",
        )
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
                ("built", "failed", "skipped", "unrecognized", "missing", "stale"),
            ),
            (completion.assurance, ("asserted", "failed_checks", "build_only")),
            (completion.obligations, ("no_declared_columns", "prose_population")),
        ):
            for field in fields:
                listed: NameList | None = getattr(owner, field)
                if listed is None:
                    continue
                floor = max(keep, 1) if field in _UNFINISHED else keep
                if len(listed.names) > floor:
                    listed.elided += len(listed.names) - floor
                    elided["names"] += len(listed.names) - floor
                    listed.names = listed.names[:floor]
        completion.elided = dict(elided)
        if completion.size() <= budget:
            return

    # The last resort: the per-model obligation lists keep one entry each above,
    # and with long model names those can still overflow. Each goes whole, its
    # count kept under `elided`, since a block over budget is cut by the reader
    # where nothing counts it.
    for listed in (
        "needs_decision",
        "identifier_conversions",
        "change_scope",
        "incremental_replay",
        "comparison",
        "repair",
    ):
        cap_list(completion.obligations, listed, 0, f"obligations.{listed}")
        completion.elided = dict(elided)
        if completion.size() <= budget:
            return
    # Then the introduced findings and the unfinished reasons, last of all:
    # `outcome` still says `built_unverified`, and `elided` counts what went.
    if completion.verification.introduced is not None:
        cap_list(completion.verification, "introduced", 0, "verification.introduced")
    if completion.unverified is not None:
        cap_list(completion, "unverified", 0, "unverified")
    completion.elided = dict(elided)
