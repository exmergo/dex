"""Normalizing validated Ossie documents into what dex reads.

Two channels, both fed from the same validated documents, because dex asks a
project two different questions and the answers are shaped differently:

- :func:`semantic_catalog` builds the neutral read catalog `explore semantic
  list` renders, which carries labels, types, dialects and lineage.
- :func:`definitions` builds the declarations channel, which carries
  declared keys and joins and nothing else.

**Names resolve the way upstream's Name Resolution says.** A field's expression
names a column of its dataset's source; a key, a relationship's `from_columns`
and `to_columns`, and a metric name **fields**. So a key or a join reaches the
warehouse only through the field it names: that field's expression, linked to a
physical column by the same rule every field follows (a bare identifier on an
addressable source). A key or relationship with any member that links no column
is opaque as a whole and named in a note, never probed on a partial tuple.

**The rule running through both is that dex claims less than it could.** Ossie
is an interchange format for a specification still under revision, and every
place the document is silent is a place where a plausible inference would be
indistinguishable from a fact once it reached a payload. So a computed
expression resolves to no column, an unresolvable metric reference produces no
lineage, and a source dex cannot read as a relation produces no physical link at
all. Each of those is stated in a note rather than left as an absence, because
an absence and a decision read the same to a caller.

**Nothing MetricFlow-shaped is invented here.** Ossie has no measures, no
entities, no aggregation types, no grain vocabulary, no ratio composition, and
no join planning. Those are gaps declared as gaps, not fields to fill with the
nearest available thing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from ..adapters import fold_identifier, normalize_relation
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
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    column_reference,
)
from ..semantic_declarations import catalog_description, read_native_declaration
from .dialects import select_expression
from .loader import LoadedDocument

__all__ = ["DECLARATION_SOURCE", "definitions", "field_columns", "semantic_catalog"]

#: What `ProjectDefinitions` records as the channel a declaration came from.
#: One value, because Ossie states its declarations in one place, unlike dbt
#: where a key may come from a manifest or from YAML resolved by name.
DECLARATION_SOURCE = "ossie"

# Ossie's temporal datatypes. The schema documents `dimension.is_time` as
# defaulting to true for exactly these and false otherwise, so an explicit
# `is_time: false` on a Date field suppresses it and is honored.
_TEMPORAL_DATATYPES = frozenset({"Date", "Time", "DateTime", "DateTimeTz"})

# A two-part `dataset.field` reference, which is the qualified form the
# expression language documents. Word boundaries on both ends so a three-part
# `a.b.c` contributes its pairs and a decimal literal contributes nothing.
_IDENTIFIER_PAIR = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")

# A quoted identifier, in any spelling the dialects use. Only ever read to
# explain a non-link, never to unwrap one into a column: whether a quoted
# name and its unquoted twin mean the same column is the warehouse's rule,
# and dex cannot tell which the author meant.
_QUOTED = re.compile(r"""["`\[].*""")

# A bare identifier token, for a dataset metric's unqualified field references.
# Matched against declared field names only, so a function name or keyword the
# scan also finds resolves to nothing.
_BARE_IDENTIFIER = re.compile(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)(?![\w.])")


