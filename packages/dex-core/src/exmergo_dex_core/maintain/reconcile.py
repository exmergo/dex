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

import yaml
from pydantic import BaseModel, Field

from ..cache import ColumnProfile, Dataset, DexCache
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


def build(
    findings: list[DriftFinding],
    snap: Snapshot,
    cache: DexCache | None,
    view: DbtProjectView,
    *,
    pii_overrides: set[str] | None = None,
) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
    """Map findings to proposals and plan edits. Pure: writes nothing.

    ``pii_overrides`` carries the config's reviewed non-PII column paths, so a
    drift-added column a human already cleared is not re-flagged into the
    scaffolded meta."""

    proposals: list[Proposal] = []
    edits: list[PlanEdit] = []
    warnings: list[str] = []

    schema_patches: dict[str, list[DriftFinding]] = {}
    definition_churn = False
    for finding in findings:
        if finding.axis == "schema" and finding.code in _PATCHABLE:
            schema_patches.setdefault(finding.identifier, []).append(finding)
        elif finding.code.startswith("definition_"):
            definition_churn = True
        else:
            proposals.append(_advisory(finding, view))

    for identifier, table_findings in sorted(schema_patches.items()):
        table = identifier.rsplit(".", 1)[-1]
        base = _base_dataset(identifier, cache, snap)
        model_path = f"models/staging/stg_{table}.sql"
        if base is None or model_path not in view.files:
            reason = (
                "no profiled baseline to rebuild from"
                if base is None
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

    grain_edits, grain_warnings = _grain_test_edits(proposals, view)
    edits.extend(grain_edits)
    warnings.extend(grain_warnings)

    if definition_churn:
        warnings.append(
            "definition changes since the baseline are recorded but not "
            "reconciled: if the current definitions are intended, re-run "
            "`maintain snapshot` to accept them as the new baseline"
        )
    return proposals, edits, warnings


# --- helpers -------------------------------------------------------------------


def _advisory(finding: DriftFinding, view: DbtProjectView) -> Proposal:
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
    base: Dataset, findings: list[DriftFinding], pii_overrides: set[str]
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
    proposals: list[Proposal], view: DbtProjectView
) -> tuple[list[PlanEdit], list[str]]:
    """Back key_lost_uniqueness proposals with a `unique` test edit when the
    scaffolded YAML exists and does not already alert. The duplicates
    themselves stay a human decision; the edit only makes the break visible."""

    edits: list[PlanEdit] = []
    warnings: list[str] = []
    for proposal in proposals:
        if proposal.finding_code != "key_lost_uniqueness" or proposal.column is None:
            continue
        table = (proposal.identifier or "").rsplit(".", 1)[-1]
        path = f"models/staging/stg_{table}.yml"
        source = view.files.get(path)
        if source is None:
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            warnings.append(f"{path} did not parse; add the unique test by hand")
            continue
        if not isinstance(parsed, dict):
            continue
        entry = next(
            (
                column
                for model in parsed.get("models") or []
                if isinstance(model, dict) and model.get("name") == f"stg_{table}"
                for column in model.get("columns") or []
                if isinstance(column, dict) and column.get("name") == proposal.column
            ),
            None,
        )
        if entry is None or "unique" in (entry.get("tests") or []):
            continue  # already alerting (or no scaffolded column entry to extend)
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
