"""dbt project bootstrap: the deterministic skeleton behind `transform init`.

Bootstrap is engine work, not agent freehand, because the generated
``profiles.yml`` is safety-relevant: it defines the build targets, and the
dev-target-only invariant depends on its shape. Engine-owned init guarantees a
single ``dev`` default, never a prod-named target, and never a persisted secret,
identically on every agent surface.

Init is strictly additive: it refuses to run where any dbt project already
exists, so propose-don't-impose holds by construction. And unlike the read-only
commands, it never falls back to a default connector: the connector is baked
into the generated profile's ``type:``, and a silent DuckDB default in a
warehouse shop would produce a project that parses and builds locally yet is
wrong. The caller must resolve the connector explicitly before calling in.

Per-connector profile rendering sits behind a small dispatch so cloud renderers
can slot in alongside their dbt adapters without touching the command; every
shipped connector renders.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..cache import DEX_DIR
from ..config import (
    CONFIG_FILE,
    BigQueryTarget,
    DatabricksTarget,
    DexConfig,
    DuckDBTarget,
    SnowflakeTarget,
    load_config,
    save_config,
)
from ..dbt_project import PROFILES_FILE, PROJECT_FILE, discover_projects
from ..diffs import file_diff

VALID_CONNECTORS = ("duckdb", "snowflake", "bigquery", "databricks", "postgres")


class InitError(Exception):
    pass


class InitResult(BaseModel):
    project_name: str
    # Relative to the repo root; also what gets pinned as dbt_project_dir.
    project_dir: str
    connector: str
    created: list[str] = Field(default_factory=list)
    diffs: list[dict[str, Any]] = Field(default_factory=list)


def sanitize_project_name(raw: str) -> str:
    """Reduce free-form input to a valid dbt project name (lowercase,
    underscores, no leading digit)."""

    name = re.sub(r"[^a-z0-9_]+", "_", (raw or "").lower()).strip("_")
    if not name:
        raise InitError(
            "transform init needs a project name, e.g. `transform init analytics`"
        )
    if name[0].isdigit():
        name = f"_{name}"
    return name


def init_project(
    name: str,
    connector: str,
    *,
    path: str | None = None,
    repo_root: Path | str = ".",
) -> InitResult:
    """Render a fresh dbt project skeleton and record the choices in config.

    Creates ``<name>/dbt_project.yml``, ``models/staging/`` and ``models/marts/``
    (kept non-empty with ``.gitkeep``), and a project-local ``profiles.yml`` with
    a single ``dev`` target; then writes ``connector``, ``dbt_project_dir``, and
    ``dbt_target: dev`` back to ``.dex/config.yml`` so the choice is explicit
    once and ambient for every later command. Everything written is returned as
    reviewable diffs.
    """

    root = Path(repo_root)
    if connector not in VALID_CONNECTORS:
        raise InitError(
            f"unknown connector '{connector}'; valid connectors: "
            + ", ".join(VALID_CONNECTORS)
        )
    render_profile = _PROFILE_RENDERERS.get(connector)
    if render_profile is None:
        raise InitError(
            f"connector '{connector}' is not yet supported for init; run "
            "`transform init <name> --connector "
            "duckdb|bigquery|snowflake|databricks|postgres`"
        )

    project_name = sanitize_project_name(name)
    existing = discover_projects(root)
    if existing:
        raise InitError(
            "a dbt project already exists ("
            + ", ".join(str(p / PROJECT_FILE) for p in existing)
            + "); init is strictly additive and never touches an existing project"
        )

    config = load_config(root) or DexConfig()
    profiles_text = render_profile(project_name, path, config, root)

    files: dict[str, str] = {
        f"{project_name}/{PROJECT_FILE}": _project_yaml(project_name),
        f"{project_name}/{PROFILES_FILE}": profiles_text,
        f"{project_name}/models/staging/.gitkeep": "",
        f"{project_name}/models/marts/.gitkeep": "",
    }
    collisions = sorted(rel for rel in files if (root / rel).exists())
    if collisions:
        raise InitError(
            "refusing to overwrite existing files: "
            + ", ".join(collisions)
            + "; init only creates, never replaces"
        )

    config.connector = connector
    config.dbt_project_dir = project_name
    config.dbt_target = "dev"

    config_rel = f"{DEX_DIR}/{CONFIG_FILE}"
    config_path = root / config_rel
    old_config = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    )

    diffs = [file_diff(rel, None, content) for rel, content in files.items()]
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    save_config(config, root)
    diffs.append(
        file_diff(config_rel, old_config, config_path.read_text(encoding="utf-8"))
    )

    created = list(files) + ([config_rel] if old_config is None else [])
    return InitResult(
        project_name=project_name,
        project_dir=project_name,
        connector=connector,
        created=created,
        diffs=diffs,
    )


# --- per-connector profile renderers -------------------------------------------


def _duckdb_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-duckdb ``dev`` target wired to the warehouse dex already
    knows. Also records the resolved path in ``config.duckdb`` so later commands
    inherit it."""

    raw = path or (config.duckdb.path if config.duckdb else None)
    if not raw:
        raise InitError(
            "no DuckDB warehouse path to wire the dev target to: pass --path "
            "<warehouse.duckdb> or set duckdb.path in .dex/config.yml "
            "(`explore map --path ...` is the usual first step)"
        )
    warehouse = Path(raw)
    if not warehouse.is_absolute():
        # profiles.yml paths resolve against dbt's working directory, which is
        # not the repo root; an absolute path keeps the target unambiguous.
        warehouse = (root / warehouse).resolve()
    config.duckdb = DuckDBTarget(path=str(warehouse))
    return yaml.safe_dump(
        {
            project_name: {
                "target": "dev",
                "outputs": {"dev": {"type": "duckdb", "path": str(warehouse)}},
            }
        },
        sort_keys=False,
    )


