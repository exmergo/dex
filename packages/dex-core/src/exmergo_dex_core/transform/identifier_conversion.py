# ruff: noqa: S608 -- every statement is built from dex's own aliases and SQL
# sqlglot rendered from the parsed model, then parsed again before it runs.
"""What an identifier conversion does to the entities a model holds (#861).

A repair can make a model compile by removing entities. ``TRY_CAST(k AS INT)
IS NOT NULL`` drops every identifier of a second, legitimate scheme
(``org_cedar``) as surely as it drops a malformed one, and a cast can merge two
distinct keys (``7`` and ``007``) into one. The build is green, the attributes
of the surviving rows are right, and the row count can even come out unchanged,
so nothing that checks rows notices.

This module names each conversion a plan adds to an identifier column and, where
counting is on, measures it with aggregate statements only: how many distinct
identifiers reach the conversion, how many fail it, how many collapse into one
key, how many change representation, how many still find their partner across a
join, and, on an edited model, which identities the prior model held that the
authored one does not. Every count states its denominator. Nothing here decides
that a differently shaped identifier is valid, malformed or a duplicate: lexical
shape cannot settle that, and the counts are reported against what the model's
declaration records instead. A loss or a merge that an assumption naming the
column records is ``declared``; one nothing records is ``undeclared``; one dex
could not measure is ``unchecked`` with its reason, never a guessed count.

Gated exactly like row attribution, and run inside its batch: static detection
always runs and opens nothing; the counts run where counting is free and on
request elsewhere, priced in the same single handshake. No statement projects a
value, so a PII-flagged identifier can be measured, and a failed statement
reports its exception type, never the warehouse's words, which for a failed cast
name the value that would not convert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

import sqlglot
from pydantic import BaseModel
from sqlglot import expressions as exp

from .. import sql_shape
from ..adapters import get_dialect
from ..cache import DexCache, match_identifier
from ..dbt_project import EditOp
from ..storage import readable_cache
from .plans import EditKind
from .row_attribution import (
    ExtraProbe,
    UnattributableError,
    cache_resolver,
    count_sql,
    counting_decision,
    naming_resolver,
    render_model_sql,
)

if TYPE_CHECKING:
    from ..engine import DexEngine
    from .plans import PlanEdit

#: The per-table key the estimate itemizes these statements under.
LABEL = "(identifier conversions)"
MAX_PER_MODEL = 4
MAX_PER_PLAN = 8

Site = Literal["filter", "join", "projection"]
Status = Literal["lossless", "declared", "undeclared", "unchecked"]

_KEY_ROLES = {"key", "foreign_key"}
_COUNT_FIELDS = (
    "input_rows",
    "null_key_rows",
    "input_entities",
    "failed_rows",
    "failed_entities",
    "retained_entities",
    "converted_entities",
    "representation_changed_entities",
    "collided_keys",
    "colliding_source_entities",
    "matched_entities",
)


class ConversionCounts(BaseModel):
    """Aggregate counts over the rows that reach one conversion.

    ``*_rows`` count rows and ``*_entities`` count distinct non-NULL source
    identifiers, so a duplicated key is one entity however many rows carry it.
    ``null_key_rows`` are rows whose identifier was already NULL: they are not
    conversion failures. ``failed_*`` are non-NULL identifiers the conversion
    turns into NULL (a hard cast is measured as its safe form, so these are the
    values that would fail the authored statement). ``converted_entities`` is
    the number of distinct keys the conversion produces; ``collided_keys`` of
    those were produced from more than one source identifier, together
    ``colliding_source_entities`` of them. ``representation_changed_entities``
    are retained identifiers whose converted text differs from their source text
    (``007`` read back as ``7``). ``matched_entities`` counts source identifiers
    whose converted key the join's other side holds, on a join only.
    """

    input_rows: int | None = None
    null_key_rows: int | None = None
    input_entities: int | None = None
    failed_rows: int | None = None
    failed_entities: int | None = None
    retained_entities: int | None = None
    converted_entities: int | None = None
    representation_changed_entities: int | None = None
    collided_keys: int | None = None
    colliding_source_entities: int | None = None
    matched_entities: int | None = None


class IdentityChange(BaseModel):
    """The prior and the authored model's identities in one output column,
    compared as text, distinct and NULL-free. ``identity_changed`` holds even
    when the two row counts are equal: that is the case it exists for."""

    column: str
    prior_rows: int | None = None
    authored_rows: int | None = None
    prior_entities: int | None = None
    authored_entities: int | None = None
    kept: int | None = None
    lost: int | None = None
    gained: int | None = None
    identity_changed: bool | None = None


class IdentifierConversion(BaseModel):
    """One conversion of an identifier column, and what it does to entities."""

    model: str
    scope: str
    site: Site
    column: str
    output_column: str | None = None
    steps: list[str]
    authored: str
    key_basis: str
    status: Status
    reason_code: str | None = None
    reason: str | None = None
    denominator: str | None = None
    partner: str | None = None
    measured: ConversionCounts | None = None
    identity: IdentityChange | None = None
    identity_reason_code: str | None = None
    identity_reason: str | None = None
    assumption: dict[str, Any] | None = None
    evidence: list[str] = []


# --- detection -----------------------------------------------------------------


@dataclass
class _Found:
    """A conversion located in one rendered tree.

    ``node`` is the outermost conversion expression, ``column`` the identifier
    it converts and ``select`` the scope it sits in, all nodes of that tree, so
    a statement built from a copy can find the same conversion by index.
    """

    scope: str
    select: exp.Select
    site: Site
    node: exp.Expression
    column: exp.Column
    steps: tuple[sql_shape.KeyStep, ...]
    output: str | None = None
    partner: exp.Expression | None = None
    unsupported: bool = False
    #: Why an unsupported entry cannot be measured: a rewrite dex does not
    #: measure, or a scope it cannot align (a UNION, a subquery).
    code: str = "unsupported_expression"

    def signature(self) -> tuple[str, str, str]:
        return (self.scope, self.site, sql_shape.match_key(self.node))


def _site(node: exp.Expression, select: exp.Select) -> tuple[Site | None, Any]:
    """Which clause of ``select`` holds ``node``: the WHERE, a join's ON, or a
    projection, with the projection or ON node; anything else is no site."""

    child = node
    parent = node.parent
    while parent is not None and parent is not select:
        if isinstance(parent, (exp.Select, exp.Subquery)) and parent is not select:
            return None, None
        child, parent = parent, parent.parent
    if parent is None:
        return None, None
    key = child.arg_key
    if key == "where":
        return "filter", None
    if key == "joins":
        on = child.args.get("on")
        if on is not None and _within(node, on):
            return "join", on
        return None, None
    if key == "expressions":
        return "projection", child
    return None, None


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    """Whether ``node`` sits at or below ``ancestor`` in the parse tree."""
    current: exp.Expression | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _outermost(node: exp.Expression) -> bool:
    """Whether ``node`` is the outermost step of a conversion chain, parentheses aside,
    so a chain is reported once.
    """
    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return parent is None or sql_shape.key_step(parent) is None


def _partner(node: exp.Expression) -> exp.Expression | None:
    """The other side of the equality a join conversion sits on."""

    current = node
    while isinstance(current.parent, exp.Paren):
        current = current.parent
    parent = current.parent
    if isinstance(parent, exp.EQ):
        return parent.expression if parent.this is current else parent.this
    return None


def find_conversions(tree: exp.Expression) -> list[_Found]:
    """Every conversion applied to a column in a scope dex can align, in a
    stable order, plus each rewrite of a single column it does not recognize.

    Only the outermost step of a chain is reported, so ``CAST(LOWER(k) AS INT)``
    is one conversion with two steps rather than two conversions.
    """

    found: list[_Found] = []
    for scope, body in sql_shape.scopes(tree).items():
        selects = [body] if isinstance(body, exp.Select) else []
        for select in selects:
            for node in list(select.walk()):
                if not isinstance(node, exp.Expression) or isinstance(node, exp.Column):
                    continue
                step = sql_shape.key_step(node)
                if step is None or not _outermost(node):
                    continue
                site, holder = _site(node, select)
                if site is None:
                    continue
                peeled = sql_shape.peel_key_conversion(node)
                if peeled is None:
                    continue
                column, steps = peeled
                output = None
                if site == "projection" and isinstance(holder, exp.Alias):
                    output = holder.alias
                elif site == "projection":
                    output = column.name
                found.append(
                    _Found(
                        scope,
                        select,
                        site,
                        node,
                        column,
                        steps,
                        output=output,
                        partner=_partner(node) if site == "join" else None,
                    )
                )
            found.extend(_unsupported(scope, select, found))
    found.extend(_unaligned(tree, found))
    return found


#: The scope name an unaligned conversion is reported under.
UNALIGNED = "(unaligned)"


def _unaligned(tree: exp.Expression, known: list[_Found]) -> list[_Found]:
    """Conversions in a place dex does not align across two versions of a model
    (a UNION branch, a subquery, a nested CTE), named rather than dropped, so a
    union of two identifier schemes never reads as having nothing to check."""

    seen = {id(f.node) for f in known}
    out: list[_Found] = []
    for node in list(tree.walk()):
        if not isinstance(node, exp.Expression) or id(node) in seen:
            continue
        if sql_shape.key_step(node) is None or not _outermost(node):
            continue
        peeled = sql_shape.peel_key_conversion(node)
        if peeled is None:
            continue
        select = node.find_ancestor(exp.Select)
        if select is None:
            continue
        site, holder = _site(node, select)
        output = holder.alias if isinstance(holder, exp.Alias) else None
        out.append(
            _Found(
                UNALIGNED,
                select,
                site or "projection",
                node,
                peeled[0],
                peeled[1],
                output=output,
                unsupported=True,
                code="unsupported_scope",
            )
        )
    return out


def _holders(select: exp.Select) -> list[tuple[Site, exp.Expression, str | None]]:
    """Every expression in ``select`` an identifier rewrite can sit in: each
    projection, each WHERE conjunct, and each side of each ON equality."""

    out: list[tuple[Site, exp.Expression, str | None]] = [
        ("projection", projection.this, projection.alias)
        for projection in select.expressions
        if isinstance(projection, exp.Alias)
    ]
    out.extend(("filter", p, None) for p in sql_shape.predicates(select, "where"))
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        for eq in [] if on is None else list(on.find_all(exp.EQ)):
            out.extend(("join", side, None) for side in (eq.this, eq.expression))
    return out


def _unsupported(scope: str, select: exp.Select, known: list[_Found]) -> list[_Found]:
    """Each identifier rewritten by a function dex does not measure
    (``regexp_replace``, ``coalesce``, a regex filter), once per column and
    site: reported so it reads as unchecked rather than as nothing to check."""

    seen = {
        (f.site, sql_shape.match_key(f.column)) for f in known if f.select is select
    }
    out: list[_Found] = []
    for site, holder, alias in _holders(select):
        for column in holder.find_all(exp.Column):
            outer: exp.Expression | None = None
            aggregated = False
            current = column.parent
            while current is not None:
                if isinstance(current, (exp.AggFunc, exp.Window)):
                    # Counting or ranking an identifier is not rewriting it.
                    aggregated = True
                    break
                if (
                    isinstance(current, exp.Func)
                    and sql_shape.key_step(current) is None
                ):
                    outer = current
                if current is holder:
                    break
                current = current.parent
            if aggregated:
                continue
            key = (site, sql_shape.match_key(column))
            if outer is None or key in seen:
                continue
            seen.add(key)
            out.append(
                _Found(
                    scope,
                    select,
                    site,
                    outer,
                    column,
                    (),
                    output=alias,
                    unsupported=True,
                )
            )
    return out


# --- which columns are identifiers -------------------------------------------


def _declared_keys(declared: dict[str, Any]) -> set[str]:
    """The lowercased columns a declaration names as keys: its grain, key and reference
    roles, and the parent population key.
    """
    keys = {str(c).lower() for c in declared.get("grain") or []}
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, dict):
            continue
        if fields.get("role") in _KEY_ROLES or fields.get("references"):
            keys.add(str(name).lower())
    parent = (declared.get("population") or {}).get("parent") or {}
    keys.update(str(c).lower() for c in (parent.get("key") or {}))
    return keys


def _named_like_key(name: str) -> bool:
    """Whether a column's name reads like an identifier (``id``, ``*_id``, ``*_key``,
    ``*_code``, ``*_sku``).
    """
    lowered = name.lower()
    return lowered == "id" or lowered.endswith(("_id", "_key", "_code", "_sku"))


def _relation_of(select: exp.Select, column: exp.Column) -> exp.Table | None:
    """The table a column is read from in ``select``: by its qualifier, or the
    one relation the select reads when it is unqualified."""

    driving = sql_shape.from_relation(select)
    tables = [driving] if isinstance(driving, exp.Table) else []
    tables.extend(
        join.this
        for join in select.args.get("joins") or []
        if isinstance(join.this, exp.Table)
    )
    qualifier = column.table.lower() if column.table else ""
    if qualifier:
        for table in tables:
            if (table.alias_or_name or "").lower() == qualifier:
                return table
        return None
    if len(tables) == 1 and not (select.args.get("joins") or []):
        return tables[0]
    return None


def _dataset_for(cache: DexCache | None, table: exp.Table | None) -> Any:
    """The cached profile of ``table``, or None where the cache has none or more than
    one relation matches.
    """
    if cache is None or table is None:
        return None
    bare = table.copy()
    bare.set("alias", None)
    name = ".".join(
        part.name
        for part in (bare.args.get("catalog"), bare.args.get("db"), bare.this)
        if part is not None
    )
    matches = match_identifier(name, [d.identifier for d in cache.datasets])
    if len(matches) != 1:
        return None
    return next(d for d in cache.datasets if d.identifier == matches[0])


def _key_basis(
    found: _Found, declared_keys: set[str], cache: DexCache | None
) -> str | None:
    """Why the converted column counts as an identifier (``join``, ``declared``,
    ``profiled_key`` or ``name``), or None where nothing says it is one.
    """
    name = found.column.name.lower()
    if found.site == "join":
        return "join"
    if name in declared_keys or (found.output or "").lower() in declared_keys:
        return "declared"
    dataset = _dataset_for(cache, _relation_of(found.select, found.column))
    # The grain the profile settled on, not any unique column: on a small table
    # a measure is unique by accident, and a price is no identifier.
    grain = [str(c).lower() for c in (getattr(dataset, "grain", None) or [])]
    if grain == [name]:
        return "profiled_key"
    if _named_like_key(name) or _named_like_key(found.output or ""):
        return "name"
    return None


# --- the statements ------------------------------------------------------------


def _text_type(dialect: str) -> exp.DataType:
    return exp.DataType.build("text", dialect=dialect)


def _measuring_conv(node: exp.Expression, dialect: str) -> exp.Expression:
    """The conversion as the probe evaluates it: every hard cast made safe, so a
    value the authored statement would reject is counted rather than failing
    the count, and safe in the form ``dialect`` keeps (see
    :func:`~..sql_shape.dialect_safe_cast`). Raises
    :class:`~..sql_shape.NoSafeCastError` where the dialect has none."""

    copy = node.copy()
    for cast in list(copy.find_all(exp.Cast)):
        if isinstance(cast, exp.TryCast) or cast.args.get("safe"):
            continue
        safe = exp.TryCast(this=cast.this, to=cast.args.get("to"))
        if cast is copy:
            copy = safe
        else:
            cast.replace(safe)
    if isinstance(copy, exp.ToNumber) and not copy.args.get("safe"):
        copy.set("safe", True)
    return sql_shape.with_safe_casts(copy, dialect)


_SCOPE_ROWS = "so its rows are not the rows the conversion reads"


def _scope_unsupported(select: exp.Select) -> str | None:
    """Why a scope's rows cannot be counted per identifier (it aggregates, deduplicates
    or windows), or None where they can.
    """
    if select.args.get("group") is not None or select.args.get("distinct"):
        return f"the scope aggregates or deduplicates, {_SCOPE_ROWS}"
    for projection in select.expressions:
        if projection.find(exp.AggFunc) is not None or projection.find(exp.Window):
            return f"the scope aggregates or windows, {_SCOPE_ROWS}"
    return None


def _detach_with(tree: exp.Expression, through: str | None = None) -> list:
    """Take the WITH clause off ``tree``, keeping the CTEs up to and including
    ``through``: a later CTE may read columns the probe has just replaced, and
    the conversion's own scope cannot read a CTE defined after it."""

    key = "with_" if "with_" in tree.args else "with"
    clause = tree.args.get(key)
    if clause is None:
        return []
    tree.set(key, None)
    ctes = list(clause.expressions)
    if through is None:
        return ctes
    names = [cte.alias_or_name.lower() for cte in ctes]
    return ctes[: names.index(through) + 1] if through in names else ctes


