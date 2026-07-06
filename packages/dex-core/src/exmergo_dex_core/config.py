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
    """Cost ceilings for a connector paradigm. Magnitudes are paradigm-relative
    (bytes, credits, DBUs, load score); DuckDB is unbounded by cost and only
    resource-bounded.

    ``ceiling`` bounds one command; a ``--budget`` flag overrides it per call.
    ``session_ceiling`` bounds cumulative spend across commands per UTC day,
    settled against the ``.dex/spend.jsonl`` ledger, so a long agent session
    (or a loop of confirmed commands) still has a hard stop.
    """

    paradigm: Paradigm = Paradigm.FREE_LOCAL
    ceiling: float | None = None
    session_ceiling: float | None = None


class DuckDBTarget(BaseModel):
    # A local DuckDB file, or a directory of Parquet/CSV. Opened read-only.
    path: str


class BigQueryTarget(BaseModel):
    """Non-secret BigQuery connection target. Credentials are never here: auth
    is Application Default Credentials, discovered at runtime by connect.py.

    ``project`` is the billing/quota project jobs run in (also the default
    project whose datasets are explored). ``datasets`` is a source allowlist
    (entries ``dataset`` or ``project.dataset``); empty means every dataset in
    the project. ``dev_dataset`` is where dbt dev builds write, and is refused
    as a source so reads and writes can never share a dataset.
    ``max_full_profile_bytes`` opts large tables into block-sampled profiling
    (TABLESAMPLE) instead of a full scan; unset means profile fully, bounded
    only by the budget.
    """

    project: str | None = None
    location: str | None = None
    datasets: list[str] = Field(default_factory=list)
    dev_dataset: str | None = None
    max_full_profile_bytes: int | None = None


class SnowflakeTarget(BaseModel):
    """Non-secret Snowflake connection target. Credentials are never here: auth
    is discovered at runtime by connect.py (connections.toml, SNOWFLAKE_* env,
    or a dbt profile), and passwords or keys are never written or logged.

    ``connection_name`` pins a ``connections.toml`` entry; unset means the
    default connection, then the environment, then a dbt profile. ``warehouse``
    is the pinned compute for every billed statement: dex refuses to spend on a
    warehouse the config does not name, so a connection-level default can never
    silently land work on oversized compute. ``databases`` is a source
    allowlist (entries ``db`` or ``db.schema``); empty means every database the
    role can see. ``dev_database``/``dev_schema`` are where dbt dev builds
    write; the pair is refused as a source so reads and writes never share a
    schema. ``max_full_profile_bytes`` opts large tables into sampled profiling
    (SAMPLE SYSTEM) instead of a full scan. ``credit_price_usd`` is the
    contract-specific dollar price of one credit; set it to see dollar figures
    next to the credit translation (no API exposes it, so dex never guesses).
    """

    account: str | None = None
    connection_name: str | None = None
    warehouse: str | None = None
    databases: list[str] = Field(default_factory=list)
    dev_database: str | None = None
    dev_schema: str | None = None
    max_full_profile_bytes: int | None = None
    credit_price_usd: float | None = None


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
    """The shape of ``.dex/config.yml``. DuckDB, BigQuery, and Snowflake
    targets are wired; the remaining cloud connector targets are not yet
    implemented."""

    connector: str = "duckdb"
    duckdb: DuckDBTarget | None = None
    bigquery: BigQueryTarget | None = None
    snowflake: SnowflakeTarget | None = None
    dbt_target: str | None = None
    # Pins the dbt project directory (relative to the repo root) when discovery
    # would be ambiguous; by default the project is located automatically.
    dbt_project_dir: str | None = None
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
    # Only fields that were loaded or assigned are written: the committed file
    # stays a record of explicit choices, not a dump of every engine default.
    path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_unset=True, exclude_none=True),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
