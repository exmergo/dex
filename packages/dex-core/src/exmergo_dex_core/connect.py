"""Connection handling and credential discovery.

All connection handling lives here so credentials and raw rows stay inside the
engine process and never reach the agent. DuckDB is a local file path, opened
read-only, no credentials. BigQuery discovers credentials ("discover, don't
ask"): Application Default Credentials only, never a prompted or pasted key,
with the project resolved from explicit config, the environment, the ADC
default, or a dbt profile, in that order. Snowflake discovers a connection the
same way: a named ``connections.toml`` entry, the default connection, the
``SNOWFLAKE_*`` environment (which is also how CI's workload-identity token
arrives), or a dbt profile. Databricks delegates to the SDK's unified auth
chain: a config-pinned ``~/.databrickscfg`` profile, the ``DATABRICKS_*``
environment (which is also how CI's OIDC federation arrives), the default
profile, or a dbt profile. Postgres follows suit: a config-pinned
``pg_service.conf`` entry, ``DATABASE_URL``, the ``PG*`` environment (resolved
natively by libpq, including ``~/.pgpass``), or a dbt profile. Redshift
discovers along both of its worlds: a config-pinned Serverless workgroup (or
provisioned cluster) resolved through the AWS default credential chain into
IAM temporary database credentials, the ``REDSHIFT_*`` environment, the
committed non-secret config target (password via ``REDSHIFT_PASSWORD``), or a
dbt profile. Every discovery failure names the fix; nothing is ever prompted
for.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .adapters import get_adapter
from .adapters.base import scope_within
from .cache import DexStore
from .config import (
    BigQueryTarget,
    DatabricksTarget,
    DexConfig,
    PostgresTarget,
    RedshiftTarget,
    SnowflakeTarget,
    load_config,
)
from .envelope import Paradigm
from .guards.cost_guard import CostGate


class CredentialDiscoveryError(Exception):
    """Raised when a cloud connection cannot be discovered. The message always
    names the command or config that fixes it, never a credential value."""


class ScopeError(Exception):
    """Raised when a source scope is not one this connector can honor: a flag it
    does not speak, or an entry outside the committed allowlist. The message
    always names the offending entry and the vocabulary that would work."""


# Where each connector keeps its committed source allowlist, and how a `--scope`
# entry reads in that connector's namespace vocabulary. DuckDB is absent because
# it has no namespace to scope: the target is one file.
_SCOPE_FIELDS = {
    "bigquery": "datasets",
    "snowflake": "databases",
    "databricks": "catalogs",
    "postgres": "schemas",
    "redshift": "schemas",
}
_SCOPE_GRAMMAR = {
    "bigquery": "--scope <dataset> (or <project>.<dataset>)",
    "snowflake": "--scope <schema>, <database>, or <database>.<schema>",
    "databricks": "--scope <catalog> (or <catalog>.<schema>)",
    "postgres": "--scope <schema>",
    "redshift": "--scope <schema>",
}


def open_adapter(
    *,
    connector: str | None = None,
    path: str | None = None,
    project: str | None = None,
    datasets: list[str] | None = None,
    scopes: list[str] | None = None,
    repo_root: str | Path = ".",
    budget: float | None = None,
    confirmed: bool = False,
    command: str | None = None,
):
    """Resolve the connection target and return an open, read-only adapter.

    Resolution order: explicit arguments win, then ``.dex/config.yml``. For
    DuckDB the only input is a file path. ``scopes`` narrows the source
    allowlist for this one command in every warehouse connector's own
    vocabulary; ``project``/``datasets`` are BigQuery's older spelling of the
    same idea. None of them are written back to config.
    ``budget``/``confirmed`` feed the cost gate on billed connectors and are
    ignored by free ones; ``command`` labels ledger entries.
    """

    config = load_config(repo_root) or DexConfig()
    connector = connector or config.connector
    assert_scope_vocabulary(
        connector, project=project, datasets=datasets, scopes=scopes
    )

    if connector == "duckdb":
        resolved = path or (config.duckdb.path if config.duckdb else None)
        if not resolved:
            raise ValueError(
                "no DuckDB path: pass --path or set duckdb.path in .dex/config.yml"
            )
        return get_adapter("duckdb", path=resolved)

    if connector == "bigquery":
        return _open_bigquery(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            project_override=project,
            dataset_override=datasets or scopes,
            # Both flags scope BigQuery, and the refusal has to name the one the
            # user actually typed.
            scope_flag=("--dataset" if datasets else "--scope" if scopes else None),
        )

    if connector == "snowflake":
        return _open_snowflake(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            scope_override=scopes,
        )

    if connector == "databricks":
        return _open_databricks(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            scope_override=scopes,
        )

    if connector == "postgres":
        return _open_postgres(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            scope_override=scopes,
        )

    if connector == "redshift":
        return _open_redshift(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            scope_override=scopes,
        )

    return get_adapter(connector)


def assert_scope_vocabulary(
    connector: str,
    *,
    project: str | None,
    datasets: list[str] | None,
    scopes: list[str] | None,
) -> None:
    """Refuse a scoping flag the connector cannot honor.

    Accepted-and-ignored is strictly worse than rejected. A ``--dataset`` silently
    dropped on Snowflake let a user confirm a budget believing it bounded an
    eight-table schema, when the estimate in fact spanned billion-row tables
    elsewhere in the allowlist. So the only two outcomes a scoping flag may have
    are "honored" and "named in an error".
    """

    if connector == "duckdb":
        for flag, value in (
            ("--project", project),
            ("--dataset", datasets),
            ("--scope", scopes),
        ):
            if value:
                raise ScopeError(
                    f"{flag} does not apply to the duckdb connector: a DuckDB "
                    "target is a single file, selected with --path"
                )
        return
    if connector not in _SCOPE_FIELDS:
        return
    if connector != "bigquery":
        for flag, value in (("--project", project), ("--dataset", datasets)):
            if value:
                raise ScopeError(
                    f"{flag} is BigQuery vocabulary and has no meaning for the "
                    f"{connector} connector; scope this command with "
                    f"{_SCOPE_GRAMMAR[connector]}"
                )
        return
    if datasets and scopes:
        raise ScopeError(
            "--dataset and --scope both scope a BigQuery command; pass one "
            "(--scope is the spelling every connector understands)"
        )


def narrow_target(target, connector: str, scope_override: list[str] | None):
    """Apply a ``--scope`` override to a connector target, in memory only.

    A per-command scope must never rewrite the committed ``.dex/config.yml``, and
    it may only narrow: a committed allowlist is a cost boundary, so a flag cannot
    reach outside it. When nothing is committed, the override sets the allowlist,
    which is what makes ``connect test --scope X`` work before a config block
    exists.

    Entries are matched textually here. Snowflake resolves bare schema names
    against the account before it can test containment, so it narrows inside the
    adapter and never reaches this function.
    """

    if not scope_override:
        return target
    field = _SCOPE_FIELDS[connector]
    committed = [str(entry) for entry in getattr(target, field)]
    outside = [s for s in scope_override if not scope_within(s, committed)]
    if committed and outside:
        raise ScopeError(
            f"scope {', '.join(repr(s) for s in outside)} is outside the committed "
            f"allowlist ({connector}.{field}: {', '.join(committed)}); --scope "
            "narrows the configured scope, it never widens it"
        )
    return target.model_copy(update={field: list(scope_override)})


def scope_origin(connector: str, flag: str | None) -> str:
    """What a scope refusal should tell the user to go edit.

    ``narrow_target`` copies a per-command scope over the committed allowlist, so
    by the time an adapter proves the entries exist it can no longer tell which
    of the two it is holding, and the fix differs entirely: edit the flag, or
    edit the file. The caller knows, so it says.
    """

    return flag or f"{connector}.{_SCOPE_FIELDS[connector]} in .dex/config.yml"


def _open_bigquery(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    project_override: str | None = None,
    dataset_override: list[str] | None = None,
    scope_flag: str | None = None,
):
    target = config.bigquery or BigQueryTarget()
    target = narrow_target(target, "bigquery", dataset_override)
    if project_override:
        # A command-line override of the committed target, applied in memory
        # only: a smoke test should not silently rewrite .dex/config.yml.
        target = target.model_copy(update={"project": project_override})
    credentials, adc_project, principal_type = _default_credentials()
    project = resolve_bigquery_project(
        target, os.environ, adc_project, repo_root=repo_root
    )
    if not project:
        raise CredentialDiscoveryError(
            "no GCP project resolved; set bigquery.project in .dex/config.yml "
            "or run `gcloud config set project <id>` (then refresh ADC with "
            "`gcloud auth application-default login`)"
        )

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(utc_midnight, connector="bigquery"),
        confirmed=confirmed,
        connector="bigquery",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "bigquery",
        project=project,
        target=target,
        cost_gate=gate,
        credentials=credentials,
        principal_type=principal_type,
        scope_origin=scope_origin("bigquery", scope_flag),
    )


def _open_snowflake(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    scope_override: list[str] | None = None,
):
    target = config.snowflake or SnowflakeTarget()
    params, method = resolve_snowflake_connection(target, os.environ, repo_root)

    try:
        import snowflake.connector
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the Snowflake client is not installed; install the connector "
            "extra: exmergo-dex-core[snowflake]"
        ) from exc

    # The pinned warehouse rides on the session so free paths and dbt agree,
    # but the adapter re-asserts the pin before anything billed regardless.
    if target.warehouse:
        params.setdefault("warehouse", target.warehouse)
    params.setdefault("client_session_keep_alive", False)
    connection = snowflake.connector.connect(**params)

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(
            utc_midnight, field="billed_seconds", connector="snowflake"
        ),
        confirmed=confirmed,
        connector="snowflake",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "snowflake",
        connection=connection,
        cost_gate=gate,
        target=target,
        account=str(params.get("account") or "") or None,
        auth_method=method,
        # Not folded into the target: a bare `--scope TPCH_SF1` is a schema name
        # that only the account can resolve to a database, so the adapter has to
        # see the override and the committed allowlist as two separate things.
        scope_override=scope_override,
    )


def resolve_snowflake_connection(
    target: SnowflakeTarget,
    env: dict | os._Environ,
    repo_root: str | Path = ".",
) -> tuple[dict, str]:
    """Discover Snowflake connection parameters, never prompting.

    Returns ``(params, auth_method)`` where ``params`` feeds
    ``snowflake.connector.connect`` and ``auth_method`` is a coarse class safe
    to surface (named_connection / default_connection / environment /
    dbt_profile, suffixed with the credential kind); parameter values,
    including any password or key a store legitimately holds, never leave the
    engine process. Order: the config-pinned ``connections.toml`` entry, the
    default connection, ``SNOWFLAKE_*`` environment variables (the CI path:
    a workload-identity token arrives as SNOWFLAKE_TOKEN), then a dbt
    profile's snowflake target. Every failure names the fix.
    """

    connections = _snowflake_connections(env)

    if target.connection_name:
        params = connections.get(target.connection_name)
        if params is None:
            raise CredentialDiscoveryError(
                f"snowflake.connection_name '{target.connection_name}' not "
                "found in connections.toml; run `snow connection add "
                f"--connection-name {target.connection_name} ...` or fix the "
                "name in .dex/config.yml"
            )
        return _normalize_connection(params, target), (
            f"named_connection:{_credential_kind(params)}"
        )

    default_name = env.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME") or connections.get(
        "__default__"
    )
    if isinstance(default_name, str) and default_name in connections:
        params = connections[default_name]
        return _normalize_connection(params, target), (
            f"default_connection:{_credential_kind(params)}"
        )

    if env.get("SNOWFLAKE_ACCOUNT") and env.get("SNOWFLAKE_USER"):
        params = {
            key: env[var]
            for var, key in (
                ("SNOWFLAKE_ACCOUNT", "account"),
                ("SNOWFLAKE_USER", "user"),
                ("SNOWFLAKE_PASSWORD", "password"),
                ("SNOWFLAKE_AUTHENTICATOR", "authenticator"),
                ("SNOWFLAKE_PRIVATE_KEY_FILE", "private_key_file"),
                ("SNOWFLAKE_TOKEN", "token"),
                ("SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER", "workload_identity_provider"),
                ("SNOWFLAKE_ROLE", "role"),
                ("SNOWFLAKE_WAREHOUSE", "warehouse"),
                ("SNOWFLAKE_DATABASE", "database"),
            )
            if env.get(var)
        }
        return _normalize_connection(params, target), (
            f"environment:{_credential_kind(params)}"
        )

    profile_params = _snowflake_from_dbt_profiles(repo_root)
    if profile_params:
        return _normalize_connection(profile_params, target), (
            f"dbt_profile:{_credential_kind(profile_params)}"
        )

    raise CredentialDiscoveryError(
        "no Snowflake connection discovered; add one with `snow connection "
        "add` (then set snowflake.connection_name in .dex/config.yml), or "
        "export SNOWFLAKE_ACCOUNT/SNOWFLAKE_USER plus a credential, or keep a "
        "snowflake target in your dbt profiles.yml"
    )


def _normalize_connection(params: dict, target: SnowflakeTarget) -> dict:
    normalized = dict(params)
    if target.account:
        normalized["account"] = target.account
    if not normalized.get("account"):
        raise CredentialDiscoveryError(
            "the discovered Snowflake connection has no account identifier; "
            "set snowflake.account in .dex/config.yml or fix the connection"
        )
    return normalized


def _credential_kind(params: dict) -> str:
    """The coarse credential class for the envelope; never a value."""

    authenticator = str(params.get("authenticator", "")).upper()
    if authenticator == "WORKLOAD_IDENTITY":
        return "workload_identity"
    if authenticator == "EXTERNALBROWSER":
        return "sso_browser"
    if (
        params.get("private_key_file")
        or params.get("private_key_path")
        or params.get("private_key")
    ):
        return "key_pair"
    if params.get("token"):
        return "token"
    if params.get("password"):
        return "password"
    return "unknown"


def _snowflake_connections(env: dict | os._Environ) -> dict:
    """Parse the Snowflake config stores the ``snow`` CLI and the Python
    connector share. Returns name -> params, plus ``__default__`` naming the
    configured default connection when one is declared.

    Search order matches the connector: ``$SNOWFLAKE_HOME``, ``~/.snowflake``,
    then the platform config dir. ``connections.toml`` holds top-level
    connection tables; ``config.toml`` holds ``[connections.<name>]`` tables
    and ``default_connection_name``.
    """

    candidates: list[Path] = []
    snowflake_home = env.get("SNOWFLAKE_HOME")
    if snowflake_home:
        candidates.append(Path(snowflake_home))
    candidates.append(Path.home() / ".snowflake")
    candidates.append(_platform_config_dir())

    connections: dict = {}
    for directory in candidates:
        connections_file = directory / "connections.toml"
        config_file = directory / "config.toml"
        try:
            if connections_file.is_file():
                parsed = tomllib.loads(connections_file.read_text(encoding="utf-8"))
                for name, params in parsed.items():
                    if isinstance(params, dict):
                        connections.setdefault(name, params)
            if config_file.is_file():
                parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
                for name, params in (parsed.get("connections") or {}).items():
                    if isinstance(params, dict):
                        connections.setdefault(name, params)
                default = parsed.get("default_connection_name")
                if isinstance(default, str):
                    connections.setdefault("__default__", default)
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return connections


def _platform_config_dir() -> Path:
    import sys

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "snowflake"
    return Path.home() / ".config" / "snowflake"


def _snowflake_from_dbt_profiles(repo_root: str | Path) -> dict | None:
    """Best-effort: the connection fields of a ``type: snowflake`` output in
    the discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if (
                isinstance(output, dict)
                and output.get("type") == "snowflake"
                and output.get("account")
            ):
                params = {
                    key: output[key]
                    for key in (
                        "account",
                        "user",
                        "password",
                        "authenticator",
                        "role",
                        "warehouse",
                        "database",
                    )
                    if output.get(key)
                }
                if output.get("private_key_path"):
                    params["private_key_file"] = output["private_key_path"]
                return params
    return None


