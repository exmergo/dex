"""Non-secret project config, read from ``.dex/config.yml``.

Config separates from secrets by construction: connector targets, the dbt target,
session budgets, and ranking hints live here and are committed to the repo.
Secrets (passwords, keys, tokens) are read at runtime from their own stores by
``connect.py`` and are never written or logged here.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from .diffs import file_diff
from .envelope import Paradigm
from .errors import ConfigurationError
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

    ``session_ceiling_declined`` records that this project was asked for a
    cumulative ceiling and chose to run without one (issue #283). It is the
    record of a decision, not a permission: it loosens nothing, every billed
    command still warns that the day's total is unbounded, and the only thing it
    changes is that the one-time ask does not fire again. It lives in the
    committed file rather than in the ``.dex/`` cache for two reasons: a
    decision that a cleared cache re-asks is not a decision, and a project
    running deliberately unbounded is exactly the kind of thing a reviewer
    should see in a diff.
    """

    paradigm: Paradigm = Paradigm.FREE_LOCAL
    ceiling: float | None = None
    session_ceiling: float | None = None
    session_ceiling_declined: bool = False


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


class ClickHouseTarget(BaseModel):
    """Non-secret ClickHouse connection target. Credentials are never here:
    auth is discovered at runtime by connect.py (CLICKHOUSE_URL, the
    CLICKHOUSE_* environment, this committed target plus CLICKHOUSE_PASSWORD,
    or a dbt profile), and passwords are never written or logged.

    ClickHouse namespaces are two-part, ``database.table``: there is no
    catalog level, and dbt-clickhouse's ``schema:`` *is* the ClickHouse
    database. So ``databases`` is the source allowlist and ``dev_database``
    is where dbt dev builds write, refused as a source so reads and writes
    never share a database.

    ``deployment`` declares which ClickHouse this is, and is a cost decision
    rather than a connection one. Self-hosted bills no currency, so dex
    guards it as database-seconds (``db_load``); ClickHouse Cloud is guarded
    in compute-seconds, with live service capacity translating those seconds
    to compute-unit-hours. ``compute_unit_price_usd`` adds the optional dollar
    translation and is refused under ``self_hosted``, where it would otherwise
    be accepted and ignored.

    ``max_full_profile_bytes`` opts large tables into sampled profiling.
    ClickHouse's ``SAMPLE`` clause only works where the table declared a
    sampling expression in its MergeTree key, which is uncommon, so the
    sampled path is ``ORDER BY rand() LIMIT n`` and is not repeatable.
    """

    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    secure: bool | None = None
    databases: list[str] = Field(default_factory=list)
    dev_database: str | None = None
    deployment: str = "self_hosted"
    compute_unit_price_usd: float | None = None
    max_full_profile_bytes: int | None = None

    @model_validator(mode="after")
    def _check_deployment(self) -> ClickHouseTarget:
        allowed = ("self_hosted", "cloud")
        if self.deployment not in allowed:
            raise ValueError(
                f"clickhouse.deployment must be one of {', '.join(allowed)}, "
                f"got '{self.deployment}'"
            )
        # Refused rather than ignored: a price per compute unit under a
        # paradigm that counts seconds would read as a configured dollar
        # translation and produce none, which is the accepted-and-ignored
        # shape this codebase refuses everywhere else.
        if self.deployment == "self_hosted" and self.compute_unit_price_usd is not None:
            raise ValueError(
                "clickhouse.compute_unit_price_usd applies to "
                "deployment: cloud, which bills compute-unit-hours. A "
                "self-hosted server bills no currency, so dex guards it in "
                "database-seconds and would never spend this number. Remove "
                "it, or set deployment: cloud"
            )
        return self


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


class MaintainConfig(BaseModel):
    """Tuning for `maintain`'s detectors.

    ``grain_min_rows``: below this row count, a lost-uniqueness finding
    (``key_lost_uniqueness`` / ``declared_grain_not_unique``) is damped to
    ``low`` rather than reported at ``high`` (issue #280). A handful of rows
    is exactly the shape where losing uniqueness means the least: a 4-row
    table with a boolean column "loses" a uniqueness it never meaningfully
    had once a fifth row repeats a value. The finding is never dropped, only
    downgraded, and the damping is named in the finding's own `data` so
    nothing about the run is silent.
    """

    grain_min_rows: int = 100


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


