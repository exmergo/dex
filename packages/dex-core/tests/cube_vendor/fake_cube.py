"""A Cube server that answers from recordings, behind ``httpx.MockTransport``.

``/v1/sql`` answers with what Cube Core v1.8.14 actually rendered for the same
query JSON (``recorded/<connector>.json``, made by ``record_cube.py``), so the
unit tests run dex against Cube's real output with no Cube process. A query
nothing recorded is answered the way Cube answers a query it cannot plan.

``/v1/meta`` answers with the recorded meta of ``scripts/cube/model``, or with
one derived from another model in Cube's own shape (:func:`meta_for`), so a test
can serve a model that differs from the repository's on purpose.

Every request is checked the way Cube's default ``checkAuth`` checks it: an
HS256 JWT signed with the server's secret and not yet expired, else 403.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from .cube_cases import RECORDED

SECRET = "fake-cube-secret-0123456789abcdef"  # noqa: S105 (a test fixture)


def recorded(connector: str) -> dict[str, dict[str, Any]]:
    return json.loads((RECORDED / f"{connector}.json").read_text(encoding="utf-8"))


def recorded_meta() -> dict[str, Any]:
    return json.loads((RECORDED / "meta.json").read_text(encoding="utf-8"))


def _canonical(query: Any) -> str:
    return json.dumps(query, sort_keys=True)


def _verify(token: str, secret: str) -> bool:
    token = token.removeprefix("Bearer ").strip()
    try:
        header, payload, signature = token.split(".")
        expected = hmac.new(
            secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
        ).digest()
        given = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except ValueError:
        return False
    return hmac.compare_digest(expected, given) and claims.get("exp", 0) > time.time()


class FakeCube:
    """One Cube server. ``responses`` overrides ``/v1/sql`` for a case name."""

    def __init__(
        self,
        connector: str = "duckdb",
        *,
        meta: dict[str, Any] | None = None,
        secret: str = SECRET,
    ) -> None:
        self.recordings = recorded(connector)
        self.meta = meta if meta is not None else recorded_meta()
        self.secret = secret
        self.responses: dict[str, Any] = {}
        self.render: Callable[[dict[str, Any]], Any] | None = None
        self.calls: list[httpx.Request] = []
        self.status: int | None = None

    def case_for(self, query: dict[str, Any]) -> str | None:
        wanted = _canonical(query)
        for name, entry in self.recordings.items():
            if _canonical(entry["query"]) == wanted:
                return name
        return None

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status is not None:
            return httpx.Response(self.status, json={"error": "forced"})
        if not _verify(request.headers.get("Authorization", ""), self.secret):
            return httpx.Response(403, json={"error": "Invalid token"})
        path = request.url.path
        if path.endswith("/v1/meta"):
            return httpx.Response(200, json=self.meta)
        if path.endswith("/v1/sql"):
            query = json.loads(request.content)["query"]
            if self.render is not None:
                return httpx.Response(200, json=self.render(query))
            name = self.case_for(query)
            if name in self.responses:
                return httpx.Response(200, json=self.responses[name])
            if name is None:
                return httpx.Response(
                    400,
                    json={"type": "UserError", "error": f"unrecorded query {query}"},
                )
            return httpx.Response(200, json=self.recordings[name]["response"])
        return httpx.Response(404, json={"error": f"no route {path}"})

    def install(self, monkeypatch) -> FakeCube:
        """Route every ``httpx.Client`` through this server for the test."""

        original = httpx.Client
        transport = httpx.MockTransport(self.handler)

        def client(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client)
        monkeypatch.setenv("CUBEJS_API_SECRET", self.secret)
        return self


def _template(sql: str | None) -> str | None:
    """A member's SQL as Cube's YAML compiler hands it to ``/v1/meta``."""

    if sql is None:
        return None
    return "`" + sql.replace("{", "${") + "`"


def meta_for(loaded: Any) -> dict[str, Any]:
    """``/v1/meta?extended=true`` for a loaded model, in Cube's own shape."""

    def member(cube_name: str, m: Any, alias: str | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": f"{cube_name}.{m.name}", "public": True}
        if m.kind == "measure":
            entry.update(type="number", aggType=m.type)
            if m.filters:
                entry["filters"] = [{"sql": _template(f)} for f in m.filters]
        else:
            entry["type"] = m.type
            if m.granularities:
                entry["granularities"] = [{"name": g} for g in m.granularities]
        if alias:
            entry["aliasMember"] = alias
        elif m.sql is not None:
            entry["sql"] = _template(m.sql)
        return entry

    cubes: list[dict[str, Any]] = [
        {
            "name": cube.name,
            "type": "cube",
            "measures": [member(cube.name, m) for m in cube.measures],
            "dimensions": [
                member(cube.name, m) for m in cube.dimensions if not m.primary_key
            ],
        }
        for cube in loaded.cubes
    ]
    for view in loaded.views:
        measures, dimensions = [], []
        for exposed in view.members:
            row = member(view.name, exposed.member, alias=exposed.source)
            row["name"] = f"{view.name}.{exposed.name}"
            (measures if exposed.kind == "measure" else dimensions).append(row)
        cubes.append(
            {
                "name": view.name,
                "type": "view",
                "measures": measures,
                "dimensions": dimensions,
            }
        )
    return {"cubes": cubes}
