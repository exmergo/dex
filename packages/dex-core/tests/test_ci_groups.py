"""The CI test groups cover the suite, and the check that says so works.

CI runs the suite as several parallel jobs (tests/ci_groups.py), which is the
one way a test could stop running without anything failing. Two things guard
against it, and both are held here:

- The split itself: every group's prefixes name real test files, no prefix
  sits in two groups, the longest matching prefix decides, and the catch-all
  takes whatever no prefix names, so a new file always lands somewhere.
- The wiring and the after-the-fact proof: ci.yml builds its matrix from
  ci_groups.py, runs each group with a report, and gates on
  scripts/check_test_groups.py, whose verdicts on an overlap, an orphan, a
  missing group, and a test selected but not run are checked against
  hand-built reports.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import ci_groups
import pytest
import yaml

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECKER = ROOT / "scripts" / "check_test_groups.py"


def _test_files() -> list[str]:
    return sorted(
        path.relative_to(PACKAGE).as_posix()
        for path in (PACKAGE / "tests").rglob("test_*.py")
    )


# --- the split -----------------------------------------------------------------


def test_the_catch_all_exists_and_names_no_prefix():
    assert ci_groups.GROUPS[ci_groups.CATCH_ALL] == ()


def test_every_prefix_names_a_test_file_and_no_two_groups_share_one():
    files = _test_files()
    owner: dict[str, str] = {}
    for group, prefixes in ci_groups.GROUPS.items():
        for prefix in prefixes:
            assert any(path.startswith(prefix) for path in files), (
                f"{group!r} prefix {prefix!r} names no test file"
            )
            assert prefix not in owner, (
                f"{prefix!r} is in both {owner[prefix]!r} and {group!r}"
            )
            owner[prefix] = group


def test_the_longest_prefix_decides():
    assert ci_groups.group_of("tests/transform/test_mutation_command.py") == "mutation"
    assert ci_groups.group_of("tests/transform/test_build.py") == "transform"


def test_every_group_has_test_files():
    owners = {ci_groups.group_of(path) for path in _test_files()}
    assert owners == set(ci_groups.GROUPS)


def test_a_file_no_prefix_names_lands_in_the_catch_all():
    assert ci_groups.group_of("tests/brand_new_area/test_x.py") == ci_groups.CATCH_ALL


# --- the wiring ------------------------------------------------------------------


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else needs


def _grouped_job(jobs: dict) -> tuple[str, dict]:
    grouped = [
        (name, job)
        for name, job in jobs.items()
        if "--ci-group " in json.dumps(job.get("steps", []))
    ]
    assert len(grouped) == 1, "exactly one ci.yml job runs the suite by group"
    return grouped[0]


def test_the_matrix_comes_from_ci_groups_rather_than_a_list_of_its_own(jobs):
    _, job = _grouped_job(jobs)
    groups = job["strategy"]["matrix"]["group"]
    assert isinstance(groups, str) and "fromJSON(needs." in groups
    producer = groups.split("needs.", 1)[1].split(".", 1)[0]
    assert producer in _needs(job)
    assert "check_test_groups.py matrix" in json.dumps(jobs[producer]["steps"])


def test_every_group_writes_a_report_and_a_gate_verifies_them(jobs):
    name, job = _grouped_job(jobs)
    steps = json.dumps(job["steps"])
    assert "--ci-group-report" in steps
    assert "upload-artifact" in steps
    gates = [
        gate
        for gate in jobs.values()
        if name in _needs(gate)
        and "check_test_groups.py verify" in json.dumps(gate.get("steps", []))
    ]
    assert gates, "a job that needs the groups runs check_test_groups.py verify"
    assert all(gate.get("if") == "always()" for gate in gates)


# --- the proof -------------------------------------------------------------------


SUITE = ["tests/a.py::t1", "tests/a.py::t2", "tests/b.py::t1", "tests/c.py::t1"]


@pytest.fixture
def verify(monkeypatch):
    module = runpy.run_path(str(CHECKER))
    monkeypatch.setitem(module["verify"].__globals__, "_groups", lambda: ["one", "two"])
    return module["verify"]


def _report(tmp_path: Path, group: str, selected: list[str], **overrides) -> None:
    report = {
        "group": group,
        "python": "3.13",
        "collected": SUITE,
        "selected": selected,
        "ran": selected,
        **overrides,
    }
    (tmp_path / f"{group}-{len(list(tmp_path.iterdir()))}.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def test_a_clean_split_verifies(tmp_path, verify):
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", SUITE[2:])
    assert verify(tmp_path) == []


def test_a_test_in_no_group_fails(tmp_path, verify):
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", SUITE[2:3])
    assert any("in no group" in problem for problem in verify(tmp_path))


def test_a_test_in_two_groups_fails(tmp_path, verify):
    _report(tmp_path, "one", SUITE[:3])
    _report(tmp_path, "two", SUITE[2:])
    assert any("in both" in problem for problem in verify(tmp_path))


def test_a_group_that_never_reported_fails(tmp_path, verify):
    _report(tmp_path, "one", SUITE)
    assert any("'two' reported 0 times" in problem for problem in verify(tmp_path))


def test_a_test_selected_but_not_run_fails(tmp_path, verify):
    _report(tmp_path, "one", SUITE[:2], ran=SUITE[:1])
    _report(tmp_path, "two", SUITE[2:])
    assert any("did not run" in problem for problem in verify(tmp_path))


def test_groups_cut_from_different_suites_fail(tmp_path, verify):
    _report(tmp_path, "one", SUITE[:2], collected=SUITE[:3])
    _report(tmp_path, "two", SUITE[2:])
    assert any("different suite" in problem for problem in verify(tmp_path))


def test_no_reports_at_all_fails(tmp_path, verify):
    assert verify(tmp_path) != []
