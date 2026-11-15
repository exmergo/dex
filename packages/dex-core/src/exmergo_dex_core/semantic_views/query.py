"""A governed Semantic View query, translated and rendered as ``SEMANTIC_VIEW(...)``.

Pure: no connection, no cache. What comes in is a request in the catalog's own
tokens (``view.table.member``, ``view.metric`` for a view-level metric) and the
validated set; what comes out is the statement dex runs through its Snowflake
connector, the result names, and what the PII gate and the pricing need. Every
refusal here happens before anything is sent.

**The statement is rendered as text.** sqlglot parses the clause, which is how the
governed spine proves it read-only, but when it generates SQL it moves
``METRICS`` ahead of ``DIMENSIONS``, which reorders the result columns. So the
clause is written here and only parsed afterwards, to check it.

**Each item carries a positional alias** (``AS "c0"``): Snowflake heads a column
with the member's unqualified name, so two tables' ``name`` would collide, and the
alias is what the result is renamed from. A fact takes no alias in Snowflake's
grammar; a facts-only query reads one table, whose member names are unique, so
its headers are the fact names.

What v1 refuses by name, before anything is sent:

- facts beside metrics (Snowflake refuses the combination) and facts beside
  dimensions (refused from the 2026_07 behaviour change on);
- a private member, a window metric, a level-of-detail member, a member naming
  its join path (``using_relationships``), and any query on a view declaring
  ``variables``;
- a dimension whose table the metric's table does not reach at an equal or
  coarser grain, read from the catalog's groupable list, which is what
  Snowflake's own ``Invalid dimension specified`` refuses;
- a grain on a dimension that is not temporal, and a bare ``--grain``, since a
  Semantic View has no ``metric_time``;
- a ``--where`` naming a metric, a private member, a name the view does not have,
  a subquery, an aggregate, a window or a placeholder;
- tokens from more than one view.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import (
    DERIVED,
    DIMENSION,
    FACT,
    METRIC,
    TIME_DIMENSION,
    Member,
    SemanticView,
)

if TYPE_CHECKING:
    from ..semantic_catalog import SemanticCatalogView
    from .catalog import Resolution
    from .loader import LoadResult

__all__ = [
    "GRAINS",
    "QueryRefused",
    "Rendered",
    "Resolved",
    "render_query",
    "render_values",
    "resolve_token",
    "where_tokens",
]

#: The units a grain suffix may name: each one Snowflake's ``DATE_TRUNC`` takes.
GRAINS: tuple[str, ...] = (
    "second",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "quarter",
    "year",
)

_TEMPORAL = ("DATE", "TIME", "TIMESTAMP", "DATETIME")

#: The member keys that make a metric a level-of-detail expression.
_LOD_KEYS = ("lod_dimension_type", "lod_dimensions")


class QueryRefused(Exception):  # noqa: N818  (a refusal, not an error)
    """A request understood and deliberately not sent. ``guard`` marks one refused
    for safety (a private member, a filter that cannot be screened) rather than
    for shape, which the layer reports as a refusal rather than a bad request."""

    def __init__(self, message: str, *, guard: bool = False) -> None:
        super().__init__(message)
        self.guard = guard


@dataclass(frozen=True)
class Resolved:
    """One token resolved against the set: the member, its view, and a grain."""

    token: str
    view: SemanticView
    member: Member
    grain: str | None = None

    @property
    def kind(self) -> str:
        """The member's kind."""
        return self.member.kind

    @property
    def table(self) -> str | None:
        """The logical table it belongs to; None for a view-level metric."""
        return self.member.table

    @property
    def reference(self) -> str:
        """How the clause names it: ``table.member``, a derived metric bare."""
        if self.member.table is None:
            return self.member.name
        return f"{self.member.table}.{self.member.name}"

    @property
    def base(self) -> str:
        """The token without its grain suffix: what the catalog lists."""
        if self.grain is None:
            return self.token
        return self.token[: -(len(self.grain) + 2)]


