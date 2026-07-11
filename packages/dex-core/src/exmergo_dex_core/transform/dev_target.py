"""Whether the dev target a build is about to use is coherent and reachable.

Two failures put a build on the wrong footing before dbt ever runs, and dbt
reports neither of them well.

The first is drift. ``transform init`` renders ``.dex/config.yml`` into
``profiles.yml``, and from then on the profile is what dbt reads. Editing the
config afterwards changes nothing, so a user who retargets their dev database
gets a green build against the old one. dex asks you to author that config file,
so it has to be live or say plainly that it is not.

The second is absence. dbt creates schemas but never databases, so a dev database
that does not exist yet fails somewhere inside dbt's ``list_schemas`` macro with
``002043: Object does not exist``, a message that names neither the database nor
the fix.

Both are refusals, not warnings, and both are free: the drift check reads two
files, and the existence check rides the connector's free metadata path. They run
before the cost gate, so a build that cannot possibly succeed is refused before
anyone is asked to weigh a budget. Neither ever rewrites ``profiles.yml``: a
hand-edited profile is a legitimate thing to have, and dex proposes rather than
imposes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..cache import DEX_DIR
from ..config import CONFIG_FILE, DexConfig
from ..dbt_project import PROFILES_FILE, duckdb_target_path, target_identifiers
from ..dbt_project import load as load_project


class DevTargetError(Exception):
    """Raised when the dev target cannot be built against. The message always
    names both of the values that disagree, or the statement that fixes it."""


# Per connector, the config fields that must agree with the profile keys they were
# rendered into, and whether the connector folds identifier case. Only fields the
# user actually set are compared, so an unset config field never reads as drift.
_DRIFT_FIELDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # (config attribute, config key path for the message, profiles.yml key)
    "snowflake": (
        ("dev_database", "snowflake.dev_database", "database"),
        ("dev_schema", "snowflake.dev_schema", "schema"),
        ("warehouse", "snowflake.warehouse", "warehouse"),
    ),
    "bigquery": (
        ("dev_dataset", "bigquery.dev_dataset", "dataset"),
        ("project", "bigquery.project", "project"),
    ),
    "databricks": (
        ("dev_catalog", "databricks.dev_catalog", "catalog"),
        ("dev_schema", "databricks.dev_schema", "schema"),
    ),
    "postgres": (("dev_schema", "postgres.dev_schema", "schema"),),
    "duckdb": (("path", "duckdb.path", "path"),),
}

# Connectors whose identifiers are case-insensitive, so DBT_DEV and dbt_dev are
# the same namespace and must not be reported as drift.
_CASE_FOLDING = {"snowflake"}


def check(
    project_dir: Path | str,
    target: str,
    config: DexConfig,
    repo_root: Path | str = ".",
) -> list[str]:
    """Refuse a dev target that has drifted or does not exist. Returns warnings.

    Raises :class:`DevTargetError` on a refusal. Anything it cannot check (an
    unreadable profile, a connector with no preflight yet) is not a refusal:
    absence of evidence stays absence of evidence, and dbt's own error remains
    the backstop.
    """

    project = Path(project_dir)
    profile = target_identifiers(project, target)
    _assert_no_drift(project, target, config, profile, repo_root)
    return _assert_namespace_exists(project, target, config, repo_root)


def _assert_no_drift(
    project: Path,
    target: str,
    config: DexConfig,
    profile: dict[str, str],
    repo_root: Path | str,
) -> None:
    if not profile:
        return
    config_rel = f"{DEX_DIR}/{CONFIG_FILE}"
    profile_rel = _relative(project / PROFILES_FILE, repo_root)

    profile_type = profile.get("type")
    if profile_type and profile_type != config.connector:
        raise DevTargetError(
            f"{config_rel} and {profile_rel} disagree about the connector: "
            f"config.yml says connector: {config.connector}, {PROFILES_FILE} "
            f"target '{target}' says type: {profile_type}; edit one to match the "
            "other (dex never rewrites a profile you may have hand-edited)"
        )

    target_config = getattr(config, config.connector, None)
    if target_config is None:
        return
    fold = config.connector in _CASE_FOLDING
    divergent = []
    for attribute, config_key, profile_key in _DRIFT_FIELDS.get(config.connector, ()):
        # An unset config field was never a claim about the dev target, so it
        # cannot have drifted away from one.
        if attribute not in target_config.model_fields_set:
            continue
        want = getattr(target_config, attribute, None)
        got = profile.get(profile_key)
        if want is None or got is None:
            continue
        if (str(want).upper() == got.upper()) if fold else (str(want) == got):
            continue
        divergent.append(
            f"  {config_key}: {want}\n  {PROFILES_FILE} {target}.{profile_key}: {got}"
        )
    if divergent:
        raise DevTargetError(
            f"{config_rel} and {profile_rel} disagree about the dev target:\n"
            + "\n".join(divergent)
            + "\nedit one to match the other, then re-run (dex never rewrites a "
            "profile you may have hand-edited)"
        )


def _assert_namespace_exists(
    project: Path, target: str, config: DexConfig, repo_root: Path | str
) -> list[str]:
    if config.connector == "duckdb":
        return _duckdb_namespace(project, target)
    if config.connector == "snowflake":
        return _snowflake_namespace(config, repo_root)
    # BigQuery, Databricks, and Postgres have the same gap: nothing checks that
    # the dev dataset/catalog/schema exists, or that the principal may write it.
    # Each needs its own free probe; until then dbt's error is the only signal.
    return []


def _duckdb_namespace(project: Path, target: str) -> list[str]:
    """A duckdb target whose file does not exist yet is legitimate when the
    project builds everything from models, but a project reading from ``sources:``
    would get a fresh empty database and then fail every source relation with a
    confusing catalog error. That case is refused with the seeding step spelled
    out; the source-less case only warns."""

    db_path = duckdb_target_path(project, target)
    if db_path is None or db_path.exists():
        return []
    if _declares_sources(project):
        raise DevTargetError(
            f"the dev target database {db_path} does not exist and the project "
            "reads from sources; seed it before building (for example copy the "
            f"source warehouse: cp <source>.duckdb {db_path}), or point the dev "
            "target at an existing database file"
        )
    return [
        f"dev target database {db_path} does not exist; dbt will create an empty one"
    ]


def _snowflake_namespace(config: DexConfig, repo_root: Path | str) -> list[str]:
    """Free: SHOW only, no warehouse, so this costs nothing on a billed connector.

    dex discovers its own connection (connections.toml, the environment) while dbt
    reads ``profiles.yml``, and the two can legitimately differ, so a connection
    dex cannot open is reported as a warning rather than raised: the preflight
    must never be the thing that breaks a build dbt could have run.
    """

    target = config.snowflake
    if target is None or not target.dev_database:
        return []

    from ..connect import open_adapter

    try:
        adapter = open_adapter(connector="snowflake", repo_root=repo_root)
    except Exception as exc:  # degrade to a note; never block a build dbt could run
        # The exception class rides along: silently degrading is how a real defect
        # in the preflight would go unnoticed for a long time.
        return [
            "could not preflight the dev database "
            f"({type(exc).__name__}: {exc}); dbt will report any problem with it"
        ]
    try:
        missing = adapter.missing_dev_namespaces(target.dev_database)
    finally:
        adapter.close()

    if not missing:
        return []
    raise DevTargetError(
        f"snowflake {missing[0]} does not exist; create it with:\n"
        f"  CREATE DATABASE IF NOT EXISTS {target.dev_database};\n"
        "(dbt creates schemas but never databases, so the first build cannot "
        "create it), or point snowflake.dev_database at a database the role "
        "can write"
    )


def _declares_sources(project: Path) -> bool:
    view = load_project(project)
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and parsed.get("sources"):
            return True
    return False


def _relative(path: Path, repo_root: Path | str) -> str:
    try:
        return str(path.resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        return str(path)
