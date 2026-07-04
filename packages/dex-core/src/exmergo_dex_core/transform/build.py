"""Dev-target dbt builds: prod-refusing, preflight-gated, subprocess-isolated.

The refusal and the cost gate live here in the engine, not in the command layer,
so every caller is gated: prod-target execution is never initiated by dex, and
nothing runs unconfirmed. The target rule is two-layered: an allowlist (``dev``
or the configured ``dbt_target``) and a denylist backstop that config cannot
whitelist, so a misconfigured ``dbt_target: prod`` still refuses.

dbt runs as a subprocess rather than in-process: dbt logs to stdout, and the
command contract is exactly one JSON envelope there, so the run is isolated and
only a sanitized summary crosses the boundary. Node results come from dbt's own
``target/run_results.json`` artifact, not from scraping log text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..dbt_project import (
    DbtProjectError,
    Edit,
    contained_path,
    duckdb_target_path,
    profiles_dir,
)
from ..dbt_project import load as load_project
from ..envelope import Cost, Paradigm, redact
from ..guards.cost_guard import preflight

# Names that mean production no matter what the config says. The build target
# must additionally be on the allowlist (dev or the configured dbt_target); this
# set is the backstop config cannot override.
_PROD_TARGET_NAMES = {"prod", "production", "prd", "live", "release", "main"}

_DBT_TIMEOUT_SECONDS = 600.0
_DEPS_TIMEOUT_SECONDS = 300.0
_PARSE_TIMEOUT_SECONDS = 120.0

# Envelope hygiene for dbt output: enough of each message to act on, never a
# multi-KB traceback, never the same message twice.
_MESSAGE_MAX_CHARS = 400
_MESSAGE_CAP = 20

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class ProdTargetRefusedError(Exception):
    pass


class DbtRunError(Exception):
    pass


def assert_dev_target(target: str, configured: str | None = None) -> str:
    """Refuse anything that is not an explicit dev target.

    The denylist is checked first and wins even over the configured target, so
    configuration alone can never route a build at production.
    """

    if target.lower() in _PROD_TARGET_NAMES:
        raise ProdTargetRefusedError(
            f"target '{target}' looks like production; dex never initiates "
            "prod-target execution (builds are dev-target only)"
        )
    allowed = {"dev"} | ({configured} if configured else set())
    if target not in allowed:
        raise ProdTargetRefusedError(
            f"target '{target}' is not the dev target; allowed here: "
            f"{', '.join(sorted(allowed))} (set dbt_target in .dex/config.yml to "
            "name your dev target)"
        )
    return target


def build(
    project_dir: Path | str | None = None,
    *,
    target: str,
    configured_target: str | None = None,
    select: str | None = None,
    ceiling: float | None = None,
    confirmed: bool = False,
    paradigm: Paradigm = Paradigm.FREE_LOCAL,
    runner: Runner | None = None,
    timeout: float = _DBT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], Cost]:
    """Run ``dbt build`` against a dev target, gated. Returns (summary, cost).

    Refusal order is deliberate: the target check runs before the cost gate, so a
    prod target is refused outright rather than merely unconfirmed. DuckDB spend
    is zero (``FREE_LOCAL``) but the preflight still runs, so the confirmation
    path is exercised on the free connector exactly as it will be on billed ones.

    On a billed paradigm the estimate is honestly ``None``: dbt has no dry-run,
    so the engine cannot preflight the bytes a build will scan. The ceiling and
    confirmation gates still bind, and the generated profile's per-statement
    ``maximum_bytes_billed`` is the server-side backstop.
    """

    assert_dev_target(target, configured_target)
    estimate = 0.0 if paradigm is Paradigm.FREE_LOCAL else None
    cost = preflight(estimate, ceiling, paradigm=paradigm, confirmed=confirmed)

    if project_dir is None:
        raise DbtRunError("no dbt project directory resolved for the build")
    project = Path(project_dir)

    seeding_warning = _check_dev_database(project, target)

    # Most real projects carry a packages.yml, and dbt refuses to compile until
    # its packages are installed; running deps here (post-gate) means the first
    # build never fails on a missing `dbt deps` step the agent has no verb for.
    deps_ran = False
    if needs_deps(project):
        deps_summary = deps(project, runner=runner)
        deps_ran = True
        if not deps_summary["success"]:
            return {
                "target": target,
                "success": False,
                "returncode": deps_summary["returncode"],
                "nodes": [],
                "counts": {},
                "messages": deps_summary["messages"],
                "deps": {"ran": True, "success": False},
            }, cost

    argv = [
        _dbt_executable(),
        "build",
        "--target",
        target,
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(profiles_dir(project)),
        "--log-format",
        "json",
    ]
    if select:
        argv += ["--select", select]

    # cwd is pinned to the project dir so relative paths in profiles.yml (for
    # example a DuckDB `path: ./dev.duckdb`) resolve against the project, never
    # against whatever directory the caller happened to launch from.
    run = runner or _default_runner(timeout, project)
    completed = run(argv)

    summary = _summarize(project, target, completed)
    if deps_ran:
        summary["deps"] = {"ran": True, "success": True}
    if seeding_warning:
        # A note, not a failure cause: kept out of `messages`, whose first entry
        # is promoted to the envelope's error line on failure.
        summary["notes"] = [seeding_warning]
    return summary, cost


def _check_dev_database(project: Path, target: str) -> str | None:
    """Catch the missing-dev-database failure mode before dbt does.

    A duckdb target whose file does not exist yet is legitimate when the project
    builds everything from models, but a project reading from ``sources:`` would
    get a fresh empty database and then fail every source relation with a
    confusing catalog error. That case is refused with the seeding step spelled
    out; the source-less case only warns.
    """

    db_path = duckdb_target_path(project, target)
    if db_path is None or db_path.exists():
        return None
    if _declares_sources(project):
        raise DbtRunError(
            f"the dev target database {db_path} does not exist and the project "
            "reads from sources; seed it before building (for example copy the "
            f"source warehouse: cp <source>.duckdb {db_path}), or point the dev "
            "target at an existing database file"
        )
    return f"dev target database {db_path} does not exist; dbt will create an empty one"


def _declares_sources(project: Path) -> bool:
    view = load_project(project)
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and parsed.get("sources"):
            return True
    return False


def shadow_parse(
    project_dir: Path | str,
    edits: list[Edit],
    *,
    target: str | None = None,
    runner: Runner | None = None,
    timeout: float = _PARSE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Validate proposed edits with dbt's own parser, without touching the project.

    The project is copied to a throwaway directory (warehouse files and build
    artifacts excluded), the edits are overlaid there, and ``dbt parse`` runs
    against the copy with cwd pinned to it, so everything dbt writes (target/,
    logs/, any stray database a relative profile path would create) lives and
    dies with the copy.

    Returns ``{"available", "reason", "messages"}``: unavailable means the
    caller degrades to a warning (dbt or profiles missing, mirroring the
    schema-validation fallback); available with empty messages means the parse
    passed; non-empty messages are the parse errors.
    """

    project = Path(project_dir)
    try:
        executable = _dbt_executable()
    except DbtRunError:
        return {
            "available": False,
            "reason": "dbt is not installed; plan validated by schema checks only",
            "messages": [],
        }
    try:
        profiles = profiles_dir(project)
    except DbtProjectError:
        return {
            "available": False,
            "reason": "no profiles.yml found; dbt parse skipped",
            "messages": [],
        }

    view = load_project(project)
    with tempfile.TemporaryDirectory(prefix="dex-shadow-") as tmp:
        shadow = Path(tmp) / (project.resolve().name or "project")
        # dbt_packages/ is deliberately copied (parse needs installed macros);
        # warehouse files are deliberately not (parse never reads them, and
        # they can be huge). `.dex` matters when the project is the repo root.
        shutil.copytree(
            project,
            shadow,
            ignore=shutil.ignore_patterns(
                "target", "logs", ".git", ".venv", ".dex", "*.duckdb", "*.db"
            ),
        )
        for edit in edits:
            edit_path = contained_path(shadow, edit.path, view.model_paths)
            edit_path.parent.mkdir(parents=True, exist_ok=True)
            edit_path.write_text(edit.new_content, encoding="utf-8")

        argv = [
            executable,
            "parse",
            "--project-dir",
            str(shadow),
            "--profiles-dir",
            str(profiles),
            "--log-format",
            "json",
        ]
        if target:
            argv += ["--target", target]
        run = runner or _default_runner(timeout, shadow)
        completed = run(argv)
        messages: list[str] = []
        if completed.returncode != 0:
            messages = _collect_messages(completed) or ["dbt parse failed"]
    return {"available": True, "reason": None, "messages": messages}


