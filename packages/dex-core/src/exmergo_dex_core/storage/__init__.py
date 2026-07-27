"""Storage backends for dex's `.dex/` scratch state.

``base`` holds the contract as three nested tiers (:class:`~.base.ExploreStore`,
:class:`~.base.MaintainStore`, :class:`~.base.Store`), so a backend implements
only what its host uses; the sibling modules are the backends. Callers receive a
store; only the entry point picks which one.

``conformance`` is the executable contract, for anyone writing a backend outside
this package. It is deliberately not imported here: it needs pytest, and a bare
``import exmergo_dex_core`` must not.
"""

from .base import Document, ExploreStore, MaintainStore, Store, spend_total
from .filesystem import DEX_DIR, FilesystemStore
from .memory import MemoryStore

__all__ = [
    "DEX_DIR",
    "Document",
    "ExploreStore",
    "FilesystemStore",
    "MaintainStore",
    "MemoryStore",
    "Store",
    "spend_total",
]
