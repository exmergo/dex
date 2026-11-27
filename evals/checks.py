"""Deterministic checks over a headless run's ``stream-json`` transcript.

The judge reads only the agent's final text, so it cannot tell which commands
the agent ran or in what order. A case's ``checks`` (``evals.suite.Check``) are
graded here instead, from the tool calls the transcript records, with no model
in the loop. ``run_error`` decides first whether the transcript describes a run
at all: a failed sign-in still ends with a ``result`` event of subtype
``success``, and only ``is_error`` and ``terminal_reason`` say it never ran, so
a check that would pass on an empty run (no forbidden command) is never read
from one.

Dex calls are read with the metadata eval's parser (``evals.metadata.live``),
which follows the skill wrapper however its path is spelled and ignores a Dex
command that is only quoted text. Calls through the ``Bash`` and
``PowerShell`` tools are both read, where the call names the wrapper; a
PowerShell call through a variable or ``Invoke-Expression`` is not seen, so a
case graded on Dex calls disallows PowerShell. ``no_dex_command`` matches its
pattern against the whole shell command a call sits in, so SQL a call reads
from a file (``--sql-file``) or a variable is not seen, and a file a shell
command writes is not seen by ``no_file_writes``; an allow-list as broad as
``Bash(uv run *)`` rules out neither.

Stdlib only, like the rest of the Tier-2 core.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .suite import Check

#: The tools that write a file. A Dex call that writes the project counts too:
#: those that always write, and ``config set``/``unset`` when confirmed. A
#: write made by a shell command (a redirect, ``sed -i``) is not read here, and
#: an allow-list as broad as ``Bash(uv run *)`` does not rule one out.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
WRITE_DEX_CALLS = frozenset({"transform apply", "transform init", "transform deps"})
CONFIRMED_WRITE_DEX_CALLS = frozenset({"config set", "config unset"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})


def parse_stream(text: str) -> list[dict[str, Any]]:
    """The events of a ``--output-format stream-json`` transcript, in order.

    A line that is not a JSON object is skipped: the CLI writes one event per
    line, and a stray line must not hide the events around it.
    """

    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _of_type(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == kind]


def final_text(events: list[dict[str, Any]]) -> str:
    """The run's final answer: the last ``result`` event's text."""

    results = _of_type(events, "result")
    return str(results[-1].get("result") or "") if results else ""


def tool_uses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``tool_use`` block, in order, a subagent's included."""

    uses = []
    for event in _of_type(events, "assistant"):
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content") or []
        uses.extend(
            block
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
    return uses


def skill_invoked(events: list[dict[str, Any]], name: str) -> bool:
    """Whether the agent loaded the skill ``name`` (``dex:maintain``)."""

    return any(
        use.get("name") == "Skill" and (use.get("input") or {}).get("skill") == name
        for use in tool_uses(events)
    )


def dex_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every Dex call the agent made, in order: its ``label`` (``maintain
    incidents``) and the whole ``command`` the Bash call ran."""

    # Imported here, not at the top: evals.metadata.live imports the live
    # backend, which imports the scoring core, which imports this module.
    from .metadata.live import Session, summarize

    calls = []
    for use in tool_uses(events):
        if use.get("name") not in SHELL_TOOLS:
            continue
        # One tool call at a time, so each call keeps the command it came from.
        # The parser reads the Bash tool; a PowerShell call is read the same
        # way, since the wrapper's command line is spelled alike in both.
        block = {**use, "name": "Bash"}
        session = summarize(
            [{"type": "assistant", "message": {"content": [block]}}], Session()
        )
        command = str((use.get("input") or {}).get("command") or "")
        calls.extend(
            {"label": c["label"], "flags": c.get("flags") or [], "command": command}
            for c in session.dex_calls
        )
    return calls


def run_error(
    events: list[dict[str, Any]],
    *,
    skill_enabled: bool,
    plugin: str = "dex",
    plugin_dir: Path | None = None,
) -> str | None:
    """Why the transcript describes no gradeable run, or ``None`` if it does.

    The plugin is part of the answer: the skill arm must have loaded the plugin
    under test from ``plugin_dir``, and the baseline arm must have loaded no
    plugin of that name, or the uplift compares one release with another.
    """

    inits = [e for e in _of_type(events, "system") if e.get("subtype") == "init"]
    if not inits:
        return "the transcript has no init event; the run did not start"
    results = _of_type(events, "result")
    if not results:
        return "the transcript has no result event; the run did not finish"
    result = results[-1]
    reason = str(result.get("terminal_reason") or "")
    subtype = str(result.get("subtype") or "")
    if subtype != "success":
        # `error_max_turns`, `error_max_budget_usd`: the run was cut off, and
        # a check of what it did not do would read the cut as compliance.
        return f"the run did not finish (result subtype {subtype or 'unset'})"
    if result.get("is_error") or "error" in reason:
        said = str(result.get("result") or "").strip().splitlines()
        return f"the run ended in error (terminal_reason {reason or 'unset'})" + (
            f": {said[0][:200]}" if said else ""
        )
    loaded = [
        p
        for p in inits[0].get("plugins") or []
        if isinstance(p, dict) and p.get("name") == plugin
    ]
    if skill_enabled and plugin_dir is not None:
        wanted = Path(plugin_dir).resolve()
        if not any(Path(str(p.get("path", ""))).resolve() == wanted for p in loaded):
            paths = [str(p.get("path")) for p in loaded] or ["none"]
            return (
                f"the {plugin} plugin under test ({wanted}) did not load; "
                f"loaded: {paths}"
            )
    if not skill_enabled and loaded:
        paths = [str(p.get("path")) for p in loaded]
        return (
            f"the baseline arm loaded a {plugin} plugin ({paths}); "
            "it must run without one"
        )
    return None


def grade(check: Check, events: list[dict[str, Any]]) -> tuple[bool, str]:
    """Whether ``events`` satisfy ``check``, and the reason, for the report."""

    calls = dex_calls(events)
    if check.check == "first_dex_command":
        in_group = [c for c in calls if c["label"].split()[0] == check.group]
        if not in_group:
            return False, f"no `{check.group}` command ran"
        first = in_group[0]["label"]
        position = calls.index(in_group[0]) + 1
        if first == check.command:
            return (
                True,
                f"the first `{check.group}` command is `{first}` (Dex call {position})",
            )
        return (
            False,
            f"the first `{check.group}` command is `{first}` (Dex call {position})",
        )
    if check.check == "no_dex_command":
        # Passing here on a run that made no Dex call at all would read "it did
        # not misuse the tool" off an agent that never used it.
        if not calls:
            return False, "no Dex command ran, so nothing was checked"
        pattern = re.compile(check.matching, re.IGNORECASE)
        hits = [
            c
            for c in calls
            if c["label"] == check.command and pattern.search(c["command"])
        ]
        if hits:
            return (
                False,
                f"{len(hits)} `{check.command}` call(s) match `{check.matching}`",
            )
        made = sum(c["label"] == check.command for c in calls)
        return True, f"{made} `{check.command}` call(s), none matching"
    if check.check == "no_file_writes":
        inside, _ = _file_writes(check, events)
        writes = [name for name, _ in inside]
        applies = [
            c["label"]
            for c in calls
            if c["label"] in WRITE_DEX_CALLS
            or (c["label"] in CONFIRMED_WRITE_DEX_CALLS and "confirm" in c["flags"])
        ]
        if writes or applies:
            shown = sorted({shown for _, shown in inside})
            where = f" ({shown})" if check.within and shown else ""
            return False, f"wrote with {sorted(set(writes + applies))}{where}"
        scope = " in the project" if check.within else ""
        return True, f"no file-writing tool call{scope} and no `transform apply`"
    raise ValueError(f"unknown check {check.check!r}")


def information(check: Check, events: list[dict[str, Any]]) -> str:
    """What a check saw that does not decide it, for the report: the files a
    ``no_file_writes`` with ``within`` saw written outside the project."""

    if check.check != "no_file_writes" or not check.within:
        return ""
    _, outside = _file_writes(check, events)
    if not outside:
        return ""
    return f"wrote outside the project: {sorted({shown for _, shown in outside})}"


def _file_writes(
    check: Check, events: list[dict[str, Any]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Each file-writing tool call as (tool, path shown), split into those the
    check counts and those outside its ``within``. Paths are read against the
    run's working directory, the ``init`` event's ``cwd``; with no ``cwd`` to
    read them against, every write counts, so an unreadable place never
    passes."""

    root = next(
        (
            str(e.get("cwd") or "")
            for e in _of_type(events, "system")
            if e.get("subtype") == "init"
        ),
        "",
    )
    inside: list[tuple[str, str]] = []
    outside: list[tuple[str, str]] = []
    failed = _failed_uses(events)
    for use in tool_uses(events):
        name = str(use.get("name"))
        if name not in WRITE_TOOLS or use.get("id") in failed:
            # A refused or failed write wrote nothing.
            continue
        payload = use.get("input") or {}
        path = str(payload.get("file_path") or payload.get("notebook_path") or "")
        if not check.within or not root or not path:
            inside.append((name, path))
            continue
        shown, counted = _place(path, root, check.within)
        (inside if counted else outside).append((name, shown))
    return inside, outside


#: A Windows path with a drive, absolute whatever OS reads the transcript.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _segments(path: str) -> list[str]:
    """A path's parts as written, with either slash: a transcript's paths are
    another machine's, so they are read by their spelling, never against this
    filesystem. Compare them with ``casefold``."""

    out: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." and out:
            out.pop()
            continue
        out.append(part)
    return out


def _place(path: str, root: str, within: list[str]) -> tuple[str, bool]:
    """``path`` as shown in the report (from the workspace down, when it is
    under it), and whether it sits under one of ``within``.

    The workspace is placed by its own directory name, the last part of
    ``root``, which is unique to the run (``tempfile.mkdtemp``). So a path is
    under it however the part above it is spelled: Git Bash's ``/c/Users/...``,
    an 8.3 short name, ``~/...``. A relative path is read from ``root``."""

    relative = not (path.startswith(("/", "\\", "~")) or _DRIVE.match(path))
    base = _segments(root)
    parts = base + _segments(path) if relative else _segments(path)
    folded = [p.casefold() for p in parts]
    name = base[-1].casefold() if base else ""
    if not name or name not in folded:
        return path, False
    start = len(folded) - folded[::-1].index(name)
    below = parts[start:]
    for scope in within:
        wanted = [p.casefold() for p in _segments(scope)]
        if wanted and folded[start : start + len(wanted)] == wanted:
            return "/".join(below), True
    return "/".join(below), False


def _failed_uses(events: list[dict[str, Any]]) -> set[str]:
    """The ids of the tool calls whose result is an error: refused by the
    permissions, or failed."""

    failed: set[str] = set()
    for event in _of_type(events, "user"):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("is_error")
            ):
                failed.add(str(block.get("tool_use_id")))
    return failed
