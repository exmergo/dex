"""Declared marts into whichever semantic layer the repository reads (#927).

The one place `transform plan`, `transform declare` and the dbt `semantic plan`
reach a derivation through, so none of them knows a vendor. A declared mart (a
model under ``transform.marts_paths`` whose post-change YAML carries a
declaration) becomes a :class:`~..semantic_source.SemanticDeclaration`, and the
configured vendor's :class:`~..semantic_source.SemanticDerivationSource` answers
for the batch:

- ``semantic.vendor: dbt`` is answered by
  :class:`.semantic_derivation.MetricFlowDerivation`, whose layer lives in the
  dbt project, so its edits join the plan's own.
- A vendor that is its own semantic source (``SEMANTIC_SOURCE_FACTORIES``) is
  answered by that source when it implements the capability. Its included
  documents sit on the layer's own write surface, so they come back as a
  companion the caller stores as a second plan through the vendor's authoring
  route, the way `maintain reconcile` stores one.
- A vendor that does not implement it is named in a warning, and nothing is
  derived.

Everything a declaration is built from is free to read: the post-change files,
the stored exploration cache, and the compiled manifest when there is one. No
connection is opened here or by any derivation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .plans import PlanEdit

if TYPE_CHECKING:
    from ..dbt_project import DbtProjectView
    from ..engine import DexEngine
    from ..semantic_source import SemanticDeclaration


@dataclass
class Outcome:
    """What one plan's derivation pass produced."""

    edits: list[PlanEdit]
    proposal: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    #: dbt's parse of exactly the plan's edits, when the pass ran one the plan's
    #: own gate can reuse.
    parse: dict[str, Any] | None = None
    #: Included documents on a native layer's own surface, for the companion
    #: plan; empty for a layer that lives in the project.
    companion: list[PlanEdit] = field(default_factory=list)
    vendor: str = "dbt"


def candidates(
    view: DbtProjectView,
    edits: list[PlanEdit],
    models: Iterable[str],
    marts_paths: Iterable[str],
) -> tuple[dict[str, str], list[tuple[str, str, dict[str, Any]]]]:
    """The post-change files, and each declared mart as ``(model, sql, declared)``.

    ``models`` is what the plan creates, edits or declares; only those under
    ``marts_paths`` whose post-change YAML carries a declaration are kept.
    """

    from . import declarations as declarations_mod
    from . import drafting
    from .semantic_derivation import declares_something, in_marts

    marts_paths = list(marts_paths)
    files = declarations_mod.post_change_files(view, edits)
    found: list[tuple[str, str, dict[str, Any]]] = []
    for model in sorted(set(models)):
        sql_path = declarations_mod.model_sql_path(files, view.model_paths, model)
        if sql_path is None or not in_marts(sql_path, marts_paths):
            continue
        declared = drafting.read_declared(files, model)
        if declares_something(declared):
            found.append((model, sql_path, declared))
    return files, found


def derive(
    engine: DexEngine,
    view: DbtProjectView,
    edits: list[PlanEdit],
    models: Iterable[str],
    *,
    cache: Any,
    dialect: str,
) -> Outcome:
    """Each declared mart in ``edits``, derived into the configured layer."""

    from ..config import SEMANTIC_SOURCE_FACTORIES
    from ..semantic_source import SemanticDerivationRequest, SemanticDerivationSource
    from .semantic_derivation import MetricFlowDerivation

    files, found = candidates(view, edits, models, engine.config.transform.marts_paths)
    if not found:
        return Outcome(edits=edits)

    vendor = (engine.config.semantic.vendor or "dbt").lower()
    names = ", ".join(model for model, _sql, _declared in found)
    documents: dict[str, str] = {}
    if vendor in SEMANTIC_SOURCE_FACTORIES:
        try:
            source: Any = engine.semantic_catalog_source()
        except Exception as exc:
            return Outcome(
                edits=edits,
                vendor=vendor,
                warnings=[
                    f"no semantic model was derived for {names}: the {vendor} "
                    f"semantic layer could not be read ({exc})"
                ],
            )
        if not isinstance(source, SemanticDerivationSource):
            return Outcome(
                edits=edits, vendor=vendor, warnings=[_declined(vendor, names)]
            )
        documents = _surface_documents(source)
    else:
        source = MetricFlowDerivation(
            view,
            edits,
            files,
            project=engine.project_dir(),
            target=engine.config.dbt_target,
        )
        documents = files

    relations = manifest_relations(view)
    request = SemanticDerivationRequest(
        mode="derive",
        declarations=tuple(
            declaration(
                model,
                declared,
                sql=files[sql_path],
                sql_path=sql_path,
                cache=cache,
                dialect=dialect,
                vars=view.vars,
                relations=relations,
                model_sql=_model_sql(files, view.model_paths),
            )
            for model, sql_path, declared in found
        ),
        documents=documents,
        project_identifiers={
            name: _identifier(name, relations) for name in _model_names(view)
        },
        cache=cache,
        connector=engine.connector or engine.config.connector,
    )
    result = source.derive_semantic(request)
    if result.edit_target == "project":
        return Outcome(
            edits=result.edits,
            proposal=result.proposal,
            warnings=list(result.warnings),
            parse=result.gate,
            vendor=result.vendor,
        )
    return Outcome(
        edits=edits,
        proposal=result.proposal,
        warnings=list(result.warnings),
        companion=list(result.edits),
        vendor=result.vendor,
    )


