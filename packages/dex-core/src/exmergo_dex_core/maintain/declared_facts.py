"""Declared facts against the built relation (#496).

A declaration (#491) says what a model means: what one row is, which rows are
in, what unit a column is in, whether it adds up. It is only worth trusting if
something checks it, and most of what it says can be checked against the
relation dbt built, with one aggregate statement per relation:

- **Unit.** A column declared ``fraction`` holding values outside 0 to 1, or
  one declared ``percent`` whose values all fall within 0 to 1, is
  ``unit_out_of_range``. The first is arithmetic; the second is a diagnostic,
  since a column of genuinely small percentages lands there too, and the
  finding says so rather than calling it proof.
- **Additivity.** A column declared ``additive`` that the cumulative detector
  (:mod:`~..explore.cumulative`) reads as a running total is
  ``additivity_contradicted``: summing it multiplies the true value, which is
  the error the declaration exists to prevent.
- **Population.** A value a ``population.filters`` entry excludes (or, for an
  ``includes`` list, any value it does not include) still present in the
  relation is ``population_contradicted``, stated as a count and never as rows.
- **Parent population** (#857). A ``population.parent`` names the model, seed
  or snapshot the rows are drawn from and the key that identifies one row per
  parent. Parent keys absent from the output are ``parent_population_missing``
  and output keys absent from the parent ``parent_population_unexpected``, both
  stated as distinct-key counts. A ``population.filters`` entry whose column the
  parent also has is applied to the parent first, so a parent the declaration
  excludes is counted as excluded rather than missing.
- **Grain**, on the build path only. `maintain verify` checks a declared grain
  through its own grain axis, where the declaration is the first of four
  sources (:func:`~.verify.grain_plan`); a build's sweep folds a declared grain
  no ``unique`` test covers into the same statement here, so it is priced with
  the rest rather than through a second gate.

``null_rule: never`` needs nothing here: it renders as a ``not_null`` test,
which the build already runs and reports.

Every statement is aggregate-only by construction (``COUNT``, ``MIN``, ``MAX``
and the detector's lag-and-compare), so no row leaves the warehouse, and the
literals in a population check are the declaration's own committed values,
quoted and escaped, never a value read from data. What this module could not
check is named per finding class in ``suppressed`` or per model in ``notes``,
so a class that did not run is never an empty, clean-looking list.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cache import match_identifier
from ..dbt_project import manifest_node_label, strip_relation_quoting
from ..errors import WarehouseQueryError
from ..guards.pii_meta import entry_meta
from .drift import DriftFinding

#: The declared-fact finding classes this module owns, as `suppressed` names them.
CLASSES = ("unit", "additivity", "population", "parent_population")

#: The units this module can check. Anything else (``currency:EUR``, ``count``,
#: a plain unit) has no range to check against.
_CHECKED_UNITS = frozenset({"fraction", "percent"})

#: How many of a filter's values a finding names before counting the rest.
_VALUES_SHOWN = 5


@dataclass(frozen=True)
class PopulationRule:
    column: str
    includes: tuple[Any, ...] | None = None
    excludes: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class ParentRule:
    """A declared ``population.parent``, with the parent's relation as the
    manifest names it (``None`` when no project node by that name has one)."""

    model: str
    relation: str | None
    #: (output column, parent column) pairs, in declared order.
    key: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DeclaredModel:
    """One model's checkable declared facts, read from the compiled manifest.

    ``grain`` is carried for the build path's own grain check and for choosing
    the entity and time columns the additivity probe orders by; ``roles`` is
    there for the second of those only.
    """

    model: str
    unique_id: str
    relation: str
    units: tuple[tuple[str, str], ...] = ()
    additive: tuple[str, ...] = ()
    filters: tuple[PopulationRule, ...] = ()
    grain: tuple[str, ...] = ()
    roles: tuple[tuple[str, str], ...] = ()
    #: Lowered columns a dbt ``unique`` test already covers on this model.
    unique_tested: frozenset[str] = frozenset()
    #: The manifest's declared ``data_type`` per lowered column, where one is.
    declared_types: tuple[tuple[str, str], ...] = ()
    parent: ParentRule | None = None

    def declares(self, finding_class: str) -> bool:
        return bool(
            {
                "unit": self.units,
                "additivity": self.additive,
                "population": self.filters,
                "parent_population": self.parent,
            }[finding_class]
        )


def plan(
    project_dir: Path, *, scope: set[str] | None = None
) -> tuple[list[DeclaredModel], list[str]]:
    """Every selected model's checkable declared facts. Free and connectionless:
    read from ``the compiled ``manifest.json``, where dbt has already merged each
    model's and column's ``config.meta`` into the node.

    ``scope`` holds model unique ids or lowered labels, the same convention
    :func:`~.verify.row_population_plan` follows, so a build (ids) and a
    `maintain verify` call (names) narrow this the same way. An ephemeral model
    has no relation to check and is left out.
    """

    from .verify import _manifest_nodes

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return [], [
            "no compiled manifest found; run `dbt compile` or `dbt build` for "
            "declared-fact findings"
        ]

    unique_tested: dict[str, set[str]] = {}
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        if not isinstance(meta, dict) or meta.get("name") != "unique":
            continue
        raw_kwargs = meta.get("kwargs")
        kwargs: dict[str, Any] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
        column = kwargs.get("column_name")
        attached = node.get("attached_node")
        if isinstance(column, str) and isinstance(attached, str):
            unique_tested.setdefault(attached, set()).add(column.lower())

    relations_by_name: dict[str, str] = {}
    for node in nodes.values():
        if (
            isinstance(node, dict)
            and node.get("resource_type") in ("model", "seed", "snapshot")
            and isinstance(node.get("name"), str)
            and isinstance(node.get("relation_name"), str)
            and node["relation_name"]
        ):
            relations_by_name.setdefault(
                node["name"].lower(), strip_relation_quoting(node["relation_name"])
            )

    models: list[DeclaredModel] = []
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        if not (isinstance(node.get("name"), str) and node["name"]):
            continue
        name = manifest_node_label(uid, node)
        if scope is not None and uid not in scope and name.lower() not in scope:
            continue
        relation = node.get("relation_name")
        if not (isinstance(relation, str) and relation):
            continue
        declared = _read(node)
        if declared is None:
            continue
        units, additive, filters, grain, roles, types, stated_parent = declared
        parent = (
            ParentRule(
                model=stated_parent[0],
                relation=relations_by_name.get(stated_parent[0].lower()),
                key=stated_parent[1],
            )
            if stated_parent is not None
            else None
        )
        if not (units or additive or filters or grain or parent):
            continue
        models.append(
            DeclaredModel(
                model=name,
                unique_id=uid,
                relation=strip_relation_quoting(relation),
                units=units,
                additive=additive,
                filters=filters,
                grain=grain,
                roles=roles,
                unique_tested=frozenset(unique_tested.get(uid, set())),
                declared_types=types,
                parent=parent,
            )
        )
    return sorted(models, key=lambda m: m.model), []


def _read(node: dict[str, Any]):
    model_dex = entry_meta(node).get("dex")
    model_dex = model_dex if isinstance(model_dex, dict) else {}
    units: list[tuple[str, str]] = []
    additive: list[str] = []
    roles: list[tuple[str, str]] = []
    types: list[tuple[str, str]] = []
    columns = node.get("columns")
    for key, column in columns.items() if isinstance(columns, dict) else []:
        if not isinstance(column, dict):
            continue
        name = column.get("name") if isinstance(column.get("name"), str) else key
        if not isinstance(name, str) or not name:
            continue
        if isinstance(column.get("data_type"), str) and column["data_type"]:
            types.append((name.lower(), column["data_type"]))
        dex = entry_meta(column).get("dex")
        if not isinstance(dex, dict):
            continue
        unit = dex.get("unit")
        if isinstance(unit, str) and unit.strip().lower() in _CHECKED_UNITS:
            units.append((name, unit.strip().lower()))
        if dex.get("additivity") == "additive":
            additive.append(name)
        if isinstance(dex.get("role"), str):
            roles.append((name, dex["role"]))

    filters: list[PopulationRule] = []
    population = model_dex.get("population")
    entries = population.get("filters") if isinstance(population, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("column"), str):
            continue
        includes = _values(entry.get("includes"))
        excludes = _values(entry.get("excludes"))
        if includes or excludes:
            filters.append(PopulationRule(entry["column"], includes, excludes))

    from ..project_definitions import read_population_parent

    stated = read_population_parent(
        population.get("parent") if isinstance(population, dict) else None
    )
    parent = (stated.model, tuple(stated.key.items())) if stated is not None else None

    grain = model_dex.get("grain")
    grain_columns = (
        tuple(grain)
        if isinstance(grain, list) and grain and all(isinstance(g, str) for g in grain)
        else ()
    )
    return (
        tuple(units),
        tuple(additive),
        tuple(filters),
        grain_columns,
        tuple(roles),
        tuple(types),
        parent,
    )


def _values(raw: Any) -> tuple[Any, ...] | None:
    if not isinstance(raw, list):
        return None
    kept = tuple(value for value in raw if isinstance(value, str | int | float | bool))
    return kept or None


# --- turning a plan into statements ----------------------------------------------


@dataclass
class _Probe:
    """One model's statements, and what each alias in them means."""

    model: DeclaredModel
    identifier: str
    aggregate_sql: str | None = None
    units: list[tuple[int, str, str, str | None]] = field(default_factory=list)
    filters: list[tuple[int, PopulationRule, str]] = field(default_factory=list)
    grain: list[str] | None = None
    cumulative: Any = None
    #: The relation's spelling of a probed column -> the declaration's, so a
    #: finding names the column the way the model declares it, as every other
    #: finding here does, whatever case the warehouse folded it to.
    spellings: dict[str, str] = field(default_factory=dict)
    cumulative_sql: str | None = None
    #: The parent-population statement and what it applied (#857).
    parent_sql: str | None = None
    parent_identifier: str | None = None
    parent_filters_applied: list[str] = field(default_factory=list)
    parent_filters_unapplied: list[str] = field(default_factory=list)

    def statements(self) -> list[str]:
        return [
            s for s in (self.aggregate_sql, self.cumulative_sql, self.parent_sql) if s
        ]


