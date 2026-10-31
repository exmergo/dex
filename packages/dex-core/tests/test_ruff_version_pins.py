"""Keep every repository Ruff pin in sync without resolving dependencies."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = ".github/workflows/lint.yml"
PRE_COMMIT_PATH = ".pre-commit-config.yaml"
PYPROJECT_PATH = "packages/dex-core/pyproject.toml"
PYPROJECT_DEV = f"{PYPROJECT_PATH} [dev]"
LOCK_PATH = "packages/dex-core/uv.lock"
CONFIG_PATHS = (WORKFLOW_PATH, PRE_COMMIT_PATH, PYPROJECT_PATH, LOCK_PATH)
RUFF_HOOK_REPO = "https://github.com/astral-sh/ruff-pre-commit"


def _workflow_ruff_versions(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    run_scripts = []
    index = 0
    while index < len(lines):
        match = re.match(
            r"^(?P<indent>[ \t]*(?:-[ \t]+)?)run:[ \t]*(?P<value>.*)$",
            lines[index],
        )
        if match is None:
            index += 1
            continue

        run_indent = len(match.group("indent"))
        value = match.group("value").strip()
        index += 1
        if re.match(r"^[|>][+-]?(?:[ \t]|$)", value):
            script_lines = []
            while index < len(lines):
                line = lines[index]
                line_indent = len(line) - len(line.lstrip(" \t"))
                if line.strip() and line_indent <= run_indent:
                    break
                script_lines.append(line)
                index += 1
            run_scripts.append("\n".join(script_lines))
        else:
            run_scripts.append(value)

    versions = []
    for script in run_scripts:
        for command in script.splitlines():
            if command.lstrip().startswith("#"):
                continue
            for match in re.finditer(
                r"(?<![\w.-])ruff(?:@(?P<version>\d+\.\d+\.\d+))?(?![\w.-])",
                command,
            ):
                version = match.group("version")
                if version is None:
                    raise AssertionError(
                        f"Expected an exact Ruff version pin in {WORKFLOW_PATH}, "
                        f"found run command {command.strip()!r}"
                    )
                versions.append(version)

    if not versions:
        raise AssertionError(f"No pinned Ruff invocation found in {WORKFLOW_PATH}")
    return versions


def _read_ruff_pins(configs: Mapping[str, str]) -> dict[str, list[str]]:
    workflow_versions = _workflow_ruff_versions(configs[WORKFLOW_PATH])

    pre_commit_versions = []
    lines = configs[PRE_COMMIT_PATH].splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- repo: {RUFF_HOOK_REPO}":
            continue
        for hook_line in lines[index + 1 :]:
            if hook_line.lstrip().startswith("- repo:"):
                break
            match = re.fullmatch(
                r"[ \t]+rev:[ \t]*v?(\d+\.\d+\.\d+)(?:[ \t]+#.*)?",
                hook_line,
            )
            if match:
                pre_commit_versions.append(match.group(1))
    if not pre_commit_versions:
        raise AssertionError(f"No Ruff hook revision found in {PRE_COMMIT_PATH}")

    project = tomllib.loads(configs[PYPROJECT_PATH])
    dev_dependencies = (
        project.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    )
    dev_versions = []
    for requirement in dev_dependencies:
        if not isinstance(requirement, str) or not requirement.lower().startswith(
            "ruff"
        ):
            continue
        match = re.fullmatch(r"ruff==([0-9]+\.[0-9]+\.[0-9]+)", requirement)
        if match is None:
            raise AssertionError(
                f"Expected an exact Ruff pin in {PYPROJECT_DEV}, found {requirement!r}"
            )
        dev_versions.append(match.group(1))
    if len(dev_versions) != 1:
        raise AssertionError(
            f"Expected one exact Ruff pin in {PYPROJECT_DEV}, found {dev_versions!r}"
        )

    lock = tomllib.loads(configs[LOCK_PATH])
    lock_versions = [
        package["version"]
        for package in lock.get("package", [])
        if package.get("name") == "ruff"
    ]
    if len(lock_versions) != 1:
        raise AssertionError(
            f"Expected one Ruff package in {LOCK_PATH}, found {lock_versions!r}"
        )

    return {
        WORKFLOW_PATH: workflow_versions,
        PRE_COMMIT_PATH: pre_commit_versions,
        PYPROJECT_DEV: dev_versions,
        LOCK_PATH: lock_versions,
    }


def _assert_ruff_pins_match(configs: Mapping[str, str]) -> None:
    pins = _read_ruff_pins(configs)
    expected = pins[PYPROJECT_DEV][0]
    mismatches = [
        f"{surface}: {', '.join(versions)}"
        for surface, versions in pins.items()
        if any(version != expected for version in versions)
    ]
    if mismatches:
        raise AssertionError(
            f"Ruff version pins differ from {PYPROJECT_DEV} ({expected}): "
            + "; ".join(mismatches)
        )


def _repo_configs() -> dict[str, str]:
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in CONFIG_PATHS}


def test_repository_ruff_pins_match() -> None:
    _assert_ruff_pins_match(_repo_configs())


@pytest.fixture
def matching_ruff_configs() -> dict[str, str]:
    return {
        WORKFLOW_PATH: (
            "steps:\n"
            "  - run: uvx ruff@0.17.2 check .\n"
            "  - run: uvx ruff@0.17.2 format --check .\n"
        ),
        PRE_COMMIT_PATH: (
            "repos:\n"
            f"  - repo: {RUFF_HOOK_REPO}\n"
            "    rev: v0.17.2\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
        ),
        PYPROJECT_PATH: ('[project.optional-dependencies]\ndev = ["ruff==0.17.2"]\n'),
        LOCK_PATH: '[[package]]\nname = "ruff"\nversion = "0.17.2"\n',
    }


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (WORKFLOW_PATH, "ruff@0.17.2", "ruff@0.15.19"),
        (PRE_COMMIT_PATH, "v0.17.2", "v0.15.19"),
        (PYPROJECT_PATH, "ruff==0.17.2", "ruff==0.15.19"),
        (LOCK_PATH, 'version = "0.17.2"', 'version = "0.15.19"'),
    ],
)
def test_reports_a_mismatched_ruff_pin(
    matching_ruff_configs: dict[str, str], path: str, old: str, new: str
) -> None:
    assert old in matching_ruff_configs[path]
    matching_ruff_configs[path] = matching_ruff_configs[path].replace(old, new, 1)

    with pytest.raises(AssertionError) as error:
        _assert_ruff_pins_match(matching_ruff_configs)

    assert path in str(error.value)
    assert "0.15.19" in str(error.value)


@pytest.mark.parametrize(
    "workflow",
    [
        "steps:\n  - run: uvx ruff@0.17.2 check .\n  - run: uvx ruff format .\n",
        (
            "steps:\n"
            "  - run: |\n"
            "      uvx ruff@0.17.2 check .\n"
            "      uvx ruff format .\n"
        ),
    ],
)
def test_rejects_unpinned_ruff_workflow_invocation(
    matching_ruff_configs: dict[str, str], workflow: str
) -> None:
    matching_ruff_configs[WORKFLOW_PATH] = workflow

    with pytest.raises(AssertionError) as error:
        _assert_ruff_pins_match(matching_ruff_configs)

    assert WORKFLOW_PATH in str(error.value)
    assert "run command" in str(error.value)
    assert "ruff format" in str(error.value)
