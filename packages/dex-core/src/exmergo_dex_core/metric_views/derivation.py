"""A Metric View derived from a declared mart, and declarations annotated onto
an existing one (#1258).

The Databricks counterpart of the MetricFlow (#499), SLayer (#927), Ossie (#500)
and Cube (#911) derivations, behind the same
:class:`~..semantic_source.SemanticDerivationSource` hook. A declaration states
most of what a Metric View needs:

- the mart's relation: the view's ``source``.
- every role but ``measure``: a field, ``expr: <column>``. A date or timestamp
  column (from the SQL, the cache, or a declared ``time`` role) gets
  ``format: {type: date | date_time}``, the only way a Metric View says a field
  is temporal.
- ``measure`` with ``aggregation``: a measure ``<column>_<agg>`` over
  ``SUM``, ``COUNT``, ``COUNT(DISTINCT ...)``, ``AVG``, ``MIN``, ``MAX`` or
  ``MEDIAN``. Fields and measures share one namespace, so the suffix keeps a
  measure from shadowing the field or source column it aggregates.
- ``foreign_key`` with ``references``: a ``many_to_one`` join on the parent's
  relation, with ``rely: {at_most_one_match: true}`` only where the parent's
  own declaration states that column as its whole grain.
- ``unit``: a ``format``, where a format means it (``currency:<code>`` as a
  currency format, ``fraction`` as a percentage, which Databricks displays
  multiplied by 100). A count keeps no unit: it counts rows.
- ``description`` and ``population``: the view's ``comment``, the population as
  one sentence at its end; a column's ``description``: its member's
  ``comment``.

**A Metric View has no metadata key.** Databricks refuses any key the
specification does not list, so ``null_rule``, ``additivity``, ``assumptions``,
a declared ``role`` beyond what the member's kind renders, and the structured
population have nowhere to go. Each is named in ``not_carried`` rather than
written somewhere Databricks would refuse. ``grain`` is not carried either: a
view declares no key on its own source; uniqueness is stated only on a join's
``rely``.

**A population is never a filter.** The view sits on the mart, whose SQL already
enforces it, so no ``filter`` is written: it would apply the rule twice and
diverge from the mart the day the two disagree.

**Where it goes.** One file per mart, ``<dir>/<mart>.<ext>``: the one directory
the configured files share (``metric_views`` where they share none), with their
suffix. It is *included*, as a plan of its own beside the mart's, only where
that path is already listed under ``semantic.databricks.files``; otherwise it is
*proposed*, validated by the same checks, with the commands that adopt it.

**Annotate.** ``semantic databricks plan`` with ``declarations`` renders each one
into the view it names (the file's stem), field by field. Only what changes is
spliced: the root ``comment``, a member's list item, an appended measure or
join. Every other byte of the file, comments included, is kept.

**YAML 1.2 throughout.** Every read is :func:`.loader.read_yaml` and every write
:func:`.loader.dump_yaml`, so a join's ``on:`` key stays text (#1262).

Nothing here opens a workspace connection or deploys a view.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import DexError
from ..semantic_declarations import (
    account,
    placed_table,
    sibling_prefix,
    table_name,
    with_population,
)
from ..yaml_items import append_item, dump_items, locate
from ..yaml_spans import before_trailing_blanks, block_end
from .loader import Dumper, _Loader, dump_yaml, read_yaml

if TYPE_CHECKING:
    from ..semantic_source import (
        DerivedDefinition,
        SemanticDeclaration,
        SemanticDerivationRequest,
        SemanticDerivationResult,
    )
    from .project import MetricViewSemanticLayer

VENDOR = "databricks"

#: Declared aggregation (lowered, spaces and dashes as underscores) to the
#: expression it means and the suffix the measure is named with. Strict, as the
#: other derivations' tables are: anything else is named, never mapped.
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

#: Aggregations whose result is a count of rows, which keeps no unit.
_COUNTING = frozenset({"count", "count_distinct"})

#: Roles that become a field. ``measure`` becomes a measure instead.
_FIELD_ROLES = frozenset({"key", "foreign_key", "dimension", "time", "attribute"})

#: The format a temporal field is written with. Databricks requires a date
#: format's ``date_format``, and a date_time format's ``time_format`` too.
TIME_FORMATS: dict[str, dict[str, str]] = {
    "date": {"type": "date", "date_format": "year_month_day"},
    "date_time": {
        "type": "date_time",
        "date_format": "year_month_day",
        "time_format": "locale_hour_minute_second",
    },
}

#: Where a derived view goes when the configured files share no directory.
DEFAULT_DIR = "metric_views"

#: Why a fact with no Metric View construct is not carried.
NO_METADATA = (
    "a Metric View has no metadata key (Databricks refuses any key the "
    "specification does not list), so this fact has nowhere to go"
)
NO_GRAIN = (
    "a Metric View declares no key on its own source; uniqueness is stated only "
    "on a join's `rely`, so the grain is not written"
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def derive(
    layer: MetricViewSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """The Metric View each declared mart states, validated, never written."""

    from ..semantic_source import SemanticDerivationResult

    if request.mode == "annotate":
        return annotate(layer, request)

    current = _views(request.documents)
    directory, suffix = placement(layer)
    prefix = sibling_prefix(
        (raw.get("source") for _p, raw in current),
        request.project_identifiers.values(),
    )
    built: list[DerivedDefinition] = [
        _build(
            declaration,
            current,
            directory=directory,
            suffix=suffix,
            prefix=prefix,
            documents=request.documents,
        )
        for declaration in request.declarations
    ]
    _validate(layer, request.documents, built)
    included = [d for d in built if d.status == "included"]
    definitions = {d.model: d for d in built}
    return SemanticDerivationResult(
        vendor=VENDOR,
        edit_target="semantic",
        edits=[_edit(d.path or "", d.content or "") for d in included],
        definitions=definitions,
        proposal=_proposal(layer, definitions),
        warnings=_warnings(definitions),
    )


# --- building one view ---------------------------------------------------------------


def _build(
    declaration: SemanticDeclaration,
    current: list[tuple[str, dict[str, Any]]],
    *,
    directory: str,
    suffix: str,
    prefix: str | None,
    documents: Mapping[str, str],
) -> DerivedDefinition:
    """One declared mart as a Metric View body, or the reason there is none."""
    from ..semantic_source import DerivedDefinition

    model = declaration.model
    declared = declaration.declared
    definition = DerivedDefinition(model=model, name=model)
    notes, limitations = definition.notes, definition.limitations
    carried: list[str] = []
    reasons: dict[str, str] = _metadata_reasons(declared)
    identifier = declaration.identifier or model

    def finish(status: str, reason: str) -> DerivedDefinition:
        """Close the definition with ``status`` and ``reason``, nothing carried."""
        definition.status = status  # type: ignore[assignment]
        definition.reason = reason
        definition.carried, definition.not_carried = account(
            declared, [], {}, default=reason
        )
        return definition

    for path, raw in current:
        if table_name(raw.get("source")) == identifier.lower():
            return finish(
                "skipped",
                f"{model} already has a Metric View ('{_stem(path)}' in {path}); an "
                "authored view is not overwritten or duplicated",
            )
    for path, raw in current:
        if _stem(path).lower() == model.lower():
            return finish(
                "failed",
                f"the Metric View name '{model}' is already taken by {path}, on "
                f"another source ({raw.get('source')}); a view is named after its "
                "file, and dex does not rename either",
            )
    definition.path = f"{directory}/{model}{suffix}"
    if definition.path in documents:
        return finish(
            "failed",
            f"{definition.path} already exists; a derived view is written to a "
            "file of its own",
        )

    source, placed = placed_table(identifier, declaration.relation, prefix)
    if placed == "siblings":
        notes.append(
            f"source is {source}: the catalog and schema the views on this "
            "project's other models already read"
        )
    elif placed == "manifest":
        notes.append(
            f"source is {source}, the relation the compiled dbt manifest names, "
            "which is whatever target last compiled the project (often dev). "
            "Point it at the Unity Catalog table the view should read"
        )
    else:
        limitations.append(
            f"source is the bare table name '{identifier}': no view sits on another "
            "of this project's models and there is no compiled manifest, so "
            "Databricks resolves it against the session's catalog and schema. "
            "Qualify it as catalog.schema.table before deploying"
        )

    columns: dict[str, dict[str, Any]] = {
        name: dict(fields)
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    }
    grain = [g for g in declared.get("grain") or [] if isinstance(g, str)]
    fields: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    #: Each measure's source column and spec, so its facts are carried only once
    #: it survives the name-collision check below.
    sources: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for name in dict.fromkeys([*grain, *columns]):
        spec = columns.get(name, {})
        role = spec.get("role")
        if role == "measure" and name not in grain:
            measure = _measure(name, spec, limitations, reasons)
            if measure is not None:
                measures.append(measure)
                sources[measure["name"]] = (name, spec)
            continue
        if name not in grain and role not in _FIELD_ROLES:
            reasons[f"columns.{name}"] = (
                "a column with no role, so the declaration does not say it belongs "
                "in the semantic layer"
            )
            continue
        if not _IDENTIFIER.match(name):
            reasons[f"columns.{name}"] = (
                f"'{name}' is not a plain identifier, so it is not written as a "
                "field name; add the field by hand with a quoted expression"
            )
            continue
        field: dict[str, Any] = {"name": name, "expr": name}
        if isinstance(spec.get("description"), str) and spec["description"]:
            field["comment"] = spec["description"]
        time_type, note = _time_format(name, spec, declaration)
        if note:
            notes.append(note)
        if time_type is not None:
            field["format"] = TIME_FORMATS[time_type]
        else:
            _unit_format(field, spec, reasons, name, aggregation=None)
        fields.append(field)
        carried.extend(_carried_member(name, spec, field))
        if role == "measure":
            reasons[f"columns.{name}.aggregation"] = (
                "a grain column is a field of the view; declare the measure on "
                "another column or add it by hand"
            )

    names = {f["name"].lower() for f in fields}
    for measure in list(measures):
        column, spec = sources[measure["name"]]
        if measure["name"].lower() in names:
            measures.remove(measure)
            reasons[f"columns.{column}.aggregation"] = (
                f"the measure name '{measure['name']}' is already a field's, and "
                "fields and measures share one namespace"
            )
            continue
        names.add(measure["name"].lower())
        carried.extend(_carried_member(column, spec, measure))

    joins = _joins(declaration, current, prefix, notes, reasons, carried)

    if grain:
        reasons.setdefault("grain", NO_GRAIN)
    body: dict[str, Any] = {"version": 1.1, "source": source}
    comment = with_population(declared.get("description"), declared.get("population"))
    if comment:
        body["comment"] = comment
    if declared.get("description"):
        carried.append("description")
    if declared.get("population") and comment:
        carried.append("population")
        notes.append(
            "the population is carried as the last sentence of the view's comment "
            "and never as a `filter`: the mart's SQL already enforces it"
        )
    if joins:
        body["joins"] = joins
    if fields:
        body["fields"] = fields
    if measures:
        body["measures"] = measures
    if not fields and not measures:
        return finish(
            "failed",
            "the declaration names no column with a role, so the view would have "
            "no field and no measure; Databricks refuses an empty view",
        )
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
    definition.content = dump_yaml(body)
    return definition


def _metadata_reasons(declared: Mapping[str, Any]) -> dict[str, str]:
    """``not_carried`` reasons for every fact a Metric View has no key for."""

    reasons: dict[str, str] = {}
    if declared.get("assumptions"):
        reasons["assumptions"] = NO_METADATA
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, Mapping):
            continue
        for key in ("null_rule", "additivity"):
            if fields.get(key) not in (None, ""):
                reasons[f"columns.{name}.{key}"] = NO_METADATA
    return reasons


def _carried_member(
    name: str, spec: Mapping[str, Any], member: Mapping[str, Any]
) -> list[str]:
    """The declared column facts a rendered member carries."""

    out = [f"columns.{name}.role"] if spec.get("role") else []
    if "comment" in member:
        out.append(f"columns.{name}.description")
    if "expr" in member and member["expr"] != name:
        out.append(f"columns.{name}.aggregation")
    fmt = member.get("format")
    if (
        spec.get("unit")
        and isinstance(fmt, Mapping)
        and fmt.get("type")
        in (
            "currency",
            "percentage",
        )
    ):
        out.append(f"columns.{name}.unit")
    return out


def _measure(
    name: str,
    spec: Mapping[str, Any],
    limitations: list[str],
    reasons: dict[str, str],
) -> dict[str, Any] | None:
    """The measure a declared aggregation states, or ``None`` with the reason."""
    declared = spec.get("aggregation")
    additivity = spec.get("additivity")
    why: str | None = None
    mapped: tuple[str, str] | None = None
    if not _IDENTIFIER.match(name):
        why = (
            f"'{name}' is not a plain identifier, so dex does not write an "
            "expression over it; add the measure by hand with a quoted expression"
        )
    elif not isinstance(declared, str) or not declared.strip():
        why = "a measure with no declared aggregation; declare `aggregation`"
    else:
        mapped = AGGREGATIONS.get(re.sub(r"[\s-]+", "_", declared.strip().lower()))
        if mapped is None:
            known = ", ".join(sorted({s for _e, s in AGGREGATIONS.values()}))
            why = (
                f"aggregation '{declared}' has no Metric View expression dex maps "
                f"({known})"
            )
        elif additivity == "non_additive" and mapped[1] in _SUMMING:
            why = (
                f"declared non_additive with aggregation '{declared}', which adds "
                "it up; the two disagree. Fix the declaration"
            )
        elif additivity == "semi_additive":
            why = (
                "semi_additive, and a Metric View expresses that as a window "
                "measure (`window` with `semiadditive: last`), which needs an "
                "ordering field and a range: the author's call, not dex's"
            )
    if why is not None or mapped is None:
        limitations.append(f"{name}: {why}, so no measure is derived")
        reasons[f"columns.{name}.aggregation"] = why or ""
        reasons.setdefault(f"columns.{name}", why or "")
        return None
    template, suffix = mapped
    measure: dict[str, Any] = {
        "name": f"{name}_{suffix}",
        "expr": template.format(name),
    }
    if isinstance(spec.get("description"), str) and spec["description"]:
        measure["comment"] = spec["description"]
    _unit_format(measure, spec, reasons, name, aggregation=suffix)
    return measure


def _unit_format(
    member: dict[str, Any],
    spec: Mapping[str, Any],
    reasons: dict[str, str],
    name: str,
    *,
    aggregation: str | None,
) -> None:
    """Set the ``format`` a declared unit means, or say why there is none."""

    unit = spec.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        return
    fmt, why = unit_format(unit, aggregation)
    if fmt is not None:
        member["format"] = fmt
    else:
        reasons[f"columns.{name}.unit"] = why or ""


def unit_format(
    unit: str, aggregation: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """The Metric View ``format`` a declared unit means, or why there is none."""

    lowered = unit.strip().lower()
    if lowered == "count":
        return None, (
            "a count needs no format: a Metric View format names a currency, a "
            "percentage, bytes or a date, and a plain number is its default"
        )
    if aggregation in _COUNTING:
        return None, (
            f"the measure counts, so its result is a number of things, not '{unit}'"
        )
    if lowered.startswith("currency:"):
        code = unit.strip().split(":", 1)[1].strip().upper()
        if re.fullmatch(r"[A-Z]{3}", code):
            return {"type": "currency", "currency_code": code}, None
        return None, f"'{code}' is not an ISO-4217 currency code"
    if lowered == "fraction":
        return {"type": "percentage"}, None
    if lowered == "percent":
        return None, (
            "a Metric View percentage format displays a fraction multiplied by "
            "100, so a value already in 0 to 100 would read 100 times too large"
        )
    return None, f"a Metric View format has no unit '{unit}'"


def _time_format(
    name: str, spec: Mapping[str, Any], declaration: SemanticDeclaration
) -> tuple[str | None, str | None]:
    """``date`` or ``date_time`` for a temporal column, and a note when the role
    decided it alone."""

    kind = (declaration.column_types.get(name) or (None, None))[0]
    raw = (declaration.data_types.get(name) or "").strip().lower()
    found: str | None = None
    if kind == "date" or raw == "date":
        found = "date"
    elif kind == "timestamp" or "timestamp" in raw or "datetime" in raw:
        found = "date_time"
    if spec.get("role") == "time" and found is None:
        return "date_time", (
            f"{name}: declared a time column whose type could not be read from the "
            "SQL or the cache; it is formatted `date_time` as declared, so check "
            "that it is a timestamp rather than a date"
        )
    return found, None


def _joins(
    declaration: SemanticDeclaration,
    current: list[tuple[str, dict[str, Any]]],
    prefix: str | None,
    notes: list[str],
    reasons: dict[str, str],
    carried: list[str],
) -> list[dict[str, Any]]:
    """One ``many_to_one`` join per declared foreign key that can be written."""
    joins: list[dict[str, Any]] = []
    used: set[str] = set()
    for reference in declaration.references:
        column = reference.column
        why: str | None = None
        if not reference.parent_column:
            why = f"references names no column of {reference.parent_model}"
        elif not _IDENTIFIER.match(column) or not _IDENTIFIER.match(
            reference.parent_column
        ):
            why = "a quoted column is not written into a join condition by dex"
        name = reference.parent_model
        if why is None and (not _IDENTIFIER.match(name) or name.lower() == "source"):
            why = (
                f"'{name}' cannot name a join (a join is named by an identifier, "
                "and never `source`)"
            )
        if why is not None:
            notes.append(f"{column}: {why}")
            reasons[f"columns.{column}.references"] = why
            continue
        if name.lower() in used:
            name = f"{name}_{column}"
            notes.append(
                f"{column}: a second key to {reference.parent_model}, so its join "
                f"is named '{name}'"
            )
        used.add(name.lower())
        wanted = (reference.parent_identifier or reference.parent_model).lower()
        sitting = next(
            (
                (path, raw["source"].strip())
                for path, raw in current
                if isinstance(raw.get("source"), str)
                and table_name(raw["source"]) == wanted
            ),
            None,
        )
        if sitting is not None:
            parent_table, placed = sitting[1], "view"
            notes.append(
                f"{column}: the join reads {parent_table}, the source of the view "
                f"already on {reference.parent_model} ({sitting[0]})"
            )
        else:
            parent_table, placed = placed_table(
                reference.parent_identifier or reference.parent_model,
                reference.parent_relation,
                prefix,
            )
        if placed == "bare":
            notes.append(
                f"{column}: the join reads the bare table name '{parent_table}'; "
                "qualify it as catalog.schema.table before deploying"
            )
        join: dict[str, Any] = {
            "name": name,
            "source": parent_table,
            "on": f"source.{column} = {name}.{reference.parent_column}",
        }
        if reference.parent_grain == (reference.parent_column,):
            join["rely"] = {"at_most_one_match": True}
            notes.append(
                f"{column}: joins {reference.parent_model} on "
                f"{reference.parent_column} (many_to_one), with "
                "`rely.at_most_one_match` because the parent declares that column "
                "as its grain"
            )
        else:
            notes.append(
                f"{column}: joins {reference.parent_model} on "
                f"{reference.parent_column} (many_to_one), without `rely`: the "
                "parent's declaration does not state that column as its grain"
            )
        joins.append(join)
        carried.append(f"columns.{column}.references")
    return joins


# --- what the layer already holds ----------------------------------------------------


def _views(documents: Mapping[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Every configured file's parsed view: ``(path, body)``."""

    out: list[tuple[str, dict[str, Any]]] = []
    for path, text in sorted(documents.items()):
        try:
            parsed = read_yaml(text)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            out.append((path, parsed))
    return out