def _open_databricks(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    scope_override: list[str] | None = None,
):
    target = config.databricks or DatabricksTarget()
    target = narrow_target(target, "databricks", scope_override)

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the Databricks client is not installed; install the connector "
            "extra: exmergo-dex-core[databricks]"
        ) from exc

    sdk_config, method = resolve_databricks_connection(target, os.environ, repo_root)
    workspace = WorkspaceClient(config=sdk_config)

    def sql_connect():
        # Built lazily by the adapter on the first billed statement only:
        # opening a SQL session lands on the warehouse and can wake it, so the
        # free metadata paths must never construct this connection.
        from databricks import sql as dbsql

        from .adapters.databricks import warehouse_http_path

        return dbsql.connect(
            server_hostname=_databricks_hostname(sdk_config.host),
            http_path=warehouse_http_path(str(target.warehouse)),
            credentials_provider=lambda: sdk_config.authenticate,
            user_agent_entry="dex",
        )

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(
            utc_midnight, field="billed_seconds", connector="databricks"
        ),
        confirmed=confirmed,
        connector="databricks",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "databricks",
        workspace=workspace,
        sql_connect=sql_connect,
        cost_gate=gate,
        target=target,
        host=_databricks_hostname(sdk_config.host),
        auth_method=method,
        scope_origin=scope_origin("databricks", "--scope" if scope_override else None),
    )


