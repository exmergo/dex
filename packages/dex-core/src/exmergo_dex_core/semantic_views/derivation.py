"""A logical table derived from a declared mart, and declarations annotated onto
an existing one (#1292).

The Snowflake counterpart of the MetricFlow (#499), SLayer (#927), Ossie (#500),
Cube (#911) and Metric View (#1258) derivations, behind the same
:class:`~..semantic_source.SemanticDerivationSource` hook. A declaration states
most of what a logical table needs, and a Semantic View has a native place for
most of it:

- the mart's relation: ``base_table {database, schema, table}``. The reader
  refuses an incomplete one, so a mart whose relation is not known in full (no
  sibling table to follow, no compiled manifest) is not derived.
- ``grain``: ``primary_key``, single-column or composite.
- a ``time`` role: a ``time_dimension``; every other role but ``measure``: a
  ``dimension``. Each is ``{name: <column>, expr: <column>}``.
- ``measure``: a ``fact`` of the same name (the row-level value), and, with a
  declared ``aggregation``, a table metric ``<column>_<agg>`` over it. Members
  share one namespace per table, so the suffix keeps a metric from shadowing its
  fact.
- ``semi_additive``: the metric's ``non_additive_dimensions`` on the table's
  first time dimension, read on its latest value only (ascending, NULLs first:
  Snowflake reads the last value in that order), so a balance is never summed
  across time.
- ``foreign_key`` with ``references``: a relationship to the parent's logical
  table, written only where that table declares the referenced column as its
  primary key or a unique key, which is what Snowflake requires of a
  relationship's right side.
- ``description``: the table's and each member's ``description``; the
  population as one sentence at the end of the table's.

**A Semantic View has no free-form metadata key.** ``null_rule``, ``unit``,
``additivity`` (beyond ``non_additive_dimensions``) and ``assumptions`` are named
in ``not_carried``, never written into ``custom_instructions``, which is Cortex
Analyst's prompt rather than a place for dex's facts. Declarations carry no
label, so no ``synonyms`` are derived; authored ones are kept.

**A population is never a filter.** The table sits on the mart, whose SQL
already enforces it, and a Semantic View's table ``filters`` are Cortex
Analyst's named filters, not a row restriction.

**Where it goes.** Relationships join the logical tables of one view, so a
derived table is spliced into the configured file whose tables sit on the
parents its foreign keys reference, or, with no foreign key and exactly one
configured file, into that one. It is appended as a list item, so every other
byte is kept, and *included* as a plan of its own beside the mart's. Where the
parents span files, or none holds them, it is derived as a view of its own,
``<dir>/<mart>.yaml``: included where that path is already configured, otherwise
*proposed* with the `dex config set` that adopts it. Both pass the same checks.

**Annotate.** ``semantic snowflake plan`` with ``declarations`` renders each one
into the logical table it names (``view.table``, or a table name unique across
the layer), splicing only what changes: the table's description, a member's
description, an appended metric, an appended relationship. Every other byte of
the file, comments inside the table included, is kept (:mod:`.splice`).

Nothing here opens a Snowflake connection or deploys a view.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from ..semantic_declarations import (
    account,
    placed_table,
    sibling_prefix,
    with_population,
)
from ..yaml_items import append_item
from .loader import Dumper, dump_yaml, read_yaml
from .splice import (
    UnwritableError,
    append_member,
    set_member_scalar,
    set_table_scalar,
)

if TYPE_CHECKING:
    from ..semantic_source import (
        DeclaredReference,
        DerivedDefinition,
        SemanticDeclaration,
        SemanticDerivationRequest,
        SemanticDerivationResult,
    )
    from .project import SemanticViewSemanticLayer

VENDOR = "snowflake"

#: Declared aggregation (lowered, spaces and dashes as underscores) to the
#: Snowflake aggregate it means and the suffix its metric is named with. Strict,
#: as the other derivations' tables are: anything else is named, never mapped.
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
    "median": ("MEDIAN({})", "median"),
}

#: Aggregations that add values up, which ``non_additive`` contradicts.
_SUMMING = frozenset({"sum"})

#: Roles that become a dimension (``time`` a time dimension). ``measure``
#: becomes a fact instead.
_DIMENSION_ROLES = frozenset({"key", "foreign_key", "dimension", "time", "attribute"})

#: Where a derived view goes when the configured files share no directory.
DEFAULT_DIR = "semantic_views"

#: How dex begins the population sentence it writes into a description.
_POPULATION = "Population: "

#: Why a fact with no Semantic View construct is not carried.
NO_METADATA = (
    "a Semantic View has no free-form metadata key, and dex does not write its "
    "facts into `custom_instructions`, which is Cortex Analyst's prompt"
)
NO_UNIT = "a Semantic View has no unit or format key, so the unit is not written"
NO_ADDITIVITY = (
    "a Semantic View states additivity only as `non_additive_dimensions` on a "
    "semi-additive metric; any other additivity has nowhere to go"
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def derive(
    layer: SemanticViewSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """The logical table each declared mart states, validated, never written."""

    from ..semantic_source import SemanticDerivationResult

    if request.mode == "annotate":
        return annotate(layer, request)

    texts = dict(request.documents)
    prefix = sibling_prefix(
        (_base_table(table) for _path, raw in _views(texts) for table in _tables(raw)),
        request.project_identifiers.values(),
    )
    directory, suffix = placement(layer)

    built: list[DerivedDefinition] = []
    changed: list[str] = []
    for declaration in request.declarations:
        definition = _build(
            declaration,
            texts,
            layer=layer,
            prefix=prefix,
            directory=directory,
            suffix=suffix,
        )
        built.append(definition)
        if definition.status == "included" and definition.path not in changed:
            changed.append(definition.path or "")

    for definition in built:
        if definition.status == "included":
            definition.content = texts[definition.path or ""]

    _validate(layer, texts, built, changed)
    definitions = {d.model: d for d in built}
    included = [
        p for p in changed if any(d.path == p and d.status == "included" for d in built)
    ]
    return SemanticDerivationResult(
        vendor=VENDOR,
        edit_target="semantic",
        edits=[_edit(path, texts[path]) for path in included],
        definitions=definitions,
        proposal=_proposal(layer, definitions),
        warnings=_warnings(definitions),
    )


# --- building one logical table ------------------------------------------------------


def _build(
    declaration: SemanticDeclaration,
    texts: dict[str, str],
    *,
    layer: SemanticViewSemanticLayer,
    prefix: str | None,
    directory: str,
    suffix: str,
) -> DerivedDefinition:
    """Build one logical table and place it: spliced into ``texts`` when
    included."""

    from ..semantic_source import DerivedDefinition

    model = declaration.model
    declared = declaration.declared
    definition = DerivedDefinition(model=model, name=model)
    notes, limitations = definition.notes, definition.limitations
    carried: list[str] = []
    reasons = _metadata_reasons(declared)
    identifier = declaration.identifier or model

    def finish(status: str, reason: str) -> DerivedDefinition:
        """Close the definition with ``status`` and ``reason``, nothing carried."""
        definition.status = status  # type: ignore[assignment]
        definition.reason = reason
        definition.carried, definition.not_carried = account(
            declared, [], {}, default=reason
        )
        return definition

    views = _views(texts)
    # The relation the table would read, so an existing table is matched on its
    # whole base_table: two marts sharing a table name in different schemas are
    # different marts.
    source, placed = placed_table(identifier, declaration.relation, prefix)
    for path, raw in views:
        for table in _tables(raw):
            if _same_relation(table, source, identifier):
                return finish(
                    "skipped",
                    f"{model} already has a logical table "
                    f"('{raw.get('name')}.{table.get('name')}' in {path}); an "
                    "authored table is not overwritten or duplicated",
                )
    if not _IDENTIFIER.match(model):
        return finish(
            "failed",
            f"'{model}' is not a plain identifier, so dex does not name a logical "
            "table after it",
        )

    # Where it goes: the file holding the parents its foreign keys name.
    parents: dict[str, tuple[str, dict[str, Any]]] = {}
    for reference in declaration.references:
        found = _parent_table(views, reference)
        if found is not None:
            parents.setdefault(reference.column, found)
    homes = sorted({path for path, _table in parents.values()})
    target: str | None = None
    if len(homes) == 1:
        target = homes[0]
    elif not homes and not declaration.references and len(layer.files) == 1 and views:
        target = views[0][0]
    elif len(homes) > 1:
        notes.append(
            "its foreign keys reference logical tables in different files "
            f"({', '.join(homes)}), and a relationship joins the tables of one view "
            "only, so it is derived as a view of its own with no relationship"
        )
    elif declaration.references:
        notes.append(
            "no configured Semantic View holds a logical table its foreign keys "
            "reference, so it is derived as a view of its own"
        )

    if target is not None:
        holder = dict(views)[target]
        if any(
            str(t.get("name", "")).lower() == model.lower() for t in _tables(holder)
        ):
            return finish(
                "failed",
                f"the logical table name '{model}' is already declared in {target}; "
                "dex does not rename a table",
            )

    parts = _parts(source)
    if len(parts) != 3:
        return finish(
            "failed",
            f"the mart's relation is known only as '{source}', and a logical "
            "table's base_table needs its database, schema and table. Compile or "
            "build the dbt project so the manifest names the relation, or give "
            "another of this project's models a logical table to follow",
        )
    if placed == "siblings":
        notes.append(
            f"base_table is {source}: the database and schema the logical tables "
            "on this project's other models already read"
        )
    else:
        notes.append(
            f"base_table is {source}, the relation the compiled dbt manifest names, "
            "which is whatever target last compiled the project (often dev). Point "
            "it at the table the view should read before deploying"
        )

    columns: dict[str, dict[str, Any]] = {
        name: dict(fields)
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    dimensions: list[dict[str, Any]] = []
    times: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    #: Each metric, its source column and spec, and whether it is semi-additive.
    pending: list[tuple[dict[str, Any], str, Mapping[str, Any], bool]] = []
    for name in dict.fromkeys([*grain, *columns]):
        spec = columns.get(name, {})
        role = spec.get("role")
        if not _IDENTIFIER.match(name):
            reasons[f"columns.{name}"] = (
                f"'{name}' is not a plain identifier, so it is not written as a "
                "member; add it by hand with a quoted expression"
            )
            continue
        if role == "measure" and name not in grain:
            facts.append(_member(name, spec))
            carried.extend(_carried_member(name, spec))
            metric = _metric(model, name, spec, limitations, reasons)
            if metric is not None:
                pending.append((metric[0], name, spec, metric[1]))
            continue
        if name not in grain and role not in _DIMENSION_ROLES:
            reasons[f"columns.{name}"] = (
                "a column with no role, so the declaration does not say it belongs "
                "in the semantic layer"
            )
            continue
        (times if role == "time" else dimensions).append(_member(name, spec))
        carried.extend(_carried_member(name, spec))
        if role == "measure":
            reasons[f"columns.{name}.aggregation"] = (
                "a grain column is a dimension of the table; declare the measure on "
                "another column or add the metric by hand"
            )

    names = {m["name"].lower() for m in [*dimensions, *times, *facts]}
    metrics: list[dict[str, Any]] = []
    for metric, column, _spec, semi in pending:
        if metric["name"].lower() in names:
            reasons[f"columns.{column}.aggregation"] = (
                f"the metric name '{metric['name']}' is already a member's, and a "
                "logical table's members share one namespace"
            )
            continue
        if semi:
            if not times:
                reasons[f"columns.{column}.aggregation"] = reasons[
                    f"columns.{column}.additivity"
                ] = (
                    "semi_additive, and the table has no time dimension to hold it "
                    "non-additive over; declare a `time` column"
                )
                limitations.append(
                    f"{column}: semi_additive with no time dimension, so no metric "
                    "is derived"
                )
                continue
            metric["non_additive_dimensions"] = latest_snapshot(model, times[0]["name"])
            carried.append(f"columns.{column}.additivity")
            notes.append(
                f"{metric['name']}: semi-additive, so it reads only the rows on the "
                f"latest {times[0]['name']}: a balance is never summed across time"
            )
        names.add(metric["name"].lower())
        metrics.append(metric)
        carried.append(f"columns.{column}.aggregation")

    body: dict[str, Any] = {"name": model}
    description = with_population(
        declared.get("description"), declared.get("population")
    )
    if description:
        body["description"] = description
    if declared.get("description"):
        carried.append("description")
    if declared.get("population") and description:
        carried.append("population")
        notes.append(
            "the population is carried as the last sentence of the table's "
            "description and never as a filter: the mart's SQL already enforces it"
        )
    body["base_table"] = {
        "database": parts[0],
        "schema": parts[1],
        "table": parts[2],
    }
    if grain:
        body["primary_key"] = {"columns": list(grain)}
        carried.append("grain")
    else:
        limitations.append(
            "the declaration states no grain, so the table declares no primary key, "
            "and no relationship can point at it"
        )
    if dimensions:
        body["dimensions"] = dimensions
    if times:
        body["time_dimensions"] = times
    if facts:
        body["facts"] = facts
    if metrics:
        body["metrics"] = metrics
    if not (dimensions or times or facts or metrics):
        return finish(
            "failed",
            "the declaration names no column with a role, so the logical table "
            "would have no member",
        )

    relationships = _relationships(
        declaration, target, parents, notes, reasons, carried
    )
    notes.append(
        "metrics beyond declared aggregations are not derived: a metric is what a "
        "column's declared aggregation states, anything more is a business "
        "definition, and that stays the author's"
    )
    definition.carried, definition.not_carried = account(
        declared,
        carried,
        reasons,
        default="not derived; the notes and limitations say why",
    )

    if target is None:
        definition.path = f"{directory}/{model}{suffix}"
        definition.name = f"{model}.{model}"
        if definition.path in texts:
            return finish(
                "failed",
                f"{definition.path} already exists; a derived view is written to a "
                "file of its own and never over another",
            )
        if any(str(raw.get("name", "")).lower() == model.lower() for _p, raw in views):
            return finish(
                "failed",
                f"a Semantic View named '{model}' is already configured; dex does "
                "not rename one",
            )
        definition.content = dump_yaml({"name": model, "tables": [body]})
        if definition.path in layer.files:
            texts[definition.path] = definition.content
            definition.status = "included"
        else:
            definition.status = "proposed"
            definition.next = _adopt(layer, [definition.path])
        return definition

    holder_name = str(dict(views)[target].get("name"))
    text: str | None = append_item(texts[target], "tables", body, dumper=Dumper)
    for relationship in relationships:
        text = (
            append_item(text, "relationships", relationship, dumper=Dumper)
            if text
            else None
        )
    if text is None:
        return finish(
            "failed",
            f"the lists in {target} are not written block-style, so dex cannot add a "
            "logical table without rewriting lines it did not author",
        )
    texts[target] = text
    definition.path = target
    definition.name = f"{holder_name}.{model}"
    definition.content = text
    definition.status = "included"
    return definition


def _member(name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """A dimension, time dimension or fact over the column ``name``."""
    member: dict[str, Any] = {"name": name, "expr": name}
    if isinstance(spec.get("description"), str) and spec["description"]:
        member["description"] = spec["description"]
    return member


def _carried_member(name: str, spec: Mapping[str, Any]) -> list[str]:
    """The declared column facts a rendered member carries."""
    out = [f"columns.{name}.role"] if spec.get("role") else []
    if isinstance(spec.get("description"), str) and spec["description"]:
        out.append(f"columns.{name}.description")
    return out


def _metadata_reasons(declared: Mapping[str, Any]) -> dict[str, str]:
    """``not_carried`` reasons for every fact a Semantic View has no key for."""

    reasons: dict[str, str] = {}
    if declared.get("assumptions"):
        reasons["assumptions"] = NO_METADATA
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, Mapping):
            continue
        if fields.get("null_rule") not in (None, ""):
            reasons[f"columns.{name}.null_rule"] = NO_METADATA
        if fields.get("unit") not in (None, ""):
            reasons[f"columns.{name}.unit"] = NO_UNIT
        if fields.get("additivity") not in (None, ""):
            reasons[f"columns.{name}.additivity"] = NO_ADDITIVITY
    return reasons


def _metric(
    table: str,
    name: str,
    spec: Mapping[str, Any],
    limitations: list[str],
    reasons: dict[str, str],
    *,
    reads: str | None = None,
) -> tuple[dict[str, Any], bool] | None:
    """The metric a declared aggregation states, and whether it is semi-additive;
    ``None`` with the reason where there is none.

    ``reads`` is what the expression aggregates: the fact ``table.name`` by
    default.
    """

    declared = spec.get("aggregation")
    additivity = spec.get("additivity")
    why: str | None = None
    mapped: tuple[str, str] | None = None
    if not isinstance(declared, str) or not declared.strip():
        why = "a measure with no declared aggregation; declare `aggregation`"
    else:
        mapped = AGGREGATIONS.get(re.sub(r"[\s-]+", "_", declared.strip().lower()))
        if mapped is None:
            known = ", ".join(sorted({s for _e, s in AGGREGATIONS.values()}))
            why = (
                f"aggregation '{declared}' has no Snowflake aggregate dex maps "
                f"({known})"
            )
        elif additivity == "non_additive" and mapped[1] in _SUMMING:
            why = (
                f"declared non_additive with aggregation '{declared}', which adds "
                "it up; the two disagree. Fix the declaration"
            )
    if why is not None or mapped is None:
        if declared:
            limitations.append(f"{name}: {why}, so no metric is derived")
        reasons[f"columns.{name}.aggregation"] = why or ""
        return None
    template, suffix = mapped
    metric: dict[str, Any] = {
        "name": f"{name}_{suffix}",
        "expr": template.format(reads or f"{table}.{name}"),
    }
    if isinstance(spec.get("description"), str) and spec["description"]:
        metric["description"] = spec["description"]
    return metric, additivity == "semi_additive"


def latest_snapshot(table: str, dimension: str) -> list[dict[str, str]]:
    """The ``non_additive_dimensions`` that read a semi-additive metric on the
    latest value of ``dimension`` only.

    Snowflake sorts by the dimension as stated and reads the rows of the **last**
    value in that order (checked on a live account, #1292): ``descending`` reads
    the earliest date, and with ``null_order: last`` (the default) a NULL date is
    last and wins. Ascending with NULLs first makes the last value the latest
    real date.
    """

    return [
        {
            "table": table,
            "dimension": dimension,
            "sort_direction": "ascending",
            "null_order": "first",
        }
    ]


def _relationships(
    declaration: SemanticDeclaration,
    target: str | None,
    parents: Mapping[str, tuple[str, dict[str, Any]]],
    notes: list[str],
    reasons: dict[str, str],
    carried: list[str],
) -> list[dict[str, Any]]:
    """One relationship per declared foreign key that can be written."""

    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for reference in declaration.references:
        column = reference.column
        parent = parents.get(column)
        why: str | None = None
        if target is None:
            why = (
                "the table is derived as a view of its own, and a relationship "
                "joins the logical tables of one view only"
            )
        elif parent is None or parent[0] != target:
            why = (
                f"no logical table in {target} sits on {reference.parent_model}, so "
                "there is no table to relate to"
            )
        elif not reference.parent_column:
            why = f"references names no column of {reference.parent_model}"
        elif not _IDENTIFIER.match(column) or not _IDENTIFIER.match(
            reference.parent_column
        ):
            why = "a quoted column is not written into a relationship by dex"
        elif not _keyed(parent[1], [reference.parent_column]):
            why = (
                f"logical table '{parent[1].get('name')}' does not declare "
                f"{reference.parent_column} as its primary key or a unique key, and "
                "Snowflake relates a table only to a key of the table it points at"
            )
        if why is not None:
            notes.append(f"{column}: {why}")
            reasons[f"columns.{column}.references"] = why
            continue
        if parent is None or not reference.parent_column:  # pragma: no cover
            continue
        right = str(parent[1].get("name"))
        name = f"{declaration.model}_to_{right}"
        if name.lower() in used:
            name = f"{name}_{column}"
        used.add(name.lower())
        out.append(
            {
                "name": name,
                "left_table": declaration.model,
                "right_table": right,
                "relationship_columns": [
                    {"left_column": column, "right_column": reference.parent_column}
                ],
            }
        )
        carried.append(f"columns.{column}.references")
        notes.append(
            f"{column}: relates to logical table '{right}' on its key "
            f"{reference.parent_column} (many to one)"
        )
    return out


# --- what the layer already holds ----------------------------------------------------


def _views(texts: Mapping[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Every configured file's parsed view: ``(path, body)``."""

    out: list[tuple[str, dict[str, Any]]] = []
    for path, text in sorted(texts.items()):
        try:
            parsed = read_yaml(text)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            out.append((path, parsed))
    return out


def _tables(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A view's logical tables, as written."""
    return [t for t in raw.get("tables") or [] if isinstance(t, dict)]


def _base_table(table: Mapping[str, Any]) -> str | None:
    """A logical table's base table as ``database.schema.table``, or None."""
    base = table.get("base_table")
    if not isinstance(base, Mapping):
        return None
    parts = [base.get(k) for k in ("database", "schema", "table")]
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return ".".join(str(p) for p in parts)


def _table_part(table: Mapping[str, Any]) -> str:
    """The table part of a logical table's base table, lowered; ``""`` if none."""
    base = table.get("base_table")
    if isinstance(base, Mapping) and isinstance(base.get("table"), str):
        return str(base["table"]).strip('"').lower()
    return ""


def _parts(relation: str) -> list[str]:
    """A dotted relation's parts, unquoted."""
    return [part.strip().strip('"`') for part in relation.split(".") if part.strip()]


def _parent_table(
    views: list[tuple[str, dict[str, Any]]], reference: DeclaredReference
) -> tuple[str, dict[str, Any]] | None:
    """The file and logical table a foreign key's parent sits on, if any.

    A table on the parent's whole relation (the one the manifest names) wins.
    Otherwise a table on its table name, or named after the parent model, is
    used only where every such table reads one relation: two candidates on
    different relations are ambiguous, and no relationship is guessed.
    """

    wanted = (reference.parent_identifier or reference.parent_model).lower()
    parent = reference.parent_model.lower()
    candidates: list[tuple[str, dict[str, Any]]] = [
        (path, table)
        for path, raw in views
        for table in _tables(raw)
        if _table_part(table) == wanted or str(table.get("name", "")).lower() == parent
    ]
    if reference.parent_relation:
        exact = [
            c
            for c in candidates
            if _same_relation(c[1], reference.parent_relation, wanted)
            and len(_parts(reference.parent_relation)) == 3
        ]
        if exact:
            return exact[0]
    relations = {(_base_table(t) or "").lower() for _p, t in candidates}
    if len(candidates) == 1 or (candidates and len(relations) == 1):
        return candidates[0]
    return None


def _same_relation(table: Mapping[str, Any], relation: str, identifier: str) -> bool:
    """Whether a logical table reads ``relation``: its whole three-part
    base_table where ``relation`` has three parts, its table name otherwise."""

    parts = [p.lower() for p in _parts(relation)]
    base = _base_table(table)
    if len(parts) == 3 and base is not None:
        return [p.lower() for p in _parts(base)] == parts
    return _table_part(table) == identifier.lower()


def _keyed(table: Mapping[str, Any], columns: list[str]) -> bool:
    """Whether ``table`` declares exactly ``columns`` as its primary key or a
    unique key, ignoring case and order."""

    wanted = sorted(c.lower() for c in columns)
    keys: list[Any] = []
    primary = table.get("primary_key")
    if isinstance(primary, Mapping):
        keys.append(primary.get("columns"))
    keys.extend(
        unique.get("columns") if isinstance(unique, Mapping) else unique
        for unique in table.get("unique_keys") or []
    )
    return any(
        isinstance(key, list) and sorted(str(c).lower() for c in key) == wanted
        for key in keys
    )


def placement(layer: SemanticViewSemanticLayer) -> tuple[str, str]:
    """The directory and suffix a derived view takes: the ones the configured
    files share, else ``semantic_views`` and ``.yaml``."""

    parents = {str(PurePosixPath(name).parent) for name in layer.files}
    suffixes = {PurePosixPath(name).suffix for name in layer.files}
    directory = DEFAULT_DIR
    if len(parents) == 1 and (only := next(iter(parents))) not in ("", "."):
        directory = only
    suffix = next(iter(suffixes)) if len(suffixes) == 1 else ".yaml"
    return directory, suffix


# --- validation ----------------------------------------------------------------------


def _validate(
    layer: SemanticViewSemanticLayer,
    texts: Mapping[str, str],
    built: list[DerivedDefinition],
    changed: list[str],
) -> None:
    """Hold every derived table to the checks a hand-authored one meets.

    The reader's rules, strict rules included, over the whole prospective set;
    a read back through the catalog; then, for what is included, the plan
    validation authoring runs.
    """

    from . import catalog as catalog_mod
    from .authoring import refusals_for, validate_plan

    ready = [d for d in built if d.status in ("included", "proposed") and d.content]
    if not ready:
        return
    prospective = dict(texts)
    for definition in ready:
        if definition.status == "proposed":
            prospective[definition.path or ""] = definition.content or ""
    validated, refusals = refusals_for(prospective, {d.path or "" for d in ready})
    by_file: dict[str, list[str]] = {}
    for diagnostic in refusals:
        by_file.setdefault(diagnostic.file, []).append(diagnostic.render())
    catalog = catalog_mod.semantic_catalog(
        validated, catalog_mod.resolution(validated, layer.connector)
    )
    catalogued = {m.name.lower() for m in catalog.semantic_models}

    for definition in ready:
        problems = by_file.get(definition.path or "")
        if problems:
            definition.status = "failed"
            definition.reason = "the Semantic View rules refuse it: " + "; ".join(
                problems
            )
            definition.validation["semantic_view"] = {
                "result": "failed",
                "detail": "; ".join(problems),
            }
            continue
        definition.validation["semantic_view"] = {"result": "passed"}
        if (definition.name or "").lower() not in catalogued:
            definition.status = "failed"
            definition.reason = (
                "the derived logical table did not read back as a catalog model"
            )
            definition.validation["catalog"] = {"result": "failed"}
            continue
        definition.validation["catalog"] = {"result": "passed"}

    included = [d for d in ready if d.status == "included"]
    paths = [p for p in changed if any(d.path == p for d in included)]
    if not paths:
        return
    try:
        validate_plan(layer, [_edit(path, texts[path]) for path in paths], "plan")
    except ValueError as exc:
        for definition in included:
            definition.status = "failed"
            definition.reason = str(exc)
            definition.validation["plan"] = {"result": "failed", "detail": str(exc)}
        return
    for definition in included:
        definition.validation["plan"] = {"result": "passed"}


def _adopt(layer: SemanticViewSemanticLayer, paths: list[str]) -> str:
    """The commands that put ``paths`` on the write surface and author them."""
    listed = ", ".join(dict.fromkeys([*layer.files, *paths]))
    return (
        f"{', '.join(paths)} is not under semantic.snowflake.files, the files dex "
        f"may write: run `dex config set semantic.snowflake.files '[{listed}]'`, "
        "then pass data.semantic_proposal.definitions as the edits file to "
        "`semantic snowflake define`, or re-run this plan to include it"
    )


def _edit(path: str, content: str) -> Any:
    """A whole-file semantic-document edit writing ``content`` to ``path``."""
    from ..transform.plans import EditKind, PlanEdit

    return PlanEdit(path=path, kind=EditKind.SEMANTIC_DOCUMENT, new_content=content)


# --- the envelope --------------------------------------------------------------------


def _proposal(
    layer: SemanticViewSemanticLayer, definitions: Mapping[str, DerivedDefinition]
) -> dict[str, Any]:
    """The ``data.semantic_proposal`` body for the batch."""
    proposed = [d for d in definitions.values() if d.status == "proposed" and d.content]
    included = [d for d in definitions.values() if d.status == "included"]
    data: dict[str, Any] = {
        "vendor": VENDOR,
        "mode": "included" if included else "proposed",
        "definitions": {
            "edits": [
                {"path": d.path, "kind": "semantic_document", "content": d.content}
                for d in proposed
            ]
        },
        "models": {name: _model_data(d) for name, d in definitions.items()},
    }
    notes = [
        "dex writes the Semantic View YAML and never deploys it: check it with "
        "`CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML('<database>.<schema>', $$ "
        "<file> $$, TRUE)` (verify_only), then create it without the flag"
    ]
    if proposed:
        data["next"] = (
            proposed[0].next
            if len(proposed) == 1
            else _adopt(layer, [d.path or "" for d in proposed])
        )
    if included:
        notes.append(
            "included tables are stored as a plan of their own on the Semantic View "
            "layer (data.semantic_proposal.plan_id), beside the dbt plan; apply both"
        )
    data["notes"] = notes
    return data


def _model_data(definition: DerivedDefinition) -> dict[str, Any]:
    """One model's entry under ``data.semantic_proposal.models``."""
    out: dict[str, Any] = {"status": definition.status}
    if definition.content is not None:
        out["logical_table"] = definition.name
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
    """One envelope warning per table included, failed or unvalidated."""
    out: list[str] = []
    for definition in definitions.values():
        if definition.status == "included":
            out.append(
                f"logical table '{definition.name}' was derived from the declaration "
                f"for '{definition.model}' into {definition.path}; see "
                "data.semantic_proposal"
            )
        elif definition.status in ("failed", "unvalidated"):
            out.append(
                f"logical table for '{definition.model}' was derived and not offered "
                f"({definition.status}): {definition.reason}"
            )
    return out


# --- annotate: declarations onto a table the layer or payload defines ----------------


def annotate(
    layer: SemanticViewSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """Render each declaration into the logical table it names.

    ``view.table``, or a table name unique across the layer. The declaration
    wins field by field; only what changes is spliced, so every other byte of
    the file keeps its place. A declaration naming no table, or a name two views
    share, is ``failed``, and the native route refuses the plan for it.
    """

    from ..semantic_source import DerivedDefinition, SemanticDerivationResult

    texts = dict(request.documents)
    definitions: dict[str, DerivedDefinition] = {}
    changed: list[str] = []
    for declaration in request.declarations:
        definition = DerivedDefinition(model=declaration.model, name=declaration.model)
        definitions[declaration.model] = definition
        found, why = _resolve_table(texts, declaration.model)
        if found is None:
            definition.status = "failed"
            definition.reason = why
            continue
        path, view_name, table_name = found
        definition.path = path
        definition.name = f"{view_name}.{table_name}"
        try:
            updated, carried, reasons = _annotated(texts[path], table_name, declaration)
        except UnwritableError as exc:
            definition.status = "failed"
            definition.reason = str(exc)
            continue
        definition.carried, definition.not_carried = account(
            declaration.declared,
            carried,
            reasons,
            default="the logical table has no member of that name",
        )
        definition.status = "included"
        if updated != texts[path]:
            texts[path] = updated
            if path not in changed:
                changed.append(path)
        else:
            definition.notes.append("the table already states every declared fact")
    for definition in definitions.values():
        if definition.status == "included" and definition.path in texts:
            definition.content = texts[definition.path]
    return SemanticDerivationResult(
        vendor=VENDOR,
        edit_target="semantic",
        edits=[_edit(path, texts[path]) for path in changed],
        definitions=definitions,
        proposal=None,
        warnings=[],
    )


def _resolve_table(
    texts: Mapping[str, str], model: str
) -> tuple[tuple[str, str, str] | None, str]:
    """``(path, view, table)`` for a declaration's model, or why there is none."""

    lowered = model.lower()
    view_part, dot, table_part = lowered.partition(".")
    found: list[tuple[str, str, str]] = []
    for path, raw in _views(texts):
        view = str(raw.get("name", ""))
        for table in _tables(raw):
            name = str(table.get("name", ""))
            if (dot and view.lower() == view_part and name.lower() == table_part) or (
                not dot and name.lower() == lowered
            ):
                found.append((path, view, name))
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, (
            f"no configured Semantic View declares a logical table named '{model}' "
            "(name it as view.table, or by a table name unique across the layer)"
        )
    listed = ", ".join(f"{v}.{t}" for _p, v, t in found)
    return None, (
        f"'{model}' names a logical table in more than one view ({listed}); name it "
        "as view.table"
    )


def _annotated(
    text: str, table_name: str, declaration: SemanticDeclaration
) -> tuple[str, list[str], dict[str, str]]:
    """``text`` with the declared facts applied to one table, what was carried,
    and why not."""

    declared = declaration.declared
    raw = read_yaml(text)
    view = raw if isinstance(raw, dict) else {}
    table = next(
        (
            t
            for t in _tables(view)
            if str(t.get("name", "")).lower() == table_name.lower()
        ),
        {},
    )
    carried: list[str] = []
    reasons = _metadata_reasons(declared)

    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    if grain:
        if _keyed({"primary_key": table.get("primary_key")}, grain):
            carried.append("grain")
        else:
            reasons["grain"] = (
                "the table's primary key is the author's, and it does not state "
                "this grain; dex does not rewrite a key on annotation"
            )

    if declared.get("description") or declared.get("population"):
        description = with_population(
            declared.get("description")
            or _without_population(table.get("description")),
            declared.get("population"),
        )
        if description and description != table.get("description"):
            text = set_table_scalar(text, table_name, "description", description)
        if declared.get("description"):
            carried.append("description")
        if declared.get("population"):
            carried.append("population")

    members: dict[str, tuple[str, dict[str, Any]]] = {}
    for section in ("dimensions", "time_dimensions", "facts", "metrics"):
        for member in table.get(section) or []:
            if isinstance(member, dict) and isinstance(member.get("name"), str):
                members[member["name"].lower()] = (section, member)
    times = [m for s, m in members.values() if s == "time_dimensions"]

    for name, spec in (declared.get("columns") or {}).items():
        if not isinstance(spec, Mapping):
            continue
        role = spec.get("role")
        found = members.get(name.lower())
        if found is not None:
            section, member = found
            if role:
                carried.append(f"columns.{name}.role")
            wanted = spec.get("description")
            if isinstance(wanted, str) and wanted:
                if member.get("description") != wanted:
                    entry = dict(member)
                    entry["description"] = wanted
                    text = set_member_scalar(
                        text, table_name, section, member["name"], "description", wanted
                    )
                    members[name.lower()] = (section, entry)
                carried.append(f"columns.{name}.description")
        elif role != "measure":
            reasons.setdefault(
                f"columns.{name}", "the logical table has no member of that name"
            )
        if role == "measure" and spec.get("aggregation"):
            text = _annotate_metric(
                text, table_name, name, spec, found, members, times, carried, reasons
            )
        if role == "foreign_key" and isinstance(spec.get("references"), str):
            text, why = _annotate_relationship(
                text, view, table_name, name, spec["references"]
            )
            if why is None:
                carried.append(f"columns.{name}.references")
                # Read the view again, so the next foreign key sees the
                # relationship this one appended and does not reuse its name.
                reread = read_yaml(text)
                view = reread if isinstance(reread, dict) else view
            else:
                reasons[f"columns.{name}.references"] = why
    return text, carried, reasons


def _annotate_metric(
    text: str,
    table: str,
    column: str,
    spec: Mapping[str, Any],
    found: tuple[str, dict[str, Any]] | None,
    members: dict[str, tuple[str, dict[str, Any]]],
    times: list[dict[str, Any]],
    carried: list[str],
    reasons: dict[str, str],
) -> str:
    """``text`` with the metric a declared aggregation states, unless one exists."""

    if found is not None and found[0] == "metrics":
        reasons[f"columns.{column}.aggregation"] = (
            f"'{column}' is a metric of the table, and a metric is not aggregated again"
        )
        return text
    reads = f"{table}.{found[1]['name']}" if found is not None else column
    limitations: list[str] = []
    built = _metric(table, column, spec, limitations, reasons, reads=reads)
    if built is None:
        return text
    metric, semi = built
    existing = members.get(metric["name"].lower())
    if existing is not None:
        authored = _aggregation_of(existing[1]) if existing[0] == "metrics" else None
        expected = metric["name"][len(column) + 1 :]
        if existing[0] != "metrics":
            reasons[f"columns.{column}.aggregation"] = (
                f"the table already has a {existing[0][:-1].replace('_', ' ')} named "
                f"'{metric['name']}'"
            )
        elif authored is None:
            reasons[f"columns.{column}.aggregation"] = (
                f"the metric '{existing[1]['name']}' is not one aggregate over one "
                "input dex can read, so nothing says it states this aggregation; "
                "check it by hand"
            )
        elif authored != expected:
            reasons[f"columns.{column}.aggregation"] = (
                f"the metric '{existing[1]['name']}' aggregates with "
                f"{authored.upper()}, and the declaration says "
                f"{spec.get('aggregation')}; "
                "dex does not rewrite an authored expression, so fix whichever is "
                "wrong"
            )
        else:
            carried.append(f"columns.{column}.aggregation")
        return text
    if semi:
        if not times:
            reasons[f"columns.{column}.aggregation"] = (
                "semi_additive, and the table has no time dimension to hold it "
                "non-additive over"
            )
            return text
        metric["non_additive_dimensions"] = latest_snapshot(table, times[0]["name"])
        carried.append(f"columns.{column}.additivity")
    text = append_member(text, table, "metrics", metric)
    members[metric["name"].lower()] = ("metrics", metric)
    carried.append(f"columns.{column}.aggregation")
    if found is None:
        # The metric is the measure's member: it aggregates the base column.
        carried.append(f"columns.{column}.role")
    return text


def _annotate_relationship(
    text: str, view: Mapping[str, Any], table: str, column: str, references: str
) -> tuple[str, str | None]:
    """``text`` with a relationship to the referenced logical table, or why not.
    An authored relationship on the same columns is the author's and is kept."""

    parent_name, _, parent_column = references.rpartition(".")
    if not parent_name or not parent_column:
        return text, f"references '{references}' does not name a model's column"
    parent = next(
        (
            t
            for t in _tables(view)
            if str(t.get("name", "")).lower() == parent_name.lower()
            or _table_part(t) == parent_name.lower()
        ),
        None,
    )
    if parent is None:
        return text, (
            f"references '{references}' names no logical table of this view, and a "
            "relationship joins the tables of one view only"
        )
    if not _IDENTIFIER.match(column) or not _IDENTIFIER.match(parent_column):
        return text, "a quoted column is not written into a relationship by dex"
    if not _keyed(parent, [parent_column]):
        return text, (
            f"logical table '{parent.get('name')}' does not declare {parent_column} "
            "as its primary key or a unique key, and Snowflake relates a table only "
            "to a key of the table it points at"
        )
    right = str(parent.get("name"))
    for existing in view.get("relationships") or []:
        if not isinstance(existing, Mapping):
            continue
        pairs = existing.get("relationship_columns") or []
        if (
            str(existing.get("left_table", "")).lower() == table.lower()
            and str(existing.get("right_table", "")).lower() == right.lower()
            and any(
                isinstance(p, Mapping)
                and str(p.get("left_column", "")).lower() == column.lower()
                and str(p.get("right_column", "")).lower() == parent_column.lower()
                for p in pairs
            )
        ):
            return text, None
    names = {
        str(r.get("name", "")).lower()
        for r in view.get("relationships") or []
        if isinstance(r, Mapping)
    }
    name = f"{table}_to_{right}"
    if name.lower() in names:
        name = f"{name}_{column}"
    relationship = {
        "name": name,
        "left_table": table,
        "right_table": right,
        "relationship_columns": [
            {"left_column": column, "right_column": parent_column}
        ],
    }
    appended = append_item(text, "relationships", relationship, dumper=Dumper)
    if appended is None:
        return text, (
            "the view's `relationships` is not a block-style list dex can append "
            "to, so the relationship was not written"
        )
    return appended, None


def _aggregation_of(member: Mapping[str, Any]) -> str | None:
    """The suffix :data:`AGGREGATIONS` gives a metric that is one aggregate call
    over one argument (``SUM(t.x)``, ``COUNT(DISTINCT x)``), else ``None``."""

    expr = member.get("expr")
    if not isinstance(expr, str):
        return None
    found = re.fullmatch(
        r"\s*([A-Za-z_]+)\s*\(\s*(distinct\s+)?[^(),]+\)\s*", expr, re.IGNORECASE
    )
    if found is None:
        return None
    function = found.group(1).lower()
    if function == "count" and found.group(2):
        return "count_distinct"
    mapped = AGGREGATIONS.get(function)
    return mapped[1] if mapped else None


def _without_population(description: Any) -> str | None:
    """An authored description less a population sentence dex wrote earlier."""

    if not isinstance(description, str):
        return None
    head, sep, _tail = description.partition(f" {_POPULATION}")
    if sep:
        return head
    return None if description.startswith(_POPULATION) else description


def declaration_models(documents: Mapping[str, str]) -> dict[str, set[str]]:
    """Each logical table in ``documents`` to its members (#1030), keyed both as
    ``view.table`` and, where the name is unique across the layer, as the bare
    table name, lowered."""

    out: dict[str, set[str]] = {}
    bare: dict[str, list[set[str]]] = {}
    for _path, raw in _views(documents):
        view = str(raw.get("name", "")).lower()
        for table in _tables(raw):
            name = str(table.get("name", "")).lower()
            members: set[str] = set()
            for section in ("dimensions", "time_dimensions", "facts", "metrics"):
                for member in table.get(section) or []:
                    if isinstance(member, dict) and isinstance(member.get("name"), str):
                        members.add(member["name"].lower())
            out[f"{view}.{name}"] = members
            bare.setdefault(name, []).append(members)
    for name, found in bare.items():
        if len(found) == 1 and name not in out:
            out[name] = found[0]
    return out
