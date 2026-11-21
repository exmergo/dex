#!/usr/bin/env python3
"""The versions a release writes, held to the shape each reader expects.

Usage:
    python3 scripts/check_release_versions.py [--root DIR]

`scripts/prepare_release.sh <engine-version> <plugin-semver>` writes two
versions into the repository. The engine version (PEP 440, no leading ``v``) is
pinned in the three skill wrappers and in hatch-vcs's ``fallback_version``, and
titles the newest ``CHANGELOG.md`` section. The plugin version (semver) goes into
every agent's plugin manifest and the copy the Claude directory reads under
``claude/``. The release workflow re-runs the script on the tagged commit, which
proves the files are what the script wrote, but not that the script was handed
the right strings: ``prepare_release.sh 1.18.1 v1.18.1`` writes
``"version": "v1.18.1"`` into every manifest, the re-run reproduces it, and
nothing fails. So this reads the files and checks what each reader needs:

- every engine pin is the same version, a canonical PEP 440 public version;
- the newest ``CHANGELOG.md`` section is that version, with a date;
- every manifest, ``claude/`` included, carries the same version, a valid
  semver 2.0 string (which has no leading ``v``);
- the plugin version is the engine version's release (``1.18.1`` for ``1.18.1``,
  ``0.1.0-alpha.1`` for ``0.1.0a1``): the major, minor and patch agree.

Every invariant holds between releases too, since only a release moves these
files, so the check can run on any commit. CI runs it on a pull request that
touches them (.github/workflows/release-prep.yml). Stdlib only, so the job
installs nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WRAPPERS = tuple(
    f"skills/{skill}/scripts/run.py" for skill in ("explore", "transform", "maintain")
)
ENGINE_PYPROJECT = "packages/dex-core/pyproject.toml"
CHANGELOG = "CHANGELOG.md"
MARKER = "<!-- towncrier release notes start -->"
MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "plugin.json",
    "gemini-extension.json",
    "claude/.claude-plugin/plugin.json",
)

#: A canonical PEP 440 public version: what the built wheel is named after, so
#: an alias (``1.0.0-alpha1``, ``v1.0.0``) would not match the wheel the release
#: workflow asserts on.
PEP440 = re.compile(
    r"^(?P<release>(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*)"
    r"(?:(?:a|b|rc)(?:0|[1-9]\d*))?"
    r"(?:\.post(?:0|[1-9]\d*))?"
    r"(?:\.dev(?:0|[1-9]\d*))?$"
)
#: The semver 2.0 grammar, from semver.org.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)
PIN = re.compile(r'^DEX_CORE_VERSION = "(?P<version>[^"]*)"', re.MULTILINE)
FALLBACK = re.compile(r'^fallback_version = "(?P<version>[^"]*)"', re.MULTILINE)
SECTION = re.compile(r"^## \[(?P<version>[^\]]*)\](?P<rest>.*)$", re.MULTILINE)
DATED = re.compile(r"^ - \d{4}-\d{2}-\d{2}$")

FIX = (
    "Re-run scripts/prepare_release.sh <engine-version> <plugin-semver> with both "
    "versions bare (1.18.1, not v1.18.1), then commit the result "
    "(git add claude && git commit -a)."
)


def _read(root: Path, path: str, problems: list[str]) -> str | None:
    try:
        return (root / path).read_text(encoding="utf-8")
    except OSError as error:
        problems.append(f"{path}: cannot be read ({error.strerror})")
        return None


def _engine_versions(root: Path, problems: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in WRAPPERS:
        text = _read(root, path, problems)
        if text is None:
            continue
        match = PIN.search(text)
        if match is None:
            problems.append(f"{path}: no DEX_CORE_VERSION pin")
        else:
            found[path] = match["version"]
    text = _read(root, ENGINE_PYPROJECT, problems)
    if text is not None:
        match = FALLBACK.search(text)
        if match is None:
            problems.append(f"{ENGINE_PYPROJECT}: no fallback_version")
        else:
            found[f"{ENGINE_PYPROJECT} fallback_version"] = match["version"]
    return found


def _plugin_versions(root: Path, problems: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in MANIFESTS:
        text = _read(root, path, problems)
        if text is None:
            continue
        try:
            version = json.loads(text).get("version")
        except (json.JSONDecodeError, AttributeError):
            problems.append(f"{path}: is not a JSON object")
            continue
        if not isinstance(version, str):
            problems.append(f"{path}: has no string version")
        else:
            found[path] = version
    return found


def _newest_section(root: Path, problems: list[str]) -> tuple[str, str] | None:
    text = _read(root, CHANGELOG, problems)
    if text is None:
        return None
    _, marker, released = text.partition(MARKER)
    if not marker:
        problems.append(f'{CHANGELOG}: no "{MARKER}" line')
        return None
    match = SECTION.search(released)
    if match is None:
        problems.append(f"{CHANGELOG}: no released section below the marker")
        return None
    return match["version"], match["rest"]


def _one(found: dict[str, str], what: str, problems: list[str]) -> str | None:
    values = set(found.values())
    if len(values) == 1:
        return values.pop()
    if values:
        listed = "; ".join(f"{path} = {value!r}" for path, value in found.items())
        problems.append(f"the {what} disagree: {listed}")
    return None


def check(root: Path) -> list[str]:
    """Every problem with the release versions under ``root``; empty when sound."""

    problems: list[str] = []
    engine_found = _engine_versions(root, problems)
    plugin_found = _plugin_versions(root, problems)
    section = _newest_section(root, problems)

    engine = _one(engine_found, "engine version pins", problems)
    release: tuple[int, ...] | None = None
    if engine is not None:
        match = PEP440.fullmatch(engine)
        if match is None:
            problems.append(
                f"engine version {engine!r} is not a canonical PEP 440 version "
                "(no leading v; 0.1.0a1, not 0.1.0-alpha1)"
            )
        else:
            release = tuple(int(part) for part in match["release"].split("."))

    plugin = _one(plugin_found, "plugin manifest versions", problems)
    if plugin is not None:
        match = SEMVER.fullmatch(plugin)
        if match is None:
            problems.append(
                f"plugin version {plugin!r} is not a semver 2.0 version "
                "(no leading v; 0.1.0-alpha.1, not 0.1.0a1)"
            )
        elif release is not None:
            core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
            padded = (*release, 0, 0, 0)[:3]
            if core != padded or len(release) > 3:
                problems.append(
                    f"plugin version {plugin!r} is not a release of engine "
                    f"version {engine!r}: major, minor and patch must agree"
                )

    if section is not None and engine is not None:
        version, rest = section
        if version != engine:
            problems.append(
                f"the newest {CHANGELOG} section is [{version}], "
                f"but the engine is pinned to {engine!r}"
            )
        if not DATED.match(rest):
            problems.append(
                f"the newest {CHANGELOG} section [{version}] "
                "has no ' - YYYY-MM-DD' date"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="the repository to check (default: this checkout)",
    )
    args = parser.parse_args(argv)
    problems = check(args.root)
    if problems:
        print("The release versions are not what a release needs:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(FIX, file=sys.stderr)
        return 1
    print("Release versions are consistent: engine pins, CHANGELOG.md and manifests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