def _wrap(ctes: list[exp.Expression], body: str, dialect: str) -> str:
    """``body`` prefixed with the CTEs it reads, rendered in ``dialect``."""
    if not ctes:
        return body
    rendered = ", ".join(cte.sql(dialect=dialect) for cte in ctes)
    return f"WITH {rendered} {body}"


@dataclass
class _Built:
    sql: str | None = None
    code: str | None = None
    reason: str | None = None
    denominator: str | None = None
    partner: str | None = None


def conversion_probe(
    tree: exp.Expression,
    index: int,
    dialect: str,
    labels: tuple[str | None, str | None] = (None, None),
    *,
    expect: tuple[str, str] | None = None,
    text_source: bool = True,
) -> _Built:
    """One aggregate statement measuring the ``index``-th conversion of a
    rendered (cache-resolved) authored tree.

    The rows counted are the rows that reach the conversion: the scope's own
    relations, joins and filters, minus only the predicate the conversion sits
    in, so an intentional filter already in the model stays respected and only
    what this conversion removes is counted. A join conversion is measured over
    the relation it reads, beside the distinct keys of the join's other side.
    ``labels`` name those two relations as the model writes them, for the
    denominator; the physical identifiers are used where they are absent.
    """

    copy = tree.copy()
    found = find_conversions(copy)
    if index >= len(found):  # pragma: no cover - indices come from the same tree
        return _Built(code="probe_failed", reason="the conversion was not found")
    target = found[index]
    if expect is not None and (target.site, sql_shape.match_key(target.node)) != expect:
        # The naming-resolved and the cache-resolved tree differ only in how
        # relations render, so this does not happen; if it ever does, measuring
        # some other conversion would be a wrong answer, and none is better.
        return _Built(code="probe_failed", reason="the conversion was not found")
    select = target.select
    if (blocker := _scope_unsupported(select)) is not None:
        return _Built(code="unsupported_scope", reason=blocker)
    raw = target.column.copy()
    try:
        conv = _measuring_conv(target.node, dialect)
        partner_conv = (
            _measuring_conv(target.partner, dialect)
            if target.partner is not None
            else None
        )
    except sql_shape.NoSafeCastError:
        return _Built(
            code="no_safe_cast_on_connector",
            reason="this connector has no cast that returns NULL instead of "
            "failing, so the values the conversion rejects cannot be counted",
        )
    text = _text_type(dialect)
    partner_sql: str | None = None
    partner_label: str | None = None

    if target.site == "join":
        relation = _relation_of(select, target.column)
        partner = target.partner
        partner_column = (
            partner
            if isinstance(partner, exp.Column)
            else (sql_shape.peel_key_conversion(partner) or (None,))[0]
            if partner is not None
            else None
        )
        partner_relation = (
            _relation_of(select, partner_column) if partner_column is not None else None
        )
        if relation is None or partner is None or partner_relation is None:
            return _Built(
                code="unsupported_scope",
                reason="the relations on both sides of the join were not identified",
            )
        own = _own_side_filters(select, relation, target.column)
        inner = exp.select(
            exp.alias_(raw, "dex_raw"), exp.alias_(conv, "dex_conv")
        ).from_(relation.copy())
        if own:
            inner = inner.where(*own)
        partner_sql = (
            exp.select(exp.alias_(partner_conv, "dex_pk"))
            .distinct()
            .from_(partner_relation.copy())
            .where(
                exp.Not(this=exp.Is(this=partner_conv.copy(), expression=exp.Null()))
            )
            .sql(dialect=dialect)
        )
        partner_label = sql_shape.text(partner)
        own_label = labels[0] or sql_shape.relation_key(relation)
        denominator = (
            f"rows of {own_label} that pass the scope's "
            f"{len(own)} filter(s) on {own_label} alone (a filter converting "
            "this key is left out), against the distinct keys of "
            f"{labels[1] or sql_shape.relation_key(partner_relation)}"
        )
        ctes = _detach_with(copy)
        inner_sql = inner.sql(dialect=dialect)
    else:
        if target.site == "filter":
            kept = [
                p
                for p in sql_shape.predicates(select, "where")
                if not _within(target.node, p)
            ]
            sql_shape.set_predicates(select, "where", kept)
        select.set(
            "expressions",
            [exp.alias_(raw, "dex_raw"), exp.alias_(conv, "dex_conv")],
        )
        select.set("order", None)
        main = target.scope == sql_shape.MAIN_SCOPE
        ctes = _detach_with(copy, None if main else target.scope)
        if main:
            inner_sql = select.sql(dialect=dialect)
        else:
            inner_sql = (
                exp.select("dex_raw", "dex_conv")
                .from_(exp.to_identifier(target.scope))
                .sql(dialect=dialect)
            )
        where = (
            "with the scope's other filters applied and the conversion's own "
            "predicate removed"
            if target.site == "filter"
            else "with the scope's filters applied"
        )
        denominator = f"rows reaching the conversion in `{target.scope}`, {where}"

    raw_text = f"CAST(d.dex_raw AS {text.sql(dialect=dialect)})"
    conv_text = f"CAST(d.dex_conv AS {text.sql(dialect=dialect)})"
    measures = (
        "SELECT COUNT(*) AS input_rows, "
        "COUNT(CASE WHEN d.dex_raw IS NULL THEN 1 END) AS null_key_rows, "
        "COUNT(DISTINCT d.dex_raw) AS input_entities, "
        "COUNT(CASE WHEN d.dex_raw IS NOT NULL AND d.dex_conv IS NULL THEN 1 END) "
        "AS failed_rows, "
        "COUNT(DISTINCT CASE WHEN d.dex_conv IS NULL THEN d.dex_raw END) "
        "AS failed_entities, "
        "COUNT(DISTINCT CASE WHEN d.dex_conv IS NOT NULL THEN d.dex_raw END) "
        "AS retained_entities, "
        "COUNT(DISTINCT d.dex_conv) AS converted_entities, "
        + (
            # Compared as text, which only says something when the source is
            # text: a DOUBLE key reads `7.0` against an integer's `7` for every
            # value, a difference of formatting and not of identity.
            f"COUNT(DISTINCT CASE WHEN d.dex_conv IS NOT NULL AND {conv_text} <> "
            f"{raw_text} THEN d.dex_raw END)"
            if text_source
            else "COUNT(CASE WHEN 1 = 0 THEN 1 END)"
        )
        + f" AS representation_changed_entities FROM ({inner_sql}) AS d"
    )
    collisions = (
        "SELECT COUNT(*) AS collided_keys, "
        "COALESCE(SUM(g.dex_n), 0) AS colliding_source_entities FROM ("
        "SELECT d.dex_conv, COUNT(DISTINCT d.dex_raw) AS dex_n "
        f"FROM ({inner_sql}) AS d WHERE d.dex_conv IS NOT NULL "
        "GROUP BY d.dex_conv HAVING COUNT(DISTINCT d.dex_raw) > 1) AS g"
    )
    columns = [f"a.{name}" for name in _COUNT_FIELDS[:8]] + [
        "c.collided_keys",
        "c.colliding_source_entities",
    ]
    joins = f"FROM ({measures}) AS a CROSS JOIN ({collisions}) AS c"
    if partner_sql is not None:
        matched = (
            "SELECT COUNT(DISTINCT d.dex_raw) AS matched_entities "
            f"FROM ({inner_sql}) AS d INNER JOIN ({partner_sql}) AS p "
            "ON d.dex_conv = p.dex_pk"
        )
        columns.append("m.matched_entities")
        joins += f" CROSS JOIN ({matched}) AS m"
    body = f"SELECT {', '.join(columns)} {joins}"
    sql = _wrap(ctes, body, dialect)
    try:
        sql = sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect)
    except sqlglot.errors.ParseError:
        return _Built(
            code="probe_failed", reason="the measuring statement did not parse"
        )
    return _Built(sql=sql, denominator=denominator, partner=partner_label)


