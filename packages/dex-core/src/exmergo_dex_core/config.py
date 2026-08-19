"""Non-secret project config, read from ``.dex/config.yml``.

Config separates from secrets by construction: connector targets, the dbt target,
session budgets, and ranking hints live here and are committed to the repo.
Secrets (passwords, keys, tokens) are read at runtime from their own stores by
``connect.py`` and are never written or logged here.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .envelope import Paradigm
from .storage import DEX_DIR

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

    ``max_rows`` and ``max_cell_chars`` bound one statement. ``max_payload_bytes``
    bounds the whole call, because a call answering several statements can flood
    context with results that are each individually small; the budget is spent in
    statement order and what one statement leaves unspent the next may use.
    ``max_statements`` bounds how many a single call may carry at all.
    """

    max_rows: int = 50
    max_cell_chars: int = 256
    max_payload_bytes: int = 16384
    max_statements: int = 10
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
    """One reviewed column, or a reviewed *class* of structurally identical
    columns, the team has decided is not PII.

    Two mutually exclusive shapes:

    - Exact (default): ``column`` is fully qualified (the cache's
      connector-normalized identifier plus the column name, e.g.
      ``MY_DB.PUBLIC.REGION.R_NAME``) so the override can never silently widen
      to a same-named column elsewhere. This is a per-column human decision.
    - Pattern (opt-in): ``column_name`` + ``scope`` clears every column named
      ``column_name`` on any table whose fully-qualified identifier matches
      the ``scope`` glob (``*`` wildcards, case-insensitive). For the
      Firestore/Mongo/DynamoDB-export case, where the same structurally
      identical column exists by construction on every entity's table in
      every environment mirror, so the exact form would otherwise cost one
      entry per table per environment for a single human decision.

    Either shape lives in the committed config, so the decision stays
    reviewable in git and durable across re-profiles.
    """

    column: str | None = None
    column_name: str | None = None
    scope: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> PIIOverride:
        is_exact = self.column is not None
        is_pattern = self.column_name is not None or self.scope is not None
        if is_exact and is_pattern:
            raise ValueError(
                "pii_overrides entry must be either 'column' (exact) or "
                "'column_name' + 'scope' (pattern), not both"
            )
        if not is_exact and not is_pattern:
            raise ValueError(
                "pii_overrides entry needs either 'column' (exact) or "
                "'column_name' + 'scope' (pattern)"
            )
        if is_pattern and (self.column_name is None or self.scope is None):
            raise ValueError(
                "pattern-form pii_overrides entry needs both 'column_name' and 'scope'"
            )
        return self


class PIIOverrideMatcher:
    """Resolves ``pii_overrides`` against a fully-qualified ``identifier.column``
    path. Exact entries match verbatim; pattern entries (``column_name`` +
    ``scope``) match any column of that name in any dataset whose identifier
    matches the ``scope`` glob. Duck-types the exact-match set every call site
    already used (``path in matcher``, ``if not matcher``), so pattern support
    needs no change at the match sites in ``explore/profile.py`` or
    ``maintain/reconcile.py``."""

    def __init__(self, overrides: list[PIIOverride]):
        self._exact = {
            entry.column.strip().lower() for entry in overrides if entry.column
        }
        self._patterns = [
            (entry.column_name.strip().lower(), entry.scope.strip().lower())
            for entry in overrides
            if entry.column_name
        ]

    def __contains__(self, path: str) -> bool:
        path = path.strip().lower()
        if path in self._exact:
            return True
        table, _, column = path.rpartition(".")
        return any(
            column == name and fnmatch.fnmatchcase(table, scope)
            for name, scope in self._patterns
        )

    def __bool__(self) -> bool:
        return bool(self._exact or self._patterns)


def pii_override_paths(overrides: list[PIIOverride]) -> PIIOverrideMatcher:
    """The form the engine matches columns against: lowered exact paths plus
    any pattern entries, case-insensitive because the connectors disagree
    about identifier case (same rationale as ``scope_within``), and a case
    mismatch must never re-block a reviewed column."""

    return PIIOverrideMatcher(overrides)


class BlobOverride(BaseModel):
    """One reviewed blob-type column a human wants profiled despite the default
    exclusion (see ``adapters.base.is_blob_type``). ``column`` is fully
    qualified, same shape and rationale as :class:`PIIOverride`: a per-column
    human decision, durable and reviewable in git, never a wildcard."""

    column: str
    reason: str | None = None


def blob_override_paths(overrides: list[BlobOverride]) -> set[str]:
    """Lowered fully-qualified column paths a human has opted back into
    profiling despite being blob-typed. Same matching rules as
    ``pii_override_paths``."""

    return {entry.column.strip().lower() for entry in overrides}


