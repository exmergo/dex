#!/usr/bin/env bash
# Couple the plugin to a specific engine release BEFORE tagging it.
#
# The engine version is the git tag (hatch-vcs derives it at build time), but the
# skill wrappers pin the engine to an exact version (DEX_CORE_VERSION). This script
# rewrites that version in all three wrappers so the tagged commit is
# self-consistent: checking out the tag, or pinning the catalog to it, installs
# exactly the engine the tag publishes. The connector extra is not part of the pin;
# the wrapper selects it at runtime, so a release is connector-neutral. It also
# opens the release's dated section in CHANGELOG.md (the date is today in UTC). The
# release workflow only verifies this coupling; it never writes back. Run this,
# review the diff, commit, then tag.
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
    "s/DEX_CORE_VERSION = \"[^\"]*\"/DEX_CORE_VERSION = \"${ENGINE_VERSION}\"/" \
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

# Opening the release section directly under [Unreleased] moves every pending
# entry into it and leaves a fresh, empty [Unreleased] above. A rerun for the same
# version leaves the file alone rather than stacking a second heading.
f="${ROOT}/CHANGELOG.md"
heading="## [${ENGINE_VERSION}] - $(date -u +%Y-%m-%d)"
if grep -qF "## [${ENGINE_VERSION}]" "$f"; then
  echo "CHANGELOG.md already has a ${ENGINE_VERSION} section; left unchanged"
elif grep -qxF "## [Unreleased]" "$f"; then
  awk -v heading="${heading}" '
    { print }
    $0 == "## [Unreleased]" { print ""; print heading }
  ' "$f" > "${f}.tmp"
  mv "${f}.tmp" "$f"
  echo "added \"${heading}\" to CHANGELOG.md"
else
  echo "CHANGELOG.md has no \"## [Unreleased]\" heading to release under" >&2
  exit 1
fi

echo
echo "Review the diff, commit, then tag:"
echo "  git diff"
echo "  git commit -am \"Release ${ENGINE_VERSION}\""
echo "  git tag v${ENGINE_VERSION} && git push origin v${ENGINE_VERSION}"