def _own_side_filters(
    select: exp.Select, relation: exp.Table, column: exp.Column
) -> list[exp.Expression]:
    """The scope's WHERE conjuncts that read only the converted side, minus any
    that convert this key: what decides which of that side's rows reach the
    join, so an intentional exclusion there is not counted as this
    conversion's loss, while a filter the conversion drives is."""

    alias = (relation.alias_or_name or "").lower()
    key = sql_shape.match_key(column)
    kept = []
    for predicate in sql_shape.predicates(select, "where"):
        columns = list(predicate.find_all(exp.Column))
        if not columns or any((c.table or "").lower() != alias for c in columns):
            continue
        converts = any(
            (peeled := sql_shape.peel_key_conversion(node)) is not None
            and sql_shape.match_key(peeled[0]) == key
            for node in predicate.walk()
            if isinstance(node, exp.Expression)
        )
        if not converts:
            kept.append(predicate.copy())
    return kept


def _outputs(tree: exp.Expression) -> set[str] | None:
    """The lowercased output column names of a statement's first SELECT branch, or None
    where they cannot be read (no branch, or a ``*``).
    """
    body = tree.unnest() if hasattr(tree, "unnest") else tree
    branches = sql_shape.select_branches(body)
    if not branches:
        return None
    names = {n.lower() for n in branches[0].named_selects}
    return None if "*" in names else names


