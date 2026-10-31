"""What the transform commands return.

The authoring commands all end the same way, in a stored plan of reviewable
diffs that nothing has applied yet, so :class:`PlanResult` serves most of them.
Applying and building are the two that touch something, and each says exactly
what it touched.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from ..attribution import COMMIT_TRAILER
from ..results import Result
from .classify import ArtifactClassification
from .evidence import BuildEvidence
from .grounding import Grounding
from .portable import PortablePlan


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
    # The duckdb warehouse path as committed: relative when the warehouse is
    # inside the repo, absolute when it is not (issue #642).
    warehouse_path: str | None = None
    created: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_dir": self.project_dir,
            "connector": self.connector,
            "connector_source": self.connector_source,
            "warehouse_path": self.warehouse_path,
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
    # Every assumption declared on the models this plan touches, read back from
    # the post-change YAML (model, decision, chosen, evidence), so the agent can
    # relay the decisions it made without reconstructing them. Empty is an
    # answer (nothing declared); absent means no dbt project to read.
    decisions: list[dict[str, Any]] | None = None
    # Per model the plan creates, edits or declares: what dex drafted from the
    # cache and the SQL, what the post-change YAML declares, and which intent
    # fields nobody has supplied. A plan offers a draft and never writes it
    # (`transform declare` writes part of one, when asked). Absent when
    # the plan touches no model.
    declarations: dict[str, dict[str, Any]] | None = None
    # The semantic model derived from each declared mart the plan touches
    # (issue #499): added to the plan where the project has a dbt semantic
    # layer, offered here where it does not. Absent when no declared mart was
    # touched.
    semantic_proposal: dict[str, Any] | None = None
    # The free pointer to `transform compare` on a plan that rewrites a model
    # that already exists (#856). Planning scans nothing for it. Absent when the
    # plan rewrites none.
    refactor_comparison_offer: dict[str, Any] | None = None

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "intent": self.intent,
            "edit_count": len(self.paths),
            "paths": self.paths,
            "plan_path": self.plan_path,
        }
        if self.refactor_comparison_offer is not None:
            payload["refactor_comparison_offer"] = self.refactor_comparison_offer
        if self.decisions is not None:
            payload["decisions"] = self.decisions
        if self.declarations is not None:
            payload["declarations"] = self.declarations
        if self.semantic_proposal is not None:
            payload["semantic_proposal"] = self.semantic_proposal
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


class DeclareResult(PlanResult):
    """Declarations proposed for models that already exist (#501).

    Every selected model is accounted for: proposed (``proposed``, each with the
    measured facts written and their evidence), skipped (``skipped``, each with
    its reason), or past the cap (``elided_count``, also stated in ``notes``).
    With nothing to propose no plan is stored, and the plan fields are omitted
    rather than empty, so an empty ``plan_id`` never reads as a plan that failed
    to store.
    """

    selector: list[str] = Field(default_factory=list)
    selected_count: int = 0
    proposed: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    elided_count: int = 0

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "selector": self.selector,
            "selected_count": self.selected_count,
            "proposed": self.proposed,
            "skipped": self.skipped,
            "elided_count": self.elided_count,
        }
        if not self.plan_id:
            return {**payload, "plan_id": None}
        return {**payload, **super().data()}


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

    ``commit_trailer`` is present exactly when something was written, because
    only then is there a commit of dex's work for it to end.
    """

    plan_id: str = ""
    written: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    conflicts_overridden: list[str] = Field(default_factory=list)
    # The assumptions declared on the models this apply wrote, as the project
    # now records them. See :attr:`PlanResult.decisions`.
    decisions: list[dict[str, Any]] | None = None

    def data(self) -> dict[str, Any]:
        if self.pending_confirmation is not None:
            return {"plan_id": self.plan_id, "conflicts": self.conflicts}
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "written": self.written,
            "conflicts_overridden": self.conflicts_overridden,
        }
        if self.decisions is not None:
            payload["decisions"] = self.decisions
        if self.commit_trailer is not None:
            payload["commit_trailer"] = self.commit_trailer
        return payload

    @property
    def commit_trailer(self) -> str | None:
        """The co-author trailer for a commit of what this apply wrote."""

        if self.pending_confirmation is not None or not self.written:
            return None
        return COMMIT_TRAILER


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
    plan: PortablePlan = Field(
        default_factory=lambda: PortablePlan(
            plan_id="", intent="", created_at="", project_dir=""
        )
    )

    def plan_model(self) -> PortablePlan:
        """The typed :class:`~exmergo_dex_core.transform.portable.PortablePlan`
        model."""
        return self.plan

    def data(self) -> dict[str, Any]:
        plan_data = (
            self.plan.data()
            if hasattr(self.plan, "data")
            else (self.plan.model_dump(mode="json") if self.plan is not None else {})
        )
        return {"plan_id": self.plan_id, "digest": self.digest, "plan": plan_data}


class ClassificationResult(Result):
    """What each edit's content actually contains.

    ``always_reports_notes`` because an empty ``notes`` is a positive statement
    here: nothing was skipped, so every edit in the payload was read.
    """

    plan_id: str | None = None
    classifications: list[ArtifactClassification] = Field(default_factory=list)

    always_reports_notes: ClassVar[bool] = True

    def classification_models(self) -> list[ArtifactClassification]:
        """The typed
        :class:`~exmergo_dex_core.transform.classify.ArtifactClassification`
        models."""
        return self.classifications

    def data(self) -> dict[str, Any]:
        classifications_data = [
            c.data() if hasattr(c, "data") else c for c in self.classifications
        ]
        payload: dict[str, Any] = {"classifications": classifications_data}
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
    grounding: Grounding = Field(default_factory=Grounding)

    always_reports_notes: ClassVar[bool] = True

    def grounding_model(self) -> Grounding:
        """The typed :class:`~exmergo_dex_core.transform.grounding.Grounding` model."""
        return self.grounding

    def data(self) -> dict[str, Any]:
        grounding_data = (
            self.grounding.data()
            if hasattr(self.grounding, "data")
            else dict(self.grounding)
        )
        payload = dict(grounding_data)
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


