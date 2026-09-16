"""What the transform commands return.

The authoring commands all end the same way, in a stored plan of reviewable
diffs that nothing has applied yet, so :class:`PlanResult` serves most of them.
Applying and building are the two that touch something, and each says exactly
what it touched.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from ..results import Result


class InitResult(Result):
    """A scaffolded dbt project. Files are already written, unlike every other
    transform command: bootstrapping a project has nothing to plan against."""

    project_name: str = ""
    project_dir: str = ""
    connector: str = ""
    # Where the connector came from, because a build that targets the wrong
    # warehouse because a config was picked up implicitly is the failure this
    # command exists to prevent.
    connector_source: str = ""
    created: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_dir": self.project_dir,
            "connector": self.connector,
            "connector_source": self.connector_source,
            "created": self.created,
        }


class PlanResult(Result):
    """A stored plan of reviewable diffs. Nothing is applied by planning."""

    plan_id: str = ""
    intent: str = ""
    paths: list[str] = Field(default_factory=list)
    plan_path: str = ""
    # Semantic authoring classifies each name it touched, so a mixed payload
    # reports which definitions it created, which it evolved, and which it
    # merely re-stated. The third class is what keeps the first two readable:
    # a whole-file edit restates every definition in the file, and without it
    # a two-object change reports as a thirty-object one.
    defined: list[str] | None = None
    updated: list[str] | None = None
    unchanged: list[str] | None = None
    # A fourth class rather than an empty diff: a removal states no content, so
    # it is the one change a reviewer cannot read off the other three.
    removed: list[str] | None = None
    # Per edited model, which authored changes can move rows and what each one
    # moved. Absent (not empty) when the edit cannot change a row population at
    # all, which is the common case and deserves no key.
    row_attribution: list[dict[str, Any]] | None = None

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "intent": self.intent,
            "edit_count": len(self.paths),
            "paths": self.paths,
            "plan_path": self.plan_path,
        }
        if self.defined is not None:
            payload["defined"] = self.defined
        if self.updated is not None:
            payload["updated"] = self.updated
        if self.unchanged is not None:
            payload["unchanged"] = self.unchanged
        if self.removed is not None:
            payload["removed"] = self.removed
        if self.row_attribution is not None:
            payload["row_attribution"] = self.row_attribution
        return payload


class PropagationResult(PlanResult):
    """A rename or a removal, propagated across the project as one plan.

    A :class:`PlanResult` because propagating *is* planning edits, the way
    scaffolding a macro is. What it adds is the evidence that nothing was
    dropped: ``sites`` counts occurrences per reference form, in the same
    vocabulary ``transform references`` reports in, so the two can be compared
    directly. A caller who ran the report first can check the rename touched what
    the report said it would.
    """

    change: str = ""
    kind: str = ""
    sites: dict[str, int] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return {
            "change": self.change,
            "kind": self.kind,
            "sites": self.sites,
            "site_count": sum(self.sites.values()),
            **super().data(),
        }


class PlacementResult(PlanResult):
    """Where a derived column should be defined, and why that answer.

    ``reasoning`` comes before the plan in :meth:`data` on purpose. The proposal
    is the point of this command and it has to be arguable, so the case for it is
    what a reader meets first rather than something below a diff they have
    already started applying.

    ``always_reports_notes`` because an empty ``notes`` is a positive statement
    here: every model in the chain took the edit, and none was skipped for
    projecting a star or for already carrying the column.
    """

    column: str = ""
    strategy: str = ""
    ancestor: str | None = None
    inputs: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)
    chain: dict[str, list[str]] = Field(default_factory=dict)
    explained: bool = False

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "column": self.column,
            "strategy": self.strategy,
            "ancestor": self.ancestor,
            "inputs": self.inputs,
            "targets": self.targets,
            "reasoning": self.reasoning,
            "chain": self.chain,
        }
        # `--explain` stores nothing, so there are no plan fields to report and
        # an empty `plan_id` would read as a plan that failed to store.
        return payload if self.explained else {**payload, **super().data()}


class MacroListResult(Result):
    """The shipped macros available to scaffold."""

    macros: list[dict[str, str]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"macros": self.macros}


class MacroResult(PlanResult):
    """One shipped macro, either already current or planned into the project.

    A :class:`PlanResult` rather than a wrapper around one: scaffolding a macro
    *is* planning an edit, and the plan's diffs and warnings are this result's
    diffs and warnings. When the project's copy already matches, ``up_to_date``
    is set and the plan fields stay empty, because there was nothing to plan.
    """

    macro: str = ""
    path: str = ""
    up_to_date: bool = False

    def data(self) -> dict[str, Any]:
        payload = {"macro": self.macro, "path": self.path}
        if self.up_to_date:
            return {**payload, "up_to_date": True}
        return {**payload, **super().data()}


class TestScaffoldResult(PlanResult):
    """A ``unit_tests:`` skeleton, scaffolded from a model's own ref()/source()
    inputs and planned like any other schema.yml edit.

    A :class:`PlanResult` for the same reason :class:`MacroResult` is one:
    scaffolding a unit test *is* planning an edit. ``inputs`` are the bare
    names of every input a ``given`` block was built for, in the order the
    model reads them.
    """

    model: str = ""
    inputs: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"model": self.model, "inputs": self.inputs, **super().data()}


class ApplyResult(Result):
    """What applying a plan wrote, or the conflicts that stopped it.

    Conflicts with ``written`` empty is the propose-don't-impose refusal: files
    changed after the plan was made, human edits are authoritative, and nothing
    was overwritten. Re-running confirmed moves those paths into
    ``conflicts_overridden`` instead.
    """

    plan_id: str = ""
    written: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    conflicts_overridden: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        if self.pending_confirmation is not None:
            return {"plan_id": self.plan_id, "conflicts": self.conflicts}
        return {
            "plan_id": self.plan_id,
            "written": self.written,
            "conflicts_overridden": self.conflicts_overridden,
        }


class PlanExportResult(Result):
    """One stored plan as a portable document, checkable by whoever receives it.

    ``digest`` is lifted out of ``plan`` and reported beside it because it is the
    one field a caller has to carry somewhere else. The document travels through
    whatever channel is convenient; the digest has to travel through one the
    caller trusts, and separating them in the payload is a small nudge toward
    doing that rather than shipping the pair together and calling it verified.
    """

    plan_id: str = ""
    digest: str = ""
    plan: dict[str, Any] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "digest": self.digest, "plan": self.plan}


class ClassificationResult(Result):
    """What each edit's content actually contains.

    ``always_reports_notes`` because an empty ``notes`` is a positive statement
    here: nothing was skipped, so every edit in the payload was read.
    """

    plan_id: str | None = None
    classifications: list[dict[str, Any]] = Field(default_factory=list)

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"classifications": self.classifications}
        if self.plan_id is not None:
            payload = {"plan_id": self.plan_id, **payload}
        return payload


class GroundingResult(Result):
    """What a plan depends on, and how finished that answer is.

    ``always_reports_notes`` because an empty ``notes`` is a positive statement:
    nothing was capped and nothing was skipped, so the graph below is the whole
    graph dex found.
    """

    plan_id: str | None = None
    grounding: dict[str, Any] = Field(default_factory=dict)

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        payload = dict(self.grounding)
        if self.plan_id is not None:
            payload = {"plan_id": self.plan_id, **payload}
        return payload


class PreflightResult(Result):
    """What the warehouse will enforce on this project's next guarded build.

    ``always_reports_notes`` because the notes here are the substance rather than
    an aside: a connector with no provider-side control says so in one, and an
    empty list is the positive statement that nothing needed saying.
    """

    preflight: dict[str, Any] = Field(default_factory=dict)

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        return dict(self.preflight)


class PlanListResult(Result):
    """Stored plans, pending and applied, newest first."""

    plans: list[dict[str, Any]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"plans": self.plans, "count": len(self.plans)}


class DepsResult(Result):
    """A ``dbt deps`` run, or the reason there was nothing to install."""

    ran: bool = False
    reason: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        if not self.ran:
            return {"ran": False, "reason": self.reason}
        return {"ran": True, **self.summary}


class MutationCoverageResult(Result):
    """Which planted defects a model's tests caught, and which they missed.

    Survivors lead the list. A caller reading this is deciding what test to write
    next, and the mutants that were caught are the ones they need to read least,
    so ordering by outcome is the difference between an answer and a table.

    ``score`` is deliberately not the headline and is null rather than zero when
    nothing ran. It is a ratio of two small integers over one model, and a number
    that invites comparison between models would be read as a quality metric it
    cannot support; the survivors themselves are the finding.

    ``baseline`` is the honesty field. Every verdict here is relative to the
    tests that passed before anything was mutated, so the excluded ones have to
    be visible: a suite whose only real test was already failing would otherwise
    report a clean sweep of survivors and read as though it had been measured.
    """

    model: str = ""
    target: str = ""
    baseline: dict[str, Any] = Field(default_factory=dict)
    mutants: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    score: float | None = None
    cap: dict[str, Any] = Field(default_factory=dict)
    runs: int = 0

    def data(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "target": self.target,
            "baseline": self.baseline,
            "mutants": self.mutants,
            "counts": self.counts,
            "score": self.score,
            "cap": self.cap,
            "runs": self.runs,
        }


class BuildResult(Result):
    """A finished dbt run, dev-target only and cost-surfaced beforehand.

    ``summary`` is dbt's own per-node accounting, kept as the adapter shaped it
    because each connector reports different figures and flattening them would
    lose the ones that matter for that warehouse.

    ``success`` stays dbt's process outcome, which is the right meaning for the
    command line and a poor one for a host: an empty selection exits zero.
    ``evidence`` is the typed answer to the question ``success`` cannot be asked,
    and it sits beside the released fields rather than replacing them, so a
    consumer reading ``success`` or ``summary`` keeps reading what it read.

    ``outcome`` is lifted to the top of the payload from inside ``evidence``
    because it is the one field a reader must not have to go looking for.
    """

    success: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] | None = None
    # What `--verify` swept and found, or the statement that it did not run.
    # Absent only when the command predates the concept; a run that declined to
    # verify still says so, because an omitted key would read as "clean".
    verification: dict[str, Any] | None = None

    def data(self) -> dict[str, Any]:
        payload = dict(self.summary)
        if self.evidence is not None:
            payload = {
                "outcome": self.evidence.get("outcome"),
                **payload,
                "evidence": self.evidence,
            }
        if self.verification is not None:
            payload["verification"] = self.verification
        return payload
