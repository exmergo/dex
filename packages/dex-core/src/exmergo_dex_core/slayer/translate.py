"""Translating a dex :class:`SemanticQuery` into a SLayer ``SlayerQuery``.

One pure function, :func:`to_slayer_query`, from the query a caller asked for
and the catalog the layer reported to the query SLayer would render. Nothing is
read from disk, nothing is rendered, and the only SLayer it imports is the query
model, so a refusal here costs nothing and happens before SLayer plans anything.

**The mapping, v1.**

- A metric is a saved SLayer measure, named as the catalog names it,
  ``model.measure``. Its model becomes ``source_model`` and the measure's own
  name goes into ``measures``. Every metric must sit on the same model: SLayer
  roots a query at one model, and choosing a root for metrics on several is a
  modelling decision dex will not make for the caller.
- ``--group-by model.column`` becomes a dimension, the column on the source
  model. The catalog decides what a metric is groupable by, so a token outside
  that list is refused by name rather than left for SLayer to reject or, worse,
  to resolve through a join the catalog never reported.
- ``--grain`` becomes a ``TimeDimension`` on the source model's default time
  dimension, at a built-in SLayer granularity. A model with no time dimension
  refuses it. Grouping by that same column as well folds it into the time
  dimension, which is what the caller meant and the only form SLayer accepts:
  the raw column and its bucket share one result name.
- ``--group-by model.column.value`` groups by the elements of a list column
  (``type: ARRAY``), one row per element. The query is sent with
  ``to_many_handling='associate'``, so each element row counts the parents that
  hold it; SLayer's default would repeat the metric's whole total on every
  element row. The whole list column is listed but groups nothing, and grouping
  by it or reading its values is refused here, naming the ``.value`` token.
- ``--where`` becomes ``filters``, verbatim, in SLayer's own dialect. A clause
  carrying ``{var}`` syntax is refused, because SLayer fills a placeholder from
  the model's declared defaults when a query passes none, and the column it
  becomes is decided after the PII gate has screened the filter.
- ``--order-by`` (``-token`` for descending) and ``--limit`` map directly. An
  order token must be one of the query's own metrics or grouped columns: the
  PII gate screens what a query groups and filters by, so ordering by anything
  else would reach a column nothing screened.

**A values request** (:func:`to_slayer_values_query`) is a dimension-only
query: the one column, no measure, and ``distinct_dimension_values`` on, which
SLayer renders as a ``GROUP BY`` of that column. It is rooted at the model the
dimension belongs to, or, with ``--metric``, at that metric's model, where the
dimension must be groupable. A grain suffix buckets a time dimension.

**Refused, in every form: an ad-hoc formula.** A metric here is a catalog name.
SLayer would accept ``sum(amount)`` as a measure, but that is a definition
written at query time, which the layer never reviewed and the catalog never
listed.

Every refusal is a :class:`SemanticBackendError` naming the token it refused.
"""

from __future__ import annotations

# First, before any SLayer import: importing SLayer rewrites sqlglot's reserved
# keywords process-wide, and this module undoes that (see `sqlglot_keywords`).
from . import sqlglot_keywords  # noqa: F401

# isort: split

import re
from collections.abc import Iterable

from pydantic import ValidationError
from slayer.core.enums import TimeGranularity
from slayer.core.query import (
    ColumnRef,
    OrderItem,
    SlayerQuery,
    TimeDimension,
    has_variable_syntax,
)

from ..explore.semantic.backend import SemanticBackendError
from ..explore.semantic.model import SemanticQuery, ValuesRequest
from ..semantic_catalog import MetricInfo, SemanticCatalogView, SemanticModelInfo

__all__ = [
    "ASSOCIATE",
    "GRAINS",
    "element_note",
    "element_tokens",
    "list_tokens",
    "source_model",
    "to_slayer_query",
    "to_slayer_values_query",
]

#: How a query grouped by a list column's elements treats the fan-out: each
#: element row counts the parents holding it (SLayer's `associate`).
ASSOCIATE = "associate"

# A catalog name: identifiers joined by dots. Anything else in a metric position
# is an expression, which is a formula rather than a name.
_NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")

