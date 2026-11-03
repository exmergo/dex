"""A MetricFlow semantic model derived from a declared mart (issue #499).

A declaration (#491) already states nearly everything a semantic model needs:
the grain is the primary entity, a foreign key is a foreign entity, a dimension
or time column is a dimension, and a measure's aggregation and additivity is a
measure. Writing the semantic model by hand restates each of those decisions, so
this module restates them once, mechanically, and says where it could not.

What it derives is a proposal in either case. In a project that already has a
dbt semantic layer (semantic models and a time spine), a derivation that passes
every validation layer is added to the plan that declared the mart, through the
same per-definition splice ``semantic plan --definitions-file`` uses. In a
project without one, nothing is added: a semantic layer needs a time spine and
MetricFlow, and adopting either is the project owner's decision. The derivation
comes back as ``data.semantic_proposal``, shaped to be passed to ``semantic plan
--definitions-file`` as it stands.

Three things it never does. It never invents a metric: a measure is a column
fact the declaration states, a metric is a business definition, and the second
stays the author's. It never guesses: an aggregation it cannot map, a composite
grain with no single primary entity, a name another definition already holds,
each is named rather than worked around. And it never offers YAML that has not
passed MetricFlow's schemas, the metric-input check, and dbt's own parse of a
throwaway copy of the project; that parse runs for a derivation whatever
``--no-parse`` says about the rest of the plan, because a semantic YAML dbt
cannot parse breaks every dbt command in the project.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..dbt_project import DbtProjectView, EditOp
from ..semantic_declarations import (
    DEX_META_KEY,
    account,
    column_metadata,
    model_metadata,
    with_population,
)
from .plans import EditKind, PlanEdit
from .validate import EditValidationError

#: Declared aggregation (lowered, spaces and dashes as underscores) to the
#: MetricFlow ``agg`` it means. Strict on purpose: an aggregation outside this
#: table is named, never mapped to its nearest neighbour. ``percentile`` is
#: absent because MetricFlow needs ``agg_params`` a declaration does not carry.
AGGREGATIONS = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "distinct_count": "count_distinct",
    "min": "min",
    "max": "max",
    "average": "average",
    "avg": "average",
    "mean": "average",
    "median": "median",
    "sum_boolean": "sum_boolean",
}

#: Aggregations that add the column's values up, which a ``non_additive``
#: column contradicts by definition.
_SUMMING = frozenset({"sum", "sum_boolean"})

#: The period a semi-additive measure reports: the last value in the window.
#: MetricFlow's own default is ``min`` (the first), which suits fewer of the
#: columns a declaration calls semi-additive (balances, season-to-date totals),
#: so the choice is written out and named rather than left implicit.
WINDOW_CHOICE = "max"

#: Granularities a ``date_trunc`` can name that MetricFlow accepts as a time
#: dimension's ``time_granularity``.
_GRANULARITIES = ("day", "week", "month", "quarter", "year")

#: Layer prefixes stripped from a model name when it names an entity.
_LAYER_PREFIX = re.compile(r"^(fct|fact|dim|mart|marts)_", re.IGNORECASE)

_REF = re.compile(r"ref\(\s*(?:['\"][^'\"]+['\"]\s*,\s*)?['\"]([^'\"]+)['\"]")

#: Where the throwaway time spine goes in the parse copy of a project that has
#: none. Never written to the project itself.
_SHADOW_SPINE = "dex_shadow_time_spine"


@dataclass
class Derivation:
    """One mart's derived semantic model and everything said about it."""

    model: str
    #: ``included``, ``proposed``, ``failed``, ``unvalidated`` or ``skipped``.
    status: str = "proposed"
    body: dict[str, Any] | None = None
    path: str | None = None
    #: Why the derivation cannot be offered at all; set means ``failed``.
    blocked: str | None = None
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    validation: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Declared fields the semantic model carries, and those it does not with why.
    carried: list[str] = field(default_factory=list)
    not_carried: dict[str, str] = field(default_factory=dict)

    @property
    def content(self) -> str | None:
        return None if self.body is None else _dump(self.body)

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.body is not None:
            out["semantic_model"] = self.body["name"]
            out["path"] = self.path
            out["content"] = self.content
        if self.blocked:
            out["reason"] = self.blocked
        if self.validation:
            out["validation"] = self.validation
        out["notes"] = self.notes
        out["limitations"] = self.limitations
        out["carried"] = self.carried
        out["not_carried"] = self.not_carried
        return out


