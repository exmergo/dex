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

from .errors import DexError

#: Name given to the outermost select, so it can key a scope map beside the
#: CTEs without colliding with a real CTE name.
MAIN_SCOPE = "(final select)"

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


def select_branches(tree: exp.Expression | None) -> list[exp.Select]:
    """Flatten a statement across any set operations into its constituent SELECTs.

    A single SELECT returns ``[tree]``. A UNION, INTERSECT or EXCEPT returns the
    list of all its branch SELECTs in order, so a caller walking joins, usages,
    or fragments across a compound statement inspects each branch independently.
    """

    if tree is None:
        return []
    if isinstance(tree, exp.Select):
        return [tree]
    if SET_OPERATIONS and isinstance(tree, SET_OPERATIONS):
        left = select_branches(tree.this) if tree.this is not None else []
        right_side = tree.args.get("expression")
        right = select_branches(right_side) if right_side is not None else []
        return left + right
    return []


def scopes(tree: exp.Expression) -> dict[str, exp.Expression]:
    """Every scope whose shape can move rows: each top-level CTE, plus the final
    select.

    Only top-level CTEs, deliberately. A CTE nested inside another is reachable
    but has no stable name across two versions of a file, and an alignment that
    can silently pair the wrong pair of scopes is worse than declining to align.
    """

    found: dict[str, exp.Expression] = {}
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None:
        for cte in with_clause.expressions:
            body = cte.this.unnest() if hasattr(cte.this, "unnest") else cte.this
            if isinstance(body, (exp.Select, *SET_OPERATIONS)):
                found[cte.alias_or_name.lower()] = body
    body = tree.unnest() if hasattr(tree, "unnest") else tree
    if isinstance(body, (exp.Select, *SET_OPERATIONS)):
        found[MAIN_SCOPE] = body
    return found


def scope_identifiers(tree: exp.Expression) -> dict[str, exp.Identifier]:
    """The original identifier node for each scope in ``tree``, keyed in lowercase.

    Top-level CTEs carry their written identifier, with quotes preserved; the
    final select carries ``MAIN_SCOPE``.
    """

    found: dict[str, exp.Identifier] = {}
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None:
        for cte in with_clause.expressions:
            alias = cte.args.get("alias")
            if alias is not None and isinstance(alias.this, exp.Identifier):
                ident = alias.this.copy()
            else:
                ident = exp.to_identifier(cte.alias_or_name)
            found[cte.alias_or_name.lower()] = ident
    found[MAIN_SCOPE] = exp.to_identifier(MAIN_SCOPE)
    return found


def scope_identifier(tree: exp.Expression, scope: str) -> exp.Identifier:
    """The written identifier of ``scope`` in ``tree``, preserving quotes."""

    return scope_identifiers(tree).get(scope, exp.to_identifier(scope))


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


def unqualified_key(node: exp.Expression) -> str:
    """A comparison form in which re-aliasing or re-casing is not a change.

    Identifiers and function names are lowercased. A column's table qualifier
    is dropped where its own select reads one relation and defines it, since
    the column can only come from there. Otherwise the qualifier is replaced by
    the relation it names, found by walking out through the enclosing selects:
    ``a.active`` edited to ``b.active`` across a join is a change, and so is a
    correlated subquery's ``a.id`` edited to ``b.id`` or to its own ``f.id``,
    while renaming ``a`` is not. An unqualified column in a select that reads
    several relations stays unqualified and so differs from a qualified one.

    String literals keep their case: ``status = 'OPEN'`` edited to
    ``status = 'open'`` can select different rows, so it must not compare equal.
    """

    bare = node.copy()
    sources = {
        id(select): _source_names(select) for select in bare.find_all(exp.Select)
    }
    for column in list(bare.find_all(exp.Column)):
        qualifier = column.table.lower()
        nearest = column.find_ancestor(exp.Select)
        owner = nearest
        while owner is not None and qualifier not in sources[id(owner)]:
            owner = owner.find_ancestor(exp.Select)
        if not qualifier or (
            owner in (nearest, None)
            and (nearest is None or len(sources[id(nearest)]) <= 1)
        ):
            column.set("table", None)
        elif owner is None:
            column.set("table", exp.to_identifier(qualifier))
        else:
            column.set("table", exp.to_identifier(sources[id(owner)][qualifier]))
        column.set("db", None)
        column.set("catalog", None)
    for select in bare.find_all(exp.Select):
        for relation in _relations(select):
            relation.set("alias", None)
    for identifier in bare.find_all(exp.Identifier):
        identifier.set("this", identifier.this.lower())
    return bare.sql(comments=False, normalize_functions="lower").strip()


