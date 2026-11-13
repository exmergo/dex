"""dbt Cloud GraphQL transport, polling, and tabular response decoding."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ... import envelope as env
from ...config import QueryLimits
from ...semantic_hosts import SemanticHostRefusedError, check_semantic_layer_host
from ...value_mask import mask_values
from .backend import SemanticBackendError
from .policy import cap_columnar


def post_graphql(
    *,
    url: str,
    token: str,
    timeout: float,
    query: str,
    allowed_hosts: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Execute one GraphQL document and return its data block.

    The URL is checked before the Authorization header exists, whoever called:
    the token goes to a dbt Cloud host's GraphQL endpoint over HTTPS or nowhere.
    """

    import httpx

    _check_graphql_url(httpx.URL(url), allowed_hosts)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"query": query},
            )
    except httpx.HTTPError as exc:
        raise SemanticBackendError(
            f"could not reach the dbt Cloud Semantic Layer: {env.redact(str(exc))}"
        ) from exc
    if response.status_code in {401, 403}:
        raise SemanticBackendError(
            "dbt Cloud Semantic Layer rejected the token (HTTP "
            f"{response.status_code}); check DBT_SL_TOKEN is a current "
            "'Semantic Layer Only' service token for this environment"
        )
    if response.status_code != 200:
        raise SemanticBackendError(
            f"dbt Cloud Semantic Layer returned HTTP {response.status_code}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise SemanticBackendError(
            "dbt Cloud Semantic Layer returned a non-JSON response"
        ) from exc
    if body.get("errors"):
        joined = "; ".join(str(item.get("message", item)) for item in body["errors"])
        # dbt Cloud relays the warehouse's words, which can quote a value (#1070).
        raise SemanticBackendError(
            f"semantic layer error: {env.redact(mask_values(joined))}"
        )
    return body.get("data") or {}


def _check_graphql_url(url: Any, allowed_hosts: tuple[str, ...]) -> None:
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port is not None
        or url.path != "/api/graphql"
        or url.query
        or url.fragment
    ):
        raise SemanticBackendError(
            "refusing to send the dbt Cloud Semantic Layer token, because the "
            "request URL is not https://<dbt Cloud host>/api/graphql"
        )
    try:
        check_semantic_layer_host(url.host, extra_hosts=allowed_hosts)
    except SemanticHostRefusedError as exc:
        raise SemanticBackendError(str(exc)) from exc


def await_result(
    post: Callable[[str], dict[str, Any]],
    *,
    environment_id: str,
    query_id: str,
    attempts: int,
    interval: float,
) -> Any:
    """Poll one submitted semantic query until it completes or times out."""

    for _ in range(attempts):
        query = (
            f"{{ query(environmentId: {environment_id}, queryId: "
            f"{json.dumps(query_id)}) {{ status error jsonResult(encoded: false) }} }}"
        )
        result = post(query).get("query") or {}
        status = result.get("status")
        if status == "SUCCESSFUL":
            return result.get("jsonResult")
        if status == "FAILED":
            raise SemanticBackendError(
                "semantic layer query failed: "
                f"{env.redact(mask_values(str(result.get('error'))))}"
            )
        time.sleep(interval)
    raise SemanticBackendError(f"timed out waiting for semantic layer query {query_id}")


def shape_json_result(
    json_result: Any,
    *,
    limits: QueryLimits,
    extra_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Decode pandas table JSON and apply dex's columnar response limits."""

    try:
        payload = (
            json.loads(json_result) if isinstance(json_result, str) else json_result
        ) or {}
    except (TypeError, ValueError) as exc:
        raise SemanticBackendError(
            "semantic layer returned an invalid tabular JSON result"
        ) from exc
    fields = (payload.get("schema") or {}).get("fields") or []
    columns = [field.get("name") for field in fields if field.get("name") != "index"]
    types = [field.get("type") for field in fields if field.get("name") != "index"]
    rows = payload.get("data") or []
    cells = [[row.get(column) for column in columns] for row in rows]
    return cap_columnar(
        columns,
        types,
        cells,
        max_rows=limits.max_rows,
        max_cell_chars=limits.max_cell_chars,
        max_payload_bytes=limits.max_payload_bytes,
        extra_notes=extra_notes,
    )