def _declined(vendor: str, names: str) -> str:
    # The layer this project reads is not MetricFlow, so a MetricFlow model
    # would be a second layer nothing reads; and this one cannot carry a
    # declaration yet, which is said rather than left to read as derived.
    return (
        f"no semantic model was derived for {names}: semantic.vendor is "
        f"'{vendor}', and the {vendor} layer does not carry declarations yet (it "
        "does not implement the derivation hook, SemanticDerivationSource), so "
        "the declared population, grain and measures reach no semantic definition"
    )


def _surface_documents(source: Any) -> dict[str, str]:
    from ..edits import SemanticEditTarget

    if not isinstance(source, SemanticEditTarget):
        return {}
    return {
        path: entry.content for path, entry in source.semantic_edit_view().files.items()
    }


# --- building one declaration --------------------------------------------------------


def declaration(
    model: str,
    declared: Mapping[str, Any],
    *,
    sql: str | None,
    sql_path: str | None,
    cache: Any,
    dialect: str,
    vars: Mapping[str, Any] | None,
    relations: Mapping[str, tuple[str, str]],
    model_sql: Callable[[str], str | None] | None = None,
) -> SemanticDeclaration:
    """``declared`` with the facts a vendor needs that the YAML does not state."""

    from ..semantic_source import DeclaredReference, SemanticDeclaration
    from .semantic_derivation import column_types

    columns = [
        name
        for name, fields in (declared.get("columns") or {}).items()
        if isinstance(fields, dict)
    ]
    relation = relations.get(model, (None, None))[0]
    types: dict[str, tuple[str | None, str | None]] = {}
    data_types: dict[str, str] = {}
    time_like = [
        name for name in columns if declared["columns"][name].get("role") == "time"
    ]
    if sql is not None:
        types = column_types(sql, cache, time_like, dialect=dialect, vars=vars)
        try:
            data_types = column_data_types(
                sql,
                cache,
                columns,
                relation=relation,
                dialect=dialect,
                vars=vars,
                model_sql=model_sql,
            )
        except Exception:
            # A type is a convenience a vendor may use and every vendor degrades
            # without, so SQL the reader cannot follow costs the types alone.
            data_types = {}
    references: list[DeclaredReference] = []
    for name in columns:
        fields = declared["columns"][name]
        target = fields.get("references")
        if fields.get("role") != "foreign_key" or not isinstance(target, str):
            continue
        parent, _, parent_column = target.rpartition(".")
        if not parent:
            parent, parent_column = target, ""
        references.append(
            DeclaredReference(
                column=name,
                parent_model=parent,
                parent_column=parent_column or None,
                parent_identifier=_identifier(parent, relations),
                parent_relation=relations.get(parent, (None, None))[0],
            )
        )
    return SemanticDeclaration(
        model=model,
        declared=declared,
        column_types=types,
        data_types=data_types,
        identifier=_identifier(model, relations),
        relation=relation,
        relation_source="manifest" if relation else None,
        references=tuple(references),
        source_path=sql_path,
    )


