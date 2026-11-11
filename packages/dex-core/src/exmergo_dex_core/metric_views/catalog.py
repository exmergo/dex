"""Normalizing a validated set of Metric Views into what dex reads.

Two channels from one validated set, as for Ossie, SLayer and Cube:

- :func:`semantic_catalog` builds the neutral read catalog `explore semantic
  list` renders.
- :func:`definitions` builds the declaration channel: declared keys and joins,
  and nothing else.

**How a Metric View maps.** The mapping follows the construct table of the
``databricks`` spoke of apache/ossie-converters (``databricks/java/README.md``,
at :data:`.dependency.CONVERTER_COMMIT`), so a view and its Ossie rendering agree
on what each construct means:

- A view is one semantic model, named after its file, on the relation its
  ``source`` names. A query source has one only where it is a plain projection of
  one table.
- A field is a dimension, queried as ``view.field``. ``display_name`` is its
  label, ``comment`` its description, ``synonyms`` and ``format`` ride in
  ``vendor_params``. A field is ``time`` only where its ``format`` says date or
  date_time: Metric Views type nothing else.
- A measure that is one aggregate is a catalog measure **and** a ``simple``
  metric reading it. ``MEASURE(a) / MEASURE(b)`` is a ``ratio``; any other
  composition is ``derived``. A ``window`` or ``partition`` measure is listed as
  a ``derived`` metric with its spec carried verbatim, and not modelled.
- Each join is a declared relationship from the table it hangs off to the table
  it joins, with every column pair, and ``rely.at_most_one_match: true`` is a
  declared key on the joined side's columns: that is what the flag asserts, and
  what Databricks does not check. A joined table that is another configured
  view's source is that view, so the views form one graph.
- Entities are a declared gap, never synthesized. ``filter`` applies to every
  query of the view and is carried on each of its metrics. ``parameters``,
  ``materialization`` and wildcards are named in the notes.

**Columns are claimed only on the model's own relation.** A field read through a
join sits on another table, so its catalog ``column`` is empty; the PII gate
still reaches the joined table's column through ``physical_columns``.

Nothing here imports sqlglot: :mod:`.lineage`, which does, is imported inside
:func:`resolution`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
)
from .model import Join, Member, MetricView

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = [
    "DECLARATION_SOURCE",
    "Resolution",
    "declared_relationships",
    "definitions",
    "member_token",
    "resolution",
    "semantic_catalog",
]

#: What `ProjectDefinitions` records as the channel a declaration came from.
DECLARATION_SOURCE = "databricks"

_SOURCE: tuple[str, ...] = ()


def member_token(view: str, member: str) -> str:
    """The reference dex uses for a field or a measure: ``view.member``."""

    return f"{view}.{member}"


@dataclass(frozen=True)
class Table:
    """A view's source or a join's, resolved for the active connector."""

    relation: str | None
    gate: str | None
    columns: dict[str, str] | None = None
    note: str | None = None


@dataclass(frozen=True)
class Resolved:
    """Where one field or measure reads from: the join path that owns the column
    (``()`` for the view's source), the column, and, for a computed field, its
    SQL over the owner's columns."""

    owner: tuple[str, ...] | None
    column: str | None = None
    expression: str | None = None
    aggregate: Any = None
    measure_refs: tuple[str, ...] = ()
    ratio: tuple[str, str] | None = None


@dataclass
class Resolution:
    """Every relation, column and join pair the set resolves to, computed once,
    so the catalog, the declarations and the fingerprint cannot disagree."""

    tables: dict[tuple[str, tuple[str, ...]], Table] = field(default_factory=dict)
    members: dict[tuple[str, str], Resolved] = field(default_factory=dict)
    pairs: dict[tuple[str, tuple[str, ...]], list[tuple[str, str]] | None] = field(
        default_factory=dict
    )
    notes: list[str] = field(default_factory=list)

    def table(self, view: str, path: tuple[str, ...] = _SOURCE) -> Table:
        """The resolved source of the view (``path=()``) or of one of its joins."""
        return self.tables.get((view, path), Table(None, None))

    def column(self, view: str, member: str) -> str | None:
        """The column the catalog may claim: only on the view's own relation."""

        found = self.members.get((view, member))
        if found is None or found.owner != _SOURCE or not self.table(view).relation:
            return None
        return found.column

    def gate(self, view: str, member: str) -> tuple[str, str] | None:
        """``(relation, column)`` the PII gate screens the member as."""

        found = self.members.get((view, member))
        if found is None or found.owner is None or not found.column:
            return None
        table = self.table(view, found.owner)
        return (table.gate, found.column) if table.gate else None

    def computed(self, view: str, member: str) -> tuple[str, str] | None:
        """``(relation, SQL)`` the PII gate parses for a computed field."""
        found = self.members.get((view, member))
        if found is None or found.owner is None or not found.expression:
            return None
        table = self.table(view, found.owner)
        return (table.gate, found.expression) if table.gate else None


# --- resolution -----------------------------------------------------------------


def resolution(loaded: LoadResult, connector: str | None) -> Resolution:
    """Resolve the set, through :mod:`.lineage` (the one sqlglot importer)."""

    from ..adapters import fold_identifier
    from . import lineage

    def fold(column: str) -> str:
        """A column name spelled the way the connector folds an unquoted identifier."""
        return fold_identifier(connector, column)

    resolved = Resolution()
    for view in loaded.views:
        resolved.tables[(view.name, _SOURCE)] = _table(
            lineage.source_relation(view.source),
            connector,
            f"view '{view.name}'",
            fold,
            resolved.notes,
        )
        for join in view.all_joins():
            resolved.tables[(view.name, join.path)] = _table(
                lineage.source_relation(join.source),
                connector,
                f"view '{view.name}': join '{join.dotted}'",
                fold,
                resolved.notes,
            )
            parents: tuple[tuple[str, ...], ...] = (
                (join.parent, join.parent[-1:]) if join.parent else (("source",),)
            )
            pairs, note = lineage.join_pairs(
                join.on, join.using, join.name, join.path, parents
            )
            if note:
                resolved.notes.append(f"view '{view.name}': {note}")
            resolved.pairs[(view.name, join.path)] = (
                [(fold(a), fold(b)) for a, b in pairs] if pairs else None
            )
        earlier: dict[str, Resolved] = {}
        for member in view.fields:
            found = _resolve_member(view, member, earlier, resolved, lineage, fold)
            resolved.members[(view.name, member.name)] = found
            earlier[member.name.lower()] = found
        for member in view.measures:
            resolved.members[(view.name, member.name)] = _resolve_measure(
                view, member, earlier, resolved, lineage, fold
            )
    return resolved


def _table(
    found: Any, connector: str | None, what: str, fold: Any, notes: list[str]
) -> Table:
    """A lineage `SourceRelation`, with its names spelled for ``connector``."""

    from ..adapters import normalize_relation

    if found.note:
        notes.append(f"{what}: {found.note}")
    columns = (
        {fold(k): fold(v) for k, v in found.columns.items()}
        if found.columns is not None
        else None
    )

    def spelled(name: str | None) -> str | None:
        """A relation name normalized for the connector, or ``None``."""
        if name is None:
            return None
        plain = name.replace("`", "")
        return normalize_relation(connector, plain) if connector else None

    relation = spelled(found.relation)
    gate = spelled(found.gate) or (found.gate.replace("`", "") if found.gate else None)
    if found.relation and relation is None:
        notes.append(
            f"{what}: source `{found.relation}` is not one fully qualified relation "
            f"on {connector or 'this connector'}, so it carries no physical link. "
            "Databricks completes a partial name from the session's catalog and "
            "schema; the PII gate still matches it against the profiled cache by "
            "suffix"
        )
    return Table(relation=relation, gate=gate, columns=columns)


def _owner(view: MetricView, qualifier: tuple[str, ...]) -> tuple[str, ...] | None:
    """The join path a qualifier names, ``()`` for the source, else ``None``."""

    lowered = tuple(q.lower() for q in qualifier)
    if lowered in ((), ("source",)):
        return _SOURCE
    join = view.join(qualifier)
    return join.path if join else None


def _through(table: Table, column: str) -> str | None:
    """A column of a source, through a projection query's output names."""

    if table.columns is None:
        return column
    return table.columns.get(column)


def _resolve_member(
    view: MetricView,
    member: Member,
    earlier: Mapping[str, Resolved],
    resolved: Resolution,
    lineage: Any,
    fold: Any,
) -> Resolved:
    """Where one field reads from: its owner's column, or its SQL over one table."""
    plain = lineage.plain_column(member.expr)
    if plain is not None:
        if not plain.qualifier and plain.column.lower() in earlier:
            # Fields may read earlier fields; a bare name that is one is that
            # field, not a source column of the same name.
            return earlier[plain.column.lower()]
        owner = _owner(view, plain.qualifier)
        if owner is None:
            return Resolved(owner=None)
        column = _through(resolved.table(view.name, owner), fold(plain.column))
        return Resolved(owner=owner, column=column)
    refs = lineage.column_refs(member.expr) or []
    owners = {_owner(view, ref.qualifier) for ref in refs}
    reads_fields = any(
        not ref.qualifier and ref.column.lower() in earlier for ref in refs
    )
    if len(owners) == 1 and None not in owners and not reads_fields:
        (owner,) = owners
        table = resolved.table(view.name, owner)
        if table.columns is None:
            qualifiers = (("source",),) if owner == _SOURCE else (owner, owner[-1:])
            expression = lineage.over_own_columns(member.expr, qualifiers)
            return Resolved(owner=owner, expression=expression)
    return Resolved(owner=None)


def _resolve_measure(
    view: MetricView,
    member: Member,
    fields: Mapping[str, Resolved],
    resolved: Resolution,
    lineage: Any,
    fold: Any,
) -> Resolved:
    """Where one measure reads from, and its aggregate, references and ratio sides."""
    refs = tuple(lineage.measure_refs(member.expr) or ())
    ratio = lineage.ratio_sides(member.expr)
    found = None if refs else lineage.aggregate(member.expr)
    if found is None or member.window or member.partition:
        return Resolved(owner=None, aggregate=None, measure_refs=refs, ratio=ratio)
    owner: tuple[str, ...] | None = None
    column: str | None = None
    if found.column is not None:
        ref = found.column
        if not ref.qualifier and ref.column.lower() in fields:
            field_ = fields[ref.column.lower()]
            owner, column = field_.owner, field_.column
        else:
            owner = _owner(view, ref.qualifier)
            if owner is not None:
                column = _through(resolved.table(view.name, owner), fold(ref.column))
    return Resolved(owner=owner, column=column, aggregate=found)


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
    physical: dict[str, tuple[str, str]] = {}
    computed: dict[str, tuple[str, str]] = {}

    if loaded.views:
        found.append(
            "a Metric View's YAML carries no name, so each view is named after its "
            "file's stem; the name it is deployed under in Unity Catalog is not read"
        )
        if any(not m.is_time for v in loaded.views for m in v.fields):
            found.append(
                "Metric Views type no field: a field is listed as `time` only where "
                "its `format` is date or date_time, and every other one as "
                "categorical, with no queryable grains"
            )

    for view in loaded.views:
        found.extend(_view_notes(view))
        models.append(
            SemanticModelInfo(
                name=view.name,
                description=view.comment,
                relation=resolved.table(view.name).relation,
            )
        )
        tokens = [member_token(view.name, m.name) for m in view.fields]
        for member, token in zip(view.fields, tokens, strict=True):
            dimensions.append(
                DimensionInfo(
                    name=token,
                    type="time" if member.is_time else "categorical",
                    label=member.display_name,
                    description=member.comment,
                    definition=member.name,
                    semantic_model=view.name,
                    column=resolved.column(view.name, member.name),
                    vendor_params=_params(view, member, resolved),
                )
            )
            _gate(view, member, token, resolved, physical, computed)
        for member in view.measures:
            token = member_token(view.name, member.name)
            _gate(view, member, token, resolved, physical, computed)
            here = resolved.members.get((view.name, member.name))
            if here is not None and here.aggregate is not None:
                measures.append(
                    MeasureInfo(
                        name=member.name,
                        agg=here.aggregate.agg,
                        expr=member.expr,
                        label=member.display_name,
                        description=member.comment,
                        semantic_model=view.name,
                        column=resolved.column(view.name, member.name),
                    )
                )
            metrics.append(_metric(view, member, tokens, resolved))

    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        measures=measures,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=found,
        physical_columns=physical,
        computed_columns=computed,
    )


