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
import re
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
    # A root shim hands the test to the package's materialization (variant c),
    # directly or through macros of its own: dbt records each call on the macro
    # that makes it, so the calls are followed to any depth.
    seen: set[str] = set()
    todo = [str(dep) for dep in (macro.get("depends_on") or {}).get("macros") or []]
    while todo:
        dep = todo.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if (
            dep.startswith(f"macro.{PACKAGE}.materialization_test_")
            or dep == f"macro.{PACKAGE}.materialize_test"
        ):
            return True
        called = (macros.get(dep) or {}).get("depends_on") or {}
        todo.extend(str(d) for d in called.get("macros") or [])
    return False


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
        # The enabled check comes first because, from 0.20.0, the hooks make it
        # before they read `edr_cli_run`, so that var cannot matter there.
        if not enabled and _hooks_check_enabled(manifest):
            hooks = "return_early"
        elif edr_cli_run is None:
            hooks = "unknown"
            notes.append("edr_cli_run is not a boolean the repository can read")
        elif edr_cli_run:
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


#: The resource types a manifest keeps outside ``nodes`` that can refer to a
#: model. dbt raises when any of them, or any node but a test, refers to a
#: disabled model; for a test it only warns (``parser/manifest.py``, 1.11.11).
_REFERRING_SECTIONS = ("exposures", "metrics", "semantic_models", "saved_queries")


def package_model_dependents(manifest: Mapping[str, Any]) -> list[str]:
    """What outside the package refers to one of its models, by unique id.

    Disabling the package's models makes dbt refuse the parse wherever a node
    other than a test refers to one. For a test dbt only warns, and disables
    the test, so a test of the project's own that reads a package model would
    drop out of the run unseen; it counts too. A caller that wants the models
    disabled leaves them on where this is not empty. The package's models are
    looked for in ``nodes`` and in ``disabled``, since a project may already
    disable them for some targets.
    """

    models = {
        uid
        for uid, node in (manifest.get("nodes") or {}).items()
        if node.get("package_name") == PACKAGE and node.get("resource_type") == "model"
    } | {
        uid
        for uid, entries in (manifest.get("disabled") or {}).items()
        if any(
            e.get("package_name") == PACKAGE and e.get("resource_type") == "model"
            for e in entries
        )
    }
    if not models:
        return []
    referring: list[str] = []
    entries = list((manifest.get("nodes") or {}).items())
    for section in _REFERRING_SECTIONS:
        entries.extend((manifest.get(section) or {}).items())
    for uid, node in entries:
        if node.get("package_name") == PACKAGE:
            continue
        reads = (node.get("depends_on") or {}).get("nodes") or []
        if any(dep in models for dep in reads):
            referring.append(uid)
    return sorted(referring)


def _test_macros(manifest: Mapping[str, Any], uids: list[str]) -> set[str]:
    """The package's ``test_*`` macros that ``uids`` reach.

    dbt records a call on the macro that makes it, not on the test, so a
    project's own generic test that wraps one of the package's is followed
    through: every call out of a macro outside the package is followed, however
    many macros lie between. Inside the package only the calls into its other
    ``test_*`` macros are, so the set the package's own tests reach is the one
    its releases were read for.
    """

    macros = manifest.get("macros") or {}
    inside = f"macro.{PACKAGE}."
    prefix = f"{inside}test_"
    seen: set[str] = set()
    todo = list(uids)
    while todo:
        uid = todo.pop()
        if uid in seen:
            continue
        seen.add(uid)
        if uid.startswith(inside) and not uid.startswith(prefix):
            continue
        called = ((macros.get(uid) or {}).get("depends_on") or {}).get("macros") or []
        todo.extend(
            str(dep)
            for dep in called
            if not uid.startswith(inside) or str(dep).startswith(prefix)
        )
    return {uid for uid in seen if uid.startswith(prefix)}


