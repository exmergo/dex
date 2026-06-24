"""The stdout envelope: the only thing that crosses the engine/agent boundary.

Every ``dex`` subcommand prints exactly one JSON object matching :class:`Envelope`
to stdout and nothing else (logs and diagnostics go to stderr). The agent reads
this envelope and decides the next step. Credentials and raw warehouse rows never
appear here: :func:`emit` runs the payload through :func:`sanitize` first, and a
leak is a release-blocking safety regression (see references/command-contract.md).

The envelope shape is itself a Tier-2 eval target, so it is defined once, here,
and reused by every command rather than hand-built per subcommand.
"""

from __future__ import annotations

import json
import re
import sys
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Status(str, Enum):
    OK = "ok"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"
    # A command that would spend money/scan data but was not confirmed. The agent
    # is expected to re-issue with --confirm and a budget after surfacing cost.
    NEEDS_CONFIRMATION = "needs_confirmation"


class Paradigm(str, Enum):
    """Cost paradigm of the active connector. DuckDB is free/local."""

    FREE_LOCAL = "free_local"
    BYTES_SCANNED = "bytes_scanned"
    COMPUTE_TIME = "compute_time"
    DB_LOAD = "db_load"


class Cost(BaseModel):
    """A preflight cost estimate, surfaced before any spend.

    ``estimate`` and ``ceiling`` are paradigm-relative magnitudes (bytes, credits,
    DBUs, or a load score); the unit is carried by ``paradigm``. For DuckDB both
    are ``None`` because the work is free and only resource-bounded.
    """

    paradigm: Paradigm = Paradigm.FREE_LOCAL
    estimate: float | None = None
    ceiling: float | None = None


class Envelope(BaseModel):
    """The single object every command prints to stdout."""

    status: Status
    data: dict[str, Any] = Field(default_factory=dict)
    cost: Cost = Field(default_factory=Cost)
    warnings: list[str] = Field(default_factory=list)
    # Reviewable diffs (propose-don't-impose). Nothing is applied just by being here.
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# Keys whose presence anywhere in ``data`` indicates a credential or secret has
# leaked into the boundary. Matched case-insensitively as a substring of the key.
_SECRET_KEY_PATTERNS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "privatekey",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "client_secret",
    "authorization",
    "session_token",
)

# A "raw row" payload is profiling's forbidden output: a list of records keyed by
# column name. Profiling must emit aggregates and flags, never row values. We flag
# any list of dicts living under a key that reads like row data.
_RAW_ROW_KEY_PATTERNS = ("rows", "records", "sample_rows", "raw", "preview_rows")


class SanitizationError(Exception):
    """Raised when an envelope payload would leak secrets or raw rows.

    This is intentionally a hard failure rather than a silent scrub: a leak is a
    bug in the calling command, and the safety tests assert it cannot ship.
    """


def _scan(value: Any, path: str = "data") -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            key_l = str(key).lower()
            if any(pat in key_l for pat in _SECRET_KEY_PATTERNS):
                raise SanitizationError(
                    f"secret-like key '{key}' at {path}: credentials never cross "
                    "the stdout boundary"
                )
            if any(pat in key_l for pat in _RAW_ROW_KEY_PATTERNS) and _looks_like_rows(sub):
                raise SanitizationError(
                    f"raw-row payload at {path}.{key}: profile-don't-exfiltrate; "
                    "emit aggregates and flags, not row values"
                )
            _scan(sub, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, sub in enumerate(value):
            _scan(sub, f"{path}[{i}]")


def _looks_like_rows(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, dict) for item in value)
    )


def sanitize(envelope: Envelope) -> Envelope:
    """Assert the envelope is safe to print. Returns it unchanged on success.

    Raises :class:`SanitizationError` if ``data`` carries a secret-like key or a
    raw-row payload. Only ``data`` is scanned: ``errors``/``warnings`` are message
    strings that the caller is responsible for keeping clean.
    """

    _scan(envelope.data)
    return envelope


def emit(envelope: Envelope) -> None:
    """Sanitize, then print exactly one JSON envelope to stdout."""

    sanitize(envelope)
    sys.stdout.write(json.dumps(envelope.model_dump(mode="json")) + "\n")


def ok(data: dict[str, Any] | None = None, **kwargs: Any) -> Envelope:
    return Envelope(status=Status.OK, data=data or {}, **kwargs)


def not_implemented(command: str) -> Envelope:
    return Envelope(
        status=Status.NOT_IMPLEMENTED,
        data={"command": command},
        warnings=[f"'{command}' is scaffolded but not yet implemented"],
    )


def error(message: str, **kwargs: Any) -> Envelope:
    return Envelope(status=Status.ERROR, errors=[message], **kwargs)


# Re-exported for callers that build messages and want to avoid leaking secrets
# into error/warning strings (e.g. a DSN that embeds a password).
def redact(text: str) -> str:
    """Best-effort redaction of secret-looking tokens in a free-text message."""

    text = re.sub(r"(://[^:/\s]+:)([^@/\s]+)(@)", r"\1***\3", text)  # user:pass@host
    return text
