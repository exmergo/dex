"""Planted defects for a model's tests to catch, and the reading of what they caught.

A test suite that passes proves the tests ran, not that they would notice if the
model were wrong. Counting tests does not distinguish the two, and neither does
coverage in any sense dbt can report: a model with a `not_null` on its key and a
model with a unit test pinning its arithmetic both read as "tested".

The way to tell them apart is to break the model on purpose and see whether
anything complains. Each mutation here stands for a defect class that recurs in
real analytics code, so a mutant that survives is not a curiosity about SQL: it
is a sentence about the suite, that this defect could ship through it. The
report is written that way, in the defect's terms rather than as a diff, because
the reader's next action is to write a test, not to read SQL they already know.

Three properties this module keeps, all of them load-bearing:

- **Pure.** Nothing here runs dbt, opens a connection, or touches the filesystem.
  It turns SQL into other SQL and reads statuses. That is what makes the defect
  library testable without a warehouse, and it is why the caller owns every
  decision about cost.
- **Mutants never compound.** Each one is applied to a fresh copy of the parsed
  tree, so a survivor means *this* defect survived, not this defect on top of the
  previous one.
- **The identity is a mutant too.** The unmutated tree goes through the same
  render, so a round trip that changes what the tests see shows up once, in the
  baseline, instead of being blamed on every mutant separately.

Mutation runs against the model's **compiled** SQL, which is what makes it work
on real projects: dbt has already expanded the macros, the ``var()`` calls and
the ``{% if %}`` branches, so a model that plan-time attribution has to refuse
for its jinja is mutated here without complaint. The cost is that the compiled
form names physical relations, and a test fixture addresses its inputs by
``ref()``. :func:`prepare` restores those references so both halves work.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp

from .. import sql_shape
from ..errors import DexError

#: How many mutants one run may execute. Every mutant is a dbt invocation, so
#: this is a bound on wall time and on spend, not a matter of taste. A caller may
#: ask for fewer; nothing may ask for more, because the ceiling is what makes the
#: command's cost predictable before it is priced.
MAX_MUTANTS = 20

#: What every mutant is written into the copied project with. Appended rather
#: than prepended: dbt gives scalar config keys to the *last* ``config()`` call
#: in a file, so a model carrying its own ``materialized='incremental'`` would
#: otherwise win and the mutant would build a relation.
#:
#: Each key earns its place. ``ephemeral`` means dbt inlines the mutant into each
#: test as a CTE and materializes nothing, so no relation is created, replaced or
#: dropped, and there is nothing to clean up. ``contract`` off because a mutant
#: legitimately changes the shape a contract pins, and the contract failing would
#: mask whether the tests noticed. ``access='protected'`` because dbt refuses to
#: parse a `public` model that is ephemeral, which would otherwise make this
#: command unusable on exactly the models most worth testing.
EPHEMERAL_HEADER = (
    "{{ config(materialized='ephemeral', contract={'enforced': false}, "
    "access='protected') }}"
)

#: dbt's own prefix for an inlined ephemeral parent.
DBT_CTE_PREFIX = "__dbt__cte__"

#: What an operator hands back: the fragment before, the fragment after, and
#: the sentence describing the defect. ``None`` when the site turned out not to
#: be mutable after all.
Applied = tuple[str, str, str]

_PLACEHOLDER = "__dex_ref_{}__"
_PLACEHOLDER_RE = re.compile(r"__dex_ref_(\d+)__")

# A defect is only worth reporting if the reader can act on it, and the action is
# always the same shape: write the test that would have caught this. Naming that
# test per operator is what turns a finding into a next step.
_SUGGESTED_TEST = {
    "comparison": "a unit test with a row exactly on the boundary",
    "predicate_drop": "an `expression_is_true` test asserting the filter holds",
    "predicate_negate": "an `expression_is_true` test asserting the filter holds",
    "join_type": "a `relationships` test, or a row-count assertion against the parent",
    # Deliberately not `accepted_values`: a dropped branch makes its rows fall
    # through to a category that is still in the allowed list, so that test
    # cannot see it. Only an assertion that a known input lands in a known
    # category can.
    "case_branch": "a unit test with a row in each branch's category",
    "division": "a unit test pinning a known ratio",
    "window_frame": "a unit test over several rows in one partition",
    "aggregate": "a unit test with more than one row per group",
}


class MutationError(DexError):
    """A model dex will not mutate, always saying which property stopped it."""


@dataclass(frozen=True)
class ParentRelation:
    """One input of the model, in both the spellings that matter.

    ``rendered`` is how the compiled SQL names it: a physical relation for an
    ordinary parent, or dbt's ``__dbt__cte__`` alias for one that is ephemeral.
    ``jinja`` is how a dbt file has to name it for a unit test fixture to bind.
    """

    rendered: str
    jinja: str
    ephemeral: bool = False


@dataclass
class PreparedModel:
    """A model's compiled SQL, parsed, with its inputs turned back into refs."""

    tree: exp.Expression
    dialect: str
    refs: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def readable(self, text: str) -> str:
        """The same text with dex's internal ref placeholders spelled as names.

        Placeholders exist so a reference survives SQL generation as an
        identifier. They must never reach a person: a finding that says the join
        to ``__dex_ref_1__`` changed names nothing the reader can act on.
        """

        def name(match: re.Match[str]) -> str:
            jinja = self.refs.get(match.group(0), match.group(0))
            quoted = re.search(r"'([^']+)'\s*\)\s*\}\}", jinja)
            return quoted.group(1) if quoted else jinja

        return _PLACEHOLDER_RE.sub(name, text)


