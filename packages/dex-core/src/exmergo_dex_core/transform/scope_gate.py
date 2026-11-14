"""Reading a plan's stored change scope, with no SQL parser (#1230).

`transform apply` holds a plan to the population changes its change scope
recorded, and a `--for-plan` build reports them. Both read the stored entries
only, so this module imports nothing beyond the standard library: an apply of a
semantic-layer plan, in an install without the SQL extra, must not need
``sqlglot`` to decide there is nothing to gate. :mod:`.change_scope`, which
writes the entries, re-exports these names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Kinds held to an assumption. ``column_added`` and ``expression_changed`` are
#: listed and never judged: adding a requested column and rewriting the formula
#: a fix is about are what repairs do, and calling every one undeclared would
#: bury the changes nobody asked for.
JUDGED = frozenset(
    {
        "column_removed",
        "type_changed",
        "aggregate_changed",
        "null_handling_changed",
    }
)

#: Stored entries that record what could not be read rather than a change.
_UNREAD = frozenset({"unread", "elided"})


def is_population_change(entry: Mapping[str, Any]) -> bool:
    """Whether one stored change-scope entry moves the model's rows.

    A plan stored before the entry carried its ``axis`` is read by kind: every
    judged kind but the output ones is a population change.
    """

    axis = entry.get("axis")
    if axis is not None:
        return axis == "population"
    kind = str(entry.get("kind") or "")
    return bool(kind) and kind not in JUDGED and kind not in _UNREAD


def undeclared_population_changes(
    stored: Mapping[str, list[Mapping[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Each undeclared population change a plan recorded, as ``{model, kind,
    column, reason}``: the changes ``transform apply`` holds to an assumption or
    an explicit acceptance (#1230)."""

    out: list[dict[str, Any]] = []
    for model, entries in sorted((stored or {}).items()):
        for entry in entries or []:
            if entry.get("status") != "undeclared" or not is_population_change(entry):
                continue
            out.append(
                {
                    k: v
                    for k, v in (
                        ("model", model),
                        ("kind", entry.get("kind")),
                        ("column", entry.get("column")),
                        ("columns", entry.get("columns") or None),
                        ("reason", entry.get("reason")),
                    )
                    if v is not None
                }
            )
    return out


def accepted_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """How an accepted population change is matched to a stored entry."""

    return (
        str(entry.get("model") or ""),
        str(entry.get("kind") or ""),
        str(entry.get("column") or ""),
    )
