"""Storage backends for dex's `.dex/` scratch state.

``base`` holds the contract as three nested tiers (:class:`~.base.ExploreStore`,
:class:`~.base.MaintainStore`, :class:`~.base.Store`), so a backend implements
only what its host uses; the sibling modules are the backends. Callers receive a
store; only the entry point picks which one.

It also holds two separate, optional contracts that sit alongside the tiers
rather than inside them: :class:`~.base.SpendLock`, which lets the cost gate make
the spend admission atomic so the cumulative ceiling binds under concurrency, and
the construction contract (:class:`~.base.StoreFactory` over a
:class:`~.base.StoreContext`), for a backend that is named somewhere rather than
handed to the engine as an instance.
``resolver`` is what turns such a name into a store, as an open registry: a
shipped name, a dotted path, or an entry point an installed distribution
registered.

``conformance`` is the executable contract, for anyone writing a backend outside
this package. It is deliberately not imported here: it needs pytest, and a bare
``import exmergo_dex_core`` must not.
"""

from .base import (
    CacheUnreadableError,
    Document,
    ExploreStore,
    MaintainStore,
    SpendLock,
    Store,
    StoreContext,
    StoreFactory,
    readable_cache,
    spend_total,
)
from .filesystem import DEX_DIR, FilesystemStore
from .memory import MemoryStore
from .resolver import build_store, resolve_store_factory

__all__ = [
    "DEX_DIR",
    "CacheUnreadableError",
    "Document",
    "ExploreStore",
    "FilesystemStore",
    "MaintainStore",
    "MemoryStore",
    "SpendLock",
    "Store",
    "StoreContext",
    "StoreFactory",
    "build_store",
    "readable_cache",
    "resolve_store_factory",
    "spend_total",
]
