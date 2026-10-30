"""Re-record what Cube Core renders for each case, from the render-only servers.

Run by hand after `scripts/setup_cube_dev.sh --db <each>` and sourcing
~/.cache/dex-dogfood/cube.env; it writes `recorded/<connector>.json` and
`recorded/meta.json`. A recording changes only when someone re-runs this
against a deliberately bumped Cube release and reads the diff:

    uv run python tests/cube_vendor/record_cube.py

Planner diagnostics dex never reads (`canUseTransformedQuery`,
`cacheKeyQueries`) are dropped to keep the recordings reviewable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.cube.loader import load_files
from exmergo_dex_core.cube.query import (
    index,
    split_time,
    to_cube_query,
    to_cube_values_query,
)
from exmergo_dex_core.cube.transport import CubeTransport
from exmergo_dex_core.explore.semantic.model import SemanticQuery

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cube_cases import (
    CONNECTORS,
    MODEL,
    MODEL_FILES,
    QUERIES,
    RECORDED,
    VALUES,
)

_DROPPED = ("canUseTransformedQuery", "cacheKeyQueries")


def cube_queries() -> dict[str, dict]:
    loaded = load_files(MODEL, MODEL_FILES)
    members = index(loaded.cubes, loaded.views)
    max_rows = QueryLimits().max_rows
    out = {
        name: to_cube_query(SemanticQuery(**case), members, max_rows=max_rows).query
        for name, case in QUERIES.items()
    }
    for name, (token, metrics) in VALUES.items():
        dimension, grain = split_time(token, members)
        out[name] = to_cube_values_query(
            dimension, grain, metrics, members, max_rows=max_rows
        ).query
    return out


def trimmed(body: dict) -> dict:
    plan = dict(body.get("sql") or {})
    for key in _DROPPED:
        plan.pop(key, None)
    return {**body, "sql": plan}


def main() -> None:
    queries = cube_queries()
    RECORDED.mkdir(exist_ok=True)
    for connector in CONNECTORS:
        url = os.environ[f"DEX_TEST_CUBE_URL_{connector.upper()}"]
        transport = CubeTransport(url)
        if connector == "postgres":
            meta = transport.meta()
            (RECORDED / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        recorded = {
            name: {"query": query, "response": trimmed(transport.sql(query))}
            for name, query in queries.items()
        }
        path = RECORDED / f"{connector}.json"
        path.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n")
        print(f"recorded {len(recorded)} renders for {connector} -> {path.name}")


if __name__ == "__main__":
    main()
