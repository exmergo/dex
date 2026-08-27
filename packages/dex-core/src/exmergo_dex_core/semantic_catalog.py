"""The neutral read model of a semantic layer: semantic models, metrics,
dimensions, entities, measures.

A semantic layer is not three lists of names. It is a set of **semantic models**,
each sitting on one physical relation and owning the entities it joins on, the
dimensions it can be sliced by, and the measures its metrics are built from;
metrics are composed out of those measures and may span several models. Both the
dbt project YAML and the dbt Cloud API are organized that way, so a catalog that
flattens it away cannot answer "what can I query, how, and what will the number
mean".

Two very different callers read this model, which is why it is a leaf module with
no dex imports of its own:

- a **project format**, through ``SemanticCatalogProject.semantic_catalog()``,
  reducing whatever it holds on disk into this shape;
- the **explore surface**, through ``SemanticCatalog``, which subclasses
  :class:`SemanticCatalogView` to add the answering backend's provenance and to
  serialize the payload.

One model set rather than two is what stops a second semantic-layer format from
needing a parallel vocabulary, and it is why the fields here are named for what
they mean rather than for what one vendor calls them.

**Where neutrality stops, stated rather than assumed.** Composition is portable:
which measures a metric draws on, a ratio's two sides, a derived metric's
expression. Every format worth supporting has all of that, so it lives in the
shared shape. Detail that only means something under one vendor's semantics
(MetricFlow's cumulative ``window``, its ``grain_to_date``, a derived metric's
``offset_window``) goes under ``vendor_params``: one declared key, so a consumer
can tell the portable half from the local dialect without a lookup table, and so
promoting a vendor's vocabulary into the core is a visible decision rather than a
drift.

The flat lists are deliberate. Nesting each element inside its semantic model
would read better and break the flat lookup the PII gate and every existing
consumer do, so provenance is a field on the element (``semantic_model``) and the
semantic models are a list of their own carrying their own metadata.

**The physical link uses that same shape.** A semantic model carries the relation
it sits on and each element carries only its own column, so the address of any
element is its column plus its model's relation, joined by the ``semantic_model``
field the element already had. A relation is long and a layer holds many more
elements than models, so carrying it once per model rather than once per element
is what keeps the link from dominating the payload. It is also what connects this
catalog to the physical one: ``explore map`` describes relations, and a metric
reaches them through its semantic models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

# What one row of `dimensions` is, which is the whole answer to why two backends
# reading one identical layer report different dimension counts. `declarations`
# means one row per (semantic model, dimension) the layer declares, single-hop
# qualified. `queryable_paths` means one row per token a query may group by,
# including dimensions reached through a join, so the same declaration appears
# once per path that reaches it. Neither is wrong; a caller comparing counts
# across backends needs to be told which it is holding.
DIMENSIONS_PER_DECLARATION = "declarations"
DIMENSIONS_PER_QUERYABLE_PATH = "queryable_paths"


@dataclass
class SemanticModelInfo:
    """One semantic model: the layer's organizing unit.

    ``model_ref`` is the transformation-layer model it sits on, by name, and
    ``agg_time_dimension`` is the model's default time dimension, which is what
    decides what a time grouping means for every metric built on its measures.
    Both arrive from a project read and neither is available over the dbt Cloud
    API, whose ``SemanticModel`` type carries only a name; a backend declares
    that rather than letting the absence read as "the project declared none".

    ``relation`` is the physical relation underneath, fully qualified, and it is
    **the only place a relation appears in the catalog**. Every dimension, entity
    declaration and measure names its own column and points at its semantic model
    by name, so a caller joins the two rather than reading a fully qualified
    relation repeated on every element of a large layer. That is what connects a
    metric to the objects ``explore map`` and ``explore profile`` describe: a
    metric names its semantic models, and they name their relations.
    """

    name: str
    label: str | None = None
    description: str | None = None
    model_ref: str | None = None
    agg_time_dimension: str | None = None
    primary_entity: str | None = None
    relation: str | None = None


@dataclass
class MeasureInfo:
    """One measure: the aggregation a metric is actually made of.

    Without this a metric is a name and a type. ``agg`` and ``expr`` are the
    difference between reading a number correctly and misreading it, because a
    measure is often a conditional expression rather than a bare column, and
    ``agg_time_dimension`` is what a time grouping on any metric over this
    measure resolves to.

    ``column`` is the physical column behind it, and it is absent wherever
    ``expr`` is a computed expression rather than a column reference: see
    :func:`column_reference`. Read it together with the owning semantic model's
    ``relation`` for the full physical address.
    """

    name: str
    agg: str | None = None
    expr: str | None = None
    agg_time_dimension: str | None = None
    label: str | None = None
    description: str | None = None
    semantic_model: str | None = None
    column: str | None = None


@dataclass
class DimensionInfo:
    """One dimension row, keyed by the token a caller groups by.

    ``name`` is that token and nothing else: it is what a caller pastes into
    ``--group-by``, so it is never repointed at the bare dimension name however
    much tidier that would read. ``definition`` is the bare name, which is what
    lets a consumer see that two qualified paths reach one declaration, and
    together with ``semantic_model`` it identifies that declaration.

    ``label`` and ``description`` stay the project's own words about the
    dimension, unqualified: they describe the dimension, not the path a query
    reaches it by.

    ``queryable_granularities`` is what a time grouping on this dimension may
    ask for. An **empty list is an answer**: a categorical dimension has no grain,
    which is what stops a caller asking for one and getting a refusal it could
    have predicted. Absent means the layer was not asked or could not say, which
    is a different thing and reads differently.

    ``column`` is the physical column behind the declaration this row reaches,
    on that declaration's semantic model. It is absent for a computed expression
    and absent wherever ``semantic_model`` is, because a path reachable from more
    than one declaring model has no single column to name and a synthesized token
    has no declaration at all. Absent is the honest answer in both cases; guessing
    a column out of an expression is what makes a reader over-claim.
    """

    name: str
    type: str
    label: str | None = None
    description: str | None = None
    definition: str | None = None
    semantic_model: str | None = None
    queryable_granularities: list[str] | None = None
    column: str | None = None


@dataclass
class EntityRole:
    """One (entity, semantic model) declaration.

    The unit the layer actually declares, and the reason an entity cannot be
    reduced to one record. ``expr`` is the physical join key and differs per
    model for the same entity; ``description`` is where a project documents that
    model's own join, including how much of the model is lost to a nullable key,
    which is the metadata an author writes most carefully and the one a flat
    merge silently discards.

    ``column`` is that join key resolved to a plain column on this model's
    relation, which is ``expr`` where ``expr`` is a bare column reference and the
    entity's own name where it declares none. It is what turns a declared entity
    into a drawable join edge, and it is absent rather than guessed where the key
    is an expression.
    """

    semantic_model: str
    type: str
    expr: str | None = None
    role: str | None = None
    description: str | None = None
    column: str | None = None


@dataclass
class EntityInfo:
    """One entity, across every semantic model that declares it.

    ``roles`` is the truth. ``type`` is **derived**, defined as "primary where
    any declaration is primary": an entity is primary in the one model it keys
    and foreign in every model that joins to it, so a single value can only ever
    be a summary. It is kept because consumers render it, and it is derived
    rather than merged because a merged value is whichever copy the iteration
    reached first, which is not a fact about the layer.

    ``label`` is available from a project read and not over the dbt Cloud API,
    whose ``Entity`` type has no such field.
    """

    name: str
    type: str
    label: str | None = None
    description: str | None = None
    roles: list[EntityRole] = field(default_factory=list)


@dataclass
class MetricComposition:
    """What a metric is built out of, in portable terms.

    A sparse record: each metric type fills the parts that apply to it, and an
    absent key means this metric has no such part rather than that the value is
    unknown. A ratio carries ``numerator`` and ``denominator``, a derived metric
    carries ``expr`` and ``input_metrics``, a simple metric carries ``measure``.

    An agent that cannot see a ratio's two sides cannot tell whether the ratio is
    additive, cannot tell that two ratios share a denominator, and cannot tell
    that the two sides come from different semantic models, which is what decides
    whether a given group-by is valid on both of them.
    """

    measure: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    expr: str | None = None
    input_metrics: list[str] | None = None


@dataclass
class MetricInfo:
    """One metric, with what it counts and what it is built from.

    ``dimensions`` is the tokens this metric can be grouped by. ``input_measures``
    is every measure the metric ultimately reads, resolved through any ratio or
    derived chain, which is what connects it to :class:`MeasureInfo` and so to the
    aggregation behind the number. ``filter`` discloses that the metric measures a
    subset, which is otherwise invisible.

    ``time_axis`` is what a time grouping on this metric actually aggregates by:
    the agg time dimension of each measure it reads. A layer's time token is one
    name over many columns, so this is the highest-value field here for reading a
    number correctly, and **more than one entry means the measures disagree**. The
    disagreement is reported rather than resolved: picking one would name a column
    that is right for part of the metric and wrong for the rest, and one of those
    columns is often absent on rows the other one has.

    ``queryable_granularities`` is the grains a time grouping may ask for, which
    the layer knows per metric and a fixed list cannot.

    ``elided_dimension_count`` is how many groupable tokens the payload cap cut
    from ``dimensions``, and it is **absent rather than zero** where nothing was
    cut. That breaks the rule the rest of dex follows for elision counts, on
    purpose: this one repeats once per metric, and a catalog is the payload a cap
    exists to keep small, so paying for a zero on every metric would spend bytes
    to say nothing. The positive statement lives once, on the catalog's own
    ``elided`` block, which is unconditional and carries the layer-wide total; a
    reader that sees zero there knows no metric carries this field.
    """

    name: str
    type: str
    label: str | None = None
    description: str | None = None
    dimensions: list[str] = field(default_factory=list)
    elided_dimension_count: int | None = None
    semantic_models: list[str] | None = None
    input_measures: list[str] | None = None
    composition: MetricComposition | None = None
    filter: str | None = None
    time_axis: list[str] | None = None
    queryable_granularities: list[str] | None = None
    vendor_params: dict[str, Any] | None = None


@dataclass
class SemanticCatalogView:
    """A semantic layer as a read catalog, from whatever holds it.

    Distinct from ``maintain``'s ``SemanticLayer``, and deliberately so: that one
    is a fingerprint, a content hash per definition plus the physical column
    behind each field, sized for detecting drift and persisted as a baseline.
    This one is a read view, sized for a caller deciding what to query, and it
    carries the labels, types and composition a fingerprint reduces away.

    ``physical_columns`` maps every dimension and entity token (bare and
    qualified) to the ``(relation, column)`` behind it, and is **never
    serialized**: it exists so the PII request-gate can resolve a token to a
    profiled column, which makes that resolution the project format's job rather
    than something a query backend re-derives. A token whose reference is a
    computed expression is absent rather than guessed, because guessing a column
    out of an expression makes the gate over-claim.

    ``notes`` is how a format says what it could not supply, the way
    ``ProjectDefinitions.notes`` and ``SemanticLayer.notes`` do.
    """

    semantic_models: list[SemanticModelInfo] = field(default_factory=list)
    metrics: list[MetricInfo] = field(default_factory=list)
    dimensions: list[DimensionInfo] = field(default_factory=list)
    entities: list[EntityInfo] = field(default_factory=list)
    measures: list[MeasureInfo] = field(default_factory=list)
    dimension_scope: str = DIMENSIONS_PER_DECLARATION
    notes: list[str] = field(default_factory=list)
    physical_columns: dict[str, tuple[str, str]] = field(default_factory=dict)

    def narrowed_to(self, metrics: list[str]) -> tuple[SemanticCatalogView, list[str]]:
        """This catalog narrowed to the metrics named, as ``(catalog, unknown)``.

        Discovery on a large layer is one payload, and a caller that already knows
        which metric it is after should not have to read the whole layer to reach
        it. Scoping keeps the named metrics and everything reachable from them:
        the measures they read, the semantic models those live in, the dimensions
        they can be grouped by, and the entities declared in any model that
        survived.

        **An entity keeps all of its declarations**, including declarations in
        models the scope dropped. Pruning them would change the derived ``type``,
        so a scope would be able to turn a primary entity into a foreign one,
        which is a false statement about the layer rather than a smaller one.

        Unknown names come back rather than being dropped, because a caller that
        misspelled a metric would otherwise get a plausible empty catalog. The
        subclass carrying the payload is preserved, so the answer is still a
        catalog and not a lesser thing.
        """

        wanted = list(dict.fromkeys(metrics))
        by_name = {m.name: m for m in self.metrics}
        unknown = [name for name in wanted if name not in by_name]
        kept_metrics = [by_name[name] for name in wanted if name in by_name]

        kept_measures = {
            name for m in kept_metrics for name in (m.input_measures or [])
        }
        kept_models = {name for m in kept_metrics for name in (m.semantic_models or [])}
        kept_dimensions = {name for m in kept_metrics for name in m.dimensions}
        measures = [m for m in self.measures if m.name in kept_measures]
        dimensions = [d for d in self.dimensions if d.name in kept_dimensions]
        kept_models.update(m.semantic_model for m in measures if m.semantic_model)
        kept_models.update(d.semantic_model for d in dimensions if d.semantic_model)

        return (
            replace(
                self,
                semantic_models=[
                    m for m in self.semantic_models if m.name in kept_models
                ],
                metrics=kept_metrics,
                dimensions=dimensions,
                entities=[
                    e
                    for e in self.entities
                    if any(r.semantic_model in kept_models for r in e.roles)
                ],
                measures=measures,
            ),
            unknown,
        )

    def matching(self, terms: list[str]) -> tuple[SemanticCatalogView, list[str]]:
        """This catalog narrowed to what the search terms touch, as
        ``(catalog, unmatched)``.

        A caller arriving at a large layer usually knows a word rather than a
        name: "revenue", "session", "tier". Every element carries the project's
        own label and description, so a substring search over those words is the
        cheapest way from a word to the metrics it concerns, and it costs no call
        to the layer because the whole catalog is already in hand.

        **A search resolves to metrics and then scopes exactly as ``--metric``
        does.** A term that matches a dimension, measure, entity or semantic model
        keeps the metrics that reach it, because a matched dimension with no
        metric beside it answers nothing a caller can query. That also keeps one
        shape for every way of narrowing this catalog: whatever the caller asked
        by, what comes back is a catalog rather than a filtered list.

        The union across terms, not the intersection. Two terms are two searches,
        and a caller writing "revenue session" is widening rather than
        constraining, which is the opposite of ``--for-dimension``, where the
        intersection is the whole question.

        Unmatched terms come back rather than being dropped. Unlike a misspelled
        metric name, a search term that matches nothing is an honest answer, so
        the caller is told which terms were the empty ones instead of the command
        being refused; a search for three words where one was a typo should still
        answer for the other two.

        Matching is case-insensitive substring over each element's name, label and
        description, plus a metric's own groupable tokens, so searching a
        dimension token finds the metrics that can be grouped by it.
        """

        wanted = list(
            dict.fromkeys(term.strip().lower() for term in terms if term.strip())
        )
        if not wanted:
            return self, []

        def hits(term: str, *values: Any) -> bool:
            return any(term in str(value).lower() for value in values if value)

        matched: set[str] = set()
        unmatched: list[str] = []
        for term in wanted:
            found: set[str] = set()
            for metric in self.metrics:
                if hits(
                    term,
                    metric.name,
                    metric.label,
                    metric.description,
                    *metric.dimensions,
                ):
                    found.add(metric.name)
            # An element other than a metric is matched for the metrics that
            # reach it: a dimension through the groupable list each metric
            # carries, a measure through the metrics that read it, and a semantic
            # model through the metrics built on it. An entity is reached through
            # every model that declares it, which is wide on purpose: an entity is
            # the layer's join hub, so a search for one is a search for what can be
            # sliced by it. A measure deliberately does not widen to its model,
            # because "in the same model as" is not "made of".
            models = {
                model.name
                for model in self.semantic_models
                if hits(term, model.name, model.label, model.description)
            }
            models.update(
                role.semantic_model
                for entity in self.entities
                if hits(term, entity.name, entity.label, entity.description)
                for role in entity.roles
            )
            measures = {
                measure.name
                for measure in self.measures
                if hits(term, measure.name, measure.label, measure.description)
            }
            dimensions = {
                dimension.name
                for dimension in self.dimensions
                if hits(
                    term,
                    dimension.name,
                    dimension.definition,
                    dimension.label,
                    dimension.description,
                )
            }
            for metric in self.metrics:
                if (
                    dimensions.intersection(metric.dimensions)
                    or measures.intersection(metric.input_measures or ())
                    or models.intersection(metric.semantic_models or ())
                ):
                    found.add(metric.name)
            if found or models or measures or dimensions:
                matched.update(found)
            else:
                unmatched.append(term)

        narrowed, _unknown = self.narrowed_to(
            [metric.name for metric in self.metrics if metric.name in matched]
        )
        return narrowed, unmatched

    def metrics_for_dimensions(
        self, dimensions: list[str]
    ) -> tuple[list[str], list[str]]:
        """The metrics groupable by **all** the named tokens, as ``(metrics, unknown)``.

        The catalog answers "what can this metric be grouped by". A caller more
        often arrives with the reverse, "I want to slice by pricing tier, what can
        I slice", and answering that by hand means reading every metric's dimension
        list and inverting it, which is the whole catalog read to ask about one
        dimension.

        It is an inversion of ``metrics[].dimensions`` rather than a second call to
        the layer, and that is a property worth keeping: both backends already
        carry that list join-resolved, so the answer costs nothing, means the same
        thing on either one, and covers a metric's own time token, which the dbt
        Cloud API's equivalent field does not accept.

        The intersection, not the union. Metrics that share a group-by are the ones
        that can be put on one chart against one axis, which is the question being
        asked, and a union would answer a different and less useful one.

        Unknown names come back rather than being dropped, and none is resolved
        when any is unknown. A token the layer does not have and a token no metric
        can be grouped by are both the empty answer, so a caller that misspelled
        one would otherwise read "no metric" as a fact about the layer.
        """

        wanted = list(dict.fromkeys(dimensions))
        known = {d.name for d in self.dimensions}
        known.update(d for m in self.metrics for d in m.dimensions)
        unknown = [name for name in wanted if name not in known]
        if unknown:
            return [], unknown
        return (
            [m.name for m in self.metrics if all(d in m.dimensions for d in wanted)],
            [],
        )


def merge_element_fields(
    store: dict[str, dict[str, Any]], key: str, element: dict[str, Any]
) -> None:
    """Fold one dimension into a catalog accumulator keyed by ``key``.

    Both backends meet the same dimension more than once: the hosted API nests it
    under every metric that can group by it, and locally the same qualified token
    can be declared in more than one model. The copies need not agree, so each
    field takes the first non-null value seen rather than the first copy outright.
    Under a plain ``setdefault`` on the whole element, whichever copy happened to
    come first could blank out a description another one carried.

    ``key`` is passed rather than read off the element because a dimension is
    filed under the token a query groups it by (``session__created_at``) while the
    element itself carries the bare name.

    **Entities do not come through here**, and that is the point. An entity's
    ``type`` is a property of the (entity, semantic model) declaration rather than
    of the entity, so folding copies into one record reported whichever the
    iteration reached first: the two backends disagreed with each other on the same
    layer, and both misreported the entity most joined in it. Entities accumulate
    per declaration into :class:`EntityRole` instead, and the single ``type``
    is derived by :func:`derive_entity_type`.
    """

    fields = store.setdefault(key, {})
    if not fields.get("type"):
        fields["type"] = (element.get("type") or "").lower()
    for name in (
        "label",
        "description",
        "definition",
        "semantic_model",
        "queryable_granularities",
        "column",
    ):
        if fields.get(name) is None:
            fields[name] = element.get(name)


def derive_entity_type(roles: list[EntityRole]) -> str:
    """One entity's ``type``, derived from every declaration of it.

    Primary wherever any model declares it primary, because that is the model
    that keys it and the fact a caller works the join graph out from; otherwise
    whatever the declarations agree on. Never the first copy encountered.
    """

    for role in roles:
        if role.type == "primary":
            return "primary"
    for role in roles:
        if role.type:
            return role.type
    return ""


# What counts as "this element references a plain column" rather than "this
# element computes something". Anchored with fullmatch, so `order_id` is a column
# and `lower(order_id)` or `a + b` is not.
_BARE_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def column_reference(expr: str | None, name: str | None) -> str | None:
    """The single physical column an element references, or None.

    Three cases, and the third is the one that matters. A bare-identifier ``expr``
    is the column; an element with no ``expr`` at all references the column its own
    name spells, which is how every format worth supporting reads an undecorated
    declaration; and a computed expression resolves to **nothing**, because a
    column guessed out of an expression makes every reader downstream over-claim.
    The PII gate reaches a column this way, so over-claiming there means screening
    the wrong column and reporting that as evidence.

    Here rather than in the dbt reader because two very different callers apply it:
    a project format holding manifest dicts, and a query backend holding a GraphQL
    payload. One rule, one place, so the two cannot drift about what "the column
    behind this" means.
    """

    if expr is None:
        return name if isinstance(name, str) and name else None
    if isinstance(expr, str) and _BARE_COLUMN.fullmatch(expr.strip()):
        return expr.strip()
    return None


@dataclass(frozen=True)
class EntityJoin:
    """One join the semantic layer declares outright, resolved to two relations.

    A semantic layer states its join graph: every entity shared by two semantic
    models is a join, with the physical key named per model, and the key routinely
    differs between the two sides for the same entity. That graph is authoritative
    and free to read, which makes it a strictly better answer than a name-based
    inference over the same warehouse.

    ``parent`` is the side the entity keys and ``child`` the side that joins to
    it, matching the direction :class:`~.cache.Relationship` is written in, so a
    caller building an edge does not have to work out which end is which.
    """

    entity: str
    parent_model: str
    parent_relation: str
    parent_column: str
    child_model: str
    child_relation: str
    child_column: str


def entity_joins(view: SemanticCatalogView) -> list[EntityJoin]:
    """Every join this layer declares, as resolved relation-to-relation edges.

    Neutral, over the neutral view, so a second semantic-layer format that fills
    the same catalog produces the same graph without a second implementation and
    without teaching the explore surface anything about a vendor.

    **The parent is the declaration that keys the entity**, which is the one typed
    ``primary``, or the single ``unique`` declaration where the layer names no
    primary. An entity with several primaries, or with several uniques and no
    primary, yields nothing: which side keys the join is then not a fact the layer
    states, and resolution here never guesses, exactly as declared-endpoint
    resolution does not.

    Children are the ``foreign`` declarations and only those. A ``natural`` key
    identifies a row in a slowly-changing table, where a correct join also needs a
    validity window the catalog does not carry, so drawing it as a plain equi-join
    would be an edge that is wrong rather than merely unproven.

    A side missing its relation or its column yields nothing rather than a partial
    edge: an element whose reference is a computed expression has no column to join
    on (see :func:`column_reference`), and a backend that cannot say which relation
    a model sits on has nothing to draw between.
    """

    relations = {m.name: m.relation for m in view.semantic_models if m.relation}
    joins: list[EntityJoin] = []
    for entity in view.entities:
        keyed = [r for r in entity.roles if r.type == "primary"] or [
            r for r in entity.roles if r.type == "unique"
        ]
        if len(keyed) != 1:
            continue
        parent = keyed[0]
        parent_relation = relations.get(parent.semantic_model)
        if not parent_relation or not parent.column:
            continue
        for role in entity.roles:
            if role.type != "foreign" or role.semantic_model == parent.semantic_model:
                continue
            child_relation = relations.get(role.semantic_model)
            if not child_relation or not role.column:
                continue
            joins.append(
                EntityJoin(
                    entity=entity.name,
                    parent_model=parent.semantic_model,
                    parent_relation=parent_relation,
                    parent_column=parent.column,
                    child_model=role.semantic_model,
                    child_relation=child_relation,
                    child_column=role.column,
                )
            )
    # Sorted so one layer always produces one edge order, which is what keeps a
    # cache written from it byte-identical across runs.
    return sorted(
        joins,
        key=lambda j: (j.entity, j.child_relation, j.child_column, j.parent_relation),
    )


def qualified_dimension(entity: str | None, name: str) -> str:
    """The token a query groups a dimension by (``session__created_at``).

    The qualifier is the declaring model's primary entity, which is how
    MetricFlow spells a dimension reached through that entity. A model with no
    primary entity leaves the name bare, because there is nothing to qualify it
    with and inventing a prefix would produce a token no query accepts.
    """

    return f"{entity}__{name}" if entity else name
