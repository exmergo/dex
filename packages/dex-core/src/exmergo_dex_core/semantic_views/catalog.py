"""Normalizing a validated set of Semantic Views into what dex reads.

Two channels from one validated set, as for Ossie, SLayer, Cube and Metric
Views:

- :func:`semantic_catalog` builds the neutral read catalog ``explore semantic
  list`` renders.
- :func:`definitions` builds the declaration channel: declared keys and joins,
  and nothing else.

**How a Semantic View maps.**

- A logical table is one semantic model on the relation its ``base_table``
  names. A ``definition`` query stands for a relation only where it is a plain
  projection of one table; otherwise the model has no relation, and the notes
  say why.
- **Names are qualified by their view.** A view is its own namespace (two views
  routinely both declare ``orders``), so a logical table is the semantic model
  ``view.table`` and a member is ``view.table.member``; a derived metric is
  ``view.metric``. Inside the view the name is what Snowflake's own
  ``SEMANTIC_VIEW(...)`` clause takes (``orders.order_count``), and the view is
  the clause's first argument. The name never depends on which other views are
  configured, so adding one moves nothing.
- **Entities are mapped, not declared a gap.** A single-column primary key is a
  primary entity on its table, named after the table, and each relationship that
  references it is a foreign declaration of that entity on the referencing table.
  Composite and unique keys reach exploration through the declaration channel.
- Dimensions and time dimensions are dimensions ``table.member``; a time
  dimension is ``time``, with no queryable grains, since Semantic Views have
  none. **Facts** are listed as dimension rows too, marked ``kind: fact`` and
  ``groupable: false`` in ``vendor_params``: a fact is row-level, queried
  through the ``FACTS`` clause, and never combined with a metric.
- A table metric that is one aggregate is a catalog measure **and** a
  ``simple`` metric; any other table metric is ``derived`` with its expression.
  A metric that is exactly one metric divided by another is a ``ratio``, and
  any other composition (a view-level metric included) is ``derived``, with
  the metrics it reads. A metric is
  groupable by its own table's dimensions and those of every table its
  relationships reach many-to-one; a derived metric by what all its inputs share.
- ``description`` is the description, and ``synonyms``, ``access_modifier``,
  ``non_additive_dimensions``, ``using_relationships`` and a window ride in
  ``vendor_params``, so ``--search`` matches synonyms. ``sample_values`` are
  carried as a count and never surfaced: Snowflake does not mask them.
- Everything the catalog has no home for (window metrics, ``NON ADDITIVE BY``,
  ASOF and range relationships, ``using_relationships``, LOD, filters and
  labels, ``variables``, ``max_staleness``, tags, and the Cortex Analyst keys) is
  named in the notes, never reinterpreted.

**Columns are claimed only on the model's own relation.** A member that reads
another member is followed to the column behind it; one computed from its own
table's columns carries no column, and the PII gate parses its expression.

Nothing here imports sqlglot: :mod:`.lineage`, which does, is imported inside
:func:`resolution`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..project_definitions import (
    DeclaredCompositeKey,
    DeclaredForeignKey,
    DeclaredKey,
    DeclaredRelationship,
    ModelDeclaration,
    ProjectDefinitions,
)
from ..semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    derive_entity_type,
)
from ..semantic_declarations import read_native_declaration
from .model import (
    DERIVED,
    FACT,
    METRIC,
    TIME_DIMENSION,
    LogicalTable,
    Member,
    Relationship,
    SemanticView,
)
from .validator import CORTEX_ONLY, UNSUPPORTED

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = [
    "DECLARATION_SOURCE",
    "DeclaredJoin",
    "Resolution",
    "declared_relationships",
    "definitions",
    "resolution",
    "semantic_catalog",
]

#: What `ProjectDefinitions` records as the channel a declaration came from.
DECLARATION_SOURCE = "snowflake"


@dataclass(frozen=True)
class Resolved:
    """Where one member reads from: the column behind it on its own table, or,
    for a computed one, its SQL over that table's columns. ``aggregate`` is set
    for a table metric that is one aggregate."""

    column: str | None = None
    expression: str | None = None
    aggregate: Any = None


@dataclass
class Resolution:
    """Every model name, relation and column the set resolves to, computed once,
    so the catalog, the declarations and the fingerprint cannot disagree."""

    #: ``(view, table)`` (both lowered) to the semantic model name.
    models: dict[tuple[str, str], str] = field(default_factory=dict)
    #: A derived metric ``(view, metric)`` (lowered) to the name it is listed as.
    derived: dict[tuple[str, str], str] = field(default_factory=dict)
    #: ``(view, table)`` to the relation the catalog may claim, connector-spelled.
    relations: dict[tuple[str, str], str | None] = field(default_factory=dict)
    #: ``(view, table)`` to the relation the PII gate screens through.
    gates: dict[tuple[str, str], str | None] = field(default_factory=dict)
    members: dict[tuple[str, str, str], Resolved] = field(default_factory=dict)
    #: ``(view, table)`` to the tables it reaches many-to-one, lowered.
    coarser: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    connector: str | None = None
    notes: list[str] = field(default_factory=list)

    def model(self, view: SemanticView, table: str) -> str:
        """The semantic model a logical table is listed as."""
        return self.models[(view.name.lower(), table.lower())]

    def relation(self, view: SemanticView, table: str) -> str | None:
        """The relation the catalog claims for a logical table."""
        return self.relations.get((view.name.lower(), table.lower()))

    def gate(self, view: SemanticView, table: str) -> str | None:
        """The relation the PII gate screens a logical table's columns through."""
        return self.gates.get((view.name.lower(), table.lower()))

    def member(self, view: SemanticView, member: Member) -> Resolved:
        """The resolution of one member (of a logical table)."""
        return self.members.get(
            (view.name.lower(), (member.table or "").lower(), member.name.lower()),
            Resolved(),
        )

    def token(self, view: SemanticView, member: Member) -> str:
        """The name a member is listed and queried as."""
        if member.table is None:
            return self.derived[(view.name.lower(), member.name.lower())]
        return f"{self.model(view, member.table)}.{member.name}"

    def fold(self, name: str) -> str:
        """A column name spelled the way the connector folds an unquoted one."""
        from ..adapters import fold_identifier

        return fold_identifier(self.connector, name)