@dataclass
class Rendered:
    """A statement ready for the governed spine.

    ``names`` renames each result header (as the adapter reports it, lowercased)
    to the token the caller wrote. ``members`` is every member the statement
    names, metric inputs not included; ``tables`` the logical tables it reaches;
    ``filter_tokens`` the tokens ``--where`` names, for the PII gate.
    """

    sql: str
    view: SemanticView
    names: dict[str, str]
    members: list[Resolved]
    tables: set[str] = field(default_factory=set)
    filter_tokens: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)


# --- tokens ----------------------------------------------------------------------


def resolve_token(loaded: LoadResult, token: str) -> Resolved:
    """``token`` as a member of the set, or refused by name.

    ``view.table.member`` or ``view.metric`` (a view-level metric), matched
    ignoring case, with an optional ``__<grain>`` on a dimension. A grain is read
    only where the name without it is a member and the name with it is not.
    """

    raw = (token or "").strip()
    parts = raw.split(".")
    view = next(
        (v for v in loaded.views if parts and v.name.lower() == parts[0].lower()),
        None,
    )
    if view is None or len(parts) not in (2, 3):
        raise QueryRefused(_unknown(raw, loaded))
    if len(parts) == 2:
        metric = view.metric(parts[1])
        if metric is None:
            raise QueryRefused(_unknown(raw, loaded))
        return Resolved(token=raw, view=view, member=metric)
    table = view.table(parts[1])
    if table is None:
        raise QueryRefused(_unknown(raw, loaded))
    member = table.member(parts[2])
    grain = None
    if member is None and "__" in parts[2]:
        head, tail = parts[2].rsplit("__", 1)
        if tail.lower() in GRAINS:
            member = table.member(head)
            grain = tail.lower()
    if member is None:
        raise QueryRefused(_unknown(raw, loaded))
    return Resolved(token=raw, view=view, member=member, grain=grain)


def _unknown(token: str, _loaded: LoadResult) -> str:
    spelled = (
        "view.table.member (`shop.orders.status`), or view.metric for a "
        "view-level metric (`shop.revenue_x2`)"
    )
    return (
        f"no such member in this semantic layer: {token or '(empty)'}. A Semantic "
        f"View token is {spelled}; list the layer with `explore semantic list`"
    )


# --- the request -----------------------------------------------------------------


def _check_member(resolved: Resolved, where: str) -> None:
    """Refuse a member v1 does not query, by name."""

    member = resolved.member
    if member.private:
        raise QueryRefused(
            f"refused: {resolved.base} is private (`access_modifier: "
            "private_access`): other expressions read it, and Snowflake never "
            f"queries it directly, so it cannot be {where}",
            guard=True,
        )
    if member.window:
        raise QueryRefused(
            f"refused: {resolved.base} is a window function metric, which a "
            "governed query does not render in this release"
        )
    if any(key in member.raw for key in _LOD_KEYS):
        raise QueryRefused(
            f"refused: {resolved.base} is a level-of-detail expression, which a "
            "governed query does not render in this release"
        )
    if member.using_relationships:
        raise QueryRefused(
            f"refused: {resolved.base} names its join path "
            f"(`using_relationships: {', '.join(member.using_relationships)}`), "
            "which a governed query does not render in this release"
        )


def _inputs(view: SemanticView, member: Member) -> list[Member]:
    """Every metric ``member`` reads, followed through composed metrics."""

    from .catalog import _targets

    found: list[Member] = []
    pending, seen = [member], set()
    while pending:
        current = pending.pop(0)
        for target in _targets(view, current):
            identity = (target.table or "", target.name.lower())
            if identity in seen:
                continue
            seen.add(identity)
            found.append(target)
            pending.append(target)
    return found


def metric_inputs(view: SemanticView, member: Member) -> list[Member]:
    """The metrics ``member`` reads, at any depth; public for the stale check."""
    return _inputs(view, member)


def _check_metric(resolved: Resolved) -> None:
    _check_member(resolved, "queried")
    for inner in _inputs(resolved.view, resolved.member):
        label = f"{inner.table}.{inner.name}" if inner.table else inner.name
        if inner.window or any(key in inner.raw for key in _LOD_KEYS):
            raise QueryRefused(
                f"refused: {resolved.base} reads {label}, a window or "
                "level-of-detail metric, which a governed query does not render "
                "in this release"
            )
        if inner.using_relationships:
            raise QueryRefused(
                f"refused: {resolved.base} reads {label}, which names its join "
                "path (`using_relationships`), and a governed query does not "
                "render that in this release"
            )


