"""A SLayer model derived from a declared mart (#927).

The SLayer counterpart of the MetricFlow derivation (#499), behind the same
:class:`~..semantic_source.SemanticDerivationSource` hook. A declaration already
states most of what a SLayer model needs:

- ``grain``: ``primary_key: true`` on each grain column, composite included.
- ``foreign_key`` with ``references``: a ``many_to_one`` join to the SLayer
  model already sitting on the parent.
- Every role: a column. The first ``time`` column is the
  ``default_time_dimension``.
- ``measure`` with ``aggregation``: a saved measure ``<column>_<agg>`` with the
  formula ``<column>:<agg>``.
- ``description``: the model's and each column's ``description``.
- ``population``, ``assumptions``: ``meta.dex``, and the population as one
  sentence at the end of the description.
- ``unit``, ``null_rule``, ``additivity``: the column's ``meta.dex``.

Three things it never does, as the MetricFlow derivation never does them. It
never invents a metric beyond a declared measure, never guesses (a
``semi_additive`` measure has no exact SLayer equivalent, since ``last`` picks
one row rather than summing across entities on the last period, so it is named
and left to the author), and never renames: a model or measure name another
definition holds fails the derivation.

**A population is metadata, never a filter.** The model sits on the mart, whose
SQL already enforces it, so ``filters`` stays empty: a model-level filter would
apply the rule twice and diverge from the mart the day the two disagree.

**Where it goes.** One file per mart, ``models/<data_source>/<mart>.yaml``
under ``semantic.slayer.base_dir``. It is *included*, as a plan of its own
beside the mart's, only where that path is already listed under
``semantic.slayer.models``: that list is the layer's write surface, and adding
to it is a configuration change with its own confirmation. Otherwise it is
*proposed*, validated by the same checks, with the two commands that adopt it.

Nothing here opens SLayer storage, a datasource, or a warehouse connection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml

from ..semantic_declarations import (
    DEX_META_KEY,
    account,
    model_metadata,
    native_column_metadata,
    placed_table,
    sibling_prefix,
    with_population,
)

if TYPE_CHECKING:
    from ..semantic_source import (
        DerivedDefinition,
        SemanticDeclaration,
        SemanticDerivationRequest,
        SemanticDerivationResult,
    )
    from .project import SlayerSemanticLayer

#: Declared aggregation (lowered, spaces and dashes as underscores) to the
#: SLayer built-in it means. Strict, as MetricFlow's table is: anything else is
#: named, never mapped to a neighbour. ``sum_boolean`` and ``percentile`` are
#: absent because SLayer spells neither the way a declaration does.
AGGREGATIONS = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "distinct_count": "count_distinct",
    "min": "min",
    "max": "max",
    "average": "avg",
    "avg": "avg",
    "mean": "avg",
    "median": "median",
}

#: Aggregations that add values up, which ``non_additive`` contradicts.
_SUMMING = frozenset({"sum"})

#: Aggregations whose result is a number whatever the column; a column of
#: unknown type they read is typed ``DOUBLE``, which is what summing implies.
_NUMERIC = frozenset({"sum", "avg", "median"})

#: Roles that become a column. Every declared role does: a SLayer column is
#: groupable, filterable and joinable whatever it is.
_COLUMN_ROLES = frozenset(
    {"key", "foreign_key", "dimension", "time", "attribute", "measure"}
)

_MODEL_PATH = re.compile(r"^models/([^/]+)/[^/]+\.ya?ml$")


def derive(
    layer: SlayerSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """The SLayer model each declared mart states, validated, never written."""

    from ..semantic_source import SemanticDerivationResult

    if request.mode == "annotate":
        return annotate(layer, request)

    current = _parsed(request.documents)
    data_source, source_problem = _data_source(layer.models, current)
    prefix = _sibling_prefix(current, request.project_identifiers)

    built: list[tuple[DerivedDefinition, dict[str, Any] | None]] = []
    bodies: dict[str, dict[str, Any]] = {}
    for declaration in request.declarations:
        definition, body = _build(
            declaration,
            current,
            batch=bodies,
            data_source=data_source,
            source_problem=source_problem,
            prefix=prefix,
            documents=request.documents,
        )
        built.append((definition, body))
        if body is not None:
            bodies[definition.path or ""] = body

    _validate(layer, request.documents, built)
    included = [d for d, _b in built if d.status == "included"]
    edits = [_edit(d) for d in included]
    definitions = {d.model: d for d, _b in built}
    return SemanticDerivationResult(
        vendor="slayer",
        edit_target="semantic",
        edits=edits,
        definitions=definitions,
        proposal=_proposal(definitions),
        warnings=_warnings(definitions),
    )


# --- building one model --------------------------------------------------------------


def _build(
    declaration: SemanticDeclaration,
    current: Mapping[str, dict[str, Any]],
    *,
    batch: Mapping[str, dict[str, Any]],
    data_source: str | None,
    source_problem: str | None,
    prefix: str | None,
    documents: Mapping[str, str],
) -> tuple[DerivedDefinition, dict[str, Any] | None]:
    from ..semantic_source import DerivedDefinition
    from .loader import FORMAT_VERSION

    model = declaration.model
    declared = declaration.declared
    definition = DerivedDefinition(model=model, name=model)
    notes, limitations = definition.notes, definition.limitations
    carried: list[str] = []
    reasons: dict[str, str] = {}

    columns_declared: dict[str, dict[str, Any]] = {
        name: dict(fields)
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    identifier = declaration.identifier or model

    def finish(status: str, reason: str) -> tuple[DerivedDefinition, None]:
        definition.status = status  # type: ignore[assignment]
        definition.reason = reason
        definition.carried, definition.not_carried = account(
            declared, [], {}, default=reason
        )
        return definition, None

    existing = _existing(current, model, identifier)
    if existing is not None:
        path, entry, same_table = existing
        if same_table:
            return finish(
                "skipped",
                f"{model} already has a SLayer model ('{entry.get('name')}' in "
                f"{path}); an authored model is not overwritten or duplicated",
            )
        return finish(
            "failed",
            f"the SLayer model name '{model}' is already declared in {path} on "
            f"another table ({entry.get('sql_table')}); dex does not rename either",
        )
    if data_source is None:
        return finish("failed", source_problem or "no data_source")

    definition.path = f"models/{data_source}/{model}.yaml"
    if definition.path in documents:
        return finish(
            "failed",
            f"{definition.path} already holds another model; dex does not overwrite it",
        )

    sql_table, table_note, table_limit = _sql_table(declaration, identifier, prefix)
    if table_note:
        notes.append(table_note)
    if table_limit:
        limitations.append(table_limit)

    columns: list[dict[str, Any]] = []
    names = list(dict.fromkeys([*grain, *columns_declared]))
    time_columns: list[str] = []
    measures: list[dict[str, Any]] = []
    for name in names:
        fields = columns_declared.get(name, {})
        role = fields.get("role")
        if name not in grain and role not in _COLUMN_ROLES:
            reasons[f"columns.{name}"] = (
                "a column with no role, so the declaration does not say it belongs "
                "in the semantic layer"
            )
            continue
        column: dict[str, Any] = {"name": name}
        slayer_type, type_note = _column_type(name, fields, declaration)
        if slayer_type:
            column["type"] = slayer_type
        if type_note:
            notes.append(type_note)
        if name in grain:
            column["primary_key"] = True
        if isinstance(fields.get("description"), str) and fields["description"]:
            column["description"] = fields["description"]
        meta = native_column_metadata(fields)
        if meta:
            column["meta"] = {DEX_META_KEY: meta}
        columns.append(column)
        carried.extend(
            f"columns.{name}.{key}"
            for key in ("role", "description", "unit", "null_rule", "additivity")
            if fields.get(key)
        )
        if role == "time":
            if slayer_type in ("DATE", "TIMESTAMP"):
                time_columns.append(name)
            else:
                limitations.append(
                    f"{name}: declared a time column, but its type is not known to "
                    "be a date or a timestamp, so it is not the default time "
                    "dimension; set its `type` and `default_time_dimension`"
                )
        if role == "measure":
            measure = _measure(name, fields, notes, limitations, reasons)
            if measure is not None:
                measures.append(measure)
                carried.append(f"columns.{name}.aggregation")
    if grain:
        carried.append("grain")
    else:
        limitations.append(
            "the declaration states no grain, so no column is the primary key"
        )

    joins = _joins(declaration, current, batch, notes, reasons)
    carried.extend(f"columns.{j['_column']}.references" for j in joins)
    for join in joins:
        join.pop("_column")

    taken = _measure_collisions(columns, measures)
    if taken:
        return finish(
            "failed",
            "saved measure names collide with this model's own columns or measures "
            f"({', '.join(taken)}); SLayer keeps both in one namespace, and dex "
            "does not rename either",
        )

    if len(time_columns) > 1:
        notes.append(
            f"default time dimension is {time_columns[0]}, the first time column "
            f"declared; {', '.join(time_columns[1:])} can be grouped by as columns"
        )

    body: dict[str, Any] = {
        "version": FORMAT_VERSION,
        "name": model,
        "sql_table": sql_table,
        "data_source": data_source,
    }
    description = with_population(
        declared.get("description"), declared.get("population")
    )
    if description:
        body["description"] = description
    if time_columns:
        body["default_time_dimension"] = time_columns[0]
    body["columns"] = columns
    if measures:
        body["measures"] = measures
    if joins:
        body["joins"] = joins
    meta = model_metadata(declared)
    if meta:
        body["meta"] = {DEX_META_KEY: meta}
    carried.extend(meta)
    if declared.get("description"):
        carried.append("description")

    notes.append(
        "metrics beyond declared measures are not derived: a saved measure is "
        "what a column's declared aggregation states, anything more is a business "
        "definition, and that stays the author's"
    )
    definition.carried, definition.not_carried = account(
        declared,
        carried,
        reasons,
        default="not derived; the notes and limitations say why",
    )
    definition.content = _dump(body)
    return definition, body


def _measure(
    name: str,
    fields: Mapping[str, Any],
    notes: list[str],
    limitations: list[str],
    reasons: dict[str, str],
) -> dict[str, Any] | None:
    declared = fields.get("aggregation")
    additivity = fields.get("additivity")
    why: str | None = None
    agg: str | None = None
    if not isinstance(declared, str) or not declared.strip():
        why = "a measure with no declared aggregation; declare `aggregation`"
    else:
        agg = AGGREGATIONS.get(re.sub(r"[\s-]+", "_", declared.strip().lower()))
        if agg is None:
            why = (
                f"aggregation '{declared}' has no SLayer built-in dex maps "
                f"({', '.join(sorted(set(AGGREGATIONS.values())))})"
            )
        elif additivity == "non_additive" and agg in _SUMMING:
            why = (
                f"declared non_additive with aggregation '{declared}', which adds "
                "it up; the two disagree. Fix the declaration"
            )
        elif additivity == "semi_additive":
            why = (
                "semi_additive, and SLayer has no exact equivalent of a measure "
                "that is not added across time: `last` reports one row's value on "
                "the last period rather than the total across entities on it. "
                f"`{name}:last` is right only where the value runs for one entity; "
                "that is the author's call, so no measure was saved"
            )
    if why is not None:
        limitations.append(f"{name}: {why}, so no saved measure is derived")
        reasons[f"columns.{name}.aggregation"] = why
        return None
    measure: dict[str, Any] = {"name": f"{name}_{agg}", "formula": f"{name}:{agg}"}
    if isinstance(fields.get("description"), str) and fields["description"]:
        measure["description"] = fields["description"]
    return measure


def _measure_collisions(
    columns: list[dict[str, Any]], measures: list[dict[str, Any]]
) -> list[str]:
    seen = {c["name"] for c in columns}
    taken: list[str] = []
    for measure in measures:
        if measure["name"] in seen:
            taken.append(measure["name"])
        seen.add(measure["name"])
    return taken


def _column_type(
    name: str, fields: Mapping[str, Any], declaration: SemanticDeclaration
) -> tuple[str | None, str | None]:
    """The SLayer ``type`` for a column, and a note when it was inferred."""

    raw = declaration.data_types.get(name)
    mapped = _slayer_type(raw) if raw else None
    if mapped:
        return mapped, None
    kind = (declaration.column_types.get(name) or (None, None))[0]
    if kind == "date":
        return "DATE", None
    if kind == "timestamp":
        return "TIMESTAMP", None
    agg = fields.get("aggregation")
    if (
        fields.get("role") == "measure"
        and isinstance(agg, str)
        and AGGREGATIONS.get(re.sub(r"[\s-]+", "_", agg.strip().lower())) in _NUMERIC
    ):
        return "DOUBLE", (
            f"{name}: its type could not be read, so it is typed DOUBLE, which "
            f"aggregation '{agg}' implies"
        )
    if fields.get("role") in ("time", "measure"):
        return None, (
            f"{name}: its type could not be read from the SQL or the cache, so no "
            "`type` is written; SLayer reads an untyped column as TEXT"
        )
    return None, None


def _slayer_type(raw: str) -> str | None:
    lowered = raw.strip().lower()
    if "timestamp" in lowered or "datetime" in lowered:
        return "TIMESTAMP"
    if lowered.startswith("date"):
        return "DATE"
    if lowered.startswith("bool"):
        return "BOOLEAN"
    if re.match(
        r"^(u?(tiny|small|big|huge)?int(eger)?\d*|int\d+|long|short)\b", lowered
    ):
        return "INT"
    if re.match(r"^(number|numeric|decimal)\s*\(\s*\d+\s*,\s*0\s*\)", lowered):
        return "INT"
    if re.match(r"^(double|float|real|number|numeric|decimal|bignumeric)", lowered):
        return "DOUBLE"
    if re.match(r"^(varchar|char|text|string|nvarchar|character)", lowered):
        return "TEXT"
    return None


# --- what the layer already holds ----------------------------------------------------


def _parsed(documents: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path, text in documents.items():
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            out[path] = parsed
    return out


def _data_source(
    configured: list[str], current: Mapping[str, dict[str, Any]]
) -> tuple[str | None, str | None]:
    """The one data source this layer's models use, or why there is none."""

    found = {m.group(1) for path in configured if (m := _MODEL_PATH.match(path))}
    found |= {
        entry["data_source"]
        for entry in current.values()
        if isinstance(entry.get("data_source"), str) and entry["data_source"]
    }
    if len(found) == 1:
        return next(iter(found)), None
    if not found:
        return None, (
            "no SLayer model is configured, so there is no data source to place a "
            "derived model under (models/<data_source>/<name>.yaml); configure the "
            "layer's first model under semantic.slayer.models"
        )
    return None, (
        "the configured SLayer models use more than one data source "
        f"({', '.join(sorted(found))}), and the declaration does not say which "
        "this mart's table is read through, so none was guessed"
    )


