#!/usr/bin/env python3
"""The CI test groups, and the proof that together they ran the whole suite.

Usage:
    python scripts/check_test_groups.py matrix [--suite engine|evals]
    python scripts/check_test_groups.py verify REPORT_DIR [--suite engine|evals]

Two suites are split this way. ``engine`` (the default) is the engine's unit
tests, grouped by packages/dex-core/tests/ci_groups.py; ``evals`` is the eval
harness's tests, grouped by evals/tests/eval_groups.py.

``matrix`` prints the suite's group names as a JSON list, which ci.yml turns
into its matrix, so a group cannot exist in that file and be missing from CI.

``verify`` reads every report a grouped run wrote (``pytest --ci-group NAME
--ci-group-report PATH``, or ``--eval-group``/``--eval-group-report`` for the
evals, searched for recursively under REPORT_DIR) and fails
unless, for every Python version that reported:

  - every group reported exactly once, and nothing reported a group that does
    not exist;
  - every group collected the same suite, so the groups are cut from one whole;
  - the groups' selections do not overlap and add up to that whole suite;
  - every group ran every test it selected.

The ``evals`` suite is held to two more, because every eval runs in CI with
everything it needs installed and is cut test by test rather than file by file:

  - no test was skipped, and no test module was skipped at import, so an eval
    cannot pass by not running;
  - every prefix in eval_groups.py names at least one collected test, so a
    renamed eval cannot leave a group cut around nothing.

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
# Each suite's groups file, and whether it is held to the strict checks.
SUITES: dict[str, tuple[Path, bool]] = {
    "engine": (ROOT / "packages" / "dex-core" / "tests" / "ci_groups.py", False),
    "evals": (ROOT / "evals" / "tests" / "eval_groups.py", True),
}


def _module(suite: str) -> dict:
    return runpy.run_path(str(SUITES[suite][0]))


def _groups(suite: str = "engine") -> list[str]:
    return list(_module(suite)["GROUPS"])


def _strict_problems(suite: str, where: str, runs: dict, whole: set[str]) -> list[str]:
    problems: list[str] = []
    skipped = set().union(*(set(run.get("skipped", [])) for run in runs.values()))
    if skipped:
        problems.append(
            f"{where}: {len(skipped)} tests or modules were skipped:" + _sample(skipped)
        )
    module = _module(suite)
    matches = module["matches"]
    problems.extend(
        f"{where}: group {group!r} prefix {prefix!r} names no collected test"
        for group, prefixes in module["GROUPS"].items()
        for prefix in prefixes
        if not any(matches(nodeid, prefix) for nodeid in whole)
    )
    return problems


def _sample(ids: set[str], limit: int = 10) -> str:
    shown = sorted(ids)[:limit]
    more = f"\n      ... and {len(ids) - limit} more" if len(ids) > limit else ""
    return "".join(f"\n      {nodeid}" for nodeid in shown) + more


def verify(report_dir: Path, suite_name: str = "engine") -> list[str]:
    groups = _groups(suite_name)
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
        if SUITES[suite_name][1]:
            problems.extend(_strict_problems(suite_name, where, runs, whole))
        if len(problems) == before:
            sizes = ", ".join(f"{g} {len(r['selected'])}" for g, r in runs.items())
            print(
                f"{where}: {len(whole)} tests, every one in exactly one group ({sizes})"
            )
    return problems


def main(argv: list[str]) -> int:
    suite = "engine"
    if len(argv) >= 2 and argv[-2] == "--suite" and argv[-1] in SUITES:
        suite, argv = argv[-1], argv[:-2]
    if argv[:1] == ["matrix"] and len(argv) == 1:
        print(json.dumps(_groups(suite)))
        return 0
    if argv[:1] == ["verify"] and len(argv) == 2:
        problems = verify(Path(argv[1]), suite)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
