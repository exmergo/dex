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

from pathlib import PurePosixPath

import yaml
from pydantic import BaseModel, Field

from ..adapters.project import PlacingProject
from ..cache import ColumnProfile, Dataset, DexCache
from ..config import PIIOverrideMatcher
from ..dbt_project import DbtProjectView
from ..explore.profile import detect_pii
from ..transform.plans import EditKind, PlanEdit
from ..transform.scaffold import model_edits
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


# Schema findings that patch a column list; the rest of the axis is advisory.
_PATCHABLE = {"column_added", "column_dropped", "column_retyped", "nullability_changed"}


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
    so a caller that does not pass one is unaffected."""

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    schema_patches: dict[str, list[DriftFinding]] = {}
    definition_churn = False
    orphan_identifiers: list[str] = []
    for finding in findings:
        if finding.axis == "schema" and finding.code in _PATCHABLE:
            schema_patches.setdefault(finding.identifier, []).append(finding)
        elif finding.code.startswith("definition_"):
            definition_churn = True
        else:
            if finding.code == "orphan_relation":
                orphan_identifiers.append(finding.identifier)
            proposals.append(_advisory(finding))

    for identifier, table_findings in sorted(schema_patches.items()):
        table = identifier.rsplit(".", 1)[-1]
        base = _base_dataset(identifier, cache, snap)
        model_path = _placed(placement, EditKind.MODEL_SQL, table)
        if (
            view is None
            or base is None
            or model_path is None
            or (model_path not in view.files)
        ):
            reason = (
                "this project format cannot receive edits"
                if view is None
                else "no profiled baseline to rebuild from"
                if base is None
                else "this project format has nowhere for a staging model to land"
                if model_path is None
                else f"no dex-scaffolded staging model at {model_path}"
            )
            proposals.extend(
                Proposal(
                    axis="schema",
                    kind="advisory",
                    finding_code=finding.code,
                    identifier=identifier,
                    column=finding.column,
                    action=(
                        f"{reason}; adjust the referencing models by hand "
                        "or with `transform plan`"
                    ),
                )
                for finding in table_findings
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
        edits.extend(table_edits)
        changes = ", ".join(
            f"{f.code.replace('_', ' ')} ({f.column})" for f in table_findings
        )
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

    grain_edits, grain_warnings = _grain_test_edits(proposals, view, placement)
    edits.extend(grain_edits)
    warnings.extend(grain_warnings)

    if definition_churn:
        warnings.append(
            "definition changes since the baseline are recorded but not "
            "reconciled: if the current definitions are intended, re-run "
            "`maintain snapshot` to accept them as the new baseline"
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
        "key_lost_uniqueness": (
            "decide: dedup upstream, change the declared grain, or accept the "
            "duplicates; the unique test keeps the break visible in builds"
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


def _grain_test_edits(
    proposals: list[Proposal],
    view: DbtProjectView | None,
    placement: PlacingProject | None = None,
) -> tuple[list[PlanEdit], list[str]]:
    """Back key_lost_uniqueness proposals with a `unique` test edit when the
    scaffolded YAML exists and does not already alert. The duplicates
    themselves stay a human decision; the edit only makes the break visible.

    Every path out of this that produces no edit says so in ``warnings``. The
    proposal itself survives either way, so a silent skip here would leave a
    reader looking at a `key_lost_uniqueness` proposal with no test edit and no
    way to tell whether dex declined or failed."""

    if view is None:
        return [], []

    edits: list[PlanEdit] = []
    warnings: list[str] = []
    for proposal in proposals:
        if proposal.finding_code != "key_lost_uniqueness" or proposal.column is None:
            continue
        table = (proposal.identifier or "").rsplit(".", 1)[-1]
        path = _placed(placement, EditKind.SCHEMA_YML, table)
        if path is None:
            warnings.append(
                f"this project format has nowhere for a test on {proposal.identifier} "
                "to land, so the lost unique key has no test edit; add a `unique` "
                "test wherever that model is defined"
            )
            continue
        source = view.files.get(path)
        if source is None:
            warnings.append(
                f"no dex-scaffolded {path} to extend, so the lost unique key on "
                f"{proposal.identifier} has no test edit; add a `unique` test "
                "wherever that model is defined"
            )
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            warnings.append(f"{path} did not parse; add the unique test by hand")
            continue
        if not isinstance(parsed, dict):
            continue
        # The convention leaks through content as well as through paths, and
        # placement only closes the path half. This matched `stg_{table}` inside
        # the YAML, so a format that placed the file correctly and names its
        # model `orders` was still missed, silently, one line before the edit.
        #
        # The model is named after the file the format chose. For dbt that is the
        # same string it always matched, because the scaffold path is
        # `models/staging/stg_{table}.yml` and its stem is `stg_{table}`; for a
        # format placing `declarations/orders.yml` it is `orders`. The format
        # already answered "where does this table's declaration live", and this
        # reads the answer instead of asserting dbt's spelling over it. A format
        # that packs many models into one file finds no entry and is warned
        # below, which is a refusal to guess rather than a wrong write.
        declared = PurePosixPath(path).stem
        entry = next(
            (
                column
                for model in parsed.get("models") or []
                if isinstance(model, dict) and model.get("name") == declared
                for column in model.get("columns") or []
                if isinstance(column, dict) and column.get("name") == proposal.column
            ),
            None,
        )
        if entry is None:
            # Split from the "already alerting" skip below, which is silent
            # because it is correct. This one is a miss: the file resolved and
            # the column entry did not, and a reader looking at the proposal
            # would otherwise have no way to tell that from dex declining.
            warnings.append(
                f"{path} declares no column '{proposal.column}' under a model "
                f"named '{declared}', so the lost unique key on "
                f"{proposal.identifier} has no test edit; add a `unique` test "
                "there by hand"
            )
            continue
        if "unique" in (entry.get("tests") or []):
            continue  # already alerting
        entry.setdefault("tests", []).append("unique")
        edits.append(
            PlanEdit(
                path=path,
                kind=EditKind.SCHEMA_YML,
                new_content=yaml.safe_dump(parsed, sort_keys=False),
            )
        )
        proposal.paths.append(path)
    return edits, warnings
