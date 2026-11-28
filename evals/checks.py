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
import os
import re
from dataclasses import dataclass
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
    incidents``), its ``flags``, the whole ``command`` the shell call ran, and
    for a ``--sql-file`` call the file its own invocation names
    (``sql_file``), not the first one the command holds."""

    calls = []
    denied = _denied_uses(events)
    for position, use in enumerate(tool_uses(events)):
        if use.get("name") not in SHELL_TOOLS or str(use.get("id")) in denied:
            # A call the permissions refused never ran. One that ran and
            # returned an error still did.
            continue
        command = str((use.get("input") or {}).get("command") or "")
        for start, label, flags, quoted in _calls_in(command):
            calls.append(
                {
                    "label": label,
                    "flags": flags,
                    "command": command,
                    "position": position,
                    "sql_file": (
                        _sql_file_argument(command, start, quoted)
                        if "sql-file" in flags
                        else None
                    ),
                }
            )
    return calls


def _calls_in(command: str) -> list[tuple[int, str, list[str], list[tuple[int, int]]]]:
    """The Dex calls in one shell command, in the order the shell runs them,
    found as the metadata eval's parser finds them (``summarize``), with
    where each starts. A PowerShell call is read the same way, since the
    wrapper's command line is spelled alike in both shells."""

    # Imported here, not at the top: evals.metadata.live imports the live
    # backend, which imports the scoring core, which imports this module.
    from .metadata import live

    patterns = [live._DEX_CALL] + [live._alias_call(n) for n in live._aliases(command)]
    quoted = live._quoted_spans(command)
    found = []
    for pattern in patterns:
        for match in pattern.finditer(command):
            if live._is_quoted_data(command, match.start(), match.end(), quoted):
                continue
            group, sub = match.group(1), match.group(2)
            label = f"{group} {sub}" if sub else group
            flags = live._flags(command, match.start(), quoted)
            found.append((match.start(), label, flags, quoted))
    return sorted(found, key=lambda f: f[0])


def _sql_file_argument(
    command: str, start: int, quoted: list[tuple[int, int]]
) -> str | None:
    """The ``--sql-file`` argument of the call starting at ``start``: the
    first one after it that is not inside quoted text. The call's own flags
    say it has one, and they are read up to its own separator, so this is
    that one."""

    for match in _SQL_FILE.finditer(command, start):
        if any(b <= match.start() < e for b, e in quoted):
            continue
        return next((g for g in match.groups() if g), None)
    return None


def _denied_uses(events: list[dict[str, Any]]) -> set[str]:
    """The ids of the tool calls the permissions refused: a ``system``
    ``permission_denied`` event names each, and the ``result`` lists them."""

    denied = {
        str(e.get("tool_use_id"))
        for e in _of_type(events, "system")
        if e.get("subtype") == "permission_denied" and e.get("tool_use_id")
    }
    for result in _of_type(events, "result"):
        for item in result.get("permission_denials") or []:
            if isinstance(item, dict) and item.get("tool_use_id"):
                denied.add(str(item["tool_use_id"]))
    return denied


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

    if changes is None:
        return [], []
    ignored = {_folded(p) for p in check.ignore}
    kept = [p for p in changes if _folded(p) not in ignored]
    if not check.within:
        # Unscoped: every change counts.
        return sorted(kept), []
    inside, outside = [], []
    for path in kept:
        parts = [p.casefold() for p in _segments(path)]
        # `_segments(".")` is empty: the scope is the whole workspace.
        under = any(
            parts[: len(scope)] == scope
            for scope in ([p.casefold() for p in _segments(w)] for w in check.within)
        )
        (inside if under else outside).append(path)
    return sorted(inside), sorted(outside)


def _folded(path: str) -> str:
    """A path inside the workspace, as this machine's file system compares it:
    the workspace changes come from it, case-insensitively on Windows."""

    joined = "/".join(_segments(path))
    return joined.casefold() if os.name == "nt" else joined


