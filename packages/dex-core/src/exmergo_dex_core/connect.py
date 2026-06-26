"""Connection handling and credential discovery.

All connection handling lives here so credentials and raw rows stay inside the
engine process and never reach the agent. In v0.1 only DuckDB is wired: a local
file path, opened read-only, no credentials. Cloud credential discovery
("discover, don't ask") is not yet implemented.
"""

from __future__ import annotations

from pathlib import Path

from .adapters import get_adapter
from .config import DexConfig, load_config


def open_adapter(
    *,
    connector: str | None = None,
    path: str | None = None,
    repo_root: str | Path = ".",
):
    """Resolve the connection target and return an open, read-only adapter.

    Resolution order: explicit arguments win, then ``.dex/config.yml``. For
    DuckDB the only input is a file path. Cloud connectors will resolve
    credentials from their stores here; nothing is ever prompted for.
    """

    config = load_config(repo_root) or DexConfig()
    connector = connector or config.connector

    if connector == "duckdb":
        resolved = path or (config.duckdb.path if config.duckdb else None)
        if not resolved:
            raise ValueError(
                "no DuckDB path: pass --path or set duckdb.path in .dex/config.yml"
            )
        return get_adapter("duckdb", path=resolved)

    # Cloud connectors discover credentials from their own stores.
    return get_adapter(connector)
