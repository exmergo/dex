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

import hashlib
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


def resolve_plugin_dir(plugin_dir: Path | str | None) -> Path:
    """The plugin directory as an absolute path, the working tree by default.

    A session runs with its workspace as the working directory, so a relative
    path would name one plugin to Claude and another to the provenance check.
    """

    return Path(plugin_dir).expanduser().resolve() if plugin_dir else REPO


def provenance(
    plugin_dir: Path | str | None = None, procedure_text: str | None = None
) -> dict[str, Any]:
    """What this run actually gave the agent, including a baseline's overrides.

    ``procedure_text`` is the text a ``--procedure-file`` run hands the agent. It
    replaces the engine's digest, because a report naming the engine's procedure
    would describe a text the agent never saw.
    """

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
        "procedure_source": "engine" if procedure_text is None else "file",
        "procedure_digest": (
            procedure["digest"]
            if procedure_text is None
            else hashlib.sha256(procedure_text.encode()).hexdigest()[:12]
        ),
        "claude_code_version": version,
        "plugin_dir": str(resolve_plugin_dir(plugin_dir)),
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
# A wrapper bound in the same command string — a shell function
# (`R() { uv ... run.py "$@"; }`) or a variable (`R="uv ... run.py"`). Calls
# through the bound name reach Dex like direct ones; the lookbehinds keep
# `--script $R`, the path-in-a-variable route above, from counting twice.
_WRAPPER = re.compile(r"\bdex\b|run\.py|exmergo_dex_core|--script\s+\S+")
_FN_DEF = re.compile(r"([A-Za-z_]\w*)\s*\(\)\s*\{([^{}]*)\}")
_VAR_DEF = re.compile(
    r"(?:^|;)\s*([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|\S+)", re.MULTILINE
)


def _aliases(command: str) -> list[str]:
    """Names bound to a Dex wrapper in this same command string. A wrapper
    defined in an earlier call needs no handling: shell state does not survive
    between calls."""
    fn_names = [
        match.group(1)
        for match in _FN_DEF.finditer(command)
        if _WRAPPER.search(match.group(2))
    ]
    var_names = [
        match.group(1)
        for match in _VAR_DEF.finditer(command)
        if _WRAPPER.search(match.group(2).strip("\"'"))
    ]
    return list(dict.fromkeys(fn_names + var_names))


def _alias_call(name: str) -> re.Pattern:
    return re.compile(
        rf"(?<!--script\s)(?<!--script\s\$)\$?\b{re.escape(name)}\b(?!\s*[=(])"
        r"(?:\s+(?:--[\w-]+(?:[ =]\S+)?\s+)*)?"
        rf"({_GROUPS})\b(?:\s+([a-z]+))?"
    )


def _quoted_spans(command: str) -> list[tuple[int, int]]:
    """Half-open [start, end) spans of single- and double-quoted text. Quoted
    alias text is data, not an invocation — `echo "R transform apply"` never
    runs R. Matches inside `$(...)` and wrapper bodies stay counted: those run
    when the command does."""
    spans: list[tuple[int, int]] = []
    quote: str | None = None
    start = 0
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote is not None:
            i += 2
            continue
        if quote is None:
            if ch in "\"'":
                quote, start = ch, i
        elif ch == quote:
            spans.append((start, i + 1))
            quote = None
        i += 1
    return spans


def summarize(
    events: list[dict[str, Any]], session: Session, plugin_dir: Path = REPO
) -> Session:
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
                    command = payload.get("command") or ""
                    patterns = [_DEX_CALL] + [
                        _alias_call(name) for name in _aliases(command)
                    ]
                    quoted = _quoted_spans(command)
                    for pattern in patterns:
                        for match in pattern.finditer(command):
                            if pattern is not _DEX_CALL and any(
                                begin <= match.start() < end for begin, end in quoted
                            ):
                                continue
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
        Path(str(p.get("path", ""))).resolve() == plugin_dir.resolve()
        for p in dex_plugins
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
    plugin_dir: Path | None = None,
) -> Session:
    plugin_dir = resolve_plugin_dir(plugin_dir)
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
        str(plugin_dir),
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
    summarize(events, session, plugin_dir)
    if session.error is not None:
        # A result event can say `is_error: false` for a process that then exited
        # non-zero or timed out; the process failure wins, and the event's cost
        # and turns are kept.
        session.is_error = True
    return session