# --- resolution -----------------------------------------------------------------


def resolution(loaded: LoadResult, connector: str | None) -> Resolution:
    """Resolve the set, through :mod:`.lineage` (the one sqlglot importer)."""

    from . import lineage
    from .loader import relationship_graph

    resolved = Resolution(connector=connector)
    for view in loaded.views:
        v = view.name.lower()
        graph = relationship_graph(view)
        for table in view.tables:
            t = table.name.lower()
            resolved.models[(v, t)] = f"{view.name}.{table.name}"
            resolved.coarser[(v, t)] = graph.coarser(t)
            relation, gate = _relation(view, table, connector, lineage, resolved)
            resolved.relations[(v, t)] = relation
            resolved.gates[(v, t)] = gate
            for member in table.members():
                resolved.members[(v, t, member.name.lower())] = _resolve_member(
                    table, member, lineage, resolved
                )
        for metric in view.metrics:
            resolved.derived[(v, metric.name.lower())] = f"{view.name}.{metric.name}"
    return resolved


def _relation(
    view: SemanticView,
    table: LogicalTable,
    connector: str | None,
    lineage: Any,
    resolved: Resolution,
) -> tuple[str | None, str | None]:
    """A logical table's ``(relation, gate)``, spelled for the connector."""

    from ..adapters import normalize_relation

    name = table.base_table
    if name is None and table.definition is not None:
        name, reason = lineage.definition_table(table.definition)
        if name is None:
            resolved.notes.append(
                f"view '{view.name}': table '{table.name}': {reason}, so it claims "
                "no relation"
            )
            return None, None
    if name is None:
        return None, None
    spelled = normalize_relation(connector, name) if connector else None
    if spelled is None and connector:
        resolved.notes.append(
            f"view '{view.name}': table '{table.name}': `{name}` is not one fully "
            f"qualified relation on {connector}, so it carries no physical link"
        )
    return spelled, spelled or name


