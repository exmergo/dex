#!/usr/bin/env bash
# Prove the publish-and-pin loop on TestPyPI before any automation (v7 §15.1,
# "mechanism early, automation late"). Run this manually with your TestPyPI
# credentials; it is intentionally not wired into CI.
#
# Steps:
#   1. Build the engine sdist + wheel with uv.
#   2. Publish to TestPyPI.
#   3. Verify a clean install of the [duckdb] extra from TestPyPI works.
#   4. Hand-bump the wrapper pin to the published version and confirm the wrapper
#      drives `dex connect test` against the installed (non-editable) package.
#
# Usage Example:
#   UV_PUBLISH_TOKEN=... scripts/testpypi_dry_run.sh 0.0.1.dev3
set -euo pipefail

VERSION="${1:?usage: testpypi_dry_run.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="${ROOT}/packages/dex-core"
TESTPYPI="https://test.pypi.org/legacy/"

echo "==> build ${VERSION}"
# Clean dist/ so only the freshly built version is published (uv build does not
# clear stale artifacts from earlier versions).
( cd "${PKG}" && rm -rf dist && uv version "${VERSION}" && uv build )

echo "==> publish to TestPyPI"
( cd "${PKG}" && uv publish --publish-url "${TESTPYPI}" )

echo "==> verify a clean install from TestPyPI (isolated temp project)"
TMP="$(mktemp -d)"
(
  cd "${TMP}"
  uv init --quiet
  # unsafe-best-match lets uv consider versions across BOTH indexes. Without it
  # uv's default first-match strategy pins each package to the first index that
  # has it, so TestPyPI's stale jsonschema (3.1.2b0) would shadow PyPI's >=4.18.
  # --refresh bypasses uv's cached index listing (which can lag a just-published
  # version), and the retry loop rides out TestPyPI's index propagation delay.
  for attempt in 1 2 3 4 5 6; do
    if uv add --refresh \
      --index "https://test.pypi.org/simple/" --extra-index-url "https://pypi.org/simple/" \
      --index-strategy unsafe-best-match "exmergo-dex-core[duckdb]==${VERSION}"; then
      break
    fi
    if [ "${attempt}" -eq 6 ]; then
      echo "ERROR: ${VERSION} still not resolvable from TestPyPI after retries" >&2
      exit 1
    fi
    echo "  (version not indexed yet; retrying in 10s...)"
    sleep 10
  done
  # Green path: create a tiny DuckDB store and confirm the installed console
  # script returns a `status: "ok"` capabilities envelope (read_only: true)
  # against it. This proves the published artifact end to end.
  uv run python -c "import duckdb; duckdb.connect('verify.duckdb').execute('create table t(id integer)')"
  echo "--> dex connect test --path verify.duckdb (expect status: ok)"
  uv run dex --path verify.duckdb connect test

  # Negative path: with no target, the contract returns a well-formed
  # clean-error envelope (not a crash). The || true guards the nonzero exit.
  echo "--> dex connect test with no path (expect a clean status: error envelope)"
  uv run dex connect test || true
)

echo "==> hand-bump wrapper pins to ${VERSION} (the release pipeline automates this)"
for skill in explore transform model; do
  sed -i.bak -E \
    "s/exmergo-dex-core\[duckdb\]==[0-9][^\"]*/exmergo-dex-core[duckdb]==${VERSION}/" \
    "${ROOT}/skills/${skill}/scripts/run.py"
  rm -f "${ROOT}/skills/${skill}/scripts/run.py.bak"
done

echo "==> done. Review the bumped pins, then revert before committing if this was a dry run."