def _sibling_prefix(
    current: Mapping[str, dict[str, Any]], identifiers: Mapping[str, str]
) -> str | None:
    """The ``database.schema`` every SLayer model on this project's models shares."""

    return sibling_prefix(
        (entry.get("sql_table") for entry in current.values()), identifiers.values()
    )


def _sql_table(
    declaration: SemanticDeclaration, identifier: str, prefix: str | None
) -> tuple[str, str | None, str | None]:
    """The table, a note saying where it came from, and a limitation if partial."""

    table, source = placed_table(identifier, declaration.relation, prefix)
    if source == "siblings":
        return (
            table,
            f"sql_table is {table}: the database and schema the "
            "SLayer models on this project's other models already read",
            None,
        )
    if source == "manifest":
        return (
            table,
            f"sql_table is {table}, the relation the compiled dbt "
            "manifest names, which is whatever target last compiled the project "
            "(often dev). Point it at the relation SLayer's datasource reads",
            None,
        )
    return (
        table,
        None,
        f"sql_table is the bare table name '{identifier}': no SLayer model sits on "
        "another of this project's models and there is no compiled manifest, so "
        "SLayer completes it from its datasource and dex's catalog carries no "
        "qualified relation for it. Qualify it before relying on it",
    )


def _existing(
    current: Mapping[str, dict[str, Any]], model: str, identifier: str
) -> tuple[str, dict[str, Any], bool] | None:
    """A configured model already on this mart's table or holding its name."""

    for path, entry in sorted(current.items()):
        table = entry.get("sql_table")
        last = table.rpartition(".")[2].strip('"`') if isinstance(table, str) else ""
        if last.lower() == identifier.lower():
            return path, entry, True
    for path, entry in sorted(current.items()):
        if entry.get("name") == model:
            return path, entry, False
    return None