class ConventionWarnings(BaseModel):
    """Which house conventions ``transform plan`` reads out of the project's own
    models, and warns when an authored model breaks.

    These are the only warnings dex raises on a style judgment rather than on a
    fact, which is why they are the only ones a project can switch off. Each
    fires only where the project's own precedent is unambiguous (see
    ``transform.conventions``), so leaving them on costs a repo with no
    consistent convention nothing; turning one off is for a house that has a
    convention and has decided it is not this one.

    ``resolved_keys``: an authored model exposes a raw foreign key where its
    siblings all resolve the equivalent key to a descriptive attribute.
    """

    resolved_keys: bool = True


# Which deployments each semantic-layer vendor ships, and the spellings that name
# them. The table exists because `backend:` collapsed vendor and deployment into
# one enum, which only extends while there is exactly one vendor, and there is
# now more than one. `api` and `cloud` are released spellings of `dbt_cloud` and
# stay accepted.
SEMANTIC_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    "dbt": ("local", "dbt_cloud"),
    # Ossie is an interchange format, not a query service. Its local deployment
    # means "read the native documents in this repository", and there is no
    # hosted one to add later: a hosted Ossie would be some vendor's service
    # speaking its own protocol, which is a different vendor rather than a second
    # deployment of this one.
    "ossie": ("local",),
}

#: The project format that answers a vendor's semantic catalog, where the vendor
#: is not the project itself. A table rather than a branch, and this is the
#: mechanism that lets `semantic.vendor: ossie` sit beside `project.format: dbt`
#: without any command learning a vendor name: the engine builds the named format
#: and injects it, and the backend reads a catalog through the same seam it
#: always did. `dbt` is absent because the configured project format already
#: answers for it.
#:
#: A vendor listed here reads its format's coordinates from the `SemanticConfig`
#: field named after the vendor (`semantic.ossie` for `ossie`), which is passed
#: through to the format as its options verbatim. That convention is what keeps
#: the engine from growing a per-vendor coordinate reader.
SEMANTIC_PROJECT_FORMATS: dict[str, str] = {"ossie": "ossie"}
#: Compatibility spelling accepted only while migrating old configuration.
LEGACY_OSSIE_PROJECT_FORMAT = "ossie"
_SEMANTIC_DEPLOYMENT_SPELLINGS: dict[str, str] = {
    "local": "local",
    "dbt_cloud": "dbt_cloud",
    "api": "dbt_cloud",
    "cloud": "dbt_cloud",
}


def canonical_semantic_deployment(value: str) -> str:
    """One deployment spelling, canonicalized. Unknown values pass through so the
    validator (or the backend resolver, for a duck-typed config) can refuse them
    by name rather than silently mapping them onto something that exists."""

    key = (value or "").strip().lower()
    return _SEMANTIC_DEPLOYMENT_SPELLINGS.get(key, key)


class OssieSemanticConfig(BaseModel):
    """The repository-confined native documents behind ``vendor: ossie``.

    The same coordinates ``project.options.files`` carries for an Ossie-only
    repository, and validated the same way here, because a bad path should be
    refused where it is written rather than several frames into a command. Both
    routes build one class, so what is checked here is checked once more by the
    format itself: this one can name the config line, that one holds for a
    caller who built the coordinates some other way.
    """

    files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_files(self) -> OssieSemanticConfig:
        from .ossie.loader import DOCUMENT_SUFFIXES

        listed = ", ".join(DOCUMENT_SUFFIXES)
        for name in self.files:
            if not name.endswith(DOCUMENT_SUFFIXES):
                raise ValueError(
                    f"semantic.ossie.files: '{name}' is not a native Ossie "
                    f"document; they are named {listed}"
                )
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"semantic.ossie.files: '{name}' has to name a file inside "
                    "the repository, written relative to its root"
                )
        if duplicated := sorted({n for n in self.files if self.files.count(n) > 1}):
            raise ValueError(
                "semantic.ossie.files names the same document twice: "
                f"{', '.join(duplicated)}"
            )
        return self