_DEFAULT_BQ_DEV_DATASET = "dbt_dev"


def _bigquery_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-bigquery ``dev`` target authenticated by ADC (method:
    oauth), so no secret is ever rendered. Writes go to a dedicated dev
    dataset, never to the source datasets dex reads from."""

    target = config.bigquery or BigQueryTarget()
    gcp_project = _resolve_init_project(target, root)
    if not gcp_project:
        raise InitError(
            "no GCP project to wire the dev target to: set bigquery.project in "
            ".dex/config.yml, or run `gcloud config set project <id>` (dex "
            "discovers Application Default Credentials; it never asks for keys)"
        )
    dev_dataset = target.dev_dataset or _DEFAULT_BQ_DEV_DATASET
    source_datasets = {entry.split(".")[-1] for entry in target.datasets}
    if dev_dataset in source_datasets:
        raise InitError(
            f"dev_dataset '{dev_dataset}' is also a source dataset in "
            "bigquery.datasets; dbt builds must write to a dataset dex does "
            "not read from (set bigquery.dev_dataset to a dedicated one)"
        )

    target.project = gcp_project
    target.dev_dataset = dev_dataset
    config.bigquery = target

    output: dict[str, Any] = {
        "type": "bigquery",
        "method": "oauth",
        "project": gcp_project,
        "dataset": dev_dataset,
        "threads": 4,
        "priority": "interactive",
    }
    if target.location:
        output["location"] = target.location
    if config.budget.ceiling is not None:
        # Server-side cap per statement dbt runs; the engine cannot dry-run a
        # dbt build, so this is the binding cost control for `transform build`.
        output["maximum_bytes_billed"] = int(config.budget.ceiling)
    return yaml.safe_dump(
        {project_name: {"target": "dev", "outputs": {"dev": output}}},
        sort_keys=False,
    )


def _resolve_init_project(target: BigQueryTarget, root: Path) -> str | None:
    """The connect-time resolution chain, minus the hard ADC requirement: init
    only renders config, so absent credentials degrade to "not found" rather
    than an auth error."""

    from ..connect import (
        CredentialDiscoveryError,
        _default_credentials,
        resolve_bigquery_project,
    )

    try:
        _credentials, adc_project, _principal = _default_credentials()
    except CredentialDiscoveryError:
        adc_project = None
    return resolve_bigquery_project(target, os.environ, adc_project, repo_root=root)


_DEFAULT_SF_DEV_SCHEMA = "DBT_DEV"


def _snowflake_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-snowflake ``dev`` target from the discovered connection,
    with no secret ever rendered: key-pair auth becomes a ``private_key_path``
    (a path, not a key), SSO stays ``externalbrowser``, and a password-based
    connection renders an ``env_var`` reference. Writes go to a dedicated dev
    database.schema, never to a source scope, on the pinned warehouse only."""

    from ..connect import CredentialDiscoveryError, resolve_snowflake_connection

    target = config.snowflake or SnowflakeTarget()
    try:
        params, _method = resolve_snowflake_connection(target, os.environ, root)
    except CredentialDiscoveryError as exc:
        raise InitError(str(exc)) from exc

    if not target.warehouse:
        raise InitError(
            "no warehouse pinned: set snowflake.warehouse in .dex/config.yml "
            "(dbt builds run only on the pinned warehouse, never a "
            "connection default)"
        )
    dev_database = target.dev_database or params.get("database")
    if not dev_database:
        raise InitError(
            "no dev database to write to: set snowflake.dev_database in "
            ".dex/config.yml (a scratch database the role can write; source "
            "databases stay read-only)"
        )
    dev_schema = target.dev_schema or _DEFAULT_SF_DEV_SCHEMA
    dev_scope = f"{dev_database}.{dev_schema}".upper()
    source_scopes = {entry.upper() for entry in target.databases}
    if dev_scope in source_scopes:
        raise InitError(
            f"dev target '{dev_scope}' is also a source scope in "
            "snowflake.databases; dbt builds must write where dex does not "
            "read (set snowflake.dev_database/dev_schema to a dedicated pair)"
        )

    target.dev_database = str(dev_database)
    target.dev_schema = dev_schema
    config.snowflake = target

    output: dict[str, Any] = {
        "type": "snowflake",
        "account": str(params["account"]),
        "user": str(params.get("user", "")),
        "role": str(params["role"]) if params.get("role") else None,
        "warehouse": target.warehouse,
        "database": str(dev_database),
        "schema": dev_schema,
        # One thread keeps the smallest warehouse from parallel bursts; the
        # per-model runtime is the guarded quantity on compute-time billing.
        "threads": 1,
        "query_tag": "dex",
    }
    output = {k: v for k, v in output.items() if v is not None}

    private_key = params.get("private_key_file") or params.get("private_key_path")
    authenticator = str(params.get("authenticator", "")).upper()
    if private_key:
        output["private_key_path"] = str(private_key)
    elif authenticator == "EXTERNALBROWSER":
        output["authenticator"] = "externalbrowser"
    elif authenticator == "WORKLOAD_IDENTITY" or (
        params.get("token") and not params.get("password")
    ):
        # Stable dbt-snowflake cannot authenticate via workload identity or a
        # raw OIDC token (support is upstream but unreleased), so a rendered
        # profile would fail every build with an opaque auth error. Refuse
        # with the working alternatives instead.
        raise InitError(
            "the discovered Snowflake connection authenticates via workload "
            "identity, which dbt-snowflake does not support yet; for dbt "
            "builds use a key-pair or SSO connection (snow connection add "
            "with --private-key-file or --authenticator externalbrowser) and "
            "pin it via snowflake.connection_name in .dex/config.yml"
        )
    else:
        # Never persist a password or token: the profile reads it from the
        # environment at dbt runtime instead (a Jinja reference, not a value).
        output["password"] = "{{ env_var('SNOWFLAKE_PASSWORD') }}"  # noqa: S105
    return yaml.safe_dump(
        {project_name: {"target": "dev", "outputs": {"dev": output}}},
        sort_keys=False,
    )