#: SLayer's built-in time granularities, the vocabulary a grain is read from.
GRAINS = tuple(grain.value for grain in TimeGranularity)

_DISCOVER = "List what the layer exposes with `explore semantic list`"


def to_slayer_query(q: SemanticQuery, view: SemanticCatalogView) -> SlayerQuery:
    """The ``SlayerQuery`` for ``q``, resolved against ``view``, or a refusal."""

    metrics = _metrics(q.metrics, view)
    source = _source_model(metrics, view)
    groupable = _groupable(metrics)

    grain = q.grain.lower() if q.grain else None
    time_column = _time_column(grain, source) if grain else None
    columns: dict[str, str] = {}  # grouped token -> column, in request order
    for token in dict.fromkeys(q.group_by):
        if token not in groupable:
            _refuse_whole_list(token, view, "group by")
            raise SemanticBackendError(
                f"cannot group {_names(metrics)} by {token}: the layer does not "
                f"report it as groupable for "
                f"{'that metric' if len(metrics) == 1 else 'every one of them'}. "
                f"A SLayer metric groups by the columns of its own model, named "
                f"model.column; bucket its time dimension with --grain. See "
                f"`explore semantic list --metric {metrics[0].name}`"
            )
        columns[token] = _column(token, source)

    measures = {metric.name: _measure(metric, source) for metric in metrics}
    time_token = f"{source.name}.{time_column}" if time_column else None

    fields: dict[str, object] = {
        "source_model": source.name,
        "measures": list(measures.values()),
    }
    dimensions = [
        ColumnRef(name=column)
        for token, column in columns.items()
        if token != time_token
    ]
    if dimensions:
        fields["dimensions"] = dimensions
    if any(_is_element(token, view) for token in columns):
        # One row per element, and a parent holding several counts under each.
        # SLayer's default (`broadcast`) instead repeats the metric's whole total
        # on every element row, a number that reads per element and is not.
        fields["to_many_handling"] = ASSOCIATE
    if time_column:
        fields["time_dimensions"] = [
            TimeDimension(dimension=ColumnRef(name=time_column), granularity=grain)
        ]
    if q.where:
        fields["filters"] = _filters(q.where)
    if q.order_by:
        orderable = {**measures, **columns}
        if time_token:
            orderable[time_token] = time_column
        fields["order"] = _order(q.order_by, orderable)
    if q.limit is not None:
        fields["limit"] = q.limit

    try:
        return SlayerQuery(**fields)
    except ValidationError as exc:
        raise SemanticBackendError(
            f"SLayer refused the query dex translated: {exc.errors()[0]['msg']}"
        ) from exc


def to_slayer_values_query(
    request: ValuesRequest, view: SemanticCatalogView
) -> SlayerQuery:
    """The dimension-only ``SlayerQuery`` that reads ``request``'s value domain.

    Unscoped, it is rooted at the model that declares the dimension, so the
    values are that column's under the model's own filters. Scoped, it is rooted
    at the metrics' model and the dimension has to be one they group by, so a
    scope never quietly reaches a column through a join the catalog did not
    report. No measure is rendered either way.
    """

    dimensions = {dimension.name: dimension for dimension in view.dimensions}
    dimension = dimensions.get(request.name)
    if dimension is None or not dimension.semantic_model:
        raise SemanticBackendError(
            f"no such dimension in this semantic layer: {request.name}. "
            f"{_DISCOVER}; a SLayer dimension is a model's column, named "
            "model.column"
        )

    _refuse_whole_list(request.name, view, "read the values of")
    if request.metrics:
        metrics = _metrics(request.metrics, view)
        source = _source_model(metrics, view)
        if request.name not in _groupable(metrics):
            raise SemanticBackendError(
                f"{request.name} is not groupable for {_names(metrics)}, so it has "
                "no values for "
                f"{'that metric' if len(metrics) == 1 else 'those metrics'}. A "
                "SLayer metric groups by the columns of its own model; drop "
                "--metric to read the column's own values. See "
                f"`explore semantic list --metric {metrics[0].name}`"
            )
    else:
        models = {model.name: model for model in view.semantic_models}
        source = models.get(dimension.semantic_model)
        if source is None:
            raise SemanticBackendError(
                f"the layer names no model for {request.name}, so there is no "
                "model to read its values from"
            )

    column = ColumnRef(name=_column(request.name, source))
    fields: dict[str, object] = {
        "source_model": source.name,
        "distinct_dimension_values": True,
    }
    if request.grain:
        if dimension.type != "time":
            raise SemanticBackendError(
                f"{request.name} is not a time dimension, so it has no "
                f"{request.grain} grain to bucket its values at"
            )
        fields["time_dimensions"] = [
            TimeDimension(dimension=column, granularity=request.grain)
        ]
    else:
        fields["dimensions"] = [column]

    try:
        return SlayerQuery(**fields)
    except ValidationError as exc:
        raise SemanticBackendError(
            f"SLayer refused the query dex translated: {exc.errors()[0]['msg']}"
        ) from exc


