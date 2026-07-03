"""dbt project bootstrap: the deterministic skeleton behind `transform init`.

Bootstrap is engine work, not agent freehand, because the generated
``profiles.yml`` is safety-relevant: it defines the build targets, and the
dev-target-only invariant depends on its shape. Engine-owned init guarantees a
single ``dev`` default, never a prod-named target, and never a persisted secret,
identically on every agent surface.

Init is strictly additive: it refuses to run where any dbt project already
exists, so propose-don't-impose holds by construction. And unlike the read-only
commands, it never falls back to a default connector: the connector is baked
into the generated profile's ``type:``, and a silent DuckDB default in a
warehouse shop would produce a project that parses and builds locally yet is
wrong. The caller must resolve the connector explicitly before calling in.

Per-connector profile rendering sits behind a small dispatch so cloud renderers
can slot in alongside their dbt adapters without touching the command; today
only DuckDB renders, and the others raise an actionable error.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..cache import DEX_DIR
from ..config import CONFIG_FILE, DexConfig, DuckDBTarget, load_config, save_config
from ..dbt_project import PROFILES_FILE, PROJECT_FILE, discover_projects
from ..diffs import file_diff

VALID_CONNECTORS = ("duckdb", "snowflake", "bigquery", "databricks", "postgres")


class InitError(Exception):
    pass


class InitResult(BaseModel):
    project_name: str
    # Relative to the repo root; also what gets pinned as dbt_project_dir.
    project_dir: str
    connector: str
    created: list[str] = Field(default_factory=list)
    diffs: list[dict[str, Any]] = Field(default_factory=list)


def sanitize_project_name(raw: str) -> str:
    """Reduce free-form input to a valid dbt project name (lowercase,
    underscores, no leading digit)."""

    name = re.sub(r"[^a-z0-9_]+", "_", (raw or "").lower()).strip("_")
    if not name:
        raise InitError(
            "transform init needs a project name, e.g. `transform init analytics`"
        )
    if name[0].isdigit():
        name = f"_{name}"
    return name


def init_project(
    name: str,
    connector: str,
    *,
    path: str | None = None,
    repo_root: Path | str = ".",
) -> InitResult:
    """Render a fresh dbt project skeleton and record the choices in config.

    Creates ``<name>/dbt_project.yml``, ``models/staging/`` and ``models/marts/``
    (kept non-empty with ``.gitkeep``), and a project-local ``profiles.yml`` with
    a single ``dev`` target; then writes ``connector``, ``dbt_project_dir``, and
    ``dbt_target: dev`` back to ``.dex/config.yml`` so the choice is explicit
    once and ambient for every later command. Everything written is returned as
    reviewable diffs.
    """

    root = Path(repo_root)
    if connector not in VALID_CONNECTORS:
        raise InitError(
            f"unknown connector '{connector}'; valid connectors: "
            + ", ".join(VALID_CONNECTORS)
        )
    render_profile = _PROFILE_RENDERERS.get(connector)
    if render_profile is None:
        raise InitError(
            f"connector '{connector}' is not yet supported for init; DuckDB is "
            "the supported path today (`transform init <name> --connector duckdb`)"
        )

    project_name = sanitize_project_name(name)
    existing = discover_projects(root)
    if existing:
        raise InitError(
            "a dbt project already exists ("
            + ", ".join(str(p / PROJECT_FILE) for p in existing)
            + "); init is strictly additive and never touches an existing project"
        )

    config = load_config(root) or DexConfig()
    profiles_text = render_profile(project_name, path, config, root)

    files: dict[str, str] = {
        f"{project_name}/{PROJECT_FILE}": _project_yaml(project_name),
        f"{project_name}/{PROFILES_FILE}": profiles_text,
        f"{project_name}/models/staging/.gitkeep": "",
        f"{project_name}/models/marts/.gitkeep": "",
    }
    collisions = sorted(rel for rel in files if (root / rel).exists())
    if collisions:
        raise InitError(
            "refusing to overwrite existing files: "
            + ", ".join(collisions)
            + "; init only creates, never replaces"
        )

    config.connector = connector
    config.dbt_project_dir = project_name
    config.dbt_target = "dev"

    config_rel = f"{DEX_DIR}/{CONFIG_FILE}"
    config_path = root / config_rel
    old_config = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    )

    diffs = [file_diff(rel, None, content) for rel, content in files.items()]
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    save_config(config, root)
    diffs.append(
        file_diff(config_rel, old_config, config_path.read_text(encoding="utf-8"))
    )

    created = list(files) + ([config_rel] if old_config is None else [])
    return InitResult(
        project_name=project_name,
        project_dir=project_name,
        connector=connector,
        created=created,
        diffs=diffs,
    )


# --- per-connector profile renderers -------------------------------------------


def _duckdb_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-duckdb ``dev`` target wired to the warehouse dex already
    knows. Also records the resolved path in ``config.duckdb`` so later commands
    inherit it."""

    raw = path or (config.duckdb.path if config.duckdb else None)
    if not raw:
        raise InitError(
            "no DuckDB warehouse path to wire the dev target to: pass --path "
            "<warehouse.duckdb> or set duckdb.path in .dex/config.yml "
            "(`explore map --path ...` is the usual first step)"
        )
    warehouse = Path(raw)
    if not warehouse.is_absolute():
        # profiles.yml paths resolve against dbt's working directory, which is
        # not the repo root; an absolute path keeps the target unambiguous.
        warehouse = (root / warehouse).resolve()
    config.duckdb = DuckDBTarget(path=str(warehouse))
    return yaml.safe_dump(
        {
            project_name: {
                "target": "dev",
                "outputs": {"dev": {"type": "duckdb", "path": str(warehouse)}},
            }
        },
        sort_keys=False,
    )


_PROFILE_RENDERERS: dict[str, Callable[[str, str | None, DexConfig, Path], str]] = {
    "duckdb": _duckdb_profile,
}


def _project_yaml(project_name: str) -> str:
    return yaml.safe_dump(
        {
            "name": project_name,
            "version": "1.0.0",
            "profile": project_name,
            "model-paths": ["models"],
        },
        sort_keys=False,
    )