class EntityAffixes(BaseModel):
    """Table-name prefixes and suffixes entity matching strips as a
    lower-confidence fallback, tried only once an exact entity-name match
    fails (see ``explore.relationships._match_parent``).

    These are the default output of CDC history modes, landing-zone
    conventions (``_data``/``_raw``/``_stg``), and layered-warehouse naming
    (``stg_``/``dim_``/``fct_``), not exotic house choices, but house
    conventions still vary enough that the list is overridable. Deliberately
    small by default: an ordered set covers the common cases without turning
    entity matching into a grab-bag of guesses. A trailing version marker
    (``_v2``, ``_v3``, ...) is always stripped and is not part of this list,
    since it is a structural convention rather than a house-specific word.
    """

    prefixes: list[str] = Field(
        default_factory=lambda: [
            "stg",
            "src",
            "raw",
            "dim",
            "fct",
            "fact",
            "int",
            "base",
        ]
    )
    suffixes: list[str] = Field(
        default_factory=lambda: ["history", "data", "raw", "snapshot", "current"]
    )


class SemanticConfig(BaseModel):
    """How ``explore semantic`` reaches the semantic layer.

    Two backends, selected by ``backend`` and overridable per command with
    ``--local`` / ``--api``. ``local`` renders metric queries with MetricFlow and
    executes them through dex's own connector and cost guard, so a dbt project
    must be present (like DuckDB needs a local file). ``dbt_cloud`` sends the
    query to a hosted dbt Cloud Semantic Layer over GraphQL and needs no local
    project (like BigQuery needs no local DuckDB); dbt Cloud owns the warehouse
    connection and executes server-side, so **dex's cost guard does not apply on
    that path** (the dbt Cloud environment governs spend, and every hosted result
    says so).

    ``host`` and ``environment_id`` are the non-secret hosted coordinates, copied
    from the dbt Cloud Semantic Layer panel (or ``DBT_SL_HOST`` / ``DBT_SL_ENV_ID``
    at runtime). The service token is a secret and is never here: connect.py reads
    it from ``DBT_SL_TOKEN`` (then ``~/.dbt/dbt_cloud.yml``) at runtime.
    """

    backend: str = "local"
    host: str | None = None
    environment_id: str | None = None


class CacheConfig(BaseModel):
    """Where dex keeps its scratch state, and how to reach it.

    Scratch state only: the exploration cache, the reconcile baseline, the last
    drift report, the two ledgers, and the stored transform plans. The dbt project
    is the source of truth, it is a git-reviewable filesystem artifact by design,
    and no setting here moves it.

    ``backend`` is an open registry rather than a closed set. It accepts a name
    dex ships (``filesystem``, the default, which writes the loose JSON under
    `.dex/` that a reviewer reads in a pull request), a dotted
    ``mypkg.stores:my_store`` path, or a name an installed distribution registered
    under the ``exmergo_dex_core.stores`` entry-point group. A backend published
    as its own package is therefore selectable without a change to dex.
    ``--cache-backend`` overrides it for one run, the way ``--connector``
    overrides the configured connector. Naming a *different* backend that way
    leaves ``options`` behind, because they are not namespaced by backend and one
    backend's coordinates are not another's.

    ``options`` reaches the selected backend's factory verbatim: dex does not
    interpret it, so the keys belong to the backend and the backend validates
    them. See ``references/storage.md``.

    **Credentials are never here.** This file is committed, so a password, key,
    token, or connection string among the options would be a secret in version
    control. A backend needing one reads it at runtime the way ``connect.py``
    does, or the host builds the store itself and passes it to ``DexEngine``,
    which always wins over anything named here.
    """

    backend: str = "filesystem"
    options: dict[str, Any] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    """Which format owns the source of truth, and how to reach it.

    The project is what declares intent: which models exist, at what grain, how
    they join, what the metrics mean. dex reads it and reasons over it. Today one
    format ships, and ``dbt`` is the default, so a repo that selects nothing
    behaves exactly as it did before this setting existed.

    ``format`` is an open registry rather than a closed set. It accepts a name dex
    ships (``dbt``), a dotted ``mypkg.projects:my_project`` path, or a name an
    installed distribution registered under the ``exmergo_dex_core.projects``
    entry-point group. A format published as its own package is therefore
    selectable without a change to dex, which is the only door open to a host that
    reaches dex as a subprocess and cannot pass an object.
    ``--project-format`` overrides it for one run, the way ``--connector``
    overrides the configured connector. Naming a *different* format that way
    leaves ``options`` behind, because they are not namespaced by format and one
    format's coordinates are not another's.

    ``options`` reaches the selected format's factory verbatim: dex does not
    interpret it, so the keys belong to the format and the format validates
    them. The shipped dbt format takes none; its one coordinate is
    ``dbt_project_dir`` below, which has its own slot because it predates this
    setting and because pinning a directory within the repository is the one
    thing a directory-keyed format needs from configuration.
    See ``references/project.md``.

    **Credentials are never here.** This file is committed, so a token or
    connection string among the options would be a secret in version control. A
    format needing one reads it at runtime the way ``connect.py`` does, or the
    host builds the project itself and passes it to ``DexEngine``, which always
    wins over anything named here.
    """

    format: str = "dbt"
    options: dict[str, Any] = Field(default_factory=dict)