def source_model(requested: list[str], view: SemanticCatalogView) -> SemanticModelInfo:
    """The one model ``requested`` metrics are queried from, or a refusal.

    The root a SLayer filter is read against, which the PII gate needs before
    the rest of the query is translated.
    """

    return _source_model(_metrics(requested, view), view)


def _metrics(requested: list[str], view: SemanticCatalogView) -> list[MetricInfo]:
    """Each requested metric, from the catalog, once."""

    if not requested:
        raise SemanticBackendError("a metric query needs at least one --metric")
    known = {metric.name: metric for metric in view.metrics}
    found: list[MetricInfo] = []
    for token in dict.fromkeys(requested):
        metric = known.get(token)
        if metric is not None:
            found.append(metric)
        elif not _NAME.fullmatch(token):
            raise SemanticBackendError(
                f"'{token}' is a formula, not a metric. A SLayer metric query takes "
                "a saved measure by its catalog name (model.measure): a formula "
                "written at query time is a definition the layer never reviewed. "
                f"Save it as a measure on the model. {_DISCOVER}"
            )
        else:
            raise SemanticBackendError(
                f"no such metric in this semantic layer: {token}. {_DISCOVER}; a "
                "SLayer metric is a saved measure named model.measure"
            )
    return found


def _source_model(
    metrics: list[MetricInfo], view: SemanticCatalogView
) -> SemanticModelInfo:
    """The one model every metric is queried from."""

    roots = list(dict.fromkeys(m for metric in metrics for m in metric.semantic_models))
    if len(roots) > 1:
        by_model = "; ".join(
            f"{root}: {_names(m for m in metrics if root in m.semantic_models)}"
            for root in roots
        )
        raise SemanticBackendError(
            f"these metrics come from more than one SLayer model ({by_model}). "
            "A SLayer query is rooted at one model, so query each model's metrics "
            "separately"
        )
    models = {model.name: model for model in view.semantic_models}
    model = models.get(roots[0]) if roots else None
    if model is None:
        raise SemanticBackendError(
            f"the layer names no model for {_names(metrics)}, so there is no "
            "source model to query it from"
        )
    return model


def element_tokens(tokens: Iterable[str], view: SemanticCatalogView) -> list[str]:
    """The tokens among ``tokens`` that group by a list column's elements."""

    return [token for token in dict.fromkeys(tokens) if _is_element(token, view)]


def list_tokens(tokens: Iterable[str], view: SemanticCatalogView) -> list[str]:
    """The tokens among ``tokens`` that read a list column, whole or by element."""

    found = []
    for token in dict.fromkeys(tokens):
        params = _dimension_params(token, view) or {}
        if params.get("element_of") or params.get("groupable") is False:
            found.append(token)
    return found


def element_note(tokens: list[str]) -> str:
    """What a grouping by list elements means for the numbers."""

    return (
        f"grouped by the elements of a list column ({', '.join(tokens)}): each row "
        "counts the parents holding that element, so a parent with several "
        "elements counts under each, rows overlap, and they do not sum to the "
        "metric's total. A parent with no elements, or a NULL list, is in no row"
    )


def _is_element(token: str, view: SemanticCatalogView) -> bool:
    params = _dimension_params(token, view)
    return bool(params and params.get("element_of"))


