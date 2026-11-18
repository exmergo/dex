"""What a Cube server says about its own model, read: ``/v1/meta`` and ``/v1/load``.

Pure functions over the two JSON documents, so the backend that calls the server
(:mod:`...explore.semantic.cube_vendor`) and the tests that replay one share the
decoding and nothing else.

**``/v1/meta?extended`` is the compiled model** (#914): every cube and view the
server compiled, JavaScript models, Jinja templates and ``extends`` included,
which the static reader can only name as compiled-only. Under
``deployment: cube_cloud`` it is the catalog ``explore semantic`` reads. What it
cannot say is where a cube's rows come from: Cube never emits ``sql_table``, and
a member's SQL arrives as Cube's compiled template rather than a column, so a catalog
read here carries no relation and no column, and says so in ``unavailable``.

**``/v1/load`` is Cube executing.** Its rows arrive keyed by member, with a
time dimension bucketed as ``member.grain``, and every number as a string
(Cube's drivers return decimals as text to keep their precision), so the
``annotation`` beside them is what says which columns are numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from ..semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DimensionInfo,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
)
from .catalog import AGGREGATIONS
from .model import DIMENSION, MEASURE, Member
from .query import Target
from .validator import PREDEFINED_GRANULARITIES

__all__ = [
    "CUBE_CLOUD_DISCLOSURE",
    "CUBE_HOSTED_CATALOG_GAPS",
    "CUBE_VENDOR_COST_WARNING",
    "decode_load",
    "meta_catalog",
    "meta_members",
    "meta_pii",
]

#: The warning every ``execution: vendor`` Cube result carries. Cube ran the
#: query under its own warehouse connection, and neither Cube Core nor Cube Cloud
#: has an API that prices a query before it runs, so there was nothing to
#: estimate and no ceiling dex could have enforced.
CUBE_VENDOR_COST_WARNING = (
    "cost guard unavailable: the Cube server executed this query (/v1/load) under "
    "its own warehouse connection, and Cube has no API that prices a query before "
    "it runs, so dex neither estimated it nor enforced a ceiling. Pass --local to "
    "have Cube render the SQL and dex run it under the cost guard"
)

#: Said on every result whose catalog is the Cube Cloud deployment's own.
CUBE_CLOUD_DISCLOSURE = (
    "definitions read from the Cube Cloud deployment's compiled /v1/meta, not "
    "from this repository's reviewed model files: what Cube Cloud serves is not "
    "checked against them. Pass --local to render against the reviewed files, "
    "which refuses a deployment serving a different model"
)

#: Fields ``/v1/meta`` has nothing for, per element kind. A cube's relation and a
#: member's column are the whole of what a catalog needs to send ``explore
#: profile`` to the table behind a metric, so their absence is declared rather
#: than left as a null a caller would read as a cube on no table.
CUBE_HOSTED_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["model_ref", "primary_entity", "relation"],
    "dimensions": ["column"],
    "measures": ["column", "expr"],
    "entities": ["name", "type", "label", "description", "roles"],
}

_RELATIONSHIPS = {
    "belongsto": "many_to_one",
    "many_to_one": "many_to_one",
    "hasmany": "one_to_many",
    "one_to_many": "one_to_many",
    "hasone": "one_to_one",
    "one_to_one": "one_to_one",
}


def _elements(meta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        element
        for element in meta.get("cubes") or []
        if isinstance(element, Mapping) and isinstance(element.get("name"), str)
    ]


def _entries(element: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    return [
        entry
        for entry in element.get(key) or []
        if isinstance(entry, Mapping)
        and isinstance(entry.get("name"), str)
        and entry.get("public") is not False
    ]


def _short(token: str) -> str:
    return token.split(".", 1)[1] if "." in token else token


def _granularities(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        g["name"]
        for g in entry.get("granularities") or []
        if isinstance(g, Mapping) and isinstance(g.get("name"), str)
    )


def _member(entry: Mapping[str, Any], kind: str) -> Member:
    """A meta entry as the reader's :class:`~.model.Member`.

    A measure's ``type`` is its aggregation, as in the YAML (``sum``), which is
    what meta calls ``aggType``; a calculated measure's is ``number``.
    """

    sql = entry.get("sql")
    return Member(
        name=_short(entry["name"]),
        kind=kind,
        type=(entry.get("aggType") or entry.get("type"))
        if kind == MEASURE
        else entry.get("type"),
        sql=sql if isinstance(sql, str) else None,
        title=entry.get("title") if isinstance(entry.get("title"), str) else None,
        description=entry.get("description")
        if isinstance(entry.get("description"), str)
        else None,
        primary_key=bool(entry.get("primaryKey")),
        granularities=_granularities(entry),
        rolling_window={"cumulative": True} if entry.get("cumulative") else None,
    )


def meta_members(meta: Mapping[str, Any]) -> dict[str, Target]:
    """Every queryable token the server compiled, as the translation reads them.

    A view member names the cube member it exposes in ``aliasMember``, which is
    the cube a data-source check would follow; a member without one is its own.
    """

    found: dict[str, Target] = {}
    for element in _elements(meta):
        owner = element["name"]
        for key, kind in (("dimensions", DIMENSION), ("measures", MEASURE)):
            for entry in _entries(element, key):
                alias = entry.get("aliasMember")
                alias = alias if isinstance(alias, str) and "." in alias else None
                found[entry["name"]] = Target(
                    entry["name"],
                    kind,
                    _member(entry, kind),
                    alias.split(".", 1)[0] if alias else owner,
                    alias,
                )
    return found


def meta_pii(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Each member's own ``meta``, by token, for the PII gate to read.

    A member a Cube model marks ``meta: {pii: true}`` is refused on that word;
    everything else falls to the cache and the name heuristic.
    """

    found: dict[str, Any] = {}
    for element in _elements(meta):
        for key in ("dimensions", "measures"):
            for entry in _entries(element, key):
                value = entry.get("meta")
                if isinstance(value, Mapping):
                    found[entry["name"]] = dict(value)
    return found


