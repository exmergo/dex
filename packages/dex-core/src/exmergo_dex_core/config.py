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


class DatabricksTarget(BaseModel):
    """Non-secret Databricks connection target. Credentials are never here: auth
    is discovered at runtime by connect.py through the SDK's unified chain (a
    ``~/.databrickscfg`` profile, ``DATABRICKS_*`` environment variables, or a
    dbt profile), and tokens are never written or logged.

    ``profile`` pins a ``~/.databrickscfg`` entry; unset means the environment,
    then the default profile, then a dbt profile. ``host`` overrides the
    workspace URL when the discovered source carries none. ``warehouse`` is the
    pinned SQL warehouse (an ID or its ``/sql/1.0/warehouses/...`` HTTP path)
    for every billed statement: dex refuses to spend on a warehouse the config
    does not name. ``catalogs`` is a source allowlist (entries ``catalog`` or
    ``catalog.schema``); empty means every Unity Catalog catalog the principal
    can see except ``system``. ``dev_catalog``/``dev_schema`` are where dbt dev
    builds write; the pair is refused as a source so reads and writes never
    share a schema. ``max_full_profile_bytes`` opts large tables into sampled
    profiling (TABLESAMPLE) instead of a full scan; table sizes are not free on
    Databricks, so the threshold binds once a size is learned in-budget.
    ``dbu_price_usd`` is the contract-specific dollar price of one DBU; set it
    to see dollar figures next to the DBU translation (it varies by cloud and
    tier, so dex never guesses).
    """

    profile: str | None = None
    host: str | None = None
    warehouse: str | None = None
    catalogs: list[str] = Field(default_factory=list)
    dev_catalog: str | None = None
    dev_schema: str | None = None
    max_full_profile_bytes: int | None = None
    dbu_price_usd: float | None = None


class PostgresTarget(BaseModel):
    """Non-secret PostgreSQL connection target. Credentials are never here:
    auth is discovered at runtime by connect.py (a pg_service.conf entry,
    DATABASE_URL, PG* environment variables, or a dbt profile), and passwords
    are supplied by PGPASSWORD, ``~/.pgpass``, or the service file, never by
    this config.

    ``service`` pins a ``pg_service.conf`` entry (the ``connection_name``
    analogue); unset means DATABASE_URL, then the PG* environment, then a dbt
    profile. ``host``/``port``/``dbname``/``user`` are an optional committed
    non-secret target used only when no other source resolves. ``schemas`` is
    a source allowlist of schema names inside the connected database; empty
    means every non-system schema the role can see. ``dev_schema`` is where
    dbt dev builds write, and is refused as a source so reads and writes never
    share a schema. ``max_full_profile_bytes`` opts large tables into sampled
    profiling (TABLESAMPLE SYSTEM) instead of a full scan.
    """

    service: str | None = None
    host: str | None = None
    port: int | None = None
    dbname: str | None = None
    user: str | None = None
    schemas: list[str] = Field(default_factory=list)
    dev_schema: str | None = None
    max_full_profile_bytes: int | None = None


class RedshiftTarget(BaseModel):
    """Non-secret Amazon Redshift connection target. Credentials are never
    here: auth is discovered at runtime by connect.py (the AWS default
    credential chain for IAM temporary database credentials, REDSHIFT_*
    environment variables, or a dbt profile), and passwords or keys are never
    written or logged.

    ``workgroup`` pins the Redshift Serverless workgroup: with it set, IAM
    auth resolves the endpoint and temporary database credentials from the
    AWS credential chain, and RPU translation reads the workgroup's base
    capacity. ``cluster_identifier`` is the provisioned-cluster analogue for
    IAM auth. ``aws_profile`` pins a named ``~/.aws`` profile; unset means
    the chain's default. ``host``/``port``/``dbname``/``user`` are an
    optional committed non-secret target for native password auth (password
    supplied by REDSHIFT_PASSWORD, never by this config). ``schemas`` is a
    source allowlist of schema names inside the connected database; empty
    means every non-system schema the user can see. ``dev_schema`` is where
    dbt dev builds write, and is refused as a source so reads and writes
    never share a schema. ``rpu_price_usd`` is the region-specific dollar
    price of one RPU-hour; set it to see dollar figures next to the RPU
    translation (it varies by region and contract, so dex never guesses).
    There is no sampled-profiling threshold: Redshift has no TABLESAMPLE, so
    the budget is the only bound on profiling cost.
    """

    workgroup: str | None = None
    cluster_identifier: str | None = None
    aws_profile: str | None = None
    region: str | None = None
    host: str | None = None
    port: int | None = None
    dbname: str | None = None
    user: str | None = None
    schemas: list[str] = Field(default_factory=list)
    dev_schema: str | None = None
    rpu_price_usd: float | None = None