@dataclass
class Prepared:
    """Every statement the checks need, settled before anything is priced.

    Built once and then priced and run, so the metadata read that decides each
    statement's shape (which columns exist, and their types) happens once per
    relation rather than once for the estimate and again for the run.
    """

    probes: list[_Probe] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Per finding class: how many models declare it in scope, and the reason
    #: each one that will not be checked was dropped.
    declared: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    #: Every model in scope that declares a parent population, so each one gets
    #: a record whether or not a statement could be written for it.
    parent_models: list[DeclaredModel] = field(default_factory=list)

    def statements(self) -> list[str]:
        return [s for probe in self.probes for s in probe.statements()]


def prepare(
    adapter,
    models: list[DeclaredModel],
    live_identifiers: list[str] | None,
    *,
    check_grain: bool = False,
    tested_grain: bool = False,
) -> Prepared:
    """Resolve each model to its relation and write the statements that check it.

    ``live_identifiers`` ``None`` means the relations cannot be listed yet (the
    build has not run, at pricing time): each model's own relation name is used
    as written and its columns are the ones the declaration names. Otherwise a
    model whose relation does not match exactly one live object is dropped with
    the reason, and its columns are resolved against what the relation has, so
    a declared spelling that differs only in case still reaches the column and a
    declared column the relation lacks is named rather than sent to fail.

    ``check_grain`` folds a declared grain into the aggregate statement, so
    it is counted against the same ``COUNT(*)`` and priced with everything
    else rather than resting on a catalog row count a freshly built relation
    may not have yet. A single-column grain a ``unique`` test already covers is
    left to that test unless ``tested_grain`` says otherwise: a build runs the
    test itself, while `maintain verify` ranks the declaration first among its
    grain sources and so checks it here instead.
    """

    from ..guards.cost_guard import CostGuardError

    prepared = Prepared()
    for finding_class in CLASSES:
        prepared.declared[finding_class] = sum(
            m.declares(finding_class) for m in models
        )
        prepared.dropped[finding_class] = []
    prepared.parent_models = [m for m in models if m.parent is not None]

    for model in models:
        if live_identifiers is None:
            identifier = model.relation
            live = None
        else:
            matches = match_identifier(model.relation, live_identifiers)
            if len(matches) != 1:
                _drop(
                    prepared,
                    model,
                    f"{model.model}: no single relation in the warehouse matched "
                    f"{model.relation}",
                    check_grain,
                )
                continue
            identifier = matches[0]
            try:
                _meta, columns = adapter.table_metadata(identifier)
            except CostGuardError:
                raise
            except Exception as exc:
                _drop(
                    prepared,
                    model,
                    f"{model.model}: the relation's columns could not be read "
                    f"({_refusal(exc)})",
                    check_grain,
                )
                continue
            live = {c.name.lower(): (c.name, c.data_type) for c in columns}
            if not live:
                live = None
        probe = _probe(
            model,
            identifier,
            live,
            adapter.dialect,
            prepared,
            check_grain and (tested_grain or grain_untested(model)),
        )
        if model.parent is not None:
            _parent_probe(adapter, probe, live, live_identifiers, prepared)
        if probe.aggregate_sql or probe.cumulative_sql or probe.parent_sql:
            prepared.probes.append(probe)
    return prepared