def semantic_catalog(
    documents: Sequence[LoadedDocument],
    *,
    connector: str | None = None,
    notes: Sequence[str] = (),
) -> SemanticCatalogView:
    """The neutral read catalog for a set of validated documents."""

    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    metrics: list[MetricInfo] = []
    physical: dict[str, tuple[str, str]] = {}
    found: list[str] = list(notes)

    for document in documents:
        # One document is one semantic model, flat at its root.
        model = document.data
        model_name = model.get("name")
        datasets = model.get("datasets") or []
        declared_fields: dict[str, set[str]] = {}
        dataset_metrics: list[tuple[str, str, dict[str, Any]]] = []

        for dataset in datasets:
            dataset_name = dataset.get("name")
            # Namespaced by the containing semantic model, so two documents
            # may each declare an ordinary `orders` without colliding. This
            # is also why one semantic-model name across the configured set
            # is a loader-level error.
            qualified = f"{model_name}.{dataset_name}"
            source = dataset.get("source")
            relation = _relation(source, connector)
            if relation is None:
                found.append(
                    f"dataset '{qualified}' has a source dex cannot read as "
                    f"one relation on {connector or 'this connector'}, so it "
                    "carries no physical link. Ossie documents a source as a "
                    "table reference or a query without a portable way to "
                    "tell them apart, and a query read as a relation would "
                    "reach the PII gate as false evidence"
                )
            models.append(
                SemanticModelInfo(
                    name=qualified,
                    label=dataset_name,
                    description=catalog_description(
                        dataset.get("description"), dataset.get("ai_context"), found
                    ),
                    relation=relation,
                )
            )
            names: set[str] = set()
            for field_ in dataset.get("fields") or []:
                dimension, note = _dimension(
                    field_, dataset_name, qualified, connector, relation
                )
                dimensions.append(dimension)
                names.add(dimension.definition or "")
                if note:
                    found.append(note)
                if dimension.column and relation:
                    physical[dimension.name] = (relation, dimension.column)
            declared_fields[str(dataset_name)] = names
            dataset_metrics.extend(
                (str(dataset_name), qualified, metric)
                for metric in dataset.get("metrics") or []
                if isinstance(metric, dict)
            )

        for dataset_name, qualified, metric in dataset_metrics:
            info, note = _dataset_metric(
                metric, dataset_name, qualified, declared_fields, connector
            )
            metrics.append(info)
            if note:
                found.append(note)

        for metric in model.get("metrics") or []:
            info, note = _metric(metric, model_name, declared_fields, connector)
            metrics.append(info)
            if note:
                found.append(note)

    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=found,
        physical_columns=physical,
    )


def _relation(source: Any, connector: str | None) -> str | None:
    """The dataset's source as a relation dex can address, or ``None``.

    **Both halves of the physical-linkage rule start here.** The connector's own
    parser has to accept the *entire* source as one fully qualified, unquoted
    relation. A query, an expression, a partially qualified name, and a quoted
    identifier are all rejected, and each rejection is correct rather than
    merely cautious: an unquoted Ossie identifier normalizes the way the
    warehouse folds it while a quoted one is exact, so the two do not name the
    same relation on Snowflake or Postgres and dex cannot tell which the author
    meant.
    """

    if not isinstance(source, str) or not connector:
        return None
    return normalize_relation(connector, source)


def _dimension(
    field_: dict[str, Any],
    dataset_name: Any,
    qualified_model: str,
    connector: str | None,
    relation: str | None,
) -> tuple[DimensionInfo, str | None]:
    """One field as a dimension row, its physical column, and any note.

    The token is `<dataset>__<field>`, which is the grouping vocabulary this
    catalog publishes. It is qualified by the dataset rather than by the
    namespaced model name because the dataset is what an Ossie author writes and
    reads; the namespace exists to keep two documents apart, not to be typed.
    """

    field_name = str(field_.get("name"))
    expression, dialect, declared, resolved, column = _link(field_, connector, relation)
    # `column_reference` resolves a bare identifier and refuses an expression.
    # Ossie always requires an expression, so its "no expression means the
    # element's own name" branch is unreachable here and a field is linked only
    # by what it actually says.
    #
    # **Both conditions, or no column.** A bare identifier on a source dex
    # cannot address is not a physical column: `column` is read together with
    # the owning model's `relation` to form an address, so carrying one half of
    # an address that has no other half claims a link that does not exist. That
    # keeps `column` meaning exactly one thing everywhere in the catalog.
    #
    # A bare identifier is the column the warehouse folded it to, so it is
    # folded the way `connector` resolves it; a quoted identifier resolves to
    # nothing and is left alone. :func:`_link` is that rule, shared with keys
    # and relationships, which reach a column only through a field.

    # The note names the *actual* reason a field did not link, and there are
    # three different ones. Misattributing them is worse than saying nothing: a
    # reader told their expression is the problem goes and rewrites a field that
    # was already a bare column.
    note = _linkage_note(
        dataset_name, field_name, expression, resolved, dialect, declared, relation
    )

    dimension_role = field_.get("dimension") or {}
    datatype = field_.get("datatype")
    declared_time = dimension_role.get("is_time")
    is_time = bool(declared_time) or (
        declared_time is None and datatype in _TEMPORAL_DATATYPES
    )

    return (
        DimensionInfo(
            name=f"{dataset_name}__{field_name}",
            type="time" if is_time else "categorical",
            label=field_.get("label"),
            description=field_.get("description"),
            definition=field_name,
            semantic_model=qualified_model,
            # An empty list is the positive statement "no grain is queryable",
            # which is true of a categorical dimension and is what the catalog
            # contract expects. For a time dimension it would be a false
            # statement: Ossie declares no grain vocabulary at all, so dex was
            # never told, and absent is the honest answer for "not asked".
            queryable_granularities=None if is_time else [],
            column=column,
            vendor_params=_vendor_params(
                datatype=datatype,
                expression=expression,
                dialect=dialect,
                declared=declared,
                ai_context=field_.get("ai_context"),
                extensions=field_.get("custom_extensions"),
            ),
        ),
        note,
    )