# --- which models --------------------------------------------------------------------


def in_marts(sql_path: str, marts_paths: Iterable[str]) -> bool:
    """Whether a model's SQL file sits under one of the configured marts folders."""

    candidate = PurePosixPath(sql_path)
    return any(PurePosixPath(root) in candidate.parents for root in marts_paths if root)


def declares_something(declared: Mapping[str, Any]) -> bool:
    return bool(declared.get("grain") or declared.get("columns"))


# --- reading the project -------------------------------------------------------------


def documents(files: Mapping[str, str]) -> list[tuple[str, Any]]:
    """Every parseable YAML document in the project, with its path."""

    out: list[tuple[str, Any]] = []
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            out.append((path, yaml.safe_load(files[path])))
        except yaml.YAMLError:
            continue
    return out


def semantic_models_by_ref(
    docs: Iterable[tuple[str, Any]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Each dbt model to the semantic models sitting on it, with their files."""

    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path, parsed in docs:
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("semantic_models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                continue
            match = _REF.search(entry["model"])
            if match:
                out.setdefault(match.group(1), []).append((path, entry))
    return out


def has_semantic_models(docs: Iterable[tuple[str, Any]]) -> bool:
    return any(
        isinstance(parsed, dict) and parsed.get("semantic_models")
        for _path, parsed in docs
    )


# --- the derivation itself -----------------------------------------------------------


def derive(
    model: str,
    declared: Mapping[str, Any],
    *,
    column_types: Mapping[str, tuple[str | None, str | None]],
    on_ref: Mapping[str, list[tuple[str, dict[str, Any]]]],
) -> Derivation:
    """The semantic model ``declared`` states, with every gap named.

    ``column_types`` maps a column to ``(kind, granularity)``: kind is ``date``,
    ``timestamp``, ``other`` or None where nothing says, and granularity is set
    where the SQL truncates the column to one. ``on_ref`` is
    :func:`semantic_models_by_ref` over the project as it will read, which is how
    a foreign key finds the entity name the model it references already uses.
    """

    derivation = Derivation(model=model)
    notes, limitations = derivation.notes, derivation.limitations
    carried: list[str] = []
    reasons: dict[str, str] = {}
    columns: dict[str, dict[str, Any]] = {
        name: dict(fields)
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]

    entities: list[dict[str, Any]] = []
    primary: str | None = None
    if len(grain) == 1:
        primary = _entity_name(grain[0], model)
        entities.append(
            _with_meta(
                {"name": primary, "type": "primary", "expr": grain[0]},
                columns.get(grain[0]),
            )
        )
        carried.extend(["grain", *_carried_column(grain[0], columns.get(grain[0]))])
    elif len(grain) > 1:
        reasons["grain"] = (
            "composite: MetricFlow's primary entity is one column, and none was guessed"
        )
        limitations.append(
            f"the grain is composite ({', '.join(grain)}), so no single column is "
            "the primary entity and none was guessed. MetricFlow needs one for any "
            "dimension or measure: add `primary_entity: <name>` naming what one row "
            "is, then pass the definition to `semantic plan --definitions-file`"
        )
    else:
        limitations.append(
            "the declaration states no grain, so there is no primary entity. "
            "Declare the grain and plan again"
        )

    used_names = {e["name"] for e in entities}
    for name, fields in columns.items():
        if fields.get("role") != "foreign_key" or name in grain:
            continue
        entity, joined = _foreign_entity(name, fields.get("references"), on_ref)
        if entity in used_names:
            fallback = _entity_name(name, model)
            notes.append(
                f"{name}: entity '{entity}' is already used in this semantic model, "
                f"so it is named '{fallback}' instead and does not join on '{entity}'"
            )
            entity, joined = fallback, None
        if entity in used_names:
            notes.append(f"{name}: no entity name free for it, so it is not derived")
            continue
        used_names.add(entity)
        entities.append(
            _with_meta({"name": entity, "type": "foreign", "expr": name}, fields)
        )
        carried.extend(_carried_column(name, fields))
        if not fields.get("references"):
            notes.append(
                f"{name}: a foreign key with no `references`, so entity '{entity}' "
                "is named from the column and joins nothing until another semantic "
                "model declares it"
            )
        elif joined:
            notes.append(f"{name}: entity '{entity}' joins {joined}")

    dimensions: list[dict[str, Any]] = []
    time_columns: list[str] = []
    for name, fields in columns.items():
        role = fields.get("role")
        if role == "dimension":
            dimensions.append(
                _with_meta(
                    _described({"name": name, "type": "categorical"}, fields), fields
                )
            )
            carried.extend(_carried_column(name, fields, described=True))
        elif role == "time":
            kind, truncated = column_types.get(name, (None, None))
            if kind == "other":
                limitations.append(
                    f"{name}: declared a time column, but its type is not a date or "
                    "a timestamp, so it is not a time dimension"
                )
                continue
            granularity = truncated or "day"
            if truncated:
                notes.append(f"{name}: granularity {truncated}, the SQL truncates it")
            elif kind == "timestamp":
                notes.append(
                    f"{name}: a timestamp, given day granularity; a finer one "
                    "(hour, minute) is the author's choice"
                )
            elif kind is None:
                notes.append(
                    f"{name}: its type could not be read, so it is given day "
                    "granularity, which a date or a timestamp both support"
                )
            dimensions.append(
                _with_meta(
                    _described(
                        {
                            "name": name,
                            "type": "time",
                            "type_params": {"time_granularity": granularity},
                        },
                        fields,
                    ),
                    fields,
                )
            )
            carried.extend(_carried_column(name, fields, described=True))
            time_columns.append(name)

    agg_time = time_columns[0] if time_columns else None
    if len(time_columns) > 1:
        notes.append(
            f"default time dimension is {agg_time}, the first time column declared; "
            f"{', '.join(time_columns[1:])} can be chosen per measure with "
            "`agg_time_dimension`"
        )

    measures: list[dict[str, Any]] = []
    for name, fields in columns.items():
        if fields.get("role") != "measure":
            continue
        measure = _measure(
            name,
            fields,
            agg_time,
            notes,
            limitations,
            groupings=[e["name"] for e in entities if e["type"] == "foreign"],
        )
        if measure is not None:
            measures.append(_with_meta(measure, fields, additivity=True))
            carried.extend(
                _carried_column(name, fields, described=True, additivity=True)
            )

    entity_columns = {e["expr"] for e in entities}
    if any((columns.get(c) or {}).get("description") for c in entity_columns):
        notes.append(
            "key column descriptions are not carried onto entities: MetricFlow's "
            "schema has no description field for an entity"
        )
        for column in entity_columns:
            reasons[f"columns.{column}.description"] = (
                "MetricFlow's schema has no description field for an entity"
            )

    unrolled = [
        name
        for name, fields in columns.items()
        if fields.get("role") in ("key", "attribute") and name not in grain
    ]
    if unrolled:
        notes.append(
            "not derived (role key or attribute, which a semantic model has no "
            f"element for): {', '.join(unrolled)}"
        )
        for column in unrolled:
            reasons[f"columns.{column}"] = (
                "role key or attribute, which a semantic model has no element for"
            )

    meta = model_metadata(declared)
    carried.extend(meta)
    derivation.carried, derivation.not_carried = account(
        declared,
        carried + (["description"] if declared.get("description") else []),
        reasons,
        default="not derived; the notes and limitations say why",
    )

    if not dimensions and not measures:
        derivation.status = "skipped"
        derivation.blocked = (
            "the declaration names no dimension, time or measure column, so there is "
            "nothing for a semantic model to carry"
        )
        return derivation

    body: dict[str, Any] = {"name": model}
    description = with_population(
        declared.get("description"), declared.get("population")
    )
    if description:
        body["description"] = description
    body["model"] = f"ref('{model}')"
    if agg_time:
        body["defaults"] = {"agg_time_dimension": agg_time}
    if entities:
        body["entities"] = entities
    if dimensions:
        body["dimensions"] = dimensions
    if measures:
        body["measures"] = measures
    if meta:
        body["config"] = {"meta": {DEX_META_KEY: meta}}
    derivation.body = body
    if primary is None:
        derivation.status = "failed"
        derivation.blocked = limitations[0]
    notes.append(
        "metrics are not derived: a measure is what a column holds, a metric is a "
        "business definition, and that stays the author's"
    )
    return derivation


def _measure(
    name: str,
    fields: Mapping[str, Any],
    agg_time: str | None,
    notes: list[str],
    limitations: list[str],
    *,
    groupings: list[str],
) -> dict[str, Any] | None:
    declared = fields.get("aggregation")
    additivity = fields.get("additivity")
    if not isinstance(declared, str) or not declared.strip():
        limitations.append(
            f"{name}: a measure with no declared aggregation, so it is not derived; "
            "declare `aggregation`"
        )
        return None
    agg = AGGREGATIONS.get(re.sub(r"[\s-]+", "_", declared.strip().lower()))
    if agg is None:
        limitations.append(
            f"{name}: aggregation '{declared}' has no MetricFlow equivalent dex maps "
            f"({', '.join(sorted(set(AGGREGATIONS.values())))}), so it is not derived"
        )
        return None
    if additivity == "non_additive" and agg in _SUMMING:
        limitations.append(
            f"{name}: declared non_additive with aggregation '{declared}', which adds "
            "it up; the two disagree, so it is not derived. Fix the declaration"
        )
        return None
    if agg_time is None:
        limitations.append(
            f"{name}: MetricFlow aggregates a measure over a time dimension, and the "
            "declaration names no time column, so it is not derived"
        )
        return None
    measure = _described({"name": name, "agg": agg, "expr": name}, fields)
    if additivity == "semi_additive":
        measure["non_additive_dimension"] = {
            "name": agg_time,
            "window_choice": WINDOW_CHOICE,
        }
        per_entity = (
            " When the value runs per entity (a balance per account, a running "
            "total per customer), add `window_groupings` naming it"
            + (f" (here perhaps: {', '.join(groupings)})" if groupings else "")
            + ", or the last day's rows are summed across every entity; the "
            "declaration does not say which, so none was guessed"
        )
        notes.append(
            f"{name}: semi_additive, so it is not added across {agg_time}; each "
            f"period reports its last value (window_choice: {WINDOW_CHOICE}). "
            "Confirm that is what the column means." + per_entity
        )
    return measure


def _with_meta(
    element: dict[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    additivity: bool = False,
) -> dict[str, Any]:
    """``element`` with the column's declared unit and NULL rule as metadata.

    Under ``config.meta.dex``, the key the model's own YAML uses, so a reader
    finds the same fact under the same name on either side. A ``non_additive``
    measure also says so there: MetricFlow states ``semi_additive`` natively
    (``non_additive_dimension``) and reads any other measure as one it may add up.
    """

    meta = column_metadata(fields)
    if additivity and (fields or {}).get("additivity") == "non_additive":
        meta["additivity"] = "non_additive"
    if meta:
        element["config"] = {"meta": {DEX_META_KEY: meta}}
    return element


def _carried_column(
    name: str,
    fields: Mapping[str, Any] | None,
    *,
    described: bool = False,
    additivity: bool = False,
) -> list[str]:
    """The declared fields of one derived column that its element carries."""

    carried = {"role", "references", "unit", "null_rule"}
    if described:
        carried.add("description")
    if additivity:
        carried |= {"aggregation", "additivity"}
    return [f"columns.{name}.{key}" for key in carried if (fields or {}).get(key)]


def _described(
    element: dict[str, Any], fields: Mapping[str, Any] | None
) -> dict[str, Any]:
    description = (fields or {}).get("description")
    if isinstance(description, str) and description.strip():
        element["description"] = description
    return element


def _entity_name(column: str, model: str) -> str:
    stem = re.sub(r"_?id$", "", column, flags=re.IGNORECASE)
    if stem:
        return stem.lower()
    return _LAYER_PREFIX.sub("", model).lower() or model.lower()


def _foreign_entity(
    column: str,
    references: Any,
    on_ref: Mapping[str, list[tuple[str, dict[str, Any]]]],
) -> tuple[str, str | None]:
    """The entity name a foreign key takes, and what it joins where known.

    The name the referenced model's own semantic model gives that column, when
    it declares one, because an entity joins by name and a fresh name would join
    nothing. Otherwise the column's own name less ``_id``.
    """

    if isinstance(references, str) and "." in references:
        parent, parent_column = references.rsplit(".", 1)
        for _path, entry in on_ref.get(parent, []):
            for entity in entry.get("entities") or []:
                if not isinstance(entity, dict) or not entity.get("name"):
                    continue
                expr = entity.get("expr") or entity["name"]
                if isinstance(expr, str) and expr.lower() == parent_column.lower():
                    return entity["name"], f"semantic model '{entry.get('name')}'"
    return _entity_name(column, column), None


# --- column types, from what is already free to read ---------------------------------


def column_types(
    sql: str,
    cache: Any,
    columns: Iterable[str],
    *,
    dialect: str,
    vars: Mapping[str, Any] | None,
) -> dict[str, tuple[str | None, str | None]]:
    """``(kind, truncated granularity)`` per column, from the SQL and the cache.

    A cast or a ``date_trunc`` in the model's own SELECT list says it outright;
    a column carried through unchanged takes the type its source's profile
    recorded. Nothing here opens a connection, and a column neither source
    speaks for is ``(None, None)``, which the caller names.
    """

    from . import drafting

    wanted = {c.lower(): c for c in columns}
    out: dict[str, tuple[str | None, str | None]] = dict.fromkeys(columns, (None, None))
    if not wanted:
        return out
    reading = drafting.read(sql, cache, dialect=dialect, vars=vars)
    for lowered, name in wanted.items():
        expression = reading.expressions.get(lowered)
        found = _from_expression(expression) if expression is not None else None
        if found is None and (origin := reading.origins.get(lowered)) is not None:
            profile = drafting._column(
                drafting.dataset_for(cache, origin.relation), origin.column
            )
            if profile is not None:
                found = (_kind(profile.data_type), None)
        if found is not None:
            out[name] = found
    return out


def _from_expression(expression: Any) -> tuple[str | None, str | None] | None:
    from sqlglot import expressions as exp

    node = expression.this if isinstance(expression, exp.Alias) else expression
    # MIN and MAX return their argument's type, so the first or last of a cast
    # date is still a date.
    while isinstance(node, (exp.Paren, exp.Min, exp.Max)):
        node = node.this
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return (_kind(node.to.sql()), None)
    if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)):
        unit = node.args.get("unit")
        text = (unit.name if unit is not None else "").lower()
        granularity = text if text in _GRANULARITIES else None
        kind = "date" if isinstance(node, exp.DateTrunc) else "timestamp"
        return (kind, granularity)
    return None


def _kind(data_type: str | None) -> str | None:
    if not data_type:
        return None
    lowered = data_type.lower()
    if "timestamp" in lowered or "datetime" in lowered:
        return "timestamp"
    if lowered.startswith("date"):
        return "date"
    return "other"


# --- placing, validating, and the parse ----------------------------------------------


def place(
    derivation: Derivation,
    sql_path: str,
    docs: Iterable[tuple[str, Any]],
    edits: list[PlanEdit],
) -> None:
    """Choose the file the semantic model goes in.

    The one file that already declares the project's semantic models, when there
    is exactly one and nothing else in this plan writes it as another kind; a
    file beside the mart otherwise. Never a file that would hold the same name
    twice: the collision check runs before this.
    """

    homes = {
        path
        for path, parsed in docs
        if isinstance(parsed, dict) and parsed.get("semantic_models")
    }
    other_kinds = {e.path for e in edits if e.kind is not EditKind.SEMANTIC_YML}
    if len(homes) == 1 and not homes & other_kinds:
        (derivation.path,) = homes
        return
    parent = PurePosixPath(sql_path).parent
    derivation.path = str(parent / f"{derivation.model}_semantic.yml")


def splice(derivation: Derivation, files: Mapping[str, str]) -> tuple[str, str]:
    """The derivation written into its file, every other byte preserved."""

    from . import semantic as semantic_mod

    body, path = _placed(derivation)
    (definition,) = semantic_mod.parse_definition_payload(
        [{"kind": "semantic_model", "path": path, "content": _dump(body)}]
    )
    spliced = semantic_mod._splice_entry(path, files.get(path, ""), definition)
    semantic_mod._verify_splice(path, spliced, [definition])
    return path, spliced


def check_collisions(
    derivation: Derivation,
    docs: Iterable[tuple[str, Any]],
    on_ref: Mapping[str, list[tuple[str, dict[str, Any]]]],
) -> None:
    """Skip a mart the layer already covers; fail a name another definition holds.

    An authored semantic model on the mart is the author's and is never
    overwritten or duplicated. A name collision is refused rather than renamed,
    because a renamed measure is a name nobody chose.
    """

    from . import semantic as semantic_mod

    existing = on_ref.get(derivation.model) or []
    if existing:
        names = ", ".join(f"'{e.get('name')}' in {p}" for p, e in existing)
        derivation.status = "skipped"
        derivation.blocked = (
            f"{derivation.model} already has a semantic model ({names}); an authored "
            "definition is not overwritten or duplicated"
        )
        return
    body = derivation.body or {}
    namespace = semantic_mod.collect_namespace(parsed for _p, parsed in docs)
    taken: list[str] = []
    name = body["name"]
    if name in namespace.semantic_models | namespace.metrics:
        taken.append(f"semantic model name '{name}'")
    taken.extend(
        f"measure '{m['name']}'"
        for m in body.get("measures") or []
        if m["name"] in namespace.measures
    )
    if taken:
        derivation.status = "failed"
        derivation.blocked = (
            "already declared elsewhere in the semantic layer: "
            + ", ".join(taken)
            + ". Measure names are global in MetricFlow, so rename the column or "
            "the existing definition; dex does not rename either"
        )


def validate_static(
    derivation: Derivation,
    view: DbtProjectView,
    payload_documents: list[Any],
) -> bool:
    """The two validation layers that need no subprocess, recorded either way."""

    from . import semantic as semantic_mod

    body, path = _placed(derivation)
    document = {"semantic_models": [body]}
    try:
        warnings = semantic_mod.validate_semantic_yaml(path, document)
    except EditValidationError as exc:
        derivation.validation["schema"] = {"result": "failed", "detail": str(exc)}
        return False
    if warnings:
        # The structural fallback: MetricFlow's schemas were not there to pass.
        derivation.validation["schema"] = {"result": "not_run", "detail": warnings[0]}
        return False
    derivation.validation["schema"] = {"result": "passed"}
    try:
        semantic_mod.check_references([*payload_documents, document], view)
    except EditValidationError as exc:
        derivation.validation["references"] = {"result": "failed", "detail": str(exc)}
        return False
    derivation.validation["references"] = {"result": "passed"}
    return True


def shadow_spine(view: DbtProjectView) -> list[PlanEdit]:
    """A time spine for the parse copy only, in a project that has none.

    dbt refuses to parse semantic models without a time spine, so a proposal
    for a project that has not adopted one could otherwise never be checked by
    dbt at all. This spine exists in the throwaway copy and nowhere else.
    """

    root = PurePosixPath(view.model_paths[0] if view.model_paths else "models")
    return [
        PlanEdit(
            path=str(root / f"{_SHADOW_SPINE}.sql"),
            kind=EditKind.MODEL_SQL,
            op=EditOp.UPSERT,
            new_content="select cast('2000-01-01' as date) as date_day\n",
        ),
        PlanEdit(
            path=str(root / f"{_SHADOW_SPINE}.yml"),
            kind=EditKind.SCHEMA_YML,
            op=EditOp.UPSERT,
            new_content=(
                "version: 2\n"
                "models:\n"
                f"  - name: {_SHADOW_SPINE}\n"
                "    time_spine:\n"
                "      standard_granularity_column: date_day\n"
                "    columns:\n"
                "      - name: date_day\n"
                "        granularity: day\n"
            ),
        ),
    ]


def validate_parse(
    derivations: list[Derivation],
    derived_edits: list[PlanEdit],
    base_edits: list[PlanEdit],
    *,
    project: Any,
    target: str | None,
    spine: list[PlanEdit],
) -> dict[str, Any] | None:
    """dbt's parse of a copy of the project with the derivations in it.

    Returns the parse result when it covered exactly ``base_edits`` plus the
    derivations and no throwaway spine, so the caller can stand it in for its
    own gate rather than parsing the same tree twice. A failure is pinned on the
    derivations only after the plan's own edits are shown to parse without
    them; otherwise the plan's own gate reports it, in its own words.
    """

    from .build import shadow_parse

    def record(result: str, detail: str | None = None) -> None:
        for derivation in derivations:
            entry: dict[str, Any] = {"result": result}
            if detail:
                entry["detail"] = detail
            if spine and result == "passed":
                entry["detail"] = (
                    "parsed with a throwaway time spine in the copy only; the "
                    "project has none"
                )
            derivation.validation["parse"] = entry

    combined = shadow_parse(
        project, [*base_edits, *spine, *derived_edits], target=target
    )
    if not combined["available"]:
        record("not_run", combined["reason"])
        return None
    if combined["success"]:
        record("passed")
        return None if spine else combined
    alone = shadow_parse(project, [*base_edits, *spine], target=target)
    if alone["available"] and alone["success"]:
        messages = combined["messages"] or ["dbt parse failed"]
        record("failed", "; ".join(messages[:3]))
    else:
        record(
            "not_run",
            "the plan's own edits do not parse, so the derivation could not be "
            "checked on its own",
        )
    return None


def _placed(derivation: Derivation) -> tuple[dict[str, Any], str]:
    if derivation.body is None or derivation.path is None:
        raise ValueError(f"the derivation for {derivation.model} has not been placed")
    return derivation.body, derivation.path


def _dump(body: Mapping[str, Any]) -> str:
    # The writer declarations render with, so a derived file reads like the
    # model YAML beside it.
    from .declarations import _dump as dump_yaml

    return dump_yaml(dict(body))


# --- the whole pass, behind the derivation hook (#927) -------------------------------


class MetricFlowDerivation:
    """The dbt vendor's :class:`~..semantic_source.SemanticDerivationSource`.

    MetricFlow's layer lives inside the transformation project, so its derived
    definitions are spliced into the plan's own edits (``edit_target`` is
    ``project``) and validated by dbt's parse of a throwaway copy, which the
    caller may reuse as its own gate. It lives here rather than on the dbt
    project adapter so the adapters never import the transform layer.

    ``annotate`` is ``unsupported``: a dbt declaration already renders into the
    model's own YAML through ``transform plan`` and ``semantic plan``, which is
    where MetricFlow reads it from.
    """

    vendor = "dbt"

    def __init__(
        self,
        view: DbtProjectView,
        edits: list[PlanEdit],
        files: Mapping[str, str],
        *,
        project: Any,
        target: str | None,
    ) -> None:
        self.view = view
        self.edits = edits
        self.files = files
        self.project = project
        self.target = target

    def derive_semantic(self, request: Any) -> Any:
        from ..semantic_source import DerivedDefinition, SemanticDerivationResult

        if request.mode != "derive":
            return SemanticDerivationResult(
                vendor=self.vendor,
                edit_target="project",
                edits=self.edits,
                definitions={
                    d.model: DerivedDefinition(
                        model=d.model,
                        status="unsupported",
                        reason=(
                            "a dbt declaration renders into the model's own YAML, "
                            "which is what MetricFlow reads"
                        ),
                    )
                    for d in request.declarations
                },
            )
        derivations, edits, proposal, warnings, parse = self._run(request)
        return SemanticDerivationResult(
            vendor=self.vendor,
            edit_target="project",
            edits=edits,
            definitions={d.model: _definition(d) for d in derivations},
            proposal=proposal,
            warnings=warnings,
            gate=parse,
        )

    def _run(
        self, request: Any
    ) -> tuple[list[Derivation], list[PlanEdit], dict[str, Any], list[str], Any]:
        from . import semantic as semantic_mod

        view, files, edits = self.view, self.files, self.edits
        docs = documents(files)
        on_ref = semantic_models_by_ref(docs)
        spine_missing = (
            semantic_mod.time_spine_warning(view, [parsed for _p, parsed in docs])
            is not None
        )
        layer = has_semantic_models(docs) and not spine_missing
        payload_documents = [
            yaml.safe_load(e.new_content)
            for e in edits
            if e.kind is EditKind.SEMANTIC_YML
            and e.op is EditOp.UPSERT
            and e.new_content
        ]

        derivations: list[Derivation] = []
        for declaration in request.declarations:
            model, sql_path = declaration.model, declaration.source_path
            derivation = derive(
                model,
                declaration.declared,
                column_types=declaration.column_types,
                on_ref=on_ref,
            )
            derivations.append(derivation)
            if derivation.body is None or derivation.status == "skipped":
                continue
            check_collisions(derivation, docs, on_ref)
            if derivation.status in ("skipped", "failed"):
                continue
            place(derivation, sql_path, docs, edits)
            try:
                splice(derivation, files)
            except EditValidationError as exc:
                derivation.status = "failed"
                derivation.blocked = str(exc)
                continue
            if not validate_static(derivation, view, payload_documents):
                derivation.status = (
                    "unvalidated"
                    if derivation.validation["schema"]["result"] == "not_run"
                    else "failed"
                )

        ready = [d for d in derivations if d.status == "proposed"]
        derived_edits = _spliced_edits(ready, files, edits)
        parse_result = None
        if ready:
            parse_result = validate_parse(
                ready,
                derived_edits,
                edits,
                project=self.project,
                target=self.target,
                spine=[] if layer else shadow_spine(view),
            )
            for derivation in ready:
                outcome = derivation.validation["parse"]["result"]
                if outcome == "failed":
                    derivation.status = "failed"
                elif outcome == "not_run":
                    derivation.status = "unvalidated"
                elif layer:
                    derivation.status = "included"

        included = [d for d in derivations if d.status == "included"]
        if included:
            edits = _with_derived(edits, _spliced_edits(included, files, edits))
        else:
            parse_result = None

        return (
            derivations,
            edits,
            _proposal(derivations, layer=layer, spine_missing=spine_missing),
            _warnings(derivations),
            parse_result,
        )


def _definition(derivation: Derivation) -> Any:
    from ..semantic_source import DerivedDefinition

    return DerivedDefinition(
        model=derivation.model,
        status=derivation.status,  # type: ignore[arg-type]
        name=None if derivation.body is None else derivation.body["name"],
        path=derivation.path,
        content=derivation.content,
        reason=derivation.blocked,
        notes=list(derivation.notes),
        limitations=list(derivation.limitations),
        validation=dict(derivation.validation),
    )


def _spliced_edits(
    derivations: list[Derivation], files: Mapping[str, str], edits: list[PlanEdit]
) -> list[PlanEdit]:
    """One whole-file edit per path, with every derivation for it spliced in."""

    text = dict(files)
    paths: list[str] = []
    for derivation in derivations:
        path, spliced = splice(derivation, text)
        text[path] = spliced
        if path not in paths:
            paths.append(path)
    return [
        PlanEdit(
            path=path,
            kind=EditKind.SEMANTIC_YML,
            op=EditOp.UPSERT,
            new_content=text[path],
        )
        for path in paths
    ]


def _with_derived(edits: list[PlanEdit], derived: list[PlanEdit]) -> list[PlanEdit]:
    """The plan's edits with each derived file replacing the plan's own edit to it,
    which the splice already started from."""

    by_path = {e.path: e for e in derived}
    out = [by_path.pop(e.path, e) for e in edits]
    return out + list(by_path.values())


def _proposal(
    derivations: list[Derivation], *, layer: bool, spine_missing: bool
) -> dict[str, Any]:
    proposed = [d for d in derivations if d.status == "proposed"]
    data: dict[str, Any] = {
        "vendor": "dbt",
        "mode": "included" if layer else "proposed",
        "time_spine": "missing" if spine_missing else "present",
        "definitions": [
            {"kind": "semantic_model", "path": d.path, "content": d.content}
            for d in proposed
        ],
        "models": {d.model: d.data() for d in derivations},
    }
    notes: list[str] = []
    if not layer:
        notes.append(
            "the project has no dbt semantic layer"
            + (
                " and no MetricFlow time spine (a day-grain date model with a "
                "`time_spine:` config), which dbt needs before it parses any "
                "semantic model"
                if spine_missing
                else ""
            )
            + "; nothing was added to the plan. Adopting one is the project "
            "owner's choice"
        )
    if proposed:
        data["next"] = (
            "save this object as a file and pass it to `semantic plan "
            "--definitions-file <file>` to add it as a reviewable plan"
            + (", once the project has a time spine" if spine_missing else "")
            + "; author the metrics on its measures there too"
        )
    data["notes"] = notes
    return data


def _warnings(derivations: list[Derivation]) -> list[str]:
    out: list[str] = []
    for derivation in derivations:
        if derivation.status == "included":
            out.append(
                f"semantic model '{derivation.model}' was derived from the "
                f"declaration and added to this plan in {derivation.path}; see "
                "data.semantic_proposal"
            )
        elif derivation.status in ("failed", "unvalidated"):
            reason = derivation.blocked or next(
                (
                    f"{layer_name}: {entry.get('detail', entry['result'])}"
                    for layer_name, entry in derivation.validation.items()
                    if entry["result"] != "passed"
                ),
                derivation.status,
            )
            out.append(
                f"semantic model for '{derivation.model}' was derived and not "
                f"offered ({derivation.status}): {reason}"
            )
    return out
