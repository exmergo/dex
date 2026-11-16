"""What an installed Elementary package does for one target (#1305).

Installing ``elementary-data/elementary`` does not decide what it does in a run.
Four switches do, and they vary independently and per target:

- whether any of the package's models is enabled, which Elementary checks
  before its hooks and most of its tests do anything;
- which ``test`` materialization dbt picks, the package's or dbt's own;
- the ``edr_cli_run`` var, which makes both run hooks return early;
- the ``disable_*`` vars, which turn off each upload ``on_run_end`` performs.

:func:`resolve` reads all four into one :class:`ElementaryRuntime`, from a
manifest dbt parsed for the target, the project's ``dbt_project.yml`` and the
installed package's release. It never opens a connection or runs dbt. A manifest
answers for the target it was parsed for and records no target name, so a
caller asking about a target parses for that target first.

Every field is read from what dbt and the package will do, not from whether the
package is installed. Read at dbt-core 1.12.5, in the manifests the fixture's
variants parse to at Elementary 0.16.4 and 0.26.1, and in the package's source
at the release tags each function names:

- **Enabled.** A package model is under ``nodes`` when enabled for the target
  and under ``disabled`` when not, including one disabled with a Jinja
  expression on the target name, which the repository alone cannot settle.
- **Hooks.** The package declares ``on-run-start`` and ``on-run-end`` in its own
  ``dbt_project.yml``, and dbt parses each into an ``operation`` node of the
  package. Disabling the package's models under ``models: elementary:`` disables
  those operations too: they move to ``disabled`` and never run.
- **Materialization.** The manifest records no ``flags``, so the choice is
  rebuilt from dbt's rule (``find_materialization_macro_by_name`` in
  ``dbt/contracts/graph/manifest.py``): the candidate for the most specific
  adapter wins, then the root project over an imported package over dbt itself,
  and while ``require_explicit_package_overrides_for_builtin_materializations``
  is true an imported candidate is passed over wherever dbt has one of its own.
  The flag defaults to true in every dbt-core from 1.8.0 (read at 1.8.0, 1.9.0,
  1.10.0 and 1.11.0), and dbt keeps that default unless the project's whole
  ``flags`` block validates: a string such as ``'false'`` or Jinja leaves the
  flag true (``read_project_flags`` in ``dbt/config/project.py``, run).
- **Vars.** The package reads each var with ``var(name, default)``, takes the
  exact strings ``"true"`` and ``"false"`` in any case as booleans, and otherwise
  uses the value as Jinja would (``get_config_var``). A value the project writes
  as Jinja is reported as unknown, never guessed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

#: The package's name, as its own ``dbt_project.yml`` gives it and as the
#: manifest's ``package_name`` carries it.
PACKAGE = "elementary"

#: dbt's flag that decides whether an imported package may replace one of dbt's
#: own materializations.
OVERRIDE_FLAG = "require_explicit_package_overrides_for_builtin_materializations"

#: Each upload ``on_run_end`` can perform, by the var that turns it off. 0.16.4
#: and 0.26.1 read all five; 0.15.2 has no freshness upload, so
#: :func:`resolve` keeps only the uploads whose var the installed hook reads.
UPLOAD_VARS: Mapping[str, str] = {
    "artifacts": "disable_dbt_artifacts_autoupload",
    "run_results": "disable_run_results",
    "test_results": "disable_tests_results",
    "freshness_results": "disable_freshness_results",
    "invocations": "disable_dbt_invocation_autoupload",
}

#: The defaults the package's ``get_default_config`` gives the vars read here:
#: all false at 0.15.2, 0.16.4 and 0.26.1, where each exists.
DEFAULTS: Mapping[str, bool] = {"edr_cli_run": False} | dict.fromkeys(
    UPLOAD_VARS.values(), False
)

Hooks = Literal["run", "return_early", "disabled", "absent", "unknown"]
Materialization = Literal["elementary", "dbt"]


@dataclass(frozen=True)
class ElementaryRuntime:
    """What the package does in a run for the target the manifest was parsed for.

    ``hooks`` is ``run`` when both hooks run in full, ``return_early`` when they
    run and do nothing (``edr_cli_run``, or the package not enabled where its
    hooks check that), ``disabled`` when dbt disabled them with the package's
    models, ``absent`` when the manifest holds none of them, and ``unknown`` when
    a var they read is unknown. ``uploads`` says which uploads ``on_run_end``
    performs; each is off unless ``hooks`` is ``run`` and the installed release
    has it, and ``None`` where its var is unknown. An upload that is on is one the
    hook attempts: it writes nothing where the package's table has not been
    built, as on a first run in a fresh dev schema. The package also keeps two to
    the commands it recognises (``is_test_command``, ``is_freshness_command``),
    which no record of a target can say.

    ``materialization_macro`` is the ``test`` materialization dbt picks, and
    ``materialization`` says whose behaviour the tests get. It is
    ``elementary`` only where the pick is the package's, or a root-project macro
    that calls the package's, and the package is enabled: where it is not, the
    package's materialization hands each test straight to dbt's
    (``materialize_test``, read at 0.16.4 and 0.26.1). ``notes`` says why a field
    is ``unknown``.
    """

    installed: bool
    release: str | None = None
    enabled: bool = False
    hooks: Hooks = "absent"
    materialization: Materialization = "dbt"
    materialization_macro: str | None = None
    uploads: Mapping[str, bool | None] = field(
        default_factory=lambda: dict.fromkeys(UPLOAD_VARS, False)
    )
    notes: tuple[str, ...] = ()


def not_installed() -> ElementaryRuntime:
    """The record for a project that does not install the package."""

    return ElementaryRuntime(installed=False)


def _project_config(project_yml: str | None) -> dict[str, Any]:
    with contextlib.suppress(yaml.YAMLError):
        loaded = yaml.safe_load(project_yml or "")
        if isinstance(loaded, dict):
            return loaded
    return {}


def _as_bool(value: Any) -> bool | None:
    """Whether the package's ``if`` on this var's value is taken.

    ``get_config_var`` turns the exact strings ``"true"`` and ``"false"``, in any
    case, into booleans and returns anything else as it is, so ``" false"`` is a
    truthy string to the package. Jinja is ``None``: dbt renders a var before
    the package sees it, and only dbt can.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            return None
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
    return bool(value)


