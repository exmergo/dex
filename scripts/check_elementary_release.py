"""Fail when the dbt package hub has an Elementary release the fixture does not run.

The weekly watch for the release matrix (#1446). It reads the package's versions
from the hub the way ``dbt deps`` does (``dbt/clients/registry.py`` builds
``<hub>/api/v1/<package>.json``, keyless), and the newest release Dex was run
against from ``TESTED_RELEASES`` in the engine's source, read without importing
the engine so the job needs no install. A release newer than that is a step 1
of "Adding a new Elementary release" in CONTRIBUTING.md: the job fails naming
it, and a person opens the pull request step 2 describes.

    python3 scripts/check_elementary_release.py
    python3 scripts/check_elementary_release.py --payload hub.json

``--payload`` reads a saved hub response instead of the network. Exit 0: the
newest slot is the hub's newest release. Exit 1: a newer release is on the hub.
Exit 2: the hub or the engine's source could not be read. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "packages/dex-core/src/exmergo_dex_core/transform/elementary.py"
PACKAGE = "elementary-data/elementary"
HUB = "https://hub.getdbt.com/"
TIMEOUT = 30

#: A release, ``major.minor.patch`` with an optional prerelease, as the engine's
#: ``release_key`` reads one. A prerelease is never a slot, so it is skipped.
_RELEASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


def release(text: str) -> tuple[int, int, int] | None:
    """``text`` as an orderable final release, ``None`` for anything else."""

    match = _RELEASE.fullmatch(str(text).strip())
    if match is None or match.group(4):
        return None
    return tuple(int(part) for part in match.groups()[:3])  # type: ignore[return-value]


def tested_releases(source: str) -> tuple[str, ...]:
    """``TESTED_RELEASES`` as the engine's source assigns it, without importing it."""

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(getattr(t, "id", None) == "TESTED_RELEASES" for t in targets):
                return tuple(ast.literal_eval(node.value))
    raise ValueError("no TESTED_RELEASES assignment in the engine's source")


def hub_releases(payload: object) -> list[str]:
    """The final releases a hub response lists for the package."""

    if not isinstance(payload, dict):
        raise ValueError("the hub response is not a JSON object")
    versions = payload.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise ValueError("the hub response lists no versions")
    return [v for v in versions if release(v) is not None]


def verdict(tested: tuple[str, ...], published: list[str]) -> tuple[int, str]:
    """The exit code and the line to print."""

    newest_tested = max(tested, key=lambda v: release(v) or (0, 0, 0))
    floor = release(newest_tested)
    newer = sorted(
        (v for v in published if (release(v) or (0, 0, 0)) > (floor or (0, 0, 0))),
        key=lambda v: release(v) or (0, 0, 0),
    )
    if not newer:
        return 0, (
            f"Elementary {newest_tested}, the newest slot of the release matrix, "
            f"is the newest release on the dbt package hub"
        )
    return 1, (
        f"::error::Elementary {newer[-1]} is on the dbt package hub; the newest "
        f"release the fixture runs is {newest_tested}"
        + (f" (also newer: {', '.join(newer[:-1])})" if len(newer) > 1 else "")
        + '. Follow "Adding a new Elementary release" in CONTRIBUTING.md: move '
        "the newest slot in a pull request that runs both fixture files."
    )


def fetch(hub: str = HUB) -> dict:
    url = f"{hub.rstrip('/')}/api/v1/{PACKAGE}.json"
    if not url.startswith("https://"):
        raise ValueError(f"the hub URL is not https: {url}")
    # https only, checked above, so no file: or custom scheme is opened.
    request = urllib.request.Request(url, headers={"User-Agent": "dex-ci"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, help="a saved hub response")
    parser.add_argument("--engine", type=Path, default=ENGINE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        tested = tested_releases(args.engine.read_text(encoding="utf-8"))
        if args.payload is not None:
            payload = json.loads(args.payload.read_text(encoding="utf-8"))
        else:
            payload = fetch()
        published = hub_releases(payload)
    except (OSError, ValueError) as exc:
        print(f"::error::cannot compare the matrix with the hub: {exc}")
        return 2
    code, line = verdict(tested, published)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