def _stem(path: str) -> str:
    """The view a file names: its stem."""
    return PurePosixPath(path).stem


def placement(layer: MetricViewSemanticLayer) -> tuple[str, str]:
    """The directory and suffix a derived view takes: the ones the configured
    files share, else ``metric_views`` and ``.yaml``."""

    from .authoring import editing_surface

    surface = editing_surface(layer)
    parents = {str(PurePosixPath(name).parent) for name in surface}
    suffixes = {PurePosixPath(name).suffix for name in surface}
    directory = DEFAULT_DIR
    if len(parents) == 1 and (only := next(iter(parents))) not in ("", "."):
        directory = only
    suffix = next(iter(suffixes)) if len(suffixes) == 1 else ".yaml"
    return directory, suffix


# --- validation ----------------------------------------------------------------------


def _validate(
    layer: MetricViewSemanticLayer,
    documents: Mapping[str, str],
    built: list[DerivedDefinition],
) -> None:
    """Hold every built view to authoring's own checks, then the catalog.

    The whole prospective set is validated at once. A view is included only
    where its path is on the write surface and a plan writing it would pass;
    otherwise it is proposed on the same checks.
    """

    from . import catalog as catalog_mod
    from .authoring import editing_surface, refusals_for, validate_plan

    ready = [d for d in built if d.content is not None and d.status == "proposed"]
    if not ready:
        return
    prospective = dict(documents)
    for definition in ready:
        prospective[definition.path or ""] = definition.content or ""
    validated, refusals = refusals_for(prospective, {d.path or "" for d in ready})
    by_file: dict[str, list[str]] = {}
    for diagnostic in refusals:
        by_file.setdefault(diagnostic.file, []).append(diagnostic.render())
    catalog = catalog_mod.semantic_catalog(
        validated, catalog_mod.resolution(validated, layer.connector)
    )
    catalogued = {m.name.lower() for m in catalog.semantic_models}
    surface = set(editing_surface(layer))

    for definition in ready:
        problems = by_file.get(definition.path or "")
        if problems:
            definition.status = "failed"
            definition.reason = "the Metric View rules refuse it: " + "; ".join(
                problems
            )
            definition.validation["metric_view"] = {
                "result": "failed",
                "detail": "; ".join(problems),
            }
            continue
        definition.validation["metric_view"] = {"result": "passed"}
        if (definition.name or "").lower() not in catalogued:
            definition.status = "failed"
            definition.reason = "the derived view did not read back as a catalog model"
            definition.validation["catalog"] = {"result": "failed"}
            continue
        definition.validation["catalog"] = {"result": "passed"}
        if definition.path not in surface:
            definition.next = _adopt(layer, [definition.path or ""])
            continue
        definition.status = "included"

    included = [d for d in ready if d.status == "included"]
    if not included:
        return
    try:
        validate_plan(
            layer, [_edit(d.path or "", d.content or "") for d in included], "define"
        )
    except ValueError as exc:
        for definition in included:
            definition.status = "failed"
            definition.reason = str(exc)
            definition.validation["plan"] = {"result": "failed", "detail": str(exc)}
        return
    for definition in included:
        definition.validation["plan"] = {"result": "passed"}


