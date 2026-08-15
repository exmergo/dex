"""`dex demo`: a seeded local warehouse, so a first run needs no credentials.

Installing dex leaves nothing to point it at. This package closes that gap: one
command, no credentials, no cloud account, and no network builds a small DuckDB
warehouse with real-looking e-commerce data and real, deliberately seeded flaws,
so the first `explore` run reports findings instead of a clean bill of health.

:mod:`.warehouse` generates and writes the file and is the only writable path in
the engine; :mod:`.commands` is the CLI shim over it and owns the surrounding
project wiring (the `.dex/config.yml` that lets every following command run with
no flags at all).
"""

from __future__ import annotations

from .warehouse import (
    DEMO_FILENAME,
    DEMO_SEED,
    DemoDependencyError,
    DemoError,
    DemoPathError,
    DemoTable,
    DemoTargetExistsError,
    DemoWarehouse,
    build_tables,
    generate_demo_warehouse,
    row_digest,
)

__all__ = [
    "DEMO_FILENAME",
    "DEMO_SEED",
    "DemoDependencyError",
    "DemoError",
    "DemoPathError",
    "DemoTable",
    "DemoTargetExistsError",
    "DemoWarehouse",
    "build_tables",
    "generate_demo_warehouse",
    "row_digest",
]