def _check_view(view: SemanticView) -> None:
    if "variables" in view.carried:
        raise QueryRefused(
            f"refused: view '{view.name}' declares `variables`, which a governed "
            "query does not bind in this release"
        )


def _one_view(resolved: Sequence[Resolved]) -> SemanticView:
    views = {r.view.name for r in resolved}
    if len(views) > 1:
        raise QueryRefused(
            "a governed query reads one semantic view, and these tokens name "
            f"{', '.join(sorted(views))}: query each view on its own"
        )
    return resolved[0].view


def _temporal(member: Member) -> bool:
    if member.kind == TIME_DIMENSION:
        return True
    data_type = (member.data_type or "").upper()
    return any(data_type.startswith(prefix) for prefix in _TEMPORAL)


def _groupable(catalog: SemanticCatalogView) -> dict[str, set[str]]:
    """Each metric token's groupable dimension tokens, lowered."""
    return {
        metric.name.lower(): {d.lower() for d in metric.dimensions or []}
        for metric in catalog.metrics
    }


def _reach(
    metrics: Sequence[Resolved],
    dimensions: Sequence[Resolved],
    catalog: SemanticCatalogView,
) -> None:
    """Refuse a dimension a metric's table does not reach many-to-one."""

    groupable = _groupable(catalog)
    for metric in metrics:
        reach = groupable.get(metric.token.lower(), set())
        for dim in dimensions:
            if dim.base.lower() not in reach:
                raise QueryRefused(
                    f"refused: {dim.base} cannot group {metric.token}: its table "
                    "is not reached from the metric's at an equal or coarser "
                    "grain through one unambiguous path of relationships, which "
                    "Snowflake refuses as an invalid dimension. `explore semantic "
                    f"list --metric {metric.token}` lists what can group it"
                )


def _facts_reach(view: SemanticView, facts: Sequence[Resolved], resolution) -> None:
    """Refuse facts no one table of them reaches the rest from."""

    tables = {f.table.lower() for f in facts if f.table}
    if len(tables) <= 1:
        return
    for table in tables:
        reach = resolution.coarser.get((view.name.lower(), table), set())
        if tables - {table} <= reach:
            return
    raise QueryRefused(
        f"refused: facts from {', '.join(sorted(tables))} cannot be read in one "
        "query: none of their tables reaches the others many-to-one, so there is "
        "no row they share"
    )


# --- --where ----------------------------------------------------------------------


def where_tokens(
    view_set: LoadResult, clauses: Sequence[str], view: SemanticView | None = None
) -> tuple[list[Resolved], str] | None:
    """The members ``--where`` names and the predicate as rendered, or None where
    a clause cannot be read (which the gate refuses rather than screening half).

    A column is ``view.table.member`` or ``table.member``; the short form is read
    against ``view`` (the request's), else against the one configured view that
    has that table.

    Raises :class:`QueryRefused` for a clause that reads, but names something a
    filter may not: a metric, a private member, a name the view lacks.
    """

    from .lineage import filter_predicate

    found: list[Resolved] = []
    rendered: list[str] = []

    def resolve(parts: list[str]) -> tuple[str, str]:
        resolved = _where_member(view_set, parts, view)
        found.append(resolved)
        return resolved.member.table or "", resolved.member.name

    for clause in clauses:
        predicate = filter_predicate(clause, resolve)
        if predicate is None:
            return None
        rendered.append(f"({predicate})")
    return found, " AND ".join(rendered)