def resolve_databricks_connection(
    target: DatabricksTarget,
    env: dict | os._Environ,
    repo_root: str | Path = ".",
):
    """Discover a Databricks workspace connection, never prompting.

    Returns ``(sdk_config, auth_method)`` where ``sdk_config`` is a
    ``databricks.sdk.core.Config`` carrying the resolved host and credential
    strategy, and ``auth_method`` is a coarse class safe to surface
    (named_profile / environment / default_profile / dbt_profile, suffixed
    with the credential kind); token values never leave the engine process.
    Credential mechanics are the SDK's unified chain, so the CLI's OAuth
    cache, PATs, OAuth M2M, and CI's OIDC federation all arrive through the
    same door. Order: the config-pinned ``~/.databrickscfg`` profile, the
    ``DATABRICKS_*`` environment, the default profile, then a dbt profile's
    databricks target. Every failure names the fix.
    """

    from databricks.sdk.core import Config

    overrides: dict = {}
    if target.host:
        overrides["host"] = target.host

    if target.profile:
        try:
            cfg = Config(profile=target.profile, **overrides)
        except ValueError as exc:
            raise CredentialDiscoveryError(
                f"databricks.profile '{target.profile}' did not resolve: check "
                "~/.databrickscfg or run `databricks auth login --host "
                f"<workspace-url> --profile {target.profile}`"
            ) from exc
        return cfg, f"named_profile:{_databricks_credential_kind(cfg)}"

    if env.get("DATABRICKS_HOST") or env.get("DATABRICKS_CONFIG_PROFILE"):
        try:
            cfg = Config(**overrides)
        except ValueError as exc:
            raise CredentialDiscoveryError(
                "DATABRICKS_* environment variables are set but no credential "
                "resolved; export DATABRICKS_TOKEN or the OAuth client "
                "variables, or run `databricks auth login`"
            ) from exc
        return cfg, f"environment:{_databricks_credential_kind(cfg)}"

    if _databrickscfg_default_exists(env):
        try:
            cfg = Config(**overrides)
        except ValueError as exc:
            raise CredentialDiscoveryError(
                "the DEFAULT profile in ~/.databrickscfg did not resolve; "
                "re-run `databricks auth login --host <workspace-url>` or pin "
                "databricks.profile in .dex/config.yml"
            ) from exc
        return cfg, f"default_profile:{_databricks_credential_kind(cfg)}"

    profile_params = _databricks_from_dbt_profiles(repo_root)
    if profile_params:
        kwargs: dict = {"host": target.host or profile_params.get("host")}
        if profile_params.get("token"):
            kwargs["token"] = profile_params["token"]
        try:
            cfg = Config(**kwargs)
        except ValueError as exc:
            raise CredentialDiscoveryError(
                "the databricks target in dbt profiles.yml did not resolve to "
                "a usable credential; run `databricks auth login --host "
                "<workspace-url>` instead"
            ) from exc
        return cfg, f"dbt_profile:{_databricks_credential_kind(cfg)}"

    raise CredentialDiscoveryError(
        "no Databricks connection discovered; run `databricks auth login "
        "--host <workspace-url>` (then optionally pin databricks.profile in "
        ".dex/config.yml), or export DATABRICKS_HOST plus a credential, or "
        "keep a databricks target in your dbt profiles.yml"
    )


