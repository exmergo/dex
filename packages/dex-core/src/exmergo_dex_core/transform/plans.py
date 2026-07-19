"""Transform plans: the propose half of propose-don't-impose.

The agent authors dbt file content; the engine validates it, pins it to the
current project state, and stores it as a plan under ``.dex/plans/``. Nothing
touches the dbt project until ``apply``, and apply re-checks the pinned hashes so
a human edit made after planning surfaces as a conflict instead of being
overwritten. Plans are cache, not truth: the dbt project stays canonical, and a
deleted plan loses nothing but a proposal.

Plan ids are content-addressed (a hash of the intent plus the edits), so
re-planning the same change is idempotent and yields the same id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from ..cache import DEX_DIR
from ..dbt_project import (
    ApplyResult,
    Edit,
    contained_path,
    content_hash,
    find_project,
    write_edits,
)
from ..dbt_project import (
    load as load_project,
)
from ..diffs import file_diff
from .validate import find_inlined_secret, validate_edit

PLANS_DIR = "plans"


class PlanError(Exception):
    pass


class PlanNotFoundError(PlanError):
    pass


class EditKind(str, Enum):
    MODEL_SQL = "model_sql"
    SCHEMA_YML = "schema_yml"
    SEMANTIC_YML = "semantic_yml"
    # A dbt project-root manifest, not a model-path file: authoring it brings
    # dependency declaration inside the plan/apply guardrail like every other edit.
    PACKAGES_YML = "packages_yml"
    # A macro definition under the project's macro paths, the surface widened
    # for scaffolded and hand-repaired macros alike.
    MACRO_SQL = "macro_sql"
    # dbt project-root config: the project settings and the connection profiles.
    # Each governs the whole project (a wider blast radius than a single model),
    # so each is pinned by name to the one root file it may target, and
    # profiles carries a secret-guard so no credential enters the plan diff.
    PROJECT_YML = "project_yml"
    PROFILES_YML = "profiles_yml"


class PlanEdit(Edit):
    kind: EditKind


class TransformPlan(BaseModel):
    schema_version: int = 1
    plan_id: str
    created_at: str
    intent: str
    # Relative to the repo root, so a plan stays valid when the repo moves.
    project_dir: str
    edits: list[PlanEdit]
    applied_at: str | None = None


class PlanStore:
    """Reader/writer for ``.dex/plans/``. Mirrors ``DexStore``'s repo scoping."""

    def __init__(self, repo_root: Path | str = "."):
        self.root = Path(repo_root)
        self.plans_dir = self.root / DEX_DIR / PLANS_DIR

    def path_for(self, plan_id: str) -> Path:
        return self.plans_dir / f"{plan_id}.json"

    def save(self, plan: TransformPlan) -> Path:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(plan.plan_id)
        path.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def load(self, plan_id: str) -> TransformPlan:
        path = self.path_for(plan_id)
        if not path.is_file():
            raise PlanNotFoundError(
                f"no plan '{plan_id}' under {self.plans_dir}; run `transform plan` "
                "first or check the id"
            )
        return TransformPlan.model_validate_json(path.read_text(encoding="utf-8"))

    def list_all(self) -> list[TransformPlan]:
        """Every stored plan, newest first."""

        if not self.plans_dir.is_dir():
            return []
        plans = [
            TransformPlan.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.plans_dir.glob("*.json")
        ]
        return sorted(plans, key=lambda p: p.created_at, reverse=True)

    def latest(self, kind: EditKind | None = None) -> TransformPlan | None:
        """The most recent unapplied plan, optionally only-of-``kind`` edits."""

        if not self.plans_dir.is_dir():
            return None
        candidates: list[TransformPlan] = []
        for path in self.plans_dir.glob("*.json"):
            plan = TransformPlan.model_validate_json(path.read_text(encoding="utf-8"))
            if plan.applied_at is not None:
                continue
            if kind is not None and any(e.kind is not kind for e in plan.edits):
                continue
            candidates.append(plan)
        return max(candidates, key=lambda p: p.created_at, default=None)