def _override_flag(project: Mapping[str, Any]) -> bool:
    """The override flag as dbt reads it from ``dbt_project.yml``.

    dbt validates the whole ``flags`` block and, where it does not validate,
    uses every flag's default, so a string or Jinja value for this flag, or an
    invalid value for any other, leaves it true (``read_project_flags``, 1.12.5,
    run). dbt's own schema is used where dbt is installed; without it, only
    this flag's own value is checked. Flags set in ``profiles.yml``, which dbt
    still reads with a deprecation, are not read here.
    """

    flags = project.get("flags")
    if not isinstance(flags, Mapping):
        return True
    value = flags.get(OVERRIDE_FLAG, True)
    if not isinstance(value, bool):
        return True
    try:
        from dbt.contracts.project import ProjectFlags
    except ImportError:
        return value
    try:
        ProjectFlags.validate(dict(flags))
    except Exception:  # dbt's ValidationError; dbt catches it the same way
        return True
    return value


def var_value(
    name: str,
    project: Mapping[str, Any],
    cli_vars: Mapping[str, Any] | None = None,
) -> bool | None:
    """The boolean the package reads for ``name`` in its own hooks.

    dbt resolves a var for a node of the package from ``--vars`` first, then the
    root project's ``vars.elementary`` scope, then its global ``vars``, and the
    package supplies the default. A value written as Jinja is ``None``, since
    only dbt can render it.
    """

    if cli_vars and name in cli_vars:
        return _as_bool(cli_vars[name])
    declared = project.get("vars")
    if isinstance(declared, Mapping):
        scoped = declared.get(PACKAGE)
        if isinstance(scoped, Mapping) and name in scoped:
            return _as_bool(scoped[name])
        if name in declared:
            return _as_bool(declared[name])
    return DEFAULTS.get(name)


