"""Drafting: the measured half of a declaration, read off the cache and the SQL.

A declaration (#491) has two halves. The intent half (what the model is for,
which rows belong, what was assumed) is the author's. The measured half is
already known to dex: ``explore`` proved the upstream keys, verified the joins,
flagged the PII, recorded the NULL rates and value domains, and noticed the
running totals. None of that reached the plan of a model built on top of them,
so an agent either probed again or guessed (#493).

For every model a plan creates or edits, this module returns three parts:

- ``drafted``: what the cache and the SQL support, each fact with the evidence
  behind it. A draft is never written into the project. It is offered, so the
  author can confirm it into a declaration or override it.
- ``declared``: what the project's YAML says once the plan lands, which is the
  payload's declarations rendered over whatever the entry already carried.
- ``missing``: the intent fields nobody has supplied, each also a warning.

Two rules hold throughout. Drafting reads the repository and the cache and
nothing else, so it is free on every connector and opens no connection; an
upstream relation the cache has not profiled is named with the command that
would profile it, rather than skipped. And a fact dex cannot support is left
out with the reason stated, never approximated: a join whose multiplicity is
unknown does not keep a key unique, and a column on the outer side of a join is
not drafted as non-null.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import yaml

from ..cache import ColumnProfile, Dataset, DexCache, match_identifier
from ..guards.pii_meta import entry_meta
from .declarations import DEX_META_KEY, model_sql_path

if TYPE_CHECKING:
    from ..references import ReferenceIndex

#: The model-level intent fields every declaration is expected to state, in the
#: order a reader would ask about them.
INTENT_FIELDS = ("description", "grain", "population", "assumptions")

#: Column fields a declaration states and dex renders; read back into
#: ``declared`` the same way :func:`.declarations.render` wrote them.
_COLUMN_FIELDS = (
    "role",
    "null_rule",
    "aggregation",
    "additivity",
    "unit",
    "references",
)

_INTEGER_TYPES = ("int", "integer", "bigint", "smallint", "tinyint", "hugeint")
_PERCENT_HINTS = ("pct", "percent", "percentage")


@dataclass
class _Origin:
    """Where an output column comes from, when it passes through unchanged."""

    relation: str
    column: str
    #: The outer join that can make this column NULL where it found no match.
    outer_join: str | None = None


@dataclass
class Usage:
    """One thing a model does with one upstream column: a filter conjunct, a
    ``CASE`` condition, or an aggregation over it.

    ``expression`` is the fragment as sqlglot renders it, with every column the
    lineage resolved rewritten to its bare upstream name, so ``o.status <> 'x'``
    in one model and ``status != 'x'`` in another read as the same decision.
    ``literals`` is set only for a bare-column comparison (``=``, ``<>``, ``IN``,
    ``NOT IN``) against literals, the one shape whose literals can be checked
    against a value domain; ``lower(status) = 'x'`` compares something else.
    """

    relation: str
    column: str
    clause: str
    expression: str
    literals: list[str | Decimal] | None = None


@dataclass
class _Reading:
    """Everything one model's SQL says, structurally."""

    origins: dict[str, _Origin] = field(default_factory=dict)
    produced: list[str] | None = None
    grain: list[str] | None = None
    grain_evidence: str | None = None
    grain_withheld: str | None = None
    #: Columns the SQL filters or branches on, resolved to their upstream origin:
    #: (relation, column) -> the clauses that use it.
    used: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    #: Each filter conjunct, ``CASE`` condition and aggregation, one fragment
    #: at a time, for precedent and the literal check.
    usages: list[Usage] = field(default_factory=list)
    #: Column names a usage fragment reads whose lineage dex could not follow,
    #: so a reader of one is not mistaken for a model that ignores it.
    untraced: set[str] = field(default_factory=set)
    #: Bare column names the SQL filters on, for the population hint.
    filtered: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    #: Placeholder identifier -> the dbt relation name it stands for.
    tokens: dict[str, str] = field(default_factory=dict)


