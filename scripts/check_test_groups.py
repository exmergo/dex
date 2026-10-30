#!/usr/bin/env python3
"""The CI test groups, and the proof that together they ran the whole suite.

Usage:
    python scripts/check_test_groups.py matrix
    python scripts/check_test_groups.py verify REPORT_DIR

``matrix`` prints the group names in packages/dex-core/tests/ci_groups.py as a
JSON list, which ci.yml turns into its test matrix, so a group cannot exist in
that file and be missing from CI.

``verify`` reads every report a grouped run wrote (``pytest --ci-group NAME
--ci-group-report PATH``, searched for recursively under REPORT_DIR) and fails
unless, for every Python version that reported:

  - every group reported exactly once, and nothing reported a group that does
    not exist;
  - every group collected the same suite, so the groups are cut from one whole;
  - the groups' selections do not overlap and add up to that whole suite;
  - every group ran every test it selected.

Splitting the suite is the one way a test could silently stop running that a
single ``pytest`` invocation never had; this is the check that it has not.
Stdlib only, so the job running it installs nothing.
"""

from __future__ import annotations

import json
import runpy
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS_FILE = ROOT / "packages" / "dex-core" / "tests" / "ci_groups.py"


def _groups() -> list[str]:
    return list(runpy.run_path(str(GROUPS_FILE))["GROUPS"])


def _sample(ids: set[str], limit: int = 10) -> str:
    shown = sorted(ids)[:limit]
    more = f"\n      ... and {len(ids) - limit} more" if len(ids) > limit else ""
    return "".join(f"\n      {nodeid}" for nodeid in shown) + more


def verify(report_dir: Path) -> list[str]:
    groups = _groups()
    reports = sorted(report_dir.rglob("*.json"))
    if not reports:
        return [f"no group reports under {report_dir}"]

    by_python: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        by_python[report["python"]][report["group"]].append(report)

    problems: list[str] = []
    for python, by_group in sorted(by_python.items()):
        where = f"Python {python}"
        before = len(problems)
        problems.extend(
            f"{where}: a report for {group!r}, which is not a group"
            for group in sorted(set(by_group) - set(groups), key=str)
        )
        for group in groups:
            count = len(by_group.get(group, []))
            if count != 1:
                problems.append(
                    f"{where}: group {group!r} reported {count} times, not once"
                )
        runs = {group: by_group[group][0] for group in groups if by_group.get(group)}
        if not runs:
            continue

        suites = {group: set(run["collected"]) for group, run in runs.items()}
        whole = set().union(*suites.values())
        for group, suite in suites.items():
            if suite != whole:
                problems.append(
                    f"{where}: group {group!r} collected a different suite; missing:"
                    + _sample(whole - suite)
                )

        owner: dict[str, str] = {}
        for group, run in runs.items():
            selected = run["selected"]
            if len(selected) != len(set(selected)):
                problems.append(f"{where}: group {group!r} selected a test twice")
            for nodeid in selected:
                if nodeid in owner:
                    problems.append(
                        f"{where}: {nodeid} is in both {owner[nodeid]!r} and {group!r}"
                    )
                owner.setdefault(nodeid, group)
            not_ran = set(selected) - set(run["ran"])
            if not_ran:
                problems.append(
                    f"{where}: group {group!r} selected tests it did not run:"
                    + _sample(not_ran)
                )

        orphans = whole - set(owner)
        if orphans:
            problems.append(
                f"{where}: {len(orphans)} collected tests are in no group:"
                + _sample(orphans)
            )
        strays = set(owner) - whole
        if strays:
            problems.append(
                f"{where}: tests selected that no group collected:" + _sample(strays)
            )
        if len(problems) == before:
            sizes = ", ".join(f"{g} {len(r['selected'])}" for g, r in runs.items())
            print(
                f"{where}: {len(whole)} tests, every one in exactly one group ({sizes})"
            )
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] == ["matrix"] and len(argv) == 1:
        print(json.dumps(_groups()))
        return 0
    if argv[:1] == ["verify"] and len(argv) == 2:
        problems = verify(Path(argv[1]))
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
