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
from dataclasses import replace
from typing import Any

from ... import metricflow_dialect
from ...config import QueryLimits
from ...semantic_catalog import SemanticCatalogView
from ..results import SemanticQueryResult, SemanticValuesResult
from . import (
    DIMENSIONS_PER_QUERYABLE_PATH,
    EXECUTION_VENDOR,
    BackendDescriptor,
    MetricInfo,
    SemanticBackendError,
    SemanticCatalog,
    SemanticQuery,
    SemanticQueryRefusedError,
    ValuesRequest,
    cost_posture,
    merge_pii_meta,
    queryable_grains,
    requested_dimension_refs,
    resolve_values_request,
    screen_dimension_refs,
    screen_values_request,
    unadjudicated_refs,
    validate_grain,
    values_reach_note,
)
from .hosted_catalog import (
    HOSTED_CATALOG_FIELDS,
    HOSTED_CATALOG_GAPS,
    HOSTED_COST_WARNING,
    decode_hosted_catalog,
    hosted_grains,
)
from .hosted_transport import await_result, post_graphql, shape_json_result

_MISSING_EXTRA = (
    "the hosted semantic-layer backend needs the [semantic-api] extra: "
    "pip install 'exmergo-dex-core[semantic-api]'"
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
    cost_guard_warning = HOSTED_COST_WARNING
    catalog_gaps = HOSTED_CATALOG_GAPS
    # One row per token a query may group by, join-resolved by the API, including
    # dimensions reached through two joins. That is why this backend reports more
    # dimensions than a project read of the same layer.
    dimension_scope = DIMENSIONS_PER_QUERYABLE_PATH
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=HOSTED_CATALOG_GAPS,
        dimension_scope=dimension_scope,
        cost_guard_warning=cost_guard_warning,
    )

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
        return post_graphql(
            url=self._url,
            token=self._token,
            timeout=self._timeout,
            query=query,
        )

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        query = (
            "{ metrics(environmentId: "
            + self._env
            + ") { "
            + HOSTED_CATALOG_FIELDS
            + " } }"
        )
        data = self._post(query)

        return decode_hosted_catalog(data, self)

    def declared_relationships(self) -> list[Any]:
        """Cloud omits physical relations, so it cannot make safe edges."""

        return []

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """dbt's own declared keys already reach grain through its project
        format's ``definitions()``; stating them here too would double them."""

        return [], []

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

    # ---- the value domain --------------------------------------------------

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One dimension's value domain, executed by dbt Cloud.

        `createDimensionValuesQuery` returns a query id that polls exactly like an
        ordinary metric query, so everything after the request is the path this
        backend already has. What is different is in front of it: the dimension has
        to be resolved before it can be asked for, both to refuse a token the layer
        does not have and to know which metrics can reach it.

        The resolution inverts the catalog's own `metrics { dimensions }` rather
        than calling `metricsForDimensions`, which answers the same question. The
        API's field returns an empty list for an unknown name and for a metric's own
        time token alike, so a typo would come back as "no metric can be grouped by
        this" with nothing to distinguish the two, and the inversion is also what
        the local backend uses, which keeps the two answering identically.
        """

        request = resolve_values_request(self._values_view(), dimension, metrics)

        # Asked about every metric that can reach the dimension, not one of them:
        # `dimensions(metrics:)` returns the intersection across the metrics it is
        # given, so one call per metric unioned is what keeps the layer's own
        # metadata authoritative here, exactly as it is on a metric query.
        meta, _reported = self._query_metadata(request.metrics or request.reachable)
        notes = screen_values_request(
            request.name, meta_lookup=self._meta_lookup(meta, request.grains)
        )

        used = self._values_shape(request)
        query_id = self._create_values_query(request, used)
        if used and not request.metrics:
            notes.append(values_reach_note(request.token, used, request.reachable))
        json_result = self._await_result(query_id)
        cost, warnings = cost_posture(self)
        return SemanticValuesResult.from_capped(
            self._shape(json_result, extra_notes=notes),
            backend=self,
            dimension=request.token,
            scoped_to=used,
            query_id=query_id,
            cost=cost,
            warnings=warnings,
        )

    def _values_view(self) -> SemanticCatalogView:
        """The narrow slice of the catalog a values request needs, one free round
        trip: which metrics reach which tokens, and the grains the layer names.

        Narrow rather than the whole catalog because none of the rest is read here,
        and a values request should not pay for the layer's prose. The two fields
        answer each other: a token has to be split into a name and a grain before it
        can be looked up, and only the layer knows whether a trailing word is a
        granularity this project defined for itself or part of a dimension name.

        Returned as the neutral view so the resolution that follows is the same code
        the local backend runs. That is what keeps the two backends resolving one
        token identically, which matters because the resolution decides which
        metrics can reach a dimension and so what the answer is.
        """

        data = self._post(
            "{ metrics(environmentId: "
            + self._env
            + ") { name queryableGranularities queryableTimeGranularities "
            "dimensions { name } } }"
        )
        return SemanticCatalogView(
            metrics=[
                MetricInfo(
                    name=m.get("name"),
                    type="",
                    dimensions=[
                        d.get("name") for d in (m.get("dimensions") or []) if d
                    ],
                    queryable_granularities=hosted_grains(m),
                )
                for m in (data.get("metrics") or [])
                if m.get("name")
            ]
        )

    def _values_shape(self, request: ValuesRequest) -> list[str]:
        """Which metrics the layer will accept for this request, settled for free.

        A dimension of one semantic model is answerable on its own: the layer reads
        the distinct values of one relation. A dimension reached through a join is
        not, because there is no measure to join from, and the layer refuses the
        request. Scoping it to a metric that reaches it is the only shape that
        exists, and it answers a narrower question, so it is the fallback rather
        than the default.

        Settled with ``compileDimensionValuesSql``, which resolves the request and
        returns the SQL **without executing anything**, because dbt Cloud accepts
        the values mutation and reports a resolution failure asynchronously, at
        poll time. Deciding after that would mean running a second query only once
        the first had already been submitted; deciding here costs one free call and
        never sends a request the layer has said it will refuse. It is a resolution
        check and nothing more: no cost is claimed from it, and the SQL is not
        selected.

        A deployment that cannot answer the probe at all falls through to the
        caller's own shape and lets the query report the layer's own words, which
        is what happened before this existed.
        """

        attempts: list[list[str]] = [request.metrics] if request.metrics else [[]]
        if not request.metrics and request.reachable:
            attempts.append(request.reachable[:1])
        if len(attempts) == 1:
            return attempts[0]
        for used in attempts:
            try:
                self._post(
                    "mutation { compileDimensionValuesSql("
                    + ", ".join(self._values_arguments(request, used))
                    + ") { queryId } }"
                )
                return used
            except SemanticBackendError:
                continue
        return attempts[0]

    def _create_values_query(self, request: ValuesRequest, used: list[str]) -> str:
        mutation = (
            "mutation { createDimensionValuesQuery("
            + ", ".join(self._values_arguments(request, used))
            + ") { queryId } }"
        )
        data = self._post(mutation)
        query_id = (data.get("createDimensionValuesQuery") or {}).get("queryId")
        if not query_id:
            raise SemanticBackendError(
                "the semantic layer returned no queryId for a values request"
            )
        return query_id

    def _values_arguments(self, request: ValuesRequest, used: list[str]) -> list[str]:
        """The arguments both values mutations take, built once so the shape the
        probe resolved is exactly the shape that gets executed."""

        group_by = f'{{name: "{_ident(request.name)}"'
        group_by += f", grain: {request.grain.upper()}}}" if request.grain else "}"
        parts = [f"environmentId: {self._env}", f"groupBy: [{group_by}]"]
        if used:
            named = ", ".join(f'{{name: "{_ident(metric)}"}}' for metric in used)
            parts.append(f"metrics: [{named}]")
        return parts

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
                    grains = hosted_grains(metric) if isinstance(metric, dict) else None
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
        return await_result(
            self._post,
            environment_id=self._env,
            query_id=query_id,
            attempts=self._POLL_ATTEMPTS,
            interval=self._POLL_INTERVAL,
        )

    def _shape(
        self, json_result: Any, *, extra_notes: list[str] | None = None
    ) -> dict[str, Any]:
        return shape_json_result(
            json_result,
            limits=self._limits,
            extra_notes=extra_notes,
        )
