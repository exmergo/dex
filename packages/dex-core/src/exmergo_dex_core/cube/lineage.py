"""From a Cube expression to the physical columns and relation behind it.

The one module in the Cube reader that imports sqlglot, imported only past
:func:`.dependency.ensure_available`. Three questions, each answered
conservatively, because a column guessed out of an expression makes the PII gate
screen the wrong column and call it evidence:

- **Which column is behind a member.** ``{CUBE}.amount``, ``amount`` and a
  reference to a member that itself is one column, each resolve to that column.
  Anything else, an expression, a quoted name, a reference to another cube or a
  per-query value, resolves to nothing.
- **Which relation a cube reads.** ``sql_table`` is the relation, read the way
  the active connector folds identifiers. A ``sql`` cube has one only when it is
  ``SELECT <columns or *> FROM <one table>`` and nothing else, and its columns
  are then traced through the projection; a filtered or joined query has none,
  and a single-table one with a filter keeps its table for the PII gate alone.
- **Which columns a join pairs.** An equality between a column of each side, or
  a conjunction of them. Anything else is a join with no declared pairs.

It also writes the one thing dex puts into Cube's SQL: a parameter as a string
literal of the connector's dialect (:func:`string_literal`), quoted by that
dialect's own escaping rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..adapters import normalize_relation
from .model import Cube, Join, Member
from .references import Resolved, resolve, scan

__all__ = [
    "CubeRelation",
    "Reads",
    "columns_read",
    "cube_relation",
    "join_pairs",
    "member_column",
    "string_literal",
]

# Placeholders substituted for the two sides' references before parsing. Not
# valid in any dialect's identifiers a model would use, so no collision.
_SELF = "__dex_cube_self__"
_OTHER = "__dex_cube_other__"

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED = re.compile(rf"^\s*(?:{_SELF}\s*\.\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass(frozen=True)
class CubeRelation:
    """The relation a cube reads, as the catalog may claim it.

    ``relation`` is a fully qualified name the connector resolves, or None.
    ``gate_relation`` is what the PII gate may match against the profiled cache
    by suffix, which can be a partial name the catalog declines to claim.
    ``projection`` maps each output column of a ``sql`` cube to the source
    column it is, ``None`` for a ``sql_table`` cube (whose columns are its own),
    and ``"*"`` survives as an identity entry. ``note`` says why there is less
    than a relation.
    """

    relation: str | None = None
    gate_relation: str | None = None
    projection: dict[str, str] | None = field(default=None)
    star: bool = False
    note: str | None = None

    def source_column(self, column: str) -> str | None:
        """The physical column behind an output column of the cube."""

        if self.projection is None:
            return column
        if column in self.projection:
            return self.projection[column]
        return column if self.star else None


def _dialect(connector: str | None) -> str | None:
    """sqlglot's dialect for a dex connector. Every dex connector name is one."""

    name = (connector or "").strip().lower()
    return name or None


def cube_relation(cube: Cube, connector: str | None) -> CubeRelation:
    if cube.sql_table is not None:
        return _table_relation(cube, cube.sql_table, connector)
    return _query_relation(cube, cube.sql or "", connector)


def _table_relation(cube: Cube, table: str, connector: str | None) -> CubeRelation:
    if "{" in table or "\\" in table:
        return CubeRelation(
            note=(
                f"cube '{cube.name}' reads sql_table '{table}', which carries a "
                "reference Cube resolves at compile time, so it carries no "
                "physical relation"
            )
        )
    table = table.strip()
    relation = normalize_relation(connector, table) if connector else None
    if relation is not None:
        return CubeRelation(relation=relation, gate_relation=relation)
    gate = table if all(_BARE.fullmatch(p) for p in table.split(".")) else None
    return CubeRelation(
        gate_relation=gate,
        note=(
            f"cube '{cube.name}' reads sql_table '{table}', which dex cannot read "
            f"as one fully qualified relation on {connector or 'this connector'}, "
            "so it carries no physical link. Cube completes a partial name from "
            "its data source; the PII gate still matches an unquoted partial name "
            "against the profiled cache by suffix"
        ),
    )