def _drop(
    prepared: Prepared, model: DeclaredModel, reason: str, check_grain: bool
) -> None:
    for finding_class in CLASSES:
        if model.declares(finding_class):
            prepared.dropped[finding_class].append(reason)
    if check_grain and model.grain:
        prepared.notes.append(f"the declared grain was not checked for {reason}")


def grain_untested(model: DeclaredModel) -> bool:
    """A declared grain no ``unique`` test checks: a composite, or a single
    column whose test the declaration did not render."""

    if not model.grain:
        return False
    return len(model.grain) > 1 or model.grain[0].lower() not in model.unique_tested


def _probe(
    model: DeclaredModel,
    identifier: str,
    live: dict[str, tuple[str, str]] | None,
    dialect: str,
    prepared: Prepared,
    check_grain: bool,
) -> _Probe:
    from ..explore import cumulative as cumulative_mod
    from ..explore.profile import is_numeric_type
    from ..guards.sql_guard import assert_select_only

    declared_types = dict(model.declared_types)

    def resolve(column: str) -> tuple[str, str | None] | None:
        if live is None:
            return column, declared_types.get(column.lower())
        return live.get(column.lower())

    probe = _Probe(model=model, identifier=identifier)
    parts = ["COUNT(*) AS dex_rows"]
    missing: dict[str, list[str]] = {}

    for index, (column, unit) in enumerate(model.units):
        resolved = resolve(column)
        if resolved is None:
            missing.setdefault("unit", []).append(column)
            continue
        spelled, data_type = resolved
        if data_type is not None and not is_numeric_type(data_type):
            prepared.notes.append(
                f"the declared unit of {model.model}.{column} was not checked: the "
                f"column is {data_type}, not a number"
            )
            continue
        q = _quote(spelled)
        parts.append(
            f"MIN({q}) AS dex_min_{index}, MAX({q}) AS dex_max_{index}, "
            f"COUNT({q}) AS dex_n_{index}"
        )
        probe.units.append((index, column, unit, data_type))

    for index, rule in enumerate(model.filters):
        resolved = resolve(rule.column)
        if resolved is None:
            missing.setdefault("population", []).append(rule.column)
            continue
        spelled, data_type = resolved
        subject, values = _comparison(
            spelled, data_type, rule.includes or rule.excludes
        )
        if rule.excludes:
            parts.append(
                f"COUNT(CASE WHEN {subject} IN ({values}) THEN 1 END) "
                f"AS dex_pop_{index}"
            )
        else:
            parts.append(
                f"COUNT(CASE WHEN {_quote(spelled)} IS NULL OR {subject} NOT IN "
                f"({values}) THEN 1 END) AS dex_pop_{index}, "
                f"COUNT(CASE WHEN {_quote(spelled)} IS NULL THEN 1 END) "
                f"AS dex_popnull_{index}"
            )
        probe.filters.append((index, rule, rule.column))

    if check_grain and model.grain:
        resolved_grain = [resolve(column) for column in model.grain]
        if all(r is not None for r in resolved_grain):
            names = [r[0] for r in resolved_grain if r is not None]
            parts.append(
                "(SELECT COUNT(*) FROM (SELECT DISTINCT "  # noqa: S608
                + ", ".join(_quote(n) for n in names)
                + f" FROM {_quote_relation(identifier)}) AS dex_g) AS dex_grain"
            )
            probe.grain = list(model.grain)
        else:
            prepared.notes.append(
                f"the declared grain on {model.model} names a column the relation "
                "does not have, so it was not checked: "
                + ", ".join(
                    c
                    for c, r in zip(model.grain, resolved_grain, strict=True)
                    if r is None
                )
            )

    for finding_class, columns in sorted(missing.items()):
        prepared.notes.append(
            f"the declared {finding_class} on {model.model} names column(s) the "
            "built relation does not have, so it was not checked: "
            + ", ".join(columns)
            + (
                "; a column a model aggregates away cannot carry its population "
                "rule into the result, so that rule is checked upstream or not at all"
                if finding_class == "population"
                else ""
            )
        )

    if probe.units or probe.filters or probe.grain:
        sql = f"SELECT {', '.join(parts)} FROM {_quote_relation(identifier)}"  # noqa: S608
        probe.aggregate_sql = assert_select_only(
            _transpile(sql, dialect), dialect=dialect
        )

    if model.additive:
        candidate, reason = _cumulative_candidate(model, resolve, live)
        if candidate is None:
            prepared.notes.append(
                f"the declared additivity on {model.model} was not checked: {reason}"
            )
            prepared.dropped["additivity"].append(f"{model.model}: {reason}")
        else:
            probe.cumulative = candidate
            probe.spellings = {
                resolved[0]: column
                for column in (*model.additive, *model.grain, *dict(model.roles))
                if (resolved := resolve(column)) is not None
            }
            probe.cumulative_sql = assert_select_only(
                _transpile(cumulative_mod.probe_sql(identifier, candidate), dialect),
                dialect=dialect,
            )

    for finding_class in ("unit", "population"):
        if model.declares(finding_class) and not (
            probe.units if finding_class == "unit" else probe.filters
        ):
            prepared.dropped[finding_class].append(
                f"{model.model}: no declared column could be measured"
            )
    return probe


