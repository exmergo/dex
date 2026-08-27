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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .diffs import file_diff
from .errors import ProjectError
from .metricflow_dialect import METRIC_TIME, STANDARD_GRAINS, order_grains
from .semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DIMENSIONS_PER_QUERYABLE_PATH,
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    column_reference,
    derive_entity_type,
    merge_element_fields,
    qualified_dimension,
)

PROJECT_FILE = "dbt_project.yml"
PROFILES_FILE = "profiles.yml"
MANIFEST_PATH = Path("target") / "manifest.json"
SEMANTIC_MANIFEST_PATH = Path("target") / "semantic_manifest.json"

# The ref()/source() call shapes as they appear in model SQL, schema YAML test
# arguments, and semantic-model `model:` fields. Shared by every reader that
# traces a dbt-level name, so they can never disagree on what counts as a ref.
REF_PATTERN = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]")
SOURCE_PATTERN = re.compile(r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")

# dbt project-root files dex may author outside the model paths: the package
# manifests (dependency declarations), the project config, and the connection
# profiles. Each governs the whole project, not one model, which is why the
# editing surface widens by exactly these known names and never to arbitrary
# root files. Kinds are pinned to the specific file each one may target in
# ``transform.plans``; here we only gate containment.
_ALLOWED_ROOT_FILES = frozenset(
    {"packages.yml", "dependencies.yml", PROJECT_FILE, PROFILES_FILE}
)

# What each of dbt's authored path families can hold. Models, snapshots, tests
# and analyses are SQL plus their properties YAML; macros are jinja plus their
# properties YAML; seeds are CSV data plus the YAML that declares their column
# types. The scan is per-family rather than one global suffix filter because
# ".csv" is only a seed under the seed paths: anywhere else it is somebody's
# fixture, and a fixture is not dex's to hash.
_YAML_SUFFIXES = frozenset({".yml", ".yaml"})
_FAMILY_SUFFIXES: dict[str, frozenset[str]] = {
    "model": _YAML_SUFFIXES | {".sql"},
    "macro": _YAML_SUFFIXES | {".sql"},
    "snapshot": _YAML_SUFFIXES | {".sql"},
    "seed": _YAML_SUFFIXES | {".csv"},
    "test": _YAML_SUFFIXES | {".sql"},
    "analysis": _YAML_SUFFIXES | {".sql"},
}


class DbtProjectError(ProjectError):
    """The dbt format's refusal, and the shipped implementation of ``ProjectError``.

    Rooted on the format-neutral base so a caller holding whatever project
    configuration named can catch one type. ``maintain`` is that caller: it reads
    layers through the project tier, and the format on the other side is not
    necessarily this one.
    """


class SourceFile(BaseModel):
    """One editable source file, keyed by its project-relative path."""

    path: str
    content: str
    sha256: str


class DbtProjectView(BaseModel):
    """The in-memory view of a dbt project.

    ``files`` holds the editable surface: the source files under each of dbt's
    authored path families, each scanned for the suffixes that family can hold
    (see :data:`_FAMILY_SUFFIXES`). ``manifest`` is the compiled artifact when
    the project has been compiled; a fresh project loads fine without one.

    A file that is *not* in ``files`` hashes as absent, so a later edit to it
    registers as a create and the apply that follows conflicts on a file nobody
    touched. That is why the scan covers every family dex can author into, not
    only the ones it reads for definitions.
    """

    root: str
    project_name: str
    profile_name: str
    model_paths: list[str] = Field(default_factory=lambda: ["models"])
    macro_paths: list[str] = Field(default_factory=lambda: ["macros"])
    snapshot_paths: list[str] = Field(default_factory=lambda: ["snapshots"])
    seed_paths: list[str] = Field(default_factory=lambda: ["seeds"])
    test_paths: list[str] = Field(default_factory=lambda: ["tests"])
    analysis_paths: list[str] = Field(default_factory=lambda: ["analyses"])
    files: dict[str, SourceFile] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None

    def path_families(self) -> list[tuple[str, list[str]]]:
        """Every authored path family, as ``(name, configured paths)``.

        One place to add the next family, and the order is the order a path is
        matched against when deciding which family it belongs to: the specific
        families first, models last as the catch-all. Two families configured to
        the same directory is a project mistake dex does not adjudicate; the
        first listed wins and the containment message names every family it
        checked.
        """

        return [
            ("macro", list(self.macro_paths)),
            ("snapshot", list(self.snapshot_paths)),
            ("seed", list(self.seed_paths)),
            ("test", list(self.test_paths)),
            ("analysis", list(self.analysis_paths)),
            ("model", list(self.model_paths)),
        ]


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

    # Wrapped rather than left to escape: a project file that is not valid YAML
    # is an unreadable project, which is exactly what `DbtProjectError` means,
    # and `yaml.YAMLError` descends from `Exception` rather than `ValueError`, so
    # it slipped past every caller's handler. `definitions` had to pair the two
    # exceptions itself to keep its never-raises promise; wrapping at the source
    # covers the callers that cannot (`maintain`'s four layer reads, which catch
    # the project family, and `write_edits`, which loads before it writes).
    try:
        raw = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DbtProjectError(f"{project_file} is not valid YAML: {exc}") from exc
    project_name = raw.get("name")
    if not project_name:
        raise DbtProjectError(f"{project_file} has no 'name'")
    model_paths = list(raw.get("model-paths", ["models"]))
    # dbt's own defaults when a key is absent, so a skeleton project's first
    # scaffolded macro, snapshot, or seed lands where dbt will look for it.
    macro_paths = list(raw.get("macro-paths", ["macros"]))
    snapshot_paths = list(raw.get("snapshot-paths", ["snapshots"]))
    seed_paths = list(raw.get("seed-paths", ["seeds"]))
    test_paths = list(raw.get("test-paths", ["tests"]))
    analysis_paths = list(raw.get("analysis-paths", ["analyses"]))

    # Suffixes unioned per directory rather than per family, so a project that
    # points two families at one directory gets both sets scanned instead of
    # whichever happened to be visited last.
    scan: dict[str, set[str]] = {}
    for family, paths in (
        ("model", model_paths),
        ("macro", macro_paths),
        ("snapshot", snapshot_paths),
        ("seed", seed_paths),
        ("test", test_paths),
        ("analysis", analysis_paths),
    ):
        for configured in paths:
            scan.setdefault(configured, set()).update(_FAMILY_SUFFIXES[family])

    files: dict[str, SourceFile] = {}
    for configured, suffixes in scan.items():
        base = root / configured
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in suffixes or not path.is_file():
                continue
            # Posix-separated regardless of OS: every consumer of `files` keys
            # (transform_layer's model-name parsing, scaffolded model paths,
            # this module's own backed_relation_names) assumes "/".
            rel = path.relative_to(root).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # A seed saved in a legacy encoding is the realistic case, and
                # loading the project is a prerequisite for every command,
                # explore included. Skipping the file costs a spurious conflict
                # if someone later edits it through dex; raising would cost them
                # the whole engine.
                continue
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
        snapshot_paths=snapshot_paths,
        seed_paths=seed_paths,
        test_paths=test_paths,
        analysis_paths=analysis_paths,
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
        target_path = contained_path(root, edit.path, view)
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


def contained_path(root: Path, rel_path: str, view: DbtProjectView) -> Path:
    """Resolve an edit path and refuse anything outside the project's editing
    surface.

    Writes are confined to the repo, and within the repo to the dbt editing
    surface: model SQL, schema.yml and semantic YAML under the model paths,
    macros under the macro paths, snapshots under the snapshot paths, seeds
    under the seed paths, singular and generic tests under the test paths,
    analyses under the analysis paths, plus the root manifests dbt keeps at the
    project root. Escapes (absolute paths, ``..``) are refused outright.

    The families are read off the view rather than passed positionally. Four of
    them was where a positional list stopped being readable, and there are six
    now; every caller already holds a view: the writer loads one, and both
    plan-time callers were handed one.
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
    if path_family(root, rel_path, view) is not None:
        return root / candidate
    listed = ", ".join(
        f"{name} ({', '.join(paths) or 'none'})" for name, paths in view.path_families()
    )
    raise DbtProjectError(
        f"edit path '{rel_path}' is outside the project's authored paths "
        f"[{listed}]; dex edits only the dbt project surface"
    )


def path_family(root: Path, rel_path: str, view: DbtProjectView) -> str | None:
    """Which authored family ``rel_path`` falls in, or ``None`` for none of them.

    The other half of containment: containment asks whether a path is inside the
    surface at all, and this asks *which* part of it, which is what decides
    whether the edit's declared kind belongs there. Both answer from the same
    traversal so they can never disagree about where a directory ends.
    """

    resolved = (root / Path(rel_path)).resolve()
    root_resolved = root.resolve()
    for name, paths in view.path_families():
        for configured in paths:
            base = (root_resolved / configured).resolve()
            if resolved == base or base in resolved.parents:
                return name
    return None


def node_files(view: DbtProjectView) -> dict[str, SourceFile]:
    """The files that build a relation dbt names after the file, keyed by path.

    A model, a snapshot and a seed each build such a relation and each is
    ``ref()``-able; a macro, a schema.yml and a semantic YAML build nothing.
    Every derivation that reads "the things this project builds" out of the file
    list goes through here, so widening the load to a new family cannot quietly
    turn its files into models by filename.

    "Node" is the looser word and it is why this function is not called that: a
    singular test *is* a dbt node, and it belongs here no more than a macro
    does, because it builds no relation and nothing can ``ref()`` it. An
    analysis is compiled and never built at all. Both are loaded so they can be
    authored and hashed; neither is a thing this project builds.
    """

    nodes: dict[str, SourceFile] = {}
    for path, source in view.files.items():
        family = path_family(Path(view.root), path, view)
        if (family in ("model", "snapshot") and path.endswith(".sql")) or (
            family == "seed" and path.endswith(".csv")
        ):
            nodes[path] = source
    return nodes


def node_name(path: str) -> str:
    """The dbt node name a node file builds: its stem, case preserved.

    dbt names a model, snapshot or seed after the file, ignoring the
    directories above it, which is why two same-named files in different
    subdirectories are a dbt error rather than two nodes. Case is preserved
    because ``ref()`` matches it; callers comparing against warehouse
    identifiers lower it themselves.
    """

    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


# --- Jinja: what a template calls ---------------------------------------------
#
# One scanner, two policies on top of it. `render_model_sql` refuses anything it
# cannot resolve, because attributing a row delta to SQL dbt never runs would be
# a wrong answer; the reference index reports the same thing as indeterminate,
# because a use it cannot resolve is still a use worth naming. Both need the same
# question answered first, "what does this template call, and with what", which
# is all this does.
#
# It lives here rather than beside either caller because this module is inside
# the zero-extra install and both callers are not: `row_attribution` and the
# reference index reach it, and neither drags the other's dependencies along.

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_REGION = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.DOTALL)
_CALL_START = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_QUOTED_ARG = re.compile(r"^(['\"])([^'\"]*)\1$")


class JinjaCall(BaseModel):
    """One call written inside a jinja region, with its arguments as read.

    An ``args`` entry is the literal string dex read, or ``None`` where the
    argument was anything else: a variable, a concatenation, a nested call, a
    keyword form. ``None`` means *dex did not resolve this*, never *there is
    nothing here*, and every consumer has to keep that distinction: collapsing
    the two is how a reference silently goes missing.

    Nested calls are reported in their own right, so ``{{ ref(var('x')) }}``
    yields both the resolved ``var('x')`` and the unresolved ``ref``. Reporting
    only the outer call would hide the var; reporting only the inner would
    invent a ref that was never written.
    """

    callee: str
    args: tuple[str | None, ...] = ()
    line: int
    #: True when the call is the entire region body, so `{{ ref('x') }}` is
    #: distinguishable from `{{ upper(ref('x')) }}`. A caller substituting the
    #: region for the call's value needs to know the difference.
    spans_region: bool = False
    #: "expression" for `{{ }}`, "statement" for `{% %}`.
    region_kind: str = "expression"
    #: Half-open ``[start, end)`` of the callee name, and of each resolved
    #: argument's *contents* inside its quotes, indexed into the same
    #: comment-masked source :class:`JinjaRegion` offsets are in. A span is
    #: ``None`` wherever the corresponding ``args`` entry is ``None``, since
    #: there is no literal to point at.
    #:
    #: Here so a caller renaming what a call names can splice exactly those
    #: bytes and leave the rest of the template alone. A rewriter working from
    #: ``line`` alone has to re-find the name by searching the line, which picks
    #: the wrong occurrence as soon as a line carries the name twice.
    callee_span: tuple[int, int] | None = None
    arg_spans: tuple[tuple[int, int] | None, ...] = ()


class JinjaRegion(BaseModel):
    """One ``{{ }}`` or ``{% %}`` span, its text, and the calls inside it.

    ``start`` and ``end`` index the *comment-masked* source that
    :func:`jinja_regions` returns alongside these, not the original, so a caller
    splicing regions out works against text where a comment can no longer look
    like code. Masking preserves length and newlines, so offsets and line
    numbers still line up with the file a human opens.
    """

    kind: str
    body: str
    start: int
    end: int
    line: int
    calls: list[JinjaCall] = Field(default_factory=list)


def jinja_regions(content: str) -> tuple[list[JinjaRegion], str]:
    """Every jinja region in ``content``, with the comment-masked source.

    The primitive both jinja readers share. Comments are masked rather than
    deleted so every reported line still matches the file, and string contents
    are masked while structure is scanned so a paren or a comma inside a quoted
    argument cannot be read as syntax; the arguments themselves come from the
    original text.

    A scanner, not a renderer. It never evaluates jinja, so it says what a
    template names, never what it produces. A region carrying no call at all
    (``{{ some_var }}``) is still returned, because "there is jinja here that dex
    did not read" is the fact a caller most needs.
    """

    masked_source = _JINJA_COMMENT.sub(lambda m: _blank_like(m.group(0)), content)
    regions: list[JinjaRegion] = []
    for match in _JINJA_REGION.finditer(masked_source):
        expression = match.group(1)
        body = expression if expression is not None else match.group(2)
        kind = "expression" if expression is not None else "statement"
        regions.append(
            JinjaRegion(
                kind=kind,
                body=body,
                start=match.start(),
                end=match.end(),
                line=masked_source.count("\n", 0, match.start()) + 1,
                calls=[
                    JinjaCall(
                        callee=callee,
                        args=tuple(value for value, _span in arguments),
                        line=masked_source.count("\n", 0, offset) + 1,
                        spans_region=call_text.strip() == body.strip(),
                        region_kind=kind,
                        callee_span=(offset, offset + len(callee)),
                        arg_spans=tuple(span for _value, span in arguments),
                    )
                    for offset, callee, arguments, call_text in sorted(
                        _calls_in(body, match.start() + 2),
                        key=lambda call: call[0],
                    )
                ],
            )
        )
    return regions, masked_source


def jinja_calls(content: str) -> list[JinjaCall]:
    """Every call inside every jinja region of ``content``, in document order."""

    regions, _masked = jinja_regions(content)
    return [call for region in regions for call in region.calls]


#: One argument as read: its literal value (``None`` when dex did not resolve it)
#: and the absolute half-open span of that literal's contents (``None`` likewise).
_Argument = tuple[str | None, tuple[int, int] | None]


def _calls_in(
    body: str, base: int
) -> list[tuple[int, str, tuple[_Argument, ...], str]]:
    """``(callee offset, callee, arguments, call text)``, nested calls included.

    Offsets are absolute: ``base`` is where ``body`` starts in the source the
    caller is indexing into, which is what lets a rewriter splice a name out of
    a nested call without re-finding it by text.
    """

    masked = _mask_strings(body)
    found: list[tuple[int, str, tuple[_Argument, ...], str]] = []
    position = 0
    while True:
        start = _CALL_START.search(masked, position)
        if start is None:
            return found
        open_paren = start.end() - 1
        close = _closing_paren(masked, open_paren)
        if close is None:
            # An unbalanced call is a broken template. Skip past the callee and
            # keep scanning: the rest of the file is still worth reading, and
            # refusing here would make one typo hide every other reference.
            position = start.end()
            continue
        inner = body[open_paren + 1 : close]
        found.append(
            (
                base + start.start(1),
                start.group(1),
                _split_args(
                    inner, masked[open_paren + 1 : close], base + open_paren + 1
                ),
                body[start.start(1) : close + 1],
            )
        )
        found.extend(_calls_in(inner, base + open_paren + 1))
        position = close + 1


def _split_args(inner: str, masked_inner: str, base: int) -> tuple[_Argument, ...]:
    """Top-level arguments of one call, each as its literal value and span.

    ``base`` is where ``inner`` starts in the source being indexed, so a span
    lands on the file a human opens rather than on this slice.
    """

    if not inner.strip():
        return ()
    args: list[_Argument] = []
    depth = 0
    start = 0
    for index, char in enumerate(masked_inner):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(_literal(inner[start:index], base + start))
            start = index + 1
    args.append(_literal(inner[start:], base + start))
    return tuple(args)


def _literal(text: str, base: int) -> _Argument:
    """One argument's literal value and the span of its contents, or two ``None``.

    The span covers what is *between* the quotes, so a rewriter replaces the
    name and leaves the quoting style the author chose alone.
    """

    quoted = _QUOTED_ARG.match(text.strip())
    if quoted is None:
        return None, None
    offset = base + len(text) - len(text.lstrip())
    return quoted.group(2), (offset + quoted.start(2), offset + quoted.end(2))


def _closing_paren(masked: str, open_paren: int) -> int | None:
    depth = 0
    for index in range(open_paren, len(masked)):
        if masked[index] == "(":
            depth += 1
        elif masked[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _mask_strings(text: str) -> str:
    """``text`` with the *contents* of every quoted run blanked, offsets preserved.

    Structure scanning runs over this so a paren, a comma or a quote written
    inside a string cannot be read as syntax. The quotes themselves survive, so
    an argument is still recognisable as a literal.
    """

    out = list(text)
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is None:
            if char in "'\"":
                quote = char
        elif char == quote:
            quote = None
        else:
            out[index] = "\n" if char == "\n" else "x"
    return "".join(out)


def _blank_like(text: str) -> str:
    """``text`` reduced to whitespace, newlines kept so line numbers survive."""

    return "".join("\n" if char == "\n" else " " for char in text)


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


def backed_relation_names(view: DbtProjectView) -> set[str]:
    """Bare table names (lowered) this project currently builds or sources,
    from files and YAML only -- no compiled manifest required, unlike
    ``model_relations`` (empty without one, see ``_declared_from_yaml``).

    Explore uses this (not ``maintain``'s ``transform_layer``/``drift.py``
    logic, which computes the same thing) to keep from importing ``maintain``,
    which already imports ``explore.relationships`` -- the reverse edge would
    risk a cycle.
    """

    names = {node_name(path).lower() for path in node_files(view)}
    for parsed, _path in yaml_documents(view):
        for src in parsed.get("sources") or []:
            if not isinstance(src, dict):
                continue
            for table in src.get("tables") or []:
                if isinstance(table, dict) and table.get("name"):
                    names.add(str(table["name"]).lower())
    return names


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

    The manifest-dict shape of :func:`~.semantic_catalog.column_reference`, whose
    docstring carries the rule. It is a thin adapter rather than a second copy
    because a query backend applies the same rule to a GraphQL payload, and two
    implementations of "the column behind this element" would eventually disagree
    about an expression, which is the direction that makes the PII gate screen the
    wrong column.
    """

    if not isinstance(entry, dict):
        return None
    return column_reference(entry.get("expr"), entry.get("name"))


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


class DeclaredCompositeKey(BaseModel):
    """A model-level ``unique_combination_of_columns`` test: the columns whose
    COMBINATION is unique, never any one of them alone.

    A distinct model from ``DeclaredKey`` rather than a widened ``column``:
    this test has no ``not_null`` variant and a different multiplicity (it is
    the model's own claim about several columns together, not one column's own
    test), so overloading ``column`` to sometimes hold a list would blur two
    different concepts into one field.
    """

    model: str
    relation: str | None = None
    columns: list[str]
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
    column; ``metric_models`` lists models reachable from any metric.
    ``declared_composite_keys`` carries model-level ``unique_combination_of_
    columns`` tests -- a grain declaration a column-level test structurally
    cannot express. ``built_relation_names`` is bare table names (lowered) the
    project builds or sources, from files/YAML alone (populated even with no
    compiled manifest, unlike ``model_relations``) -- explore's orphan-relation
    down-ranking reads this. ``notes`` are analyst-readable caveats for the
    caller's envelope.
    """

    present: bool = False
    project_dir: str | None = None
    manifest_loaded: bool = False
    manifest_stale: bool = False
    relationship_source: str | None = None
    semantic_source: str | None = None
    foreign_keys: list[DeclaredForeignKey] = Field(default_factory=list)
    declared_keys: list[DeclaredKey] = Field(default_factory=list)
    declared_composite_keys: list[DeclaredCompositeKey] = Field(default_factory=list)
    model_relations: dict[str, str] = Field(default_factory=dict)
    primary_entities: dict[str, str] = Field(default_factory=dict)
    metric_models: list[str] = Field(default_factory=list)
    built_relation_names: list[str] = Field(default_factory=list)
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
        # A malformed `dbt_project.yml` is covered: `load` wraps the parser error
        # rather than letting it escape, which is what keeps the never-raises
        # promise in this docstring true. It was not always so, and the failure
        # was invisible from here: `yaml.YAMLError` descends from `Exception`,
        # not `ValueError`, so it went straight past a handler that looked
        # complete.
        return ProjectDefinitions(
            notes=[
                f"dbt project at '{project}' could not be read ({exc}); "
                "declared definitions unavailable"
            ]
        )

    defs = ProjectDefinitions(present=True, project_dir=str(project))
    defs.built_relation_names = sorted(backed_relation_names(view))

    nodes = (view.manifest or {}).get("nodes")
    if isinstance(nodes, dict) and nodes:
        _declared_from_manifest(view.manifest or {}, defs)
    else:
        _declared_from_yaml(view, defs)

    semantic_manifest = _read_semantic_manifest(Path(view.root))
    if semantic_manifest is not None:
        _semantic_from_manifest(semantic_manifest[0], defs)
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
    composite_keys: dict[tuple[str, tuple[str, ...]], DeclaredCompositeKey] = {}
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        if not isinstance(meta, dict):
            continue
        kwargs = meta.get("kwargs")
        kwargs = kwargs if isinstance(kwargs, dict) else {}
        test_name = meta.get("name")
        # A model-level composite-unique test carries no column_name at all
        # (dbt-core's own built-in test and the dbt_utils macro both compile
        # to this same stripped-namespace name), so it must be checked before
        # the column_name gate below -- placed after, it would still hit that
        # gate's `continue` and vanish, exactly today's silent-drop bug.
        if test_name == "unique_combination_of_columns":
            combo = kwargs.get("combination_of_columns")
            if (
                isinstance(combo, list)
                and len(combo) >= 2
                and all(isinstance(c, str) for c in combo)
            ):
                child = attached_name(node)
                if child is not None:
                    dedup_key = (child, tuple(sorted(c.lower() for c in combo)))
                    composite_keys.setdefault(
                        dedup_key,
                        DeclaredCompositeKey(
                            model=child,
                            relation=relations.get(child),
                            columns=list(combo),
                            source="manifest",
                        ),
                    )
            continue
        column = kwargs.get("column_name")
        if not isinstance(column, str) or not column:
            continue
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
    defs.declared_composite_keys = list(composite_keys.values())
    defs.model_relations.update(relations)
    defs.manifest_loaded = True
    defs.relationship_source = "manifest"


def _declared_from_yaml(view: DbtProjectView, defs: ProjectDefinitions) -> None:
    """Column-level tests straight from schema YAML: model-level names only,
    no physical resolution. Model-level relationships tests (declared under the
    model's ``tests:`` with a ``column_name`` kwarg) are a manifest-only shape.

    A ``unique_combination_of_columns`` test is itself a model-level construct
    (it names no single column), so it is read from the model's own ``tests:``/
    ``data_tests:`` block -- sibling to ``columns:``, never reached by the
    column loop below.
    """

    keys: dict[tuple[str, str], DeclaredKey] = {}
    composite_keys: dict[tuple[str, tuple[str, ...]], DeclaredCompositeKey] = {}
    for parsed, _path in yaml_documents(view):
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            model = entry["name"]
            for test in entry.get("tests") or entry.get("data_tests") or []:
                if not isinstance(test, dict):
                    continue
                combo_cfg = next(
                    (
                        cfg
                        for name, cfg in test.items()
                        if name == "unique_combination_of_columns"
                        or name.endswith(".unique_combination_of_columns")
                    ),
                    None,
                )
                if combo_cfg is None:
                    continue
                combo_cfg = combo_cfg if isinstance(combo_cfg, dict) else {}
                combo = combo_cfg.get("combination_of_columns")
                if (
                    isinstance(combo, list)
                    and len(combo) >= 2
                    and all(isinstance(c, str) for c in combo)
                ):
                    dedup_key = (model, tuple(sorted(c.lower() for c in combo)))
                    composite_keys.setdefault(
                        dedup_key,
                        DeclaredCompositeKey(
                            model=model, columns=list(combo), source="yaml"
                        ),
                    )
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
    defs.declared_composite_keys = list(composite_keys.values())
    defs.relationship_source = "yaml"
    if defs.foreign_keys:
        defs.notes.append(
            "declared joins read from schema YAML (project not compiled); "
            "physical resolution is name-based"
        )


def _read_semantic_manifest(project: Path) -> tuple[dict[str, Any], str] | None:
    """``target/semantic_manifest.json`` as ``(payload, raw text)`` when present
    and carrying semantic models; an empty or unreadable artifact falls back to
    raw YAML.

    The raw text comes back beside the parsed payload because MetricFlow's own
    manifest parser takes the document rather than a dict, and reading a large
    artifact twice for one command is work for nothing.
    """

    path = project / SEMANTIC_MANIFEST_PATH
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("semantic_models"):
        return payload, text
    return None


@dataclass(frozen=True)
class ResolvedPath:
    """One token a metric query may group by, and what it reaches.

    The unit a join-resolved read produces: ``user__pricing_tier`` is a
    ``pricing_tier`` declared in the ``users`` model, reached through the ``user``
    entity. ``definition`` and ``semantic_model`` are what let a caller see that
    several paths reach one declaration, and they are left unset rather than
    guessed where a path is reachable from more than one declaring model.
    """

    token: str
    definition: str | None = None
    semantic_model: str | None = None
    type: str = ""
    grains: tuple[str, ...] = ()


def resolve_group_by_paths(manifest_text: str) -> dict[str, list[ResolvedPath]] | None:
    """Per metric, every dimension path a query may group by, or None.

    Asks MetricFlow, which owns the join-resolution rules this catalog would
    otherwise have to restate: which models a metric's measures live in, which
    entities those models share, and how far a dimension can be reached through
    them. A hand-rolled single-hop union under-reports a layer with joins, and the
    dimension it drops is often the one every metric description tells a caller to
    group by.

    **None means the join graph could not be resolved**, which is the whole reason
    this is separated from the manifest read. ``explore semantic list --local`` is
    a dependency-free read of a compiled artifact, and it stays one: an install
    that picked no extras gets the declared single-hop view and a payload that says
    so. Returning None rather than raising is what makes that a declared
    degradation instead of a failure.

    A manifest the resolver refuses degrades the same way, on purpose. The
    resolver validates the whole artifact against its own schema, so a manifest
    written by a different version of dbt can fail a read that dex itself performed
    without trouble, and losing the catalog over the joins is the worse of the two
    outcomes. The absence is declared either way, in ``dimension_scope`` before any
    note, so a caller can tell a resolved list from an unresolved one.

    One lookup builds the whole answer, because constructing it parses the
    manifest. Date-part specs (``extract(year from ...)``) are skipped: they are
    real, and their queryable spelling is not a ``__``-joined token, so emitting
    one would name something no query accepts.
    """

    try:
        from metricflow_semantics.model.dbt_manifest_parser import (
            parse_manifest_from_dbt_generated_manifest,
        )
        from metricflow_semantics.model.semantic_manifest_lookup import (
            SemanticManifestLookup,
        )
    except ImportError:
        return None

    try:
        return _resolve_through(
            SemanticManifestLookup(
                parse_manifest_from_dbt_generated_manifest(manifest_text)
            ).metric_lookup
        )
    except Exception:
        # Broad, and around the resolution rather than only the parse: this is a
        # third-party validator and resolver over an artifact dex did not write,
        # and every way either of them can refuse one means the same thing here.
        return None


def _resolve_through(lookup: Any) -> dict[str, list[ResolvedPath]]:
    """The resolution itself, given MetricFlow's metric lookup.

    Apart from :func:`resolve_group_by_paths` only so that its whole body, and not
    just the parse, sits inside one refusal-means-degrade boundary.
    """

    resolved: dict[str, list[ResolvedPath]] = {}
    for reference in lookup.metric_references:
        found: dict[str, dict[str, Any]] = {}
        for spec in lookup.get_common_group_by_items(
            metric_references=(reference,)
        ).annotated_specs:
            kind = spec.element_type.name
            if (
                kind not in ("DIMENSION", "TIME_DIMENSION")
                or spec.date_part is not None
            ):
                continue
            token = "__".join([*spec.entity_link_names, spec.element_name])
            entry = found.setdefault(
                token, {"grains": [], "time": False, "models": set(), "name": None}
            )
            entry["time"] = entry["time"] or kind == "TIME_DIMENSION"
            entry["name"] = spec.element_name
            entry["models"].update(spec.origin_semantic_model_names)
            grain = getattr(spec.time_grain, "name", None)
            if grain and grain not in entry["grains"]:
                # The resolver names a custom granularity here the same way it
                # names a standard one, so this is also how a deployment's own
                # granularities reach the catalog.
                entry["grains"].append(grain)

        paths: list[ResolvedPath] = []
        for token, entry in sorted(found.items()):
            models = sorted(entry["models"])
            synthesized = token == METRIC_TIME
            paths.append(
                ResolvedPath(
                    token=token,
                    # dex's own token, not a declaration: it resolves per metric,
                    # so attributing it to one model's dimension would be a claim
                    # the layer does not make.
                    definition=None if synthesized else entry["name"],
                    semantic_model=(
                        None if synthesized or len(models) != 1 else models[0]
                    ),
                    type="time" if entry["time"] else "categorical",
                    grains=tuple(order_grains(entry["grains"])),
                )
            )
        resolved[reference.element_name] = paths
    return resolved


def _grains_from(base: str | None, custom: tuple[str, ...] = ()) -> list[str] | None:
    """The grains a time column declared at ``base`` can be queried at.

    A daily column can be rolled up to a week or a year and cannot be split into
    an hour, so the answer is the standard grains at or coarser than the declared
    one. None where nothing was declared, because "any grain" and "we do not know"
    are different answers and only one of them should read as complete.
    """

    if not base or base.lower() not in STANDARD_GRAINS:
        return None
    floor = STANDARD_GRAINS.index(base.lower())
    return [*STANDARD_GRAINS[floor:], *custom]


@dataclass
class _LayerIndex:
    """What a metric needs to know about the layer around it.

    Passed as one object rather than as five maps threaded through a signature:
    every one of them is derived in the same pass over the semantic models, and a
    metric reads whichever of them its own type happens to need.
    """

    measure_owner: dict[str, str]
    measure_agg_time: dict[str, str | None]
    model_dimensions: dict[str, list[str]]
    dimension_grains: dict[tuple[str, str], list[str] | None]
    resolved: dict[str, list[ResolvedPath]] | None = None


def semantic_catalog(
    project: Path,
    *,
    resolve_paths: Callable[[str], dict[str, list[ResolvedPath]] | None] | None = None,
) -> SemanticCatalogView:
    """The project's semantic layer as a read catalog.

    The compiled ``target/semantic_manifest.json`` is the source rather than the
    authored YAML, and the two are not interchangeable here. The manifest has
    already resolved what a reader would otherwise have to reconstruct: a
    ratio or derived metric's ``input_measures`` all the way down to the
    aggregations it really reads, each semantic model's physical relation, and
    the inherited defaults. The YAML fingerprint ``maintain`` takes is the
    opposite trade on purpose, hashing exactly what the author wrote so a
    baseline survives a dbt upgrade; a catalog wants the resolution.

    Takes the project directory rather than a loaded view because the compiled
    artifact is the only file it reads: loading the view would scan and hash every
    authored file in the project to answer a question none of them can.

    Raises :class:`DbtProjectError` when the project has no compiled semantic
    manifest, because an empty catalog and an uncompiled project are different
    answers and only one of them is fixed by running ``dbt parse``. The caller
    adds whatever alternative it can offer.

    ``resolve_paths`` is how the join graph gets resolved, defaulting to
    :func:`resolve_group_by_paths`. It is injected rather than called directly so
    that both halves of the contract are reachable in a test: the resolved read,
    and the declared single-hop read an install without the ``[semantic]`` extra
    gets. A resolver that answers None is the second of those, and the catalog
    declares it rather than letting a short list read as the whole layer.
    """

    read = _read_semantic_manifest(project)
    if read is None:
        raise DbtProjectError(
            "no compiled semantic manifest at target/semantic_manifest.json; run "
            "`dbt parse` in the project so the semantic layer can be read"
        )
    manifest, manifest_text = read
    resolved = (resolve_paths or resolve_group_by_paths)(manifest_text)

    models: list[SemanticModelInfo] = []
    measures: list[MeasureInfo] = []
    dimensions: dict[str, dict[str, Any]] = {}
    entity_roles: dict[str, list[EntityRole]] = {}
    entity_words: dict[str, dict[str, Any]] = {}
    physical: dict[str, tuple[str, str]] = {}
    model_dimensions: dict[str, list[str]] = {}
    measure_owner: dict[str, str] = {}
    measure_agg_time: dict[str, str | None] = {}
    dimension_grains: dict[tuple[str, str], list[str] | None] = {}
    # (semantic model, bare dimension name) -> the physical column behind it, so a
    # join-resolved path can be given the same column resolution a declared token
    # gets. Without it the PII gate would fall back to the name heuristic on every
    # token the join resolution added, which is the weaker screening.
    columns_by_definition: dict[tuple[str, str], tuple[str, str]] = {}
    # (semantic model, bare dimension name) -> what the project says about it and
    # which column it sits on, so a path that reaches a declaration carries the
    # same words and the same physical column the declaration does. The hosted API
    # returns the words on every path it names, and a caller comparing the two
    # backends should not find one of them silent.
    words_by_definition: dict[tuple[str, str], dict[str, Any]] = {}
    custom_grains = _custom_granularities(manifest)

    for entry in manifest.get("semantic_models") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        model_name = entry["name"]
        node_relation = entry.get("node_relation")
        node_relation = node_relation if isinstance(node_relation, dict) else {}
        defaults = entry.get("defaults")
        defaults = defaults if isinstance(defaults, dict) else {}
        agg_time = defaults.get("agg_time_dimension")
        relation = node_relation.get("relation_name")
        relation = _strip_relation_quoting(str(relation)) if relation else None

        declared_entities = [
            e for e in entry.get("entities") or [] if isinstance(e, dict)
        ]
        primary = next(
            (
                e.get("name")
                for e in declared_entities
                if str(e.get("type", "")).lower() == "primary"
            ),
            None,
        )
        for element in declared_entities:
            name = element.get("name")
            if not isinstance(name, str):
                continue
            entity_roles.setdefault(name, []).append(
                EntityRole(
                    semantic_model=model_name,
                    type=str(element.get("type") or "").lower(),
                    expr=element.get("expr"),
                    role=element.get("role"),
                    description=element.get("description"),
                    column=physical_column(element),
                )
            )
            # An entity's words are the same wherever it is declared, so the first
            # model that wrote any wins; its per-model caveats live on the role.
            words = entity_words.setdefault(name, {})
            for field_name in ("label", "description"):
                if words.get(field_name) is None:
                    words[field_name] = element.get(field_name)

        qualified: list[str] = []
        for element in entry.get("dimensions") or []:
            if not isinstance(element, dict) or not isinstance(
                element.get("name"), str
            ):
                continue
            bare = element["name"]
            token = qualified_dimension(primary, bare)
            qualified.append(token)
            kind = str(element.get("type") or "").lower()
            element_params = element.get("type_params")
            element_params = element_params if isinstance(element_params, dict) else {}
            # A categorical dimension gets an empty list rather than nothing: "no
            # grain applies here" is an answer, and it is the one that stops a
            # caller asking for a grain the dimension could never have.
            grains = (
                _grains_from(element_params.get("time_granularity"), custom_grains)
                if kind == "time"
                else []
            )
            dimension_grains[(model_name, bare)] = grains
            words_by_definition[(model_name, bare)] = {
                "type": kind,
                "label": element.get("label"),
                "description": element.get("description"),
                "column": physical_column(element),
            }
            merge_element_fields(
                dimensions,
                token,
                {
                    "type": element.get("type"),
                    "label": element.get("label"),
                    "description": element.get("description"),
                    "definition": bare,
                    "semantic_model": model_name,
                    "queryable_granularities": grains,
                    "column": physical_column(element),
                },
            )
        model_dimensions[model_name] = qualified

        for element in entry.get("measures") or []:
            if not isinstance(element, dict) or not isinstance(
                element.get("name"), str
            ):
                continue
            measure_owner[element["name"]] = model_name
            measure_agg_time[element["name"]] = (
                element.get("agg_time_dimension") or agg_time
            )
            measures.append(
                MeasureInfo(
                    name=element["name"],
                    agg=str(element.get("agg") or "").lower() or None,
                    expr=element.get("expr"),
                    # Resolved, not verbatim: a measure with no time dimension of
                    # its own uses the model's default, which is the value
                    # MetricFlow aggregates by and therefore the one a caller
                    # needs. Reporting the null instead would make the field mean
                    # "unknown" on the majority of a well-configured layer.
                    agg_time_dimension=element.get("agg_time_dimension") or agg_time,
                    label=element.get("label"),
                    description=element.get("description"),
                    semantic_model=model_name,
                    column=physical_column(element),
                )
            )

        if relation:
            for element in [*(entry.get("dimensions") or []), *declared_entities]:
                column = physical_column(element)
                name = element.get("name") if isinstance(element, dict) else None
                if not column or not isinstance(name, str):
                    continue
                columns_by_definition[(model_name, name)] = (relation, column)
                for token in {name, qualified_dimension(primary, name)}:
                    physical.setdefault(token, (relation, column))

        models.append(
            SemanticModelInfo(
                name=model_name,
                label=entry.get("label"),
                description=entry.get("description"),
                model_ref=node_relation.get("alias")
                or _parse_relation_ref(str(entry.get("model", ""))),
                agg_time_dimension=agg_time,
                primary_entity=primary or entry.get("primary_entity"),
                relation=relation,
            )
        )

    # dex's own synthesis rather than a manifest entry, so it carries no label,
    # description or owning model: every word in the catalog is the project's.
    dimensions.setdefault(METRIC_TIME, {"type": "time"})

    # A join-resolved path is a row of its own, folded in beside the declarations
    # so a dimension the project declares stays visible even when no metric can
    # reach it (which is exactly what a hosted read cannot see). The declarations
    # were folded first, so the project's own words win and the resolution only
    # fills in what it alone knows.
    for paths in (resolved or {}).values():
        for path in paths:
            declaration = (path.semantic_model, path.definition)
            words = words_by_definition.get(declaration, {})
            merge_element_fields(
                dimensions,
                path.token,
                {
                    "type": words.get("type") or path.type,
                    "label": words.get("label"),
                    "description": words.get("description"),
                    "definition": path.definition,
                    "semantic_model": path.semantic_model,
                    "queryable_granularities": list(path.grains),
                    "column": words.get("column"),
                },
            )
            column = columns_by_definition.get(declaration)
            if column is not None:
                physical.setdefault(path.token, column)

    index = _LayerIndex(
        measure_owner=measure_owner,
        measure_agg_time=measure_agg_time,
        model_dimensions=model_dimensions,
        dimension_grains=dimension_grains,
        resolved=resolved,
    )
    metrics = [
        _metric_info(entry, index)
        for entry in manifest.get("metrics") or []
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]

    return SemanticCatalogView(
        semantic_models=sorted(models, key=lambda m: m.name),
        metrics=metrics,
        dimensions=[
            DimensionInfo(
                name=name,
                type=fields.get("type") or "",
                label=fields.get("label"),
                description=fields.get("description"),
                definition=fields.get("definition"),
                semantic_model=fields.get("semantic_model"),
                queryable_granularities=fields.get("queryable_granularities"),
                column=fields.get("column"),
            )
            for name, fields in sorted(dimensions.items())
        ],
        entities=[
            EntityInfo(
                name=name,
                type=derive_entity_type(roles),
                label=entity_words.get(name, {}).get("label"),
                description=entity_words.get(name, {}).get("description"),
                roles=roles,
            )
            for name, roles in sorted(entity_roles.items())
        ],
        measures=sorted(measures, key=lambda m: m.name),
        dimension_scope=(
            DIMENSIONS_PER_QUERYABLE_PATH if resolved else DIMENSIONS_PER_DECLARATION
        ),
        notes=_catalog_notes(metrics),
        physical_columns=physical,
    )


def _custom_granularities(manifest: dict[str, Any]) -> tuple[str, ...]:
    """The granularities this project declares on its own time spines.

    dbt lets a project define granularities of its own (a fiscal quarter, a
    retail week) on the time spine, and they are queryable exactly like a standard
    grain. A fixed list cannot contain them, which is half of why validating a
    grain against one was wrong.
    """

    configuration = manifest.get("project_configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    found: list[str] = []
    for spine in configuration.get("time_spines") or []:
        if not isinstance(spine, dict):
            continue
        for granularity in spine.get("custom_granularities") or []:
            name = (
                granularity.get("name")
                if isinstance(granularity, dict)
                else granularity
            )
            if isinstance(name, str) and name not in found:
                found.append(name)
    return tuple(found)


def _catalog_notes(metrics: list[MetricInfo]) -> list[str]:
    """What this read of the layer has to say about the layer.

    One thing, and it is the thing a caller reading the lists alone gets wrong: a
    metric whose measures aggregate over different time columns has no single time
    axis, so grouping it by the layer's time token buckets part of the number by
    one timestamp and the rest by another, invisibly, in a result that looks like
    any other.

    What the *read* could not do (a join graph left unresolved because the resolver
    is absent) is said by the surface rather than here, because the alternatives it
    has to offer are that surface's own.
    """

    disagreeing = sorted(m.name for m in metrics if len(m.time_axis or ()) > 1)
    if not disagreeing:
        return []
    return [
        f"{', '.join(disagreeing)} aggregate over more than one time column "
        "(see time_axis): grouping by metric_time uses each measure's own, so "
        "the parts of one number can be bucketed by different timestamps"
    ]


def _metric_info(entry: dict[str, Any], index: _LayerIndex) -> MetricInfo:
    """One metric: what it is built from, what it can be grouped by, and what a
    time grouping on it resolves to.

    Groupable dimensions come from the join resolver where it ran, which is the
    same answer a hosted read gives for the same layer. Without it they are the
    dimensions of the semantic models owning the metric's input measures,
    entity-qualified and single-hop, which under-reports a layer with joins; the
    catalog says so in a note rather than letting the shorter list read as
    complete.

    ``time_axis`` is read per input measure rather than per metric, because that
    is where the disagreement lives: a ratio whose two sides sit in different
    models aggregates over each model's own time column, so a single value would
    be right about half the number.
    """

    params = entry.get("type_params")
    params = params if isinstance(params, dict) else {}

    def named(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            return value["name"]
        return None

    input_measures = [
        name
        for name in (named(m) for m in params.get("input_measures") or [])
        if name is not None
    ]
    owners = sorted(
        {index.measure_owner[m] for m in input_measures if m in index.measure_owner}
    )
    resolved = (index.resolved or {}).get(entry["name"])
    if resolved is not None:
        groupable = {path.token for path in resolved}
    else:
        groupable = {METRIC_TIME}
        for owner in owners:
            groupable.update(index.model_dimensions.get(owner, []))

    time_axis: list[str] = []
    axis_grains: list[list[str] | None] = []
    for measure in input_measures:
        axis = index.measure_agg_time.get(measure)
        if not axis:
            continue
        if axis not in time_axis:
            time_axis.append(axis)
        axis_grains.append(
            index.dimension_grains.get((index.measure_owner.get(measure, ""), axis))
        )
    # The metric's floor is its coarsest axis, so the grains it can be queried at
    # are the ones every axis can serve. One unknown axis makes the whole answer
    # unknown rather than optimistic.
    granularities: list[str] | None = None
    if axis_grains and all(axis_grains):
        granularities = [
            grain
            for grain in axis_grains[0] or []
            if all(grain in (other or []) for other in axis_grains[1:])
        ]
    if resolved is not None:
        # The resolver states this outright, so prefer it over the derivation.
        from_resolver = next(
            (path.grains for path in resolved if path.token == METRIC_TIME), ()
        )
        granularities = list(from_resolver) or granularities

    input_metrics = [
        name
        for name in (named(m) for m in params.get("metrics") or [])
        if name is not None
    ]
    composition = MetricComposition(
        measure=named(params.get("measure")),
        numerator=named(params.get("numerator")),
        denominator=named(params.get("denominator")),
        expr=params.get("expr"),
        input_metrics=input_metrics or None,
    )

    return MetricInfo(
        name=entry["name"],
        type=str(entry.get("type") or "").lower(),
        label=entry.get("label"),
        description=entry.get("description"),
        dimensions=sorted(groupable),
        semantic_models=owners or None,
        input_measures=input_measures or None,
        composition=composition,
        filter=_where_template(entry.get("filter")),
        time_axis=sorted(time_axis) or None,
        queryable_granularities=granularities,
        vendor_params=_metricflow_params(params, str(entry.get("type") or "").lower()),
    )


def _where_template(value: Any) -> str | None:
    """A filter's SQL template, from either spelling the artifacts use.

    dbt has carried a metric filter as a single ``where_sql_template`` and as a
    ``where_filters`` list across versions, and the hosted API returns the single
    form. Several templates join with ``AND``, which is how MetricFlow itself
    combines them.
    """

    if isinstance(value, str):
        return value or None
    if not isinstance(value, dict):
        return None
    single = value.get("where_sql_template")
    if isinstance(single, str) and single:
        return single
    templates = [
        f.get("where_sql_template")
        for f in value.get("where_filters") or []
        if isinstance(f, dict) and isinstance(f.get("where_sql_template"), str)
    ]
    return " AND ".join(t for t in templates if t) or None


def _metricflow_params(
    params: dict[str, Any], metric_type: str
) -> dict[str, Any] | None:
    """The parts of a metric's definition that only mean something under
    MetricFlow, under one key rather than promoted into the neutral core.

    A cumulative window, a grain-to-date, a derived metric's per-input offset
    window and whether the metric can be queried at all without a time dimension
    are real and worth carrying; they are also this vendor's semantics, and a
    consumer needs to be able to tell that without a lookup table.

    ``requires_metric_time`` is derived rather than read, because the compiled
    artifact does not carry it: a metric that accumulates or offsets its window
    has no meaning without a time axis to accumulate or offset along. It is
    written only when true, so an absent key means false and 27 metrics do not
    each pay for a false.
    """

    def window(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        count, granularity = value.get("count"), value.get("granularity")
        if count is None and granularity is None:
            return None
        return {"count": count, "granularity": granularity}

    # dbt has moved a cumulative metric's window and grain-to-date under their own
    # key and kept mirroring them at the top level; read both so the fields do not
    # silently vanish on a newer project.
    cumulative_params = params.get("cumulative_type_params")
    cumulative_params = cumulative_params if isinstance(cumulative_params, dict) else {}

    vendor: dict[str, Any] = {}
    cumulative = window(params.get("window")) or window(cumulative_params.get("window"))
    if cumulative is not None:
        vendor["window"] = cumulative
    grain_to_date = params.get("grain_to_date") or cumulative_params.get(
        "grain_to_date"
    )
    if grain_to_date:
        vendor["grain_to_date"] = grain_to_date
    offsets = {}
    for input_metric in params.get("metrics") or []:
        if not isinstance(input_metric, dict):
            continue
        offset = window(input_metric.get("offset_window"))
        if offset is not None and isinstance(input_metric.get("name"), str):
            offsets[input_metric["name"]] = offset
    if offsets:
        vendor["offset_windows"] = offsets
    offset_to_grain = any(
        isinstance(x, dict) and x.get("offset_to_grain")
        for x in params.get("metrics") or []
    )
    if metric_type == "cumulative" or offsets or offset_to_grain:
        vendor["requires_metric_time"] = True
    return vendor or None


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
