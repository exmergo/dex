"""Where CI runs the Elementary fixture, and why a skip there fails (#1540).

Both fixture files skip themselves when ``DEX_TEST_ELEMENTARY`` is unset, and
pytest counts a skip as a pass, so the one way these runs could stop proving
anything is silent. The properties are held here, hermetically, against the
checker and the workflow:

- scripts/check_no_skips.py passes a report in which every case ran, and fails
  one holding a skip (naming the case and its reason), no case at all, or no
  readable file.
- integration.yml runs each fixture file in a step that sets the variable on
  the step and then checks that step's own JUnit report with the checker; the
  general Postgres suite leaves the file to that step; and the workflow has no
  pull request trigger.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "scripts" / "check_no_skips.py"
WORKFLOW = ROOT / ".github" / "workflows" / "integration.yml"
ENV = "DEX_TEST_ELEMENTARY"
FILES = (
    "tests/integration/test_postgres_elementary.py",
    "tests/transform/test_elementary_fixture.py",
)

PASSED = '<testcase classname="tests.t" name="test_ran" time="0.1"/>'
SKIPPED = (
    '<testcase classname="tests.t" name="test_gated[a]" time="0">'
    '<skipped type="pytest.skip" message="set DEX_TEST_ELEMENTARY=1 to run"/>'
    "</testcase>"
)


def _report(tmp_path: Path, *cases: str, name: str = "report.xml") -> Path:
    path = tmp_path / name
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        f'<testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _check(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, str(CHECKER), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the checker ------------------------------------------------------------------


def test_the_checker_passes_a_run_in_which_every_case_ran(tmp_path: Path):
    result = _check(_report(tmp_path, PASSED, PASSED))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_the_checker_fails_a_skip_and_names_the_case_and_its_reason(tmp_path: Path):
    result = _check(_report(tmp_path, PASSED, SKIPPED))
    assert result.returncode == 1
    assert "tests.t::test_gated[a] was skipped" in result.stdout
    assert "set DEX_TEST_ELEMENTARY=1 to run" in result.stdout
    assert result.stdout.startswith("::error::")


def test_the_checker_fails_an_expected_failure_and_says_so(tmp_path: Path):
    xfail = (
        '<testcase classname="tests.t" name="test_known" time="0">'
        '<skipped type="pytest.xfail" message="known bug"/></testcase>'
    )
    result = _check(_report(tmp_path, PASSED, xfail))
    assert result.returncode == 1
    assert "tests.t::test_known is an expected failure: known bug" in result.stdout


def test_the_checker_reads_every_report_it_is_given(tmp_path: Path):
    clean = _report(tmp_path, PASSED, name="clean.xml")
    gated = _report(tmp_path, SKIPPED, name="gated.xml")
    result = _check(clean, gated)
    assert result.returncode == 1
    assert "gated.xml" in result.stdout


def test_the_checker_fails_a_run_that_collected_nothing(tmp_path: Path):
    result = _check(_report(tmp_path))
    assert result.returncode == 1
    assert "no test case" in result.stdout


@pytest.mark.parametrize("body", [None, "not xml"], ids=["missing", "unparseable"])
def test_the_checker_fails_a_report_it_cannot_read(tmp_path: Path, body: str | None):
    path = tmp_path / "report.xml"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    result = _check(path)
    assert result.returncode == 1
    assert "cannot read the JUnit report" in result.stdout


def test_the_checker_refuses_to_run_with_no_report():
    assert _check().returncode == 2


# --- the workflow -----------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow: dict):
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, job, step


@pytest.mark.parametrize("file", FILES)
def test_each_fixture_file_runs_once_with_its_variable_and_a_skip_check(
    workflow: dict, file: str
):
    runners = [
        (job, step)
        for _, job, step in _steps(workflow)
        if file in str(step.get("run", ""))
        and f"--ignore={file}" not in str(step["run"])
    ]
    assert len(runners) == 1, f"{file} should run in exactly one step"
    job, step = runners[0]
    assert (step.get("env") or {}).get(ENV) == "1"
    assert ENV not in (job.get("env") or {})
    run = step["run"]
    report = run.split("--junitxml", 1)[1].split()[0]
    assert f"check_no_skips.py {report}" in run
    assert run.index("pytest") < run.index("check_no_skips.py")


def test_the_variable_is_set_only_on_the_two_fixture_steps(workflow: dict):
    """A step that collects a fixture file without naming it (`pytest tests`)
    would run it a second time, and outside the skip check, if it had the
    variable; so nothing but the two named steps may set it."""

    assert ENV not in (workflow.get("env") or {})
    holders = []
    for _, job, step in _steps(workflow):
        assert ENV not in (job.get("env") or {})
        if ENV in (step.get("env") or {}):
            holders.append(step["run"])
    assert len(holders) == len(FILES)
    for file in FILES:
        assert sum(file in run for run in holders) == 1


def test_the_general_postgres_suite_leaves_the_elementary_file_to_its_step(
    workflow: dict,
):
    suite = next(
        step
        for step in workflow["jobs"]["postgres"]["steps"]
        if step.get("name") == "Live Postgres integration suite"
    )
    assert f"--ignore={FILES[0]}" in suite["run"]


def test_the_workflow_never_runs_on_a_pull_request(workflow: dict):
    # PyYAML reads the bare key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "schedule", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