def _parent_probe(
    adapter,
    probe: _Probe,
    live: dict[str, tuple[str, str]] | None,
    live_identifiers: list[str] | None,
    prepared: Prepared,
) -> None:
    """Write the statement that holds a model's keys to its declared parent's.

    One aggregate statement of scalar subqueries, each an ungrouped count over
    one row, so nothing but counts comes back:

    - the output's rows, its distinct non-NULL keys, and its rows with a NULL
      key component;
    - the parent's distinct non-NULL keys, and those its population filters
      keep (the eligible parents);
    - how many eligible parents the output holds, and how many output keys the
      parent holds (each a LEFT JOIN of distinct keys against distinct keys
      with a hit flag compared ``= 1``, so a connector that fills a miss with a
      default still counts it as one).

    A filter applies to the parent only where the parent has that column: the
    declaration states the filter over the model's columns, and the parent is a
    different relation, so a filter it cannot evaluate is named as unapplied
    rather than guessed at.
    """

    from ..guards.cost_guard import CostGuardError
    from ..guards.sql_guard import assert_select_only

    model = probe.model
    parent = model.parent
    if parent is None:  # pragma: no cover - callers only pass parent models
        raise ValueError("no declared parent")
    name = model.model
    if parent.relation is None:
        prepared.dropped["parent_population"].append(
            f"{name}: the project has no built model, seed or snapshot named "
            f"'{parent.model}' to compare against"
        )
        return

    parent_live: dict[str, tuple[str, str]] | None = None
    if live_identifiers is None:
        parent_identifier = parent.relation
    else:
        matches = match_identifier(parent.relation, live_identifiers)
        if len(matches) != 1:
            prepared.dropped["parent_population"].append(
                f"{name}: no single relation in the warehouse matched the parent "
                f"{parent.relation}"
            )
            return
        parent_identifier = matches[0]
        try:
            _meta, columns = adapter.table_metadata(parent_identifier)
        except CostGuardError:
            raise
        except Exception as exc:
            prepared.dropped["parent_population"].append(
                f"{name}: the parent's columns could not be read ({_refusal(exc)})"
            )
            return
        parent_live = {c.name.lower(): (c.name, c.data_type) for c in columns} or None

    declared_types = dict(model.declared_types)

    def out_column(column: str) -> str | None:
        if live is None:
            return column
        found = live.get(column.lower())
        return found[0] if found else None

    def parent_column(column: str) -> tuple[str, str | None] | None:
        if parent_live is None:
            return column, None
        return parent_live.get(column.lower())

    out_keys = [out_column(o) for o, _p in parent.key]
    parent_keys = [parent_column(p) for _o, p in parent.key]
    absent = [o for (o, _p), r in zip(parent.key, out_keys, strict=True) if r is None]
    absent += [
        f"{parent.model}.{p}"
        for (_o, p), r in zip(parent.key, parent_keys, strict=True)
        if r is None
    ]
    if absent:
        prepared.dropped["parent_population"].append(
            f"{name}: the parent key names column(s) a relation does not have "
            f"({', '.join(absent)})"
        )
        return

    out_names = [c for c in out_keys if c is not None]
    parent_names = [c[0] for c in parent_keys if c is not None]
    out_rel = _quote_relation(probe.identifier)
    parent_rel = _quote_relation(parent_identifier)
    out_nonnull = " AND ".join(f"{_quote(c)} IS NOT NULL" for c in out_names)
    out_anynull = " OR ".join(f"{_quote(c)} IS NULL" for c in out_names)
    parent_nonnull = " AND ".join(f"{_quote(c)} IS NOT NULL" for c in parent_names)

    kept: list[str] = []
    for rule in model.filters:
        resolved = parent_column(rule.column)
        if resolved is None:
            probe.parent_filters_unapplied.append(rule.column)
            continue
        spelled, data_type = resolved
        data_type = data_type or declared_types.get(rule.column.lower())
        subject, values = _comparison(
            spelled, data_type, rule.includes or rule.excludes
        )
        if rule.excludes:
            kept.append(f"({_quote(spelled)} IS NULL OR {subject} NOT IN ({values}))")
        else:
            kept.append(f"{subject} IN ({values})")
        probe.parent_filters_applied.append(rule.column)
    eligible_where = " AND ".join([parent_nonnull, *kept])

    out_select = ", ".join(f"{_quote(c)} AS k{i}" for i, c in enumerate(out_names))
    parent_select = ", ".join(
        f"{_quote(c)} AS k{i}" for i, c in enumerate(parent_names)
    )
    on = " AND ".join(f"a.k{i} = b.k{i}" for i in range(len(out_names)))

    def distinct(select: str, relation: str, where: str, hit: bool = False) -> str:
        flag = ", 1 AS dex_hit" if hit else ""
        return f"(SELECT DISTINCT {select}{flag} FROM {relation} WHERE {where})"  # noqa: S608

    out_d = distinct(out_select, out_rel, out_nonnull)
    out_hit = distinct(out_select, out_rel, out_nonnull, hit=True)
    parent_all = distinct(parent_select, parent_rel, parent_nonnull)
    parent_all_hit = distinct(parent_select, parent_rel, parent_nonnull, hit=True)
    parent_eligible = distinct(parent_select, parent_rel, eligible_where)
    sql = (
        "SELECT "  # noqa: S608
        f"(SELECT COUNT(*) FROM {out_rel}) AS dex_out_rows, "
        f"(SELECT COUNT(*) FROM {out_rel} WHERE {out_anynull}) AS dex_out_null, "
        f"(SELECT COUNT(*) FROM {out_d} a) AS dex_out_keys, "
        f"(SELECT COUNT(*) FROM {parent_all} a) AS dex_parent_keys, "
        f"(SELECT COUNT(*) FROM {parent_eligible} a) AS dex_eligible_parents, "
        f"(SELECT COUNT(CASE WHEN b.dex_hit = 1 THEN 1 END) FROM {parent_eligible} a "
        f"LEFT JOIN {out_hit} b ON {on}) AS dex_parents_present, "
        f"(SELECT COUNT(CASE WHEN b.dex_hit = 1 THEN 1 END) FROM {out_d} a "
        f"LEFT JOIN {parent_all_hit} b ON {on}) AS dex_out_known"
    )
    probe.parent_identifier = parent_identifier
    probe.parent_sql = assert_select_only(
        _transpile(sql, adapter.dialect), dialect=adapter.dialect
    )


