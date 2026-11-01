"""A model's output column names, read from its SQL, through its stars.

The dbt style guide ends every model in ``select * from final``, so a reader that
stops at the outermost SELECT knows nothing about the model. This follows a star
to whatever it mirrors: a CTE, a subquery, the two sides of a join, or a relation
the caller can name the columns of (``relation_columns``).

A static read of a template, not a render. Where the columns cannot be known, the
answer is ``columns=None`` and a ``reason`` saying which case it was, because a
caller told "could not resolve" has nothing to act on and one told "no cached
profile of stg_orders" does. Nothing here opens a connection or reads the cache.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import DexError


@dataclass(frozen=True)
class RelationRef:
    """A relation a star reads: ``kind`` is ``ref``, ``source`` or ``table``.

    ``parts`` are the literal arguments of the ``ref()`` or ``source()`` call,
    or the dotted name of a table the SQL spells out.
    """

    kind: str
    parts: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.parts[-1]

    def __str__(self) -> str:
        if self.kind == "table":
            return ".".join(self.parts)
        quoted = ", ".join(f"'{part}'" for part in self.parts)
        return f"{self.kind}({quoted})"


@dataclass
class RelationColumns:
    """A catalog's answer: the columns, or why not, and any profile it read."""

    columns: list[str] | None = None
    reason: str | None = None
    read_from: list[str] = field(default_factory=list)


RelationColumnsReader = Callable[[RelationRef], RelationColumns]


@dataclass
class SelectList:
    """``columns`` lowercased, or ``None`` with the ``reason``. ``read_from`` names
    each cached profile the answer rests on, since a profile can be stale."""

    columns: set[str] | None = None
    reason: str | None = None
    read_from: list[str] = field(default_factory=list)


class _UnresolvedError(DexError):
    """Internal: why a star could not be followed. Becomes ``SelectList.reason``."""


def resolve_select_list(
    sql: str,
    *,
    dialect: str | None = None,
    relation_columns: RelationColumnsReader | None = None,
) -> SelectList:
    """The output columns of ``sql``'s outermost SELECT, or why they are unknown.

    ``relation_columns`` is what a star over a relation asks. Without one, such a
    star is unresolved and the read is pure SQL.
    """

    import sqlglot

    from .validate import strip_jinja

    text, relations = _tokenize_relations(sql)
    stripped = strip_jinja(text)
    if not stripped:
        return SelectList(reason="the model holds no SQL dex can read")
    try:
        parsed = sqlglot.parse_one(stripped, read=dialect or "duckdb")
    except Exception:
        return SelectList(reason="the SQL did not parse with its jinja removed")

    reader = _Reader(relations, relation_columns)
    try:
        names = reader.outputs(parsed, {}, frozenset())
    except _UnresolvedError as exc:
        return SelectList(reason=str(exc), read_from=reader.read_from)
    if not names:
        return SelectList(reason="the SELECT list is empty", read_from=reader.read_from)
    return SelectList(columns=names, read_from=reader.read_from)


def _tokenize_relations(sql: str) -> tuple[str, dict[str, RelationRef]]:
    """``sql`` with each literal ``ref()``/``source()`` a plain identifier, so a
    star over one can still say which relation it reads once jinja is gone."""

    from ..dbt_project import jinja_regions

    regions, text = jinja_regions(sql)
    relations: dict[str, RelationRef] = {}
    pieces: list[str] = []
    cursor = 0
    for region in regions:
        call = next((c for c in region.calls if c.spans_region), None)
        if (
            call is None
            or call.callee not in {"ref", "source"}
            or not call.args
            or None in call.args
        ):
            continue
        reference = RelationRef(call.callee, tuple(call.args))
        token = next((t for t, r in relations.items() if r == reference), None)
        if token is None:
            token = f"dex_relation_{len(relations)}"
            relations[token] = reference
        pieces.extend((text[cursor : region.start], token))
        cursor = region.end
    pieces.append(text[cursor:])
    return "".join(pieces), relations