def _where_member(
    loaded: LoadResult, parts: list[str], view: SemanticView | None
) -> Resolved:
    token = ".".join(parts)
    if len(parts) == 2:
        matches = (
            [view]
            if view is not None
            else [v for v in loaded.views if v.table(parts[0]) is not None]
        )
        if len(matches) != 1:
            raise QueryRefused(
                f"--where names {token}, which more than one configured view (or "
                "none) has; write it as view.table.member"
            )
        token = f"{matches[0].name}.{token}"
    resolved = resolve_token(loaded, token)
    if resolved.grain is not None:
        raise QueryRefused(
            f"--where names {token}: a grain suffix is a grouping, not a filter; "
            "filter on the dimension itself"
        )
    if resolved.kind in (METRIC, DERIVED):
        raise QueryRefused(
            f"refused: --where names {resolved.token}, a metric. A Semantic View "
            "filter reads dimensions and facts only, before any metric is "
            "computed; filter on the dimensions instead"
        )
    _check_member(resolved, "filtered on")
    return resolved


# --- rendering ---------------------------------------------------------------------


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _qualified(fq: str) -> str:
    """A deployed view's ``DB.SCHEMA.NAME`` (as SHOW spells each part), quoted."""
    return ".".join(_quote(part) for part in fq.split("."))


def _order(order_by: Sequence[str], aliases: dict[str, str]) -> list[str]:
    clauses: list[str] = []
    for entry in order_by:
        descending = entry.startswith("-")
        token = entry[1:] if descending else entry
        alias = aliases.get(token.lower())
        if alias is None:
            raise QueryRefused(
                f"--order-by {token} is not a member of this query; order by one "
                "of its own metrics or grouped members (`-` for descending)"
            )
        clauses.append(f"{alias}{' DESC' if descending else ''}")
    return clauses


def render_query(
    loaded: LoadResult,
    resolution: Resolution,
    catalog: SemanticCatalogView,
    deployed: str,
    *,
    metrics: Sequence[str],
    group_by: Sequence[str],
    where: Sequence[str] = (),
    order_by: Sequence[str] = (),
    grain: str | None = None,
    limit: int | None = None,
    max_rows: int,
) -> Rendered:
    """The ``SEMANTIC_VIEW`` statement for one governed query."""

    plan = translate_query(
        loaded,
        resolution,
        catalog,
        metrics=metrics,
        group_by=group_by,
        where=where,
        grain=grain,
    )
    return plan.render(deployed, order_by=order_by, limit=limit, max_rows=max_rows)


@dataclass
class Translated:
    """A request checked against the set, rendered against a deployed view later."""

    view: SemanticView
    metrics: list[Resolved]
    dimensions: list[Resolved]
    facts: list[Resolved]
    filters: list[Resolved]
    predicate: str | None
    distinct_values: bool = False

    @property
    def members(self) -> list[Resolved]:
        """Every member named, in clause order, filters last."""
        return [*self.dimensions, *self.facts, *self.metrics, *self.filters]

    def tables(self, resolution: Resolution) -> set[str]:
        """The logical tables the statement reaches (lowered): each member's,
        each metric input's, and the coarser tables a dimension is joined in from."""

        found: set[str] = set()
        for item in self.members:
            if item.table:
                found.add(item.table.lower())
            if item.kind in (METRIC, DERIVED):
                found.update(
                    m.table.lower() for m in _inputs(self.view, item.member) if m.table
                )
        return found

    def render(
        self,
        deployed: str,
        *,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        max_rows: int,
    ) -> Rendered:
        names: dict[str, str] = {}
        aliases: dict[str, str] = {}
        dims: list[str] = []
        for index, item in enumerate(self.dimensions):
            alias = f"c{index}"
            expr = item.reference
            if item.grain is not None:
                expr = f"DATE_TRUNC('{item.grain}', {expr})"
            dims.append(f"{expr} AS {_quote(alias)}")
            names[alias] = item.token
            aliases[item.token.lower()] = _quote(alias)
        offset = len(self.dimensions)
        mets: list[str] = []
        for index, item in enumerate(self.metrics, start=offset):
            alias = f"c{index}"
            mets.append(f"{item.reference} AS {_quote(alias)}")
            names[alias] = item.token
            aliases[item.token.lower()] = _quote(alias)
        facts: list[str] = []
        for item in self.facts:
            facts.append(item.reference)
            names[item.member.name.lower()] = item.token
            aliases[item.token.lower()] = item.member.name
        body = [_qualified(deployed)]
        if dims:
            body.append("DIMENSIONS " + ", ".join(dims))
        if mets:
            body.append("METRICS " + ", ".join(mets))
        if facts:
            body.append("FACTS " + ", ".join(facts))
        if self.predicate:
            body.append(f"WHERE {self.predicate}")
        clause = f"SEMANTIC_VIEW({' '.join(body)})"
        order = _order(order_by, aliases)
        if self.distinct_values and not order:
            order = [aliases[self.dimensions[0].token.lower()]]
        # One row past the cap, so the adapter can tell a result the cap cut
        # from one that happened to fit; a caller's own --limit is exact.
        rows = max_rows + 1 if limit is None or limit > max_rows else limit
        projection = _quote("c0") if self.distinct_values and self.metrics else "*"
        # Every part is dex-built: aliases are positional, members come from
        # the validated view, and the predicate was re-generated by sqlglot from
        # a parse whose every column resolved to a member.
        sql = f"SELECT {projection} FROM {clause}"  # noqa: S608
        if order:
            sql += " ORDER BY " + ", ".join(order)
        sql += f" LIMIT {rows}"
        return Rendered(
            sql=sql,
            view=self.view,
            names=names,
            members=self.members,
            filter_tokens=[f.token for f in self.filters],
            order=order,
        )


