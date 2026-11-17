"""A governed Metric View query, translated and rendered with ``MEASURE()`` (#1362).

Pure: no connection, no cache. What comes in is a request in the catalog's own
tokens (``view.field``, ``view.measure``, the view named after its file) and the
validated set; what comes out is the statement dex runs through its Databricks
connector, the result names, and what the PII gate and the pricing need. Every
refusal here happens before anything is sent.

**The statement is plain Databricks SQL over the deployed view**::

    SELECT `status` AS `orders.status`, MEASURE(`revenue`) AS `orders.revenue`
    FROM `main`.`sales`.`orders`
    WHERE (`region` = 'EMEA')
    GROUP BY ALL
    ORDER BY `orders.revenue` DESC
    LIMIT 51

Each output column is aliased by the token the request wrote, so the result
names what was asked for. A ``ratio`` or ``derived`` measure is selected through
``MEASURE()`` like any other: Databricks composes it, and dex never re-derives a
formula. The view's own ``filter`` is Databricks' to apply, so it is not
repeated. The statement is written as text and parsed afterwards by the governed
spine (SELECT-only), so the column order is the order the request named.

What v1 refuses by name, before anything is sent:

- a window or ``partition`` measure, and a measure reading one through
  ``MEASURE()``;
- any query on a view declaring ``parameters`` (a query has to bind them) or a
  ``materialization`` (Databricks may answer from it, and dex can neither hold it
  to the repository nor price it);
- tokens from more than one view;
- a grain on a field whose ``format`` is not ``date`` or ``date_time``, and a
  bare ``--grain``, since a Metric View has no ``metric_time``;
- a ``--where`` naming a measure, calling ``MEASURE()``, naming something that is
  not a field of the view, or holding a subquery, an aggregate, a window or a
  placeholder;
- a metric query with no measure, which is what ``explore semantic values`` is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import DexError
from .model import FIELD, MEASURE, Member, MetricView

if TYPE_CHECKING:
    from .catalog import Resolution
    from .loader import LoadResult

__all__ = [
    "GRAINS",
    "QueryRefused",
    "Rendered",
    "Resolved",
    "Translated",
    "base_tables",
    "field_columns",
    "filter_fields",
    "resolve_token",
    "token_of",
    "translate_query",
    "translate_values",
    "value_columns",
]

#: The units a grain suffix may name: each one Databricks' ``DATE_TRUNC`` takes.
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

_SOURCE: tuple[str, ...] = ()


class QueryRefused(DexError, ValueError):  # noqa: N818  (a refusal, not an error)
    """A request understood and deliberately not sent. ``guard`` marks one refused
    for safety (a filter that cannot be screened) rather than for shape, which
    the layer reports as a refusal rather than a bad request."""

    def __init__(self, message: str, *, guard: bool = False) -> None:
        super().__init__(message)
        self.guard = guard


@dataclass(frozen=True)
class Resolved:
    """One token resolved against the set: the member, its view, and a grain."""

    token: str
    view: MetricView
    member: Member
    grain: str | None = None

    @property
    def kind(self) -> str:
        """``field`` or ``measure``."""
        return self.member.kind

    @property
    def canonical(self) -> str:
        """The token as the catalog spells it, without a grain."""
        return token_of(self.view, self.member)

    @property
    def base(self) -> str:
        """The token as written, without its grain suffix."""
        if self.grain is None:
            return self.token
        return self.token[: -(len(self.grain) + 2)]


@dataclass
class Rendered:
    """A statement ready for the governed spine.

    ``names`` renames each result header (as the adapter reports it, lowercased)
    to the token the caller wrote.
    """

    sql: str
    view: MetricView
    names: dict[str, str]
    order: list[str] = field(default_factory=list)


def token_of(view: MetricView, member: Member) -> str:
    """A member's catalog token, ``view.member``."""
    return f"{view.name}.{member.name}"


# --- tokens ----------------------------------------------------------------------


