"""Storage backends for dex's `.dex/` scratch state.

``base`` holds the contract (see :class:`~.base.Store` for what a backend owes its
callers); the sibling modules are the backends. Callers receive a ``Store``; only
the entry point picks which one.
"""

from .base import Document, Store, spend_total
from .filesystem import DEX_DIR, FilesystemStore
from .memory import MemoryStore

__all__ = [
    "DEX_DIR",
    "Document",
    "FilesystemStore",
    "MemoryStore",
    "Store",
    "spend_total",
]
