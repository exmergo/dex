"""Contradictions: an edit that disagrees with what the model declares (#495).

The costliest edits to an existing model are the ones that still compile: a
filter removed, a grain widened, a running total summed. Row attribution already
says *that* an edit moved rows; once a model carries a declaration (#491) there
is also a statement of what it was supposed to do, and this module compares the
two.

Three states are read, never two: the prior SQL with the prior declaration, the
authored SQL with the post-change declaration, and the difference between them.
That is what tells the sides apart:

- the SQL changed, the declaration did not, and they now disagree: the SQL looks
  stale;
- the declaration changed and the SQL did not: the declaration looks stale, or
  the plan is half done (a declaration-only change is never read as an
  unacknowledged SQL edit);
- both changed in this plan and agree: silent, because the change is the job;
- both already disagreed and this plan touches neither: silent, so an unrelated
  edit is not buried under a contradiction it did not cause.

Five rules are checked: a declared population filter, a declared population
parent, the declared grain, a declared column, and the additivity an upstream
model declares for a column this one aggregates. The parent rule is the one that
also runs on a model the plan creates (#1029), since it needs no prior. Everything
is compared on sqlglot's reading of the SQL rather than on its text, so a reformat
or an alias rename is silent, and a construct dex cannot read (a macro, a function
around the column, a subquery) is reported as *could not check*, never as a rule
removed.

Read-only and connectionless: it reads the repository and the stored cache and
nothing else. It never refuses and never blocks apply. A warning here is a
question for the author, not enforcement.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..dbt_project import jinja_regions
from .declarations import model_sql_path
from .drafting import Usage, _Reading, _untoken, dataset_for, read_declared

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..references import ReferenceIndex

_FILTER_CLAUSES = ("where", "having", "qualify")
_RATIO_UNITS = ("fraction", "percent")
#: The declaration fields that make a column *declared* for the columns check: a
#: description alone is documentation, not a statement of what the column is.
_COLUMN_FIELDS = (
    "role",
    "null_rule",
    "aggregation",
    "additivity",
    "unit",
    "references",
)


@dataclass
class Contradiction:
    """One declared rule this plan's SQL and declaration disagree on.

    ``status`` is ``contradicted``, or ``unchecked`` where the SQL could not be
    read for this rule and the plan touched it. ``stale`` names the side that
    looks out of date: ``sql``, ``declaration``, or ``either`` when the plan
    changed both and they still disagree. ``prior`` and ``authored`` are SQL
    fragments or column lists out of the repository, never a data value.
    """

    model: str
    check: str
    status: str
    rule: str
    stale: str | None = None
    prior: str | None = None
    authored: str | None = None
    preexisting: bool = False
    reason: str | None = None
    column: str | None = None
    #: Where the declared rule lives, when that is not the model itself.
    declared_on: str | None = None
    row_delta: int | None = None
    row_reason: str | None = None
    #: The declared parent, for that rule.
    parent: str | None = None
    #: The plan creates this model, so "the plan changed both" means it wrote both.
    created: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "check": self.check,
            "status": self.status,
            "rule": self.rule,
        }
        for key in (
            "column",
            "parent",
            "declared_on",
            "stale",
            "prior",
            "authored",
            "reason",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.status == "contradicted":
            out["preexisting"] = self.preexisting
        if self.row_delta is not None:
            out["row_delta"] = self.row_delta
        if self.created:
            out["created"] = True
        return out


@dataclass
class _Verdict:
    """Whether one reading implements one rule, and what it does instead."""

    state: str  # holds | contradicted | unknown
    said: str | None = None
    reason: str | None = None
    signature: Any = None


@dataclass
class _Side:
    """One version of the model: its reading, its declaration, its project."""

    reading: _Reading
    declared: dict[str, Any]
    files: Mapping[str, str]


def check(
    model: str,
    *,
    prior_reading: _Reading,
    reading: _Reading,
    prior_declared: dict[str, Any],
    declared: dict[str, Any],
    prior_files: Mapping[str, str],
    files: Mapping[str, str],
    model_paths: list[str],
    cache: DexCache | None = None,
    cumulative: Mapping[str, set[str]] | None = None,
    index: Callable[[], ReferenceIndex | None] | None = None,
) -> list[Contradiction]:
    """Every declared rule ``model``'s prior and authored versions disagree on.

    ``prior_*`` is the model as the project holds it now and the rest is the
    model once the plan lands. ``cumulative`` maps a cache identifier to the
    columns the cumulative detector flagged there, for a running total no
    declaration names yet. ``index`` builds the post-change reference index, called
    only when a parent is declared.
    """

    before = _Side(prior_reading, prior_declared, prior_files)
    after = _Side(reading, declared, files)
    found: list[Contradiction] = []
    found.extend(_population(model, before, after))
    found.extend(_parent(model, before, after, model_paths, index))
    grain = _grain(model, before, after)
    found.extend(grain)
    dropped = {c.column for c in grain if c.column}
    found.extend(_columns(model, before, after, skip=dropped))
    found.extend(
        _additivity(model, before, after, model_paths, cache, cumulative or {})
    )
    for item in found:
        item.prior = _untoken(item.prior, prior_reading.tokens)
        item.authored = _untoken(item.authored, reading.tokens)
        item.reason = _untoken(item.reason, reading.tokens)
    return found


def check_new(
    model: str,
    *,
    reading: _Reading,
    declared: dict[str, Any],
    files: Mapping[str, str],
    model_paths: list[str],
    index: Callable[[], ReferenceIndex | None] | None = None,
) -> list[Contradiction]:
    """What a model the plan creates contradicts: only its declared parent, the
    one rule that means something with no prior version to compare."""

    after = _Side(reading, declared, files)
    before = _Side(_Reading(), {}, {})
    found = _parent(model, before, after, model_paths, index, created=True)
    for item in found:
        item.authored = _untoken(item.authored, reading.tokens)
        item.reason = _untoken(item.reason, reading.tokens)
    return found


def _judge(
    model: str,
    check: str,
    rule: str,
    before: _Verdict,
    after: _Verdict,
    *,
    rule_changed: bool,
    **extra: Any,
) -> Contradiction | None:
    """The state table in the module docstring, for one rule."""

    if after.state == "holds":
        return None
    sql_changed = before.signature != after.signature
    if not (rule_changed or sql_changed):
        return None
    if after.state == "unknown":
        return Contradiction(
            model,
            check,
            "unchecked",
            rule,
            prior=before.said,
            authored=after.said,
            reason=after.reason,
            **extra,
        )
    if not rule_changed:
        stale, preexisting = "sql", before.state == "contradicted"
    elif not sql_changed:
        stale, preexisting = "declaration", False
    else:
        stale, preexisting = "either", False
    return Contradiction(
        model,
        check,
        "contradicted",
        rule,
        stale=stale,
        prior=before.said,
        authored=after.said,
        preexisting=preexisting,
        **extra,
    )


# --- population -------------------------------------------------------------------


def _filters(declared: dict[str, Any]) -> dict[str, dict[str, Any]]:
    population = declared.get("population")
    if not isinstance(population, dict):
        return {}
    out = {}
    for entry in population.get("filters") or []:
        if isinstance(entry, dict) and isinstance(entry.get("column"), str):
            out[entry["column"].lower()] = entry
    return out


def _population(model: str, before: _Side, after: _Side) -> list[Contradiction]:
    prior_filters = _filters(before.declared)
    out = []
    for column, entry in _filters(after.declared).items():
        prior_entry = prior_filters.get(column)
        found = _judge(
            model,
            "population",
            f"population.filters {_describe_filter(entry)}",
            _filter_verdict(before.reading, prior_entry or entry),
            _filter_verdict(after.reading, entry),
            rule_changed=_filter_key(prior_entry) != _filter_key(entry),
            column=entry["column"],
        )
        if found is not None:
            out.append(found)
    return out


# --- population parent ------------------------------------------------------------


def _declared_parent(declared: dict[str, Any]) -> str | None:
    population = declared.get("population")
    if not isinstance(population, dict):
        return None
    parent = population.get("parent")
    if isinstance(parent, dict) and isinstance(parent.get("model"), str):
        return parent["model"]
    return None


def _parent(
    model: str,
    before: _Side,
    after: _Side,
    model_paths: list[str],
    index: Callable[[], ReferenceIndex | None] | None,
    *,
    created: bool = False,
) -> list[Contradiction]:
    """The relation driving the model against the parent it declares.

    Any lineage link in either direction satisfies the rule: a relation derived
    from the parent, however it joins, and a relation the parent itself is built
    from, such as its source. That misses a model driven from a child table that
    merely joins the parent upstream, which verification can still catch; a false
    alarm on a legitimate model is the error avoided. Nothing is inferred from a
    name: no declared parent, no check.
    """

    parent = _declared_parent(after.declared)
    if parent is None:
        return []
    prior_parent = _declared_parent(before.declared)
    graph = index() if index is not None else None
    found = _judge(
        model,
        "population_parent",
        f"population.parent {parent}",
        _parent_verdict(before, prior_parent or parent, graph, model_paths, model),
        _parent_verdict(after, parent, graph, model_paths, model),
        rule_changed=prior_parent != parent,
        parent=parent,
        created=created,
    )
    return [found] if found is not None else []


def _parent_verdict(
    side: _Side,
    parent: str,
    graph: ReferenceIndex | None,
    model_paths: list[str],
    model: str,
) -> _Verdict:
    reading = side.reading
    drivers = reading.driving
    signature = (
        frozenset(d.lower() for d in drivers),
        reading.driving_unread is not None,
    )
    said = ", ".join(drivers) or None
    if parent.lower() in {d.lower() for d in drivers}:
        return _Verdict("holds", said=said, signature=signature)
    if graph is not None and _in_lineage(graph, parent, drivers, model):
        return _Verdict("holds", said=said, signature=signature)

    def unknown(reason: str) -> _Verdict:
        return _Verdict("unknown", said=said, reason=reason, signature=signature)

    if reading.driving_unread:
        return unknown(reading.driving_unread)
    if not drivers:
        return unknown(
            reading.incomplete[0]
            if reading.incomplete
            else "the driving relation could not be read"
        )
    if reading.driving_literal:
        return unknown(
            f"`{reading.driving_literal[0]}` is written as a literal name, so dex "
            "cannot place it in the ref() graph"
        )
    if graph is None:
        return unknown("the project's ref() graph was not available")
    opened = _unsettled(side, graph, parent, drivers, model_paths, model)
    if opened:
        return unknown(opened)
    return _Verdict("contradicted", said=said, signature=signature)


def _lowered(names: set[str]) -> set[str]:
    return {name.lower() for name in names}


def _in_lineage(
    graph: ReferenceIndex, parent: str, drivers: list[str], model: str
) -> bool:
    """Whether any driver derives from the parent or feeds it."""

    upstream = _lowered(graph.ancestors_of(parent))
    for driver in drivers:
        if parent.lower() in _lowered(graph.ancestors_of(driver)):
            return True
        if driver.lower() in upstream:
            return True
        # A source is no node of the ref() graph, so what links it to the parent
        # is a model upstream of the parent reading it.
        readers = {
            PurePosixPath(path).stem.lower()
            for path in graph.readers_of(driver, packages=True)
        }
        if (readers - {model.lower()}) & upstream:
            return True
    return False


#: Jinja calls that cannot hide a relation a model reads.
_TRANSPARENT_CALLS = frozenset(
    {"ref", "source", "config", "var", "is_incremental", "this", "env_var"}
)

#: Macros that shape columns or values and read no relation of their own: a
#: relation they are handed arrives through ``ref()``, ``source()`` or a column
#: listing, which names nothing new. Package staging models (Fivetran's, the
#: dbt-labs packages') are written with these, so without them every package
#: parent's lineage would read as hidden (#1155). A region whose outermost call
#: is one of these is read as a whole, since its arguments (a package's own
#: ``get_<table>_columns()`` list) only describe columns.
_CROSS_DATABASE = (
    "type_string",
    "type_timestamp",
    "type_int",
    "type_bigint",
    "type_numeric",
    "type_float",
    "type_boolean",
    "current_timestamp",
    "current_timestamp_backcompat",
    "dateadd",
    "datediff",
    "date_trunc",
    "last_day",
    "safe_cast",
    "cast_bool_to_text",
    "concat",
    "hash",
    "length",
    "replace",
    "right",
    "position",
    "split_part",
    "string_literal",
    "escape_single_quotes",
    "any_value",
    "bool_or",
    "listagg",
)
_COLUMN_ONLY_CALLS = frozenset(
    {
        "adapter.get_columns_in_relation",
        "adapter.quote",
        "dbt_utils.star",
        "dbt_utils.generate_surrogate_key",
        "dbt_utils.surrogate_key",
        "dbt_utils.safe_add",
        "fivetran_utils.fill_staging_columns",
        "fivetran_utils.source_relation",
        "fivetran_utils.timestamp_add",
        "fivetran_utils.timestamp_diff",
        "fivetran_utils.json_parse",
        "fivetran_utils.string_agg",
        *_CROSS_DATABASE,
        *(f"dbt.{name}" for name in _CROSS_DATABASE),
        *(f"dbt_utils.{name}" for name in _CROSS_DATABASE),
    }
)


def _hidden_call(regions: list[Any]) -> str | None:
    """The first call in ``regions`` that may read a relation dex cannot see."""

    for region in regions:
        if region.calls and region.calls[0].callee in _COLUMN_ONLY_CALLS:
            continue
        for call in region.calls:
            if call.callee not in _TRANSPARENT_CALLS | _COLUMN_ONLY_CALLS:
                return call.callee
    return None


def _unsettled(
    side: _Side,
    graph: ReferenceIndex,
    parent: str,
    drivers: list[str],
    model_paths: list[str],
    model: str,
) -> str | None:
    """Why the lineage cannot rule the parent out, when something could hide it:
    a relation the project does not define, a ref dex could not resolve, or a
    macro call that may read a relation of its own."""

    own_sql = side.files.get(model_sql_path(side.files, model_paths, model) or "", "")
    regions, _ = jinja_regions(own_sql)
    sources = {
        str(call.args[-1]).lower()
        for region in regions
        for call in region.calls
        if call.callee == "source" and call.args
    }
    if graph.node_kind(parent) is None and graph.package_node_kind(parent) is None:
        return (
            f"the declared parent '{parent}' is not defined in this project or an "
            "installed package, so its lineage cannot be read"
        )
    lineage = set(graph.ancestors_of(parent))
    for driver in drivers:
        if driver.lower() in sources:
            continue
        if graph.node_kind(driver) is None and graph.package_node_kind(driver) is None:
            return (
                f"'{driver}' is not defined in this project or an installed package "
                "(packages not installed yet, or a name nothing defines), so its "
                "lineage cannot be read"
            )
        lineage |= graph.ancestors_of(driver)
    stems = _lowered(lineage)
    for reference in graph.indeterminate_for("model"):
        if PurePosixPath(reference.path).stem.lower() in stems:
            return (
                f"{reference.path} carries a ref() dex could not resolve, so the "
                "lineage may reach the parent"
            )
    for name in sorted(lineage):
        path = model_sql_path(side.files, model_paths, name)
        text = side.files[path] if path is not None else _package_sql(graph, name)
        if text is None:
            continue
        regions, _ = jinja_regions(text)
        hidden = _hidden_call(regions)
        if hidden is not None:
            return (
                f"'{name}' calls {hidden}(), which may read a relation dex cannot see"
            )
    return None


def _package_sql(graph: ReferenceIndex, name: str) -> str | None:
    """The SQL of a model only an installed package defines, read from disk."""

    path = graph.package_file_of(name)
    if path is None or not path.endswith(".sql"):
        return None
    from .build import packages_dir

    root = Path(graph.view.root)
    # The index spells a package file under its install directory's path
    # relative to the project, or under the directory's bare name when the
    # configured install path lies outside the project.
    for candidate in (root / path, packages_dir(root).parent / path):
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
    return None


def _describe_filter(entry: dict[str, Any]) -> str:
    parts = [str(entry["column"])]
    for key in ("includes", "excludes"):
        values = entry.get(key)
        if values:
            parts.append(f"{key} {_values(values)}")
    return " ".join(parts)


def _values(values: list[Any]) -> str:
    return (
        "["
        + ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in values)
        + "]"
    )


def _filter_key(entry: dict[str, Any] | None) -> Any:
    if entry is None:
        return None
    return tuple(
        frozenset(_value_key(v) for v in entry.get(key) or [])
        for key in ("includes", "excludes")
    )


def _value_key(value: Any) -> tuple[str, Any]:
    """A declared or compared literal in one comparable form: YAML hands back
    ``1`` and ``1.0`` and the SQL parse hands back ``Decimal('1')``."""

    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float, Decimal)):
        return ("n", Decimal(str(value)).normalize())
    return ("s", str(value))


def _filter_verdict(reading: _Reading, entry: dict[str, Any]) -> _Verdict:
    column = entry["column"].lower()
    names = {column}
    origin = reading.origins.get(column)
    if origin is not None:
        names.add(origin.column.lower())
    uses = [
        u
        for u in reading.usages
        if u.clause in _FILTER_CLAUSES and u.column.lower() in names
    ]
    expressions = list(dict.fromkeys(u.expression for u in uses))
    said = " AND ".join(expressions) or None
    # Matched on sqlglot's rendering, lowered, so `o.status <> 'x'` and
    # `status != 'x'` are one predicate and a reformat moves nothing.
    signature = frozenset(e.lower() for e in expressions)

    if not uses:
        if names & reading.untraced:
            return _Verdict(
                "unknown",
                reason=(
                    f"the SQL filters on {column} somewhere dex could not trace to "
                    "an upstream column"
                ),
                signature=signature,
            )
        if not reading.usages and reading.incomplete:
            return _Verdict(
                "unknown", reason=reading.incomplete[0], signature=("unread",)
            )
        return _Verdict("contradicted", signature=signature)

    excluded: set[Any] = set()
    included: set[Any] | None = None
    unreadable = []
    for use in uses:
        polarity = _polarity(use.expression)
        if use.literals is None or polarity is None:
            unreadable.append(use.expression)
            continue
        values = {_value_key(v) for v in use.literals}
        if polarity:
            included = values if included is None else included & values
        else:
            excluded |= values

    includes, excludes = _filter_key(entry)
    holds = True
    if includes:
        holds = included is not None and included == includes
    if excludes:
        holds = holds and (
            excluded == excludes
            or (not excluded and included is not None and not included & excludes)
        )
    if holds:
        return _Verdict("holds", said=said, signature=signature)
    if unreadable:
        return _Verdict(
            "unknown",
            said=said,
            reason=(
                f"`{unreadable[0]}` does not compare the bare column to literals, so "
                "dex cannot tell which values it keeps"
            ),
            signature=signature,
        )
    return _Verdict("contradicted", said=said, signature=signature)


def _polarity(expression: str) -> bool | None:
    """True for a comparison that keeps the listed values (``=``, ``IN``), False
    for one that drops them (``<>``, ``NOT IN``), None for anything else."""

    import sqlglot
    from sqlglot import expressions as exp

    try:
        node = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError:
        return None
    keeps = True
    while isinstance(node, (exp.Not, exp.Paren)):
        if isinstance(node, exp.Not):
            keeps = not keeps
        node = node.this
    if isinstance(node, (exp.EQ, exp.In)):
        return keeps
    if isinstance(node, exp.NEQ):
        return not keeps
    return None


# --- grain ------------------------------------------------------------------------


def _grain(model: str, before: _Side, after: _Side) -> list[Contradiction]:
    declared = after.declared.get("grain")
    if not isinstance(declared, list) or not declared:
        return []
    grain = [str(g).lower() for g in declared]
    prior = before.declared.get("grain")
    prior_grain = [str(g).lower() for g in prior] if isinstance(prior, list) else None
    after_verdict = _grain_verdict(after.reading, grain)
    found = _judge(
        model,
        "grain",
        f"grain [{', '.join(declared)}]",
        _grain_verdict(before.reading, prior_grain or grain),
        after_verdict,
        rule_changed=prior_grain is None or set(prior_grain) != set(grain),
    )
    if found is None:
        return []
    dropped = [
        g for g in grain if after.reading.produced and g not in after.reading.produced
    ]
    if len(dropped) == 1:
        found.column = dropped[0]
    return [found]


def _grain_verdict(reading: _Reading, grain: list[str]) -> _Verdict:
    produced = reading.produced
    dropped = [g for g in grain if produced is not None and g not in produced]
    # The declared grain is matched against every proven key, not only the one
    # the draft picked: the pick may prefer a column the declaration does not
    # name (a PII-flagged first key, #1079), and an edit that strips the
    # declared key of its proof must still contradict while another key remains.
    proven = reading.proven_grains
    if proven is None:
        proven = [reading.grain] if reading.grain else []
    signature = (
        tuple(sorted(reading.grain or ())),
        tuple(sorted(tuple(sorted(key)) for key in proven)),
        tuple(dropped),
        reading.grain_withheld,
    )
    if dropped:
        return _Verdict(
            "contradicted",
            said=f"does not select {', '.join(dropped)}",
            signature=signature,
        )
    if reading.grain:
        said = f"[{', '.join(reading.grain)}]"
        if any(set(key) == set(grain) for key in proven):
            return _Verdict("holds", said=said, signature=signature)
        return _Verdict(
            "contradicted",
            said=f"{said} ({reading.grain_evidence})",
            signature=signature,
        )
    reason = reading.grain_withheld or (
        reading.incomplete[0] if reading.incomplete else "the grain could not be read"
    )
    return _Verdict("unknown", reason=reason, signature=signature)


# --- columns ----------------------------------------------------------------------


def _declared_columns(declared: dict[str, Any]) -> dict[str, dict[str, Any]]:
    columns = declared.get("columns")
    if not isinstance(columns, dict):
        return {}
    return {
        name.lower(): entry
        for name, entry in columns.items()
        if isinstance(entry, dict) and any(entry.get(k) for k in _COLUMN_FIELDS)
    }


def _columns(
    model: str, before: _Side, after: _Side, *, skip: set[str | None]
) -> list[Contradiction]:
    prior_columns = _declared_columns(before.declared)
    out = []
    for name, entry in _declared_columns(after.declared).items():
        if name in skip:
            continue
        prior_entry = prior_columns.get(name)
        found = _judge(
            model,
            "columns",
            f"{name} {_describe_column(entry)}",
            _column_verdict(before.reading, name),
            _column_verdict(after.reading, name),
            rule_changed=prior_entry is None
            or {k: prior_entry.get(k) for k in _COLUMN_FIELDS}
            != {k: entry.get(k) for k in _COLUMN_FIELDS},
            column=name,
        )
        if found is not None:
            out.append(found)
    return out


def _describe_column(entry: dict[str, Any]) -> str:
    """``as the measure (aggregation sum, unit currency:EUR)``: why it mattered,
    which is what the schema.yml divergence warning cannot say."""

    role = entry.get("role")
    head = f"as a {role}" if role else "with a declared meaning"
    if role == "foreign_key" and entry.get("references"):
        head += f" to {entry['references']}"
    facts = [
        f"{key} {entry[key]}"
        for key in ("aggregation", "additivity", "unit")
        if entry.get(key)
    ]
    return head + (f" ({', '.join(facts)})" if facts else "")


def _column_verdict(reading: _Reading, name: str) -> _Verdict:
    if reading.produced is None:
        reason = (
            reading.incomplete[0]
            if reading.incomplete
            else ("the SELECT list could not be read")
        )
        return _Verdict("unknown", reason=reason, signature=None)
    if name in reading.produced:
        return _Verdict("holds", said="selected", signature=True)
    return _Verdict("contradicted", signature=False)


# --- additivity -------------------------------------------------------------------


def _aggregate(expression: str) -> str | None:
    """``SUM``/``AVG`` when ``expression`` is one, else None. A ``MAX`` or a
    ``COUNT`` over a running total is not adding it up."""

    import sqlglot
    from sqlglot import expressions as exp

    try:
        node = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError:
        return None
    if isinstance(node, exp.Sum):
        return "sum"
    if isinstance(node, exp.Avg):
        return "avg"
    return None


def _additivity(
    model: str,
    before: _Side,
    after: _Side,
    model_paths: list[str],
    cache: DexCache | None,
    cumulative: Mapping[str, set[str]],
) -> list[Contradiction]:
    targets: dict[tuple[str, str], tuple[str, str]] = {}
    for side in (before, after):
        for use in side.reading.usages:
            if use.clause == "aggregation" and _aggregate(use.expression):
                targets.setdefault(
                    (use.relation.lower(), use.column.lower()),
                    (use.relation, use.column),
                )

    out = []
    for relation, column in targets.values():
        rule = _additivity_rule(relation, column, after.files, model_paths)
        evidence = None
        if rule is None:
            dataset = dataset_for(cache, relation)
            flagged = cumulative.get(dataset.identifier, set()) if dataset else set()
            if _declared_additive(relation, column, after.files, model_paths):
                continue
            if column.lower() not in {c.lower() for c in flagged}:
                continue
            rule = {"additivity": "semi_additive"}
            evidence = "data"
        prior_rule = (
            rule
            if evidence
            else _additivity_rule(relation, column, before.files, model_paths)
        )
        if evidence:
            described = (
                f"{relation}.{column} is a running total: the cumulative check found "
                "it never decreasing within an entity, and no declaration says so yet"
            )
        else:
            described = f"{column} {_describe_additivity(rule)}"
        found = _judge(
            model,
            "additivity",
            described,
            _additivity_verdict(before.reading, relation, column, prior_rule or rule),
            _additivity_verdict(after.reading, relation, column, rule),
            rule_changed=prior_rule != rule,
            column=column,
            declared_on=None if evidence else relation,
        )
        if found is not None:
            out.append(found)
    return out


def _additivity_rule(
    relation: str,
    column: str,
    files: Mapping[str, str],
    model_paths: list[str],
) -> dict[str, Any] | None:
    """The upstream model's declared additivity for ``column``, when it is one
    that does not add: the parent defines the column, so the parent says."""

    if model_sql_path(files, model_paths, relation) is None:
        return None
    entry = _declared_columns(read_declared(files, relation)).get(column.lower())
    if entry is None or entry.get("additivity") not in (
        "semi_additive",
        "non_additive",
    ):
        return None
    return {"additivity": entry["additivity"], "unit": entry.get("unit")}


def _declared_additive(
    relation: str,
    column: str,
    files: Mapping[str, str],
    model_paths: list[str],
) -> bool:
    """Whether the parent declares the column additive, which a human decided and
    which outranks what the cumulative check measured."""

    if model_sql_path(files, model_paths, relation) is None:
        return False
    entry = _declared_columns(read_declared(files, relation)).get(column.lower())
    return entry is not None and entry.get("additivity") == "additive"


def _describe_additivity(rule: dict[str, Any]) -> str:
    if rule["additivity"] == "semi_additive":
        return "semi_additive (it does not add across time)"
    unit = rule.get("unit")
    if unit in _RATIO_UNITS:
        return f"non_additive with unit {unit} (a ratio: it does not add or average)"
    return "non_additive (it does not add)"


def _additivity_verdict(
    reading: _Reading, relation: str, column: str, rule: dict[str, Any]
) -> _Verdict:
    ratio = rule["additivity"] == "non_additive" and rule.get("unit") in _RATIO_UNITS
    offending = list(
        dict.fromkeys(
            use.expression
            for use in _uses_of(reading, relation, column)
            if use.clause == "aggregation"
            and (
                (kind := _aggregate(use.expression)) == "sum"
                or (kind == "avg" and ratio)
            )
        )
    )
    signature = frozenset(e.lower() for e in offending)
    if offending:
        return _Verdict("contradicted", said=", ".join(offending), signature=signature)
    if not reading.usages and reading.incomplete:
        return _Verdict("unknown", reason=reading.incomplete[0], signature=("unread",))
    return _Verdict("holds", signature=signature)


def _uses_of(reading: _Reading, relation: str, column: str) -> list[Usage]:
    return [
        u
        for u in reading.usages
        if u.relation.lower() == relation.lower() and u.column.lower() == column.lower()
    ]


# --- the envelope -----------------------------------------------------------------


def pair_rows(
    found: list[dict[str, Any]], attribution: list[dict[str, Any]] | None
) -> None:
    """Cite row attribution's own measurement on each population contradiction.

    Attribution is the one measuring path; this only reads what it reported. A
    removed or replaced predicate on the filtered column is the change that
    stands for the rule. Where attribution named it without measuring (a billed
    connector without ``--attribute-rows``), its reason is carried instead.
    """

    for item in found:
        if item["check"] != "population" or item.get("stale") not in ("sql", "either"):
            continue
        column = str(item.get("column", "")).lower()
        for change in _filter_changes(attribution or []):
            if not _names_column(change.get("prior") or "", column):
                continue
            if change.get("delta") is not None:
                item["row_delta"] = change["delta"]
            elif change.get("reason"):
                item["row_reason"] = change["reason"]
            break


def _filter_changes(attribution: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        change
        for record in attribution
        for change in record.get("changes") or []
        if change.get("kind") in ("predicate_removed", "predicate_replaced")
    ]


def _names_column(fragment: str, column: str) -> bool:
    import sqlglot
    from sqlglot import expressions as exp

    try:
        node = sqlglot.parse_one(fragment)
    except sqlglot.errors.ParseError:
        return re.search(rf"\b{re.escape(column)}\b", fragment.lower()) is not None
    return any(c.name.lower() == column for c in node.find_all(exp.Column))


def warnings(model: str, found: list[dict[str, Any]]) -> list[str]:
    """One sentence per contradiction: the model, the declared rule, the changed
    SQL, the side that looks stale, and both ways out.

    Rules left unchecked for one shared reason (a macro dex cannot render leaves
    every rule of the model unread) are said once, naming each rule, so one
    unreadable predicate is not six warnings.
    """

    out: list[str] = []
    unchecked: dict[str, list[str]] = {}
    for item in found:
        if item["status"] == "unchecked":
            unchecked.setdefault(str(item.get("reason")), []).append(_subject(item))
        else:
            out.append(_sentence(model, item))
    for reason, subjects in unchecked.items():
        out.append(
            f"'{model}': could not check this edit against {_listed(subjects)}: "
            f"{reason}. Nothing was assumed removed; read the SQL against the "
            "declaration before applying"
        )
    return out


def _subject(item: dict[str, Any]) -> str:
    owner = item.get("declared_on")
    if owner:
        return f"what '{owner}' declares, {item['rule']}"
    if item["check"] == "additivity":
        return item["rule"]
    return f"its declared {item['rule']}"


def _listed(subjects: list[str]) -> str:
    if len(subjects) <= 2:
        return " and ".join(subjects)
    return "; ".join(subjects[:-1]) + "; and " + subjects[-1]


def _sentence(model: str, item: dict[str, Any]) -> str:
    check, rule = item["check"], item["rule"]
    prior, authored = item.get("prior"), item.get("authored")
    stale = item.get("stale")
    if check == "population_parent":
        stated = f"'{model}' declares one row per entity of '{item.get('parent')}'"
        change = (
            f"its SQL is driven from '{authored}', which is neither that parent nor "
            "derived from it"
        )
    elif check == "population":
        stated = f"'{model}' declares {rule}"
        if authored and (stale == "declaration" or authored == prior):
            change = f"its SQL filters `{authored}`, which does not implement it"
        elif prior and authored:
            change = f"the filter behind it changed from `{prior}` to `{authored}`"
        elif prior:
            change = f"the filter behind it (`{prior}`) is gone"
        elif authored:
            change = f"its SQL filters `{authored}`, which does not implement it"
        else:
            change = "no filter in its SQL implements it"
    elif check == "grain":
        stated = f"'{model}' declares {rule}"
        if authored and authored.startswith("does not select"):
            change = f"its SQL {authored}"
        elif stale == "declaration" or authored == prior:
            change = f"its SQL reads at grain {authored}"
        else:
            change = f"its SQL now reads at grain {authored}"
            if prior:
                change += f", where it read at {prior}"
    elif check == "columns":
        stated = f"'{model}' declares {rule}"
        change = f"its SELECT list does not produce {item.get('column')}"
    else:
        owner = item.get("declared_on")
        stated = f"'{owner}' declares {rule}" if owner else rule
        change = f"'{model}' aggregates it with {authored}"

    if stale == "sql":
        side = "the SQL looks stale"
        fix = _sql_fix(check, item)
    elif stale == "declaration":
        side = (
            "the declaration looks stale, or the plan is half done: this plan "
            "changes the declaration and not the SQL"
        )
        fix = (
            "edit the SQL in this same plan to implement the declaration, or "
            "correct the declaration if it is the side that is wrong"
        )
    elif item.get("created"):
        side = "this plan writes both the SQL and the declaration and they disagree"
        fix = "make one match the other before applying"
    else:
        side = (
            "this plan changes both the SQL and the declaration and they still disagree"
        )
        fix = "make one match the other before applying"
    if check == "population_parent":
        fix = (
            f"drive the model from '{item.get('parent')}' and left-join the rest, or "
            "correct the declared parent if it is the side that is wrong"
        )
    text = f"{stated}, and {change}; {side}. {fix[0].upper()}{fix[1:]}"
    if item.get("preexisting"):
        text += " (the two already disagreed before this plan)"
    if item.get("row_delta") is not None:
        text += (
            f". Row attribution measured that change at {item['row_delta']:+d} rows "
            "against the prior model"
        )
    elif item.get("row_reason"):
        text += (
            ". Row attribution named that change and did not measure it: "
            f"{item['row_reason']}"
        )
    return text


def _sql_fix(check: str, item: dict[str, Any]) -> str:
    if check == "population_parent":
        return ""  # _sentence names both fixes for this rule, whichever side is stale
    if check == "additivity":
        owner = item.get("declared_on")
        where = f"the declaration on '{owner}'" if owner else "a declaration on it"
        return (
            "aggregate it the way it allows (the period-end value with MAX or a "
            "last-value window for a running total, a ratio recomputed from its "
            "numerator and denominator), or, if this sum only adds across "
            f"dimensions it does add over, correct {where} in this same plan"
        )
    restore, rule = {
        "population": ("the filter", "the declared population"),
        "grain": ("the SQL", "the declared grain"),
        "columns": ("the column", "the declared column"),
    }[check]
    return (
        f"restore {restore} if {rule} still holds, or, if changing it is the job, "
        "update the declaration in this same plan"
    )