def _dimension_params(token: str, view: SemanticCatalogView) -> dict | None:
    for dimension in view.dimensions:
        if dimension.name == token:
            return dimension.vendor_params or None
    return None


def _refuse_whole_list(token: str, view: SemanticCatalogView, verb: str) -> None:
    """Refuse grouping by, or reading the values of, a whole list column.

    SLayer groups a list by its elements and refuses the list itself
    (``ListTypeError``) only once it renders; here the refusal comes first and
    names the element token to use instead.
    """

    params = _dimension_params(token, view)
    if not params or params.get("groupable") is not False:
        return
    element = params.get("group_by")
    if element:
        model = token.rsplit(".", 1)[0]
        raise SemanticBackendError(
            f"cannot {verb} {token}: it is a list column "
            f"(ARRAY of {params.get('element_type')}), and SLayer groups a list by "
            f"its elements. Use {model}.{element}; filter on the whole list with "
            f"contains({token.rsplit('.', 1)[1]}, ...), contains_any or "
            "contains_all in --where"
        )
    raise SemanticBackendError(
        f"cannot {verb} {token}: it is a list column whose element type is "
        "UNKNOWN, so SLayer has no element to group by. Declare its "
        "`element_type` on the model"
    )


def _groupable(metrics: list[MetricInfo]) -> set[str]:
    """The tokens every one of these metrics can be grouped by."""

    tokens = set(metrics[0].dimensions)
    for metric in metrics[1:]:
        tokens &= set(metric.dimensions)
    return tokens


def _measure(metric: MetricInfo, source: SemanticModelInfo) -> str:
    """A saved measure's own name: the metric token without its model."""

    prefix = f"{source.name}."
    if not metric.name.startswith(prefix):
        raise SemanticBackendError(
            f"metric {metric.name} is not named model.measure on model "
            f"'{source.name}', so it does not name a saved SLayer measure"
        )
    return metric.name[len(prefix) :]


def _column(token: str, source: SemanticModelInfo) -> str:
    prefix = f"{source.name}."
    if not token.startswith(prefix):
        raise SemanticBackendError(
            f"{token} is not a column of model '{source.name}', and a SLayer metric "
            "query groups by its source model's own columns"
        )
    return token[len(prefix) :]


def _time_column(grain: str, source: SemanticModelInfo) -> str:
    """The column ``--grain`` buckets: the model's default time dimension."""

    if grain.lower() not in GRAINS:
        raise SemanticBackendError(
            f"unknown time grain '{grain}' for a SLayer layer; SLayer's built-in "
            f"grains are {', '.join(GRAINS)}. A datasource's custom grain is not "
            "available: dex does not read datasources, which carry credentials"
        )
    if not source.agg_time_dimension:
        raise SemanticBackendError(
            f"model '{source.name}' has no time dimension, so --grain {grain} has "
            "nothing to bucket. Set `default_time_dimension` on the model, or drop "
            "--grain"
        )
    return source.agg_time_dimension


def _filters(clauses: list[str]) -> list[str]:
    for clause in clauses:
        if has_variable_syntax(clause):
            raise SemanticBackendError(
                f"refused filter {clause!r}: it carries {{variable}} syntax, which "
                "SLayer fills from the model's declared defaults after the PII gate "
                "has read the filter. Write the value into the clause"
            )
    return list(clauses)


def _order(tokens: list[str], orderable: dict[str, str]) -> list[OrderItem]:
    """Each ``token`` or ``-token`` as a sort key on a result column."""

    items: list[OrderItem] = []
    for raw in tokens:
        descending = raw.startswith("-")
        token = raw[1:] if descending else raw
        column = orderable.get(token)
        if column is None:
            raise SemanticBackendError(
                f"cannot order by {token}: a SLayer metric query orders by its own "
                f"metrics and grouped columns ({', '.join(orderable)}). Group by it "
                "first, so the PII gate screens it, or order by one of those"
            )
        items.append(
            OrderItem(
                column=ColumnRef(name=column),
                direction="desc" if descending else "asc",
            )
        )
    return items


def _names(metrics: Iterable[MetricInfo]) -> str:
    return ", ".join(metric.name for metric in metrics)
