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
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..dbt_project import DbtProjectError, Edit, contained_path, profiles_dir
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
    dev_target_check: Callable[[], list[str]] | None = None,
    runner: Runner | None = None,
    timeout: float = _DBT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], Cost]:
    """Run ``dbt build`` against a dev target, gated. Returns (summary, cost).

    Refusal order is deliberate. The target check runs first, so a prod target is
    refused outright rather than merely unconfirmed. ``dev_target_check`` runs
    next, before the cost gate: it is free, and a dev target that has drifted or
    does not exist makes the build impossible, so the user should learn that
    instead of being asked to weigh a budget for a run that cannot succeed. It
    returns warnings and raises to refuse; the callable is injected so the engine
    stays independent of connection handling and testable without a warehouse.

    On a billed paradigm the estimate is honestly ``None``: dbt has no dry-run,
    so the engine cannot preflight the bytes a build will scan. The ceiling and
    confirmation gates still bind, and the generated profile's per-statement
    ``maximum_bytes_billed`` is the server-side backstop.
    """

    assert_dev_target(target, configured_target)

    if project_dir is None:
        raise DbtRunError("no dbt project directory resolved for the build")
    # Absolute so --project-dir/--profiles-dir cannot resolve a second time
    # against the cwd we pin below: a relative project dir would otherwise
    # double (dbt would look for project/project and fail).
    project = Path(project_dir).resolve()

    target_warnings = dev_target_check() if dev_target_check is not None else []

    estimate = 0.0 if paradigm is Paradigm.FREE_LOCAL else None
    cost = preflight(estimate, ceiling, paradigm=paradigm, confirmed=confirmed)

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
        str(profiles_dir(project).resolve()),
        "--log-format",
        "json",
    ]
    if select:
        argv += ["--select", select]

    # A hard compile-time/parse-time failure (a Jinja context error, for
    # instance) dies before dbt ever reaches node execution, so it never
    # (re)writes target/run_results.json -- a stale one left over from a prior
    # successful build would otherwise still be sitting there, and _summarize
    # would report its node results as if they belonged to this invocation
    # (issue #76). Clearing it first means "file absent" always means "this
    # invocation wrote nothing", never "an old invocation's results carried
    # over".
    (project / "target" / "run_results.json").unlink(missing_ok=True)

    # cwd is pinned to the project dir so relative paths in profiles.yml (for
    # example a DuckDB `path: ./dev.duckdb`) resolve against the project, never
    # against whatever directory the caller happened to launch from.
    run = runner or _default_runner(timeout, project, env=_build_env(paradigm, ceiling))
    completed = run(argv)

    summary = _summarize(project, target, completed)
    if deps_ran:
        summary["deps"] = {"ran": True, "success": True}
    if target_warnings:
        # Notes, not failure causes: kept out of `messages`, whose first entry
        # is promoted to the envelope's error line on failure.
        summary["notes"] = list(target_warnings)
    return summary, cost


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

    Returns ``{"available", "reason", "success", "messages"}``: unavailable
    means the caller degrades to a warning (dbt or profiles missing, mirroring
    the schema-validation fallback). When available, ``success`` says whether
    the parse itself passed; ``messages`` is populated either way, since dbt
    logs deprecation notices on a project that parses cleanly, and a plan that
    validates today should not go on to warn about them for the first time at
    `transform build` (#55). On failure, ``messages`` are the parse errors
    (dbt's own summary event leads if present; see :func:`_collect_messages`).
    """

    # Absolute so --profiles-dir (pointing at the real project) does not
    # resolve against the shadow tempdir we pin as cwd below.
    project = Path(project_dir).resolve()
    try:
        executable = _dbt_executable()
    except DbtRunError:
        return {
            "available": False,
            "reason": "dbt is not installed; plan validated by schema checks only",
            "success": None,
            "messages": [],
        }
    try:
        profiles = profiles_dir(project)
    except DbtProjectError:
        return {
            "available": False,
            "reason": "no profiles.yml found; dbt parse skipped",
            "success": None,
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
            str(profiles.resolve()),
            "--log-format",
            "json",
        ]
        if target:
            argv += ["--target", target]
        run = runner or _default_runner(timeout, shadow)
        completed = run(argv)
        success = completed.returncode == 0
        # Collected unconditionally: a passing parse can still have logged
        # deprecation notices, which the caller surfaces as plan-time warnings
        # rather than letting the author discover them for the first time at
        # `transform build`.
        messages = _collect_messages(completed)
        if not success and not messages:
            messages = ["dbt parse failed"]
    return {"available": True, "reason": None, "success": success, "messages": messages}


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

    # Absolute so --project-dir does not double against the pinned cwd.
    project = Path(project_dir).resolve()
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


def _build_env(paradigm: Paradigm, ceiling: float | None) -> dict[str, str] | None:
    """Environment overrides for the dbt subprocess, or ``None`` to inherit.

    On db-load gating (Postgres) the profile has no statement-timeout key, but
    libpq honors ``PGOPTIONS``, so the ceiling becomes a per-statement
    server-side ``statement_timeout`` — the ``maximum_bytes_billed`` analogue:
    a build statement cannot load the database past the budget even though dbt
    has no dry-run.
    """

    if paradigm is not Paradigm.DB_LOAD or ceiling is None:
        return None
    cap = f"-c statement_timeout={max(int(ceiling), 1)}s"
    existing = os.environ.get("PGOPTIONS", "")
    return {**os.environ, "PGOPTIONS": f"{existing} {cap}".strip()}


def _default_runner(
    timeout: float, cwd: Path, env: dict[str, str] | None = None
) -> Runner:
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
            env=env,
        )

    return run


# dbt 1.11 emits deprecation notices (e.g. PropertyMovedToConfigDeprecation) on
# every run of a normally-authored project, tagged `[WARNING]` in their own
# message text regardless of `info.level`. Left undistinguished from a real
# failure, one of these reliably wins the errors[0] slot merely by logging
# before the actual cause does. `MainEncounteredError` is dbt's own summary of
# what actually killed a *run-level* fatal (a connection or parse failure,
# before any node executes) and always leads when present.
#
# A per-node failure has no MainEncounteredError at all; instead dbt fires, in
# order, a bare progress line (`LogModelResult`, e.g. "1 of 1 ERROR creating
# sql view model x .... [ERROR in 0.1s]"), a bare `RunResultFailure` header
# ("Failure in model x (models/x.sql)"), and only then `RunResultError`, whose
# `msg` is the actual dbt exception text (`result.message`) -- the one event
# that names *why* the node failed. None of the first two are deprecation-
# tagged, so without also promoting `RunResultError`, whichever of them
# logged first silently wins errors[0] ahead of the real cause -- verified
# against a real `dbt build --log-format json` failure (issue #76).
_DEPRECATION_MARKER = "[WARNING]"
_MAIN_ENCOUNTERED_ERROR = "MainEncounteredError"
_RUN_RESULT_ERROR = "RunResultError"

# dbt wraps a failure's actual cause behind one or more generic headers with no
# information of their own. dbt_common's DbtRuntimeError (and its Database/
# Compilation/Validation/Runtime subclasses) render a per-node failure as
# "<Type> Error in <node> (<path>)", with the cause on the next line (e.g.
# "Database Error in model x (models/x.sql)\n  Argument 2 to JSON_VALUE must
# be a constant expression\n  compiled code at ..."). A whole-invocation fatal
# caught by dbt's own top-level handler is wrapped again in "Encountered an
# error:", and a chained/nested exception repeats a bare "<Type> Error" once
# per level before the innermost, actual message (verified against a real
# `dbt build --log-format json` run: "Encountered an error:\nRuntime Error\n
# Compilation Error\n    Could not render {{ 1/0 }}: division by zero"). This
# shape comes from dbt_common, not from any connector, so it is identical on
# Snowflake, BigQuery, or any other adapter. Keeping only the first line (a
# bare "first line of msg" truncation) silently dropped the cause behind
# whichever of these wrappers led; the #50 deprecation fix never caught it
# because #50's own repro didn't spell one out in its test message (#76).
_GENERIC_HEADER = re.compile(
    r"^(?:Encountered an error:|"
    r"(?:Database|Runtime|Compilation|Validation|Internal) Error(?: in .+)?)$"
)

# dbt colors its console messages (red failures, yellow warnings) even under
# --log-format json, baking raw ANSI escapes into info.msg; harmless on a
# terminal, but unreadable noise once that text crosses into a JSON envelope.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _collect_messages(
    completed: subprocess.CompletedProcess, log_hint: Path | None = None
) -> list[str]:
    """Reduce dbt's output to actionable one-liners for the envelope.

    Keeps the first line of each error/warn message (redacted, length-capped,
    deduplicated) -- except a generic header (see _GENERIC_HEADER), which says
    nothing about why the node or run failed on its own, so the nearest
    following line that isn't itself another generic header rides along with
    it. Deprecation notices sink below real errors so they cannot poison the
    errors[0] slot, and dbt's own structured summaries of what actually
    failed -- MainEncounteredError for a run-level fatal, RunResultError for a
    per-node one -- always lead over an uninformative progress line or bare
    failure header that merely logged first. When anything was cut, the last
    entry points at the full log
    instead of letting a raw traceback cross the envelope boundary.
    """

    entries: list[tuple[str, str]] = []
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
        msg = redact(_ANSI_ESCAPE.sub("", str(info["msg"])))
        msg_lines = msg.splitlines()
        first_line = msg_lines[0].strip() if msg_lines else msg.strip()
        if _GENERIC_HEADER.match(first_line):
            cause = next(
                (
                    stripped
                    for line in msg_lines[1:]
                    if (stripped := line.strip())
                    and not _GENERIC_HEADER.match(stripped)
                ),
                None,
            )
            if cause:
                sep = "" if first_line.endswith(":") else ":"
                first_line = f"{first_line}{sep} {cause}"
        if len(first_line) > _MESSAGE_MAX_CHARS:
            first_line = first_line[:_MESSAGE_MAX_CHARS] + "..."
            trimmed = True
        if first_line != msg:
            trimmed = True
        if first_line in seen:
            trimmed = True
            continue
        seen.add(first_line)
        entries.append((str(info.get("name") or ""), first_line))

    primary = [m for _, m in entries if _DEPRECATION_MARKER not in m]
    deprecations = [m for _, m in entries if _DEPRECATION_MARKER in m]
    main_error = next(
        (m for name, m in entries if name == _MAIN_ENCOUNTERED_ERROR), None
    )
    # Per-node cause(s), in case a build fails several models at once; a
    # run-level MainEncounteredError (when present) still leads them, since it
    # is the reason none of them, or nothing after them, ran at all.
    node_causes = [
        m for name, m in entries if name == _RUN_RESULT_ERROR and m != main_error
    ]
    promoted = ([main_error] if main_error is not None else []) + node_causes
    if promoted:
        primary = promoted + [m for m in primary if m not in promoted]
    messages = primary + deprecations

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
