"""An Ossie dataset derived from a declared mart (#500).

The Ossie counterpart of the MetricFlow (#499), SLayer (#927) and Cube (#911)
derivations, behind the same :class:`~..semantic_source.SemanticDerivationSource`
hook. A declaration states most of what a dataset needs, and Ossie has a native
place for most of it:

- each declared column: a field of the same name, ``expression: {sql: <column>}``,
  typed with Ossie's ``datatype`` where the SQL or the cache knows the type.
- ``time`` role: ``dimension: {is_time: true}``.
- ``grain``: ``primary_key``, every grain field, composite included. The grain
  is the dataset's row identity; ``unique_keys`` stays for further keys.
- ``foreign_key`` with ``references``: a relationship to the dataset in the same
  document whose field resolves to the referenced column.
- ``measure`` with ``aggregation``: a dataset-scoped metric ``<column>_<agg>``.
- ``description``: the native ``description`` of the dataset and each field.
- ``population``, ``assumptions``: the dataset's ``ai_context``, as
  ``instructions`` prose (the population sentence) plus a structured ``dex`` key;
  the population sentence also closes the description.
- ``unit``, ``null_rule``, ``additivity``: the field's ``ai_context.dex``.

``custom_extensions`` is never used: its ``data`` is an opaque string, and a
declaration encoded into a string inside YAML cannot be reviewed in a diff.

**A population is metadata, never a filter.** Ossie has no dataset or metric
filter, and the dataset sits on the mart, whose SQL already enforces it.

**Where it goes.** Relationships live inside one model, so a derived dataset is
spliced into the configured document holding the datasets its foreign keys
reference, or, with no foreign key and exactly one configured document, into
that one. It is appended as a list item, so every other byte of the document is
kept, and *included* as a plan of its own beside the mart's. Where the parents
span documents, or no document can hold it, it is derived as a document of its
own. If that path is already configured, it is included as a plan even before
the file exists; otherwise it is proposed with the configuration change that
adopts it. Both paths pass the same validation checks.

**Annotate.** ``semantic ossie plan`` with ``declarations`` renders each one
into the dataset it names, field by field; only that dataset's list item is
re-serialized, and a relationship it declares is appended to the model's list.

Nothing here opens a warehouse connection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from ..semantic_declarations import (
    DEX_META_KEY,
    account,
    model_metadata,
    native_column_metadata,
    placed_table,
    population_sentence,
    sibling_prefix,
    table_name,
    with_population,
)
from ..yaml_items import append_item, dump_items, locate
from . import catalog as catalog_mod
from .loader import validate_document_contents

if TYPE_CHECKING:
    from ..semantic_source import (
        DerivedDefinition,
        SemanticDeclaration,
        SemanticDerivationRequest,
        SemanticDerivationResult,
    )
    from .project import OssieSemanticLayer

#: Declared aggregation (lowered, spaces and dashes as underscores) to the
#: OSSIE_SQL_2026 aggregate it means, and the suffix its metric is named with.
#: Strict, as the other derivations' tables are: anything else is named, never
#: mapped to a neighbour. ``median`` is absent: it is no portable aggregate.
AGGREGATIONS: dict[str, tuple[str, str]] = {
    "sum": ("SUM({})", "sum"),
    "count": ("COUNT({})", "count"),
    "count_distinct": ("COUNT(DISTINCT {})", "count_distinct"),
    "distinct_count": ("COUNT(DISTINCT {})", "count_distinct"),
    "min": ("MIN({})", "min"),
    "max": ("MAX({})", "max"),
    "average": ("AVG({})", "avg"),
    "avg": ("AVG({})", "avg"),
    "mean": ("AVG({})", "avg"),
}

#: Aggregations that add values up, which ``non_additive`` contradicts.
_SUMMING = frozenset({"sum"})

#: Roles that make a column a field. A ``measure`` is a field too: a dataset
#: metric aggregates the dataset's own fields, never its source's columns.
_FIELD_ROLES = frozenset(
    {"key", "foreign_key", "dimension", "time", "attribute", "measure"}
)

#: Where a proposed document goes when the configured files share no directory.
DEFAULT_DIR = "semantic"

#: The version every derived document declares: the pinned schema's constant.
VERSION = "0.2.0.dev0"


def derive(
    layer: OssieSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """The dataset each declared mart states, validated, never written."""

    from ..semantic_source import SemanticDerivationResult

    if request.mode == "annotate":
        return annotate(layer, request)

    texts = dict(request.documents)
    prefix = sibling_prefix(
        (
            dataset.get("source")
            for _p, data in _documents(texts)
            for dataset in data.get("datasets") or []
            if isinstance(dataset, dict)
        ),
        request.project_identifiers.values(),
    )
    directory = _directory(layer)

    built: list[DerivedDefinition] = []
    changed: list[str] = []
    for declaration in request.declarations:
        definition = _build(
            declaration,
            texts,
            layer=layer,
            prefix=prefix,
            directory=directory,
            connector=request.connector or layer.connector,
        )
        built.append(definition)
        if definition.status == "included" and definition.path not in changed:
            changed.append(definition.path or "")

    for definition in built:
        if definition.status == "included":
            definition.content = texts[definition.path or ""]

    _validate(layer, request, texts, built, changed)
    definitions = {d.model: d for d in built}
    included = [
        p for p in changed if any(d.path == p and d.status == "included" for d in built)
    ]
    return SemanticDerivationResult(
        vendor="ossie",
        edit_target="semantic",
        edits=[_edit(path, texts[path]) for path in included],
        definitions=definitions,
        proposal=_proposal(definitions),
        warnings=_warnings(definitions),
    )


# --- building one dataset ------------------------------------------------------------


def _build(
    declaration: SemanticDeclaration,
    texts: dict[str, str],
    *,
    layer: OssieSemanticLayer,
    prefix: str | None,
    directory: str,
    connector: str | None,
) -> DerivedDefinition:
    """Build one dataset and place it: spliced into ``texts`` when included."""

    from ..semantic_source import DerivedDefinition

    model = declaration.model
    declared = declaration.declared
    definition = DerivedDefinition(model=model, name=model)
    notes, limitations = definition.notes, definition.limitations
    carried: list[str] = []
    reasons: dict[str, str] = {}
    identifier = declaration.identifier or model

    def finish(status: str, reason: str) -> DerivedDefinition:
        definition.status = status  # type: ignore[assignment]
        definition.reason = reason
        definition.carried, definition.not_carried = account(
            declared, [], {}, default=reason
        )
        return definition

    documents = _documents(texts)
    for path, data in documents:
        for dataset in data.get("datasets") or []:
            if isinstance(dataset, dict) and table_name(dataset.get("source")) == (
                identifier.lower()
            ):
                return finish(
                    "skipped",
                    f"{model} already has a dataset ('{dataset.get('name')}' in "
                    f"{path}); an authored dataset is not overwritten or duplicated",
                )

    # Where it goes: the document holding the parents its foreign keys name.
    parents: dict[str, tuple[str, dict[str, Any]]] = {}
    for reference in declaration.references:
        wanted = (reference.parent_identifier or reference.parent_model).lower()
        for path, data in documents:
            for dataset in data.get("datasets") or []:
                if not isinstance(dataset, dict):
                    continue
                if (
                    table_name(dataset.get("source")) == wanted
                    or dataset.get("name") == reference.parent_model
                ):
                    parents.setdefault(reference.column, (path, dataset))
    homes = sorted({path for path, _d in parents.values()})
    target: str | None = None
    if len(homes) == 1:
        target = homes[0]
    elif (
        not homes
        and len(documents) == 1
        and len(layer.files) == 1
        and not declaration.references
    ):
        target = documents[0][0]
    elif len(homes) > 1:
        notes.append(
            "its foreign keys reference datasets in different documents "
            f"({', '.join(homes)}), and a relationship joins datasets of one model "
            "only, so it is derived as a document of its own with no relationship"
        )
    elif not homes and documents:
        notes.append(
            "no configured document holds a dataset its foreign keys reference, so "
            "it is derived as a document of its own"
        )
    elif not documents:
        notes.append(
            "no configured Ossie document could be read to hold it, so it is "
            "derived as a document of its own"
        )

    if target is not None:
        holder = dict(documents)[target]
        if any(
            isinstance(d, dict) and d.get("name") == model
            for d in holder.get("datasets") or []
        ):
            return finish(
                "failed",
                f"the dataset name '{model}' is already declared in {target}; dex "
                "does not rename a dataset",
            )

    source, placed = placed_table(identifier, declaration.relation, prefix)
    if placed == "siblings":
        notes.append(
            f"source is {source}: the database and schema the datasets on this "
            "project's other models already read"
        )
    elif placed == "manifest":
        notes.append(
            f"source is {source}, the relation the compiled dbt manifest names, "
            "which is whatever target last compiled the project (often dev). Point "
            "it at the relation the layer's consumers read"
        )
    else:
        limitations.append(
            f"source is the bare table name '{identifier}': no dataset sits on "
            "another of this project's models and there is no compiled manifest, "
            "so the source is not one relation dex can address, and none of its "
            "fields links a physical column. Qualify it before relying on it"
        )

    columns_declared: dict[str, dict[str, Any]] = {
        name: dict(fields)
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    fields: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for name in dict.fromkeys([*grain, *columns_declared]):
        column = columns_declared.get(name, {})
        role = column.get("role")
        if name not in grain and role not in _FIELD_ROLES:
            reasons[f"columns.{name}"] = (
                "a column with no role, so the declaration does not say it belongs "
                "in the semantic layer"
            )
            continue
        fields.append(_field(name, column, declaration, notes))
        carried.extend(_carried_member(name, column, with_role=bool(role)))
        if role == "measure":
            metric = _metric(name, column, limitations, reasons)
            if metric is not None:
                metrics.append(metric)
                carried.append(f"columns.{name}.aggregation")
    if grain:
        carried.append("grain")
    else:
        limitations.append(
            "the declaration states no grain, so the dataset declares no primary key"
        )
    names = {f["name"].casefold() for f in fields}
    clashing = [m["name"] for m in metrics if m["name"].casefold() in names]
    if clashing:
        return finish(
            "failed",
            f"the metric name {', '.join(clashing)} is also a field of the dataset, "
            "and a dataset's fields and metrics share one namespace",
        )

    body: dict[str, Any] = {"name": model, "source": source}
    if grain:
        body["primary_key"] = list(grain)
    description = with_population(
        declared.get("description"), declared.get("population")
    )
    if description:
        body["description"] = description
    if declared.get("description"):
        carried.append("description")
    meta = model_metadata(declared)
    if meta:
        body["ai_context"] = _ai_context(None, meta, declared.get("population"))
        carried.extend(meta)
    body["fields"] = fields
    if metrics:
        body["metrics"] = metrics

    relationships: list[dict[str, Any]] = []
    if target is not None:
        for reference in declaration.references:
            parent = parents.get(reference.column)
            why = None
            if parent is None:
                why = (
                    f"no dataset in {target} sits on {reference.parent_model}, so "
                    "there is no dataset to relate to"
                )
            elif not reference.parent_column:
                why = f"references names no column of {reference.parent_model}"
            else:
                to_field = _field_for_column(
                    parent[1], reference.parent_column, connector
                )
                if to_field is None:
                    why = (
                        f"dataset '{parent[1].get('name')}' declares no field "
                        f"exposing {reference.parent_column}, and a relationship "
                        "names fields; declare one first"
                    )
                else:
                    relationships.append(
                        {
                            "name": f"{model}_to_{parent[1].get('name')}",
                            "from": model,
                            "to": parent[1].get("name"),
                            "from_columns": [reference.column],
                            "to_columns": [to_field],
                        }
                    )
                    carried.append(f"columns.{reference.column}.references")
                    notes.append(
                        f"{reference.column}: relates to dataset "
                        f"'{parent[1].get('name')}' on its field '{to_field}' (the "
                        "declared foreign key)"
                    )
            if why is not None:
                notes.append(f"{reference.column}: {why}")
                reasons[f"columns.{reference.column}.references"] = why
    else:
        for reference in declaration.references:
            reasons[f"columns.{reference.column}.references"] = (
                "the dataset is derived as a document of its own, and a "
                "relationship joins datasets of one model only"
            )

    notes.append(
        "metrics beyond declared measures are not derived: a dataset metric is "
        "what a column's declared aggregation states, anything more is a business "
        "definition, and that stays the author's"
    )
    definition.carried, definition.not_carried = account(
        declared,
        carried,
        reasons,
        default="not derived; the notes and limitations say why",
    )

    if target is None:
        definition.path = f"{directory}/{model}.ossie.yaml"
        if definition.path in texts:
            return finish(
                "failed",
                f"{definition.path} already exists; a proposed document is written "
                "to a file of its own and never over another",
            )
        if any(data.get("name") == model for _p, data in documents):
            return finish(
                "failed",
                f"a semantic model named '{model}' is already configured; dex does "
                "not rename one",
            )
        definition.content = dump_items(
            {"version": VERSION, "name": model, "datasets": [body]}
        )
        if definition.path in layer.files:
            texts[definition.path] = definition.content
            definition.status = "included"
        else:
            definition.status = "proposed"
            definition.next = _adopt(layer, definition)
        return definition

    text = append_item(texts[target], "datasets", body)
    for relationship in relationships:
        text = append_item(text, "relationships", relationship) if text else None
    if text is None:
        return finish(
            "failed",
            f"the lists in {target} are not written block-style, so dex cannot add a "
            "dataset without rewriting lines it did not author",
        )
    texts[target] = text
    definition.path = target
    definition.content = text
    definition.status = "included"
    return definition


def _field(
    name: str,
    column: Mapping[str, Any],
    declaration: SemanticDeclaration,
    notes: list[str],
) -> dict[str, Any]:
    field_: dict[str, Any] = {"name": name, "expression": {"sql": name}}
    datatype = _datatype(name, declaration)
    if datatype:
        field_["datatype"] = datatype
    elif column.get("role") == "time":
        notes.append(
            f"{name}: declared a time column, and its type could not be read from "
            "the SQL or the cache; it is a time dimension as declared"
        )
    if column.get("role") == "time":
        field_["dimension"] = {"is_time": True}
    if isinstance(column.get("description"), str) and column["description"]:
        field_["description"] = column["description"]
    meta = native_column_metadata(column)
    if meta:
        field_["ai_context"] = {DEX_META_KEY: meta}
    return field_


def _carried_member(
    name: str, fields: Mapping[str, Any], *, with_role: bool
) -> list[str]:
    keys = ["description", "unit", "null_rule", "additivity"]
    if with_role:
        keys.append("role")
    return [f"columns.{name}.{key}" for key in keys if fields.get(key)]


def _metric(
    name: str,
    fields: Mapping[str, Any],
    limitations: list[str],
    reasons: dict[str, str],
) -> dict[str, Any] | None:
    declared = fields.get("aggregation")
    additivity = fields.get("additivity")
    why: str | None = None
    mapped: tuple[str, str] | None = None
    if not isinstance(declared, str) or not declared.strip():
        why = "a measure with no declared aggregation; declare `aggregation`"
    else:
        mapped = AGGREGATIONS.get(re.sub(r"[\s-]+", "_", declared.strip().lower()))
        if mapped is None:
            known = ", ".join(sorted({s for _t, s in AGGREGATIONS.values()}))
            why = (
                f"aggregation '{declared}' has no portable OSSIE_SQL_2026 aggregate "
                f"dex maps ({known})"
            )
        elif additivity == "non_additive" and mapped[1] in _SUMMING:
            why = (
                f"declared non_additive with aggregation '{declared}', which adds "
                "it up; the two disagree. Fix the declaration"
            )
        elif additivity == "semi_additive":
            why = (
                "semi_additive, and an Ossie metric has no way to sum across "
                "entities but not across time; which expression stands for it is "
                "the author's call"
            )
    if why is not None or mapped is None:
        limitations.append(f"{name}: {why}, so no metric is derived")
        reasons[f"columns.{name}.aggregation"] = why or ""
        return None
    template, suffix = mapped
    metric: dict[str, Any] = {
        "name": f"{name}_{suffix}",
        "expression": {"sql": template.format(name)},
    }
    if isinstance(fields.get("description"), str) and fields["description"]:
        metric["description"] = fields["description"]
    return metric


def _datatype(name: str, declaration: SemanticDeclaration) -> str | None:
    raw = declaration.data_types.get(name)
    mapped = ossie_datatype(raw) if raw else None
    if mapped is None:
        kind = (declaration.column_types.get(name) or (None, None))[0]
        mapped = {"date": "Date", "timestamp": "DateTime"}.get(kind or "")
    return mapped


def ossie_datatype(raw: str) -> str | None:
    """The Ossie ``datatype`` a warehouse type reads as, or ``None``."""

    lowered = raw.strip().lower()
    if ("timestamp" in lowered and "tz" in lowered) or "with time zone" in lowered:
        return "DateTimeTz"
    if "timestamp" in lowered or "datetime" in lowered:
        return "DateTime"
    if lowered.startswith("date"):
        return "Date"
    if lowered.startswith("time"):
        return "Time"
    if lowered.startswith("bool"):
        return "Boolean"
    if re.match(r"^(u?(tiny|small|big|huge)?int(eger)?\d*|int\d+|long|short)", lowered):
        return "Integer"
    if re.match(r"^(numeric|decimal|number|bignumeric)", lowered):
        return "Decimal"
    if re.match(r"^(double|float|real)", lowered):
        return "Float"
    if re.match(r"^(varchar|char|text|string|nvarchar|character)", lowered):
        return "String"
    return None


def _ai_context(
    existing: Any, facts: Mapping[str, Any], population: Any
) -> dict[str, Any]:
    """``ai_context`` as an object carrying ``facts`` under ``dex``.

    An authored string becomes ``instructions``; authored instructions are kept,
    and only the population sentence dex wrote before is replaced.
    """

    context: dict[str, Any]
    if isinstance(existing, dict):
        context = dict(existing)
    elif isinstance(existing, str) and existing:
        context = {"instructions": existing}
    else:
        context = {}
    sentence = population_sentence(population) if population else None
    instructions = context.get("instructions")
    if sentence and (
        not instructions
        or (isinstance(instructions, str) and instructions.startswith("Population: "))
    ):
        context["instructions"] = sentence
    dex = context.get(DEX_META_KEY)
    merged = dict(dex) if isinstance(dex, dict) else {}
    merged.update(facts)
    context[DEX_META_KEY] = merged
    return context


# --- what the layer already holds ----------------------------------------------------


def _documents(texts: Mapping[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Every parseable flat document: ``(path, data)``."""

    out: list[tuple[str, dict[str, Any]]] = []
    for path, text in sorted(texts.items()):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and "semantic_model" not in parsed:
            out.append((path, parsed))
    return out