def _gate(
    view: MetricView,
    member: Member,
    token: str,
    resolved: Resolution,
    physical: dict[str, tuple[str, str]],
    computed: dict[str, tuple[str, str]],
) -> None:
    """Record where the PII gate screens ``token``: a column, or an expression."""
    if (found := resolved.gate(view.name, member.name)) is not None:
        physical[token] = found
    elif (expression := resolved.computed(view.name, member.name)) is not None:
        computed[token] = expression


def _params(view: MetricView, member: Member, resolved: Resolution) -> dict[str, Any]:
    """What a Metric View states about a member that the neutral model has no
    field for."""

    params: dict[str, Any] = {"expr": member.expr}
    if member.synonyms:
        params["synonyms"] = list(member.synonyms)
    if member.format:
        params["format"] = dict(member.format)
    if member.window:
        params["window"] = [dict(spec) for spec in member.window]
    if member.partition:
        params["partition"] = dict(member.partition)
    if member.derived_name:
        params["derived_name"] = True
    here = resolved.members.get((view.name, member.name))
    if here is not None and here.owner:
        params["join"] = ".".join(here.owner)
    return params


def _metric(
    view: MetricView, member: Member, groupable: list[str], resolved: Resolution
) -> MetricInfo:
    """One measure as the metric the catalog lists for it."""
    here = resolved.members.get((view.name, member.name)) or Resolved(owner=None)
    token = member_token(view.name, member.name)
    filters = [f for f in (view.filter, here.aggregate and here.aggregate.filter) if f]
    base: dict[str, Any] = {
        "name": token,
        "label": member.display_name,
        "description": member.comment,
        "dimensions": list(groupable),
        "semantic_models": [view.name],
        "filter": " AND ".join(f"({f})" for f in filters)
        if len(filters) > 1
        else (filters[0] if filters else None),
        "vendor_params": _params(view, member, resolved),
    }
    if here.aggregate is not None:
        return MetricInfo(
            type="simple",
            input_measures=[member.name],
            composition=MetricComposition(measure=member.name),
            **base,
        )
    underlying = _underlying(view, member, resolved)
    if here.ratio is not None and not member.window and not member.partition:
        numerator, denominator = (view.measure(side) for side in here.ratio)
        if numerator is not None and denominator is not None:
            return MetricInfo(
                type="ratio",
                input_measures=underlying or None,
                composition=MetricComposition(
                    numerator=member_token(view.name, numerator.name),
                    denominator=member_token(view.name, denominator.name),
                ),
                **base,
            )
    inputs = [
        member_token(view.name, found.name)
        for name in here.measure_refs
        if (found := view.measure(name)) is not None
    ]
    return MetricInfo(
        type="derived",
        input_measures=underlying or None,
        composition=MetricComposition(expr=member.expr, input_metrics=inputs or None),
        **base,
    )


