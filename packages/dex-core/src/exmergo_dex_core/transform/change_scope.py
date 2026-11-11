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


def _cte_output_map(
    scopes: Mapping[str, exp.Expression], exclude: set[str]
) -> dict[str, tuple[dict[str, exp.Expression], bool]]:
    """Mappable outputs of the CTEs in ``scopes`` that ``exclude`` does not name.

    Only CTEs outside ``exclude`` are followed: a CTE both versions share has
    its own comparison in the scope loop, and resolving through it here would
    report the same change twice. A CTE body that is not a plain SELECT (a set
    operation) maps to no outputs and a star, so a reference to it is left as
    written rather than guessed at.
    """

    found: dict[str, tuple[dict[str, exp.Expression], bool]] = {}
    for name, body in scopes.items():
        if name == sql_shape.MAIN_SCOPE or name in exclude:
            continue
        found[name] = _projections(body)
    return found


def _cte_qualifiers(select: exp.Expression, cte_names: set[str]) -> dict[str, str]:
    """The qualifier a select uses for each CTE it reads, to the CTE it names.

    ``from p as q`` reads the CTE ``p`` under the qualifier ``q``; ``from p``
    reads it under ``p``. Only relations that are CTEs of this query are
    mapped, so a qualifier that names a real table never resolves here.
    """

    mapping: dict[str, str] = {}
    if not isinstance(select, exp.Select):
        return mapping
    relations: list[exp.Expression] = []
    driving = sql_shape.from_relation(select)
    if driving is not None:
        relations.append(driving)
    relations.extend(join.relation for join in sql_shape.joins(select))
    for relation in relations:
        if not isinstance(relation, exp.Table):
            continue
        underlying = relation.name.lower()
        if underlying not in cte_names:
            continue
        mapping[(relation.alias or underlying).lower()] = underlying
    return mapping


def _resolve_cte_references(
    expression: exp.Expression,
    cte_outputs: Mapping[str, tuple[dict[str, exp.Expression], bool]],
    qualifiers: Mapping[str, str],
    scopes: Mapping[str, exp.Expression],
    cte_names: set[str],
    seen: frozenset[str] = frozenset(),
) -> exp.Expression:
    """``expression`` with its references to CTE outputs replaced by the
    expressions those outputs compute, following chains of CTEs.

    Only references to CTEs in ``cte_outputs`` are followed. A reference is
    read in the context of the select that wrote it: a column of the final
    select under ``qualifiers``, and a column of a CTE's own output under that
    CTE's qualifiers, so a chain through more than one CTE resolves each step
    where it is defined. A column inside a nested select belongs to that
    select, not to this one, so it is never substituted. A reference that
    cannot be resolved, a bare name two CTEs produce, or a chain that revisits
    a CTE returns the expression as written: an unresolved expression is never
    presented as proven equivalent (#1253).
    """

    resolved = expression.copy()
    for column in list(resolved.find_all(exp.Column)):
        if column.find_ancestor(exp.Select, exp.Subquery) is not None:
            continue
        qualifier = column.table.lower()
        name = column.name.lower()
        if qualifier:
            cte = qualifiers.get(qualifier)
            if cte is None or cte not in cte_outputs:
                continue
            outputs, star = cte_outputs[cte]
            if name not in outputs:
                if star:
                    continue
                return expression
        else:
            candidates = [
                cte
                for cte in dict.fromkeys(qualifiers.values())
                if cte in cte_outputs and name in cte_outputs[cte][0]
            ]
            if not candidates:
                continue
            if len(candidates) > 1:
                return expression
            cte = candidates[0]
            outputs = cte_outputs[cte][0]
        if cte in seen:
            return expression
        replacement = _resolve_cte_references(
            outputs[name],
            cte_outputs,
            _cte_qualifiers(scopes[cte], cte_names),
            scopes,
            cte_names,
            seen | {cte},
        )
        if replacement is outputs[name]:
            return expression
        if column is resolved:
            resolved = replacement
        else:
            column.replace(replacement)
    return resolved


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

    shared_ctes = (set(before_scopes) & set(after_scopes)) - {sql_shape.MAIN_SCOPE}
    prior_ctes = _cte_output_map(before_scopes, shared_ctes)
    authored_ctes = _cte_output_map(after_scopes, shared_ctes)
    prior_names = set(before_scopes) - {sql_shape.MAIN_SCOPE}
    authored_names = set(after_scopes) - {sql_shape.MAIN_SCOPE}

    for scope in sorted(before_scopes.keys() & after_scopes.keys()):
        left, _ = _projections(before_scopes[scope])
        right, _ = _projections(after_scopes[scope])
        prior_qualifiers = _cte_qualifiers(before_scopes[scope], prior_names)
        authored_qualifiers = _cte_qualifiers(after_scopes[scope], authored_names)
        for name in sorted(left.keys() & right.keys()):
            # Follow an output alias through a CTE that only one version has
            # before comparing what it aggregates, so moving a calculation
            # reads as a move and changing it still reads as a change (#1253).
            prior_expr = _resolve_cte_references(
                left[name], prior_ctes, prior_qualifiers, before_scopes, prior_names
            )
            authored_expr = _resolve_cte_references(
                right[name],
                authored_ctes,
                authored_qualifiers,
                after_scopes,
                authored_names,
            )
            for change in _expression_changes(scope, name, prior_expr, authored_expr):
                changed = True
                out.append(change)

    if changed:
        return "changed", reason
    return ("unchecked" if reason else "kept"), reason


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
    scope: str, name: str, prior: exp.Expression, authored: exp.Expression
) -> list[ScopeChange]:
    """How one column's expression changed: a cast, an aggregate, a NULL
    treatment, or, failing all three, the expression itself."""

    if sql_shape.unqualified_key(prior) == sql_shape.unqualified_key(authored):
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
    before_cast, after_cast = _cast_target(prior), _cast_target(authored)
    if before_cast != after_cast:
        found.append(
            change(
                "type_changed",
                f"`{name}`{where} is cast to {after_cast or 'no type'} "
                f"where it was cast to {before_cast or 'no type'}",
            )
        )
    before_aggs, after_aggs = _aggregates(prior), _aggregates(authored)
    if before_aggs != after_aggs:
        found.append(
            change(
                "aggregate_changed",
                f"`{name}`{where} aggregates "
                f"{', '.join(sorted(after_aggs)) or 'nothing'} where it aggregated "
                f"{', '.join(sorted(before_aggs)) or 'nothing'}",
            )
        )
    before_nulls, after_nulls = _null_handling(prior), _null_handling(authored)
    if before_nulls != after_nulls:
        found.append(
            change(
                "null_handling_changed",
                f"`{name}`{where} changes its NULL treatment "
                f"({_describe(before_nulls)} before, {_describe(after_nulls)} now)",
            )
        )
    if not found:
        found.append(
            change("expression_changed", f"`{name}`{where} is computed differently")
        )
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