def _databricks_credential_kind(cfg) -> str:
    """The coarse credential class for the envelope; never a value. The SDK's
    ``auth_type`` names the strategy that won its unified chain."""

    auth_type = str(getattr(cfg, "auth_type", "") or "").lower()
    if auth_type == "pat":
        return "token"
    if auth_type in ("databricks-cli", "external-browser"):
        return "oauth_user"
    if auth_type == "oauth-m2m":
        return "oauth_m2m"
    if "oidc" in auth_type:
        return "workload_identity"
    if auth_type.startswith(("azure", "google")):
        return "cloud_native"
    return "unknown"


def _databricks_hostname(host: object) -> str:
    """The bare workspace hostname (an identifier, not a secret): what the SQL
    driver wants, and what capabilities surface."""

    return str(host or "").removeprefix("https://").removeprefix("http://").rstrip("/")


def _databrickscfg_default_exists(env: dict | os._Environ) -> bool:
    """Whether ``~/.databrickscfg`` (or ``DATABRICKS_CONFIG_FILE``) declares a
    default profile the SDK would resolve without any other signal: either a
    classic ``[DEFAULT]`` section with a host, or the newer CLI's
    ``[__settings__] default_profile`` pointer at a profile with a host."""

    import configparser

    path = Path(env.get("DATABRICKS_CONFIG_FILE") or Path.home() / ".databrickscfg")
    if not path.is_file():
        return False
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
    except (OSError, configparser.Error):
        return False
    if parser.defaults().get("host"):
        return True
    if parser.has_section("__settings__"):
        pointed = parser.get("__settings__", "default_profile", fallback=None)
        if (
            pointed
            and parser.has_section(pointed)
            and parser.get(pointed, "host", fallback=None)
        ):
            return True
    return False