def monitoring_tests(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    """The enabled tests whose package test reads ``is_elementary_enabled()``.

    Read from the macros the manifest holds, since the set varies by release.
    These are the package's monitoring tests, among them its anomaly tests,
    ``schema_changes``, ``json_schema``, ``python`` and, from 0.20.0, the
    freshness and SLA tests. Each returns no rows wherever the package is not
    enabled, so it checks nothing there; where it is enabled, each one reads
    the package's history or writes a table into its schema, or both.
    ``volume_anomalies`` reads it through ``test_table_anomalies``, so the
    macros a test macro calls count too. The ``*_with_context`` tests do not
    read it at 0.16.4 or 0.26.1. Returned as ``{node name: the nodes it
    reads}``, sorted by name, so a caller can keep only the tests its
    selection reaches.
    """

    found = {
        str(node.get("name")): sorted((node.get("depends_on") or {}).get("nodes") or [])
        for node in _monitoring_test_nodes(manifest).values()
    }
    return dict(sorted(found.items()))


def _monitoring_test_nodes(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The enabled monitoring tests :func:`monitoring_tests` names, by unique id."""

    macros = manifest.get("macros") or {}
    found: dict[str, Mapping[str, Any]] = {}
    for uid, node in (manifest.get("nodes") or {}).items():
        if node.get("resource_type") != "test":
            continue
        depends = node.get("depends_on") or {}
        if any(
            "is_elementary_enabled" in str((macros.get(m) or {}).get("macro_sql"))
            for m in _test_macros(manifest, list(depends.get("macros") or []))
        ):
            found[str(uid)] = node
    return found


#: What Elementary's own config gives an anomaly test's ``days_back`` when
#: neither the test nor the project sets it (``get_default_config``, 14 at
#: 0.15.2, 0.16.4 and 0.26.1).
DEFAULT_DAYS_BACK = 14

#: The codes :attr:`UnpricedWork.codes` reports, one per kind of work a build's
#: estimate leaves out. Carried in ``cost.unpriced``.
HOOK_WORK = "elementary_hooks"
MONITORING_WORK = "elementary_monitoring_tests"
EXTRAS_WORK = "elementary_test_extras"


@dataclass(frozen=True)
class GatedTest:
    """One selected monitoring test, with the history window it reads.

    ``days_back`` is the window an anomaly test computes its metrics over, and
    ``days_back_source`` says where it came from: ``test`` (the test's own
    argument or config), ``model`` (the tested node's Elementary config),
    ``project`` (the ``days_back`` var), ``default`` (the
    package's 14), ``unread`` where the test sets ``training_period`` or
    ``seasonality``, which Elementary turns into a window of its own, and
    ``unknown`` for a value only dbt can render. A test that is not an anomaly
    test has no window: both are ``None``.
    """

    name: str
    days_back: int | None = None
    days_back_source: str | None = None


@dataclass(frozen=True)
class UnpricedWork:
    """What a build runs for the Elementary package that no compiled node shows.

    A build is priced from its compiled nodes. Three kinds of the package's work
    are never compiled into one (#1309):

    - ``hooks``: the ``on-run-start`` and ``on-run-end`` hooks, which create the
      package's tables and upload the run's results to them. ``True`` where
      the record says they run, and where it cannot say (``unknown``);
    - ``gated_tests``: the selected monitoring tests, which compute metrics
      over their ``days_back`` window and write tables in the package's
      schema. Only where the package is enabled, since they check;
    - ``extras_tests``: the selected tests Elementary's ``test``
      materialization handles, for each of which it can count the failed rows
      and query a sample of them. Only where dbt picks that materialization and
      the package is enabled. The package's anomaly and schema-change tests
      collect their own rows and are not among them.
    """

    hooks: bool = False
    hooks_state: str | None = None
    gated_tests: tuple[GatedTest, ...] = ()
    extras_tests: tuple[str, ...] = ()

    @property
    def codes(self) -> list[str]:
        """One code per kind of work left out; any code makes the estimate a floor."""

        return [
            code
            for code, present in (
                (HOOK_WORK, self.hooks),
                (MONITORING_WORK, bool(self.gated_tests)),
                (EXTRAS_WORK, bool(self.extras_tests)),
            )
            if present
        ]

    def as_dict(self) -> dict[str, Any]:
        """The structured form, for ``data.unpriced``."""

        return {
            "package": PACKAGE,
            "work": self.codes,
            "hooks": self.hooks_state if self.hooks else None,
            "monitoring_tests": [
                {
                    "name": t.name,
                    "days_back": t.days_back,
                    "days_back_source": t.days_back_source,
                }
                for t in self.gated_tests[:_LISTED]
            ],
            "monitoring_test_count": len(self.gated_tests),
            "materialized_test_count": len(self.extras_tests),
        }

    def warnings(self) -> list[str]:
        """One sentence per kind of work, each saying the estimate is a floor."""

        out: list[str] = []
        if self.hooks:
            may = "may run" if self.hooks_state == "unknown" else "run"
            out.append(
                f"the Elementary package's on-run-start and on-run-end hooks {may} "
                "in this build and are not priced: they create the package's "
                "tables and upload this run's results, invocation and artifacts "
                "to them, as its disable_* vars leave on, so the estimate is a "
                "floor"
            )
        if self.gated_tests:
            windows = [
                f"{t.name} over {t.days_back} days"
                if t.days_back is not None
                else f"{t.name} over a window dbt decides"
                for t in self.gated_tests
                if t.days_back_source is not None
            ]
            shown = _listing(windows) if windows else ""
            out.append(
                f"{_plural(len(self.gated_tests), 'Elementary monitoring test')} "
                "selected run metric queries and create tables in the package's "
                "schema that no compiled node shows"
                + (f" (the anomaly tests read their history: {shown})" if shown else "")
                + "; none of it is priced, so the estimate is a floor"
            )
        if self.extras_tests:
            out.append(
                "Elementary's test materialization handles "
                f"{_plural(len(self.extras_tests), 'selected test')}, and for each "
                "one it can count the failed rows and query a sample of them, "
                "statements no compiled node shows; neither is priced, so the "
                "estimate is a floor"
            )
        return out


#: How many monitoring tests a warning or ``data.unpriced`` lists by name.
_LISTED = 10


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _listing(items: list[str]) -> str:
    shown = ", ".join(items[:_LISTED])
    if len(items) > _LISTED:
        shown += f", and {len(items) - _LISTED} more"
    return shown


def _elementary_config(node: Mapping[str, Any] | None) -> dict[str, Any]:
    """The package's settings on a model or source, as the package reads them.

    ``config.elementary``, then ``config.meta.elementary``, then
    ``meta.elementary``, each overriding the one before
    (``get_elementary_config_from_node``, 0.16.4 and 0.26.1).
    """

    merged: dict[str, Any] = {}
    if not node:
        return merged
    config = node.get("config") or {}
    for place in (
        config.get("elementary"),
        (config.get("meta") or {}).get("elementary"),
        (node.get("meta") or {}).get("elementary"),
    ):
        if isinstance(place, Mapping):
            merged.update(place)
    return merged


def _tested_node(
    manifest: Mapping[str, Any], node: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The model, snapshot, seed or source a test is attached to."""

    reads = [node.get("attached_node")]
    reads += list((node.get("depends_on") or {}).get("nodes") or [])
    for uid in reads:
        if not uid:
            continue
        found = (manifest.get("nodes") or {}).get(uid) or (
            manifest.get("sources") or {}
        ).get(uid)
        if found:
            return found
    return None


def _days_back(
    node: Mapping[str, Any],
    project: Mapping[str, Any],
    cli_vars: Mapping[str, Any] | None,
    tested: Mapping[str, Any] | None = None,
) -> tuple[int | None, str | None]:
    """The ``days_back`` window of an anomaly test, and where it came from.

    Elementary takes a test argument first, then the tested node's own
    Elementary config, then the ``days_back`` var, then its default
    (``get_test_argument``, ``get_anomalies_test_configuration``).
    ``training_period`` and ``seasonality`` each change the window by rules of
    their own (``training_period_to_days_back``, ``get_days_back``), so a test
    or model setting either is ``unread`` rather than guessed.
    """

    meta = node.get("test_metadata") or {}
    if "anomal" not in str(meta.get("name") or ""):
        return None, None
    kwargs = meta.get("kwargs") or {}
    config = node.get("config") or {}
    model = _elementary_config(tested)
    for place in (kwargs, config, config.get("meta") or {}, model):
        if isinstance(place, Mapping) and (
            "training_period" in place or "seasonality" in place
        ):
            return None, "unread"
    value: Any = None
    source = "default"
    for place, where in ((kwargs, "test"), (config, "test"), (model, "model")):
        if isinstance(place, Mapping) and place.get("days_back") is not None:
            value, source = place["days_back"], where
            break
    else:
        if cli_vars and "days_back" in cli_vars:
            value, source = cli_vars["days_back"], "project"
        else:
            declared = project.get("vars")
            if isinstance(declared, Mapping):
                scoped = declared.get(PACKAGE)
                if isinstance(scoped, Mapping) and "days_back" in scoped:
                    value, source = scoped["days_back"], "project"
                elif "days_back" in declared:
                    value, source = declared["days_back"], "project"
    if source == "default":
        return DEFAULT_DAYS_BACK, source
    if isinstance(value, bool):
        return None, "unknown"
    if isinstance(value, int):
        return value, source
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()), source
    return None, "unknown"


def _collects_own_rows(node: Mapping[str, Any]) -> bool:
    """Whether Elementary's materialization hands this test straight to dbt's.

    It does for the package's own tests other than the ``*_with_context`` ones
    (``materialize_test``, read at 0.16.4 and 0.26.1), which collect their own
    result rows: no failed-row count, no sample query.
    """

    meta = node.get("test_metadata") or {}
    return meta.get("namespace") == PACKAGE and not str(
        meta.get("name") or ""
    ).endswith("_with_context")


def unpriced_work(
    runtime: ElementaryRuntime,
    manifest: Mapping[str, Any],
    project_yml: str | None = None,
    *,
    selected: set[str] | None = None,
    cli_vars: Mapping[str, Any] | None = None,
) -> UnpricedWork:
    """What the package will run in a build that a compiled-node estimate leaves out.

    ``runtime`` is the record :func:`resolve` read for the build's target, and
    ``manifest`` the one parsed for it. ``selected`` names the nodes the build
    selects, by unique id (the pricing compile's own selection); ``None`` takes
    every enabled test. Each kind of work is keyed off the record alone, never
    off whether the package is installed:

    - the hooks where ``hooks`` is ``run`` (or ``unknown``);
    - the monitoring tests where ``enabled``;
    - the per-test extras where ``materialization`` is ``elementary``, which
      the record only says where the package is enabled.

    A project without the package, or one that disables it for the target,
    gets an empty record.
    """

    if not runtime.installed:
        return UnpricedWork()
    project = _project_config(project_yml)
    hooks = runtime.hooks in ("run", "unknown")

    def chosen(uid: str) -> bool:
        return selected is None or uid in selected

    gated: list[GatedTest] = []
    if runtime.enabled:
        for uid, node in sorted(
            _monitoring_test_nodes(manifest).items(),
            key=lambda item: str(item[1].get("name")),
        ):
            if not chosen(uid):
                continue
            days, source = _days_back(
                node, project, cli_vars, _tested_node(manifest, node)
            )
            gated.append(GatedTest(str(node.get("name")), days, source))

    extras: list[str] = []
    if runtime.materialization == "elementary":
        extras = sorted(
            str(node.get("name"))
            for uid, node in (manifest.get("nodes") or {}).items()
            if node.get("resource_type") == "test"
            and chosen(str(uid))
            and not _collects_own_rows(node)
        )

    return UnpricedWork(
        hooks=hooks,
        hooks_state=runtime.hooks if hooks else None,
        gated_tests=tuple(gated),
        extras_tests=tuple(extras),
    )


#: Elementary's ``no_results_query()``: a test that compiled to it returns no
#: rows whatever the data holds, so it passed having checked nothing. The text is
#: the same at 0.16.4 and 0.26.1 and the macro has no adapter dispatch; the
#: literal it compares ``num`` with is matched loosely, in case a release moves
#: it.
_NO_RESULTS = re.compile(
    r"\s*with\s+nothing\s+as\s*\(\s*select\s+1\s+as\s+num\s*\)\s*"
    r"select\s+\*\s+from\s+nothing\s+where\s+num\s*=\s*\d+\s*;?\s*",
    re.IGNORECASE,
)

#: The package's tests whose verdict rests on what earlier runs stored in its
#: schema, by test macro (#1308): the anomaly tests, which score this run's
#: metrics against a training set of earlier ones; ``schema_changes``, which
#: compares against the last column snapshot; ``volume_threshold`` and
#: ``metric_stability``, which compare against cached metrics; and
#: ``execution_sla``, which reads the runs ``on_run_end`` recorded. Each passes
#: where that history is missing. Read at 0.26.1 (0.16.4 carries the anomaly
#: tests and ``schema_changes`` only). Not Elementary's own ``get_test_type``
#: map, which leaves the last three out. ``schema_changes_from_baseline`` reads
#: the baseline the YAML declares, ``json_schema`` and ``data_freshness_sla``
#: read this run's rows, and the ``*_with_context`` tests wrap dbt's own, so none
#: is here.
HISTORY_TESTS = frozenset(
    f"macro.{PACKAGE}.test_{name}"
    for name in (
        "table_anomalies",
        "volume_anomalies",
        "freshness_anomalies",
        "event_freshness_anomalies",
        "column_anomalies",
        "all_columns_anomalies",
        "dimension_anomalies",
        "schema_changes",
        "volume_threshold",
        "metric_stability",
        "execution_sla",
    )
)

#: Why a passing Elementary test checked nothing (#1308), by reason code.
UncheckedReason = Literal[
    "package_disabled", "compiled_to_no_results", "history_unmeasured"
]

UNCHECKED_REASONS: Mapping[str, str] = {
    "package_disabled": (
        "the Elementary package's models are disabled for this target, and the "
        "test compiled to the package's no-results query, so it passed without "
        "reading the model"
    ),
    "compiled_to_no_results": (
        "the test compiled to the Elementary package's no-results query, so it "
        "passed without reading the model"
    ),
    "history_unmeasured": (
        "the test judges this run against what earlier runs stored in the "
        "Elementary schema, and passes where that history is missing; dex does "
        "not measure how much history the target holds"
    ),
}


def is_no_results_query(sql: str | None) -> bool:
    """Whether ``sql`` is the package's ``no_results_query()`` and nothing else."""

    return bool(sql) and _NO_RESULTS.fullmatch(str(sql)) is not None


def unchecked_tests(
    manifest: Mapping[str, Any], runtime: ElementaryRuntime
) -> dict[str, UncheckedReason]:
    """The Elementary tests whose pass says nothing about the rows (#1308).

    Keyed by unique id, for every enabled test that reaches one of the package's
    ``test_*`` macros, directly or through a project's own generic test. A test
    of dbt's own, or of another package, is never listed. In order:

    1. A test whose compiled SQL (what the manifest of the build recorded) is
       the no-results query checked nothing, whatever its name:
       ``package_disabled`` where the package's models are disabled for the
       target, ``compiled_to_no_results`` where they are not
       (``collect_metrics`` returns it whenever it runs).
    2. Where no compiled SQL was recorded and the package is disabled, a test
       whose package macro checks ``is_elementary_enabled()`` is
       ``package_disabled``: that check is what compiles it to the no-results
       query.
    3. Where the package is enabled, a test that reaches one of
       :data:`HISTORY_TESTS` is ``history_unmeasured``.

    A caller holds a failed test to its failure: only a pass is read as nothing
    checked. Nothing here opens a connection or counts the history.
    """

    if not runtime.installed:
        return {}
    macros = manifest.get("macros") or {}
    found: dict[str, UncheckedReason] = {}
    for uid, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, Mapping) or node.get("resource_type") != "test":
            continue
        reached = _test_macros(
            manifest, list((node.get("depends_on") or {}).get("macros") or [])
        )
        if not reached:
            continue
        compiled = node.get("compiled_code")
        if is_no_results_query(compiled):
            found[str(uid)] = (
                "compiled_to_no_results" if runtime.enabled else "package_disabled"
            )
        elif not runtime.enabled:
            if compiled is None and any(
                "is_elementary_enabled" in str((macros.get(m) or {}).get("macro_sql"))
                for m in reached
            ):
                found[str(uid)] = "package_disabled"
        elif reached & HISTORY_TESTS:
            found[str(uid)] = "history_unmeasured"
    return found


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
