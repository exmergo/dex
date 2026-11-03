"""The agent procedure: one text, held here, carried by every surface (#502).

How an agent should work with Dex used to be written in four places that drift
apart (three SKILL.md bodies and ``AGENTS.md``), and an agent driving the CLI
from the PyPI package had none of them. The text now lives once, in
``procedure.md`` beside this module, and ``dex procedure`` emits it. Every copy
sits between the two markers below, so a copy can be found, checked against
this text, and refreshed.

The engine never writes a copy. ``dex procedure --block`` returns the
paste-ready block and the human, or a harness, places it, the way
``explore diagram`` returns a diagram without writing one. ``check`` only reads.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from functools import cache
from importlib import resources

BEGIN = "<!-- dex:procedure:begin -->"
END = "<!-- dex:procedure:end -->"

__all__ = [
    "BEGIN",
    "END",
    "CopyStatus",
    "block",
    "check",
    "digest",
    "region",
    "text",
]


class CopyStatus(str, Enum):
    """How one file's copy compares with the engine's text."""

    CURRENT = "current"  # exactly one marked region, equal to the text
    STALE = "stale"  # exactly one marked region, different from the text
    MISSING = "missing"  # no marked region at all
    MALFORMED = "malformed"  # markers repeated, unpaired, or out of order


@cache
def text() -> str:
    """The canonical procedure, as Markdown ending in one newline."""

    return (
        resources.files("exmergo_dex_core.procedure")
        .joinpath("procedure.md")
        .read_text(encoding="utf-8")
    )


def digest() -> str:
    """A short, stable fingerprint of the text, so a copy can name its version."""

    return hashlib.sha256(text().encode("utf-8")).hexdigest()[:12]


def block() -> str:
    """The paste-ready block: the text between the two markers.

    Pasted into a ``CLAUDE.md`` or ``AGENTS.md``, it is found and checked by
    :func:`check` exactly as the copies in this repository are.
    """

    return f"{BEGIN}\n{text()}{END}\n"


def region(document: str) -> str | None:
    """The text between the markers, or None where there is not exactly one
    well-ordered pair. The leading newline after the begin marker is dropped,
    so a current copy's region equals :func:`text`."""

    if document.count(BEGIN) != 1 or document.count(END) != 1:
        return None
    start, end = document.index(BEGIN), document.index(END)
    if start > end:
        return None
    inner = document[start + len(BEGIN) : end]
    return inner[1:] if inner.startswith("\n") else inner


def check(document: str) -> CopyStatus:
    """Compare a document's marked copy with the engine's text."""

    if BEGIN not in document and END not in document:
        return CopyStatus.MISSING
    inner = region(document)
    if inner is None:
        return CopyStatus.MALFORMED
    return CopyStatus.CURRENT if inner == text() else CopyStatus.STALE
