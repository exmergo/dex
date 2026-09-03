"""Backend identity, capability declarations, and resolution.

This module is the semantic backend seam.  It deliberately knows nothing about
catalog serialization, PII policy, MetricFlow, or GraphQL.  A backend describes
itself once with :class:`BackendDescriptor`; command and result layers consume
that descriptor instead of reaching through a growing set of class attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ...errors import DexError
from ...semantic_catalog import DIMENSIONS_PER_DECLARATION

EXECUTION_DEX = "dex"
EXECUTION_VENDOR = "vendor"


@dataclass(frozen=True)
class BackendDescriptor:
    """Stable facts about one semantic backend.

    ``execution`` is the load-bearing axis: it decides whether dex owns a
    statement it can price and cap.  Catalog gaps and dimension scope live here
    as capability declarations rather than being inferred from missing fields.
    """

    name: str
    vendor: str
    deployment: str
    execution: str
    catalog_gaps: dict[str, list[str]] = field(default_factory=dict)
    dimension_scope: str = DIMENSIONS_PER_DECLARATION
    cost_guard_warning: str | None = None


class SemanticBackendError(DexError):
    """A semantic backend could not be constructed, reached, or queried."""


class SemanticQueryRefusedError(SemanticBackendError):
    """A semantic request was understood and deliberately not executed."""


class SemanticBackend(Protocol):
    """The behavior required from a semantic backend."""

    descriptor: BackendDescriptor

    def list_definitions(self) -> Any: ...

    def query(self, q: Any) -> Any: ...

    def values(self, dimension: str, metrics: list[str]) -> Any: ...

    def filter_refs(self, clauses: list[str]) -> list[str] | None: ...


def descriptor_from_backend(backend: Any) -> BackendDescriptor:
    """Read a descriptor, including compatibility for narrow test doubles."""

    declared = getattr(backend, "descriptor", None)
    if isinstance(declared, BackendDescriptor):
        return declared
    return BackendDescriptor(
        name=getattr(backend, "name", ""),
        vendor=getattr(backend, "vendor", ""),
        deployment=getattr(backend, "deployment", ""),
        execution=getattr(backend, "execution", ""),
        catalog_gaps=dict(getattr(backend, "catalog_gaps", None) or {}),
        dimension_scope=getattr(backend, "dimension_scope", None)
        or DIMENSIONS_PER_DECLARATION,
        cost_guard_warning=getattr(backend, "cost_guard_warning", None),
    )


def backend_axes(backend: Any) -> dict[str, str]:
    """A backend's provenance as payload fields."""

    descriptor = descriptor_from_backend(backend)
    return {
        "backend": descriptor.name,
        "vendor": descriptor.vendor,
        "deployment": descriptor.deployment,
        "execution": descriptor.execution,
    }


def catalog_declarations(backend: Any) -> dict[str, Any]:
    """The catalog capabilities declared by a backend."""

    descriptor = descriptor_from_backend(backend)
    return {
        "unavailable": dict(descriptor.catalog_gaps),
        "dimension_scope": descriptor.dimension_scope,
    }


def cost_posture(backend: Any) -> tuple[Any, list[str]]:
    """Return the cost result implied by who executes the semantic query."""

    descriptor = descriptor_from_backend(backend)
    if descriptor.execution != EXECUTION_VENDOR:
        return None, []
    from ... import envelope as env

    warnings = [descriptor.cost_guard_warning] if descriptor.cost_guard_warning else []
    return env.Cost(paradigm=env.Paradigm.HOSTED), warnings


def values_gap(backend: Any) -> str:
    """Explain that a backend has no dimension-values implementation."""

    named = descriptor_from_backend(backend).name or type(backend).__name__
    return (
        f"the '{named}' semantic backend does not read a dimension's value "
        "domain; implement `values(dimension, metrics)` on it, or ask a backend "
        "that does (`--local` / `--api`)"
    )


_EXECUTION_DEPLOYMENTS: dict[str, dict[str, str]] = {
    "dbt": {EXECUTION_DEX: "local", EXECUTION_VENDOR: "dbt_cloud"}
}


def resolve_backend(
    engine: Any, *, api: bool = False, local: bool = False
) -> SemanticBackend:
    """Resolve the configured semantic deployment or an execution override."""

    from ...config import SEMANTIC_DEPLOYMENTS, canonical_semantic_deployment

    if api and local:
        raise SemanticBackendError("choose one of --local or --api, not both")

    semantic = getattr(engine.config, "semantic", None)
    vendor = (getattr(semantic, "vendor", None) or "dbt").strip().lower()
    if vendor not in SEMANTIC_DEPLOYMENTS:
        raise SemanticBackendError(
            f"unknown semantic vendor '{vendor}'; dex ships "
            f"{', '.join(sorted(SEMANTIC_DEPLOYMENTS))}"
        )

    if api or local:
        execution = EXECUTION_VENDOR if api else EXECUTION_DEX
        deployment = _EXECUTION_DEPLOYMENTS[vendor][execution]
    else:
        configured = getattr(semantic, "deployment", None) or getattr(
            semantic, "backend", None
        )
        deployment = canonical_semantic_deployment(configured or "local")

    source = getattr(engine, "semantic_source", None)
    if deployment == "dbt_cloud":
        from .hosted import HostedDbtCloudBackend

        return HostedDbtCloudBackend.from_config(engine.config, source)
    if deployment == "local":
        if source is not None:
            raise SemanticBackendError(
                "a semantic source supplies a hosted dbt Cloud token and has no "
                "meaning for the local backend, which renders metric SQL and runs "
                "it through this engine's own connector. Select the hosted backend "
                "(semantic.deployment: dbt_cloud, or --api), or drop the source "
                "and let the connector's credential govern"
            )
        from .local import LocalMetricFlowBackend

        return LocalMetricFlowBackend.from_engine(engine)
    raise SemanticBackendError(
        f"vendor '{vendor}' has no deployment '{deployment}'; use one of "
        f"{', '.join(SEMANTIC_DEPLOYMENTS[vendor])} (or pass --local / --api)"
    )