def has_package_spec(project_dir: Path | str) -> bool:
    """True when the project declares dbt packages (packages.yml, or a
    dependencies.yml with a ``packages:`` key)."""

    project = Path(project_dir)
    if (project / "packages.yml").is_file():
        return True
    dependencies = project / "dependencies.yml"
    if dependencies.is_file():
        try:
            parsed = yaml.safe_load(dependencies.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return False
        return isinstance(parsed, dict) and bool(parsed.get("packages"))
    return False


def needs_deps(project_dir: Path | str) -> bool:
    """True when declared packages are not installed yet (dbt_packages/ missing
    or empty). Lockfile staleness is not tracked; `transform deps` is the
    explicit refresh."""

    project = Path(project_dir)
    if not has_package_spec(project):
        return False
    packages_dir = project / "dbt_packages"
    return not packages_dir.is_dir() or not any(packages_dir.iterdir())


def deps(
    project_dir: Path | str,
    *,
    runner: Runner | None = None,
    timeout: float = _DEPS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``dbt deps``. Returns a sanitized summary dict.

    Not confirmation-gated: deps never connects to a warehouse and writes only
    ``dbt_packages/`` plus the lockfile under the project dir. Inside ``build()``
    it runs after the target check and the cost gate anyway.
    """

    project = Path(project_dir)
    argv = [
        _dbt_executable(),
        "deps",
        "--project-dir",
        str(project),
        "--log-format",
        "json",
    ]
    run = runner or _default_runner(timeout, project)
    completed = run(argv)
    messages: list[str] = []
    if completed.returncode != 0:
        messages = _collect_messages(completed, log_hint=project / "logs" / "dbt.log")
    packages_dir = project / "dbt_packages"
    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "packages_dir_exists": packages_dir.is_dir() and any(packages_dir.iterdir()),
        "messages": messages,
    }


# --- helpers -----------------------------------------------------------------


def _dbt_executable() -> str:
    # Prefer the dbt installed next to this interpreter (the [duckdb] extra pulls
    # dbt-duckdb, and with it dbt-core); PATH is the fallback.
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("dbt")
    if found:
        return found
    raise DbtRunError(
        "dbt executable not found; install the connector extra that carries it "
        "(e.g. exmergo-dex-core[duckdb])"
    )


def _default_runner(timeout: float, cwd: Path) -> Runner:
    def run(argv: list[str]) -> subprocess.CompletedProcess:
        # The argv is engine-built (dbt executable + validated target/paths),
        # never raw user input, and shell=False.
        return subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd),
        )

    return run


def _collect_messages(
    completed: subprocess.CompletedProcess, log_hint: Path | None = None
) -> list[str]:
    """Reduce dbt's output to actionable one-liners for the envelope.

    Keeps the first line of each error/warn message (redacted, length-capped,
    deduplicated); when anything was cut, the last entry points at the full log
    instead of letting a raw traceback cross the envelope boundary.
    """

    messages: list[str] = []
    seen: set[str] = set()
    trimmed = False
    for line in (completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = event.get("info", {})
        if info.get("level") not in {"error", "warn"} or not info.get("msg"):
            continue
        msg = redact(str(info["msg"]))
        first_line = msg.splitlines()[0] if msg else msg
        if len(first_line) > _MESSAGE_MAX_CHARS:
            first_line = first_line[:_MESSAGE_MAX_CHARS] + "..."
            trimmed = True
        if first_line != msg:
            trimmed = True
        if first_line in seen:
            trimmed = True
            continue
        seen.add(first_line)
        messages.append(first_line)
    if not messages and completed.stderr:
        messages.append(redact(completed.stderr.strip().splitlines()[-1]))
    if len(messages) > _MESSAGE_CAP:
        messages = messages[:_MESSAGE_CAP]
        trimmed = True
    if trimmed and log_hint is not None:
        messages.append(f"full output: {log_hint}")
    return messages


def _summarize(
    project: Path, target: str, completed: subprocess.CompletedProcess
) -> dict[str, Any]:
    """Reduce a dbt run to a sanitized summary; raw log text stays behind."""

    nodes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    bytes_billed = 0.0
    saw_billing = False
    run_results = project / "target" / "run_results.json"
    if run_results.is_file():
        results = json.loads(run_results.read_text(encoding="utf-8")).get("results", [])
        for result in results:
            status = str(result.get("status", "unknown"))
            nodes.append(
                {
                    "name": str(result.get("unique_id", "")).split(".")[-1],
                    "status": status,
                    "execution_time": result.get("execution_time"),
                }
            )
            counts[status] = counts.get(status, 0) + 1
            # dbt-bigquery stamps per-node billing into adapter_response; free
            # adapters do not, so the key's absence means nothing to report.
            response = result.get("adapter_response") or {}
            if "bytes_billed" in response:
                saw_billing = True
                bytes_billed += float(response.get("bytes_billed") or 0)

    messages: list[str] = []
    if completed.returncode != 0:
        messages = _collect_messages(completed, log_hint=project / "logs" / "dbt.log")

    summary = {
        "target": target,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "nodes": nodes,
        "counts": counts,
        "messages": messages,
    }
    if saw_billing:
        summary["bytes_billed"] = bytes_billed
    return summary
