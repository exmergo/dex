"""A cube derived from a declared mart (#911).

The Cube counterpart of the MetricFlow (#499) and SLayer (#927) derivations,
behind the same :class:`~..semantic_source.SemanticDerivationSource` hook. A
declaration already states most of what a cube needs:

- ``grain``: ``primary_key: true`` on each grain dimension, composite included.
- Every role but ``measure``: a dimension, ``sql: "{CUBE}.<column>"``, typed
  from the SQL or the cache (``time``, ``number``, ``boolean``, else
  ``string``). A ``time`` column is a ``time`` dimension whatever else is known.
- ``measure`` with ``aggregation``: a measure named after the column, of the
  Cube type the aggregation means. A measure column is not also a dimension,
  as MetricFlow does not make it one.
- ``foreign_key`` with ``references``: a ``many_to_one`` join to the cube
  already sitting on the parent's table.
- ``description``: the cube's and each member's ``description``.
- ``population``, ``assumptions``: ``meta.dex``, and the population as one
  sentence at the end of the description.
- ``unit``, ``null_rule``, ``additivity``: the member's ``meta.dex``.

Three things it never does, as the other derivations never do. It never invents
a metric beyond a declared measure (no implicit ``count``), never guesses (a
``semi_additive`` measure has no exact Cube equivalent, and ``median`` no Cube
type, so both are named and left to the author), and never renames: a cube name
another definition holds fails the derivation.

**A population is metadata, never a filter.** The cube sits on the mart, whose
SQL already enforces it, so no measure ``filters`` and no ``segments`` are
written: either would apply the rule twice and diverge from the mart the day the
two disagree.

**Where it goes.** One file per mart, ``<dir>/<mart>.yml``, where ``<dir>`` is
the one directory the configured YAML files share (``model/cubes`` where they
share none). It is *included*, as a plan of its own beside the mart's, only
where that path is already listed under ``semantic.cube.files``: that list is
the layer's write surface, and adding to it is a configuration change with its
own confirmation. Otherwise it is *proposed*, validated by the same checks, with
the two commands that adopt it.

**Annotate.** ``semantic cube plan`` with ``declarations`` renders each one
into the cube it names, field by field. Only that cube's entry is re-serialized
and spliced back into its file, so every other cube, view and comment in the
file keeps its bytes.

Nothing here starts Node, calls a Cube server, or opens a warehouse connection.
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
    column_metadata,
    model_metadata,
    native_column_metadata,
    placed_table,
    sibling_prefix,
    table_name,
    with_population,
)
from .yaml_items import locate

if TYPE_CHECKING:
    from ..semantic_source import (
        DerivedDefinition,
        SemanticDeclaration,
        SemanticDerivationRequest,
        SemanticDerivationResult,
    )
    from .project import CubeSemanticLayer

#: Declared aggregation (lowered, spaces and dashes as underscores) to the Cube
#: measure type it means. Strict, as the other derivations' tables are:
#: anything else is named, never mapped to a neighbour. ``median`` is absent
#: because Cube has no median type.
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
}

#: Aggregations that add values up, which ``non_additive`` contradicts.
_SUMMING = frozenset({"sum"})

#: Roles that become a dimension. ``measure`` becomes a measure instead.
_DIMENSION_ROLES = frozenset({"key", "foreign_key", "dimension", "time", "attribute"})

#: Where a derived cube goes when the configured files share no directory.
DEFAULT_DIR = "model/cubes"


def derive(
    layer: CubeSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """The cube each declared mart states, validated, never written."""

    from ..semantic_source import SemanticDerivationResult

    if request.mode == "annotate":
        return annotate(layer, request)

    current = _cubes(request.documents)
    directory = _directory(layer)
    prefix = sibling_prefix(
        (_get(entry, "sql_table") for _p, entry, _k in current),
        request.project_identifiers.values(),
    )

    built: list[tuple[DerivedDefinition, dict[str, Any] | None]] = []
    batch: list[dict[str, Any]] = []
    for declaration in request.declarations:
        definition, body = _build(
            declaration,
            current,
            batch=batch,
            directory=directory,
            prefix=prefix,
            data_source=layer.data_source,
            documents=request.documents,
        )
        built.append((definition, body))
        if body is not None:
            batch.append(body)

    _validate(layer, request.documents, built)
    included = [d for d, _b in built if d.status == "included"]
    definitions = {d.model: d for d, _b in built}
    return SemanticDerivationResult(
        vendor="cube",
        edit_target="semantic",
        edits=[_edit(d) for d in included],
        definitions=definitions,
        proposal=_proposal(definitions),
        warnings=_warnings(definitions),
    )


# --- building one cube ---------------------------------------------------------------


def _build(
    declaration: SemanticDeclaration,
    current: list[tuple[str, dict[str, Any], str]],
    *,
    batch: list[dict[str, Any]],
    directory: str,
    prefix: str | None,
    data_source: str,
    documents: Mapping[str, str],
) -> tuple[DerivedDefinition, dict[str, Any] | None]:
    from ..semantic_source import DerivedDefinition

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

    for path, entry, kind in current:
        if (
            kind == "cubes"
            and table_name(_get(entry, "sql_table")) == identifier.lower()
        ):
            return finish(
                "skipped",
                f"{model} already has a cube ('{entry.get('name')}' in {path}); an "
                "authored cube is not overwritten or duplicated",
            )
    for path, entry, kind in current:
        if entry.get("name") == model:
            where = (
                f"on another table ({_get(entry, 'sql_table')})"
                if kind == "cubes"
                else "as a view"
            )
            return finish(
                "failed",
                f"the Cube name '{model}' is already declared in {path} {where}; "
                "cubes and views share one namespace, and dex does not rename either",
            )

    definition.path = f"{directory}/{model}.yml"
    if definition.path in documents:
        return finish(
            "failed",
            f"{definition.path} already holds other Cube definitions; a derived "
            "cube is written to a file of its own and never spliced into one",
        )

    sql_table, source = placed_table(identifier, declaration.relation, prefix)
    if source == "siblings":
        notes.append(
            f"sql_table is {sql_table}: the database and schema the cubes on this "
            "project's other models already read"
        )
    elif source == "manifest":
        notes.append(
            f"sql_table is {sql_table}, the relation the compiled dbt manifest "
            "names, which is whatever target last compiled the project (often "
            "dev). Point it at the relation Cube's data source reads"
        )
    else:
        limitations.append(
            f"sql_table is the bare table name '{identifier}': no cube sits on "
            "another of this project's models and there is no compiled manifest, "
            "so Cube resolves it in its data source's default schema and dex's "
            "catalog carries no qualified relation for it. Qualify it before "
            "relying on it"
        )

    dimensions: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    names = list(dict.fromkeys([*grain, *columns_declared]))
    for name in names:
        fields = columns_declared.get(name, {})
        role = fields.get("role")
        if role == "measure" and name not in grain:
            measure = _measure(name, fields, limitations, reasons)
            if measure is not None:
                measures.append(measure)
                carried.extend(_carried_member(name, fields, with_role=True))
                carried.append(f"columns.{name}.aggregation")
            elif fields.get("description") or column_metadata(fields):
                reasons.setdefault(
                    f"columns.{name}",
                    "no measure was derived for this column, so there is no "
                    "member to carry it",
                )
            continue
        if name not in grain and role not in _DIMENSION_ROLES:
            reasons[f"columns.{name}"] = (
                "a column with no role, so the declaration does not say it belongs "
                "in the semantic layer"
            )
            continue
        dimension: dict[str, Any] = {"name": name, "sql": f"{{CUBE}}.{name}"}
        cube_type, type_note = _dimension_type(name, fields, declaration)
        dimension["type"] = cube_type
        if type_note:
            notes.append(type_note)
        if name in grain:
            dimension["primary_key"] = True
        _describe(dimension, fields)
        dimensions.append(dimension)
        carried.extend(_carried_member(name, fields, with_role=bool(role)))
        if role == "measure":
            reasons[f"columns.{name}.aggregation"] = (
                "a grain column is a dimension of the cube, and a measure of the "
                "same name would collide with it"
            )
    if grain:
        carried.append("grain")
    else:
        limitations.append(
            "the declaration states no grain, so no dimension is the primary key, "
            "and Cube cannot deduplicate this cube's measures across a fan-out join"
        )

    joins = _joins(declaration, current, batch, notes, reasons)
    carried.extend(f"columns.{j.pop('_column')}.references" for j in joins)

    body: dict[str, Any] = {"name": model, "sql_table": sql_table}
    if data_source != "default":
        body["data_source"] = data_source
    description = with_population(
        declared.get("description"), declared.get("population")
    )
    if description:
        body["description"] = description
    if declared.get("description"):
        carried.append("description")
    if joins:
        body["joins"] = joins
    body["dimensions"] = dimensions
    if measures:
        body["measures"] = measures
    meta = model_metadata(declared)
    if meta:
        body["meta"] = {DEX_META_KEY: meta}
    carried.extend(meta)

    notes.append(
        "metrics beyond declared measures are not derived: a measure is what a "
        "column's declared aggregation states, anything more is a business "
        "definition, and that stays the author's"
    )
    definition.carried, definition.not_carried = account(
        declared,
        carried,
        reasons,
        default="not derived; the notes and limitations say why",
    )
    definition.content = dump_file(body)
    return definition, body


def _carried_member(
    name: str, fields: Mapping[str, Any], *, with_role: bool
) -> list[str]:
    keys = ["description", "unit", "null_rule", "additivity"]
    if with_role:
        keys.append("role")
    return [f"columns.{name}.{key}" for key in keys if fields.get(key)]


def _describe(member: dict[str, Any], fields: Mapping[str, Any]) -> None:
    if isinstance(fields.get("description"), str) and fields["description"]:
        member["description"] = fields["description"]
    meta = native_column_metadata(fields)
    if meta:
        member["meta"] = {DEX_META_KEY: meta}


def _measure(
    name: str,
    fields: Mapping[str, Any],
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
                f"aggregation '{declared}' has no Cube measure type dex maps "
                f"({', '.join(sorted(set(AGGREGATIONS.values())))})"
            )
        elif additivity == "non_additive" and agg in _SUMMING:
            why = (
                f"declared non_additive with aggregation '{declared}', which adds "
                "it up; the two disagree. Fix the declaration"
            )
        elif additivity == "semi_additive":
            why = (
                "semi_additive, and Cube has no measure that sums across entities "
                "but not across time: a rolling window or a multi-stage measure "
                "can express it, and which one is the author's call"
            )
    if why is not None:
        limitations.append(f"{name}: {why}, so no measure is derived")
        reasons[f"columns.{name}.aggregation"] = why
        reasons.setdefault(f"columns.{name}", why)
        return None
    measure: dict[str, Any] = {
        "name": name,
        "sql": f"{{CUBE}}.{name}",
        "type": agg,
    }
    _describe(measure, fields)
    return measure


def _dimension_type(
    name: str, fields: Mapping[str, Any], declaration: SemanticDeclaration
) -> tuple[str, str | None]:
    """The Cube dimension ``type``, and a note when the role decided it alone."""

    raw = declaration.data_types.get(name)
    mapped = cube_type(raw) if raw else None
    kind = (declaration.column_types.get(name) or (None, None))[0]
    if mapped is None and kind in ("date", "timestamp"):
        mapped = "time"
    if fields.get("role") == "time":
        if mapped not in (None, "time"):
            return "time", (
                f"{name}: declared a time column, and its type reads as {raw}; it "
                "is a `time` dimension as declared, so check that Cube can treat "
                "it as a timestamp"
            )
        if mapped is None:
            return "time", (
                f"{name}: declared a time column, and its type could not be read "
                "from the SQL or the cache; it is a `time` dimension as declared"
            )
        return "time", None
    return mapped or "string", None


def cube_type(raw: str) -> str | None:
    """The Cube dimension type a warehouse type reads as, or ``None``."""

    lowered = raw.strip().lower()
    if "timestamp" in lowered or "datetime" in lowered or lowered.startswith("date"):
        return "time"
    if lowered.startswith("bool"):
        return "boolean"
    if re.match(
        r"^(u?(tiny|small|big|huge)?int(eger)?\d*|int\d+|long|short|double|float|"
        r"real|number|numeric|decimal|bignumeric)",
        lowered,
    ):
        return "number"
    if re.match(r"^(varchar|char|text|string|nvarchar|character)", lowered):
        return "string"
    return None


# --- what the layer already holds ----------------------------------------------------


def _cubes(documents: Mapping[str, str]) -> list[tuple[str, dict[str, Any], str]]:
    """Every cube and view the documents declare: ``(path, entry, kind)``."""

    out: list[tuple[str, dict[str, Any], str]] = []
    for path, text in sorted(documents.items()):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for kind in ("cubes", "views"):
            out.extend(
                (path, entry, kind)
                for entry in parsed.get(kind) or []
                if isinstance(entry, dict)
            )
    return out


def _get(entry: Mapping[str, Any], key: str) -> Any:
    """A key in either spelling Cube accepts (``sql_table`` or ``sqlTable``)."""

    if key in entry:
        return entry[key]
    camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key)
    return entry.get(camel)


def _directory(layer: CubeSemanticLayer) -> str:
    from .authoring import editing_surface

    parents = {str(PurePosixPath(name).parent) for name in editing_surface(layer)}
    if len(parents) == 1:
        only = next(iter(parents))
        return only if only not in ("", ".") else DEFAULT_DIR
    return DEFAULT_DIR


def _joins(
    declaration: SemanticDeclaration,
    current: list[tuple[str, dict[str, Any], str]],
    batch: list[dict[str, Any]],
    notes: list[str],
    reasons: dict[str, str],
) -> list[dict[str, Any]]:
    candidates = [entry for _p, entry, kind in current if kind == "cubes"] + batch
    joins: list[dict[str, Any]] = []
    joined: set[str] = set()
    for reference in declaration.references:
        target = None
        wanted = (reference.parent_identifier or reference.parent_model).lower()
        for entry in candidates:
            if (
                table_name(_get(entry, "sql_table")) == wanted
                or entry.get("name") == reference.parent_model
            ):
                target = entry
                break
        why: str | None = None
        if target is None:
            why = (
                f"no cube sits on {reference.parent_model}, so there is no cube to "
                "join to; the join is derived once one does"
            )
        elif not reference.parent_column:
            why = f"references names no column of {reference.parent_model}"
        elif target["name"] in joined:
            why = (
                f"another declared foreign key already joins cube '{target['name']}'; "
                "Cube allows one join between a pair of cubes unless the second has "
                "an alias, and dex does not invent one"
            )
        if why is not None or target is None:
            notes.append(f"{reference.column}: {why}")
            reasons[f"columns.{reference.column}.references"] = why or ""
            continue
        joined.add(target["name"])
        joins.append(
            {
                "name": target["name"],
                "sql": (
                    f"{{CUBE}}.{reference.column} = "
                    f"{{{target['name']}}}.{reference.parent_column}"
                ),
                "relationship": "many_to_one",
                "_column": reference.column,
            }
        )
        notes.append(
            f"{reference.column}: joins cube '{target['name']}' on "
            f"{reference.parent_column} (many_to_one, the declared foreign key)"
        )
    return joins


# --- validation ----------------------------------------------------------------------


def _validate(
    layer: CubeSemanticLayer,
    documents: Mapping[str, str],
    built: list[tuple[DerivedDefinition, dict[str, Any] | None]],
) -> None:
    """Hold every built cube to authoring's own checks, then the catalog.

    The whole prospective set is validated at once (the configured files plus
    every derived one), so a join between two marts derived together resolves.
    A cube is included only where its path is on the write surface and a plan
    writing it would pass; otherwise it is proposed on the same checks.
    """

    from . import catalog as catalog_mod
    from .authoring import editing_surface, refusals_for, validate_plan

    ready = [d for d, body in built if body is not None and d.content is not None]
    if not ready:
        return
    prospective = dict(documents)
    for definition in ready:
        prospective[definition.path or ""] = definition.content or ""
    edited = {d.path or "" for d in ready}
    validated, refusals = refusals_for(prospective, edited)
    by_file: dict[str, list[str]] = {}
    for diagnostic in refusals:
        by_file.setdefault(diagnostic.file, []).append(diagnostic.render())

    catalog = catalog_mod.semantic_catalog(
        validated,
        catalog_mod.resolution(validated, layer.connector),
        data_source=layer.data_source,
    )
    catalogued = {m.name for m in catalog.semantic_models}
    surface = set(editing_surface(layer))

    for definition in ready:
        problems = by_file.get(definition.path or "")
        if problems:
            definition.status = "failed"
            definition.reason = "Cube's model rules refuse it: " + "; ".join(problems)
            definition.validation["cube"] = {
                "result": "failed",
                "detail": "; ".join(problems),
            }
            continue
        definition.validation["cube"] = {"result": "passed"}
        if definition.name not in catalogued:
            definition.status = "failed"
            definition.reason = "the derived cube did not read back as a catalog model"
            definition.validation["catalog"] = {"result": "failed"}
            continue
        definition.validation["catalog"] = {"result": "passed"}
        if definition.path not in surface:
            definition.status = "proposed"
            definition.next = _adopt(layer, definition)
            continue
        definition.status = "included"

    _hold_back_dependents(layer, built)
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


def _hold_back_dependents(
    layer: CubeSemanticLayer,
    built: list[tuple[DerivedDefinition, dict[str, Any] | None]],
) -> None:
    """Propose, rather than include, a cube that joins one only proposed.

    A cube derived in the same batch may be the join target of another, and a
    plan writing the joining cube alone would leave a join to a cube no
    configured file declares. Followed to a fixed point, so a chain of joins is
    held back whole.
    """

    by_name = {d.name: (d, body) for d, body in built if body is not None}
    changed = True
    while changed:
        changed = False
        for definition, body in by_name.values():
            if definition.status != "included" or body is None:
                continue
            waiting = sorted(
                join["name"]
                for join in body.get("joins") or []
                if join["name"] in by_name
                and by_name[join["name"]][0].status != "included"
            )
            if not waiting:
                continue
            definition.status = "proposed"
            definition.notes.append(
                f"joins {', '.join(waiting)}, derived in this plan and not yet on "
                "the write surface, so it is proposed with it rather than written "
                "alone; adopt both"
            )
            paths = [by_name[name][0].path or "" for name in waiting]
            listed = ", ".join(dict.fromkeys([*layer.files, *paths]))
            definition.next = (
                f"add {', '.join(paths)} to semantic.cube.files with `dex config "
                f"set semantic.cube.files '[{listed}]'`, then re-run this plan to "
                "include both"
            )
            changed = True


def _adopt(layer: CubeSemanticLayer, definition: DerivedDefinition) -> str:
    listed = ", ".join([*layer.files, definition.path or ""])
    return (
        f"{definition.path} is not under semantic.cube.files, the files dex may "
        f"write: run `dex config set semantic.cube.files '[{listed}]'`, then pass "
        "data.semantic_proposal.definitions as the edits file to `semantic cube "
        "define`, or re-run this plan to include it"
    )


def _edit(definition: DerivedDefinition) -> Any:
    from ..transform.plans import EditKind, PlanEdit

    return PlanEdit(
        path=definition.path or "",
        kind=EditKind.SEMANTIC_DOCUMENT,
        new_content=definition.content,
    )


def dump_file(body: Mapping[str, Any]) -> str:
    """One cube as a whole model file."""

    return yaml.safe_dump({"cubes": [dict(body)]}, sort_keys=False, allow_unicode=True)


# --- the envelope --------------------------------------------------------------------


def _proposal(definitions: Mapping[str, DerivedDefinition]) -> dict[str, Any]:
    proposed = [d for d in definitions.values() if d.status == "proposed"]
    included = [d for d in definitions.values() if d.status == "included"]
    data: dict[str, Any] = {
        "vendor": "cube",
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
                "add each proposed path to semantic.cube.files with `dex config "
                "set`, then pass data.semantic_proposal.definitions as the edits "
                "file to `semantic cube define`, or re-run this plan to include them"
            )
        )
    if included:
        notes.append(
            "included cubes are stored as a plan of their own on the Cube layer "
            "(data.semantic_proposal.plan_id), beside the dbt plan; apply both"
        )
    data["notes"] = notes
    return data


def _model_data(definition: DerivedDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {"status": definition.status}
    if definition.content is not None:
        out["cube"] = definition.name
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
                f"cube '{definition.model}' was derived from the declaration for "
                f"{definition.path}; see data.semantic_proposal"
            )
        elif definition.status in ("failed", "unvalidated"):
            out.append(
                f"cube for '{definition.model}' was derived and not offered "
                f"({definition.status}): {definition.reason}"
            )
    return out


# --- annotate: declarations onto a cube the layer or payload defines -----------------


def annotate(
    layer: CubeSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """Render each declaration into the cube it names.

    The declaration wins field by field. Only the named cube's entry is
    re-serialized, and it is spliced back where it was, so every other cube,
    view and comment in its file keeps its bytes; a cube with nothing to change
    keeps its own bytes too. A declaration naming no cube is ``failed``, and the
    native route refuses the plan for it.
    """

    from ..semantic_source import DerivedDefinition, SemanticDerivationResult
    from ..transform.plans import EditKind, PlanEdit

    texts = dict(request.documents)
    entries = _cubes(texts)
    definitions: dict[str, DerivedDefinition] = {}
    changed_paths: list[str] = []
    for declaration in request.declarations:
        definition = DerivedDefinition(model=declaration.model, name=declaration.model)
        definitions[declaration.model] = definition
        found = [
            (p, k) for p, entry, k in entries if entry.get("name") == declaration.model
        ]
        if not found:
            definition.status = "failed"
            definition.reason = f"no configured cube is named '{declaration.model}'"
            continue
        path, kind = found[0]
        definition.path = path
        if kind == "views":
            definition.status = "failed"
            definition.reason = (
                f"'{declaration.model}' is a view, which reads its cubes' members "
                "and has no table of its own; declare the cubes it reads instead"
            )
            continue
        located = locate(texts[path], declaration.model)
        if located is None:
            definition.status = "failed"
            definition.reason = (
                f"the cube '{declaration.model}' in {path} is not written as a "
                "block-style list item dex can rewrite on its own, so nothing in "
                "the file was changed"
            )
            continue
        start, end, indent, entry = located
        changed, carried, reasons = _annotated(entry, declaration, texts)
        definition.carried, definition.not_carried = account(
            declaration.declared,
            carried,
            reasons,
            default="the cube has no member of that name",
        )
        definition.status = "included"
        if changed:
            texts[path] = texts[path][:start] + _item(entry, indent) + texts[path][end:]
            if path not in changed_paths:
                changed_paths.append(path)
            entries = _cubes(texts)
        else:
            definition.notes.append("the cube already states every declared fact")
        definition.content = texts[path]
    edits = [
        PlanEdit(path=path, kind=EditKind.SEMANTIC_DOCUMENT, new_content=texts[path])
        for path in changed_paths
    ]
    for definition in definitions.values():
        if definition.path in texts and definition.status == "included":
            definition.content = texts[definition.path]
    return SemanticDerivationResult(
        vendor="cube",
        edit_target="semantic",
        edits=edits,
        definitions=definitions,
        proposal=None,
        warnings=[],
    )


def _item(entry: Mapping[str, Any], indent: int) -> str:
    dumped = yaml.safe_dump([dict(entry)], sort_keys=False, allow_unicode=True)
    pad = " " * indent
    return "".join(
        pad + line if line.strip() else line for line in dumped.splitlines(True)
    )


def _set(entry: dict[str, Any], key: str, value: Any) -> None:
    """Set ``key`` in the spelling the author already uses, snake_case if none."""

    camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key)
    entry[camel if camel in entry and key not in entry else key] = value


def _drop(entry: dict[str, Any], key: str) -> None:
    camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key)
    entry.pop(key, None)
    entry.pop(camel, None)


def _annotated(
    entry: dict[str, Any],
    declaration: SemanticDeclaration,
    texts: Mapping[str, str],
) -> tuple[bool, list[str], dict[str, str]]:
    """Apply the declared facts to ``entry`` in place; whether anything moved."""

    declared = declaration.declared
    before = yaml.safe_dump(entry, sort_keys=False)
    carried: list[str] = []
    reasons: dict[str, str] = {}

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
        _merge_meta(entry, meta)
        carried.extend(meta)

    dimensions = [d for d in entry.get("dimensions") or [] if isinstance(d, dict)]
    by_dimension = {d.get("name"): d for d in dimensions}
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    if grain:
        missing = [g for g in grain if g not in by_dimension]
        if missing:
            reasons["grain"] = (
                f"the cube has no dimension {', '.join(missing)}, so the grain was "
                "not written"
            )
        else:
            for name, dimension in by_dimension.items():
                if name in grain:
                    _set(dimension, "primary_key", True)
                elif _get(dimension, "primary_key"):
                    _drop(dimension, "primary_key")
            carried.append("grain")

    measures = entry.get("measures")
    if not isinstance(measures, list):
        measures = []
    by_measure = {m.get("name"): m for m in measures if isinstance(m, dict)}
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, dict):
            continue
        role = fields.get("role")
        member = by_dimension.get(name) or by_measure.get(name)
        if role == "measure" and member is None:
            limitations: list[str] = []
            measure = _measure(name, fields, limitations, reasons)
            if measure is not None:
                measures.append(measure)
                by_measure[name] = measure
                carried.append(f"columns.{name}.aggregation")
                carried.extend(_carried_member(name, fields, with_role=True))
            continue
        if member is None:
            reasons[f"columns.{name}"] = "the cube has no member of that name"
            continue
        if role == "measure":
            if name in by_dimension and name not in by_measure:
                reasons[f"columns.{name}.aggregation"] = (
                    f"'{name}' is a dimension of the cube, and a measure of the same "
                    "name would collide with it"
                )
            elif name in by_measure:
                carried.append(f"columns.{name}.aggregation")
        if isinstance(fields.get("description"), str) and fields["description"]:
            member["description"] = fields["description"]
        column_meta = native_column_metadata(fields)
        if column_meta:
            _merge_meta(member, column_meta)
        carried.extend(_carried_member(name, fields, with_role=bool(role)))
        if role == "foreign_key" and fields.get("references"):
            why = _annotate_join(entry, name, fields["references"], texts)
            if why is None:
                carried.append(f"columns.{name}.references")
            else:
                reasons[f"columns.{name}.references"] = why
    if measures and "measures" not in entry:
        entry["measures"] = measures
    return yaml.safe_dump(entry, sort_keys=False) != before, carried, reasons


def _merge_meta(member: dict[str, Any], facts: Mapping[str, Any]) -> None:
    if not isinstance(member.get("meta"), dict):
        member["meta"] = {}
    dex = member["meta"].setdefault(DEX_META_KEY, {})
    if not isinstance(dex, dict):
        dex = member["meta"][DEX_META_KEY] = {}
    dex.update(facts)


def _annotate_join(
    entry: dict[str, Any], column: str, references: str, texts: Mapping[str, str]
) -> str | None:
    """Add the declared join, or say why not. An authored join to the same
    cube is the author's and is left as it is."""

    parent, _, parent_column = references.rpartition(".")
    names = {e.get("name") for _p, e, kind in _cubes(texts) if kind == "cubes"}
    if not parent or parent not in names:
        return f"references '{references}' names no configured cube"
    joins = entry.get("joins")
    if not isinstance(joins, list):
        joins = entry["joins"] = []
    if any(isinstance(j, dict) and j.get("name") == parent for j in joins):
        return None
    joins.append(
        {
            "name": parent,
            "sql": f"{{CUBE}}.{column} = {{{parent}}}.{parent_column}",
            "relationship": "many_to_one",
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