def _databricks_from_dbt_profiles(repo_root: str | Path) -> dict | None:
    """Best-effort: the connection fields of a ``type: databricks`` output in
    the discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if (
                isinstance(output, dict)
                and output.get("type") == "databricks"
                and output.get("host")
            ):
                return {
                    key: output[key]
                    for key in ("host", "token", "http_path")
                    if output.get(key)
                }
    return None


def _open_postgres(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    scope_override: list[str] | None = None,
):
    target = config.postgres or PostgresTarget()
    target = narrow_target(target, "postgres", scope_override)
    params, method = resolve_postgres_connection(target, os.environ, repo_root)

    try:
        import psycopg
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the Postgres client is not installed; install the connector "
            "extra: exmergo-dex-core[postgres]"
        ) from exc

    # autocommit keeps the session out of idle-in-transaction (which blocks
    # vacuum on a production primary); application_name is the attribution
    # tag, the QUERY_TAG analogue. The adapter additionally sets the session
    # read-only before any statement runs.
    connection = psycopg.connect(**params, autocommit=True, application_name="dex")

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.DB_LOAD,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(
            utc_midnight, field="billed_seconds", connector="postgres"
        ),
        confirmed=confirmed,
        connector="postgres",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "postgres",
        connection=connection,
        cost_gate=gate,
        target=target,
        auth_method=method,
        scope_origin=scope_origin("postgres", "--scope" if scope_override else None),
    )


def resolve_postgres_connection(
    target: PostgresTarget,
    env: dict | os._Environ,
    repo_root: str | Path = ".",
) -> tuple[dict, str]:
    """Discover Postgres connection parameters, never prompting.

    Returns ``(params, auth_method)`` where ``params`` feeds
    ``psycopg.connect`` and ``auth_method`` is a coarse class safe to surface
    (config_service / database_url / environment / config_target /
    dbt_profile, suffixed with the credential kind); parameter values,
    including any password a store legitimately holds, never leave the engine
    process. Order: the config-pinned ``pg_service.conf`` entry,
    ``DATABASE_URL``, the ``PG*`` environment (resolved natively by libpq,
    including ``~/.pgpass`` and ``PGSERVICE``), the committed non-secret
    config target, then a dbt profile's postgres target. Every failure names
    the fix.
    """

    if target.service:
        service_params = _pg_service_params(target.service, env)
        if service_params is None:
            raise CredentialDiscoveryError(
                f"postgres.service '{target.service}' not found in "
                "pg_service.conf; add the entry (PGSERVICEFILE or "
                "~/.pg_service.conf) or fix the name in .dex/config.yml"
            )
        # libpq resolves the service entry itself; the parsed fields above
        # only validated existence and stay inside the engine.
        return {"service": target.service}, (
            f"config_service:{_pg_credential_kind(service_params)}"
        )

    url = env.get("DATABASE_URL")
    if url:
        params = _pg_url_params(url)
        return {"conninfo": url}, f"database_url:{_pg_credential_kind(params)}"

    if env.get("PGSERVICE"):
        return {}, "environment:service_file"
    if env.get("PGHOST") or env.get("PGDATABASE"):
        # libpq reads the PG* variables (and ~/.pgpass) natively; passing no
        # explicit params lets its own resolution rules apply.
        kind = "password" if env.get("PGPASSWORD") else "external"
        return {}, f"environment:{kind}"

    if target.host or target.dbname:
        params = {
            key: value
            for key, value in (
                ("host", target.host),
                ("port", target.port),
                ("dbname", target.dbname),
                ("user", target.user),
            )
            if value is not None
        }
        kind = "password" if env.get("PGPASSWORD") else "external"
        return params, f"config_target:{kind}"

    profile_params = _postgres_from_dbt_profiles(repo_root)
    if profile_params:
        return profile_params, f"dbt_profile:{_pg_credential_kind(profile_params)}"

    raise CredentialDiscoveryError(
        "no Postgres connection discovered; export DATABASE_URL (or PGHOST/"
        "PGDATABASE and friends), or add a pg_service.conf entry and set "
        "postgres.service in .dex/config.yml, or set postgres.host/dbname "
        "there, or keep a postgres target in your dbt profiles.yml"
    )


def _pg_credential_kind(params: dict) -> str:
    """The coarse credential class for the envelope; never a value. Anything
    that is not an inline password is ``external`` (``~/.pgpass``, peer/trust
    auth, SSL client certificates), deliberately coarse."""

    return "password" if params.get("password") else "external"


def _pg_url_params(url: str) -> dict:
    """Parse a Postgres URL into libpq keywords, for classification only (the
    URL itself is what gets passed to the driver). Failures mean "no fields",
    never an error that could echo the URL."""

    try:
        from psycopg.conninfo import conninfo_to_dict

        return {k: v for k, v in conninfo_to_dict(url).items() if v is not None}
    except Exception:
        return {}


def _pg_service_params(service: str, env: dict | os._Environ) -> dict | None:
    """The named entry of the pg_service.conf file, or ``None`` when the file
    or entry is missing. Search order matches libpq: ``PGSERVICEFILE``, then
    ``~/.pg_service.conf``. Parsed fields are used for existence validation
    and credential classification only; they never leave the engine."""

    import configparser

    candidates: list[Path] = []
    service_file = env.get("PGSERVICEFILE")
    if service_file:
        candidates.append(Path(service_file))
    candidates.append(Path.home() / ".pg_service.conf")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(candidate.read_text(encoding="utf-8"))
        except (OSError, configparser.Error):
            continue
        if parser.has_section(service):
            return dict(parser.items(service))
    return None


def _postgres_from_dbt_profiles(repo_root: str | Path) -> dict | None:
    """Best-effort: the connection fields of a ``type: postgres`` output in
    the discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if (
                isinstance(output, dict)
                and output.get("type") == "postgres"
                and output.get("host")
            ):
                params = {
                    key: output[key]
                    for key in ("host", "port", "user", "password", "sslmode")
                    if output.get(key)
                }
                dbname = output.get("dbname") or output.get("database")
                if dbname:
                    params["dbname"] = dbname
                return params
    return None


