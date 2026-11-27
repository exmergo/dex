"""Where the Elementary package writes its tables in production, and the
source allowlist entry that lets dex read them (#1473).

``dex maintain incidents`` reads the package's run history (``dbt_invocations``,
``elementary_test_results``) from the schema it writes. The source allowlist in
``.dex/config.yml`` is a committed cost boundary that ``--scope`` can only
narrow, so a schema outside it is refused before any connection opens.
``dex init`` proposes the entry instead of leaving that refusal to be found.

The schema is production's, worked out statically the way dbt names it there,
since dex never runs dbt against production:

- **The custom schema** is the ``+schema`` the root ``dbt_project.yml`` gives the
  package's models (``models.elementary``, else a root-level ``models``), else
  the package's own ``dbt_project.yml``, else the one an install dex stored or
  wrote sets. None of them means the models build in production's own schema.
- **The naming** is dbt's default (``<prod schema>_<custom>``) or the layered
  override ``transform init --layered-schemas`` scaffolds (``<custom>_<prod
  target>``). Any other ``generate_schema_name`` is the project's own Jinja,
  which dex does not run, so the schema is unresolved.
- **Production's schema** is the committed ``<connector>.prod``, else the
  profile output named by ``dbt_prod_target`` where its values are written as
  literals.

A value dex cannot read (Jinja in ``+schema`` or ``+database``, a ``+schema`` set
on a folder inside the package) leaves the schema unresolved too, with what to
commit or write by hand. Nothing here reads the manifest dbt last parsed: it was
parsed for the dev target, and the dev schema is never a guess at production's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

#: Why the production schema is unresolved. Stable codes a caller can branch on.
PROD_UNSET = "prod_unset"
PROD_TARGET_UNSET = "prod_target_unset"
CUSTOM_SCHEMA_NAMING = "custom_schema_naming"
TEMPLATED_CONFIG = "templated_config"
NESTED_SCHEMA_CONFIG = "nested_schema_config"
PROJECT_UNREADABLE = "project_unreadable"

#: The comment the allowlist entry carries in ``.dex/config.yml``: why it is
#: there and what reading it costs. Written without an apostrophe, so the
#: proposed command can quote it with single quotes in any shell.
COMMENT = (
    "The schema the Elementary package writes its run history to "
    "(dbt_invocations, elementary_test_results). dex maintain incidents and dex "
    "maintain diagnose read it there: five aggregate statements over the newest "
    "20 runs per call, priced before any of them runs."
)


@dataclass(frozen=True)
class Located:
    """A namespace the package's tables are built in."""

    database: str | None
    schema: str

    def spelled(self) -> str:
        return f"{self.database}.{self.schema}" if self.database else self.schema


@dataclass(frozen=True)
class Resolved:
    """Production's schema for the package, and what settled it: ``prod``
    (the committed ``<connector>.prod``) or ``profile`` (the profile output
    ``dbt_prod_target`` names)."""

    located: Located
    source: str


@dataclass(frozen=True)
class Unresolved:
    """Production's schema could not be worked out: why, and the command that
    would settle it (a template to fill in where dex cannot settle it)."""

    reason_code: str
    detail: str
    ask: str | None


def entries(
    connector: str, located: Located, *, project: str | None = None
) -> list[str]:
    """How the connector's source allowlist spells ``located``: the forms one
    entry may take, finest first. The first is what a proposed entry adds.

    On BigQuery a bare dataset entry reads the connection's own ``project``, so
    it spells ``located`` only where that is the project it sits in."""

    qualified = located.spelled()
    if connector == "bigquery":
        if (
            located.database
            and project
            and located.database.casefold() != project.casefold()
        ):
            return [qualified]
        return [qualified, located.schema]
    if connector == "snowflake":
        return [qualified, located.schema]
    if connector in {"databricks", "trino"}:
        return [qualified]
    return [located.schema]