@dataclass(frozen=True)
class Mutant:
    """One planted defect: what it is, where, and what would have caught it."""

    id: str
    operator: str
    scope: str
    defect: str
    before: str
    after: str
    suggested_test: str
    body: str

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "operator": self.operator,
            "scope": self.scope,
            "defect": self.defect,
            "before": self.before,
            "after": self.after,
            "suggested_test": self.suggested_test,
        }


@dataclass
class MutantBatch:
    """The mutants a run will execute, and an honest account of the rest.

    ``elided`` is per operator rather than a single number because the cap is
    spread across defect classes: a caller who sees five comparison mutants and
    no join mutant has to be able to tell "this model has no joins" from "the cap
    cut the joins off".
    """

    identity: str
    mutants: list[Mutant] = field(default_factory=list)
    elided: dict[str, int] = field(default_factory=dict)
    considered: int = 0
    unparsed: int = 0
    cap: int = MAX_MUTANTS

    @property
    def elided_total(self) -> int:
        return sum(self.elided.values())


@dataclass(frozen=True)
class Verdict:
    """What one mutant's run says about the suite."""

    outcome: str
    caught_by: list[str] = field(default_factory=list)
    warn_only: bool = False


# --- preparing the model -------------------------------------------------------


def prepare(
    compiled_code: str,
    *,
    dialect: str,
    parents: Sequence[ParentRelation] = (),
) -> PreparedModel:
    """Parse a model's compiled SQL and put its ``ref()`` and ``source()`` back.

    dbt compiles a reference into a physical relation name, and inlines an
    ephemeral parent as a ``__dbt__cte__`` CTE. Both have to be undone: a unit
    test binds its fixtures to the *reference*, so a mutant that still named the
    warehouse relation would quietly read real data in a test that was supposed
    to be reading fixtures, and would pass for the wrong reason.

    Matching is exact on the parsed relation, never on a suffix. dbt renders a
    ``relation_name`` through the same code that renders a ``ref()``, so the two
    texts agree by construction, and an exact match is therefore available; a
    tolerant one would risk binding two same-named relations in different
    databases to a single reference, which silently mutates the wrong model.
    A relation matching no parent is left exactly as written and noted, since
    dex would rather read a hardcoded table honestly than guess at a reference
    the project never declared.
    """

    try:
        tree = sqlglot.parse_one(compiled_code, read=dialect)
    except Exception as exc:
        raise MutationError(
            f"dex could not parse this model's compiled SQL ({_clip(str(exc))}), "
            "so it cannot plant a defect in it without guessing"
        ) from exc
    if tree is None:
        raise MutationError("the model's compiled SQL is empty")
    if not isinstance(tree, (exp.Select, exp.SetOperation, exp.With, exp.Subquery)):
        raise MutationError(
            f"the model's compiled SQL is a {type(tree).__name__.lower()} rather "
            "than a single query, and dex will not mutate a statement it cannot "
            "reason about as one SELECT"
        )

    prepared = PreparedModel(tree=tree, dialect=dialect)
    by_rendered = {p.rendered: p for p in parents}

    # The ephemeral parents first: they are CTEs in this tree, and removing one
    # has to happen before its references are rewritten, or the CTE body itself
    # gets a placeholder pointing at the parent it *is*.
    cte_jinja: dict[str, str] = {}
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None:
        keep = []
        for cte in list(with_clause.expressions):
            alias = cte.alias_or_name
            if not alias.startswith(DBT_CTE_PREFIX):
                keep.append(cte)
                continue
            parent = by_rendered.get(alias)
            if parent is None:
                raise MutationError(
                    f"the compiled SQL inlines '{alias}', which dex cannot match to "
                    "any of this model's declared parents, so it cannot restore the "
                    "reference it stands for"
                )
            cte_jinja[alias.lower()] = parent.jinja
        if keep:
            with_clause.set("expressions", keep)
        else:
            tree.set("with_", None)
            tree.set("with", None)

    for table in tree.find_all(exp.Table):
        bare = _relation_text(table, dialect)
        parent = by_rendered.get(bare)
        jinja = parent.jinja if parent is not None else cte_jinja.get(bare.lower())
        if jinja is None:
            if not _names_a_scope(table, tree):
                prepared.notes.append(
                    f"{bare} matches none of this model's declared parents and is "
                    "left as written; a unit test fixture cannot stand in for it"
                )
            continue
        token = _PLACEHOLDER.format(len(prepared.refs))
        prepared.refs[token] = jinja
        table.set("this", exp.to_identifier(token, quoted=False))
        table.set("db", None)
        table.set("catalog", None)

    return prepared