def resolve_token(loaded: LoadResult, token: str) -> Resolved:
    """``token`` as a field or measure of the set, or refused by name.

    ``view.field`` or ``view.measure``, matched ignoring case, with an optional
    ``__<grain>`` on a field. A grain is read only where the name without it is a
    member and the name with it is not.
    """

    raw = (token or "").strip()
    parts = raw.split(".")
    if len(parts) != 2:
        raise QueryRefused(_unknown(raw))
    view = next((v for v in loaded.views if v.name.lower() == parts[0].lower()), None)
    if view is None:
        raise QueryRefused(_unknown(raw))
    member = view.member(parts[1])
    grain = None
    if member is None and "__" in parts[1]:
        head, tail = parts[1].rsplit("__", 1)
        if tail.lower() in GRAINS:
            member = view.member(head)
            grain = tail.lower()
    if member is None:
        raise QueryRefused(_unknown(raw))
    return Resolved(token=raw, view=view, member=member, grain=grain)


def _unknown(token: str) -> str:
    return (
        f"no such field or measure in this semantic layer: {token or '(empty)'}. A "
        "Metric View token is view.field or view.measure (`orders.order_status`, "
        "`orders.revenue`), the view named after its file; list the layer with "
        "`explore semantic list`"
    )


# --- the request -----------------------------------------------------------------


def _one_view(resolved: Sequence[Resolved]) -> MetricView:
    views = {r.view.name for r in resolved}
    if len(views) > 1:
        raise QueryRefused(
            "a governed query reads one Metric View, and these tokens name "
            f"{', '.join(sorted(views))}: query each view on its own"
        )
    return resolved[0].view


def _check_view(view: MetricView) -> None:
    if view.parameters:
        names = ", ".join(str(p.get("name")) for p in view.parameters)
        raise QueryRefused(
            f"refused: view '{view.name}' declares parameters ({names}), which a "
            "governed query does not bind in this release"
        )
    if view.materialization:
        raise QueryRefused(
            f"refused: view '{view.name}' declares a `materialization`, so "
            "Databricks may answer a query from an aggregate it maintains, which "
            "dex can neither hold to the repository nor price. A governed query "
            "does not read a materialized view in this release"
        )


def measure_inputs(view: MetricView, member: Member) -> list[Member] | None:
    """Every measure ``member`` reads through ``MEASURE()``, at any depth, or
    None where an expression cannot be read."""

    from . import lineage

    found: list[Member] = []
    pending, seen = [member], {member.name.lower()}
    while pending:
        current = pending.pop(0)
        names = lineage.measure_refs(current.expr)
        if names is None:
            return None
        for name in names:
            target = view.measure(name)
            if target is None or target.name.lower() in seen:
                continue
            seen.add(target.name.lower())
            found.append(target)
            pending.append(target)
    return found


def _check_measure(resolved: Resolved) -> None:
    member = resolved.member
    for current in [member, *(measure_inputs(resolved.view, member) or [])]:
        if not current.window and not current.partition:
            continue
        kind = "window" if current.window else "partition"
        reads = "" if current is member else f" reads {current.name}, which"
        raise QueryRefused(
            f"refused: {resolved.base}{reads} is a {kind} measure, which a "
            "governed query does not render in this release"
        )
    if measure_inputs(resolved.view, member) is None:
        raise QueryRefused(
            f"refused: dex could not read which measures {resolved.base} is "
            "composed of, so it cannot check what the query relies on"
        )


def _check_grain(resolved: Resolved) -> None:
    if resolved.grain is not None and not resolved.member.is_time:
        raise QueryRefused(
            f"{resolved.token}: a grain buckets a date or timestamp field, and "
            f"{resolved.base} is not one: a Metric View types a field as temporal "
            "only through a `format` of type date or date_time"
        )


# --- --where ----------------------------------------------------------------------


def filter_fields(
    loaded: LoadResult, clauses: Sequence[str], view: MetricView | None = None
) -> tuple[list[Resolved], str] | None:
    """The fields ``--where`` names and the predicate as rendered, or None where a
    clause cannot be read (which the gate refuses rather than screening half).

    A column is ``field`` or ``view.field``; the short form is read against
    ``view`` (the request's), else against the one configured view that has a
    field of that name.

    Raises :class:`QueryRefused` for a clause that reads, but names something a
    filter may not: a measure, a ``MEASURE()`` call, a name the view lacks.
    """

    from . import lineage

    found: list[Resolved] = []
    rendered: list[str] = []

    def resolve(parts: list[str]) -> str:
        resolved = _where_member(loaded, parts, view)
        found.append(resolved)
        return resolved.member.name

    for clause in clauses:
        called = lineage.measure_refs(clause or "")
        if called:
            raise QueryRefused(
                f"refused: --where calls MEASURE({called[0]}). A Metric View "
                "filter reads fields only, before any measure is computed; filter "
                "on the fields instead",
                guard=True,
            )
        predicate = lineage.filter_predicate(clause, resolve)
        if predicate is None:
            return None
        rendered.append(f"({predicate})")
    return found, " AND ".join(rendered)