class SemanticConfig(BaseModel):
    """How ``explore semantic`` reaches the semantic layer, on two axes.

    ``vendor`` is which semantic-layer format answers: ``dbt``/MetricFlow, or
    ``ossie`` for native Apache Ossie documents in the repository. It is
    ambient per repo, chosen once, the same rule ``connector:`` follows: a repo
    has one semantic layer, so there is no more reason to retype the vendor per
    command than to retype the warehouse.

    ``deployment`` is which endpoint or artifact of that vendor is read. For dbt:
    ``local`` renders metric queries with MetricFlow and executes them through
    dex's own connector and cost guard, so a dbt project must be present (like
    DuckDB needs a local file); ``dbt_cloud`` sends the query to a hosted dbt Cloud
    Semantic Layer over GraphQL and needs no local project (like BigQuery needs no
    local DuckDB). For Ossie there is one deployment, ``local``, because Ossie
    specifies an interchange document rather than a service to reach.

    ``ossie.files`` names those documents, relative to the repository root, and
    is required with ``vendor: ossie`` and refused without it. It is the
    beside-dbt route: `project.format` keeps naming dbt and the semantic
    catalog comes from Ossie. An Ossie-only repository names
    `project.format: ossie` instead and puts the same list in
    `project.options.files`, which builds the same reader.

    A third property, **who executes**, is derived from those two and never
    configured, because it is what decides whether the cost guard can apply at
    all. Each backend declares it (``execution``: ``dex`` or ``vendor``) and every
    result carries it. ``--local`` / ``--api`` override that axis for one command.

    ``backend`` is the released spelling of the two axes as one enum and is still
    accepted: ``local`` reads as dbt plus the local deployment, ``dbt_cloud`` as
    dbt plus the hosted one. Setting both is fine while they agree and refused
    when they contradict, because a config dex accepts and then ignores is worse
    than one it refuses.

    ``host`` and ``environment_id`` are the non-secret hosted coordinates, copied
    from the dbt Cloud Semantic Layer panel (or ``DBT_SL_HOST`` / ``DBT_SL_ENV_ID``
    at runtime). The service token is a secret and is never here: connect.py reads
    it from ``DBT_SL_TOKEN`` (then ``~/.dbt/dbt_cloud.yml``) at runtime.
    """

    backend: str = "local"
    vendor: str = "dbt"
    deployment: str | None = None
    host: str | None = None
    environment_id: str | None = None
    ossie: OssieSemanticConfig = Field(default_factory=OssieSemanticConfig)

    @model_validator(mode="after")
    def _check_axes(self) -> SemanticConfig:
        vendor = (self.vendor or "").strip().lower()
        if vendor not in SEMANTIC_DEPLOYMENTS:
            raise ValueError(
                f"semantic.vendor must be one of "
                f"{', '.join(sorted(SEMANTIC_DEPLOYMENTS))}, got '{self.vendor}'"
            )
        allowed = SEMANTIC_DEPLOYMENTS[vendor]

        from_backend = canonical_semantic_deployment(self.backend)
        explicit_backend = "backend" in self.model_fields_set
        if explicit_backend and from_backend not in allowed:
            raise ValueError(
                f"semantic.backend '{self.backend}' names no deployment of vendor "
                f"'{vendor}'; use one of {', '.join(allowed)}"
            )

        if self.deployment is None:
            deployment = from_backend
        else:
            deployment = canonical_semantic_deployment(self.deployment)
            if deployment not in allowed:
                raise ValueError(
                    f"semantic.deployment must be one of {', '.join(allowed)} for "
                    f"vendor '{vendor}', got '{self.deployment}'"
                )
            # Refused rather than resolved by precedence: the two keys are two
            # spellings of one choice, and picking a winner would leave the other
            # accepted and ignored, which reads as a setting that took effect.
            if explicit_backend and deployment != from_backend:
                raise ValueError(
                    f"semantic.backend '{self.backend}' and semantic.deployment "
                    f"'{self.deployment}' name different deployments. They are two "
                    "spellings of one choice; keep whichever you prefer and remove "
                    "the other"
                )

        # Written through ``object.__setattr__`` so the derived values do not join
        # ``model_fields_set``: ``save_config`` dumps with ``exclude_unset``, and a
        # derived default written back into the committed file would read as a
        # choice the author made. ``backend`` is kept consistent with the canonical
        # deployment rather than left stale, so either spelling answers correctly.
        object.__setattr__(self, "vendor", vendor)
        object.__setattr__(self, "deployment", deployment)
        object.__setattr__(self, "backend", deployment)
        # A vendor whose catalog comes from a project format carries its
        # coordinates in the section named after it, and those coordinates mean
        # nothing under any other vendor. Driven off the table rather than
        # written per vendor, so the next one is a row rather than three more
        # conditions here.
        for named, section in _vendor_sections(self):
            if named == vendor and not section.files:
                raise ValueError(
                    f"semantic.vendor: {named} needs semantic.{named}.files: "
                    "the native documents to read, named relative to the "
                    "repository root"
                )
            if named != vendor and section.files:
                raise ValueError(
                    f"semantic.{named}.files is only valid for semantic.vendor: {named}"
                )
        # `host` and `environment_id` reach a hosted service, so they are
        # meaningless for a vendor with no hosted deployment. Read off the
        # deployment table rather than named, for the same reason.
        if "dbt_cloud" not in allowed and (self.host or self.environment_id):
            raise ValueError(
                "semantic.host and semantic.environment_id are the hosted dbt "
                f"Cloud coordinates, and semantic.vendor: {vendor} has no "
                f"hosted deployment (it offers {', '.join(allowed)})"
            )
        return self