def _link(
    field_: dict[str, Any], connector: str | None, relation: str | None
) -> tuple[str | None, str | None, dict[str, str], str | None, str | None]:
    """A field's selected expression and the physical column it links to.

    Returns ``(expression, dialect, declared, resolved, column)``: ``resolved``
    is the folded bare identifier, ``column`` the same only where the dataset's
    source is an addressable relation.
    """

    expression, dialect, declared = select_expression(
        field_.get("expression"), connector
    )
    bare = column_reference(expression, None) if expression else None
    resolved = fold_identifier(connector, bare) if bare else None
    return expression, dialect, declared, resolved, (resolved if relation else None)


def field_columns(
    dataset: dict[str, Any], names: Sequence[Any], connector: str | None = None
) -> list[tuple[str, str | None]]:
    """Each name as ``(field, physical column or None)``, per Name Resolution.

    A key or relationship member names a field of the dataset, compared
    case-insensitively as upstream compares it, and reaches the warehouse only
    through that field's own link. A name that is no declared field (which the
    loader refuses) and a field that links no column both resolve to ``None``.
    """

    relation = _relation(dataset.get("source"), connector)
    by_name = {
        f["name"].casefold(): f
        for f in dataset.get("fields") or []
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    }
    resolved: list[tuple[str, str | None]] = []
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        field_ = by_name.get(name.casefold())
        if field_ is None:
            resolved.append((name, None))
            continue
        resolved.append((str(field_["name"]), _link(field_, connector, relation)[4]))
    return resolved


def _opaque(fields: Sequence[tuple[str, str | None]]) -> list[str]:
    return [name for name, column in fields if column is None]


def _linkage_note(
    dataset_name: Any,
    field_name: str,
    expression: str | None,
    resolved: str | None,
    dialect: str | None,
    declared: dict[str, str],
    relation: str | None,
) -> str | None:
    """Why this field carries no physical column, or ``None`` when it does.

    Silent when the dataset itself is opaque: that is one fact about the dataset,
    already stated once where it belongs, and repeating it per field would bury
    the fields whose own expression is the reason.
    """

    if relation is None:
        return None
    if expression is None:
        if not declared:
            return None
        return (
            f"field '{dataset_name}.{field_name}' declares only "
            f"{', '.join(sorted(declared))} expressions, which are not SQL, so "
            "dex preserves them as written and reads no physical column from "
            "them"
        )
    if resolved is not None:
        return None
    if _QUOTED.fullmatch(expression.strip()):
        return (
            f"field '{dataset_name}.{field_name}' is a quoted identifier "
            f"({expression}), so it carries no physical column. An unquoted "
            "identifier is folded the way the warehouse folds it while a quoted "
            "one is exact, so the two need not name the same column and dex "
            "cannot tell which was meant"
        )
    return (
        f"field '{dataset_name}.{field_name}' is computed rather than a bare "
        f"column ({dialect}: {expression}), so it carries no physical column. A "
        "column guessed out of an expression makes the PII gate screen the "
        "wrong one and report it as evidence"
    )


