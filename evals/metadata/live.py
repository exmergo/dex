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
        """One git command in the repository, its trimmed standard output."""

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
    """What one live agent session did: its cost, turns and outcome, the plugins and
    skills it loaded, and the Dex calls it made in order.
    """

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
    #: The same calls in the order the agent made them, which is what a
    #: procedure step such as "compare before apply" is graded on (#1049).
    dex_sequence: list[str] = field(default_factory=list)
    #: The same calls with what the transcript shows about each: ``label``,
    #: ``plan`` (the literal id named, or ``None``), ``flags`` (the ``--flag``
    #: names the call passed, without their dashes or values, see ``_flags``)
    #: and, on a comparison, ``verdict`` (see ``_compare_verdict``), and on a
    #: plan, ``toggle`` and ``plan_id`` (see ``_plan_toggle``). A procedure step
    #: reads these.
    dex_calls: list[dict[str, Any]] = field(default_factory=list)
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
    """A pattern for a Dex call made through the shell alias or variable ``name``."""

    return re.compile(
        rf"(?<!--script\s)(?<!--script\s\$)\$?\b{re.escape(name)}\b(?!\s*[=(])"
        r"(?:\s+(?:--[\w-]+(?:[ =]\S+)?\s+)*)?"
        rf"({_GROUPS})\b(?:\s+([a-z]+))?"
    )


def _quoted_spans(command: str) -> list[tuple[int, int]]:
    """Half-open [start, end) spans of single- and double-quoted text."""

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


# The shells' own way of running a quoted string: `bash -c "..."`, `eval "..."`.
_RUNS_ITS_ARGUMENT = re.compile(
    r"(?:\b(?:ba|da|k|z)?sh\s+(?:-\w+\s+)*-\w*c|\beval)\s+$"
)


def _is_quoted_data(
    command: str, start: int, end: int, quoted: list[tuple[int, int]]
) -> bool:
    """Whether a match is text rather than a call: `echo "dex transform apply"`
    runs nothing. A match has to sit wholly inside the quotes, since a quoted
    path (`"/x/run.py" transform apply`) is a call, and a quoted string the
    shell runs stays counted: `"$(dex ...)"`, `bash -c "dex ..."`."""

    for begin, stop in quoted:
        if begin <= start and end <= stop:
            before = command[begin:start]
            if command[begin] == '"' and before.count("$(") > before.count(")"):
                return False
            return not _RUNS_ITS_ARGUMENT.search(command[:begin])
    return False


_FLAG = re.compile(r"(?<![\w-])--([A-Za-z][\w-]*)")
_SEPARATORS = ";&|\n"


def _flags(command: str, start: int, quoted: list[tuple[int, int]]) -> list[str]:
    """The ``--flags`` of the call starting at ``start``, up to the shell's next
    command separator (``;``, ``&&``, ``||``, ``|``, a newline). A separator or a
    flag inside quotes is data, not shell: ``transform plan "keep --cold-deps"``
    passes no flag, and ``--where "a; b"`` does not end the call. Global flags
    before the group (``dex --confirm transform build``) count too, since the
    engine reads them the same way. Two shell forms that look like separators
    are not: a newline after a backslash continues the command, and the ``&`` of
    a redirect (``2>&1``, ``>&``, ``&>``) belongs to the redirect. An unquoted
    shell comment ends the scan; a hash inside a word or quotes does not."""

    def inside(position: int) -> bool:
        """Identify quoted data that cannot end the shell call."""
        return any(begin <= position < stop for begin, stop in quoted)

    def escaped(position: int) -> bool:
        """An odd run of preceding backslashes escapes a shell character."""
        begin = position
        while begin > 0 and command[begin - 1] == "\\":
            begin -= 1
        return (position - begin) % 2 == 1

    def separates(position: int) -> bool:
        """End the call at shell operators and comments, honoring escapes."""
        char = command[position]
        if inside(position):
            return False
        if char == "#":
            before = position - 1
            # Backslash-newline disappears before the shell identifies words.
            while before > 0 and command[before] == "\n" and escaped(before):
                before -= 2
            previous = command[before : before + 1] if before >= 0 else ""
            return before < 0 or (
                (previous.isspace() or previous in ";&|()<>") and not escaped(before)
            )
        if char not in _SEPARATORS:
            return False
        if char == "\n":
            return not escaped(position)
        if char == "&":
            return command[position - 1 : position] != ">" and (
                command[position + 1 : position + 2] != ">"
            )
        return True

    end = start
    while end < len(command) and not separates(end):
        end += 1
    return list(
        dict.fromkeys(
            match.group(1)
            for match in _FLAG.finditer(command, start, end)
            if not inside(match.start())
        )
    )