def identity_probe(
    prior: exp.Expression, authored: exp.Expression, column: str, dialect: str
) -> str:
    """One statement comparing the distinct identities ``column`` holds in the
    prior and the authored model, as text, plus both row counts."""

    text = _text_type(dialect).sql(dialect=dialect)
    ident = exp.to_identifier(column).sql(dialect=dialect)

    def body(tree: exp.Expression) -> str:
        inner = tree.copy()
        inner.set("order", None)
        return inner.sql(dialect=dialect)

    p, a = body(prior), body(authored)
    keys = (
        "SELECT DISTINCT CAST(x.{c} AS {t}) AS dex_k FROM ({b}) AS x "
        "WHERE x.{c} IS NOT NULL"
    )
    sql = (
        "SELECT pr.prior_rows, pr.prior_entities, au.authored_rows, "
        "au.authored_entities, k.kept FROM ("
        f"SELECT COUNT(*) AS prior_rows, COUNT(DISTINCT CAST(x.{ident} AS {text})) "
        f"AS prior_entities FROM ({p}) AS x) AS pr CROSS JOIN ("
        "SELECT COUNT(*) AS authored_rows, "
        f"COUNT(DISTINCT CAST(x.{ident} AS {text})) "
        f"AS authored_entities FROM ({a}) AS x) AS au CROSS JOIN ("
        "SELECT COUNT(*) AS kept FROM ("
        + keys.format(c=ident, t=text, b=p)
        + ") AS pk INNER JOIN ("
        + keys.format(c=ident, t=text, b=a)
        + ") AS ak ON pk.dex_k = ak.dex_k) AS k"
    )
    return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect)


