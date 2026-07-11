# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Thin PEP 723 wrapper that drives dex-core via the command contract.

The skill never re-implements logic. It forwards its arguments to the pinned
`dex-core` engine and lets the engine print the sanitized JSON envelope. Run it
with `uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <dex subcommand> ...`.

Two execution modes, chosen automatically:
  - Monorepo checkout (this repo): `packages/dex-core` is found above the skill,
    so the engine runs from an editable local install. This is what makes the
    wrapper work before the package is published.
  - Installed plugin: no local package is present, so the pinned PyPI release is
    installed hermetically by uv.

The engine version is pinned; the connector *extra* is chosen at runtime from the
active connector, so one published release serves every warehouse. This wrapper is
stdlib-only and runs before the engine is installed, so it resolves the connector
itself (it cannot import the engine) with the same precedence the engine uses:
an explicit --connector flag, then the top-level `connector:` in
<repo-root>/.dex/config.yml, then DuckDB. The guess only picks which extra to
install; the full argv is still forwarded, so the engine stays authoritative for
the actual connection and a wrong guess surfaces as a clean error envelope.

`DEX_CORE_VERSION` is the single line bumped at release time, by
scripts/prepare_release.sh before the tag; nothing else here changes per release.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Rewritten by scripts/prepare_release.sh to the tagged version. The connector
# extra is deliberately NOT part of this pin: it is chosen at runtime (see
# _resolve_connector), so a release artifact is connector-neutral.
DEX_CORE_VERSION = "1.1.1"

# Connector id -> packaging extra. The engine's connector ids and the pyproject
# extras share names, so this is the identity set today. An unknown or unset
# connector falls back to the light DuckDB on-ramp and lets the installed engine
# emit the canonical error rather than the wrapper guessing wrong.
_KNOWN_CONNECTORS = ("duckdb", "snowflake", "bigquery", "databricks", "postgres")
_DEFAULT_CONNECTOR = "duckdb"


def _connector_from_config(config_path: Path) -> str | None:
    """Read the top-level scalar `connector:` from .dex/config.yml, stdlib only.

    This bootstrap script has no YAML dependency and only needs enough to pick the
    right extra to install, so it scans for a single unindented `connector:` key.
    The engine remains the source of truth for the full config; anything richer or
    malformed here just falls through to the DuckDB default.
    """

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("connector:"):  # top-level only; indented keys ignored
            value = line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
            return value or None
    return None


def _resolve_connector(argv: list[str], cwd: Path) -> str:
    """Pick the connector whose extra we install, mirroring the engine's order:
    explicit --connector, then .dex/config.yml, then DuckDB."""

    # allow_abbrev=False and parse_known_args so we only peek at these two flags
    # and never consume or reorder the argv that is forwarded to the engine.
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--connector")
    parser.add_argument("--repo-root", default=".")
    known, _ = parser.parse_known_args(argv)

    connector = known.connector or _connector_from_config(
        cwd / known.repo_root / ".dex" / "config.yml"
    )
    return connector if connector in _KNOWN_CONNECTORS else _DEFAULT_CONNECTOR


def _engine_spec(connector: str, skill_dir: Path | None = None) -> list[str]:
    skill_dir = skill_dir or Path(
        os.environ.get("CLAUDE_SKILL_DIR", Path(__file__).resolve().parent.parent)
    )
    local_pkg = (skill_dir / ".." / ".." / "packages" / "dex-core").resolve()
    if local_pkg.is_dir():
        # Resolve the local package WITH the connector extra (a plain path drops
        # extras). Non-editable is fine: the engine is imported fresh each run.
        return ["--with", f"exmergo-dex-core[{connector}] @ {local_pkg.as_uri()}"]
    return ["--with", f"exmergo-dex-core[{connector}]=={DEX_CORE_VERSION}"]


def main() -> int:
    argv = sys.argv[1:]
    connector = _resolve_connector(argv, Path.cwd())
    cmd = [
        "uv",
        "run",
        *_engine_spec(connector),
        "python",
        "-m",
        "exmergo_dex_core",
        *argv,
    ]
    # The engine runs in uv's own ephemeral environment, so an inherited
    # VIRTUAL_ENV (e.g. the user's activated venv) is irrelevant here and only
    # makes uv print a mismatch warning on every call. Drop it.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