def meta_catalog(meta: Mapping[str, Any]) -> SemanticCatalogView:
    """The neutral read catalog for a compiled model, in the static reader's
    shape: a cube or view per semantic model, each dimension keyed
    ``cube.member``, each aggregating measure a measure and a ``simple`` (or
    ``cumulative``) metric, each calculated one a ``derived`` metric.

    Groupability follows the joins meta declares, directed as Cube's join graph
    is; a view is grouped by its own members. A calculated measure's SQL is not
    served, so a ratio cannot be told from any other composition and none is
    claimed.
    """

    elements = _elements(meta)
    notes: list[str] = []
    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    measures: list[MeasureInfo] = []
    metrics: list[MetricInfo] = []

    own_dimensions = {
        element["name"]: [e["name"] for e in _entries(element, "dimensions")]
        for element in elements
    }
    joins = {
        element["name"]: [
            j["name"]
            for j in element.get("joins") or []
            if isinstance(j, Mapping) and isinstance(j.get("name"), str)
        ]
        for element in elements
    }
    reach: dict[str, list[str]] = {}
    for name in own_dimensions:
        seen, frontier = [name], [name]
        while frontier:
            for target in joins.get(frontier.pop(0), []):
                if target in own_dimensions and target not in seen:
                    seen.append(target)
                    frontier.append(target)
        reach[name] = seen

    for element in elements:
        name = element["name"]
        is_view = element.get("type") == "view"
        times = [
            _short(e["name"])
            for e in _entries(element, "dimensions")
            if e.get("type") == "time"
        ]
        time = times[0] if len(times) == 1 else None
        if len(times) > 1:
            notes.append(
                f"{'view' if is_view else 'cube'} '{name}' has {len(times)} time "
                f"dimensions ({', '.join(times)}) and Cube declares no default, so "
                "its measures name no time axis: a query names the one it buckets by"
            )
        models.append(
            SemanticModelInfo(
                name=name,
                label=element.get("title")
                if isinstance(element.get("title"), str)
                else None,
                description=element.get("description")
                if isinstance(element.get("description"), str)
                else None,
                agg_time_dimension=time,
            )
        )
        for entry in _entries(element, "dimensions"):
            member = _member(entry, DIMENSION)
            is_time = member.type == "time"
            params: dict[str, Any] = {"type": member.type}
            if member.primary_key:
                params["primary_key"] = True
            if member.granularities:
                params["granularities"] = list(member.granularities)
            if isinstance(entry.get("aliasMember"), str):
                params["alias_member"] = entry["aliasMember"]
            dimensions.append(
                DimensionInfo(
                    name=entry["name"],
                    type="time" if is_time else "categorical",
                    label=member.title,
                    description=member.description,
                    definition=member.name,
                    semantic_model=name,
                    queryable_granularities=(
                        [*PREDEFINED_GRANULARITIES, *member.granularities]
                        if is_time
                        else []
                    ),
                    vendor_params=params,
                )
            )
        groupable = [
            token
            for owner in ([name] if is_view else reach[name])
            for token in own_dimensions.get(owner, [])
        ]
        axis = next(
            (
                e
                for e in _entries(element, "dimensions")
                if time is not None and _short(e["name"]) == time
            ),
            None,
        )
        grains = [*PREDEFINED_GRANULARITIES, *_granularities(axis)] if axis else None
        for entry in _entries(element, "measures"):
            member = _member(entry, MEASURE)
            aggregating = member.type in AGGREGATIONS
            params = {"type": member.type}
            if isinstance(entry.get("aliasMember"), str):
                params["alias_member"] = entry["aliasMember"]
            if aggregating:
                measures.append(
                    MeasureInfo(
                        name=member.name,
                        agg=AGGREGATIONS[member.type or ""],
                        agg_time_dimension=time,
                        label=member.title,
                        description=member.description,
                        semantic_model=name,
                    )
                )
            served_filters = [
                f["sql"]
                for f in entry.get("filters") or []
                if isinstance(f, Mapping) and isinstance(f.get("sql"), str)
            ]
            metrics.append(
                MetricInfo(
                    name=entry["name"],
                    filter=" AND ".join(f"({f})" for f in served_filters) or None,
                    type=(
                        ("cumulative" if member.rolling_window else "simple")
                        if aggregating
                        else "derived"
                    ),
                    label=member.title,
                    description=member.description,
                    dimensions=list(groupable),
                    semantic_models=[name],
                    input_measures=[member.name] if aggregating else None,
                    composition=MetricComposition(measure=member.name)
                    if aggregating
                    else None,
                    time_axis=[time] if time else None,
                    queryable_granularities=grains,
                    vendor_params=params,
                )
            )

    notes.append(
        "read from the Cube server's compiled /v1/meta: JavaScript, Jinja and "
        "`extends` definitions are answered as compiled, so nothing is named "
        "compiled-only; /v1/meta carries no sql_table, and member SQL arrives as "
        "Cube's compiled template, so no "
        "relation or column is claimed (see unavailable)"
    )
    return SemanticCatalogView(
        semantic_models=models,
        metrics=metrics,
        dimensions=dimensions,
        measures=measures,
        dimension_scope=DIMENSIONS_PER_DECLARATION,
        notes=notes,
    )