def render(prepared: PreparedModel, tree: exp.Expression | None = None) -> str:
    """A parsed tree back to a dbt model file: refs restored, the rest inert.

    Everything that is not a reference is wrapped in ``{% raw %}``. Compiled SQL
    is data as far as this command is concerned, and dbt would otherwise render a
    ``{{`` inside a string literal or a comment a second time, which is both a
    wrong mutant and a way to make dbt evaluate text that came out of a
    warehouse.
    """

    sql = (tree if tree is not None else prepared.tree).sql(
        dialect=prepared.dialect, pretty=True
    )
    if "endraw" in sql:
        raise MutationError(
            "the model's compiled SQL contains the text 'endraw', which dex "
            "cannot safely quote for dbt; mutation coverage is unavailable here"
        )
    out: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER_RE.finditer(sql):
        literal = sql[cursor : match.start()]
        if literal:
            out.append("{% raw %}" + literal + "{% endraw %}")
        out.append(prepared.refs.get(match.group(0), match.group(0)))
        cursor = match.end()
    tail = sql[cursor:]
    if tail:
        out.append("{% raw %}" + tail + "{% endraw %}")
    return "".join(out) + "\n" + EPHEMERAL_HEADER + "\n"


# --- the defect library --------------------------------------------------------


def _comparison_sites(tree: exp.Expression) -> list[exp.Expression]:
    # Boundary comparisons only. Flipping `=` to `<>` is a different defect and,
    # in a join condition, one the warehouse or the tests reject immediately,
    # which would spend a run to learn nothing.
    return list(tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE))