def _open_redshift(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    scope_override: list[str] | None = None,
):
    target = config.redshift or RedshiftTarget()
    target = narrow_target(target, "redshift", scope_override)
    params, method, compute = resolve_redshift_connection(target, os.environ, repo_root)

    try:
        import redshift_connector
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the Redshift client is not installed; install the connector "
            "extra: exmergo-dex-core[redshift]"
        ) from exc

    # application_name is the attribution tag (SYS_CONNECTION_LOG); the
    # adapter additionally sets query_group and a best-effort session
    # read-only mode before any statement runs. autocommit keeps session SETs
    # (statement_timeout) outside any transaction the driver would otherwise
    # open.
    connection = redshift_connector.connect(**params, application_name="dex")
    connection.autocommit = True

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(
            utc_midnight, field="billed_seconds", connector="redshift"
        ),
        confirmed=confirmed,
        connector="redshift",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "redshift",
        connection=connection,
        cost_gate=gate,
        target=target,
        compute=compute,
        auth_method=method,
        scope_origin=scope_origin("redshift", "--scope" if scope_override else None),
    )


def resolve_redshift_connection(
    target: RedshiftTarget,
    env: dict | os._Environ,
    repo_root: str | Path = ".",
) -> tuple[dict, str, dict | None]:
    """Discover Redshift connection parameters, never prompting.

    Returns ``(params, auth_method, compute)`` where ``params`` feeds
    ``redshift_connector.connect``, ``auth_method`` is a coarse class safe to
    surface (iam_serverless / iam_cluster / environment / config_target /
    dbt_profile, suffixed with the credential kind), and ``compute`` carries
    the control-plane facts for the RPU translation (kind, workgroup, base
    capacity) or ``None`` when they are unknowable. Parameter values,
    including any password an environment legitimately holds, never leave the
    engine process.

    Order: the config-pinned Serverless ``workgroup`` (IAM temporary
    credentials through the AWS default chain, endpoint and database
    discovered from the control plane), the config-pinned provisioned
    ``cluster_identifier`` (IAM, GetClusterCredentials), the ``REDSHIFT_*``
    environment, the committed non-secret config target (password via
    ``REDSHIFT_PASSWORD``), then a dbt profile's redshift target. Every
    failure names the fix.
    """

    if target.workgroup:
        return _redshift_serverless_iam(target, env)

    if target.cluster_identifier:
        if not target.dbname or not target.user:
            raise CredentialDiscoveryError(
                "IAM auth against a provisioned cluster needs the database "
                "and user: set redshift.dbname and redshift.user in "
                ".dex/config.yml alongside redshift.cluster_identifier"
            )
        params: dict = {
            "iam": True,
            "cluster_identifier": target.cluster_identifier,
            "database": target.dbname,
            "db_user": target.user,
        }
        if target.aws_profile:
            params["profile"] = target.aws_profile
        if target.region:
            params["region"] = target.region
        return (
            params,
            f"iam_cluster:{_aws_credential_kind(target, env)}",
            _redshift_compute("provisioned"),
        )

    if env.get("REDSHIFT_HOST"):
        params = {"host": env["REDSHIFT_HOST"]}
        if env.get("REDSHIFT_PORT"):
            params["port"] = int(env["REDSHIFT_PORT"])
        if env.get("REDSHIFT_DATABASE"):
            params["database"] = env["REDSHIFT_DATABASE"]
        if env.get("REDSHIFT_USER"):
            params["user"] = env["REDSHIFT_USER"]
        if env.get("REDSHIFT_PASSWORD"):
            params["password"] = env["REDSHIFT_PASSWORD"]
        kind = "password" if env.get("REDSHIFT_PASSWORD") else "external"
        return params, f"environment:{kind}", _redshift_host_compute(params["host"])

    if target.host or target.dbname:
        params = {
            key: value
            for key, value in (
                ("host", target.host),
                ("port", target.port),
                ("database", target.dbname),
                ("user", target.user),
            )
            if value is not None
        }
        if env.get("REDSHIFT_PASSWORD"):
            params["password"] = env["REDSHIFT_PASSWORD"]
            kind = "password"
        else:
            kind = "external"
        return (
            params,
            f"config_target:{kind}",
            _redshift_host_compute(target.host),
        )

    profile_params = _redshift_from_dbt_profiles(repo_root, env)
    if profile_params:
        kind = "password" if profile_params.get("password") else "external"
        return (
            profile_params,
            f"dbt_profile:{kind}",
            _redshift_host_compute(profile_params.get("host")),
        )

    raise CredentialDiscoveryError(
        "no Redshift connection discovered; set redshift.workgroup in "
        ".dex/config.yml (Serverless, IAM via the AWS credential chain), or "
        "redshift.cluster_identifier plus dbname/user (provisioned, IAM), or "
        "export REDSHIFT_HOST/REDSHIFT_DATABASE (password via "
        "REDSHIFT_PASSWORD), or set redshift.host/dbname there, or keep a "
        "redshift target in your dbt profiles.yml"
    )