def _query_relation(cube: Cube, sql: str, connector: str | None) -> CubeRelation:
    holes, problem = scan(sql)
    if problem or holes:
        return CubeRelation(
            note=(
                f"cube '{cube.name}' is defined by a sql query that carries "
                "references Cube resolves per query, so it carries no physical "
                "relation"
            )
        )
    unread = CubeRelation(
        note=(
            f"cube '{cube.name}' is defined by a sql query rather than a table, "
            "so it carries no physical relation. A query read as a relation would "
            "reach the PII gate as false evidence"
        )
    )
    try:
        parsed = sqlglot.parse_one(_unescaped(sql), read=_dialect(connector))
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        return unread
    if not isinstance(parsed, exp.Select):
        return unread
    tables = list(parsed.find_all(exp.Table))
    source = parsed.args.get("from_") or parsed.args.get("from")
    if (
        len(tables) != 1
        or source is None
        or not isinstance(source.this, exp.Table)
        or parsed.args.get("joins")
        or parsed.find(exp.Subquery)
    ):
        return unread
    table = tables[0]
    name = ".".join(part.name for part in table.parts)
    relation = normalize_relation(connector, name) if connector else None

    projection: dict[str, str] = {}
    star = False
    for item in parsed.expressions:
        if isinstance(item, exp.Star):
            star = True
            continue
        target = item.this if isinstance(item, exp.Alias) else item
        if isinstance(target, exp.Column) and isinstance(target.this, exp.Star):
            star = True
            continue
        if not isinstance(target, exp.Column) or target.this.quoted:
            continue
        output = item.alias_or_name
        if output:
            projection[output] = target.name

    narrowed = [
        key
        for key in ("where", "group", "having", "qualify", "limit", "distinct", "order")
        if parsed.args.get(key)
    ]
    if narrowed or relation is None:
        why = (
            f"narrows its table ({', '.join(narrowed)})"
            if narrowed
            else f"reads '{name}', which dex cannot read as one fully qualified "
            f"relation on {connector or 'this connector'}"
        )
        return CubeRelation(
            gate_relation=relation or name,
            projection=projection,
            star=star,
            note=(
                f"cube '{cube.name}' is defined by a sql query that {why}, so the "
                "catalog claims no relation for it: its rows are not the table's. "
                "The PII gate still screens its columns against that table"
            ),
        )
    return CubeRelation(
        relation=relation, gate_relation=relation, projection=projection, star=star
    )


def _unescaped(sql: str) -> str:
    """Cube's ``\\{`` and ``\\}`` as the literal braces they stand for."""

    return sql.replace("\\{", "{").replace("\\}", "}")


def member_column(
    cube: Cube,
    member: Member,
    cubes: Mapping[str, Cube],
    relation: CubeRelation,
    _visiting: frozenset[str] = frozenset(),
) -> str | None:
    """The one physical column behind ``member``, or None.

    A reference to another member of the same cube is followed (Cube allows a
    dimension to be defined as another), with a guard against a cycle, which
    Cube itself refuses.
    """

    if member.sql is None or member.case or member.name in _visiting:
        return None
    holes, problem = scan(member.sql)
    if problem:
        return None
    visiting = _visiting | {member.name}
    text = member.sql
    for hole in reversed(holes):
        resolved, _ = resolve(hole, cube, cubes)
        if resolved is None or resolved.via or resolved.cube != cube.name:
            return None
        if resolved.kind == "table":
            replacement = _SELF
        elif resolved.kind == "member":
            inner = cube.member(resolved.member or "")
            column = (
                member_column(cube, inner, cubes, relation, visiting)
                if inner is not None
                else None
            )
            if column is None:
                return None
            # The inner column is already physical; keep it as an output
            # column of this cube so the projection is applied once.
            replacement = f"{_SELF}.{_output_for(relation, column)}"
        else:
            return None
        text = text[: hole.start] + replacement + text[hole.end :]
    found = _QUALIFIED.match(text)
    if found is None:
        return None
    return relation.source_column(found.group(1))


@dataclass(frozen=True)
class Reads:
    """What one expression reads: its cube's physical columns, and the members
    it references, in its own cube or reached through a join."""

    columns: frozenset[str] = frozenset()
    members: tuple[Resolved, ...] = ()


def columns_read(
    sql: str | None,
    cube: Cube,
    cubes: Mapping[str, Cube],
    relation: CubeRelation,
    connector: str | None,
) -> Reads | None:
    """Every physical column of ``cube``'s relation ``sql`` reads, or None.

    The schema axis asks this of every expression a cube holds (a computed
    dimension, a measure's filter, a segment), because a column dropped from
    the table breaks any of them, not only a member that is one column. None
    where dex cannot tell: a per-query value, a reference into another cube's
    table, a quoted or qualified name, or SQL that does not parse. Nothing
    there is guessed, so the caller names it as unchecked.
    """

    if sql is None:
        return Reads()
    holes, problem = scan(sql)
    if problem:
        return None
    members: list[Resolved] = []
    text = sql
    for hole in reversed(holes):
        resolved, _ = resolve(hole, cube, cubes)
        if resolved is None or resolved.kind == "context":
            return None
        if resolved.kind == "table":
            if resolved.via or resolved.cube != cube.name:
                return None
            replacement = _SELF
        else:
            members.append(resolved)
            # A member is a value here; what it reads is its own question.
            replacement = "NULL"
        text = text[: hole.start] + replacement + text[hole.end :]
    try:
        parsed = sqlglot.parse_one(_unescaped(text), read=_dialect(connector))
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        return None
    columns: set[str] = set()
    for column in parsed.find_all(exp.Column):
        if column.this.quoted or column.table not in ("", _SELF) or column.db:
            return None
        physical = relation.source_column(column.name)
        if physical is None:
            return None
        columns.add(physical)
    return Reads(columns=frozenset(columns), members=tuple(reversed(members)))