class DexConfig(BaseModel):
    """The shape of ``.dex/config.yml``: one optional target per connector plus
    the connector selection, budgets, and engine limits."""

    # The DuckDB on-ramp: a config that omits `connector:` (or a bare `--path`
    # read with no config) means the free local connector. This default only
    # applies to a config that actually exists or an explicit `--path`; it is NOT
    # a fallback for a missing config. `open_adapter` refuses when no config
    # resolves and nothing explicit is given, rather than fabricating a duckdb
    # target, so this default can never stand in for a config that was not found.
    connector: str = "duckdb"
    duckdb: DuckDBTarget | None = None
    bigquery: BigQueryTarget | None = None
    snowflake: SnowflakeTarget | None = None
    databricks: DatabricksTarget | None = None
    postgres: PostgresTarget | None = None
    redshift: RedshiftTarget | None = None
    dbt_target: str | None = None
    # Which format owns the source of truth. Defaults to dbt, so a repo that
    # selects nothing behaves exactly as it did before this setting existed.
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    # Pins the dbt project directory (relative to the repo root) when discovery
    # would be ambiguous; by default the project is located automatically. Stays
    # a top-level key rather than moving under `project.options`: it predates the
    # format setting, it is read by `transform build` (which pins its subprocess
    # cwd to it) as well as by the project seam, and moving a released key to win
    # a tidier shape would cost a deprecation for no reader's benefit.
    dbt_project_dir: str | None = None
    # How `explore semantic` reaches the semantic layer (local MetricFlow vs a
    # hosted dbt Cloud deployment). Defaults to local; a bare project queries the
    # dbt project it lives in.
    semantic: SemanticConfig = Field(default_factory=SemanticConfig)
    # Where the non-canonical `.dex/` scratch state lives. Defaults to the loose
    # JSON under `.dex/`, so a repo that selects nothing behaves exactly as it did
    # before this setting existed.
    cache: CacheConfig = Field(default_factory=CacheConfig)
    budget: Budget = Field(default_factory=Budget)
    ranking_hints: list[str] = Field(default_factory=list)
    # Affixes entity matching strips as a lower-confidence fallback when an
    # exact entity name misses (issue #208). The default list is small on
    # purpose; house-specific conventions (a shop's own `_bak`/`ods_`) are the
    # reason this is overridable rather than fixed.
    entity_affixes: EntityAffixes = Field(default_factory=EntityAffixes)
    query: QueryLimits = Field(default_factory=QueryLimits)
    cluster: ClusterLimits = Field(default_factory=ClusterLimits)
    # How many top-ranked objects `explore map` deep-profiles on a large
    # warehouse; the rest stay inventory-only. Selective by default, overridable.
    profile_top_n: int = 25
    # How fresh a cached profile must be to skip re-scanning it (`explore map` /
    # `explore relationships`); 0 disables reuse (always re-profile).
    profile_freshness_hours: float = 24.0
    # Whether `explore query` and `explore cluster` may profile an object the
    # connection has but the cache cannot speak for, instead of refusing. Priced
    # and disclosed when it happens. Top-level rather than under `query:` because
    # both commands honor it, and it governs profiling rather than result shape.
    # Set false for the strict prerequisite (`--no-auto-profile` per command).
    auto_profile: bool = True
    # Columns a human has reviewed and cleared as not PII. The only way to
    # durably clear a detector flag; hand-edits to the cache are overwritten by
    # the next profile, this list is re-applied on every profile.
    pii_overrides: list[PIIOverride] = Field(default_factory=list)
    # Blob-type columns (BYTES/BLOB/bytea/BINARY, scalar or repeated) a human has
    # reviewed and wants profiled despite the default exclusion. Re-applied on
    # every profile, same durability rationale as pii_overrides.
    blob_overrides: list[BlobOverride] = Field(default_factory=list)


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
