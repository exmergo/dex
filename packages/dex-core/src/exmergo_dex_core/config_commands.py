"""``dex config get|validate|set|unset``: read, check and change ``.dex/config.yml``.

These verbs run before any engine exists, and that is the point of them. Every
other command builds its engine from this file, so a file that cannot build one
would otherwise leave no command able to say what is wrong with it, let alone fix
it. They touch only the file: no connection, no store, no dialect engine. That
keeps them working on the lightest install, and on a config whose ``cache:``
block is the very thing that is broken.

``set`` and ``unset`` propose before they write, through the same handshake a
billed command uses. Without ``--confirm`` they answer ``needs_confirmation`` with
the diff and write nothing; the same command with ``--confirm`` writes. The file
is committed and carries its reasoning in comments, so a change to it is
something a person reviews first, the way they review a plan.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field

from . import envelope as env
from .command_args import repo_root
from .config import config_path
from .config_document import (
    ABSENT,
    CONFIG_RELPATH,
    ConfigDocument,
    ConfigProblem,
    InvalidConfigError,
    parse_value,
)
from .errors import ConfigurationError, RequestError
from .results import ConfirmationRequest, Result, to_envelope
from .session_cap import (
    SESSION_CEILING_CAP_ENV,
    SessionCeilingCap,
    moot_write_warning,
    parse_session_ceiling_cap,
)
from .values_free import caller_only_message

# Flags every subcommand accepts that change one run rather than the project.
# The config verbs read and write the committed file and build no engine, so
# each of these would be accepted and then silently do nothing, which is the
# shape this codebase refuses everywhere else.
_RUN_OVERRIDES = (
    ("connector", "--connector"),
    ("path", "--path"),
    ("scope", "--scope"),
    ("project", "--project"),
    ("dataset", "--dataset"),
    ("budget", "--budget"),
    ("cache_backend", "--cache-backend"),
    ("project_format", "--project-format"),
    ("session_ceiling", "--session-ceiling"),
    ("no_session_ceiling", "--no-session-ceiling"),
)


class ConfigChangeRefusedError(RequestError):
    """A ``set`` or ``unset`` that would leave the file unusable, refused before
    any write."""

    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = list(problems)
        listed = "; ".join(problem.describe() for problem in self.problems)
        super().__init__(
            f"this change would leave {CONFIG_RELPATH} unusable, so nothing was "
            f"changed: {listed}"
        )


class ConfigSettingsResult(Result):
    """The effective settings, one row per value, with where each came from."""

    path: str
    key: str | None = None
    settings: list[dict[str, Any]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"path": self.path, "key": self.key, "settings": self.settings}


class ConfigValidationResult(Result):
    """Every problem in the file. An empty list is the answer "usable as is"."""

    path: str
    problems: list[ConfigProblem] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "valid": not self.problems,
            "problems": [problem.data() for problem in self.problems],
        }


class ConfigChangeResult(Result):
    """What a ``set`` or ``unset`` changes, one row per value, and whether it has
    been written yet."""

    path: str
    changes: list[dict[str, Any]] = Field(default_factory=list)
    written: bool = False

    def data(self) -> dict[str, Any]:
        has_diff = any(
            bool(d.get("unified")) for d in (self.diffs or []) if isinstance(d, dict)
        )
        return {
            "path": self.path,
            "changed": bool(self.changes or has_diff),
            "written": self.written,
            "changes": self.changes,
        }


def config_get(
    root: Path | str, key: str | None, *, environ: Mapping[str, str]
) -> ConfigSettingsResult:
    """The effective value of ``key`` (or of every setting) and where it came from.

    Refuses a file that no command could load, naming its problems, because a
    value read out of a file the engine would refuse is not a value anything uses.
    """

    path = _committed_config(root)
    document = ConfigDocument.read(path)
    settings = document.settings(key, environ=environ)
    return ConfigSettingsResult(
        path=str(path),
        key=key,
        settings=[setting.data() for setting in settings],
        warnings=document.warnings(),
    )


def config_validate(root: Path | str) -> ConfigValidationResult:
    """Every problem in the file, located by setting and line. Runs nothing else."""

    path = _committed_config(root)
    document = ConfigDocument.read(path)
    return ConfigValidationResult(path=str(path), problems=document.problems())


def config_set(
    root: Path | str,
    pairs: list[str],
    *,
    confirmed: bool,
    session_cap: SessionCeilingCap | None = None,
) -> ConfigChangeResult:
    """Set one or more keys, as a diff to confirm and then as a write.

    Several pairs are applied as one change and validated once, at the end,
    because some settings are only valid together: ``semantic.vendor ossie``
    needs ``semantic.ossie.files`` and each is refused without the other.

    Refuses a change that adds a problem stopping commands. A problem already in
    the file is reported and does not block, so a broken file can be repaired one
    key at a time; with ``unset``, these are the only verbs that can repair one
    at all, since every other command refuses to start on it.

    Unconfirmed, nothing is written and the result waits on the confirmation.
    Confirmed, the file is re-read and the change re-made against what it holds
    now, so an edit someone made in between survives.
    """

    if not pairs or len(pairs) % 2:
        raise RequestError(
            "dex config set takes KEY VALUE pairs, e.g. dex config set "
            "query.max_rows 100 (several pairs make one change)"
        )
    keys = pairs[0::2]
    if repeated := sorted({key for key in keys if keys.count(key) > 1}):
        raise RequestError(f"{', '.join(repeated)} is set more than once in one call")
    if caller_only := sorted(
        {key for key in keys if caller_only_message(key) is not None}
    ):
        raise RequestError(
            f"{', '.join(caller_only)} {caller_only_message(caller_only[0])}"
        )
    path = _committed_config(root)
    before = ConfigDocument.read(path)
    after = before
    changed: list[str] = []
    for key, text in zip(keys, pairs[1::2], strict=True):
        value = parse_value(key, text)
        current = after.written(key)
        if current is not ABSENT and current == value:
            continue
        after = after.with_value(key, value)
        changed.append(key)
    if not changed:
        return ConfigChangeResult(
            path=str(path),
            notes=["every key already has that value; nothing to change"],
            warnings=before.warnings(),
        )
    return _propose(
        path, before, after, changed, confirmed=confirmed, session_cap=session_cap
    )


def config_unset(
    root: Path | str,
    keys: list[str],
    *,
    confirmed: bool,
    duplicate: bool = False,
    session_cap: SessionCeilingCap | None = None,
) -> ConfigChangeResult:
    """Remove one or more keys, as a diff to confirm and then as a write.

    Takes any key the file writes, including one the schema does not know:
    removing a mistyped or retired key is the main use, and those are exactly the
    keys the schema cannot vouch for. A block left with no keys goes with its
    last one. Several keys are one change, validated once, as with ``set``, and a
    removal that would add a problem stopping commands is refused the same way.

    If a key is written more than once, removes the earlier occurrence(s) that
    YAML ignored and keeps the winning value.

    A key the file does not write is a note rather than a refusal, since the file
    already says what was asked. The note names the nearest key the file does
    write, because the likeliest reason is a typo in the key being removed.

    A key inside one item of a list is refused: a list changes as a whole, which
    is how ``set`` writes one too.
    """

    path = _committed_config(root)
    before = ConfigDocument.read(path)
    if duplicate and not keys:
        dup_problems = before.duplicate_problems()
        if not dup_problems:
            return ConfigChangeResult(
                path=str(path),
                notes=[f"no keys are set twice in {CONFIG_RELPATH}"],
                warnings=before.warnings(),
            )
        keys = list(dict.fromkeys(p.key for p in dup_problems))

    if not keys:
        raise RequestError(
            "dex config unset takes one or more keys, e.g. dex config unset "
            "budget.session_celing (several keys make one change)"
        )
    if repeated := sorted({key for key in keys if keys.count(key) > 1}):
        raise RequestError(f"{', '.join(repeated)} is named more than once in one call")
    for key in keys:
        if any(not part for part in key.split(".")):
            raise RequestError(
                f"'{key}' is not a setting key; keys are dotted paths such as "
                "budget.session_ceiling"
            )
        if "[" in key:
            listed = key.split("[", 1)[0]
            raise RequestError(
                f"'{key}' names a key inside one item of a list, and a list changes "
                f"as a whole: dex config get {listed} shows the list without any key "
                f"dex does not read, and dex config set {listed} '<list>' writes it "
                "back"
            )
    if nested := sorted(
        {inner for inner in keys for outer in keys if inner.startswith(outer + ".")}
    ):
        raise RequestError(
            f"{', '.join(nested)} is inside another key this call removes; name only "
            "the outer one"
        )
    after = before
    removed: list[str] = []
    notes: list[str] = []
    for key in keys:
        dup = after._locate_duplicate(key.split("."))
        if dup is not None:
            mapping, dup_indices, win_idx = dup
            dup_lines = [mapping.value[i][0].start_mark.line + 1 for i in dup_indices]
            win_line = mapping.value[win_idx][0].start_mark.line + 1
            after = after.without(key, duplicate_only=True)
            removed.append(key)
            lines_str = (
                f"line {dup_lines[0]}"
                if len(dup_lines) == 1
                else f"lines {', '.join(str(n) for n in dup_lines)}"
            )
            notes.append(
                f"{key} is set twice in {CONFIG_RELPATH}; removed the ignored "
                f"occurrence on {lines_str} (the value on line {win_line} was kept)"
            )
            continue

        if duplicate:
            notes.append(
                f"{key} is not set twice in {CONFIG_RELPATH}, so there is no "
                "duplicate to remove"
            )
            continue

        # Through `without` even for a key that may be absent, so a file the
        # editor refuses (one that does not parse, say) is refused rather than
        # answered "not there".
        edited = after.without(key)
        if edited is after:
            nearest = before.closest_written(key)
            hint = f"; did you mean {nearest}?" if nearest else ""
            notes.append(
                f"{key} is not in {CONFIG_RELPATH}, so there is nothing to remove{hint}"
            )
            continue
        after = edited
        removed.append(key)
    for key in removed:
        parts = key.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            if before.written(parent) is not ABSENT and after.written(parent) is ABSENT:
                notes.append(f"{parent} had no other settings, so it is removed too")
                break
    notes = list(dict.fromkeys(notes))
    if not removed:
        return ConfigChangeResult(
            path=str(path), notes=notes, warnings=before.warnings()
        )
    return _propose(
        path,
        before,
        after,
        removed,
        confirmed=confirmed,
        notes=notes,
        session_cap=session_cap,
    )


def _propose(
    path: Path,
    before: ConfigDocument,
    after: ConfigDocument,
    keys: list[str],
    *,
    confirmed: bool,
    notes: list[str] | None = None,
    session_cap: SessionCeilingCap | None = None,
) -> ConfigChangeResult:
    """A change to the file: refused, proposed as a diff, or written.

    Refuses a change that adds a problem stopping commands. A problem already in
    the file is reported and does not block, so a broken file can be repaired one
    key at a time. Unconfirmed, nothing is written and the result waits on the
    confirmation. Confirmed, it is written; the caller made ``after`` from the
    file as this command read it, so an edit made between the proposal and the
    confirmation survives.
    """

    already = {problem.identity for problem in before.problems()}
    introduced = [
        problem
        for problem in after.problems()
        if problem.fatal and problem.identity not in already
    ]
    if introduced:
        raise ConfigChangeRefusedError(introduced)
    proposal: dict[str, Any] = {
        "path": str(path),
        "changes": after.changes_from(before, keys),
        "diffs": [after.diff_from(before)],
        "warnings": [
            *after.edit_warnings,
            *(f"{CONFIG_RELPATH} {problem.describe()}" for problem in after.problems()),
            *_moot_under_cap(before, after, keys, session_cap),
        ],
        "notes": list(notes or []),
    }
    if not confirmed:
        return ConfigChangeResult(
            **proposal,
            pending_confirmation=ConfirmationRequest(
                data={
                    "hint": (
                        f"nothing has been written; review the diff to "
                        f"{CONFIG_RELPATH}, then re-run the same command with "
                        "--confirm to write it"
                    )
                }
            ),
        )
    after.write(path)
    return ConfigChangeResult(**proposal, written=True)


def _moot_under_cap(
    before: ConfigDocument,
    after: ConfigDocument,
    keys: list[str],
    session_cap: SessionCeilingCap | None,
) -> list[str]:
    """The warning a change to the daily ceiling carries when a host's cap
    (#787) leaves the ceiling in force where it was: the write is proposed and
    written as asked, and says it is moot while the cap holds."""

    if session_cap is None or not any(
        key == "budget" or key.startswith("budget.session_ceiling") for key in keys
    ):
        return []
    return moot_write_warning(
        session_cap,
        before=_written_ceiling(before),
        after=_written_ceiling(after),
        what=f"this change to {', '.join(keys)}",
    )


def _written_ceiling(document: ConfigDocument) -> float | None:
    """The daily ceiling a document writes, or None where it writes none (or
    something no command would load, which the problems already report)."""

    value = document.written("budget.session_ceiling")
    if value is ABSENT or value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cmd_config(args: argparse.Namespace) -> env.Envelope:
    """The CLI shim. ``main`` routes here before it builds an engine."""

    root = repo_root(args)
    try:
        passed = [
            flag
            for attribute, flag in _RUN_OVERRIDES
            if getattr(args, attribute, None) not in (None, False)
        ]
        if passed:
            raise RequestError(
                f"{', '.join(passed)} changes one run, and dex config reads and "
                f"writes the committed {CONFIG_RELPATH}, which no run override "
                "touches; to change a setting for the project, use dex config set "
                "<key> <value>"
            )
        if args.subcommand == "get":
            return to_envelope(
                config_get(root, getattr(args, "key", None), environ=os.environ)
            )
        if args.subcommand == "validate":
            result = config_validate(root)
            if not result.problems:
                return to_envelope(result)
            # An error, not an `ok` carrying problems, so `dex config validate`
            # exits non-zero and works as a CI gate on its own.
            return env.Envelope(
                status=env.Status.ERROR,
                reason=env.Reason.CONFIGURATION,
                errors=[problem.describe() for problem in result.problems],
                data=result.data(),
            )
        confirmed = bool(getattr(args, "confirm", False))
        session_cap = parse_session_ceiling_cap(
            os.environ.get(SESSION_CEILING_CAP_ENV), source="environment"
        )
        if args.subcommand == "unset":
            return to_envelope(
                config_unset(
                    root,
                    list(getattr(args, "keys", None) or []),
                    confirmed=confirmed,
                    duplicate=bool(getattr(args, "duplicate", False)),
                    session_cap=session_cap,
                )
            )
        return to_envelope(
            config_set(
                root,
                list(getattr(args, "pairs", None) or []),
                confirmed=confirmed,
                session_cap=session_cap,
            )
        )
    except (InvalidConfigError, ConfigChangeRefusedError) as exc:
        return env.error_for(
            exc,
            data={"problems": [problem.data() for problem in exc.problems]},
        )
    except (ConfigurationError, RequestError) as exc:
        return env.error_for(exc)


def _committed_config(root: Path | str) -> Path:
    path = config_path(root)
    if path is None:
        raise ConfigurationError(
            f"no {CONFIG_RELPATH} found at '{root}' or above it within the "
            "repository; `dex transform init` writes one for a dbt project, and "
            "`dex demo` writes one beside a local demo warehouse"
        )
    return path
