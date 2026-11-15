#!/usr/bin/env python3
"""Run a developer tool at the version packages/dex-core/uv.lock pins.

Usage:
    python scripts/uvx_locked.py TOOL [ARGS...]
    python scripts/uvx_locked.py --version TOOL

The first form runs ``uvx TOOL@<locked version> ARGS...`` and exits with its
status; the second prints the locked version and runs nothing. For example::

    python scripts/uvx_locked.py ruff check .
    python scripts/uvx_locked.py towncrier build --draft --version preview

Why this exists: Ruff and towncrier are pinned once, in the engine's ``[dev]``
extra, and Dependabot moves that pin and the lockfile together. Every other
caller (the Lint and Changelog workflows, the pre-commit hooks, the release
script) used to restate the version by hand, and Dependabot could not see those
copies, so each tool bump failed CI until someone edited them (#1137). Reading
the lock instead leaves one place to change, and the one Dependabot already
changes. ``uvx`` keeps each call a single cached tool fetch rather than a sync of
the whole project.

Stdlib only, and readable by any Python 3 (macOS still ships a 3.9 as
``/usr/bin/python3``), so it runs before anything is installed: the lockfile is
read line by line for its ``[[package]]`` tables rather than with ``tomllib``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packages" / "dex-core" / "uv.lock"

USAGE = "usage: uvx_locked.py TOOL [ARGS...] | uvx_locked.py --version TOOL"


class LockError(Exception):
    """The lockfile does not settle one version for the tool."""


def normalize(name: str) -> str:
    """A distribution name as the lockfile writes it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


_KEY = re.compile(r'^(name|version)\s*=\s*"([^"]*)"\s*$')


def packages(lock_text: str) -> list[dict[str, str]]:
    """Each ``[[package]]`` table's top-level ``name`` and ``version``.

    uv writes both as the first plain keys of every table, before any sub-table
    or inline array, so reading those two keys is enough and needs no TOML parser.
    """
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in lock_text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            current = {} if line == "[[package]]" else None
            if current is not None:
                found.append(current)
            continue
        if current is None:
            continue
        match = _KEY.match(line)
        if match and match.group(1) not in current:
            current[match.group(1)] = match.group(2)
    return found


def locked_version(tool: str, lock_text: str) -> str:
    """The one version ``lock_text`` pins for ``tool``.

    Refuses a tool the lock does not hold, and one it holds at more than one
    version (a fork by Python version or platform), since then there is no single
    version to run and guessing one would be the drift this script removes.
    """
    wanted = normalize(tool)
    versions = sorted(
        {
            package["version"]
            for package in packages(lock_text)
            if normalize(package.get("name", "")) == wanted and "version" in package
        }
    )
    if not versions:
        raise LockError(
            f"{tool} is not in {LOCK.relative_to(ROOT)}; add it to the [dev] extra "
            "in packages/dex-core/pyproject.toml and run `uv lock`"
        )
    if len(versions) > 1:
        raise LockError(
            f"{LOCK.relative_to(ROOT)} pins {tool} at {', '.join(versions)}; "
            "expected exactly one version"
        )
    return versions[0]


def command(tool: str, version: str, args: Sequence[str]) -> list[str]:
    """The argv that runs ``tool`` at ``version``: ``uvx``, else ``uv tool run``."""
    runner = ["uvx"] if shutil.which("uvx") else ["uv", "tool", "run"]
    return [*runner, f"{tool}@{version}", *args]


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 2
    print_only = argv[0] == "--version"
    if print_only:
        if len(argv) != 2:
            print(USAGE, file=sys.stderr)
            return 2
        tool, args = argv[1], []
    else:
        tool, args = argv[0], list(argv[1:])
    try:
        version = locked_version(tool, LOCK.read_text(encoding="utf-8"))
    except (OSError, LockError) as error:
        print(f"uvx_locked.py: {error}", file=sys.stderr)
        return 1
    if print_only:
        print(version)
        return 0
    # A child process rather than exec, so the exit status reaches the caller the
    # same way on Windows, where os.exec* does not replace the process.
    return subprocess.run(command(tool, version, args), check=False).returncode  # noqa: S603


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