def _flip_comparison(node: exp.Expression, dialect: str) -> Applied | None:
    pairs = {exp.GT: exp.GTE, exp.GTE: exp.GT, exp.LT: exp.LTE, exp.LTE: exp.LT}
    replacement = pairs[type(node)](this=node.this, expression=node.expression)
    before, after = _text(dialect, node), _text(dialect, replacement)
    node.replace(replacement)
    inclusive = isinstance(replacement, (exp.GTE, exp.LTE))
    return (
        before,
        after,
        f"the boundary '{before}' "
        f"{'now includes' if inclusive else 'now excludes'} the edge value "
        f"('{after}'), so a row sitting exactly on it "
        f"{'enters' if inclusive else 'leaves'} the model",
    )


def _predicate_sites(tree: exp.Expression) -> list[exp.Expression]:
    sites: list[exp.Expression] = []
    for select in tree.find_all(exp.Select):
        for clause in ("where", "having", "qualify"):
            sites.extend(sql_shape.predicates(select, clause))
    return sites


def _drop_predicate(node: exp.Expression, dialect: str) -> Applied | None:
    select, clause = _owning_clause(node)
    if select is None:
        return None
    before = _text(dialect, node)
    kept = [
        p
        for p in sql_shape.predicates(select, clause)
        if sql_shape.match_key(p) != sql_shape.match_key(node)
    ]
    sql_shape.set_predicates(select, clause, kept)
    return (
        before,
        "",
        f"the filter '{before}' stops filtering, so every row it was excluding "
        "now flows into the model",
    )


def _negate_predicate(node: exp.Expression, dialect: str) -> Applied | None:
    select, clause = _owning_clause(node)
    if select is None:
        return None
    before = _text(dialect, node)
    negated = exp.Not(this=exp.Paren(this=node.copy()))
    swapped = [
        negated if sql_shape.match_key(p) == sql_shape.match_key(node) else p
        for p in sql_shape.predicates(select, clause)
    ]
    sql_shape.set_predicates(select, clause, swapped)
    return (
        before,
        _text(dialect, negated),
        f"the filter '{before}' is inverted, so the model keeps exactly the rows "
        "it was written to exclude",
    )


def _join_sites(tree: exp.Expression) -> list[exp.Expression]:
    sites: list[exp.Expression] = []
    for select in tree.find_all(exp.Select):
        sites.extend(
            join.node
            for join in sql_shape.joins(select)
            if join.side in {"inner", "left"}
        )
    return sites


def _retype_join(node: exp.Expression, dialect: str) -> Applied | None:
    side = " ".join(p for p in (node.side or "", node.kind or "") if p).lower()
    relation = _text(dialect, node.this)
    if side == "left":
        node.set("side", None)
        node.set("kind", "INNER")
        return (
            f"left join {relation}",
            f"inner join {relation}",
            f"the left join to {relation} became an inner join, so every row "
            "whose match is missing is silently dropped from the model",
        )
    node.set("side", "LEFT")
    node.set("kind", None)
    return (
        f"inner join {relation}",
        f"left join {relation}",
        f"the inner join to {relation} became a left join, so rows that should "
        "have been filtered out survive, carrying nulls",
    )


def _case_sites(tree: exp.Expression) -> list[exp.Expression]:
    return [c for c in tree.find_all(exp.Case) if (c.args.get("ifs") or [])]


def _drop_case_branch(node: exp.Expression, dialect: str) -> Applied | None:
    branches = list(node.args.get("ifs") or [])
    dropped = branches[0]
    # A lone branch prints as a whole `CASE WHEN ... END`, which reads as though
    # the entire expression were the thing being dropped.
    before = (
        f"when {_text(dialect, dropped.this)} "
        f"then {_text(dialect, dropped.args.get('true'))}"
    )
    remaining = branches[1:]
    if remaining:
        node.set("ifs", remaining)
        after = _text(dialect, node)
    else:
        # A CASE with no branches is not printable, so the whole expression
        # collapses to whatever it would have fallen through to.
        fallback = node.args.get("default") or exp.null()
        after = _text(dialect, fallback)
        node.replace(fallback)
    return (
        before,
        after,
        f"the branch '{before}' is gone, so the category it handled falls "
        "through to the default and is reported as something it is not",
    )