def _redshift_serverless_iam(
    target: RedshiftTarget, env: dict | os._Environ
) -> tuple[dict, str, dict | None]:
    """The Serverless IAM path: the workgroup pin plus the AWS default
    credential chain resolve everything else (endpoint, port, database) from
    the control plane, and GetCredentials mints temporary database
    credentials inside the driver. Nothing is prompted; every failure names
    the fix."""

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the Redshift client is not installed; install the connector "
            "extra: exmergo-dex-core[redshift]"
        ) from exc

    session = boto3.Session(
        profile_name=target.aws_profile or None,
        region_name=target.region or None,
    )
    try:
        client = session.client("redshift-serverless")
        workgroup = client.get_workgroup(workgroupName=target.workgroup)["workgroup"]
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise CredentialDiscoveryError(
            f"could not resolve Redshift Serverless workgroup "
            f"'{target.workgroup}': configure the AWS credential chain "
            "(aws configure, AWS_* environment variables, or "
            "redshift.aws_profile), set redshift.region when the default "
            "region is wrong, and grant redshift-serverless:GetWorkgroup "
            "and redshift-serverless:GetCredentials"
        ) from exc

    endpoint = workgroup.get("endpoint") or {}
    host = endpoint.get("address")
    if not host:
        raise CredentialDiscoveryError(
            f"workgroup '{target.workgroup}' has no reachable endpoint yet "
            f"(status {workgroup.get('status', 'unknown')}); wait for it to "
            "become AVAILABLE or check its network configuration"
        )
    database = target.dbname or _redshift_namespace_database(
        client, workgroup.get("namespaceName")
    )
    if not database:
        raise CredentialDiscoveryError(
            "could not discover the database of workgroup "
            f"'{target.workgroup}'; set redshift.dbname in .dex/config.yml "
            "or grant redshift-serverless:GetNamespace"
        )
    params: dict = {
        "iam": True,
        "host": host,
        "port": int(endpoint.get("port") or 5439),
        "database": database,
    }
    if target.aws_profile:
        params["profile"] = target.aws_profile
    if target.region:
        params["region"] = target.region
    compute = _redshift_compute(
        "serverless",
        workgroup=str(workgroup.get("workgroupName", target.workgroup)),
        base_capacity_rpus=(
            float(workgroup["baseCapacity"])
            if workgroup.get("baseCapacity") is not None
            else None
        ),
    )
    return params, f"iam_serverless:{_aws_credential_kind(target, env)}", compute


def _redshift_namespace_database(client, namespace_name: str | None) -> str | None:
    """The default database of a Serverless namespace, or ``None`` when it
    cannot be read (the caller then requires ``redshift.dbname``). Takes the
    already-built redshift-serverless client: constructing a second one per
    connect would re-load the service model for nothing."""

    if not namespace_name:
        return None
    try:
        namespace = client.get_namespace(namespaceName=namespace_name)["namespace"]
        return str(namespace["dbName"]) if namespace.get("dbName") else None
    except Exception:
        return None


def _aws_credential_kind(target: RedshiftTarget, env: dict | os._Environ) -> str:
    """The coarse AWS credential class for the envelope; never a value or an
    identity. Deliberately coarse: a pinned profile, the environment, or
    whatever else the default chain found (SSO, a role, instance metadata)."""

    if target.aws_profile:
        return "profile"
    if env.get("AWS_ACCESS_KEY_ID"):
        return "environment"
    return "default_chain"