def _reads_match(
    call: dict[str, Any], events: list[dict[str, Any]], pattern: re.Pattern
) -> bool:
    """Whether what a call reads matches: its own command, and for a
    ``--sql-file`` call also the file its own invocation names.

    The file's contents count as established only by a successful ``Write``
    of the whole file, before the query, at a path that is the same file
    inside the workspace, with nothing touching that file between the
    ``Write`` and the query. Anything else leaves them unknown: an ``Edit``
    or ``MultiEdit``, a failed or refused write, a shell command that names
    the file (a redirect, an append, a ``cat``), the query's own command
    naming it again, or a write at a spelling that cannot be placed. Unknown
    contents count as a match, since an unread file is no evidence it is
    clean."""

    text = call["command"]
    if pattern.search(text):
        return True
    if "sql-file" not in call["flags"]:
        return False
    argument = call.get("sql_file")
    root = _root(events)
    target = _locate(argument or "", root)
    if target is None:
        return True
    name = target.parts[-1].casefold() if target.parts else ""
    failed = _failed_uses(events) | _denied_uses(events)
    contents: str | None = None
    for use in tool_uses(events)[: call["position"]]:
        payload = use.get("input") or {}
        tool = use.get("name")
        if tool in WRITE_TOOLS:
            path = str(payload.get("file_path") or payload.get("notebook_path") or "")
            place = _locate(path, root)
            if place is None or place != target:
                same_name = bool(_segments(path)) and (
                    _segments(path)[-1].casefold() == name
                )
                unplaced = place is None or not place.inside or not target.inside
                if (same_name and unplaced) or (
                    place is not None and place.could_be(target)
                ):
                    # A spelling that cannot be told apart from the file: an
                    # unplaceable path, or one an 8.3 short name may alias.
                    contents = None
                continue
            ok = tool == "Write" and str(use.get("id")) not in failed
            contents = str(payload.get("content") or "") if ok else None
        elif (
            tool in SHELL_TOOLS
            and name
            and name in str(payload.get("command") or "").casefold()
        ):
            contents = None
    if name and text.casefold().count(name) > 1:
        # The query's own command does something else with the file too.
        contents = None
    return contents is None or bool(pattern.search(contents))


def _root(events: list[dict[str, Any]]) -> str:
    """The run's working directory, from the ``init`` event."""

    return next(
        (
            str(e.get("cwd") or "")
            for e in _of_type(events, "system")
            if e.get("subtype") == "init"
        ),
        "",
    )


#: The file a ``--sql-file`` argument names, quoted or not.
_SQL_FILE = re.compile(r"""--sql-file(?:=|\s+)(?:"([^"]+)"|'([^']+)'|(\S+))""")


