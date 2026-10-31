"""Where Dex's dbt runs write artifacts, and where readers look (#862, #1014).

Dex's dbt runtime and a separately pinned project or CI runtime can share a
``target/`` directory. Incompatible dbt versions have left
``target/run/<project>/snapshots/<name>.sql`` as a directory that a later
version then opens as a file (``IsADirectoryError``).

The policy is one setting, ``transform.artifacts``:

``isolated`` (the default)
    Every dbt invocation Dex owns on the real project (the build, the compile
    that prices it, the ``dbt ls`` that selects seeds, the settlement read after
    a failed run) writes to :func:`isolated_artifact_dir`,
    ``<target-path>/dex/<runtime-key>``. The runtime key is the installed
    dbt-core version, so two Dex runs with the same runtime share partial-parse
    state while a different runtime never reads or writes them. The directory sits
    inside the project's own target path, so whatever ignores ``target/`` (git,
    ``dbt clean``, the shadow copies Dex validates in) ignores it too. The
    project's own ``target/`` is never written.

``shared``
    Dex writes the project's own target path, for a caller that needs the
    artifacts where its other tooling looks. Before writing,
    :func:`resolve_build_artifact_dir` refuses a directory another runtime left
    incompatible: a snapshot path that is a directory where dbt expects a file,
    or a manifest written by a different dbt minor version. The refusal names
    the recovery. Nothing is deleted.

Readers do not know which policy wrote last, and do not need to:
:func:`readable_artifact` returns the newer of the file in Dex's directory and
the file in the project's target path. After a Dex build that is Dex's; after
the user's own ``dbt parse`` it is theirs.

:func:`shared_target_dir` resolves the project's target path the way dbt does:
``DBT_TARGET_PATH``, then ``target-path`` in ``dbt_project.yml``, then
``target``. A reader that names an expected invocation id refuses results from
a different invocation. Missing artifacts stay missing. Neither case is a
successful completion.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from ..errors import DexError

#: Relative path under ``target/`` where the file-vs-directory collision has been
#: observed across dbt versions (snapshot compiled SQL).
_SNAPSHOT_RUN_GLOB = "run/*/snapshots"


@dataclass(frozen=True)
class ArtifactProducer:
    """Who wrote the artifacts this evidence is about.

    Paths are relative project labels only. Absolute home directories and
    credential-bearing strings are refused rather than redacted in place, so a
    caller cannot accidentally publish them by formatting this object.
    """

    dbt_version: str | None
    adapter_version: str | None
    project: str | None
    target: str | None
    artifact_directory: str
    invocation_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dbt_version": self.dbt_version,
            "adapter_version": self.adapter_version,
            "project": self.project,
            "target": self.target,
            "artifact_directory": self.artifact_directory,
            "invocation_id": self.invocation_id,
        }


_UNSAFE_PATH = re.compile(r"(^|[/\\])(\.env|credentials|secrets)([/\\]|$)", re.I)


def sanitize_artifact_directory(path: Path | str, *, project_root: Path | str) -> str:
    """Return a path label safe for public evidence.

    Prefer a path relative to ``project_root``. Fall back to the final path
    component. Refuse credential-like segments.
    """

    root = Path(project_root).resolve()
    raw = Path(path)
    try:
        rel = raw.resolve().relative_to(root)
        label = rel.as_posix() or "target"
    except ValueError:
        label = raw.name or "target"
    if _UNSAFE_PATH.search(label) or label.startswith(".."):
        raise ValueError("artifact directory label is not publishable")
    return label


def artifact_producer(
    *,
    project_root: Path | str,
    target: str,
    artifact_directory: Path | str,
    metadata: dict[str, Any] | None = None,
    project_name: str | None = None,
    adapter_version: str | None = None,
) -> ArtifactProducer:
    """Build sanitized producer metadata from dbt run_results metadata + context."""

    meta = metadata or {}
    return ArtifactProducer(
        dbt_version=(str(meta["dbt_version"]) if meta.get("dbt_version") else None),
        adapter_version=(
            str(adapter_version)
            if adapter_version
            else (str(meta["adapter_version"]) if meta.get("adapter_version") else None)
        ),
        project=project_name,
        target=target,
        artifact_directory=sanitize_artifact_directory(
            artifact_directory, project_root=project_root
        ),
        invocation_id=(
            str(meta["invocation_id"]) if meta.get("invocation_id") else None
        ),
    )


@dataclass(frozen=True)
class CollisionFinding:
    """A path that one runtime left as a directory and another expects as a file."""

    path: str
    kind: str  # "directory_where_file_expected"


def find_snapshot_path_collisions(artifact_dir: Path | str) -> list[CollisionFinding]:
    """Detect snapshot compiled-SQL paths that are directories instead of files.

    Models the dbt 1.12.6 (directory) then 1.10.11 (file) collision without
    running either runtime.
    """

    root = Path(artifact_dir)
    if not root.is_dir():
        return []
    found: list[CollisionFinding] = []
    for snapshots_dir in root.glob(f"{_SNAPSHOT_RUN_GLOB}"):
        if not snapshots_dir.is_dir():
            continue
        # A compiled snapshot named ``foo.sql`` that is actually a directory.
        found.extend(
            CollisionFinding(
                path=child.as_posix(),
                kind="directory_where_file_expected",
            )
            for child in snapshots_dir.iterdir()
            if child.name.endswith(".sql") and child.is_dir()
        )
    return found


#: The folder inside the project's target path that holds Dex's per-runtime
#: artifact directories.
DEX_ARTIFACT_FOLDER = "dex"


def runtime_artifact_dir(shared: Path | str, runtime_key: str) -> Path:
    """Per-runtime artifact directory inside ``shared`` (no deletion of siblings).

    ``<shared>/dex/<key>``: dbt writes ``run/``, ``compiled/`` and its JSON
    artifacts at the top of a target path, never under ``dex/``, so another
    runtime using ``shared`` cannot collide with it.
    """

    key = re.sub(r"[^A-Za-z0-9._-]+", "_", runtime_key.strip()) or "runtime"
    return Path(shared) / DEX_ARTIFACT_FOLDER / key


class ArtifactCollisionError(DexError, ValueError):
    """Shared artifact dir has a cross-runtime file/directory collision."""


def resolve_artifact_dir(
    shared: Path | str,
    runtime_key: str,
    *,
    isolate_on_collision: bool = True,
) -> Path:
    """Choose a writable artifact directory for this runtime.

    Same-runtime reuse of ``shared`` is fine when no collision is present.
    On collision, either return an isolated directory inside it (default) or
    refuse. Never recursively deletes caller-owned artifacts.
    """

    shared_path = Path(shared)
    collisions = find_snapshot_path_collisions(shared_path)
    if not collisions:
        shared_path.mkdir(parents=True, exist_ok=True)
        return shared_path
    if not isolate_on_collision:
        names = ", ".join(c.path for c in collisions[:5])
        raise ArtifactCollisionError(
            "incompatible artifacts in shared directory "
            f"(directory where file expected: {names}); "
            "use an isolated artifact directory per runtime"
        )
    isolated = runtime_artifact_dir(shared_path, runtime_key)
    isolated.mkdir(parents=True, exist_ok=True)
    return isolated


@dataclass(frozen=True)
class ArtifactPaths:
    """The artifact files dex reads back from one directory.

    ``directory`` is that directory. ``run_results`` and ``manifest`` are the
    paths inside it, whether or not the files exist yet.
    """

    directory: Path
    run_results: Path
    manifest: Path
    semantic_manifest: Path


def artifact_paths(artifact_dir: Path | str) -> ArtifactPaths:
    """Name the JSON artifacts Dex reads inside ``artifact_dir``."""

    directory = Path(artifact_dir)
    return ArtifactPaths(
        directory=directory,
        run_results=directory / "run_results.json",
        manifest=directory / "manifest.json",
        semantic_manifest=directory / "semantic_manifest.json",
    )


def prepare_artifact_dir(
    project_root: Path | str,
    runtime_key: str,
    *,
    shared_relative: str = "target",
    isolate_on_collision: bool = True,
) -> Path:
    """Resolve the artifact directory for ``runtime_key`` in this project.

    ``shared_relative`` is the caller-owned directory, ``target`` by default.
    The choice is :func:`resolve_artifact_dir`. A clean shared directory is
    reused. A collision isolates or refuses. Files already in the shared
    directory are left where they are.
    """

    return resolve_artifact_dir(
        Path(project_root) / shared_relative,
        runtime_key,
        isolate_on_collision=isolate_on_collision,
    )


def clear_run_results(artifact_dir: Path | str) -> None:
    """Unlink ``run_results.json`` in this directory and nothing else.

    A missing file is a no-op, so a first build and a directory that was never
    written both pass. The rest of the directory, including a previous
    runtime's collision, stays.
    """

    artifact_paths(artifact_dir).run_results.unlink(missing_ok=True)


class ForeignInvocationError(DexError, ValueError):
    """``run_results.json`` belongs to a different invocation than expected."""


def assert_same_invocation(expected_id: str | None, actual_id: str | None) -> None:
    """Raise when both invocation ids are present and they differ.

    A missing artifact has no id, and a reader that did not name one is not
    checking. Neither of those is a foreign invocation. Two different ids are,
    and a successful completion must not be returned from them.
    """

    if expected_id is None or actual_id is None or expected_id == actual_id:
        return
    raise ForeignInvocationError(
        f"run results belong to invocation '{actual_id}', not '{expected_id}'"
    )


# --- the artifact policy (#1014) ---------------------------------------------

ArtifactPolicy = Literal["isolated", "shared"]
ARTIFACT_POLICIES: tuple[str, ...] = ("isolated", "shared")
DEFAULT_ARTIFACT_POLICY: ArtifactPolicy = "isolated"


@functools.lru_cache(maxsize=1)
def dbt_runtime_key() -> str | None:
    """``dbt-<version>`` of the dbt-core Dex runs, or ``None`` without one.

    Read from the installed distribution's metadata, so nothing is imported and
    no subprocess is spawned. The build runs the ``dbt`` beside this interpreter
    (see ``build._dbt_executable``), which is this distribution.
    """

    try:
        return f"dbt-{metadata.version('dbt-core')}"
    except metadata.PackageNotFoundError:
        return None


def shared_target_dir(project_root: Path | str) -> Path:
    """The project's own target path, resolved the way dbt resolves it.

    ``DBT_TARGET_PATH`` wins, then ``target-path`` in ``dbt_project.yml``, then
    ``target``. A relative path is relative to the project. A value carrying
    Jinja cannot be rendered here and falls back to ``target``, which is what a
    reader would have assumed before this function existed.
    """

    project = Path(project_root)
    configured = os.environ.get("DBT_TARGET_PATH") or _yml_target_path(project)
    if not configured or "{{" in configured or "{%" in configured:
        configured = "target"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else project / path


def _yml_target_path(project: Path) -> str | None:
    import yaml

    try:
        document = yaml.safe_load(
            (project / "dbt_project.yml").read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError):
        return None
    value = document.get("target-path") if isinstance(document, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def isolated_artifact_dir(
    project_root: Path | str, runtime_key: str | None = None
) -> Path | None:
    """``<target-path>/dex/<runtime-key>``, or ``None`` with no dbt runtime."""

    key = runtime_key or dbt_runtime_key()
    if key is None:
        return None
    return runtime_artifact_dir(shared_target_dir(project_root), key)


class IncompatibleArtifactsError(ArtifactCollisionError):
    """The shared target path holds artifacts this runtime must not write over."""


def resolve_build_artifact_dir(
    project_root: Path | str,
    policy: str = DEFAULT_ARTIFACT_POLICY,
    *,
    runtime_key: str | None = None,
) -> Path:
    """The directory every Dex-owned dbt invocation on this project writes to.

    ``isolated``: :func:`isolated_artifact_dir`, created if missing. ``shared``:
    the project's target path, after refusing one that holds a cross-runtime
    collision or a manifest from a different dbt minor version. The refusal
    names both ways out and deletes nothing.
    """

    if policy not in ARTIFACT_POLICIES:
        raise ValueError(
            f"transform.artifacts must be one of {', '.join(ARTIFACT_POLICIES)}, "
            f"not '{policy}'"
        )
    key = runtime_key or dbt_runtime_key()
    shared = shared_target_dir(project_root)
    if policy == "isolated" and key is not None:
        isolated = runtime_artifact_dir(shared, key)
        isolated.mkdir(parents=True, exist_ok=True)
        return isolated
    collisions = find_snapshot_path_collisions(shared)
    found = _foreign_manifest_version(shared, key)
    if collisions or found:
        label = sanitize_artifact_directory(shared, project_root=project_root)
        why = (
            f"a snapshot path another dbt runtime left as a directory "
            f"({collisions[0].path.rsplit('/', 1)[-1]})"
            if collisions
            else f"a manifest written by dbt {found}"
        )
        raise IncompatibleArtifactsError(
            f"transform.artifacts is 'shared', and {label}/ holds {why}, which "
            f"this runtime ({key or 'dbt'}) cannot safely write over. Dex deletes "
            f"nothing there. Either run `dbt clean` with the runtime that wrote "
            "it, or unset transform.artifacts so Dex writes to its own "
            f"{label}/{DEX_ARTIFACT_FOLDER}/ directory"
        )
    shared.mkdir(parents=True, exist_ok=True)
    return shared


def _foreign_manifest_version(shared: Path, runtime_key: str | None) -> str | None:
    """The dbt version of a manifest in ``shared`` whose minor differs from ours."""

    import json

    if runtime_key is None:
        return None
    try:
        document = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
        written = str(document["metadata"]["dbt_version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ours = runtime_key.removeprefix("dbt-")
    return None if _minor(written) == _minor(ours) else written


def _minor(version: str) -> tuple[str, ...]:
    return tuple(version.split(".")[:2])


def readable_artifact(project_root: Path | str, name: str) -> Path:
    """The newer of ``name`` in Dex's directory and in the project's target path.

    Readers cannot know which policy, or which tool, wrote last, so they read
    whichever file was written last. With neither present the answer is the
    project's own path, so a reader's "missing" message names the place a user
    would look.
    """

    shared = shared_target_dir(project_root) / name
    isolated_dir = isolated_artifact_dir(project_root)
    candidates = [shared] + ([isolated_dir / name] if isolated_dir else [])
    present: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            present.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not present:
        return shared
    return max(present, key=lambda pair: pair[0])[1]


def readable_artifact_dir(project_root: Path | str) -> Path:
    """The directory holding the manifest :func:`readable_artifact` would pick."""

    return readable_artifact(project_root, "manifest.json").parent