def _division_sites(tree: exp.Expression) -> list[exp.Expression]:
    return list(tree.find_all(exp.Div, exp.SafeDivide))


def _swap_division(node: exp.Expression, dialect: str) -> Applied | None:
    before = _text(dialect, node)
    numerator, denominator = node.this, node.expression
    node.set("this", denominator)
    node.set("expression", numerator)
    after = _text(dialect, node)
    # A guarded division (Snowflake's DIV0, a NULLIF wrapper) tests the
    # denominator by name, and swapping the operands without the guard leaves it
    # checking the operand that is no longer the divisor.
    guard = _enclosing_zero_guard(node, denominator)
    if guard is not None:
        guard.set("this", numerator.copy())
    return (
        before,
        after,
        f"the ratio '{before}' is inverted, so it reports the reciprocal of the "
        "rate it is named for",
    )


def _window_sites(tree: exp.Expression) -> list[exp.Expression]:
    return [
        spec
        for spec in tree.find_all(exp.WindowSpec)
        if any(
            isinstance(spec.args.get(bound), exp.Literal)
            and not spec.args[bound].args.get("is_string")
            for bound in ("start", "end")
        )
    ]


def _shift_window_frame(node: exp.Expression, dialect: str) -> Applied | None:
    for bound in ("start", "end"):
        literal = node.args.get(bound)
        if not isinstance(literal, exp.Literal) or literal.args.get("is_string"):
            continue
        try:
            value = int(literal.this)
        except (TypeError, ValueError):
            continue
        before = _text(dialect, node)
        node.set(bound, exp.Literal.number(value + 1))
        return (
            before,
            _text(dialect, node),
            f"the window frame reaches one row further than it should "
            f"({value} became {value + 1}), so every rolling value is computed "
            "over the wrong span",
        )
    return None


def _aggregate_sites(tree: exp.Expression) -> list[exp.Expression]:
    return list(tree.find_all(exp.Sum, exp.Max))


def _swap_aggregate(node: exp.Expression, dialect: str) -> Applied | None:
    before = _text(dialect, node)
    replacement = (exp.Max if isinstance(node, exp.Sum) else exp.Sum)(this=node.this)
    node.replace(replacement)
    after = _text(dialect, replacement)
    original, swapped = (
        ("total", "largest single value")
        if isinstance(node, exp.Sum)
        else ("largest single value", "total")
    )
    return (
        before,
        after,
        f"'{before}' now reports the {swapped} rather than the {original}, which "
        "agrees with it whenever a group holds exactly one row",
    )


@dataclass(frozen=True)
class _Operator:
    name: str
    sites: Callable[[exp.Expression], list[exp.Expression]]
    apply: Callable[[exp.Expression, str], Applied | None]


# Ordered as the issue's defect table is, and iterated round robin, so a cap cuts
# the tail of each class rather than everything after the first.
_OPERATORS: tuple[_Operator, ...] = (
    _Operator("comparison", _comparison_sites, _flip_comparison),
    _Operator("predicate_drop", _predicate_sites, _drop_predicate),
    _Operator("join_type", _join_sites, _retype_join),
    _Operator("case_branch", _case_sites, _drop_case_branch),
    _Operator("division", _division_sites, _swap_division),
    _Operator("window_frame", _window_sites, _shift_window_frame),
    _Operator("aggregate", _aggregate_sites, _swap_aggregate),
    _Operator("predicate_negate", _predicate_sites, _negate_predicate),
)