class QueryLimits(BaseModel):
    """Hard bounds on `explore query` results, enforced in the engine.

    The caps protect agent context from token blowups: an oversized result is
    truncated with an explicit note rather than trusted to agent frugality.
    """

    max_rows: int = 50
    max_cell_chars: int = 256
    max_payload_bytes: int = 16384
    timeout_seconds: float = 30.0


class ClusterLimits(BaseModel):
    """Bounds on `explore cluster`, enforced in the engine.

    Clustering must never load a giant table into anything: only a bounded
    sample of the feature columns is pulled into the engine process for
    scikit-learn, and only aggregates (cluster sizes and centroids) cross the
    stdout boundary. ``sample_rows`` caps how many rows the sample query fetches;
    the sample clause the engine emits is dialect-aware (TABLESAMPLE / SAMPLE /
    USING SAMPLE) so a metered warehouse scans a fraction, not the whole table.
    ``min_rows`` refuses clustering a sample too small to be meaningful.
    ``k_min``/``k_max`` bound the silhouette sweep when ``-k`` is not given;
    ``silhouette_sample`` caps the (quadratic) silhouette computation.
    ``max_features`` bounds the feature width. ``random_state`` fixes the
    scikit-learn seed, and ``sample_seed`` fixes the sample draw: both are
    needed for a reproducible run, because re-drawing the sample changes the
    answer (a different draw can change the chosen k, not just the rounding).
    Only some dialects can seed a sample; where the engine cannot, the envelope
    says the result is not reproducible rather than implying it is. Set
    ``sample_seed`` to null for a fresh draw per run.
    """

    sample_rows: int = 20000
    min_rows: int = 50
    k_min: int = 2
    k_max: int = 8
    silhouette_sample: int = 5000
    max_features: int = 20
    random_state: int = 0
    sample_seed: int | None = 0
    timeout_seconds: float = 60.0


class PIIOverride(BaseModel):
    """One reviewed column the team has decided is not PII.

    ``column`` is fully qualified (the cache's connector-normalized identifier
    plus the column name, e.g. ``MY_DB.PUBLIC.REGION.R_NAME``) so an override
    can never silently widen to a same-named column elsewhere. No wildcards: an
    override records a per-column human decision, and living in the committed
    config makes that decision reviewable in git and durable across re-profiles.
    """

    column: str
    reason: str | None = None


def pii_override_paths(overrides: list[PIIOverride]) -> set[str]:
    """Lowered fully-qualified column paths, the form the engine matches columns
    against. Case-insensitive because the connectors disagree about identifier
    case (same rationale as ``scope_within``), and a case mismatch must never
    re-block a reviewed column."""

    return {entry.column.strip().lower() for entry in overrides}


class DexConfig(BaseModel):
    """The shape of ``.dex/config.yml``: one optional target per connector plus
    the connector selection, budgets, and engine limits."""

    connector: str = "duckdb"
    duckdb: DuckDBTarget | None = None
    bigquery: BigQueryTarget | None = None
    snowflake: SnowflakeTarget | None = None
    databricks: DatabricksTarget | None = None
    postgres: PostgresTarget | None = None
    redshift: RedshiftTarget | None = None
    dbt_target: str | None = None
    # Pins the dbt project directory (relative to the repo root) when discovery
    # would be ambiguous; by default the project is located automatically.
    dbt_project_dir: str | None = None
    budget: Budget = Field(default_factory=Budget)
    ranking_hints: list[str] = Field(default_factory=list)
    query: QueryLimits = Field(default_factory=QueryLimits)
    cluster: ClusterLimits = Field(default_factory=ClusterLimits)
    # How many top-ranked objects `explore map` deep-profiles on a large
    # warehouse; the rest stay inventory-only. Selective by default, overridable.
    profile_top_n: int = 25
    # How fresh a cached profile must be to skip re-scanning it (`explore map` /
    # `explore relationships`); 0 disables reuse (always re-profile).
    profile_freshness_hours: float = 24.0
    # Columns a human has reviewed and cleared as not PII. The only way to
    # durably clear a detector flag; hand-edits to the cache are overwritten by
    # the next profile, this list is re-applied on every profile.
    pii_overrides: list[PIIOverride] = Field(default_factory=list)


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