def _joins(
    declaration: SemanticDeclaration,
    current: Mapping[str, dict[str, Any]],
    batch: Mapping[str, dict[str, Any]],
    notes: list[str],
    reasons: dict[str, str],
) -> list[dict[str, Any]]:
    candidates = [*current.values(), *batch.values()]
    joins: list[dict[str, Any]] = []
    for reference in declaration.references:
        target = None
        wanted = (reference.parent_identifier or reference.parent_model).lower()
        for entry in candidates:
            table = entry.get("sql_table")
            last = (
                table.rpartition(".")[2].strip('"`').lower()
                if isinstance(table, str)
                else ""
            )
            if last == wanted or entry.get("name") == reference.parent_model:
                target = entry
                break
        if target is None or not reference.parent_column:
            why = (
                f"no SLayer model sits on {reference.parent_model}, so there is "
                "no model to join to; the join is derived once one does"
                if target is None
                else f"references names no column of {reference.parent_model}"
            )
            notes.append(f"{reference.column}: {why}")
            reasons[f"columns.{reference.column}.references"] = why
            continue
        joins.append(
            {
                "target_model": target["name"],
                "join_pairs": [[reference.column, reference.parent_column]],
                "cardinality": "many_to_one",
                "_column": reference.column,
            }
        )
        notes.append(
            f"{reference.column}: joins SLayer model '{target['name']}' on "
            f"{reference.parent_column} (many_to_one, the declared foreign key)"
        )
    return joins


