#!/usr/bin/env python3
"""Check the changelog fragments in changelog.d/, and that a pull request has one.

Usage:
    uv run --project packages/dex-core --no-sync python \
        scripts/check_changelog_fragments.py
    git diff --name-status --no-renames origin/main... \\
        | uv run --project packages/dex-core --no-sync python \
            scripts/check_changelog_fragments.py --changes -
    ... --changes - --labels no-changelog

With no arguments it checks every file in changelog.d/: a name towncrier will
collect, a type towncrier.toml declares, and a body that says something. With
`--changes` (the pull request's `git diff --name-status --no-renames` output) it
also requires a fragment and refuses a direct CHANGELOG.md edit, unless the
`no-changelog` label is among `--labels`.

Prints what to do and exits 1 when anything fails, exits 0 otherwise. The types
and ignored names are read from towncrier.toml so they are declared in one place.
Standard library only, so CI runs it without installing anything.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Collection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = "https://github.com/exmergo/dex/blob/main/CONTRIBUTING.md#changelog-fragments"
EXEMPT_LABEL = "no-changelog"
CHANGELOG = "CHANGELOG.md"


def check(
    root: Path, changes: str | None = None, labels: Collection[str] = ()
) -> list[str]:
    """Return what is wrong, one message per problem; empty means it passes.

    `changes` is `git diff --name-status --no-renames` output for a pull request,
    or None to check only the fragments already in the tree.
    """

    config = tomllib.loads((root / "towncrier.toml").read_text(encoding="utf-8"))
    towncrier = config["tool"]["towncrier"]
    directory = towncrier["directory"]
    types = [entry["directory"] for entry in towncrier["type"]]
    ignored = {name.lower() for name in towncrier.get("ignore", [])}
    # Stricter than towncrier on purpose: it would also collect `613.fixed` or
    # `613.fixed.txt`, and an empty file as a bare link with no text.
    fragment = re.compile(
        r"(?:\d+|\+[A-Za-z0-9][A-Za-z0-9_-]*)"
        rf"\.(?:{'|'.join(map(re.escape, types))})(?:\.\d+)?\.md"
    )
    naming = (
        f"Name it {directory}/<pull request number>.<type>.md, or "
        f"{directory}/+<short-slug>.<type>.md before the pull request has a "
        f"number, where <type> is one of: {', '.join(types)}."
    )

    problems = []
    for entry in sorted((root / directory).iterdir()):
        if entry.name.lower() in ignored:
            continue
        shown = f"{directory}/{entry.name}"
        if entry.is_dir():
            problems.append(
                f"{shown}/ is a directory. Fragments go directly in {directory}/."
            )
        elif not fragment.fullmatch(entry.name):
            problems.append(f"{shown} is not named as a fragment. {naming}")
        elif not entry.read_text(encoding="utf-8").strip():
            problems.append(
                f"{shown} is empty. Describe the change as a user will see it."
            )

    if changes is None or EXEMPT_LABEL in labels:
        return problems

    touched = {}
    for line in changes.splitlines():
        status, _, path = line.partition("\t")
        if path:
            touched[path] = status[:1]
    if CHANGELOG in touched:
        problems.append(
            f"This pull request edits {CHANGELOG}, which is written when a release "
            "is prepared. Move your entry into a fragment instead: the same text, "
            "without the leading '- ' and without a link to this pull request, "
            f"which the release adds. {naming} Then restore the file:\n"
            f"    git fetch origin && git checkout origin/main -- {CHANGELOG}\n"
            f"A release-preparation pull request carries the {EXEMPT_LABEL} label "
            "instead."
        )
    elif not any(
        status != "D"
        and path.startswith(f"{directory}/")
        and path.rpartition("/")[2].lower() not in ignored
        for path, status in touched.items()
    ):
        problems.append(
            f"This pull request adds no changelog fragment. {naming} If the change "
            "is invisible to users (documentation, CI, tests or release "
            f"preparation only), a maintainer applies the {EXEMPT_LABEL} label "
            "instead."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--changes",
        metavar="FILE",
        help="the pull request's `git diff --name-status --no-renames`, - for stdin",
    )
    parser.add_argument(
        "--labels", default="", help="the pull request's labels, comma-separated"
    )
    args = parser.parse_args(argv)

    changes = None
    if args.changes == "-":
        changes = sys.stdin.read()
    elif args.changes is not None:
        changes = Path(args.changes).read_text(encoding="utf-8")
    labels = {label.strip() for label in args.labels.split(",") if label.strip()}

    problems = check(ROOT, changes, labels)
    if not problems:
        print("Changelog fragments are in order.")
        return 0
    for problem in problems:
        print(f"error: {problem}\n", file=sys.stderr)
    print(f"How to add a changelog fragment: {GUIDE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
