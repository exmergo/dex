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
command that is only quoted text. It reads the ``Bash`` tool only, so a case
graded here disallows the ``PowerShell`` tool.

Stdlib only, like the rest of the Tier-2 core.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .suite import Check

#: The tools that write a file. A Dex call that writes the project
#: (``transform apply``) counts too.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
WRITE_DEX_CALLS = frozenset({"transform apply"})


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
        content = (event.get("message") or {}).get("content") or []
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
        if use.get("name") != "Bash":
            continue
        # One tool call at a time, so each call keeps the command it came from.
        session = summarize(
            [{"type": "assistant", "message": {"content": [use]}}], Session()
        )
        command = str((use.get("input") or {}).get("command") or "")
        calls.extend(
            {"label": c["label"], "command": command} for c in session.dex_calls
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
        writes = [
            u.get("name") for u in tool_uses(events) if u.get("name") in WRITE_TOOLS
        ]
        applies = [c["label"] for c in calls if c["label"] in WRITE_DEX_CALLS]
        if writes or applies:
            return False, f"wrote with {sorted(set(writes + applies))}"
        return True, "no file-writing tool call and no `transform apply`"
    raise ValueError(f"unknown check {check.check!r}")
