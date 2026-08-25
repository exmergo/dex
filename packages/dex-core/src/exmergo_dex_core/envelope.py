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

from .errors import DexError


class Status(str, Enum):
    OK = "ok"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"
    # A command that would spend money/scan data but was not confirmed. The agent
    # is expected to re-issue with --confirm and a budget after surfacing cost.
    NEEDS_CONFIRMATION = "needs_confirmation"


class Paradigm(str, Enum):
    """Cost paradigm of the active connector. DuckDB is free/local.

    ``FREE_LOCAL`` is DuckDB's own paradigm and never a stand-in for "not
    applicable": it is a positive claim that the connector in play bills
    nothing. A command with no connector to describe reports no paradigm at all
    (``Cost.paradigm is None``) rather than borrowing this one.
    """

    FREE_LOCAL = "free_local"
    BYTES_SCANNED = "bytes_scanned"
    COMPUTE_TIME = "compute_time"
    DB_LOAD = "db_load"
    # The hosted dbt Cloud Semantic Layer executes queries server-side under its
    # own warehouse connection, so dex's cost guard is structurally unavailable:
    # no dry-run estimate and no ceiling. `estimate` and `ceiling` stay None and
    # the command warns that dbt Cloud, not dex, governs spend.
    HOSTED = "hosted"


class Cost(BaseModel):
    """A preflight cost estimate, surfaced before any spend.

    ``estimate`` and ``ceiling`` are paradigm-relative magnitudes (bytes, credits,
    DBUs, or a load score); the unit is carried by ``paradigm``. For DuckDB both
    are ``None`` because the work is free and only resource-bounded.

    ``paradigm`` names **the connector this command ran against**, so a caller
    reading a free metadata command still learns what a billed one will cost in.
    It defaults to ``None``, meaning no connector was resolved, and that default
    is load-bearing: a refusal built without a paradigm has to say nothing rather
    than claim ``free_local``, which is a positive assertion a host branching on
    this field would read as "this refusal was not about money".
    """

    paradigm: Paradigm | None = None
    estimate: float | None = None
    ceiling: float | None = None


class Reason(str, Enum):
    """Why an error envelope was refused, coarse enough for a host to branch
    on retry/setup/stop without parsing prose. Derived from the exception's
    class (see :func:`reason_for`), never invented per call site, so it stays
    consistent with every refusal's typed exception as new ones are added.
    Populated only when ``status`` is ``error``.
    """

    GUARD = "guard"  # a cost/PII/firewall/safety policy said no
    PREREQUISITE = "prerequisite"  # run a named setup command first, then retry
    CONNECTION = "connection"  # the warehouse/backend could not be reached
    CONFIGURATION = (
        "configuration"  # the engine/repo is wired without something it needs
    )
    REQUEST = "request"  # this call's input cannot be resolved or used
    EXECUTION_FAILURE = (
        "execution_failure"  # the operation ran (e.g. dbt build) and failed
    )
    INTERNAL = "internal"  # unclassified: not a deliberate dex refusal


class Envelope(BaseModel):
    """The single object every command prints to stdout."""

    status: Status
    data: dict[str, Any] = Field(default_factory=dict)
    cost: Cost = Field(default_factory=Cost)
    warnings: list[str] = Field(default_factory=list)
    # Reviewable diffs (propose-don't-impose). Nothing is applied just by being here.
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    reason: Reason | None = None


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


class SanitizationError(DexError):
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
            if any(pat in key_l for pat in _RAW_ROW_KEY_PATTERNS) and _looks_like_rows(
                sub
            ):
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


_reason_overrides_cache: list[tuple[type[BaseException], Reason]] | None = None