def _redshift_compute(
    kind: str,
    *,
    workgroup: str | None = None,
    base_capacity_rpus: float | None = None,
) -> dict:
    """The compute-facts shape every discovery path returns. The adapter reads
    exactly these three keys for the RPU translation, so one constructor keeps
    the shape from drifting per path."""

    return {
        "kind": kind,
        "workgroup": workgroup,
        "base_capacity_rpus": base_capacity_rpus,
    }


def _redshift_host_compute(host: str | None) -> dict | None:
    """Compute facts inferred from an endpoint host alone: a Serverless
    endpoint is recognizable by its domain, but its workgroup and base
    capacity are unknowable without the control plane, so the RPU translation
    degrades and only the wake-minimum honesty remains."""

    if not host:
        return None
    kind = "serverless" if ".redshift-serverless." in host else "provisioned"
    return _redshift_compute(kind)


_ENV_VAR_TEMPLATE = re.compile(r"\{\{\s*env_var\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def _profile_scalar(value, env: dict | os._Environ) -> str | None:
    """A profiles.yml scalar with dbt's env_var indirection honored. dex's own
    rendered profiles keep the password as ``{{ env_var('REDSHIFT_PASSWORD') }}``
    (never a value), so a reference resolves from the environment the way dbt
    would; any other unrendered template is unusable, never passed through as
    a literal (the driver would misreport that as a wrong password)."""

    if value is None:
        return None
    text = str(value)
    if "{{" not in text:
        return text
    match = _ENV_VAR_TEMPLATE.fullmatch(text.strip())
    if match:
        return env.get(match.group(1)) or None
    return None


def _redshift_from_dbt_profiles(
    repo_root: str | Path, env: dict | os._Environ
) -> dict | None:
    """Best-effort: the connection fields of a ``type: redshift`` output in
    the discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if not isinstance(output, dict) or output.get("type") != "redshift":
                continue
            if str(output.get("method") or "").lower() == "iam":
                # An IAM output mints its credentials at dbt runtime; it
                # carries nothing durable to connect with natively, and the
                # workgroup/cluster config paths own that story.
                continue
            host = _profile_scalar(output.get("host"), env)
            if not host:
                continue
            # A declared field the environment cannot render (dex's own
            # profiles keep the password as {{ env_var(...) }}) makes the
            # output unusable as discovered: skip it rather than connect
            # with a hole where a credential should be, and let the terminal
            # discovery error name the export that fixes it.
            fields: dict = {}
            unusable = False
            for key in ("user", "password", "dbname", "database"):
                raw = output.get(key)
                if not raw:
                    continue
                value = _profile_scalar(raw, env)
                if not value:
                    unusable = True
                    break
                fields["database" if key == "database" else key] = value
            if unusable:
                continue
            params: dict = {"host": host}
            port = output.get("port")
            if isinstance(port, int) or (isinstance(port, str) and port.isdigit()):
                params["port"] = int(port)
            for key in ("user", "password"):
                if key in fields:
                    params[key] = fields[key]
            dbname = fields.get("dbname") or fields.get("database")
            if dbname:
                params["database"] = dbname
            return params
    return None


def _default_credentials():
    """Discover Application Default Credentials, never prompting.

    Returns ``(credentials, adc_project, principal_type)``. ``principal_type``
    is a coarse class of principal (user / service_account /
    impersonated_service_account / external_account / metadata), safe to
    surface; the principal's identity never is.
    """

    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the BigQuery client is not installed; install the connector "
            "extra: exmergo-dex-core[bigquery]"
        ) from exc

    try:
        credentials, adc_project = google.auth.default()
    except DefaultCredentialsError as exc:
        raise CredentialDiscoveryError(
            "no Google Application Default Credentials found; run "
            "`gcloud auth application-default login` (or point "
            "GOOGLE_APPLICATION_CREDENTIALS at a service-account file) and retry"
        ) from exc

    module = type(credentials).__module__
    if "impersonated" in module:
        principal_type = "impersonated_service_account"
    elif "external_account" in module:
        principal_type = "external_account"
    elif "service_account" in module:
        principal_type = "service_account"
    elif "compute_engine" in module:
        principal_type = "metadata"
    else:
        principal_type = "user"
    return credentials, adc_project, principal_type


def resolve_bigquery_project(
    target: BigQueryTarget,
    env: dict | os._Environ,
    adc_project: str | None,
    *,
    repo_root: str | Path = ".",
) -> str | None:
    """The project resolution chain: explicit config, then the environment,
    then the ADC default, then a dbt profile's bigquery target. Pure given its
    inputs (the environment is injected), so the order is directly testable."""

    return (
        target.project
        or env.get("GOOGLE_CLOUD_PROJECT")
        or env.get("GCLOUD_PROJECT")
        or adc_project
        or _project_from_dbt_profiles(repo_root)
    )


def _project_from_dbt_profiles(repo_root: str | Path) -> str | None:
    """Best-effort: the ``project`` of a ``type: bigquery`` output in the
    discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if (
                isinstance(output, dict)
                and output.get("type") == "bigquery"
                and output.get("project")
            ):
                return str(output["project"])
    return None
