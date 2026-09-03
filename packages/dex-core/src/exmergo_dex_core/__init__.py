"""exmergo-dex-core: the portable analytics-engineering engine behind dex.

Two surfaces over one engine. :class:`DexEngine` is the programmatic API:

    from exmergo_dex_core import DexEngine

    with DexEngine(connector="duckdb", path="shop.duckdb") as eng:
        mapped = eng.map()
        rows = eng.query("select status, count(*) from orders group by status")

Nothing above touches disk: the default store keeps state in the process, so
importing this package cannot leave a ``.dex/`` directory in a consumer's repo.
:meth:`DexEngine.from_repo` is the opt-in to a project on disk.

The command contract in :mod:`exmergo_dex_core.cli` is the other surface, and it
is the first consumer of the first: every agent surface (SKILL.md, AGENTS.md) is
a thin wrapper over it, and it runs the same code a library call does.

A process serving more than one end user supplies the connection too, so each
request reaches the warehouse as its own principal rather than as the container:

    from exmergo_dex_core import ConnectionSource, DexEngine

    with DexEngine(
        connector="snowflake",
        config=cfg,
        store=store,
        connection=ConnectionSource(connect=lambda: user_conn),
    ) as eng:
        ...

A hosted dbt Cloud Semantic Layer has a credential of its own, supplied the same
way with :class:`SemanticSource`. That surface needs nothing on the filesystem: no
project, no store, no connector.

Read :class:`DexEngine`'s docstring before wiring this into a service. One engine
belongs to one principal and one session, an engine given an explicit config never
reads one from disk, and supplying a credential supplies identity but never the
cost guard; all three matter the moment a process serves more than one user.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

# Names are resolved on first access rather than imported here, for two reasons.
# The dialect engine and the dbt reader live behind connector extras, so eager
# imports would make a bare `pip install exmergo-dex-core` fail at import rather
# than at the first feature that needs one. And the CLI runs as a fresh
# subprocess per command, so keeping package import to almost nothing is free
# latency on every invocation.
_EXPORTS = {
    "BaselineUnreadableError": "maintain.commands",
    "BudgetExhaustedError": "results",
    "CacheRequiredError": "explore.commands",
    "CacheUnreadableError": "storage.base",
    "CeilingRequiredError": "guards.cost_guard",
    "ClusterDependencyError": "explore.cluster",
    "ClusterError": "explore.cluster",
    "ColumnProfile": "cache",
    "ConfigurationError": "errors",
    "ConfirmationRequest": "results",
    "ConfirmationRequiredError": "guards.cost_guard",
    "ConnectionSource": "connect",
    "ConnectorError": "errors",
    "Cost": "envelope",
    "CostGuardError": "guards.cost_guard",
    "CredentialDiscoveryError": "connect",
    "DEMO_FILENAME": "demo",
    "Dataset": "cache",
    "DemoDependencyError": "demo",
    "DemoError": "demo",
    "DemoPathError": "demo",
    "DemoTargetExistsError": "demo",
    "DexCache": "cache",
    "DexConfig": "config",
    "DexEngine": "engine",
    "DexError": "errors",
    "DialectDependencyError": "guards.dialect",
    "Document": "storage",
    "ExploreStore": "storage",
    "FilesystemStore": "storage",
    "LedgerUnreadableError": "guards.cost_guard",
    "MaintainStore": "storage",
    "MemoryStore": "storage",
    "MermaidDiagram": "explore.diagram",
    "NoBaselineError": "maintain.commands",
    "NoConnectorSelectedError": "errors",
    "OssieDependencyError": "ossie.loader",
    "OverCeilingError": "guards.cost_guard",
    "Paradigm": "envelope",
    "PlanError": "transform.plans",
    "PlanNotFoundError": "transform.plans",
    "PrerequisiteError": "errors",
    "ProjectError": "errors",
    "QueryRefusedError": "guards.query_firewall",
    "Relationship": "cache",
    "RepoRootRequiredError": "errors",
    "RequestError": "errors",
    "Result": "results",
    "ScopeError": "connect",
    "SemanticBackendError": "explore.semantic",
    "SemanticLayerError": "explore.semantic",
    "SemanticQueryRefusedError": "explore.semantic",
    "SemanticSource": "connect",
    "SessionCeilingDecisionRequiredError": "guards.cost_guard",
    "Snapshot": "maintain.snapshot",
    "SpendHistory": "storage",
    "SpendLock": "storage",
    "SpendLockTimeoutError": "guards.cost_guard",
    "Store": "storage",
    "StoreContext": "storage",
    "StoreFactory": "storage",
    "StoreRequiredError": "errors",
    "WarehouseQueryError": "errors",
    "generate_demo_warehouse": "demo",
    "render_er_mermaid": "explore.diagram",
    "to_envelope": "results",
}

if TYPE_CHECKING:  # what a type checker and an IDE see; never run
    from .cache import ColumnProfile, Dataset, DexCache, Relationship
    from .config import DexConfig
    from .connect import (
        ConnectionSource,
        CredentialDiscoveryError,
        ScopeError,
        SemanticSource,
    )
    from .demo import (
        DEMO_FILENAME,
        DemoDependencyError,
        DemoError,
        DemoPathError,
        DemoTargetExistsError,
        generate_demo_warehouse,
    )
    from .engine import DexEngine
    from .envelope import Cost, Paradigm
    from .errors import (
        ConfigurationError,
        ConnectorError,
        DexError,
        NoConnectorSelectedError,
        PrerequisiteError,
        ProjectError,
        RepoRootRequiredError,
        RequestError,
        StoreRequiredError,
        WarehouseQueryError,
    )
    from .explore.cluster import ClusterDependencyError, ClusterError
    from .explore.commands import CacheRequiredError
    from .explore.diagram import MermaidDiagram, render_er_mermaid
    from .explore.semantic import (
        SemanticBackendError,
        SemanticLayerError,
        SemanticQueryRefusedError,
    )
    from .guards.cost_guard import (
        CeilingRequiredError,
        ConfirmationRequiredError,
        CostGuardError,
        LedgerUnreadableError,
        OverCeilingError,
        SessionCeilingDecisionRequiredError,
        SpendLockTimeoutError,
    )
    from .guards.dialect import DialectDependencyError
    from .guards.query_firewall import QueryRefusedError
    from .maintain.commands import BaselineUnreadableError, NoBaselineError
    from .maintain.snapshot import Snapshot
    from .ossie.loader import OssieDependencyError
    from .results import (
        BudgetExhaustedError,
        ConfirmationRequest,
        Result,
        to_envelope,
    )
    from .storage import (
        CacheUnreadableError,
        Document,
        ExploreStore,
        FilesystemStore,
        MaintainStore,
        MemoryStore,
        SpendHistory,
        SpendLock,
        Store,
        StoreContext,
        StoreFactory,
    )
    from .transform.plans import PlanError, PlanNotFoundError

try:
    __version__ = version("exmergo-dex-core")
except PackageNotFoundError:
    # Running from a source tree with no installed distribution metadata.
    __version__ = "0.0.0"

# Spelled out rather than derived from `_EXPORTS` so a reader (and a type
# checker) sees the public surface without executing anything. `tests/
# test_engine.py` asserts the two stay in step and that every name resolves.
__all__ = [
    "DEMO_FILENAME",
    "BaselineUnreadableError",
    "BudgetExhaustedError",
    "CacheRequiredError",
    "CacheUnreadableError",
    "CeilingRequiredError",
    "ClusterDependencyError",
    "ClusterError",
    "ColumnProfile",
    "ConfigurationError",
    "ConfirmationRequest",
    "ConfirmationRequiredError",
    "ConnectionSource",
    "ConnectorError",
    "Cost",
    "CostGuardError",
    "CredentialDiscoveryError",
    "Dataset",
    "DemoDependencyError",
    "DemoError",
    "DemoPathError",
    "DemoTargetExistsError",
    "DexCache",
    "DexConfig",
    "DexEngine",
    "DexError",
    "DialectDependencyError",
    "Document",
    "ExploreStore",
    "FilesystemStore",
    "LedgerUnreadableError",
    "MaintainStore",
    "MemoryStore",
    "MermaidDiagram",
    "NoBaselineError",
    "NoConnectorSelectedError",
    "OssieDependencyError",
    "OverCeilingError",
    "Paradigm",
    "PlanError",
    "PlanNotFoundError",
    "PrerequisiteError",
    "ProjectError",
    "QueryRefusedError",
    "Relationship",
    "RepoRootRequiredError",
    "RequestError",
    "Result",
    "ScopeError",
    "SemanticBackendError",
    "SemanticLayerError",
    "SemanticQueryRefusedError",
    "SemanticSource",
    "SessionCeilingDecisionRequiredError",
    "Snapshot",
    "SpendHistory",
    "SpendLock",
    "SpendLockTimeoutError",
    "Store",
    "StoreContext",
    "StoreFactory",
    "StoreRequiredError",
    "WarehouseQueryError",
    "__version__",
    "generate_demo_warehouse",
    "render_er_mermaid",
    "to_envelope",
]


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list[str]:
    return list(__all__)
