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

Read :class:`DexEngine`'s docstring before wiring this into a service. One engine
belongs to one principal and one session, and an engine given an explicit config
never reads one from disk; both matter the moment a process serves more than one
user.
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
    "BudgetExhaustedError": "results",
    "CeilingRequiredError": "guards.cost_guard",
    "ColumnProfile": "cache",
    "ConfirmationRequest": "results",
    "ConfirmationRequiredError": "guards.cost_guard",
    "Cost": "envelope",
    "CostGuardError": "guards.cost_guard",
    "Dataset": "cache",
    "DexCache": "cache",
    "DexConfig": "config",
    "Document": "storage",
    "DexEngine": "engine",
    "FilesystemStore": "storage",
    "MemoryStore": "storage",
    "OverCeilingError": "guards.cost_guard",
    "Paradigm": "envelope",
    "QueryRefusedError": "guards.query_firewall",
    "Relationship": "cache",
    "Result": "results",
    "Snapshot": "maintain.snapshot",
    "Store": "storage",
    "to_envelope": "results",
}

if TYPE_CHECKING:  # what a type checker and an IDE see; never run
    from .cache import ColumnProfile, Dataset, DexCache, Relationship
    from .config import DexConfig
    from .engine import DexEngine
    from .envelope import Cost, Paradigm
    from .guards.cost_guard import (
        CeilingRequiredError,
        ConfirmationRequiredError,
        CostGuardError,
        OverCeilingError,
    )
    from .guards.query_firewall import QueryRefusedError
    from .maintain.snapshot import Snapshot
    from .results import (
        BudgetExhaustedError,
        ConfirmationRequest,
        Result,
        to_envelope,
    )
    from .storage import Document, FilesystemStore, MemoryStore, Store

try:
    __version__ = version("exmergo-dex-core")
except PackageNotFoundError:
    # Running from a source tree with no installed distribution metadata.
    __version__ = "0.0.0"

# Spelled out rather than derived from `_EXPORTS` so a reader (and a type
# checker) sees the public surface without executing anything. `tests/
# test_engine.py` asserts the two stay in step and that every name resolves.
__all__ = [
    "BudgetExhaustedError",
    "CeilingRequiredError",
    "ColumnProfile",
    "ConfirmationRequest",
    "ConfirmationRequiredError",
    "Cost",
    "CostGuardError",
    "Dataset",
    "DexCache",
    "DexConfig",
    "DexEngine",
    "Document",
    "FilesystemStore",
    "MemoryStore",
    "OverCeilingError",
    "Paradigm",
    "QueryRefusedError",
    "Relationship",
    "Result",
    "Snapshot",
    "Store",
    "__version__",
    "to_envelope",
]


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list[str]:
    return list(__all__)
