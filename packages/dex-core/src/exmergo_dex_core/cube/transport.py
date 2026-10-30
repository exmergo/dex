"""The Cube REST API, as far as dex speaks it: ``/v1/meta`` and ``/v1/sql``.

Cube renders and dex executes, so dex asks a Cube server two things and nothing
else: what model it is serving (``/v1/meta``, for the stale-renderer check) and
what SQL a query would run (``/v1/sql``, which plans without executing). The
paths are a closed set, :data:`CUBE_PATHS`, so ``/v1/load`` (which executes in
the Cube server, with the Cube server's warehouse credentials) is not reachable
from here at all. Dex never hands Cube a warehouse credential either: the
server is a renderer, and the statement it renders reaches the warehouse only
through dex's own connector.

**The host is bound before the secret is read**
(:func:`~...semantic_hosts.check_cube_url`): a loopback Cube server by
default, anything else only by exact name from ``DEX_CUBE_ALLOWED_HOSTS``,
which the repository cannot write.

**The token is minted per request and lives a minute.** Cube's default
``checkAuth`` verifies an HS256 JWT signed with ``CUBEJS_API_SECRET``, read here
from the environment and nowhere else. The token carries a ``dex_scopes`` claim
naming the two endpoints dex calls. Cube enforces scopes through the
deployment's own ``contextToApiScopes``, which grants every scope by default, so
the claim binds only on a server configured to read it; what binds everywhere
is that dex calls nothing else, and that a captured token expires quickly.
Neither the secret nor a token is ever stored on a result, and both are
scrubbed from any error text before it leaves this module.

Signed with the standard library (``hmac``), so the only dependency is the HTTP
client, ``httpx``, which ships in the ``[semantic-api]`` extra.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from .. import envelope as env
from ..errors import DexError
from ..guards.dialect import DialectDependencyError
from ..semantic_hosts import (
    SemanticHostRefusedError,
    check_cube_url,
    env_cube_allowed_hosts,
)

#: The only paths dex requests, relative to the API base.
CUBE_PATHS = frozenset({"/v1/meta", "/v1/sql"})

#: Where the signing secret is read from. Never from configuration.
SECRET_ENV = "CUBEJS_API_SECRET"  # noqa: S105  (a variable name, not a secret)

#: The API base a ``deployment: local`` Cube Core answers on when
#: ``semantic.cube.url`` is not set: Cube's own default port.
DEFAULT_URL = "http://localhost:4000/cubejs-api"

#: How long a minted token is valid, in seconds.
TOKEN_SECONDS = 60

#: The scopes the token claims, one per endpoint dex calls.
TOKEN_SCOPES = ("meta", "sql")

# Cube answers "Continue wait" while it is still compiling or planning; a render
# never touches the warehouse, so a short bounded wait is the whole budget.
_WAIT_ATTEMPTS = 10
_WAIT_SECONDS = 0.5

_INSTALL = 'pip install "exmergo-dex-core[cube,semantic-api]"'


class CubeTransportDependencyError(DialectDependencyError):
    """The HTTP client a Cube render needs (``httpx``) is not installed."""


class CubeTransportError(DexError):
    """Cube could not be reached, refused the token, or answered with no plan.

    ``resolution`` is True when the server understood the request and could not
    plan it (a member it does not have, an invalid filter), which is the query's
    fault rather than the connection's.
    """

    def __init__(self, message: str, *, resolution: bool = False) -> None:
        super().__init__(message)
        self.resolution = resolution


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_token(secret: str, *, now: float | None = None) -> str:
    """An HS256 JWT Cube's default ``checkAuth`` accepts, valid for a minute."""

    issued = int(time.time() if now is None else now)
    header = _b64(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    claims = {
        "iat": issued,
        "exp": issued + TOKEN_SECONDS,
        "dex_scopes": list(TOKEN_SCOPES),
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(
        secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{_b64(signature)}"


class CubeTransport:
    """One Cube server's ``/v1/meta`` and ``/v1/sql``, under dex's rules.

    ``client`` is an ``httpx.Client`` to send through, for a test that serves
    the API from a ``MockTransport``; absent, one is opened per request.
    """

    def __init__(
        self,
        url: str | None,
        *,
        env_vars: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        environ = os.environ if env_vars is None else env_vars
        base = url or DEFAULT_URL
        try:
            self.base_url = check_cube_url(
                base, extra_hosts=env_cube_allowed_hosts(environ)
            )
        except SemanticHostRefusedError as exc:
            raise CubeTransportError(str(exc)) from exc
        self._environ = environ
        self._timeout = timeout
        self._client = client

    # ---- the two calls -----------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """``/v1/meta?extended=true``: every cube and view the server compiled."""

        body = self._request("GET", "/v1/meta", params={"extended": "true"})
        if not isinstance(body, dict) or not isinstance(body.get("cubes"), list):
            raise CubeTransportError("the Cube server's /v1/meta answer has no cubes")
        return body

    def sql(self, query: Mapping[str, Any]) -> Any:
        """``/v1/sql``: the plan Cube would run for ``query``, never executed."""

        return self._request("POST", "/v1/sql", json={"query": dict(query)})

    # ---- one request -------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path not in CUBE_PATHS:  # a programming error, refused all the same
            raise CubeTransportError(f"dex does not call the Cube API at {path}")
        try:
            import httpx
        except ImportError as exc:
            raise CubeTransportDependencyError(
                "a governed Cube query asks a Cube server to render its SQL over "
                "HTTP, and this install has no HTTP client. Install the extras that "
                f"carry it:\n  {_INSTALL}"
            ) from exc

        url = f"{self.base_url}{path}"
        for _ in range(_WAIT_ATTEMPTS):
            # Read here, after the host was bound in the constructor, and minted
            # per request, so no token outlives the call that needed it.
            secret = self._environ.get(SECRET_ENV) or ""
            if not secret:
                raise CubeTransportError(
                    f"a governed Cube query signs its requests with {SECRET_ENV}, "
                    "which is not set. Export the Cube server's API secret in your "
                    "own environment; dex never reads it from configuration"
                )
            token = mint_token(secret)
            try:
                response = self._send(httpx, method, url, token, kwargs)
            except httpx.HTTPError as exc:
                raise CubeTransportError(
                    f"could not reach the Cube server at {self.base_url}: "
                    f"{self._scrub(str(exc), secret, token)}"
                ) from exc
            status = response.status_code
            if status in {401, 403}:
                raise CubeTransportError(
                    f"the Cube server at {self.base_url} rejected dex's token (HTTP "
                    f"{status}); check that {SECRET_ENV} is the server's API secret"
                )
            try:
                body = response.json()
            except ValueError:
                raise CubeTransportError(
                    f"the Cube server at {self.base_url} answered {path} with HTTP "
                    f"{status} and a body that is not JSON"
                ) from None
            error = body.get("error") if isinstance(body, dict) else None
            if error == "Continue wait":
                time.sleep(_WAIT_SECONDS)
                continue
            if error is not None or status != 200:
                detail = self._scrub(str(error or f"HTTP {status}"), secret, token)
                raise CubeTransportError(detail, resolution=400 <= status < 500)
            return body
        raise CubeTransportError(
            f"the Cube server at {self.base_url} was still compiling after "
            f"{_WAIT_ATTEMPTS} attempts; retry once it has started"
        )

    def _send(self, httpx: Any, method: str, url: str, token: str, kwargs: dict) -> Any:
        headers = {"Authorization": token, "Content-Type": "application/json"}
        if self._client is not None:
            return self._client.request(method, url, headers=headers, **kwargs)
        with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
            return client.request(method, url, headers=headers, **kwargs)

    @staticmethod
    def _scrub(text: str, secret: str, token: str) -> str:
        for value in (token, secret):
            if value:
                text = text.replace(value, "***")
        return env.redact(text)[:500]

    def __repr__(self) -> str:  # never the secret, which is not held anyway
        return f"CubeTransport({self.base_url!r})"