def draft(
    models: Iterable[str],
    files: Mapping[str, str],
    model_paths: Iterable[str],
    cache: DexCache | None,
    *,
    dialect: str,
    vars: Mapping[str, Any] | None = None,
    domain_cap: int = 25,
    cumulative: Mapping[str, set[str]] | None = None,
    cache_note: str | None = None,
    index: Callable[[], ReferenceIndex] | None = None,
    prior: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Each model's ``drafted``/``declared``/``missing``, and the warnings.

    ``files`` is the project as it reads once the plan lands. ``cache`` has had
    any ``pii_overrides`` applied already. ``cumulative`` maps a cache identifier
    to the columns the cumulative detector flagged there. ``cache_note`` says
    why the cache is absent, where it is. ``index`` builds the reference index
    of that same project state, called at most once and only when a model uses
    a column precedent can be read for; without it no precedent is drafted.
    ``prior`` is the project as it reads before the plan; with it, a model that
    already exists also carries ``contradictions`` (see :mod:`.contradictions`),
    whose warnings the caller renders once row attribution has run.
    """

    model_paths = list(model_paths)
    cumulative = cumulative or {}
    out: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    built: list[ReferenceIndex] = []

    def reference_index() -> ReferenceIndex | None:
        if index is None:
            return None
        if not built:
            built.append(index())
        return built[0]

    for model in sorted(set(models)):
        sql_path = model_sql_path(files, model_paths, model)
        if sql_path is None:
            continue
        declared = read_declared(files, model)
        reading = read(files[sql_path], cache, dialect=dialect, vars=vars)
        drafted = _drafted(
            reading,
            cache,
            files,
            model_paths,
            domain_cap=domain_cap,
            cumulative=cumulative,
            cache_note=cache_note,
        )
        if reading.usages and (found := reference_index()) is not None:
            precedent = _precedent(
                model, reading, files, model_paths, cache, found, dialect, vars
            )
            if precedent:
                drafted["precedent"] = precedent
        missing = _missing(declared, reading)
        found = _contradictions(
            model,
            reading,
            declared,
            files,
            prior,
            model_paths,
            cache,
            dialect=dialect,
            vars=vars,
            cumulative=cumulative,
            warnings=warnings,
        )
        model_warnings = _warnings(
            model,
            declared,
            drafted,
            reading,
            missing,
            grain_judged=any(c["check"] == "grain" for c in found or []),
        )
        model_warnings.extend(_literal_warnings(model, reading, cache))
        out[model] = {
            "drafted": _untoken(drafted, reading.tokens),
            "declared": declared,
            "missing": _untoken(missing, reading.tokens),
        }
        if found is not None:
            out[model]["contradictions"] = found
        warnings.extend(_untoken(model_warnings, reading.tokens))
    return out, warnings


def _contradictions(
    model: str,
    reading: _Reading,
    declared: dict[str, Any],
    files: Mapping[str, str],
    prior: Mapping[str, str] | None,
    model_paths: list[str],
    cache: DexCache | None,
    *,
    dialect: str,
    vars: Mapping[str, Any] | None,
    cumulative: Mapping[str, set[str]],
    warnings: list[str],
) -> list[dict[str, Any]] | None:
    """What this edit contradicts in the model's declaration, or None where
    there is nothing to compare: a model the plan creates has no prior."""

    if prior is None:
        return None
    prior_path = model_sql_path(prior, model_paths, model)
    if prior_path is None:
        return None
    from .contradictions import check

    # Commentary on the plan, like the draft around it: a defect here is a
    # warning naming the exception, never a draft or a plan that fails to exist.
    try:
        found = check(
            model,
            prior_reading=read(prior[prior_path], cache, dialect=dialect, vars=vars),
            reading=reading,
            prior_declared=read_declared(prior, model),
            declared=declared,
            prior_files=prior,
            files=files,
            model_paths=model_paths,
            cache=cache,
            cumulative=cumulative,
        )
    except Exception as exc:
        warnings.append(
            f"could not check '{model}' against its declaration "
            f"({type(exc).__name__}: {exc}); the plan itself is unaffected"
        )
        return None
    return [item.as_dict() for item in found]


def _untoken(value: Any, tokens: Mapping[str, str]) -> Any:
    """``value`` with every parse placeholder put back to the relation it
    stands for, so no internal name reaches a reader."""

    if isinstance(value, str):
        # Longest first, so `dex_relation_1` cannot rewrite part of `_10`.
        for token in sorted(tokens, key=len, reverse=True):
            value = value.replace(token, tokens[token])
        return value
    if isinstance(value, list):
        return [_untoken(item, tokens) for item in value]
    if isinstance(value, dict):
        return {_untoken(k, tokens): _untoken(v, tokens) for k, v in value.items()}
    return value


# --- reading what the project declares ------------------------------------------


def read_declared(files: Mapping[str, str], model: str) -> dict[str, Any]:
    """What the model's YAML entry declares, in the declaration's own shape."""

    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if isinstance(entry, dict) and entry.get("name") == model:
                return _declared_from(entry)
    return {}


def _declared_from(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(entry.get("description"), str) and entry["description"].strip():
        out["description"] = entry["description"]
    dex = entry_meta(entry).get(DEX_META_KEY)
    if isinstance(dex, dict):
        for key in ("grain", "population", "assumptions"):
            if dex.get(key):
                out[key] = dex[key]
    columns: dict[str, Any] = {}
    for column in entry.get("columns") or []:
        if not isinstance(column, dict) or not isinstance(column.get("name"), str):
            continue
        stated: dict[str, Any] = {}
        if isinstance(column.get("description"), str) and column["description"]:
            stated["description"] = column["description"]
        column_dex = entry_meta(column).get(DEX_META_KEY)
        if isinstance(column_dex, dict):
            stated.update({k: column_dex[k] for k in _COLUMN_FIELDS if k in column_dex})
        if stated:
            columns[column["name"]] = stated
    if columns:
        out["columns"] = columns
    return out


# --- reading the SQL --------------------------------------------------------------


def read(
    sql: str,
    cache: DexCache | None,
    *,
    dialect: str,
    vars: Mapping[str, Any] | None,
) -> _Reading:
    reading = _Reading()
    try:
        from ..guards import dialect as dialect_guard

        dialect_guard.ensure_available()
    except Exception:
        reading.incomplete.append(
            "the SQL was not read: sqlglot is not installed, so nothing was "
            "drafted from it"
        )
        return reading

    import sqlglot

    from .row_attribution import UnattributableError, render_model_sql

    relations = reading.tokens

    def resolve(name: str) -> str:
        # A placeholder rather than the dbt name, so `with orders as (select *
        # from {{ ref('orders') }})` cannot read the CTE as its own source.
        for token, known in relations.items():
            if known == name:
                return token
        token = f"dex_relation_{len(relations)}"
        relations[token] = name
        return token

    try:
        rendered = render_model_sql(sql, resolve, vars)
    except UnattributableError as exc:
        reading.incomplete.append(f"the SQL was not read statically: {exc}")
        return reading
    try:
        tree = sqlglot.parse_one(rendered, dialect=dialect)
    except sqlglot.errors.ParseError as exc:
        reading.incomplete.append(
            f"the SQL did not parse ({type(exc).__name__}), so nothing was drafted "
            "from it"
        )
        return reading

    _Lineage(tree, relations, cache, reading).run()
    return reading


class _Lineage:
    """Column lineage and key propagation through one statement's scopes."""

    def __init__(
        self,
        tree: Any,
        relations: Mapping[str, str],
        cache: DexCache | None,
        reading: _Reading,
    ) -> None:
        from .. import sql_shape

        self.shape = sql_shape
        self.tree = tree
        self.relations = relations
        self.cache = cache
        self.reading = reading
        self.ctes = {
            name: scope
            for name, scope in sql_shape.scopes(tree).items()
            if name != sql_shape.MAIN_SCOPE
        }
        self._outputs: dict[int, dict[str, Any] | None] = {}

    def run(self) -> None:
        from sqlglot import expressions as exp

        shape = self.shape
        if shape.SET_OPERATIONS and isinstance(self.tree, shape.SET_OPERATIONS):
            self.reading.incomplete.append(
                "the model is a set operation (UNION and the like), so no column "
                "or key was carried through it"
            )
            return
        if not isinstance(self.tree, exp.Select):
            self.reading.incomplete.append("the model's SQL is not a SELECT")
            return

        outputs = self.outputs(self.tree)
        if outputs is None:
            self.reading.incomplete.append(
                "the final SELECT list could not be read (a `*` over a relation "
                "the cache has no columns for), so no column was drafted"
            )
        else:
            self.reading.produced = list(outputs)
            for name, expression in outputs.items():
                origin = self.origin(self.tree, expression)
                if origin is not None:
                    self.reading.origins[name] = origin

        keys, evidence = self.keys(self.tree)
        if keys:
            self.reading.grain = sorted(keys[0], key=self._output_order(outputs))
            self.reading.grain_evidence = evidence
        else:
            self.reading.grain_withheld = evidence

        for scope in [*self.ctes.values(), self.tree]:
            self._collect_usage(scope)

        # Every relation the statement reads, in the order it names them: the
        # `ref()`/`source()` calls, and any table written into the SQL directly.
        named = []
        for table in self.tree.find_all(exp.Table):
            kind, target = self.target(table)
            if kind == "physical" and target not in named:
                named.append(target)
        self.reading.relations = named

    @staticmethod
    def _output_order(outputs: dict[str, Any] | None):
        order = {name: i for i, name in enumerate(outputs or {})}
        return lambda name: (order.get(name, len(order)), name)

    # -- sources and outputs ------------------------------------------------------

    def sources(self, select: Any) -> list[tuple[str, Any, Any]]:
        """``(alias, relation, join)`` for the FROM relation and every join."""

        found = []
        driving = self.shape.from_relation(select)
        if driving is not None:
            found.append((self.alias(driving), driving, None))
        found.extend(
            (self.alias(join.relation), join.relation, join)
            for join in self.shape.joins(select)
        )
        return found

    def alias(self, relation: Any) -> str:
        """The name a column qualifier uses for ``relation``.

        An unaliased ``{{ ref('stg_orders') }}`` renders to a table named
        ``stg_orders``, so ``stg_orders.status`` qualifies it, even though the
        statement was parsed with a placeholder in its place.
        """

        from sqlglot import expressions as exp

        if (
            isinstance(relation, exp.Table)
            and not relation.alias
            and relation.name in self.relations
        ):
            return self.relations[relation.name].rsplit(".", 1)[-1].lower()
        return relation.alias_or_name.lower()

    def target(self, relation: Any) -> tuple[str, Any]:
        """``("physical", name)``, ``("scope", select)`` or ``("unknown", None)``."""

        from sqlglot import expressions as exp

        if isinstance(relation, exp.Subquery) and isinstance(relation.this, exp.Select):
            return "scope", relation.this
        if isinstance(relation, exp.Table):
            name = relation.name
            if not relation.db and name in self.relations:
                return "physical", self.relations[name]
            if not relation.db and name.lower() in self.ctes:
                return "scope", self.ctes[name.lower()]
            return "physical", ".".join(
                part for part in (relation.catalog, relation.db, name) if part
            )
        return "unknown", None

    def columns_of(self, relation: Any) -> list[str] | None:
        kind, target = self.target(relation)
        if kind == "scope":
            outputs = self.outputs(target)
            return None if outputs is None else list(outputs)
        if kind == "physical":
            dataset = dataset_for(self.cache, target)
            if dataset is not None and dataset.columns:
                return [column.name for column in dataset.columns]
        return None

    def outputs(self, select: Any) -> dict[str, Any] | None:
        """Output column name (lowered) to the expression that produces it."""

        from sqlglot import expressions as exp

        if id(select) in self._outputs:
            return self._outputs[id(select)]
        self._outputs[id(select)] = None  # a cycle reads as unreadable
        out: dict[str, Any] = {}
        sources = self.sources(select)
        for projection in select.expressions:
            star = projection if isinstance(projection, exp.Star) else None
            if isinstance(projection, exp.Column) and isinstance(
                projection.this, exp.Star
            ):
                star = projection
            if star is None:
                # An unnamed expression (`count(*)`) is named by the warehouse,
                # differently on each; its text keeps GROUP BY ordinals aligned.
                name = projection.alias_or_name or self.shape.text(projection)
                out[name.lower()] = projection
                continue
            qualifier = star.table.lower() if isinstance(star, exp.Column) else ""
            expanded = [
                (alias, relation)
                for alias, relation, _join in sources
                if not qualifier or alias == qualifier
            ]
            if not expanded:
                return None
            for alias, relation in expanded:
                columns = self.columns_of(relation)
                if columns is None:
                    return None
                for column in columns:
                    out.setdefault(column.lower(), exp.column(column, table=alias))
        self._outputs[id(select)] = out
        return out

    # -- column origins -----------------------------------------------------------

    def origin(self, select: Any, expression: Any, depth: int = 0) -> _Origin | None:
        """Where ``expression`` comes from, if it is a column carried unchanged."""

        from sqlglot import expressions as exp

        if depth > 32:
            return None
        node = expression.this if isinstance(expression, exp.Alias) else expression
        while isinstance(node, exp.Paren):
            node = node.this
        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            return None
        found = self.source_of(select, node)
        if found is None:
            return None
        _alias, relation, join = found
        outer = self.outer_join(select, join)
        kind, target = self.target(relation)
        if kind == "physical":
            return _Origin(target, node.name, outer)
        if kind != "scope":
            return None
        outputs = self.outputs(target)
        inner = None if outputs is None else outputs.get(node.name.lower())
        if inner is None:
            return None
        carried = self.origin(target, inner, depth + 1)
        if carried is not None and carried.outer_join is None:
            carried.outer_join = outer
        return carried

    def source_of(self, select: Any, column: Any) -> tuple[str, Any, Any] | None:
        sources = self.sources(select)
        qualifier = column.table.lower()
        if qualifier:
            return next((s for s in sources if s[0] == qualifier), None)
        if len(sources) == 1:
            return sources[0]
        having = [
            s
            for s in sources
            if column.name.lower() in [c.lower() for c in self.columns_of(s[1]) or []]
        ]
        if len(having) == 1:
            return having[0]
        if not any(self.columns_of(s[1]) is None for s in sources) and not having:
            return None
        self.reading.incomplete.append(
            f"'{column.name}' is not qualified in a select that reads several "
            "relations, so its lineage was not followed"
        )
        return None

    def outer_join(self, select: Any, join: Any) -> str | None:
        """The join that can turn this source's columns NULL, if any does."""

        joins = self.shape.joins(select)
        if join is not None and ("left" in join.side or "full" in join.side):
            return self.label(join)
        for later in joins:
            if later is join:
                continue
            if "full" in later.side or "right" in later.side:
                return self.label(later)
        return None

    # -- keys ---------------------------------------------------------------------

    def keys(self, select: Any, depth: int = 0) -> tuple[list[frozenset[str]], str]:
        """The output column sets proven unique in ``select``, and why.

        Conservative by construction: a key survives a join only when the join
        is an equality on a key proven unique on the joined side, so it can
        match at most one row; any other join, a set operation or an unnest
        withholds the key and says which construct did.
        """

        if depth > 32:
            return [], "the CTE chain is too deep to follow"
        outputs = self.outputs(select)
        if outputs is None:
            return [], "the SELECT list could not be read"

        group = select.args.get("group")
        if group is not None:
            names = []
            for expression in group.expressions:
                name = self._output_name(select, outputs, expression)
                if name is None:
                    return [], (
                        "it groups by an expression that is not an output column "
                        f"({self.shape.text(expression)})"
                    )
                names.append(name)
            return [frozenset(names)], (
                f"the model groups by {', '.join(names)}, so each combination is "
                "one row"
            )
        if select.args.get("distinct") is not None:
            return [frozenset(outputs)], "SELECT DISTINCT makes every output row unique"
        if self.shape.multiplies_rows(select):
            return [], "an UNNEST or LATERAL turns one input row into several"

        driving = self.shape.from_relation(select)
        if driving is None:
            return [], "it selects from nothing"
        alias = self.alias(driving)
        base, base_evidence = self.relation_keys(driving, depth)
        if not base:
            return [], base_evidence

        for join in self.shape.joins(select):
            if "semi" in join.side or "anti" in join.side:
                continue
            if "right" in join.side or "full" in join.side:
                return [], (
                    f"the {self.label(join)} brings in rows with no match on the "
                    "driving side, so its key is not carried"
                )
            joined_alias = self.alias(join.relation)
            joined_keys, _ = self.relation_keys(join.relation, depth)
            on_joined = self._equated(join, joined_alias)
            if not any(key <= on_joined for key in joined_keys):
                return [], (
                    f"the {self.label(join)} is not an equality on a key proven unique "
                    "on its joined side, so it can fan rows out"
                )

        carried: list[frozenset[str]] = []
        for key in base:
            names = []
            for column in sorted(key):
                name = self._projected_as(select, outputs, alias, column)
                if name is None:
                    break
                names.append(name)
            else:
                carried.append(frozenset(names))
        if not carried:
            return [], (
                f"the key of {self._display(driving)} ({base_evidence}) is not "
                "selected unchanged"
            )
        return carried, f"{base_evidence}, carried through unchanged"

    def relation_keys(
        self, relation: Any, depth: int
    ) -> tuple[list[frozenset[str]], str]:
        kind, target = self.target(relation)
        if kind == "scope":
            return self.keys(target, depth + 1)
        if kind != "physical":
            return [], "its driving relation is not one dex can follow"
        dataset = dataset_for(self.cache, target)
        if dataset is None or not dataset.columns:
            return [], f"{target} is not profiled, so no key of it is proven"
        proven = proven_keys(dataset)
        if not proven:
            return [], f"{target} has no proven unique key in the cache"
        lowered = [frozenset(c.lower() for c in key) for key in proven]
        return lowered, (f"{', '.join(proven[0])} is a proven unique key of {target}")

    def _equated(self, join: Any, joined_alias: str) -> frozenset[str]:
        """Joined-side columns the ON (or USING) requires equal to something."""

        from sqlglot import expressions as exp

        if join.using:
            return frozenset(name.lower() for name in join.using)
        if join.on is None:
            return frozenset()
        stack, conjuncts = [join.on], []
        while stack:
            node = stack.pop()
            if isinstance(node, exp.And):
                stack.extend((node.this, node.expression))
            elif isinstance(node, exp.Paren):
                stack.append(node.this)
            else:
                conjuncts.append(node)
        columns: set[str] = set()
        for node in conjuncts:
            if not isinstance(node, exp.EQ):
                continue
            for side in (node.this, node.expression):
                if isinstance(side, exp.Column) and side.table.lower() == joined_alias:
                    columns.add(side.name.lower())
        return frozenset(columns)

    def _output_name(
        self, select: Any, outputs: dict[str, Any], expression: Any
    ) -> str | None:
        from sqlglot import expressions as exp

        if isinstance(expression, exp.Literal) and expression.is_int:
            index = int(expression.this) - 1
            names = list(outputs)
            return names[index] if 0 <= index < len(names) else None
        wanted = self.shape.match_key(expression)
        for name, projection in outputs.items():
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if self.shape.match_key(inner) == wanted:
                return name
        if isinstance(expression, exp.Column) and expression.name.lower() in outputs:
            return expression.name.lower()
        return None

    def _projected_as(
        self, select: Any, outputs: dict[str, Any], alias: str, column: str
    ) -> str | None:
        from sqlglot import expressions as exp

        single = len(self.sources(select)) == 1
        for name, projection in outputs.items():
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(inner, exp.Column) or inner.name.lower() != column:
                continue
            qualifier = inner.table.lower()
            if qualifier == alias or (not qualifier and single):
                return name
            if not qualifier:
                found = self.source_of(select, inner)
                if found is not None and found[0] == alias:
                    return name
        return None

    def label(self, join: Any) -> str:
        """A join named by the relation the author wrote, not the placeholder
        the statement was parsed with."""

        on = f" on {self.shape.text(join.on)}" if join.on is not None else ""
        return f"{join.side} join to {self._display(join.relation)}{on}"

    def _display(self, relation: Any) -> str:
        kind, target = self.target(relation)
        if kind == "physical":
            return target
        return relation.alias_or_name or "a subquery"

    # -- what the SQL filters and branches on -------------------------------------

    def _collect_usage(self, select: Any) -> None:
        from sqlglot import expressions as exp

        for clause in ("where", "having", "qualify"):
            node = select.args.get(clause)
            if node is None:
                continue
            for column in node.find_all(exp.Column):
                self._use(select, column, clause)
                if column.name.lower() not in self.reading.filtered:
                    self.reading.filtered.append(column.name.lower())
            for conjunct in _conjuncts(node.this):
                self._fragment(select, conjunct, clause)
        for case in select.find_all(exp.Case):
            for branch in case.args.get("ifs") or []:
                for column in branch.this.find_all(exp.Column):
                    self._use(select, column, "case")
                if case.find_ancestor(exp.Select) is select:
                    self._fragment(select, branch.this, "case")
        for aggregate in select.find_all(exp.AggFunc):
            if aggregate.find_ancestor(exp.Select) is not select:
                continue
            self._fragment(select, aggregate, "aggregation")

    def _use(self, select: Any, column: Any, clause: str) -> None:
        if column.find_ancestor(type(select)) is not select:
            return
        origin = self.origin(select, column)
        if origin is None:
            return
        self.reading.used.setdefault((origin.relation, origin.column), set()).add(
            clause
        )

    def _fragment(self, select: Any, node: Any, clause: str) -> None:
        """Record ``node`` as one usage of each upstream column it resolves to."""

        from sqlglot import expressions as exp

        columns = list(node.find_all(exp.Column))
        origins: list[_Origin | None] = []
        for column in columns:
            in_scope = column.find_ancestor(exp.Select) is select
            star = isinstance(column.this, exp.Star)
            origin = self.origin(select, column) if in_scope and not star else None
            if origin is None and in_scope and not star:
                self.reading.untraced.add(column.name.lower())
            origins.append(origin)
        if not any(origins):
            return

        # The copy walks in the same order as the original, which is how each
        # copied column finds its origin; the model's own tree is not touched.
        rendered = node.copy()
        copies = list(rendered.find_all(exp.Column))
        for copied, origin in zip(copies, origins, strict=True):
            if isinstance(copied.this, exp.Star):
                continue
            name = origin.column if origin is not None else copied.name
            replacement = exp.column(name.lower())
            if copied is rendered:
                rendered = replacement
            else:
                copied.replace(replacement)
        expression = self.shape.text(rendered)

        literals = _compared_literals(node)
        seen: set[tuple[str, str]] = set()
        for column, origin in zip(columns, origins, strict=True):
            if origin is None:
                continue
            key = (origin.relation, origin.column.lower())
            if key in seen:
                continue
            seen.add(key)
            self.reading.usages.append(
                Usage(
                    relation=origin.relation,
                    column=origin.column,
                    clause=clause,
                    expression=expression,
                    literals=(
                        literals[1] if literals and literals[0] is column else None
                    ),
                )
            )


def _conjuncts(node: Any) -> list[Any]:
    """The top-level ``AND`` terms of a predicate, parentheses unwrapped."""

    from sqlglot import expressions as exp

    stack, out = [node], []
    while stack:
        current = stack.pop()
        if isinstance(current, exp.And):
            stack.extend((current.expression, current.this))
        elif isinstance(current, exp.Paren):
            stack.append(current.this)
        else:
            out.append(current)
    return out


def _compared_literals(node: Any) -> tuple[Any, list[str | Decimal]] | None:
    """``(column, literals)`` when ``node`` compares a bare column to literals.

    ``=``, ``<>``, ``IN`` and their negations only. A column wrapped in a
    function, a comparison against another column or a subquery, and a NULL
    test are not a value the domain could contain, so they return None.
    """

    from sqlglot import expressions as exp

    while isinstance(node, (exp.Not, exp.Paren)):
        node = node.this
    if isinstance(node, (exp.EQ, exp.NEQ)):
        sides = (node.this, node.expression)
        for column, other in (sides, sides[::-1]):
            if isinstance(column, exp.Column) and not isinstance(column.this, exp.Star):
                value = _literal(other)
                return (column, [value]) if value is not None else None
        return None
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        if node.args.get("query") is not None or not node.expressions:
            return None
        values = [_literal(item) for item in node.expressions]
        if any(value is None for value in values):
            return None
        return node.this, values  # type: ignore[return-value]
    return None


def _literal(node: Any) -> str | Decimal | None:
    from decimal import InvalidOperation

    from sqlglot import expressions as exp

    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        value = _literal(node.this)
        return -value if isinstance(value, Decimal) else None
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        return str(node.this)
    try:
        return Decimal(str(node.this))
    except InvalidOperation:
        return None


# --- cache reads --------------------------------------------------------------------


def dataset_for(cache: DexCache | None, name: str) -> Dataset | None:
    """The cache's one dataset ``name`` resolves to, or None (absent or ambiguous)."""

    if cache is None:
        return None
    matches = match_identifier(name, [d.identifier for d in cache.datasets])
    if len(matches) != 1:
        return None
    return next(d for d in cache.datasets if d.identifier == matches[0])


def proven_keys(dataset: Dataset) -> list[list[str]]:
    """Keys a profile *proved*: exact single-column uniqueness, or a composite
    an exact combination probe found unique. An approximate count is a signal,
    not a proof, and is not carried."""

    singles = [
        [column.name]
        for column in dataset.columns
        if column.is_unique
        and column.distinct_count_exact
        and column.null_fraction in (0.0, None)
    ]
    return singles + [list(key) for key in dataset.composite_keys]


def _column(dataset: Dataset | None, name: str) -> ColumnProfile | None:
    if dataset is None:
        return None
    return next((c for c in dataset.columns if c.name.lower() == name.lower()), None)


# --- assembling the draft -------------------------------------------------------------


def _drafted(
    reading: _Reading,
    cache: DexCache | None,
    files: Mapping[str, str],
    model_paths: list[str],
    *,
    domain_cap: int,
    cumulative: Mapping[str, set[str]],
    cache_note: str | None,
) -> dict[str, Any]:
    drafted: dict[str, Any] = {}
    if reading.grain:
        drafted["grain"] = {"value": reading.grain, "evidence": reading.grain_evidence}
    elif reading.grain_withheld and reading.produced is not None:
        drafted["grain"] = {"value": None, "withheld": reading.grain_withheld}

    columns: dict[str, Any] = {}
    for name, origin in reading.origins.items():
        facts = _column_facts(
            name, origin, reading, cache, files, model_paths, cumulative=cumulative
        )
        if facts:
            columns[name] = facts
    if columns:
        drafted["columns"] = columns

    domains = []
    for (relation, column), clauses in sorted(reading.used.items()):
        entry = _domain(relation, column, clauses, cache, domain_cap)
        if entry is not None:
            domains.append(entry)
    if domains:
        drafted["domains"] = domains

    inputs = []
    for relation in reading.relations:
        dataset = dataset_for(cache, relation)
        entry: dict[str, Any] = {"relation": relation}
        if dataset is not None and dataset.columns:
            entry["profiled"] = True
            entry["identifier"] = dataset.identifier
        else:
            entry["profiled"] = False
            ambiguous = (
                match_identifier(relation, [d.identifier for d in cache.datasets])
                if cache is not None
                else []
            )
            if len(ambiguous) > 1:
                entry["reason"] = (
                    f"it matches {len(ambiguous)} relations in the cache "
                    f"({', '.join(ambiguous)})"
                )
            else:
                entry["reason"] = cache_note or "the cache has not profiled it"
            entry["command"] = f"explore profile {relation}"
        inputs.append(entry)
    if inputs:
        drafted["inputs"] = inputs
    if reading.incomplete:
        drafted["incomplete"] = list(dict.fromkeys(reading.incomplete))
    return drafted


def _column_facts(
    name: str,
    origin: _Origin,
    reading: _Reading,
    cache: DexCache | None,
    files: Mapping[str, str],
    model_paths: list[str],
    *,
    cumulative: Mapping[str, set[str]],
) -> dict[str, Any]:
    dataset = dataset_for(cache, origin.relation)
    profile = _column(dataset, origin.column)
    source = f"{origin.relation}.{origin.column}"
    facts: dict[str, Any] = {"from": source}

    if reading.grain == [name]:
        facts["role"] = {
            "value": "key",
            "evidence": f"the model's drafted grain: {reading.grain_evidence}",
        }
    elif dataset is not None and cache is not None:
        reference = _verified_reference(
            cache, dataset, origin.column, files, model_paths
        )
        if reference is not None and reference[0] is not None:
            facts["role"] = {"value": "foreign_key", "evidence": reference[1]}
            facts["references"] = {"value": reference[0], "evidence": reference[1]}
        elif reference is not None:
            facts["references"] = {"value": None, "withheld": reference[1]}

    if profile is None:
        return facts

    if profile.pii is not None:
        facts["pii"] = {
            "value": {
                "category": profile.pii.category.value,
                "confidence": profile.pii.confidence,
            },
            "evidence": f"flagged on {source} by the profile",
        }

    if origin.outer_join is not None:
        facts["nullable"] = {
            "value": True,
            "evidence": (
                f"it comes from the outer side of the {origin.outer_join}; a row "
                "with no match carries NULL here"
            ),
        }
    elif profile.null_fraction == 0.0:
        facts["nullable"] = {
            "value": False,
            "evidence": (
                f"{source} held no NULL when profiled, and no outer join sits "
                "between it and this column"
            ),
        }
    elif profile.null_fraction is not None:
        facts["nullable"] = {
            "value": True,
            "evidence": f"{source} was {profile.null_fraction:.1%} NULL when profiled",
        }

    if dataset is not None and origin.column.lower() in {
        c.lower() for c in cumulative.get(dataset.identifier, set())
    }:
        facts["additivity"] = {
            "value": "semi_additive",
            "evidence": (
                f"the cumulative check found {source} never decreasing within an "
                "entity over time; summing it across rows multiplies the true value"
            ),
        }

    unit = _unit_shape(name, profile)
    if unit is not None:
        facts["unit"] = {
            "value": unit[0],
            "evidence": f"{unit[1]} in {source}; a hypothesis about the unit, not a "
            "certified one",
        }
    return facts


def _verified_reference(
    cache: DexCache,
    dataset: Dataset,
    column: str,
    files: Mapping[str, str],
    model_paths: list[str],
) -> tuple[str | None, str] | None:
    """The reference a verified join supports: ``(target, evidence)``, or
    ``(None, why)`` for a join that was measured and did not hold.

    ``verified`` means *measured*, not clean: the probe also records a join
    whose values mostly miss. A declared ``references`` renders a dbt
    ``relationships`` test, which fails on a single orphan, so only a join
    measured with none is drafted as one.
    """

    for edge in cache.relationships:
        if (
            not edge.verified
            or edge.from_dataset != dataset.identifier
            or len(edge.from_columns) != 1
            or edge.from_columns[0].lower() != column.lower()
        ):
            continue
        parent = edge.to_dataset.rsplit(".", 1)[-1]
        target = f"{parent}.{edge.to_columns[0]}"
        joined = (
            f"the join {dataset.identifier}.{column} -> {edge.to_dataset}."
            f"{edge.to_columns[0]}"
        )
        if edge.orphan_fraction is None:
            return None, (
                f"{joined} was probed with no non-NULL value to match, so it "
                "proves nothing"
            )
        if edge.orphan_fraction > 0:
            return None, (
                f"{joined} was verified with {edge.orphan_fraction:.1%} of its "
                "values unmatched; a relationships test on it would fail"
            )
        evidence = f"{joined} was verified, no orphans"
        if model_sql_path(files, model_paths, parent) is None:
            evidence += (
                f"; {parent} is not a model of this project, so declare `references` "
                "only once one stands for it"
            )
        return target, evidence
    return None


def _unit_shape(name: str, profile: ColumnProfile) -> tuple[str, str] | None:
    bounds = []
    for value in (profile.min_value, profile.max_value):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return None
        bounds.append(float(value))
    low, high = bounds
    lowered = name.lower()
    if any(hint in lowered for hint in _PERCENT_HINTS) and low >= 0 and high <= 100:
        return (
            "percent",
            "it is named like a percentage and every profiled value lies between "
            "0 and 100",
        )
    integer = any(t in profile.data_type.lower() for t in _INTEGER_TYPES)
    if not integer and low >= 0 and high <= 1:
        return "fraction", "every profiled value lies between 0 and 1"
    return None


def _domain(
    relation: str,
    column: str,
    clauses: set[str],
    cache: DexCache | None,
    cap: int,
) -> dict[str, Any] | None:
    dataset = dataset_for(cache, relation)
    profile = _column(dataset, column)
    if profile is None:
        return None
    entry: dict[str, Any] = {
        "column": f"{relation}.{column}",
        "used_in": sorted(clauses),
    }
    if profile.pii is not None:
        # The same rule `explore semantic values` applies: a flagged column's
        # values are never drafted, at any confidence.
        entry["values"] = None
        entry["withheld"] = (
            f"flagged as {profile.pii.category.value}; a PII column's domain is never "
            "drafted (clear a reviewed column with pii_overrides in .dex/config.yml)"
        )
        return entry
    domain = profile.value_domain
    if domain is None:
        entry["values"] = None
        entry["withheld"] = (
            "the profile recorded no value domain for it (too many distinct values, "
            "or a profile that kept none)"
        )
        return entry
    values = [item.value for item in domain.values]
    entry["values"] = values[:cap]
    entry["elided"] = domain.elided + max(0, len(values) - cap)
    entry["complete"] = entry["elided"] == 0
    return entry


# --- precedent and literals ---------------------------------------------------------


def _precedent(
    model: str,
    reading: _Reading,
    files: Mapping[str, str],
    model_paths: list[str],
    cache: DexCache | None,
    index: ReferenceIndex,
    dialect: str,
    vars: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """What the project's other models do with each column this one uses."""

    from .precedent import usage_of

    own: dict[tuple[str, str], list[dict[str, str]]] = {}
    for usage in reading.usages:
        entry = {"usage": usage.clause, "expression": usage.expression}
        uses = own.setdefault((usage.relation, usage.column.lower()), [])
        if entry not in uses:
            uses.append(entry)
    found = usage_of(
        [(relation, column) for relation, column in own],
        files,
        model_paths,
        cache,
        dialect=dialect,
        vars=vars,
        index=index,
        exclude=model,
    )
    out = []
    for key in sorted(own):
        answer = found[key]
        out.append({"column": answer.pop("column"), "this_model": own[key], **answer})
    return out


def _literal_warnings(
    model: str, reading: _Reading, cache: DexCache | None
) -> list[str]:
    """A compared literal the column's complete, exact domain never holds.

    Silent unless the domain can prove absence: profiled, no PII flag at any
    confidence (the rule the drafted domain already follows, and stricter than
    the firewall's threshold), and nothing elided. The profiler builds a domain
    from an exact distinct count over the whole relation, so ``elided == 0`` is
    the complete domain as of that profile. A warning, never a refusal: a filter
    on a value that does not exist yet is sometimes the intent.
    """

    out: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for usage in reading.usages:
        if not usage.literals:
            continue
        profile = _column(dataset_for(cache, usage.relation), usage.column)
        if profile is None or profile.pii is not None:
            continue
        domain = profile.value_domain
        if domain is None or domain.elided or not domain.values:
            continue
        values = [item.value for item in domain.values]
        source = f"{usage.relation}.{usage.column}"
        for literal in usage.literals:
            key = (usage.relation, usage.column.lower(), repr(literal))
            if key in seen:
                continue
            seen.add(key)
            present = _held(literal, values)
            if present is None or present:
                continue
            shown = f"'{literal}'" if isinstance(literal, str) else str(literal)
            message = (
                f"'{model}' compares {source} to {shown}, which none of its "
                f"{len(values)} profiled values is"
            )
            close = _closest(literal, values)
            if close:
                message += (
                    "; "
                    + ", ".join(f"'{value}'" for value in close)
                    + " matches it ignoring case and whitespace"
                )
            else:
                message += (
                    "; check the literal, or keep it if the value is expected to "
                    "appear later"
                )
            out.append(message)
    return out


def _held(literal: str | Decimal, values: list[Any]) -> bool | None:
    """Whether ``values`` holds ``literal``, or None when the types differ and
    the comparison would prove nothing (a string literal against a number
    column is cast by the warehouse, not by dex)."""

    if isinstance(literal, str):
        strings = [v for v in values if isinstance(v, str)]
        if len(strings) != len(values):
            return None
        return literal in strings
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return None
        numbers.append(Decimal(str(value)))
    return literal in numbers


def _closest(literal: str | Decimal, values: list[Any], limit: int = 3) -> list[str]:
    if not isinstance(literal, str):
        return []

    def fold(text: str) -> str:
        return " ".join(text.split()).casefold()

    wanted = fold(literal)
    return [v for v in values if isinstance(v, str) and fold(v) == wanted][:limit]


# --- what is missing ------------------------------------------------------------------


def _missing(declared: dict[str, Any], reading: _Reading) -> list[str]:
    missing = [name for name in INTENT_FIELDS if not declared.get(name)]
    stated = {k.lower(): v for k, v in (declared.get("columns") or {}).items()}
    for column in reading.produced or []:
        entry = stated.get(column, {})
        if not entry.get("role"):
            missing.append(f"columns.{column}.role")
        elif entry["role"] == "measure":
            missing.extend(
                f"columns.{column}.{key}"
                for key in ("aggregation", "additivity", "unit")
                if not entry.get(key)
            )
    return missing


def _warnings(
    model: str,
    declared: dict[str, Any],
    drafted: dict[str, Any],
    reading: _Reading,
    missing: list[str],
    *,
    grain_judged: bool = False,
) -> list[str]:
    out = []
    for name in INTENT_FIELDS:
        if name not in missing:
            continue
        if name == "description":
            out.append(f"'{model}' declares no description of what one row is for")
        elif name == "grain":
            grain = drafted.get("grain") or {}
            if grain.get("value"):
                out.append(
                    f"'{model}' declares no grain; dex drafted "
                    f"[{', '.join(grain['value'])}] ({grain['evidence']}), confirm it "
                    "or declare another"
                )
            else:
                out.append(f"'{model}' declares no grain")
        elif name == "population":
            if reading.filtered:
                hint = ""
                filtering = {"where", "having", "qualify"}
                counted = [
                    f"{p['column']} ({p['readers']})"
                    for p in drafted.get("precedent", [])
                    if p["readers"]
                    and any(use["usage"] in filtering for use in p["this_model"])
                ]
                if counted:
                    hint = (
                        "; drafted.precedent shows how the project's other models "
                        f"treat {', '.join(counted)}"
                    )
                out.append(
                    f"'{model}' declares no population rule; its SQL filters on "
                    f"{', '.join(reading.filtered)}{hint}"
                )
            else:
                out.append(f"'{model}' declares no population rule (which rows are in)")
        elif name == "assumptions":
            out.append(
                f"'{model}' declares no assumptions, so no decision it rests on is "
                "recorded"
            )
    measure_fields: dict[str, set[str]] = {}
    for entry in missing:
        if entry.startswith("columns.") and not entry.endswith(".role"):
            _, column, field = entry.split(".", 2)
            measure_fields.setdefault(column, set()).add(field)
    groups: dict[tuple[str, ...], list[str]] = {}
    for column, fields in sorted(measure_fields.items()):
        absent = tuple(
            field for field in ("aggregation", "additivity", "unit") if field in fields
        )
        groups.setdefault(absent, []).append(column)
    for fields, columns in groups.items():
        plural = len(columns) > 1
        out.append(
            f"'{model}' declares {'measures' if plural else 'measure'} "
            f"{', '.join(columns)} without {'their' if plural else 'its'} "
            f"{', '.join(fields)}"
        )
    unprofiled = [i["relation"] for i in drafted.get("inputs", []) if not i["profiled"]]
    if unprofiled:
        out.append(
            f"'{model}' reads {', '.join(unprofiled)}, which the cache has not "
            "profiled, so nothing was drafted from it; run "
            + ", ".join(f"`explore profile {r}`" for r in unprofiled)
        )
    # A contradiction already says the same thing about this edit, with the side
    # that looks stale, so the generic disagreement would only repeat it.
    if (grain_declared := declared.get("grain")) and not grain_judged:
        grain = (drafted.get("grain") or {}).get("value")
        if grain and sorted(c.lower() for c in grain_declared) != sorted(grain):
            out.append(
                f"'{model}' declares grain [{', '.join(grain_declared)}] and dex "
                f"drafted [{', '.join(grain)}] from the SQL and the cache; one of "
                "them is wrong"
            )
    return out
