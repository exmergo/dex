"""The live backend: one headless Claude Code session per task, then the scorer.

The agent's only configuration is the block ``dex procedure --block`` returns,
placed as the workspace's ``CLAUDE.md``, which is what #503 requires of a
benchmark harness: a published result describes what users get. The plugin under
test is the working tree's (``--plugin-dir``), and the ``dex`` CLI on the
agent's PATH is the engine environment this suite runs in, so both surfaces are
the candidate.

Provenance is recorded rather than assumed (#791): the stream's init event says
which plugins and skills actually loaded, and a run whose loaded dex plugin is not
the working tree is flagged, because its numbers would describe another release.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..claude_agent import _child_env, _require_claude

REPO = Path(__file__).resolve().parents[2]
ALLOWED_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "Skill"]
DISALLOWED_TOOLS = ["Bash(git commit*)", "Bash(git push*)", "WebFetch", "WebSearch"]
# The suffix every task carries, mirroring an unattended benchmark trial: no human
# will answer a question, so the agent finishes the job rather than asking.
UNATTENDED = (
    "You are working unattended: nobody will answer questions, so finish the task. "
    "This is a disposable local project on a synthetic DuckDB warehouse; you may "
    "change it and build it on dev. Do not create git commits."
)


def procedure_envelope() -> dict[str, Any]:
    """``dex procedure --block`` from the engine this suite runs in."""

    proc = subprocess.run(
        [sys.executable, "-m", "exmergo_dex_core", "procedure", "--block"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return json.loads(proc.stdout)["data"]


def provenance() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()

    procedure = procedure_envelope()
    claude = shutil.which("claude")
    version = (
        subprocess.run(
            [claude, "--version"], capture_output=True, text=True
        ).stdout.strip()
        if claude
        else None
    )
    return {
        "commit": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "engine_version": procedure["engine_version"],
        "procedure_digest": procedure["digest"],
        "claude_code_version": version,
        "plugin_dir": str(REPO),
        "engine_python": sys.executable,
    }


@dataclass
class Session:
    cost_usd: float | None = None
    turns: int | None = None
    duration_s: float = 0.0
    is_error: bool = False
    error: str | None = None
    final: str = ""
    plugins: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    skills_triggered: list[str] = field(default_factory=list)
    dex_commands: dict[str, int] = field(default_factory=dict)
    candidate_plugin_loaded: bool | None = None


_GROUPS = "explore|transform|maintain|semantic|config|connect|demo|procedure"
# `dex <group>`, the engine module, or the skill wrapper, however its path is
# spelled (`run.py`, or `--script $R` with the path in a shell variable).
_DEX_CALL = re.compile(
    r"(?:\bdex|run\.py[\"']?|exmergo_dex_core|--script\s+\S+)\s+"
    r"(?:--[\w-]+(?:[ =]\S+)?\s+)*"
    rf"({_GROUPS})\b(?:\s+([a-z]+))?"
)


def summarize(events: list[dict[str, Any]], session: Session) -> Session:
    skills: list[str] = []
    commands: dict[str, int] = {}
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            session.plugins = list(event.get("plugins") or [])
            session.model = event.get("model")
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                payload = block.get("input") or {}
                if block.get("name") == "Skill":
                    name = payload.get("skill") or payload.get("command") or ""
                    if name and name not in skills:
                        skills.append(name)
                if block.get("name") == "Bash":
                    for match in _DEX_CALL.finditer(payload.get("command") or ""):
                        group, sub = match.group(1), match.group(2)
                        label = f"{group} {sub}" if sub else group
                        commands[label] = commands.get(label, 0) + 1
        if event.get("type") == "result":
            session.cost_usd = event.get("total_cost_usd")
            session.turns = event.get("num_turns")
            session.is_error = bool(event.get("is_error"))
            session.final = str(event.get("result") or "")[-2000:]
    session.skills_triggered = skills
    session.dex_commands = commands
    dex_plugins = [p for p in session.plugins if "dex" in str(p.get("name", ""))]
    session.candidate_plugin_loaded = bool(dex_plugins) and all(
        Path(str(p.get("path", ""))).resolve() == REPO for p in dex_plugins
    )
    return session


def run_session(
    prompt: str,
    workspace: Path,
    transcript: Path,
    *,
    model: str,
    max_turns: int,
    max_budget_usd: float,
    timeout: int,
) -> Session:
    binary = _require_claude("claude")
    env = _child_env()
    # The candidate engine's `dex` and the dbt it pins, first on PATH.
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    args = [
        binary,
        "-p",
        f"{prompt}\n\n{UNATTENDED}",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        str(max_budget_usd),
        "--plugin-dir",
        str(REPO),
        # The user's own settings would load their installed plugins, the wrong
        # dex among them (#791), and their memory; the project's are the fixture's.
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--disallowedTools",
        *DISALLOWED_TOOLS,
    ]
    started = time.monotonic()
    session = Session()
    events: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(
            args,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        transcript.write_text(proc.stdout, encoding="utf-8")
        for line in proc.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if proc.returncode != 0:
            session.error = (proc.stderr or "").strip()[
                -2000:
            ] or f"exit {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        partial = (
            exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        )
        transcript.write_text(partial, encoding="utf-8")
        session.error = f"timed out after {timeout}s"
    session.duration_s = round(time.monotonic() - started, 1)
    summarize(events, session)
    if session.error is not None:
        # A result event can say `is_error: false` for a process that then exited
        # non-zero or timed out; the process failure wins, and the event's cost
        # and turns are kept.
        session.is_error = True
    return session