# --- orchestration -------------------------------------------------------------


@dataclass
class _Entry:
    record: IdentifierConversion
    probe: ExtraProbe | None = None
    identity: ExtraProbe | None = None
    baseline: ExtraProbe | None = None
    #: False where the profile says the source column is not text, so a
    #: respelling is not counted (and reads as not measured, never as zero).
    text_source: bool = True


@dataclass
class Diagnosis:
    """The conversions a plan adds, and the statements that would measure them.

    Built before row attribution runs, so its statements ride the same batch;
    :meth:`settle` reads their answers back once the batch is done.
    """

    entries: list[_Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def probes(self) -> list[ExtraProbe]:
        """Every distinct statement the diagnosis needs run, once each, for the row
        attribution batch to carry.
        """
        out: dict[int, ExtraProbe] = {}
        for entry in self.entries:
            for probe in (entry.probe, entry.baseline, entry.identity):
                if probe is not None:
                    out.setdefault(id(probe), probe)
        return list(out.values())

    def settle(self) -> list[IdentifierConversion]:
        """Settle every conversion from the batch's answers, add one warning per
        undeclared conversion, and return the records.
        """
        for entry in self.entries:
            _settle(entry, self.assumptions.get(entry.record.model, []))
        records = [entry.record for entry in self.entries]
        grouped: dict[tuple[str, str, str], list[IdentifierConversion]] = {}
        for record in records:
            if record.status == "undeclared":
                key = (record.model, record.column, record.authored)
                grouped.setdefault(key, []).append(record)
        self.warnings.extend(_warning(group) for group in grouped.values())
        return records


def diagnose(
    engine: DexEngine,
    edits: list[PlanEdit],
    *,
    requested: bool | None,
    view: Any,
    post_change: dict[str, str],
) -> Diagnosis:
    """Find the conversions ``edits`` add to identifier columns and build the
    statements that would measure them; nothing runs here."""

    from .drafting import read_declared

    diagnosis = Diagnosis()
    counting, free, connector = counting_decision(engine, requested)
    dialect = get_dialect(connector)
    cache = readable_cache(engine.store) if counting else None
    detection_cache = cache if cache is not None else readable_cache(engine.store)
    resolver = cache_resolver(cache, dialect) if counting else naming_resolver
    total = 0
    for edit in edits:
        if edit.kind is not EditKind.MODEL_SQL or edit.op is not EditOp.UPSERT:
            continue
        if edit.new_content is None or not edit.path.endswith(".sql"):
            continue
        model = PurePosixPath(edit.path).stem
        current = view.files.get(edit.path)
        prior_sql = current.content if current is not None else None
        declared = read_declared(post_change, model)
        diagnosis.assumptions[model] = [
            a for a in declared.get("assumptions") or [] if isinstance(a, dict)
        ]
        try:
            authored_plain = sqlglot.parse_one(
                render_model_sql(edit.new_content, naming_resolver, view.vars),
                dialect=dialect,
            )
        except (UnattributableError, sqlglot.errors.ParseError):
            continue
        prior_plain = None
        if prior_sql is not None:
            try:
                prior_plain = sqlglot.parse_one(
                    render_model_sql(prior_sql, naming_resolver, view.vars),
                    dialect=dialect,
                )
            except (UnattributableError, sqlglot.errors.ParseError):
                prior_plain = None
        existing = (
            {f.signature() for f in find_conversions(prior_plain)}
            if prior_plain is not None
            else set()
        )
        keys = _declared_keys(declared)
        found = [
            (i, f)
            for i, f in enumerate(find_conversions(authored_plain))
            if f.signature() not in existing
        ]
        picked: list[tuple[int, _Found, str]] = []
        for index, conversion in found:
            basis = _key_basis(conversion, keys, detection_cache)
            if basis is not None:
                picked.append((index, conversion, basis))
        if not picked:
            continue

        rendered: tuple[exp.Expression, exp.Expression | None] | None = None
        render_reason: str | None = None
        if counting:
            try:
                authored_tree = sqlglot.parse_one(
                    render_model_sql(edit.new_content, resolver, view.vars),
                    dialect=dialect,
                )
                prior_tree = None
                if prior_sql is not None:
                    try:
                        prior_tree = sqlglot.parse_one(
                            render_model_sql(prior_sql, resolver, view.vars),
                            dialect=dialect,
                        )
                    except (UnattributableError, sqlglot.errors.ParseError):
                        prior_tree = None
                rendered = (authored_tree, prior_tree)
            except UnattributableError as exc:
                render_reason = str(exc)
            except sqlglot.errors.ParseError:
                render_reason = "the authored model did not parse once rendered"

        measurable = 0
        shared: dict[str, tuple[ExtraProbe, ExtraProbe] | None] = {}
        for index, conversion, basis in picked:
            record = IdentifierConversion(
                model=model,
                scope=conversion.scope,
                site=conversion.site,
                column=conversion.column.name,
                output_column=conversion.output,
                steps=[s.label() for s in conversion.steps],
                authored=sql_shape.text(conversion.node),
                key_basis=basis,
                status="unchecked",
                evidence=_evidence(
                    detection_cache,
                    _relation_of(conversion.select, conversion.column),
                    conversion.column.name,
                ),
            )
            entry = _Entry(
                record=record, text_source=_text_source(detection_cache, conversion)
            )
            diagnosis.entries.append(entry)
            if conversion.unsupported:
                _unchecked(
                    record,
                    conversion.code,
                    "the identifier is rewritten by an expression dex does not "
                    "measure; only casts and lower, upper and trim are"
                    if conversion.code == "unsupported_expression"
                    else "the conversion sits in a UNION branch, a subquery or a "
                    "nested CTE, which dex does not align between two versions",
                )
                continue
            if measurable >= MAX_PER_MODEL or total >= MAX_PER_PLAN:
                _unchecked(
                    record,
                    "cap_reached",
                    f"beyond the cap of {MAX_PER_MODEL} conversions per model and "
                    f"{MAX_PER_PLAN} per plan",
                )
                continue
            total += 1
            measurable += 1
            if not counting:
                _unchecked(
                    record,
                    "counting_off",
                    "counting is off (--no-attribute-rows)"
                    if free
                    else (
                        f"measuring it costs {connector} spend, so it is opt-in: "
                        "re-run with --attribute-rows to price and measure it"
                    ),
                )
                continue
            if rendered is None:
                code = (
                    "unprofiled_input"
                    if render_reason and "explore" in render_reason
                    else "unrenderable_sql"
                )
                _unchecked(record, code, render_reason or "the model did not render")
                continue
            authored_tree, prior_tree = rendered
            built = conversion_probe(
                authored_tree,
                index,
                dialect,
                _labels(conversion),
                expect=(conversion.site, sql_shape.match_key(conversion.node)),
                text_source=entry.text_source,
            )
            if built.sql is None:
                _unchecked(record, built.code or "probe_failed", built.reason or "")
                continue
            record.denominator = built.denominator
            record.partner = built.partner
            entry.probe = ExtraProbe(sql=built.sql, label=LABEL)
            _plan_identity(
                entry, conversion, prior_tree, authored_tree, declared, dialect, shared
            )
    return diagnosis


def _plan_identity(
    entry: _Entry,
    conversion: _Found,
    prior: exp.Expression | None,
    authored: exp.Expression,
    declared: dict[str, Any],
    dialect: str,
    shared: dict[str, tuple[ExtraProbe, ExtraProbe] | None],
) -> None:
    """Compare the identities one output column holds before and after.

    The column is the one the conversion defines (a projection's alias), the
    converted column itself where the driving relation carries it straight
    through, or the declared single-column grain. A join conversion on the
    joined side names a column of another relation, so it falls to the grain.
    The statements are shared per column: two conversions on one model read
    the same two model versions once.
    """

    record = entry.record
    if prior is None:
        record.identity_reason_code = "new_model"
        record.identity_reason = "the model is new, so there is no prior identity set"
        return
    prior_out, authored_out = _outputs(prior), _outputs(authored)
    if prior_out is None or authored_out is None:
        record.identity_reason_code = "no_shared_key_column"
        record.identity_reason = (
            "an output list dex cannot read (a `*`), so no column carries the "
            "identifier in both versions"
        )
        return
    driving = sql_shape.from_relation(conversion.select)
    own = _relation_of(conversion.select, conversion.column)
    carried = conversion.column.name if own is not None and own is driving else None
    partner = conversion.partner
    # A join converting the joined side's key: the identity the model carries
    # is the driving side's, the column the conversion meets.
    if (
        carried is None
        and isinstance(partner, exp.Column)
        and _relation_of(conversion.select, partner) is driving
    ):
        carried = partner.name
    grain = [str(c).lower() for c in declared.get("grain") or []]
    for candidate in (
        conversion.output,
        carried,
        grain[0] if len(grain) == 1 else None,
    ):
        if candidate and candidate.lower() in prior_out & authored_out:
            column = candidate.lower()
            break
    else:
        record.identity_reason_code = "no_shared_key_column"
        record.identity_reason = (
            "no output column carries the identifier in both the prior and the "
            "authored model"
        )
        return
    record.identity = IdentityChange(column=column)
    if column not in shared:
        shared[column] = (
            ExtraProbe(sql=count_sql(prior, dialect), label=LABEL),
            ExtraProbe(
                sql=identity_probe(prior, authored, column, dialect), label=LABEL
            ),
        )
    entry.baseline, entry.identity = shared[column]


def _text_source(cache: DexCache | None, conversion: _Found) -> bool:
    """Whether the converted column holds text, as far as the profile knows:
    unknown counts as text, since that is the case a respelling matters in."""

    dataset = _dataset_for(cache, _relation_of(conversion.select, conversion.column))
    if dataset is None:
        return True
    name = conversion.column.name.lower()
    for column in dataset.columns:
        if column.name.lower() == name:
            kind = (column.data_type or "").lower()
            return any(t in kind for t in ("char", "text", "string", "varchar"))
    return True


def _labels(conversion: _Found) -> tuple[str | None, str | None]:
    """The relations a join conversion reads, named as the model names them."""

    own = _relation_of(conversion.select, conversion.column)
    partner = conversion.partner
    column = (
        partner
        if isinstance(partner, exp.Column)
        else (sql_shape.peel_key_conversion(partner) or (None,))[0]
        if partner is not None
        else None
    )
    other = _relation_of(conversion.select, column) if column is not None else None
    return (
        sql_shape.relation_key(own) if own is not None else None,
        sql_shape.relation_key(other) if other is not None else None,
    )


def _evidence(
    cache: DexCache | None, table: exp.Table | None, column: str
) -> list[str]:
    """The profile's evidence that ``column`` mixes value shapes, as a sentence that
    does not call either shape invalid.
    """
    dataset = _dataset_for(cache, table)
    if dataset is None:
        return []
    mixed = any(
        getattr(finding, "code", None) == "mixed_identifier_shapes"
        and str(getattr(finding, "column", "")).lower() == column.lower()
        for finding in getattr(dataset, "findings", None) or []
    )
    if not mixed:
        return []
    return [
        f"the profile of {dataset.identifier} observed more than one value shape "
        f"in {column}; shape alone does not say which identifiers are valid"
    ]


def _unchecked(record: IdentifierConversion, code: str, reason: str) -> None:
    record.status = "unchecked"
    record.reason_code = code
    record.reason = reason


def _int(row: dict[str, Any], name: str) -> int | None:
    """The integer count named ``name`` in a probe row, None where the probe returned
    none.
    """
    value = row.get(name)
    return None if value is None else int(value)


def _settle(entry: _Entry, assumptions: list[dict[str, Any]]) -> None:
    """Read one conversion's probe answers back into its record and decide its status,
    or mark it unchecked where the probe did not answer.
    """
    record = entry.record
    if record.reason_code is not None:
        return
    probe = entry.probe
    if probe is None or probe.row is None:
        code, reason = (
            probe.error
            if probe is not None and probe.error
            else ("probe_failed", "it did not run")
        )
        _unchecked(record, code, reason)
        record.identity_reason_code = record.identity_reason_code or code
        return
    counts = ConversionCounts(**{n: _int(probe.row, n) for n in _COUNT_FIELDS})
    if not entry.text_source:
        counts.representation_changed_entities = None
    record.measured = counts
    _settle_identity(entry)

    # What the conversion itself does to the model's entities. In a filter it
    # removes the identifiers it fails on and nothing else: the key it computes
    # is never an output, so a merge or a respelling there changes no identity.
    # Unmatched keys across a join are reported but never decide this: a key
    # that was an orphan before the conversion is not the conversion's loss.
    lost = counts.failed_entities or 0
    merged = 0 if record.site == "filter" else counts.colliding_source_entities or 0
    respelled = (
        0 if record.site == "filter" else counts.representation_changed_entities or 0
    )
    identity = record.identity
    defines = (
        record.site == "projection"
        and identity is not None
        and identity.column == (record.output_column or "").lower()
    )
    # The identity comparison covers the whole edit, so it decides this only
    # where the conversion itself defines the compared column.
    identity_lost = (identity.lost or 0) if defines and identity else 0
    if not (lost or merged or respelled or identity_lost):
        record.status = "lossless"
        return
    names = {record.column.lower(), (record.output_column or "").lower()} - {""}
    for assumption in assumptions:
        column = assumption.get("column")
        if isinstance(column, str) and column.strip().lower() in names:
            record.status = "declared"
            record.assumption = {
                k: assumption.get(k)
                for k in ("decision", "chosen", "evidence", "column")
                if assumption.get(k) is not None
            }
            if assumption.get("evidence") == "default":
                record.reason = (
                    "recorded as a `default`: a conventional choice, not the "
                    "business's rule"
                )
            return
    record.status = "undeclared"


def _settle_identity(entry: _Entry) -> None:
    """Compare the prior and authored identity sets for one conversion, or record why
    the comparison could not run.
    """
    record = entry.record
    identity = record.identity
    if identity is None:
        return
    baseline = entry.baseline
    if baseline is not None and baseline.row is None:
        code = baseline.error[0] if baseline.error else "baseline_failed"
        record.identity = None
        record.identity_reason_code = (
            "baseline_failed" if code == "probe_failed" else code
        )
        record.identity_reason = (
            "the prior model does not run against today's relations, so there "
            "is no prior identity set to compare"
            if code == "probe_failed"
            else (baseline.error[1] if baseline.error else "")
        )
        return
    probe = entry.identity
    if probe is None or probe.row is None:
        record.identity = None
        code, reason = (
            probe.error
            if probe is not None and probe.error
            else (
                "probe_failed",
                "it did not run",
            )
        )
        record.identity_reason_code, record.identity_reason = code, reason
        return
    row = probe.row
    identity.prior_rows = _int(row, "prior_rows")
    identity.authored_rows = _int(row, "authored_rows")
    identity.prior_entities = _int(row, "prior_entities")
    identity.authored_entities = _int(row, "authored_entities")
    identity.kept = _int(row, "kept")
    if None not in (identity.prior_entities, identity.authored_entities, identity.kept):
        identity.lost = identity.prior_entities - identity.kept  # type: ignore[operator]
        identity.gained = identity.authored_entities - identity.kept  # type: ignore[operator]
        identity.identity_changed = bool(identity.lost or identity.gained)


def _effects(record: IdentifierConversion) -> str:
    """What one measured conversion does, as clauses with their denominators."""

    counts = record.measured or ConversionCounts()
    parts = []
    if counts.failed_entities:
        parts.append(
            f"{counts.failed_entities} of {counts.input_entities} distinct "
            f"`{record.column}` values do not convert"
        )
    if counts.colliding_source_entities:
        parts.append(
            f"{counts.colliding_source_entities} distinct values collapse into "
            f"{counts.collided_keys} key(s)"
        )
    if counts.representation_changed_entities:
        parts.append(
            f"{counts.representation_changed_entities} change representation "
            "(for example a leading zero dropped)"
        )
    if counts.matched_entities is not None and counts.retained_entities:
        unmatched = counts.retained_entities - counts.matched_entities
        if unmatched:
            parts.append(
                f"{unmatched} of {counts.retained_entities} converted values find "
                "no match across the join"
            )
    identity = record.identity
    if identity is not None and identity.identity_changed:
        parts.append(
            f"against the prior model {identity.lost} identities in "
            f"`{identity.column}` are gone and {identity.gained} are new "
            f"(rows {identity.prior_rows} -> {identity.authored_rows})"
        )
    scope = f" ({record.denominator})" if record.denominator else ""
    return f"{'; '.join(parts)}{scope}"


def _warning(group: list[IdentifierConversion]) -> str:
    """One warning per conversion as written, however many clauses carry it:
    the same cast in the ON and the WHERE is one decision, measured twice."""

    first = group[0]
    if len(group) == 1:
        effects = _effects(first)
    else:
        effects = "; ".join(f"in the {r.site}, {_effects(r)}" for r in group)
    return (
        f"{first.model}: `{first.authored}` changes which `{first.column}` "
        f"entities the model holds: {effects}. Nothing declared records this, and "
        "shape alone does not make a value invalid or a duplicate. Keep the other "
        "identifier scheme (compare as text, normalize, a bridge or a namespace), "
        "or record the choice as an assumption with "
        f"`column: {first.output_column or first.column}`"
    )
