"""Complete project setup shared by terminal, agents and library callers.

Discovery and rendering precede every write. Setup checks connectivity, never
explores tables, builds a model, or decides how much a person may spend.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import Field

from . import envelope as env
from .config import DexConfig, load_config
from .config_document import ConfigDocument
from .dbt_project import (
    discover_projects,
    resolve_target,
    target_identifiers,
    write_project_file,
)
from .diffs import file_diff
from .errors import RequestError
from .onboarding_docs import (
    ENTERPRISE_DESCRIPTION,
    ENTERPRISE_URL,
    README_BEGIN,
    README_END,
    agent_document,
    managed,
    readme_body,
    skills_status,
)
from .results import ConfirmationRequest, Result, to_envelope
from .values_free import require_allowed

if TYPE_CHECKING:
    from .engine import DexEngine

MODES = ("new", "demo", "adopt", "explore")
STATE = ".dex/setup.json"


class SetupResult(Result):
    mode: str | None = None
    project_dir: str | None = None
    connector: str | None = None
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    missing_inputs: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    readiness: dict[str, Any] = Field(default_factory=dict)
    next_steps: list[dict[str, str]] = Field(default_factory=list)
    skills: dict[str, Any] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    connection: dict[str, Any] = Field(default_factory=dict)
    failure_reason: env.Reason | None = None

    def data(self) -> dict[str, Any]:
        return self.model_dump(
            exclude={
                "cost",
                "notes",
                "warnings",
                "diffs",
                "pending_confirmation",
                "pending_offer",
                "spend",
                "result_mode",
                "session_ceiling_cap",
                "connection",
            }
        ) | {
            "enterprise": {"description": ENTERPRISE_DESCRIPTION, "url": ENTERPRISE_URL}
        }


def setup_envelope(result: SetupResult) -> env.Envelope:
    envelope = to_envelope(result)
    if result.pending_confirmation is None and (
        result.missing_inputs or result.problems
    ):
        envelope.status = env.Status.ERROR
        envelope.reason = result.failure_reason or env.Reason.REQUEST
        envelope.errors = result.problems or [
            f"provide {row['name']}: {row['detail']}" for row in result.missing_inputs
        ]
    if result.connection:
        envelope.connection = env.Connection.model_validate(result.connection)
    return envelope


def _read(path: Path) -> str | None:
    return path.read_bytes().decode("utf-8") if path.is_file() else None


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RequestError("project selection must stay inside the repository")
    return resolved.relative_to(root).as_posix()


def discover(root: Path) -> dict[str, Any]:
    root = root.resolve()
    config = load_config(root)
    projects = [_relative(root, p) for p in discover_projects(root)]
    if config and config.dbt_project_dir:
        pinned = _relative(root, root / config.dbt_project_dir)
        if (root / pinned / "dbt_project.yml").is_file() and pinned not in projects:
            projects.append(pinned)
    semantic = (
        config.semantic.vendor
        if config and "semantic" in config.model_fields_set
        else None
    )
    # Recognizable authored formats only; detection never selects a vendor or
    # rewrites definitions. A standalone layer is not an invitation to add dbt.
    semantic_files = []
    for directory in (root, root / "semantic", root / "models", root / "cubes"):
        if directory.is_dir():
            semantic_files.extend(
                _relative(root, path)
                for path in directory.iterdir()
                if path.is_file()
                and (
                    path.name.endswith(".ossie.json")
                    or path.name
                    in {"slayer.yml", "slayer.yaml", "cube.yml", "cube.yaml"}
                )
            )
    return {
        "projects": sorted(projects),
        "semantic": semantic,
        "semantic_files": semantic_files,
        "agent_files": [p for p in ("AGENTS.md", "CLAUDE.md") if (root / p).is_file()],
    }


def _config_text(root: Path, config: DexConfig, before: str | None) -> str:
    """Splice only chosen fields; never materialize defaults or budget choices."""
    if before is None:
        return yaml.safe_dump(
            config.model_dump(mode="json", exclude_unset=True, exclude_none=True),
            sort_keys=False,
        )
    document = ConfigDocument(before, repo_root=root)
    values = config.model_dump(mode="json", exclude_unset=True, exclude_none=True)

    def amend(prefix: str, items: dict) -> None:
        nonlocal document
        for key, value in items.items():
            full = f"{prefix}.{key}" if prefix else key
            if key == "budget" and not prefix:
                continue
            if isinstance(value, dict):
                amend(full, value)
            elif document.written(full) != value:
                document = document.with_value(full, value)

    amend("", values)
    return document.text


def _adopt(
    config: DexConfig,
    root: Path,
    project_dir: str,
    target: str | None,
    connector: str | None,
) -> tuple[str | None, list[str]]:
    from .connect import _profile_scalar
    from .transform.build import assert_dev_target

    config.dbt_project_dir = project_dir
    chosen = target or config.dbt_target or "dev"
    if target is None and config.dbt_target is None:
        with contextlib.suppress(Exception):
            default = resolve_target(root / project_dir)
            assert_dev_target(default.name, default.name)
            chosen = default.name
    problems = []
    try:
        resolved = resolve_target(root / project_dir, chosen)
        assert_dev_target(resolved.name, chosen)
        if connector and connector != resolved.type:
            raise RequestError(
                f"connector {connector} conflicts with existing profile type "
                f"{resolved.type}; select the matching connector"
            )
        connector = resolved.type
        config.dbt_target = resolved.name
        fields = target_identifiers(root / project_dir, resolved.name)
        mapping = {
            "bigquery": {"project": "project", "dataset": "dev_dataset"},
            "snowflake": {
                "database": "dev_database",
                "schema": "dev_schema",
                "warehouse": "warehouse",
            },
            "databricks": {"catalog": "dev_catalog", "schema": "dev_schema"},
            "postgres": {"schema": "dev_schema"},
            "redshift": {"schema": "dev_schema"},
            "clickhouse": {"schema": "dev_database"},
            "trino": {"database": "dev_catalog", "schema": "dev_schema"},
            "duckdb": {"path": "path"},
        }
        if connector not in mapping:
            raise RequestError(f"profile connector {connector} is not supported by Dex")
        existing = getattr(config, connector)
        block = (
            existing.model_dump(exclude_none=True, exclude_unset=True)
            if existing
            else {}
        )
        for profile_key, config_key in mapping[connector].items():
            value = _profile_scalar(fields.get(profile_key), os.environ)
            if value and config_key not in block:
                if connector == "duckdb":
                    value = os.path.relpath(
                        (root / project_dir / value).resolve(), root
                    )
                block[config_key] = value
        raw = config.model_dump(mode="json", exclude_unset=True, exclude_none=True)
        raw[connector] = block
        raw["connector"] = connector
        replacement = DexConfig.model_validate(raw)
        setattr(config, connector, getattr(replacement, connector))
    except RequestError:
        raise
    except Exception as exc:
        # Profile/domain errors are safe only after the central redactor.
        problems.append(env.redact(str(exc)))
    return connector, problems


def initialize(
    engine: DexEngine,
    name: str | None = None,
    *,
    mode: str | None = None,
    project_dir: str | None = None,
    target: str | None = None,
    dev_destination: str | None = None,
    agent_files: list[str] | None = None,
    no_agent_instructions: bool = False,
    layered_schemas: bool = False,
    in_place: bool = False,
) -> SetupResult:
    require_allowed(engine.result_mode, "init")
    from .transform.init import VALID_CONNECTORS, prepare_project, sanitize_project_name

    root = Path(engine.require_repo_root("initializing a project")).resolve()
    if not root.is_dir():
        raise RequestError(
            "the repository directory must exist; create it and run dex init there"
        )
    before = _read(root / ".dex/config.yml")
    configured = load_config(root)
    if configured is None and engine.has_declared_config:
        configured = engine.config
    config = (configured or DexConfig()).model_copy(deep=True)
    facts = discover(root)
    state_text = _read(root / STATE)
    try:
        state = json.loads(state_text) if state_text else {}
        if not isinstance(state, dict):
            raise ValueError("not an object")
        if state and (
            state.get("mode") not in MODES
            or not isinstance(state.get("project_name"), str)
            or (
                state.get("project_dir") is not None
                and not isinstance(state["project_dir"], str)
            )
        ):
            raise ValueError("invalid setup choices")
    except ValueError as exc:
        raise RequestError(
            "invalid .dex/setup.json; move the damaged setup record aside "
            "and run dex init again; config and authored project files are preserved"
        ) from exc
    result = SetupResult(candidates=facts["projects"])
    if mode is not None and mode not in MODES:
        raise RequestError(f"mode must be one of {', '.join(MODES)}")
    selected = project_dir or config.dbt_project_dir
    if selected:
        selected = _relative(root, root / selected)
    if project_dir and not (root / selected / "dbt_project.yml").is_file():
        raise RequestError("--project-dir must name an existing dbt project")
    if name and name not in {".", "./"}:
        requested = sanitize_project_name(name)
        if requested in facts["projects"]:
            selected = requested
    mode = (
        mode
        or (state.get("mode") if not name else None)
        or (
            "adopt"
            if facts["projects"]
            else "explore"
            if facts["semantic"] or facts["semantic_files"]
            else "new"
            if name
            else None
        )
    )
    result.mode = mode
    if mode is None:
        result.missing_inputs.append(
            {
                "name": "mode",
                "choices": list(MODES),
                "detail": "choose new (recommended), demo, adopt, or explore",
            }
        )
        return result
    if mode in {"adopt", "new", "demo"} and facts["projects"]:
        if layered_schemas and not state.get("layered_schemas"):
            raise RequestError(
                "--layered-schemas scaffolds new projects only; "
                "use transform macro generate_schema_name to plan existing changes"
            )
        if not selected and len(facts["projects"]) == 1:
            selected = facts["projects"][0]
        if not selected:
            result.missing_inputs.append(
                {
                    "name": "project_dir",
                    "choices": facts["projects"],
                    "detail": "select the existing dbt project with --project-dir",
                }
            )
            return result
        if name and name not in {".", "./"} and sanitize_project_name(name) != selected:
            raise RequestError(
                f"existing project {selected} differs from requested {name}; "
                "adopt it with --project-dir or use a separate repository"
            )
    if mode == "adopt" and not selected:
        result.missing_inputs.append(
            {
                "name": "project_dir",
                "choices": facts["projects"],
                "detail": (
                    "no dbt project found; choose new or explore, or "
                    "select --project-dir"
                ),
            }
        )
        return result
    if mode == "adopt" and not (root / selected / "dbt_project.yml").is_file():
        raise RequestError("--project-dir must name an existing dbt project")
    if target and not facts["projects"]:
        raise RequestError(
            "--target selects an existing dev profile; new projects use dev"
        )
    semantic_only = (
        mode == "explore"
        and facts["semantic"] in {"ossie", "slayer", "cube", "databricks"}
        and "connector" not in config.model_fields_set
        and not any(
            (
                engine.connector,
                engine.path,
                engine.scopes,
                engine.datasets,
                engine.project,
            )
        )
    )
    connector = engine.connector or (
        config.connector
        if "connector" in config.model_fields_set and not semantic_only
        else None
    )
    if mode == "demo":
        if connector and connector != "duckdb":
            raise RequestError(
                "the demo uses DuckDB; use a fresh repository or "
                "select the configured connector"
            )
        connector = "duckdb"
    recovered: dict[str, str] = {}
    if (
        state.get("mode") in {"new", "demo"}
        and mode != "explore"
        and selected == state.get("project_dir")
        and selected
        and (root / selected / "dbt_project.yml").is_file()
        and connector
    ):
        from .dbt_project import profiles_dir

        repair = prepare_project(
            state["project_name"],
            connector,
            repo_root=root,
            config=config,
            resume=True,
            in_place=selected == ".",
            layered_schemas=state.get("layered_schemas", False),
        )
        recovered = {
            key: value
            for key, value in repair.files.items()
            if key != ".dex/config.yml"
        }
        with contextlib.suppress(Exception):
            profiles_dir(root / selected)
            prefix = "" if selected == "." else selected + "/"
            recovered.pop(prefix + "profiles.yml", None)
    repairing_profile = any(path.endswith("profiles.yml") for path in recovered)
    if (
        selected
        and (root / selected / "dbt_project.yml").is_file()
        and mode != "explore"
        and not repairing_profile
    ):
        connector, result.problems = _adopt(config, root, selected, target, connector)
    if connector is None and not semantic_only:
        result.missing_inputs.append(
            {
                "name": "connector",
                "choices": list(VALID_CONNECTORS),
                "detail": "select the data connector with --connector",
            }
        )
        return result
    if connector not in VALID_CONNECTORS and not semantic_only:
        raise RequestError(f"unsupported connector {connector}")
    result.connector = connector
    if connector:
        config.connector = connector
    raw = config.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    block = raw.setdefault(connector, {}) if connector else {}
    if engine.project:
        if connector != "bigquery":
            raise RequestError("--project applies only to BigQuery")
        block["project"] = engine.project
    from .connect import _SCOPE_FIELDS, assert_scope_vocabulary

    assert_scope_vocabulary(
        connector,
        project=engine.project,
        datasets=engine.datasets,
        scopes=engine.scopes,
    )
    scopes = engine.scopes or engine.datasets
    if scopes:
        block[_SCOPE_FIELDS[connector]] = scopes
    if connector == "duckdb":
        path = (
            engine.path
            or block.get("path")
            or ("dex_demo.duckdb" if mode == "demo" else None)
        )
        if not path:
            result.missing_inputs.append(
                {
                    "name": "path",
                    "detail": (
                        "select an existing DuckDB warehouse with --path, or "
                        "choose --mode demo"
                    ),
                }
            )
            return result
        block["path"] = path
    if dev_destination:
        _set_destination(connector, block, dev_destination)
    config = DexConfig.model_validate(raw)
    files: dict[str, str] = dict(recovered)
    fresh = mode in {"new", "demo"} and not facts["projects"]
    if fresh:
        project_name = name or state.get("project_name") or root.name
        scaffold = prepare_project(
            project_name,
            connector,
            path=engine.path,
            repo_root=root,
            layered_schemas=layered_schemas or state.get("layered_schemas", False),
            in_place=in_place or name in {".", "./"} or state.get("project_dir") == ".",
            project=engine.project,
            config=config,
            resume=bool(state),
        )
        files.update(scaffold.files)
        config = scaffold.config
        selected = scaffold.result.project_dir
        project_name = scaffold.result.project_name
    else:
        project_name = (
            _project_name(root / selected)
            if selected
            and mode != "explore"
            and (root / selected / "dbt_project.yml").is_file()
            else root.name
        )
    result.project_dir = selected if mode != "explore" else None
    files[".dex/config.yml"] = _config_text(root, config, before)
    result.skills = skills_status(root)
    chosen_agents = (
        []
        if no_agent_instructions
        else agent_files
        if agent_files is not None
        else state.get("agent_files", facts["agent_files"] or ["AGENTS.md"])
    )
    for filename in chosen_agents:
        if filename not in {"AGENTS.md", "CLAUDE.md"}:
            raise RequestError("agent files must be AGENTS.md or CLAUDE.md")
        files[filename] = agent_document(_read(root / filename) or "")
    # Validate the README's markers before writing anything or opening a client.
    old_readme = _read(root / "README.md")
    managed(old_readme or "", "", README_BEGIN, README_END)
    state_data = {
        "version": 1,
        "mode": mode,
        "project_name": project_name,
        "project_dir": result.project_dir,
        "layered_schemas": layered_schemas or state.get("layered_schemas", False),
        "agent_files": chosen_agents,
    }
    files[STATE] = json.dumps(state_data, indent=2) + "\n"
    if (
        before is not None
        and before != files[".dex/config.yml"]
        and not engine.confirmed
    ):
        result.diffs = [file_diff(".dex/config.yml", before, files[".dex/config.yml"])]
        result.pending_confirmation = ConfirmationRequest(
            data={
                "hint": (
                    "review the configuration diff, then run the same dex "
                    "init command with --confirm; nothing has been written"
                )
            }
        )
        return result
    # Check every affected path before the first write, including symlink escapes.
    for filename in [*files, "README.md"]:
        _relative(root, root / filename)
        if (root / filename).exists() and not (root / filename).is_file():
            raise RequestError(
                f"{filename} is not a file; move it before running dex init"
            )
    demo_path = root / (config.duckdb.path if config.duckdb else "dex_demo.duckdb")
    if mode == "demo":
        _relative(root, demo_path)
    if mode == "demo" and not demo_path.exists():
        from .demo.warehouse import generate_demo_warehouse

        generate_demo_warehouse(demo_path)
        result.created.append(_relative(root, demo_path))
    elif mode == "demo" and not state:
        raise RequestError(
            "the demo warehouse already exists; adopt it with "
            "--mode new --connector duckdb --path, or use a fresh "
            "directory"
        )
    for filename, content in files.items():
        _write(root, filename, content, result)
    result.readiness["configuration"] = {"status": "passed"}
    _verify(engine, config, root, result)
    connection_status = result.readiness["connection"]["status"]
    next_command = (
        "dex explore semantic list"
        if semantic_only
        else "dex connect test"
        if connection_status != "passed"
        else "dex explore map"
    )
    result.next_steps = [
        {
            "command": next_command,
            "purpose": "inspect the existing standalone semantic layer"
            if semantic_only
            else "verify the connection"
            if connection_status != "passed"
            else "explicit follow-up: explore the configured source scope",
        }
    ]
    if result.problems:
        result.next_steps.insert(
            0,
            {
                "command": "dex init",
                "purpose": (
                    "resolve the reported prerequisites and rerun setup; "
                    "existing files are preserved"
                ),
            },
        )
    target_block = getattr(config, connector) if connector else None
    source = (
        list(getattr(target_block, _SCOPE_FIELDS.get(connector, ""), []))
        if target_block
        else []
    )
    from .connect import dev_target_scope

    body = readme_body(
        name=str(project_name),
        connector=connector,
        project_dir=result.project_dir,
        scope=source,
        destination=(
            "local DuckDB file selected in .dex/config.yml"
            if connector == "duckdb"
            else dev_target_scope(connector, target_block)
            if target_block
            else None
        ),
        connection=connection_status,
        next_command=next_command,
        skills=result.skills,
        semantic=facts["semantic"]
        or ("standalone" if facts["semantic_files"] else None),
        structure=_structure(
            root, result.project_dir, config.duckdb.path if config.duckdb else None
        ),
        unfinished=[*result.problems, *result.warnings],
        agent_files=chosen_agents,
        target=config.dbt_target or "dev",
    )
    readme = managed(
        old_readme or f"# Dex {project_name}\n", body, README_BEGIN, README_END
    )
    if _read(root / "README.md") != old_readme:
        result.problems.append(
            "README.md changed during setup; run dex init again to"
            " refresh its managed section"
        )
    else:
        _write(root, "README.md", readme, result)
    return result


def _project_name(project: Path) -> str:
    try:
        metadata = yaml.safe_load((project / "dbt_project.yml").read_text())
    except yaml.YAMLError as exc:
        raise RequestError(
            "existing dbt_project.yml is not valid YAML; repair it and run dex init"
        ) from exc
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"].strip()
    ):
        raise RequestError(
            "existing dbt_project.yml needs a nonempty name; "
            "repair the authored project configuration and run dex init again"
        )
    return metadata["name"]


def _structure(root: Path, project_dir: str | None, warehouse: str | None) -> str:
    lines = [".dex/                  # repository-level configuration and state"]
    if warehouse and not Path(warehouse).is_absolute() and (root / warehouse).is_file():
        lines.append(f"{warehouse}    # local warehouse")
    if project_dir is None:
        return "\n".join([*lines, "# Exploration setup; no dbt project was created."])
    project = root / project_dir
    prefix = "" if project_dir == "." else f"{project_dir}/"
    lines.append(f"{prefix}dbt_project.yml    # existing dbt project")
    if (project / "profiles.yml").is_file():
        lines.append(f"{prefix}profiles.yml       # local dev profile")
    else:
        lines.append("# Profile resolved through DBT_PROFILES_DIR or ~/.dbt")
    authored = yaml.safe_load((project / "dbt_project.yml").read_text()) or {}
    for model_path in authored.get("model-paths", ["models"]):
        lines.append(f"{prefix}{model_path}/    # dbt models")
        lines.extend(
            f"{prefix}{model_path}/{layer}/"
            for layer in ("staging", "intermediate", "marts")
            if (project / model_path / layer).is_dir()
        )
    return "\n".join(lines)


def _set_destination(connector: str, block: dict, value: str) -> None:
    fields = {
        "bigquery": ("dev_dataset",),
        "snowflake": ("dev_database", "dev_schema"),
        "databricks": ("dev_catalog", "dev_schema"),
        "postgres": ("dev_schema",),
        "redshift": ("dev_schema",),
        "clickhouse": ("dev_database",),
        "trino": ("dev_catalog", "dev_schema"),
    }
    if connector not in fields:
        raise RequestError(
            "DuckDB uses the selected local warehouse file; "
            "--dev-destination applies to warehouse namespaces"
        )
    keys = fields[connector]
    parts = value.split(".")
    if len(keys) == 1:
        block[keys[0]] = value
    elif len(parts) == len(keys) and all(parts):
        block.update(zip(keys, parts, strict=True))
    else:
        raise RequestError(f"--dev-destination for {connector} needs {'.'.join(keys)}")


def _write(root: Path, filename: str, content: str, result: SetupResult) -> None:
    path = root / filename
    old = _read(path)
    if old == content:
        return
    result.diffs.append(file_diff(filename, old, content))
    if old is None:
        result.created.append(filename)
    else:
        result.updated.append(filename)
    if filename == ".dex/config.yml":
        path.parent.mkdir(parents=True, exist_ok=True)
        ConfigDocument(content, repo_root=root).write(path)
    else:
        write_project_file(path, content)


def _verify(
    engine: DexEngine, config: DexConfig, root: Path, result: SetupResult
) -> None:
    from .engine import DexEngine
    from .transform import dev_target
    from .transform.build import assert_dev_target

    if result.connector is None:
        result.readiness.update(
            {
                "connection": {
                    "status": "not_applicable",
                    "reason_code": "standalone_semantic_layer",
                },
                "build": {"status": "not_applicable"},
                "build_prerequisites": {"status": "not_applicable"},
            }
        )
        return

    adapter_module = (
        "clickhouse" if config.connector == "clickhouse" else config.connector
    )
    try:
        dbt_installed = (
            importlib.util.find_spec(f"dbt.adapters.{adapter_module}") is not None
        )
    except ModuleNotFoundError:
        dbt_installed = False
    result.readiness["build"] = (
        {"status": "unchecked", "reason_code": "not_run_during_setup"}
        if result.project_dir
        else {"status": "not_applicable"}
    )
    result.readiness["build_prerequisites"] = (
        {"status": "unchecked", "dbt_adapter_installed": dbt_installed}
        if result.project_dir
        else {"status": "not_applicable"}
    )
    with DexEngine(
        config=config,
        store=engine.store,
        repo_root=root,
        connection=engine.connection,
        session_ceiling_cap=engine.session_ceiling_cap,
    ) as check:
        try:
            connected = check.connect_test()
            result.cost = connected.cost
            result.connection = check.connection_provenance().model_dump()
            result.readiness["connection"] = {"status": "passed"}
        except Exception as exc:
            result.failure_reason = env.reason_for(exc)
            result.readiness["connection"] = {
                "status": "failed",
                "recovery": env.redact(str(exc)),
            }
            result.problems.append(env.redact(str(exc)))
            return
        if result.project_dir:
            try:
                if not dbt_installed:
                    raise RequestError(
                        f"install exmergo-dex-core[{config.connector}] "
                        "to provide the dbt adapter"
                    )
                selected = config.dbt_target or "dev"
                assert_dev_target(selected, config.dbt_target)
                resolve_target(root / result.project_dir, selected)
                warnings = dev_target.check(
                    root / result.project_dir,
                    selected,
                    config,
                    root,
                    store=engine.store,
                    connection=engine.connection,
                )
                result.warnings.extend(warnings)
                result.readiness["build_prerequisites"]["status"] = (
                    "unchecked" if warnings else "passed"
                )
                if warnings:
                    result.readiness["build_prerequisites"]["details"] = warnings
                result.warnings.extend(
                    dev_target.content_check(
                        config, root, store=engine.store, connection=engine.connection
                    )
                )
            except Exception as exc:
                result.failure_reason = env.reason_for(exc)
                result.readiness["build_prerequisites"].update(
                    status="failed", recovery=env.redact(str(exc))
                )
                result.problems.append(env.redact(str(exc)))
