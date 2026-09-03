"""Backend identity, capability declarations, and resolution.

This module is the semantic backend seam.  It deliberately knows nothing about
catalog serialization, PII policy, MetricFlow, or GraphQL.  A backend describes
itself once with :class:`BackendDescriptor`; command and result layers consume
that descriptor instead of reaching through a growing set of class attributes.
"""

from __future__ import annotations

from collections.abc import Callable
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


class SemanticLayerError(DexError):
    """A semantic layer could not be constructed, reached, or queried."""


class SemanticQueryRefusedError(SemanticLayerError):
    """A semantic request was understood and deliberately not executed."""


class SemanticLayer(Protocol):
    """The behavior required from a semantic layer and its runtime.

    A layer is the semantic system of record, whether it is a local dbt
    artifact, dbt Cloud, or native Ossie documents.  ``backend`` was the old
    name for this seam; it hid that all three implementations own discovery as
    well as execution.
    """

    descriptor: BackendDescriptor

    def list_definitions(self) -> Any: ...

    def query(self, q: Any) -> Any: ...

    def values(self, dimension: str, metrics: list[str]) -> Any: ...

    def filter_refs(self, clauses: list[str]) -> list[str] | None: ...

    def declared_relationships(self) -> list[Any]:
        """Native full-pair declarations, or an empty list when unavailable."""
        ...

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Dataset keys this layer declares itself, as ``(keys, composite_keys)``.

        Empty for a layer whose declared keys already reach the grain channel
        through the transformation project's own tier-1 ``definitions()`` (dbt):
        stating them here too would risk a caller double-counting rather than
        ever adding information. Non-empty only for a layer that is itself the
        sole declaration channel for its repository, such as native Ossie
        documents, which are never a transformation project and so have no
        other route to grain detection.
        """
        ...


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
    "dbt": {EXECUTION_DEX: "local", EXECUTION_VENDOR: "dbt_cloud"},
    "ossie": {EXECUTION_DEX: "local"},
}


def _local_metricflow(engine: Any) -> SemanticLayer:
    from .local import LocalMetricFlowBackend

    return LocalMetricFlowBackend.from_engine(engine)


def _hosted_dbt_cloud(engine: Any) -> SemanticLayer:
    from .hosted import HostedDbtCloudBackend

    return HostedDbtCloudBackend.from_config(
        engine.config, getattr(engine, "semantic_source", None)
    )


def _local_ossie(engine: Any) -> SemanticLayer:
    from .ossie import LocalOssieLayer

    return LocalOssieLayer.from_engine(engine)


# Which backend answers for a resolved (vendor, deployment) pair. A table rather
# than a chain of conditions, so a vendor is a row here and nowhere else, and so
# a deployment that resolved is a deployment that gets used: the previous shape
# computed one and then short-circuited past it for a vendor, which happened to
# be harmless only because that vendor has exactly one deployment.
#
# Each entry is a loader rather than a class, because the backends sit behind
# different extras and importing all three to select one would make every
# install pay for the other two.
_BACKENDS: dict[tuple[str, str], Callable[[Any], SemanticLayer]] = {
    ("dbt", "local"): _local_metricflow,
    ("dbt", "dbt_cloud"): _hosted_dbt_cloud,
    ("ossie", "local"): _local_ossie,
}

# Deployments dex executes itself, so a hosted semantic source (a dbt Cloud
# token) has no meaning for them and is refused rather than ignored. Keyed by
# the pair for the same reason the table above is.
_SOURCE_REFUSALS: dict[tuple[str, str], str] = {
    ("dbt", "local"): (
        "a semantic source supplies a hosted dbt Cloud token and has no "
        "meaning for the local backend, which renders metric SQL and runs it "
        "through this engine's own connector. Select the hosted backend "
        "(semantic.deployment: dbt_cloud, or --api), or drop the source and "
        "let the connector's credential govern"
    ),
    ("ossie", "local"): (
        "a semantic source supplies a hosted dbt Cloud token and has no "
        "meaning for native Apache Ossie documents, which are files in this "
        "repository. Drop the source, or select a hosted dbt semantic layer "
        "with semantic.vendor: dbt and semantic.deployment: dbt_cloud"
    ),
}


def resolve_semantic_layer(
    engine: Any, *, api: bool = False, local: bool = False
) -> SemanticLayer:
    """Resolve the configured semantic layer or an execution override."""

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
        try:
            deployment = _EXECUTION_DEPLOYMENTS[vendor][execution]
        except KeyError as exc:
            raise SemanticBackendError(
                f"semantic vendor '{vendor}' has no "
                f"{'hosted' if api else 'local'} execution override"
            ) from exc
    else:
        configured = getattr(semantic, "deployment", None) or getattr(
            semantic, "backend", None
        )
        deployment = canonical_semantic_deployment(configured or "local")

    build = _BACKENDS.get((vendor, deployment))
    if build is None:
        raise SemanticBackendError(
            f"vendor '{vendor}' has no deployment '{deployment}'; use one of "
            f"{', '.join(SEMANTIC_DEPLOYMENTS[vendor])} (or pass --local / --api)"
        )
    if getattr(engine, "semantic_source", None) is not None:
        refusal = _SOURCE_REFUSALS.get((vendor, deployment))
        if refusal is not None:
            raise SemanticBackendError(refusal)
    return build(engine)


# RETRO: Compatibility names retained for callers that imported the first version of
# this seam. New code must use SemanticLayer / resolve_semantic_layer.
SemanticBackend = SemanticLayer
SemanticBackendError = SemanticLayerError


def resolve_backend(
    engine: Any, *, api: bool = False, local: bool = False
) -> SemanticLayer:
    """Deprecated compatibility alias for :func:`resolve_semantic_layer`."""

    return resolve_semantic_layer(engine, api=api, local=local)
