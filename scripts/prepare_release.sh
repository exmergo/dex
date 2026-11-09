#!/usr/bin/env bash
# Prepare a release BEFORE tagging it: couple the plugin to the engine version and
# write the release's section of CHANGELOG.md.
#
# The engine version is the git tag (hatch-vcs derives it at build time), but the
# skill wrappers pin the engine to an exact version (DEX_CORE_VERSION). This script
# rewrites that version in all three wrappers so the tagged commit is
# self-consistent: checking out the tag, or pinning the catalog to it, installs
# exactly the engine the tag publishes. The connector extra is not part of the pin;
# the wrapper selects it at runtime, so a release is connector-neutral. It also
# rewrites hatch-vcs's fallback_version in packages/dex-core/pyproject.toml, which
# is the version the installed plugin's engine reports: the plugin builds that
# bundled source from a directory with no .git, where the tag cannot be read.
#
# It also builds the release's dated CHANGELOG.md section (the date is today in
# UTC) from the fragments in changelog.d/, and deletes the fragments it used. The
# release workflow only verifies all this; it never writes back. Run this, review
# the diff, commit, then tag.
#
# Last, it regenerates claude/, the plugin folder the Claude directory reads. The
# directory scans only that folder, so it has to hold everything the plugin loads:
# the skills (tracked files only, copied after their pins move), the Claude
# manifest, the license, and the README with its repository-relative links made
# absolute, since README.md's targets are not in claude/. The installed folder has
# no packages/dex-core beside its skills, so the wrappers install the engine they
# pin from PyPI. claude/.claude-plugin/icon.png is the one file not regenerated:
# the directory reads the icon once, when a listing is first saved. Nothing else
# in claude/ is edited by hand; it moves with each release, not with main.
#
# Usage:
#   scripts/prepare_release.sh <engine-version> [plugin-semver]
#   scripts/prepare_release.sh 0.1.0a1
#   scripts/prepare_release.sh 0.1.0a1 0.1.0-alpha.1
#
# <engine-version> is the PEP 440 version you will tag, without the leading v
# (for example 0.1.0a1 for an alpha, 0.1.0 for a release). Use the canonical
# PEP 440 spelling: it must match the built wheel name the workflow asserts on.
# [plugin-semver], if given, bumps the plugin manifests each agent reads:
# .claude-plugin/plugin.json (Claude Code), .codex-plugin/plugin.json (Codex),
# plugin.json (the Agent Plugins manifest Copilot CLI, Cursor and Kiro read), and
# gemini-extension.json (Gemini CLI). A
# manifest left behind advertises the previous version to that agent's users, and
# packages/dex-core/tests/test_plugin_manifests.py fails the release gate on it. The
# plugin version is semver, distinct from the engine's PEP 440 string, so it is set
# explicitly rather than copied (0.1.0a1 is valid PEP 440 but not valid semver).
#
# Towncrier renders the notes, fetched by uvx at the version pinned below. Setting
# TOWNCRIER replaces that command, for example with an installed
# `python -m towncrier`; the test suite does this so it needs no network.
set -euo pipefail
shopt -s nullglob

ENGINE_VERSION="${1:?usage: prepare_release.sh <engine-version> [plugin-semver]}"
ENGINE_VERSION="${ENGINE_VERSION#v}"
PLUGIN_VERSION="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_DATE="$(date -u +%Y-%m-%d)"
read -r -a TOWNCRIER_CMD <<<"${TOWNCRIER:-uvx towncrier@26.9.0}"

CHANGELOG="${ROOT}/CHANGELOG.md"
FRAGMENTS="${ROOT}/changelog.d"
MARKER="<!-- towncrier release notes start -->"

# Without the marker towncrier does not fail: it writes the release above the
# file's title. So its absence is refused here, before anything is touched.
if ! grep -qxF "${MARKER}" "${CHANGELOG}"; then
  echo "CHANGELOG.md has no \"${MARKER}\" line to release under" >&2
  exit 1
fi

# The notes are built before any file is edited, so a fragment towncrier refuses
# leaves the working tree exactly as it was. A rerun for a version that already
# has its section leaves the changelog and any newer fragments alone.
built=""
if grep -qF "## [${ENGINE_VERSION}]" "${CHANGELOG}"; then
  pending=0
  for f in "${FRAGMENTS}"/*; do
    [ "${f##*/}" = "README.md" ] || pending=$((pending + 1))
  done
  echo "CHANGELOG.md already has a ${ENGINE_VERSION} section; left unchanged"
  echo "(${pending} fragment(s) in changelog.d/ stay pending for the next release)"
