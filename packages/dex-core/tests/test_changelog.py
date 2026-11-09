"""The changelog pipeline: fragments in, one release section out.

A pull request describes its change in a fragment under `changelog.d/`.
`scripts/check_changelog_fragments.py` holds the fragment names and the pull
request policy, and `scripts/prepare_release.sh` collects the fragments into
`CHANGELOG.md` with towncrier. None of it is engine code, and the release script
only runs when someone cuts a release, which is the worst moment to learn it
broke. So it is held here:

- The release script runs in a scratch repository against fixture fragments: no
  tag, no publish, and no network, because `TOWNCRIER` points it at the dev
  extra's towncrier instead of the copy uvx would fetch. It must keep released
  history byte-for-byte, render each pending fragment exactly once, delete only
  what it consumed, leave the git index alone, and change nothing when a fragment
  is malformed or the version was already prepared.
- The fragment check runs against fixture trees, the way CI hands it a pull
  request's changes and labels.
- Every pinned towncrier version in the repository is the same one, so the
  version these tests exercise is the version a release runs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PREPARE = ROOT / "scripts" / "prepare_release.sh"
CHECKER = ROOT / "scripts" / "check_changelog_fragments.py"
BASH = shutil.which("bash")
GIT = shutil.which("git")

# An installed wheel has no scripts or changelog.d/ beside it, same as
# test_plugin_manifests.py.
pytestmark = pytest.mark.skipif(
    not (ROOT / "changelog.d").is_dir(), reason="needs a repository checkout"
)
needs_release_tools = pytest.mark.skipif(
    BASH is None
    or GIT is None
    or os.name != "posix"
    or importlib.util.find_spec("towncrier") is None,
    reason="the release script is bash and needs git and the dev extra's towncrier",
)

MARKER = "<!-- towncrier release notes start -->"
HEADER = f"""# Changelog

Pending changes are fragments in changelog.d/.

{MARKER}

"""
# Released history, deliberately in the shapes the real file holds: a link in the
# headline, a continuation line, and an older section below.
HISTORY = """## [1.0.1] - 2026-01-02

### Fixed