def manifest_relations(view: DbtProjectView) -> dict[str, tuple[str, str]]:
    """Each model the compiled manifest names, to ``(relation, identifier)``.

    Empty without a manifest. The relation is the one dbt compiled for whatever
    target last ran, which the derivation that uses it says.
    """

    from ..dbt_project import strip_relation_quoting

    out: dict[str, tuple[str, str]] = {}
    nodes = (view.manifest or {}).get("nodes")
    if not isinstance(nodes, dict):
        return out
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name, relation = node.get("name"), node.get("relation_name")
        if not isinstance(name, str) or not isinstance(relation, str) or not relation:
            continue
        stripped = strip_relation_quoting(relation)
        alias = node.get("alias")
        identifier = alias if isinstance(alias, str) and alias else name
        out[name] = (stripped, identifier)
    return out


def _identifier(model: str, relations: Mapping[str, tuple[str, str]]) -> str:
    return relations.get(model, (None, model))[1] or model


def _model_names(view: DbtProjectView) -> list[str]:
    from ..dbt_project import node_files, node_name

    return sorted({node_name(path) for path in node_files(view)})


#: How many of the project's own models a column is followed back through to
#: find a profiled type: staging, an intermediate layer or two, and the mart.
_LINEAGE_DEPTH = 6


def _model_sql(
    files: Mapping[str, str], model_paths: list[str]
) -> Callable[[str], str | None]:
    from . import declarations as declarations_mod

    def read(model: str) -> str | None:
        path = declarations_mod.model_sql_path(files, model_paths, model)
        return files.get(path) if path else None

    return read


def column_data_types(
    sql: str,
    cache: Any,
    columns: Iterable[str],
    *,
    relation: str | None,
    dialect: str,
    vars: Mapping[str, Any] | None,
    model_sql: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """The warehouse type of each column, where something free states it.

    The mart's own profile first (a dev build someone explored), then a cast or
    a truncation in the model's SELECT list, then the profile of the column it
    carries through unchanged. Where that column comes from another of the
    project's models nobody profiled, the same reading follows it into that
    model's SQL, a bounded number of hops, so a fresh project whose sources are
    profiled still types its marts. A column none of those speaks for is absent.
    """

    from . import drafting

    out: dict[str, str] = {}
    own = drafting.dataset_for(cache, relation) if relation else None
    wanted = list(columns)
    for name in wanted:
        profile = drafting._column(own, name)
        if profile is not None and profile.data_type:
            out[name] = profile.data_type
    missing = [name for name in wanted if name not in out]
    if not missing:
        return out
    for name in missing:
        found = _traced_type(
            sql, name, cache, dialect=dialect, vars=vars, model_sql=model_sql
        )
        if found:
            out[name] = found
    return out


def _traced_type(
    sql: str,
    column: str,
    cache: Any,
    *,
    dialect: str,
    vars: Mapping[str, Any] | None,
    model_sql: Callable[[str], str | None] | None,
) -> str | None:
    from . import drafting

    readings: dict[str, Any] = {}
    for _hop in range(_LINEAGE_DEPTH):
        reading = readings.get(sql) or drafting.read(
            sql, cache, dialect=dialect, vars=vars
        )
        readings[sql] = reading
        lowered = column.lower()
        expression = reading.expressions.get(lowered)
        found = _declared_type(expression) if expression is not None else None
        if found:
            return found
        origin = reading.origins.get(lowered)
        if origin is None:
            return None
        profile = drafting._column(
            drafting.dataset_for(cache, origin.relation), origin.column
        )
        if profile is not None and profile.data_type:
            return profile.data_type
        upstream = model_sql(origin.relation) if model_sql is not None else None
        if upstream is None:
            return None
        sql, column = upstream, origin.column
    return None


def _declared_type(expression: Any) -> str | None:
    from sqlglot import expressions as exp

    node = expression.this if isinstance(expression, exp.Alias) else expression
    while isinstance(node, (exp.Paren, exp.Min, exp.Max)):
        node = node.this
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return node.to.sql()
    if isinstance(node, exp.DateTrunc):
        return "DATE"
    if isinstance(node, (exp.TimestampTrunc, exp.DatetimeTrunc)):
        return "TIMESTAMP"
    if isinstance(node, exp.Count):
        return "BIGINT"
    return None