def _adopt(layer: MetricViewSemanticLayer, paths: list[str]) -> str:
    """The commands that put ``paths`` on the write surface and author them."""
    listed = ", ".join(dict.fromkeys([*layer.files, *paths]))
    return (
        f"{', '.join(paths)} is not under semantic.databricks.files, the files dex "
        f"may write: run `dex config set semantic.databricks.files '[{listed}]'`, "
        "then pass data.semantic_proposal.definitions as the edits file to "
        "`semantic databricks define`, or re-run this plan to include it"
    )


def _edit(path: str, content: str) -> Any:
    """A whole-file semantic-document edit writing ``content`` to ``path``."""
    from ..transform.plans import EditKind, PlanEdit

    return PlanEdit(path=path, kind=EditKind.SEMANTIC_DOCUMENT, new_content=content)


# --- the envelope --------------------------------------------------------------------


def _proposal(
    layer: MetricViewSemanticLayer, definitions: Mapping[str, DerivedDefinition]
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
        "dex writes the Metric View YAML body and never deploys it: create the view "
        "with `CREATE VIEW <catalog>.<schema>.<name> WITH METRICS LANGUAGE YAML AS "
        "$$ <file> $$`, named after the file"
    ]
    if proposed:
        data["next"] = (
            proposed[0].next
            if len(proposed) == 1
            else _adopt(layer, [d.path or "" for d in proposed])
        )
    if included:
        notes.append(
            "included views are stored as a plan of their own on the Metric View "
            "layer (data.semantic_proposal.plan_id), beside the dbt plan; apply both"
        )
    data["notes"] = notes
    return data


