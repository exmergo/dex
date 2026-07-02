"""Non-secret project config, read from ``.dex/config.yml``.

Config separates from secrets by construction: connector targets, the dbt target,
session budgets, and ranking hints live here and are committed to the repo.
Secrets (passwords, keys, tokens) are read at runtime from their own stores by
``connect.py`` and are never written or logged here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .cache import DEX_DIR
from .envelope import Paradigm

CONFIG_FILE = "config.yml"


class Budget(BaseModel):
    """A per-session cost ceiling for a connector paradigm. Magnitude is
    paradigm-relative (bytes, credits, DBUs, load score); DuckDB is unbounded by
    cost and only resource-bounded."""

    paradigm: Paradigm = Paradigm.FREE_LOCAL
    ceiling: float | None = None


class DuckDBTarget(BaseModel):
    # A local DuckDB file, or a directory of Parquet/CSV. Opened read-only.
    path: str


class QueryLimits(BaseModel):
    """Hard bounds on `explore query` results, enforced in the engine.

    The caps protect agent context from token blowups: an oversized result is
    truncated with an explicit note rather than trusted to agent frugality.
    """

    max_rows: int = 50
    max_cell_chars: int = 256
    max_payload_bytes: int = 16384
    timeout_seconds: float = 30.0


class DexConfig(BaseModel):
    """The shape of ``.dex/config.yml``. Only the DuckDB target is wired in v0.1;
    cloud connector targets are not yet implemented."""

    connector: str = "duckdb"
    duckdb: DuckDBTarget | None = None
    dbt_target: str | None = None
    budget: Budget = Field(default_factory=Budget)
    ranking_hints: list[str] = Field(default_factory=list)
    query: QueryLimits = Field(default_factory=QueryLimits)
    # How many top-ranked objects `explore map` deep-profiles on a large
    # warehouse; the rest stay inventory-only. Selective by default, overridable.
    profile_top_n: int = 25


def load_config(repo_root: Path | str = ".") -> DexConfig | None:
    path = Path(repo_root) / DEX_DIR / CONFIG_FILE
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return DexConfig.model_validate(raw)


def save_config(config: DexConfig, repo_root: Path | str = ".") -> Path:
    dex_dir = Path(repo_root) / DEX_DIR
    dex_dir.mkdir(parents=True, exist_ok=True)
    path = dex_dir / CONFIG_FILE
    path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True), sort_keys=False
        ),
        encoding="utf-8",
    )
    return path
