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
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .diffs import file_diff

PROJECT_FILE = "dbt_project.yml"
PROFILES_FILE = "profiles.yml"
MANIFEST_PATH = Path("target") / "manifest.json"
SEMANTIC_MANIFEST_PATH = Path("target") / "semantic_manifest.json"

# The ref()/source() call shapes as they appear in model SQL, schema YAML test
# arguments, and semantic-model `model:` fields. Shared by every reader that
# traces a dbt-level name, so they can never disagree on what counts as a ref.
REF_PATTERN = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]")
SOURCE_PATTERN = re.compile(r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")
BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# dbt project-root files dex may author outside the model paths: the package
# manifests (dependency declarations), the project config, and the connection
# profiles. Each governs the whole project, not one model, which is why the
# editing surface widens by exactly these known names and never to arbitrary
# root files. Kinds are pinned to the specific file each one may target in
# ``transform.plans``; here we only gate containment.
_ALLOWED_ROOT_FILES = frozenset(
    {"packages.yml", "dependencies.yml", PROJECT_FILE, PROFILES_FILE}
)


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
    macro_paths: list[str] = Field(default_factory=lambda: ["macros"])
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


class EditOp(str, Enum):
    """The operation an edit performs, orthogonal to the file's ``kind``.

    ``UPSERT`` writes ``new_content`` (create or update, decided by whether the
    file already exists), the only behavior before deletes existed. ``DELETE``
    removes the file. The default is ``UPSERT`` so every stored plan written
    before this field existed deserializes unchanged.
    """

    UPSERT = "upsert"
    DELETE = "delete"


class Edit(BaseModel):
    """One proposed file change, pinned to the content it was planned against.

    ``old_content_hash`` is the sha256 of the file at plan time; ``None`` means
    the file did not exist (a create). ``write_edits`` re-checks it so a human
    edit after planning is detected as a conflict, never silently overwritten.

    ``op`` distinguishes writing content from removing the file. A delete carries
    no ``new_content`` (there is nothing to write) but still pins
    ``old_content_hash``, so removing a file a human edited after planning is a
    conflict, not a silent deletion.
    """

    path: str
    new_content: str | None = None
    old_content_hash: str | None = None
    op: EditOp = EditOp.UPSERT

    @model_validator(mode="after")
    def _content_matches_op(self) -> Edit:
        if self.op is EditOp.UPSERT and self.new_content is None:
            raise ValueError(f"an upsert edit needs new_content: '{self.path}'")
        if self.op is EditOp.DELETE and self.new_content is not None:
            raise ValueError(f"a delete edit carries no new_content: '{self.path}'")
        return self


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
    # dbt's own default when the key is absent, so a skeleton project's first
    # scaffolded macro lands where dbt will look for it.
    macro_paths = list(raw.get("macro-paths", ["macros"]))

    files: dict[str, SourceFile] = {}
    for model_path in model_paths + macro_paths:
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

    # Root-level config files dex may author (project settings, connection
    # targets, package manifests). Included so an edit to an existing one pins
    # the real content hash instead of mis-registering as a create, which would
    # otherwise surface at apply as a spurious conflict.
    for root_file in _ALLOWED_ROOT_FILES:
        path = root / root_file
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            files[root_file] = SourceFile(
                path=root_file, content=content, sha256=content_hash(content)
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
        macro_paths=macro_paths,
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


# The only keys of a profile output that may leave this module. Every one is a
# namespace identifier, not a credential: no user, account, host, password, token,
# or key path is ever surfaced, so what comes back is always safe to put in an
# envelope and show to the agent.
_TARGET_IDENTIFIER_KEYS = frozenset(
    {"type", "database", "schema", "warehouse", "dataset", "project", "catalog", "path"}
)


def target_identifiers(
    project_dir: Path | str, target: str | None = None
) -> dict[str, str]:
    """The namespace a profiles.yml target writes to, and nothing else.

    Where ``resolve_target`` answers "which adapter", this answers "which
    database, schema, warehouse". It exists so the engine can compare the
    rendered profile against ``.dex/config.yml`` and refuse a build whose config
    has silently drifted out of the profile that actually governs it. Missing
    profile or target yields ``{}``: the caller degrades to no check rather than
    erroring on a project it cannot read.
    """

    project = Path(project_dir)
    try:
        view_profile = load(project).profile_name
        profiles = _load_profiles(project)
    except (DbtProjectError, yaml.YAMLError):
        return {}
    profile = profiles.get(view_profile)
    if not isinstance(profile, dict):
        return {}
    outputs = profile.get("outputs") or {}
    output = outputs.get(target or profile.get("target"))
    if not isinstance(output, dict):
        return {}
    return {
        key: str(value)
        for key, value in output.items()
        if key in _TARGET_IDENTIFIER_KEYS and value is not None
    }


def target_role(project_dir: Path | str, target: str | None = None) -> str | None:
    """The role a profiles.yml target authenticates as, for a privilege preflight.

    Deliberately not part of :func:`target_identifiers`, whose result is
    envelope-safe by contract and therefore carries namespace identifiers only.
    A role name is an identity, so it gets its own door and one narrow caller:
    asking the warehouse whether *that* role may write the dev namespace.

    It has to be the profile's role rather than the one dex connects as, because
    reading a warehouse with a read-only role while dbt builds with a writing one
    is an ordinary split, and asking the wrong role would refuse a build dbt could
    have run. Callers may name it in the refusal (the GRANT that fixes the problem
    is useless without it) and nowhere else.
    """

    project = Path(project_dir)
    try:
        view_profile = load(project).profile_name
        profiles = _load_profiles(project)
    except (DbtProjectError, yaml.YAMLError):
        return None
    profile = profiles.get(view_profile)
    if not isinstance(profile, dict):
        return None
    outputs = profile.get("outputs") or {}
    output = outputs.get(target or profile.get("target"))
    if not isinstance(output, dict):
        return None
    role = output.get("user")
    return str(role) if role else None


def target_auth_method(
    project_dir: Path | str, target: str | None = None
) -> str | None:
    """The auth ``method`` a profiles.yml target declares, or None.

    Exists for one question: is this target IAM-authenticated? dbt-redshift's
    ``method: iam`` mints a database user from the caller's identity at run
    time, so the profile's ``user`` field is not a durable identity a privilege
    preflight can interrogate. Overloading the user field for that signal (a
    sentinel value) would misfire on profiles that carry a real user alongside
    IAM auth, so the method gets read directly.
    """

    project = Path(project_dir)
    try:
        view_profile = load(project).profile_name
        profiles = _load_profiles(project)
    except (DbtProjectError, yaml.YAMLError):
        return None
    profile = profiles.get(view_profile)
    if not isinstance(profile, dict):
        return None
    outputs = profile.get("outputs") or {}
    output = outputs.get(target or profile.get("target"))
    if not isinstance(output, dict):
        return None
    method = output.get("method")
    return str(method) if method else None


def duckdb_target_path(
    project_dir: Path | str, target: str | None = None
) -> Path | None:
    """The database file a duckdb target points at, or None.

    Relative paths resolve against the project dir, matching the cwd dbt runs
    with. None for non-duckdb outputs, in-memory databases, or an unresolvable
    profile/target. Only the path crosses the boundary; the output's other
    connection fields stay behind, per this module's contract (a local file
    path is not a credential).
    """

    project = Path(project_dir)
    try:
        view_profile = load(project).profile_name
        profiles = _load_profiles(project)
    except DbtProjectError:
        return None
    profile = profiles.get(view_profile)
    if not isinstance(profile, dict):
        return None
    outputs = profile.get("outputs") or {}
    name = target or profile.get("target")
    output = outputs.get(name)
    if not isinstance(output, dict) or str(output.get("type")) != "duckdb":
        return None
    raw_path = output.get("path")
    if not raw_path or str(raw_path) == ":memory:":
        return None
    path = Path(str(raw_path))
    return path if path.is_absolute() else project / path


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
        target_path = contained_path(
            root, edit.path, view.model_paths, view.macro_paths
        )
        current = (
            target_path.read_text(encoding="utf-8") if target_path.is_file() else None
        )
        current_hash = content_hash(current) if current is not None else None

        if edit.op is EditOp.DELETE and current is None:
            # Already gone (e.g. a re-run): a no-op, not a conflict.
            continue
        if (
            edit.op is EditOp.UPSERT
            and current is not None
            and current == edit.new_content
        ):
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
        # A delete renders as a diff against /dev/null (new is None).
        diffs.append(file_diff(edit.path, current, edit.new_content))
        staged.append((target_path, edit, current))

    if conflicts and not confirmed:
        return ApplyResult(written=[], diffs=diffs, conflicts=conflicts)

    written: list[str] = []
    for target_path, edit, _current in staged:
        if edit.op is EditOp.DELETE:
            target_path.unlink(missing_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(edit.new_content, encoding="utf-8")
        written.append(edit.path)
    return ApplyResult(written=written, diffs=diffs, conflicts=conflicts)


def contained_path(
    root: Path,
    rel_path: str,
    model_paths: list[str],
    macro_paths: list[str] | None = None,
) -> Path:
    """Resolve an edit path and refuse anything outside the project's editing
    surface.

    Writes are confined to the repo, and within the repo to the dbt editing
    surface: model SQL, schema.yml, and semantic YAML live under the model
    paths, and macros under the macro paths. Escapes (absolute paths, ``..``)
    are refused outright.
    """

    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise DbtProjectError(f"edit path must be project-relative: '{rel_path}'")
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    # The dbt package manifests live at the project root, so they are allowed by
    # name (still inside the project, still not an arbitrary escape).
    if resolved.parent == root_resolved and resolved.name in _ALLOWED_ROOT_FILES:
        return root / candidate
    allowed = list(model_paths) + list(macro_paths or [])
    for allowed_path in allowed:
        base = (root_resolved / allowed_path).resolve()
        if resolved == base or base in resolved.parents:
            return root / candidate
    raise DbtProjectError(
        f"edit path '{rel_path}' is outside the project's model and macro "
        f"paths ({', '.join(allowed)}); dex edits only the dbt project surface"
    )


# --- Read view: what the project declares -------------------------------------
#
# Everything below is read-only projection over the loaded project: declared
# foreign keys and column tests, semantic definitions, and the physical
# relations they resolve to. Consumers that must work without a dbt project
# (explore on a raw warehouse) go through `definitions()`, which degrades to an
# empty view instead of raising.


def yaml_documents(view: DbtProjectView) -> list[tuple[dict[str, Any], str]]:
    """Every parseable YAML document under the model paths, with its path.

    A broken hand-written file is skipped, not an error: readers of declared
    definitions must not fail on files they don't own.
    """

    documents: list[tuple[dict[str, Any], str]] = []
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            documents.append((parsed, source.path))
    return documents


def semantic_yaml_entries(
    view: DbtProjectView,
) -> list[tuple[str, dict[str, Any], str]]:
    """Raw ``semantic_models`` / ``metrics`` YAML entries as ``(kind, entry,
    path)`` triples, kind being ``"semantic_model"`` or ``"metric"``.

    Sourced from the YAML files only, never a compiled artifact, so consumers
    that fingerprint definitions hash exactly what the author wrote.
    """

    entries: list[tuple[str, dict[str, Any], str]] = []
    for parsed, path in yaml_documents(view):
        entries.extend(
            ("semantic_model", entry, path)
            for entry in parsed.get("semantic_models") or []
            if isinstance(entry, dict) and entry.get("name")
        )
        entries.extend(
            ("metric", entry, path)
            for entry in parsed.get("metrics") or []
            if isinstance(entry, dict) and entry.get("name")
        )
    return entries


def physical_column(entry: Any) -> str | None:
    """The single physical column a dimension/entity/measure references, if any.

    A bare-identifier ``expr`` is the column; a name with no ``expr`` is treated
    by dbt as the column itself; a computed expression maps to None. Guessing
    columns out of expressions would make every reader over-claim.
    """

    if not isinstance(entry, dict):
        return None
    expr = entry.get("expr")
    if expr is None:
        name = entry.get("name")
        return name if isinstance(name, str) else None
    if isinstance(expr, str) and BARE_IDENTIFIER.fullmatch(expr.strip()):
        return expr.strip()
    return None


def metric_inputs(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The measures and metrics one metric definition draws from, by type.

    Simple/cumulative metrics ground in one measure; ratio and derived metrics
    reference other metrics; conversion metrics ground in two measures. Unknown
    types yield nothing rather than guessing.
    """

    metric_type = str(entry.get("type", "")).lower()
    params = entry.get("type_params")
    params = params if isinstance(params, dict) else {}
    measures: list[str] = []
    metrics: list[str] = []

    def add(bucket: list[str], value: Any) -> None:
        if isinstance(value, str):
            bucket.append(value)
        elif isinstance(value, dict) and isinstance(value.get("name"), str):
            bucket.append(value["name"])

    if metric_type in {"simple", "cumulative"}:
        add(measures, params.get("measure"))
    elif metric_type == "ratio":
        add(metrics, params.get("numerator"))
        add(metrics, params.get("denominator"))
    elif metric_type == "derived":
        for input_metric in params.get("metrics") or []:
            add(metrics, input_metric)
    elif metric_type == "conversion":
        conversion = params.get("conversion_type_params")
        if isinstance(conversion, dict):
            add(measures, conversion.get("base_measure"))
            add(measures, conversion.get("conversion_measure"))
    return measures, metrics


class DeclaredForeignKey(BaseModel):
    """One ``relationships`` test: child column to parent column.

    ``relation`` / ``to_relation`` carry quote-stripped physical names when the
    manifest resolves them; the YAML fallback leaves them None, and downstream
    resolution is name-based.
    """

    model: str
    relation: str | None = None
    column: str
    to_model: str
    to_relation: str | None = None
    to_column: str
    source: str


class DeclaredKey(BaseModel):
    """A column carrying ``unique`` and/or ``not_null`` tests on one model."""

    model: str
    relation: str | None = None
    column: str
    unique: bool = False
    not_null: bool = False
    source: str


class ProjectDefinitions(BaseModel):
    """What the dbt project declares, loaded once for consumers that must keep
    working without one.

    ``present`` False means no readable project: every collection is empty and
    consumers degrade instead of erroring. ``relationship_source`` and
    ``semantic_source`` record where each half came from (``"manifest"`` is
    exact, ``"yaml"`` resolves by name). ``model_relations`` maps referable
    names (model names and ``source.table``) to quote-stripped physical
    relations. ``primary_entities`` maps model names to their declared grain
    column; ``metric_models`` lists models reachable from any metric. ``notes``
    are analyst-readable caveats for the caller's envelope.
    """

    present: bool = False
    project_dir: str | None = None
    manifest_loaded: bool = False
    manifest_stale: bool = False
    relationship_source: str | None = None
    semantic_source: str | None = None
    foreign_keys: list[DeclaredForeignKey] = Field(default_factory=list)
    declared_keys: list[DeclaredKey] = Field(default_factory=list)
    model_relations: dict[str, str] = Field(default_factory=dict)
    primary_entities: dict[str, str] = Field(default_factory=dict)
    metric_models: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def definitions(
    repo_root: Path | str = ".", project_dir: Path | str | None = None
) -> ProjectDefinitions:
    """Load the project's declared and semantic definitions, degrading quietly.

    ``project_dir`` pins the project (callers pass ``dbt_project_dir`` from
    ``.dex/config.yml``); otherwise the repo root and its immediate children
    are searched. No project, an ambiguous choice, or an unreadable project
    yields the empty view (with a note where there is something actionable to
    say), never an exception: explore runs on raw warehouses where absence is
    the normal case.
    """

    root = Path(repo_root)
    if project_dir is not None:
        project = Path(project_dir)
    else:
        candidates = discover_projects(root)
        if not candidates:
            return ProjectDefinitions()
        if len(candidates) > 1:
            listed = ", ".join(str(c) for c in candidates)
            return ProjectDefinitions(
                notes=[
                    f"multiple dbt projects found ({listed}); set "
                    "dbt_project_dir in .dex/config.yml to use their declared "
                    "definitions"
                ]
            )
        project = candidates[0]

    try:
        view = load(project)
    except DbtProjectError as exc:
        return ProjectDefinitions(
            notes=[
                f"dbt project at '{project}' could not be read ({exc}); "
                "declared definitions unavailable"
            ]
        )

    defs = ProjectDefinitions(present=True, project_dir=str(project))

    nodes = (view.manifest or {}).get("nodes")
    if isinstance(nodes, dict) and nodes:
        _declared_from_manifest(view.manifest or {}, defs)
    else:
        _declared_from_yaml(view, defs)

    semantic_manifest = _read_semantic_manifest(Path(view.root))
    if semantic_manifest is not None:
        _semantic_from_manifest(semantic_manifest, defs)
    else:
        _semantic_from_yaml(semantic_yaml_entries(view), defs)

    if defs.manifest_loaded:
        _flag_stale_manifest(view, defs)
    return defs


def _strip_relation_quoting(relation: str) -> str:
    """``"db"."schema"."table"`` / `` `project.dataset.table` `` / bracketed
    forms down to plain dotted parts, matching adapter-normalized identifiers."""

    text = relation.strip()
    # BigQuery wraps the whole dotted name in one backtick pair; strip before
    # splitting so the dots become visible.
    if text.startswith("`") and text.endswith("`"):
        text = text.strip("`")
    parts = [
        part.strip().strip('"').strip("`").lstrip("[").rstrip("]")
        for part in text.split(".")
    ]
    return ".".join(part for part in parts if part)


def _parse_relation_ref(value: Any) -> str | None:
    """A ``ref('x')`` / ``source('a', 'b')`` argument as a referable name."""

    if not isinstance(value, str):
        return None
    ref = REF_PATTERN.search(value)
    if ref:
        return ref.group(1)
    src = SOURCE_PATTERN.search(value)
    if src:
        return f"{src.group(1)}.{src.group(2)}"
    return None


def _declared_from_manifest(manifest: dict[str, Any], defs: ProjectDefinitions) -> None:
    """Declared FKs and column tests from compiled test nodes, physically
    resolved through each node's ``relation_name``. Every access is guarded:
    hand-rolled or truncated manifests must fall through quietly, not raise."""

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}

    # unique_id -> referable name, and referable name -> physical relation.
    names: dict[str, str] = {}
    relations: dict[str, str] = {}
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        config = node.get("config")
        if isinstance(config, dict) and config.get("enabled") is False:
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        names[uid] = name
        relation = node.get("relation_name")
        # Ephemeral models compile with a null relation_name: referable in the
        # project but not physically resolvable, so they stay out of relations.
        if isinstance(relation, str) and relation:
            relations[name] = _strip_relation_quoting(relation)
    for uid, node in sources.items():
        if not isinstance(node, dict):
            continue
        source_name, table = node.get("source_name"), node.get("name")
        if not (isinstance(source_name, str) and isinstance(table, str)):
            continue
        key = f"{source_name}.{table}"
        names[uid] = key
        relation = node.get("relation_name")
        if isinstance(relation, str) and relation:
            relations[key] = _strip_relation_quoting(relation)

    def attached_name(node: dict[str, Any], exclude: str | None = None) -> str | None:
        attached = node.get("attached_node")
        if isinstance(attached, str) and attached in names:
            return names[attached]
        # Older manifests lack attached_node; a relationships test then depends
        # on exactly the child and the parent, so the non-parent entry is the child.
        depends = node.get("depends_on")
        dep_nodes = depends.get("nodes") if isinstance(depends, dict) else None
        for dep in dep_nodes or []:
            name = names.get(dep)
            if name is not None and name != exclude:
                return name
        return None

    keys: dict[tuple[str, str], DeclaredKey] = {}
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        if not isinstance(meta, dict):
            continue
        kwargs = meta.get("kwargs")
        kwargs = kwargs if isinstance(kwargs, dict) else {}
        column = kwargs.get("column_name")
        if not isinstance(column, str) or not column:
            continue
        test_name = meta.get("name")
        if test_name == "relationships":
            to_model = _parse_relation_ref(kwargs.get("to"))
            field = kwargs.get("field")
            if not to_model or not isinstance(field, str) or not field:
                continue
            child = attached_name(node, exclude=to_model)
            if child is None:
                continue
            defs.foreign_keys.append(
                DeclaredForeignKey(
                    model=child,
                    relation=relations.get(child),
                    column=column,
                    to_model=to_model,
                    to_relation=relations.get(to_model),
                    to_column=field,
                    source="manifest",
                )
            )
        elif test_name in ("unique", "not_null"):
            child = attached_name(node)
            if child is None:
                continue
            key = keys.setdefault(
                (child, column),
                DeclaredKey(
                    model=child,
                    relation=relations.get(child),
                    column=column,
                    source="manifest",
                ),
            )
            if test_name == "unique":
                key.unique = True
            else:
                key.not_null = True

    defs.declared_keys = list(keys.values())
    defs.model_relations.update(relations)
    defs.manifest_loaded = True
    defs.relationship_source = "manifest"


def _declared_from_yaml(view: DbtProjectView, defs: ProjectDefinitions) -> None:
    """Column-level tests straight from schema YAML: model-level names only,
    no physical resolution. Model-level relationships tests (declared under the
    model's ``tests:`` with a ``column_name`` kwarg) are a manifest-only shape."""

    keys: dict[tuple[str, str], DeclaredKey] = {}
    for parsed, _path in yaml_documents(view):
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            model = entry["name"]
            for column in entry.get("columns") or []:
                if not isinstance(column, dict) or not isinstance(
                    column.get("name"), str
                ):
                    continue
                col_name = column["name"]
                tests = column.get("tests") or column.get("data_tests") or []
                for test in tests:
                    kind = test if isinstance(test, str) else None
                    if isinstance(test, dict):
                        if "relationships" in test:
                            cfg = test.get("relationships")
                            cfg = cfg if isinstance(cfg, dict) else {}
                            to_model = _parse_relation_ref(cfg.get("to"))
                            field = cfg.get("field")
                            if to_model and isinstance(field, str) and field:
                                defs.foreign_keys.append(
                                    DeclaredForeignKey(
                                        model=model,
                                        column=col_name,
                                        to_model=to_model,
                                        to_column=field,
                                        source="yaml",
                                    )
                                )
                            continue
                        kind = next(
                            (k for k in ("unique", "not_null") if k in test), None
                        )
                    if kind in ("unique", "not_null"):
                        key = keys.setdefault(
                            (model, col_name),
                            DeclaredKey(model=model, column=col_name, source="yaml"),
                        )
                        if kind == "unique":
                            key.unique = True
                        else:
                            key.not_null = True

    defs.declared_keys = list(keys.values())
    defs.relationship_source = "yaml"
    if defs.foreign_keys:
        defs.notes.append(
            "declared joins read from schema YAML (project not compiled); "
            "physical resolution is name-based"
        )


def _read_semantic_manifest(project: Path) -> dict[str, Any] | None:
    """``target/semantic_manifest.json`` when present and carrying semantic
    models; an empty or unreadable artifact falls back to raw YAML."""

    path = project / SEMANTIC_MANIFEST_PATH
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("semantic_models"):
        return payload
    return None


def _primary_entity_column(entities: Any) -> str | None:
    """The declared grain: the primary entity's column, when it is a plain
    column reference (bare ``expr``, or a name with no ``expr``)."""

    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("type", "")).lower() != "primary":
            continue
        return physical_column(entity)
    return None


def _semantic_from_manifest(payload: dict[str, Any], defs: ProjectDefinitions) -> None:
    """Grain and metric lineage from the compiled semantic manifest, whose
    ``node_relation`` also resolves each semantic model physically (useful even
    when manifest.json is absent). ``input_measures`` is pre-resolved there, so
    ratio/derived chains need no chasing."""

    measure_owner: dict[str, str] = {}
    for entry in payload.get("semantic_models") or []:
        if not isinstance(entry, dict):
            continue
        node_relation = entry.get("node_relation")
        node_relation = node_relation if isinstance(node_relation, dict) else {}
        model = node_relation.get("alias") or _parse_relation_ref(
            str(entry.get("model", ""))
        )
        if not isinstance(model, str) or not model:
            continue
        relation = node_relation.get("relation_name")
        if isinstance(relation, str) and relation:
            defs.model_relations.setdefault(model, _strip_relation_quoting(relation))
        grain = _primary_entity_column(entry.get("entities"))
        if grain:
            defs.primary_entities[model] = grain
        for measure in entry.get("measures") or []:
            if isinstance(measure, dict) and isinstance(measure.get("name"), str):
                measure_owner[measure["name"]] = model

    reachable: set[str] = set()
    for metric in payload.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        params = metric.get("type_params")
        params = params if isinstance(params, dict) else {}
        for input_measure in params.get("input_measures") or []:
            name = (
                input_measure.get("name")
                if isinstance(input_measure, dict)
                else input_measure
            )
            owner = measure_owner.get(name) if isinstance(name, str) else None
            if owner:
                reachable.add(owner)
    defs.metric_models = sorted(reachable)
    defs.semantic_source = "manifest"


def _semantic_from_yaml(
    entries: list[tuple[str, dict[str, Any], str]], defs: ProjectDefinitions
) -> None:
    """The same grain and lineage from raw YAML entries. Ratio and derived
    metrics reference other metrics, so lineage resolves transitively down to
    measures (with a seen-set: a metric cycle is an authoring error, not a
    reason to recurse forever)."""

    if not entries:
        return
    measure_owner: dict[str, str] = {}
    metric_graph: dict[str, tuple[list[str], list[str]]] = {}
    for kind, entry, _path in entries:
        if kind == "semantic_model":
            model = _parse_relation_ref(str(entry.get("model", "")))
            if not model:
                continue
            grain = _primary_entity_column(entry.get("entities"))
            if grain:
                defs.primary_entities[model] = grain
            for measure in entry.get("measures") or []:
                if isinstance(measure, dict) and isinstance(measure.get("name"), str):
                    measure_owner[measure["name"]] = model
        else:
            metric_graph[str(entry["name"])] = metric_inputs(entry)

    def grounded_measures(name: str, seen: set[str]) -> list[str]:
        if name in seen:
            return []
        seen.add(name)
        measures, metrics = metric_graph.get(name, ([], []))
        grounded = list(measures)
        for metric in metrics:
            grounded.extend(grounded_measures(metric, seen))
        return grounded

    reachable: set[str] = set()
    for name in metric_graph:
        for measure in grounded_measures(name, set()):
            owner = measure_owner.get(measure)
            if owner:
                reachable.add(owner)
    defs.metric_models = sorted(reachable)
    defs.semantic_source = "yaml"


def _flag_stale_manifest(view: DbtProjectView, defs: ProjectDefinitions) -> None:
    """A manifest older than the newest model source describes a project state
    that may no longer exist; note it, never refuse on it."""

    metadata = (view.manifest or {}).get("metadata")
    generated = metadata.get("generated_at") if isinstance(metadata, dict) else None
    if not isinstance(generated, str):
        return
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        return
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)

    root = Path(view.root)
    newest: float | None = None
    for model_path in view.model_paths:
        base = root / model_path
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix not in {".sql", ".yml", ".yaml"} or not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    if newest is None:
        return
    if datetime.fromtimestamp(newest, tz=UTC) > generated_at:
        defs.manifest_stale = True
        defs.notes.append(
            "compiled dbt artifacts are older than the model sources; "
            "declared definitions may lag recent edits"
        )