class RefactorComparisonResult(Result):
    """Whether a stored plan keeps one model's results on the compared inputs.

    ``status`` is ``equivalent``, ``different`` or ``not_checked``, and
    ``reason_code`` says which of them and why. ``equivalent`` means the schema
    and the row multiset matched on ``input_scope`` at ``compared_at``. It is not
    a claim about a build, or about any input that was not compared. Every field
    here is a count, a name or a type: no row comes back.

    ``keyed`` is present only when a key was given or declared, and unique on both
    sides. ``key_source`` says which, because a key the request named and one a
    declaration stated are different evidence.
    """

    plan_id: str = ""
    model: str = ""
    target: str = ""
    status: str = "not_checked"
    reason_code: str = ""
    detail: str | None = None
    compared_at: str = ""
    baseline_compiled: str | None = None
    proposed_compiled: str | None = None
    input_scope: list[str] = Field(default_factory=list)
    schema_diff: dict[str, Any] | None = None
    multiset: dict[str, Any] | None = None
    key_columns: list[str] | None = None
    key_source: str | None = None
    keyed: dict[str, Any] | None = None
    keyed_reason: str | None = None
    profiled_on_demand: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "model": self.model,
            "target": self.target,
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "compared_at": self.compared_at,
            "baseline_compiled": self.baseline_compiled,
            "proposed_compiled": self.proposed_compiled,
            "input_scope": self.input_scope,
            "schema": self.schema_diff,
            "multiset": self.multiset,
            "key_columns": self.key_columns,
            "key_source": self.key_source,
            "keyed": self.keyed,
            "keyed_reason": self.keyed_reason,
            "profiled_on_demand": self.profiled_on_demand,
        }


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

    ``equivalence`` says whether the survivors were compared with the model on
    the dev data, how many differ and how many do not, and the self-check that
    makes the comparison worth trusting. It is present even when the check was
    off, carrying the reason, because an absent key would read as "nothing to
    compare". Equivalent survivors stay in ``score``: a fixture can still catch
    them, and leaving them out would flatter the suite.

    ``downstream`` says whether the tests on the models downstream of this one
    ran against each mutant too, which models carried it, which tests were left
    out and why, and the reason when the routing was off. A defect only those
    tests caught is ``caught_downstream``. ``score`` counts it as a miss, since
    it answers for the model's own suite and must mean what it meant before
    downstream tests ran; ``score_with_downstream`` counts it as caught, and is
    null when they did not run.
    """

    model: str = ""
    target: str = ""
    baseline: dict[str, Any] = Field(default_factory=dict)
    mutants: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    score: float | None = None
    score_with_downstream: float | None = None
    cap: dict[str, Any] = Field(default_factory=dict)
    runs: int = 0
    equivalence: dict[str, Any] | None = None
    downstream: dict[str, Any] | None = None

    def data(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "target": self.target,
            "baseline": self.baseline,
            "mutants": self.mutants,
            "counts": self.counts,
            "score": self.score,
            "score_with_downstream": self.score_with_downstream,
            "cap": self.cap,
            "runs": self.runs,
            "equivalence": self.equivalence,
            "downstream": self.downstream,
        }


class BuildResult(Result):
    """A finished dbt run, dev-target only and cost-surfaced beforehand.

    ``summary`` is dbt's own per-node accounting, kept as the adapter shaped it
    because each connector reports different figures and flattening them would
    lose the ones that matter for that warehouse.

    ``success`` stays dbt's process outcome, which is the right meaning for the
    command line and a poor one for a host: an empty selection exits zero.
    ``evidence`` is the typed
    :class:`~exmergo_dex_core.transform.evidence.BuildEvidence` answer to the
    question ``success`` cannot be asked, and it sits beside the released
    fields rather than replacing them, so a consumer reading ``success`` or
    ``summary`` keeps reading what it read.

    ``outcome`` is lifted to the top of the payload from inside ``evidence``
    because it is the one field a reader must not have to go looking for.
    """

    success: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence: BuildEvidence | None = None
    # What `--verify` swept and found, or the statement that it did not run.
    # Absent only when the command predates the concept; a run that declined to
    # verify still says so, because an omitted key would read as "clean".
    verification: dict[str, Any] | None = None
    # The assumptions declared on the models this build ran, as the project
    # records them. See :attr:`PlanResult.decisions`.
    decisions: list[dict[str, Any]] | None = None

    def evidence_model(self) -> BuildEvidence | None:
        """The typed :class:`~exmergo_dex_core.transform.evidence.BuildEvidence`
        model."""
        return self.evidence

    def data(self) -> dict[str, Any]:
        payload = dict(self.summary)
        if self.evidence is not None:
            evidence_data = (
                self.evidence.data()
                if hasattr(self.evidence, "data")
                else self.evidence
            )
            outcome = (
                evidence_data.get("outcome")
                if isinstance(evidence_data, dict)
                else getattr(self.evidence, "outcome", None)
            )
            payload = {
                "outcome": outcome,
                **payload,
                "evidence": evidence_data,
            }
        if self.verification is not None:
            payload["verification"] = self.verification
        if self.decisions is not None:
            payload["decisions"] = self.decisions
        return payload