def relationship(raw: object) -> str | None:
    """A meta join's relationship, canonical (``belongsTo`` is ``many_to_one``)."""

    key = str(raw or "").replace("_", "").lower()
    return _RELATIONSHIPS.get(key) or _RELATIONSHIPS.get(str(raw or "").lower())


def _number(value: Any) -> Any:
    """A numeric string as an int where it is whole, else a float; anything else
    as it came. A value Cube returned as a number is kept."""

    if not isinstance(value, str):
        return value
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return value
    if not parsed.is_finite():
        return value
    if parsed == parsed.to_integral_value() and "e" not in value.lower():
        return int(parsed)
    return float(parsed)


def decode_load(
    body: Mapping[str, Any], names: Mapping[str, str], order: Sequence[str]
) -> tuple[list[str], list[str], list[list[Any]]]:
    """``/v1/load``'s rows as ``(columns, types, cells)``, in the caller's tokens.

    ``names`` maps Cube's result key (``member`` or ``member.grain``) to the
    token the caller wrote; ``order`` is those keys in the order the caller
    named them. A number Cube returned as text is decoded where the annotation
    says the member is a number; a time bucket stays the ISO text Cube sends.
    """

    annotation = body.get("annotation") or {}
    kinds: dict[str, str] = {}
    for section in ("measures", "dimensions", "timeDimensions"):
        for key, entry in (annotation.get(section) or {}).items():
            if isinstance(entry, Mapping):
                kinds[key] = str(entry.get("type") or "")
    keys = [key for key in order if key in names]
    columns = [names[key] for key in keys]
    types = [kinds.get(key) or "string" for key in keys]
    cells: list[list[Any]] = []
    for row in body.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        cells.append(
            [
                _number(row.get(key)) if kind == "number" else row.get(key)
                for key, kind in zip(keys, types, strict=True)
            ]
        )
    return columns, types, cells