def _where_member(
    loaded: LoadResult, parts: list[str], view: MetricView | None
) -> Resolved:
    token = ".".join(parts)
    if len(parts) == 1:
        matches = (
            [view]
            if view is not None
            else [v for v in loaded.views if v.member(parts[0]) is not None]
        )
        if len(matches) != 1:
            raise QueryRefused(
                f"--where names {token}, which more than one configured view (or "
                "none) has; write it as view.field"
            )
        token = f"{matches[0].name}.{token}"
    resolved = resolve_token(loaded, token)
    if resolved.grain is not None:
        raise QueryRefused(
            f"--where names {token}: a grain suffix is a grouping, not a filter; "
            "filter on the field itself"
        )
    if resolved.kind == MEASURE:
        raise QueryRefused(
            f"refused: --where names {resolved.token}, a measure. A Metric View "
            "filter reads fields only, before any measure is computed; filter on "
            "the fields instead",
            guard=True,
        )
    return resolved


# --- rendering ---------------------------------------------------------------------


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _qualified(fq: str) -> str:
    """A deployed view's ``catalog.schema.name``, each part quoted."""
    return ".".join(_quote(part) for part in fq.split("."))


@dataclass
class Translated:
    """A request checked against the set, rendered against a deployed view later."""

    view: MetricView
    measures: list[Resolved]
    fields: list[Resolved]
    filters: list[Resolved]
    predicate: str | None
    distinct_values: bool = False

    @property
    def members(self) -> list[Resolved]:
        """Every member named, in clause order, filters last."""
        return [*self.fields, *self.measures, *self.filters]

    def render(
        self,
        deployed: str,
        *,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        max_rows: int,
    ) -> Rendered:
        """The statement over the view deployed as ``deployed``."""

        names: dict[str, str] = {}
        aliases: dict[str, str] = {}

        def alias(item: Resolved) -> str:
            quoted = _quote(item.token)
            names[item.token.lower()] = item.token
            for key in (item.token, _grained(item.canonical, item.grain)):
                aliases.setdefault(key.lower(), quoted)
            return quoted

        projection: list[str] = []
        for item in self.fields:
            expr = _quote(item.member.name)
            if item.grain is not None:
                expr = f"DATE_TRUNC('{item.grain.upper()}', {expr})"
            projection.append(f"{expr} AS {alias(item)}")
        measures: list[str] = []
        for index, item in enumerate(self.measures):
            # A values read projects the field alone: the measure is there to
            # keep the groups it is present for, under a name no token can have.
            name = f"__m{index}" if self.distinct_values else None
            target = _quote(name) if name else alias(item)
            measures.append(f"MEASURE({_quote(item.member.name)}) AS {target}")

        selected = ", ".join([*projection, *measures])
        # Every part is dex-built: fields and measures come from the validated
        # view, aliases are validated tokens, and the predicate was re-generated
        # by sqlglot from a parse whose every column resolved to a field.
        statement = f"SELECT {selected} FROM {_qualified(deployed)}"  # noqa: S608
        if self.predicate:
            statement += f" WHERE {self.predicate}"
        statement += " GROUP BY ALL"
        if self.distinct_values and measures:
            # Every part is dex-built: the alias is a validated token.
            statement = (
                f"SELECT {aliases[self.fields[0].token.lower()]} FROM ({statement})"  # noqa: S608
            )
        order = _order(order_by, aliases)
        if self.distinct_values and not order:
            order = [aliases[self.fields[0].token.lower()]]
        if order:
            statement += " ORDER BY " + ", ".join(order)
        # One row past the cap, so the adapter can tell a result the cap cut
        # from one that happened to fit; a caller's own --limit is exact.
        rows = max_rows + 1 if limit is None or limit > max_rows else limit
        statement += f" LIMIT {rows}"
        return Rendered(sql=statement, view=self.view, names=names, order=order)


def _grained(token: str, grain: str | None) -> str:
    return token if grain is None else f"{token}__{grain}"


