"""Transform plans: the propose half of propose-don't-impose.

The agent authors dbt file content; the engine validates it, pins it to the
current project state, and hands it to the store as a plan. Nothing touches the
dbt project until ``apply``, and apply re-checks the pinned hashes so a human edit
made after planning surfaces as a conflict instead of being overwritten. Plans are
cache, not truth: the dbt project stays canonical, and a deleted plan loses
nothing but a proposal.

Plan ids are content-addressed (a hash of the intent plus the edits), so
re-planning the same change is idempotent and yields the same id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import sqlglot
import yaml
from pydantic import BaseModel
from sqlglot import expressions as exp

from ..dbt_project import (
    REF_PATTERN,
    ApplyResult,
    DbtProjectView,
    Edit,
    EditOp,
    contained_path,
    content_hash,
    find_project,
    write_edits,
)
from ..dbt_project import (
    load as load_project,
)
from ..diffs import file_diff
from ..errors import DexError
from .validate import _PLACEHOLDER, find_inlined_secret, strip_jinja, validate_edit

if TYPE_CHECKING:
    from ..adapters.project import EditableProject
    from ..storage import Store


class PlanError(DexError):
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


def contained_key(rel_path: str, surface: list[str]) -> str:
    """Refuse an edit path outside the surface a project format declared.

    The format-neutral half of containment. ``rel_path`` is a key into whatever
    the format's ``load()`` returned rather than a filesystem path, so this
    matches by path segment instead of resolving anything: a format keyed by
    something other than a directory still gets its surface honored, and a
    prefix cannot admit a sibling that merely starts with the same characters
    (``declarations`` admits ``declarations/orders.yml``, not
    ``declarations_backup/orders.yml``).

    Escapes are refused before the surface is consulted. An absolute path or one
    climbing out through ``..`` is not a format's to permit, and a format that
    listed one as its surface would be declaring the rest of the filesystem.
    """

    candidate = PurePosixPath(rel_path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PlanError(
            f"edit path must be project-relative and may not climb out of the "
            f"project: '{rel_path}'"
        )
    for prefix in surface:
        base = PurePosixPath(str(prefix).replace("\\", "/"))
        if candidate == base or base in candidate.parents:
            return rel_path
    listed = ", ".join(surface) if surface else "none"
    raise PlanError(
        f"edit path '{rel_path}' is outside the editing surface this project "
        f"format declares ({listed}); dex edits only what a format says it owns"
    )


def plan(
    intent: str,
    edits: list[PlanEdit],
    project_dir: Path | str | None = None,
    repo_root: Path | str = ".",
    *,
    store: Store,
    project_format: EditableProject | None = None,
) -> tuple[TransformPlan, list[dict[str, Any]], list[str]]:
    """Validate agent-authored edits and store them as a plan. Writes no project file.

    Returns the plan, the reviewable diffs against the current project, and any
    validation warnings. Each edit is pinned to the sha256 of the file it would
    change (``None`` for a create), which is what apply later re-checks.

    ``repo_root`` locates the dbt project (the source of truth, always a
    filesystem artifact); ``store`` is where the proposal itself lands.

    ``project_format`` is the format the edits were built against. Omitting it
    loads the dbt project and validates against dbt's own surface, which is what
    every caller predating the seam does and gets the behavior it always had. A
    format passed here supplies both halves of the check that used to be dbt's
    by assumption: the files an edit is pinned against, and the surface it may
    land in. Those two must come from the same place. Pinning against dbt's view
    while placing into a format's own keyspace hashes an existing file as absent,
    which renders a one-line change as a whole-file create and turns the next
    apply into a conflict on a file nobody touched.
    """

    if not edits:
        raise PlanError("a plan needs at least one edit")

    # A format that declares no surface is not routed through this seam at all:
    # it goes down the path it went down before the seam existed, loading the dbt
    # project and validating against dbt's paths. That is a deliberate fallback
    # rather than an oversight. Such a format cannot place an edit either (the
    # placement seam and the surface are declared together), so the edits reaching
    # here are dbt-shaped and dbt's view is the right thing to pin them against.
    declares_surface = getattr(project_format, "editing_surface", None)
    if project_format is not None and declares_surface is not None:
        view = project_format.load()
        project = Path(project_dir) if project_dir else Path(getattr(view, "root", "."))
    else:
        project = Path(project_dir) if project_dir else find_project(repo_root)
        view = load_project(project)

    # Which validations apply is a question about the view's shape, not about
    # which class produced it: a format handing back a `DbtProjectView` is
    # offering dbt's surface and gets dbt's checks, and asking the view keeps
    # this from becoming the `isinstance(project, DbtProject)` gate the seam
    # exists to remove.
    dbt_shaped = isinstance(view, DbtProjectView)
    surface = [] if dbt_shaped else list(declares_surface())

    warnings: list[str] = []
    pinned: list[PlanEdit] = []
    diffs: list[dict[str, Any]] = []
    project_resolved = project.resolve()
    macro_bases = (
        [(project_resolved / mp).resolve() for mp in view.macro_paths]
        if dbt_shaped
        else []
    )
    # The one root file each config kind may target, resolved once. Both the
    # kind and the path must agree: a config kind aimed elsewhere, or one of
    # these files reached by any other kind, is refused.
    root_config = (
        {
            EditKind.PROJECT_YML: (project_resolved / "dbt_project.yml").resolve(),
            EditKind.PROFILES_YML: (project_resolved / "profiles.yml").resolve(),
        }
        if dbt_shaped
        else {}
    )
    config_targets = {target: kind for kind, target in root_config.items()}
    for edit in edits:
        # Containment is checked at plan time as well as at write time, so a bad
        # path is refused before it ever becomes a stored proposal. Which surface
        # it is checked against is the format's to say; that it is checked at all
        # is not.
        if dbt_shaped:
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
        else:
            # The three checks above read dbt's project layout (its macro paths,
            # its root manifests) to decide whether a kind and its location
            # agree. Another format's layout makes no such claim, and asserting
            # dbt's over it would refuse exactly the edits this seam exists to
            # allow. Agreement there is the format's own, expressed through what
            # `edit_path` places and what `write_edits` accepts.
            contained_key(edit.path, surface)
        current = view.files.get(edit.path)
        if edit.op is EditOp.DELETE and current is None:
            raise PlanError(
                f"nothing to delete at '{edit.path}': the file is not part of "
                "the project (it may already be gone, or the path is wrong)"
            )
        # A delete has no content to structurally validate; the guard that runs
        # after this loop verifies the post-deletion project instead.
        if edit.op is EditOp.UPSERT:
            warnings.extend(validate_edit(edit))
        # The profiles secret-guard, current side: validate_edit covers the
        # proposed content, but the diff also surfaces the removed (on-disk)
        # content, so a pre-existing inlined credential is refused before any
        # diff is built, never reaching agent context. A delete surfaces the same
        # removed content, so it is guarded too.
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
        if (
            edit.kind is EditKind.PROJECT_YML
            and edit.op is EditOp.UPSERT
            and current is not None
        ):
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

    # The whole-plan delete guard: a delete is refused if any file that survives
    # the plan still refers to a deleted model. Run against the pinned edits so
    # an update in this same plan that removes the offending ref is honored.
    #
    # Both guards below read dbt's dependency vocabulary out of file content:
    # `ref()` for the orphan scan, and dbt macro calls for the missing-macro
    # warning. Another format expresses dependency its own way, so running these
    # over its files would find nothing and report that absence as a clean bill.
    # Silence is more honest than a check that cannot see what it is checking.
    if dbt_shaped:
        warnings.extend(validate_deletions(view, pinned))

        # Late import: scaffold imports PlanEdit from this module.
        from .scaffold import missing_macro_warnings

        warnings.extend(missing_macro_warnings(edits, view))
        warnings.extend(column_contract_warnings(edits, view))

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
    store.save_plan(new_plan)
    return new_plan, diffs, warnings


def apply(
    plan_id: str,
    repo_root: Path | str = ".",
    *,
    store: Store,
    confirmed: bool = False,
    project_format: EditableProject | None = None,
) -> ApplyResult:
    """Write a stored plan's edits into the project, hash-checked and
    all-or-nothing.

    ``project_format`` is the tier-3 format the plan should be written through.
    Omitting it writes with dbt's own writer, which is what every caller
    predating the seam does. Passing one matters for the same reason planning
    through it does: this module's writer resolves each edit as a path under the
    dbt project and re-hashes what it finds on disk, so a plan placed into
    another format's keyspace would be refused here even after the plan store
    accepted it, and the tier-3 ``write_edits`` the format implemented would
    never be reached. A plan a format could store and not apply is not a write
    path.
    """

    stored = store.load_plan(plan_id)
    project = Path(repo_root) / stored.project_dir
    if project_format is None:
        result = write_edits(list(stored.edits), project, confirmed=confirmed)
    else:
        result = project_format.write_edits(
            list(stored.edits), project, confirmed=confirmed
        )
    if result.written:
        stored.applied_at = datetime.now(UTC).isoformat()
        store.save_plan(stored)
    return result


def validate_deletions(view: DbtProjectView, edits: list[PlanEdit]) -> list[str]:
    """Refuse a plan whose deletions would orphan a ``ref()``, atomically.

    The project state *after* this plan is computed in memory (current files
    minus the deletions, with this plan's upserts overlaid), then every surviving
    file is scanned for a ``ref()`` to a deleted model. A dangling ref is a hard
    refusal naming the offenders; a deleted model whose only remaining trace is a
    schema.yml doc entry is a soft warning, since an orphaned doc block is a
    legitimate follow-up edit rather than a broken project.

    Overlaying the upserts is what makes the guard atomic across the whole plan:
    a plan that deletes a model *and* updates its referrer to drop the ref is
    accepted, while deleting the model alone is refused.
    """

    delete_paths = {e.path for e in edits if e.op is EditOp.DELETE}
    if not delete_paths:
        return []

    macro_prefixes = tuple(f"{mp}/" for mp in view.macro_paths)
    deleted_models = {
        Path(path).stem
        for path in delete_paths
        if path.endswith(".sql") and not path.startswith(macro_prefixes)
    }
    if not deleted_models:
        return []

    surviving: dict[str, str] = {
        path: source.content
        for path, source in view.files.items()
        if path not in delete_paths
    }
    for edit in edits:
        if edit.op is EditOp.UPSERT and edit.new_content is not None:
            surviving[edit.path] = edit.new_content

    danglers: dict[str, list[str]] = {}
    for path, content in surviving.items():
        hits = sorted(set(REF_PATTERN.findall(content)) & deleted_models)
        if hits:
            danglers[path] = hits
    if danglers:
        detail = "; ".join(
            f"{path} still ref()s {', '.join(names)}"
            for path, names in sorted(danglers.items())
        )
        raise PlanError(
            "this plan deletes "
            f"{', '.join(sorted(deleted_models))} but the surviving project "
            f"still references it: {detail}. Add the edits that remove those "
            "references to this same plan (or drop the deletion)."
        )

    warnings: list[str] = []
    for path, content in surviving.items():
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        documented = {
            entry.get("name")
            for entry in parsed.get("models") or []
            if isinstance(entry, dict)
        }
        orphaned = sorted(documented & deleted_models)
        if orphaned:
            warnings.append(
                f"{path}: still documents deleted model(s) {', '.join(orphaned)}; "
                "remove the schema.yml entry in a follow-up edit"
            )
    return warnings


def column_contract_warnings(edits: list[PlanEdit], view: DbtProjectView) -> list[str]:
    """Warn when a model's authored SELECT list diverges from what its
    schema.yml declares, in either direction (issue #214).

    schema.yml is the closest thing a dbt project has to a column contract,
    and plan time is the cheapest moment to check it against what was actually
    authored. Always a warning, never a refusal: the declaration is
    frequently the side that is stale, and the caller is often deliberately
    changing the model's shape.

    Scoped to models actually authored in this plan (not every model in the
    project) and only when that model declares a ``columns:`` list at all; a
    model with none has no contract to compare against and produces nothing.
    Declared columns are read with this plan's own SCHEMA_YML upserts
    overlaid on the current project files, so a model and its doc edited
    together in the same plan are compared against each other, not against a
    stale on-disk declaration.

    Where the authored SELECT list cannot be resolved statically (a bare
    ``select *``, a qualified ``t.*``, or an unaliased macro call standing in
    for a column), the comparison is skipped and that is said outright,
    rather than guessed at.
    """

    schema_sources = {
        path: source.content
        for path, source in view.files.items()
        if path.endswith((".yml", ".yaml"))
    }
    for edit in edits:
        if edit.kind is EditKind.SCHEMA_YML and edit.new_content is not None:
            schema_sources[edit.path] = edit.new_content

    declared_by_model: dict[str, list[str]] = {}
    for content in schema_sources.values():
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            columns = [
                column["name"]
                for column in entry.get("columns") or []
                if isinstance(column, dict) and isinstance(column.get("name"), str)
            ]
            if columns:
                declared_by_model[entry["name"]] = columns

    warnings: list[str] = []
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or edit.new_content is None
        ):
            continue
        model = Path(edit.path).stem
        declared = declared_by_model.get(model)
        if not declared:
            continue
        declared_lower = {c.lower() for c in declared}

        produced = _select_columns(edit.new_content)
        if produced is None:
            warnings.append(
                f"{edit.path}: schema.yml declares column(s) for {model}, but "
                "the SELECT list could not be resolved statically (a `select "
                "*`/`t.*`, or a macro standing in for a column); the "
                "contract comparison was skipped"
            )
            continue

        missing = sorted(declared_lower - produced)
        if missing:
            warnings.append(
                f"{edit.path}: schema.yml declares column(s) {', '.join(missing)} "
                f"for {model} that the SELECT list does not produce"
            )
        extra = sorted(produced - declared_lower)
        if extra:
            warnings.append(
                f"{edit.path}: the SELECT list produces column(s) "
                f"{', '.join(extra)} not declared in schema.yml for {model}"
            )
    return warnings


def _select_columns(sql: str) -> set[str] | None:
    """The lowercased output column names of a model's outermost SELECT, or
    ``None`` if they cannot be resolved statically.

    Unresolvable covers a bare ``select *``, a qualified ``t.*``, an unaliased
    macro call standing in for a column (indistinguishable from a real column
    once jinja is stripped to a placeholder, and can expand to any number of
    real columns), a set operation (a UNION's branches can each project
    differently), and anything that fails to parse. An *aliased* macro call
    (``{{ some_macro(x) }} as total``) is resolvable: the alias is the real
    output name regardless of what expression computed it.
    """

    stripped = strip_jinja(sql)
    if not stripped:
        return None
    try:
        parsed = sqlglot.parse_one(stripped, read="duckdb")
    except Exception:
        return None

    node = parsed
    while isinstance(node, (exp.With, exp.Subquery)):
        node = node.this
    if not isinstance(node, exp.Select):
        return None

    columns: set[str] = set()
    for projection in node.expressions:
        if isinstance(projection, exp.Star):
            return None
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return None
        name = projection.alias_or_name
        if not name or _PLACEHOLDER in name:
            return None
        columns.add(name.lower())
    return columns or None


def _plan_id(intent: str, edits: list[PlanEdit]) -> str:
    canonical = json.dumps(
        {
            "intent": intent,
            "edits": [e.model_dump(mode="json") for e in edits],
        },
        sort_keys=True,
    )
    return "p" + content_hash(canonical)[:10]
