"""The CI eval groups cover every eval, and the check that says so works.

CI runs the eval tests as several parallel jobs (eval_groups.py), which is the
one way an eval could stop running without anything failing. Held here, as
packages/dex-core/tests/test_ci_groups.py holds the engine's split:

- The split: every prefix names a real test file and test function, no prefix
  sits in two groups, a prefix stops at a whole test name, the longest prefix
  decides, and the catch-all takes whatever no prefix names.
- The wiring: ci.yml builds its eval matrix from eval_groups.py, runs each
  group with a report, and gates on ``check_test_groups.py verify --suite
  evals``.
- The proof: that check fails a skipped eval, a module skipped at import and a
  prefix naming no collected test, besides everything the engine's check fails.

Stdlib only except the workflow read, which needs PyYAML and so runs in the
grouped CI run, where nothing may skip.
"""

from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path

import eval_groups
import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECKER = ROOT / "scripts" / "check_test_groups.py"


def _prefixes() -> list[tuple[str, str]]:
    return [
        (group, prefix)
        for group, prefixes in eval_groups.GROUPS.items()
        for prefix in prefixes
    ]


# --- the split -----------------------------------------------------------------


def test_the_catch_all_exists_and_names_no_prefix():
    assert eval_groups.GROUPS[eval_groups.CATCH_ALL] == ()


def test_every_prefix_names_a_test_file_and_function():
    for group, prefix in _prefixes():
        path, _, name = prefix.partition("::")
        file = ROOT / path
        assert file.is_file() and file.name.startswith("test_"), (
            f"{group!r} prefix {prefix!r} names no test file"
        )
        name = name.split("[", 1)[0]
        if not name:
            continue
        tests = {
            node.name
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        }
        assert any(
            eval_groups.matches(f"{path}::{test}", f"{path}::{name}") for test in tests
        ), f"{group!r} prefix {prefix!r} names no test function in {path}"


def test_no_two_groups_share_a_prefix():
    owner: dict[str, str] = {}
    for group, prefix in _prefixes():
        assert prefix not in owner, (
            f"{prefix!r} is in both {owner[prefix]!r} and {group!r}"
        )
        owner[prefix] = group


def test_a_prefix_stops_at_a_whole_test_name():
    prefix = "evals/tests/test_x.py::test_a"
    assert eval_groups.matches("evals/tests/test_x.py::test_a", prefix)
    assert eval_groups.matches("evals/tests/test_x.py::test_a[p]", prefix)
    assert not eval_groups.matches("evals/tests/test_x.py::test_a_longer", prefix)
    assert eval_groups.matches("evals/tests/test_x.py::test_a", "evals/tests/test_x.py")
    assert eval_groups.matches("evals/tests/test_x.py::test_a_b", prefix + "_")


def test_the_longest_prefix_decides():
    reference = (
        "evals/tests/test_metadata.py::test_every_reference_answer_scores_perfect"
    )
    assert eval_groups.group_of(f"{reference}[share/build_product_share]") == (
        "references-a"
    )
    assert eval_groups.group_of(f"{reference}[composite_grain/build_x]") == (
        "references-b"
    )
    assert eval_groups.group_of(f"{reference}[a_fixture_added_later/task]") == (
        "references-a"
    )


def test_a_test_no_prefix_names_lands_in_the_catch_all():
    assert eval_groups.group_of("evals/tests/test_brand_new.py::test_x") == (
        eval_groups.CATCH_ALL
    )
    assert eval_groups.group_of("evals/tests/test_metadata.py::test_brand_new") == (
        eval_groups.CATCH_ALL
    )


# --- the wiring ------------------------------------------------------------------


@pytest.fixture(scope="module")
def jobs() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else needs


def _grouped_job(jobs: dict) -> tuple[str, dict]:
    grouped = [
        (name, job)
        for name, job in jobs.items()
        if "--eval-group " in json.dumps(job.get("steps", []))
    ]
    assert len(grouped) == 1, "exactly one ci.yml job runs the evals by group"
    return grouped[0]


