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
pattern against the whole shell command a call sits in, and for a
``--sql-file`` call against the file it names as the transcript shows it
written; SQL held in a shell variable is not seen. ``no_file_writes`` reads
the file-writing tool calls and, where the backend compared the workspace
before and after the run (``AgentResult.changes``), every file the run
changed, so a write a shell command made is caught there.

Stdlib only, like the rest of the Tier-2 core.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .suite import Check

#: The tools that write a file. A write made by a shell command (a redirect,
#: ``sed -i``) is not a tool call; the workspace comparison catches it.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: Dex calls that write the project or ``.dex/`` (a plan, a snapshot): read
#: from the command surface in AGENTS.md. ``transform place`` writes unless
#: ``--explain``; ``transform test`` writes in either of its two modes (a
#: stored plan, or a mutation record); every ``semantic`` command stores a
#: plan. The engine's own bookkeeping on a read (``.dex/cache.json``,
#: ``.dex/queries.jsonl``) is not a write here.
WRITE_DEX_CALLS = frozenset(
    {
        "transform apply",
        "transform init",
        "transform deps",
        "transform plan",
        "transform declare",
        "transform rename",
        "transform remove",
        "transform repair",
        "transform build",
        "transform test",
        "maintain snapshot",
        "maintain reconcile",
        "demo",
    }
)
WRITE_DEX_GROUPS = frozenset({"semantic"})
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


def grade(
    check: Check,
    events: list[dict[str, Any]],
    changes: list[str] | None = None,
) -> tuple[bool, str]:
    """Whether ``events`` satisfy ``check``, and the reason, for the report.

    ``changes`` is what the run changed in its workspace (``AgentResult.
    changes``), where the backend could read it: a ``no_file_writes`` with
    ``within`` then fails on a change under the project however it was made
    (a shell command, a script, a Dex command), not only on a write the tool
    calls show. A call made with ``--help`` does nothing and is not read.
    """

    calls = [c for c in dex_calls(events) if "help" not in c["flags"]]
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
            if c["label"] == check.command and _reads_match(c, events, pattern)
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
        applies = [c["label"] for c in calls if _writes_project(c)]
        changed, _ = _changes(check, changes)
        if writes or applies or changed:
            said = []
            if writes or applies:
                shown = sorted({shown for _, shown in inside})
                where = f" ({shown})" if check.within and shown else ""
                said.append(f"wrote with {sorted(set(writes + applies))}{where}")
            if changed:
                said.append(f"changed in the project: {changed}")
            return False, "; ".join(said)
        scope = " in the project" if check.within else ""
        read = "" if changes is None else ", and nothing in it changed"
        return True, f"no file-writing tool call{scope} and no Dex write{read}"
    raise ValueError(f"unknown check {check.check!r}")


def information(
    check: Check,
    events: list[dict[str, Any]],
    changes: list[str] | None = None,
) -> str:
    """What a check saw that does not decide it, for the report: the files a
    ``no_file_writes`` with ``within`` saw written outside the project."""

    if check.check != "no_file_writes" or not check.within:
        return ""
    _, outside = _file_writes(check, events)
    _, elsewhere = _changes(check, changes)
    shown = sorted({shown for _, shown in outside} | set(elsewhere))
    return f"wrote outside the project: {shown}" if shown else ""


def _writes_project(call: dict[str, Any]) -> bool:
    label, flags = call["label"], call["flags"]
    if label in WRITE_DEX_CALLS or label.split()[0] in WRITE_DEX_GROUPS:
        return True
    if label in CONFIRMED_WRITE_DEX_CALLS:
        return "confirm" in flags
    if label == "transform place":
        return "explain" not in flags
    return False


def _changes(check: Check, changes: list[str] | None) -> tuple[list[str], list[str]]:
    """The workspace's changed files, split into those under ``within`` (minus
    ``ignore``) and the rest (minus ``ignore``); both empty when the backend
    could not read the workspace."""

    if changes is None or not check.within:
        return [], []
    ignored = {_folded(p) for p in check.ignore}
    inside, outside = [], []
    for path in changes:
        if _folded(path) in ignored:
            continue
        parts = _folded(path).split("/")
        under = any(
            parts[: len(scope)] == scope
            for scope in (_folded(w).split("/") for w in check.within)
        )
        (inside if under else outside).append(path)
    return sorted(inside), sorted(outside)


def _folded(path: str) -> str:
    return "/".join(_segments(path)).casefold()


def _reads_match(
    call: dict[str, Any], events: list[dict[str, Any]], pattern: re.Pattern
) -> bool:
    """Whether what a call reads matches: its own command, and for a
    ``--sql-file`` call also every file the transcript shows written whose
    name the command holds, and every other shell command that names it (a
    heredoc). A ``--sql-file`` whose SQL the transcript does not show counts
    as a match, since an unread file is no evidence it is clean."""

    text = call["command"]
    if pattern.search(text):
        return True
    if "sql-file" not in call["flags"]:
        return False
    found = _SQL_FILE.search(text)
    argument = next((g for g in found.groups() if g), "") if found else ""
    name = _segments(argument)[-1].casefold() if _segments(argument) else ""
    seen: list[str] = []
    for use in tool_uses(events) if name else []:
        payload = use.get("input") or {}
        if use.get("name") in WRITE_TOOLS:
            path = str(payload.get("file_path") or payload.get("notebook_path") or "")
            parts = _segments(path)
            if parts and parts[-1].casefold() == name:
                seen.append(_written_text(payload))
        elif use.get("name") in SHELL_TOOLS:
            other = str(payload.get("command") or "")
            if other != text and name in other.casefold():
                seen.append(other)
    # Nothing showing what the file holds counts as a hit.
    return not seen or any(pattern.search(s) for s in seen)


#: The file a ``--sql-file`` argument names, quoted or not.
_SQL_FILE = re.compile(r"""--sql-file(?:=|\s+)(?:"([^"]+)"|'([^']+)'|(\S+))""")


def _written_text(payload: dict[str, Any]) -> str:
    parts = [str(payload.get("content") or ""), str(payload.get("new_string") or "")]
    parts += [str(e.get("new_string") or "") for e in payload.get("edits") or []]
    return " ".join(parts)


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