def _output_for(relation: CubeRelation, column: str) -> str:
    """The output name a physical column has on a cube, inverting the projection."""

    if relation.projection is None:
        return column
    for output, source in relation.projection.items():
        if source == column:
            return output
    return column


def join_pairs(
    cube: Cube,
    join: Join,
    cubes: Mapping[str, Cube],
    relations: Mapping[str, CubeRelation],
    connector: str | None,
) -> tuple[list[tuple[str, str]] | None, str | None]:
    """The ordered ``(this cube's column, target's column)`` pairs a join keys on.

    None with a note where the condition is not a conjunction of equalities
    between a column of each side, because half a composite join, or a range
    join read as an equality, is a join nobody declared.
    """

    target = cubes.get(join.target)
    if target is None:
        return None, None
    label = f"join {cube.name} -> {join.reached_as}"
    holes, problem = scan(join.sql)
    if problem:
        return None, f"{label}: its sql has {problem}"
    text = join.sql
    for hole in reversed(holes):
        side = _side(hole, cube, target, join, cubes, relations)
        if side is None:
            return None, (
                f"{label} keys on `{{{hole.text}}}`, which is not a column of "
                "either side, so it is not declared as an edge"
            )
        text = text[: hole.start] + side + text[hole.end :]
    try:
        condition = sqlglot.parse_one(_unescaped(text), read=_dialect(connector))
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        return None, f"{label}: dex could not read its sql as a join condition"

    terms = list(condition.flatten()) if isinstance(condition, exp.And) else [condition]
    pairs: list[tuple[str, str]] = []
    for term in terms:
        pair = _equality(term)
        if pair is None:
            return None, (
                f"{label} is not an equality between a column of each side "
                f"(`{join.sql}`), so it is declared as a join with no column pairs"
            )
        # Raw column references on either side name a cube's output columns,
        # which a `sql` cube's projection maps to the table's; member references
        # were substituted as output names too, so the projection applies once.
        frm = relations[cube.name].source_column(pair[0])
        to = relations[target.name].source_column(pair[1])
        if frm is None or to is None:
            return None, (
                f"{label} keys on a column its cube's sql does not project from a "
                "table, so it is not declared as an edge"
            )
        pairs.append((frm, to))
    return pairs, None


def _side(hole, cube, target, join, cubes, relations) -> str | None:
    """A join reference as `side.physical_column`, or `side` for a table."""

    head = (hole.path or ("",))[0]
    # `{target}` / `{alias}` and `{target.member}` name the joined side, written
    # by the alias where the join declares one.
    if (
        hole.path
        and head in {join.reached_as, join.target}
        and head not in {"CUBE", "TABLE"}
    ):
        rest = hole.path[1:]
        if not rest:
            return _OTHER
        if len(rest) == 1 and (member := target.member(rest[0])) is not None:
            relation = relations[target.name]
            column = member_column(target, member, cubes, relation)
            return f"{_OTHER}.{_output_for(relation, column)}" if column else None
        return None
    resolved, _ = resolve(hole, cube, cubes)
    if resolved is None or resolved.via or resolved.cube != cube.name:
        return None
    if resolved.kind == "table":
        return _SELF
    member = cube.member(resolved.member or "")
    if member is None:
        return None
    relation = relations[cube.name]
    column = member_column(cube, member, cubes, relation)
    return f"{_SELF}.{_output_for(relation, column)}" if column else None


def _equality(term: exp.Expression) -> tuple[str, str] | None:
    if isinstance(term, exp.Paren):
        term = term.this
    if not isinstance(term, exp.EQ):
        return None
    sides = {}
    for column in (term.left, term.right):
        if not isinstance(column, exp.Column) or column.this.quoted:
            return None
        sides[column.table] = column.name
    if set(sides) != {_SELF, _OTHER}:
        return None
    return sides[_SELF], sides[_OTHER]


def string_literal(value: str, connector: str) -> str:
    """``value`` as a string literal of ``connector``'s dialect.

    The dialect decides the escaping: a doubled quote on Postgres and DuckDB, a
    backslash on BigQuery, Snowflake and Redshift, which also escape the
    backslash itself. Hand-rolled quoting would get one of them wrong.
    """

    return exp.Literal.string(value).sql(dialect=_dialect(connector))
