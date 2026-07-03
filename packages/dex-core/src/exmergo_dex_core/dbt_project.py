"""The dbt project: the source of truth (read and write).

dex maintains no canonical model of its own. The dbt project is canonical, and
this module is the interface to it. Reads load the project into an in-memory view:
the raw source files under the model paths (the editing surface) plus the compiled
``manifest.json`` when present (dbt's own documented, versioned serialization of
nodes, sources, tests, semantic models, metrics, and lineage). Writes go back into
the source files as reviewable diffs; dex never holds a competing copy, so human
dbt edits are authoritative by construction.

The write path enforces propose-don't-impose mechanically: every edit carries the
sha256 of the file content it was planned against, and a mismatch at write time
means a human edited the file since the plan was made. That is a conflict: nothing
is written, the divergence is surfaced as a diff, and the caller must either
re-plan against current state or explicitly confirm the overwrite.

Absent a dbt project, explore still works (writing only to the ``.dex/`` cache),
but transform and maintain require one, since dbt is what they edit and diff.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .diffs import file_diff

PROJECT_FILE = "dbt_project.yml"
PROFILES_FILE = "profiles.yml"
MANIFEST_PATH = Path("target") / "manifest.json"


class DbtProjectError(Exception):
    pass


class SourceFile(BaseModel):
    """One editable source file, keyed by its project-relative path."""

    path: str
    content: str
    sha256: str


class DbtProjectView(BaseModel):
    """The in-memory view of a dbt project.

    ``files`` holds every ``*.sql``/``*.yml``/``*.yaml`` under the model paths:
    the surface transform edits. ``manifest`` is the compiled artifact when the
    project has been compiled; a fresh project loads fine without one.
    """

    root: str
    project_name: str
    profile_name: str
    model_paths: list[str] = Field(default_factory=lambda: ["models"])
    files: dict[str, SourceFile] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None


class TargetInfo(BaseModel):
    """A profiles.yml output, reduced to what is safe to surface.

    Only the name and adapter type cross the boundary; the output's connection
    fields (paths, hosts, credentials) never leave this module.
    """

    name: str
    type: str
    is_default: bool


class Edit(BaseModel):
    """One proposed file change, pinned to the content it was planned against.

    ``old_content_hash`` is the sha256 of the file at plan time; ``None`` means
    the file did not exist (a create). ``write_edits`` re-checks it so a human
    edit after planning is detected as a conflict, never silently overwritten.
    """

    path: str
    new_content: str
    old_content_hash: str | None = None


class Conflict(BaseModel):
    path: str
    expected_sha256: str | None
    found_sha256: str | None


class ApplyResult(BaseModel):
    written: list[str] = Field(default_factory=list)
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_projects(repo_root: Path | str = ".") -> list[Path]:
    """Every dbt project the search surface can see: the repo root itself, or
    its immediate children. Shared by ``find_project`` and ``transform init``'s
    already-exists refusal, so the two can never disagree."""

    root = Path(repo_root)
    if (root / PROJECT_FILE).is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if (child / PROJECT_FILE).is_file())


def find_project(repo_root: Path | str = ".") -> Path:
    """Locate the dbt project: the repo root itself, or a unique child directory.

    Ambiguity is an error rather than a guess; the caller can pin the project with
    ``dbt_project_dir`` in ``.dex/config.yml``.
    """

    root = Path(repo_root)
    candidates = discover_projects(root)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise DbtProjectError(
            f"no dbt project found under '{root}': transform and maintain edit a "
            "dbt project, so one is required (set dbt_project_dir in "
            ".dex/config.yml to pin it)"
        )
    raise DbtProjectError(
        f"multiple dbt projects under '{root}': "
        f"{', '.join(str(c) for c in candidates)}; set dbt_project_dir in "
        ".dex/config.yml to pin one"
    )


def load(project_dir: Path | str = ".") -> DbtProjectView:
    """Load the dbt project (source files + manifest if compiled)."""

    root = Path(project_dir)
    project_file = root / PROJECT_FILE
    if not project_file.is_file():
        raise DbtProjectError(f"no {PROJECT_FILE} in '{root}'")

    raw = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    project_name = raw.get("name")
    if not project_name:
        raise DbtProjectError(f"{project_file} has no 'name'")
    model_paths = list(raw.get("model-paths", ["models"]))

    files: dict[str, SourceFile] = {}
    for model_path in model_paths:
        base = root / model_path
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in {".sql", ".yml", ".yaml"} or not path.is_file():
                continue
            rel = str(path.relative_to(root))
            content = path.read_text(encoding="utf-8")
            files[rel] = SourceFile(
                path=rel, content=content, sha256=content_hash(content)
            )

    manifest: dict[str, Any] | None = None
    manifest_file = root / MANIFEST_PATH
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DbtProjectError(
                f"corrupt manifest at {manifest_file}: {exc}"
            ) from exc

    return DbtProjectView(
        root=str(root),
        project_name=project_name,
        # dbt defaults the profile name to the project name when unset.
        profile_name=raw.get("profile", project_name),
        model_paths=model_paths,
        files=files,
        manifest=manifest,
    )


def resolve_target(project_dir: Path | str, target: str | None = None) -> TargetInfo:
    """Resolve a profiles.yml target to its name and adapter type, nothing more.

    Search order matches dbt: ``DBT_PROFILES_DIR``, the project directory, then
    ``~/.dbt``. The output's connection fields (credentials among them) are read
    here and deliberately not returned.
    """

    view_profile = load(project_dir).profile_name
    profiles = _load_profiles(Path(project_dir))
    profile = profiles.get(view_profile)
    if not isinstance(profile, dict):
        raise DbtProjectError(f"profile '{view_profile}' not found in {PROFILES_FILE}")

    default = profile.get("target")
    outputs = profile.get("outputs") or {}
    name = target or default
    if not name:
        raise DbtProjectError(
            f"profile '{view_profile}' declares no default target; pass --target"
        )
    output = outputs.get(name)
    if not isinstance(output, dict):
        raise DbtProjectError(
            f"target '{name}' not found in profile '{view_profile}' "
            f"(available: {', '.join(sorted(outputs)) or 'none'})"
        )
    return TargetInfo(
        name=name, type=str(output.get("type", "unknown")), is_default=name == default
    )


def profiles_dir(project_dir: Path | str) -> Path:
    """The directory whose profiles.yml governs this project (dbt search order)."""

    env_dir = os.environ.get("DBT_PROFILES_DIR")
    if env_dir and (Path(env_dir) / PROFILES_FILE).is_file():
        return Path(env_dir)
    if (Path(project_dir) / PROFILES_FILE).is_file():
        return Path(project_dir)
    home = Path.home() / ".dbt"
    if (home / PROFILES_FILE).is_file():
        return home
    raise DbtProjectError(
        f"no {PROFILES_FILE} found (looked in $DBT_PROFILES_DIR, the project "
        "directory, and ~/.dbt)"
    )


def _load_profiles(project_dir: Path) -> dict[str, Any]:
    path = profiles_dir(project_dir) / PROFILES_FILE
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_edits(
    edits: list[Edit], project_dir: Path | str, *, confirmed: bool = False
) -> ApplyResult:
    """Write plan edits into the project, all-or-nothing.

    Per edit, the current file is re-hashed against ``old_content_hash``:

    - match (or an untouched create): clean, apply.
    - current content already equals ``new_content``: an already-applied no-op,
      not a conflict.
    - anything else: a human edited the file since the plan; a conflict.

    Any conflict with ``confirmed=False`` writes nothing and surfaces the
    divergence as diffs of current content against the plan's proposal. With
    ``confirmed=True`` the conflicts are overridden explicitly.
    """

    root = Path(project_dir)
    view = load(project_dir)

    staged: list[tuple[Path, Edit, str | None]] = []
    conflicts: list[Conflict] = []
    diffs: list[dict[str, Any]] = []
    for edit in edits:
        target_path = contained_path(root, edit.path, view.model_paths)
        current = (
            target_path.read_text(encoding="utf-8") if target_path.is_file() else None
        )
        current_hash = content_hash(current) if current is not None else None

        if current is not None and current == edit.new_content:
            # Already applied (e.g. a re-run): a no-op, not a conflict.
            continue
        if current_hash != edit.old_content_hash:
            conflicts.append(
                Conflict(
                    path=edit.path,
                    expected_sha256=edit.old_content_hash,
                    found_sha256=current_hash,
                )
            )
        diffs.append(file_diff(edit.path, current, edit.new_content))
        staged.append((target_path, edit, current))

    if conflicts and not confirmed:
        return ApplyResult(written=[], diffs=diffs, conflicts=conflicts)

    written: list[str] = []
    for target_path, edit, _current in staged:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(edit.new_content, encoding="utf-8")
        written.append(edit.path)
    return ApplyResult(written=written, diffs=diffs, conflicts=conflicts)


def contained_path(root: Path, rel_path: str, model_paths: list[str]) -> Path:
    """Resolve an edit path and refuse anything outside the project's model paths.

    Writes are confined to the repo, and within the repo to the dbt editing
    surface: model SQL, schema.yml, and semantic YAML all live under the model
    paths. Escapes (absolute paths, ``..``) are refused outright.
    """

    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise DbtProjectError(f"edit path must be project-relative: '{rel_path}'")
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    for model_path in model_paths:
        base = (root_resolved / model_path).resolve()
        if resolved == base or base in resolved.parents:
            return root / candidate
    raise DbtProjectError(
        f"edit path '{rel_path}' is outside the project's model paths "
        f"({', '.join(model_paths)}); dex edits only the dbt project surface"
    )
