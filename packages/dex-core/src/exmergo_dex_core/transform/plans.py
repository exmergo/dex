"""Repository edit plans: the propose half of propose-don't-impose.

The agent authors file content; the engine validates it, pins it to the current
source state, and hands it to the store as a plan. Nothing touches the source of
truth until ``apply``, and apply re-checks the pinned hashes so a human edit made
after planning surfaces as a conflict instead of being overwritten. Plans are
cache, not truth: the transformation project or semantic layer stays canonical,
and a deleted plan loses nothing but a proposal.

Plan ids are content-addressed (a hash of the intent plus the edits), so
re-planning the same change is idempotent and yields the same id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel

from ..diffs import file_diff
from ..edits import ApplyResult, Edit, EditOp, content_hash
from ..errors import DexError

if TYPE_CHECKING:
    from ..adapters.project import EditableProject
    from ..config import ConventionWarnings
    from ..dbt_project import DbtProjectView
    from ..edits import SemanticEditTarget
    from ..storage import Store


class PlanError(DexError):
    pass


class PlanNotFoundError(PlanError):
    pass


class EditKind(str, Enum):
    MODEL_SQL = "model_sql"
    SCHEMA_YML = "schema_yml"
    SEMANTIC_YML = "semantic_yml"
    # A semantic-layer document, independent of any transformation project.
    # Native Ossie is the first consumer; the kind intentionally names the
    # artifact rather than its vendor so another file-backed layer can reuse it.
    SEMANTIC_DOCUMENT = "semantic_document"
    # A dbt project-root manifest, not a model-path file: authoring it brings
    # dependency declaration inside the plan/apply guardrail like every other edit.
    PACKAGES_YML = "packages_yml"
    # A macro definition under the project's macro paths, the surface widened
    # for scaffolded and hand-repaired macros alike.
    MACRO_SQL = "macro_sql"
    # A snapshot block under the project's snapshot paths, and a seed's CSV data
    # under its seed paths. Both build a relation dbt names after the file and
    # both are ref()-able, which is why each is confined to its own family the
    # way a macro is: a snapshot filed under models/ is parsed as a model and
    # fails the build, and a seed filed anywhere else is never loaded at all.
    SNAPSHOT_SQL = "snapshot_sql"
    SEED_CSV = "seed_csv"
    # A file under the project's test paths (a singular test, which is a SELECT
    # that must return no rows, or a generic test definition) and one under its
    # analysis paths (SQL dbt compiles and never runs). Neither builds a
    # relation and nothing can ref() either, but each is still dbt's to parse
    # from its own directory: a singular test filed under models/ is built as a
    # model, and an analysis filed anywhere else is never compiled at all.
    #
    # `test_sql` is dbt's `test-paths`, not `transform test --scaffold` (which
    # writes a unit_tests: block through schema_yml) and not the generic tests
    # declared inside a schema.yml.
    TEST_SQL = "test_sql"
    ANALYSIS_SQL = "analysis_sql"
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
    # Which independent repository axis owns the edits. Old stored plans omit
    # this and remain transformation-project plans by default.
    edit_target: str = "project"
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


# Which kind may be authored where, keyed by dbt's authored path family and then
# by file suffix. dbt decides all of it: a `.sql` under the snapshot paths is
# parsed as a snapshot block and nothing else, a seed is loaded only from a
# `.csv` under the seed paths, and the properties YAML that declares a seed's
# column types or a snapshot's tests belongs beside the thing it describes.
#
# `schema_yml` is admitted under the snapshot, seed, test and analysis families
# for exactly that reason (a singular test's config and severity, an analysis's
# description), and deliberately *not* under macros: macro properties YAML is a
# different document shape, dex has never authored one, and a test pins both
# directions of today's macro rule. Widening that is a live behavior change and
# belongs in its own change, not smuggled in beside new kinds.
_PLACEMENT: dict[str, dict[str, frozenset[EditKind]]] = {
    "macro": {".sql": frozenset({EditKind.MACRO_SQL})},
    "snapshot": {
        ".sql": frozenset({EditKind.SNAPSHOT_SQL}),
        ".yml": frozenset({EditKind.SCHEMA_YML}),
        ".yaml": frozenset({EditKind.SCHEMA_YML}),
    },
    "seed": {
        ".csv": frozenset({EditKind.SEED_CSV}),
        ".yml": frozenset({EditKind.SCHEMA_YML}),
        ".yaml": frozenset({EditKind.SCHEMA_YML}),
    },
    "test": {
        ".sql": frozenset({EditKind.TEST_SQL}),
        ".yml": frozenset({EditKind.SCHEMA_YML}),
        ".yaml": frozenset({EditKind.SCHEMA_YML}),
    },
    "analysis": {
        ".sql": frozenset({EditKind.ANALYSIS_SQL}),
        ".yml": frozenset({EditKind.SCHEMA_YML}),
        ".yaml": frozenset({EditKind.SCHEMA_YML}),
    },
    "model": {
        ".sql": frozenset({EditKind.MODEL_SQL}),
        ".yml": frozenset({EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML}),
        ".yaml": frozenset({EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML}),
    },
}


def _home_families() -> dict[EditKind, str]:
    """The one family each kind belongs to, where it has one.

    Derived from :data:`_PLACEMENT` so the two can never drift: a kind exactly
    one family admits lives there, and a kind several admit (``schema_yml``) has
    no single home and is judged by where it actually landed instead.
    """

    families: dict[EditKind, set[str]] = {}
    for family, by_suffix in _PLACEMENT.items():
        for kinds in by_suffix.values():
            for kind in kinds:
                families.setdefault(kind, set()).add(family)
    return {
        kind: next(iter(found)) for kind, found in families.items() if len(found) == 1
    }


_HOME_FAMILY: dict[EditKind, str] = _home_families()

# The project-root manifests. Each governs the whole project rather than sitting
# in a path family, and each is pinned to its own filename separately, so the
# family table has nothing to say about them.
_ROOT_KINDS = frozenset(
    {EditKind.PACKAGES_YML, EditKind.PROJECT_YML, EditKind.PROFILES_YML}
)


def _an(word: str) -> str:
    """``word`` with the article that reads right in front of it.

    Refusal text is assembled from kind and family names, and "a analysis_sql
    edit" reads as a typo in the one message whose whole job is to be trusted.
    """

    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def assert_kind_placement(
    edit: PlanEdit, family: str | None, view: DbtProjectView
) -> None:
    """Refuse an edit whose kind and location disagree, in either direction.

    ``family`` is what :func:`~..dbt_project.path_family` made of the edit's
    path, and ``None`` means containment admitted it as a project-root manifest,
    which the family table does not govern.

    Both directions matter and they fail differently. A kind in the wrong family
    is told where its family is; a file in a family that does not admit its kind
    is told which kinds that family admits for that suffix. Either way the
    message names the fix rather than the rule.
    """

    if family is None or edit.kind in _ROOT_KINDS:
        return
    suffix = PurePosixPath(edit.path).suffix.lower()
    admitted = _PLACEMENT[family].get(suffix, frozenset())
    home = _HOME_FAMILY.get(edit.kind)
    if home is not None and home != family:
        configured = dict(view.path_families()).get(home) or []
        # Both halves, because either one is a complete fix and only the caller
        # knows which they meant: move the file, or relabel the kind.
        instead = (
            f"; a {suffix} file there is "
            + " or ".join(sorted(k.value for k in admitted))
            if admitted
            else ""
        )
        raise PlanError(
            f"{_an(edit.kind.value)} edit must live under the project's {home} "
            f"paths ({', '.join(configured) or 'none configured'}), got "
            f"'{edit.path}', which is {_an(family)} path{instead}"
        )
    if edit.kind in admitted:
        return
    if not admitted:
        holds = "; ".join(
            f"{ext} -> {', '.join(sorted(k.value for k in kinds))}"
            for ext, kinds in sorted(_PLACEMENT[family].items())
        )
        named = suffix or "suffixless"
        raise PlanError(
            f"'{edit.path}' is under {_an(family)} path, which holds {holds}; "
            f"dex authors no '{named}' file there"
        )
    raise PlanError(
        f"'{edit.path}' is under {_an(family)} path but the edit kind is "
        f"{edit.kind.value}; use "
        f"{' or '.join(sorted(k.value for k in admitted))} for a {suffix} file "
        "there"
    )


def admit_edit(
    edit: PlanEdit,
    view: DbtProjectView,
    project: Path,
    *,
    cache: Any = None,
    pii_overrides: Any = None,
) -> list[str]:
    """Every check that can refuse one dbt-shaped edit, and the warnings it
    raises. Stores nothing, writes nothing, reads no subprocess.

    Containment, kind-and-location agreement, the root-manifest pinning, and the
    per-kind content validation, in that order, which is the order the refusals
    read best in: where a file may live, then whether this kind may live there,
    then whether the content is what that kind promises.

    Split out of :func:`plan` because it has a second caller, and the order that
    caller needs it in is the point. ``transform plan`` hands snapshots, seeds
    and the config kinds to dbt's own parser before it stores anything, and dbt
    parses a *copy of the project with the edit written into it*. Running that
    first would mean a seed refused for carrying personal data had already been
    written to disk and read by a subprocess, and it would mean dbt's message
    ("Encountered unknown tag 'snapshot'") reaching the caller in place of dex's
    ("a snapshot_sql edit must live under the project's snapshot paths"). dex
    refuses on its own terms first; dbt's parser is the backstop behind it.
    """

    from ..dbt_project import contained_path, path_family

    resolved = contained_path(project, edit.path, view).resolve()
    assert_kind_placement(edit, path_family(project, edit.path, view), view)

    # The one root file each config kind may target. Both the kind and the path
    # must agree: a config kind aimed elsewhere, or one of these files reached
    # by any other kind, is refused.
    project_resolved = project.resolve()
    root_config = {
        EditKind.PROJECT_YML: (project_resolved / "dbt_project.yml").resolve(),
        EditKind.PROFILES_YML: (project_resolved / "profiles.yml").resolve(),
    }
    if edit.kind in root_config and resolved != root_config[edit.kind]:
        raise PlanError(
            f"a {edit.kind.value} edit must target the project's "
            f"{root_config[edit.kind].name}, got '{edit.path}'"
        )
    target_kind = {target: kind for kind, target in root_config.items()}.get(resolved)
    if target_kind is not None and edit.kind is not target_kind:
        raise PlanError(
            f"'{edit.path}' is a project config file but the edit kind is "
            f"{edit.kind.value}; use {target_kind.value} for it"
        )

    # A delete has no content to structurally validate; the whole-plan guard
    # verifies the post-deletion project instead.
    if edit.op is not EditOp.UPSERT:
        return []
    from .validate import validate_edit

    return validate_edit(edit, cache=cache, pii_overrides=pii_overrides)


def plan(
    intent: str,
    edits: list[PlanEdit],
    project_dir: Path | str | None = None,
    repo_root: Path | str = ".",
    *,
    store: Store,
    project_format: EditableProject | None = None,
    semantic_layer: SemanticEditTarget | None = None,
    edit_target: str = "project",
    pii_overrides: Any = None,
    conventions: ConventionWarnings | None = None,
) -> tuple[TransformPlan, list[dict[str, Any]], list[str]]:
    """Validate agent-authored edits and store them as a plan. Writes no source file.

    Returns the plan, the reviewable diffs against the current project, and any
    validation warnings. Each edit is pinned to the sha256 of the file it would
    change (``None`` for a create), which is what apply later re-checks.

    ``repo_root`` locates the repository source of truth; ``store`` is where the
    proposal itself lands.

    ``semantic_layer`` is the independent semantic-document edit target. It is
    deliberately separate from ``project_format``: accepting semantic edits
    does not make a layer a transformation project. The two routes share only
    hash pinning, containment, plan storage, diffs, and atomic apply.

    ``project_format`` is the format the edits were built against. Omitting it
    loads the dbt project and validates against dbt's own surface, which is what
    every caller predating the seam does and gets the behavior it always had. A
    format passed here supplies both halves of the check that used to be dbt's
    by assumption: the files an edit is pinned against, and the surface it may
    land in. Those two must come from the same place. Pinning against dbt's view
    while placing into a format's own keyspace hashes an existing file as absent,
    which renders a one-line change as a whole-file create and turns the next
    apply into a conflict on a file nobody touched.

    ``pii_overrides`` is the reviewed-columns matcher from the engine config,
    read only by the seed gate. Absent (a host calling this directly, a test),
    the gate still runs on the seed's own header and on whatever the exploration
    cache already flagged; an override is what a human uses to clear a column,
    not what makes the check happen.

    ``conventions`` is which house-convention warnings the project has left on
    (see :mod:`.conventions`). Absent means all of them, so a host calling this
    directly gets the same reading a configured repo gets by default; switching
    one off is a choice a project makes, not something a caller falls into by
    not passing it.
    """

    if not edits:
        raise PlanError("a plan needs at least one edit")
    if edit_target not in {"project", "semantic"}:
        raise PlanError(f"unknown edit target '{edit_target}'")
    if (edit_target == "semantic") != (semantic_layer is not None):
        raise PlanError(
            "semantic plans need a semantic-layer edit target, and project "
            "plans may not carry one"
        )

    # A format that does not place is not routed through this seam at all: it goes
    # down the path it went down before the seam existed, loading the dbt project
    # and validating against dbt's paths. That is a deliberate fallback rather than
    # an oversight, and the edits reaching here are dbt-shaped, so dbt's view is the
    # right thing to pin them against.
    #
    # Asked structurally rather than by probing for `editing_surface`, because the
    # branch needs `load()` as well and a format holding one without the other used
    # to reach this line and raise `AttributeError` from inside it. `PlacingProject`
    # is the object that says all three are there; `placement_gap` is what tells an
    # implementer which one is not.
    #
    if semantic_layer is not None:
        view = semantic_layer.semantic_edit_view()
        project = Path(getattr(view, "root", "."))
        places = True
        semantic_surface = list(semantic_layer.semantic_editing_surface())
        dbt_shaped = False
    else:
        # Late import: `adapters.project` reads this module's `EditKind`.
        from ..adapters.project import PlacingProject
        from ..dbt_project import DbtProjectView, find_project
        from ..dbt_project import load as load_project

        places = isinstance(project_format, PlacingProject)
        semantic_surface = []
    if semantic_layer is None and places:
        view = project_format.load()
        project = Path(project_dir) if project_dir else Path(getattr(view, "root", "."))
    elif semantic_layer is None:
        project = Path(project_dir) if project_dir else find_project(repo_root)
        view = load_project(project)

    # Which validations apply is a question about the view's shape, not about
    # which class produced it: a format handing back a `DbtProjectView` is
    # offering dbt's surface and gets dbt's checks, and asking the view keeps
    # this from becoming the `isinstance(project, DbtProject)` gate the seam
    # exists to remove.
    if semantic_layer is None:
        dbt_shaped = isinstance(view, DbtProjectView)
    surface = (
        []
        if dbt_shaped or not places
        else semantic_surface
        if semantic_layer is not None
        else list(project_format.editing_surface())
    )

    warnings: list[str] = []
    pinned: list[PlanEdit] = []
    diffs: list[dict[str, Any]] = []
    # Read at most once, and only when a seed is actually in the plan: the cache
    # is a stored document, and every other kind has no use for it.
    cached: list[Any] = []

    def seed_cache() -> Any:
        if not cached:
            from ..storage import CacheUnreadableError, readable_cache

            try:
                cached.append(readable_cache(store))
            except CacheUnreadableError as exc:
                # An unreadable cache narrows the seed's PII gate to the header
                # detector rather than stopping the plan. Said out loud, because
                # a check that quietly got weaker is worse than one that failed.
                cached.append(None)
                warnings.append(
                    f"the exploration cache could not be read ({exc}), so a "
                    "seed's columns were checked against their own names only, "
                    "not against columns a profile already flagged"
                )
        return cached[0]

    for edit in edits:
        # Containment is checked at plan time as well as at write time, so a bad
        # path is refused before it ever becomes a stored proposal. Which surface
        # it is checked against is the format's to say; that it is checked at all
        # is not.
        if dbt_shaped:
            warnings.extend(
                admit_edit(
                    edit,
                    view,
                    project,
                    cache=seed_cache() if edit.kind is EditKind.SEED_CSV else None,
                    pii_overrides=pii_overrides,
                )
            )
        else:
            # `admit_edit`'s checks read dbt's project layout (its path families,
            # its root manifests) to decide whether a kind and its location
            # agree. Another format's layout makes no such claim, and asserting
            # dbt's over it would refuse exactly the edits this seam exists to
            # allow. Agreement there is the format's own, expressed through what
            # `edit_path` places and what `write_edits` accepts.
            contained_key(edit.path, surface)
            if (
                edit.op is EditOp.UPSERT
                and edit.kind is not EditKind.SEMANTIC_DOCUMENT
            ):
                from .validate import validate_edit

                warnings.extend(validate_edit(edit))
        current = view.files.get(edit.path)
        if edit.op is EditOp.DELETE and current is None:
            raise PlanError(
                f"nothing to delete at '{edit.path}': the file is not part of "
                "the project (it may already be gone, or the path is wrong)"
            )
        # The profiles secret-guard, current side: validate_edit covers the
        # proposed content, but the diff also surfaces the removed (on-disk)
        # content, so a pre-existing inlined credential is refused before any
        # diff is built, never reaching agent context. A delete surfaces the same
        # removed content, so it is guarded too.
        if edit.kind is EditKind.PROFILES_YML and current is not None:
            from .validate import find_inlined_secret

            secret_key = find_inlined_secret(current.content)
            if secret_key is not None:
                raise PlanError(
                    f"{edit.path}: the current profiles.yml inlines a literal "
                    f"credential in '{secret_key}'; move it to "
                    "{{ env_var('NAME') }} before editing so no credential "
                    "enters the plan diff"
                )
        # A dbt_project.yml that drops a path key silently orphans the files
        # under it; warn rather than refuse, since a deliberate restructure is a
        # legitimate reason to change them. Every key dex authors into is
        # checked: covering only some of them makes the warning a coin flip on
        # which family the caller happened to restructure.
        if (
            edit.kind is EditKind.PROJECT_YML
            and edit.op is EditOp.UPSERT
            and current is not None
        ):
            old = yaml.safe_load(current.content) or {}
            new = yaml.safe_load(edit.new_content) or {}
            for key in (
                "model-paths",
                "macro-paths",
                "snapshot-paths",
                "seed-paths",
                "test-paths",
                "analysis-paths",
            ):
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

        # The one plan-time check that reads a convention rather than a
        # declaration, and the only one a project can switch off, because it is
        # a style judgment rather than a fact. Late-imported with the two above:
        # it reads this module's own vocabulary, and it reaches into explore for
        # the foreign-key naming rules, which no command that never plans should
        # pay for.
        if conventions is None or conventions.resolved_keys:
            from .conventions import unresolved_key_warnings

            warnings.extend(unresolved_key_warnings(edits, view, project))

    created_at = datetime.now(UTC).isoformat()
    try:
        rel_project = str(project.resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        rel_project = str(project)
    new_plan = TransformPlan(
        plan_id=_plan_id(intent, pinned, edit_target=edit_target),
        created_at=created_at,
        intent=intent,
        project_dir=rel_project,
        edit_target=edit_target,
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
    semantic_layer: SemanticEditTarget | None = None,
) -> ApplyResult:
    """Write a stored plan's edits into its source, hash-checked and
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

    Raises ``PlanError`` when a stored edit falls outside the surface the format
    declares, before anything is handed to the writer. Confirmation does not
    override it.
    """

    from ..adapters.project import PlacingProject

    stored = store.load_plan(plan_id)
    project = Path(repo_root) / stored.project_dir
    if stored.edit_target == "semantic":
        if semantic_layer is None:
            raise PlanError(
                "this plan targets a semantic layer, but the configured layer "
                "does not provide a semantic-document write surface"
            )
        surface = list(semantic_layer.semantic_editing_surface())
        for edit in stored.edits:
            contained_key(edit.path, surface)
        result = semantic_layer.write_semantic_edits(
            list(stored.edits), confirmed=confirmed
        )
    elif project_format is None:
        from ..dbt_project import write_edits

        result = write_edits(list(stored.edits), project, confirmed=confirmed)
    else:
        # Containment is re-checked here, against the surface the format declares
        # now, for the same reason the hashes are re-checked: a plan is a stored
        # artifact that sat through a human review, and what it was validated
        # against at plan time is not what it is being written into. The shipped
        # format re-checks inside its own writer; a second format is otherwise
        # trusted to, and this is the one guarantee that is not a format's to
        # decide.
        #
        # A hard refusal rather than a conflict: `confirmed` is the handshake for
        # a human edit someone can look at and accept, and no one accepts a write
        # outside the surface the format itself declared.
        if isinstance(project_format, PlacingProject):
            surface = list(project_format.editing_surface())
            for edit in stored.edits:
                contained_key(edit.path, surface)
        result = project_format.write_edits(
            list(stored.edits), project, confirmed=confirmed
        )
    if result.written:
        stored.applied_at = datetime.now(UTC).isoformat()
        store.save_plan(stored)
    return result


#: Reference forms that stop the project compiling once their target is gone.
#: A `ref()` is the obvious one; a semantic model's `model:` and a relationship
#: test's `to:` are the same thing written in YAML, and dbt fails to parse on
#: either. A `schema.yml` entry that merely *documents* the model is not here on
#: purpose: an orphaned doc block is a legitimate follow-up edit, so it stays the
#: soft warning it has always been rather than becoming a refusal a caller cannot
#: see the reason for.
_BREAKS_ON_DELETE = frozenset(
    {"ref_call", "semantic_model_ref", "yaml_relationship_to"}
)


def validate_deletions(view: DbtProjectView, edits: list[PlanEdit]) -> list[str]:
    """Refuse a plan whose deletions would orphan a ``ref()``, atomically.

    The project state *after* this plan is computed in memory (current files
    minus the deletions, with this plan's upserts overlaid), then indexed for
    references to the deleted nodes. A dangling ref is a hard refusal naming the
    offenders and the line; a deleted model whose only remaining trace is a
    schema.yml doc entry is a soft warning, since an orphaned doc block is a
    legitimate follow-up edit rather than a broken project.

    Overlaying the upserts is what makes the guard atomic across the whole plan:
    a plan that deletes a model *and* updates its referrer to drop the ref is
    accepted, while deleting the model alone is refused.

    The scan is :class:`~..references.ReferenceIndex` rather than a regex over
    file text, which changes three things and each is deliberate. A seed's data
    rows and a YAML string no longer count, so a CSV row that happens to contain
    the characters ``ref('x')`` stops blocking a delete. The two-argument
    ``ref('package', 'model')`` form is finally read as the model it names,
    rather than as the package, so a dangling reference written that way is
    caught instead of missed. And a reference dex cannot resolve statically
    (``{{ ref(var('x')) }}``) *warns* rather than refusing: it may or may not name
    the deleted node, dex cannot tell, and refusing on it would be unsatisfiable,
    because no edit the caller could make would make it resolvable.

    Packages are not scanned. The question is whether *this project* still points
    at what it is deleting, and a package cannot be edited to fix it anyway.
    """

    from ..dbt_project import SourceFile, node_files, node_name

    delete_paths = {e.path for e in edits if e.op is EditOp.DELETE}
    if not delete_paths:
        return []

    # A snapshot and a seed are ref()-able exactly as a model is, so deleting
    # one while a surviving model still ref()s it breaks the build the same way.
    # `node_files` is what says which of the deleted paths build a node at all,
    # which is why a macro is excluded and a seed's `.csv` is included: the
    # `.endswith(".sql")` test this used to make would have missed every seed.
    nodes = node_files(view)
    deleted_models = {node_name(path) for path in delete_paths if path in nodes}
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

    from ..references import ReferenceIndex

    after = view.model_copy(
        update={
            "files": {
                path: SourceFile(
                    path=path, content=content, sha256=content_hash(content)
                )
                for path, content in surviving.items()
            }
        }
    )
    index = ReferenceIndex(after, scan_packages=False)

    danglers: dict[str, list[str]] = {}
    for model in sorted(deleted_models):
        for kind in ("model", "seed", "snapshot"):
            for reference in index.references_to(model, kind)[0]:
                if reference.form not in _BREAKS_ON_DELETE:
                    continue
                danglers.setdefault(reference.path, []).append(
                    f"{model} (line {reference.line}, {reference.form})"
                )
    if danglers:
        detail = "; ".join(
            f"{path} still references {', '.join(names)}"
            for path, names in sorted(danglers.items())
        )
        raise PlanError(
            "this plan deletes "
            f"{', '.join(sorted(deleted_models))} but the surviving project "
            f"still references it: {detail}. Add the edits that remove those "
            "references to this same plan (or drop the deletion)."
        )

    warnings: list[str] = []
    unresolved = [
        reference
        for kind in ("model", "seed", "snapshot")
        for reference in index.indeterminate_for(kind)
    ]
    if unresolved:
        where = ", ".join(
            f"{reference.path}:{reference.line}" for reference in unresolved
        )
        warnings.append(
            f"the surviving project has {len(unresolved)} reference(s) dex could "
            f"not resolve ({where}); one of them may name a node this plan "
            "deletes, and dex cannot tell from the source alone"
        )
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

        produced = select_columns(edit.new_content)
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


def select_columns(sql: str) -> set[str] | None:
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

    import sqlglot
    from sqlglot import expressions as exp

    from .validate import _PLACEHOLDER, strip_jinja

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


def _plan_id(
    intent: str, edits: list[PlanEdit], *, edit_target: str = "project"
) -> str:
    payload: dict[str, Any] = {
        "intent": intent,
        "edits": [e.model_dump(mode="json") for e in edits],
    }
    # Preserve every existing project plan id. The discriminator is needed only
    # for the new axis, where an identical path/content must not collide with a
    # transformation-project proposal.
    if edit_target != "project":
        payload["edit_target"] = edit_target
    canonical = json.dumps(payload, sort_keys=True)
    return "p" + content_hash(canonical)[:10]