def _model_data(definition: DerivedDefinition) -> dict[str, Any]:
    """One model's entry under ``data.semantic_proposal.models``."""
    out: dict[str, Any] = {"status": definition.status}
    if definition.content is not None:
        out["view"] = definition.name
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
    """One envelope warning per view included, failed or unvalidated."""
    out: list[str] = []
    for definition in definitions.values():
        if definition.status == "included":
            out.append(
                f"Metric View '{definition.model}' was derived from the declaration "
                f"for {definition.path}; see data.semantic_proposal"
            )
        elif definition.status in ("failed", "unvalidated"):
            out.append(
                f"Metric View for '{definition.model}' was derived and not offered "
                f"({definition.status}): {definition.reason}"
            )
    return out


# --- annotate: declarations onto a view the layer or payload defines -----------------


def annotate(
    layer: MetricViewSemanticLayer, request: SemanticDerivationRequest
) -> SemanticDerivationResult:
    """Render each declaration into the view it names (its file's stem).

    The declaration wins field by field. Only what changes is spliced, so every
    other byte of the file keeps its place; a view with nothing to change keeps
    its own bytes. A declaration naming no configured view is ``failed``, and
    the native route refuses the plan for it.
    """

    from ..semantic_source import DerivedDefinition, SemanticDerivationResult

    texts = dict(request.documents)
    definitions: dict[str, DerivedDefinition] = {}
    changed: list[str] = []
    for declaration in request.declarations:
        definition = DerivedDefinition(model=declaration.model, name=declaration.model)
        definitions[declaration.model] = definition
        paths = [p for p in texts if _stem(p).lower() == declaration.model.lower()]
        if not paths:
            definition.status = "failed"
            definition.reason = (
                f"no configured Metric View is named '{declaration.model}' (a view "
                "is named after its file's stem)"
            )
            continue
        path = paths[0]
        definition.path = path
        try:
            updated, carried, reasons = _annotated(texts[path], declaration, texts)
        except _UnwritableError as exc:
            definition.status = "failed"
            definition.reason = str(exc)
            continue
        definition.carried, definition.not_carried = account(
            declaration.declared,
            carried,
            reasons,
            default="the view has no field or measure of that name",
        )
        definition.status = "included"
        if updated != texts[path]:
            texts[path] = updated
            if path not in changed:
                changed.append(path)
        else:
            definition.notes.append("the view already states every declared fact")
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


