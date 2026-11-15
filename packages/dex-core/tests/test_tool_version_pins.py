"""Developer tools are pinned once, and everything else reads the lockfile.

Ruff and towncrier are pinned by an exact `==` in the engine's `[dev]` extra and
locked in packages/dex-core/uv.lock. Dependabot's `dev-tools` group moves those
two together and sees nothing else, so every other caller (the Lint and Changelog
workflows, the pre-commit hooks, the release script, the commands in the guides)
runs the locked version through scripts/uvx_locked.py. Before that, each caller
restated the version and every tool bump failed CI until someone edited them by
hand (#1137). Held here:

- the `[dev]` pin and the lock agree, for each tool;
- no tracked workflow, hook, script or guide restates a version by hand;
- the helper reads the one locked version, refuses a lock that does not settle
  one, and runs `uvx <tool>@<version>` with the caller's arguments.

Nothing here resolves or fetches a package.
"""

from __future__ import annotations

import re
import runpy
import subprocess
import tomllib
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
PYPROJECT = PACKAGE / "pyproject.toml"
LOCK = PACKAGE / "uv.lock"
HELPER = ROOT / "scripts" / "uvx_locked.py"
TOOLS = ("ruff", "towncrier")

pytestmark = pytest.mark.skipif(
    not HELPER.is_file(), reason="needs a repository checkout"
)


@pytest.fixture(scope="module")
def helper() -> dict:
    return runpy.run_path(str(HELPER))


def _dev_pin(tool: str) -> str:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = project["project"]["optional-dependencies"]["dev"]
    pins = [
        match.group(1)
        for requirement in dev
        if (match := re.fullmatch(rf"{tool}==(\d+(?:\.\d+)+)", requirement))
    ]
    named = [r for r in dev if re.match(rf"{tool}\b", r)]
    assert len(pins) == 1 and len(named) == 1, (
        f"expected one exact `{tool}==` pin in the [dev] extra, found {named!r}"
    )
    return pins[0]


def _locked(tool: str) -> list[str]:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return [p["version"] for p in lock["package"] if p["name"] == tool]


# --- one pin per tool -----------------------------------------------------------


@pytest.mark.parametrize("tool", TOOLS)
def test_the_dev_pin_and_the_lock_agree(tool):
    assert _locked(tool) == [_dev_pin(tool)]


@pytest.mark.parametrize("tool", TOOLS)
def test_the_helper_reads_what_the_lock_pins(helper, tool):
    text = LOCK.read_text(encoding="utf-8")
    assert helper["locked_version"](tool, text) == _locked(tool)[0]


# A version restated next to a tool's name: `ruff@<version>`, `towncrier@<version>`,
# or the astral-sh/ruff-pre-commit hook's `rev:`, which carried Ruff's version.
RESTATED = re.compile(r"\b(?:ruff|towncrier)@v?\d|astral-sh/ruff-pre-commit")
# Where a caller would restate one. CHANGELOG.md is history, and claude/ is
# regenerated from the tree at each release.
CALLERS = re.compile(
    r"^(?:\.github/|scripts/|\.pre-commit-config\.yaml$|[^/]+\.md$|changelog\.d/"
    r"|docs/|skills/|references/)"
)


def test_nothing_restates_a_tool_version():
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    offenders = {}
    for name in filter(None, listed.split("\0")):
        if name == "CHANGELOG.md" or not CALLERS.match(name):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), 1)
            if RESTATED.search(line)
        ]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "run the tool through scripts/uvx_locked.py instead of restating its "
        f"version: {offenders}"
    )


def test_lint_changelog_and_hooks_run_through_the_helper():
    for path, tool in (
        (".github/workflows/lint.yml", "ruff"),
        (".github/workflows/changelog.yml", "towncrier"),
        (".pre-commit-config.yaml", "ruff"),
    ):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert f"scripts/uvx_locked.py {tool}" in text, path
    release = (ROOT / "scripts" / "prepare_release.sh").read_text(encoding="utf-8")
    assert 'scripts/uvx_locked.py" towncrier' in release


# --- the helper -----------------------------------------------------------------

LOCK_FIXTURE = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "ruff"
version = "0.17.2"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://example.invalid/ruff-0.17.2.whl", hash = "sha256:00" },
]

[[package]]
name = "scipy"
version = "1.17.1"
source = { registry = "https://pypi.org/simple" }

[package.optional-dependencies]
dev = [
    { name = "pytest", version = "9.0.0" },
]

[[package]]
name = "scipy"
version = "1.18.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "Typing_Extensions"
version = "4.15.0"
"""


def test_the_helper_finds_one_version_and_normalizes_names(helper):
    assert helper["locked_version"]("ruff", LOCK_FIXTURE) == "0.17.2"
    assert helper["locked_version"]("typing.extensions", LOCK_FIXTURE) == "4.15.0"


def test_the_helper_ignores_keys_inside_sub_tables(helper):
    # `version = "9.0.0"` under [package.optional-dependencies] is not a package.
    with pytest.raises(helper["LockError"], match="pytest is not in"):
        helper["locked_version"]("pytest", LOCK_FIXTURE)


def test_the_helper_refuses_a_forked_version(helper):
    with pytest.raises(helper["LockError"], match=r"1\.17\.1, 1\.18\.0"):
        helper["locked_version"]("scipy", LOCK_FIXTURE)


def test_the_helper_agrees_with_tomllib_on_the_real_lock(helper):
    text = LOCK.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)["package"]
    expected = sorted((p["name"], p["version"]) for p in parsed if "version" in p)
    found = sorted(
        (p["name"], p["version"]) for p in helper["packages"](text) if "version" in p
    )
    assert found == expected


def test_the_helper_runs_uvx_with_the_callers_arguments(helper, monkeypatch):
    ran = []

    def fake_run(argv, check):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 3)

    globals_ = helper["main"].__globals__
    monkeypatch.setitem(globals_, "subprocess", type("S", (), {"run": fake_run}))
    monkeypatch.setitem(globals_, "shutil", type("W", (), {"which": lambda _: "uvx"}))

    assert helper["main"](["ruff", "check", "--fix", "a.py"]) == 3
    assert ran == [["uvx", f"ruff@{_locked('ruff')[0]}", "check", "--fix", "a.py"]]


def test_the_helper_falls_back_to_uv_tool_run(helper, monkeypatch):
    globals_ = helper["command"].__globals__
    monkeypatch.setitem(globals_, "shutil", type("W", (), {"which": lambda _: None}))
    assert helper["command"]("ruff", "1.0.0", ["--version"]) == [
        "uv",
        "tool",
        "run",
        "ruff@1.0.0",
        "--version",
    ]


def test_the_helper_prints_the_version_and_reports_usage(helper, capsys):
    assert helper["main"](["--version", "towncrier"]) == 0
    assert capsys.readouterr().out.strip() == _locked("towncrier")[0]
    assert helper["main"]([]) == 2
    assert helper["main"](["--version"]) == 2
    assert helper["main"](["--version", "not-a-locked-tool"]) == 1
    assert "not-a-locked-tool is not in" in capsys.readouterr().err