def enumerate_mutants(
    prepared: PreparedModel, *, cap: int = MAX_MUTANTS
) -> MutantBatch:
    """Every defect this model can carry, ordered so a cap stays representative.

    Sites are counted on the original tree and each mutant is then applied to a
    fresh copy, which is what keeps them independent: mutant seven is this model
    with one defect, not with seven.

    The round robin is the whole reason the order is not simply "all comparisons,
    then all joins". A model with forty comparisons and one join would otherwise
    spend an entire capped run proving things about comparisons and never test
    whether anything catches a join defect, which is the more expensive bug.
    """

    cap = max(0, min(cap, MAX_MUTANTS))
    identity = render(prepared)

    planned: list[tuple[str, int]] = []
    counts: dict[str, int] = {}
    for operator in _OPERATORS:
        found = len(operator.sites(prepared.tree))
        counts[operator.name] = found
        planned.extend((operator.name, index) for index in range(found))

    ordered = _round_robin(planned)
    batch = MutantBatch(identity=identity, considered=len(ordered), cap=cap)
    by_name = {operator.name: operator for operator in _OPERATORS}

    for operator_name, index in ordered:
        if len(batch.mutants) >= cap:
            batch.elided[operator_name] = batch.elided.get(operator_name, 0) + 1
            continue
        operator = by_name[operator_name]
        tree = prepared.tree.copy()
        sites = operator.sites(tree)
        if index >= len(sites):
            continue
        node = sites[index]
        scope = _scope_label(node, tree)
        applied = operator.apply(node, prepared.dialect)
        if applied is None:
            continue
        before, after, defect = (prepared.readable(part) for part in applied)
        try:
            body = render(prepared, tree)
            # A mutant that cannot be re-read is a mutant dex cannot stand
            # behind, so it is dropped here rather than sent to the warehouse to
            # fail there and be reported as a defect the tests "rejected".
            sqlglot.parse_one(
                _PLACEHOLDER_RE.sub("x", tree.sql(dialect=prepared.dialect)),
                read=prepared.dialect,
            )
        except Exception:
            batch.unparsed += 1
            continue
        batch.mutants.append(
            Mutant(
                id=f"m{len(batch.mutants) + 1:02d}",
                operator=operator_name,
                scope=scope,
                defect=defect,
                before=before,
                after=after,
                suggested_test=_SUGGESTED_TEST[operator_name],
                body=body,
            )
        )
    return batch


# --- reading the runs ----------------------------------------------------------


def classify(
    baseline: dict[str, str],
    run: dict[str, str] | None,
    *,
    warn_severity: Iterable[str] = (),
) -> Verdict:
    """What one mutant's test statuses say, judged only against what passed before.

    Only the tests that passed at baseline can testify. A test that was already
    failing says nothing about this defect, and counting it as a catch would
    report a suite as strong precisely because it was broken.

    ``rejected`` is kept apart from ``killed`` deliberately. A mutant every test
    errors on is one the warehouse refused to run, so a real change of that shape
    would fail the build outright rather than ship: that is not evidence the
    tests would have caught it, and folding it into ``killed`` would flatter the
    suite.
    """

    if not run:
        return Verdict(outcome="not_run")
    passing = [name for name, status in baseline.items() if status == "pass"]
    missing = [name for name in passing if name not in run]
    if missing:
        return Verdict(outcome="not_run")

    caught = [n for n in passing if run[n] in {"fail", "warn"}]
    errored = [n for n in passing if run[n] == "error"]
    if caught:
        warned = set(warn_severity)
        return Verdict(
            outcome="killed",
            caught_by=sorted(caught),
            warn_only=all(run[n] == "warn" or n in warned for n in caught),
        )
    if errored:
        return Verdict(outcome="rejected", caught_by=sorted(errored))
    return Verdict(outcome="survived")