def _resolve_member(
    table: LogicalTable, member: Member, lineage: Any, resolved: Resolution
) -> Resolved:
    """The column behind one member, followed through the members it reads."""

    if member.kind == METRIC:
        found = lineage.aggregate(member.expr)
        if found is None or member.window:
            return Resolved()
        column = (
            _column(table, found.read, lineage, resolved, set())
            if found.read is not None
            else None
        )
        return Resolved(column=column, aggregate=found)
    read = lineage.plain(member.expr)
    if read is not None:
        return Resolved(
            column=_column(table, read, lineage, resolved, {member.name.lower()})
        )
    reads = lineage.reads(member.expr) or []
    columns: dict[str, str] = {}
    for found in reads:
        column = _column(table, found, lineage, resolved, {member.name.lower()})
        if column is None:
            return Resolved()
        columns[found.name.lower()] = column
    if reads:
        return Resolved(
            expression=lineage.over_columns(member.expr, table.name, columns)
        )
    return Resolved()


def _column(
    table: LogicalTable, read: Any, lineage: Any, resolved: Resolution, seen: set
) -> str | None:
    """The base column a read names on its own table, following members."""

    if read.qualifier is not None and read.qualifier.lower() != table.name.lower():
        return None
    target = table.member(read.name)
    if target is None or read.name.lower() in seen:
        return resolved.fold(read.name)
    if target.kind == METRIC:
        return None
    inner = lineage.plain(target.expr)
    if inner is None:
        return None
    return _column(table, inner, lineage, resolved, seen | {read.name.lower()})


# --- the read catalog -------------------------------------------------------------


def semantic_catalog(
    loaded: LoadResult, resolved: Resolution, *, notes: Sequence[str] = ()
) -> SemanticCatalogView:
    """The neutral read catalog for a validated set."""

    found: list[str] = [*notes, *resolved.notes]
    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    measures: list[MeasureInfo] = []
    metrics: list[MetricInfo] = []
    entities: dict[str, EntityInfo] = {}
    physical: dict[str, tuple[str, str]] = {}
    computed: dict[str, tuple[str, str]] = {}

    if any(m.kind == FACT for v in loaded.views for t in v.tables for m in t.facts):
        found.append(
            "a Semantic View fact is row-level and is queried through the FACTS "
            "clause, never beside a metric: facts are listed as dimension rows "
            "marked `kind: fact` and `groupable: false`"
        )
    if any(t.time_dimensions for v in loaded.views for t in v.tables):
        found.append(
            "a Semantic View declares no time grains: a time dimension is listed "
            "as `time` with no queryable grains"
        )

    for view in loaded.views:
        found.extend(_view_notes(view))
        groupable: dict[str, list[str]] = {}
        for table in view.tables:
            model = resolved.model(view, table.name)
            relation = resolved.relation(view, table.name)
            gate = resolved.gate(view, table.name)
            single_key = (
                table.primary_key[0]
                if table.primary_key and len(table.primary_key) == 1
                else None
            )
            models.append(
                SemanticModelInfo(
                    name=model,
                    description=table.description,
                    relation=relation,
                    primary_entity=model if single_key else None,
                )
            )
            if single_key is not None:
                column = resolved.fold(single_key)
                entities.setdefault(
                    model, EntityInfo(name=model, type="")
                ).roles.append(
                    EntityRole(
                        semantic_model=model,
                        type="primary",
                        expr=single_key,
                        column=column,
                    )
                )
            for member in (*table.dimensions, *table.time_dimensions, *table.facts):
                token = resolved.token(view, member)
                here = resolved.member(view, member)
                dimensions.append(
                    DimensionInfo(
                        name=token,
                        type="time" if member.kind == TIME_DIMENSION else "categorical",
                        description=member.description,
                        definition=member.name,
                        semantic_model=model,
                        column=here.column if relation else None,
                        vendor_params=_params(member),
                    )
                )
                _gate(token, here, gate, physical, computed)
            for member in table.metrics:
                here = resolved.member(view, member)
                _gate(resolved.token(view, member), here, gate, physical, computed)
                if here.aggregate is not None:
                    measures.append(
                        MeasureInfo(
                            name=member.name,
                            agg=here.aggregate.agg,
                            expr=member.expr,
                            description=member.description,
                            semantic_model=model,
                            column=here.column if relation else None,
                        )
                    )
        for table in view.tables:
            groupable[table.name.lower()] = _groupable(view, table, resolved)
        _foreign_roles(view, resolved, entities)
        for table in view.tables:
            metrics.extend(
                _table_metric(view, table, member, groupable, resolved)
                for member in table.metrics
            )
        metrics.extend(
            _derived_metric(view, member, groupable, resolved)
            for member in view.metrics
        )

    for entity in entities.values():
        entity.type = derive_entity_type(entity.roles)
    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        entities=list(entities.values()),
        measures=measures,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=found,
        physical_columns=physical,
        computed_columns=computed,
    )