def _metric(
    metric: dict[str, Any],
    model_name: Any,
    declared_fields: dict[str, set[str]],
    connector: str | None,
) -> tuple[MetricInfo, str | None]:
    """One metric, with lineage that is genuinely conservative.

    Lineage comes from qualified `dataset.field` references that actually
    resolve against the datasets in this semantic model. **When nothing
    resolves, lineage is empty**, because Ossie carries no metric-to-dataset
    reference at all: naming every dataset in the model would be the maximal
    claim dressed as a conservative one, and a caller reading "this metric
    touches these five tables" has no way to tell it from a fact.

    ``dimensions`` stays empty for the same reason one step further out.
    Lineage says an expression mentions a dataset. It does not say a field can
    group the metric, and it does not say a relationship path between two
    datasets is executable, which is what a groupable token asserts.
    """

    name = str(metric.get("name"))
    expression, dialect, declared = select_expression(
        metric.get("expression"), connector
    )
    lineage = sorted(
        {
            f"{model_name}.{dataset}"
            for dataset, field_name in _qualified_references(expression)
            if field_name in declared_fields.get(dataset, ())
        }
    )
    note = None
    if expression and not lineage:
        note = (
            f"metric '{name}' names no dataset field dex could resolve, so its "
            "lineage is empty. Ossie states no metric-to-dataset reference, and "
            "naming every dataset in the semantic model would be a claim the "
            "document does not make"
        )

    return (
        MetricInfo(
            name=name,
            type="expression",
            description=metric.get("description"),
            # Empty, and declared as a gap by the backend rather than left to
            # read as "this metric can be grouped by nothing".
            dimensions=[],
            semantic_models=lineage or None,
            vendor_params=_vendor_params(
                datatype=metric.get("datatype"),
                expression=expression,
                dialect=dialect,
                declared=declared,
                ai_context=metric.get("ai_context"),
                extensions=metric.get("custom_extensions"),
            ),
        ),
        note,
    )


def _dataset_metric(
    metric: dict[str, Any],
    dataset_name: str,
    qualified: str,
    declared_fields: dict[str, set[str]],
    connector: str | None,
) -> tuple[MetricInfo, str | None]:
    """A dataset-scoped metric (`datasets[].metrics`), named `dataset.metric`.

    Upstream's Metric Scoping: it aggregates its own dataset's declared fields,
    written unqualified, so its lineage is that one dataset whenever any bare
    identifier in it names one of those fields. As conservative as a model
    metric: an expression naming none of them carries no lineage and says so.
    The integrity rules upstream adds for these (a shared field and metric
    namespace, no reference to another dataset) are not checked here yet.
    """

    name = f"{dataset_name}.{metric.get('name')}"
    expression, dialect, declared = select_expression(
        metric.get("expression"), connector
    )
    fields = {f.casefold() for f in declared_fields.get(dataset_name, ())}
    named = {token.casefold() for token in _BARE_IDENTIFIER.findall(expression or "")}
    lineage = [qualified] if named & fields else []
    note = None
    if expression and not lineage:
        note = (
            f"dataset metric '{name}' names no declared field of dataset "
            f"'{dataset_name}', so its lineage is empty"
        )
    return (
        MetricInfo(
            name=name,
            type="expression",
            description=metric.get("description"),
            dimensions=[],
            semantic_models=lineage or None,
            vendor_params=_vendor_params(
                datatype=metric.get("datatype"),
                expression=expression,
                dialect=dialect,
                declared=declared,
                ai_context=metric.get("ai_context"),
                extensions=metric.get("custom_extensions"),
            ),
        ),
        note,
    )