def _package_names(manifest: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for section in ("nodes", "macros"):
        for entry in (manifest.get(section) or {}).values():
            if isinstance(entry, Mapping) and entry.get("package_name"):
                names.add(str(entry["package_name"]))
    return names


def _elementary_entries(
    manifest: Mapping[str, Any], resource_type: str
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """The package's enabled and disabled nodes of one resource type."""

    enabled = [
        node
        for node in (manifest.get("nodes") or {}).values()
        if node.get("package_name") == PACKAGE
        and node.get("resource_type") == resource_type
    ]
    disabled = [
        entry
        for entries in (manifest.get("disabled") or {}).values()
        for entry in entries
        if entry.get("package_name") == PACKAGE
        and entry.get("resource_type") == resource_type
    ]
    return enabled, disabled


def _hooks_check_enabled(manifest: Mapping[str, Any]) -> bool:
    """Whether the package's hooks return early when it is not enabled.

    Read from the hook macros the manifest holds, since it varies by release:
    from 0.20.0 both begin by calling ``is_elementary_enabled()``, and up to
    0.19.0 neither does (read at the tags from 0.17.0).
    """

    macros = manifest.get("macros") or {}
    return all(
        "is_elementary_enabled"
        in str((macros.get(f"macro.{PACKAGE}.{name}") or {}).get("macro_sql") or "")
        for name in ("on_run_start", "on_run_end")
    )


def _materialization(
    manifest: Mapping[str, Any],
    root: str,
    imported: set[str],
    flag: bool,
) -> str | None:
    """The ``test`` materialization dbt picks, by dbt's own rule.

    Locality is dbt's: the root project, then an installed package, then dbt and
    its adapter. The candidates are those for the target's adapter and for
    ``default``. dbt also weighs the adapter's parent adapters (Redshift's is
    Postgres), which a manifest does not record, so a materialization a package
    defines for a parent adapter alone is not seen here. The package defines only
    ``default`` and ``snowflake`` ones at 0.15.2, 0.16.4 and 0.26.1.
    """

    adapter = str((manifest.get("metadata") or {}).get("adapter_type") or "")
    chain = [a for a in (adapter, "default") if a]
    candidates: list[tuple[int, int, str]] = []
    for uid, macro in (manifest.get("macros") or {}).items():
        name = str(macro.get("name") or "")
        suffix = name.removeprefix("materialization_test_")
        if suffix == name or suffix not in chain:
            continue
        package = str(macro.get("package_name") or "")
        specificity = chain.index(suffix)
        locality = 3 if package == root else 2 if package in imported else 1
        candidates.append((specificity, locality, uid))
    if not candidates:
        return None
    # dbt sorts the less specific first and, within a specificity, the lower
    # locality first, then takes the last.
    candidates.sort(key=lambda c: (-c[0], c[1]))
    best = candidates[-1]
    if best[1] == 2 and flag and any(c[1] == 1 for c in candidates):
        allowed = [c for c in candidates if c[1] in (1, 3)]
        best = allowed[-1]
    return best[2]


def _is_elementary_materialization(manifest: Mapping[str, Any], uid: str) -> bool:
    macros = manifest.get("macros") or {}
    macro = macros.get(uid) or {}
    if macro.get("package_name") == PACKAGE:
        return True
    # A root shim hands the test to the package's materialization (variant c).
    depends = (macro.get("depends_on") or {}).get("macros") or []
    return any(
        str(dep).startswith(f"macro.{PACKAGE}.materialization_test_")
        or dep == f"macro.{PACKAGE}.materialize_test"
        for dep in depends
    )


def resolve(
    manifest: Mapping[str, Any],
    project_yml: str | None,
    *,
    release: str | None = None,
    installed_packages: set[str] | None = None,
    cli_vars: Mapping[str, Any] | None = None,
) -> ElementaryRuntime:
    """Read what the package does for the target ``manifest`` was parsed for.

    ``project_yml`` is the root project's ``dbt_project.yml`` text and
    ``release`` the installed package's version, which the manifest does not
    carry. ``installed_packages`` names every installed package, so dbt's
    locality can be told for a package that defines a ``test``
    materialization; without it, every package but the root project and dbt's
    own (``dbt`` and ``dbt_<adapter>``) counts as installed.
    """

    names = _package_names(manifest)
    if PACKAGE not in names and not any(
        e.get("package_name") == PACKAGE
        for entries in (manifest.get("disabled") or {}).values()
        for e in entries
    ):
        return not_installed()

    project = _project_config(project_yml)
    root = str(
        (manifest.get("metadata") or {}).get("project_name")
        or project.get("name")
        or ""
    )
    adapter = str((manifest.get("metadata") or {}).get("adapter_type") or "")
    if installed_packages is None:
        installed_packages = names - {root, "dbt", f"dbt_{adapter}"}
    notes: list[str] = []

    models, _ = _elementary_entries(manifest, "model")
    enabled = bool(models)

    picked = _materialization(
        manifest, root, installed_packages, _override_flag(project)
    )
    materialization: Materialization = (
        "elementary"
        if enabled and picked and _is_elementary_materialization(manifest, picked)
        else "dbt"
    )

    operations, disabled_operations = _elementary_entries(manifest, "operation")
    edr_cli_run = var_value("edr_cli_run", project, cli_vars)
    hooks: Hooks
    if operations:
        if edr_cli_run is None:
            hooks = "unknown"
            notes.append("edr_cli_run is not a boolean the repository can read")
        elif edr_cli_run or (not enabled and _hooks_check_enabled(manifest)):
            hooks = "return_early"
        else:
            hooks = "run"
    elif disabled_operations:
        hooks = "disabled"
    else:
        hooks = "absent"

    # An upload exists only where the release's own on_run_end reads its var:
    # 0.15.2 has no freshness upload, 0.16.4 and 0.26.1 have all five.
    end_sql = str(
        ((manifest.get("macros") or {}).get(f"macro.{PACKAGE}.on_run_end") or {}).get(
            "macro_sql"
        )
        or ""
    )
    uploads: dict[str, bool | None] = {}
    for upload, name in UPLOAD_VARS.items():
        if end_sql and name not in end_sql:
            uploads[upload] = False
        elif hooks == "run":
            off = var_value(name, project, cli_vars)
            uploads[upload] = None if off is None else not off
        elif hooks == "unknown":
            uploads[upload] = None
        else:
            uploads[upload] = False

    return ElementaryRuntime(
        installed=True,
        release=release,
        enabled=enabled,
        hooks=hooks,
        materialization=materialization,
        materialization_macro=picked,
        uploads=uploads,
        notes=tuple(notes),
    )


def read(
    project_dir: Path | str,
    manifest: Mapping[str, Any],
    *,
    cli_vars: Mapping[str, Any] | None = None,
) -> ElementaryRuntime:
    """:func:`resolve` with the project's own files read from ``project_dir``.

    The release is the ``version`` in the installed package's own
    ``dbt_project.yml``, under ``packages-install-path``. A package installed
    from git or a local path carries one too; where none can be read it is
    ``None``.
    """

    from .package_overrides import _installed

    root = Path(project_dir)
    project_yml: str | None = None
    with contextlib.suppress(OSError, UnicodeError):
        project_yml = (root / "dbt_project.yml").read_text(encoding="utf-8")
    release: str | None = None
    installed: set[str] = set()
    for name, directory, _ in _installed(root):
        installed.add(name)
        if name != PACKAGE:
            continue
        with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError):
            own = yaml.safe_load(
                (directory / "dbt_project.yml").read_text(encoding="utf-8")
            )
            if isinstance(own, dict) and own.get("version") is not None:
                release = str(own["version"])
    return resolve(
        manifest,
        project_yml,
        release=release,
        installed_packages=installed or None,
        cli_vars=cli_vars,
    )
