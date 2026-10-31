"""``dex procedure``: the agent procedure, in the standard envelope (#502).

Routed in ``main`` before any engine is built, like ``dex config``: it opens no
connection, reads no ``.dex/config.yml``, and needs no connector extra, so an
agent with nothing but the package can get the procedure, and a harness can
get it before a project exists.
"""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import Field

from .. import envelope as env
from ..results import Result, to_envelope
from . import BEGIN, END, CopyStatus, block, check, digest, text

#: How to place the block, stated once so the envelope and the docs agree.
PLACEMENT = (
    "Paste data.block into the repository's CLAUDE.md or AGENTS.md (or the agent "
    "config a harness hands its agent). Dex never writes either file. Re-run "
    "`dex procedure --check <file>` after upgrading Dex to find a stale copy."
)


def _engine_version() -> str:
    try:
        return version("exmergo-dex-core")
    except PackageNotFoundError:  # pragma: no cover - a source tree run
        return "0.0.0"


class ProcedureResult(Result):
    """The procedure text, optionally the paste-ready block, and copy checks."""

    with_block: bool = False
    copies: list[dict[str, Any]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "text": text(),
            "digest": digest(),
            "engine_version": _engine_version(),
            "markers": {"begin": BEGIN, "end": END},
            "placement": PLACEMENT,
        }
        if self.with_block:
            data["block"] = block()
        if self.copies:
            data["copies"] = self.copies
        return data


def _copy_row(path: str) -> dict[str, Any]:
    target = Path(path)
    try:
        document = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"path": path, "status": "absent", "detail": "no such file"}
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path, "status": "unreadable", "detail": type(exc).__name__}
    status = check(document)
    detail = {
        CopyStatus.CURRENT: "matches this engine's procedure",
        CopyStatus.STALE: "differs from this engine's procedure; paste "
        "`dex procedure --block` over the marked region",
        CopyStatus.MISSING: "carries no marked procedure; paste `dex procedure "
        "--block` into it",
        CopyStatus.MALFORMED: "its procedure markers are repeated, unpaired or out "
        "of order; keep exactly one begin and one end marker",
    }[status]
    return {"path": path, "status": status.value, "detail": detail}


def cmd_procedure(args: argparse.Namespace) -> env.Envelope:
    """The CLI shim. ``main`` routes here before it builds an engine."""

    paths = list(getattr(args, "check", None) or [])
    result = ProcedureResult(
        with_block=bool(getattr(args, "block", False)),
        copies=[_copy_row(path) for path in paths],
    )
    envelope = to_envelope(result)
    problems = [row for row in result.copies if row["status"] != "current"]
    if not problems:
        return envelope
    # An error rather than `ok` carrying problems, so `--check` gates CI on its
    # own, as `dex config validate` does. The payload stays whole: a caller
    # reads which copies are stale and gets the text to refresh them with.
    envelope.status = env.Status.ERROR
    envelope.reason = env.Reason.REQUEST
    envelope.errors = [f"{row['path']}: {row['detail']}" for row in problems]
    return envelope
