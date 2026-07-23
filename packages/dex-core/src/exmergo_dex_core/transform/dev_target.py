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

import json
from pathlib import Path

import yaml

from ..cache import DEX_DIR
from ..config import CONFIG_FILE, DexConfig
from ..dbt_project import (
    MANIFEST_PATH,
    PROFILES_FILE,
    duckdb_target_path,
    target_auth_method,
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
    "redshift": (("dev_schema", "redshift.dev_schema", "schema"),),
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
        return _snowflake_namespace(project, config, repo_root)
    if config.connector == "databricks":
        return _databricks_namespace(project, config, repo_root)
    if config.connector == "bigquery":
        return _bigquery_namespace(project, config, repo_root)
    if config.connector == "postgres":
        return _postgres_namespace(project, target, config, repo_root)
    if config.connector == "redshift":
        return _redshift_namespace(project, target, config, repo_root)
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


def _snowflake_namespace(
    project: Path, config: DexConfig, repo_root: Path | str
) -> list[str]:
    """Free: SHOW only, no warehouse, so this costs nothing on a billed connector.

    A project with per-layer ``+database:`` config never has any node resolve
    into ``dev_database`` at all, in which case refusing over its absence
    would block a build that was never going to touch it. A compiled manifest
    already answers "does anything target it" for free (issue #110); with no
    manifest to consult yet, there is nothing to rule the database out with,
    so the refusal stays unconditional, same as before.
    """

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
    if _confirmed_out_of_scope(project, "snowflake", "database", target.dev_database):
        return []
    raise DevTargetError(
        f"snowflake {missing[0]} does not exist; create it with:\n"
        f"  CREATE DATABASE IF NOT EXISTS {target.dev_database};\n"
        "(dbt creates schemas but never databases, so the first build cannot "
        "create it), or point snowflake.dev_database at a database the role "
        "can write"
    )


def _databricks_namespace(
    project: Path, config: DexConfig, repo_root: Path | str
) -> list[str]:
    """Free: Unity Catalog REST only, so the billed SQL warehouse is never woken.

    The closest analogue of the Snowflake case: dbt-databricks creates the dev
    schema (``create schema if not exists <catalog>.<schema>``) but never the
    catalog it lives in, so a missing catalog fails the first build from inside
    that statement, naming neither the catalog nor the fix.

    Both checks below defer to a compiled manifest when one exists: a project
    with per-layer ``+catalog:``/``+schema:`` config never resolves any node
    into ``dev_catalog``/``dev_schema`` at all, in which case neither problem
    is one this build will ever hit (issue #110). No manifest yet means
    nothing to rule either out with, so both stay unconditional, same as
    before.
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
        if _confirmed_out_of_scope(
            project, "databricks", "database", target.dev_catalog
        ):
            return []
        raise DevTargetError(
            f"databricks {missing[0]} does not exist; create it with:\n"
            f"  CREATE CATALOG IF NOT EXISTS {target.dev_catalog};\n"
            "(dbt creates schemas but never catalogs, so the first build cannot "
            "create it), or point databricks.dev_catalog at a catalog the principal "
            "can write"
        )
    if not ungranted:
        return []
    if _confirmed_out_of_scope(project, "databricks", "schema", schema):
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


def _bigquery_namespace(
    project: Path, config: DexConfig, repo_root: Path | str
) -> list[str]:
    """Free: a metadata GET, no query, so nothing is billed on a bytes-billed
    connector.

    BigQuery is the connector where the missing dev namespace is *not* fatal:
    dbt-bigquery's ``create_schema`` issues ``CREATE SCHEMA IF NOT EXISTS``, which
    creates the dataset, so an absent one is the normal state before a first build
    and gets a warning rather than a refusal. Refusing it would block a build that
    would have succeeded. What dbt cannot create is the project, and an unreachable
    one is raised by the adapter.

    A project with per-layer ``+schema:`` config (or an equivalent
    ``generate_schema_name`` convention) never has any node resolve into
    ``dev_dataset`` at all, in which case the dataset's absence is irrelevant:
    dbt will never try to create it. A prior build leaves a compiled manifest
    on disk that already answers this for free (issue #110); with no manifest
    to consult yet (a project's first build), there is nothing to rule the
    dataset out with, so the warning stays unconditional, same as before.
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
    bare_dataset = dataset.rpartition(".")[2] if "." in dataset else dataset
    if _confirmed_out_of_scope(project, "bigquery", "schema", bare_dataset):
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

    Both problems this can raise are about ``dev_schema`` specifically, so a
    compiled manifest proving no node resolves into it at all (a per-layer
    ``+schema:`` convention) rules out either one at once: neither existence
    nor privilege on a namespace nothing targets is this build's problem
    (issue #110). No manifest yet means nothing to rule it out with, so the
    refusal stays unconditional, same as before.
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
    if _confirmed_out_of_scope(project, "postgres", "schema", pg.dev_schema):
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


def _redshift_namespace(
    project: Path, target: str, config: DexConfig, repo_root: Path | str
) -> list[str]:
    """Cheap: catalog lookups and privilege predicates, no scan.

    Redshift asks the Postgres question: dbt creates the dev schema, so its
    absence is not the failure; the privilege to create it is. The user that
    needs the privilege is the one in the rendered profile, not the one dex
    connects as. A ``method: iam`` profile is the exception: its database user
    is minted from the caller's identity at dbt runtime regardless of what the
    profile's ``user`` field says, so there is no durable user to ask a
    privilege question about and the check degrades to dbt's own error.

    Both problems this can raise are about ``dev_schema`` specifically, so a
    compiled manifest proving no node resolves into it at all rules out either
    one at once, the same as Postgres (issue #110).
    """

    rs = config.redshift
    if rs is None or not rs.dev_schema:
        return []
    role = target_role(project, target)
    if not role or target_auth_method(project, target) == "iam":
        # No rendered profile to read a user from, or IAM auth: an IAM target
        # mints its database user from the caller's identity at dbt runtime,
        # so whatever the user field says, there is no durable user to ask a
        # privilege question about.
        return []

    adapter, note = _open_for_preflight("redshift", repo_root)
    if adapter is None:
        return [note]
    try:
        missing = adapter.missing_dev_namespaces(rs.dev_schema, role=role)
    except Exception as exc:  # the profile's user does not exist in the database
        raise DevTargetError(str(exc)) from exc
    finally:
        adapter.close()

    if not missing:
        return []
    if _confirmed_out_of_scope(project, "redshift", "schema", rs.dev_schema):
        return []
    problem = missing[0]
    if problem.startswith("dev_schema"):
        raise DevTargetError(
            f"redshift {problem} does not exist and user {role} may not create "
            "it; create it with:\n"
            f"  CREATE SCHEMA IF NOT EXISTS {rs.dev_schema} AUTHORIZATION "
            f"{role};\n"
            "(dbt creates its dev schema, but only if the user may, so the first "
            "build otherwise dies on a bare permission error), or point "
            "redshift.dev_schema at a schema the user can write"
        )
    raise DevTargetError(
        f"redshift user {role} is missing {problem}; grant it with:\n"
        f"  GRANT USAGE, CREATE ON SCHEMA {rs.dev_schema} TO {role};\n"
        "(dbt builds every model in that schema), or point redshift.dev_schema "
        "at a schema the user can write"
    )


# The folder layers a layered init routes to their own schemas, and how many
# pre-existing object names a content warning spells out before summarizing.
_LAYER_SCHEMAS = ("staging", "intermediate", "marts")
_CONTENT_SAMPLE_CAP = 5


def content_check(
    config: DexConfig, repo_root: Path | str = ".", *, layered: bool = False
) -> list[str]:
    """Warn when a namespace the new project will build into already holds
    tables or views.

    Run at ``transform init`` time, where a colliding dev namespace is still
    trivial to rename; discovered any later and it surfaces as a confusing
    model-name clash in the middle of a build. Free on every connector (each
    ``list_namespace_objects`` rides the same metadata path as the build
    preflight) and advisory by design: existing content is a warning, never a
    refusal, and a connection dex cannot open degrades to a note, because init
    is credential-optional and must stay that way.

    DuckDB's base namespace is deliberately not checked: the dev target is the
    same file as the source warehouse, so "the namespace holds objects" is true
    of every working setup and warning on it would only train users to skim.
    Only the layered schemas, which are genuinely dbt-owned, are probed there.
    """

    target_name = config.dbt_target or "dev"
    connector = config.connector
    block = getattr(config, connector, None)
    if block is None:
        return []

    # Each probe is (display name for the warning, args for the connector's
    # list_namespace_objects). The layered names mirror what the scaffolded
    # generate_schema_name macro will compose: <layer>_<target name>.
    layer_names = [f"{layer}_{target_name}" for layer in _LAYER_SCHEMAS]
    probes: list[tuple[str, tuple[str, ...]]] = []
    if connector == "bigquery" and block.dev_dataset:
        gcp_project, _, dataset = block.dev_dataset.rpartition(".")
        gcp_project = gcp_project or (block.project or "")
        prefix = f"{gcp_project}." if gcp_project else ""
        probes.append((f"{prefix}{dataset}", (dataset,)))
        if layered:
            probes.extend((f"{prefix}{name}", (name,)) for name in layer_names)
    elif connector == "snowflake" and block.dev_database:
        from .init import _DEFAULT_SF_DEV_SCHEMA

        database = block.dev_database.upper()
        schemas = [(block.dev_schema or _DEFAULT_SF_DEV_SCHEMA).upper()]
        if layered:
            schemas.extend(name.upper() for name in layer_names)
        probes.extend(
            (f"{database}.{schema}", (database, schema)) for schema in schemas
        )
    elif connector == "databricks" and block.dev_catalog:
        from .init import _DEFAULT_DBX_DEV_SCHEMA

        schemas = [block.dev_schema or _DEFAULT_DBX_DEV_SCHEMA]
        if layered:
            schemas.extend(layer_names)
        probes.extend(
            (f"{block.dev_catalog}.{schema}", (block.dev_catalog, schema))
            for schema in schemas
        )
    elif connector in ("postgres", "redshift") and block.dev_schema:
        schemas = [block.dev_schema]
        if layered:
            schemas.extend(layer_names)
        probes.extend((schema, (schema,)) for schema in schemas)
    elif connector == "duckdb" and layered:
        probes.extend((name, (name,)) for name in layer_names)
    if not probes:
        return []

    from ..connect import open_adapter

    degraded = (
        "could not check the dev namespaces for existing content ({kind}: "
        "{detail}); a name collision would surface on the first build instead"
    )
    try:
        adapter = open_adapter(connector=connector, repo_root=repo_root)
    except Exception as exc:
        return [degraded.format(kind=type(exc).__name__, detail=exc)]

    warnings: list[str] = []
    try:
        for display, args in probes:
            objects = adapter.list_namespace_objects(*args)
            if not objects:
                continue
            shown = ", ".join(objects[:_CONTENT_SAMPLE_CAP])
            beyond = len(objects) - _CONTENT_SAMPLE_CAP
            listing = shown if beyond <= 0 else f"{shown}, and {beyond} more"
            noun = "object" if len(objects) == 1 else "objects"
            warnings.append(
                f"dev namespace {display} already contains {len(objects)} "
                f"{noun} ({listing}); a dbt build writes alongside them and "
                "replaces same-named relations, so pick a different dev "
                "namespace if this content is unrelated"
            )
    except Exception as exc:
        return [degraded.format(kind=type(exc).__name__, detail=exc)]
    finally:
        adapter.close()
    return warnings


# Resource types that physically write into a schema; a test asserts against
# one but creates nothing, and an ephemeral model compiles a schema it will
# never actually issue a CREATE against (it inlines into whatever depends on
# it), so neither counts as "targeting" a namespace for this check.
_MATERIALIZING_RESOURCE_TYPES = {"model", "seed", "snapshot"}


def _manifest_namespaces(project: Path, field: str) -> set[str] | None:
    """Every distinct value a compiled manifest's model/seed/snapshot nodes
    resolve to at the given namespace level, or ``None`` when there is no
    manifest to read yet (a project's first build, before dbt has ever
    compiled it here) or it cannot be parsed.

    ``field`` is one of dbt's own generic three-part naming keys on a node:
    ``"schema"`` for a schema/dataset check (BigQuery's dataset, Postgres's
    and Redshift's schema), ``"database"`` for a database/catalog check
    (Snowflake's database, and Databricks's catalog -- dbt-databricks maps
    the catalog onto this same generic key).

    Reads the file directly rather than going through :func:`dbt_project.load`
    (which raises on a corrupt manifest and scans every project file just to
    reach it): this is a best-effort preflight signal, so any problem reading
    it degrades to ``None`` -- the caller falls back to its unconditional
    check, the same as before this existed (issue #110).
    """

    manifest_file = project / MANIFEST_PATH
    if not manifest_file.is_file():
        return None
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    nodes = manifest.get("nodes") if isinstance(manifest, dict) else None
    if not isinstance(nodes, dict):
        return None

    values: set[str] = set()
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in _MATERIALIZING_RESOURCE_TYPES:
            continue
        node_config = node.get("config")
        materialized = (
            node_config.get("materialized") if isinstance(node_config, dict) else None
        )
        if materialized == "ephemeral":
            continue
        value = node.get(field)
        if isinstance(value, str) and value:
            values.add(value)
    return values


def _confirmed_out_of_scope(
    project: Path, connector: str, field: str, candidate: str
) -> bool:
    """True only when a compiled manifest exists and proves nothing resolves
    into ``candidate`` at this namespace level (see :func:`_manifest_namespaces`
    for what ``field`` means). False when there is no manifest yet (nothing to
    prove absence with) or something does target it -- the caller's existing
    refusal or warning stands either way, unchanged from before this existed.

    Folds case for connectors whose identifiers are case-insensitive
    (``_CASE_FOLDING``), the same rule the drift check already uses, so
    ``DBT_DEV`` and ``dbt_dev`` are recognized as the same namespace here too.
    """

    namespaces = _manifest_namespaces(project, field)
    if namespaces is None:
        return False
    fold = connector in _CASE_FOLDING
    pool = {(n.upper() if fold else n) for n in namespaces}
    return (candidate.upper() if fold else candidate) not in pool


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
