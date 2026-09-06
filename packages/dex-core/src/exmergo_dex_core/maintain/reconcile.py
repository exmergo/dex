"""Reconcile: from detected drift to proposed dbt edits.

Reconcile reads what detection recorded in `.dex/drift.json` and maps each
finding to the most honest response its axis allows. The action space differs
sharply by axis, and every proposal says which kind it is:

- ``mechanical``: schema drift on a dex-scaffolded staging model maps to a
  re-scaffold of the model pair from the drift-patched profile. High
  confidence, automatable, still a reviewable diff.
- ``advisory``: grain and semantic drift have no clean automatic fix (the
  warehouse is read-only and dex cannot know whether a new categorical value
  belongs in a metric), so the proposal is a decision surfaced, at most backed
  by a test edit that makes the break visible in builds.

Edits go through the same plan store ``transform apply`` writes from
(content-addressed plan id, hash-pinned edits), so reconcile itself never
writes to the project and human edits stay authoritative at apply time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..adapters.project import PlacingProject
from ..cache import ColumnProfile, Dataset, DexCache
from ..config import PIIOverrideMatcher
from ..dbt_project import DbtProjectView, ProjectDefinitions
from ..explore.profile import detect_pii
from ..transform.plans import EditKind, PlanEdit
from ..transform.rewrite import column_tests
from ..transform.scaffold import model_edits
from .declare import DeclarationEdits, Declined, Placed
from .drift import DriftFinding
from .snapshot import Snapshot


class Proposal(BaseModel):
    """One reconcile proposal: what to do about one piece of drift.

    ``kind`` sets the expectation: a mechanical proposal is backed by edits in
    the plan; an advisory one is a surfaced decision, backed by an edit only
    when a test can make the break visible.
    """

    axis: str
    kind: str
    finding_code: str
    identifier: str | None = None
    column: str | None = None
    action: str
    paths: list[str] = Field(default_factory=list)


# Schema findings `_patched_dataset` knows how to apply to a profile.
_PATCHABLE = {"column_added", "column_dropped", "column_retyped", "nullability_changed"}

#: The subset that has a written form. A retype has none: `data_type` carries the
#: connector's own spelling rather than a canonical one (Snowflake renders
#: NUMBER(38,0) and NUMBER(10,2) both as FIXED, BigQuery renders a repeated record
#: as ARRAY<STRUCT>), so a type dex wrote into a declaration would not be the
#: warehouse's. It is surfaced as a decision instead.
_MECHANICAL = {"column_added", "column_dropped", "nullability_changed"}


def _placed(placement: PlacingProject | None, kind: EditKind, table: str) -> str | None:
    """Where an edit of ``kind`` for ``table`` goes, asking the format first.

    ``None`` placement is the dbt scaffold convention, which keeps every caller
    that predates the seam on the paths it already had. A format that answers
    ``None`` for a kind is declining that kind, and the caller turns it into the
    same advisory a missing scaffold file produces.
    """

    if placement is None:
        # The same two kinds `DbtProject.edit_path` resolves, declined the same
        # way for the rest: this branch is that method's convention inlined for
        # callers holding no format, so answering a path where it answers `None`
        # would make the shim and the format disagree about the same input.
        suffix = {EditKind.MODEL_SQL: "sql", EditKind.SCHEMA_YML: "yml"}.get(kind)
        return None if suffix is None else f"models/staging/stg_{table}.{suffix}"
    return placement.edit_path(kind, table)


def build(
    findings: list[DriftFinding],
    snap: Snapshot,
    cache: DexCache | None,
    view: DbtProjectView | None,
    *,
    pii_overrides: PIIOverrideMatcher | None = None,
    placement: PlacingProject | None = None,
    definitions: ProjectDefinitions | None = None,
) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
    """Map findings to proposals and plan edits. Pure: writes nothing.

    ``view`` is ``None`` when the project format declines the write tier, or when
    it declares the tier but is not one dex can author edits for. Every proposal
    is advisory in that case and no edit is produced, which is the same outcome
    the scaffold-convention checks below already reach for a project that has no
    dex-scaffolded models, arrived at by declaration instead of by coincidence.

    ``pii_overrides`` carries the config's reviewed non-PII column paths, so a
    drift-added column a human already cleared is not re-flagged into the
    scaffolded meta.

    ``placement`` is the format answering where each edit lands. ``None`` keeps
    the dbt scaffold convention this module hard-coded before the seam existed,
    so a caller that does not pass one is unaffected.

    ``definitions`` is the format answering what the project *declares*, which is
    the question a file's bytes cannot answer. A model may declare a composite
    grain, and a column-level ``unique`` on one of its members then asserts
    something the project explicitly does not claim: dbt runs both tests and the
    new one fails every build forever, while a format that resolves the two the
    way dbt's semantics imply discards it. Neither is an edit worth proposing, so
    without this the module could only propose it and hope."""

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    schema_patches: dict[str, list[DriftFinding]] = {}
    definition_churn = False
    orphan_identifiers: list[str] = []
    for finding in findings:
        if finding.axis == "schema" and finding.code in _PATCHABLE:
            schema_patches.setdefault(finding.identifier, []).append(finding)
        elif finding.code.startswith("definition_") or finding.code.startswith(
            "model_"
        ):
            # A model added, removed, or content-changed is the project's own
            # edit, the same as a semantic definition changing: there is no
            # warehouse-side fix to propose, only a baseline to catch up.
            definition_churn = True
        else:
            if finding.code == "orphan_relation":
                orphan_identifiers.append(finding.identifier)
            proposals.append(_advisory(finding))

    declarations = DeclarationEdits(view, placement)
    for identifier, table_findings in sorted(schema_patches.items()):
        table = identifier.rsplit(".", 1)[-1]
        # A retype is always its own decision, and never a reason on its own to
        # rebuild anything: nothing dex writes carries a type, so a plan built for
        # one would propose the file it already has.
        proposals.extend(
            _retype_advisory(finding)
            for finding in table_findings
            if finding.code not in _MECHANICAL
        )
        written = [f for f in table_findings if f.code in _MECHANICAL]
        if not written:
            continue
        base = _base_dataset(identifier, cache, snap)
        model_path = _placed(placement, EditKind.MODEL_SQL, table)
        if view is not None and model_path is None:
            # The format authors no staging model, and this drift was never about
            # one. The declaration it does place is a file the drift describes, and
            # patching it needs no profile to rebuild from: the finding carries
            # every fact a column entry states.
            placed = declarations.resolve(table)
            if isinstance(placed, Placed):
                _declare(
                    declarations,
                    placed,
                    identifier,
                    table,
                    written,
                    proposals,
                    pii_overrides,
                )
            else:
                proposals.extend(
                    _schema_advisory(identifier, finding, placed.reason)
                    for finding in written
                )
            continue
        if view is None or base is None or model_path not in view.files:
            reason = (
                "this project format cannot receive edits"
                if view is None
                else "no profiled baseline to rebuild from"
                if base is None
                else f"no dex-scaffolded staging model at {model_path}"
            )
            proposals.extend(
                _schema_advisory(identifier, finding, reason) for finding in written
            )
            continue
        patched = _patched_dataset(base, table_findings, pii_overrides or set())
        table_edits = model_edits(patched)
        # The scaffold generates both the path and the dbt SQL inside it, so a
        # format that places a staging model somewhere else would receive dbt
        # content written to a path the generator did not agree to. Placement
        # alone cannot close this channel; authoring the content is the larger
        # design the seam deliberately does not attempt. Refuse rather than
        # write the mismatch, and say which of the two is missing.
        misplaced = any(
            edit.path != _placed(placement, edit.kind, table) for edit in table_edits
        )
        if misplaced:
            proposals.extend(
                Proposal(
                    axis="schema",
                    kind="advisory",
                    finding_code=finding.code,
                    identifier=identifier,
                    column=finding.column,
                    action=(
                        "this project format places a staging model somewhere "
                        "dex cannot author one for it; re-scaffold "
                        f"stg_{table} wherever that model is defined"
                    ),
                )
                for finding in table_findings
            )
            continue
        for edit in table_edits:
            declarations.stage_whole(edit.path, edit.kind, edit.new_content or "")
        changes = ", ".join(f"{f.code.replace('_', ' ')} ({f.column})" for f in written)
        proposals.append(
            Proposal(
                axis="schema",
                kind="mechanical",
                finding_code="schema_drift",
                identifier=identifier,
                action=(
                    f"re-scaffold stg_{table} from the drifted source "
                    f"({changes}); review the diff for hand-written logic "
                    "the scaffold cannot know about"
                ),
                paths=[edit.path for edit in table_edits],
            )
        )

    warnings.extend(_grain_test_edits(proposals, declarations, definitions))
    edits.extend(declarations.edits())
    warnings.extend(declarations.warnings)
    _drop_empty_proposals(proposals, {edit.path for edit in edits})

    if definition_churn:
        warnings.append(
            "definition or model changes since the baseline are recorded but "
            "not reconciled: if the current project state is intended, "
            "re-run `maintain snapshot` to accept it as the new baseline"
        )
    if len(orphan_identifiers) > 1:
        warnings.append(
            f"{len(orphan_identifiers)} orphan relations found this run; once "
            "you've confirmed none are read, drop them together in one "
            "governed pass: dbt run-operation drop_orphan_relations --args "
            f"'{_run_operation_args(orphan_identifiers)}'"
        )
    return proposals, edits, warnings


# --- helpers -------------------------------------------------------------------


def _run_operation_args(identifiers: list[str]) -> str:
    """The ``--args`` payload for ``dbt run-operation drop_orphan_relations``."""

    relations = ", ".join(f'"{identifier}"' for identifier in identifiers)
    return f"{{relations: [{relations}], dry_run: false}}"


def _advisory(finding: DriftFinding) -> Proposal:
    if finding.code == "orphan_relation":
        return Proposal(
            axis=finding.axis,
            kind="advisory",
            finding_code=finding.code,
            identifier=finding.identifier,
            column=finding.column,
            action=(
                "no dbt model or source declares this relation anymore; once "
                "you've confirmed nothing reads it, drop it through the "
                "governed macro (dex never executes this itself): scaffold "
                "it with `transform macro drop_orphan_relations` if the "
                "project does not have it yet, then run dbt run-operation "
                "drop_orphan_relations --args "
                f"'{_run_operation_args([finding.identifier])}'"
            ),
        )
    actions = {
        "table_added": (
            "a new table appeared; scaffold a staging model with "
            "`transform plan --scaffold` if it should enter the project"
        ),
        "table_dropped": (
            "the table is gone; remove its source declaration and decide the "
            "fate of the models built on it"
        ),
        "dangling_source": (
            "the declared source no longer matches the warehouse; remove or "
            "repoint the declaration and decide the downstream models' fate"
        ),
        "possible_rename": (
            "if this is a rename, update the staging model and downstream "
            "references to the new name instead of dropping the column"
        ),
        "row_count_changed": (
            "check the load or pipeline; if the new volume is expected, re-run "
            "`explore map` and `maintain snapshot` to accept it"
        ),
        # No promise of a test here. `_grain_test_edits` runs after this and
        # declines on five separate paths, so a clause asserting an edit would be
        # false on most of them; it appends the clause itself where it does emit
        # one.
        "key_lost_uniqueness": (
            "decide: dedup upstream, change the declared grain, or accept the "
            "duplicates"
        ),
        "declared_grain_not_unique": (
            "the project asserts a grain the data does not have, so this is a "
            "declaration to fix rather than drift to absorb: widen the "
            "combination to the real grain, dedup upstream, or drop the claim. "
            "dex proposes no edit, because narrowing or widening a declared "
            "grain is choosing one"
        ),
        "join_orphans_increased": (
            "investigate the upstream load; a dbt `relationships` test would "
            "make the orphaned keys visible in builds"
        ),
        "dimension_cardinality_changed": (
            "decide whether the new categorical value belongs in the impacted "
            "metric definitions; a firewalled `explore query` can name it"
        ),
        "dangling_reference": (
            "update the semantic definition with `semantic update`, or restore "
            "the model/column it references"
        ),
    }
    return Proposal(
        axis=finding.axis,
        kind="advisory",
        finding_code=finding.code,
        identifier=finding.identifier,
        column=finding.column,
        action=actions.get(
            finding.code, "review the finding; no automatic fix applies"
        ),
    )


def _schema_advisory(identifier: str, finding: DriftFinding, reason: str) -> Proposal:
    return Proposal(
        axis="schema",
        kind="advisory",
        finding_code=finding.code,
        identifier=identifier,
        column=finding.column,
        action=(
            f"{reason}; adjust the referencing models by hand or with `transform plan`"
        ),
    )


def _retype_advisory(finding: DriftFinding) -> Proposal:
    """A type change, surfaced rather than written.

    Nothing dex authors carries a type, and there is no type it could carry: the
    snapshot records the connector's own spelling rather than a canonical one, so
    a written type would be one warehouse's word for the column rather than the
    column's. Naming both spellings is what lets a reader decide whether the
    change is a widening they can absorb or one that breaks a downstream cast.
    """

    if finding.code != "column_retyped":
        return _advisory(finding)
    before = finding.data.get("type_before", "?")
    after = finding.data.get("type_after", "?")
    return Proposal(
        axis=finding.axis,
        kind="advisory",
        finding_code=finding.code,
        identifier=finding.identifier,
        column=finding.column,
        action=(
            f"the column changed type ({before} -> {after}); dex proposes no edit, "
            "because nothing it writes declares a type and the type it holds is "
            "the connector's spelling rather than a canonical one. Update the "
            "declared type and the models reading this column by hand"
        ),
    )


def _declare(
    declarations: DeclarationEdits,
    placed: Placed,
    identifier: str,
    table: str,
    written: list[DriftFinding],
    proposals: list[Proposal],
    pii_overrides: PIIOverrideMatcher | None,
) -> None:
    """Stage the schema drift onto the declaration the format placed.

    Every finding that could not be staged becomes its own advisory carrying the
    reason, so a reader never has to infer from a shorter list that dex declined
    something. The rest fold into one proposal, which says in words that the model
    itself was not authored: this format declined that kind, and a declined half
    a consumer cannot see is indistinguishable from one dex dropped.
    """

    staged: list[DriftFinding] = []
    for finding in written:
        if finding.code == "column_added":
            refusal = declarations.add_column(
                placed, _added_column(identifier, finding, pii_overrides)
            )
        elif finding.code == "column_dropped":
            refusal = declarations.drop_column(placed, finding.column or "")
        else:
            nullable = bool(finding.data.get("nullable_after", True))
            refusal = declarations.want_tests(
                placed,
                finding.column or "",
                add=() if nullable else ("not_null",),
                remove=("not_null",) if nullable else (),
            )
        if refusal is None:
            staged.append(finding)
        else:
            proposals.append(_schema_advisory(identifier, finding, refusal))
    if not staged:
        return
    changes = ", ".join(f"{f.code.replace('_', ' ')} ({f.column})" for f in staged)
    proposals.append(
        Proposal(
            axis="schema",
            kind="mechanical",
            finding_code="schema_drift",
            identifier=identifier,
            action=(
                f"update '{placed.model}' in {placed.path} to match the drifted "
                f"source ({changes}); this project format places no staging model "
                f"for {table}, so dex authored nothing for the model itself: make "
                "the matching change wherever that model is defined"
            ),
            paths=[placed.path],
        )
    )


def _added_column(
    identifier: str,
    finding: DriftFinding,
    pii_overrides: PIIOverrideMatcher | None,
) -> ColumnProfile:
    """The profile behind one added column, flagged the way the scaffold flags it.

    Name-based only, at base confidence: no aggregates exist for a column that
    appeared since the last profile, so there is no shape evidence to refine it
    with and the flag blocks until the next one. A column a human already cleared
    is cleared here too, with the audit kept.
    """

    data_type = str(finding.data.get("data_type", ""))
    column = finding.column or ""
    flag = detect_pii(column, data_type)
    overridden = f"{identifier}.{column}".lower() in (pii_overrides or set())
    return ColumnProfile(
        name=column,
        data_type=data_type,
        pii=None if overridden else flag,
        pii_overridden=flag.category if overridden and flag else None,
    )


def _drop_empty_proposals(proposals: list[Proposal], written: set[str]) -> None:
    """Demote a mechanical proposal whose paths produced no edit.

    The fold drops an edit that reproduced the file it already had, and a splice
    that did not verify is dropped too. Either leaves a proposal claiming a plan
    behind it that is not there, which tells a reader an edit landed when none
    did: the same failure the unique test's clause exists to prevent, one axis
    over.
    """

    for index, proposal in enumerate(proposals):
        if proposal.kind != "mechanical" or set(proposal.paths) & written:
            continue
        proposals[index] = proposal.model_copy(
            update={
                "kind": "advisory",
                "action": (
                    f"{proposal.action}. dex proposed no edit in the end: what it "
                    "would have written is what the project already holds, so the "
                    "drift is real and does not show up in anything dex authors"
                ),
                "paths": [],
            }
        )


def _base_dataset(
    identifier: str, cache: DexCache | None, snap: Snapshot
) -> Dataset | None:
    """The freshest profiled view of a table to patch: the cache wins over the
    snapshot (it may carry newer profiles), profiled entries only."""

    for source in (cache, snap.warehouse):
        if source is None:
            continue
        for dataset in source.datasets:
            if dataset.identifier == identifier and dataset.columns:
                return dataset
    return None


def _patched_dataset(
    base: Dataset,
    findings: list[DriftFinding],
    pii_overrides: PIIOverrideMatcher | set[str],
) -> Dataset:
    """Apply the detected column drift to the baseline profile, so the
    re-scaffold reflects the warehouse as it is now without re-profiling.
    New columns get name-based PII flags at base confidence (no aggregates
    exist yet, so no shape evidence: the flag blocks until the next profile
    refines it); an overridden column is cleared with the audit recorded."""

    patched = base.model_copy(deep=True)
    columns = {c.name: c for c in patched.columns}
    for finding in findings:
        if finding.code == "column_added" and finding.column not in columns:
            data_type = str(finding.data.get("data_type", ""))
            flag = detect_pii(finding.column, data_type)
            overridden = f"{base.identifier}.{finding.column}".lower() in pii_overrides
            profile = ColumnProfile(
                name=finding.column,
                data_type=data_type,
                pii=None if overridden else flag,
                pii_overridden=flag.category if overridden and flag else None,
            )
            patched.columns.append(profile)
            columns[finding.column] = profile
        elif finding.code == "column_dropped":
            patched.columns = [c for c in patched.columns if c.name != finding.column]
            columns.pop(finding.column, None)
        elif finding.code == "column_retyped" and finding.column in columns:
            columns[finding.column].data_type = str(finding.data.get("type_after", ""))
        elif finding.code == "nullability_changed" and finding.column in columns:
            columns[finding.column].nullable = bool(
                finding.data.get("nullable_after", True)
            )
    live = set(columns)
    patched.candidate_keys = [key for key in patched.candidate_keys if set(key) <= live]
    if patched.grain and not set(patched.grain) <= live:
        patched.grain = None
    return patched


#: Appended to a key_lost_uniqueness action only where a test edit is actually
#: in the plan. Kept here rather than in `_advisory`'s table because the two run
#: in that order and only this side knows the answer.
_TEST_EDIT_CLAUSE = "; the unique test keeps the break visible in builds"


def _grain_test_edits(
    proposals: list[Proposal],
    declarations: DeclarationEdits,
    definitions: ProjectDefinitions | None = None,
) -> list[str]:
    """Back key_lost_uniqueness proposals with a `unique` test where one belongs.

    The duplicates themselves stay a human decision; the test only makes the break
    visible in builds. It goes through the same accumulator the schema axis uses,
    so a table that drifted both ways arrives as one edit for its declaration
    rather than two edits pinned to the same content, the second of which would
    silently replace the first.

    Every path out of this that produces no edit says so in the returned warnings,
    and the action string gains its "the unique test keeps the break visible"
    clause only where an edit was produced. The proposal itself survives either
    way, so a silent skip would leave a reader looking at a `key_lost_uniqueness`
    proposal with no test edit and no way to tell whether dex declined or failed,
    and an unconditional promise would tell them an edit landed when none did.
    """

    warnings: list[str] = []
    for proposal in proposals:
        if proposal.finding_code != "key_lost_uniqueness" or proposal.column is None:
            continue
        table = (proposal.identifier or "").rsplit(".", 1)[-1]
        placed = declarations.resolve(table)
        if isinstance(placed, Declined):
            warnings.append(
                f"{placed.reason}, so the lost unique key on {proposal.identifier} "
                "has no test edit; add a `unique` test wherever that model is "
                "defined"
            )
            continue
        content = declarations.content_for(placed.path)
        current = (
            column_tests(content, placed.model, proposal.column)
            if content is not None
            else None
        )
        if current is None:
            warnings.append(
                f"{placed.path} declares no column '{proposal.column}' under a "
                f"model named '{placed.model}', so the lost unique key on "
                f"{proposal.identifier} has no test edit; add a `unique` test "
                "there by hand"
            )
            continue
        if "unique" in current.values:
            continue  # already alerting
        composite, unresolved = _declared_grain(
            definitions, placed.model, proposal.column
        )
        if unresolved:
            # Not a decline: nothing established that a composite covers this
            # column, so the edit stands. What is missing is the confidence, and
            # a reader deciding whether to apply the diff should know the check
            # could not run rather than read silence as a clean answer.
            warnings.append(
                f"the project's declarations for '{placed.model}' could not be "
                f"read ({unresolved}), so the `unique` test proposed on "
                f"{proposal.identifier}.{proposal.column} was not checked against "
                "a declared composite grain; confirm the model's grain before "
                "applying"
            )
        elif composite is not None:
            # The edit would assert a grain the project does not claim. dbt runs
            # a column-level `unique` and a `unique_combination_of_columns`
            # independently, so it would fail every build from here on and could
            # only go green by changing the declared grain; a format resolving the
            # two as dbt's semantics imply discards it instead. Proposing it and
            # letting the format sort it out is how a plan comes back
            # `conflicts=0` having changed nothing.
            warnings.append(
                f"'{placed.model}' declares a composite grain "
                f"({', '.join(composite)}), so no column-level `unique` is "
                f"proposed on {proposal.column}: the project never claimed that "
                "column is unique on its own. Either the composite is still the "
                "grain, in which case re-run `explore map` and `maintain "
                f"snapshot` to re-baseline, or something downstream relied on "
                f"{proposal.column} alone and that assumption is now false"
            )
            continue
        refusal = declarations.want_tests(placed, proposal.column, add=("unique",))
        if refusal is not None:
            warnings.append(
                f"{refusal}, so the lost unique key on {proposal.identifier} has "
                "no test edit; act on the drift that removed the column instead"
            )
            continue
        proposal.paths.append(placed.path)
        proposal.action += _TEST_EDIT_CLAUSE
    return warnings


def _declared_grain(
    definitions: ProjectDefinitions | None, model: str, column: str
) -> tuple[list[str] | None, str | None]:
    """The declared composite grain of ``model`` covering ``column``, or why the
    question could not be answered.

    Matching is by model name against the name the placed file gave, the same
    answer the caller already took the file's own word for, so the two halves of
    the check cannot disagree about which model this is. Returning the columns
    rather than a boolean is deliberate: the warning has to name the combination,
    because that is the fact that tells an operator whether to re-baseline or to
    go looking for what relied on the column alone.
    """

    # `None` is a caller that did not ask for the check, and is silent for the
    # same reason `placement=None` is: an optional argument left out must not
    # change what an existing caller sees. An *absent* project is different. The
    # caller only reaches here holding a loaded view, so a format that answers
    # "nothing readable" on the declarations channel while handing over that view
    # is contradicting itself, and the operator is about to apply an edit decided
    # on the half that came back empty.
    if definitions is None:
        return None, None
    if not definitions.present:
        return None, "the project format reported no readable project"
    target = column.lower()
    for composite in definitions.declared_composite_keys:
        if composite.model != model:
            continue
        if target in {c.lower() for c in composite.columns}:
            return list(composite.columns), None
    return None, None
