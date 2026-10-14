"""SLayer semantic models as dex's semantic layer (`semantic.vendor: slayer`).

Importing this package never imports SLayer. The `[slayer]` extra is heavy (a
base SLayer brings FastAPI, uvicorn, mcp, duckdb, jafgen and tantivy), so the
availability check reads distribution metadata instead, and anything that needs
SLayer itself reaches it only past :func:`ensure_available`.
"""

from __future__ import annotations

from .dependency import (
    SLAYER_DISTRIBUTION,
    SLAYER_VERSION,
    SlayerDependencyError,
    ensure_available,
    installed_version,
)

__all__ = [
    "SLAYER_DISTRIBUTION",
    "SLAYER_VERSION",
    "SlayerDependencyError",
    "ensure_available",
    "installed_version",
]
