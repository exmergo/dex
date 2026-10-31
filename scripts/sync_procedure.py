"""Refresh every in-repository copy of the agent procedure from the engine (#502).

The procedure lives once, in ``packages/dex-core/src/exmergo_dex_core/procedure/
procedure.md``. The three SKILL.md bodies and ``AGENTS.md`` each carry a copy
between the ``dex:procedure`` markers, and ``tests/test_skill_bodies.py`` fails
while any copy differs. Edit the engine's text, then run:

    uv run --project packages/dex-core python scripts/sync_procedure.py

``--check`` reports the stale copies and exits 1 without writing. This is
repository tooling: the engine itself never writes a copy (``dex procedure
--block`` returns the block for a person or a harness to place).

``claude/skills/`` is not touched here: ``scripts/prepare_release.sh`` regenerates
it from ``skills/`` at release time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "dex-core" / "src"))

from exmergo_dex_core.procedure import (  # noqa: E402
    BEGIN,
    END,
    CopyStatus,
    check,
    text,
)

COPIES = (
    "skills/explore/SKILL.md",
    "skills/transform/SKILL.md",
    "skills/maintain/SKILL.md",
    "AGENTS.md",
)


def refreshed(document: str) -> str:
    start, end = document.index(BEGIN), document.index(END)
    return document[: start + len(BEGIN)] + "\n" + text() + document[end:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report, never write")
    args = parser.parse_args(argv)
    failed = False
    for relpath in COPIES:
        path = ROOT / relpath
        document = path.read_text(encoding="utf-8")
        status = check(document)
        if status is CopyStatus.CURRENT:
            continue
        if status is not CopyStatus.STALE:
            print(f"{relpath}: {status.value}; place one marked region by hand")
            failed = True
            continue
        if args.check:
            print(f"{relpath}: stale")
            failed = True
            continue
        path.write_text(refreshed(document), encoding="utf-8")
        print(f"{relpath}: refreshed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