# A literal plan id. `$PLAN`, a flag or a redirect names none this can read.
_PLAN_ARGUMENT = re.compile(r"\s+(\w[\w.-]*)")
_STATUS = re.compile(r'"status"\s*:\s*"(\w+)"')
_VERDICTS = ("equivalent", "different", "not_checked")


def _result_text(block: dict[str, Any]) -> str:
    """A tool result block's text, its content parts joined when it has several."""

    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content)
    return str(content or "")


def _compare_verdict(block: dict[str, Any]) -> str | None:
    """What one comparison returned: a verdict, ``not_run`` when the command
    came back without one (an error, a cost handshake), or ``None`` when the
    agent's own pipe left nothing to read."""

    text = _result_text(block)
    statuses = _STATUS.findall(text)
    for status in statuses:
        if status in _VERDICTS:
            return status
    if text.strip().strip('"') in _VERDICTS:
        return text.strip().strip('"')
    if block.get("is_error") or {"error", "needs_confirmation"} & set(statuses):
        return "not_run"
    return None


_OUTCOME = re.compile(r'"outcome"\s*:\s*"(\w+)"')


def _build_outcome(block: dict[str, Any]) -> str | None:
    """What one ``transform build`` established: ``data.completion.outcome``
    (``validated``, ``failed``, ``skipped``, ...), the first one the envelope
    carries since completion leads the payload; ``failed`` for an error
    envelope that carries none, ``not_run`` for a cost handshake; ``None`` when
    the agent's own pipe left nothing to read (#1114)."""

    text = _result_text(block)
    found = _OUTCOME.findall(text)
    if found:
        return found[0]
    statuses = set(_STATUS.findall(text))
    if "needs_confirmation" in statuses:
        return "not_run"
    if block.get("is_error") or "error" in statuses:
        return "failed"
    return None


_TOGGLE = re.compile(r'"toggle"\s*:\s*\{[^{}]*?"verdict"\s*:\s*"(\w+)"')


_PLAN_ID = re.compile(r'"plan_id"\s*:\s*"([\w.-]+)"')


def _plan_toggle(block: dict[str, Any]) -> str | None:
    """What one ``transform plan`` said about a toggle contract (#1113): the
    ``data.toggle`` verdict, ``error`` when the plan failed (a tool error or an
    error envelope, which stores no plan), ``absent`` when a readable plan
    result carried no toggle, or ``None`` only when the agent's pipe left
    nothing to read."""

    text = _result_text(block)
    found = _TOGGLE.findall(text)
    if found:
        return found[0]
    statuses = _STATUS.findall(text)
    if block.get("is_error") or "error" in statuses:
        return "error"
    if '"plan_id"' in text or statuses:
        return "absent"
    return None


def _plan_id(block: dict[str, Any]) -> str | None:
    """The plan id a ``transform plan`` result names, if it is readable."""

    found = _PLAN_ID.findall(_result_text(block))
    return found[0] if found else None


_SURVIVED = re.compile(r'"survived"\s*:\s*(\d+)')


