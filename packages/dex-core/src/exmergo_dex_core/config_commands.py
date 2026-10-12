"""``dex config get|validate|set``: read, check and change ``.dex/config.yml``.

These verbs run before any engine exists, and that is the point of them. Every
other command builds its engine from this file, so a file that cannot build one
would otherwise leave no command able to say what is wrong with it, let alone fix
it. They touch only the file: no connection, no store, no dialect engine. That
keeps them working on the lightest install, and on a config whose ``cache:``
block is the very thing that is broken.

``set`` proposes before it writes, through the same handshake a billed command
uses. Without ``--confirm`` it answers ``needs_confirmation`` with the diff and
writes nothing; the same command with ``--confirm`` writes. The file is committed
and carries its reasoning in comments, so a change to it is something a person
reviews first, the way they review a plan.
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
    """A ``set`` that would leave the file unusable, refused before any write."""

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
    """What a ``set`` changes, and whether it has been written yet."""

    path: str
    changes: list[dict[str, Any]] = Field(default_factory=list)
    written: bool = False

    def data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "changed": bool(self.changes),
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
    root: Path | str, pairs: list[str], *, confirmed: bool
) -> ConfigChangeResult:
    """Set one or more keys, as a diff to confirm and then as a write.

    Several pairs are applied as one change and validated once, at the end,
    because some settings are only valid together: ``semantic.vendor ossie``
    needs ``semantic.ossie.files`` and each is refused without the other.

    Refuses a change that adds a problem stopping commands. A problem already in
    the file is reported and does not block, so a broken file can be repaired one
    key at a time; this is the only verb that can repair one at all, since every
    other command refuses to start on it.

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
    path = _committed_config(root)
    before = ConfigDocument.read(path)
    after = before
    changes: list[dict[str, Any]] = []
    for key, text in zip(keys, pairs[1::2], strict=True):
        value = parse_value(key, text)
        current = after.written(key)
        if current is not ABSENT and current == value:
            continue
        after = after.with_value(key, value)
        change: dict[str, Any] = {"key": key, "after": value}
        if current is not ABSENT:
            change["before"] = current
        changes.append(change)
    if not changes:
        return ConfigChangeResult(
            path=str(path),
            notes=["every key already has that value; nothing to change"],
            warnings=before.warnings(),
        )

    already = {problem.identity for problem in before.problems()}
    introduced = [
        problem
        for problem in after.problems()
        if problem.fatal and problem.identity not in already
    ]
    if introduced:
        raise ConfigChangeRefusedError(introduced)
    warnings = [
        *after.edit_warnings,
        *(f"{CONFIG_RELPATH} {problem.describe()}" for problem in after.problems()),
    ]
    diff = after.diff_from(before)
    if not confirmed:
        return ConfigChangeResult(
            path=str(path),
            changes=changes,
            diffs=[diff],
            warnings=warnings,
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
    return ConfigChangeResult(
        path=str(path),
        changes=changes,
        written=True,
        diffs=[diff],
        warnings=warnings,
    )


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
        return to_envelope(
            config_set(
                root,
                list(getattr(args, "pairs", None) or []),
                confirmed=bool(getattr(args, "confirm", False)),
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