def _cumulative_candidate(model: DeclaredModel, resolve, live):
    """The entity and time columns the detector orders an additive column by.

    Read from the declaration rather than guessed from a profile, since a built
    relation in the dev target is never profiled: a composite grain pairing an
    entity with a time column is the strongest choice (the table is one row per
    entity per period by its own statement), then a ``time`` role and a
    ``foreign_key`` (or ``key``) role, then the relation's first temporal column
    and first id-shaped one. A single-column grain is never the entity, since a
    partition of one row per key has no sequence to read.
    """

    from ..adapters.base import is_temporal_type
    from ..explore.cumulative import CumulativeCandidate
    from ..explore.profile import is_numeric_type
    from ..explore.relationships import is_id_shaped

    roles = dict(model.roles)

    def temporal(column: str) -> bool:
        resolved = resolve(column)
        if resolved is None:
            return False
        return roles.get(column) == "time" or (
            resolved[1] is not None and is_temporal_type(resolved[1])
        )

    grain = list(model.grain)
    temporals = [c for c in grain if temporal(c)]
    temporals += [c for c, role in model.roles if role == "time" and resolve(c)]
    entities = [c for c in grain if len(grain) > 1 and not temporal(c) and resolve(c)]
    entities += [
        c for c, role in model.roles if role in ("foreign_key", "key") and resolve(c)
    ]
    single_key = grain[0].lower() if len(grain) == 1 else None
    entities = [c for c in entities if c.lower() != single_key]
    if live is not None:
        temporals += [
            spelled
            for spelled, data_type in live.values()
            if is_temporal_type(data_type)
        ]
        entities += [
            spelled
            for spelled, _ in live.values()
            if is_id_shaped(spelled) and spelled.lower() != single_key
        ]
    if not temporals or not entities:
        missing = " and ".join(
            f"no {what}"
            for what, found in (("entity", entities), ("time column", temporals))
            if not found
        )
        return None, (
            f"there is {missing} to order it by; declare a `time` role and a "
            "`foreign_key` role, or a composite grain naming both"
        )
    entity, time = resolve(entities[0])[0], resolve(temporals[0])[0]
    measures = []
    for column in model.additive:
        resolved = resolve(column)
        if resolved is None or resolved[0] in (entity, time):
            continue
        if resolved[1] is not None and not is_numeric_type(resolved[1]):
            continue
        measures.append(resolved[0])
    if not measures:
        return None, "no column declared additive is a numeric column of the relation"
    return CumulativeCandidate(entity, time, measures), None


def _comparison(
    column: str, data_type: str | None, values: tuple[Any, ...] | None
) -> tuple[str, str]:
    """The column and literal list a population check compares, kept to one type.

    A filter declared as strings against a numeric column (or the reverse) would
    be refused by a strict warehouse, so the two sides are compared as text
    whenever their types disagree, and as written whenever they agree.
    """

    from ..adapters.base import is_string_type

    values = values or ()
    textual_column = data_type is not None and is_string_type(data_type)
    as_text = any(isinstance(v, str) for v in values) or textual_column
    subject = _quote(column)
    if as_text and data_type is not None and not textual_column:
        subject = f"CAST({subject} AS VARCHAR)"
    rendered = ", ".join(_literal(v, as_text=as_text) for v in values)
    return subject, rendered


def _literal(value: Any, *, as_text: bool) -> str:
    if as_text:
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        return "'" + text.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return repr(value)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_relation(identifier: str) -> str:
    return ".".join(_quote(part) for part in identifier.split("."))


def _transpile(sql: str, dialect: str) -> str:
    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


# --- pricing ------------------------------------------------------------------------


def cost(adapter, prepared: Prepared) -> tuple[float, int]:
    """``(estimate, statement_count)`` for every statement ``prepared`` holds.

    Zero on a connector with no cost gate, where the statements bill nothing.
    """

    from .. import command_args

    statements = prepared.statements()
    if not statements or command_args.cost_gate(adapter) is None:
        return 0.0, len(statements)
    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0, len(statements)
    return sum(float(estimator(sql)) for sql in statements), len(statements)


def _standalone_handshake(adapter) -> Callable[[float, int], object | None]:
    from .. import command_args

    def ask(estimate: float, statement_count: int):
        return command_args.confirmation_request(
            "maintain verify",
            adapter,
            estimate,
            per_table={"(declared facts)": estimate},
            axes=["declared_facts"],
            notes=[
                f"the estimate buys {statement_count} aggregate statement(s) "
                "checking what the project's declarations say against the "
                "relations they describe"
            ],
        )

    return ask


# --- running and judging --------------------------------------------------------


@dataclass
class DeclaredFactsResult:
    findings: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suppressed: dict[str, str] = field(default_factory=dict)
    offer: object | None = None
    # The declared grains the statement counted unique, per model (#841).
    proven_grains: dict[str, dict] = field(default_factory=dict)
    #: One record per declared parent population, measured or not (#857): the
    #: declaration, the counts, and a status, so a clean check is visible as
    #: evidence rather than as an absent finding.
    parent_coverage: list[dict] = field(default_factory=list)


def nothing_declared(finding_class: str, where: str) -> str:
    return {
        "unit": f"no model {where} declares a column's unit as fraction or percent",
        "additivity": f"no model {where} declares a column additive",
        "population": (
            f"no model {where} declares population filters with includes or excludes"
        ),
        "parent_population": f"no model {where} declares a population.parent",
    }[finding_class]