def _underlying(view: MetricView, member: Member, resolved: Resolution) -> list[str]:
    """The aggregating measures a composed measure is made of, followed through
    other composed measures."""

    found: list[str] = []
    pending = [member]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        here = resolved.members.get((view.name, current.name))
        for name in here.measure_refs if here else ():
            target = view.measure(name)
            if target is None or target.name in seen:
                continue
            seen.add(target.name)
            inner = resolved.members.get((view.name, target.name))
            if inner is not None and inner.aggregate is not None:
                found.append(target.name)
            else:
                pending.append(target)
    return found


def _view_notes(view: MetricView) -> list[str]:
    """What a view declares that the catalog carries without modelling it."""
    notes: list[str] = []
    if view.filter:
        notes.append(
            f"view '{view.name}' filters every query (`filter`), so each of its "
            "metrics carries that predicate"
        )
    if view.parameters:
        names = ", ".join(str(p.get("name")) for p in view.parameters)
        notes.append(
            f"view '{view.name}' declares parameters ({names}), which a query "
            "binds; they are carried and not modelled"
        )
    if view.materialization:
        notes.append(
            f"view '{view.name}' declares a materialization, which Databricks "
            "maintains; it is carried and not modelled, and dex never deploys one"
        )
    for member in view.measures:
        if member.window or member.partition:
            kind = "window" if member.window else "partition"
            notes.append(
                f"view '{view.name}': measure '{member.name}' is a {kind} measure, "
                f"listed as a derived metric with its {kind} in vendor_params and "
                "not modelled"
            )
    notes.extend(
        f"view '{view.name}': `{wildcard.expr}` is expanded by Databricks when "
        "the view is created; the members it adds are not listed"
        for wildcard in view.wildcards
    )
    if view.fields_key == "dimensions":
        notes.append(
            f"view '{view.name}' lists its fields under `dimensions`, the older "
            "spelling of `fields`"
        )
    return notes