def _vendor_sections(semantic: SemanticConfig) -> list[tuple[str, Any]]:
    """Every ``(vendor, its config section)`` pair a project format reads.

    The section is the `SemanticConfig` field named after the vendor, which is
    the convention `SEMANTIC_PROJECT_FORMATS` documents and the engine relies on
    when it passes a section through as a format's options.
    """

    pairs = []
    for named in SEMANTIC_PROJECT_FORMATS:
        section = getattr(semantic, named, None)
        if section is not None:
            pairs.append((named, section))
    return pairs


class CacheConfig(BaseModel):
    """Where dex keeps its scratch state, and how to reach it.

    Scratch state only: the exploration cache, the reconcile baseline, the last
    drift report, the two ledgers, and the stored transform plans. The dbt project
    is the source of truth, it is a git-reviewable filesystem artifact by design,
    and no setting here moves it.

    ``backend`` is an open registry rather than a closed set. It accepts a name
    dex ships (``filesystem``, the default, which writes the loose JSON under
    `.dex/` that a reviewer reads in a pull request; or ``sqlite``, which writes
    one `.dex/dex.db` file instead and is not git-reviewable, for a host that
    wants durable state without scattering JSON files), a dotted
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

    # Where this config was read from, stamped by `load_config` and absent on one
    # a host built itself. Private, so it never joins a dump and can never be
    # written back into the file it names.
    #
    # It exists for the one-time cumulative-ceiling ask (issue #283), which needs
    # to know not merely that a config file sits at the repo root but that *this*
    # config is that file: a host holding its own object has already made the
    # budget decisions the ask would go looking for, and amending a file whose
    # settings are not the ones in play would record a decision about the wrong
    # project's budget. See `session_ceiling_undecided`.
    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        """The file this config was loaded from, or None if it was not loaded."""

        return self._source_path

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
    clickhouse: ClickHouseTarget | None = None
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
    # Which house-convention warnings `transform plan` raises (issue #223). All
    # on by default: each stays silent unless the project's own models agree
    # unanimously, so a repo with no convention never hears from them.
    conventions: ConventionWarnings = Field(default_factory=ConventionWarnings)
    query: QueryLimits = Field(default_factory=QueryLimits)
    cluster: ClusterLimits = Field(default_factory=ClusterLimits)
    maintain: MaintainConfig = Field(default_factory=MaintainConfig)
    # How many top-ranked objects `explore map` deep-profiles on a large
    # warehouse; the rest stay inventory-only. Selective by default, overridable.
    profile_top_n: int = 25
    # How fresh a cached profile must be to skip re-scanning it (`explore map` /
    # `explore relationships`); 0 disables reuse (always re-profile).
    profile_freshness_hours: float = 24.0
    # How many values `explore profile` serializes for a column's value domain
    # (issue #290): the most frequent ones, with the rest folded into that
    # domain's `elided` count. The default equals the probe's own cap
    # (`adapters.base.VALUE_DOMAIN_CAP`), so nothing is cut unless a repo asks;
    # lowering it trims the payload and never what is probed or cached.
    profile_value_domain_cap: int = 25
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


def config_path(repo_root: Path | str = ".") -> Path | None:
    """The committed config file at ``repo_root``, or None when there is none.

    ``load_config`` answers the same question by returning None, but a caller
    that means to *amend* the file needs the path without parsing it, and needs
    to tell "no file" apart from "a file that parsed to nothing".
    """

    path = Path(repo_root) / DEX_DIR / CONFIG_FILE
    return path if path.is_file() else None


def record_session_ceiling_decision(
    repo_root: Path | str,
    *,
    session_ceiling: float | None = None,
    declined: bool = False,
) -> tuple[DexConfig, dict[str, Any]]:
    """Write the cumulative-ceiling decision into ``.dex/config.yml``.

    Returns the amended config and the file diff, because a write dex performed
    on the caller's behalf has to be visible: the envelope carries the diff the
    way ``transform init`` carries the files it created, so the amendment is
    reviewable rather than something a caller discovers in ``git status``.

    Re-read from disk rather than amended in memory on purpose. The config the
    engine is holding may have been overridden for this run (``--connector``, an
    injected object), and writing that back would commit a one-run override as a
    project setting. Only the budget field the decision names is touched.
    """

    if (session_ceiling is None) == (not declined):
        raise ConfigurationError(
            "a cumulative-ceiling decision is either a ceiling or a decline, "
            "not both and not neither: pass --session-ceiling <value> or "
            "--no-session-ceiling"
        )
    if session_ceiling is not None and session_ceiling <= 0:
        raise ConfigurationError(
            "--session-ceiling must be a positive magnitude, got "
            f"{session_ceiling}; to run without a cumulative ceiling pass "
            "--no-session-ceiling instead, which records that choice"
        )

    root = Path(repo_root)
    path = config_path(root)
    if path is None:
        raise ConfigurationError(
            f"no {DEX_DIR}/{CONFIG_FILE} at '{root}' to record a "
            "cumulative-ceiling decision in; commit a config for this project "
            "first (`dex transform init` writes one), or pass --budget alone "
            "for an ad-hoc read"
        )
    old = path.read_text(encoding="utf-8")
    config = DexConfig.model_validate(yaml.safe_load(old) or {})
    if declined:
        config.budget.session_ceiling = None
        config.budget.session_ceiling_declined = True
        config.budget.model_fields_set.discard("session_ceiling")
    else:
        config.budget.session_ceiling = session_ceiling
        # A ceiling supersedes an earlier decline, and once it does, the flag is
        # noise in a committed file: dropped rather than written as an explicit
        # `false` that reads like a second setting.
        config.budget.session_ceiling_declined = False
        config.budget.model_fields_set.discard("session_ceiling_declined")
    # `budget` itself has to join `model_fields_set`, or `save_config`'s
    # `exclude_unset` dump drops the whole block a nested assignment landed in.
    config.budget = config.budget
    save_config(config, root)
    return config, file_diff(
        f"{DEX_DIR}/{CONFIG_FILE}", old, path.read_text(encoding="utf-8")
    )


def load_config(repo_root: Path | str = ".") -> DexConfig | None:
    path = Path(repo_root) / DEX_DIR / CONFIG_FILE
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = DexConfig.model_validate(raw)
    # Stamped here and nowhere else: this is the only function that turns a file
    # into a config, so it is the only one that can honestly say a config came
    # from one. See `DexConfig.source_path`.
    config._source_path = path
    return config


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