def test_the_matrix_comes_from_eval_groups_rather_than_a_list_of_its_own(jobs):
    _, job = _grouped_job(jobs)
    groups = job["strategy"]["matrix"]["group"]
    assert isinstance(groups, str) and "fromJSON(needs." in groups
    producer = groups.split("needs.", 1)[1].split(".", 1)[0]
    assert producer in _needs(job)
    assert "check_test_groups.py matrix --suite evals" in json.dumps(
        jobs[producer]["steps"]
    )


def test_every_group_runs_every_eval_file_and_a_gate_verifies_them(jobs):
    name, job = _grouped_job(jobs)
    steps = json.dumps(job["steps"])
    # The whole eval tree, so a test file outside test_metadata.py is grouped too.
    assert "pytest evals/tests " in steps
    assert "--eval-group-report" in steps
    assert "--junitxml" in steps
    assert "upload-artifact" in steps
    gates = [
        gate
        for gate in jobs.values()
        if name in _needs(gate)
        and "check_test_groups.py verify eval-group-reports --suite evals"
        in json.dumps(gate.get("steps", []))
    ]
    assert gates, "a job that needs the eval groups runs check_test_groups.py verify"
    assert all(gate.get("if") == "always()" for gate in gates)
    assert "evals" in jobs and jobs["evals"] in gates, (
        "the gate keeps the `evals` check name"
    )


# --- the proof -------------------------------------------------------------------


SUITE = [
    "evals/tests/test_a.py::test_one",
    "evals/tests/test_a.py::test_two",
    "evals/tests/test_b.py::test_one",
]
GROUPS = {"one": ("evals/tests/test_a.py",), "two": ()}


@pytest.fixture
def checker(monkeypatch):
    module = runpy.run_path(str(CHECKER))
    fake = {"GROUPS": GROUPS, "matches": eval_groups.matches}
    monkeypatch.setitem(module["verify"].__globals__, "_module", lambda suite: fake)
    return module


def _report(tmp_path: Path, group: str, selected: list[str], **overrides) -> None:
    report = {
        "group": group,
        "python": "3.13",
        "collected": SUITE,
        "selected": selected,
        "ran": selected,
        "skipped": [],
        **overrides,
    }
    (tmp_path / f"{group}.json").write_text(json.dumps(report), encoding="utf-8")


def test_a_clean_split_verifies(tmp_path, checker):
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", SUITE[2:])
    assert checker["verify"](tmp_path, "evals") == []


def test_a_skipped_eval_fails(tmp_path, checker):
    _report(tmp_path, "one", SUITE[:2], skipped=[SUITE[1]])
    _report(tmp_path, "two", SUITE[2:])
    problems = checker["verify"](tmp_path, "evals")
    assert any("skipped" in problem and SUITE[1] in problem for problem in problems)


def test_a_module_skipped_at_import_fails(tmp_path, checker):
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", SUITE[2:], skipped=["tests/test_metadata.py"])
    assert any("skipped" in problem for problem in checker["verify"](tmp_path, "evals"))


def test_a_prefix_naming_no_collected_test_fails(tmp_path, checker, monkeypatch):
    stale = {**GROUPS, "one": ("evals/tests/test_a.py", "evals/tests/test_gone.py")}
    fake = {"GROUPS": stale, "matches": eval_groups.matches}
    monkeypatch.setitem(checker["verify"].__globals__, "_module", lambda suite: fake)
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", SUITE[2:])
    problems = checker["verify"](tmp_path, "evals")
    assert any("test_gone.py" in problem for problem in problems)


def test_an_eval_in_no_group_fails(tmp_path, checker):
    _report(tmp_path, "one", SUITE[:2])
    _report(tmp_path, "two", [])
    assert any("in no group" in p for p in checker["verify"](tmp_path, "evals"))


def test_a_group_that_never_reported_fails(tmp_path, checker):
    _report(tmp_path, "one", SUITE)
    problems = checker["verify"](tmp_path, "evals")
    assert any("'two' reported 0 times" in problem for problem in problems)


def test_the_engine_suite_still_allows_a_skip(tmp_path, checker):
    _report(tmp_path, "one", SUITE[:2], skipped=[SUITE[1]])
    _report(tmp_path, "two", SUITE[2:])
    assert checker["verify"](tmp_path, "engine") == []


def test_the_real_checker_reads_this_split():
    module = runpy.run_path(str(CHECKER))
    assert module["_groups"]("evals") == list(eval_groups.GROUPS)