def _file_writes(
    check: Check, events: list[dict[str, Any]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Each file-writing tool call as (tool, path shown), split into those the
    check counts and those outside its ``within``. Paths are placed against
    the run's working directory, the ``init`` event's ``cwd``; with no
    ``cwd`` to place them against, every write counts, so an unreadable place
    never passes. A write that failed or that the permissions refused wrote
    nothing."""

    root = _root(events)
    inside: list[tuple[str, str]] = []
    outside: list[tuple[str, str]] = []
    skipped = _failed_uses(events) | _denied_uses(events)
    for use in tool_uses(events):
        name = str(use.get("name"))
        if name not in WRITE_TOOLS or str(use.get("id")) in skipped:
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
#: Git Bash's spelling of a drive: `/c/Users/...`.
_GIT_BASH_DRIVE = re.compile(r"^[A-Za-z]$")
#: An 8.3 short name: up to six characters, `~`, a number (`PROGRA~1`).
_SHORT_NAME = re.compile(r"^(.{1,6})~\d+$")


def _segments(path: str) -> list[str]:
    """A path's parts as written, with either slash: a transcript's paths are
    another machine's, so they are read by their spelling, never against this
    filesystem."""

    out: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." and out:
            out.pop()
            continue
        out.append(part)
    return out


@dataclass(frozen=True)
class _Located:
    """Where a path is: ``inside`` the workspace with ``parts`` below it, or
    outside it with its whole ``parts``; compared case-insensitively only
    for a Windows-style workspace (``folds``)."""

    inside: bool
    parts: tuple[str, ...]
    folds: bool

    def __eq__(self, other: object) -> bool:
        """The same file, shown: each part equal as spelled (case-insensitively
        on Windows). An 8.3 short name is not proof: `FOOBAR~1` may be
        `foobaralpha` or `foobarbeta`."""

        if not isinstance(other, _Located):
            return NotImplemented
        return self._matches(other, short_names=False)

    def could_be(self, other: _Located) -> bool:
        """Possibly the same file: equal once an 8.3 short name is allowed to
        stand for a long name it abbreviates."""

        return self._matches(other, short_names=True)

    def _matches(self, other: _Located, *, short_names: bool) -> bool:
        if self.inside != other.inside or len(self.parts) != len(other.parts):
            return False
        return all(
            _same_part(a, b, self.folds, short_names=short_names)
            for a, b in zip(self.parts, other.parts, strict=True)
        )

    def __hash__(self) -> int:
        return hash((self.inside, len(self.parts)))


def _windows_style(path: str) -> bool:
    return bool(_DRIVE.match(path))


def _absolute(path: str, root: str) -> list[str] | None:
    """``path`` as absolute parts, read against ``root``, with every ``.``
    and ``..`` resolved after the join, so `../x.sql` from `/work` is
    `/x.sql`, not inside `/work`. The known alias spellings of a Windows path
    are turned into its drive form: Git Bash's `/c/...`, and `~` for this
    user's home. ``None`` for no path."""

    if not path:
        return None
    windows = _windows_style(root)
    if path.startswith("~"):
        home = str(Path.home())
        rest = path[1:].lstrip("/\\")
        return _dots(_segments(home) + _raw_parts(rest))
    if _DRIVE.match(path):
        return _dots(_raw_parts(path))
    if path.startswith(("/", "\\")):
        parts = _dots(_raw_parts(path))
        if windows and parts and _GIT_BASH_DRIVE.match(parts[0]):
            return [f"{parts[0].upper()}:", *parts[1:]]
        return parts
    return _dots(_segments(root) + _raw_parts(path))


def _raw_parts(path: str) -> list[str]:
    """A path's parts with either slash, ``.`` and ``..`` kept for ``_dots``."""

    return [p for p in path.replace("\\", "/").split("/") if p]


def _dots(parts: list[str]) -> list[str]:
    """``parts`` with ``.`` dropped and each ``..`` taking off the part before
    it. A ``..`` with nothing left to take off is kept, so a path that climbs
    above the top never lands inside the workspace."""

    out: list[str] = []
    for part in parts:
        if part == ".":
            continue
        if part == ".." and out and out[-1] != "..":
            out.pop()
            continue
        out.append(part)
    return out


def _locate(path: str, root: str) -> _Located | None:
    """Where ``path`` is against the workspace ``root``: under it when its
    parts begin with all of the root's parts (each compared as
    ``_same_part`` compares them), not when the workspace's name merely
    appears in it."""

    base = _segments(root)
    parts = _absolute(path, root)
    if parts is None or not base:
        return None
    folds = _windows_style(root)
    if len(parts) >= len(base) and all(
        _same_part(a, b, folds) for a, b in zip(parts, base, strict=False)
    ):
        return _Located(True, tuple(parts[len(base) :]), folds)
    return _Located(False, tuple(parts), folds)


def _same_part(a: str, b: str, folds: bool, *, short_names: bool = True) -> bool:
    """One path part against another: exactly on a case-sensitive file
    system; on Windows case-insensitively, and (with ``short_names``) an 8.3
    short name (`PROGRA~1`) against a long name it may abbreviate. That is
    enough to place a path in the workspace, never to prove two paths are
    one file."""

    if not folds:
        return a == b
    if a.casefold() == b.casefold():
        return True
    return short_names and (_short_for(a, b) or _short_for(b, a))


def _short_for(short: str, long: str) -> bool:
    match = _SHORT_NAME.match(short)
    if not match:
        return False
    squeezed = long.replace(" ", "").replace(".", "").upper()
    return squeezed.startswith(match.group(1).upper()) and squeezed != short.upper()


def _place(path: str, root: str, within: list[str]) -> tuple[str, bool]:
    """``path`` as shown in the report (from the workspace down, when it is
    under it), and whether it sits under one of ``within``.

    A path is under the workspace when it begins with the workspace's own
    path, in any of its known spellings: as `cwd` gives it, Git Bash's
    `/c/...`, `~` for the home directory, 8.3 short names. `/srv/work/x.sql`
    is not under `/work`. A relative path is read from the workspace."""

    place = _locate(path, root)
    if place is None or not place.inside:
        return path, False
    below = list(place.parts)
    for scope in within:
        # An empty scope (`"."`) is the whole workspace.
        wanted = _segments(scope)
        if len(below) >= len(wanted) and all(
            _same_part(a, b, place.folds) for a, b in zip(below, wanted, strict=False)
        ):
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