# --- validation ----------------------------------------------------------------------


def _validate(
    layer: SlayerSemanticLayer,
    documents: Mapping[str, str],
    built: list[tuple[DerivedDefinition, dict[str, Any] | None]],
) -> None:
    """Hold every built model to authoring's own checks, then the catalog.

    The whole prospective set is validated at once (the configured models plus
    every derived one), so a join between two marts derived together resolves.
    A model is included only where its path is on the write surface and a plan
    writing it would pass; otherwise it is proposed on the same checks.
    """

    from . import catalog as catalog_mod
    from .authoring import refusals_for, validate_plan

    ready = [d for d, body in built if body is not None and d.content is not None]
    if not ready:
        return
    prospective = dict(documents)
    for definition in ready:
        prospective[definition.path or ""] = definition.content or ""
    edited = {d.path or "" for d in ready}
    validated, refusals, skipped_formulas = refusals_for(prospective, edited)
    by_file: dict[str, list[str]] = {}
    for diagnostic in refusals:
        by_file.setdefault(diagnostic.file, []).append(diagnostic.render())

    loaded = [m for m in validated.models if m.file in edited]
    catalog = catalog_mod.semantic_catalog(loaded, connector=layer.connector)
    catalogued = {m.name for m in catalog.semantic_models}
    configured = set(layer.models)

    for definition in ready:
        problems = by_file.get(definition.path or "")
        if problems:
            definition.status = "failed"
            definition.reason = "SLayer refuses it: " + "; ".join(problems)
            definition.validation["slayer"] = {
                "result": "failed",
                "detail": "; ".join(problems),
            }
            continue
        definition.validation["slayer"] = (
            {"result": "passed"}
            if not skipped_formulas
            else {
                "result": "passed",
                "detail": "formulas not parsed: the [sql] extra is not installed",
            }
        )
        if definition.name not in catalogued:
            definition.status = "failed"
            definition.reason = "the derived model did not read back as a catalog model"
            definition.validation["catalog"] = {"result": "failed"}
            continue
        definition.validation["catalog"] = {"result": "passed"}
        if definition.path not in configured:
            definition.status = "proposed"
            definition.next = _adopt(layer, definition)
            continue
        definition.status = "included"

    included = [d for d in ready if d.status == "included"]
    if not included:
        return
    try:
        validate_plan(layer, [_edit(d) for d in included], "define")
    except ValueError as exc:
        for definition in included:
            definition.status = "failed"
            definition.reason = str(exc)
            definition.validation["plan"] = {"result": "failed", "detail": str(exc)}
        return
    for definition in included:
        definition.validation["plan"] = {"result": "passed"}


