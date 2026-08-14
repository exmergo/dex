"""Which authored change moved which rows, answered at plan time.

Validation proves an edit is well formed against the schema. It says nothing
about behaviour, so the class of change that alters *which rows enter a model*
passes silently: the model compiles, the columns are right, every value is
plausible, and the row population is different from what it was. On a fix or
refactor ticket that is the failure mode, because the caller was asked to change
some behaviour and not other behaviour, and nothing distinguishes the two.

A whole-model row delta cannot separate the two either, since a ticket routinely
*requires* a row-population change. Attribution is the thing that helps: name the
individual predicate, join, or grain change, and measure each one against the
prior model as a common baseline.

Two halves, gated differently and deliberately. Naming a change is static, free,
and needs no connection, so it always runs. Measuring one is a ``COUNT``
aggregate against relations the model already reads, which is free on DuckDB and
spend everywhere else, so counting runs automatically on a free connector and
only on request elsewhere, priced through the ordinary handshake. Neither half
ever refuses: changing the filter is sometimes exactly the job, and the point is
that it is visible while the caller can still act on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import expressions as exp

from ..adapters import get_dialect, is_free_connector
from ..cache import DexCache, match_identifier
from ..dbt_project import EditOp
from ..dbt_project import load as load_project
from ..errors import DexError
from ..guards.query_firewall import QueryRefusedError, inspect_query
from ..storage import readable_cache
from .plans import EditKind

if TYPE_CHECKING:
    from ..adapters.base import Adapter
    from ..engine import DexEngine
    from ..results import ConfirmationRequest
    from .plans import PlanEdit

# Bounds on how much of one plan gets measured. Both are stated in a warning
# whenever they bind: a capped run that reported nothing about the cap would read
# as "everything was covered", which is the one thing attribution must not imply.
MAX_CHANGES_PER_MODEL = 6
MAX_MODELS = 5


class RowPopulationChange(BaseModel):
    """One authored change that can move rows, and what it moved.

    Shaped after :class:`~..maintain.drift.DriftFinding` on purpose, down to its
    honesty flag: ``attributed`` is False when dex could not isolate or measure
    this change, and ``reason`` says why. A change dex cannot speak for says so
    rather than being dropped, because a silently shorter list is indistinguishable
    from a clean edit.

    ``prior`` and ``authored`` carry SQL fragments (a predicate, a join clause),
    never a data value: the counts are aggregates and nothing row-level crosses
    the envelope.
    """

    scope: str
    kind: str
    detail: str
    prior: str | None = None
    authored: str | None = None
    rows: int | None = None
    delta: int | None = None
    attributed: bool = True
    reason: str | None = None


class ModelAttribution(BaseModel):
    """One edited model's row-population report.

    ``net_delta`` is measured on the authored model as a whole, not summed from
    the changes, and that distinction is the point. Counterfactuals evaluated in
    isolation do not compose: when two changes interact, the per-change deltas do
    not add up to what the warehouse would actually produce. ``interacts`` says
    so explicitly rather than leaving a reader to add the column up and trust the
    total.
    """

    path: str
    changes: list[RowPopulationChange] = Field(default_factory=list)
    baseline_rows: int | None = None
    authored_rows: int | None = None
    net_delta: int | None = None
    interacts: bool = False


@dataclass
class AttributionOutcome:
    """What :func:`attribute` produced, plus the two things its caller settles.

    ``adapter`` is set only when statements actually ran, so the caller knows
    whether there is spend to stamp. ``pending`` is the priced ask on a metered
    connector, returned rather than raised because the plan it belongs to is
    already stored and discarding it to ask about the counts would make the
    caller pay attention twice for one answer.
    """

    models: list[ModelAttribution] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending: ConfirmationRequest | None = None
    adapter: Adapter | None = None

    def __bool__(self) -> bool:
        return bool(self.models)


# --- jinja resolution ----------------------------------------------------------

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_STMT = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_REF_CALL = re.compile(r"^\s*ref\s*\(\s*(.+?)\s*\)\s*$", re.DOTALL)
_SOURCE_CALL = re.compile(r"^\s*source\s*\(\s*(.+?)\s*\)\s*$", re.DOTALL)
_CONFIG_CALL = re.compile(r"^\s*config\s*\(", re.DOTALL)
_STRING_ARG = re.compile(r"['\"]([^'\"]+)['\"]")


class UnattributableError(DexError):
    """This model's SQL cannot be reduced to something dex may reason about.

    Control flow rather than a refusal: it never reaches a caller, because every
    one of these is caught and becomes the ``reason`` on a finding that says it
    could not attribute. A ``DexError`` all the same, so the one rule the error
    hierarchy makes ("everything the engine raises is catchable as ``DexError``")
    has no exception carved out of it for a class nobody is meant to catch.
    """


#: A relation resolver: the name a ``ref()`` or ``source()`` points at, in, an
#: identifier the rendered SQL may use, out. Two exist. The naming resolver keeps
#: the dbt name and needs nothing, which is what lets a change be *named* on a
#: connector dex will never query; the cache resolver produces the physical
#: identifier a count has to run against. Rendering is otherwise identical, so a
#: model reads the same way whichever half of the feature is in play.
Resolver = Callable[[str], str]


def naming_resolver(name: str) -> str:
    """Keep the dbt relation name. dbt model and source names are already legal
    identifiers, so this both parses and reads the way the author wrote it."""

    return name


def cache_resolver(cache: DexCache | None, dialect: str) -> Resolver:
    """Resolve through the same :func:`~..cache.match_identifier` the query
    firewall and ``explore profile`` use, so nothing here reimplements dbt's own
    resolution and a name dex cannot place fails the way it fails everywhere
    else."""

    known = [d.identifier for d in cache.datasets] if cache is not None else []

    def resolve(name: str) -> str:
        if cache is None:
            raise UnattributableError(
                f"there is no exploration cache, so '{name}' cannot be resolved to "
                "a warehouse relation; run `explore map` first"
            )
        matches = match_identifier(name, known)
        if not matches:
            raise UnattributableError(
                f"'{name}' is not in the exploration cache, so the rows it "
                f"contributes cannot be counted; run `explore profile {name}` first"
            )
        if len(matches) > 1:
            raise UnattributableError(
                f"'{name}' is ambiguous in the exploration cache "
                f"({', '.join(matches)}); qualify it in the model"
            )
        return exp.to_table(matches[0]).sql(dialect=dialect, identify=True)

    return resolve


def render_model_sql(sql: str, resolve: Resolver) -> str:
    """A dbt model reduced to plain SQL over relations ``resolve`` can name.

    A ``{{ config() }}`` header is dropped. Every other jinja construct raises
    :class:`UnattributableError`: a macro call or an ``{% if %}`` means the rendered
    shape is not the shape dex can see, and guessing at it would attribute a
    delta to a statement the warehouse never runs.
    """

    text = _JINJA_COMMENT.sub("", sql)
    statement = _JINJA_STMT.search(text)
    if statement is not None:
        raise UnattributableError(
            f"the model carries a jinja statement block "
            f"({_clip(statement.group(0))}); its rendered shape depends on dbt, "
            "so dex cannot isolate a change in it"
        )

    def replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if _CONFIG_CALL.match(body):
            return ""
        ref = _REF_CALL.match(body)
        source = _SOURCE_CALL.match(body)
        if ref is None and source is None:
            raise UnattributableError(
                f"the model calls {{{{ {_clip(body)} }}}}, which only dbt can "
                "render; dex cannot isolate a change against it"
            )
        args = _STRING_ARG.findall((ref or source).group(1))
        if not args:
            raise UnattributableError(
                f"could not read a relation name out of {{{{ {_clip(body)} }}}}"
            )
        # ref('pkg', 'model') and source('name', 'table') both name the relation
        # last, so the trailing argument is the one to resolve either way.
        return resolve(args[-1])

    rendered = _JINJA_EXPR.sub(replace, text).strip()
    if not rendered:
        raise UnattributableError(
            "the model is entirely jinja; there is no SQL to compare"
        )
    return rendered


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


# sqlglot underlines the offending token with ANSI escapes, which are for a
# terminal and this message goes into a JSON envelope.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _reason(exc: Exception, limit: int = 240) -> str:
    """A failure as one clean sentence fit for an envelope.

    Warehouse and parser errors arrive with terminal formatting and, on some
    connectors, a full statement echo. The cap keeps a reason readable without
    losing the part that names the problem, which is always at the front.
    """

    text = " ".join(_ANSI.sub("", str(exc)).split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


# --- scopes and row-population features ----------------------------------------

MAIN_SCOPE = "(final select)"


def _scopes(tree: exp.Expression) -> dict[str, exp.Select]:
    """Every scope whose shape can move rows: each top-level CTE, plus the final
    select.

    Only top-level CTEs, deliberately. A CTE nested inside another is reachable
    but has no stable name across two versions of a file, and an alignment that
    can silently pair the wrong pair of scopes is worse than declining to align.
    """

    scopes: dict[str, exp.Select] = {}
    # sqlglot renamed the arg key "with" to "with_" between major versions, the
    # same rename the query firewall absorbs for "from". Both spellings are in
    # the supported range, so both are read.
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None:
        for cte in with_clause.expressions:
            if isinstance(cte.this, exp.Select):
                scopes[cte.alias_or_name.lower()] = cte.this
    if isinstance(tree, exp.Select):
        scopes[MAIN_SCOPE] = tree
    return scopes


def _predicates(select: exp.Select, key: str) -> list[exp.Expression]:
    """A WHERE/HAVING/QUALIFY condition flattened across its top-level ANDs.

    Flattening is what makes attribution per-predicate rather than per-clause:
    ``where a and b`` edited to ``where a and c`` is one change to ``b``, not a
    wholesale replacement of the filter.
    """

    clause = select.args.get(key)
    if clause is None:
        return []

    flat: list[exp.Expression] = []
    stack = [clause.this]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.And):
            stack.extend((node.expression, node.this))
        elif isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
            stack.append(node.this)
        else:
            flat.append(node)
    return flat


def _text(node: exp.Expression | None) -> str:
    """What a fragment says, normalized by sqlglot's own generator and no further.

    This is the display form, so it keeps the case the author wrote. Lowercasing
    it would rewrite string literals, and a finding that reports
    ``status <> 'cancelled'`` for an edit to ``'CANCELLED'`` names a predicate
    the model does not contain.
    """

    return "" if node is None else node.sql(comments=False).strip()


def _key(node: exp.Expression | None) -> str:
    """The matching form of :func:`_text`: case-insensitive, so re-casing an
    identifier is not mistaken for a row-affecting edit."""

    return _text(node).lower()


def _relation_key(node: exp.Expression | None) -> str:
    """A relation's identity with its alias removed.

    Aliasing cannot change which rows a relation contributes, so it must not
    split one relation into two. Keying on the aliased text would read
    ``from orders`` edited to ``from orders o`` as a source swap and go on to
    report a delta for a change that is pure notation.
    """

    if node is None:
        return ""
    bare = node.copy()
    bare.set("alias", None)
    return _key(bare)


@dataclass
class _Join:
    side: str
    relation: exp.Expression
    on: exp.Expression | None
    node: exp.Join

    @property
    def key(self) -> str:
        return _relation_key(self.relation)

    @property
    def name(self) -> str:
        return _text(self.relation)

    @property
    def label(self) -> str:
        on = f" on {_text(self.on)}" if self.on is not None else ""
        return f"{self.side} join {self.name}{on}"


def _joins(select: exp.Select) -> list[_Join]:
    out = []
    for node in select.args.get("joins") or []:
        side = " ".join(
            part for part in (node.side or "", node.kind or "") if part
        ).lower()
        out.append(
            _Join(
                side=side or "inner",
                relation=node.this,
                on=node.args.get("on"),
                node=node,
            )
        )
    return out


def _group_by(select: exp.Select) -> list[str]:
    group = select.args.get("group")
    return [] if group is None else [_text(e) for e in group.expressions]


# --- change detection ----------------------------------------------------------


@dataclass
class _Change:
    """One row-affecting difference, plus how to reproduce it on the prior model.

    ``mutate`` is what makes a counterfactual possible: it applies this change,
    and only this change, to a fresh copy of the prior model's scope. Isolating
    against a common baseline is the whole reason a per-change delta means
    anything.
    """

    kind: str
    scope: str
    detail: str
    prior: str | None
    authored: str | None
    mutate: Callable[[exp.Select], None]

    def finding(self) -> RowPopulationChange:
        return RowPopulationChange(
            scope=self.scope,
            kind=self.kind,
            detail=self.detail,
            prior=self.prior,
            authored=self.authored,
        )


def _set_predicates(select: exp.Select, key: str, preds: list[exp.Expression]) -> None:
    if not preds:
        select.set(key, None)
        return
    condition = preds[0]
    for extra in preds[1:]:
        condition = exp.And(this=condition, expression=extra)
    wrapper = {"where": exp.Where, "having": exp.Having, "qualify": exp.Qualify}[key]
    select.set(key, wrapper(this=condition))


def _predicate_mutator(
    clause: str, dialect: str, *, drop: str | None = None, add: str | None = None
) -> Callable[[exp.Select], None]:
    def mutate(select: exp.Select) -> None:
        preds = _predicates(select, clause)
        if drop is not None:
            preds = [p for p in preds if _key(p) != drop]
        if add is not None:
            preds.append(sqlglot.parse_one(add, dialect=dialect))
        _set_predicates(select, clause, preds)

    return mutate


def _clause_changes(
    scope: str, prior: exp.Select, authored: exp.Select, clause: str, dialect: str
) -> list[_Change]:
    """Predicate-level changes in one clause of one scope.

    A lone removal paired with a lone addition reads as a replacement, which is
    both what the caller did and the shape that makes the finding legible. Any
    other combination stays as independent additions and removals rather than
    guessing which new predicate stands in for which old one.
    """

    before = {_key(p): _text(p) for p in _predicates(prior, clause)}
    after = {_key(p): _text(p) for p in _predicates(authored, clause)}
    removed = [k for k in before if k not in after]
    added = [k for k in after if k not in before]
    noun = {
        "where": "predicate",
        "having": "HAVING predicate",
        "qualify": "QUALIFY predicate",
    }[clause]

    # One out, one in, in the same clause: the author rewrote a filter, and
    # saying so is both what happened and the shape a reader can act on. Any
    # other combination stays as independent additions and removals, because
    # pairing them would mean guessing which new predicate stands in for which
    # old one, and a wrong guess attributes a delta to the wrong edit.
    if len(removed) == 1 and len(added) == 1:
        old, new = removed[0], added[0]
        return [
            _Change(
                kind="predicate_replaced",
                scope=scope,
                detail=(f"the {noun} `{before[old]}` was replaced by `{after[new]}`"),
                prior=before[old],
                authored=after[new],
                mutate=_predicate_mutator(clause, dialect, drop=old, add=after[new]),
            )
        ]

    changes = [
        _Change(
            kind="predicate_removed",
            scope=scope,
            detail=f"the {noun} `{before[key]}` was removed",
            prior=before[key],
            authored=None,
            mutate=_predicate_mutator(clause, dialect, drop=key),
        )
        for key in removed
    ]
    changes.extend(
        _Change(
            kind="predicate_added",
            scope=scope,
            detail=f"the {noun} `{after[key]}` was added",
            prior=None,
            authored=after[key],
            mutate=_predicate_mutator(clause, dialect, add=after[key]),
        )
        for key in added
    )
    return changes


def _join_changes(
    scope: str,
    prior: exp.Select,
    authored: exp.Select,
    prior_ctes: set[str],
    authored_ctes: set[str],
) -> list[_Change]:
    """Join-level changes, matched on the relation rather than on position.

    Matching on the relation is what tells an added join apart from a retyped
    one. Both move rows, and they move them for different reasons a reader needs
    to tell apart: a new relation widens the universe, an inner-to-left change
    stops narrowing it.
    """

    # A join onto a renamed CTE is the same bookkeeping _source_changes skips.
    before = {
        j.key: j for j in _joins(prior) if not _names_a_cte(j.relation, prior_ctes)
    }
    after = {
        j.key: j
        for j in _joins(authored)
        if not _names_a_cte(j.relation, authored_ctes)
    }
    changes: list[_Change] = []

    for key, join in after.items():
        if key not in before:
            node = join.node.copy()
            changes.append(
                _Change(
                    kind="join_added",
                    scope=scope,
                    detail=f"a {join.side} join to `{join.name}` was added",
                    prior=None,
                    authored=join.label,
                    mutate=lambda select, node=node: select.set(
                        "joins", [*(select.args.get("joins") or []), node.copy()]
                    ),
                )
            )
            continue
        old = before[key]
        if old.side != join.side:
            side, kind = join.node.args.get("side"), join.node.args.get("kind")
            changes.append(
                _Change(
                    kind="join_side_changed",
                    scope=scope,
                    detail=(
                        f"the join to `{join.name}` changed from {old.side} "
                        f"to {join.side}"
                    ),
                    prior=old.label,
                    authored=join.label,
                    mutate=lambda select, key=key, side=side, kind=kind: _retype_join(
                        select, key, side, kind
                    ),
                )
            )
        elif _key(old.on) != _key(join.on):
            on = join.on.copy() if join.on is not None else None
            changes.append(
                _Change(
                    kind="join_condition_changed",
                    scope=scope,
                    detail=f"the join condition on `{join.name}` changed",
                    prior=old.label,
                    authored=join.label,
                    mutate=lambda select, key=key, on=on: _rejoin_on(select, key, on),
                )
            )

    changes.extend(
        _Change(
            kind="join_removed",
            scope=scope,
            detail=f"the {join.side} join to `{join.name}` was removed",
            prior=join.label,
            authored=None,
            mutate=lambda select, key=key: select.set(
                "joins",
                [
                    j
                    for j in (select.args.get("joins") or [])
                    if _relation_key(j.this) != key
                ],
            ),
        )
        for key, join in before.items()
        if key not in after
    )
    return changes


def _retype_join(select: exp.Select, key: str, side: Any, kind: Any) -> None:
    for node in select.args.get("joins") or []:
        if _relation_key(node.this) == key:
            node.set("side", side)
            node.set("kind", kind)


def _rejoin_on(select: exp.Select, key: str, on: exp.Expression | None) -> None:
    for node in select.args.get("joins") or []:
        if _relation_key(node.this) == key:
            node.set("on", on.copy() if on is not None else None)


def _grain_changes(
    scope: str, prior: exp.Select, authored: exp.Select
) -> list[_Change]:
    changes: list[_Change] = []
    was_distinct = prior.args.get("distinct") is not None
    is_distinct = authored.args.get("distinct") is not None
    if was_distinct != is_distinct:
        distinct = authored.args.get("distinct")
        changes.append(
            _Change(
                kind="distinct_added" if is_distinct else "distinct_removed",
                scope=scope,
                detail=f"DISTINCT was {'added' if is_distinct else 'removed'}",
                prior="distinct" if was_distinct else None,
                authored="distinct" if is_distinct else None,
                mutate=lambda select, d=distinct: select.set(
                    "distinct", d.copy() if d is not None else None
                ),
            )
        )

    before, after = _group_by(prior), _group_by(authored)
    if before != after:
        group = authored.args.get("group")
        changes.append(
            _Change(
                kind="group_by_changed",
                scope=scope,
                detail=(
                    f"the GROUP BY changed from ({', '.join(before) or 'none'}) "
                    f"to ({', '.join(after) or 'none'})"
                ),
                prior=", ".join(before) or None,
                authored=", ".join(after) or None,
                mutate=lambda select, g=group: select.set(
                    "group", g.copy() if g is not None else None
                ),
            )
        )
    return changes


def _from_relation(select: exp.Select) -> exp.Expression | None:
    # sqlglot renamed the arg key "from" to "from_" between major versions.
    clause = select.args.get("from_") or select.args.get("from")
    return None if clause is None else clause.this


def _names_a_cte(node: exp.Expression, ctes: set[str]) -> bool:
    """Whether a FROM/JOIN relation is one of this query's own CTEs.

    A CTE is an internal name, so renaming one is bookkeeping rather than a
    change of source. Without this check a renamed CTE reads as the model being
    repointed at a different table, which is both wrong and alarming, and the
    scope-alignment findings already report the rename properly.
    """

    return isinstance(node, exp.Table) and node.name.lower() in ctes


def _source_changes(
    scope: str,
    prior: exp.Select,
    authored: exp.Select,
    prior_ctes: set[str],
    authored_ctes: set[str],
) -> list[_Change]:
    """The driving relation swapped for another one.

    Repointing a model at a different table is the largest row-population change
    available and the easiest to make by accident, since nothing else about the
    model has to move. Aliasing is excluded by :func:`_relation_key`, so renaming
    the same relation reports nothing.
    """

    before, after = _from_relation(prior), _from_relation(authored)
    if before is None or after is None:
        return []
    if _relation_key(before) == _relation_key(after):
        return []
    if _names_a_cte(before, prior_ctes) or _names_a_cte(after, authored_ctes):
        return []

    replacement = after.copy()

    def mutate(select: exp.Select) -> None:
        clause = select.args.get("from_") or select.args.get("from")
        if clause is not None:
            clause.set("this", replacement.copy())

    return [
        _Change(
            kind="source_changed",
            scope=scope,
            detail=(
                f"the driving relation changed from `{_text(before)}` to "
                f"`{_text(after)}`"
            ),
            prior=_text(before),
            authored=_text(after),
            mutate=mutate,
        )
    ]


def classify(
    prior_sql: str, authored_sql: str, dialect: str
) -> tuple[list[_Change], list[RowPopulationChange]]:
    """Every difference between two model versions that can move rows.

    Returns the isolable changes alongside the ones that could not be isolated,
    already shaped as findings. Column expressions, aliases, casts, ordering and
    formatting produce nothing here at all, because they cannot change which rows
    enter a model and reporting them would bury the signal under the noise of an
    ordinary refactor.
    """

    try:
        prior = sqlglot.parse_one(prior_sql, dialect=dialect)
        authored = sqlglot.parse_one(authored_sql, dialect=dialect)
    except sqlglot.errors.ParseError as exc:
        return [], [
            RowPopulationChange(
                scope=MAIN_SCOPE,
                kind="unattributable",
                detail="the model's SQL could not be parsed for comparison",
                attributed=False,
                reason=_reason(exc),
            )
        ]

    prior_scopes, authored_scopes = _scopes(prior), _scopes(authored)
    prior_ctes = prior_scopes.keys() - {MAIN_SCOPE}
    authored_ctes = authored_scopes.keys() - {MAIN_SCOPE}
    changes: list[_Change] = []
    unresolved: list[RowPopulationChange] = []

    unresolved.extend(
        RowPopulationChange(
            scope=name,
            kind="unattributable",
            detail=f"the CTE `{name}` is new in this edit",
            attributed=False,
            reason=(
                "there is no matching scope in the prior model to compare it "
                "against, so any row-population effect it has is folded into "
                "the whole-model net rather than attributed"
            ),
        )
        for name in sorted(authored_scopes.keys() - prior_scopes.keys())
    )
    unresolved.extend(
        RowPopulationChange(
            scope=name,
            kind="unattributable",
            detail=f"the CTE `{name}` was removed or renamed in this edit",
            attributed=False,
            reason=(
                "a renamed scope cannot be paired with its replacement without "
                "guessing, so its effect is folded into the whole-model net"
            ),
        )
        for name in sorted(prior_scopes.keys() - authored_scopes.keys())
    )

    for name in prior_scopes.keys() & authored_scopes.keys():
        before, after = prior_scopes[name], authored_scopes[name]
        if not isinstance(before, exp.Select) or not isinstance(after, exp.Select):
            continue
        changes.extend(_source_changes(name, before, after, prior_ctes, authored_ctes))
        for clause in ("where", "having", "qualify"):
            changes.extend(_clause_changes(name, before, after, clause, dialect))
        changes.extend(_join_changes(name, before, after, prior_ctes, authored_ctes))
        changes.extend(_grain_changes(name, before, after))

    # A stable order so a re-plan of the same edit reports the same sequence:
    # the final select last, since that is where a reader ends up looking.
    changes.sort(key=lambda c: (c.scope == MAIN_SCOPE, c.scope, c.kind))
    return changes, unresolved


# --- counting ------------------------------------------------------------------


def count_sql(tree: exp.Expression, dialect: str) -> str:
    """One model version wrapped in a bare row count.

    ``COUNT(*)`` projects no column, which is what keeps this inside the read-only
    and PII guarantees: the statement measures the row population and can carry
    nothing else. A top-level ORDER BY is dropped because several dialects reject
    one inside a derived table and ordering cannot change a count anyway; a LIMIT
    is kept, because a limit genuinely is row population.
    """

    inner = tree.copy()
    inner.set("order", None)
    subquery = exp.Subquery(
        this=inner,
        alias=exp.TableAlias(this=exp.to_identifier("dex_row_population")),
    )
    counted = (
        exp.Select()
        .select(exp.alias_(exp.Count(this=exp.Star()), "dex_rows"))
        .from_(subquery)
    )
    return counted.sql(dialect=dialect)


def _variant(prior: exp.Expression, change: _Change, dialect: str) -> str:
    copy = prior.copy()
    scope = _scopes(copy).get(change.scope)
    if scope is None:  # pragma: no cover - alignment guarantees the scope exists
        raise UnattributableError(
            f"scope `{change.scope}` vanished from the prior model"
        )
    change.mutate(scope)
    return count_sql(copy, dialect)


@dataclass
class _Statement:
    """One count to run, and where its answer goes."""

    sql: str
    model: int
    change: int | None  # None is the model-level baseline/authored pair
    authored: bool = False


# --- orchestration -------------------------------------------------------------


@dataclass
class _Model:
    """One edited model as it moves through the pipeline.

    ``prior``/``authored`` are the rendered trees the whole report is derived
    from, so classification and counting always read the same SQL. They are
    ``None`` when rendering failed, which is the state that turns every change on
    this model into a named-but-unmeasured finding.
    """

    record: ModelAttribution
    changes: list[_Change] = field(default_factory=list)
    prior: exp.Expression | None = None
    authored: exp.Expression | None = None


def attribute(
    engine: DexEngine,
    edits: list[PlanEdit],
    *,
    requested: bool | None = None,
) -> AttributionOutcome:
    """Report the row-population consequence of each authored change that has one.

    Runs *after* the plan is stored, and nothing it does can prevent a plan from
    existing: every failure degrades to a finding that says it could not
    attribute and why. ``requested`` is the tri-state behind ``--attribute-rows``:
    None follows the connector (counted where counting is free, named only where
    it is not), True counts and prices, False never counts.
    """

    candidates = [
        edit
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL and edit.op is EditOp.UPSERT
    ]
    if not candidates:
        return AttributionOutcome()

    dialect = get_dialect(engine.connector or engine.config.connector)
    try:
        view = load_project(engine.project_dir())
    except Exception as exc:
        return AttributionOutcome(
            warnings=[
                "could not read the dbt project to compare the authored models "
                f"against their current versions ({type(exc).__name__}: {exc})"
            ]
        )

    connector = engine.connector or engine.config.connector
    free = is_free_connector(connector)
    counting = free if requested is None else requested
    # Choosing the resolver here, once, is what keeps classification and counting
    # reading the same SQL. The cache is loaded only when counting, so the
    # named-only path stays free of both the store read and the connection.
    resolver = (
        cache_resolver(readable_cache(engine.store), dialect)
        if counting
        else naming_resolver
    )

    warnings: list[str] = []
    models: list[_Model] = []
    for edit in candidates:
        current = view.files.get(edit.path)
        if current is None:
            continue  # authoring a model that does not exist yet has no prior
        model = _prepare(
            edit.path, current.content, edit.new_content, resolver, dialect
        )
        if model is not None:
            models.append(model)

    if not models:
        return AttributionOutcome()

    for model in models:
        if len(model.changes) > MAX_CHANGES_PER_MODEL:
            dropped = model.changes[MAX_CHANGES_PER_MODEL:]
            model.changes = model.changes[:MAX_CHANGES_PER_MODEL]
            model.record.changes.extend(
                _named_only(dropped, "beyond the per-model attribution cap")
            )
            warnings.append(
                f"{model.record.path}: {len(model.changes) + len(dropped)} "
                f"row-affecting changes found; the first {MAX_CHANGES_PER_MODEL} "
                f"are measured and {len(dropped)} are named only"
            )

    if len(models) > MAX_MODELS:
        warnings.append(
            f"{len(models)} edited models have row-affecting changes; the first "
            f"{MAX_MODELS} are measured and {len(models) - MAX_MODELS} are named only"
        )
        for model in models[MAX_MODELS:]:
            model.record.changes.extend(
                _named_only(model.changes, "beyond the per-plan attribution cap")
            )
            model.changes = []

    if not counting:
        hint = (
            "counting is off (--no-attribute-rows)"
            if free
            else (
                f"measuring a change costs {connector} spend, so it is opt-in: "
                "re-run with --attribute-rows to price and measure it"
            )
        )
        for model in models:
            model.record.changes.extend(_named_only(model.changes, hint))
        return AttributionOutcome(models=[m.record for m in models], warnings=warnings)

    return _count(engine, models, dialect, warnings)


def _prepare(
    path: str, prior_sql: str, authored_sql: str, resolve: Resolver, dialect: str
) -> _Model | None:
    """Render both versions, classify their difference, and keep the trees.

    Two renderings, tried in that order, and the fallback is the point. A model
    whose relations dex cannot place still renders under the naming resolver,
    which needs nothing, so the caller still learns *which* change moved rows
    even where nothing can measure it. Only a model that renders neither way,
    which means jinja dex cannot reduce at all, reports a single finding saying
    so, and only when the file actually changed: a byte-identical edit has no
    change to be unable to attribute.

    ``None`` means there is nothing to report, which is the ordinary outcome for
    an edit that cannot move rows.
    """

    record = ModelAttribution(path=path)
    degraded: str | None = None
    try:
        prior = render_model_sql(prior_sql, resolve)
        authored = render_model_sql(authored_sql, resolve)
    except UnattributableError as exc:
        degraded = str(exc)
        try:
            prior = render_model_sql(prior_sql, naming_resolver)
            authored = render_model_sql(authored_sql, naming_resolver)
        except UnattributableError as bare:
            if prior_sql.strip() == authored_sql.strip():
                return None
            record.changes.append(
                RowPopulationChange(
                    scope=MAIN_SCOPE,
                    kind="unattributable",
                    detail="this edit's effect on the row population is unknown",
                    attributed=False,
                    reason=str(bare),
                )
            )
            return _Model(record=record)

    changes, unresolved = classify(prior, authored, dialect)
    if not changes and not unresolved:
        return None
    record.changes.extend(unresolved)
    # The trees are kept even with nothing isolable to measure, because the
    # whole-model net is exactly what a reader needs when the reason a change
    # could not be attributed is that it was a rename or a macro: it says how
    # much moved, having already said that dex cannot say what moved it.
    if degraded is not None:
        record.changes.extend(_named_only(changes, degraded))
        return _Model(record=record)
    try:
        trees = (
            sqlglot.parse_one(prior, dialect=dialect),
            sqlglot.parse_one(authored, dialect=dialect),
        )
    except sqlglot.errors.ParseError as exc:  # pragma: no cover - classify parses first
        record.changes.extend(_named_only(changes, _reason(exc)))
        return _Model(record=record)
    return _Model(record=record, changes=changes, prior=trees[0], authored=trees[1])


def _named_only(changes: list[_Change], reason: str) -> list[RowPopulationChange]:
    findings = []
    for change in changes:
        finding = change.finding()
        finding.attributed = False
        finding.reason = reason
        findings.append(finding)
    return findings


def _count(
    engine: DexEngine,
    models: list[_Model],
    dialect: str,
    warnings: list[str],
) -> AttributionOutcome:
    """Build, guard, price, and run the counts. Every failure degrades in place.

    The order is the point. Statements are built and firewall-approved before a
    connection is opened, so a plan that cannot be counted never touches the
    warehouse; and the whole batch is priced in one handshake, so a caller agrees
    to the attribution once rather than per statement.
    """

    records = [m.record for m in models]
    cache = readable_cache(engine.store)
    statements: list[_Statement] = []

    for index, model in enumerate(models):
        if model.prior is None or model.authored is None:
            continue
        statements.append(
            _Statement(sql=count_sql(model.prior, dialect), model=index, change=None)
        )
        statements.append(
            _Statement(
                sql=count_sql(model.authored, dialect),
                model=index,
                change=None,
                authored=True,
            )
        )
        for change in model.changes:
            model.record.changes.append(change.finding())
            position = len(model.record.changes) - 1
            try:
                statements.append(
                    _Statement(
                        sql=_variant(model.prior, change, dialect),
                        model=index,
                        change=position,
                    )
                )
            except (UnattributableError, sqlglot.errors.ParseError) as exc:
                _mark(model.record.changes[position], _reason(exc))

    if not statements or cache is None:
        return AttributionOutcome(models=records, warnings=warnings)

    limits = engine.config.query
    approved: list[tuple[_Statement, str]] = []
    for statement in statements:
        try:
            inspected = inspect_query(statement.sql, cache, limits, dialect=dialect)
        except QueryRefusedError as exc:
            _refuse(records, statement, _reason(exc))
            continue
        approved.append((statement, inspected.sql))

    if not approved:
        return AttributionOutcome(models=records, warnings=warnings)

    from .. import command_args

    try:
        adapter = engine._adapter("transform plan")
    except Exception as exc:
        warnings.append(
            "could not open a connection to measure the row-population change "
            f"({type(exc).__name__}: {exc}); the changes are named, not measured"
        )
        _mark_all(records, "no connection was available to measure it")
        return AttributionOutcome(models=records, warnings=warnings)

    estimator = getattr(adapter, "query_estimate", None)
    estimate = sum(estimator(sql) for _statement, sql in approved) if estimator else 0.0
    pending = command_args.confirmation_request(
        "transform plan",
        adapter,
        estimate,
        per_table={"(row-population attribution)": estimate},
        notes=[
            f"{len(approved)} row count(s) would attribute this edit's "
            "row-population change to the change that caused it; the plan is "
            "already stored, so re-running confirmed measures it without "
            "re-planning"
        ],
    )
    if pending is not None:
        _mark_all(records, "awaiting confirmation of the spend to measure it")
        return AttributionOutcome(models=records, warnings=warnings, pending=pending)

    counts: dict[tuple[int, int | None, bool], int] = {}
    for statement, sql in approved:
        try:
            result = adapter.run_query(
                sql, max_rows=1, timeout_seconds=limits.timeout_seconds
            )
            counts[(statement.model, statement.change, statement.authored)] = int(
                result.cells[0][0]
            )
        except Exception as exc:
            # The usual cause for a variant is that the change depends on
            # something else in the same edit (a predicate over a column an added
            # join brings in), so applying it to the prior model alone does not
            # produce valid SQL. Naming that is more useful than the raw error.
            context = (
                "this change cannot be measured on its own because the prior "
                "model does not provide everything it references"
                if statement.change is not None
                else "the count did not run"
            )
            _refuse(records, statement, f"{context}: {_reason(exc)}")

    _settle(records, counts)
    return AttributionOutcome(models=records, warnings=warnings, adapter=adapter)


def _mark(finding: RowPopulationChange, reason: str) -> None:
    finding.attributed = False
    finding.reason = reason


def _mark_all(records: list[ModelAttribution], reason: str) -> None:
    for record in records:
        for finding in record.changes:
            if finding.attributed:
                _mark(finding, reason)


def _refuse(
    records: list[ModelAttribution], statement: _Statement, reason: str
) -> None:
    """A statement that never ran takes down exactly what depended on it.

    A failed baseline invalidates every delta for that model, since every one of
    them is measured against it. A failed variant takes down only its own change.
    """

    record = records[statement.model]
    if statement.change is None:
        for finding in record.changes:
            if finding.attributed:
                _mark(finding, reason)
        return
    _mark(record.changes[statement.change], reason)


def _settle(
    records: list[ModelAttribution],
    counts: dict[tuple[int, int | None, bool], int],
) -> None:
    for index, record in enumerate(records):
        baseline = counts.get((index, None, False))
        record.baseline_rows = baseline
        record.authored_rows = counts.get((index, None, True))
        if baseline is None or record.authored_rows is None:
            continue
        record.net_delta = record.authored_rows - baseline

        summed = 0
        for position, finding in enumerate(record.changes):
            rows = counts.get((index, position, False))
            if not finding.attributed:
                continue
            if rows is None:
                _mark(finding, "the counting statement returned no row")
                continue
            finding.rows = rows
            finding.delta = rows - baseline
            summed += finding.delta
        # Isolated counterfactuals do not compose. Saying so is what stops a
        # reader from adding the column up and trusting a total the warehouse
        # would never produce.
        measured = [f for f in record.changes if f.delta is not None]
        record.interacts = bool(measured) and summed != record.net_delta
