"""The Cube query dialect: which members a Cube filter names.

The PII gate refuses a filtered query unless the backend can say which members
the filter names (:func:`.explore.semantic.policy.requested_dimension_refs`). A
Cube filter is a JSON object, not an expression: ``{"member": "orders.status",
"operator": "equals", "values": ["paid"]}``, combined with ``{"and": [...]}`` and
``{"or": [...]}``. Each ``--where`` is one such object or a JSON list of them,
which Cube ANDs, so the members are read off the structure with no parser.

**Fail-closed.** :func:`filter_refs` returns None for anything it cannot account
for, and the neutral gate then refuses the query rather than screening half of
it:

- a clause that is not JSON, or not an object or a list of objects;
- a key Cube's filter grammar does not have, or a known key of the wrong shape;
- an operator outside the pinned release's set;
- a member that is not a ``cube.member`` string, or a logical group with
  nothing in it;
- ``values`` that are not a list of scalars.

Cube's older spelling, ``dimension`` in place of ``member``, is read as the same
key. A filter on a measure is a ``HAVING`` and is reported like any other
member: the gate resolves a measure token to the columns its ``sql`` reads.

A leaf beside :mod:`.slayer_dialect` and :mod:`.metricflow_dialect`, importing
nothing of dex's but the error base, so it is importable wherever the Cube
layer is.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import DexError

#: The filter operators of the pinned Cube release (``v1.8.14``).
OPERATORS = frozenset(
    {
        "equals",
        "notEquals",
        "contains",
        "notContains",
        "startsWith",
        "notStartsWith",
        "endsWith",
        "notEndsWith",
        "gt",
        "gte",
        "lt",
        "lte",
        "set",
        "notSet",
        "inDateRange",
        "notInDateRange",
        "beforeDate",
        "beforeOrOnDate",
        "afterDate",
        "afterOrOnDate",
        "measureFilter",
    }
)

_MEMBER = re.compile(r"[_a-zA-Z][_a-zA-Z0-9]*\.[_a-zA-Z][_a-zA-Z0-9]*")
_LEAF_KEYS = frozenset({"member", "dimension", "operator", "values"})


class CubeFilterError(DexError, ValueError):
    """A ``--where`` clause that is not a Cube filter, with what it should be."""


def parse_where(clauses: list[str]) -> list[dict[str, Any]]:
    """The filter objects ``clauses`` carry, in order, or raise naming the clause.

    Each clause is one JSON object or a JSON list of them. This checks the
    container only; :func:`filter_refs` reads what is inside.
    """

    filters: list[dict[str, Any]] = []
    for clause in clauses:
        try:
            parsed = json.loads(clause)
        except ValueError as exc:
            raise CubeFilterError(
                f"--where {clause[:120]!r} is not a Cube filter: under "
                "semantic.vendor: cube a filter is JSON, such as "
                '\'{"member": "orders.status", "operator": "equals", '
                '"values": ["paid"]}\', with "and"/"or" groups for logic'
            ) from exc
        items = parsed if isinstance(parsed, list) else [parsed]
        if not items or not all(isinstance(item, dict) for item in items):
            raise CubeFilterError(
                f"--where {clause[:120]!r} is not a Cube filter: it has to be one "
                "filter object or a list of them"
            )
        filters.extend(items)
    return filters


def filter_refs(filters: list[dict[str, Any]]) -> list[str] | None:
    """The ``cube.member`` tokens these filters name, in order, or None.

    None is the statement that something here could not be read, which the gate
    refuses; it never raises for the same reason.
    """

    found: list[str] = []
    for item in filters:
        if not _walk(item, found):
            return None
    return list(dict.fromkeys(found))


def member_of(item: dict[str, Any]) -> str | None:
    """A leaf filter's member under either spelling."""

    member = item.get("member", item.get("dimension"))
    return member if isinstance(member, str) else None


def _walk(item: Any, found: list[str]) -> bool:
    if not isinstance(item, dict) or not item:
        return False
    logical = [key for key in ("and", "or") if key in item]
    if logical:
        if len(item) != 1:
            return False
        group = item[logical[0]]
        if not isinstance(group, list) or not group:
            return False
        return all(_walk(child, found) for child in group)
    if set(item) - _LEAF_KEYS or ("member" in item and "dimension" in item):
        return False
    member = member_of(item)
    if member is None or not _MEMBER.fullmatch(member):
        return False
    if item.get("operator") not in OPERATORS:
        return False
    values = item.get("values", [])
    if not isinstance(values, list) or not all(
        isinstance(value, (str, int, float, bool)) for value in values
    ):
        return False
    found.append(member)
    return True