def _adopt(layer: SlayerSemanticLayer, definition: DerivedDefinition) -> str:
    listed = ", ".join([*layer.models, definition.path or ""])
    return (
        f"{definition.path} is not under semantic.slayer.models, the files dex "
        f"may write: run `dex config set semantic.slayer.models '[{listed}]'`, "
        "then pass data.semantic_proposal.definitions as the edits file to "
        "`semantic slayer define`, or re-run this plan to include it"
    )


def _edit(definition: DerivedDefinition) -> Any:
    from ..transform.plans import EditKind, PlanEdit

    return PlanEdit(
        path=definition.path or "",
        kind=EditKind.SEMANTIC_DOCUMENT,
        new_content=definition.content,
    )


def _dump(body: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(body), sort_keys=False, allow_unicode=True)


# --- the envelope --------------------------------------------------------------------


def _proposal(definitions: Mapping[str, DerivedDefinition]) -> dict[str, Any]:
    proposed = [d for d in definitions.values() if d.status == "proposed"]
    included = [d for d in definitions.values() if d.status == "included"]
    data: dict[str, Any] = {
        "vendor": "slayer",
        "mode": "included" if included else "proposed",
        "definitions": {
            "edits": [
                {"path": d.path, "kind": "semantic_document", "content": d.content}
                for d in proposed
            ]
        },
        "models": {name: _model_data(d) for name, d in definitions.items()},
    }
    notes: list[str] = []
    if proposed:
        data["next"] = (
            proposed[0].next
            if len(proposed) == 1
            else (
                "add each proposed path to semantic.slayer.models with `dex config "
                "set`, then pass data.semantic_proposal.definitions as the edits file "
                "to `semantic slayer define`, or re-run this plan to include them"
            )
        )
    if included:
        notes.append(
            "included models are stored as a plan of their own on the SLayer "
            "layer (data.semantic_proposal.plan_id), beside the dbt plan; apply both"
        )
    data["notes"] = notes
    return data


