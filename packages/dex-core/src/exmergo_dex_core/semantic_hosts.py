"""Which hosts may receive a semantic layer's token: dbt Cloud's, and a Cube server's.

The host and the token come from different places. The token is the user's,
from ``DBT_SL_TOKEN``, ``~/.dbt/dbt_cloud.yml``, or a host-supplied
:class:`~.connect.SemanticSource`. The host may come from ``.dex/config.yml``,
which is committed to a repository the user may not have written: a clone, a
fork, a pull request an agent is reviewing. Sending one to the other unchecked
hands the user's token to whoever wrote the config, so the host is bound to dbt
Cloud's own domains before any token is read, and again before any request is
sent.

A private-link or single-tenant host outside those domains is admitted only by
exact name, and only from somewhere the repository cannot write: the
``DEX_SL_ALLOWED_HOSTS`` environment variable, or ``SemanticSource.allowed_hosts``
from an embedding host. Never from ``.dex/config.yml``.

A Cube server is bound the same way for the same reason (:func:`check_cube_url`):
the JWT dex mints from ``CUBEJS_API_SECRET`` goes to a server on this machine, or
to a host named exactly in ``DEX_CUBE_ALLOWED_HOSTS`` over https.

Imports nothing from the package but the error base, so the resolver, the
backend, and the transport can all hold the same rule without a cycle.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

from .errors import DexError

#: dbt Cloud's domains. A host matches one when it *is* that domain or ends in
#: ``.`` plus it, so ``sl.cloud.getdbt.com`` and the documented
#: ``<account>.semantic-layer.<region>.dbt.com`` pass while ``evildbt.com`` and
#: ``getdbt.com.evil.example`` do not.
DBT_CLOUD_DOMAINS: tuple[str, ...] = ("getdbt.com", "dbt.com")

#: Exact extra hosts, comma-separated. Read from the environment only, which the
#: user controls and the repository does not (dex loads no ``.env`` file).
ALLOWED_HOSTS_ENV = "DEX_SL_ALLOWED_HOSTS"

# The host is interpolated into ``https://{host}/api/graphql``, so anything that
# would change what that URL means (a path, a query, userinfo, a port) is refused
# rather than parsed around. Letters, digits, hyphens and dots are all a DNS name
# needs.
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")


class SemanticHostRefusedError(DexError, ValueError):
    """A host the token may not be sent to. The message names the host and the
    fix, and never the token, which has not been read."""


def normalize_host(host: str) -> str:
    """Lowercased, outer whitespace and a trailing dot removed."""

    return host.strip().lower().rstrip(".")


def env_allowed_hosts(env: Mapping[str, str] | os._Environ) -> tuple[str, ...]:
    """The exact hosts ``DEX_SL_ALLOWED_HOSTS`` names, normalized."""

    raw = env.get(ALLOWED_HOSTS_ENV) or ""
    return tuple(h for h in (normalize_host(part) for part in raw.split(",")) if h)


def check_semantic_layer_host(host: object, *, extra_hosts: Iterable[str] = ()) -> str:
    """Return ``host`` normalized if the token may be sent to it; else raise.

    Called before any token is read, so a refusal never costs the user a token
    read, let alone a token sent.
    """

    shown = str(host)[:200]
    normalized = normalize_host(str(host or ""))
    if not normalized or not _HOSTNAME.match(normalized):
        raise SemanticHostRefusedError(
            f"semantic.host {shown!r} is not a bare hostname. The hosted semantic "
            "layer host must be a name such as sl.cloud.getdbt.com, with no "
            "scheme, path, port or credentials"
        )
    if any(normalized == d or normalized.endswith("." + d) for d in DBT_CLOUD_DOMAINS):
        return normalized
    if normalized in {normalize_host(h) for h in extra_hosts}:
        return normalized
    raise SemanticHostRefusedError(
        f"refusing to send the dbt Cloud Semantic Layer token to {normalized!r}: it "
        "is not a dbt Cloud host (*.getdbt.com or *.dbt.com). The host can come "
        "from .dex/config.yml, which the repository controls, and the token is "
        "yours, so it is only sent to dbt Cloud. For a private-link or "
        f"single-tenant host, name it exactly in {ALLOWED_HOSTS_ENV} in your own "
        "environment; .dex/config.yml cannot widen this list"
    )


# --- Cube --------------------------------------------------------------------------

#: Exact extra Cube hosts, comma-separated. Environment only, for the reason
#: ``DEX_SL_ALLOWED_HOSTS`` is: ``semantic.cube.url`` is committed, and the
#: token minted from ``CUBEJS_API_SECRET`` is the user's.
CUBE_ALLOWED_HOSTS_ENV = "DEX_CUBE_ALLOWED_HOSTS"

#: The hosts a ``deployment: local`` Cube server is admitted on without asking:
#: the machine dex runs on, and nothing a repository could point elsewhere.
LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")


def env_cube_allowed_hosts(env: Mapping[str, str] | os._Environ) -> tuple[str, ...]:
    """The exact hosts ``DEX_CUBE_ALLOWED_HOSTS`` names, normalized."""

    raw = env.get(CUBE_ALLOWED_HOSTS_ENV) or ""
    return tuple(h for h in (normalize_host(part) for part in raw.split(",")) if h)


def check_cube_url(url: object, *, extra_hosts: Iterable[str] = ()) -> str:
    """Return the Cube API base ``url`` if a token may be sent to it; else raise.

    Called before ``CUBEJS_API_SECRET`` is read. A loopback host is admitted,
    over http or https, because a local Cube Core is a process on this machine.
    Any other host needs an exact entry in ``DEX_CUBE_ALLOWED_HOSTS`` and https,
    since the token would otherwise cross a network in the clear. A URL carrying
    userinfo, a query or a fragment is refused rather than parsed around: the
    request paths are appended to it, and those would change what they mean.
    """

    from urllib.parse import urlsplit

    shown = str(url)[:200]
    try:
        parts = urlsplit(str(url or ""))
        parts.port  # noqa: B018  (raises on a malformed port)
    except ValueError as exc:
        raise SemanticHostRefusedError(
            f"semantic.cube.url {shown!r} is not a URL: {exc}"
        ) from exc
    host = normalize_host(parts.hostname or "")
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise SemanticHostRefusedError(
            f"semantic.cube.url {shown!r} is not a plain Cube API base such as "
            "http://localhost:4000/cubejs-api: a scheme, a host, an optional port "
            "and path, and no credentials, query or fragment"
        )
    if host in LOOPBACK_HOSTS:
        return str(url).rstrip("/")
    if host not in {normalize_host(h) for h in extra_hosts}:
        raise SemanticHostRefusedError(
            f"refusing to send a Cube API token to {host!r}: only a Cube server on "
            "this machine (localhost, 127.0.0.1, ::1) is admitted by default. The "
            "URL can come from .dex/config.yml, which the repository controls, and "
            "the token is minted from your CUBEJS_API_SECRET. To use a Cube server "
            f"elsewhere, name its host exactly in {CUBE_ALLOWED_HOSTS_ENV} in your "
            "own environment; .dex/config.yml cannot widen this list"
        )
    if parts.scheme != "https":
        raise SemanticHostRefusedError(
            f"refusing to send a Cube API token to {host!r} over http: a host other "
            "than this machine is reached over https only"
        )
    return str(url).rstrip("/")
