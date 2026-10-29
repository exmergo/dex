"""Normalizing validated SLayer models into what dex reads.

Two channels from the same validated models, as for Ossie, because dex asks a
semantic source two differently shaped questions:

- :func:`semantic_catalog` builds the neutral read catalog `explore semantic
  list` renders.
- :func:`definitions` builds the declaration channel: declared keys and joins,
  and nothing else.

**How SLayer maps, and where it does not.** dex's catalog is shaped by
MetricFlow, and SLayer is shaped differently:

- A ``SlayerModel`` is a semantic model, on the relation its ``sql_table``
  names. A model backed by ``sql`` or ``source_queries`` has no relation to
  name, and says so in a note.
- A ``Column`` is a dimension. SLayer also lets a query aggregate any column,
  but declares no split between the two, so a column is not *also* invented as
  a measure.
- A saved ``ModelMeasure`` is a named formula that a query references by name,
  which is what a metric is here. It declares no aggregation of its own (the
  formula may combine several), so it is not a catalog measure, and the catalog
  carries no measures at all: the gap is declared rather than filled.
- A custom ``Aggregation`` is a function a formula applies to a column, not an
  element that answers a number, so it is named in a note rather than listed.
- A metric's ``filter`` is the model's ``filters``, plus a column ``filter``
  where every aggregation in the formula reads a column carrying that one
  filter: SLayer has no per-measure filter, and a filtered column is where it
  puts a predicate meant for one metric.
- ``default_time_dimension`` is per model rather than per measure, and becomes
  the model's time dimension and the time axis of every metric on it.
- SLayer has no entities. Joins and declared keys reach dex through the
  declaration channel instead.

**What is never read.** A ``Column`` can carry ``sampled``, ``sampled_values``
and ``distinct_count``: values and cardinalities SLayer cached from the
warehouse. Data values reach agent context only through `explore query`'s
firewall, so none of them is carried into the catalog, the declarations, or the
fingerprint. ``meta`` is free-form and is not carried either.

Nothing here imports SLayer. The models arrive already validated, and only
their attributes are read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..adapters import fold_identifier, normalize_relation
from ..project_definitions import (
    DeclaredCompositeKey,
    DeclaredForeignKey,
    DeclaredKey,
    DeclaredRelationship,
    ProjectDefinitions,
)
from ..semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DimensionInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    column_reference,
)

if TYPE_CHECKING:
    from .loader import LoadedModel

__all__ = [
    "DECLARATION_SOURCE",
    "METRIC_TYPE",
    "definitions",
    "dimension_token",
    "semantic_catalog",
]

#: What `ProjectDefinitions` records as the channel a declaration came from.
DECLARATION_SOURCE = "slayer"

#: The catalog type of a saved SLayer measure: a named formula, which may
#: aggregate one column or combine several aggregations.
METRIC_TYPE = "formula"

# SLayer's temporal data types, the ones its own `effective_default_time_dimension`
# treats as time.
_TEMPORAL = frozenset({"DATE", "TIMESTAMP"})

# One unquoted identifier part, the shape `parse_relation` accepts.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def dimension_token(model: str, column: str) -> str:
    """The reference a SLayer query uses for a model's column: ``model.column``.

    SLayer resolves a dotted reference by model name, which is also why a model
    name may not contain a dot.
    """

    return f"{model}.{column}"


def semantic_catalog(
    loaded: Sequence[LoadedModel],
    *,
    connector: str | None = None,
    notes: Sequence[str] = (),
) -> SemanticCatalogView:
    """The neutral read catalog for a set of validated models."""

    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    metrics: list[MetricInfo] = []
    physical: dict[str, tuple[str, str]] = {}
    computed_columns: dict[str, tuple[str, str]] = {}
    found: list[str] = list(notes)

    for entry in loaded:
        model = entry.model
        relation, relation_note = _relation(model, connector)
        if relation_note:
            found.append(relation_note)
        # What the PII gate resolves against, which may be the partial name the
        # catalog declines to claim as a relation: the gate matches it against
        # the profiled cache by suffix, failing closed when it is ambiguous.
        gate_relation = relation or _partial_relation(model)
        time_dimension = _time_dimension(model)
        models.append(
            SemanticModelInfo(
                name=model.name,
                description=model.description,
                agg_time_dimension=time_dimension,
                relation=relation,
            )
        )

        tokens: list[str] = []
        computed: list[str] = []
        for col in model.columns:
            gate_column = physical_column(col, connector)
            column = gate_column if relation else None
            if gate_relation and gate_column is None:
                computed.append(col.name)
            token = dimension_token(model.name, col.name)
            tokens.append(token)
            is_time = _type(col) in _TEMPORAL
            dimensions.append(
                DimensionInfo(
                    name=token,
                    type="time" if is_time else "categorical",
                    label=col.label,
                    description=col.description,
                    definition=col.name,
                    semantic_model=model.name,
                    # Empty is the positive statement that a categorical column
                    # has no grain. A time column's grains are not declared per
                    # column in SLayer, so absent is the honest answer there.
                    queryable_granularities=None if is_time else [],
                    column=column,
                    vendor_params=_column_params(col),
                )
            )
            if gate_column and gate_relation:
                physical[token] = (gate_relation, gate_column)
            elif gate_relation and isinstance(col.sql, str) and col.sql.strip():
                # Not a column to name, but an expression the gate can read: it
                # screens every column the expression reads instead of the name.
                computed_columns[token] = (gate_relation, col.sql)
        if computed:
            one = len(computed) == 1
            found.append(
                f"model '{model.name}': {', '.join(computed)} "
                f"{'is' if one else 'are'} computed or quoted rather than a bare "
                f"column, so {'it carries' if one else 'they carry'} no physical "
                "column. A column guessed out of an expression makes the PII gate "
                "screen the wrong one and report it as evidence"
            )
        if model.aggregations:
            names = ", ".join(a.name for a in model.aggregations)
            found.append(
                f"model '{model.name}' defines aggregations {names}: functions a "
                "formula applies to a column, such as `name(column)`, rather than "
                "elements that answer a number, so they are not listed as metrics"
            )
        if model.hidden:
            found.append(
                f"model '{model.name}' is hidden in SLayer: it stays queryable and "
                "is listed here, but SLayer leaves it out of its own listings"
            )

        for measure in model.measures:
            metric_filter, filter_note = _metric_filter(entry, measure)
            if filter_note:
                found.append(filter_note)
            metrics.append(
                _metric(model.name, measure, tokens, metric_filter, time_dimension)
            )

    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=found,
        physical_columns=physical,
        computed_columns=computed_columns,
    )


def _metric(
    model: str,
    measure: Any,
    tokens: list[str],
    metric_filter: str | None,
    time_dimension: str | None,
) -> MetricInfo:
    """One saved measure as a formula metric."""

    return MetricInfo(
        name=dimension_token(model, str(measure.name)),
        type=METRIC_TYPE,
        label=measure.label,
        description=measure.description,
        # A saved measure is queried from its own model, so every column of that
        # model groups it. Columns reached through a join are not listed: which
        # paths SLayer accepts depends on join arity it resolves at query time,
        # which is what `dimension_scope: declarations` says.
        dimensions=list(tokens),
        semantic_models=[model],
        composition=MetricComposition(expr=measure.template),
        filter=metric_filter,
        time_axis=[time_dimension] if time_dimension else None,
        vendor_params=_measure_params(measure),
    )


def physical_column(col: Any, connector: str | None = None) -> str | None:
    """The single physical column behind a SLayer column, or ``None``.

    The catalog's one rule (:func:`~..semantic_catalog.column_reference`): no
    ``sql`` means the column's own name, a bare identifier is that column, and
    anything else, a quoted identifier included, resolves to nothing. SLayer
    unquotes ``"Name"`` itself, but whether a quoted name and its unquoted twin
    are one column is the warehouse's folding rule, and dex cannot tell which the
    author meant.

    A bare identifier is the column the warehouse folded it to, so it is folded
    the way ``connector`` resolves it (``CUSTOMER_ID`` on Snowflake); a quoted
    identifier resolves to nothing and is left alone.

    A column carrying a ``filter`` keeps its column. Its values are a subset of
    that column's, so the PII gate screening that column screens the right one.
    """

    resolved = column_reference(col.sql, col.name)
    return fold_identifier(connector, resolved) if resolved else None


def _relation(model: Any, connector: str | None) -> tuple[str | None, str | None]:
    """The relation a model sits on, and a note when it has none dex can name."""

    if not model.sql_table:
        kind = "SQL" if model.sql else "source queries"
        return None, (
            f"model '{model.name}' is backed by {kind} rather than a table, so it "
            "carries no physical relation. A query read as a relation would reach "
            "the PII gate as false evidence"
        )
    relation = normalize_relation(connector, model.sql_table) if connector else None
    if relation is None:
        return None, (
            f"model '{model.name}' reads sql_table '{model.sql_table}', which dex "
            f"cannot read as one fully qualified relation on "
            f"{connector or 'this connector'}, so it carries no physical link. "
            "SLayer completes a partial name from its datasource, and dex does "
            "not read datasources, which carry credentials. The PII gate still "
            "matches an unquoted partial name against the profiled cache by "
            "suffix"
        )
    return relation, None


def _partial_relation(model: Any) -> str | None:
    """A ``sql_table`` too short to be a relation, kept for the PII gate alone.

    ``main.orders`` or ``orders`` names a relation dex cannot qualify without
    SLayer's datasource, so the catalog claims none. The gate needs no claim: it
    matches a name against the profiled cache by suffix, as ``explore profile``
    resolves its arguments, and with nothing to match it falls to the heuristic.
    Only unquoted dotted identifiers qualify; anything else names no column dex
    can look up, and leaving it out keeps the gate from screening a guess.
    """

    table = model.sql_table
    if not table:
        return None
    parts = table.split(".")
    if not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    return table


def _time_dimension(model: Any) -> str | None:
    """The model's time dimension, as SLayer itself resolves it.

    The declared ``default_time_dimension`` when it names a column, else the
    model's only date or timestamp column, which is SLayer's own
    ``effective_default_time_dimension`` rule. A declared name matching no column
    is reported by the loader and yields nothing here rather than a dangling
    name.
    """

    declared = model.default_time_dimension
    if declared:
        return declared if model.get_column(declared) is not None else None
    return model.effective_default_time_dimension


def _type(col: Any) -> str:
    return str(getattr(col.type, "value", col.type))


def _metric_filter(entry: LoadedModel, measure: Any) -> tuple[str | None, str | None]:
    """The predicate a saved measure measures under, and a note when part of it
    could not be read.

    Two places in SLayer narrow a metric. A model's ``filters`` apply to every
    query on the model. A column's ``filter`` applies wherever that column is
    aggregated (SLayer desugars it to ``CASE WHEN filter THEN value END``), which
    is where SLayer puts a predicate meant for one metric rather than for every
    metric on the model. So a formula whose every aggregation reads a column
    carrying one same filter measures that subset as a whole, and the filter is
    the metric's. Where only some aggregations are filtered (a ratio of a
    filtered count to an unfiltered one), the predicate narrows a part of the
    number rather than the number, and is not reported as the metric's, which is
    how a MetricFlow ratio over a filtered side reads too.
    """

    model = entry.model
    filters = list(model.filters)
    columns = entry.aggregated_columns(measure)
    if columns is None:
        note = None
        if any(col.filter for col in model.columns):
            note = (
                f"model '{model.name}': the formula of saved measure "
                f"'{measure.name}' could not be read, so whether it aggregates a "
                "filtered column is unknown and its filter states only the "
                "model's own filters"
            )
        return _filter(filters), note
    # `*` and a dotted name resolve to no column of this model, so a formula
    # reading one is never wholly narrowed by a column filter here.
    found = {
        (col.filter if col is not None else None)
        for col in (model.get_column(name) for name in columns)
    }
    if columns and len(found) == 1 and (shared := found.pop()):
        filters.append(shared)
    return _filter(filters), None


def _filter(filters: Sequence[str]) -> str | None:
    """Always-applied predicates, as one.

    SLayer applies every model filter to every query on the model, so a metric
    there measures the subset they select, which is what ``filter`` discloses.
    """

    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return " AND ".join(f"({f})" for f in filters)


def _column_params(col: Any) -> dict[str, Any] | None:
    """What SLayer states about a column that the neutral model has no field for.

    Never ``sampled``, ``sampled_values`` or ``distinct_count``: those are values
    cached from the warehouse, and values reach agent context only through the
    query firewall.
    """

    params: dict[str, Any] = {"data_type": _type(col)}
    if col.sql is not None:
        params["sql"] = col.sql
    if col.db_type:
        params["db_type"] = col.db_type
    if col.filter:
        params["filter"] = col.filter
    if col.granularity is not None:
        params["granularity"] = str(col.granularity)
    if col.allowed_aggregations is not None:
        params["allowed_aggregations"] = list(col.allowed_aggregations)
    if col.hidden:
        params["hidden"] = True
    return params


def _measure_params(measure: Any) -> dict[str, Any] | None:
    params: dict[str, Any] = {"formula": measure.template}
    if measure.type is not None:
        params["data_type"] = str(getattr(measure.type, "value", measure.type))
    return params


# --- the declaration channel ---------------------------------------------------


def definitions(
    loaded: Sequence[LoadedModel],
    *,
    connector: str | None = None,
    notes: Sequence[str] = (),
    present: bool = True,
) -> ProjectDefinitions:
    """Declared keys and joins, for grain detection and the relationship channel."""

    keys: list[DeclaredKey] = []
    composite: list[DeclaredCompositeKey] = []
    joins: list[DeclaredForeignKey] = []
    relationships: list[DeclaredRelationship] = []
    relations: dict[str, str] = {}
    found: list[str] = list(notes)
    by_name = {entry.model.name: entry.model for entry in loaded}

    for entry in loaded:
        relation, _ = _relation(entry.model, connector)
        if relation:
            relations[entry.model.name] = relation

    for entry in loaded:
        model = entry.model
        relation = relations.get(model.name)
        for columns in declared_key_columns(model, connector):
            if len(columns) == 1:
                keys.append(
                    DeclaredKey(
                        model=model.name,
                        relation=relation,
                        column=columns[0],
                        unique=True,
                        source=DECLARATION_SOURCE,
                    )
                )
            else:
                composite.append(
                    DeclaredCompositeKey(
                        model=model.name,
                        relation=relation,
                        columns=columns,
                        source=DECLARATION_SOURCE,
                    )
                )
        for join in model.joins:
            declared, note = declared_relationship(
                model, join, by_name, relations, connector
            )
            if note:
                found.append(note)
            if declared is None:
                continue
            relationships.append(declared)
            single = _foreign_key(declared, join)
            if single is not None:
                joins.append(single)

    if loaded:
        found.append(
            "a SLayer model declares the relation it reads and builds none, so it "
            "contributes no built-relation names and cannot tell explore that a "
            "warehouse relation is unaccounted for"
        )

    return ProjectDefinitions(
        present=present,
        relationship_source=DECLARATION_SOURCE if loaded else None,
        semantic_source=DECLARATION_SOURCE if loaded else None,
        foreign_keys=joins,
        declared_relationships=relationships,
        declared_keys=keys,
        declared_composite_keys=composite,
        source_relations=relations,
        notes=found,
    )


def declared_key_columns(model: Any, connector: str | None = None) -> list[list[str]]:
    """Every column combination the model declares unique, as physical columns.

    The primary key is one declaration, however many columns it has: a sole
    primary-key column is a single key, several are one composite grain in
    declared order, and **never also** several single keys, which would claim
    each member unique on its own. ``unique`` is SLayer's single-column claim and
    stands alone, including on a composite member where the author made it.

    A key column with no physical column behind it cannot be checked against
    the warehouse, so a declaration containing one is left out rather than
    recorded against a guessed column.
    """

    declared: list[list[str]] = []
    primary = [physical_column(c, connector) for c in model.columns if c.primary_key]
    if primary and all(primary):
        declared.append([str(c) for c in primary])
    for col in model.columns:
        if not col.unique:
            continue
        column = physical_column(col, connector)
        if column and [column] not in declared:
            declared.append([column])
    return declared


def declared_relationship(
    model: Any,
    join: Any,
    by_name: dict[str, Any],
    relations: dict[str, str],
    connector: str | None = None,
) -> tuple[DeclaredRelationship | None, str | None]:
    """One SLayer join with every ordered pair, as physical columns.

    Written child to parent, the direction the relationship channel reads: the
    joining model is the child, except where the author declared
    ``one_to_many``, which says the joining model is the one side. A join with
    no declared cardinality keeps the direction it was written in, and its arity
    stays undetermined rather than assumed.
    """

    target = by_name.get(join.target_model)
    if target is None:
        return None, None
    pairs: list[tuple[str, str]] = []
    for source_key, target_key in join.join_pairs:
        frm = _column_named(model, source_key, connector)
        to = _column_named(target, target_key, connector)
        if frm is None or to is None:
            return None, (
                f"join {model.name} -> {target.name} keys on a column with no "
                "physical column behind it, so it is not declared as an edge"
            )
        pairs.append((frm, to))

    child, parent = model.name, target.name
    if _cardinality(join) == "one_to_many":
        child, parent = parent, child
        pairs = [(to, frm) for frm, to in pairs]
    return (
        DeclaredRelationship(
            model=child,
            relation=relations.get(child),
            to_model=parent,
            to_relation=relations.get(parent),
            column_pairs=pairs,
            source=DECLARATION_SOURCE,
            name=join.name,
        ),
        None,
    )


def _foreign_key(
    declared: DeclaredRelationship, join: Any
) -> DeclaredForeignKey | None:
    """A single-pair join as a foreign key, where that claim holds.

    A foreign key says the parent side is a key. ``many_to_many`` says it is
    not, so that join stays a relationship only. A composite join stays one too:
    half of it written as a foreign key is a join the author never declared.
    """

    if len(declared.column_pairs) != 1 or _cardinality(join) == "many_to_many":
        return None
    ((column, to_column),) = declared.column_pairs
    return DeclaredForeignKey(
        model=declared.model,
        relation=declared.relation,
        column=column,
        to_model=declared.to_model,
        to_relation=declared.to_relation,
        to_column=to_column,
        source=DECLARATION_SOURCE,
    )


def _cardinality(join: Any) -> str | None:
    value = join.cardinality
    return None if value is None else str(getattr(value, "value", value))


def _column_named(model: Any, name: str, connector: str | None = None) -> str | None:
    col = model.get_column(name)
    return physical_column(col, connector) if col is not None else None
