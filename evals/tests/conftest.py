"""Put the repo root on sys.path so ``import evals`` works wherever pytest runs.

Also the CI eval groups (``eval_groups.py``): ``--eval-group NAME`` runs only
the tests that file assigns to that group, and ``--eval-group-report PATH``
writes what was collected, selected, run and skipped, for
``scripts/check_test_groups.py verify --suite evals`` to prove the groups ran
every eval between them, once each, with none skipped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_groups  # noqa: E402

_COLLECTED = pytest.StashKey[list[str]]()
_SELECTED = pytest.StashKey[list[str]]()
_RAN: list[str] = []
_SKIPPED: list[str] = []
# pytest's node id (relative to its rootdir) to the repo-root-relative one.
_IDS: dict[str, str] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("eval groups")
    group.addoption(
        "--eval-group",
        choices=sorted(eval_groups.GROUPS),
        help="run only the tests evals/tests/eval_groups.py assigns to this group",
    )
    group.addoption(
        "--eval-group-report",
        metavar="PATH",
        help=(
            "write what was collected, selected, run and skipped as JSON, for "
            "scripts/check_test_groups.py to prove the groups cover the evals"
        ),
    )


def _nodeid(item: pytest.Item) -> str:
    """The node id relative to the repo root, wherever pytest put its rootdir."""

    path = item.path.resolve().relative_to(ROOT).as_posix()
    _, sep, rest = item.nodeid.partition("::")
    return path + sep + rest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    _IDS.update((item.nodeid, _nodeid(item)) for item in items)
    config.stash[_COLLECTED] = [_IDS[item.nodeid] for item in items]
    group = config.getoption("--eval-group")
    if group is None:
        return
    keep, drop = [], []
    for item in items:
        mine = eval_groups.group_of(_IDS[item.nodeid]) == group
        (keep if mine else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    config.stash[_SELECTED] = [_IDS[item.nodeid] for item in keep]


def pytest_collectreport(report: pytest.CollectReport) -> None:
    # A module skipped at import (``pytest.importorskip`` at the top) collects
    # no tests at all, so it would otherwise be absent rather than skipped.
    if report.skipped:
        _SKIPPED.append(report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        _SKIPPED.append(_IDS.get(report.nodeid, report.nodeid))


def pytest_runtest_logfinish(nodeid: str) -> None:
    _RAN.append(_IDS.get(nodeid, nodeid))


def pytest_sessionfinish(session: pytest.Session) -> None:
    config = session.config
    report = config.getoption("--eval-group-report")
    if not report:
        return
    collected = config.stash.get(_COLLECTED, [])
    Path(report).write_text(
        json.dumps(
            {
                "group": config.getoption("--eval-group"),
                "python": "{}.{}".format(*sys.version_info[:2]),
                "collected": collected,
                "selected": config.stash.get(_SELECTED, collected),
                "ran": _RAN,
                "skipped": sorted(set(_SKIPPED)),
            }
        ),
        encoding="utf-8",
    )
