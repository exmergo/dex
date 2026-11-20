"""The release versions check: what `scripts/check_release_versions.py` holds.

`prepare_release.sh 1.18.1 v1.18.1` wrote `"version": "v1.18.1"` into every
plugin manifest, and the release workflow's re-run of the script reproduced it,
so nothing in CI noticed. The check reads the files the script writes and holds
each version to the shape its reader expects. It runs against fixture trees
here, and against this checkout, which must always pass: only a release moves
these files, so the invariants hold between releases too.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "scripts" / "check_release_versions.py"

pytestmark = pytest.mark.skipif(
    not CHECKER.is_file(), reason="needs a repository checkout"
)

WRAPPERS = [
    f"skills/{skill}/scripts/run.py" for skill in ("explore", "transform", "maintain")
]
MANIFESTS = [
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "plugin.json",
    "gemini-extension.json",
    "claude/.claude-plugin/plugin.json",
]
MARKER = "<!-- towncrier release notes start -->"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_release_versions", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(
    root: Path,
    engine: str = "1.18.1",
    plugin: str = "1.18.1",
    section: str = "## [1.18.1] - 2026-11-20",
) -> Path:
    for wrapper in WRAPPERS:
        path = root / wrapper
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'DEX_CORE_VERSION = "{engine}"\n', encoding="utf-8")
    pyproject = root / "packages/dex-core/pyproject.toml"
    pyproject.parent.mkdir(parents=True, exist_ok=True)
    pyproject.write_text(
        f'# fallback_version = "not me"\nfallback_version = "{engine}"\n',
        encoding="utf-8",
    )
    for manifest in MANIFESTS:
        path = root / manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": "dex", "version": plugin}), encoding="utf-8"
        )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n{MARKER}\n\n{section}\n\n"
        "### Fixed\n\n- A fix.\n\n## [1.0.0] - 2026-01-01\n",
        encoding="utf-8",
    )
    return root


def test_this_checkout_passes(checker):
    assert checker.check(ROOT) == []


@pytest.mark.parametrize(
    ("engine", "plugin"),
    [
        ("1.18.1", "1.18.1"),
        ("0.1.0a1", "0.1.0-alpha.1"),
        ("2.0.0rc2", "2.0.0-rc.2"),
        ("9.9.9", "9.9.9-rc.1"),
        ("1.0", "1.0.0"),
        ("1.2.3.post1", "1.2.3"),
    ],
)
def test_a_well_prepared_release_passes(checker, tmp_path, engine, plugin):
    root = _write(tmp_path, engine, plugin, f"## [{engine}] - 2026-11-20")
    assert checker.check(root) == []


def test_a_v_prefixed_plugin_version_is_refused(checker, tmp_path):
    """The 1.18.1 mistake, exactly as the release commit held it."""

    problems = checker.check(_write(tmp_path, plugin="v1.18.1"))
    assert len(problems) == 1
    assert "'v1.18.1' is not a semver 2.0 version" in problems[0]


@pytest.mark.parametrize(
    "plugin", ["1.18", "1.18.1.0", "0.1.0a1", "01.18.1", "", "1.18.1\n"]
)
def test_a_plugin_version_that_is_not_semver_is_refused(checker, tmp_path, plugin):
    problems = checker.check(_write(tmp_path, plugin=plugin))
    assert any("is not a semver 2.0 version" in p for p in problems), problems


@pytest.mark.parametrize("engine", ["v1.18.1", "1.18.1-alpha1", "1.18.01", "1.18.1a"])
def test_an_engine_version_that_is_not_canonical_pep440_is_refused(
    checker, tmp_path, engine
):
    root = _write(tmp_path, engine=engine, section=f"## [{engine}] - 2026-11-20")
    problems = checker.check(root)
    assert any("is not a canonical PEP 440 version" in p for p in problems), problems


def test_a_plugin_version_of_another_release_is_refused(checker, tmp_path):
    problems = checker.check(_write(tmp_path, plugin="1.18.0"))
    assert any("is not a release of engine version" in p for p in problems), problems


def test_one_manifest_left_behind_is_named(checker, tmp_path):
    root = _write(tmp_path)
    stale = root / "claude/.claude-plugin/plugin.json"
    stale.write_text(json.dumps({"version": "1.18.0"}), encoding="utf-8")
    problems = checker.check(root)
    assert len(problems) == 1
    assert "plugin manifest versions disagree" in problems[0]
    assert "claude/.claude-plugin/plugin.json = '1.18.0'" in problems[0]


def test_one_engine_pin_left_behind_is_named(checker, tmp_path):
    root = _write(tmp_path)
    (root / WRAPPERS[1]).write_text('DEX_CORE_VERSION = "1.18.0"\n', encoding="utf-8")
    problems = checker.check(root)
    assert any(
        "engine version pins disagree" in p and WRAPPERS[1] in p for p in problems
    ), problems


def test_a_changelog_section_for_another_version_is_refused(checker, tmp_path):
    problems = checker.check(_write(tmp_path, section="## [1.18.0] - 2026-11-16"))
    assert any("newest CHANGELOG.md section is [1.18.0]" in p for p in problems)


def test_a_changelog_section_with_a_v_is_refused(checker, tmp_path):
    problems = checker.check(_write(tmp_path, section="## [v1.18.1] - 2026-11-20"))
    assert any("section is [v1.18.1]" in p for p in problems), problems


def test_an_undated_changelog_section_is_refused(checker, tmp_path):
    problems = checker.check(_write(tmp_path, section="## [1.18.1]"))
    assert any("has no ' - YYYY-MM-DD' date" in p for p in problems), problems


def test_a_missing_manifest_is_named(checker, tmp_path):
    root = _write(tmp_path)
    (root / "gemini-extension.json").unlink()
    problems = checker.check(root)
    assert problems == [
        "gemini-extension.json: cannot be read (No such file or directory)"
    ]


def test_the_command_line_fails_with_the_fix(tmp_path):
    root = _write(tmp_path, plugin="v1.18.1")
    result = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "'v1.18.1' is not a semver 2.0 version" in result.stderr
    assert "scripts/prepare_release.sh" in result.stderr


def test_the_command_line_passes_quietly_on_a_sound_tree(tmp_path):
    result = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, str(CHECKER), "--root", str(_write(tmp_path))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