def _qualified_references(expression: str | None) -> Iterable[tuple[str, str]]:
    """Every `dataset.field` pair a SQL expression mentions.

    Deliberately a scan for the multi-part form the expression language
    documents rather than a parse. Resolution is what makes it safe: a pair only
    becomes lineage when both halves name something the document declares, so a
    coincidental `schema.column` inside a function call resolves to nothing and
    contributes nothing.

    A possible metric-to-metric reference is not resolved here and is not
    promoted into composition anywhere. Ossie has not defined the grammar or the
    scope for one, so an unresolved token stays opaque expression text, which the
    catalog preserves verbatim under vendor parameters.
    """

    if not expression:
        return
    for chunk in _IDENTIFIER_PAIR.finditer(expression):
        yield chunk.group(1), chunk.group(2)


def _vendor_params(
    *,
    datatype: Any,
    expression: str | None,
    dialect: str | None,
    declared: dict[str, str],
    ai_context: Any,
    extensions: Any,
) -> dict[str, Any] | None:
    """Everything Ossie states that the neutral model has no field for.

    Flat keys, matching what both shipped backends already write here, so one
    declared escape hatch has one convention. The catalog already reports its
    `vendor` at the top level, so nesting these under a second `ossie` key would
    say the same thing twice and make a consumer look it up.

    ``None`` when there is nothing to carry, so the serializer's sparse pruning
    drops the field entirely rather than emitting an empty object.
    """

    params: dict[str, Any] = {}
    if datatype:
        params["datatype"] = datatype
    if expression:
        params["expression"] = expression
    if dialect:
        params["dialect"] = dialect
    if declared:
        # Every dialect the document declares, SQL and not, verbatim. The
        # selected one is among them; `dialect` above says which.
        params["dialects"] = dict(declared)
    if ai_context:
        params["ai_context"] = ai_context
    if extensions:
        params["custom_extensions"] = extensions
    return params or None


def definitions(
    documents: Sequence[LoadedDocument],
    *,
    connector: str | None = None,
    notes: Sequence[str] = (),
    present: bool = True,
) -> ProjectDefinitions:
    """The declarations channel: declared keys, joins, and relations.

    Separate from the catalog because the consumers are separate. This is what
    `explore profile --use-project` reads to override a heuristic grain and what
    `explore relationships --use-project` reads for a declared edge, and neither
    wants labels or dialects.
    """

    keys: list[DeclaredKey] = []
    composite: list[DeclaredCompositeKey] = []
    joins: list[DeclaredForeignKey] = []
    declared_relationships: list[DeclaredRelationship] = []
    relations: dict[str, str] = {}
    declarations: dict[str, ModelDeclaration] = {}
    unread: list[str] = []
    found: list[str] = list(notes)

    for document in documents:
        model = document.data
        model_name = model.get("name")
        by_name: dict[str, str] = {}
        datasets: dict[str, dict[str, Any]] = {}
        for dataset in model.get("datasets") or []:
            qualified = f"{model_name}.{dataset.get('name')}"
            by_name[str(dataset.get("name"))] = qualified
            datasets[str(dataset.get("name"))] = dataset
            relation = _relation(dataset.get("source"), connector)
            if relation:
                relations[qualified] = relation
            _keys(dataset, qualified, relation, keys, composite, found, connector)
            declaration, declaration_notes = _declaration(
                dataset, qualified, relation, connector
            )
            unread.extend(declaration_notes)
            if declaration is not None:
                declarations[qualified] = declaration

        for rel in model.get("relationships") or []:
            pairs, note = _resolved_pairs(rel, datasets, connector)
            if note:
                found.append(note)
            if pairs is None:
                continue
            declaration, note = _join(rel, by_name, relations, pairs)
            if declaration is not None:
                joins.append(declaration)
            if note:
                found.append(note)
            relationship = _declared_relationship(rel, by_name, relations, pairs)
            if relationship is not None:
                declared_relationships.append(relationship)

    if documents:
        # Ossie declares relations and builds none, so the field explore reads to
        # down-rank a relation nothing in the project accounts for stays empty.
        # Said out loud, because empty here otherwise reads as "this project
        # accounts for no relation in the warehouse".
        found.append(
            "an Ossie document declares the relations it reads and builds none, "
            "so it contributes no built-relation names and cannot tell explore "
            "that a warehouse relation is unaccounted for"
        )

    return ProjectDefinitions(
        present=present,
        relationship_source=DECLARATION_SOURCE if documents else None,
        semantic_source=DECLARATION_SOURCE if documents else None,
        foreign_keys=joins,
        declared_relationships=declared_relationships,
        declared_keys=keys,
        declared_composite_keys=composite,
        # Every dataset is a relation the document reads, never one it builds,
        # so none of them is a model a build could have failed to produce.
        source_relations=relations,
        model_declarations=declarations,
        declaration_notes=unread,
        notes=found,
    )


