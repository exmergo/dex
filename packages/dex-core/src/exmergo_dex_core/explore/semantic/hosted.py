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
from typing import Any

from ... import envelope as env
from ...config import QueryLimits
from ..results import SemanticQueryResult
from . import (
    DimensionInfo,
    EntityInfo,
    MetricInfo,
    SemanticBackendError,
    SemanticCatalog,
    SemanticQuery,
    SemanticQueryRefusedError,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
    unadjudicated_refs,
)

# The very explicit line the founder asked for: wherever a reader or agent might
# expect the cost guard, say plainly that it does not apply on the hosted path.
_HOSTED_COST_WARNING = (
    "cost guard unavailable on the hosted semantic layer: dbt Cloud owns the "
    "warehouse connection and executes this query server-side, so dex applies no "
    "cost estimate or ceiling here. Spend is governed by the dbt Cloud "
    "environment's own limits, not by dex."
)

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


# MetricFlow standard time grains, the only values the API's grain enum accepts.
_GRAINS = ("day", "week", "month", "quarter", "year")
# Metric/dimension/entity names are identifiers; validating them keeps
# caller-supplied values out of the GraphQL query as anything but a quoted name or
# a known enum, so a name can never smuggle in extra query structure.
_IDENT = re.compile(r"[A-Za-z0-9_]+")


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise SemanticBackendError(f"invalid semantic-layer name: {name!r}")
    return name


def _split_grain(token: str, default_grain: str | None) -> tuple[str, str | None]:
    """A group-by/order-by token to ``(name, grain)``. A trailing ``__<grain>`` is
    a grain (``metric_time__month``); an ordinary ``entity__dimension`` is not.
    ``metric_time`` picks up the query's ``--grain`` when the token carries none."""

    name, grain = token, None
    if "__" in token:
        head, tail = token.rsplit("__", 1)
        if tail.lower() in _GRAINS:
            name, grain = head, tail.lower()
    if name == "metric_time" and grain is None and default_grain:
        grain = default_grain.lower()
    if grain is not None and grain not in _GRAINS:
        raise SemanticBackendError(
            f"unknown time grain '{grain}'; use one of {', '.join(_GRAINS)}"
        )
    return _ident(name), grain


class HostedDbtCloudBackend:
    name = "dbt_cloud"

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

    def list_definitions(self) -> SemanticCatalog:
        query = (
            "{ metrics(environmentId: " + self._env + ") "
            "{ name type label description "
            "dimensions { name type } entities { name type } } }"
        )
        data = self._post(query)
        metrics: list[MetricInfo] = []
        dim_types: dict[str, str] = {}
        ent_types: dict[str, str] = {}
        for m in data.get("metrics") or []:
            dims = [d.get("name") for d in (m.get("dimensions") or [])]
            for d in m.get("dimensions") or []:
                dim_types.setdefault(d.get("name"), (d.get("type") or "").lower())
            for e in m.get("entities") or []:
                ent_types.setdefault(e.get("name"), (e.get("type") or "").lower())
            metrics.append(
                MetricInfo(
                    name=m.get("name"),
                    type=(m.get("type") or "").lower(),
                    label=m.get("label"),
                    description=m.get("description"),
                    dimensions=dims,
                )
            )
        return SemanticCatalog(
            backend=self.name,
            metrics=metrics,
            dimensions=[
                DimensionInfo(name=n, type=t) for n, t in sorted(dim_types.items())
            ],
            entities=[EntityInfo(name=n, type=t) for n, t in sorted(ent_types.items())],
        )

    # ---- query -------------------------------------------------------------

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        refs = requested_dimension_refs(q)
        meta = self._dimension_meta(q.metrics)
        lookup = (lambda _ref: None) if meta is None else meta.get
        blocked = screen_dimension_refs(refs, meta_lookup=lookup)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII from "
                "the semantic layer. PII is flagged, never surfaced; query a "
                "non-PII dimension instead."
            )
        notes = _screening_notes(unadjudicated_refs(refs, meta_lookup=lookup), meta)

        query_id = self._create_query(q)
        json_result = self._await_result(query_id)
        # A hosted cost is a paradigm and nothing else: dbt Cloud ran the query
        # under its own warehouse connection, so there is no estimate dex could
        # honestly report and no ceiling it could have enforced.
        return SemanticQueryResult.from_capped(
            self._shape(json_result, extra_notes=notes),
            backend=self.name,
            query_id=query_id,
            cost=env.Cost(paradigm=env.Paradigm.HOSTED),
            warnings=[_HOSTED_COST_WARNING],
        )

    def _dimension_meta(self, metrics: list[str]) -> dict[str, Any] | None:
        """``dimension name -> its dbt config.meta`` for the PII gate, or None when
        the layer could not be asked at all.

        Best-effort by design: a metadata call that fails leaves the name heuristic
        screening every ref, which is the fail-closed floor. The None is what lets
        the caller tell that degradation apart from a layer that answered and simply
        carries no PII metadata, so neither one passes unremarked."""

        try:
            metric_inputs = ", ".join(f'{{name: "{_ident(m)}"}}' for m in metrics)
            query = (
                f"{{ dimensions(environmentId: {self._env}, metrics: "
                f"[{metric_inputs}]) {{ name config {{ meta }} }} }}"
            )
            data = self._post(query)
        except SemanticBackendError:
            return None
        meta: dict[str, Any] = {}
        for d in data.get("dimensions") or []:
            cfg = d.get("config")
            meta[d.get("name")] = cfg.get("meta") if isinstance(cfg, dict) else None
        return meta

    def _create_query(self, q: SemanticQuery) -> str:
        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")
        metrics = ", ".join(f'{{name: "{_ident(m)}"}}' for m in q.metrics)
        parts = [f"environmentId: {self._env}", f"metrics: [{metrics}]"]
        if q.group_by:
            parts.append(f"groupBy: {self._group_by(q)}")
        if q.where:
            clauses = ", ".join("{sql: " + json.dumps(c) + "}" for c in q.where)
            parts.append(f"where: [{clauses}]")
        if q.order_by:
            parts.append(f"orderBy: {self._order_by(q)}")
        if q.limit:
            parts.append(f"limit: {int(q.limit)}")
        mutation = "mutation { createQuery(" + ", ".join(parts) + ") { queryId } }"
        data = self._post(mutation)
        query_id = (data.get("createQuery") or {}).get("queryId")
        if not query_id:
            raise SemanticBackendError("the semantic layer returned no queryId")
        return query_id

    def _group_by(self, q: SemanticQuery) -> str:
        entries = []
        for token in q.group_by:
            name, grain = _split_grain(token, q.grain)
            if grain:
                entries.append(f'{{name: "{name}", grain: {grain.upper()}}}')
            else:
                entries.append(f'{{name: "{name}"}}')
        return "[" + ", ".join(entries) + "]"

    def _order_by(self, q: SemanticQuery) -> str:
        entries = []
        for token in q.order_by:
            descending = token.startswith("-")
            name, grain = _split_grain(token[1:] if descending else token, q.grain)
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