def _gate(
    token: str,
    here: Resolved,
    gate: str | None,
    physical: dict[str, tuple[str, str]],
    computed: dict[str, tuple[str, str]],
) -> None:
    """Record where the PII gate screens ``token``: a column, or an expression."""
    if gate is None:
        return
    if here.column:
        physical[token] = (gate, here.column)
    elif here.expression:
        computed[token] = (gate, here.expression)


def _params(member: Member) -> dict[str, Any]:
    """What a Semantic View states about a member that the neutral model has no
    field for."""

    params: dict[str, Any] = {"expr": member.expr}
    if member.kind == FACT:
        params["kind"] = "fact"
        params["groupable"] = False
        params["combines_with"] = "dimensions and other facts; never a metric"
    if member.synonyms:
        params["synonyms"] = list(member.synonyms)
    if member.data_type:
        params["data_type"] = member.data_type
    if member.private:
        params["access"] = "private"
    if member.is_enum:
        params["is_enum"] = True
    if member.sample_value_count:
        # Carried as a count, never surfaced: Snowflake does not mask them.
        params["sample_values"] = {"count": member.sample_value_count}
    if member.non_additive:
        params["non_additive_dimensions"] = [dict(e) for e in member.non_additive]
    if member.using_relationships:
        params["using_relationships"] = list(member.using_relationships)
    if member.window:
        params["window"] = True
    if member.labels:
        params["labels"] = list(member.labels)
    return params


def _groupable(
    view: SemanticView, table: LogicalTable, resolved: Resolution
) -> list[str]:
    """The tokens a metric on ``table`` can be grouped by: the dimensions of its
    own table and of every table its relationships reach many-to-one."""

    reach = resolved.coarser.get((view.name.lower(), table.name.lower()), set())
    tokens: list[str] = []
    for other in view.tables:
        if other.name.lower() != table.name.lower() and other.name.lower() not in reach:
            continue
        tokens.extend(
            resolved.token(view, member)
            for member in (*other.dimensions, *other.time_dimensions)
        )
    return tokens


def _table_metric(
    view: SemanticView,
    table: LogicalTable,
    member: Member,
    groupable: dict[str, list[str]],
    resolved: Resolution,
) -> MetricInfo:
    """One table metric as the metric the catalog lists for it."""
    here = resolved.member(view, member)
    model = resolved.model(view, table.name)
    base: dict[str, Any] = {
        "name": resolved.token(view, member),
        "description": member.description,
        "dimensions": list(groupable[table.name.lower()]),
        "semantic_models": [model],
        "vendor_params": _params(member),
    }
    if here.aggregate is not None:
        return MetricInfo(
            type="simple",
            input_measures=[member.name],
            composition=MetricComposition(measure=member.name),
            **base,
        )
    return _composed(view, member, resolved, base)


