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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..dbt_project import profiles_dir
from ..envelope import Cost, Paradigm, redact
from ..guards.cost_guard import preflight

# Names that mean production no matter what the config says. The build target
# must additionally be on the allowlist (dev or the configured dbt_target); this
# set is the backstop config cannot override.
_PROD_TARGET_NAMES = {"prod", "production", "prd", "live", "release", "main"}

_DBT_TIMEOUT_SECONDS = 600.0

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
    runner: Runner | None = None,
    timeout: float = _DBT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], Cost]:
    """Run ``dbt build`` against a dev target, gated. Returns (summary, cost).

    Refusal order is deliberate: the target check runs before the cost gate, so a
    prod target is refused outright rather than merely unconfirmed. DuckDB spend
    is zero (``FREE_LOCAL``) but the preflight still runs, so the confirmation
    path is exercised on the free connector exactly as it will be on billed ones.
    """

    assert_dev_target(target, configured_target)
    cost = preflight(0.0, ceiling, paradigm=Paradigm.FREE_LOCAL, confirmed=confirmed)

    if project_dir is None:
        raise DbtRunError("no dbt project directory resolved for the build")
    project = Path(project_dir)

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

    run = runner or _default_runner(timeout)
    completed = run(argv)

    summary = _summarize(project, target, completed)
    return summary, cost


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


def _default_runner(timeout: float) -> Runner:
    def run(argv: list[str]) -> subprocess.CompletedProcess:
        # The argv is engine-built (dbt executable + validated target/paths),
        # never raw user input, and shell=False.
        return subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )

    return run


def _summarize(
    project: Path, target: str, completed: subprocess.CompletedProcess
) -> dict[str, Any]:
    """Reduce a dbt run to a sanitized summary; raw log text stays behind."""

    nodes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
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

    messages: list[str] = []
    if completed.returncode != 0:
        # Surface dbt's own human-readable messages (redacted), never raw log text
        # wholesale: enough to act on, nothing to leak.
        for line in (completed.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = event.get("info", {})
            if info.get("level") in {"error", "warn"} and info.get("msg"):
                messages.append(redact(str(info["msg"])))
        if not messages and completed.stderr:
            messages.append(redact(completed.stderr.strip().splitlines()[-1]))

    return {
        "target": target,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "nodes": nodes,
        "counts": counts,
        "messages": messages[:20],
    }
