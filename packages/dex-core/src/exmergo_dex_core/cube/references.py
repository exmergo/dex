"""Cube's ``{...}`` references, found and resolved without a Cube compiler.

Every ``sql``, ``sql_table`` and join ``sql`` in a YAML model is a Python
f-string to Cube: text with ``{expression}`` holes, ``\\{`` and ``\\}`` for a
literal brace. A hole is a reference:

- ``{CUBE}`` (or ``{TABLE}``) is the cube the expression sits on;
  ``{CUBE.member}`` one of its members.
- ``{member}`` is a member of that same cube.
- ``{cube}`` is another cube, and ``{cube.member}`` one of its members;
  ``{cube1.cube2.member}`` is a member of ``cube2`` reached from ``cube1``
  through the joins they declare.
- ``{FILTER_PARAMS...}``, ``{SECURITY_CONTEXT...}``, ``{SQL_UTILS...}`` and
  ``{FILTER_GROUP...}`` are values Cube supplies per query. They are not errors,
  but an expression reading one has no single column behind it.

Resolution is what the loader checks a set with (an unresolved ``{member}`` is a
refusal naming the element) and what :mod:`.lineage` substitutes before it reads
an expression. Pure Python: no sqlglot.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .model import Cube

__all__ = [
    "CONTEXT_SYMBOLS",
    "CURRENT_CUBE",
    "Hole",
    "Resolved",
    "resolve",
    "scan",
]

#: Cube's names for the cube an expression sits on.
CURRENT_CUBE = frozenset({"CUBE", "TABLE"})

#: Values Cube supplies per query (`CONTEXT_SYMBOLS` in `CubeSymbols.ts`).
CONTEXT_SYMBOLS = frozenset(
    {
        "SECURITY_CONTEXT",
        "security_context",
        "securityContext",
        "FILTER_PARAMS",
        "FILTER_GROUP",
        "SQL_UTILS",
        "COMPILE_CONTEXT",
    }
)

_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_HEAD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Hole:
    """One ``{...}`` in an expression: where it sits and what it says.

    ``path`` is the dotted name inside, or None for an expression that is not a
    plain path (a context call such as ``FILTER_PARAMS.x.filter('y')``), whose
    leading name is kept in ``head``.
    """

    start: int
    end: int
    text: str
    path: tuple[str, ...] | None
    head: str | None


@dataclass(frozen=True)
class Resolved:
    """What one hole refers to.

    ``kind`` is ``table`` (a cube's relation: ``{CUBE}``, ``{orders}``),
    ``member`` (``cube``'s member ``member``), or ``context`` (a per-query
    value). ``via`` is the join path walked to reach ``cube`` from the cube the
    expression sits on, empty for the cube itself.
    """

    kind: str
    cube: str | None = None
    member: str | None = None
    via: tuple[str, ...] = ()


def scan(sql: str) -> tuple[list[Hole], str | None]:
    """Every hole in ``sql``, and a problem if its braces do not pair.

    ``\\{`` and ``\\}`` are literal braces. A hole may not contain another,
    because no reference Cube resolves does, and a nested one is an expression
    the f-string transpiler would read differently from a regex.
    """

    holes: list[Hole] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "\\" and sql[index + 1 : index + 2] in {"{", "}"}:
            index += 2
            continue
        if char == "}":
            return holes, f"an unpaired `}}` at position {index}"
        if char != "{":
            index += 1
            continue
        close = _closing(sql, index + 1)
        if close is None:
            return holes, f"an unpaired `{{` at position {index}"
        text = sql[index + 1 : close].strip()
        path = tuple(text.split(".")) if _PATH.match(text) else None
        head_match = _HEAD.match(text)
        holes.append(
            Hole(
                start=index,
                end=close + 1,
                text=text,
                path=path,
                head=head_match.group(1) if head_match else None,
            )
        )
        index = close + 1
    return holes, None


def _closing(sql: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    for index in range(start, len(sql)):
        char = sql[index]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return index
            depth -= 1
    return None


def resolve(
    hole: Hole, current: Cube, cubes: Mapping[str, Cube]
) -> tuple[Resolved | None, str | None]:
    """What ``hole`` refers to from ``current``, or the reason it resolves to
    nothing.

    A path walks declared joins only, in the direction they are declared, the
    way Cube's join graph is directed: ``{orders.customers.region}`` needs
    ``orders`` to join ``customers``.
    """

    if hole.path is None:
        if hole.head in CONTEXT_SYMBOLS:
            return Resolved(kind="context"), None
        return None, f"`{{{hole.text}}}` is not a reference dex can read"
    head, rest = hole.path[0], list(hole.path[1:])
    via: tuple[str, ...]
    if head in CONTEXT_SYMBOLS:
        return Resolved(kind="context"), None

    if head in CURRENT_CUBE:
        cube, via = current, ()
    elif not rest and current.member(head) is not None:
        return Resolved(kind="member", cube=current.name, member=head), None
    elif head in cubes:
        cube, via = cubes[head], ()
    elif (join := current.join_to(head)) is not None and join.target in cubes:
        # A join alias names the cube it reaches, from the cube that declares it.
        cube, via = cubes[join.target], (head,)
    else:
        return None, (
            f"`{{{hole.text}}}` names '{head}', which is neither a member of "
            f"'{current.name}' nor a cube in the configured files"
        )

    while rest:
        segment = rest.pop(0)
        if not rest and cube.member(segment) is not None:
            return Resolved(
                kind="member", cube=cube.name, member=segment, via=via
            ), None
        join = cube.join_to(segment)
        if join is None or join.target not in cubes:
            return None, (
                f"`{{{hole.text}}}`: '{cube.name}' declares no join to '{segment}', "
                "and Cube walks a reference path through declared joins only"
            )
        cube, via = cubes[join.target], (*via, segment)
    return Resolved(kind="table", cube=cube.name, via=via), None