def _source_names(select: exp.Select) -> dict[str, str]:
    """Each relation a select reads, keyed by the name its columns qualify it
    with, mapped to the relation itself and its occurrence (a self-join's two
    sides stay apart)."""

    seen: dict[str, int] = {}
    named: dict[str, str] = {}
    for relation in _relations(select):
        key = relation_key(relation)
        seen[key] = seen.get(key, 0) + 1
        named[relation.alias_or_name.lower()] = f"{key}#{seen[key]}"
    return named


def _relations(select: exp.Select) -> list[exp.Expression]:
    """The relations a select reads directly: its FROM and each join."""

    from_ = select.args.get("from_") or select.args.get("from")
    relations = [from_.this] if from_ is not None else []
    return relations + [join.this for join in select.args.get("joins") or []]


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


def _projection_holds_scalar_aggregate(projection: exp.Expression) -> bool:
    """Whether this projection expression itself aggregates rows.

    A window (`COUNT(*) OVER ()`) keeps one output row per input, so it is not
    a reducer. An aggregate nested in a scalar subquery belongs to that
    subquery's scope, not this select's: the outer query still projects one
    row per driving row. Only an aggregate that is part of this select's own
    projection, outside a window or subquery, collapses the input.
    """

    stack: list[exp.Expression] = [projection]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Window, exp.Subquery)):
            continue
        if isinstance(node, exp.AggFunc):
            return True
        stack.extend(node.iter_expressions())
    return False


def holds_scalar_aggregate(select: exp.Select) -> bool:
    """Whether this select collapses every input row into one by aggregating
    without a GROUP BY.

    `SELECT COUNT(*) FROM t` and `SELECT SUM(a), MAX(b) FROM t` intentionally
    return one row. A `GROUP BY` is already named by :func:`reducing_clauses`;
    this helper is only for the no-group form, so callers that already saw a
    group do not double-count.
    """

    if select.args.get("group") is not None:
        return False
    return any(
        _projection_holds_scalar_aggregate(projection)
        for projection in select.expressions
    )