def _declaration(
    dataset: dict[str, Any],
    qualified: str,
    relation: str | None,
    connector: str | None,
) -> tuple[ModelDeclaration | None, list[str]]:
    """The dataset's declaration: its ``primary_key`` is the grain (#976), and the
    rest is read from ``ai_context.dex`` on the dataset and its fields (#977)."""

    key = field_columns(dataset, dataset.get("primary_key") or [], connector)
    grain = [str(c) for _, c in key] if key and not _opaque(key) else None
    members = [
        (
            f["name"],
            field_columns(dataset, [f["name"]], connector)[0][1],
            f.get("ai_context"),
        )
        for f in dataset.get("fields") or []
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    ]
    return read_native_declaration(
        qualified,
        relation,
        grain,
        dataset.get("ai_context"),
        members,
        source=DECLARATION_SOURCE,
    )


def _keys(
    dataset: dict[str, Any],
    qualified: str,
    relation: str | None,
    keys: list[DeclaredKey],
    composite: list[DeclaredCompositeKey],
    notes: list[str],
    connector: str | None = None,
) -> None:
    """A dataset's key declarations, split by arity.

    A one-column key is a `DeclaredKey`; a multi-column key is a
    `DeclaredCompositeKey` and is **never also** emitted as several single keys.
    The two say very different things: "this combination is unique" is a grain,
    while "each of these is unique on its own" is a much stronger claim that the
    document does not make and that reconcile would act on.

    A key names fields, so each member reaches its physical column through the
    field it names (:func:`field_columns`). A key with any member that links no
    column stays declared in the document and is emitted nowhere: it is named
    in a note, and nothing probes a partial tuple of it.
    """

    for label, declared in (
        ("primary key", dataset.get("primary_key")),
        *(("unique key", key) for key in dataset.get("unique_keys") or []),
    ):
        fields = field_columns(dataset, declared or [], connector)
        if not fields:
            continue
        opaque = _opaque(fields)
        if opaque:
            notes.append(
                f"dataset '{qualified}' declares a {label} on "
                f"{', '.join(name for name, _ in fields)}, and "
                f"{', '.join(opaque)} links no physical column, so the key is "
                "declared but not verified: nothing probes it, and a composite "
                "is never checked on part of its tuple"
            )
            continue
        columns = [column for _, column in fields if column]
        if len(columns) == 1:
            keys.append(
                DeclaredKey(
                    model=qualified,
                    relation=relation,
                    column=columns[0],
                    unique=True,
                    primary=label == "primary key",
                    source=DECLARATION_SOURCE,
                )
            )
        else:
            composite.append(
                DeclaredCompositeKey(
                    model=qualified,
                    relation=relation,
                    columns=columns,
                    claim="primary" if label == "primary key" else "unique",
                    source=DECLARATION_SOURCE,
                )
            )


