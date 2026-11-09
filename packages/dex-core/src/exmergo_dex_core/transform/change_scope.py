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
``column`` names a column it governs, ``undeclared`` otherwise. Nothing here
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


def analyze(
    edits: Iterable[tuple[str, str, str]],
    assumptions: Iterable[Mapping[str, Any]],
    dialect: str,
    vars: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """``data.change_scope`` for ``(path, prior_sql, authored_sql)`` triples.

    ``assumptions`` are ``data.decisions`` entries (``model``, ``column``, ...).
    ``None`` when no edit rewrites an existing model.
    """

    by_model: dict[str, list[Mapping[str, Any]]] = {}
    for assumption in assumptions:
        model = assumption.get("model")
        if isinstance(model, str):
            by_model.setdefault(model.lower(), []).append(assumption)

    models: list[ModelScope] = []
    for path, prior_sql, authored_sql in edits:
        if prior_sql.strip() == authored_sql.strip():
            continue
        model = PurePosixPath(path).stem
        models.append(
            scope_for(
                path,
                prior_sql,
                authored_sql,
                dialect,
                by_model.get(model.lower(), []),
                vars,
            )
        )
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

    for change in changes:
        _settle(change, assumptions)
    # Undeclared first, so a cap never hides the change that needs a decision.
    order = {"undeclared": 0, "declared": 1, None: 2}
    changes.sort(
        key=lambda c: (order[c.status], c.axis, c.scope, c.kind, c.column or "")
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
            for change in _expression_changes(scope, name, left[name], right[name]):
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
