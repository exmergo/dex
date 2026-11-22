#!/usr/bin/env python3
"""Fail when a pytest run that must run everything skipped something.

Usage:
    python3 scripts/check_no_skips.py <junit.xml> [<junit.xml>...]

Some suites skip themselves when an opt-in variable is unset, and pytest counts
a skip as a pass. In a CI step that exists to run such a suite, a dropped
variable, a missing executable or an absent extra would then read as green
while nothing ran. This reads the JUnit XML pytest wrote (`--junitxml`) and
exits 1 when any test case was skipped, naming each one with its reason, or
when the files hold no test case at all. It exits 0 otherwise. pytest writes an
expected failure (`xfail`) as a skip too, so one fails the check as well, named
as an expected failure rather than as a skip: in such a step a known failure is
something to fix or to move out, not to carry.

Standard library only, so CI runs it without installing anything.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check(paths: list[Path]) -> list[str]:
    """Return what is wrong, one message per problem; empty means it passes."""

    problems: list[str] = []
    ran = 0
    for path in paths:
        try:
            # The report is the one pytest just wrote in the same job.
            root = ET.parse(path).getroot()  # noqa: S314
        except (OSError, ET.ParseError) as exc:
            problems.append(f"{path}: cannot read the JUnit report ({exc})")
            continue
        for case in root.iter("testcase"):
            ran += 1
            skipped = case.find("skipped")
            if skipped is None:
                continue
            name = "::".join(
                part for part in (case.get("classname"), case.get("name")) if part
            )
            reason = skipped.get("message") or "no reason given"
            what = (
                "is an expected failure"
                if skipped.get("type") == "pytest.xfail"
                else "was skipped"
            )
            problems.append(f"{path}: {name} {what}: {reason}")
    if ran == 0 and not problems:
        problems.append(f"no test case in {', '.join(map(str, paths))}")
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: check_no_skips.py <junit.xml> [<junit.xml>...]", file=sys.stderr)
        return 2
    problems = check([Path(arg) for arg in argv])
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(
            f"{len(problems)} problem(s): this step runs a suite that skips itself "
            "when its opt-in is missing, so a skip here is a failure, not a pass.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