_DEFAULT_DBX_DEV_SCHEMA = "dbt_dev"


def _databricks_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-databricks ``dev`` target from the discovered connection,
    with no secret ever rendered: a user OAuth connection stays
    ``auth_type: oauth`` (dbt runs its own browser flow against the same
    workspace), OAuth M2M renders the client ID with the secret as an
    ``env_var`` reference, and a token-based connection (a PAT, or CI's
    exchanged OIDC token) reads ``DATABRICKS_TOKEN`` at runtime. Writes go to
    a dedicated dev catalog.schema, never to a source scope, on the pinned
    warehouse only."""

    from ..connect import (
        CredentialDiscoveryError,
        _databricks_hostname,
        resolve_databricks_connection,
    )

    target = config.databricks or DatabricksTarget()
    try:
        sdk_config, method = resolve_databricks_connection(target, os.environ, root)
    except CredentialDiscoveryError as exc:
        raise InitError(str(exc)) from exc

    if not target.warehouse:
        raise InitError(
            "no warehouse pinned: set databricks.warehouse in .dex/config.yml "
            "(dbt builds run only on the pinned SQL warehouse)"
        )
    if not target.dev_catalog:
        raise InitError(
            "no dev catalog to write to: set databricks.dev_catalog in "
            ".dex/config.yml (a catalog the principal can write; source "
            "catalogs stay read-only)"
        )
    dev_schema = target.dev_schema or _DEFAULT_DBX_DEV_SCHEMA
    dev_catalog = str(target.dev_catalog).lower()
    dev_scope = f"{dev_catalog}.{dev_schema.lower()}"
    source_scopes = {entry.lower() for entry in target.catalogs}
    if dev_scope in source_scopes or dev_catalog in source_scopes:
        raise InitError(
            f"dev target '{dev_scope}' overlaps a source scope in "
            "databricks.catalogs; dbt builds must write where dex does not "
            "read (set databricks.dev_catalog/dev_schema to a dedicated pair)"
        )

    target.dev_catalog = dev_catalog
    target.dev_schema = dev_schema
    config.databricks = target

    from ..adapters.databricks import warehouse_http_path

    output: dict[str, Any] = {
        "type": "databricks",
        "host": _databricks_hostname(sdk_config.host),
        "http_path": warehouse_http_path(str(target.warehouse)),
        "catalog": dev_catalog,
        "schema": dev_schema,
        # One thread keeps the pinned warehouse from parallel bursts; the
        # per-model runtime is the guarded quantity on compute-time billing.
        "threads": 1,
        # dbt-databricks parses this as a JSON object of tag key-values.
        "query_tags": '{"application": "dex"}',
    }
    if config.budget.ceiling is not None:
        # Server-side cap per statement dbt runs; the engine cannot dry-run a
        # dbt build, so this is the binding cost control for `transform build`
        # (the maximum_bytes_billed analogue, in warehouse-seconds).
        output["session_properties"] = {
            "STATEMENT_TIMEOUT": max(int(config.budget.ceiling), 1)
        }

    kind = method.rsplit(":", 1)[-1]
    if kind == "oauth_user":
        output["auth_type"] = "oauth"
    elif kind == "oauth_m2m":
        # The client ID is an identifier; the secret stays an env reference,
        # read at dbt runtime, never a rendered value.
        output["auth_type"] = "oauth"
        client_id = getattr(sdk_config, "client_id", None)
        if client_id:
            output["client_id"] = str(client_id)
        output["client_secret"] = "{{ env_var('DATABRICKS_CLIENT_SECRET') }}"  # noqa: S105
    else:
        # PATs and CI's exchanged OIDC token both arrive as DATABRICKS_TOKEN:
        # a Jinja reference, not a value.
        output["token"] = "{{ env_var('DATABRICKS_TOKEN') }}"  # noqa: S105
    return yaml.safe_dump(
        {project_name: {"target": "dev", "outputs": {"dev": output}}},
        sort_keys=False,
    )


_DEFAULT_PG_DEV_SCHEMA = "dbt_dev"


def _postgres_profile(
    project_name: str, path: str | None, config: DexConfig, root: Path
) -> str:
    """A single dbt-postgres ``dev`` target from the discovered connection,
    with no secret ever rendered: the password is an ``env_var`` reference
    (``PGPASSWORD``, empty default so ``~/.pgpass`` and peer auth still apply
    at dbt runtime). Writes go to a dedicated dev schema, never to a source
    schema dex reads from."""

    from ..config import PostgresTarget
    from ..connect import CredentialDiscoveryError, resolve_postgres_connection

    target = config.postgres or PostgresTarget()
    try:
        _params, method = resolve_postgres_connection(target, os.environ, root)
    except CredentialDiscoveryError as exc:
        raise InitError(str(exc)) from exc

    fields = _pg_connection_fields(target, method)
    host = fields.get("host")
    dbname = fields.get("dbname")
    user = fields.get("user")
    if not host or not dbname or not user:
        missing = ", ".join(
            name
            for name, value in (("host", host), ("dbname", dbname), ("user", user))
            if not value
        )
        raise InitError(
            f"the discovered Postgres connection does not name {missing}, "
            "which dbt-postgres requires; export DATABASE_URL with them (or "
            "PGHOST/PGDATABASE/PGUSER), or complete the pg_service.conf entry"
        )

    dev_schema = target.dev_schema or _DEFAULT_PG_DEV_SCHEMA
    if dev_schema in set(target.schemas):
        raise InitError(
            f"dev_schema '{dev_schema}' is also a source schema in "
            "postgres.schemas; dbt builds must write where dex does not read "
            "(set postgres.dev_schema to a dedicated schema)"
        )

    target.dev_schema = dev_schema
    config.postgres = target

    output: dict[str, Any] = {
        "type": "postgres",
        "host": str(host),
        "port": int(fields.get("port") or 5432),
        "user": str(user),
        # Never persist a password: the profile reads it from the environment
        # at dbt runtime (a Jinja reference, not a value); the empty default
        # keeps ~/.pgpass and peer auth working when no variable is set.
        "password": "{{ env_var('PGPASSWORD', '') }}",
        "dbname": str(dbname),
        "schema": dev_schema,
        # One thread keeps the operational database from parallel bursts; the
        # per-statement load is the guarded quantity on db-load gating.
        "threads": 1,
        "connect_timeout": 10,
    }
    if fields.get("sslmode"):
        output["sslmode"] = str(fields["sslmode"])
    return yaml.safe_dump(
        {project_name: {"target": "dev", "outputs": {"dev": output}}},
        sort_keys=False,
    )


def _pg_connection_fields(target: Any, method: str) -> dict:
    """The non-secret connection fields of the source that won discovery, for
    rendering into the profile. Values stay inside the engine except the ones
    deliberately rendered (host/port/user/dbname/sslmode)."""

    from ..connect import _pg_service_params, _pg_url_params

    env = os.environ
    source = method.split(":", 1)[0]
    if source == "config_service" and target.service:
        return _pg_service_params(target.service, env) or {}
    if source == "database_url":
        return _pg_url_params(env.get("DATABASE_URL", ""))
    if source == "environment":
        if env.get("PGSERVICE"):
            return _pg_service_params(env["PGSERVICE"], env) or {}
        return {
            key: env[var]
            for var, key in (
                ("PGHOST", "host"),
                ("PGPORT", "port"),
                ("PGDATABASE", "dbname"),
                ("PGUSER", "user"),
                ("PGSSLMODE", "sslmode"),
            )
            if env.get(var)
        }
    if source == "config_target":
        return {
            key: value
            for key, value in (
                ("host", target.host),
                ("port", target.port),
                ("dbname", target.dbname),
                ("user", target.user),
            )
            if value is not None
        }
    from ..connect import _postgres_from_dbt_profiles

    return _postgres_from_dbt_profiles(".") or {}


_PROFILE_RENDERERS: dict[str, Callable[[str, str | None, DexConfig, Path], str]] = {
    "duckdb": _duckdb_profile,
    "bigquery": _bigquery_profile,
    "snowflake": _snowflake_profile,
    "databricks": _databricks_profile,
    "postgres": _postgres_profile,
}


def _project_yaml(project_name: str) -> str:
    return yaml.safe_dump(
        {
            "name": project_name,
            "version": "1.0.0",
            "profile": project_name,
            "model-paths": ["models"],
        },
        sort_keys=False,
    )
