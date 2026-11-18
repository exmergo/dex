"""The Cube REST API, as far as dex speaks it: ``/v1/meta``, ``/v1/sql``, ``/v1/load``.

Two transports share this module, and each can reach only its own paths:

- **render** (``execution: dex``): what model a Cube server serves
  (``/v1/meta``, for the stale-renderer check) and what SQL a query would run
  (``/v1/sql``, which plans without executing). Dex executes that SQL through
  its own connector, so a render transport cannot reach ``/v1/load`` at all
  (:data:`RENDER_PATHS`), and the Cube server is a renderer that never needs a
  warehouse credential.
- **load** (``execution: vendor``, ``--api``, #914): ``/v1/meta`` and
  ``/v1/load``, which executes in the Cube server under the Cube server's own
  warehouse credentials (:data:`LOAD_PATHS`). No cost guard can apply there, and
  the layer that uses it says so on every result.

**The host is bound before any credential is read**
(:func:`~...semantic_hosts.check_cube_url`): a loopback Cube server by default,
a Cube Cloud host under ``deployment: cube_cloud``, and anything else only by
exact name from ``DEX_CUBE_ALLOWED_HOSTS``, which the repository cannot write.

**Where the token comes from depends on the deployment.** A Cube Core
(``deployment: local``) verifies an HS256 JWT signed with ``CUBEJS_API_SECRET``,
so dex mints one per request, valid for a minute, carrying a ``dex_scopes``
claim naming the endpoints that transport calls. Cube enforces scopes through
the deployment's own ``contextToApiScopes`` (``scripts/cube/cube.js`` reads the
claim), which grants every scope by default, so the claim binds only on a server
configured to read it; what binds everywhere is that a transport calls nothing
else, and that a captured token expires quickly. Cube Cloud
(``deployment: cube_cloud``) signs its own tokens, so dex sends the one the user
gives it: ``CUBE_API_TOKEN`` from the environment, or a host-supplied
:class:`~...connect.SemanticSource`. Each source is refused under the other
deployment, since a credential dex accepted and did not use would read as one
that took effect. Neither the secret nor a token is ever stored on a result, and
both are scrubbed from any error text before it leaves this module, as are the
values a warehouse error relayed by ``/v1/load`` can quote.

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
from collections.abc import Callable, Mapping
from typing import Any

from .. import envelope as env
from ..errors import DexError
from ..guards.dialect import DialectDependencyError
from ..semantic_hosts import (
    CUBE_CLOUD_DEPLOYMENT,
    SemanticHostRefusedError,
    check_cube_url,
    env_cube_allowed_hosts,
)
from ..value_mask import mask_values

#: The paths a render transport (dex executes) requests, relative to the API base.
RENDER_PATHS = frozenset({"/v1/meta", "/v1/sql"})

#: The paths a load transport (Cube executes, ``--api``) requests.
LOAD_PATHS = frozenset({"/v1/meta", "/v1/load"})

#: The released name of :data:`RENDER_PATHS`.
CUBE_PATHS = RENDER_PATHS

#: Where a Cube Cloud token is read from under ``deployment: cube_cloud``.
TOKEN_ENV = "CUBE_API_TOKEN"  # noqa: S105  (a variable name, not a secret)

#: The environment variable that beats ``semantic.cube.url`` at run time.
URL_ENV = "DEX_CUBE_URL"

#: Where the signing secret is read from. Never from configuration.
SECRET_ENV = "CUBEJS_API_SECRET"  # noqa: S105  (a variable name, not a secret)

#: The API base a ``deployment: local`` Cube Core answers on when
#: ``semantic.cube.url`` is not set: Cube's own default port.
DEFAULT_URL = "http://localhost:4000/cubejs-api"

#: How long a minted token is valid, in seconds.
TOKEN_SECONDS = 60

#: The scopes a render token claims, one per endpoint a render transport calls.
TOKEN_SCOPES = ("meta", "sql")

#: The scopes a load token claims (``--api`` on a Cube Core). Cube names the
#: scope ``/v1/load`` needs ``data``, not after the endpoint.
LOAD_SCOPES = ("meta", "data")

# Cube answers "Continue wait" while it is still compiling or planning; a render
# never touches the warehouse, so a short bounded wait is the whole budget.
_WAIT_ATTEMPTS = 10
_WAIT_SECONDS = 0.5

# A load runs the query in the warehouse, and Cube answers "Continue wait" until
# it has the rows, so its budget is the hosted dbt Cloud backend's: a bounded
# minute and a half rather than a render's few seconds.
_LOAD_ATTEMPTS = 90
_LOAD_SECONDS = 1.0

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


def mint_token(
    secret: str, *, now: float | None = None, scopes: tuple[str, ...] = TOKEN_SCOPES
) -> str:
    """An HS256 JWT Cube's default ``checkAuth`` accepts, valid for a minute."""

    issued = int(time.time() if now is None else now)
    header = _b64(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    claims = {
        "iat": issued,
        "exp": issued + TOKEN_SECONDS,
        "dex_scopes": list(scopes),
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(
        secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{_b64(signature)}"


class CubeTransport:
    """One Cube server's API under dex's rules: a render transport by default,
    a load transport with ``execution="vendor"``.

    ``deployment`` decides the token: minted from ``CUBEJS_API_SECRET`` under
    ``local``, the user's own under ``cube_cloud`` (``token``, a host-supplied
    callable, else ``CUBE_API_TOKEN``). A supplied ``token`` reads nothing
    ambient: not ``DEX_CUBE_URL``, not ``DEX_CUBE_ALLOWED_HOSTS``, which is the
    rule the hosted dbt Cloud resolver follows, so one process-wide variable
    cannot redirect a request a host has already pointed somewhere.

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
        deployment: str = "local",
        execution: str = "dex",
        token: Callable[[], str] | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        environ = os.environ if env_vars is None else env_vars
        cloud = deployment == CUBE_CLOUD_DEPLOYMENT
        if token is not None and not cloud:
            raise CubeTransportError(
                "a semantic source supplies a Cube Cloud token, and "
                "semantic.deployment is local: a Cube Core verifies tokens dex "
                f"mints from {SECRET_ENV}. Set semantic.deployment: cube_cloud, "
                "or drop the source"
            )
        ambient = token is None
        base = (environ.get(URL_ENV) if ambient else None) or url
        if not base and cloud:
            raise CubeTransportError(
                "semantic.deployment: cube_cloud needs the deployment's REST API "
                "base: set semantic.cube.url (or export DEX_CUBE_URL) to the "
                "deployment's URL plus /cubejs-api, such as "
                "https://<deployment>.<region>.cubecloudapp.dev/cubejs-api"
            )
        try:
            self.base_url = check_cube_url(
                base or DEFAULT_URL,
                extra_hosts=(
                    env_cube_allowed_hosts(environ) if ambient else tuple(allowed_hosts)
                ),
                deployment=deployment,
            )
        except SemanticHostRefusedError as exc:
            raise CubeTransportError(str(exc)) from exc
        if not cloud and environ.get(TOKEN_ENV):
            raise CubeTransportError(
                f"{TOKEN_ENV} is set and semantic.deployment is local: a Cube Core "
                f"verifies tokens dex mints from {SECRET_ENV}, so that token would "
                "go unused. Set semantic.deployment: cube_cloud to use it, or "
                f"unset {TOKEN_ENV}"
            )
        self.deployment = deployment
        self.execution = execution
        self.paths = LOAD_PATHS if execution == "vendor" else RENDER_PATHS
        self._scopes = LOAD_SCOPES if execution == "vendor" else TOKEN_SCOPES
        self._cloud = cloud
        self._supplied = token
        self._static: str | None = None
        self._environ = environ
        self._timeout = timeout
        self._client = client

    # ---- the calls ---------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """``/v1/meta?extended=true``: every cube and view the server compiled."""

        body = self._request("GET", "/v1/meta", params={"extended": "true"})
        if not isinstance(body, dict) or not isinstance(body.get("cubes"), list):
            raise CubeTransportError("the Cube server's /v1/meta answer has no cubes")
        return body

    def sql(self, query: Mapping[str, Any]) -> Any:
        """``/v1/sql``: the plan Cube would run for ``query``, never executed."""

        return self._request("POST", "/v1/sql", json={"query": dict(query)})

    def load(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """``/v1/load``: Cube executes ``query`` and returns its rows."""

        body = self._request("POST", "/v1/load", json={"query": dict(query)})
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise CubeTransportError(
                "the Cube server's /v1/load answer carries no rows"
            )
        return body

    # ---- the token ---------------------------------------------------------------

    def _token(self) -> tuple[str, str]:
        """``(token, secret)``: what goes in the header, and what else to scrub.

        Read here, after the host was bound in the constructor. A Core's token is
        minted per request so none outlives the call that needed it; a Cloud
        token is read once per transport, which is one command.
        """

        if not self._cloud:
            secret = self._environ.get(SECRET_ENV) or ""
            if not secret:
                raise CubeTransportError(
                    f"a Cube query signs its requests with {SECRET_ENV}, which is "
                    "not set. Export the Cube server's API secret in your own "
                    "environment; dex never reads it from configuration"
                )
            return mint_token(secret, scopes=self._scopes), secret
        if self._static is None:
            if self._supplied is not None:
                value = self._supplied()
                missing = (
                    "the supplied semantic source returned no token; its `token` "
                    "callable must return a Cube Cloud API token"
                )
            else:
                value = self._environ.get(TOKEN_ENV) or ""
                missing = (
                    f"semantic.deployment: cube_cloud authenticates with "
                    f"{TOKEN_ENV}, which is not set. Export a Cube Cloud API token "
                    "for the deployment (Cube Cloud's API tokens, or `cube "
                    "deployments token`) in your own environment; dex never reads "
                    "it from configuration"
                )
            if not value:
                raise CubeTransportError(missing)
            self._static = str(value)
        return self._static, ""

    # ---- one request -------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path not in self.paths:  # a programming error, refused all the same
            raise CubeTransportError(f"dex does not call the Cube API at {path} here")
        try:
            import httpx
        except ImportError as exc:
            raise CubeTransportDependencyError(
                "a Cube query asks a Cube server over HTTP, and this install has "
                "no HTTP client. Install the extras that carry it:\n  "
                f"{_INSTALL}"
            ) from exc

        loading = path == "/v1/load"
        attempts = _LOAD_ATTEMPTS if loading else _WAIT_ATTEMPTS
        pause = _LOAD_SECONDS if loading else _WAIT_SECONDS
        url = f"{self.base_url}{path}"
        for _ in range(attempts):
            token, secret = self._token()
            try:
                response = self._send(httpx, method, url, token, kwargs)
            except httpx.HTTPError as exc:
                raise CubeTransportError(
                    f"could not reach the Cube server at {self.base_url}: "
                    f"{self._scrub(str(exc), secret, token)}"
                ) from exc
            status = response.status_code
            if status in {401, 403}:
                check = (
                    f"check that {TOKEN_ENV} is a current API token for this deployment"
                    if self._cloud
                    else f"check that {SECRET_ENV} is the server's API secret"
                )
                raise CubeTransportError(
                    f"the Cube server at {self.base_url} rejected dex's token (HTTP "
                    f"{status}) for {path}; {check}"
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
                time.sleep(pause)
                continue
            if error is not None or status != 200:
                detail = self._scrub(
                    str(error or f"HTTP {status}"), secret, token, values=loading
                )
                raise CubeTransportError(detail, resolution=400 <= status < 500)
            return body
        what = "still running the query" if loading else "still compiling"
        raise CubeTransportError(
            f"the Cube server at {self.base_url} was {what} after {attempts} "
            "attempts; retry once it has finished"
        )

    def _send(self, httpx: Any, method: str, url: str, token: str, kwargs: dict) -> Any:
        headers = {"Authorization": token, "Content-Type": "application/json"}
        if self._client is not None:
            return self._client.request(method, url, headers=headers, **kwargs)
        with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
            return client.request(method, url, headers=headers, **kwargs)

    @staticmethod
    def _scrub(text: str, secret: str, token: str, *, values: bool = False) -> str:
        for value in (token, secret):
            if value:
                text = text.replace(value, "***")
        # A load runs in the warehouse, whose errors can quote a value (a cast
        # that failed on one); a render never reads a row.
        if values:
            text = mask_values(text)
        return env.redact(text)[:500]

    def __repr__(self) -> str:  # never a credential, which is not held here
        return f"CubeTransport({self.base_url!r}, {self.execution!r})"
