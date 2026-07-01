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

`DEX_CORE_PIN` is the single line bumped at release time, by
scripts/prepare_release.sh before the tag; nothing else here changes per release.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Rewritten by scripts/prepare_release.sh to exmergo-dex-core[duckdb]==X.Y.Z.
DEX_CORE_PIN = "exmergo-dex-core[duckdb]==0.1.0a2"


def _engine_spec() -> list[str]:
    skill_dir = Path(
        os.environ.get("CLAUDE_SKILL_DIR", Path(__file__).resolve().parent.parent)
    )
    local_pkg = (skill_dir / ".." / ".." / "packages" / "dex-core").resolve()
    if local_pkg.is_dir():
        # Resolve the local package WITH its [duckdb] extra (a plain path drops
        # extras). Non-editable is fine: the engine is imported fresh each run.
        return ["--with", f"exmergo-dex-core[duckdb] @ {local_pkg.as_uri()}"]
    return ["--with", DEX_CORE_PIN]


def main() -> int:
    cmd = [
        "uv",
        "run",
        *_engine_spec(),
        "python",
        "-m",
        "exmergo_dex_core",
        *sys.argv[1:],
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