def findings(
    adapter,
    prepared: Prepared,
    *,
    where: str = "in scope",
    timeout_seconds: float = 30.0,
    handshake: Callable[[float, int], object | None] | None = None,
    pii_overrides: Any = None,
    min_rows: int = 100,
) -> DeclaredFactsResult:
    """Run what ``prepared`` holds and judge each declared fact against it.

    ``handshake`` gates the statements exactly as :func:`~.verify.relation_
    counts` gates its counts: handed the estimate and the statement count, it
    returns a request to defer behind or ``None`` to proceed. ``where`` names
    the scope in a suppression reason ("in scope", "this build ran").
    """

    from ..explore import cumulative as cumulative_mod
    from ..guards.cost_guard import CostGuardError

    result = DeclaredFactsResult(notes=list(prepared.notes))
    for finding_class in CLASSES:
        if not prepared.declared.get(finding_class):
            result.suppressed[finding_class] = nothing_declared(finding_class, where)

    statements = prepared.statements()
    if statements:
        estimate, count = cost(adapter, prepared)
        ask = handshake if handshake is not None else _standalone_handshake(adapter)
        offer = ask(estimate, count)
        if offer is not None:
            reason = (
                "checking it is a scan, offered above; the declarations were read "
                "but not measured"
            )
            for finding_class in CLASSES:
                if prepared.declared.get(finding_class):
                    result.suppressed[finding_class] = reason
            pending = sorted(p.model.model for p in prepared.probes if p.grain)
            if pending:
                result.notes.append(
                    "the declared grain was not measured for "
                    + ", ".join(pending)
                    + ": counting it is a scan, offered above"
                )
            result.offer = offer
            _unchecked_parents(prepared, result, "scan_offered", reason)
            return result

    checked: dict[str, int] = dict.fromkeys(CLASSES, 0)
    for probe in prepared.probes:
        model = probe.model.model
        if probe.aggregate_sql:
            try:
                answer = adapter.run_query(
                    probe.aggregate_sql, max_rows=1, timeout_seconds=timeout_seconds
                )
            except CostGuardError:
                raise
            except Exception as exc:
                detail = _refusal(exc)
                reason = f"{model}: the warehouse refused the check ({detail})"
                result.notes.append(
                    f"the declared facts on {model} could not be measured: the "
                    f"warehouse refused the statement ({detail})"
                )
                for finding_class in ("unit", "population"):
                    if probe.model.declares(finding_class):
                        prepared.dropped[finding_class].append(reason)
            else:
                row = dict(zip(answer.columns, answer.cells[0], strict=True))
                row = {str(k).lower(): v for k, v in row.items()}
                _judge_aggregate(
                    probe, row, result, checked, prepared, pii_overrides, min_rows
                )
        if probe.parent_sql:
            _run_parent(adapter, probe, result, checked, prepared, timeout_seconds)
        if probe.cumulative is not None:
            try:
                fractions = cumulative_mod.measure_fractions(
                    adapter,
                    probe.identifier,
                    probe.cumulative,
                    timeout_seconds=timeout_seconds,
                )
            except CostGuardError:
                raise
            except Exception as exc:
                detail = _refusal(exc)
                result.notes.append(
                    f"the declared additivity on {model} could not be measured: "
                    f"the warehouse refused the statement ({detail})"
                )
                prepared.dropped["additivity"].append(
                    f"{model}: the warehouse refused the check ({detail})"
                )
            else:
                _judge_additivity(probe, fractions, result, checked, prepared)

    _unchecked_parents(prepared, result, "not_measured", None)
    for finding_class in CLASSES:
        if finding_class in result.suppressed:
            continue
        if prepared.declared.get(finding_class) and not checked[finding_class]:
            reasons = prepared.dropped.get(finding_class) or [
                "no declared column could be measured"
            ]
            result.suppressed[finding_class] = _suppression_reason(reasons)
    return result


def _unchecked_parents(
    prepared: Prepared,
    result: DeclaredFactsResult,
    reason_code: str,
    reason: str | None,
) -> None:
    """A record for every declared parent population that has none yet, so an
    obligation nothing measured reads as unchecked rather than as absent."""

    recorded = {r["model"] for r in result.parent_coverage}
    for model in prepared.parent_models:
        if model.model in recorded:
            continue
        why = reason or next(
            (
                d.partition(": ")[2]
                for d in prepared.dropped.get("parent_population", [])
                if d.startswith(f"{model.model}: ")
            ),
            "the check could not be written",
        )
        probe = next(
            (p for p in prepared.probes if p.model.model == model.model),
            _Probe(model=model, identifier=model.relation),
        )
        result.parent_coverage.append(
            _parent_record(probe, None, "unchecked", reason_code, why)
        )


def _run_parent(
    adapter,
    probe: _Probe,
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
    timeout_seconds: float,
) -> None:
    from ..guards.cost_guard import CostGuardError

    model = probe.model.model
    try:
        answer = adapter.run_query(
            probe.parent_sql, max_rows=1, timeout_seconds=timeout_seconds
        )
    except CostGuardError:
        raise
    except Exception as exc:
        detail = _refusal(exc)
        result.notes.append(
            f"the declared population.parent on {model} could not be measured: "
            f"the warehouse refused the statement ({detail})"
        )
        prepared.dropped["parent_population"].append(
            f"{model}: the warehouse refused the check ({detail})"
        )
        result.parent_coverage.append(
            _parent_record(probe, None, "unchecked", "probe_refused", detail)
        )
        return
    row = {
        str(k).lower(): int(v or 0)
        for k, v in zip(answer.columns, answer.cells[0], strict=True)
    }
    _judge_parent(probe, row, result, checked, prepared)


