"""SELECT-only enforcement: defense in depth on top of the read-only connection.

The read-only adapter connection already makes writes impossible at the engine.
This guard is the second layer: it parses generated SQL and refuses anything that
is not a single read-only SELECT, so a bug in SQL generation cannot turn into a
write or a multi-statement injection even on a connection that happened to be
writable. Every statement the explore engine sends routes through here.
"""

# TODO: should we use scoped read-only roles instead of SQL semantics inspection?

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp


class NotSelectOnlyError(Exception):
    """Raised when SQL is not a single read-only SELECT statement."""


def _read_only_roots() -> tuple[type, ...]:
    # SELECT covers aggregates/profiling; WITH wraps a SELECT; a set operation
    # (UNION/INTERSECT/EXCEPT) of SELECTs is read-only; Subquery wraps one;
    # DESCRIBE/PRAGMA are read-only introspection. Built tolerant of sqlglot
    # version differences in how set operations are classed.
    candidates = [
        exp.Select,
        exp.With,
        exp.Subquery,
        exp.Describe,
        exp.Pragma,
        getattr(exp, "SetOperation", None),
        getattr(exp, "Union", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "Except", None),
    ]
    return tuple(c for c in candidates if isinstance(c, type))


# Statement types that write or otherwise mutate; their presence anywhere in the
# parse tree fails the guard, which also catches a write smuggled into a CTE.
_FORBIDDEN = tuple(
    c
    for c in (
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Drop", None),
        getattr(exp, "Create", None),
        getattr(exp, "Alter", None),
        getattr(exp, "AlterTable", None),
        getattr(exp, "Merge", None),
        getattr(exp, "TruncateTable", None),
        getattr(exp, "Command", None),
    )
    if isinstance(c, type)
)

_ALLOWED_ROOTS = _read_only_roots()


def assert_select_only(sql: str, *, dialect: str = "duckdb") -> str:
    """Return ``sql`` unchanged if it is a single read-only query; else raise.

    Multi-statement input (``;``-chained) is refused outright: profiling never
    needs more than one statement, and allowing several is how a DDL/DML slips in.
    A read or write node anywhere in the tree (not just at the root) fails it.
    """

    statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    if len(statements) != 1:
        raise NotSelectOnlyError(
            f"expected exactly one statement, parsed {len(statements)}"
        )

    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise NotSelectOnlyError(
            f"only read-only SELECT statements are allowed, got {type(root).__name__}"
        )
    if _FORBIDDEN:
        forbidden = next(root.find_all(*_FORBIDDEN), None)
        if forbidden is not None:
            raise NotSelectOnlyError(
                f"write/DDL statement is not allowed: {type(forbidden).__name__}"
            )
    return sql