- **An old fix** ([#2](https://github.com/exmergo/dex/issues/2)). The body
  continues on a second line.

## [1.0.0] - 2026-01-01

### Added

- The first release.
"""
FRAGMENTS = {
    "554.fixed.md": "**A fixed thing.** It used to fail.\n",
    "554.added.md": (
        "**A new flag.** The first paragraph.\n\n"
        "A second paragraph with the migration guidance.\n"
    ),
    "560.added.md": "**Another addition.** It comes with a list:\n\n- one\n- two\n",
    "560.added.1.md": "**A second addition from the same pull request.**\n",
    "+orphan-note.changed.md": "**A change with no pull request yet.**\n",
}
WRAPPERS = [
    f"skills/{skill}/scripts/run.py" for skill in ("explore", "transform", "maintain")
]
#: The engine's own pyproject, whose hatch-vcs fallback is the version the
#: installed plugin's engine reports (#731), so the release couples it too. Only
#: the lines the script rewrites are written, the comment and the key around it.
ENGINE_PYPROJECT = "packages/dex-core/pyproject.toml"
ENGINE_PYPROJECT_TEXT = (
    "[tool.hatch.version.raw-options]\n"
    'root = "../.."\n'
    '# fallback_version = "keep me" is a comment, not the key\n'
    'fallback_version = "{version}"\n'
)
MANIFESTS = [
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "plugin.json",
    "gemini-extension.json",
]
#: The shapes of link README.md holds: a picture source, a relative link, a `./`
#: link, an absolute one, and an anchor. Only the relative ones change in claude/.
README = (
    '<img src="assets/preview-light.png" />\n'
    "See [LICENSE](LICENSE), [AGENTS.md](./AGENTS.md),\n"
    "[the docs](https://docs.exmergo.com/dex) and [Install](#install).\n"
)
README_IN_CLAUDE = (
    '<img src="https://raw.githubusercontent.com/exmergo/dex/main/'
    'assets/preview-light.png" />\n'
    "See [LICENSE](https://github.com/exmergo/dex/blob/main/LICENSE),"
    " [AGENTS.md](https://github.com/exmergo/dex/blob/main/AGENTS.md),\n"
    "[the docs](https://docs.exmergo.com/dex) and [Install](#install).\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [GIT, "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    """A committed scratch repository holding everything the release script
    touches, with the real script, towncrier.toml and changelog.d/README.md."""

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(PREPARE, repo / "scripts" / "prepare_release.sh")
    shutil.copy2(ROOT / "towncrier.toml", repo / "towncrier.toml")
    (repo / "CHANGELOG.md").write_text(HEADER + HISTORY, encoding="utf-8")
    fragments = repo / "changelog.d"
    fragments.mkdir()
    shutil.copy2(ROOT / "changelog.d" / "README.md", fragments / "README.md")
    for name, body in FRAGMENTS.items():
        (fragments / name).write_text(body, encoding="utf-8")
    for wrapper in WRAPPERS:
        path = repo / wrapper
        path.parent.mkdir(parents=True)
        path.write_text('DEX_CORE_VERSION = "1.0.1"\n', encoding="utf-8")
    engine_pyproject = repo / ENGINE_PYPROJECT
    engine_pyproject.parent.mkdir(parents=True)
    engine_pyproject.write_text(
        ENGINE_PYPROJECT_TEXT.format(version="1.0.1"), encoding="utf-8"
    )
    for manifest in MANIFESTS:
        path = repo / manifest
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            '{\n  "name": "dex",\n  "version": "1.0.1"\n}\n', encoding="utf-8"
        )
    (repo / "README.md").write_text(README, encoding="utf-8")
    (repo / "LICENSE").write_text("Apache License\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=dex",
        "-c",
        "user.email=dex@example.invalid",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        "fixture",
    )

    # The dev extra's towncrier, through a wrapper so the interpreter path is one
    # word whatever directory it lives in.
    towncrier = tmp_path / "towncrier"
    towncrier.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m towncrier "$@"\n', encoding="utf-8"
    )
    towncrier.chmod(towncrier.stat().st_mode | stat.S_IXUSR)
    return repo


def _prepare(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TOWNCRIER": str(repo.parent / "towncrier")}
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [BASH, str(repo / "scripts" / "prepare_release.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(repo: Path) -> dict[str, str]:
    return {
        str(path.relative_to(repo)): path.read_text(encoding="utf-8")
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


@needs_release_tools
def test_a_release_collects_every_fragment_once_under_its_own_section(release_repo):
    before = _utc_today()
    result = _prepare(release_repo, "9.9.9", "9.9.9-rc.1")
    after = _utc_today()

    assert result.returncode == 0, result.stderr
    text = (release_repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Everything above the marker and everything already released is untouched,
    # byte for byte; only the new section sits between them.
    assert text.startswith(HEADER)
    assert text.endswith(HISTORY)
    section = text[len(HEADER) : -len(HISTORY)]
    assert section.split("\n", 1)[0] in {
        f"## [9.9.9] - {before}",
        f"## [9.9.9] - {after}",
    }
    assert re.findall(r"^### (.+)$", section, re.MULTILINE) == [
        "Added",
        "Changed",
        "Fixed",
    ]
    for body in FRAGMENTS.values():
        headline = body.split("\n", 1)[0]
        assert text.count(headline) == 1, headline
    assert "[#554](https://github.com/exmergo/dex/pull/554)" in section
    assert "[#560](https://github.com/exmergo/dex/pull/560)" in section
    (orphan,) = [line for line in section.splitlines() if "no pull request yet" in line]
    assert "pull/" not in orphan
    # Continuation paragraphs and lists stay inside their bullet.
    assert "\n\n  A second paragraph with the migration guidance." in section
    assert "\n  - one\n  - two\n" in section


@needs_release_tools
def test_a_release_deletes_what_it_consumed_and_stages_nothing(release_repo):
    result = _prepare(release_repo, "9.9.9", "9.9.9-rc.1")

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (release_repo / "changelog.d").iterdir()) == [
        "README.md"
    ]
    for name in FRAGMENTS:
        assert f"consumed changelog.d/{name}" in result.stdout
    for wrapper in WRAPPERS:
        text = (release_repo / wrapper).read_text(encoding="utf-8")
        assert text == 'DEX_CORE_VERSION = "9.9.9"\n'
    # The plugin builds its bundled engine with no .git, so this fallback is the
    # version every `.dex/` stamp it writes carries; it moves with the pin.
    assert (release_repo / ENGINE_PYPROJECT).read_text(
        encoding="utf-8"
    ) == ENGINE_PYPROJECT_TEXT.format(version="9.9.9")
    for manifest in MANIFESTS:
        loaded = json.loads((release_repo / manifest).read_text(encoding="utf-8"))
        assert loaded["version"] == "9.9.9-rc.1"
    # towncrier run inside a checkout would `git add` the changelog and `git rm`
    # the fragments. Every change has to be left in the working tree instead.
    assert _git(release_repo, "diff", "--cached", "--name-only").stdout == ""
    status = _git(release_repo, "status", "--porcelain").stdout.splitlines()
    assert " M CHANGELOG.md" in status
    assert " D changelog.d/554.fixed.md" in status


@needs_release_tools
def test_a_release_regenerates_the_claude_directory_folder(release_repo):
    """claude/ is the folder the Claude directory reads, and it has to hold the
    released skills, so it is copied after the pins move. A file it held that the
    skills no longer track goes, and the icon, read once by the directory, stays."""

    stale = release_repo / "claude" / "skills" / "explore" / "removed.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("dropped from skills/\n", encoding="utf-8")
    icon = release_repo / "claude" / ".claude-plugin" / "icon.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"\x89PNG")

    result = _prepare(release_repo, "9.9.9", "9.9.9-rc.1")

    assert result.returncode == 0, result.stderr
    claude = release_repo / "claude"
    for wrapper in WRAPPERS:
        assert (claude / wrapper).read_text(encoding="utf-8") == (
            'DEX_CORE_VERSION = "9.9.9"\n'
        )
    assert not stale.exists()
    assert icon.read_bytes() == b"\x89PNG"
    manifest = json.loads(
        (claude / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "9.9.9-rc.1"
    assert (claude / "LICENSE").read_text(encoding="utf-8") == "Apache License\n"
    assert (claude / "README.md").read_text(encoding="utf-8") == README_IN_CLAUDE


@needs_release_tools
def test_a_rerun_for_a_prepared_version_changes_nothing(release_repo):
    assert _prepare(release_repo, "9.9.9", "9.9.9-rc.1").returncode == 0
    late = release_repo / "changelog.d" / "570.fixed.md"
    late.write_text("**Merged after the release was prepared.**\n", encoding="utf-8")
    prepared = _tree(release_repo)

    result = _prepare(release_repo, "9.9.9", "9.9.9-rc.1")

    assert result.returncode == 0, result.stderr
    assert "already has a 9.9.9 section" in result.stdout
    assert "1 fragment(s) in changelog.d/ stay pending" in result.stdout
    assert _tree(release_repo) == prepared


@needs_release_tools
@pytest.mark.parametrize(
    "name",
    [
        "554.fix.md",  # not a declared type
        "fix-help.fixed.md",  # neither a pull request number nor a "+" slug
    ],
)
def test_a_fragment_towncrier_refuses_leaves_the_tree_untouched(release_repo, name):
    (release_repo / "changelog.d" / name).write_text(
        "**Misnamed.**\n", encoding="utf-8"
    )
    untouched = _tree(release_repo)

    result = _prepare(release_repo, "9.9.9", "9.9.9-rc.1")

    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
    assert "check_changelog_fragments.py" in result.stderr
    assert _tree(release_repo) == untouched


@needs_release_tools
def test_a_changelog_without_the_marker_is_refused(release_repo):
    # towncrier would not fail here: it would write the release above the title.
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(HEADER.replace(MARKER, "## [Unreleased]") + HISTORY)
    untouched = _tree(release_repo)

    result = _prepare(release_repo, "9.9.9")

    assert result.returncode == 1
    assert MARKER in result.stderr
    assert _tree(release_repo) == untouched


# --- the fragment check ---------------------------------------------------------


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_changelog_fragments", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fragment_root(tmp_path: Path) -> Path:
    shutil.copy2(ROOT / "towncrier.toml", tmp_path / "towncrier.toml")
    (tmp_path / "changelog.d").mkdir()
    shutil.copy2(
        ROOT / "changelog.d" / "README.md", tmp_path / "changelog.d" / "README.md"
    )
    return tmp_path


def _add(root: Path, name: str, body: str = "**A change.**\n") -> None:
    (root / "changelog.d" / name).write_text(body, encoding="utf-8")


def test_the_fragments_in_this_checkout_pass(checker):
    assert checker.check(ROOT) == []


@pytest.mark.parametrize(
    "name",
    ["554.fixed.md", "554.fixed.1.md", "613.security.md", "+help-text.changed.md"],
)
def test_a_well_named_fragment_passes(checker, fragment_root, name):
    _add(fragment_root, name)

    assert checker.check(fragment_root) == []


@pytest.mark.parametrize(
    "name",
    [
        "554.fix.md",  # an undeclared type: towncrier refuses it
        "554.fixed",  # towncrier would collect it, but every fragment is Markdown
        "554.fixed.txt",
        "fix-help.fixed.md",  # a slug without the "+" renders as a broken link
        "+.fixed.md",
        "554.md",
        "notes.md",
    ],
)
def test_a_misnamed_fragment_is_named_with_the_fix(checker, fragment_root, name):
    _add(fragment_root, name)

    (problem,) = checker.check(fragment_root)

    assert f"changelog.d/{name} is not named as a fragment" in problem
    assert "added, changed, deprecated, removed, fixed, security" in problem


def test_an_empty_fragment_is_refused(checker, fragment_root):
    _add(fragment_root, "554.fixed.md", "\n  \n")

    (problem,) = checker.check(fragment_root)

    assert "changelog.d/554.fixed.md is empty" in problem


def test_a_directory_in_changelog_d_is_refused(checker, fragment_root):
    (fragment_root / "changelog.d" / "fixed").mkdir()

    (problem,) = checker.check(fragment_root)

    assert "changelog.d/fixed/ is a directory" in problem


def test_a_pull_request_with_a_fragment_passes(checker, fragment_root):
    _add(fragment_root, "554.fixed.md")
    changes = "M\tpackages/dex-core/src/x.py\nA\tchangelog.d/554.fixed.md\n"

    assert checker.check(fragment_root, changes) == []


@pytest.mark.parametrize(
    "changes",
    [
        "M\tpackages/dex-core/src/x.py\n",
        "M\tchangelog.d/README.md\n",  # the guide is not a fragment
        "D\tchangelog.d/554.fixed.md\n",  # removing one is not describing a change
    ],
)
def test_a_pull_request_without_a_fragment_is_told_how_to_add_one(
    checker, fragment_root, changes
):
    (problem,) = checker.check(fragment_root, changes)

    assert "adds no changelog fragment" in problem
    assert "no-changelog label" in problem


def test_a_direct_changelog_edit_is_told_how_to_move_it(checker, fragment_root):
    _add(fragment_root, "554.fixed.md")
    changes = "M\tCHANGELOG.md\nA\tchangelog.d/554.fixed.md\n"

    (problem,) = checker.check(fragment_root, changes)

    assert "edits CHANGELOG.md" in problem
    assert "git checkout origin/main -- CHANGELOG.md" in problem


@pytest.mark.parametrize("changes", ["M\tsrc/x.py\n", "M\tCHANGELOG.md\n"])
def test_the_no_changelog_label_exempts_a_pull_request(checker, fragment_root, changes):
    assert checker.check(fragment_root, changes, {"bug", "no-changelog"}) == []


def test_the_label_does_not_exempt_a_misnamed_fragment(checker, fragment_root):
    _add(fragment_root, "554.fix.md")

    (problem,) = checker.check(fragment_root, "M\tCHANGELOG.md\n", {"no-changelog"})

    assert "554.fix.md is not named as a fragment" in problem


def test_the_command_line_fails_with_the_guide(tmp_path: Path):
    changes = tmp_path / "changes.txt"
    changes.write_text("M\tCHANGELOG.md\n", encoding="utf-8")
    run = [sys.executable, str(CHECKER), "--changes", str(changes)]

    refused = subprocess.run(run, capture_output=True, text=True, check=False)  # noqa: S603
    exempt = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [*run, "--labels", "documentation,no-changelog"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert refused.returncode == 1
    assert "CONTRIBUTING.md#changelog-fragments" in refused.stderr
    assert exempt.returncode == 0, exempt.stderr


# --- one towncrier version ------------------------------------------------------

PIN = re.compile(r"towncrier(?:@|==)(\d+(?:\.\d+)+)")


@pytest.mark.skipif(GIT is None, reason="lists the repository's files with git")
def test_every_towncrier_pin_is_the_same_version():
    listed = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [GIT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pins: dict[str, set[str]] = {}
    for name in filter(None, listed.split("\0")):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = set(PIN.findall(text))
        if found:
            pins[name] = found

    # The places a release, CI and the tests take the version from.
    assert {
        "scripts/prepare_release.sh",
        ".github/workflows/changelog.yml",
        "packages/dex-core/pyproject.toml",
    } <= pins.keys()
    assert len(set().union(*pins.values())) == 1, pins