def _mutation_survivors(block: dict[str, Any]) -> int | None:
    """How many mutants one ``transform test --mutate`` run reported surviving,
    read from ``data.counts.survived``; ``None`` when the agent's pipe hid it or
    the run returned no counts (an error, a cost handshake)."""

    found = _SURVIVED.findall(_result_text(block))
    return int(found[0]) if found else None


def summarize(
    events: list[dict[str, Any]], session: Session, plugin_dir: Path = REPO
) -> Session:
    """Extract executed Dex calls, their flags and results from agent events."""

    skills: list[str] = []
    commands: dict[str, int] = {}
    sequence: list[str] = []
    calls: list[dict[str, Any]] = []
    pending_compares: dict[str, dict[str, Any]] = {}
    pending_mutations: dict[str, dict[str, Any]] = {}
    pending_builds: dict[str, dict[str, Any]] = {}
    pending_plans: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    call = pending_compares.pop(str(block.get("tool_use_id")), None)
                    if call is not None:
                        call["verdict"] = _compare_verdict(block)
                    run = pending_mutations.pop(str(block.get("tool_use_id")), None)
                    if run is not None:
                        run["survived"] = _mutation_survivors(block)
                    built = pending_builds.pop(str(block.get("tool_use_id")), None)
                    if built is not None:
                        built["outcome"] = _build_outcome(block)
                    planned = pending_plans.pop(str(block.get("tool_use_id")), None)
                    if planned is not None:
                        planned["toggle"] = _plan_toggle(block)
                        planned["plan_id"] = _plan_id(block)
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
                    found: list[tuple[int, dict[str, Any]]] = []
                    for pattern in patterns:
                        for match in pattern.finditer(command):
                            if _is_quoted_data(
                                command, match.start(), match.end(), quoted
                            ):
                                continue
                            group, sub = match.group(1), match.group(2)
                            label = f"{group} {sub}" if sub else group
                            commands[label] = commands.get(label, 0) + 1
                            plan = _PLAN_ARGUMENT.match(command, match.end())
                            found.append(
                                (
                                    match.start(),
                                    {
                                        "label": label,
                                        "plan": plan.group(1) if plan else None,
                                        "verdict": None,
                                        "flags": _flags(command, match.start(), quoted),
                                    },
                                )
                            )
                    # Within one command string, in the order the shell runs them.
                    ordered = [call for _, call in sorted(found, key=lambda f: f[0])]
                    sequence.extend(call["label"] for call in ordered)
                    calls.extend(ordered)
                    # One result cannot be split between two comparisons.
                    compares = [
                        call for call in ordered if call["label"] == "transform compare"
                    ]
                    if len(compares) == 1 and block.get("id"):
                        pending_compares[str(block["id"])] = compares[0]
                    # The same for a mutation run's survivor count (#1112).
                    mutations = [
                        call
                        for call in ordered
                        if call["label"] == "transform test"
                        and "mutate" in (call.get("flags") or [])
                    ]
                    if len(mutations) == 1 and block.get("id"):
                        pending_mutations[str(block["id"])] = mutations[0]
                    # And for a build's outcome, which says whether a repair
                    # built before the comparison that relies on it (#1114).
                    builds = [
                        call for call in ordered if call["label"] == "transform build"
                    ]
                    if len(builds) == 1 and block.get("id"):
                        pending_builds[str(block["id"])] = builds[0]
                    # And for what a plan said about a toggle contract (#1113).
                    plans = [
                        call for call in ordered if call["label"] == "transform plan"
                    ]
                    if len(plans) == 1 and block.get("id"):
                        pending_plans[str(block["id"])] = plans[0]
        if event.get("type") == "result":
            session.cost_usd = event.get("total_cost_usd")
            session.turns = event.get("num_turns")
            session.is_error = bool(event.get("is_error"))
            session.final = str(event.get("result") or "")[-2000:]
    session.skills_triggered = skills
    session.dex_commands = commands
    session.dex_sequence = sequence
    session.dex_calls = calls
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
    """Run one agent session on ``prompt`` in ``workspace``, recording its events to
    ``transcript``.
    """

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
