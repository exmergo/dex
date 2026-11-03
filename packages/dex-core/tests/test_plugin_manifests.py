"""The plugin manifests each agent reads, held in step.

Claude Code reads `.claude-plugin/plugin.json`; Codex reads the compatibility
manifest `.codex-plugin/plugin.json`; Copilot CLI, Cursor and Kiro read
the Agent Plugins `plugin.json` at the repository root; Gemini CLI reads
`gemini-extension.json`. `scripts/prepare_release.sh` bumps the version in all
four, and the descriptions and keywords are copies of one another. A manifest
that falls behind advertises the previous version, or a stale description, to that
agent's users and to the directories that list it, with nothing else in the repo
noticing.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLAUDE = ".claude-plugin/plugin.json"
AGENT_PLUGINS = "plugin.json"
GEMINI = "gemini-extension.json"
CODEX = ".codex-plugin/plugin.json"
MANIFESTS = (CLAUDE, AGENT_PLUGINS, GEMINI, CODEX)

# Same reasoning as test_skill_bodies.py: an installed wheel has no manifests beside it.
pytestmark = pytest.mark.skipif(
    not (ROOT / "skills").is_dir(), reason="needs a repository checkout"
)


def manifests() -> dict[str, dict]:
    return {
        path: json.loads((ROOT / path).read_text(encoding="utf-8"))
        for path in MANIFESTS
    }


@pytest.mark.parametrize("field", ["name", "version", "description"])
def test_the_manifests_agree(field: str):
    values = {path: manifest.get(field) for path, manifest in manifests().items()}
    assert len(set(values.values())) == 1, f"manifest {field}s disagree: {values}"


def test_the_name_is_the_install_id():
    """`name` is the install id (`dex@exmergo`) and the `/dex:*` namespace in
    Claude Code; presentation belongs in host-specific manifest fields."""

    assert manifests()[CLAUDE]["name"] == "dex"


def test_the_plugin_manifests_carry_the_same_keywords():
    loaded = manifests()
    assert loaded[AGENT_PLUGINS]["keywords"] == loaded[CLAUDE]["keywords"]
    assert loaded[CODEX]["keywords"] == loaded[CLAUDE]["keywords"]


@pytest.mark.parametrize("field", ["author", "homepage", "repository", "license"])
def test_codex_metadata_matches_the_portable_manifest(field: str):
    loaded = manifests()
    assert loaded[CODEX][field] == loaded[AGENT_PLUGINS][field]


def test_codex_packages_all_three_self_contained_skills():
    manifest = manifests()[CODEX]
    assert manifest["requires_local_executor"] is True
    path = manifest["skills"]
    assert path.startswith("./") and ".." not in Path(path).parts
    skills = ROOT / path
    assert {p.name for p in skills.iterdir() if p.is_dir()} == {
        "explore",
        "transform",
        "maintain",
    }
    for skill in skills.iterdir():
        assert (skill / "SKILL.md").is_file()
        assert (skill / "scripts/run.py").is_file()
        assert any((skill / "references").glob("*.md"))


def test_codex_overlay_is_not_shadowed_by_inline_settings():
    # Codex replaces the overlay wholesale when inline settings are present.
    # Until those are migrated together, this would silently lose the icons and
    # local-executor requirement when installing from the source repository.
    assert "com.openai" not in manifests()[AGENT_PLUGINS].get("extensions", {})


def test_codex_listing_has_bundled_square_icons():
    interface = manifests()[CODEX]["interface"]
    for field in ("displayName", "shortDescription"):
        assert 0 < len(interface[field]) <= 30
    for field in ("composerIcon", "logo"):
        path = interface[field]
        assert path.startswith("./") and ".." not in Path(path).parts
        data = (ROOT / path).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        assert 48 <= width == height <= 4096
        assert len(data) <= 50 * 1024, "catalog icons must fit within 50 KB"


TERMS_URL = "https://www.exmergo.com/terms/dex"
PRIVACY_URL = "https://www.exmergo.com/privacy/dex"


def test_the_directory_listings_carry_the_same_legal_links():
    """Anthropic's directory reads `privacyPolicyUrl` and `termsOfServiceUrl` from
    the Claude manifest, and Codex reads the same links from `interface` with a
    capitalised `URL`. A listing submitted with one stale link is rejected, or
    worse, accepted pointing at the wrong page."""

    loaded = manifests()
    claude, codex = loaded[CLAUDE], loaded[CODEX]["interface"]
    assert claude["privacyPolicyUrl"] == codex["privacyPolicyURL"] == PRIVACY_URL
    assert claude["termsOfServiceUrl"] == codex["termsOfServiceURL"] == TERMS_URL