def allowlist_command(connector: str, committed: Sequence[str], entry: str) -> str:
    """The ``dex config set`` that adds ``entry`` to the committed allowlist,
    with the comment saying why it is there. Shown, never run: ``config set``
    answers with the diff and writes only on ``--confirm``."""

    from shlex import quote

    from ..connect import _SCOPE_FIELDS

    field = _SCOPE_FIELDS[connector]
    listed = quote("[" + ", ".join([*committed, entry]) + "]")
    return f"dex config set {connector}.{field} {listed} --comment {quote(COMMENT)}"


def allowlist_template(connector: str, committed: Sequence[str]) -> str:
    """The command to fill in where dex cannot work the schema out."""

    return allowlist_command(
        connector, committed, "<the schema the Elementary package writes in production>"
    )


def production_namespace(
    project_dir: Path | str,
    config: Any,
    connector: str,
    *,
    planned_schema: str | None = None,
) -> Resolved | Unresolved:
    """Where the package's models build in production, or why that is unknown.

    ``planned_schema`` is the ``+schema`` an install dex stored as a plan and the
    project has not applied yet: the root ``dbt_project.yml`` does not say it
    yet, and it will once the plan lands.
    """

    from ..config import _PROD_SPELLING

    project = Path(project_dir)
    try:
        root = _yaml(project / "dbt_project.yml")
    except (OSError, yaml.YAMLError) as exc:
        return Unresolved(
            PROJECT_UNREADABLE,
            f"dbt_project.yml could not be read ({type(exc).__name__})",
            None,
        )
    placement = _placement(project, root, planned_schema)
    if isinstance(placement, Unresolved):
        return _with_template(placement, connector, config)
    custom, database = placement
    naming = _naming(project, root)
    if naming == "custom":
        return _with_template(
            Unresolved(
                CUSTOM_SCHEMA_NAMING,
                "the project's own generate_schema_name decides the schema the "
                "package writes in production, and dex does not run it",
                None,
            ),
            connector,
            config,
        )
    target = getattr(config, connector, None)
    prod_target = getattr(config, "dbt_prod_target", None)
    base = _prod_base(project, connector, target, prod_target)
    if base is None:
        return Unresolved(
            PROD_UNSET,
            f"{connector}.prod is not set in .dex/config.yml"
            + (
                f", and the profile output for dbt_prod_target ({prod_target}) does "
                "not name its schema as a literal"
                if prod_target
                else ", and no dbt_prod_target names a profile output to read"
            )
            + ", so production's schema is not known",
            f"dex config set {connector}.prod '<{_PROD_SPELLING[connector]}>'",
        )
    container, prod_schema, source = base
    if custom is None:
        schema = prod_schema
    elif naming == "layered":
        if not prod_target:
            return Unresolved(
                PROD_TARGET_UNSET,
                "the project's generate_schema_name names a custom schema "
                "<schema>_<target name>, and dbt_prod_target is not set in "
                ".dex/config.yml",
                "dex config set dbt_prod_target '<the production target name>'",
            )
        schema = f"{custom}_{prod_target}"
    else:
        schema = f"{prod_schema}_{custom}"
    return Resolved(Located(database or container, schema), source)


def _yaml(path: Path) -> Mapping[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, Mapping) else {}