def _derived_metric(
    view: SemanticView,
    member: Member,
    groupable: dict[str, list[str]],
    resolved: Resolution,
) -> MetricInfo:
    """One view-level metric: derived from the metrics it reads, groupable by
    what they all share."""

    inputs = metric_inputs(view, member, resolved)
    tables = _input_tables(view, member)
    shared: list[str] | None = None
    for table in tables:
        dims = groupable.get(table, [])
        shared = dims if shared is None else [d for d in shared if d in dims]
    base: dict[str, Any] = {
        "name": resolved.token(view, member),
        "description": member.description,
        "dimensions": shared or [],
        "semantic_models": sorted(
            {resolved.model(view, t) for t in tables if view.table(t) is not None}
        )
        or None,
        "vendor_params": _params(member),
    }
    return _composed(view, member, resolved, base, inputs)


def _composed(
    view: SemanticView,
    member: Member,
    resolved: Resolution,
    base: dict[str, Any],
    inputs: list[str] | None = None,
) -> MetricInfo:
    """A metric built from other metrics: a ``ratio`` where it is exactly one
    metric divided by another, ``derived`` otherwise."""

    from . import lineage
    from .loader import resolve_read

    inputs = metric_inputs(view, member, resolved) if inputs is None else inputs
    sides = None if member.window else lineage.ratio_sides(member.expr)
    if sides is not None:
        targets = [resolve_read(view, member, s.qualifier, s.name) for s in sides]
        if all(
            t.member is not None and t.member.kind in (METRIC, DERIVED) for t in targets
        ):
            numerator, denominator = (
                resolved.token(view, t.member)  # type: ignore[arg-type]
                for t in targets
            )
            return MetricInfo(
                type="ratio",
                input_measures=underlying(view, member, resolved) or None,
                composition=MetricComposition(
                    numerator=numerator, denominator=denominator
                ),
                **base,
            )
    return MetricInfo(
        type="derived",
        input_measures=underlying(view, member, resolved) or None,
        composition=MetricComposition(expr=member.expr, input_metrics=inputs or None),
        **base,
    )


def _targets(view: SemanticView, member: Member) -> list[Member]:
    """The metrics (table or derived) an expression reads."""
    from . import lineage
    from .loader import resolve_read

    found: list[Member] = []
    for read in lineage.reads(member.expr) or []:
        target = resolve_read(view, member, read.qualifier, read.name)
        if target.member is not None and target.member.kind in (METRIC, DERIVED):
            found.append(target.member)
    return found


def metric_inputs(
    view: SemanticView, member: Member, resolved: Resolution
) -> list[str]:
    """The tokens of the metrics an expression reads, in order, once each."""
    return list(dict.fromkeys(resolved.token(view, m) for m in _targets(view, member)))


def _input_tables(view: SemanticView, member: Member) -> list[str]:
    """The logical tables a derived metric's inputs sit on, followed through
    other derived metrics."""

    found: list[str] = []
    pending, seen = [member], set()
    while pending:
        current = pending.pop(0)
        for target in _targets(view, current):
            if target.table is not None:
                found.append(target.table.lower())
            elif target.name.lower() not in seen:
                seen.add(target.name.lower())
                pending.append(target)
    return list(dict.fromkeys(found))


def underlying(view: SemanticView, member: Member, resolved: Resolution) -> list[str]:
    """The aggregating table metrics a composed metric is made of, followed
    through other composed metrics."""

    found: list[str] = []
    pending, seen = [member], set()
    while pending:
        current = pending.pop(0)
        for target in _targets(view, current):
            identity = (target.table or "", target.name.lower())
            if identity in seen:
                continue
            seen.add(identity)
            if resolved.member(view, target).aggregate is not None:
                found.append(target.name)
            else:
                pending.append(target)
    return found


