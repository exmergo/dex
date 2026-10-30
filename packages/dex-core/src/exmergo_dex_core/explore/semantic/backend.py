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
    artifact, dbt Cloud, native Ossie documents, or SLayer model files.
    ``backend`` was the old name for this seam; it hid that every
    implementation owns discovery as well as execution.
    """

    descriptor: BackendDescriptor

    def list_definitions(self) -> Any: ...

    def query(self, q: Any) -> Any: ...

    def values(self, dimension: str, metrics: list[str]) -> Any: ...

    def filter_refs(self, clauses: list[str]) -> list[str] | None: ...

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
        """The dimension tokens this layer's time token buckets for ``metrics``.

        A time grouping (MetricFlow's ``metric_time``, SLayer's ``--grain``)
        projects the time dimension of each measure the metrics read, without
        the caller naming it, so the PII gate screens these tokens as it screens
        a grouped dimension. None is this backend's stated position that it
        cannot resolve them, which leaves the time token on the name heuristic
        and the result disclosing that it did.
        """
        ...

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


# Which deployment an execution override selects, for the vendors where the two
# are one axis: dbt's local artifact is the only thing dex can execute, and dbt
# Cloud the only thing that executes for itself.
_EXECUTION_DEPLOYMENTS: dict[str, dict[str, str]] = {
    "dbt": {EXECUTION_DEX: "local", EXECUTION_VENDOR: "dbt_cloud"},
    "ossie": {EXECUTION_DEX: "local"},
    "slayer": {EXECUTION_DEX: "local"},
}

# The vendors whose deployment (where the layer runs) and execution (who runs
# the SQL) are independent axes, and the executions each offers in this release.
# Every Cube deployment serves the same REST API, so `--local` and `--api` choose
# who executes and leave the configured deployment alone: Cube renders over
# `/v1/sql` and dex executes, or Cube executes over `/v1/load` (#914).
_INDEPENDENT_EXECUTIONS: dict[str, tuple[str, ...]] = {
    "cube": (EXECUTION_DEX,),
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


def _local_slayer(engine: Any) -> SemanticLayer:
    from .slayer import LocalSlayerLayer

    return LocalSlayerLayer.from_engine(engine)


def _local_cube(engine: Any) -> SemanticLayer:
    from .cube import LocalCubeLayer

    return LocalCubeLayer.from_engine(engine)


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
    ("slayer", "local"): _local_slayer,
    ("cube", "local"): _local_cube,
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
    ("slayer", "local"): (
        "a semantic source supplies a hosted dbt Cloud token and has no "
        "meaning for SLayer model files, which are files in this repository. "
        "Drop the source, or select a hosted dbt semantic layer with "
        "semantic.vendor: dbt and semantic.deployment: dbt_cloud"
    ),
    ("cube", "local"): (
        "a semantic source supplies a hosted dbt Cloud token and has no "
        "meaning for Cube model files, which are files in this repository. "
        "Drop the source, or select a hosted dbt semantic layer with "
        "semantic.vendor: dbt and semantic.deployment: dbt_cloud"
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

    configured = getattr(semantic, "deployment", None) or getattr(
        semantic, "backend", None
    )
    if (api or local) and vendor in _INDEPENDENT_EXECUTIONS:
        execution = EXECUTION_VENDOR if api else EXECUTION_DEX
        if execution not in _INDEPENDENT_EXECUTIONS[vendor]:
            raise SemanticBackendError(
                f"semantic vendor '{vendor}' has no hosted execution (--api) in "
                "this release: dex asks a Cube server to render SQL over /v1/sql "
                "and executes it itself under the cost guard, and does not yet "
                "have Cube execute a query over /v1/load. Drop --api"
            )
        deployment = canonical_semantic_deployment(configured or "local")
    elif api or local:
        execution = EXECUTION_VENDOR if api else EXECUTION_DEX
        try:
            deployment = _EXECUTION_DEPLOYMENTS[vendor][execution]
        except KeyError as exc:
            raise SemanticBackendError(
                f"semantic vendor '{vendor}' has no "
                f"{'hosted' if api else 'local'} execution override"
            ) from exc
    else:
        deployment = canonical_semantic_deployment(configured or "local")

    build = _BACKENDS.get((vendor, deployment))
    if build is None and deployment in SEMANTIC_DEPLOYMENTS[vendor]:
        # A deployment the vendor has, with no runtime dex can drive yet. Said
        # as that, rather than as a deployment that does not exist, which would
        # send the reader to change a setting that is already right.
        raise SemanticBackendError(
            f"dex has no `explore semantic` backend for semantic vendor "
            f"'{vendor}' yet; the {vendor} layer is configured, and this command "
            "cannot read it"
        )
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