def _order(order_by: Sequence[str], aliases: dict[str, str]) -> list[str]:
    clauses: list[str] = []
    for entry in order_by:
        descending = entry.startswith("-")
        token = entry[1:] if descending else entry
        alias = aliases.get(token.lower())
        if alias is None:
            raise QueryRefused(
                f"--order-by {token} is not a member of this query; order by one "
                "of its own measures or grouped fields (`-` for descending)"
            )
        clauses.append(f"{alias}{' DESC' if descending else ''}")
    return clauses


def _distinct(items: Sequence[Resolved]) -> list[Resolved]:
    """``items`` with a repeat of the same member and grain dropped."""
    seen: set[tuple[str, str | None]] = set()
    found: list[Resolved] = []
    for item in items:
        key = (item.member.name.lower(), item.grain)
        if key not in seen:
            seen.add(key)
            found.append(item)
    return found


def translate_query(
    loaded: LoadResult,
    *,
    metrics: Sequence[str],
    group_by: Sequence[str],
    where: Sequence[str] = (),
    grain: str | None = None,
) -> Translated:
    """Check a request against the set, refusing what v1 does not render."""

    if grain:
        raise QueryRefused(
            "a Metric View has no `metric_time`, so a bare --grain has nothing to "
            "bucket: put the grain on a date field instead "
            "(`--group-by orders.order_date__month`)"
        )
    named = [resolve_token(loaded, token) for token in metrics]
    grouped = [resolve_token(loaded, token) for token in group_by]
    everything = [*named, *grouped]
    if not everything:
        raise QueryRefused(
            "a Metric View query needs at least one measure (discover them with "
            "`explore semantic list`)"
        )
    view = _one_view(everything)
    _check_view(view)
    for item in named:
        if item.kind == FIELD:
            raise QueryRefused(
                f"{item.token} is a field, not a measure: group by it with --group-by"
            )
    for item in grouped:
        if item.kind == MEASURE:
            raise QueryRefused(
                f"{item.token} is a measure: pass it as a metric, not --group-by"
            )
    if not named:
        raise QueryRefused(
            "a Metric View query needs at least one measure: listing a field's "
            "values is `explore semantic values "
            f"{grouped[0].token}`"
        )
    for item in named:
        _check_measure(item)
    for item in grouped:
        _check_grain(item)

    filters: list[Resolved] = []
    predicate: str | None = None
    if where:
        read = filter_fields(loaded, where, view)
        if read is None:
            raise QueryRefused(
                "refused: dex could not read which fields this --where names, so "
                "it cannot be screened for PII. A Metric View filter is a "
                "Databricks predicate over the view's fields (`status = 'F'`, or "
                "`orders.status`), with no subquery, aggregate, window, measure or "
                "placeholder; correct the clause, or move the condition into "
                "--group-by",
                guard=True,
            )
        filters, predicate = read
        _one_view([*everything, *filters])
    return Translated(
        view=view,
        measures=_distinct(named),
        fields=_distinct(grouped),
        filters=filters,
        predicate=predicate,
    )


def translate_values(
    loaded: LoadResult, dimension: str, metrics: Sequence[str]
) -> Translated:
    """A values request checked against the set."""

    item = resolve_token(loaded, dimension)
    if item.kind == MEASURE:
        raise QueryRefused(f"{item.token} is a measure, not a field")
    named = [resolve_token(loaded, token) for token in metrics]
    view = _one_view([item, *named])
    _check_view(view)
    _check_grain(item)
    for metric in named:
        if metric.kind != MEASURE:
            raise QueryRefused(f"--metric {metric.token} is not a measure")
        _check_measure(metric)
    return Translated(
        view=view,
        measures=_distinct(named),
        fields=[item],
        filters=[],
        predicate=None,
        distinct_values=True,
    )


# --- what the PII gate and the pricing read ------------------------------------------


@dataclass(frozen=True)
class ValueColumn:
    """One column a measure returns the values of: the token the gate screens it
    as, and ``(relation, column)`` where the table behind it is known."""

    token: str
    gate: tuple[str, str] | None


