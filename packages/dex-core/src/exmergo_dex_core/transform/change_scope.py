"""What a plan changes about the models it rewrites, beside its request (#1115).

A repair is asked to change some behaviour and keep the rest. The two failures
this reports are the ones nothing else at plan time sees: a filter meant for the
aggregates that also lands on a roster model, and a coverage fix that also
changes what an empty window counts. Row attribution names the first and
deliberately ignores the second, since column expressions cannot move rows.

Per existing model the plan rewrites, this reads both versions statically, with
no query and no connection, and says two things:

- ``population``: ``changed`` when an authored predicate, join, driving relation,
  DISTINCT or GROUP BY can move rows (row attribution's own classification),
  ``kept`` when nothing can, ``unchecked`` when the edit could not be read.
- ``outputs``: a column the final select no longer produces, one it newly
  produces, and, for every column both versions produce in a scope they share,
  a changed cast target, aggregate or NULL treatment.

Each change that alters a row population, removes an output, or changes a type,
an aggregate or a NULL treatment is held to the model's existing assumptions,
the way identifier conversions are (#861): ``declared`` when an assumption's
``column`` names a column it governs, ``undeclared`` otherwise.

Re-driving a model (#1228) is the one population change held to more: the
relations its rows come from changed, or its declared ``population.parent`` was
removed or replaced. That changes which entity the model is about, which only
the request or a contract can decide, so it is ``declared`` only by an
assumption at ``request`` or ``contract`` tier. Observed counts never settle
it: an agent that sees NULL aggregates and re-drives the model from the child
table has answered a join-key question with a new population. The baseline is
the applied model, or, for a model only an earlier unapplied plan wrote, that
plan. Nothing here
refuses a plan. An undeclared change is either one the request asked for, which
an assumption should record with the evidence that settles it, or one nobody
decided, which should be undone.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import expressions as exp

from .. import sql_shape
from .row_attribution import (
    UnattributableError,
    changed_set_operations,
    classify,
    naming_resolver,
    render_model_sql,
)

MAX_MODELS = 25
MAX_CHANGES_PER_MODEL = 8

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

_NULL_FUNCTIONS = ("coalesce", "ifnull", "nvl", "nullif", "nvl2", "zeroifnull")
# Functions that turn one row into several when they sit in a select list
# (`select unnest(items)`; `generate_series` does in Postgres).
_ROW_EXPANDING = (
    exp.Explode,
    exp.ExplodeOuter,
    exp.Unnest,
    exp.Inline,
    exp.GenerateSeries,
)


class ScopeChange(BaseModel):
    """One thing the plan changes about a model."""

    axis: str  # population | output
    kind: str
    scope: str
    detail: str
    #: The output column an output change is about.
    column: str | None = None
    #: The columns a population change's predicate or join reads.
    columns: list[str] | None = None
    prior: str | None = None
    authored: str | None = None
    #: ``redriven`` for a change of the relations driving the model or of its
    #: declared parent (#1228), whose settling needs ``request`` or ``contract``.
    reason: str | None = None
    #: ``declared`` or ``undeclared`` for a judged change; absent for one that
    #: is only listed (a column added, a rewritten expression).
    status: str | None = None
    assumption: dict[str, Any] | None = None
    note: str | None = None


class ModelScope(BaseModel):
    """One rewritten model's change scope, as ``data.change_scope`` lists it."""

    model: str
    path: str
    population: str  # changed | kept | unchecked
    outputs: str  # changed | kept | unchecked
    changes: list[ScopeChange] = Field(default_factory=list)
    undeclared: int = 0
    reason: str | None = None
    #: True where the final select's removals and additions could not be read
    #: (a star), whatever ``outputs`` says: a changed CTE column makes it
    #: ``changed`` without making the removals readable.
    outputs_unread: bool | None = None
    elided: int = 0
    #: What the model was compared with: ``applied`` (the file on disk) or
    #: ``earlier_plan`` (a model only an earlier unapplied plan wrote, #1228).
    against: str | None = None


