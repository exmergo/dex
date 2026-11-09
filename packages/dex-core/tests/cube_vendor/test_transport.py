"""The Cube REST client: two paths, a bound host, a minute-long token (#913)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

pytest.importorskip("httpx")

import httpx

from exmergo_dex_core.cube import transport as transport_mod
from exmergo_dex_core.cube.transport import (
    CUBE_PATHS,
    DEFAULT_URL,
    TOKEN_SECONDS,
    CubeTransport,
    CubeTransportError,
    mint_token,
)
from exmergo_dex_core.semantic_hosts import (
    SemanticHostRefusedError,
    check_cube_url,
)

SECRET = "s3cr3t-value-for-tests"  # noqa: S105 (a test fixture)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _transport(handler, url=None, **env):
    return CubeTransport(
        url, env_vars={"CUBEJS_API_SECRET": SECRET, **env}, client=_client(handler)
    )


def _ok_meta(request):
    return httpx.Response(200, json={"cubes": []})


# --- what dex calls --------------------------------------------------------------


def test_the_paths_are_meta_and_sql_and_nothing_else():
    assert frozenset({"/v1/meta", "/v1/sql"}) == CUBE_PATHS
    with pytest.raises(CubeTransportError, match="/v1/load"):
        _transport(_ok_meta)._request("GET", "/v1/load")


def test_meta_is_extended_and_sql_posts_the_query():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/v1/meta"):
            return httpx.Response(200, json={"cubes": []})
        return httpx.Response(200, json={"sql": {"sql": ["SELECT 1", []]}})

    t = _transport(handler)
    t.meta()
    t.sql({"measures": ["orders.count"]})

    assert str(seen[0].url) == f"{DEFAULT_URL}/v1/meta?extended=true"
    assert seen[1].method == "POST" and seen[1].url.path == "/cubejs-api/v1/sql"
    assert json.loads(seen[1].content) == {"query": {"measures": ["orders.count"]}}


def test_a_redirect_is_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    with pytest.raises(CubeTransportError, match="not JSON"):
        _transport(handler).meta()


# --- the token -------------------------------------------------------------------


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_the_token_is_an_hs256_jwt_cube_s_check_auth_accepts():
    token = mint_token(SECRET, now=1_000)
    header, payload, signature = token.split(".")
    expected = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256)

    assert json.loads(base64.urlsafe_b64decode(header + "==")) == {
        "alg": "HS256",
        "typ": "JWT",
    }
    assert (
        signature == base64.urlsafe_b64encode(expected.digest()).rstrip(b"=").decode()
    )
    assert _claims(token) == {
        "iat": 1_000,
        "exp": 1_000 + TOKEN_SECONDS,
        "dex_scopes": ["meta", "sql"],
    }


def test_a_token_is_minted_per_request():
    tokens: list[str] = []

    def handler(request):
        tokens.append(request.headers["Authorization"])
        return httpx.Response(200, json={"cubes": []})

    t = _transport(handler)
    t.meta()
    t.meta()
    assert len(tokens) == 2 and all(token.count(".") == 2 for token in tokens)


def test_no_secret_is_refused_before_any_request():
    seen: list[httpx.Request] = []
    t = CubeTransport(None, env_vars={}, client=_client(lambda r: seen.append(r)))

    with pytest.raises(CubeTransportError, match="CUBEJS_API_SECRET"):
        t.meta()
    assert seen == []


def test_a_rejected_token_names_the_secret_and_never_the_token():
    def handler(request):
        return httpx.Response(
            403, json={"error": f"bad {request.headers['Authorization']}"}
        )

    with pytest.raises(CubeTransportError) as caught:
        _transport(handler).meta()
    assert "CUBEJS_API_SECRET" in str(caught.value)
    assert "eyJ" not in str(caught.value) and SECRET not in str(caught.value)


def test_error_text_is_scrubbed_of_the_token_and_the_secret():
    def handler(request):
        token = request.headers["Authorization"]
        return httpx.Response(400, json={"error": f"echo {token} and {SECRET}"})

    with pytest.raises(CubeTransportError) as caught:
        _transport(handler).sql({})
    message = str(caught.value)
    assert caught.value.resolution is True
    assert "eyJ" not in message and SECRET not in message and "***" in message


def test_a_connection_failure_is_scrubbed_too():
    def handler(request):
        raise httpx.ConnectError(f"refused with {request.headers['Authorization']}")

    with pytest.raises(CubeTransportError) as caught:
        _transport(handler).meta()
    assert "could not reach the Cube server" in str(caught.value)
    assert "eyJ" not in str(caught.value)


def test_the_transport_holds_no_secret_in_its_repr():
    assert SECRET not in repr(_transport(_ok_meta))


# --- continue wait ---------------------------------------------------------------


def test_continue_wait_is_retried_a_bounded_number_of_times(monkeypatch):
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, json={"error": "Continue wait"})
        return httpx.Response(200, json={"cubes": []})

    assert _transport(handler).meta() == {"cubes": []}
    assert len(calls) == 3

    def forever(request):
        return httpx.Response(200, json={"error": "Continue wait"})

    with pytest.raises(CubeTransportError, match="still compiling"):
        _transport(forever).meta()


# --- the host --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:4000/cubejs-api",
        "http://127.0.0.1:4001/cubejs-api",
        "https://localhost/cubejs-api",
        "http://[::1]:4000/cubejs-api",
    ],
)
def test_a_loopback_server_is_admitted_over_http(url):
    assert check_cube_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://cube.example.com/cubejs-api",
        "https://localhost.example.com/cubejs-api",
        "https://127.0.0.1.example.com/cubejs-api",
    ],
)
def test_any_other_host_needs_the_environment(url):
    with pytest.raises(SemanticHostRefusedError, match="DEX_CUBE_ALLOWED_HOSTS"):
        check_cube_url(url)


def test_an_allowed_host_is_admitted_over_https_only():
    allowed = ["cube.example.com"]
    assert check_cube_url("https://cube.example.com/cubejs-api", extra_hosts=allowed)
    with pytest.raises(SemanticHostRefusedError, match="over http"):
        check_cube_url("http://cube.example.com/cubejs-api", extra_hosts=allowed)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@localhost:4000/cubejs-api",
        "http://localhost:4000/cubejs-api?x=1",
        "http://localhost:4000/cubejs-api#frag",
        "ftp://localhost/cubejs-api",
        "localhost:4000",
    ],
)
def test_a_url_that_is_not_a_plain_base_is_refused(url):
    with pytest.raises(SemanticHostRefusedError):
        check_cube_url(url)


def test_the_host_is_bound_before_the_secret_is_read():
    class Watched(dict):
        def get(self, key, default=None):
            if key == "CUBEJS_API_SECRET":
                raise AssertionError("the secret was read before the host was bound")
            return super().get(key, default)

    with pytest.raises(CubeTransportError, match=r"cube\.example\.com"):
        CubeTransport("https://cube.example.com/cubejs-api", env_vars=Watched())


def test_the_allowlist_comes_from_the_environment_mapping():
    t = CubeTransport(
        "https://cube.example.com/cubejs-api",
        env_vars={"DEX_CUBE_ALLOWED_HOSTS": " Cube.Example.com ,other.example"},
    )
    assert t.base_url == "https://cube.example.com/cubejs-api"