def _model_data(definition: DerivedDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {"status": definition.status}
    if definition.content is not None:
        out["slayer_model"] = definition.name
        out["path"] = definition.path
        out["content"] = definition.content
    if definition.reason:
        out["reason"] = definition.reason
    if definition.validation:
        out["validation"] = definition.validation
    if definition.next:
        out["next"] = definition.next
    out["notes"] = definition.notes
    out["limitations"] = definition.limitations
    out["carried"] = definition.carried
    out["not_carried"] = definition.not_carried
    return out


def _warnings(definitions: Mapping[str, DerivedDefinition]) -> list[str]:
    out: list[str] = []
    for definition in definitions.values():
        if definition.status == "included":
            out.append(
                f"SLayer model '{definition.model}' was derived from the "
                f"declaration for {definition.path}; see data.semantic_proposal"
            )
        elif definition.status in ("failed", "unvalidated"):
            out.append(
                f"SLayer model for '{definition.model}' was derived and not "
                f"offered ({definition.status}): {definition.reason}"
            )
    return out


# --- annotate: declarations onto a model the layer or payload defines ----------------


def annotate(
    layer: SlayerSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """Render each declaration into the SLayer model it names.

    The declaration wins field by field and nothing else in the file moves: a
    model with nothing to change keeps its bytes. A declaration naming a model no
    document defines is refused by the caller before this runs; here it is
    ``failed`` for completeness.
    """

    from ..semantic_source import DerivedDefinition, SemanticDerivationResult
    from ..transform.plans import EditKind, PlanEdit

    current = _parsed(request.documents)
    by_name = {
        entry.get("name"): path
        for path, entry in current.items()
        if isinstance(entry.get("name"), str)
    }
    definitions: dict[str, DerivedDefinition] = {}
    edits: list[Any] = []
    for declaration in request.declarations:
        definition = DerivedDefinition(model=declaration.model, name=declaration.model)
        definitions[declaration.model] = definition
        path = by_name.get(declaration.model)
        if path is None:
            definition.status = "failed"
            definition.reason = (
                f"no configured SLayer model is named '{declaration.model}'"
            )
            continue
        definition.path = path
        entry = current[path]
        changed, carried, reasons = _annotated(entry, declaration, current)
        definition.carried, definition.not_carried = account(
            declaration.declared,
            carried,
            reasons,
            default="the SLayer model has no column of that name",
        )
        definition.status = "included"
        if changed:
            definition.content = _dump(entry)
            edits.append(
                PlanEdit(
                    path=path,
                    kind=EditKind.SEMANTIC_DOCUMENT,
                    new_content=definition.content,
                )
            )
        else:
            definition.content = request.documents[path]
            definition.notes.append("the model already states every declared fact")
    return SemanticDerivationResult(
        vendor="slayer",
        edit_target="semantic",
        edits=edits,
        definitions=definitions,
        proposal=None,
        warnings=[],
    )


def _annotated(
    entry: dict[str, Any],
    declaration: SemanticDeclaration,
    current: Mapping[str, dict[str, Any]],
) -> tuple[bool, list[str], dict[str, str]]:
    """Apply the declared facts to ``entry`` in place; whether anything moved."""

    declared = declaration.declared
    before = yaml.safe_dump(entry, sort_keys=False)
    carried: list[str] = []
    reasons: dict[str, str] = {}

    description = with_population(
        declared.get("description") or _without_population(entry.get("description")),
        declared.get("population"),
    )
    if description and (declared.get("description") or declared.get("population")):
        entry["description"] = description
        if declared.get("description"):
            carried.append("description")
    meta = model_metadata(declared)
    if meta:
        entry.setdefault("meta", {})
        if not isinstance(entry["meta"], dict):
            entry["meta"] = {}
        dex = entry["meta"].setdefault(DEX_META_KEY, {})
        dex.update(meta)
        carried.extend(meta)

    columns = {
        c.get("name"): c for c in entry.get("columns") or [] if isinstance(c, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    if grain:
        missing = [g for g in grain if g not in columns]
        if missing:
            reasons["grain"] = (
                f"the SLayer model has no column {', '.join(missing)}, so the grain "
                "was not written"
            )
        else:
            for name, column in columns.items():
                if name in grain:
                    column["primary_key"] = True
                elif column.get("primary_key"):
                    column.pop("primary_key")
            carried.append("grain")

    measures = entry.setdefault("measures", []) if isinstance(entry, dict) else []
    measure_names = {m.get("name") for m in measures if isinstance(m, dict)}
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, dict):
            continue
        column = columns.get(name)
        if column is None:
            reasons[f"columns.{name}"] = "the SLayer model has no column of that name"
            continue
        if isinstance(fields.get("description"), str) and fields["description"]:
            column["description"] = fields["description"]
            carried.append(f"columns.{name}.description")
        column_meta = native_column_metadata(fields)
        if column_meta:
            column.setdefault("meta", {})
            if not isinstance(column["meta"], dict):
                column["meta"] = {}
            column["meta"].setdefault(DEX_META_KEY, {}).update(column_meta)
        carried.extend(
            f"columns.{name}.{key}"
            for key in ("role", "unit", "null_rule", "additivity")
            if fields.get(key)
        )
        if fields.get("role") == "measure":
            notes: list[str] = []
            limitations: list[str] = []
            measure = _measure(name, fields, notes, limitations, reasons)
            if measure is not None:
                if measure["name"] in columns:
                    reasons[f"columns.{name}.aggregation"] = (
                        f"saved measure name '{measure['name']}' is a column of "
                        "this model"
                    )
                else:
                    if measure["name"] not in measure_names:
                        measures.append(measure)
                        measure_names.add(measure["name"])
                    carried.append(f"columns.{name}.aggregation")
        if fields.get("role") == "foreign_key" and fields.get("references"):
            why = _annotate_join(entry, name, fields["references"], current)
            if why is None:
                carried.append(f"columns.{name}.references")
            else:
                reasons[f"columns.{name}.references"] = why
    if not measures:
        entry.pop("measures", None)
    return yaml.safe_dump(entry, sort_keys=False) != before, carried, reasons


def _annotate_join(
    entry: dict[str, Any],
    column: str,
    references: str,
    current: Mapping[str, dict[str, Any]],
) -> str | None:
    """Add the declared join, or say why not. An authored join to the same
    model is the author's and is left as it is."""

    parent, _, parent_column = references.rpartition(".")
    names = {e.get("name") for e in current.values()}
    if not parent or parent not in names:
        return f"references '{references}' names no configured SLayer model"
    joins = entry.setdefault("joins", [])
    if any(isinstance(j, dict) and j.get("target_model") == parent for j in joins):
        return None
    joins.append(
        {
            "target_model": parent,
            "join_pairs": [[column, parent_column]],
            "cardinality": "many_to_one",
        }
    )
    return None


def _without_population(description: Any) -> str | None:
    """An authored description less a population sentence dex wrote earlier."""

    if not isinstance(description, str):
        return None
    head, sep, _tail = description.partition(" Population: ")
    if sep:
        return head
    return None if description.startswith("Population: ") else description