def _field_for_column(
    dataset: Mapping[str, Any], column: str, connector: str | None
) -> str | None:
    """The field of ``dataset`` exposing ``column``, or named it; ``None`` if none.

    A field whose own expression is the bare column wins over one merely named
    like it, since names (keys, relationships) reach columns through fields.
    """

    named: str | None = None
    for field_ in dataset.get("fields") or []:
        if not isinstance(field_, dict) or not isinstance(field_.get("name"), str):
            continue
        resolved = catalog_mod._link(field_, connector, None)[3]
        if resolved is not None and resolved.casefold() == column.casefold():
            return field_["name"]
        if named is None and field_["name"].casefold() == column.casefold():
            named = field_["name"]
    return named


def _directory(layer: OssieSemanticLayer) -> str:
    parents = {str(PurePosixPath(name).parent) for name in layer.files}
    if len(parents) == 1:
        only = next(iter(parents))
        return only if only not in ("", ".") else DEFAULT_DIR
    return DEFAULT_DIR


# --- validation ----------------------------------------------------------------------


def _validate(
    layer: OssieSemanticLayer,
    request: SemanticDerivationRequest,
    texts: Mapping[str, str],
    built: list[DerivedDefinition],
    changed: list[str],
) -> None:
    """Hold every derived document to the stack a hand-authored one meets.

    Schema, integrity and expression checks over the whole prospective set, then
    a read back through the catalog, then (for what is included) the plan
    validation authoring runs, the exploration cache included.
    """

    from .authoring import validate_plan

    ready = [d for d in built if d.status in ("included", "proposed") and d.content]
    if not ready:
        return
    prospective = dict(texts)
    for definition in ready:
        if definition.status == "proposed":
            prospective[definition.path or ""] = definition.content or ""
    validated = validate_document_contents(
        prospective, connector=request.connector or layer.connector
    )
    by_file: dict[str, list[str]] = {}
    for diagnostic in validated.errors:
        by_file.setdefault(diagnostic.file, []).append(diagnostic.render())
    catalog = catalog_mod.semantic_catalog(
        validated.documents, connector=request.connector or layer.connector
    )
    catalogued = {m.label for m in catalog.semantic_models}

    for definition in ready:
        problems = by_file.get(definition.path or "")
        if problems:
            definition.status = "failed"
            definition.reason = "the Ossie checks refuse it: " + "; ".join(problems)
            definition.validation["ossie"] = {
                "result": "failed",
                "detail": "; ".join(problems),
            }
            continue
        definition.validation["ossie"] = {"result": "passed"}
        if definition.name not in catalogued:
            definition.status = "failed"
            definition.reason = (
                "the derived dataset did not read back as a catalog model"
            )
            definition.validation["catalog"] = {"result": "failed"}
            continue
        definition.validation["catalog"] = {"result": "passed"}

    included = [d for d in ready if d.status == "included"]
    paths = [p for p in changed if any(d.path == p for d in included)]
    if not paths:
        return
    try:
        validate_plan(
            layer,
            [_edit(path, texts[path]) for path in paths],
            "plan",
            cache=request.cache,
        )
    except ValueError as exc:
        for definition in included:
            definition.status = "failed"
            definition.reason = str(exc)
            definition.validation["plan"] = {"result": "failed", "detail": str(exc)}
        return
    for definition in included:
        definition.validation["plan"] = {"result": "passed"}