class _Reader:
    def __init__(
        self,
        relations: dict[str, RelationRef],
        relation_columns: RelationColumnsReader | None,
    ) -> None:
        self.relations = relations
        self.relation_columns = relation_columns
        self.read_from: list[str] = []

    def outputs(
        self, node: Any, ctes: dict[str, Any], seen: frozenset[str]
    ) -> set[str]:
        from sqlglot import expressions as exp

        from .validate import _PLACEHOLDER

        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select):
            raise _UnresolvedError(
                "a set operation, whose branches each project their own columns"
                if isinstance(node, exp.SetOperation)
                else "the statement is not a plain SELECT"
            )
        ctes = {**ctes, **_local_ctes(node)}

        names: set[str] = set()
        for projection in node.expressions:
            star, qualifier = _star_of(projection)
            if star is not None:
                names |= self._expand(node, star, qualifier, ctes, seen)
                continue
            # Only an alias or a bare column states a name. sqlglot's `.name` on
            # `max(a)` is `a` and on `count(*)` is `*`; the warehouse says otherwise.
            name = projection.alias or (
                projection.name if isinstance(projection, exp.Column) else ""
            )
            if _PLACEHOLDER in name:
                raise _UnresolvedError("a macro standing in for a column")
            if not name:
                raise _UnresolvedError(
                    "an unaliased expression, named by the warehouse"
                )
            names.add(name.lower())
        return names

    def _expand(
        self,
        select: Any,
        star: Any,
        qualifier: str,
        ctes: dict[str, Any],
        seen: frozenset[str],
    ) -> set[str]:
        if star.args.get("rename"):
            raise _UnresolvedError("a `* RENAME`")
        sources = self._sources(select)
        if qualifier:
            sources = [s for s in sources if self._alias_of(s) == qualifier]
            if not sources:
                raise _UnresolvedError(f"a `{qualifier}.*` naming nothing in its FROM")
        if not sources:
            raise _UnresolvedError("a `*` with no FROM clause")

        names: set[str] = set()
        for source in sources:
            names |= self._columns_of(source, ctes, seen)
        excluded = star.args.get("except_") or star.args.get("except") or []
        return names - {column.name.lower() for column in excluded}

    def _sources(self, select: Any) -> list[Any]:
        """What a star over ``select`` reads. A semi or anti join only filters."""

        from .. import sql_shape

        if select.args.get("laterals"):
            raise _UnresolvedError(
                "a `*` over a LATERAL, whose columns depend on its body"
            )
        driving = sql_shape.from_relation(select)
        sources = [driving] if driving is not None else []
        sources.extend(
            join.this
            for join in select.args.get("joins") or []
            if str(join.args.get("kind") or "").upper() not in {"SEMI", "ANTI"}
        )
        return sources

    def _alias_of(self, source: Any) -> str:
        from sqlglot import expressions as exp

        if (
            isinstance(source, exp.Table)
            and not source.alias
            and source.name in self.relations
        ):
            return self.relations[source.name].name.lower()
        return source.alias_or_name.lower()

    def _columns_of(
        self, source: Any, ctes: dict[str, Any], seen: frozenset[str]
    ) -> set[str]:
        from sqlglot import expressions as exp

        from .validate import _PLACEHOLDER

        if isinstance(source, exp.Subquery):
            if source.args.get("pivots"):
                raise _UnresolvedError("a `*` over a PIVOT")
            return self.outputs(source.this, ctes, seen)
        if not isinstance(source, exp.Table) or not source.name:
            raise _UnresolvedError(
                "a `*` over a table function or VALUES, whose columns the SQL "
                "does not name"
            )
        if source.args.get("pivots"):
            raise _UnresolvedError("a `*` over a PIVOT")
        name = source.name
        if _PLACEHOLDER in name:
            raise _UnresolvedError("a `*` over a macro standing in for a relation")

        unqualified = not source.db and not source.catalog
        if unqualified and name in self.relations:
            return self._catalog(self.relations[name])
        lowered = name.lower()
        if unqualified and lowered in ctes:
            if lowered in seen:
                raise _UnresolvedError(f"a recursive CTE ({name})")
            return self.outputs(ctes[lowered], ctes, seen | {lowered})
        parts = tuple(p for p in (source.catalog, source.db, name) if p)
        return self._catalog(RelationRef("table", parts))

    def _catalog(self, relation: RelationRef) -> set[str]:
        if self.relation_columns is None:
            raise _UnresolvedError(f"a `*` over {relation}")
        answer = self.relation_columns(relation)
        self.read_from.extend(
            place for place in answer.read_from if place not in self.read_from
        )
        if answer.columns is None:
            raise _UnresolvedError(answer.reason or f"a `*` over {relation}")
        return {column.lower() for column in answer.columns}


def _local_ctes(select: Any) -> dict[str, Any]:
    with_clause = select.args.get("with_") or select.args.get("with")
    if with_clause is None:
        return {}
    return {cte.alias.lower(): cte.this for cte in with_clause.expressions}


def _star_of(projection: Any) -> tuple[Any, str]:
    """The star a projection is, and its qualifier: ``(None, "")`` if neither."""

    from sqlglot import expressions as exp

    if isinstance(projection, exp.Star):
        return projection, ""
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return projection.this, projection.table.lower()
    return None, ""