def translate_query(
    loaded: LoadResult,
    resolution: Resolution,
    catalog: SemanticCatalogView,
    *,
    metrics: Sequence[str],
    group_by: Sequence[str],
    where: Sequence[str] = (),
    grain: str | None = None,
) -> Translated:
    """Check a request against the set, refusing what v1 does not render."""

    if grain:
        raise QueryRefused(
            "a Semantic View has no `metric_time`, so a bare --grain has nothing "
            "to bucket: put the grain on the time dimension instead "
            "(`--group-by shop.orders.ordered_on__month`)"
        )
    named = [resolve_token(loaded, token) for token in metrics]
    grouped = [resolve_token(loaded, token) for token in group_by]
    everything = [*named, *grouped]
    if not everything:
        raise QueryRefused(
            "a Semantic View query needs at least one metric, or facts alone "
            "(discover them with `explore semantic list`)"
        )
    view = _one_view(everything)
    _check_view(view)

    for item in named:
        if item.kind in (DIMENSION, TIME_DIMENSION):
            raise QueryRefused(
                f"{item.token} is a dimension, not a metric: group by it with "
                "--group-by"
            )
        if item.kind == FACT:
            raise QueryRefused(
                f"{item.token} is a fact: a fact is row-level and read with "
                "--group-by in a query of facts alone, never as a metric"
            )
    for item in grouped:
        if item.kind in (METRIC, DERIVED):
            raise QueryRefused(
                f"{item.token} is a metric: pass it as a metric, not --group-by"
            )
    facts = [g for g in grouped if g.kind == FACT]
    dims = [g for g in grouped if g.kind != FACT]
    if facts and named:
        raise QueryRefused(
            f"refused: {', '.join(f.token for f in facts)} "
            f"{'is a fact' if len(facts) == 1 else 'are facts'}, and Snowflake "
            "never reads facts in the same query as metrics: query the facts "
            "alone, or a metric built on them"
        )
    if facts and dims:
        raise QueryRefused(
            f"refused: facts ({', '.join(f.token for f in facts)}) beside "
            f"dimensions ({', '.join(d.token for d in dims)}): Snowflake's 2026_07 "
            "behaviour change refuses the combination, so dex refuses it before "
            "anything is sent. Query the facts alone"
        )
    for item in named:
        _check_metric(item)
    for item in grouped:
        _check_member(item, "grouped by")
        if item.grain is not None and not _temporal(item.member):
            raise QueryRefused(
                f"{item.token}: a grain buckets a time dimension, and "
                f"{item.base} is categorical (no temporal `data_type`)"
            )
        if item.grain is not None and item.kind == FACT:
            raise QueryRefused(f"{item.token}: a fact takes no grain")
    if facts:
        _facts_reach(view, facts, resolution)

    filters: list[Resolved] = []
    predicate: str | None = None
    if where:
        read = where_tokens(loaded, where, view)
        if read is None:
            raise QueryRefused(
                "refused: dex could not read which members this --where names, so "
                "it cannot be screened for PII. A Semantic View filter is a "
                "Snowflake predicate over `view.table.member` (or `table.member`) "
                "dimensions and facts, with no subquery, aggregate, window or "
                "placeholder; correct the clause, or move the condition into "
                "--group-by",
                guard=True,
            )
        filters, predicate = read
        _one_view([*everything, *filters])
    filter_dims = [f for f in filters if f.kind != FACT]
    if named:
        _reach(named, [*dims, *filter_dims], catalog)
    return Translated(
        view=view,
        metrics=named,
        dimensions=dims,
        facts=facts,
        filters=filters,
        predicate=predicate,
    )


