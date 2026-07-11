"""Whether the dev target a build is about to use is coherent and reachable.

Two failures put a build on the wrong footing before dbt ever runs, and dbt
reports neither of them well.

The first is drift. ``transform init`` renders ``.dex/config.yml`` into
``profiles.yml``, and from then on the profile is what dbt reads. Editing the
config afterwards changes nothing, so a user who retargets their dev database
gets a green build against the old one. dex asks you to author that config file,
so it has to be live or say plainly that it is not.

The second is that the dev target cannot be built into. dbt creates schemas, and
nothing above them, so the namespace the schema lives in has to be there already:
a missing Snowflake database dies inside dbt's ``list_schemas`` macro with
``002043: Object does not exist``, and a missing Databricks catalog dies inside
the ``create schema`` it issues. Neither message names the object or the fix. The
rule is therefore: what dbt cannot create for itself is refused here, and what it
can create is not. That lands differently per connector, so each has its own free
probe. BigQuery does create its dev dataset, so an absent one is a warning and
only an unreachable project is fatal. Postgres creates its dev schema too, but
only if the role may, so what gets checked there is the privilege, asked of the
role in the rendered profile rather than the one dex reads with.

The refusals are free: the drift check reads two files, and every existence check
rides the connector's free metadata path. They run before the cost gate, so a
build that cannot possibly succeed is refused before anyone is asked to weigh a
budget. Neither ever rewrites ``profiles.yml``: a hand-edited profile is a
legitimate thing to have, and dex proposes rather than imposes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..cache import DEX_DIR
from ..config import CONFIG_FILE, DexConfig
from ..dbt_project import (
    PROFILES_FILE,
    duckdb_target_path,
    target_identifiers,
    target_role,
)
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
    """What dbt cannot create for itself is fatal; what it can create is not.

    That rule is the whole shape of this dispatch, and it lands differently per
    connector because their ``create_schema`` implementations differ. dbt creates
    schemas everywhere, so no connector's dev *schema* is checked. Above the
    schema it stops: it never creates a Snowflake database or a Databricks
    catalog, so a missing one is refused. BigQuery is the exception, because
    dbt-bigquery does create its dev dataset; only the project it cannot create,
    so an absent dataset is a warning and an unreachable project a refusal.
    Postgres inverts it again: dbt creates the schema, but only if the role may,
    so the privilege is what gets checked.
    """

    if config.connector == "duckdb":
        return _duckdb_namespace(project, target)
    if config.connector == "snowflake":
        return _snowflake_namespace(config, repo_root)
    if config.connector == "databricks":
        return _databricks_namespace(config, repo_root)
    if config.connector == "bigquery":
        return _bigquery_namespace(config, repo_root)
    if config.connector == "postgres":
        return _postgres_namespace(project, target, config, repo_root)
    return []


def _open_for_preflight(connector: str, repo_root: Path | str):
    """The adapter, or the note to degrade to.

    dex discovers its own connection (a connections file, the environment) while
    dbt reads ``profiles.yml``, and the two can legitimately differ, so a
    connection dex cannot open is reported rather than raised: the preflight must
    never be the thing that breaks a build dbt could have run. The exception class
    rides along, because silently degrading is how a real defect in the preflight
    would go unnoticed for a long time.
    """

    from ..connect import open_adapter

    try:
        return open_adapter(connector=connector, repo_root=repo_root), None
    except Exception as exc:
        return None, (
            "could not preflight the dev database "
            f"({type(exc).__name__}: {exc}); dbt will report any problem with it"
        )


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
    """Free: SHOW only, no warehouse, so this costs nothing on a billed connector."""

    target = config.snowflake
    if target is None or not target.dev_database:
        return []

    adapter, note = _open_for_preflight("snowflake", repo_root)
    if adapter is None:
        return [note]
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


def _databricks_namespace(config: DexConfig, repo_root: Path | str) -> list[str]:
    """Free: Unity Catalog REST only, so the billed SQL warehouse is never woken.

    The closest analogue of the Snowflake case: dbt-databricks creates the dev
    schema (``create schema if not exists <catalog>.<schema>``) but never the
    catalog it lives in, so a missing catalog fails the first build from inside
    that statement, naming neither the catalog nor the fix.
    """

    target = config.databricks
    if target is None or not target.dev_catalog:
        return []

    from .init import _DEFAULT_DBX_DEV_SCHEMA

    schema = target.dev_schema or _DEFAULT_DBX_DEV_SCHEMA
    adapter, note = _open_for_preflight("databricks", repo_root)
    if adapter is None:
        return [note]
    try:
        missing = adapter.missing_dev_namespaces(target.dev_catalog)
        ungranted = (
            adapter.dev_write_grants(target.dev_catalog, schema) if not missing else []
        )
    finally:
        adapter.close()

    if missing:
        raise DevTargetError(
            f"databricks {missing[0]} does not exist; create it with:\n"
            f"  CREATE CATALOG IF NOT EXISTS {target.dev_catalog};\n"
            "(dbt creates schemas but never catalogs, so the first build cannot "
            "create it), or point databricks.dev_catalog at a catalog the principal "
            "can write"
        )
    if not ungranted:
        return []
    grants = "\n".join(f"  GRANT {grant} TO `<principal>`;" for grant in ungranted)
    return [
        f"the dev target {target.dev_catalog}.{schema} exists but Unity Catalog "
        "reports no privilege on it for this principal, so the build may fail with "
        "PERMISSION_DENIED after the warehouse has already woken. Grant it:\n"
        f"{grants}\n"
        "(ownership and metastore-admin rights are invisible to the grants API, so "
        "this is a warning, not a refusal: the build may still succeed)"
    ]


def _bigquery_namespace(config: DexConfig, repo_root: Path | str) -> list[str]:
    """Free: a metadata GET, no query, so nothing is billed on a bytes-billed
    connector.

    BigQuery is the connector where the missing dev namespace is *not* fatal:
    dbt-bigquery's ``create_schema`` issues ``CREATE SCHEMA IF NOT EXISTS``, which
    creates the dataset, so an absent one is the normal state before a first build
    and gets a warning rather than a refusal. Refusing it would block a build that
    would have succeeded. What dbt cannot create is the project, and an unreachable
    one is raised by the adapter.
    """

    target = config.bigquery
    if target is None or not target.dev_dataset:
        return []

    dataset = target.dev_dataset
    adapter, note = _open_for_preflight("bigquery", repo_root)
    if adapter is None:
        return [note]
    try:
        missing = adapter.missing_dev_namespaces(dataset)
        qualified = dataset if "." in dataset else f"{adapter.project}.{dataset}"
    except Exception as exc:  # an unreachable dev project: dbt cannot create one
        raise DevTargetError(str(exc)) from exc
    finally:
        adapter.close()

    if not missing:
        return []
    location = target.location or "<location>"
    return [
        f"dev_dataset {qualified} does not exist; dbt will create it on the first "
        "build, which needs bigquery.datasets.create on the project. Without that "
        "permission the build fails there instead, so create it first: "
        f"bq mk --dataset --location={location} {qualified}"
    ]


def _postgres_namespace(
    project: Path, target: str, config: DexConfig, repo_root: Path | str
) -> list[str]:
    """Free: catalog lookups and privilege predicates, no scan.

    Postgres inverts the question. dbt creates the dev schema, so its absence is
    not the failure; the privilege to create it is. And the role that needs the
    privilege is the one in the rendered profile, not the one dex connects as:
    reading the warehouse read-only and building as a role that can write is a
    perfectly ordinary split, and this preflight exists precisely to catch the
    case where the writing role cannot, in fact, write.
    """

    pg = config.postgres
    if pg is None or not pg.dev_schema:
        return []
    role = target_role(project, target)
    if not role:
        # No rendered profile to read a role from (or it names none): there is no
        # role to ask a privilege question about, so there is nothing to check.
        return []

    adapter, note = _open_for_preflight("postgres", repo_root)
    if adapter is None:
        return [note]
    try:
        missing = adapter.missing_dev_namespaces(pg.dev_schema, role=role)
    except Exception as exc:  # the profile's role does not exist in the database
        raise DevTargetError(str(exc)) from exc
    finally:
        adapter.close()

    if not missing:
        return []
    problem = missing[0]
    if problem.startswith("dev_schema"):
        raise DevTargetError(
            f"postgres {problem} does not exist and role {role} may not create "
            "it; create it with:\n"
            f"  CREATE SCHEMA IF NOT EXISTS {pg.dev_schema} AUTHORIZATION "
            f"{role};\n"
            "(dbt creates its dev schema, but only if the role may, so the first "
            "build otherwise dies on a bare permission error), or point "
            "postgres.dev_schema at a schema the role can write"
        )
    raise DevTargetError(
        f"postgres role {role} is missing {problem}; grant it with:\n"
        f"  GRANT USAGE, CREATE ON SCHEMA {pg.dev_schema} TO {role};\n"
        "(dbt builds every model in that schema), or point postgres.dev_schema "
        "at a schema the role can write"
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