def analyze(
    edits: Iterable[tuple[str, str, str]],
    assumptions: Iterable[Mapping[str, Any]],
    dialect: str,
    vars: Mapping[str, Any] | None = None,
    *,
    parents: Mapping[str, tuple[str | None, str | None]] | None = None,
    against: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """``data.change_scope`` for ``(path, prior_sql, authored_sql)`` triples.

    ``assumptions`` are ``data.decisions`` entries (``model``, ``column``, ...).
    ``None`` when no edit rewrites an existing model. ``parents`` maps a path
    to its declared ``population.parent`` before and after, and ``against`` a
    path to what its prior SQL is (``applied`` unless stated), for #1228.
    """

    by_model: dict[str, list[Mapping[str, Any]]] = {}
    for assumption in assumptions:
        model = assumption.get("model")
        if isinstance(model, str):
            by_model.setdefault(model.lower(), []).append(assumption)

    models: list[ModelScope] = []
    for path, prior_sql, authored_sql in edits:
        if prior_sql.strip() == authored_sql.strip() and not _parent_moved(
            (parents or {}).get(path)
        ):
            continue
        model = PurePosixPath(path).stem
        found = scope_for(
            path,
            prior_sql,
            authored_sql,
            dialect,
            by_model.get(model.lower(), []),
            vars,
            parents=(parents or {}).get(path),
        )
        found.against = (against or {}).get(path, "applied")
        models.append(found)
    if not models:
        return None
    models.sort(key=lambda m: (-m.undeclared, m.model))
    elided = max(0, len(models) - MAX_MODELS)
    shown = models[:MAX_MODELS]
    return {
        "models": [m.model_dump(mode="json", exclude_none=True) for m in shown],
        "population": {
            state: sorted(m.model for m in models if m.population == state)
            for state in ("changed", "kept", "unchecked")
        },
        "undeclared": sum(m.undeclared for m in models),
        "elided": {"models": elided},
        # Named, so a build can carry them as unchecked rather than read a
        # capped list as the whole plan.
        **({"beyond_cap": [m.model for m in models[MAX_MODELS:]]} if elided else {}),
    }


def scope_for(
    path: str,
    prior_sql: str,
    authored_sql: str,
    dialect: str,
    assumptions: list[Mapping[str, Any]],
    vars: Mapping[str, Any] | None = None,
    *,
    parents: tuple[str | None, str | None] | None = None,
) -> ModelScope:
    """One model's change scope; never raises."""

    model = PurePosixPath(path).stem
    try:
        prior = render_model_sql(prior_sql, naming_resolver, vars)
        authored = render_model_sql(authored_sql, naming_resolver, vars)
        prior_tree = sqlglot.parse_one(prior, dialect=dialect)
        authored_tree = sqlglot.parse_one(authored, dialect=dialect)
    except UnattributableError as exc:
        return ModelScope(
            model=model,
            path=path,
            population="unchecked",
            outputs="unchecked",
            reason=str(exc),
        )
    except sqlglot.errors.ParseError:
        return ModelScope(
            model=model,
            path=path,
            population="unchecked",
            outputs="unchecked",
            reason="the model's SQL could not be parsed for comparison",
        )

    changes: list[ScopeChange] = []
    unread_sets = changed_set_operations(
        sql_shape.scopes(prior_tree), sql_shape.scopes(authored_tree)
    )
    population, population_reason = _population(
        prior, authored, dialect, changes, unread_sets
    )
    if unread_sets:
        # Row attribution compares SELECT bodies only, so an edit inside a UNION
        # (a branch's WHERE) is unattributable there and must not read as
        # `kept` here. This one note names those scopes together, which is why
        # `_population` leaves row attribution's entries for them out.
        note = (
            f"the set operation in {', '.join(f'`{n}`' for n in unread_sets)} "
            "changed and its branches were not compared"
        )
        population_reason = "; ".join(r for r in (population_reason, note) if r)
        if population == "kept":
            population = "unchecked"
    outputs, outputs_reason = _outputs(prior_tree, authored_tree, changes)
    unread = outputs_reason is not None
    redriven = _redriven(prior_sql, authored_sql, dialect, vars, parents)
    if redriven:
        changes.extend(redriven)
        population = "changed"

    for change in changes:
        _settle(change, assumptions)
    # Undeclared first, so a cap never hides the change that needs a decision.
    # A redrive leads its status (#1228): it changes which entity the model is
    # about, so no per-model or completion cap may cut it before a formula.
    order = {"undeclared": 0, "declared": 1, None: 2}
    changes.sort(
        key=lambda c: (
            order[c.status],
            c.reason != "redriven",
            c.axis,
            c.scope,
            c.kind,
            c.column or "",
        )
    )
    undeclared = sum(1 for c in changes if c.status == "undeclared")
    elided = max(0, len(changes) - MAX_CHANGES_PER_MODEL)
    return ModelScope(
        model=model,
        path=path,
        population=population,
        outputs=outputs,
        changes=changes[:MAX_CHANGES_PER_MODEL],
        undeclared=undeclared,
        reason="; ".join(r for r in (population_reason, outputs_reason) if r) or None,
        outputs_unread=True if unread else None,
        elided=elided,
    )


# --- population ----------------------------------------------------------------


def _population(
    prior: str,
    authored: str,
    dialect: str,
    out: list[ScopeChange],
    unread_sets: Iterable[str] = (),
) -> tuple[str, str | None]:
    """Append each row-moving change row attribution classifies, and return the
    population state with the reason it could not be read, if any.

    Row attribution's entries for ``unread_sets`` are skipped: the caller
    reports those scopes itself.
    """

    isolable, unresolved = classify(prior, authored, dialect)
    skip = set(unread_sets)
    unresolved = [u for u in unresolved if u.scope not in skip]
    out.extend(
        ScopeChange(
            axis="population",
            kind=change.kind,
            scope=change.scope,
            detail=change.detail,
            prior=change.prior,
            authored=change.authored,
            columns=_columns_of(change.prior, change.authored, dialect),
        )
        for change in isolable
    )
    if isolable:
        return "changed", None
    if unresolved:
        return "unchecked", "; ".join(sorted({u.detail for u in unresolved}))
    return "kept", None


def _parent_moved(parents: tuple[str | None, str | None] | None) -> bool:
    if parents is None:
        return False
    before, after = parents
    return before is not None and (after or "").lower() != before.lower()


def _redriven(
    prior_sql: str,
    authored_sql: str,
    dialect: str,
    vars: Mapping[str, Any] | None,
    parents: tuple[str | None, str | None] | None,
) -> list[ScopeChange]:
    """The re-driving changes between two versions of a model (#1228): the set
    of relations its rows come from, read as drafting reads it (the FROM chain
    through CTEs, plus what a RIGHT, FULL or CROSS join keeps), and a declared
    parent removed or replaced. A version whose drivers cannot be read reports
    nothing here; row attribution names what it could not isolate."""

    from .drafting import read

    out: list[ScopeChange] = []
    try:
        before = read(prior_sql, None, dialect=dialect, vars=vars)
        after = read(authored_sql, None, dialect=dialect, vars=vars)
    except Exception:
        before = after = None
    if (
        before is not None
        and after is not None
        and before.driving
        and after.driving
        and before.driving_unread is None
        and after.driving_unread is None
        and {d.lower() for d in before.driving} != {d.lower() for d in after.driving}
    ):
        prior = ", ".join(before.driving)
        authored = ", ".join(after.driving)
        out.append(
            ScopeChange(
                axis="population",
                kind="redriven",
                scope="model",
                detail=(
                    f"the relations the model's rows come from changed from "
                    f"`{prior}` to `{authored}`, which changes which entity it "
                    "is about"
                ),
                prior=prior,
                authored=authored,
                reason="redriven",
            )
        )
    if parents is not None and _parent_moved(parents):
        was, now = parents
        out.append(
            ScopeChange(
                axis="population",
                kind="redriven",
                scope="model",
                detail=(
                    f"the declared population.parent `{was}` was "
                    + (f"replaced by `{now}`" if now else "removed")
                ),
                prior=was,
                authored=now,
                reason="redriven",
            )
        )
    return out


def _columns_of(prior: str | None, authored: str | None, dialect: str) -> list[str]:
    """The column names a change's prior and authored fragments read, lowercased."""

    names: set[str] = set()
    for fragment in (prior, authored):
        if not fragment:
            continue
        node = _parse_fragment(fragment, dialect)
        if node is None:
            continue
        names.update(c.name.lower() for c in node.find_all(exp.Column) if c.name)
    return sorted(names)


def _parse_fragment(fragment: str, dialect: str) -> exp.Expression | None:
    """A predicate or join label as an expression, or ``None`` when unreadable."""

    try:
        return sqlglot.parse_one(fragment, dialect=dialect)
    except sqlglot.errors.ParseError:
        pass
    # A join label (``left join x on a.k = b.k``): read its condition alone.
    lowered = fragment.lower()
    if " on " in lowered:
        try:
            return sqlglot.parse_one(
                fragment[lowered.index(" on ") + 4 :], dialect=dialect
            )
        except sqlglot.errors.ParseError:
            return None
    return None


# --- outputs -------------------------------------------------------------------


def _projections(select: exp.Expression) -> tuple[dict[str, exp.Expression], bool]:
    """Output name to expression for a SELECT, and whether it projects a star."""

    if not isinstance(select, exp.Select):
        return {}, True
    found: dict[str, exp.Expression] = {}
    star = False
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            star = True
            continue
        name = projection.alias_or_name
        if not name:
            continue
        body = projection.this if isinstance(projection, exp.Alias) else projection
        found[name.lower()] = body
    return found, star


def _outputs(
    prior: exp.Expression, authored: exp.Expression, out: list[ScopeChange]
) -> tuple[str, str | None]:
    """Append the removed, added and re-expressed output columns, and return the
    output state with the reason removals could not be read, if any. A star in
    the final select leaves removals unread while shared CTEs are still
    compared, so ``changed`` can carry a reason too."""

    before_scopes, after_scopes = sql_shape.scopes(prior), sql_shape.scopes(authored)
    reason: str | None = None
    changed = False

    main_before = before_scopes.get(sql_shape.MAIN_SCOPE)
    main_after = after_scopes.get(sql_shape.MAIN_SCOPE)
    before, star_before = (
        _projections(main_before) if main_before is not None else ({}, True)
    )
    after, star_after = (
        _projections(main_after) if main_after is not None else ({}, True)
    )
    if star_before or star_after:
        reason = (
            "the final select projects a star or a set operation, so which "
            "output columns were removed or added could not be read"
        )
    else:
        for name in sorted(before.keys() - after.keys()):
            changed = True
            out.append(
                ScopeChange(
                    axis="output",
                    kind="column_removed",
                    scope=sql_shape.MAIN_SCOPE,
                    column=name,
                    detail=f"the output column `{name}` is no longer produced",
                    prior=sql_shape.text(before[name]),
                )
            )
        for name in sorted(after.keys() - before.keys()):
            changed = True
            out.append(
                ScopeChange(
                    axis="output",
                    kind="column_added",
                    scope=sql_shape.MAIN_SCOPE,
                    column=name,
                    detail=f"the output column `{name}` is new",
                    authored=sql_shape.text(after[name]),
                )
            )

    for scope in sorted(before_scopes.keys() & after_scopes.keys()):
        left, _ = _projections(before_scopes[scope])
        right, _ = _projections(after_scopes[scope])
        for name in sorted(left.keys() & right.keys()):
            before_seen: list[tuple[exp.Select, bool]] = []
            after_seen: list[tuple[exp.Select, bool]] = []
            before_read = _through_ctes(
                left[name], before_scopes[scope], before_scopes, visited=before_seen
            )
            after_read = _through_ctes(
                right[name], after_scopes[scope], after_scopes, visited=after_seen
            )
            # The same text read through the same scopes is compared in each
            # of them; through differently named CTEs, only the trace sees a
            # change inside them.
            same_path = sql_shape.unqualified_key(left[name]) == (
                sql_shape.unqualified_key(right[name])
            ) and _scope_names(before_seen, before_scopes) == _scope_names(
                after_seen, after_scopes
            )
            read = (
                (before_read, after_read)
                if before_read is not None and after_read is not None and not same_path
                else None
            )
            same_rows = read is not None and _same_rows(
                before_seen, after_seen, before_scopes, after_scopes
            )
            for change in _expression_changes(
                scope, name, left[name], right[name], read, same_rows
            ):
                changed = True
                out.append(change)

    if changed:
        return "changed", reason
    return ("unchecked" if reason else "kept"), reason


def _scope_names(
    seen: list[tuple[exp.Select, bool]], scopes: Mapping[str, exp.Expression]
) -> list[str]:
    """The names of the scopes a trace reads, with every CTE its rows come
    from (``count(*)`` reads its FROM without naming a column)."""

    traced = [select for select, _ in seen]
    rows = _feeding(traced, scopes) or traced
    return sorted(name for select in rows for name, s in scopes.items() if s is select)


def _source(
    column: exp.Column,
    select: exp.Select,
    ctes: Mapping[str, exp.Expression],
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """The table column ``column`` reads in ``select``, as ``table.column``,
    followed through the plain-column projections of the CTEs on the way, so
    ``p.status`` and ``c.status`` stay two columns while a CTE's ``status``
    over the same table is the same one. ``None`` when that cannot be told:
    an unqualified name over several relations, a star, a table read twice
    in the select, a CTE that computes the column, or anything ``_cte_read``
    cannot read by name."""

    if isinstance(column.this, exp.Star):
        return None
    relations = _relations_of(select)
    qualifier = column.table.lower()
    if qualifier:
        relations = [r for r in relations if r.alias_or_name.lower() == qualifier]
    if len(relations) != 1:
        return None
    cte = _cte_read(relations[0], select, ctes)
    if cte is None:
        table = sql_shape.relation_key(relations[0])
        everything = _relations_of(select)
        if sum(sql_shape.relation_key(r) == table for r in everything) > 1:
            return None  # A self-join's two sides read alike by table.
        return f"{table}.{column.name.lower()}"
    if not cte or cte in seen:
        return None
    computed = _projections(ctes[cte])[0].get(column.name.lower())
    if not isinstance(computed, exp.Column):
        return None
    return _source(computed, ctes[cte], ctes, seen | {cte})


def _row_shape(
    selects: list[exp.Select],
    aggregating: list[exp.Select],
    ctes: Mapping[str, exp.Expression],
) -> tuple[object, ...] | None:
    """What decides which rows reach a traced calculation, over every select
    the rows pass through: the WHERE conjuncts; the GROUP BY keys and HAVING
    conjuncts of each select that computes a traced aggregate, in trace
    order, apart from those of the other selects; the columns each row
    DISTINCT compares;
    each join's type and condition; and the tables read, counted, since a
    second copy of a table fans rows out. ``None`` when a column in any of
    them cannot be resolved to its table (``_source``), or the way holds a
    subquery, a ``USING`` or ``NATURAL`` join, a ``GROUP BY ALL``, ``CUBE``,
    ``ROLLUP`` or ``GROUPING SETS``, a name that is also a projection alias
    for something else, an ordinal beside a star, a ``DISTINCT ON``, a
    ``DISTINCT`` over a star, a table or CTE read more than once, or an
    aggregate or ``HAVING`` in a select the trace does not compute in.

    Columns are compared as the tables they read, and columns an inner join
    or WHERE equates are one: grouping by ``p.project_id`` and by a CTE's
    ``project_id`` over parts agree when the rows are joined on
    ``c.project_id = p.project_id``."""

    merged: dict[str, frozenset[str]] = {}
    for select in selects:
        conditions = [join.args.get("on") for join in select.args.get("joins") or []]
        where = select.args.get("where")
        conditions.append(where.this if where is not None else None)
        for condition in conditions:
            for conjunct in sql_shape.flatten_conjuncts(condition):
                left, right = conjunct.this, conjunct.expression
                if not (
                    isinstance(conjunct, exp.EQ)
                    and isinstance(left, exp.Column)
                    and isinstance(right, exp.Column)
                ):
                    continue
                a, b = _source(left, select, ctes), _source(right, select, ctes)
                if a and b:
                    both = merged.get(a, frozenset({a})) | merged.get(b, frozenset({b}))
                    merged.update(dict.fromkeys(both, both))

    unresolved: list[exp.Expression] = []

    def key(node: exp.Expression, select: exp.Select) -> str:
        """``node`` with each column replaced by the columns it equals; a
        node that cannot be resolved is added to ``unresolved``."""
        if node.find(exp.Select) is not None:
            unresolved.append(node)
            return ""
        # A name that is also an alias for another expression can mean either
        # (DuckDB reads aliases in WHERE, GROUP BY and HAVING).
        aliases = {
            p.alias.lower()
            for p in select.expressions
            if isinstance(p, exp.Alias)
            and not (
                isinstance(p.this, exp.Column)
                and p.this.name.lower() == p.alias.lower()
            )
        }
        copy = node.copy()
        for column in list(copy.find_all(exp.Column)):
            found = _source(column, select, ctes)
            if found is None or (not column.table and column.name.lower() in aliases):
                unresolved.append(node)
                return ""
            same = exp.var("{" + ",".join(sorted(merged.get(found, {found}))) + "}")
            if column is copy:
                copy = same
            else:
                column.replace(same)
        return sql_shape.unqualified_key(copy)

    def grouped_by(node: exp.Expression, select: exp.Select) -> exp.Expression:
        """What a GROUP BY key groups by: an ordinal is its projection, unless
        a star in the select list hides which one that is."""
        projections = select.expressions
        bare = node
        while isinstance(bare, exp.Paren):
            bare = bare.this
        if isinstance(bare, exp.Literal) and bare.is_int:
            position = int(bare.this) - 1
            stars = any(
                isinstance(p, exp.Star)
                or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                for p in projections
            )
            if not stars and 0 <= position < len(projections):
                return projections[position].unalias()
            unresolved.append(node)
        return node

    row_filters: set[str] = set()
    # Grouping and HAVING where a traced aggregate is computed, per select,
    # apart from those of every other select on the way.
    at_aggregate: dict[int, tuple[tuple[str, ...], ...]] = {}
    elsewhere: set[tuple[str, str]] = set()
    distincts: set[tuple[str, ...]] = set()
    joins: Counter[str] = Counter()
    tables: Counter[str] = Counter()
    reads: Counter[str] = Counter()
    for select in selects:
        condition = select.args.get("where")
        if condition is not None:
            row_filters.update(
                key(c, select) for c in sql_shape.flatten_conjuncts(condition.this)
            )
        stage: dict[str, list[str]] = {"group": [], "having": []}
        condition = select.args.get("having")
        if condition is not None:
            stage["having"] = [
                key(c, select) for c in sql_shape.flatten_conjuncts(condition.this)
            ]
        group = select.args.get("group")
        if group is not None:
            if any(
                group.args.get(k) for k in ("all", "cube", "rollup", "grouping_sets")
            ):
                unresolved.append(group)
            stage["group"] = [
                key(grouped_by(k, select), select) for k in group.expressions
            ]
        if any(select is s for s in aggregating):
            at_aggregate[id(select)] = tuple(tuple(sorted(v)) for v in stage.values())
        elif stage["having"] or any(
            p.find(exp.AggFunc) is not None for p in select.expressions
        ):
            # An aggregate the trace does not read still collapses the rows it
            # passes on: `select max(x) from t` feeds `count(*)` one row.
            unresolved.append(select)
        else:
            elsewhere.update((clause, k) for clause, ks in stage.items() for k in ks)
        distinct = select.args.get("distinct")
        if distinct is not None:
            # DISTINCT ON keeps one row per key, chosen by ORDER BY; a star
            # compares columns no name here lists.
            if distinct.args.get("on") is not None or any(
                isinstance(c, exp.Star) for c in select.expressions
            ):
                unresolved.append(distinct)
            distincts.add(
                tuple(sorted(key(c.unalias(), select) for c in select.expressions))
            )
        for join in select.args.get("joins") or []:
            if join.args.get("using") or join.method:
                return None
            on = join.args.get("on")
            joins[f"{join.side} {join.kind} on {key(on, select) if on else ''}"] += 1
        for relation in _relations_of(select):
            reads[sql_shape.relation_key(relation)] += 1
            if _cte_read(relation, select, ctes) is None:
                tables[sql_shape.relation_key(relation)] += 1
    # A table or CTE read twice (a self-join, also through CTEs) leaves which
    # copy a column comes from unknown.
    if unresolved or any(n > 1 for n in reads.values()):
        return None
    return (
        frozenset(row_filters),
        # In trace order, so a HAVING stays bound to the aggregate it filters.
        tuple(at_aggregate[id(s)] for s in aggregating),
        frozenset(elsewhere),
        frozenset(distincts),
        joins,
        tables,
    )


def _relations_of(select: exp.Select) -> list[exp.Expression]:
    """The relations a select reads directly: its FROM and each join."""

    from_ = select.args.get("from_") or select.args.get("from")
    relations = [from_.this] if from_ is not None else []
    return relations + [join.this for join in select.args.get("joins") or []]


def _same_rows(
    prior: list[tuple[exp.Select, bool]],
    authored: list[tuple[exp.Select, bool]],
    prior_ctes: Mapping[str, exp.Expression],
    authored_ctes: Mapping[str, exp.Expression],
) -> bool:
    """Whether the rows each side's calculation reads are produced alike, so a
    moved aggregate is the same calculation (#1253): the same filters, the
    same grouping where the aggregate is computed and elsewhere, the same row
    DISTINCTs and joins on both paths, the same tables read as often, and no
    outer join, LIMIT or OFFSET on either path (an outer join changes what
    ``count(*)`` sees for a row with no match). ``prior`` and ``authored`` are
    each trace's selects with whether its expression aggregates there.
    Otherwise the expressions are compared as written."""

    prior_rows = _feeding([s for s, _ in prior], prior_ctes)
    authored_rows = _feeding([s for s, _ in authored], authored_ctes)
    if prior_rows is None or authored_rows is None:
        return False
    before = _row_shape(prior_rows, _aggregating(prior), prior_ctes)
    after = _row_shape(authored_rows, _aggregating(authored), authored_ctes)
    return before is not None and before == after


def _aggregating(seen: list[tuple[exp.Select, bool]]) -> list[exp.Select]:
    """The selects where a trace's expression aggregates, each once, in trace
    order: ``s / c`` over one CTE computing both is one stage, not two."""

    found: list[exp.Select] = []
    for select, aggregates in seen:
        if aggregates and not any(select is known for known in found):
            found.append(select)
    return found


def _feeding(
    selects: list[exp.Select], ctes: Mapping[str, exp.Expression]
) -> list[exp.Select] | None:
    """``selects`` and every CTE their rows come from, followed through FROM
    and JOIN: a filter or DISTINCT upstream shapes the rows even when no traced
    column reads it, as with ``count(*)``. ``None`` when a select on the way
    reads anything but tables and the CTEs it can see (a derived table,
    ``VALUES``, an ``UNNEST``, a nested ``WITH``'s own CTE, a CTE defined
    after it), joins with an outer join, limits or samples its rows, is a set
    operation, expands rows in its select list (``unnest``), or holds
    something whose result depends on where a filter sits or on row order: a
    window function, a ``QUALIFY``, or an ``ORDER BY`` in a CTE (an ordered
    aggregate downstream reads it)."""

    main = ctes.get(sql_shape.MAIN_SCOPE)
    found: list[exp.Select] = []
    stack = list(selects)
    while stack:
        select = stack.pop()
        if any(select is known for known in found):
            continue
        if any(
            select.args.get(key)
            for key in ("limit", "offset", "fetch", "qualify", "sample")
        ):
            return None
        if select is not main and select.args.get("order"):
            return None
        if any(
            p.find(exp.Window, *_ROW_EXPANDING) is not None for p in select.expressions
        ):
            return None
        if any(join.side for join in select.args.get("joins") or []):
            return None
        if any(r.args.get("sample") for r in _relations_of(select)):
            return None
        found.append(select)
        for relation in _relations_of(select):
            cte = _cte_read(relation, select, ctes)
            if cte == "":
                return None
            if cte is not None:
                body = ctes[cte]
                if not isinstance(body, exp.Select):
                    return None
                stack.append(body)
    return found


def _resolves_by_name(tree: exp.Expression) -> bool:
    """Whether every name in ``tree`` can be matched by its lowercased text,
    which ``_through_ctes`` relies on (#1253). Not when an identifier is
    quoted (``"N"`` and ``n`` are two names), a pivot rewrites the columns, a
    named window carries part of a calculation, the ``WITH`` is recursive, or
    two CTE names fold to the same text."""

    if any(identifier.quoted for identifier in tree.find_all(exp.Identifier)):
        return False
    if tree.find(exp.Pivot) is not None:
        return False
    if any(select.args.get("windows") for select in tree.find_all(exp.Select)):
        return False
    top = tree.args.get("with_") or tree.args.get("with")
    if top is not None:
        if top.args.get("recursive"):
            return False
        names = [cte.alias_or_name.lower() for cte in top.expressions]
        if len(names) != len(set(names)):
            return False
    return True


def _cte_read(
    relation: exp.Expression, select: exp.Select, ctes: Mapping[str, exp.Expression]
) -> str | None:
    """The top-level CTE a relation of ``select`` reads by name, ``None`` for
    a table, or ``""`` when its columns cannot be told by name: a derived
    table, columns renamed by position (``a(n, x)`` or ``from t as o(n, x)``),
    a nested ``WITH``'s own CTE, or a CTE ``select`` cannot see."""

    alias = relation.args.get("alias")
    if not isinstance(relation, exp.Table) or (alias is not None and alias.columns):
        return ""
    name = relation.name.lower()
    # A CTE body's own WITH binds names that shadow the top-level ones.
    nested = select.args.get("with_") or select.args.get("with")
    if (
        nested is not None
        and select is not ctes.get(sql_shape.MAIN_SCOPE)
        and name in {item.alias_or_name.lower() for item in nested.expressions}
    ):
        return ""
    names = [key for key in ctes if key != sql_shape.MAIN_SCOPE]
    if relation.db or name not in names:
        return None
    # A non-recursive CTE sees only the ones defined before it; the final
    # select sees them all. A later name read inside a CTE is a table.
    position = next((i for i, key in enumerate(names) if ctes[key] is select), None)
    definition = ctes[name].find_ancestor(exp.CTE)
    if name not in (names if position is None else names[:position]) or (
        definition is not None and definition.alias_column_names
    ):
        return ""
    return name


def _through_ctes(
    node: exp.Expression,
    select: exp.Expression,
    ctes: Mapping[str, exp.Expression],
    seen: frozenset[str] = frozenset(),
    visited: list[tuple[exp.Select, bool]] | None = None,
) -> exp.Expression | None:
    """``node``, read in ``select``, with every column that reads a top-level
    CTE's output replaced by the expression that CTE computes it with, followed
    through further CTEs (#1253), so an aggregate moved into a CTE is compared
    as the aggregate it still is. Each column of a table keeps the table
    column it reads (``_source``) in its ``meta``, for ``_identity``.

    ``None`` when a column cannot be traced: a query whose names do not resolve
    by lowercased text (``_resolves_by_name``), an unqualified name over several
    relations, a qualifier the select does not read, a table read twice in the
    select, a derived table, columns renamed by position (``a(n, x)`` or
    ``from t as o(n, x)``), a CTE the current one cannot see (a later or nested
    definition), a CTE that does not project the name, a CTE reached again, or
    a subquery in ``node``. An untraced expression is
    compared as written, never as proven equivalent. ``visited`` collects
    each select the trace reads, with whether the expression read there
    aggregates."""

    if not isinstance(select, exp.Select) or node.find(exp.Select) is not None:
        return None
    main = ctes.get(sql_shape.MAIN_SCOPE)
    if main is None or not _resolves_by_name(main):
        return None
    if visited is not None:
        visited.append((select, node.find(exp.AggFunc) is not None))
    # The name each relation's columns qualify it with, to the CTE it reads.
    reads = {
        relation.alias_or_name.lower(): _cte_read(relation, select, ctes)
        for relation in _relations_of(select)
    }

    out = node.copy()
    for column in list(out.find_all(exp.Column)):
        if isinstance(column.this, exp.Star):
            return None
        qualifier = column.table.lower()
        if qualifier:
            if qualifier not in reads:
                return None
            cte = reads[qualifier]
        elif len(reads) == 1:
            cte = next(iter(reads.values()))
        else:
            return None
        if cte is None:
            # A table's column is compared as that table's (`_identity`):
            # `sum(a.price)` and `sum(b.price)` over one join are two sums.
            source = _source(column, select, ctes)
            if source is None:
                return None
            column.meta["source"] = source
            continue
        if not cte or cte in seen:
            return None
        computed = _projections(ctes[cte])[0].get(column.name.lower())
        if computed is None:
            return None
        traced = _through_ctes(computed, ctes[cte], ctes, seen | {cte}, visited)
        if traced is None:
            return None
        if column is out:
            out = traced
        elif (
            isinstance(column.parent, (exp.Binary, exp.Unary))
            and not isinstance(column.parent, exp.Paren)
            and not isinstance(traced, (exp.Column, exp.Literal, exp.Func))
        ):
            # Parentheses only where an operator needs them: `sum(subtotal)`
            # traced to `sum(price * quantity)` must read as the same aggregate,
            # and `s / c` traced to `sum(price) / count(*)` as the same ratio.
            column.replace(exp.Paren(this=traced))
        else:
            column.replace(traced)
    return out


def _identity(node: exp.Expression) -> exp.Expression:
    """A traced expression with each table column replaced by the table column
    it reads, so the same name from two tables compares as two; an expression
    read as written comes back unchanged."""

    out = node.copy()
    for column in list(out.find_all(exp.Column)):
        source = column.meta.get("source")
        if source:
            if column is out:
                return exp.var(source)
            column.replace(exp.var(source))
    return out


def _cast_target(node: exp.Expression) -> str | None:
    """The type an expression is cast to at its outermost level, if it is cast."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return sql_shape.match_key(node.args.get("to"))
    return None


def _aggregates(node: exp.Expression) -> Counter[str]:
    """Each aggregate in an expression as ``func([distinct ]arg)``, counted, with
    ``*`` for a bare ``count(*)``."""

    found: Counter[str] = Counter()
    for agg in node.find_all(exp.AggFunc):
        arg = agg.this
        distinct = isinstance(arg, exp.Distinct)
        inner = (arg.expressions[0] if arg.expressions else None) if distinct else arg
        star = inner is None or isinstance(inner, exp.Star)
        shape = "*" if star else sql_shape.unqualified_key(inner)
        found[f"{agg.key}({'distinct ' if distinct else ''}{shape})"] += 1
    return found


def _null_handling(node: exp.Expression) -> Counter[str]:
    """The NULL-handling functions in an expression, counted by kind."""

    found: Counter[str] = Counter()
    for func in node.find_all(exp.Func):
        name = (func.sql_name() or "").lower()
        if isinstance(func, exp.Coalesce) or name in _NULL_FUNCTIONS:
            found["nullif" if isinstance(func, exp.Nullif) else "coalesce"] += 1
    return found


def _expression_changes(
    scope: str,
    name: str,
    prior: exp.Expression,
    authored: exp.Expression,
    read: tuple[exp.Expression, exp.Expression] | None = None,
    same_rows: bool = False,
) -> list[ScopeChange]:
    """How one column's expression changed: a cast, an aggregate, a NULL
    treatment, or, failing all three, the expression itself.

    ``read`` is both expressions traced through the CTEs they read, when both
    could be, and ``same_rows`` whether their rows are produced alike
    (``_same_rows``). When they are, casts, aggregates and NULL treatment are
    compared on the traced pair, so an aggregate that only moved into a CTE is
    not an aggregate change, and a cast or ``coalesce`` added inside the CTE
    still is one (#1253); otherwise a changed text is compared as written. An
    unchanged text that reads differently named CTEs is compared on the traced
    pair, and is a change when the calculations differ, as when ``n`` reads a
    CTE that now computes it another way; which rows reach it is population's
    to report."""

    same_text = sql_shape.unqualified_key(prior) == sql_shape.unqualified_key(authored)
    same_trace = read is not None and sql_shape.unqualified_key(
        _identity(read[0])
    ) == sql_shape.unqualified_key(_identity(read[1]))
    if same_text and (read is None or same_trace):
        return []
    shown = (sql_shape.text(prior), sql_shape.text(authored))
    where = "" if scope == sql_shape.MAIN_SCOPE else f" in `{scope}`"

    def change(kind: str, detail: str) -> ScopeChange:
        """One output change on this column, with both expressions shown."""
        return ScopeChange(
            axis="output",
            kind=kind,
            scope=scope,
            column=name,
            detail=detail,
            prior=shown[0],
            authored=shown[1],
        )

    found: list[ScopeChange] = []
    traced = (
        read if read is not None and (same_rows or same_text) else (prior, authored)
    )
    before_cast, after_cast = _cast_target(traced[0]), _cast_target(traced[1])
    if before_cast != after_cast:
        found.append(
            change(
                "type_changed",
                f"`{name}`{where} is cast to {after_cast or 'no type'} "
                f"where it was cast to {before_cast or 'no type'}",
            )
        )
    before_aggs, after_aggs = _aggregates(traced[0]), _aggregates(traced[1])
    resolved = _aggregates(_identity(traced[0])), _aggregates(_identity(traced[1]))
    if resolved[0] != resolved[1]:
        if before_aggs == after_aggs:
            # Only the table an operand reads differs: name it.
            before_aggs, after_aggs = resolved
        found.append(
            change(
                "aggregate_changed",
                f"`{name}`{where} aggregates "
                f"{', '.join(sorted(after_aggs)) or 'nothing'} where it aggregated "
                f"{', '.join(sorted(before_aggs)) or 'nothing'}",
            )
        )
    before_nulls, after_nulls = _null_handling(traced[0]), _null_handling(traced[1])
    if before_nulls != after_nulls:
        found.append(
            change(
                "null_handling_changed",
                f"`{name}`{where} changes its NULL treatment "
                f"({_describe(before_nulls)} before, {_describe(after_nulls)} now)",
            )
        )
    if not found:
        how = (
            "reads the same calculation through a CTE"
            if same_trace and same_rows
            else "is computed differently"
        )
        found.append(change("expression_changed", f"`{name}`{where} {how}"))
    return found


def _describe(found: Counter[str]) -> str:
    """A counted set of functions as words, ``none`` when empty."""

    if not found:
        return "none"
    return ", ".join(f"{n} x {k}" if n > 1 else k for k, n in sorted(found.items()))


# --- settling ------------------------------------------------------------------


def _settle(change: ScopeChange, assumptions: list[Mapping[str, Any]]) -> None:
    """Mark a judged change ``declared`` by the first assumption naming a column it
    governs, copying that assumption, or ``undeclared``."""

    if change.axis == "output" and change.kind not in JUDGED:
        return
    if change.reason == "redriven":
        _settle_redriven(change, assumptions)
        return
    if change.axis == "population":
        names = set(change.columns or [])
    else:
        names = {(change.column or "").lower()} - {""}
    for assumption in assumptions:
        column = assumption.get("column")
        if names:
            if not isinstance(column, str):
                continue
            if column.strip().lower().rsplit(".", 1)[-1] not in names:
                continue
        # A population change whose predicate names no readable column (a
        # swapped driving relation) is settled by any assumption on the model.
        change.status = "declared"
        change.assumption = {
            k: assumption.get(k)
            for k in ("decision", "chosen", "evidence", "column")
            if assumption.get(k) is not None
        }
        if assumption.get("evidence") == "default":
            change.note = (
                "recorded as a `default`: a conventional choice, not the "
                "business's rule"
            )
        return
    change.status = "undeclared"


#: The tiers that settle re-driving a model: which entity a model is about is the
#: request's or a contract's to say, never the data's (#1228).
REDRIVE_TIERS = ("request", "contract")


def _settle_redriven(change: ScopeChange, assumptions: list[Mapping[str, Any]]) -> None:
    """``declared`` by the first assumption on the model at ``request`` or
    ``contract`` tier, whatever column it names, since a swapped driver names
    none; otherwise ``undeclared``, saying what a lower tier cannot settle."""

    for assumption in assumptions:
        if assumption.get("evidence") in REDRIVE_TIERS:
            change.status = "declared"
            change.assumption = {
                k: assumption.get(k)
                for k in ("decision", "chosen", "evidence", "column")
                if assumption.get(k) is not None
            }
            return
    change.status = "undeclared"
    lower = sorted(
        {
            str(a.get("evidence"))
            for a in assumptions
            if a.get("evidence") and a.get("evidence") not in REDRIVE_TIERS
        }
    )
    change.note = (
        "re-driving a model is settled only by an assumption at request or "
        "contract tier; observed counts never settle it, and NULL or zero "
        "aggregates on a parent-driven model point at the join key, not at a new "
        "driving relation"
        + (
            f" (this model's assumptions are at {', '.join(lower)} tier)"
            if lower
            else ""
        )
    )


# --- envelope helpers ----------------------------------------------------------


def warnings(report: Mapping[str, Any] | None) -> list[str]:
    """One sentence per model with an undeclared change."""

    out: list[str] = []
    for entry in (report or {}).get("models") or []:
        undeclared = [
            c for c in entry.get("changes") or [] if c.get("status") == "undeclared"
        ]
        if not undeclared:
            continue
        parts = [c["detail"] for c in undeclared[:4]]
        more = max(len(undeclared), entry.get("undeclared", 0)) - len(parts)
        if more > 0:
            parts.append(f"{more} more")
        out.append(
            f"'{entry['model']}': this plan changes more than the formula "
            f"({'; '.join(parts)}), and no assumption names the column it "
            "governs. If the request asks for it, record an assumption naming that "
            "`column`, with the evidence that settles it; otherwise undo it "
            "(data.change_scope)"
        )
    return out


def stored_form(report: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Per model, the judged changes as the plan records them for a build."""

    out: dict[str, list[dict[str, Any]]] = {}
    for entry in (report or {}).get("models") or []:
        judged = [
            {
                k: v
                for k, v in (
                    ("kind", c.get("kind")),
                    ("column", c.get("column")),
                    ("status", c.get("status")),
                    ("reason", c.get("reason")),
                )
                if v is not None
            }
            for c in entry.get("changes") or []
            if c.get("status") is not None
        ]
        # Either axis left unread is unchecked, never clean: a final `select *`
        # whose removals could not be read is as unfinished as an unrenderable
        # model, even where a CTE change made `outputs` read `changed`.
        if "unchecked" in (entry.get("population"), entry.get("outputs")) or entry.get(
            "outputs_unread"
        ):
            judged.append({"kind": "unread", "status": "unchecked"})
        # Changes the per-model cap cut were never judged here.
        if entry.get("elided"):
            judged.append({"kind": "elided", "status": "unchecked"})
        if judged:
            out[entry["model"]] = judged
    for model in (report or {}).get("beyond_cap") or []:
        out[model] = [{"kind": "elided", "status": "unchecked"}]
    return out