def render_values(
    loaded: LoadResult,
    resolution: Resolution,
    catalog: SemanticCatalogView,
    deployed: str,
    dimension: str,
    metrics: Sequence[str],
    *,
    max_rows: int,
) -> Rendered:
    """One dimension's values: a ``DIMENSIONS``-only clause, or one carrying the
    ``--metric`` that scopes them, which projects the dimension alone."""

    plan = translate_values(loaded, resolution, catalog, dimension, metrics)
    return plan.render(deployed, max_rows=max_rows)


def translate_values(
    loaded: LoadResult,
    resolution: Resolution,
    catalog: SemanticCatalogView,
    dimension: str,
    metrics: Sequence[str],
) -> Translated:
    """A values request checked against the set."""

    dim = resolve_token(loaded, dimension)
    if dim.kind == FACT:
        raise QueryRefused(
            f"{dim.token} is a fact: a fact is not grouped, so it has no value "
            "domain to list; profile the column behind it with `explore profile`"
        )
    if dim.kind in (METRIC, DERIVED):
        raise QueryRefused(f"{dim.token} is a metric, not a dimension")
    named = [resolve_token(loaded, token) for token in metrics]
    view = _one_view([dim, *named])
    _check_view(view)
    _check_member(dim, "listed")
    if dim.grain is not None and not _temporal(dim.member):
        raise QueryRefused(
            f"{dim.token}: a grain buckets a time dimension, and {dim.base} is "
            "categorical"
        )
    for item in named:
        if item.kind not in (METRIC, DERIVED):
            raise QueryRefused(f"--metric {item.token} is not a metric")
        _check_metric(item)
    _reach(named, [dim], catalog)
    return Translated(
        view=view,
        metrics=named,
        dimensions=[dim],
        facts=[],
        filters=[],
        predicate=None,
        distinct_values=True,
    )


def value_inputs(view: SemanticView, metric: Member) -> list[Member] | None:
    """The row-level members whose values ``metric`` can return, or None where
    one of its aggregates cannot be read.

    A metric is read with its inputs: ``MAX(customers.email)`` returns an email
    as surely as grouping by one. Every aggregate in the metric (and in each
    metric it reads) whose result is one of its inputs is found, and the members
    it reads are returned for the gate to screen as grouped members.
    """

    from .lineage import value_reads
    from .loader import resolve_read

    found: list[Member] = []
    for current in [metric, *_inputs(view, metric)]:
        reads = value_reads(current.expr)
        if reads is None:
            return None
        for read in reads:
            target = resolve_read(view, current, read.qualifier, read.name)
            if target.member is not None and target.member.row_level:
                found.append(target.member)
            elif target.member is not None:
                continue  # a metric inside MAX(): its own inputs are walked too
            elif target.column is not None:
                # A base column read directly: screened by its own name on the
                # metric's table, through a member standing for it.
                found.append(
                    Member(
                        kind=FACT,
                        name=target.column,
                        expr=target.column,
                        table=target.table,
                    )
                )
            else:
                return None
    return found


def token_of(view: SemanticView, member: Member) -> str:
    """A member's catalog token."""
    if member.table is None:
        return f"{view.name}.{member.name}"
    return f"{view.name}.{member.table}.{member.name}"