def _foreign_roles(
    view: SemanticView, resolved: Resolution, entities: dict[str, EntityInfo]
) -> None:
    """Each single-pair relationship to a table's single-column primary key, as
    a foreign declaration of that table's entity on the referencing table."""

    for rel in view.relationships:
        right = view.table(rel.right_table)
        if (
            right is None
            or not rel.equi
            or len(rel.columns) != 1
            or not right.primary_key
            or len(right.primary_key) != 1
            or rel.columns[0].right.lower() != right.primary_key[0].lower()  # type: ignore[union-attr]
        ):
            continue
        entity = entities.get(resolved.model(view, right.name))
        if entity is None:
            continue
        left = rel.columns[0].left
        entity.roles.append(
            EntityRole(
                semantic_model=resolved.model(view, rel.left_table),
                type="foreign",
                expr=left,
                column=resolved.fold(left),
            )
        )


def _view_notes(view: SemanticView) -> list[str]:
    """What a view declares that the catalog carries without modelling it."""

    notes: list[str] = []
    prefix = f"view '{view.name}'"
    notes.extend(
        f"{prefix} declares `{key}`, which only Cortex Analyst reads; it is carried "
        "and not modelled"
        for key in CORTEX_ONLY
        if key in view.carried
    )
    notes.extend(
        f"{prefix} declares `{key}`, which the catalog has no place for; it is "
        "carried and not modelled"
        for key in UNSUPPORTED
        if key in view.carried
    )
    for rel in view.relationships:
        kinds = sorted({p.kind for p in rel.columns} - {"equal"})
        if kinds:
            notes.append(
                f"{prefix}: relationship '{rel.name}' is {' and '.join(kinds)}, which "
                "dex carries and does not model as a key join"
            )
    for table in view.tables:
        where = f"{prefix}: table '{table.name}'"
        if table.filters:
            notes.append(
                f"{where} declares {len(table.filters)} filter(s), which Cortex "
                "Analyst applies; they are carried and not modelled"
            )
        if table.ranges:
            notes.append(
                f"{where} declares a distinct range constraint, carried for its "
                "range relationship"
            )
        for member in table.members():
            what = f"{where}: {member.kind.replace('_', ' ')} '{member.name}'"
            if member.window:
                notes.append(
                    f"{what} is a window function metric: listed as derived with its "
                    "expression, not modelled"
                )
            if member.non_additive:
                notes.append(
                    f"{what} is non-additive over "
                    + ", ".join(
                        f"{e['table']}.{e['dimension']}" for e in member.non_additive
                    )
                    + " (`NON ADDITIVE BY`): carried in vendor_params, not modelled"
                )
            if member.using_relationships:
                notes.append(
                    f"{what} names its join path (`using_relationships`): carried, "
                    "not modelled"
                )
            if "lod_dimension_type" in member.raw or "lod_dimensions" in member.raw:
                notes.append(
                    f"{what} is a level-of-detail expression: carried, not modelled"
                )
            if member.labels:
                notes.append(f"{what} carries labels ({', '.join(member.labels)})")
            if member.private:
                notes.append(
                    f"{what} is private: other expressions read it, and it is never "
                    "queried directly"
                )
    notes.extend(
        f"{prefix}: metric '{member.name}' is private: other expressions read it, "
        "and it is never queried directly"
        for member in view.metrics
        if member.private
    )
    return notes


# --- the declaration channel ----------------------------------------------------


@dataclass(frozen=True)
class DeclaredJoin:
    """One key join as the declaration channel and the fingerprint read it."""

    view: SemanticView
    relationship: Relationship
    declared: DeclaredRelationship
    cardinality: str | None