class _UnwritableError(DexError):
    """The view cannot be rewritten in place; the message says why. Never leaves
    :func:`annotate`, which turns it into the definition's ``failed`` reason."""


def _annotated(
    text: str, declaration: SemanticDeclaration, texts: Mapping[str, str]
) -> tuple[str, list[str], dict[str, str]]:
    """``text`` with the declared facts applied, what was carried, and why not."""

    declared = declaration.declared
    try:
        raw = read_yaml(text)
    except yaml.YAMLError as exc:
        raise _UnwritableError(f"the view's file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise _UnwritableError("the view's file is not a Metric View mapping")
    carried: list[str] = []
    reasons = _metadata_reasons(declared)
    if declared.get("grain"):
        reasons["grain"] = NO_GRAIN

    if declared.get("description") or declared.get("population"):
        comment = with_population(
            declared.get("description") or _without_population(raw.get("comment")),
            declared.get("population"),
        )
        if comment and comment != raw.get("comment"):
            text = set_root_scalar(text, "comment", comment)
        if declared.get("description"):
            carried.append("description")
        if declared.get("population"):
            carried.append("population")

    section = "dimensions" if raw.get("dimensions") else "fields"
    members = {
        str(m.get("name")).lower(): (section, m)
        for m in raw.get(section) or []
        if isinstance(m, dict) and isinstance(m.get("name"), str)
    }
    members.update(
        {
            str(m.get("name")).lower(): ("measures", m)
            for m in raw.get("measures") or []
            if isinstance(m, dict) and isinstance(m.get("name"), str)
        }
    )
    joins = {
        str(j.get("name")).lower()
        for j in raw.get("joins") or []
        if isinstance(j, dict)
    }

    for name, spec in (declared.get("columns") or {}).items():
        if not isinstance(spec, Mapping):
            continue
        role = spec.get("role")
        mapped = None
        aggregation = spec.get("aggregation")
        if isinstance(aggregation, str):
            mapped = AGGREGATIONS.get(
                re.sub(r"[\s-]+", "_", aggregation.strip().lower())
            )
        found = members.get(name.lower())
        if role == "measure" and mapped is not None:
            found = members.get(f"{name}_{mapped[1]}".lower()) or (
                found if found and found[0] == "measures" else None
            )
        if found is None and role == "measure":
            limitations: list[str] = []
            measure = _measure(name, spec, limitations, reasons)
            if measure is None:
                continue
            if measure["name"].lower() in members:
                reasons[f"columns.{name}.aggregation"] = (
                    f"the view already has a member named '{measure['name']}'"
                )
                continue
            appended = append_item(text, "measures", measure, dumper=Dumper)
            if appended is None:
                raise _UnwritableError(
                    "the view's `measures` is not a block-style list dex can append "
                    "to, so nothing in the file was changed"
                )
            text = appended
            members[measure["name"].lower()] = ("measures", measure)
            carried.extend(_carried_member(name, spec, measure))
            continue
        if found is None:
            reasons.setdefault(
                f"columns.{name}", "the view has no field or measure of that name"
            )
            continue
        where, member = found
        entry = dict(member)
        if isinstance(spec.get("description"), str) and spec["description"]:
            entry["comment"] = spec["description"]
        if spec.get("unit") and not _is_time(entry):
            fmt, why = unit_format(
                str(spec["unit"]),
                _aggregation_of(entry) if where == "measures" else None,
            )
            if fmt is not None:
                entry["format"] = fmt
            else:
                reasons[f"columns.{name}.unit"] = why or ""
        elif spec.get("unit"):
            reasons[f"columns.{name}.unit"] = (
                "the field is formatted as a date; a unit format would replace it"
            )
        if role:
            carried.append(f"columns.{name}.role")
        authored = _aggregation_of(entry) if where == "measures" else None
        if (
            role == "measure"
            and where == "measures"
            and mapped is not None
            and (authored is not None and authored != mapped[1])
        ):
            reasons[f"columns.{name}.aggregation"] = (
                f"the measure '{member['name']}' aggregates with {authored.upper()}, "
                f"and the declaration says {aggregation}; dex does not rewrite an "
                "authored expression, so fix whichever is wrong"
            )
        elif role == "measure" and where == "measures":
            carried.append(f"columns.{name}.aggregation")
        elif role == "measure":
            reasons[f"columns.{name}.aggregation"] = (
                f"'{name}' is a field of the view, and a measure of the same name "
                "would collide with it"
            )
        if "comment" in entry and spec.get("description"):
            carried.append(f"columns.{name}.description")
        if (
            "format" in entry
            and f"columns.{name}.unit" not in reasons
            and spec.get("unit")
        ):
            carried.append(f"columns.{name}.unit")
        if entry != member:
            text = _replace_item(text, where, str(member["name"]), entry)
            members[name.lower()] = (where, entry)
        if role == "foreign_key" and isinstance(spec.get("references"), str):
            text, why = _annotate_join(text, name, spec["references"], texts, joins)
            if why is None:
                carried.append(f"columns.{name}.references")
            else:
                reasons[f"columns.{name}.references"] = why
    return text, carried, reasons


def _is_time(member: Mapping[str, Any]) -> bool:
    """Whether a member's ``format`` marks it a date or a timestamp."""
    fmt = member.get("format")
    return isinstance(fmt, Mapping) and fmt.get("type") in ("date", "date_time")


def _aggregation_of(member: Mapping[str, Any]) -> str | None:
    """The suffix :data:`AGGREGATIONS` gives a measure that is one aggregate call
    over one argument (``SUM(x)``, ``COUNT(DISTINCT x)``), else ``None``."""

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


def _replace_item(text: str, section: str, name: str, entry: Mapping[str, Any]) -> str:
    """``text`` with the member ``name`` in ``section`` re-rendered as ``entry``."""
    located = locate(text, name, section, load=read_yaml)
    if located is None:
        raise _UnwritableError(
            f"{section}['{name}'] is not written as a block-style list item dex can "
            "rewrite on its own, so nothing in the file was changed"
        )
    start, end, indent, _parsed = located
    return text[:start] + dump_items([dict(entry)], indent, Dumper) + text[end:]


def _annotate_join(
    text: str,
    column: str,
    references: str,
    texts: Mapping[str, str],
    joins: set[str],
) -> tuple[str, str | None]:
    """``text`` with a join to the referenced view's source, or why not. An
    authored join of the same name is the author's and is left as it is."""

    parent, _, parent_column = references.rpartition(".")
    target = next(
        (
            (path, read)
            for path, read in _views(texts)
            if _stem(path).lower() == parent.lower()
        ),
        None,
    )
    if not parent or target is None:
        return text, (
            f"references '{references}' names no configured Metric View, so there "
            "is no source to join"
        )
    if not _IDENTIFIER.match(parent) or parent.lower() == "source":
        return text, f"'{parent}' cannot name a join"
    if not _IDENTIFIER.match(column) or not _IDENTIFIER.match(parent_column):
        return text, "a quoted column is not written into a join condition by dex"
    if parent.lower() in joins:
        return text, None
    source = target[1].get("source")
    if not isinstance(source, str) or not source.strip():
        return text, f"the view '{parent}' has no source table to join"
    join = {
        "name": parent,
        "source": source.strip(),
        "on": f"source.{column} = {parent}.{parent_column}",
    }
    appended = append_item(text, "joins", join, dumper=Dumper)
    if appended is None:
        return text, (
            "the view's `joins` is not a block-style list dex can append to, so the "
            "join was not written"
        )
    joins.add(parent.lower())
    return appended, None


def set_root_scalar(text: str, key: str, value: str) -> str:
    """``text`` with the root ``key`` set to ``value``, every other byte kept.

    Where the key is written, its lines are replaced; where it is not, it is
    added after the ``source`` entry (or at the end), so a view keeps its
    authored order.
    """

    try:
        root = yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise _UnwritableError(f"the view's file is not valid YAML: {exc}") from exc
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise _UnwritableError("the view's root is not a block-style mapping")
    rendered = dump_yaml({key: value})
    after: int | None = None
    for key_node, _value in root.value:
        line_start = text.rfind("\n", 0, key_node.start_mark.index) + 1
        if key_node.start_mark.index != line_start:
            raise _UnwritableError(
                "the view's root keys are indented; dex cannot place one"
            )
        end = before_trailing_blanks(text, block_end(text, line_start, 0))
        if key_node.value == key:
            return text[:line_start] + rendered + text[end:]
        if key_node.value == "source":
            after = end
    if after is None:
        after = len(text)
    head = text[:after]
    if head and not head.endswith("\n"):
        head += "\n"
    return head + rendered + text[after:]


def _without_population(comment: Any) -> str | None:
    """An authored comment less a population sentence dex wrote earlier."""

    if not isinstance(comment, str):
        return None
    head, sep, _tail = comment.partition(" Population: ")
    if sep:
        return head
    return None if comment.startswith("Population: ") else comment


def declaration_models(documents: Mapping[str, str]) -> dict[str, set[str]]:
    """Each view in ``documents`` (its stem, lowered) to its members (#1030)."""

    out: dict[str, set[str]] = {}
    for path, raw in _views(documents):
        members: set[str] = set()
        for key in ("fields", "dimensions", "measures"):
            for member in raw.get(key) or []:
                if isinstance(member, dict) and isinstance(member.get("name"), str):
                    members.add(member["name"].lower())
        out[_stem(path).lower()] = members
    return out