def reducing_clauses(select: exp.Select) -> list[str]:
    """Every clause in this select that gives it an honest reason to hold fewer
    rows than the relation it reads.

    The list is what separates a defect from a design. A model with none of
    these should have as many rows as its driving relation, and a shortfall is
    then a real finding; a model with any of them was written to hold fewer, and
    reporting it would bury the first case under the second.

    Named in the order a reader would look for them, and returned as prose
    fragments because they end up in a finding a person reads. A scalar
    aggregate without a GROUP BY is one of those reasons (#1098): the select
    collapses its input on purpose, the same way a GROUP BY does.
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
    elif holds_scalar_aggregate(select):
        found.append("a scalar aggregate")
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


def from_relation(select: exp.Expression | None) -> exp.Expression | None:
    """The driving relation: what the FROM clause names, before any join."""

    if not isinstance(select, exp.Select):
        return None
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


# --- identifier conversions (#861) ----------------------------------------------


@dataclass(frozen=True)
class KeyStep:
    """One step of a conversion applied to an identifier column.

    ``kind`` is ``cast`` (fails the statement on a value it cannot convert),
    ``try_cast`` (turns that value into NULL: ``TRY_CAST``, BigQuery's
    ``SAFE_CAST``, Snowflake's ``TRY_TO_NUMBER``) or ``normalization`` (a
    lexical rewrite: ``lower``, ``upper``, ``trim``, ``ltrim``, ``rtrim``).
    ``name`` is how the step reads, and ``target_type`` the type a cast
    converts to, rendered as written.
    """

    kind: str
    name: str
    target_type: str | None = None

    def label(self) -> str:
        """How the step reads in a message: its name, with the cast's target type where
        it has one.
        """
        return f"{self.name}({self.target_type})" if self.target_type else self.name


def key_step(node: exp.Expression) -> KeyStep | None:
    """The conversion ``node`` applies to its own ``this``, or None.

    Only steps whose effect on identity can be measured with an aggregate are
    recognized. Anything else (``regexp_replace``, ``concat``, a ``CASE``) is a
    rewrite whose meaning dex does not claim to know, and a caller reports it
    as unsupported rather than guessing.
    """

    if isinstance(node, exp.Cast):
        to = node.args.get("to")
        target = to.sql() if to is not None else None
        safe = isinstance(node, exp.TryCast) or bool(node.args.get("safe"))
        return KeyStep(
            "try_cast" if safe else "cast", "try_cast" if safe else "cast", target
        )
    if isinstance(node, exp.ToNumber) and not node.args.get("format"):
        safe = bool(node.args.get("safe"))
        return KeyStep("try_cast" if safe else "cast", "to_number", "NUMBER")
    if isinstance(node, exp.Lower):
        return KeyStep("normalization", "lower")
    if isinstance(node, exp.Upper):
        return KeyStep("normalization", "upper")
    if isinstance(node, exp.Trim):
        position = str(node.args.get("position") or "").upper()
        name = {"LEADING": "ltrim", "TRAILING": "rtrim"}.get(position, "trim")
        return KeyStep("normalization", name)
    return None


def peel_key_conversion(
    node: exp.Expression | None,
) -> tuple[exp.Column, tuple[KeyStep, ...]] | None:
    """The column a chain of recognized conversions is applied to, and the
    chain outermost first; None for a bare column or anything else.

    ``CAST(LOWER(TRIM(k)) AS INT)`` peels to ``k`` with three steps. A chain
    broken by a step dex does not recognize returns None, and
    :func:`wraps_one_column` is how a caller tells that apart from an
    expression that is no conversion at all.
    """

    steps: list[KeyStep] = []
    current = node
    while current is not None and not isinstance(current, exp.Column):
        if isinstance(current, exp.Paren):
            current = current.this
            continue
        step = key_step(current)
        if step is None:
            return None
        steps.append(step)
        current = current.this
    if current is None or not steps:
        return None
    return current, tuple(steps)


def wraps_one_column(node: exp.Expression | None) -> exp.Column | None:
    """The one column an expression reads, when it reads exactly one and is not
    that bare column itself: the shape of a rewrite applied to an identifier."""

    if node is None or isinstance(node, exp.Column):
        return None
    columns = {match_key(c): c for c in node.find_all(exp.Column)}
    if len(columns) != 1:
        return None
    return next(iter(columns.values()))


def conjunctive_conversion_pairs(
    on: exp.Expression | None,
) -> list[tuple[exp.Column, exp.Column, exp.Expression, exp.Expression]] | None:
    """:func:`conjunctive_equality_pairs`, with each side allowed to be a
    recognized conversion of a column (:func:`peel_key_conversion`) as well as
    the bare column: ``TRY_CAST(a.k AS INT) = b.k`` is a key equality whose
    left key is converted (#861).

    Each entry is ``(left_column, right_column, left_side, right_side)``, the
    sides as written. ``None`` on the same terms as the bare version: the moment
    any top-level conjunct is not such an equality, so no subset of a condition
    is ever probed as though it were the whole key.
    """

    if on is None:
        return None
    pairs = []
    for conjunct in _flatten_and(on):
        if isinstance(conjunct, exp.Paren):
            conjunct = conjunct.this
        if not isinstance(conjunct, exp.EQ):
            return None
        sides = []
        for side in (conjunct.this, conjunct.expression):
            if isinstance(side, exp.Column):
                sides.append(side)
                continue
            peeled = peel_key_conversion(side)
            if peeled is None:
                return None
            sides.append(peeled[0])
        pairs.append((sides[0], sides[1], conjunct.this, conjunct.expression))
    return pairs or None


def rewrites_a_key(on: exp.Expression | None) -> bool:
    """Whether an ON equates a column with a rewrite of one column that is not a
    conversion dex can measure (``regexp_replace(a.k, ...) = b.k``)."""

    if on is None:
        return False
    for conjunct in _flatten_and(on):
        if isinstance(conjunct, exp.Paren):
            conjunct = conjunct.this
        if not isinstance(conjunct, exp.EQ):
            continue
        for side in (conjunct.this, conjunct.expression):
            if (
                not isinstance(side, exp.Column)
                and peel_key_conversion(side) is None
                and wraps_one_column(side) is not None
            ):
                return True
    return False


def measuring_key_template(side: exp.Expression, column: exp.Column) -> str | None:
    """A conversion as a DuckDB-dialect template with ``{}`` where its column
    goes, for a probe to apply to whatever reference it reads the column by.

    ``None`` for a bare column. Every hard cast is made safe, so a value the
    authored cast would reject is counted as a failed conversion rather than
    failing the probe.
    """

    if side is column:
        return None
    copy = side.copy()
    placeholder = "dex_key_placeholder"
    for node in list(copy.find_all(exp.Column)):
        if match_key(node) == match_key(column):
            node.replace(exp.column(placeholder))
    for cast in list(copy.find_all(exp.Cast)):
        if isinstance(cast, exp.TryCast) or cast.args.get("safe"):
            continue
        safe = exp.TryCast(this=cast.this, to=cast.args.get("to"))
        if cast is copy:
            copy = safe
        else:
            cast.replace(safe)
    if isinstance(copy, exp.ToNumber):
        copy.set("safe", True)
    rendered = copy.sql(dialect="duckdb").replace("{", "{{").replace("}", "}}")
    return rendered.replace(placeholder, "{}")


class NoSafeCastError(DexError, ValueError):
    """The dialect has no cast that returns NULL instead of failing.

    Control flow: every caller turns it into an ``unchecked`` record with
    ``reason_code: no_safe_cast_on_connector``."""


#: Dialects with no safe cast at all: a cast to a type :func:`_guarded_cast`
#: covers is measured through that guard, and a probe that has to count the
#: values any other cast rejects cannot be written there, so it is reported
#: unchecked rather than run as a cast that fails on the first such value.
NO_SAFE_CAST = frozenset({"postgres"})

#: The integer types a guarded cast covers, with their ranges (#1075).
_GUARDED_INTEGERS = {
    exp.DataType.Type.SMALLINT: (-(2**15), 2**15 - 1),
    exp.DataType.Type.INT: (-(2**31), 2**31 - 1),
    exp.DataType.Type.BIGINT: (-(2**63), 2**63 - 1),
}

#: An integer as text: a sign, digits, surrounding whitespace. Leading zeros
#: are free, and 19 significant digits is past BIGINT, so the NUMERIC cast the
#: range test makes never meets a value too long for it. The whitespace is the
#: ASCII set the integer and numeric parsers skip: ``[[:space:]]`` follows the
#: locale and also matches Unicode spaces those parsers reject.
_INTEGER_TEXT = r"^[ \t\n\r\v\f]*[+-]?0*[0-9]{1,19}[ \t\n\r\v\f]*$"


def _guarded_cast(node: exp.TryCast, dialect: str) -> exp.Expression | None:
    """``node`` as a cast whose argument is NULL wherever the value would not
    convert, or None for a target type this does not cover.

    Every CAST here is total, as in
    :func:`~.adapters.base.type_contradiction_expressions`: its argument can
    be converted on every row, so nothing depends on the engine evaluating a
    CASE lazily (#307). The final cast reads the value as text, the form the
    shape test checked, so a numeric, float or boolean source (``2.00``,
    ``1e15``, ``true``) counts as failed even where Postgres would cast it. A
    cast nested in its argument is made safe too, which the top-down rewrite
    would not reach.
    """

    to = node.args.get("to")
    bounds = _GUARDED_INTEGERS.get(to.this) if isinstance(to, exp.DataType) else None
    if bounds is None:
        return None
    operand = with_safe_casts(node.this.copy(), dialect)
    text = exp.Cast(this=operand, to=exp.DataType.build("text"))
    # A nondeterministic collation rejects a regex, so the shape test reads C.
    c_locale = exp.to_identifier("C", quoted=True)
    c_text = exp.Collate(this=text.copy(), expression=c_locale)
    shaped = exp.RegexpLike(this=c_text, expression=exp.Literal.string(_INTEGER_TEXT))
    digits = exp.Case().when(shaped, text.copy()).else_(exp.Literal.string("0"))
    in_range = exp.Between(
        this=exp.Cast(this=digits, to=exp.DataType.build("numeric")),
        low=exp.Literal.number(bounds[0]),
        high=exp.Literal.number(bounds[1]),
    )
    value = exp.Case().when(exp.and_(shaped.copy(), in_range), text.copy())
    return exp.Cast(this=value, to=to.copy())


def dialect_safe_cast(node: exp.Expression, dialect: str) -> exp.Expression:
    """``node`` (a ``TryCast``) as a cast that returns NULL on ``dialect``.

    sqlglot renders a ``TryCast`` it did not parse from Snowflake as a plain
    ``CAST`` there, and as a ``CAST`` to a ``Nullable`` type on ClickHouse, and
    both throw on a value they cannot convert. That is a failed probe, and a
    warehouse message quoting the value. Snowflake's ``TRY_CAST`` also takes
    only text, so the input is cast to text first. Postgres has no safe cast,
    so an integer target is guarded (:func:`_guarded_cast`) and any other
    raises :class:`NoSafeCastError`.
    """

    if not isinstance(node, exp.TryCast):
        return node
    if dialect in NO_SAFE_CAST:
        guarded = _guarded_cast(node, dialect)
        if guarded is None:
            raise NoSafeCastError(dialect)
        return guarded
    to = node.args.get("to")
    if dialect == "snowflake" and node.args.get("requires_string"):
        return node  # parsed from Snowflake: already its own TRY_CAST
    if dialect == "snowflake":
        text = exp.Cast(this=node.this.copy(), to=exp.DataType.build("varchar"))
        return exp.TryCast(this=text, to=to.copy(), requires_string=True)
    if dialect == "clickhouse":
        return exp.Anonymous(
            this="accurateCastOrNull",
            expressions=[node.this.copy(), exp.Literal.string(_clickhouse_type(to))],
        )
    return node


def with_safe_casts(tree: exp.Expression, dialect: str) -> exp.Expression:
    """Every ``TryCast`` in ``tree`` made safe on ``dialect`` (see
    :func:`dialect_safe_cast`); raises :class:`NoSafeCastError` where none is."""

    return tree.transform(lambda node: dialect_safe_cast(node, dialect))


def _clickhouse_type(to: exp.Expression) -> str:
    """A ClickHouse cast target with any ``Nullable(...)`` wrapper removed."""
    rendered = to.sql("clickhouse")
    if rendered.startswith("Nullable(") and rendered.endswith(")"):
        return rendered[len("Nullable(") : -1]
    return rendered


# --- row predicates a probe can carry (#1085, #1073) --------------------------------
#
# A filter decides which rows a model reads. A probe that measures those rows has to
# carry the same filter, and it can do so only for a filter that means the same thing
# when it is moved: a deterministic, row-by-row condition over columns and literals.
# One whitelist answers "is this such a filter" for the join contract (which moves a
# CTE's filter onto the physical relation under it) and for identifier-conversion
# counts (which move a later CTE's filter back to the conversion's scope).

#: Node types a carried predicate may be built from. Anything else, a function
#: included, is refused rather than assumed deterministic and portable.
_PREDICATE_NODES: tuple[type, ...] = tuple(
    c
    for c in (
        exp.Column,
        exp.Identifier,
        exp.Literal,
        exp.Boolean,
        exp.Null,
        exp.Neg,
        exp.Paren,
        exp.And,
        exp.Or,
        exp.Not,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.Is,
        exp.In,
        exp.Between,
        exp.Like,
        exp.ILike,
        exp.Coalesce,
        exp.Cast,
        exp.TryCast,
        exp.DataType,
        exp.DataTypeParam,
        exp.Lower,
        exp.Upper,
        exp.Trim,
    )
    if isinstance(c, type)
)


def simple_row_predicate(
    predicate: exp.Expression | None, dialect: str | None = None
) -> str | None:
    """Why ``predicate`` cannot be carried into a probe as written, or None
    where it can.

    A carried predicate is a deterministic, row-by-row condition over columns
    and literals: comparisons, ``IS``, ``IN`` over a literal list, ``BETWEEN``,
    ``LIKE``, boolean logic, ``COALESCE``, casts and ``lower``/``upper``/``trim``.
    ``coalesce(_fivetran_active, true)`` is one: a bare boolean expression is a
    predicate. A subquery, an aggregate, a window, a clock or random function,
    or any other function is refused, since moving it could change what it
    means, and a probe that answers a different question is worse than none.
    A safe cast is refused on a ``dialect`` with no cast that returns NULL
    (:data:`NO_SAFE_CAST`), since the probe could not render it.
    """

    if predicate is None:
        return "there is no predicate"
    for node in predicate.walk():
        if isinstance(node, (exp.Subquery, exp.Select)):
            return "the filter holds a subquery"
        if isinstance(node, exp.AggFunc):
            return "the filter aggregates"
        if isinstance(node, exp.Window):
            return "the filter reads a window"
        if isinstance(node, exp.TryCast) and dialect in NO_SAFE_CAST:
            return "the filter uses a safe cast this connector cannot render"
        if isinstance(node, exp.In) and (
            node.args.get("query") is not None
            or node.args.get("unnest") is not None
            or node.args.get("field") is not None
        ):
            return "the filter's IN reads something other than a literal list"
        if not isinstance(node, _PREDICATE_NODES):
            name = type(node).__name__
            return f"the filter uses {name}, which a probe does not carry"
    if predicate.find(exp.Column) is None:
        return "the filter reads no column"
    return None


def _projection_map(
    select: exp.Select,
) -> tuple[dict[str, exp.Expression], bool]:
    """Each output name of ``select`` (lowered) mapped to the expression that
    computes it, and whether the select projects a bare ``*``."""

    named: dict[str, exp.Expression] = {}
    star = False
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            star = True
        elif isinstance(projection, exp.Alias):
            named[projection.alias.lower()] = projection.this
        elif isinstance(projection, exp.Column):
            if isinstance(projection.this, exp.Star):
                star = True
            else:
                named[projection.name.lower()] = projection
    return named, star


def substitute_columns(
    predicate: exp.Expression, select: exp.Select
) -> exp.Expression | None:
    """``predicate``, written over ``select``'s output columns, rewritten in
    ``select``'s own input terms: every column replaced by the expression
    ``select`` computes it with.

    Filtering a scope's output rows and filtering its input rows on the
    substituted condition keep the same rows, provided each substituted
    expression is a row-by-row function of its input. So a projection that
    aggregates, windows or holds a subquery is not substituted (``None``), and
    neither is a ``select`` that groups, deduplicates or holds a set operation.
    A bare ``*`` passes a name through unchanged, only when ``select`` reads one
    relation, since with more the name's owner is a schema question. A column
    ``select`` does not project is ``None``.
    """

    if not isinstance(select, exp.Select):
        return None
    if select.args.get("group") is not None or select.args.get("distinct"):
        return None
    named, star = _projection_map(select)
    single = len(joins(select)) == 0
    out = predicate.copy()
    for column in list(out.find_all(exp.Column)):
        source = named.get(column.name.lower())
        if source is None:
            if not (star and single):
                return None
            replacement: exp.Expression = exp.column(column.name)
        else:
            if any(
                isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select))
                for n in source.walk()
            ):
                return None
            replacement = source.copy()
        if column is out:
            out = replacement
        else:
            column.replace(
                exp.Paren(this=replacement)
                if not isinstance(replacement, (exp.Column, exp.Literal))
                else replacement
            )
    return out


def flatten_conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """A condition flattened across its top-level ANDs (parentheses around an
    AND included); empty for no condition."""

    return [] if node is None else _flatten_and(node)