def _templated(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _config(block: Any, name: str) -> Any:
    """``+name`` in a ``models:`` block, else ``name`` written without the plus
    where it is not a folder (a folder's value is a mapping)."""

    if not isinstance(block, Mapping):
        return None
    if f"+{name}" in block:
        return block[f"+{name}"]
    value = block.get(name)
    return None if isinstance(value, Mapping) else value


def _nested(block: Any) -> bool:
    """Whether a folder inside ``block`` sets its own schema or database."""

    if not isinstance(block, Mapping):
        return False
    for key, value in block.items():
        if str(key).startswith("+") or not isinstance(value, Mapping):
            continue
        if any(_config(value, name) is not None for name in ("schema", "database")):
            return True
        if _nested(value):
            return True
    return False


def _placement(
    project: Path, root: Mapping[str, Any], planned_schema: str | None
) -> tuple[str | None, str | None] | Unresolved:
    """The package's custom schema and database, as the project configures
    them: ``(schema, database)``, either ``None`` where nothing sets it."""

    models = root.get("models")
    package = models.get("elementary") if isinstance(models, Mapping) else None
    from .package_overrides import _installed

    # Through the reader every package lookup uses, so packages-install-path
    # is honored rather than assuming dbt_packages/.
    own: Mapping[str, Any] = {}
    for name, directory, _ in _installed(project):
        if name == "elementary":
            try:
                own = _yaml(directory / "dbt_project.yml")
            except (OSError, yaml.YAMLError):
                own = {}
    own_models = own.get("models")
    own_package = (
        own_models.get("elementary") if isinstance(own_models, Mapping) else None
    )
    if _nested(package) or _nested(own_package):
        return Unresolved(
            NESTED_SCHEMA_CONFIG,
            "a folder inside the Elementary package sets its own schema or "
            "database in dbt_project.yml, so its tables may land in more than one "
            "schema",
            None,
        )
    found: dict[str, Any] = {}
    for name in ("schema", "database"):
        for block in (package, models, own_package):
            value = _config(block, name)
            if value is not None:
                found[name] = value
                break
    if "schema" not in found and planned_schema:
        found["schema"] = planned_schema
    for name, value in found.items():
        if _templated(value):
            return Unresolved(
                TEMPLATED_CONFIG,
                f"the Elementary package's +{name} in dbt_project.yml is Jinja, "
                "which dex does not render for the production target",
                None,
            )
    schema, database = found.get("schema"), found.get("database")
    return (
        str(schema).strip() if schema else None,
        str(database).strip() if database else None,
    )


def _naming(project: Path, root: Mapping[str, Any]) -> str:
    from .test_cost import schema_naming

    paths = root.get("macro-paths")
    view = SimpleNamespace(
        root=project,
        macro_paths=[str(p) for p in paths] if isinstance(paths, list) else ["macros"],
    )
    return schema_naming(view)


#: Which profile keys name the namespace, per connector: the container first
#: (``None`` where the allowlist spells the schema alone), then the schema.
_PROFILE_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bigquery": (("project", "database"), ("dataset", "schema")),
    "snowflake": (("database",), ("schema",)),
    "databricks": (("catalog",), ("schema",)),
    "trino": (("catalog", "database"), ("schema",)),
    "postgres": ((), ("schema",)),
    "redshift": ((), ("schema",)),
    "clickhouse": ((), ("schema",)),
}


def _prod_base(
    project: Path, connector: str, target: Any, prod_target: str | None
) -> tuple[str | None, str, str] | None:
    """Production's container and schema, and where they were read:
    ``(container, schema, source)``."""

    prod = getattr(target, "prod", None) if target is not None else None
    if prod:
        parts = str(prod).split(".")
        if len(parts) == 2:
            return parts[0], parts[1], "prod"
        return _default_container(connector, target), parts[-1], "prod"
    if not prod_target or connector not in _PROFILE_KEYS:
        return None
    from ..dbt_project import target_identifiers

    identifiers = target_identifiers(project, prod_target)
    containers, schemas = _PROFILE_KEYS[connector]

    def first(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = identifiers.get(key)
            if value and not _templated(value):
                return str(value).strip()
        return None

    schema = first(schemas)
    if not schema:
        return None
    container = first(containers) if containers else None
    if containers and container is None:
        if any(_templated(identifiers.get(key)) for key in containers):
            return None
        container = _default_container(connector, target)
    return container, schema, "profile"


def _default_container(connector: str, target: Any) -> str | None:
    """The container a one-part production schema sits in: BigQuery's
    configured project. The other connectors spell production in full or scope
    the schema alone."""

    if connector == "bigquery" and target is not None:
        return getattr(target, "project", None) or None
    return None


def _with_template(found: Unresolved, connector: str, config: Any) -> Unresolved:
    from ..connect import _SCOPE_FIELDS

    target = getattr(config, connector, None)
    committed = (
        [str(e) for e in getattr(target, _SCOPE_FIELDS[connector], None) or []]
        if target is not None and connector in _SCOPE_FIELDS
        else []
    )
    return Unresolved(
        found.reason_code, found.detail, allowlist_template(connector, committed)
    )
