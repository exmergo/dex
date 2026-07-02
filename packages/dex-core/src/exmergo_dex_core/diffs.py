"""Reviewable file diffs for the envelope's ``diffs`` field.

Every proposed repo change crosses the boundary as a unified diff (propose-don't-
impose): the agent and the human review the diff; nothing is applied just by being
emitted. Diffs live in the envelope's dedicated ``diffs`` field, never in ``data``,
so SQL or YAML text is not mistaken for a raw-row payload by the sanitizer (which
scans only ``data``).

Named ``diffs`` (plural) because ``diff.py`` is the maintain drift engine: that
module diffs warehouse state against a snapshot, this one renders file changes.
"""

from __future__ import annotations

import difflib
from typing import Any


def file_diff(path: str, old: str | None, new: str | None) -> dict[str, Any]:
    """Render one file change as an envelope-ready diff dict.

    ``old is None`` means the file does not exist yet (a create); ``new is None``
    means it is being removed (a delete). The unified text uses ``a/``/``b/``
    prefixes so it reads like ``git diff`` output.
    """

    if old is None and new is None:
        raise ValueError("file_diff needs at least one of old/new content")

    op = "update"
    if old is None:
        op = "create"
    elif new is None:
        op = "delete"

    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    unified = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}" if old is not None else "/dev/null",
            tofile=f"b/{path}" if new is not None else "/dev/null",
        )
    )

    additions = sum(
        1
        for line in unified.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1
        for line in unified.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return {
        "path": path,
        "op": op,
        "unified": unified,
        "additions": additions,
        "deletions": deletions,
    }
