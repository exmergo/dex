"""The hosted dbt Cloud Semantic Layer backend: a thin GraphQL client over httpx.

dbt Cloud owns the warehouse connection and executes the query server-side, so
this backend does not open a warehouse adapter, does not estimate cost, and cannot
set a ceiling: the cost guard is structurally unavailable here and every result
says so. What dex still owns is the request and the returned aggregates, so PII is
screened before the query is sent (the layer's own dimension metadata when it
carries a PII flag, a name heuristic otherwise), the service token never leaves
this process, and the result is capped for agent context like ``explore query``.

Transport is single-transport GraphQL: ``createQuery`` (mutation) returns a
``queryId``, then ``query(queryId)`` is polled until ``SUCCESSFUL`` and its
``jsonResult`` (pandas ``orient='table'``) is reshaped into the columnar envelope.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from typing import Any

from ... import envelope as env
from ... import metricflow_dialect
from ...config import QueryLimits
from ..results import SemanticQueryResult
from . import (
    DIMENSIONS_PER_QUERYABLE_PATH,
    EXECUTION_VENDOR,
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticBackendError,
    SemanticCatalog,
    SemanticModelInfo,
    SemanticQuery,
    SemanticQueryRefusedError,
    cap_columnar,
    cost_posture,
    derive_entity_type,
    merge_element_fields,
    merge_pii_meta,
    queryable_grains,
    requested_dimension_refs,
    screen_dimension_refs,
    unadjudicated_refs,
    validate_grain,
)

# The very explicit line the founder asked for: wherever a reader or agent might
# expect the cost guard, say plainly that it does not apply on the hosted path.
_HOSTED_COST_WARNING = (
    "cost guard unavailable on the hosted semantic layer: dbt Cloud owns the "
    "warehouse connection and executes this query server-side, so dex applies no "
    "cost estimate or ceiling here. Spend is governed by the dbt Cloud "
    "environment's own limits, not by dex."
)

# What a hosted catalog cannot answer, declared per element kind rather than left
# to be inferred from a missing key. Measured by introspecting the live schema:
# `Entity` has no `label` field at all, `SemanticModel` carries only `name`, and
# `Measure` carries no words. A silent absence would read as a dbt project that
# documented none of it, which for a well-documented project is the opposite of
# the truth, and a note alone is the part of a payload a caller truncates first.
_HOSTED_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": [
        "label",
        "description",
        "model_ref",
        "agg_time_dimension",
        "primary_entity",
    ],
    "entities": ["label"],
    "measures": ["label", "description"],
}

# The one gap worth prose as well, because it is the field a caller is most
# likely to go looking for and there is somewhere better to get it.
_HOSTED_ENTITY_LABEL_NOTE = (
    "hosted list: the dbt Cloud Semantic Layer API exposes no label on entities "
    "(its Entity type has no such field), so an entity here carries a "
    "description at most; list with --local to read the labels the dbt project "
    "declares"
)

# A hosted catalog is reached metric by metric, so an element no metric touches is
# not in it. Said only when it can actually bite, which is any layer with metrics.
_HOSTED_REACH_NOTE = (
    "hosted list: every element is reached through a metric, so a measure, "
    "entity declaration or semantic model that no metric draws on is absent; "
    "list with --local for the layer as the project declares it"
)

_MISSING_EXTRA = (
    "the hosted semantic-layer backend needs the [semantic-api] extra: "
    "pip install 'exmergo-dex-core[semantic-api]'"
)


def _model_name(value: Any) -> str | None:
    """A ``semanticModel { name }`` selection reduced to the name it carries."""

    return value.get("name") if isinstance(value, dict) else None


def _bare_dimension(token: str | None, semantic_model: str | None) -> str | None:
    """The bare dimension name behind a queryable token.

    The API returns the token a query groups by (``agent__session__created_at``),
    never the declaration behind it, so the bare name is read off the token: the
    qualifier is a chain of entity names and ``__`` is the separator MetricFlow
    itself uses, so what follows the last one is the dimension. Together with the
    owning semantic model it identifies the declaration, which is what lets a
    caller see that several paths reach one dimension.

    Only where an owning model is known, which is what keeps dex's own synthesized
    ``metric_time`` from being given a declaration it does not have.
    """

    if not token or not semantic_model:
        return None
    _, separator, tail = token.rpartition("__")
    return tail if separator else token


def _metric_info(payload: dict[str, Any], owners: list[str]) -> MetricInfo:
    """One metric from the hosted catalog, composition included.

    Composition is kept portable and MetricFlow's own vocabulary is kept apart:
    a ratio's two sides and a derived metric's inputs mean the same thing in any
    semantic layer, while a cumulative window or a grain-to-date only means
    something here, so it travels under one declared vendor key.
    """

    params = payload.get("typeParams")
    params = params if isinstance(params, dict) else {}

    def named(value: Any) -> str | None:
        return value.get("name") if isinstance(value, dict) else None

    def window(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        count, granularity = value.get("count"), value.get("granularity")
        if count is None and granularity is None:
            return None
        return {"count": count, "granularity": granularity}

    inputs = [
        name
        for name in (named(x) for x in params.get("inputMeasures") or [])
        if name is not None
    ]
    input_metrics = [
        name
        for name in (named(x) for x in params.get("metrics") or [])
        if name is not None
    ]

    vendor: dict[str, Any] = {}
    if (cumulative := window(params.get("window"))) is not None:
        vendor["window"] = cumulative
    if params.get("grainToDate"):
        vendor["grain_to_date"] = params["grainToDate"]
    offsets = {
        x["name"]: offset
        for x in params.get("metrics") or []
        if isinstance(x, dict)
        and isinstance(x.get("name"), str)
        and (offset := window(x.get("offsetWindow"))) is not None
    }
    if offsets:
        vendor["offset_windows"] = offsets

    if payload.get("requiresMetricTime"):
        # Written only when true, so an absent key means false and a layer of
        # dozens of metrics does not pay for a false on each of them.
        vendor["requires_metric_time"] = True

    # One name over several columns: each measure aggregates over its own time
    # dimension, so a metric drawing on measures from two models has two.
    time_axis = sorted(
        {
            measure["aggTimeDimension"]
            for measure in payload.get("measures") or []
            if isinstance(measure, dict) and measure.get("aggTimeDimension")
        }
    )

    metric_filter = payload.get("filter")
    return MetricInfo(
        name=payload.get("name"),
        type=(payload.get("type") or "").lower(),
        label=payload.get("label"),
        description=payload.get("description"),
        dimensions=[d.get("name") for d in (payload.get("dimensions") or [])],
        semantic_models=owners or None,
        input_measures=inputs or None,
        time_axis=time_axis or None,
        queryable_granularities=_grains(payload),
        composition=MetricComposition(
            measure=named(params.get("measure")),
            numerator=named(params.get("numerator")),
            denominator=named(params.get("denominator")),
            expr=params.get("expr"),
            input_metrics=input_metrics or None,
        ),
        filter=(
            metric_filter.get("whereSqlTemplate")
            if isinstance(metric_filter, dict)
            else None
        ),
        vendor_params=vendor or None,
    )


def _screening_notes(unknown: list[str], meta: dict[str, Any] | None) -> list[str]:
    """The disclosure that the PII gate cleared dimensions on their names alone.

    The layer's own ``config.meta`` is what makes the gate authoritative here, the
    way a column profile does on the local backend. Where it says nothing the name
    heuristic is the only thing that ran, and a result that does not say so lets
    the weaker screening pass for the stronger one. The two silences are reported
    separately because they have different fixes: a layer that answered and carries
    no PII metadata wants the dimension marked in the dbt project, while a metadata
    call that never answered wants retrying.
    """

    if not unknown:
        return []
    if meta is None:
        return [
            "PII screening used the name heuristic alone: the dimension-metadata "
            "call to dbt Cloud did not answer, so the layer's own PII metadata "
            "was unavailable for this query."
        ]
    return [
        "PII screening used the name heuristic alone for "
        f"{', '.join(unknown)}: the semantic layer carries no PII metadata for "
        "them. Mark a dimension with `meta: {pii: true}` in the dbt project to "
        "make the layer authoritative."
    ]


_GRAIN_FIELDS = ("queryableTimeGranularities", "queryableGranularities")


def _grains(payload: dict[str, Any]) -> list[str] | None:
    """The grains the API reports for one metric or dimension, or None if it did
    not answer.

    Both fields are read and merged because they answer the same question in two
    shapes: ``queryableGranularities`` is the standard enum, and
    ``queryableTimeGranularities`` is the one that can also name a granularity the
    project defined for itself. An **empty list is the answer** for a categorical
    dimension, and it is the fact that stops a caller asking it for a month. A
    payload carrying neither field is a deployment that was not asked, which is
    not the same statement and must not read as one.
    """

    if not any(field in payload for field in _GRAIN_FIELDS):
        return None
    return metricflow_dialect.order_grains(
        [value for field in _GRAIN_FIELDS for value in payload.get(field) or []]
    )


# Metric/dimension/entity names are identifiers; validating them keeps
# caller-supplied values out of the GraphQL query as anything but a quoted name or
# a known enum, so a name can never smuggle in extra query structure.
_IDENT = re.compile(r"[A-Za-z0-9_]+")


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise SemanticBackendError(f"invalid semantic-layer name: {name!r}")
    return name


def _split_grain(
    token: str, default_grain: str | None, grains: tuple[str, ...]
) -> tuple[str, str | None]:
    """A group-by/order-by token to ``(name, grain)``, both safe to interpolate.

    The split itself is the dialect's, which is also where the vocabulary comes
    from: ``grains`` is what the layer reported for the metrics being queried, so a
    granularity a project defined for itself is recognized as a grain suffix
    instead of being read as part of a dimension name. A grain that arrives this
    way is already one of those values, and the name still passes ``_ident``,
    because both land in the query as structure rather than as a quoted value.
    """

    name, grain = metricflow_dialect.split_grain(token, default_grain, grains=grains)
    return _ident(name), grain


class HostedDbtCloudBackend:
    name = "dbt_cloud"
    vendor = "dbt"
    deployment = "dbt_cloud"
    # dbt Cloud owns the warehouse connection, so dex never holds a statement it
    # could price or cap. Everything the no-cost-guard posture needs follows from
    # this one declaration; see `cost_posture`.
    execution = EXECUTION_VENDOR
    cost_guard_warning = _HOSTED_COST_WARNING
    catalog_gaps = _HOSTED_CATALOG_GAPS
    # One row per token a query may group by, join-resolved by the API, including
    # dimensions reached through two joins. That is why this backend reports more
    # dimensions than a project read of the same layer.
    dimension_scope = DIMENSIONS_PER_QUERYABLE_PATH

    _POLL_ATTEMPTS = 90
    _POLL_INTERVAL = 1.0

    def __init__(
        self,
        host: str,
        environment_id: str,
        token: str,
        limits: QueryLimits,
        *,
        timeout: float = 60.0,
    ) -> None:
        self._url = f"https://{host}/api/graphql"
        self._env = str(environment_id)
        # Secret: held only for the Authorization header, never logged, never put
        # in an envelope (the sanitizer would hard-fail on it anyway).
        self._token = token
        self._limits = limits
        self._timeout = timeout

    @classmethod
    def from_config(cls, config, source=None) -> HostedDbtCloudBackend:
        """Build from a config, and from a host-supplied token when there is one.

        ``source`` is a :class:`~...connect.SemanticSource`. Its callable runs
        exactly once, here, so a metric query that polls dbt Cloud dozens of times
        costs the host one token read rather than dozens.
        """

        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise SemanticBackendError(_MISSING_EXTRA) from exc
        from ...connect import (
            CredentialDiscoveryError,
            resolve_semantic_layer_connection,
        )

        semantic = getattr(config, "semantic", None)
        try:
            host, env_id, token, _kind = resolve_semantic_layer_connection(
                semantic, os.environ, source
            )
        except CredentialDiscoveryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        limits = getattr(config, "query", None) or QueryLimits()
        return cls(host, env_id, token, limits)

    # ---- transport ---------------------------------------------------------

    def _post(self, query: str) -> dict[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query},
                )
        except httpx.HTTPError as exc:
            raise SemanticBackendError(
                f"could not reach the dbt Cloud Semantic Layer: {env.redact(str(exc))}"
            ) from exc
        if resp.status_code == 401 or resp.status_code == 403:
            raise SemanticBackendError(
                "dbt Cloud Semantic Layer rejected the token (HTTP "
                f"{resp.status_code}); check DBT_SL_TOKEN is a current "
                "'Semantic Layer Only' service token for this environment"
            )
        if resp.status_code != 200:
            raise SemanticBackendError(
                f"dbt Cloud Semantic Layer returned HTTP {resp.status_code}"
            )
        body = resp.json()
        if body.get("errors"):
            joined = "; ".join(str(e.get("message", e)) for e in body["errors"])
            raise SemanticBackendError(f"semantic layer error: {env.redact(joined)}")
        return body.get("data") or {}

    # ---- discovery ---------------------------------------------------------

    # One document, one round trip, and every field on it verified against the
    # live schema by introspection before it was written here. A field name the
    # schema does not have fails the *whole* request, so the metrics list goes
    # down with it: never add one speculatively. `label` on an entity is the
    # standing example (the API answers "Cannot query field 'label' on type
    # 'Entity'"), and `Measure` has no words either.
    _CATALOG_FIELDS = (
        "name type label description "
        "queryableGranularities queryableTimeGranularities requiresMetricTime "
        "filter { whereSqlTemplate } "
        "typeParams { measure { name } inputMeasures { name } "
        "numerator { name } denominator { name } expr "
        "window { count granularity } grainToDate "
        "metrics { name offsetWindow { count granularity } } } "
        "measures { name agg expr aggTimeDimension } "
        "semanticModels { name } "
        "dimensions { name type label description semanticModel { name } "
        "queryableGranularities queryableTimeGranularities } "
        "entities { name type description expr role semanticModel { name } }"
    )

    def list_definitions(self) -> SemanticCatalog:
        query = (
            "{ metrics(environmentId: "
            + self._env
            + ") { "
            + self._CATALOG_FIELDS
            + " } }"
        )
        data = self._post(query)

        metrics: list[MetricInfo] = []
        dims: dict[str, dict[str, Any]] = {}
        roles: dict[str, dict[str, EntityRole]] = {}
        entity_words: dict[str, str | None] = {}
        measures: dict[str, MeasureInfo] = {}
        models: set[str] = set()

        for m in data.get("metrics") or []:
            owners = [
                s["name"]
                for s in (m.get("semanticModels") or [])
                if isinstance(s, dict) and s.get("name")
            ]
            models.update(owners)

            for d in m.get("dimensions") or []:
                owner = _model_name(d.get("semanticModel"))
                if owner:
                    models.add(owner)
                merge_element_fields(
                    dims,
                    d.get("name"),
                    {
                        "type": d.get("type"),
                        "label": d.get("label"),
                        "description": d.get("description"),
                        "definition": _bare_dimension(d.get("name"), owner),
                        "semantic_model": owner,
                        "queryable_granularities": _grains(d),
                    },
                )

            for e in m.get("entities") or []:
                name, owner = e.get("name"), _model_name(e.get("semanticModel"))
                if not name:
                    continue
                if owner:
                    models.add(owner)
                # Keyed by (entity, model) because that is the unit the layer
                # declares: the same declaration is reached through every metric
                # that can group by it, and two declarations of one entity
                # genuinely differ in type, join key and description.
                roles.setdefault(name, {}).setdefault(
                    owner or "",
                    EntityRole(
                        semantic_model=owner or "",
                        type=str(e.get("type") or "").lower(),
                        expr=e.get("expr"),
                        role=e.get("role"),
                        description=e.get("description"),
                    ),
                )
                if entity_words.get(name) is None:
                    entity_words[name] = e.get("description")

            for measure in m.get("measures") or []:
                name = measure.get("name")
                if not name or name in measures:
                    continue
                measures[name] = MeasureInfo(
                    name=name,
                    agg=str(measure.get("agg") or "").lower() or None,
                    expr=measure.get("expr"),
                    agg_time_dimension=measure.get("aggTimeDimension"),
                    # Deduced, not returned: the API carries no owning model on a
                    # measure, and a measure lives in exactly one semantic model,
                    # so a metric naming one model pins every measure it reads.
                    # A measure reachable only through metrics that span several
                    # models stays unset rather than being attributed to a guess.
                    semantic_model=owners[0] if len(owners) == 1 else None,
                )

            metrics.append(_metric_info(m, owners))

        notes = [_HOSTED_REACH_NOTE]
        if roles:
            notes.append(_HOSTED_ENTITY_LABEL_NOTE)
        disagreeing = sorted(m.name for m in metrics if len(m.time_axis or ()) > 1)
        if disagreeing:
            notes.append(
                f"{', '.join(disagreeing)} aggregate over more than one time "
                "column (see time_axis): grouping by metric_time uses each "
                "measure's own, so the parts of one number can be bucketed by "
                "different timestamps"
            )
        return SemanticCatalog.from_backend(
            self,
            semantic_models=[SemanticModelInfo(name=n) for n in sorted(models)],
            metrics=metrics,
            dimensions=[
                DimensionInfo(
                    name=name,
                    type=fields.get("type") or "",
                    label=fields.get("label"),
                    description=fields.get("description"),
                    definition=fields.get("definition"),
                    semantic_model=fields.get("semantic_model"),
                    queryable_granularities=fields.get("queryable_granularities"),
                )
                for name, fields in sorted(dims.items())
            ],
            entities=[
                EntityInfo(
                    name=name,
                    type=derive_entity_type(list(declared.values())),
                    description=entity_words.get(name),
                    roles=[declared[key] for key in sorted(declared)],
                )
                for name, declared in sorted(roles.items())
            ],
            measures=[measures[name] for name in sorted(measures)],
            notes=notes,
        )

    # ---- query -------------------------------------------------------------

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        """The dimensions and entities these filter clauses name.

        dbt Cloud takes the clause as MetricFlow's Jinja dialect and renders it
        server-side, so what a clause references is read with that dialect's own
        grammar rather than with anything shared.
        """

        return metricflow_dialect.filter_refs(clauses)

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        refs = requested_dimension_refs(q, filter_refs=self.filter_refs)
        meta, reported = self._query_metadata(q.metrics)
        # Every grain the layer named, in one vocabulary, so a token carrying a
        # granularity this project defined for itself is read as a grain suffix
        # rather than as part of a dimension name.
        vocabulary = tuple(
            dict.fromkeys(grain for values in reported.values() for grain in values)
        )
        lookup = self._meta_lookup(meta, vocabulary)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII from "
                "the semantic layer. PII is flagged, never surfaced; query a "
                "non-PII dimension instead."
            )
        notes = _screening_notes(unadjudicated_refs(refs, meta_lookup=lookup), meta)

        # Refused after the gate on purpose: a query that would disclose PII is
        # refused for that reason whatever else is wrong with it.
        grain = validate_grain(q.grain, available=queryable_grains(q.metrics, reported))
        query_id = self._create_query(replace(q, grain=grain), vocabulary)
        json_result = self._await_result(query_id)
        # A hosted cost is a paradigm and nothing else: dbt Cloud ran the query
        # under its own warehouse connection, so there is no estimate dex could
        # honestly report and no ceiling it could have enforced. That follows from
        # `execution`, so it is read rather than restated here.
        cost, warnings = cost_posture(self)
        return SemanticQueryResult.from_capped(
            self._shape(json_result, extra_notes=notes),
            backend=self,
            query_id=query_id,
            cost=cost,
            warnings=warnings,
        )

    def _query_metadata(
        self, metrics: list[str]
    ) -> tuple[dict[str, Any] | None, dict[str, list[str]]]:
        """Everything dex asks the layer before sending a query, in one document:
        ``(dimension name -> its dbt config.meta, metric name -> queryable
        grains)``. The first is None when the layer could not be asked at all.

        **One aliased field per metric, in one document.** ``dimensions(metrics:)``
        returns the dimensions common to *all* the listed metrics, not their union,
        so asking about a whole multi-metric query at once shrinks the
        authoritative map instead of growing it, and the further apart the metrics
        are in the layer the less of it survives. Everything outside that
        intersection falls through to the name heuristic, which is the
        authoritative half of the PII gate quietly not running: the one thing this
        gate may not do. Asking per metric is what keeps it authoritative, and the
        aliases keep that to a single round trip, so nothing here needs to be
        issued concurrently.

        Best-effort by design: a metadata call that fails leaves the name heuristic
        screening every ref, which is the fail-closed floor. The None is what lets
        the caller tell that degradation apart from a layer that answered and simply
        carries no PII metadata, so neither one passes unremarked. One unknown
        metric fails the whole document rather than its own alias, which costs the
        map for the other metrics; that query is about to be refused by
        ``createQuery`` for the same reason, so it never reaches the warehouse.

        The grains ride along in the same document, unaliased and for every metric
        in the layer, because that field takes no metric argument and two fields of
        two words each are cheaper than a second round trip. They are what a
        requested ``--grain`` is validated against, so that a grain the layer
        accepts is not refused by dex and a grain it does not is refused by name."""

        if not metrics:
            # Nothing to ask about, and an empty selection set is not a document.
            # `_create_query` refuses this query by name a moment later.
            return None, {}
        try:
            fields = " ".join(
                f"a{index}: dimensions(environmentId: {self._env}, "
                f'metrics: [{{name: "{_ident(metric)}"}}]) '
                f"{{ name config {{ meta }} }}"
                for index, metric in enumerate(dict.fromkeys(metrics))
            )
            fields += (
                f" grains: metrics(environmentId: {self._env}) "
                "{ name queryableGranularities queryableTimeGranularities }"
            )
            data = self._post("{ " + fields + " }")
        except SemanticBackendError:
            return None, {}
        meta: dict[str, Any] = {}
        reported: dict[str, list[str]] = {}
        for alias, payload in data.items():
            if alias == "grains":
                for metric in payload or []:
                    grains = _grains(metric) if isinstance(metric, dict) else None
                    # A metric the layer answered about, only. An unanswered one
                    # stays out of the map, which is what makes the requested grain
                    # pass through to the layer instead of being refused by dex.
                    if grains is not None and metric.get("name"):
                        reported[metric["name"]] = grains
                continue
            for d in payload or []:
                cfg = d.get("config")
                value = cfg.get("meta") if isinstance(cfg, dict) else None
                merge_pii_meta(meta, d.get("name"), value)
        return meta, reported

    def _meta_lookup(self, meta: dict[str, Any] | None, grains: tuple[str, ...] = ()):
        """The gate's authoritative lookup, over the tokens a caller actually
        writes rather than the names the API returns.

        A group-by token may carry a time grain (``user__created_at__month``) that
        no dimension name has, so a token that misses is retried without it.
        Without that, a grain suffix is enough on its own to drop a flagged
        dimension to the name heuristic, which is the same fail-open the aliased
        query above closes."""

        if meta is None:
            return lambda _ref: None

        vocabulary = grains or metricflow_dialect.STANDARD_GRAINS

        def lookup(ref: str) -> Any:
            if ref in meta:
                return meta[ref]
            head, sep, tail = ref.rpartition("__")
            if sep and tail.lower() in vocabulary:
                return meta.get(head)
            return None

        return lookup

    def _create_query(self, q: SemanticQuery, grains: tuple[str, ...] = ()) -> str:
        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")
        metrics = ", ".join(f'{{name: "{_ident(m)}"}}' for m in q.metrics)
        parts = [f"environmentId: {self._env}", f"metrics: [{metrics}]"]
        if q.group_by:
            parts.append(f"groupBy: {self._group_by(q, grains)}")
        if q.where:
            clauses = ", ".join("{sql: " + json.dumps(c) + "}" for c in q.where)
            parts.append(f"where: [{clauses}]")
        if q.order_by:
            parts.append(f"orderBy: {self._order_by(q, grains)}")
        if q.limit:
            parts.append(f"limit: {int(q.limit)}")
        mutation = "mutation { createQuery(" + ", ".join(parts) + ") { queryId } }"
        data = self._post(mutation)
        query_id = (data.get("createQuery") or {}).get("queryId")
        if not query_id:
            raise SemanticBackendError("the semantic layer returned no queryId")
        return query_id

    def _group_by(self, q: SemanticQuery, grains: tuple[str, ...] = ()) -> str:
        entries = []
        for token in q.group_by:
            name, grain = _split_grain(token, q.grain, grains)
            if grain:
                entries.append(f'{{name: "{name}", grain: {grain.upper()}}}')
            else:
                entries.append(f'{{name: "{name}"}}')
        return "[" + ", ".join(entries) + "]"

    def _order_by(self, q: SemanticQuery, grains: tuple[str, ...] = ()) -> str:
        entries = []
        for token in q.order_by:
            descending = token.startswith("-")
            name, grain = _split_grain(
                token[1:] if descending else token, q.grain, grains
            )
            if name in q.metrics:
                inner = f'metric: {{name: "{name}"}}'
            elif grain:
                inner = f'groupBy: {{name: "{name}", grain: {grain.upper()}}}'
            else:
                inner = f'groupBy: {{name: "{name}"}}'
            entries.append(
                f"{{{inner}, descending: {'true' if descending else 'false'}}}"
            )
        return "[" + ", ".join(entries) + "]"

    def _await_result(self, query_id: str) -> Any:
        for _ in range(self._POLL_ATTEMPTS):
            query = (
                f"{{ query(environmentId: {self._env}, queryId: "
                f"{json.dumps(query_id)}) {{ status error "
                f"jsonResult(encoded: false) }} }}"
            )
            data = self._post(query)
            result = data.get("query") or {}
            status = result.get("status")
            if status == "SUCCESSFUL":
                return result.get("jsonResult")
            if status == "FAILED":
                raise SemanticBackendError(
                    "semantic layer query failed: "
                    f"{env.redact(str(result.get('error')))}"
                )
            time.sleep(self._POLL_INTERVAL)
        raise SemanticBackendError(
            f"timed out waiting for semantic layer query {query_id}"
        )

    def _shape(
        self, json_result: Any, *, extra_notes: list[str] | None = None
    ) -> dict[str, Any]:
        payload = (
            json.loads(json_result) if isinstance(json_result, str) else json_result
        ) or {}
        fields = (payload.get("schema") or {}).get("fields") or []
        columns: list[str] = []
        types: list[str] = []
        for f in fields:
            # `index` is the pandas row index, an artifact of orient='table'.
            if f.get("name") == "index":
                continue
            columns.append(f.get("name"))
            types.append(f.get("type"))
        rows = payload.get("data") or []
        cells = [[row.get(c) for c in columns] for row in rows]
        return cap_columnar(
            columns,
            types,
            cells,
            max_rows=self._limits.max_rows,
            max_cell_chars=self._limits.max_cell_chars,
            max_payload_bytes=self._limits.max_payload_bytes,
            extra_notes=extra_notes,
        )
