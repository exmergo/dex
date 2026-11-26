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
- scripts/check_elementary_release.py, the weekly watch for a new release
  (#1446), reads the engine's TESTED_RELEASES as the engine holds them, passes
  when the hub's newest release is the newest slot, fails naming a newer one,
  skips a prerelease, and fails a response it cannot read; its job needs no
  secret and no write token.
"""

from __future__ import annotations

import importlib.util
import json
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


# --- the watch for a new Elementary release (#1446) ------------------------------

WATCH = ROOT / "scripts" / "check_elementary_release.py"
CANARIES = ROOT / ".github" / "workflows" / "upstream-deps-canaries.yml"


def _hub(*versions: str) -> dict:
    """A hub response in the shape dbt's registry client checks."""

    return {
        "name": "elementary",
        "namespace": "elementary-data",
        "latest": versions[-1],
        "versions": {
            v: {"name": "elementary", "version": v, "packages": [], "downloads": {}}
            for v in versions
        },
    }


def _watch(tmp_path: Path, payload: object) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "hub.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, str(WATCH), "--payload", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_watch_reads_the_tested_releases_the_engine_holds():
    """It reads TESTED_RELEASES from source rather than importing the engine, so
    the job needs no install; the reading has to be the engine's own value."""

    from exmergo_dex_core.transform.elementary import TESTED_RELEASES

    spec = importlib.util.spec_from_file_location("check_elementary_release", WATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.tested_releases(module.ENGINE.read_text("utf-8")) == TESTED_RELEASES


def test_the_watch_passes_when_the_hub_s_newest_release_is_the_newest_slot(
    tmp_path: Path,
):
    from exmergo_dex_core.transform.elementary import NEWEST_TESTED

    result = _watch(tmp_path, _hub("0.15.2", "0.23.0", NEWEST_TESTED))
    assert result.returncode == 0, result.stdout + result.stderr
    assert NEWEST_TESTED in result.stdout


def test_the_watch_fails_naming_a_newer_release_and_the_rule(tmp_path: Path):
    from exmergo_dex_core.transform.elementary import NEWEST_TESTED

    major, minor, patch = (int(p) for p in NEWEST_TESTED.split("."))
    patch_above = f"{major}.{minor}.{patch + 1}"
    minor_above = f"{major}.{minor + 1}.0"
    result = _watch(tmp_path, _hub(NEWEST_TESTED, patch_above, minor_above))
    assert result.returncode == 1
    assert result.stdout.startswith("::error::")
    assert f"Elementary {minor_above} is on the dbt package hub" in result.stdout
    assert f"also newer: {patch_above}" in result.stdout
    assert f"the fixture runs is {NEWEST_TESTED}" in result.stdout
    assert "Adding a new Elementary release" in result.stdout


def test_the_watch_skips_a_prerelease(tmp_path: Path):
    from exmergo_dex_core.transform.elementary import NEWEST_TESTED

    major, minor, _ = NEWEST_TESTED.split(".")
    candidate = f"{major}.{int(minor) + 1}.0-rc.1"
    assert _watch(tmp_path, _hub(NEWEST_TESTED, candidate)).returncode == 0


@pytest.mark.parametrize("payload", [{"name": "elementary"}, [], {"versions": {}}])
def test_the_watch_fails_a_hub_response_it_cannot_read(tmp_path: Path, payload):
    result = _watch(tmp_path, payload)
    assert result.returncode == 2
    assert "cannot compare the matrix with the hub" in result.stdout


def test_the_watch_runs_weekly_with_no_secret_and_a_read_only_token():
    canaries = yaml.safe_load(CANARIES.read_text(encoding="utf-8"))
    triggers = canaries.get("on", canaries.get(True))
    assert "schedule" in triggers
    assert canaries["permissions"] == {"contents": "read"}
    job = canaries["jobs"]["elementary-release"]
    assert "permissions" not in job and "env" not in job
    text = yaml.safe_dump(job)
    assert "secrets." not in text and "vars." not in text
    [run] = [s["run"] for s in job["steps"] if "run" in s]
    assert run == "python3 scripts/check_elementary_release.py"


# --- the Postgres file runs every Postgres slot (#1446) ---------------------------


#: The rendered-test case, which runs at the sample-flag boundary alone.
RENDERED_CASE = (
    "test_a_failing_rendered_test_keeps_no_sample_from_the_sample_flag_boundary"
)


def _versions_of(function) -> set[str]:
    """The releases a test function is parametrized over, by a ``version`` arg."""

    found: set[str] = set()
    for mark in getattr(function, "pytestmark", []):
        if mark.name != "parametrize":
            continue
        names, values = mark.args[0], mark.args[1]
        names = (
            [n.strip() for n in names.split(",")] if isinstance(names, str) else names
        )
        if "version" not in names:
            continue
        at = names.index("version")
        for value in values:
            found.add(value if len(names) == 1 else value[at])
    return found


@pytest.mark.parametrize(
    "case",
    [
        "test_each_variant_builds_at_each_postgres_slot",
        "test_a_mutation_run_leaves_the_dev_schemas_as_it_found_them",
        "test_without_the_fix_a_mutation_run_leaves_rows_in_the_dev_elementary_schema",
    ],
)
def test_each_release_parametrized_postgres_case_runs_every_postgres_slot(case: str):
    """The matrix test holds MATRIX to POSTGRES_VERSIONS; this holds the Postgres
    file to POSTGRES_VERSIONS, so a case moved back to a narrower list drops no
    slot while CI stays green."""

    import importlib

    from transform.elementary_fixture import POSTGRES_VERSIONS, SAMPLE_FLAG_VERSION

    module = importlib.import_module("integration.test_postgres_elementary")
    assert _versions_of(getattr(module, case)) == set(POSTGRES_VERSIONS)
    switch = module.test_the_per_test_sampling_switch_is_ignored_before_0_20
    assert set(POSTGRES_VERSIONS) <= _versions_of(switch)
    assert _versions_of(getattr(module, RENDERED_CASE)) == {SAMPLE_FLAG_VERSION}


def test_a_truncated_hub_read_is_unreadable_not_a_new_release(monkeypatch, capsys):
    """``http.client.IncompleteRead`` is no ``OSError``; it must exit 2, never 1,
    which would say a newer release is on the hub."""

    import http.client

    spec = importlib.util.spec_from_file_location("check_elementary_release", WATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def truncated(*_: object) -> dict:
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(module, "fetch", truncated)
    assert module.main([]) == 2
    assert "cannot compare the matrix with the hub" in capsys.readouterr().out
