"""exmergo-dex-core: the portable analytics-engineering engine behind dex.

All non-trivial logic lives here; every agent surface (SKILL.md, AGENTS.md) is a
thin wrapper over the command contract in :mod:`exmergo_dex_core.cli`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("exmergo-dex-core")
except PackageNotFoundError:
    # Running from a source tree with no installed distribution metadata.
    __version__ = "0.0.0"