def _parent_record(
    probe: _Probe,
    counts: dict[str, Any] | None,
    status: str,
    reason_code: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    parent = probe.model.parent
    if parent is None:  # pragma: no cover - callers only pass parent models
        raise ValueError("no declared parent")
    record: dict[str, Any] = {
        "model": probe.model.model,
        "declared": {
            "parent": parent.model,
            "key": [{"column": o, "parent_column": p} for o, p in parent.key],
            "filters_applied": list(probe.parent_filters_applied),
            "filters_unapplied": list(probe.parent_filters_unapplied),
        },
        "parent_relation": probe.parent_identifier or parent.relation,
        "status": status,
    }
    if counts is not None:
        record["measured"] = counts
    if reason_code is not None:
        record["reason_code"] = reason_code
    if reason is not None:
        record["reason"] = reason
    return record


def _judge_parent(
    probe: _Probe,
    row: dict[str, int],
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
) -> None:
    """Judge one declared parent population from its counts.

    Coverage is undefined, never 100%, when nothing is eligible on the parent
    side: an empty parent cannot be fully represented or under-represented. An
    output with rows but no parent keys still has its unexpected keys judged,
    since every one of them is unexpected.
    """

    model = probe.model.model
    parent = probe.model.parent
    if parent is None:  # pragma: no cover - callers only pass parent models
        raise ValueError("no declared parent")
    out_keys = row.get("dex_out_keys", 0)
    eligible = row.get("dex_eligible_parents", 0)
    present = row.get("dex_parents_present", 0)
    known = row.get("dex_out_known", 0)
    parent_keys = row.get("dex_parent_keys", 0)
    missing = eligible - present
    unexpected = out_keys - known
    counts = {
        "output_rows": row.get("dex_out_rows", 0),
        "output_keys": out_keys,
        "output_null_key_rows": row.get("dex_out_null", 0),
        "parent_keys": parent_keys,
        "eligible_parent_keys": eligible,
        "excluded_by_filters": parent_keys - eligible,
        "parents_present": present,
        "missing_parents": missing,
        "unexpected_keys": unexpected,
        "coverage": round(present / eligible, 4) if eligible else None,
    }
    if eligible == 0 and out_keys == 0:
        reason = (
            "the parent has no eligible key and the output none either, so "
            "coverage is undefined rather than complete"
        )
        prepared.dropped["parent_population"].append(f"{model}: {reason}")
        result.parent_coverage.append(
            _parent_record(probe, counts, "not_applicable", "empty_input", reason)
        )
        return
    checked["parent_population"] += 1
    key_text = ", ".join(f"{o} -> {parent.model}.{p}" for o, p in parent.key)
    data: dict[str, Any] = {
        "model": model,
        "parent": parent.model,
        "parent_relation": probe.parent_identifier,
        "key": [{"column": o, "parent_column": p} for o, p in parent.key],
        "filters_applied": list(probe.parent_filters_applied),
        "filters_unapplied": list(probe.parent_filters_unapplied),
        **counts,
    }
    contradicted = False
    if missing > 0:
        contradicted = True
        unapplied = probe.parent_filters_unapplied
        scope = (
            f" after the declared filters on {', '.join(probe.parent_filters_applied)} "
            "excluded the parents they rule out"
            if probe.parent_filters_applied
            else ""
        )
        caveat = (
            f"; the declared filter(s) on {', '.join(unapplied)} name columns the "
            "parent does not have, so some of these may be exclusions the count "
            "cannot see"
            if unapplied
            else ""
        )
        result.findings.append(
            DriftFinding(
                axis="declaration",
                code="parent_population_missing",
                identifier=model,
                severity="medium" if unapplied else "high",
                detail=(
                    f"'{model}' declares one row per {parent.model} ({key_text}), "
                    f"but {missing} of {eligible} eligible {parent.model} key(s) "
                    f"have no row in it{scope}{caveat}: a model driven from "
                    "another relation, or an inner join, drops the parents with "
                    "nothing to join"
                ),
                data=data,
            )
        )
    if unexpected > 0:
        contradicted = True
        result.findings.append(
            DriftFinding(
                axis="declaration",
                code="parent_population_unexpected",
                identifier=model,
                severity="high",
                detail=(
                    f"'{model}' declares one row per {parent.model} ({key_text}), "
                    f"but {unexpected} of its {out_keys} distinct key(s) are not "
                    f"{parent.model} keys: its rows come from somewhere other than "
                    "the declared parent. They are not a replacement population; "
                    "drive the model from the parent, or correct the declaration"
                ),
                data=data,
            )
        )
    result.parent_coverage.append(
        _parent_record(probe, counts, "contradicted" if contradicted else "satisfied")
    )


def _suppression_reason(reasons: list[str]) -> str:
    """One class's suppression line, with models that share a reason named once.

    Each reason reads ``model: why``. When every model is dropped for the same
    ``why``, repeating it per model buries the fact that the whole axis is off
    for the project, so equal reasons collapse into one entry that lists the
    models it applies to.
    """
    grouped: dict[str, list[str]] = {}
    for reason in reasons:
        model, separator, why = reason.partition(": ")
        if not separator:
            model, why = "", reason
        names = grouped.setdefault(why, [])
        if model and model not in names:
            names.append(model)
    if len(grouped) == 1:
        ((why, names),) = grouped.items()
        if len(names) > 1:
            return (
                f"no declaration could be checked ({len(names)} models: "
                f"{', '.join(names)}): {why}"
            )
    return "no declaration could be checked: " + "; ".join(
        f"{', '.join(names)}: {why}" if names else why for why, names in grouped.items()
    )


def _refusal(exc: Exception) -> str:
    """A refused statement's reason, redacted: one check going unmeasured is
    that check's note, never the sweep's failure, whatever the driver raised."""

    from ..envelope import redact

    if isinstance(exc, WarehouseQueryError):
        return exc.detail
    return redact(f"{type(exc).__name__}: {exc}")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _judge_aggregate(
    probe: _Probe,
    row: dict[str, Any],
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
    pii_overrides: Any,
    min_rows: int,
) -> None:
    from ..explore.profile import detect_pii, is_min_max_safe

    model = probe.model.model
    rows = int(row.get("dex_rows") or 0)
    if rows == 0:
        reason = f"{model}: the built relation has no rows"
        result.notes.append(
            f"the declared facts on {model} were not checked: the built relation "
            "has no rows, so there is nothing for a declaration to be wrong about yet"
        )
        for finding_class in ("unit", "population"):
            if probe.model.declares(finding_class):
                prepared.dropped[finding_class].append(reason)
        return

    unit_checked = False
    for index, column, unit, data_type in probe.units:
        non_null = int(row.get(f"dex_n_{index}") or 0)
        low, high = (
            _number(row.get(f"dex_min_{index}")),
            _number(row.get(f"dex_max_{index}")),
        )
        if non_null == 0 or low is None or high is None:
            result.notes.append(
                f"the declared unit of {model}.{column} was not checked: the column "
                "holds no non-NULL numeric value"
            )
            continue
        unit_checked = True
        # A percent whose every value sits within 0 to 1 is the fraction it
        # was probably computed as; a fraction outside it is arithmetic.
        wrong = (low < 0 or high > 1) if unit == "fraction" else high <= 1 and low >= 0
        if not wrong:
            continue
        path = f"{probe.identifier}.{column}"
        cleared = pii_overrides is not None and path.lower() in pii_overrides
        safe = cleared or is_min_max_safe(
            data_type or "DOUBLE", detect_pii(column, data_type or "DOUBLE")
        )
        result.findings.append(
            _unit_finding(model, column, unit, low, high, non_null, safe)
        )
    if unit_checked:
        checked["unit"] += 1

    population_checked = False
    for index, rule, _column in probe.filters:
        present = int(row.get(f"dex_pop_{index}") or 0)
        population_checked = True
        if present == 0:
            continue
        nulls = int(row.get(f"dex_popnull_{index}") or 0)
        result.findings.append(_population_finding(model, rule, present, nulls, rows))
    if population_checked:
        checked["population"] += 1

    if probe.grain is not None:
        distinct = row.get("dex_grain")
        if distinct is not None and int(distinct) >= rows:
            result.proven_grains[model] = {
                "columns": list(probe.grain),
                "source": "declaration",
            }
        if distinct is not None and int(distinct) < rows:
            from .verify import _grain_broken_finding

            result.findings.append(
                _grain_broken_finding(
                    model,
                    list(probe.grain),
                    rows,
                    int(distinct),
                    exact=True,
                    source="declaration",
                    min_rows=min_rows,
                )
            )


def _unit_finding(
    model: str,
    column: str,
    unit: str,
    low: float,
    high: float,
    non_null: int,
    show_values: bool,
) -> DriftFinding:
    if show_values:
        observed = f"its values run from {low:g} to {high:g}"
    else:
        observed = (
            "its observed range is withheld because the column looks like personal data"
        )
    if unit == "fraction":
        detail = (
            f"'{model}.{column}' is declared a fraction (0 to 1), but {observed}; a "
            "share computed as a percentage, or a ratio over the wrong denominator, "
            "lands here"
        )
    else:
        detail = (
            f"'{model}.{column}' is declared a percent, but {observed}, all within "
            "0 to 1, which is what a fraction stored as a percent looks like; a "
            "column of genuinely small percentages lands here too, so read it as a "
            "question rather than proof"
        )
    data: dict[str, Any] = {
        "model": model,
        "column": column,
        "declared_unit": unit,
        "non_null_count": non_null,
    }
    if show_values:
        data.update({"min": low, "max": high})
    else:
        data["values_withheld"] = True
    return DriftFinding(
        axis="declaration",
        code="unit_out_of_range",
        identifier=model,
        column=column,
        severity="medium",
        detail=detail,
        data=data,
    )


def _shown(values: Iterable[Any]) -> str:
    values = list(values)
    head = ", ".join(
        repr(v) if isinstance(v, str) else str(v) for v in values[:_VALUES_SHOWN]
    )
    extra = len(values) - _VALUES_SHOWN
    return head + (f" and {extra} more" if extra > 0 else "")


def _population_finding(
    model: str, rule: PopulationRule, present: int, nulls: int, rows: int
) -> DriftFinding:
    if rule.excludes:
        claim = f"excludes {rule.column} in ({_shown(rule.excludes)})"
        found = f"{present} of its {rows} row(s) still hold an excluded value"
    else:
        claim = f"includes only {rule.column} in ({_shown(rule.includes or ())})"
        found = f"{present} of its {rows} row(s) hold a value outside that list" + (
            f", {nulls} of them NULL" if nulls else ""
        )
    data: dict[str, Any] = {
        "model": model,
        "column": rule.column,
        "row_count": rows,
        "contradicting_rows": present,
    }
    if rule.excludes:
        data["excludes"] = list(rule.excludes)
    else:
        data["includes"] = list(rule.includes or ())
        data["null_rows"] = nulls
    return DriftFinding(
        axis="declaration",
        code="population_contradicted",
        identifier=model,
        column=rule.column,
        severity="high",
        detail=(
            f"'{model}' declares that its population {claim}, but {found}: the "
            "filter the declaration describes is missing from the SQL, or the "
            "declaration no longer describes the model"
        ),
        data=data,
    )


def _judge_additivity(
    probe: _Probe,
    fractions: dict[str, tuple[float, int]],
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
) -> None:
    from ..explore.cumulative import MIN_OBSERVATIONS, reads_cumulative

    model = probe.model.model
    candidate = probe.cumulative
    entity = probe.spellings.get(candidate.entity, candidate.entity)
    temporal = probe.spellings.get(candidate.temporal, candidate.temporal)
    any_verdict = False
    for probed in candidate.measures:
        measure = probe.spellings.get(probed, probed)
        fraction, observations = fractions.get(probed, (0.0, 0))
        verdict = reads_cumulative(fraction, observations)
        if verdict is None:
            result.notes.append(
                f"the declared additivity of {model}.{measure} was not checked: "
                f"only {observations} consecutive observation(s) within "
                f"{entity} ordered by {temporal}, and the "
                f"cumulative detector needs {MIN_OBSERVATIONS}"
            )
            continue
        any_verdict = True
        if not verdict:
            continue
        result.findings.append(
            DriftFinding(
                axis="declaration",
                code="additivity_contradicted",
                identifier=model,
                column=measure,
                severity="medium",
                detail=(
                    f"'{model}.{measure}' is declared additive, but over "
                    f"{observations} consecutive observations within "
                    f"{entity}, ordered by {temporal}, only "
                    f"{fraction:.1%} decreased: it reads as a running total or a "
                    "snapshot, and summing it across rows multiplies the true "
                    "value. Declare it semi_additive, or derive the per-row "
                    "increment"
                ),
                data={
                    "model": model,
                    "column": measure,
                    "entity": entity,
                    "ordered_by": temporal,
                    "observations": observations,
                    "decrease_fraction": fraction,
                },
            )
        )
    if any_verdict:
        checked["additivity"] += 1
    else:
        prepared.dropped["additivity"].append(
            f"{model}: too few observations for the cumulative detector"
        )