def _adopt(layer: OssieSemanticLayer, definition: DerivedDefinition) -> str:
    listed = ", ".join(dict.fromkeys([*layer.files, definition.path or ""]))
    return (
        f"{definition.path} is not under semantic.ossie.files, the documents dex "
        f"may write: run `dex config set semantic.ossie.files '[{listed}]'`, then "
        "pass data.semantic_proposal.definitions as the edits file to `semantic "
        "ossie define`"
    )


def _edit(path: str, content: str) -> Any:
    from ..transform.plans import EditKind, PlanEdit

    return PlanEdit(path=path, kind=EditKind.SEMANTIC_DOCUMENT, new_content=content)


# --- the envelope --------------------------------------------------------------------


def _proposal(definitions: Mapping[str, DerivedDefinition]) -> dict[str, Any]:
    proposed = [d for d in definitions.values() if d.status == "proposed"]
    included = [d for d in definitions.values() if d.status == "included"]
    data: dict[str, Any] = {
        "vendor": "ossie",
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
                "add each proposed path to semantic.ossie.files with `dex config "
                "set`, then pass data.semantic_proposal.definitions as the edits "
                "file to `semantic ossie define`"
            )
        )
    if included:
        notes.append(
            "included datasets are stored as a plan of their own on the Ossie layer "
            "(data.semantic_proposal.plan_id), beside the dbt plan; apply both"
        )
    data["notes"] = notes
    return data


