"""The row-affecting shape of one SELECT, read in one place.

Several parts of the engine ask the same structural questions of a parsed
statement: what is the driving relation, what does it join and on what, what
filters or aggregates it, which named scopes does it have. `transform plan`
asks them to attribute a row-population change to the edit that caused it;
`maintain verify` asks them to decide whether a model's row count can honestly
be compared with its parent's. The questions are identical and the answers must
not diverge, so they live here rather than in each caller.

Keeping them together also confines a version hazard. sqlglot renamed the
argument keys ``from`` to ``from_`` and ``with`` to ``with_`` between majors,
and both spellings are inside the supported range, so every reader of a FROM or
a WITH has to accept either. Spread across call sites that is a rename waiting
to be half-applied; here it is absorbed once.

Almost everything here is a pure read of a parsed tree. The one exception is
:func:`set_predicates`, which is the inverse of :func:`predicates`: the reader
flattens a WHERE across its top-level ANDs, and rebuilding the clause from a
flattened list is the operation that undoes it. The two belong together, because
a caller that splits a clause one way and reassembles it another produces a
statement neither function describes.

Nothing here executes anything or opens a connection. The module imports sqlglot
at the top, so a caller that must survive its absence (the base install carries
no dialect engine) should reach it behind
:func:`~.guards.dialect.ensure_available` and degrade on the refusal rather than
importing unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import expressions as exp

#: Name given to the outermost select, so it can key a scope map beside the
#: CTEs without colliding with a real CTE name.
MAIN_SCOPE = "(final select)"


def scopes(tree: exp.Expression) -> dict[str, exp.Select]:
    """Every scope whose shape can move rows: each top-level CTE, plus the final
    select.

    Only top-level CTEs, deliberately. A CTE nested inside another is reachable
    but has no stable name across two versions of a file, and an alignment that
    can silently pair the wrong pair of scopes is worse than declining to align.
    """

    found: dict[str, exp.Select] = {}
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None:
        for cte in with_clause.expressions:
            if isinstance(cte.this, exp.Select):
                found[cte.alias_or_name.lower()] = cte.this
    if isinstance(tree, exp.Select):
        found[MAIN_SCOPE] = tree
    return found


def predicates(select: exp.Select, key: str) -> list[exp.Expression]:
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


def set_predicates(select: exp.Select, key: str, preds: list[exp.Expression]) -> None:
    """Rebuild a WHERE/HAVING/QUALIFY clause from flattened predicates, in place.

    The inverse of :func:`predicates`. An empty list removes the clause outright
    rather than leaving an empty wrapper, because a ``WHERE`` with nothing under
    it is not something a generator can print.
    """

    if not preds:
        select.set(key, None)
        return
    condition = preds[0]
    for extra in preds[1:]:
        condition = exp.And(this=condition, expression=extra)
    wrapper = {"where": exp.Where, "having": exp.Having, "qualify": exp.Qualify}[key]
    select.set(key, wrapper(this=condition))


def text(node: exp.Expression | None) -> str:
    """What a fragment says, normalized by sqlglot's own generator and no further.

    This is the display form, so it keeps the case the author wrote. Lowercasing
    it would rewrite string literals, and a finding that reports
    ``status <> 'cancelled'`` for an edit to ``'CANCELLED'`` names a predicate
    the model does not contain.
    """

    return "" if node is None else node.sql(comments=False).strip()


def match_key(node: exp.Expression | None) -> str:
    """The matching form of :func:`text`: case-insensitive, so re-casing an
    identifier is not mistaken for a row-affecting edit."""

    return text(node).lower()


def relation_key(node: exp.Expression | None) -> str:
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
    return match_key(bare)


@dataclass
class Join:
    """One join in a select: how it joins, what it joins, and on what.

    ``using`` is ``JOIN ... USING (col, ...)``'s own column list: bare names,
    unqualified by either side, each an implicit ``left.col = right.col``.
    Empty for an ``ON``-conditioned join or one with neither (a CROSS JOIN,
    an UNNEST).
    """

    side: str
    relation: exp.Expression
    on: exp.Expression | None
    node: exp.Join
    using: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return relation_key(self.relation)

    @property
    def name(self) -> str:
        return text(self.relation)

    @property
    def label(self) -> str:
        on = f" on {text(self.on)}" if self.on is not None else ""
        return f"{self.side} join {self.name}{on}"


def joins(select: exp.Select) -> list[Join]:
    out = []
    for node in select.args.get("joins") or []:
        side = " ".join(
            part for part in (node.side or "", node.kind or "") if part
        ).lower()
        using = node.args.get("using") or []
        out.append(
            Join(
                side=side or "inner",
                relation=node.this,
                on=node.args.get("on"),
                node=node,
                using=tuple(u.name for u in using),
            )
        )
    return out


def group_by(select: exp.Select) -> list[str]:
    group = select.args.get("group")
    return [] if group is None else [text(e) for e in group.expressions]


# Built by name at import, the way the guards build theirs, because sqlglot
# collapsed the three set-operation classes under one base between majors and
# both shapes are inside the supported range. A class that is not there in the
# installed version simply drops out of the tuple.
SET_OPERATIONS = tuple(
    c
    for c in (
        getattr(exp, "SetOperation", None),
        getattr(exp, "Union", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "Except", None),
    )
    if isinstance(c, type)
)

#: Constructs that turn one input row into several by design. A query holding
#: one has an honest reason to be larger than the relation it reads, so a caller
#: judging row growth must not read it as a defect.
ROW_MULTIPLIERS = tuple(
    c
    for c in (
        getattr(exp, "Unnest", None),
        getattr(exp, "Lateral", None),
        getattr(exp, "Explode", None),
        getattr(exp, "Posexplode", None),
    )
    if isinstance(c, type)
)

#: Join sides that filter rather than widen: a semi join keeps a subset of the
#: left side and an anti join keeps its complement, so both reduce.
_FILTERING_SIDES = ("semi", "anti")


def reducing_clauses(select: exp.Select) -> list[str]:
    """Every clause in this select that gives it an honest reason to hold fewer
    rows than the relation it reads.

    The list is what separates a defect from a design. A model with none of
    these should have as many rows as its driving relation, and a shortfall is
    then a real finding; a model with any of them was written to hold fewer, and
    reporting it would bury the first case under the second.

    Named in the order a reader would look for them, and returned as prose
    fragments because they end up in a finding a person reads.
    """

    found: list[str] = []
    if select.args.get("where") is not None:
        found.append("a WHERE filter")
    if select.args.get("having") is not None:
        found.append("a HAVING filter")
    if select.args.get("qualify") is not None:
        found.append("a QUALIFY filter")
    if select.args.get("group") is not None:
        found.append("a GROUP BY")
    if select.args.get("distinct") is not None:
        found.append("a DISTINCT")
    if select.args.get("limit") is not None:
        found.append("a LIMIT")
    found.extend(
        f"a {join.side} join"
        for join in joins(select)
        if any(side in join.side for side in _FILTERING_SIDES)
    )
    return found


def multiplies_rows(node: exp.Expression) -> bool:
    """Whether anything under ``node`` turns one row into several by design."""

    return (
        bool(ROW_MULTIPLIERS)
        and next(node.find_all(*ROW_MULTIPLIERS), None) is not None
    )


def equality_column_pairs(
    on: exp.Expression | None,
) -> list[tuple[exp.Column, exp.Column]]:
    """The column-to-column equalities an ON condition contains, as the raw
    nodes rather than rendered text.

    A range predicate or a column compared against a literal or an expression
    is excluded already: only a bare ``column = column`` counts, since a key
    is a pair of columns and nothing else identifies one. Kept as nodes
    (rather than :func:`equality_columns`'s strings) because a caller that
    needs to know which *relation* each side belongs to -- attributing a join
    predicate to the two tables it equates, not just displaying it -- needs
    each column's own table qualifier, which text alone throws away.
    """

    if on is None:
        return []
    return [
        (node.this, node.expression)
        for node in on.find_all(exp.EQ)
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column)
    ]


def equality_columns(on: exp.Expression | None) -> list[str]:
    """The column pairs an ON condition equates, rendered as written.

    A join's key is what a reader needs to act on a fanout finding, and it is
    the equality pairs rather than the whole condition: an ON carrying a range
    predicate or a constant beside the key would otherwise report the noise
    alongside the one part that identifies the grain.
    """

    return [
        f"{text(left)} = {text(right)}" for left, right in equality_column_pairs(on)
    ]


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    """A condition flattened across its top-level ANDs -- the same walk
    :func:`predicates` does for a WHERE/HAVING/QUALIFY clause, lifted out so
    an ON condition (which carries no such clause wrapper) can use it too."""

    flat: list[exp.Expression] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.And):
            stack.extend((current.expression, current.this))
        elif isinstance(current, exp.Paren) and isinstance(current.this, exp.And):
            stack.append(current.this)
        else:
            flat.append(current)
    return flat


def conjunctive_equality_pairs(
    on: exp.Expression | None,
) -> list[tuple[exp.Column, exp.Column]] | None:
    """The column-to-column equalities an ON condition *requires together*
    for a row to match -- every top-level ANDed conjunct, and only when
    every one of them is a plain ``column = column`` equality.

    Unlike :func:`equality_column_pairs`, which finds every ``column =
    column`` anywhere in the tree regardless of Boolean structure, this
    respects it. An equality sitting inside an OR is not required for the
    join to match at all (``a.x = b.x OR a.y = b.y`` matches on either), so
    folding it into a composite key alongside a real conjunct would demand a
    stricter match than the join itself does; probed as a composite, a
    healthy join could then read as completely orphaned. A range predicate
    or anything else non-equality shares the same problem in the other
    direction: it narrows the join in a way no probed equality reflects.

    Returns ``None`` -- rather than a partial list -- the moment any
    top-level conjunct is not a plain column-to-column equality, so a caller
    never probes a subset of the real condition and calls it the join's key;
    an ON with nothing conjunctive at all in it (no ``AND`` at the top,
    itself not a plain equality) is the same case, one conjunct that fails
    the test.
    """

    if on is None:
        return None
    pairs: list[tuple[exp.Column, exp.Column]] = []
    for conjunct in _flatten_and(on):
        if isinstance(conjunct, exp.Paren):
            conjunct = conjunct.this
        if not isinstance(conjunct, exp.EQ):
            return None
        left, right = conjunct.this, conjunct.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return None
        pairs.append((left, right))
    return pairs or None


def from_relation(select: exp.Select) -> exp.Expression | None:
    """The driving relation: what the FROM clause names, before any join."""

    clause = select.args.get("from_") or select.args.get("from")
    return None if clause is None else clause.this


def names_a_cte(node: exp.Expression, ctes: set[str]) -> bool:
    """Whether a FROM/JOIN relation is one of this query's own CTEs.

    A CTE is an internal name, so renaming one is bookkeeping rather than a
    change of source. Without this check a renamed CTE reads as the model being
    repointed at a different table, which is both wrong and alarming, and the
    scope-alignment findings already report the rename properly.
    """

    return isinstance(node, exp.Table) and node.name.lower() in ctes