def declared_relationships(
    loaded: LoadResult, resolved: Resolution
) -> list[DeclaredJoin]:
    """Every relationship whose pairs are all key equalities, written child
    (left) to parent (right), with its arity.

    ``many_to_one``, or ``one_to_one`` where the left columns are themselves a
    key of the left table, or ``many_to_many`` where the right columns are no
    key. ASOF and range relationships join on something other than equality and
    are named in the notes instead.
    """

    found: list[DeclaredJoin] = []
    for view in loaded.views:
        for rel in view.relationships:
            if not rel.equi:
                continue
            left, right = view.table(rel.left_table), view.table(rel.right_table)
            if left is None or right is None:
                continue
            if not right.is_key(rel.right_columns):
                arity = "many_to_many"
            elif left.is_key(rel.left_columns):
                arity = "one_to_one"
            else:
                arity = "many_to_one"
            found.append(
                DeclaredJoin(
                    view=view,
                    relationship=rel,
                    declared=DeclaredRelationship(
                        model=resolved.model(view, left.name),
                        relation=resolved.relation(view, left.name),
                        to_model=resolved.model(view, right.name),
                        to_relation=resolved.relation(view, right.name),
                        column_pairs=[
                            (resolved.fold(p.left), resolved.fold(p.right))  # type: ignore[arg-type]
                            for p in rel.columns
                        ],
                        source=DECLARATION_SOURCE,
                        name=f"{view.name}.{rel.name}",
                    ),
                    cardinality=arity,
                )
            )
    return found


def definitions(
    loaded: LoadResult, resolved: Resolution, *, notes: Sequence[str] = ()
) -> ProjectDefinitions:
    """Declared keys and joins, for grain detection and the relationship channel."""

    keys: list[DeclaredKey] = []
    composite: list[DeclaredCompositeKey] = []
    foreign: list[DeclaredForeignKey] = []
    relationships: list[DeclaredRelationship] = []
    relations: dict[str, str] = {}
    declarations: dict[str, ModelDeclaration] = {}
    for view in loaded.views:
        for table in view.tables:
            model = resolved.model(view, table.name)
            relation = resolved.relation(view, table.name)
            if relation:
                relations[model] = relation
            if table.primary_key:
                # A primary key is a grain claim (#976). A Semantic View has no
                # metadata block a dex declaration could live in, so the key is
                # the whole declaration.
                declaration, _ = read_native_declaration(
                    model,
                    relation,
                    [resolved.fold(c) for c in table.primary_key],
                    None,
                    [],
                    source=DECLARATION_SOURCE,
                )
                if declaration is not None:
                    declarations[model] = declaration
            for columns, primary in [
                *([(table.primary_key, True)] if table.primary_key else []),
                *((key, False) for key in table.unique_keys),
            ]:
                folded = [resolved.fold(c) for c in columns]
                if len(folded) == 1:
                    keys.append(
                        DeclaredKey(
                            model=model,
                            relation=relation,
                            column=folded[0],
                            unique=True,
                            primary=primary,
                            source=DECLARATION_SOURCE,
                        )
                    )
                else:
                    composite.append(
                        DeclaredCompositeKey(
                            model=model,
                            relation=relation,
                            columns=folded,
                            claim="primary" if primary else "unique",
                            source=DECLARATION_SOURCE,
                        )
                    )
    for declared in declared_relationships(loaded, resolved):
        relationship = declared.declared
        relationships.append(relationship)
        if (
            len(relationship.column_pairs) == 1
            and declared.cardinality != "many_to_many"
        ):
            ((column, to_column),) = relationship.column_pairs
            foreign.append(
                DeclaredForeignKey(
                    model=relationship.model,
                    relation=relationship.relation,
                    column=column,
                    to_model=relationship.to_model,
                    to_relation=relationship.to_relation,
                    to_column=to_column,
                    source=DECLARATION_SOURCE,
                )
            )

    found = [*notes, *resolved.notes]
    if loaded.views:
        found.append(
            "a Semantic View declares the relations it reads and builds none, so it "
            "contributes no built-relation names and cannot tell explore that a "
            "warehouse relation is unaccounted for"
        )
    return ProjectDefinitions(
        present=True,
        relationship_source=DECLARATION_SOURCE if loaded.views else None,
        semantic_source=DECLARATION_SOURCE if loaded.views else None,
        foreign_keys=foreign,
        declared_relationships=relationships,
        declared_keys=keys,
        declared_composite_keys=composite,
        source_relations=relations,
        model_declarations=declarations,
        notes=found,
    )