def value_columns(
    view: MetricView,
    member: Member,
    resolution: Resolution,
    fold: Callable[[str], str],
) -> list[ValueColumn] | None:
    """The columns ``member`` (and every measure it reads) returns the values of,
    or None where one of its expressions cannot be read.

    A measure is read with its inputs: ``MAX(customer.email)`` returns an email
    as surely as grouping by one. A bare name that is a field of the view is that
    field, screened as its catalog token; any other column is screened as
    ``view.<join path or source>.<column>``, through the table the join reads.
    """

    from . import lineage

    inputs = measure_inputs(view, member)
    if inputs is None:
        return None
    found: list[ValueColumn] = []
    for current in [member, *inputs]:
        reads = lineage.value_reads(current.expr)
        if reads is None:
            return None
        for ref in reads:
            field_ = view.member(ref.column) if not ref.qualifier else None
            if field_ is not None and field_.kind == FIELD:
                found.append(ValueColumn(token_of(view, field_), None))
                continue
            column = _column(view, ref, resolution, fold)
            if column is None:
                return None
            found.append(column)
    return list(dict.fromkeys(found))


def field_columns(
    view: MetricView,
    member: Member,
    resolution: Resolution,
    fold: Callable[[str], str],
) -> list[ValueColumn] | None:
    """Every column a field's expression reads, followed through the earlier
    fields it reads, or None where an expression cannot be read.

    What the PII gate screens a field as where the catalog's lineage has no
    column or expression for it: one computed over more than one table
    (``CONCAT(customer.c_name, customer.nation.n_name)``) projects each of
    them, and screening its name alone would pass a flagged column under a
    neutral one.
    """

    from . import lineage

    found: list[ValueColumn] = []
    pending, seen = [member], {member.name.lower()}
    while pending:
        current = pending.pop(0)
        refs = lineage.column_refs(current.expr)
        if refs is None:
            return None
        for ref in refs:
            earlier = view.member(ref.column) if not ref.qualifier else None
            if earlier is not None and earlier.kind == FIELD:
                if earlier.name.lower() not in seen:
                    seen.add(earlier.name.lower())
                    pending.append(earlier)
                continue
            column = _column(view, ref, resolution, fold)
            if column is None:
                return None
            found.append(column)
    return list(dict.fromkeys(found))


def _column(
    view: MetricView, ref: Any, resolution: Resolution, fold: Callable[[str], str]
) -> ValueColumn | None:
    """A column reference as the token the gate screens it as, through the table
    its qualifier names; None for a qualifier naming no join."""

    from .catalog import _owner, _through

    owner = _owner(view, ref.qualifier)
    if owner is None:
        return None
    table = resolution.table(view.name, owner)
    column = _through(table, fold(ref.column))
    where = "source" if owner == _SOURCE else ".".join(owner)
    gate = (table.gate, column) if table.gate and column else None
    return ValueColumn(f"{view.name}.{where}.{ref.column}", gate)


def base_tables(view: MetricView, plan: Translated) -> list[str] | None:
    """The tables the statement reaches through the view, as written: its source
    (every table a query source reads) and the source of each join a member, a
    filter or the view's own ``filter`` reads through, with the joins above it.
    None where an expression or a source cannot be read, so the caller refuses
    rather than under-prices.
    """

    from . import lineage

    paths: set[tuple[str, ...]] = set()
    fields = {m.name.lower(): m for m in view.fields}
    pending: list[str] = [item.member.expr for item in plan.members]
    for item in plan.measures:
        inputs = measure_inputs(view, item.member)
        if inputs is None:
            return None
        pending.extend(m.expr for m in inputs)
    if view.filter:
        pending.append(view.filter)
    seen_fields: set[str] = set()
    while pending:
        refs = lineage.column_refs(pending.pop())
        if refs is None:
            return None
        for ref in refs:
            if not ref.qualifier and ref.column.lower() in fields:
                name = ref.column.lower()
                if name not in seen_fields:
                    seen_fields.add(name)
                    pending.append(fields[name].expr)
                continue
            lowered = tuple(q.lower() for q in ref.qualifier)
            if lowered in ((), ("source",)):
                continue
            join = view.join(ref.qualifier)
            if join is None:
                return None
            paths.update(join.path[:depth] for depth in range(1, len(join.path) + 1))

    found: list[str] = []
    sources = [view.source]
    for path in sorted(paths):
        join = view.join(path)
        if join is None:
            return None
        sources.append(join.source)
    for source in sources:
        tables = lineage.source_tables(source)
        if tables is None:
            return None
        found.extend(t for t in tables if t not in found)
    return found