def plan(
    intent: str,
    edits: list[PlanEdit],
    project_dir: Path | str | None = None,
    repo_root: Path | str = ".",
) -> tuple[TransformPlan, list[dict[str, Any]], list[str]]:
    """Validate agent-authored edits and store them as a plan. Writes no project file.

    Returns the plan, the reviewable diffs against the current project, and any
    validation warnings. Each edit is pinned to the sha256 of the file it would
    change (``None`` for a create), which is what apply later re-checks.
    """

    if not edits:
        raise PlanError("a plan needs at least one edit")

    project = Path(project_dir) if project_dir else find_project(repo_root)
    view = load_project(project)

    warnings: list[str] = []
    pinned: list[PlanEdit] = []
    diffs: list[dict[str, Any]] = []
    project_resolved = project.resolve()
    macro_bases = [(project_resolved / mp).resolve() for mp in view.macro_paths]
    # The one root file each config kind may target, resolved once. Both the
    # kind and the path must agree: a config kind aimed elsewhere, or one of
    # these files reached by any other kind, is refused.
    root_config = {
        EditKind.PROJECT_YML: (project_resolved / "dbt_project.yml").resolve(),
        EditKind.PROFILES_YML: (project_resolved / "profiles.yml").resolve(),
    }
    config_targets = {target: kind for kind, target in root_config.items()}
    for edit in edits:
        # Containment is checked at plan time as well as at write time, so a bad
        # path is refused before it ever becomes a stored proposal.
        resolved = contained_path(
            project, edit.path, view.model_paths, view.macro_paths
        ).resolve()
        # Kind and surface must agree: a macro written into models/ would be
        # parsed as a model and fail the build, and a model written into
        # macros/ would silently never become a model.
        in_macros = any(
            resolved == base or base in resolved.parents for base in macro_bases
        )
        if edit.kind is EditKind.MACRO_SQL and not in_macros:
            raise PlanError(
                f"a macro_sql edit must live under the project's macro paths "
                f"({', '.join(view.macro_paths)}), got '{edit.path}'"
            )
        if edit.kind is not EditKind.MACRO_SQL and in_macros:
            raise PlanError(
                f"'{edit.path}' is under a macro path but the edit kind is "
                f"{edit.kind.value}; use macro_sql for macro files"
            )
        if edit.kind in root_config and resolved != root_config[edit.kind]:
            raise PlanError(
                f"a {edit.kind.value} edit must target the project's "
                f"{root_config[edit.kind].name}, got '{edit.path}'"
            )
        target_kind = config_targets.get(resolved)
        if target_kind is not None and edit.kind is not target_kind:
            raise PlanError(
                f"'{edit.path}' is a project config file but the edit kind is "
                f"{edit.kind.value}; use {target_kind.value} for it"
            )
        warnings.extend(validate_edit(edit))
        current = view.files.get(edit.path)
        # The profiles secret-guard, current side: validate_edit covers the
        # proposed content, but the diff also surfaces the removed (on-disk)
        # content, so a pre-existing inlined credential is refused before any
        # diff is built, never reaching agent context.
        if edit.kind is EditKind.PROFILES_YML and current is not None:
            secret_key = find_inlined_secret(current.content)
            if secret_key is not None:
                raise PlanError(
                    f"{edit.path}: the current profiles.yml inlines a literal "
                    f"credential in '{secret_key}'; move it to "
                    "{{ env_var('NAME') }} before editing so no credential "
                    "enters the plan diff"
                )
        # A dbt_project.yml that drops model or macro paths silently orphans the
        # files under them; warn rather than refuse, since a deliberate
        # restructure is a legitimate reason to change them.
        if edit.kind is EditKind.PROJECT_YML and current is not None:
            old = yaml.safe_load(current.content) or {}
            new = yaml.safe_load(edit.new_content) or {}
            for key in ("model-paths", "macro-paths"):
                dropped = set(old.get(key) or []) - set(new.get(key) or [])
                if dropped:
                    warnings.append(
                        f"{edit.path}: {key} drops {sorted(dropped)}; files under "
                        "those paths would no longer be part of the project"
                    )
        pinned.append(
            edit.model_copy(
                update={"old_content_hash": current.sha256 if current else None}
            )
        )
        diffs.append(
            file_diff(edit.path, current.content if current else None, edit.new_content)
        )

    # Late import: scaffold imports PlanEdit from this module.
    from .scaffold import missing_macro_warnings

    warnings.extend(missing_macro_warnings(edits, view))

    created_at = datetime.now(UTC).isoformat()
    try:
        rel_project = str(project.resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        rel_project = str(project)
    new_plan = TransformPlan(
        plan_id=_plan_id(intent, pinned),
        created_at=created_at,
        intent=intent,
        project_dir=rel_project,
        edits=pinned,
    )
    PlanStore(repo_root).save(new_plan)
    return new_plan, diffs, warnings


def apply(
    plan_id: str, repo_root: Path | str = ".", *, confirmed: bool = False
) -> ApplyResult:
    """Write a stored plan's edits into the dbt project, hash-checked and
    all-or-nothing."""

    store = PlanStore(repo_root)
    stored = store.load(plan_id)
    project = Path(repo_root) / stored.project_dir
    result = write_edits(list(stored.edits), project, confirmed=confirmed)
    if result.written:
        stored.applied_at = datetime.now(UTC).isoformat()
        store.save(stored)
    return result


def _plan_id(intent: str, edits: list[PlanEdit]) -> str:
    canonical = json.dumps(
        {
            "intent": intent,
            "edits": [e.model_dump(mode="json") for e in edits],
        },
        sort_keys=True,
    )
    return "p" + content_hash(canonical)[:10]
