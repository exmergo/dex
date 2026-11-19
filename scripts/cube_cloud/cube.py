# The Cube Cloud fixture deployment dex's `cube_cloud` integration job reads
# (#914): the model in scripts/cube/model, over an in-memory DuckDB built from
# scripts/cube/shop.sql, the same rows dex's own DuckDB fixture holds. So a
# number Cube Cloud executes (`--api`) and one dex executes over what Cube Cloud
# renders (`--local`) can be held to each other and to hand-written SQL.
#
# scripts/setup_cube_cloud_ci.sh assembles this file, shop.sql and the model
# into one directory and deploys it; nothing here holds a credential, and the
# warehouse is the DuckDB this file builds, so the deployment reaches nothing.
from pathlib import Path

from cube import config

SHOP = (Path(__file__).resolve().parent / "shop.sql").read_text(encoding="utf-8")


@config("driver_factory")
def driver_factory(ctx: dict) -> dict:
    return {"type": "duckdb", "initSql": SHOP}