def _resolved_pairs(
    rel: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    connector: str | None,
) -> tuple[list[tuple[str, str]] | None, str | None]:
    """A relationship's ordered column pairs, through the fields it names.

    ``(None, note)`` when the relationship is opaque: an endpoint is missing,
    the arity differs, or any member's field links no column. Opaque as a
    whole, because a composite join probed on part of its tuple is a join the
    author never declared.
    """

    name = rel.get("name")
    child = datasets.get(str(rel.get("from")))
    parent = datasets.get(str(rel.get("to")))
    from_columns = [c for c in (rel.get("from_columns") or []) if isinstance(c, str)]
    to_columns = [c for c in (rel.get("to_columns") or []) if isinstance(c, str)]
    if child is None or parent is None or not from_columns or not to_columns:
        return None, None
    if len(from_columns) != len(to_columns):
        return None, (
            f"relationship '{name}' has {len(from_columns)} child fields and "
            f"{len(to_columns)} parent fields; it is opaque rather than paired"
        )
    child_fields = field_columns(child, from_columns, connector)
    parent_fields = field_columns(parent, to_columns, connector)
    opaque = [f"{rel.get('from')}.{n}" for n in _opaque(child_fields)] + [
        f"{rel.get('to')}.{n}" for n in _opaque(parent_fields)
    ]
    if opaque:
        return None, (
            f"relationship '{name}' joins on {', '.join(opaque)}, which "
            f"{'links' if len(opaque) == 1 else 'link'} no physical column, so "
            "the relationship is declared but not verified: nothing probes it, "
            "and a composite is never checked on part of its tuple"
        )
    return [
        (str(a), str(b))
        for (_, a), (_, b) in zip(child_fields, parent_fields, strict=True)
    ], None


def _join(
    rel: dict[str, Any],
    by_name: dict[str, str],
    relations: dict[str, str],
    pairs: list[tuple[str, str]],
) -> tuple[DeclaredForeignKey | None, str | None]:
    """One relationship as a declared join, or a note saying why not.

    Ossie writes `from` as the many side and `to` as the one side, which is the
    direction `DeclaredForeignKey` already uses, so no side-swapping is needed.
    ``pairs`` are the physical columns its fields resolve to.

    **A composite relationship becomes a note here rather than a partial
    edge.** ``DeclaredForeignKey`` carries one column per side, so half of a
    composite join written into it would be a join the author never declared and
    a predicate the verifier would probe as though they had. The full ordered
    pairs reach map, relationships, and diagram through
    :func:`_declared_relationship` instead, which is the shape that can hold
    them.
    """

    name = rel.get("name")
    child = by_name.get(str(rel.get("from")))
    parent = by_name.get(str(rel.get("to")))
    if child is None or parent is None or not pairs:
        return None, None
    if len(pairs) != 1:
        return None, (
            f"relationship '{name}' joins {len(pairs)} columns to "
            f"{len(pairs)}, and a declared join carries one column per "
            "side, so it is recorded here as a note rather than as half of "
            "itself. The composite edge reaches the diagram through the "
            "semantic catalog"
        )
    return (
        DeclaredForeignKey(
            model=child,
            relation=relations.get(child),
            column=pairs[0][0],
            to_model=parent,
            to_relation=relations.get(parent),
            to_column=pairs[0][1],
            source=DECLARATION_SOURCE,
        ),
        None,
    )


def _declared_relationship(
    rel: dict[str, Any],
    by_name: dict[str, str],
    relations: dict[str, str],
    pairs: list[tuple[str, str]],
) -> DeclaredRelationship | None:
    """Preserve an Ossie relationship's full ordered pairs.

    The neutral shape a composite join survives in: every pair, in declared
    order, so a consumer probes the complete tuple rather than a component.
    """

    name = rel.get("name")
    child = by_name.get(str(rel.get("from")))
    parent = by_name.get(str(rel.get("to")))
    if child is None or parent is None or not pairs:
        return None
    return DeclaredRelationship(
        model=child,
        relation=relations.get(child),
        to_model=parent,
        to_relation=relations.get(parent),
        column_pairs=list(pairs),
        source=DECLARATION_SOURCE,
        name=str(name) if name else None,
    )