def _reason_overrides() -> list[tuple[type[BaseException], Reason]]:
    """Built on first use, not at import time: the exception classes it
    references live in modules that already import this one at their own top
    level (``explore/commands.py``, ``transform/commands.py``,
    ``transform/build.py``, ``connect.py``, ``guards/cost_guard.py``), so
    importing them here at module load would be circular. By the time any
    error envelope is built, every module is already loaded, so a call-time
    import (the same lazy-import pattern ``cli.py``'s own ``_run`` already
    uses for command modules) is safe. Cached after the first call so the
    import cost is paid once, not on every error envelope.

    Two tiers, not one: a handful of exception families live in modules with
    no optional-dependency imports of their own and are always available; the
    rest live behind connector extras (sqlglot, scikit-learn, the dbt reader)
    that a bare or partial install may not have -- and importing THOSE must
    not itself crash the very refusal (a missing extra) that names the gap.
    """

    global _reason_overrides_cache
    if _reason_overrides_cache is not None:
        return _reason_overrides_cache

    # Always safe: no imports of their own (`errors.py`) or explicitly
    # designed to import without the dialect engine (`guards/dialect.py` is
    # the module that checks whether sqlglot is even installed).
    from .demo.warehouse import (
        DemoDependencyError,
        DemoPathError,
        DemoTargetExistsError,
    )
    from .errors import (
        ConfigurationError,
        ConnectorError,
        DexError,
        PrerequisiteError,
        RequestError,
        WarehouseQueryError,
    )
    from .guards.dialect import DialectDependencyError

    overrides: list[tuple[type[BaseException], Reason]] = [
        (DemoTargetExistsError, Reason.GUARD),
        (DemoDependencyError, Reason.PREREQUISITE),
        (DemoPathError, Reason.REQUEST),
        (DialectDependencyError, Reason.PREREQUISITE),
        (PrerequisiteError, Reason.PREREQUISITE),  # CacheRequiredError, NoBaselineError
        (ConnectorError, Reason.CONNECTION),  # every *ConnectionError subclass
        (ConfigurationError, Reason.CONFIGURATION),
        (WarehouseQueryError, Reason.EXECUTION_FAILURE),  # the server said no
        (RequestError, Reason.REQUEST),
        (DexError, Reason.REQUEST),  # generic fallback for any other deliberate refusal
        (ValueError, Reason.REQUEST),  # the pre-typed-error convention: bad input
    ]

    # Everything else pulls in a connector extra transitively (sqlglot,
    # scikit-learn, the dbt reader chain) that a bare or partial install may
    # not have -- and a missing-extra refusal (DialectDependencyError,
    # ClusterDependencyError) is exactly the scenario where that extra is
    # absent. A ModuleNotFoundError here must not crash the one command
    # reporting exactly that, so a failed optional import just keeps the
    # safe subset above (an unlisted subclass still resolves through
    # `DexError`/`ValueError`) instead of propagating.
    try:
        from .connect import CredentialDiscoveryError
        from .dbt_project import DbtProjectError
        from .explore.cluster import ClusterDependencyError, ClusterError
        from .explore.semantic import SemanticBackendError, SemanticQueryRefusedError
        from .guards.cost_guard import CostGuardError
        from .guards.query_firewall import QueryRefusedError
        from .guards.sql_guard import NotSelectOnlyError
        from .results import BudgetExhaustedError
        from .transform.build import DbtRunError, ProdTargetRefusedError
        from .transform.commands import BuildFailedError, DbtParseError
        from .transform.plans import PlanError
        from .transform.scaffold import ScaffoldError
        from .transform.validate import EditValidationError
    except ImportError:
        _reason_overrides_cache = overrides
        return overrides

    # Most-specific-first: a subclass here overrides its parent's bucket.
    # Anything not listed falls through to the nearest listed ancestor via
    # isinstance, so a new DexError subclass only needs an entry when its own
    # reason diverges from its parent's.
    _reason_overrides_cache = [
        # A demo target that already exists is a safety refusal, not bad input:
        # the command declines to touch a warehouse it did not create.
        (DemoTargetExistsError, Reason.GUARD),
        (DemoDependencyError, Reason.PREREQUISITE),
        (DemoPathError, Reason.REQUEST),
        (SemanticQueryRefusedError, Reason.GUARD),  # policy, not backend failure
        (ClusterDependencyError, Reason.PREREQUISITE),  # install the extra, retry
        (DialectDependencyError, Reason.PREREQUISITE),
        (PrerequisiteError, Reason.PREREQUISITE),
        (CostGuardError, Reason.GUARD),  # OverCeilingError, CeilingRequiredError
        (BudgetExhaustedError, Reason.GUARD),
        (QueryRefusedError, Reason.GUARD),
        (NotSelectOnlyError, Reason.GUARD),
        (ProdTargetRefusedError, Reason.GUARD),
        (ConnectorError, Reason.CONNECTION),
        (CredentialDiscoveryError, Reason.CONFIGURATION),  # will not appear on a retry
        (ConfigurationError, Reason.CONFIGURATION),
        (DbtProjectError, Reason.CONFIGURATION),
        (SemanticBackendError, Reason.CONFIGURATION),  # after SemanticQueryRefusedError
        (BuildFailedError, Reason.EXECUTION_FAILURE),
        (DbtRunError, Reason.EXECUTION_FAILURE),
        # The statement ran and the server refused it: an execution failure for
        # the same reason a failed dbt run is one, and never `internal`.
        (WarehouseQueryError, Reason.EXECUTION_FAILURE),
        (RequestError, Reason.REQUEST),
        (DbtParseError, Reason.REQUEST),
        (PlanError, Reason.REQUEST),  # PlanNotFoundError
        (ScaffoldError, Reason.REQUEST),
        (EditValidationError, Reason.REQUEST),
        (ClusterError, Reason.REQUEST),  # after ClusterDependencyError
        (DexError, Reason.REQUEST),
        (ValueError, Reason.REQUEST),
    ]
    return _reason_overrides_cache


def reason_for(exc: BaseException) -> Reason:
    """Classify why a caught exception refused, from its class alone.

    Fails toward the informative: an unrecognized ``DexError`` subclass or a
    bare ``ValueError`` still reads as ``REQUEST`` (a deliberate refusal, bad
    input assumed) rather than ``INTERNAL``, which is reserved for exceptions
    that are not a deliberate dex refusal at all.
    """

    for exc_type, reason in _reason_overrides():
        if isinstance(exc, exc_type):
            return reason
    return Reason.INTERNAL


def error_for(
    exc: BaseException, message: str | None = None, **kwargs: Any
) -> Envelope:
    """Build an error envelope from a caught exception: ``errors`` defaults to
    ``str(exc)`` (override via ``message`` for call sites that add context or
    redact), and ``reason`` is derived automatically via :func:`reason_for`.
    """

    return Envelope(
        status=Status.ERROR,
        errors=[message if message is not None else str(exc)],
        reason=reason_for(exc),
        **kwargs,
    )


def needs_confirmation(
    data: dict[str, Any] | None = None, cost: Cost | None = None, **kwargs: Any
) -> Envelope:
    """A command that would spend (or overwrite) but was not confirmed.

    Carries the preflight cost so the agent can surface it and re-issue the
    command with ``--confirm``.
    """

    return Envelope(
        status=Status.NEEDS_CONFIRMATION,
        data=data or {},
        cost=cost or Cost(),
        **kwargs,
    )


# Re-exported for callers that build messages and want to avoid leaking secrets
# into error/warning strings (e.g. a DSN that embeds a password).
def redact(text: str) -> str:
    """Best-effort redaction of secret-looking tokens in a free-text message."""

    text = re.sub(r"(://[^:/\s]+:)([^@/\s]+)(@)", r"\1***\3", text)  # user:pass@host
    return text