else
  # Run in a checkout, `towncrier build` stages CHANGELOG.md and runs `git rm` on
  # the fragments it used. Building in a copy outside any repository keeps this
  # script to working-tree edits, so `git diff` shows the whole release and the
  # maintainer decides what gets staged.
  built="$(mktemp -d)"
  trap 'rm -rf "${built}"' EXIT
  cp "${ROOT}/towncrier.toml" "${CHANGELOG}" "${built}/"
  cp -R "${FRAGMENTS}" "${built}/changelog.d"
  if ! (
    cd "${built}"
    "${TOWNCRIER_CMD[@]}" build --yes --version "${ENGINE_VERSION}" --date "${RELEASE_DATE}"
  ) >/dev/null; then
    echo "towncrier could not build the notes; nothing was changed." >&2
    echo "python3 scripts/check_changelog_fragments.py names the fragment at fault." >&2
    exit 1
  fi
fi

for skill in explore transform maintain; do
  f="${ROOT}/skills/${skill}/scripts/run.py"
  sed -i.bak -E \
    "s/DEX_CORE_VERSION = \"[^\"]*\"/DEX_CORE_VERSION = \"${ENGINE_VERSION}\"/" \
    "$f"
  rm -f "${f}.bak"
  echo "pinned ${f#"${ROOT}/"} -> ${ENGINE_VERSION}"
done

f="${ROOT}/packages/dex-core/pyproject.toml"
sed -i.bak -E \
  "s/^fallback_version = \"[^\"]*\"/fallback_version = \"${ENGINE_VERSION}\"/" \
  "$f"
rm -f "${f}.bak"
echo "pinned ${f#"${ROOT}/"} fallback_version -> ${ENGINE_VERSION}"

if [ -n "${PLUGIN_VERSION}" ]; then
  for manifest in .claude-plugin/plugin.json .codex-plugin/plugin.json plugin.json gemini-extension.json; do
    f="${ROOT}/${manifest}"
    sed -i.bak -E "s/(\"version\": \")[^\"]+(\")/\1${PLUGIN_VERSION}\2/" "$f"
    rm -f "${f}.bak"
    echo "bumped ${manifest} -> ${PLUGIN_VERSION}"
  done
fi

CLAUDE_DIR="${ROOT}/claude"
rm -rf "${CLAUDE_DIR}/skills"
while IFS= read -r -d '' f; do
  mkdir -p "${CLAUDE_DIR}/$(dirname "$f")"
  cp "${ROOT}/${f}" "${CLAUDE_DIR}/${f}"
done < <(git -C "${ROOT}" ls-files -z skills)
mkdir -p "${CLAUDE_DIR}/.claude-plugin"
cp "${ROOT}/.claude-plugin/plugin.json" "${CLAUDE_DIR}/.claude-plugin/plugin.json"
cp "${ROOT}/LICENSE" "${CLAUDE_DIR}/LICENSE"
perl -pe 's#\]\((?!https?://|\#|mailto:)(?:\./)?([^)\s]+)\)#](https://github.com/exmergo/dex/blob/main/$1)#g;
          s#(src(?:set)?=")(?!https?://)([^"]+)"#$1https://raw.githubusercontent.com/exmergo/dex/main/$2"#g' \
  "${ROOT}/README.md" >"${CLAUDE_DIR}/README.md"
echo "regenerated claude/ (skills, .claude-plugin/plugin.json, LICENSE, README.md)"

# Towncrier deleted the fragments it used from the copy, so whatever is missing
# there is exactly what this release consumed.
if [ -n "${built}" ]; then
  cp "${built}/CHANGELOG.md" "${CHANGELOG}"
  echo "added \"## [${ENGINE_VERSION}] - ${RELEASE_DATE}\" to CHANGELOG.md"
  for f in "${FRAGMENTS}"/*; do
    if [ ! -e "${built}/changelog.d/${f##*/}" ]; then
      rm -f "$f"
      echo "consumed ${f#"${ROOT}/"}"
    fi
  done
fi

echo
echo "Review the whole release, then commit it together (-a includes the deleted"
echo "fragments; git add claude picks up a file new to claude/). The pull request"
echo "carries the no-changelog label:"
echo "  git status && git diff"
echo "  git add claude && git commit -am \"Release ${ENGINE_VERSION}\""
echo "Once it is merged, tag the merged commit:"
echo "  git tag v${ENGINE_VERSION} <merged-commit> && git push origin v${ENGINE_VERSION}"
