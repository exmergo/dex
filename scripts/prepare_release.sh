#!/usr/bin/env bash
# Couple the plugin to a specific engine release BEFORE tagging it.
#
# The engine version is the git tag (hatch-vcs derives it at build time), but the
# skill wrappers install the engine by an exact pin (DEX_CORE_PIN). This script
# rewrites that pin in all three wrappers so the tagged commit is self-consistent:
# checking out the tag, or pinning the catalog to it, installs exactly the engine
# the tag publishes. The release workflow only verifies this coupling; it never
# writes back. Run this, review the diff, commit, then tag.
#
# Usage:
#   scripts/prepare_release.sh <engine-version> [plugin-semver]
#   scripts/prepare_release.sh 0.1.0a1
#   scripts/prepare_release.sh 0.1.0a1 0.1.0-alpha.1
#
# <engine-version> is the PEP 440 version you will tag, without the leading v
# (for example 0.1.0a1 for an alpha, 0.1.0 for a release). Use the canonical
# PEP 440 spelling: it must match the built wheel name the workflow asserts on.
# [plugin-semver], if given, bumps .claude-plugin/plugin.json; its version is
# semver, distinct from the engine's PEP 440 string, so it is set explicitly
# rather than copied (0.1.0a1 is valid PEP 440 but not valid semver).
set -euo pipefail

ENGINE_VERSION="${1:?usage: prepare_release.sh <engine-version> [plugin-semver]}"
ENGINE_VERSION="${ENGINE_VERSION#v}"
PLUGIN_VERSION="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

for skill in explore transform maintain; do
  f="${ROOT}/skills/${skill}/scripts/run.py"
  sed -i.bak -E \
    "s/exmergo-dex-core\[duckdb\]==[0-9][^\"]*/exmergo-dex-core[duckdb]==${ENGINE_VERSION}/" \
    "$f"
  rm -f "${f}.bak"
  echo "pinned ${f#"${ROOT}/"} -> ${ENGINE_VERSION}"
done

if [ -n "${PLUGIN_VERSION}" ]; then
  f="${ROOT}/.claude-plugin/plugin.json"
  sed -i.bak -E "s/(\"version\": \")[^\"]+(\")/\1${PLUGIN_VERSION}\2/" "$f"
  rm -f "${f}.bak"
  echo "bumped .claude-plugin/plugin.json -> ${PLUGIN_VERSION}"
fi

echo
echo "Review the diff, commit, then tag:"
echo "  git diff"
echo "  git commit -am \"Release ${ENGINE_VERSION}\""
echo "  git tag v${ENGINE_VERSION} && git push origin v${ENGINE_VERSION}"