# --- the declaration channel ----------------------------------------------------


def _model_names(
    loaded: LoadResult, resolved: Resolution
) -> dict[tuple[str, tuple[str, ...]], str]:
    """The model each table is declared under: a view for its own source, the
    view whose source a join reads where one does, else ``view.join.path``."""

    by_relation = {
        table.relation: view.name
        for view in loaded.views
        if (table := resolved.table(view.name)).relation
    }
    names: dict[tuple[str, tuple[str, ...]], str] = {}
    for view in loaded.views:
        names[(view.name, _SOURCE)] = view.name
        for join in view.all_joins():
            relation = resolved.table(view.name, join.path).relation
            other = by_relation.get(relation) if relation else None
            names[(view.name, join.path)] = (
                other if other and other != view.name else f"{view.name}.{join.dotted}"
            )
    return names


@dataclass(frozen=True)
class DeclaredJoin:
    """One join as the declaration channel and the fingerprint read it."""

    view: MetricView
    join: Join
    relationship: DeclaredRelationship
    key_model: str
    key_relation: str | None
    key_columns: list[str]


def declared_relationships(
    loaded: LoadResult, resolved: Resolution
) -> list[DeclaredJoin]:
    """Every join whose condition names key pairs, written child to parent.

    The table a join hangs off is the child for ``many_to_one`` (the default),
    and the joined table is the child for ``one_to_many``.
    """

    names = _model_names(loaded, resolved)
    found: list[DeclaredJoin] = []
    for view in loaded.views:
        for join in view.all_joins():
            pairs = resolved.pairs.get((view.name, join.path))
            if not pairs:
                continue
            here = (view.name, join.path)
            above = (view.name, join.parent)
            child, parent = names[above], names[here]
            child_relation = resolved.table(view.name, join.parent).relation
            parent_relation = resolved.table(view.name, join.path).relation
            ordered = list(pairs)
            if join.cardinality == "one_to_many":
                child, parent = parent, child
                child_relation, parent_relation = parent_relation, child_relation
                ordered = [(b, a) for a, b in pairs]
            found.append(
                DeclaredJoin(
                    view=view,
                    join=join,
                    relationship=DeclaredRelationship(
                        model=child,
                        relation=child_relation,
                        to_model=parent,
                        to_relation=parent_relation,
                        column_pairs=ordered,
                        source=DECLARATION_SOURCE,
                        name=f"{view.name}.{join.dotted}",
                    ),
                    key_model=names[here],
                    key_relation=resolved.table(view.name, join.path).relation,
                    key_columns=[b for _, b in pairs],
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
    names = _model_names(loaded, resolved)
    relations = {
        names[(view, path)]: table.relation
        for (view, path), table in resolved.tables.items()
        if table.relation and (view, path) in names
    }
    seen_keys: set[tuple[str, tuple[str, ...]]] = set()
    for declared in declared_relationships(loaded, resolved):
        relationship = declared.relationship
        relationships.append(relationship)
        if len(relationship.column_pairs) == 1:
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
        if declared.join.at_most_one_match:
            identity = (
                declared.key_model,
                tuple(c.lower() for c in declared.key_columns),
            )
            if identity in seen_keys:
                continue
            seen_keys.add(identity)
            if len(declared.key_columns) == 1:
                keys.append(
                    DeclaredKey(
                        model=declared.key_model,
                        relation=declared.key_relation,
                        column=declared.key_columns[0],
                        unique=True,
                        source=DECLARATION_SOURCE,
                    )
                )
            else:
                composite.append(
                    DeclaredCompositeKey(
                        model=declared.key_model,
                        relation=declared.key_relation,
                        columns=list(declared.key_columns),
                        claim="unique",
                        source=DECLARATION_SOURCE,
                    )
                )

    found = [*notes, *resolved.notes]
    if loaded.views:
        found.append(
            "a Metric View declares the relations it reads and builds none, so it "
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
        notes=found,
    )