def _model_data(definition: DerivedDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {"status": definition.status}
    if definition.content is not None:
        out["dataset"] = definition.name
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
                f"Ossie dataset '{definition.model}' was derived from the "
                f"declaration into {definition.path}; see data.semantic_proposal"
            )
        elif definition.status in ("failed", "unvalidated"):
            out.append(
                f"Ossie dataset for '{definition.model}' was derived and not "
                f"offered ({definition.status}): {definition.reason}"
            )
    return out


# --- annotate: declarations onto a dataset the layer or payload defines ---------------


def annotate(
    layer: OssieSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """Render each declaration into the dataset it names.

    The declaration wins field by field. Only the named dataset's list item is
    re-serialized and spliced back where it was, and a declared relationship is
    appended to the model's list, so every other byte of the document is kept.
    A declaration naming no dataset, or naming a grain column no field exposes,
    is ``failed``, and the native route refuses the plan for it.
    """

    from ..semantic_source import DerivedDefinition, SemanticDerivationResult

    connector = request.connector or layer.connector
    texts = dict(request.documents)
    definitions: dict[str, DerivedDefinition] = {}
    changed_paths: list[str] = []
    for declaration in request.declarations:
        definition = DerivedDefinition(model=declaration.model, name=declaration.model)
        definitions[declaration.model] = definition
        found = [
            path
            for path, data in _documents(texts)
            for dataset in data.get("datasets") or []
            if isinstance(dataset, dict) and dataset.get("name") == declaration.model
        ]
        if not found:
            definition.status = "failed"
            definition.reason = (
                f"no configured Ossie document declares a dataset named "
                f"'{declaration.model}'"
            )
            continue
        if len(found) > 1:
            definition.status = "failed"
            definition.reason = (
                f"datasets named '{declaration.model}' are declared in "
                f"{', '.join(found)}, so the declaration names no one dataset"
            )
            continue
        path = found[0]
        definition.path = path
        located = locate(texts[path], declaration.model, "datasets")
        if located is None:
            definition.status = "failed"
            definition.reason = (
                f"the dataset '{declaration.model}' in {path} is not written as a "
                "block-style list item dex can rewrite on its own, so nothing in "
                "the document was changed"
            )
            continue
        start, end, indent, entry = located
        outcome = _annotated(entry, declaration, connector)
        if isinstance(outcome, str):
            definition.status = "failed"
            definition.reason = outcome
            continue
        changed, carried, reasons, relationships = outcome
        relationships = _resolve_parents(
            relationships, _documents({path: texts[path]}), connector, reasons
        )
        text = texts[path]
        if changed:
            text = text[:start] + dump_items([entry], indent) + text[end:]
        for relationship in relationships:
            key = f"columns.{relationship.pop('_column')}.references"
            if _has_relationship(text, relationship):
                carried.append(key)
                continue
            appended = append_item(text, "relationships", relationship)
            if appended is None:
                reasons[key] = (
                    f"the relationships list in {path} is not written block-style"
                )
                continue
            text = appended
            carried.append(key)
        definition.carried, definition.not_carried = account(
            declaration.declared,
            carried,
            reasons,
            default="the dataset has no field of that name",
        )
        definition.status = "included"
        if text != texts[path]:
            texts[path] = text
            if path not in changed_paths:
                changed_paths.append(path)
        else:
            definition.notes.append("the dataset already states every declared fact")
        definition.content = texts[path]
    edits = [_edit(path, texts[path]) for path in changed_paths]
    for definition in definitions.values():
        if definition.path in texts and definition.status == "included":
            definition.content = texts[definition.path]
    return SemanticDerivationResult(
        vendor="ossie",
        edit_target="semantic",
        edits=edits,
        definitions=definitions,
        proposal=None,
        warnings=[],
    )


def _resolve_parents(
    relationships: list[dict[str, Any]],
    documents: list[tuple[str, dict[str, Any]]],
    connector: str | None,
    reasons: dict[str, str],
) -> list[dict[str, Any]]:
    """Each relationship pointed at the parent dataset and field, or dropped.

    ``references`` names a parent model and its column; the relationship names a
    dataset of the same document and the field exposing that column.
    """

    datasets = [
        d
        for _p, data in documents
        for d in data.get("datasets") or []
        if isinstance(d, dict)
    ]
    out: list[dict[str, Any]] = []
    for relationship in relationships:
        parent_model, parent_column = relationship["to"], relationship["to_columns"][0]
        key = f"columns.{relationship['_column']}.references"
        parent = next(
            (
                d
                for d in datasets
                if d.get("name") == parent_model
                or table_name(d.get("source")) == str(parent_model).lower()
            ),
            None,
        )
        if parent is None:
            reasons[key] = (
                f"no dataset in the same document sits on {parent_model}, and a "
                "relationship joins datasets of one model only"
            )
            continue
        to_field = _field_for_column(parent, parent_column, connector)
        if to_field is None:
            reasons[key] = (
                f"dataset '{parent.get('name')}' declares no field exposing "
                f"{parent_column}; a relationship names fields, so declare one first"
            )
            continue
        relationship.pop("_column")
        relationship["name"] = f"{relationship['from']}_to_{parent.get('name')}"
        relationship["to"] = parent.get("name")
        relationship["to_columns"] = [to_field]
        relationship["_column"] = key.split(".")[1]
        out.append(relationship)
    return out


def _has_relationship(text: str, relationship: Mapping[str, Any]) -> bool:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    for existing in (data or {}).get("relationships") or []:
        if not isinstance(existing, dict):
            continue
        if existing.get("name") == relationship["name"] or (
            existing.get("from") == relationship["from"]
            and existing.get("to") == relationship["to"]
            and existing.get("from_columns") == relationship["from_columns"]
        ):
            return True
    return False


def _annotated(
    entry: dict[str, Any],
    declaration: SemanticDeclaration,
    connector: str | None,
) -> str | tuple[bool, list[str], dict[str, str], list[dict[str, Any]]]:
    """Apply the declared facts to ``entry`` in place.

    Returns ``(changed, carried, reasons, relationships)``, or the reason the
    declaration cannot be carried at all.
    """

    declared = declaration.declared
    before = yaml.safe_dump(entry, sort_keys=False)
    carried: list[str] = []
    reasons: dict[str, str] = {}

    fields = [f for f in entry.get("fields") or [] if isinstance(f, dict)]

    def field_of(column: str) -> dict[str, Any] | None:
        name = _field_for_column(entry, column, connector)
        return next((f for f in fields if f.get("name") == name), None)

    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    if grain:
        resolved = [(g, field_of(g)) for g in grain]
        missing = [g for g, f in resolved if f is None]
        if missing:
            return (
                f"dataset '{declaration.model}' declares no field exposing the grain "
                f"column {', '.join(missing)}; a key names fields, so author the "
                "field first"
            )
        entry["primary_key"] = [str(f["name"]) for _g, f in resolved if f]
        carried.append("grain")

    if declared.get("description") or declared.get("population"):
        description = with_population(
            declared.get("description")
            or _without_population(entry.get("description")),
            declared.get("population"),
        )
        if description:
            entry["description"] = description
        if declared.get("description"):
            carried.append("description")
    meta = model_metadata(declared)
    if meta:
        entry["ai_context"] = _ai_context(
            entry.get("ai_context"), meta, declared.get("population")
        )
        carried.extend(meta)

    metrics = entry.get("metrics")
    if not isinstance(metrics, list):
        metrics = []
    relationships: list[dict[str, Any]] = []
    for name, column in (declared.get("columns") or {}).items():
        if not isinstance(column, dict):
            continue
        field_ = field_of(name)
        if field_ is None:
            reasons[f"columns.{name}"] = "the dataset has no field exposing it"
            continue
        if isinstance(column.get("description"), str) and column["description"]:
            field_["description"] = column["description"]
        if column.get("role") == "time":
            dimension = field_.get("dimension")
            field_["dimension"] = {
                **(dimension if isinstance(dimension, dict) else {}),
                "is_time": True,
            }
        column_meta = native_column_metadata(column)
        if column_meta:
            context = field_.get("ai_context")
            context = (
                dict(context)
                if isinstance(context, dict)
                else ({"instructions": context} if isinstance(context, str) else {})
            )
            dex = context.get(DEX_META_KEY)
            context[DEX_META_KEY] = {
                **(dex if isinstance(dex, dict) else {}),
                **column_meta,
            }
            field_["ai_context"] = context
        carried.extend(
            _carried_member(name, column, with_role=bool(column.get("role")))
        )
        if column.get("role") == "measure":
            limitations: list[str] = []
            metric = _metric(str(field_["name"]), column, limitations, reasons)
            if metric is not None:
                if metric["name"] in {f.get("name") for f in fields}:
                    reasons[f"columns.{name}.aggregation"] = (
                        f"'{metric['name']}' is a field of the dataset, and a "
                        "dataset's fields and metrics share one namespace"
                    )
                else:
                    if not any(
                        isinstance(m, dict) and m.get("name") == metric["name"]
                        for m in metrics
                    ):
                        metrics.append(metric)
                    carried.append(f"columns.{name}.aggregation")
        if column.get("role") == "foreign_key" and column.get("references"):
            relationship = _declared_relationship(
                declaration.model, str(field_["name"]), column["references"]
            )
            if relationship is None:
                reasons[f"columns.{name}.references"] = (
                    f"references '{column['references']}' names no dataset and column"
                )
            else:
                relationship["_column"] = name
                relationships.append(relationship)
    if metrics and "metrics" not in entry:
        entry["metrics"] = metrics
    return (
        yaml.safe_dump(entry, sort_keys=False) != before,
        carried,
        reasons,
        relationships,
    )


def _declared_relationship(
    dataset: str, field_name: str, references: str
) -> dict[str, Any] | None:
    """A relationship for ``references`` (``parent.column``), naming the
    parent's column as its field. The prospective document is validated after,
    so a parent or field that is not declared there refuses the plan by name."""

    parent, _, parent_column = references.rpartition(".")
    if not parent or not parent_column:
        return None
    return {
        "name": f"{dataset}_to_{parent}",
        "from": dataset,
        "to": parent,
        "from_columns": [field_name],
        "to_columns": [parent_column],
    }


def _without_population(description: Any) -> str | None:
    """An authored description less a population sentence dex wrote earlier."""

    if not isinstance(description, str):
        return None
    head, sep, _tail = description.partition(" Population: ")
    if sep:
        return head
    return None if description.startswith("Population: ") else description