def inline_into_test(
    test_sql: str, *, model_name: str, body: str, dialect: str
) -> str | None:
    """A test's compiled SQL with the mutant standing in for the model.

    Only used for pricing, and only on connectors that charge by what a statement
    scans: a mutant that drops a partition predicate scans more than the model it
    came from, so pricing every mutant at the baseline's cost would under-report
    the batch, which is the one direction a cost guard must never round.

    Substring replacement is not available here. dbt inlines a model's ephemeral
    parents into the model's own compiled SQL, so the text in the manifest and
    the text inside the test's CTE are different strings whenever the model has
    an ephemeral parent. Returns ``None`` when the CTE cannot be found or the
    test will not parse, and the caller prices that test at its baseline.
    """

    try:
        tree = sqlglot.parse_one(test_sql, read=dialect)
        replacement = sqlglot.parse_one(body, read=dialect)
    except Exception:
        return None
    if tree is None or replacement is None:
        return None
    alias = f"{DBT_CTE_PREFIX}{model_name}".lower()
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is None:
        return None
    for cte in with_clause.expressions:
        if cte.alias_or_name.lower() == alias:
            cte.set("this", replacement)
            return tree.sql(dialect=dialect)
    return None


# --- helpers -------------------------------------------------------------------


def _round_robin(planned: list[tuple[str, int]]) -> list[tuple[str, int]]:
    by_operator: dict[str, list[tuple[str, int]]] = {}
    for entry in planned:
        by_operator.setdefault(entry[0], []).append(entry)
    ordered: list[tuple[str, int]] = []
    while any(by_operator.values()):
        for operator in _OPERATORS:
            queue = by_operator.get(operator.name)
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def _text(dialect: str, node: exp.Expression | None) -> str:
    """A fragment as the model's own dialect spells it.

    :func:`sql_shape.text` is dialect free, which is right for matching and
    wrong for a report: read as duckdb, ``a / b`` carries a safe-division flag
    that the default dialect prints as ``a / NULLIF(b, 0)``. Showing a reader SQL
    that is not what will run undermines the one thing a finding has to be.
    """

    return "" if node is None else node.sql(dialect=dialect, comments=False).strip()


def _relation_text(table: exp.Table, dialect: str) -> str:
    """A relation's identity with any alias removed, in the dialect's spelling."""

    bare = table.copy()
    bare.set("alias", None)
    return bare.sql(dialect=dialect)


def _names_a_scope(table: exp.Table, tree: exp.Expression) -> bool:
    """True when this table reference is really a CTE name defined in the tree."""

    return table.name.lower() in {name.lower() for name in sql_shape.scopes(tree)}


def _owning_clause(node: exp.Expression) -> tuple[exp.Select | None, str]:
    """The SELECT whose WHERE/HAVING/QUALIFY this predicate belongs to."""

    clauses = (
        (exp.Where, "where"),
        (exp.Having, "having"),
        (exp.Qualify, "qualify"),
    )
    for clause, key in clauses:
        owner = node.find_ancestor(clause)
        if owner is not None:
            select = owner.find_ancestor(exp.Select)
            if select is not None:
                return select, key
    return None, ""


def _enclosing_zero_guard(
    node: exp.Expression, denominator: exp.Expression
) -> exp.EQ | None:
    """The ``denominator = 0`` test guarding this division, if there is one.

    Snowflake's ``DIV0`` does not survive a parse and a print: it comes back as a
    conditional testing the divisor by name. Swapping the operands underneath
    that guard would leave it checking a column that is no longer the divisor.
    """

    conditional = node.find_ancestor(exp.If, exp.Case)
    if conditional is None:
        return None
    wanted = sql_shape.match_key(denominator)
    for candidate in conditional.find_all(exp.EQ):
        other = candidate.expression
        if (
            isinstance(other, exp.Literal)
            and str(other.this) == "0"
            and sql_shape.match_key(candidate.this) == wanted
        ):
            return candidate
    return None


def _scope_label(node: exp.Expression, tree: exp.Expression) -> str:
    """Where in the model a site sits, named the way its author would name it."""

    select = node if isinstance(node, exp.Select) else node.find_ancestor(exp.Select)
    if select is None:
        return sql_shape.MAIN_SCOPE
    cte = select.find_ancestor(exp.CTE)
    if cte is not None:
        return cte.alias_or_name
    union = select.find_ancestor(exp.SetOperation)
    if union is not None:
        branches = list(union.find_all(exp.Select))
        if select in branches:
            return f"branch {branches.index(select) + 1} of the final union"
    return sql_shape.MAIN_SCOPE


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."
