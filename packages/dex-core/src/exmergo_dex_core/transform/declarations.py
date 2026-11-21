"""Declarations: what a model means, stated once and rendered into the project.

A model dex helps build used to carry almost nothing about what it means: which
rows are in, what one row is, what each column is, and which decisions the SQL
rests on. Those decisions lived only in an agent's transcript, so the next
agent, the next reviewer and `maintain` all started from nothing, and they are
exactly the decisions agents get wrong (#491).

A declaration is that statement, authored beside the edits in a plan payload:

    {"edits": [...], "declarations": [{"model": ..., "grain": [...], ...}]}

It is rendered into the model's **existing** YAML entry, wherever in the project
that entry lives, by splicing only the keys dex owns: the model and column
``description``, the tests a declaration implies, and ``config.meta.dex``. Every
other byte of the file stays where it was, for the reason every rewrite in this
package gives: re-serialising a ``schema.yml`` reflows it and drops the comments,
and the diff stops showing what changed. A model with no entry gets one where the
scaffold would put it, and never a second one beside an existing entry, which dbt
refuses (``DuplicatePatchPathError``).

Three rules hold throughout:

- **Missing intent warns, it never refuses.** A partial declaration, or none, is a
  warning. A *malformed* one (a column the model does not produce, an unknown
  role, a reference that does not resolve) is refused with the fix named.
- **No values from data.** A declaration is authored text. A population filter on
  a column that looks like personal data is refused the way a PII-shaped seed
  column is, because its values would land in git.
- **The YAML is the record.** ``data.decisions`` is read back out of the rendered
  ``config.meta.dex.assumptions`` rather than out of the payload, so plan, apply
  and build all report the same thing from the same place.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .. import generated_tests as gt
from .. import model_versions as mv
from ..dbt_project import DbtProjectView
from ..guards.pii_meta import entry_meta

# The declaration model and its parser live in a leaf (#927), so a semantic
# layer's own authoring route can read a declaration without importing the dbt
# rewriter and sqlglot behind it. Re-exported here, where the dbt callers look.
from ..semantic_declarations import Additivity as Additivity
from ..semantic_declarations import Assumption as Assumption
from ..semantic_declarations import ColumnDeclaration as ColumnDeclaration
from ..semantic_declarations import Declaration as Declaration
from ..semantic_declarations import DeclarationError as DeclarationError
from ..semantic_declarations import Evidence as Evidence
from ..semantic_declarations import Population as Population
from ..semantic_declarations import PopulationFilter as PopulationFilter
from ..semantic_declarations import Role as Role
from ..semantic_declarations import parse_declarations as parse_declarations
from .claims import CLAIM_KEY, claim_id, claim_of, without_claim
from .history_reading import history_reader
from .join_reading import read_model_joins
from .package_overrides import (
    DocumentedOverride,
    documented_overrides,
    override_refusal,
)
from .package_targets import find_package_target
from .plans import EditKind, EditOp, PlanEdit, select_columns
from .rewrite import (
    RewriteError,
    _mapping,
    _pair_span,
    _scalar_value,
    _sequence,
    _span,
    _unsampled,
    before_trailing_blanks,
    column_anchor,
    generated_spec,
    output_columns,
    prevailing_test_key,
    splice,
    yaml_blocks,
)

#: The namespace under ``config.meta`` that dex owns. Everything a declaration
#: says that dbt has no construct for lands here, and nothing else does.
DEX_META_KEY = "dex"


# --- where a model is declared --------------------------------------------------


def schema_path(files: Mapping[str, str], model: str) -> str | None:
    """The YAML file whose ``models:`` list declares ``model``, if one does.

    The lookup `transform place` and `maintain reconcile` share, so the three
    agree on where a model is declared. Searched by content rather than derived
    from a naming convention, because a project is free to declare every staging
    model in one ``_staging.yml``.
    """

    base, version = mv.split_label(model)
    if version is not None:
        versioned = mv.lookup(mv.cached_version_map(files), base)
        if versioned is not None:
            return versioned.yaml_path
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        for block in yaml_blocks(files[path]):
            if block.form == "yaml_model_entry" and block.name == model:
                return path
    return None


def yaml_entry_paths(files: Mapping[str, str], model: str) -> list[str]:
    """Every YAML file whose ``models:`` list has an entry named ``model``.

    :func:`schema_path` stops at the first; this names them all, for a refusal
    that has to point at every file dbt would read (#1161).
    """

    found: list[str] = []
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        if any(
            block.form == "yaml_model_entry" and block.name == model
            for block in yaml_blocks(files[path])
        ):
            found.append(path)
    return found


def package_documented_overrides(
    view: DbtProjectView, files: Mapping[str, str], models: Iterable[str]
) -> dict[str, DocumentedOverride]:
    """The models in ``models`` that are, after the plan, project overrides of a
    package model the package documents (see :mod:`.package_overrides`).

    A version label is never one: a versioned package model is not overridable.
    """

    root = getattr(view, "root", None)
    if not root:
        return {}
    wanted = {m for m in models if mv.split_label(m)[1] is None}
    paths = _planned_model_paths(view.model_paths, files.get("dbt_project.yml"))
    candidates = {m for m in wanted if model_sql_path(files, paths, m) is not None}
    # A Python model is not on dex's editable surface, but an existing one is an
    # override all the same, and its YAML entry collides just as a SQL one's does.
    candidates |= _python_models(Path(root), paths, wanted - candidates)
    if not candidates:
        return {}
    return documented_overrides(root, candidates, files.get("dbt_project.yml"))


def _planned_model_paths(current: Iterable[str], project_yml: str | None) -> list[str]:
    """The model paths now and as the plan's ``dbt_project.yml`` leaves them."""

    paths = list(current)
    try:
        loaded = yaml.safe_load(project_yml or "") or {}
    except yaml.YAMLError:
        return paths
    planned = loaded.get("model-paths") if isinstance(loaded, dict) else None
    for path in planned if isinstance(planned, list) else []:
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    return paths


def _python_models(root: Path, model_paths: Iterable[str], names: set[str]) -> set[str]:
    """The names in ``names`` a ``.py`` file under the project's model paths defines."""

    found: set[str] = set()
    if not names:
        return found
    for model_path in model_paths:
        base = root / model_path
        if not base.is_dir():
            continue
        found.update(p.stem for p in base.rglob("*.py") if p.stem in names)
    return found


def refuse_package_documented_entries(
    view: DbtProjectView, edits: list[PlanEdit]
) -> None:
    """Refuse a plan that leaves a project YAML entry dbt would apply on top of
    a package's.

    An override of a model its package documents takes the package's entry, so
    any project entry for the same name fails dbt's parse as a duplicate. This
    says so in dex's words, naming both files, before dbt runs (#1161). Only the
    names this plan touches are judged (a model it writes, an entry in a YAML
    file it writes, or every entry once it edits ``dbt_project.yml``), so a plan
    elsewhere in the project is not refused for a conflict it did not make.
    """

    files = post_change_files(view, edits)
    touched_project = any(e.path == "dbt_project.yml" for e in edits)
    touched_yaml = {
        e.path
        for e in edits
        if e.op is EditOp.UPSERT and e.path.endswith((".yml", ".yaml"))
    }
    touched = {
        PurePosixPath(e.path).stem
        for e in edits
        if e.kind is EditKind.MODEL_SQL and e.op is EditOp.UPSERT
    }
    entries: dict[str, list[str]] = {}
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        for block in yaml_blocks(files[path]):
            if block.form != "yaml_model_entry" or not block.name:
                continue
            entries.setdefault(block.name, []).append(path)
            if path in touched_yaml:
                touched.add(block.name)
    judged = set(entries) if touched_project else set(entries) & touched
    overrides = package_documented_overrides(view, files, judged)
    for model in sorted(overrides):
        raise DeclarationError(
            override_refusal(overrides[model], sorted(set(entries[model])))
        )


def model_sql_path(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> str | None:
    """The ``.sql`` file that defines ``model``, if the project has one.

    A version label (``dim_teams.v2``) is that version's file, and the bare name
    of a versioned model is its default version's, the file an unversioned
    ``ref()`` reads (#895).
    """

    versioned_path = _version_sql_path(files, model_paths, model)
    if versioned_path is not None:
        return versioned_path or None
    roots = [PurePosixPath(p) for p in model_paths]
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix != ".sql" or candidate.stem != model:
            continue
        if any(root == candidate or root in candidate.parents for root in roots):
            return path
    return None


#: The Jinja a model's select list can hold without hiding a column: a
#: ``ref()``, a ``source()`` or a ``config()``. Any other expression or block
#: (a loop, ``dbt_utils.star``) may write columns no static read sees.
_INERT_JINJA = re.compile(
    r"\{\{-?\s*(?:ref|source|config)\s*\(.*?\)\s*-?\}\}"
    r"|\{%-?\s*(?:config|do\s+config)\s*\(.*?\)\s*-?%\}"
    r"|\{#.*?#\}",
    re.DOTALL,
)


def model_outputs(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> frozenset[str] | None:
    """The lowercased columns ``model``'s SQL provably outputs, or ``None``
    when that is not known (no file, a star, a set operation, Jinja that could
    write a column, SQL that does not parse, a CTE that renames its columns).
    ``None`` never means *empty*: a generated column test renders unless its
    column is provably absent (#1453).

    A versioned model's bare name is ``None`` too: its tests go on the shared
    entry every version inherits, and one version's file says nothing about
    another's. A version label (``dim_teams.v2``) reads that version's file.
    """

    _base, version = mv.split_label(model)
    model_paths = list(model_paths)
    if version is None and _version_sql_path(files, model_paths, model) is not None:
        return None
    path = model_sql_path(files, model_paths, model)
    content = files.get(path) if path else None
    if not content:
        return None
    rest = _INERT_JINJA.sub("", content)
    if "{{" in rest or "{%" in rest or _renames_cte_columns(content):
        return None
    try:
        names = output_columns(content, path or "", through_ctes=True)
    except Exception:
        return None
    return frozenset(names) if names is not None else None


def _renames_cte_columns(content: str) -> bool:
    """Whether a CTE names its columns (``with c(status) as (...)``), which a
    projection read through ``select * from c`` would not see. Read from the
    SQL :func:`output_columns` parses (Jinja blanked to placeholders), and
    ``True`` where it does not parse, so the outputs stay unknown."""

    import sqlglot
    from sqlglot import exp

    from ..references import blank_jinja

    try:
        parsed = sqlglot.parse_one(blank_jinja(content), read="duckdb")
    except Exception:
        return True
    if parsed is None:
        return True
    return any(
        cte.args.get("alias") is not None and cte.args["alias"].columns
        for cte in parsed.find_all(exp.CTE)
    )


def _version_sql_path(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> str | None:
    """A versioned model's file for ``model``: ``None`` when ``model`` names no
    versioned model, and ``""`` when it names one no ``.sql`` file builds."""

    vmap = mv.cached_version_map(files, list(model_paths))
    if not vmap:
        return None
    base, version = mv.split_label(model)
    versioned = mv.lookup(vmap, base)
    if versioned is None:
        return None
    item = versioned.version(version if version is not None else versioned.default)
    if item is None or item.file is None or not item.file.endswith(".sql"):
        return ""
    return item.file


_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def _is_below_0_20(release: str | None) -> bool | None:
    """Whether Elementary release ``release`` is strictly below 0.20.0.

    Returns ``None`` when ``release`` is absent or cannot be parsed as a
    supported version format.
    """
    if not release:
        return None
    match = _SEMVER_RE.match(release.strip())
    if not match:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch") or 0)
    prerelease = match.group("prerelease") is not None

    triplet = (major, minor, patch)
    return triplet < (0, 20, 0) or (triplet == (0, 20, 0) and prerelease)


_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_SQL_LINE_COMMENT = re.compile(r"-{2}.*$", re.MULTILINE)
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_MATERIALIZATION_RE = re.compile(
    r"\{%[-+]?\s*materialization\s+([a-zA-Z0-9_]+)\s*,\s*([a-zA-Z0-9_]+)\s*[-+]?%\}"
    r"(.*?)"
    r"\{%[-+]?\s*endmaterialization\s*[-+]?%\}",
    re.DOTALL,
)
_MACRO_RE = re.compile(
    r"\{%[-+]?\s*macro\s+([a-zA-Z0-9_]+)\s*(?:\([^)]*\))?\s*[-+]?%\}"
    r"(.*?)"
    r"\{%[-+]?\s*endmacro\s*[-+]?%\}",
    re.DOTALL,
)


def _proposed_macros(
    view: DbtProjectView,
    files: Mapping[str, str],
    project_name: str,
) -> dict[str, dict[str, Any]]:
    """Root project macros and materializations parsed from proposed ``files``."""
    from ..dbt_project import jinja_calls
    from . import elementary

    macros: dict[str, dict[str, Any]] = {}
    for path, content in files.items():
        if not path.endswith(".sql"):
            continue
        if not any(path.startswith(mp + "/") for mp in view.macro_paths):
            continue
        clean = _JINJA_COMMENT.sub("", content)
        clean = _SQL_BLOCK_COMMENT.sub("", clean)
        clean = _SQL_LINE_COMMENT.sub("", clean)

        for match in _MATERIALIZATION_RE.finditer(clean):
            mat_type, adapter, body = match.group(1), match.group(2), match.group(3)
            macro_name = f"materialization_{mat_type}_{adapter}"
            uid = f"macro.{project_name}.{macro_name}"
            deps: list[str] = []
            for call in jinja_calls(body):
                callee = call.callee
                if callee.startswith(f"{elementary.PACKAGE}."):
                    suffix = callee[len(elementary.PACKAGE) + 1 :]
                    deps.append(f"macro.{elementary.PACKAGE}.{suffix}")
                elif "." not in callee and callee not in (
                    "return",
                    "config",
                    "adapter",
                ):
                    deps.append(f"macro.{project_name}.{callee}")
            macros[uid] = {
                "unique_id": uid,
                "name": macro_name,
                "package_name": project_name,
                "depends_on": {"macros": deps},
            }

        for match in _MACRO_RE.finditer(clean):
            name, body = match.group(1), match.group(2)
            uid = f"macro.{project_name}.{name}"
            deps = []
            for call in jinja_calls(body):
                callee = call.callee
                if callee.startswith(f"{elementary.PACKAGE}."):
                    suffix = callee[len(elementary.PACKAGE) + 1 :]
                    deps.append(f"macro.{elementary.PACKAGE}.{suffix}")
                elif "." not in callee and callee not in (
                    "return",
                    "config",
                    "adapter",
                ):
                    deps.append(f"macro.{project_name}.{callee}")
            macros[uid] = {
                "unique_id": uid,
                "name": name,
                "package_name": project_name,
                "depends_on": {"macros": deps},
            }
    return macros


def _elementary_sampling_warning(
    view: DbtProjectView,
    files: Mapping[str, str],
) -> str | None:
    """A warning if Elementary's test materialization runs and ignores sampling.

    ``None`` when Elementary is not installed, when dbt's materialization is
    confirmed, or when the installed package is 0.20.0 or later.
    """
    from . import elementary
    from .package_overrides import _installed

    root = Path(view.root)
    installed_packages: set[str] = set()
    release: str | None = None
    with contextlib.suppress(Exception):
        for name, directory, _ in _installed(root):
            installed_packages.add(name)
            if name == elementary.PACKAGE:
                own_yml = directory / "dbt_project.yml"
                if own_yml.is_file():
                    with contextlib.suppress(Exception):
                        own = yaml.safe_load(own_yml.read_text(encoding="utf-8"))
                        if isinstance(own, dict) and own.get("version") is not None:
                            release = str(own["version"])

    project_yml_text = files.get("dbt_project.yml")
    if project_yml_text is None:
        with contextlib.suppress(Exception):
            project_yml_text = (root / "dbt_project.yml").read_text(encoding="utf-8")

    project_name = view.project_name
    if not project_name:
        with contextlib.suppress(Exception):
            cfg = yaml.safe_load(project_yml_text or "")
            if isinstance(cfg, dict) and cfg.get("name"):
                project_name = str(cfg["name"])
    if not project_name:
        project_name = "root"

    proposed = _proposed_macros(view, files, project_name)

    manifest = view.manifest
    if manifest is None:
        from ..dbt_project import manifest_file

        with contextlib.suppress(Exception):
            compiled = manifest_file(view.root)
            if compiled.is_file():
                manifest = json.loads(compiled.read_text(encoding="utf-8"))

    materialization = None
    if manifest and isinstance(manifest, Mapping):
        with contextlib.suppress(Exception):
            manifest_to_resolve = manifest
            if proposed:
                manifest_to_resolve = dict(manifest)
                macros_dict = dict(manifest_to_resolve.get("macros") or {})
                macros_dict.update(proposed)
                manifest_to_resolve["macros"] = macros_dict
            runtime = elementary.resolve(
                manifest_to_resolve,
                project_yml_text,
                release=release,
                installed_packages=installed_packages or None,
            )
            if runtime.installed:
                materialization = runtime.materialization
                if runtime.release:
                    release = runtime.release

    if materialization is None:
        if elementary.PACKAGE not in installed_packages:
            return None
        project = elementary._project_config(project_yml_text)
        models_cfg = project.get("models")
        enabled = True
        if isinstance(models_cfg, Mapping):
            elem_cfg = models_cfg.get(elementary.PACKAGE)
            if isinstance(elem_cfg, Mapping):
                val = elem_cfg.get("+enabled", elem_cfg.get("enabled", True))
                if val is False:
                    enabled = False
        if not enabled:
            return None

        elem_mat = f"macro.{elementary.PACKAGE}.materialization_test_default"
        synthetic_manifest = {
            "metadata": {"adapter_type": "default", "project_name": project_name},
            "nodes": {
                f"model.{elementary.PACKAGE}.dummy": {
                    "unique_id": f"model.{elementary.PACKAGE}.dummy",
                    "package_name": elementary.PACKAGE,
                    "resource_type": "model",
                }
            },
            "disabled": {},
            "macros": {
                "macro.dbt.materialization_test_default": {
                    "unique_id": "macro.dbt.materialization_test_default",
                    "name": "materialization_test_default",
                    "package_name": "dbt",
                },
                elem_mat: {
                    "unique_id": elem_mat,
                    "name": "materialization_test_default",
                    "package_name": elementary.PACKAGE,
                    "depends_on": {
                        "macros": [f"macro.{elementary.PACKAGE}.materialize_test"]
                    },
                },
                **proposed,
            },
        }
        with contextlib.suppress(Exception):
            runtime = elementary.resolve(
                synthetic_manifest,
                project_yml_text,
                release=release,
                installed_packages=installed_packages,
            )
            if runtime.installed:
                materialization = runtime.materialization

    if materialization != "elementary":
        return None

    below = _is_below_0_20(release)
    if below is True and release is not None:
        return (
            f"installed Elementary release {release} ignores "
            "disable_test_samples; upgrade the package to 0.20.0 or later, or "
            "set test_sample_row_count: 0 for the project"
        )
    if below is None:
        return (
            "could not tell the installed Elementary release; releases below 0.20.0 "
            "ignore disable_test_samples; upgrade the package to 0.20.0 or later, or "
            "set test_sample_row_count: 0 for the project"
        )
    return None


# --- the whole pass -------------------------------------------------------------


def apply_declarations(
    declarations: list[Declaration],
    view: DbtProjectView,
    edits: list[PlanEdit],
    *,
    cache: Any = None,
    pii_overrides: Any = None,
    render_tests: bool | list[str] = False,
    generated: list[dict[str, Any]] | None = None,
    dialect: str | None = None,
) -> tuple[list[PlanEdit], list[str]]:
    """Validate ``declarations`` and render them into the plan's edits.

    Returns the edits with each declaration folded in (one edit per path, merged
    into a ``schema_yml`` edit the payload already carries for that file) and the
    warnings for intent that was left unstated.

    ``render_tests`` is ``declarations.render_tests`` (#1449), which decides
    whether the generated tests render beside the built-ins; ``generated``
    collects one row per generated test the declarations give, rendered or not
    (see :func:`render`). ``dialect`` is the configured connector, which decides
    whether a kind that names its dialects renders at all (#1451).
    """

    files = post_change_files(view, edits)
    overrides = package_documented_overrides(
        view, files, [d.model for d in declarations]
    )
    for declaration in declarations:
        override = overrides.get(declaration.model)
        if override is not None:
            sql_path = model_sql_path(files, view.model_paths, declaration.model)
            paths = yaml_entry_paths(files, declaration.model) or [
                _default_schema_path(sql_path or "", declaration.model)
            ]
            raise DeclarationError(override_refusal(override, paths, declaring=True))
    warnings: list[str] = []
    for declaration in declarations:
        warnings.extend(
            _check(declaration, files, view, cache=cache, pii_overrides=pii_overrides)
        )

    flagged_tests: list[bool] = []
    rendered: dict[str, str] = {}
    # One reader over the project as the plan leaves it: its ref() graph is
    # built on the first history it reads (#1458).
    history = history_reader(view, files)
    for declaration in declarations:
        sql_path = model_sql_path(files, view.model_paths, declaration.model)
        path = schema_path(files, declaration.model)
        if path is None:
            path = _default_schema_path(sql_path or "", declaration.model)
        try:
            files[path] = render(
                files.get(path),
                declaration,
                notes=warnings,
                flagged_tests=flagged_tests,
                render_tests=render_tests,
                generated=generated,
                dialect=dialect,
                outputs=model_outputs(files, view.model_paths, declaration.model),
                joins=read_model_joins(
                    files, view.model_paths, declaration.model, dialect
                ),
                history=history,
            )
        except RewriteError as exc:
            raise DeclarationError(
                f"dex could not write the declaration of '{declaration.model}' into "
                f"{path} predictably ({exc}); make that change by hand, then "
                "declare again"
            ) from exc
        rendered[path] = files[path]
    # A references implies its parent column is unique (#1457); that test is
    # the parent's, so it renders into the parent's entry, after every
    # declaration above has written its own.
    for path, content in render_referenced_keys(
        files,
        view,
        [d.model for d in declarations],
        render_tests=render_tests,
        dialect=dialect,
        generated=generated,
        notes=warnings,
        flagged_tests=flagged_tests,
    ).items():
        files[path] = content
        rendered[path] = content

    merged = list(edits)
    for path, content in rendered.items():
        existing = next(
            (
                i
                for i, edit in enumerate(merged)
                if edit.path == path and edit.op is EditOp.UPSERT
            ),
            None,
        )
        if existing is not None:
            merged[existing] = merged[existing].model_copy(
                update={"new_content": content}
            )
        else:
            merged.append(
                PlanEdit(path=path, kind=EditKind.SCHEMA_YML, new_content=content)
            )

    if any(flagged_tests):
        warn = _elementary_sampling_warning(view, files)
        if warn is not None and warn not in warnings:
            warnings.append(warn)

    return merged, warnings


def undeclared_model_warnings(
    edits: list[PlanEdit], view: DbtProjectView, declared: Iterable[str]
) -> list[str]:
    """A warning per model this plan writes SQL for and declares nothing about.

    Only models the plan authors: dex makes its own output well declared and does
    not police the models a human wrote. A model whose YAML already carries a dex
    declaration from an earlier plan is declared, and is left alone.
    """

    files = post_change_files(view, edits)
    vmap = mv.cached_version_map(files, view.model_paths)
    named = mv.expand(declared, vmap)
    # An override of a model its package documents takes the package's entry
    # and can carry no declaration of its own, so none is owed (#1161).
    overrides = package_documented_overrides(
        view,
        files,
        [
            PurePosixPath(e.path).stem
            for e in edits
            if e.kind is EditKind.MODEL_SQL and e.op is EditOp.UPSERT
        ],
    )
    warnings: list[str] = []
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or not edit.path.endswith(".sql")
        ):
            continue
        model = mv.label_for_file(edit.path, vmap) or PurePosixPath(edit.path).stem
        if model in named or model in overrides or _carries_declaration(files, model):
            continue
        warnings.append(
            f"{edit.path}: this plan writes '{model}' and declares nothing about what "
            "it means; add a `declarations` entry (description, grain, columns, "
            "assumptions) so a reviewer and `maintain` can check it"
        )
    return warnings


def post_change_files(view: DbtProjectView, edits: list[PlanEdit]) -> dict[str, str]:
    """Every project file as it will read once ``edits`` are applied."""

    files = {path: source.content for path, source in view.files.items()}
    for edit in edits:
        if edit.op is EditOp.DELETE:
            files.pop(edit.path, None)
        elif edit.new_content is not None:
            files[edit.path] = edit.new_content
    return files


# --- validation -----------------------------------------------------------------


def _check(
    declaration: Declaration,
    files: Mapping[str, str],
    view: DbtProjectView,
    *,
    cache: Any,
    pii_overrides: Any,
) -> list[str]:
    model = declaration.model
    warnings: list[str] = []
    target = _version_target(declaration, files, view)
    if target is not None:
        sql_path, produced, unread = target
        warnings.extend(
            f"{path}: the SELECT list could not be read statically (a `select *` "
            f"or a macro standing in for a column), so the columns declared for "
            f"'{model}' were not checked against it"
            for path in unread
        )
    else:
        sql_path = model_sql_path(files, view.model_paths, model)
        if sql_path is None:
            raise DeclarationError(
                f"'{model}' is not a model in this project or in this plan; declare "
                "a model the project has, or add its SQL to the same payload"
            )
        produced = select_columns(files[sql_path])
        if produced is None:
            warnings.append(
                f"{sql_path}: the SELECT list could not be read statically (a "
                f"`select *` or a macro standing in for a column), so the columns "
                f"declared for '{model}' were not checked against it"
            )
    if produced is not None:
        unknown = [c for c in declaration.columns if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares column(s) {', '.join(unknown)} that its SELECT "
                f"list in {sql_path} does not produce; declare only output columns, "
                "or add them to the model first"
            )
        missing_grain = [
            g for g in declaration.grain or [] if g.lower() not in produced
        ]
        if missing_grain:
            raise DeclarationError(
                f"'{model}' declares grain column(s) {', '.join(missing_grain)} that "
                f"its SELECT list in {sql_path} does not produce; a grain names "
                "columns of the model"
            )

    for name, column in declaration.columns.items():
        if column.references is not None:
            warnings.extend(
                _check_reference(model, name, column.references, files, view)
            )
        elif column.role is Role.FOREIGN_KEY:
            warnings.append(
                f"'{model}.{name}' is a foreign_key with no `references`, so no "
                "relationships test was written for it"
            )
        if column.freshness is not None:
            _check_freshness(model, name, column, files)
        elif (
            column.role is not None
            and column.role is not Role.TIME
            and _yaml_column_dex(files, model, name).get("freshness")
        ):
            # The merge keeps the freshness the YAML already states, so a new
            # role would leave it on a column that is no longer the time.
            raise DeclarationError(
                f"'{model}.{name}' already declares freshness, which belongs on "
                f"the column with role `time`, and this declaration makes its role "
                f"`{column.role.value}`; keep `role: time` on it, or remove its "
                "freshness from the model's YAML first"
            )

    if declaration.population is not None:
        _check_filters(model, declaration.population, cache, pii_overrides)
        parent = declaration.population.parent
        if parent is not None:
            warnings.extend(
                _check_parent(model, declaration, parent, produced, files, view)
            )

    if declaration.history is not None:
        warnings.extend(
            _check_history(
                model, declaration, produced, files, view, cache, pii_overrides
            )
        )

    declared_columns = _declared_column_roles(files, declaration.model)
    declared_columns.update(name.lower() for name in declaration.columns)
    warnings.extend(_partial_intent(declaration, produced, declared_columns))
    return warnings


#: Model-level dex keys dbt merges across a version and its shared entry by
#: concatenating lists and merging mappings, so one stated at both levels is read
#: back as neither statement.
_MERGED_KEYS = ("grain", "population", "assumptions", "history")


def _version_target(
    declaration: Declaration, files: Mapping[str, str], view: DbtProjectView
) -> tuple[str, set[str] | None, list[str]] | None:
    """Where a declaration on a versioned model lands, and what it may name (#895).

    ``None`` when the declaration names no versioned model. Otherwise the file
    the column checks read (a version's SQL, or the YAML entry for the shared
    entry), the columns the declaration may name, and the files whose SELECT list
    could not be read. ``dim_teams.v2`` names one version; the bare
    ``dim_teams`` names the shared top-level entry every version inherits, so its
    columns are those any version produces and its grain one every version
    produces. A version's file stem (``dim_teams_v2``) is refused with the label,
    because a declaration under that name would create a second, plain entry.
    """

    model = declaration.model
    vmap = mv.cached_version_map(files, view.model_paths)
    base, version = mv.split_label(model)
    versioned = mv.lookup(vmap, base)
    if versioned is None:
        if version is not None and "." in model:
            raise DeclarationError(
                f"'{model}' names a version, and the project declares no versioned "
                f"model '{base}'; declare '{base}' under `models:` with a "
                "`versions:` list first"
            )
        label = mv.label_for_stem(model, vmap)
        if label is not None:
            raise DeclarationError(
                f"'{model}' is the file that builds '{label}', a version of a "
                f'versioned model; declare it as "model": "{label}" so it '
                "renders into that version's entry"
            )
        return None

    model_dex = set(_model_dex(declaration))
    if version is not None:
        item = versioned.version(version)
        if item is None:
            raise DeclarationError(
                f"'{model}' names version {version} of '{versioned.base}', which "
                f"lists versions {', '.join(v.v for v in versioned.versions)}"
            )
        shared = _dex_of(versioned.entry)
        clash = sorted(model_dex & set(shared) & set(_MERGED_KEYS))
        if clash:
            raise DeclarationError(
                f"'{versioned.base}' states {', '.join(clash)} on its shared "
                f"top-level entry, which dbt merges into every version (lists "
                f"concatenated), so a {', '.join(clash)} declared on '{model}' "
                "would be read back as neither. Move it off the shared entry and "
                "declare it on each version instead"
            )
        if item.file is None or not item.file.endswith(".sql"):
            return (versioned.yaml_path, None, [item.file] if item.file else [])
        produced = select_columns(files[item.file])
        return (item.file, produced, [] if produced is not None else [item.file])

    clash = sorted(
        {
            key
            for item in versioned.versions
            for key in _dex_of(item.item)
            if key in model_dex and key in _MERGED_KEYS
        }
    )
    if clash:
        raise DeclarationError(
            f"a version of '{model}' states {', '.join(clash)} itself, and dbt "
            "merges the shared entry into every version (lists concatenated), so "
            f"a {', '.join(clash)} declared on the shared '{model}' would be read "
            "back as neither. Declare it on each version (`<model>.v<N>`) instead"
        )
    union: set[str] = set()
    every: set[str] | None = None
    unread: list[str] = []
    for item in versioned.versions:
        produced = (
            select_columns(files[item.file])
            if item.file is not None and item.file.endswith(".sql")
            else None
        )
        if produced is None:
            unread.append(item.file or versioned.yaml_path)
            continue
        union |= produced
        every = produced if every is None else every & produced
    if unread:
        return (versioned.yaml_path, None, unread)
    missing_grain = [
        g
        for g in declaration.grain or []
        if every is not None and g.lower() not in every
    ]
    if missing_grain:
        raise DeclarationError(
            f"'{model}' is versioned, and a grain declared on its shared entry "
            f"applies to every version, but not every version produces "
            f"{', '.join(missing_grain)}; declare the grain on each version"
        )
    return (versioned.yaml_path, union, [])


def _dex_of(node: Any) -> dict[str, Any]:
    dex = entry_meta(node).get(DEX_META_KEY) if isinstance(node, dict) else None
    return dex if isinstance(dex, dict) else {}


def _check_reference(
    model: str,
    column: str,
    target: str,
    files: Mapping[str, str],
    view: DbtProjectView,
    *,
    claim: str | None = None,
) -> list[str]:
    """Refuse a reference whose parent or parent column neither the project nor
    an installed package has, and return the warnings for what could not be
    checked.

    The project is looked up first, so a project override of a package model is
    the parent, as it is for ``ref()``. A package model, seed or snapshot is the
    parent only when the project defines none of that name (#1155): its column
    is checked against the package's SQL where that can be read, and is
    reported as unchecked where it cannot, since a staging model that lists its
    columns through a macro is no reason to refuse a real parent.

    ``claim`` is how the refusal names what made the reference, so a
    ``population.parent`` key reads as that rather than as a column's
    ``references``."""

    said = claim or f"'{model}.{column}' references '{target}'"
    parent, _, parent_column = target.rpartition(".")
    if not parent or not parent_column:
        raise DeclarationError(f"{said}; write the target as <model>.<column>")
    parent_sql = model_sql_path(files, view.model_paths, parent)
    if parent_sql is not None:
        produced = select_columns(files[parent_sql])
        if produced is not None and parent_column.lower() not in produced:
            raise DeclarationError(
                f"{said}, and {parent_sql} does not produce a column '{parent_column}'"
            )
        return []
    if _is_seed_or_snapshot(files, view, parent):
        return []
    packaged = find_package_target(
        view.root,
        parent,
        project_yml=files.get("dbt_project.yml"),
        manifest=view.manifest,
    )
    if packaged is not None:
        if not packaged.enabled:
            raise DeclarationError(
                f"{said}, and {packaged.described()} is disabled in this project's "
                "dbt_project.yml with no project model of that name, so dbt builds "
                "no node called that; name the model that replaces it"
            )
        if packaged.columns is None:
            return [
                f"{said}: the columns of {packaged.described()} could not be read "
                f"({packaged.unread}), so whether it produces '{parent_column}' was "
                "not checked here; `transform build --verify` reads the built "
                "relation"
            ]
        if parent_column.lower() not in packaged.columns:
            raise DeclarationError(
                f"{said}, and {packaged.described()} does not produce a column "
                f"'{parent_column}'"
            )
        return []
    raise DeclarationError(
        f"{said}, and neither the project nor an installed package has a model, "
        f"seed or snapshot named '{parent}'{_source_hint(files, parent)}"
    )


def _source_hint(files: Mapping[str, str], name: str) -> str:
    """A clause saying ``name`` is a source, which a declaration cannot name."""

    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            loaded = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        sources = loaded.get("sources") if isinstance(loaded, dict) else None
        for source in sources if isinstance(sources, list) else []:
            tables = source.get("tables") if isinstance(source, dict) else None
            for table in tables if isinstance(tables, list) else []:
                if isinstance(table, dict) and table.get("name") == name:
                    return (
                        f" ('{name}' is a table of source '{source.get('name')}', "
                        "and a source is not a supported parent: name the staging "
                        "model that selects from it)"
                    )
    return ""


def _check_parent(
    model: str,
    declaration: Declaration,
    parent: Any,
    produced: set[str] | None,
    files: Mapping[str, str],
    view: DbtProjectView,
) -> list[str]:
    """Validate ``population.parent`` (#857): every output key column is one the
    model produces, and every parent column is one the parent produces.

    A parent that is the model itself is refused: a key set always equals
    itself, so the check would prove nothing. A key that is not the declared
    grain is a warning rather than a refusal, since a model may legitimately
    hold one row per parent at a grain written differently, but the check then
    counts entities the grain does not name.
    """

    if parent.model.lower() == model.lower():
        raise DeclarationError(
            f"'{model}' declares itself as its population.parent; name the model, "
            "seed or snapshot its rows are drawn from"
        )
    if produced is not None:
        unknown = [c for c in parent.key if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares population.parent key column(s) "
                f"{', '.join(unknown)} that its SELECT list does not produce; the "
                "key maps output columns to the parent's"
            )
    warnings: list[str] = []
    for output_column, parent_column in parent.key.items():
        warnings += _check_reference(
            model,
            output_column,
            f"{parent.model}.{parent_column}",
            files,
            view,
            claim=(
                f"'{model}' declares population.parent {parent.model} keyed "
                f"{output_column} -> {parent_column}"
            ),
        )
    grain = [g.lower() for g in declaration.grain or []]
    keys = [k.lower() for k in parent.key]
    if grain and sorted(grain) != sorted(keys):
        return [
            *warnings,
            f"'{model}' declares population.parent keyed on {', '.join(parent.key)} "
            f"but its grain is {', '.join(declaration.grain or [])}; the parent "
            "check counts entities by the key, so the two describe different rows "
            "unless the key is the grain",
        ]
    return warnings


def _check_history(
    model: str,
    declaration: Declaration,
    produced: set[str] | None,
    files: Mapping[str, str],
    view: DbtProjectView,
    cache: Any,
    pii_overrides: Any,
) -> list[str]:
    """Validate a ``history`` block (#1027): a real snapshot, output columns the
    model produces, and a target that is safe to commit. A policy left out is a
    warning, since it is intent still owed rather than a malformed statement."""

    history = declaration.history
    if history is None:  # pragma: no cover - the caller only passes a history
        return []
    if not snapshot_defined(files, history.snapshot, view.snapshot_paths):
        raise DeclarationError(
            f"'{model}' declares history from '{history.snapshot}', and the project "
            "has no snapshot by that name; a temporal contract is verified against "
            "a dbt snapshot's versions"
        )
    measures = history.measures
    named = [
        *history.entity,
        *(
            column
            for column in (
                measures.transition_count,
                measures.latest_target_transition_at,
                measures.first_target_at,
            )
            if column is not None
        ),
    ]
    if produced is not None:
        unknown = [c for c in named if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares history column(s) {', '.join(unknown)} that its "
                "SELECT list does not produce; `entity` keys and `measures` name "
                "output columns of the model"
            )
    try:
        _check_filters(
            model,
            Population(
                filters=[
                    PopulationFilter(
                        column=history.status.column,
                        includes=[history.status.target],
                    )
                ]
            ),
            cache,
            pii_overrides,
        )
    except DeclarationError as exc:
        raise DeclarationError(
            f"'{model}' declares history on status column "
            f"'{history.status.column}', whose target would be committed to git, "
            f"and the column looks like personal data: {exc}"
        ) from exc

    owed: list[str] = []
    if history.initial_observation is None:
        owed.append("initial_observation (`baseline` or `transition`)")
    if history.null_status is None:
        owed.append("null_status (`ignore` or `state`)")
    if history.population is None:
        owed.append("population (`ever_target` or `all_observed`)")
    timed = measures.latest_target_transition_at or measures.first_target_at
    if timed and (history.time is None or history.time.provenance is None):
        owed.append("time, with its provenance (`business` or `capture`)")
    if owed:
        return [
            f"'{model}' declares history without stating {', '.join(owed)}; Dex "
            "assumes no temporal policy, so verification reports what depends on "
            "it as unchecked"
        ]
    return []


def _is_seed_or_snapshot(
    files: Mapping[str, str], view: DbtProjectView, name: str
) -> bool:
    seed_roots = [PurePosixPath(p) for p in view.seed_paths]
    for path in files:
        candidate = PurePosixPath(path)
        if (
            candidate.suffix == ".csv"
            and candidate.stem == name
            and any(root in candidate.parents for root in seed_roots)
        ):
            return True
    return snapshot_defined(files, name, view.snapshot_paths)


def snapshot_defined(
    files: Mapping[str, str], name: str, snapshot_paths: Iterable[str] | None = None
) -> bool:
    """Whether the project defines a snapshot called ``name``: a ``{% snapshot %}``
    block (under ``snapshot_paths``, where given), or a ``snapshots:`` entry in a
    YAML file, which is how dbt 1.9 and later define one."""

    roots = (
        None if snapshot_paths is None else [PurePosixPath(p) for p in snapshot_paths]
    )
    block = re.compile(rf"{{%-?\s*snapshot\s+{re.escape(name)}\s*-?%}}")
    for path, content in files.items():
        candidate = PurePosixPath(path)
        if candidate.suffix == ".sql":
            placed = roots is None or any(r in candidate.parents for r in roots)
            if placed and block.search(content):
                return True
        elif candidate.suffix in (".yml", ".yaml") and "snapshots" in content:
            try:
                parsed = yaml.safe_load(content)
            except yaml.YAMLError:
                continue
            entries = parsed.get("snapshots") if isinstance(parsed, dict) else None
            if any(
                isinstance(entry, dict) and entry.get("name") == name
                for entry in (entries if isinstance(entries, list) else [])
            ):
                return True
    return False


def _check_filters(
    model: str, population: Population, cache: Any, pii_overrides: Any
) -> None:
    """Refuse a filter list on a column that looks like personal data.

    The seed gate's reasoning, for the same reason: a filter's values are authored
    text that lands in git and stays there. Read from names and types and the
    cache's flags, never from a value.
    """

    from ..explore.profile import classify_pii
    from ..guards import PII_BLOCK_CONFIDENCE

    flagged: dict[str, Any] = {}
    for dataset in getattr(cache, "datasets", None) or []:
        for column in dataset.columns:
            if column.pii is None:
                continue
            known = flagged.get(column.name.lower())
            if known is None or column.pii.confidence > known.confidence:
                flagged[column.name.lower()] = column.pii

    for entry in population.filters:
        if not (entry.includes or entry.excludes):
            continue
        if pii_overrides is not None and f"{model}.{entry.column}" in pii_overrides:
            continue
        detected, provisional = classify_pii(entry.column, "varchar")
        candidates = [
            flag
            for flag in (
                flagged.get(entry.column.lower()),
                None if provisional else detected,
            )
            if flag is not None
        ]
        strongest = max(candidates, key=lambda flag: flag.confidence, default=None)
        if strongest is not None and strongest.confidence >= PII_BLOCK_CONFIDENCE:
            raise DeclarationError(
                f"'{model}' filters its population on '{entry.column}', which looks "
                f"like {strongest.category.value} (confidence "
                f"{strongest.confidence:g}); a filter's values are committed to git, "
                "so state the rule in `population.rule` without listing them. If the "
                f"review says it is not personal data, add `- {{column: "
                f"{model}.{entry.column}}}` under pii_overrides in .dex/config.yml"
            )


def _check_freshness(
    model: str, name: str, column: Any, files: Mapping[str, str]
) -> None:
    """Refuse a ``freshness`` on a column that is not the model's declared time
    (#1460): the lag is read from the newest value of a timestamp, and a role
    other than ``time`` says the column is something else. The role is this
    declaration's, else the one the model's YAML already states."""

    role = column.role.value if column.role is not None else None
    if role is None:
        role = _yaml_column_role(files, model, name)
    if role == Role.TIME.value:
        return
    stated = f"its role is `{role}`" if role else "it declares no role"
    raise DeclarationError(
        f"'{model}.{name}' declares freshness, but {stated}; freshness belongs on "
        "the column with role `time` (the event's time or the load time), so "
        "declare `role: time` on it or move the freshness to that column"
    )


def _yaml_column_role(files: Mapping[str, str], model: str, column: str) -> str | None:
    """The role the model's YAML already declares for ``column``, if any."""

    role = _yaml_column_dex(files, model, column).get("role")
    return role if isinstance(role, str) else None


def _yaml_column_dex(
    files: Mapping[str, str], model: str, column: str
) -> dict[str, Any]:
    """The ``config.meta.dex`` the model's YAML already states for ``column``."""

    for _label, entry in _entries(files, {model}):
        for item in entry.get("columns") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            if item["name"].lower() != column.lower():
                continue
            column_dex = entry_meta(item).get(DEX_META_KEY)
            if isinstance(column_dex, dict):
                return column_dex
    return {}


def _declared_column_roles(files: Mapping[str, str], model: str) -> set[str]:
    """Column names that already have a role in the model's YAML declaration."""
    declared: set[str] = set()

    for _label, entry in _entries(files, {model}):
        for column in entry.get("columns") or []:
            if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                continue

            column_dex = entry_meta(column).get(DEX_META_KEY)
            if isinstance(column_dex, dict) and column_dex.get("role"):
                declared.add(column["name"].lower())

    return declared


def _partial_intent(
    declaration: Declaration,
    produced: set[str] | None,
    declared_columns: set[str] | None = None,
) -> list[str]:
    # A missing model-level field (description, grain, population, assumptions)
    # is warned once per field by `drafting`, which reads the post-change YAML
    # and so also counts what the entry declared before this payload.
    warnings = []

    if produced is not None:
        declared = (
            {column.lower() for column in declaration.columns}
            if declared_columns is None
            else {column.lower() for column in declared_columns}
        )
        undeclared = sorted(column for column in produced if column not in declared)

        if undeclared:
            warnings.append(
                f"'{declaration.model}' produces column(s) {', '.join(undeclared)} "
                "with no declared role"
            )

    for index, assumption in enumerate(declaration.assumptions):
        absent = [
            field
            for field in ("decision", "chosen", "evidence")
            if getattr(assumption, field) is None
        ]
        if absent:
            warnings.append(
                f"'{declaration.model}' assumption {index + 1} has no "
                f"{', '.join(absent)}"
            )

    return warnings


# --- rendering ------------------------------------------------------------------


def render(
    content: str | None,
    declaration: Declaration,
    *,
    notes: list[str] | None = None,
    flagged_tests: list[bool] | None = None,
    render_tests: bool | list[str] = False,
    generated: list[dict[str, Any]] | None = None,
    dialect: str | None = None,
    outputs: frozenset[str] | None = None,
    joins: Any = None,
    history: Any = None,
) -> str:
    """``content`` with ``declaration`` spliced into its entry for the model.

    Each key is written by its own splice against a fresh parse of the text the
    previous splice produced, so no offset is ever measured against bytes that
    have since moved. A version label (``dim_teams.v2``) renders into that item
    of the model's ``versions:`` list (#895); ``notes`` collects what a reviewer
    should be told about how (a column forked from the shared entry).

    The built-in tests always render. The generated tests (#1449) render only
    where ``render_tests`` turns their kind on. They are generated from the
    declaration as the entry states it once this one is merged in, not from
    the payload alone, so declaring a description again, or ``transform
    declare`` on a model declared before the switch was on, renders the tests
    the facts already declared imply. ``generated`` collects one row per
    generated test that declaration gives: its model, column, test,
    cadence tag and claim, and a ``status`` of ``added`` or ``already_listed``
    (on), ``not_rendered`` with the ``reason`` or ``listed_while_off`` (off). A
    kind turned on that does not support ``dialect`` (#1451) is
    ``not_rendered`` with the reason ``dialect_unsupported``. ``outputs`` are
    the columns the model's SQL provably outputs (:func:`model_outputs`, or
    ``None`` where unknown): a test reading a column outside them (the column
    it sits under, or a model-level test's grain or parent key) is
    ``not_rendered`` with the reason ``column_not_output`` (#1453, #1454).
    ``joins`` is what the plan read of the model's joins
    (:func:`.join_reading.read_model_joins`), which ``dex_join_coverage`` reads
    (#1456); ``None`` reads none. A generated test already in the YAML is never
    removed: the switch decides what renders, not what stays. A listed
    ``dex_join_coverage`` whose join the model's SQL no longer writes is
    reported ``stale``, with a note, for a reviewer to remove. ``history``
    reads a declared ``history`` block's snapshot for ``dex_temporal_contract``
    (#1458; see :func:`_generated`).
    """

    text = _render(content, declaration, notes, flagged_tests, [])
    if not gt.kinds():
        # No kind registered: nothing to generate, so no entry to read back.
        return text
    planned = _generated(
        _effective(text, declaration),
        render_tests,
        dialect,
        outputs,
        joins,
        history=history,
    )
    on = [g for g in planned if g["enabled"]]
    if on:
        # Rendered on their own, after the facts: a bare declaration of the
        # model writes no key, so this pass adds the generated tests only.
        text = _render(
            text, Declaration(model=declaration.model), notes, flagged_tests, on
        )
    if generated is not None and planned:
        generated.extend(_report_rows(content, text, planned))
    if joins is not None and getattr(joins, "read", False):
        for row in _stale_join_tests(content, declaration.model, planned):
            if generated is not None:
                generated.append(row)
            if notes is not None:
                notes.append(_stale_note(row))
    for row in _stale_freshness_tests(content, declaration.model, planned):
        if generated is not None:
            generated.append(row)
        if notes is not None:
            notes.append(_stale_note(row))
    if history is not None:
        # A listed dex_temporal_contract the history no longer gives (#1458):
        # a policy, the snapshot, a measure or its column changed, or the
        # history was removed, so the test holds the model to a contract the
        # declaration no longer states.
        for row in _stale_join_tests(
            content,
            declaration.model,
            planned,
            test=gt.TEMPORAL_CONTRACT,
            reason="history_changed",
        ):
            if generated is not None:
                generated.append(row)
            if notes is not None:
                notes.append(_stale_note(row))
    return text


def _report_rows(
    content: str | None, text: str, planned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The ``data.generated_tests`` rows for ``planned``, rendered from
    ``content`` into ``text``."""

    rows: list[dict[str, Any]] = []
    for g in planned:
        row = {key: g[key] for key in ("model", "column", "test", "cadence", "claim")}
        if g.get("variant") is not None:
            row["variant"] = g["variant"]
        # Read at the test's own place, matched the way rendering matches,
        # so a renamed model's tests elsewhere in the file and a hand-written
        # equivalent are each told apart (#1449).
        listed_before = _listed_at(content, g)
        if g["enabled"]:
            row["status"] = (
                "added"
                if not listed_before and _listed_at(text, g)
                else "already_listed"
            )
        elif listed_before:
            row["status"] = "listed_while_off"
        else:
            row["status"] = "not_rendered"
            row["reason"] = g["off_reason"]
        rows.append(row)
    return rows


# --- tests on a referenced parent (#1457) ---------------------------------------


def _project_references(
    files: Mapping[str, str],
) -> list[tuple[str, str, str, str]]:
    """Every ``references`` the project's YAML declares, as ``(child, column,
    parent, parent column)``, read from each model entry (each version under
    its label, as dbt builds it) in file order."""

    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict):
                continue
            for label, variant in mv.entry_variants(entry):
                for column in variant.get("columns") or []:
                    if not isinstance(column, dict) or not isinstance(
                        column.get("name"), str
                    ):
                        continue
                    dex = entry_meta(column).get(DEX_META_KEY)
                    target = dex.get("references") if isinstance(dex, dict) else None
                    if not isinstance(target, str):
                        continue
                    parent, _, parent_column = target.strip().rpartition(".")
                    if not parent or not parent_column:
                        continue
                    key = (label.lower(), column["name"].lower(), target.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((label, column["name"], parent, parent_column))
    return out


def _unique_tested(files: Mapping[str, str], model: str) -> frozenset[str]:
    """The columns of ``model``'s entry that list dbt's ``unique`` test,
    folded to lower case."""

    out: set[str] = set()
    for _label, entry in _entries(files, {model}):
        for column in entry.get("columns") or []:
            if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                continue
            listed = column.get("data_tests") or column.get("tests") or []
            if isinstance(listed, list) and any(_whole_unique(test) for test in listed):
                out.add(column["name"].lower())
    return frozenset(out)


def _whole_unique(test: Any) -> bool:
    """Whether ``test`` is dbt's ``unique`` over every row of the column: one
    with a ``where`` (which dbt applies first, so a key repeated outside it
    goes unseen) or disabled covers nothing. An ``enabled`` other than a
    literal true (a Jinja expression dbt renders per target) is not credited:
    the referenced-key test then renders, rather than no check holding."""

    if _listed_name(test) != "unique":
        return False
    if not isinstance(test, Mapping):
        return True
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return True
    config = body.get("config")
    for scope in (config if isinstance(config, Mapping) else {}, body):
        if scope.get("where") not in (None, ""):
            return False
        if "enabled" in scope and scope["enabled"] is not True:
            return False
    return True


def _parent_unwritable(
    files: Mapping[str, str], view: DbtProjectView, parent: str
) -> str | None:
    """Why ``parent``'s YAML entry cannot hold a test (#1457), or ``None``
    where it can: its project model has an entry under ``models:``."""

    if model_sql_path(files, view.model_paths, parent) is not None:
        if package_documented_overrides(view, files, [parent]):
            return gt.PARENT_IN_PACKAGE
        return None if schema_path(files, parent) is not None else gt.PARENT_UNDECLARED
    if _is_seed_or_snapshot(files, view, parent):
        return gt.PARENT_UNDECLARED
    with contextlib.suppress(Exception):
        packaged = find_package_target(
            view.root,
            parent,
            project_yml=files.get("dbt_project.yml"),
            manifest=view.manifest,
        )
        if packaged is not None:
            return gt.PARENT_IN_PACKAGE
    return gt.PARENT_UNDECLARED


def render_referenced_keys(
    files: Mapping[str, str],
    view: DbtProjectView,
    declared: Iterable[str],
    *,
    render_tests: bool | list[str] = False,
    dialect: str | None = None,
    generated: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    flagged_tests: list[bool] | None = None,
) -> dict[str, str]:
    """The tests a ``references`` implies on its parent (#1457), rendered into
    the parent's YAML entry: ``{path: new content}`` for each file changed.

    Reads every ``references`` in ``files`` (the project as the plan leaves
    it) whose child or parent is among ``declared``, so declaring a child
    renders its parent's test, and declaring the parent renders the tests the
    references declared before it imply. One test per parent column, however
    many children reference it. A parent that cannot hold the test (no
    ``models:`` entry, a seed, a snapshot, a package's model) renders nothing
    and is listed ``not_rendered`` with the reason: Dex never creates an entry
    to hold a test. Each row of ``generated`` names the children under
    ``referenced_by``.
    """

    kinds = [kind for kind in gt.kinds() if kind.on_parent]
    if not kinds:
        return {}
    vmap = mv.cached_version_map(files, view.model_paths)
    wanted = {name.lower() for name in mv.expand(declared, vmap)}
    wanted |= {name.lower() for name in declared}
    # parent (folded) -> (its spelling, {column folded: (spelling, children)})
    parents: dict[str, tuple[str, dict[str, tuple[str, list[str]]]]] = {}
    for child, column, parent, parent_column in _project_references(files):
        # A bare name of a versioned model is the version an unversioned
        # ref() reads, so its test goes on that version alone: on the shared
        # entry every version would inherit it, one the child never joins.
        if mv.split_label(parent)[1] is None:
            versioned = mv.lookup(vmap, parent)
            if versioned is not None and versioned.default_label is not None:
                parent = str(versioned.default_label)
        if child.lower() not in wanted and parent.lower() not in wanted:
            continue
        spelled, columns = parents.setdefault(parent.lower(), (parent, {}))
        _name, children = columns.setdefault(parent_column.lower(), (parent_column, []))
        children.append(f"{child}.{column}")

    out: dict[str, str] = {}
    current = dict(files)
    for spelled, columns in parents.values():
        references = gt.ParentReferences(
            columns={
                name: tuple(sorted(children)) for name, children in columns.values()
            },
            unique_tested=_unique_tested(current, spelled),
        )
        referenced_by = {
            name.lower(): refs for name, refs in references.columns.items()
        }
        blocked = _parent_unwritable(current, view, spelled)
        path = schema_path(current, spelled) if blocked is None else None
        content = current.get(path) if path else None
        stated = (
            _effective(content, Declaration(model=spelled))
            if content
            else Declaration(model=spelled)
        )
        planned = _generated(
            stated,
            render_tests,
            dialect,
            model_outputs(current, view.model_paths, spelled),
            parent_references=references,
        )
        if blocked is not None:
            for g in planned:
                g["enabled"], g["off_reason"] = False, blocked
        text = content
        on = [g for g in planned if g["enabled"]]
        if on and path is not None:
            try:
                text = _render(
                    content, Declaration(model=spelled), notes, flagged_tests, on
                )
            except RewriteError as exc:
                raise DeclarationError(
                    f"dex could not write the {gt.REFERENCED_KEY} test into "
                    f"'{spelled}''s entry in {path} predictably ({exc}); add it by "
                    "hand, then declare again"
                ) from exc
            if text != content:
                current[path] = text
                out[path] = text
        if generated is not None:
            for row in _report_rows(content, text or "", planned):
                row["referenced_by"] = list(
                    referenced_by.get(str(row["column"]).lower(), ())
                )
                generated.append(row)
    return out


def _stale_note(row: Mapping[str, Any]) -> str:
    if row["test"] == gt.FRESHNESS:
        return (
            f"'{row['model']}.{row['column']}' lists a {gt.FRESHNESS} test (claim "
            f"{row['claim']}) for a freshness it no longer declares (its max_lag or "
            "basis changed); it still holds the column to the old lag, so remove it "
            "from the YAML"
        )
    if row["test"] == gt.TEMPORAL_CONTRACT:
        return (
            f"'{row['model']}' lists a {gt.TEMPORAL_CONTRACT} test (claim "
            f"{row['claim']}) the history it declares no longer gives (a policy, "
            "the snapshot, a measure or its column changed, or the history was "
            "removed); it holds the model to a contract the declaration no longer "
            "states, so remove it from the YAML"
        )
    return (
        f"'{row['model']}' lists a {gt.JOIN_COVERAGE} test (claim {row['claim']}) "
        "for a join its SQL no longer writes; it measures inputs the model does "
        "not join, so remove it from the YAML (`transform declare "
        f"{row['model']}` renders the test for the join it writes now)"
    )


def stale_join_tests(
    view: DbtProjectView,
    edits: list[PlanEdit],
    declared: Iterable[str],
    dialect: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The listed ``dex_join_coverage`` tests a plan leaves stale on the models
    whose SQL it edits without declaring them (#1456): a join is usually
    changed by editing the SQL alone, which renders no declaration, so the
    check :func:`render` runs for a declared model runs here for the rest.
    Returns ``data.generated_tests`` rows (``status: stale``) and a warning
    per row. A model whose new SQL the plan cannot read says nothing: whether
    its join changed is not known."""

    names = {name.lower() for name in declared}
    files = post_change_files(view, edits)
    vmap = mv.cached_version_map(files, view.model_paths)
    # A version's file builds its label (dim_teams_v2.sql is dim_teams.v2),
    # whose YAML is the versioned entry, as undeclared_model_warnings reads it.
    labels = [
        mv.label_for_file(edit.path, vmap) or PurePosixPath(edit.path).stem
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL and edit.op is EditOp.UPSERT
    ]
    edited = [label for label in labels if label.lower() not in names]
    if not edited:
        return [], []
    rows: list[dict[str, Any]] = []
    for model in dict.fromkeys(edited):
        path = schema_path(files, model)
        content = files.get(path) if path else None
        if not content or gt.JOIN_COVERAGE not in content:
            continue
        joins = read_model_joins(files, view.model_paths, model, dialect)
        if not joins.read:
            continue
        stated = _effective(content, Declaration(model=model))
        planned = _generated(
            stated,
            True,
            dialect,
            model_outputs(files, view.model_paths, model),
            joins,
        )
        rows.extend(_stale_join_tests(content, model, planned))
    return rows, [_stale_note(row) for row in rows]


def _stale_join_tests(
    content: str | None,
    model: str,
    planned: list[dict[str, Any]],
    *,
    test: str = gt.JOIN_COVERAGE,
    reason: str = "join_not_in_model",
) -> list[dict[str, Any]]:
    """The ``dex_join_coverage`` tests the model's entry lists whose join no
    longer appears among the joins the plan read (#1456), as
    ``data.generated_tests`` rows with ``status: stale``. Matched by claim, else
    by arguments, as rendering matches a listed test. ``test`` and ``reason``
    ask the same of another model-level kind (#1458: a ``dex_temporal_contract``
    whose claim the declared history no longer gives, ``history_changed``)."""

    node = _listed_entry(content, model)
    if node is None:
        return []
    listed = node.get("data_tests") or node.get("tests") or []
    current = [g["spec"] for g in planned if g["test"] == test and g["enabled"]]
    current += [
        g["spec"]
        for g in planned
        if g["test"] == test
        and not g["enabled"]
        and g["off_reason"] not in (gt.JOIN_UNREAD,)
    ]
    out: list[dict[str, Any]] = []
    for item in listed if isinstance(listed, list) else []:
        if _listed_name(item) != test or not gt.is_generated(item):
            continue
        claims = {claim_of(spec) for spec in current}
        if claim_of(item) in claims or any(
            _arguments_of(item) == _arguments_of(spec) for spec in current
        ):
            continue
        row: dict[str, Any] = {
            "model": model,
            "column": None,
            "test": test,
            "cadence": gt.SCHEDULED,
            "claim": claim_of(item),
            "status": "stale",
            "reason": reason,
        }
        # A temporal test names its measure (#1458), as its rendered row does.
        measure = (_arguments_of(item) or {}).get("measure")
        if isinstance(measure, str):
            row["variant"] = measure
        out.append(row)
    return out


def _listed_entry(content: str | None, model: str) -> Mapping[str, Any] | None:
    """The model's YAML entry in ``content`` as written (a version's item of
    ``versions:`` for a label), or ``None``."""

    if not content:
        return None
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    base, version = mv.split_label(model)
    entries = loaded.get("models") if isinstance(loaded, Mapping) else None
    node: Any = next(
        (
            e
            for e in entries or []
            if isinstance(e, Mapping) and str(e.get("name", "")).lower() == base.lower()
        ),
        None,
    )
    if node is not None and version is not None:
        node = next(
            (
                item
                for item in node.get("versions") or []
                if isinstance(item, Mapping) and str(item.get("v")) == str(version)
            ),
            None,
        )
    return node if isinstance(node, Mapping) else None


def _stale_freshness_tests(
    content: str | None, model: str, planned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The ``dex_freshness`` tests a column lists whose claim the declared
    freshness no longer gives (#1460): its ``max_lag`` or ``basis`` changed, so
    the test still holds the column to the old lag. Each is a
    ``data.generated_tests`` row with ``status: stale`` and ``reason:
    freshness_changed``. Only the columns whose freshness this declaration
    states are read: a column it says nothing about keeps what it lists."""

    node = _listed_entry(content, model)
    if node is None:
        return []
    current: dict[str, list[Any]] = {}
    for g in planned:
        if g["test"] == gt.FRESHNESS and g["column"] is not None:
            current.setdefault(str(g["column"]).lower(), []).append(g["spec"])
    out: list[dict[str, Any]] = []
    for column in node.get("columns") or []:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            continue
        specs = current.get(column["name"].lower())
        if specs is None:
            continue
        claims = {claim_of(spec) for spec in specs}
        listed = column.get("data_tests") or column.get("tests") or []
        for item in listed if isinstance(listed, list) else []:
            if _listed_name(item) != gt.FRESHNESS or not gt.is_generated(item):
                continue
            # Matched as rendering matches a listed test: by claim, else by
            # arguments, so a basis-only change (same lag, a test that behaves
            # the same) keeps the listed test rather than calling it stale.
            if claim_of(item) in claims or any(
                _arguments_of(item) == _arguments_of(spec) for spec in specs
            ):
                continue
            out.append(
                {
                    "model": model,
                    "column": column["name"],
                    "test": gt.FRESHNESS,
                    "cadence": gt.EVERY_RUN,
                    "claim": claim_of(item),
                    "status": "stale",
                    "reason": "freshness_changed",
                }
            )
    return out


def _render(
    content: str | None,
    declaration: Declaration,
    notes: list[str] | None,
    flagged_tests: list[bool] | None,
    generated: list[dict[str, Any]],
) -> str:
    model = declaration.model
    base, version = mv.split_label(model)
    if version is not None and content is not None:
        return _render_version(
            content,
            declaration,
            base,
            version,
            notes,
            flagged_tests=flagged_tests,
            generated=generated,
        )
    text = _with_entry(content, model)
    entry = lambda root: _model_entry(root, model)  # noqa: E731
    text = _restyle_flow_entry(text, entry)

    if declaration.description is not None:
        text = _set_key(text, entry, "description", declaration.description)
    model_dex = _model_dex(declaration)
    if model_dex:
        text = _set_dex(text, entry, model_dex)

    for name, column in _rendered_columns(declaration, generated).items():
        text = _ensure_column(text, model, name)
        locate = _column_locator(model, name)
        if column["description"] is not None:
            text = _set_key(text, locate, "description", column["description"])
        if column["dex"]:
            text = _set_dex(text, locate, column["dex"])
        if column["tests"]:
            text, flagged = _add_tests(text, locate, column["tests"])
            if flagged and flagged_tests is not None:
                flagged_tests.append(True)
    model_tests = [g["spec"] for g in generated if g["column"] is None]
    if model_tests:
        text, flagged = _add_tests(text, entry, model_tests)
        if flagged and flagged_tests is not None:
            flagged_tests.append(True)
    return text


def _effective(text: str, declaration: Declaration) -> Declaration:
    """The declaration ``text`` states for the model: what was declared before,
    with ``declaration`` merged in. Falls back to ``declaration`` where the entry
    holds something a declaration would refuse (a hand edit), so a fact written
    by hand can only add tests, never stop the payload's own."""

    from .drafting import read_declared

    stated = read_declared({"schema.yml": text}, declaration.model)
    try:
        return Declaration.model_validate({**stated, "model": declaration.model})
    except ValueError:
        return declaration


def _generated(
    declaration: Declaration,
    render_tests: bool | list[str],
    dialect: str | None = None,
    outputs: frozenset[str] | None = None,
    joins: Any = None,
    parent_references: gt.ParentReferences | None = None,
    history: Any = None,
) -> list[dict[str, Any]]:
    """Every generated test the declaration gives, each with its claim, its
    rendered spec, and whether ``render_tests`` turns its kind on (#1449) on
    ``dialect`` (#1451), for columns the model outputs (#1453, #1454), with
    the joins the plan read for a kind that reads them (#1456).

    ``parent_references`` asks for the other half (#1457): the tests of the
    kinds that check what other models' declarations imply about this one
    (``on_parent``), and only those. Without it, those kinds give nothing.

    ``history`` reads a declared ``history`` block's snapshot (#1458):
    :func:`.history_reading.history_reader`, called as ``history(model,
    history)``. ``None`` reads nothing, and a kind that reads it gives
    nothing."""

    out: list[dict[str, Any]] = []
    model = declaration.model
    for kind in gt.kinds():
        if kind.on_parent != (parent_references is not None):
            continue
        switched = gt.enabled(kind, render_tests)
        on = switched and gt.supports(kind, dialect)
        off_reason = gt.DIALECT_UNSUPPORTED if switched else gt.off_reason(render_tests)
        if kind.on_parent:
            instances = kind.instances(  # type: ignore[call-arg]
                declaration, parent_references
            )
        elif kind.reads_joins:
            instances = kind.instances(declaration, joins)  # type: ignore[call-arg]
        elif kind.reads_history:
            reading = (
                history(model, declaration.history)
                if history is not None and declaration.history is not None
                else None
            )
            instances = kind.instances(declaration, reading)  # type: ignore[call-arg]
        else:
            instances = kind.instances(declaration)
        for instance in instances:
            claim = claim_id(
                model, instance.field, instance.columns, dict(instance.params)
            )
            renders, reason = on, None if on else off_reason
            # The model's columns the test reads: the one it sits under, or a
            # model-level test's columns (a grain, a parent key; #1454).
            reads = (
                (instance.column,) if instance.column is not None else instance.columns
            )
            if (
                on
                and outputs is not None
                and any(column.lower() not in outputs for column in reads)
            ):
                renders, reason = False, gt.COLUMN_NOT_OUTPUT
            if instance.withheld is not None:
                # A fact only maintain verify judges, as a question (#1455).
                renders, reason = False, instance.withheld
            out.append(
                {
                    "model": model,
                    "column": instance.column,
                    "test": kind.name,
                    "cadence": kind.cadence,
                    "claim": claim,
                    "variant": instance.variant,
                    "enabled": renders,
                    "off_reason": reason,
                    "spec": generated_spec(
                        kind.name,
                        cadence=kind.cadence,
                        claim=claim,
                        arguments=instance.arguments,
                        test_name=(
                            f"{kind.name}_{claim}" if kind.named_by_claim else None
                        ),
                    ),
                }
            )
    return out


def _listed_at(content: str | None, test: Mapping[str, Any]) -> bool:
    """Whether ``content`` lists the generated ``test`` where it belongs: the
    model's entry (or the version's item, for a label), then the column it sits
    under, matched by :func:`_is_listed`."""

    if not content:
        return False
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return False
    base, version = mv.split_label(test["model"])
    entries = loaded.get("models") if isinstance(loaded, Mapping) else None
    node: Any = next(
        (
            e
            for e in entries or []
            if isinstance(e, Mapping) and str(e.get("name", "")).lower() == base.lower()
        ),
        None,
    )
    if node is not None and version is not None:
        item = next(
            (
                item
                for item in node.get("versions") or []
                if isinstance(item, Mapping) and str(item.get("v")) == str(version)
            ),
            None,
        )
        # The version as dbt builds it: inherited columns (and their tests)
        # included, its own overrides and exclusions applied.
        node = mv.effective_entry(node, item) if item is not None else None
    if node is not None and test["column"] is not None:
        node = next(
            (
                c
                for c in node.get("columns") or []
                if isinstance(c, Mapping)
                and str(c.get("name", "")).lower() == test["column"].lower()
            ),
            None,
        )
    if not isinstance(node, Mapping):
        return False
    listed = node.get("data_tests") or node.get("tests") or []
    return isinstance(listed, list) and _is_listed(test["spec"], listed)


def _render_version(
    content: str,
    declaration: Declaration,
    base: str,
    version: str,
    notes: list[str] | None,
    flagged_tests: list[bool] | None = None,
    generated: list[dict[str, Any]] | None = None,
) -> str:
    """Render a declaration into one item of ``base``'s ``versions:`` list.

    The version's own ``description`` and ``config.meta.dex`` take the model
    facts, and its own ``columns`` the column facts. A column the version only
    inherits from the shared entry is overridden the way dbt reads an override:
    excluded from the version's ``include``/``exclude`` element and written as
    the version's own entry, carrying the inherited description, tests and meta
    forward, since dbt replaces an inherited column wholesale with a same-named
    version entry and would otherwise also parse its tests twice. A fact the
    inherited entry already states forks nothing.
    """

    item = lambda root: _version_item(root, base, version)  # noqa: E731
    text = _restyle_flow_entry(content, item)
    if item(_compose(text)) is None:
        raise RewriteError(f"it has no version {version} under '{base}'")
    label = f"{base}.v{version}"
    if declaration.description is not None:
        text = _set_key(text, item, "description", declaration.description)
    model_dex = _model_dex(declaration)
    if model_dex:
        text = _set_dex(text, item, model_dex)

    generated = generated or []
    for name, column in _rendered_columns(declaration, generated).items():
        text, owned = _ensure_version_column(text, base, version, name, column)
        if not owned:
            continue
        if owned == "forked" and notes is not None:
            notes.append(
                f"'{label}.{name}' was inherited from the shared '{base}' entry; it "
                f"is now excluded from v{version}'s include/exclude and declared as "
                f"v{version}'s own column, with the inherited description and tests "
                "carried over, so the declared facts apply to this version only"
            )
        locate = _version_column_locator(base, version, name)
        if column["description"] is not None:
            text = _set_key(text, locate, "description", column["description"])
        if column["dex"]:
            text = _set_dex(text, locate, column["dex"])
        if column["tests"]:
            text, flagged = _add_tests(text, locate, column["tests"])
            if flagged and flagged_tests is not None:
                flagged_tests.append(True)
    model_tests = [g["spec"] for g in generated if g["column"] is None]
    if model_tests:
        text, flagged = _add_tests(text, item, model_tests)
        if flagged and flagged_tests is not None:
            flagged_tests.append(True)
    return text


def _ensure_version_column(
    text: str, base: str, version: str, name: str, column: dict[str, Any]
) -> tuple[str, str | None]:
    """The version's own entry for ``name``: ``owned``, ``forked``, ``created``,
    or ``None`` where the inherited entry already says everything declared."""

    root = _compose(text)
    if _version_column_locator(base, version, name)(root) is not None:
        return text, "owned"
    entry = _plain(_model_entry(root, base))
    item = _plain(_version_item(root, base, version))
    inherited = next(
        (c for c in mv.inherited_columns(entry, item) if c.get("name") == name),
        None,
    )
    if inherited is None:
        return _append_version_column(text, base, version, {"name": name}), "created"
    if _already_states(inherited, column):
        return text, None
    text = _exclude_from_version(text, base, version, name)
    return _append_version_column(text, base, version, dict(inherited)), "forked"


def _already_states(entry: Mapping[str, Any], column: dict[str, Any]) -> bool:
    """Whether a column entry already carries every fact ``column`` would write."""

    if (
        column["description"] is not None
        and entry.get("description") != column["description"]
    ):
        return False
    dex = _dex_of(entry)
    if any(dex.get(key) != value for key, value in column["dex"].items()):
        return False
    tests = entry.get("data_tests") or entry.get("tests") or []
    listed = [t for t in tests if isinstance(t, (str, dict))]
    return all(_is_listed(spec, listed) for spec in column["tests"])


def _exclude_from_version(text: str, base: str, version: str, name: str) -> str:
    """``text`` with ``name`` no longer inherited by version ``version``."""

    root = _compose(text)
    item_node = _version_item(root, base, version)
    columns = _child(item_node, "columns")
    item = _plain(item_node)
    include, exclude, position = mv.include_exclude(item)
    if isinstance(include, list):
        rewritten: dict[str, Any] = {"include": [c for c in include if c != name]}
    else:
        # A version with no include/exclude element includes everything; it
        # is written as `all`, the spelling dbt documents (an unquoted `*` is
        # a YAML alias).
        written = include if position is not None and include else "all"
        rewritten = {"include": written, "exclude": [*exclude, name]}
    if columns is None:
        return _set_key(
            text, lambda r: _version_item(r, base, version), "columns", [rewritten]
        )
    listed = columns[1]
    if listed.flow_style or not listed.value:
        raise RewriteError(f"version {version}'s `columns:` is not a block list")
    if position is not None:
        node = listed.value[position]
        start, end = _item_span(text, node)
        dash = text[start : node.start_mark.index]
        dash_indent = dash[: len(dash) - len(dash.lstrip(" "))]
        return splice(
            text,
            [
                (
                    start,
                    before_trailing_blanks(text, end),
                    _render_flow_item(rewritten, dash_indent),
                )
            ],
        )
    first = listed.value[0]
    start, _end = _item_span(text, first)
    line = text[start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    return splice(text, [(start, start, _render_flow_item(rewritten, dash_indent))])


def _render_flow_item(value: dict[str, Any], dash_indent: str) -> str:
    """An include/exclude element, with its lists written inline."""

    # One line each; a scalar's dump trails a `...` document end marker.
    pairs = [
        f"{key}: {_dump(v, flow=True).splitlines()[0]}" for key, v in value.items()
    ]
    lines = [f"{dash_indent}- {pairs[0]}\n"]
    lines += [f"{dash_indent}  {pair}\n" for pair in pairs[1:]]
    return "".join(lines)


def _append_version_column(
    text: str, base: str, version: str, column: dict[str, Any]
) -> str:
    """``text`` with ``column`` appended to version ``version``'s own columns."""

    root = _compose(text)
    columns = _child(_version_item(root, base, version), "columns")
    if columns is None or not _sequence(columns[1]):
        return _set_key(
            text, lambda r: _version_item(r, base, version), "columns", [column]
        )
    listed = columns[1]
    if listed.flow_style:
        raise RewriteError(f"version {version}'s `columns:` is written in flow style")
    first = listed.value[0]
    line_start = text.rfind("\n", 0, first.start_mark.index) + 1
    line = text[line_start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    end = max(_item_span(text, node)[1] for node in listed.value)
    at = before_trailing_blanks(text, end)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + _render_item(column, dash_indent))])


def _item_span(text: str, node: Any) -> tuple[int, int]:
    from .rewrite import _entry_span

    return _entry_span(text, node)


def _plain(node: Any) -> dict[str, Any]:
    if node is None:
        return {}
    value = yaml.safe_load(yaml.serialize(node))
    return value if isinstance(value, dict) else {}


def _rendered_columns(
    declaration: Declaration, generated: Iterable[Mapping[str, Any]] = ()
) -> dict[str, dict[str, Any]]:
    """What each column receives: its description, its dex meta, and its tests.

    ``generated`` are the generated tests the switch turned on (#1449); each one
    that sits under a column follows that column's built-ins.

    Each test carries the id of the claim it checks (#1448), derived from the
    declared fact and never from the test's name: a ``null_rule: never``, a
    ``references`` (with the parent it names), and a single-column grain, whose
    ``unique`` and ``not_null`` are two claims since either can fail alone. A
    grain column also declared ``null_rule: never`` has one ``not_null``, and it
    carries the null rule's claim, the fact rendered first.
    """

    out: dict[str, dict[str, Any]] = {}
    model = declaration.model

    def slot(name: str) -> dict[str, Any]:
        return out.setdefault(name, {"description": None, "dex": {}, "tests": []})

    for name, column in declaration.columns.items():
        entry = slot(name)
        entry["description"] = column.description
        entry["dex"] = {
            key: value.value if isinstance(value, Enum) else value
            for key, value in (
                ("role", column.role),
                ("null_rule", column.null_rule),
                ("aggregation", column.aggregation),
                ("additivity", column.additivity),
                ("unit", column.unit),
                ("references", column.references),
            )
            if value is not None
        }
        if column.freshness is not None:
            entry["dex"]["freshness"] = column.freshness.model_dump(mode="json")
        if column.null_rule is not None and column.null_rule.strip().lower() == "never":
            entry["tests"].append(
                _unsampled(
                    "not_null",
                    claim=claim_id(model, "null_rule", [name], {"null_rule": "never"}),
                )
            )
        if column.references is not None:
            parent, _, parent_column = column.references.rpartition(".")
            to = mv.ref_call(parent)
            entry["tests"].append(
                _unsampled(
                    "relationships",
                    claim=claim_id(
                        model, "references", [name], {"references": column.references}
                    ),
                    arguments={"to": to, "field": parent_column},
                )
            )

    grain = declaration.grain or []
    if len(grain) == 1:
        tests = slot(grain[0])["tests"]
        for name in ("unique", "not_null"):
            if name not in {_spec_name(spec) for spec in tests}:
                claim = claim_id(model, "grain", grain, {"check": name})
                tests.insert(
                    0 if name == "unique" else 1, _unsampled(name, claim=claim)
                )
    for test in generated:
        if test["column"] is not None:
            slot(test["column"])["tests"].append(test["spec"])
    return out


def _model_dex(declaration: Declaration) -> dict[str, Any]:
    dex: dict[str, Any] = {}
    if declaration.grain:
        dex["grain"] = list(declaration.grain)
    if declaration.population is not None:
        dex["population"] = declaration.population.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
    if declaration.assumptions:
        dex["assumptions"] = [
            assumption.model_dump(mode="json", exclude_none=True)
            for assumption in declaration.assumptions
        ]
    if declaration.history is not None:
        dex["history"] = declaration.history.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
    return dex


# The splices below share one shape: compose the current text, locate a node,
# splice one key, return the new text. A node is located by a function rather
# than held, because every splice moves the bytes after it.


def _model_entry(root: Any, model: str) -> Any:
    for key, node in _mapping(root):
        if key != "models":
            continue
        for entry in _sequence(node):
            if _scalar_value(dict(_mapping(entry)).get("name")) == model:
                return entry
    return None


def _column_locator(model: str, column: str):
    def locate(root: Any) -> Any:
        entry = _model_entry(root, model)
        for item in _sequence(dict(_mapping(entry)).get("columns")):
            if _scalar_value(dict(_mapping(item)).get("name")) == column:
                return item
        return None

    return locate


def _version_item(root: Any, base: str, version: str) -> Any:
    entry = _model_entry(root, base)
    for item in _sequence(dict(_mapping(entry)).get("versions")):
        value = dict(_mapping(item)).get("v")
        if isinstance(value, yaml.ScalarNode) and value.value == version:
            return item
    return None


def _version_column_locator(base: str, version: str, column: str):
    def locate(root: Any) -> Any:
        item = _version_item(root, base, version)
        for entry in _sequence(dict(_mapping(item)).get("columns")):
            if _scalar_value(dict(_mapping(entry)).get("name")) == column:
                return entry
        return None

    return locate


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text)
    except yaml.YAMLError as exc:
        raise RewriteError(
            f"it does not parse as YAML: {exc.__class__.__name__}"
        ) from exc


def _carries_anchor(root: Any, node: Any) -> bool:
    """True when the subtree is aliased: used more than once.

    PyYAML resolves an alias to the anchored node object itself, so a node
    that appears more than once in the document is an aliased one: a restyle
    re-renders the subtree from its parsed value, which would drop the
    anchor and break a reference written elsewhere (#1203). Such a mapping
    keeps refusing, with the reason named; an anchor that nothing aliases
    does not, because no reference can break.
    """

    counts: dict[int, int] = {}

    def count(current: Any, seen: set[int]) -> None:
        # every VISIT counts, even one the seen-set skips: an aliased node is
        # precisely one that is reached twice, and a revisit must not be
        # mistaken for a cycle before it is counted
        counts[id(current)] = counts.get(id(current), 0) + 1
        if id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, yaml.MappingNode):
            for k, v in current.value:
                count(k, seen)
                count(v, seen)
        elif isinstance(current, yaml.SequenceNode):
            for item in current.value:
                count(item, seen)

    count(root, set())

    def carries(current: Any, seen: set[int]) -> bool:
        if id(current) in seen:
            return False
        seen.add(id(current))
        if counts.get(id(current), 0) > 1:
            return True
        if isinstance(current, yaml.MappingNode):
            return any(carries(k, seen) or carries(v, seen) for k, v in current.value)
        if isinstance(current, yaml.SequenceNode):
            return any(carries(item, seen) for item in current.value)
        return False

    return carries(node, set())


def _restyle_flow_entry(text: str, locate) -> str:
    """``text`` with the located entry restyled from flow style to block style.

    A declaration is written INTO the entry, so a flow-style entry blocks it
    whole: the entry is converted to block style in the same edit instead,
    keeping its keys and values, and every byte outside its span (other
    entries, comments) exactly as the author wrote it (#1203). An entry
    nested in a flow-style list is left untouched: a block entry cannot sit
    inside one, and the downstream refusal names that.
    """

    root = _compose(text)
    node = locate(root)
    if (
        node is None
        or not isinstance(node, yaml.MappingNode)
        or not node.flow_style
        or not node.value
    ):
        return text
    if _carries_anchor(root, node):
        raise RewriteError(
            "the entry is a flow-style mapping that is aliased; a restyle "
            "would break its YAML anchor, rewrite it in block style"
        )
    start, end = _span(node)
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    if prefix.strip() != "-":
        return text
    body = _dump(yaml.safe_load(yaml.serialize(node))).strip("\n")
    lines = body.split("\n")
    indent = " " * len(prefix)
    rendered = lines[0] + "".join(f"\n{indent}{ln}" for ln in lines[1:])
    return splice(text, [(start, end, rendered)])


def _restyle_flow_value(text: str, root: Any, parent: Any, key: str) -> str:
    """``text`` with ``parent``'s flow-style ``key`` value restyled to block.

    A block mapping cannot sit inside a flow one, so the value's own keys and
    values are re-rendered one level under the parent's; every byte outside
    the pair is untouched. An empty flow mapping holds nothing to keep: its
    pair is dropped, and the caller writes the key fresh (#1203).
    """

    found = _child(parent, key)
    if found is None:
        return text
    k, v = found
    if not isinstance(v, yaml.MappingNode) or not v.flow_style:
        return text
    if _carries_anchor(root, v):
        raise RewriteError(
            f"'{key}' is a flow-style mapping that is aliased; a restyle "
            "would break its YAML anchor, rewrite it in block style"
        )
    if not v.value:
        line_start = text.rfind("\n", 0, k.start_mark.index) + 1
        if text[line_start : k.start_mark.index].strip():
            raise RewriteError(f"'{key}' shares its line with the entry's bullet")
        start, end = _pair_span(text, _span(k), v)
        return splice(text, [(start, end, "")])
    body = _dump(yaml.safe_load(yaml.serialize(v))).strip("\n")
    indent = " " * (parent.value[0][0].start_mark.column + 2)
    start, end = _span(v)
    rendered = "\n" + "\n".join(f"{indent}{ln}" for ln in body.split("\n"))
    return splice(text, [(start, end, rendered)])


def _child(mapping: Any, key: str) -> tuple[Any, Any] | None:
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None


def _set_key(text: str, locate, key: str, value: Any) -> str:
    """Write ``key: value`` into the located mapping, replacing any prior value."""

    mapping = locate(_compose(text))
    if not isinstance(mapping, yaml.MappingNode) or not mapping.value:
        raise RewriteError(f"the entry that would hold '{key}' is not a mapping")
    if mapping.flow_style:
        raise RewriteError(
            f"the entry that would hold '{key}' is written in flow style; rewrite it "
            "in block style"
        )
    indent = " " * mapping.value[0][0].start_mark.column
    rendered = _render_pair(key, value, indent)

    found = _child(mapping, key)
    if found is not None:
        k, v = found
        line_start = text.rfind("\n", 0, k.start_mark.index) + 1
        if text[line_start : k.start_mark.index].strip():
            raise RewriteError(f"'{key}' shares its line with the entry's bullet")
        start, end = _pair_span(text, _span(k), v)
        return splice(text, [(start, before_trailing_blanks(text, end), rendered)])

    # A new key goes before `columns:` where there is one, so a model's own
    # description and config read above its column list, the way dbt documents it.
    anchor = _child(mapping, "columns")
    if anchor is not None:
        line_start = text.rfind("\n", 0, anchor[0].start_mark.index) + 1
        if not text[line_start : anchor[0].start_mark.index].strip():
            return splice(text, [(line_start, line_start, rendered)])
    last_key, last_value = mapping.value[-1]
    _start, end = _pair_span(text, _span(last_key), last_value)
    at = before_trailing_blanks(text, end)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + rendered)])


def _set_dex(text: str, locate, dex: dict[str, Any]) -> str:
    """Merge ``dex`` into the located entry's ``config.meta.dex``.

    Shallow: a key the declaration states replaces the one already there, and a
    key it leaves out is kept, so a later partial declaration does not erase an
    earlier one. Only the ``dex`` namespace is rewritten; a PII stamp or any other
    key under ``config.meta`` is untouched.
    """

    text = _restyle_flow_entry(text, locate)
    root = _compose(text)
    node = locate(root)
    plain = yaml.safe_load(yaml.serialize(node)) if node is not None else None
    existing = (entry_meta(plain) or {}).get(DEX_META_KEY)
    merged = {**existing, **dex} if isinstance(existing, dict) else dict(dex)
    if isinstance(existing, dict):
        merged = _merge_population(existing, merged)
    return _set_meta(text, locate, DEX_META_KEY, merged)


def _set_meta(text: str, locate, key: str, value: Any) -> str:
    """Write ``key: value`` under the located entry's ``config.meta``, creating
    ``config`` and ``meta`` where they are missing and restyling either from
    flow to block style. Every other key under ``config.meta`` is untouched, so
    the ``dex`` namespace and a PII stamp (#1100) are written side by side."""

    text = _restyle_flow_entry(text, locate)
    root = _compose(text)
    node = locate(root)
    config = _child(node, "config")
    if (
        config is not None
        and isinstance(config[1], yaml.MappingNode)
        and config[1].flow_style
    ):
        # `config: {...}` written in flow style: the mapping is restyled to
        # block style in the same edit, so the steps below write into a
        # block mapping instead of refusing (#1203). An empty `{}` holds
        # nothing to keep: the pair is dropped and written fresh below.
        text = _restyle_flow_value(text, root, node, "config")
        root = _compose(text)
        node = locate(root)
        config = _child(node, "config")
    if config is None:
        return _set_key(text, locate, "config", {"meta": {key: value}})

    def config_node(r: Any) -> Any:
        found = _child(locate(r), "config")
        return found[1] if found else None

    meta = _child(config[1], "meta")
    if (
        meta is not None
        and isinstance(meta[1], yaml.MappingNode)
        and meta[1].flow_style
    ):
        # `config.meta` in flow style: same restyle, one level down.
        text = _restyle_flow_value(text, root, config[1], "meta")
        root = _compose(text)
        node = locate(root)
        config = _child(node, "config")
        meta = _child(config[1], "meta") if config is not None else None
    if config is not None and not (
        isinstance(config[1], yaml.MappingNode) and config[1].value
    ):
        # `config` is null or empty after the restyles (an only `meta: {}`
        # pair is dropped): writing it fresh keeps nothing that was there,
        # and `_set_key` could not write into a null anyway.
        return _set_key(text, locate, "config", {"meta": {key: value}})
    if meta is None:
        return _set_key(text, config_node, "meta", {key: value})

    def meta_node(r: Any) -> Any:
        found = _child(config_node(r), "meta")
        return found[1] if found else None

    return _set_key(text, meta_node, key, value)


def _merge_population(existing: Mapping[str, Any], merged: dict[str, Any]) -> dict:
    """Keep ``population``'s own keys a later declaration leaves out (#857).

    A model-level key is replaced whole, but ``population`` holds three
    independent statements (a rule, filters, a parent), and a payload that adds
    a parent to a model whose filters were declared earlier would otherwise
    erase the filters. One level, the same rule one level down."""

    before, after = existing.get("population"), merged.get("population")
    if isinstance(before, dict) and isinstance(after, dict) and before is not after:
        merged["population"] = {**before, **after}
    return merged


def _has_sampling_flag(spec: Any) -> bool:
    if isinstance(spec, Mapping):
        for body in spec.values():
            if isinstance(body, Mapping):
                cfg = body.get("config")
                if isinstance(cfg, Mapping):
                    meta = cfg.get("meta")
                    if (
                        isinstance(meta, Mapping)
                        and meta.get("disable_test_samples") is True
                    ):
                        return True
                meta = body.get("meta")
                if (
                    isinstance(meta, Mapping)
                    and meta.get("disable_test_samples") is True
                ):
                    return True
    return False


def _add_tests(text: str, locate: Any, specs: list[Any]) -> tuple[str, bool]:
    """Add the tests ``specs`` names to the located column, keeping its own.

    A test the column already lists, by name, is left exactly as the author wrote
    it. An existing list keeps its key (``tests`` or ``data_tests``); a column with
    no list takes the spelling the file already uses.

    Returns the updated text and whether any test with ``disable_test_samples``
    was newly inserted.
    """

    root = _compose(text)
    node = locate(root)
    _refuse_shared_entry(text, root, node)
    found = None
    for key in ("data_tests", "tests"):
        found = _child(node, key)
        if found is not None:
            break
    if found is None:
        updated = _set_key(text, locate, prevailing_test_key(text), specs)
        inserted_flagged = any(_has_sampling_flag(s) for s in specs)
        return updated, inserted_flagged

    if not isinstance(found[1], yaml.SequenceNode):
        raise RewriteError("a column's test list is not a list")
    text = _claim_earlier_renders(text, locate, specs)
    root = _compose(text)
    node = locate(root)
    found = _child(node, "data_tests") or _child(node, "tests")
    if found is None:  # the claims above change no key, so this cannot happen
        raise RewriteError("a column's test list moved while it was claimed")
    key, listed = found
    present = [_node_value(item) for item in _sequence(listed)]
    todo = [spec for spec in specs if not _is_listed(spec, present)]
    if not todo:
        return text, False

    inserted_flagged = any(_has_sampling_flag(s) for s in todo)
    if _paths(root, listed) > 1:
        # A list another column aliases: this column gets its own copy first,
        # then the tests are added to that copy alone (#1496).
        return _add_tests(_unshare_tests(text, key, listed), locate, specs)
    if listed.flow_style:
        end = listed.end_mark.index
        if text[end - 1] != "]":
            raise RewriteError("a column's test list is not closed")
        items = ", ".join(_flow(spec) for spec in todo)
        joiner = ", " if listed.value else ""
        return (
            splice(text, [(end - 1, end - 1, joiner + items)]),
            inserted_flagged,
        )

    # The list's own dashes, from the token stream: an item that is an alias
    # carries the marks of its anchor, which can sit in another column (#1496).
    column = listed.start_mark.column
    dashes = [
        t.start_mark.index
        for t in yaml.scan(text)
        if isinstance(t, yaml.BlockEntryToken)
        and listed.start_mark.index <= t.start_mark.index < listed.end_mark.index
        and t.start_mark.column == column
    ]
    if not dashes:
        raise RewriteError("a column's test list does not start its items with '-'")
    line_start = text.rfind("\n", 0, dashes[0]) + 1
    dash_indent = text[line_start : dashes[0]]
    if dash_indent.strip():
        raise RewriteError(
            "a column's test list does not start its items on their own lines"
        )
    last_key_line = text.rfind("\n", 0, dashes[-1]) + 1
    from ..yaml_spans import block_end

    end = block_end(text, last_key_line, len(dash_indent))
    at = before_trailing_blanks(text, end)
    rendered = "".join(_render_item(spec, dash_indent) for spec in todo)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + rendered)]), inserted_flagged


def _paths(root: Any, target: Any) -> int:
    """How many paths lead from ``root`` to ``target``: more than one when a
    YAML alias or merge key shares it or a node above it, since PyYAML
    resolves an alias to the anchored node itself."""

    memo: dict[int, int] = {}

    def walk(current: Any, above: frozenset[int]) -> int:
        if current is target:
            return 1
        if id(current) in memo:
            return memo[id(current)]
        if id(current) in above:  # a recursive alias: no path through it
            return 0
        below = above | {id(current)}
        if isinstance(current, yaml.MappingNode):
            children = [n for pair in current.value for n in pair]
        elif isinstance(current, yaml.SequenceNode):
            children = current.value
        else:
            children = []
        memo[id(current)] = sum(walk(child, below) for child in children)
        return memo[id(current)]

    return walk(root, frozenset())


def _refuse_shared_entry(text: str, root: Any, node: Any) -> None:
    """Refuse a column whose entry, or an entry above it, is shared through a
    YAML alias or merge key (`<<`): anything written into it would land on
    every entry sharing it (#1496). The list a column holds is unshared
    instead, by ``_unshare_tests``; a shared entry above it is not."""

    if _paths(root, node) < 2:
        return
    collections: dict[int, Any] = {}
    stack = [root]
    while stack:
        current = stack.pop()
        if (
            isinstance(current, yaml.ScalarNode)
            or current.start_mark.index in collections
        ):
            continue
        collections[current.start_mark.index] = current
        if isinstance(current, yaml.MappingNode):
            stack.extend(n for pair in current.value for n in pair)
        else:
            stack.extend(current.value)
    names = sorted(
        f"'&{t.value}'"
        for t in yaml.scan(text)
        if isinstance(t, yaml.AnchorToken)
        and t.start_mark.index in collections
        and _paths(collections[t.start_mark.index], node) > 0
    )
    via = " or ".join(names) if names else "a YAML alias"
    raise RewriteError(
        f"the column's entry is shared with others through {via} (an alias or a "
        "merge key), so a test written into it would land on each of them; give "
        "the column its own entry"
    )


def _unshare_tests(text: str, key: Any, listed: Any) -> str:
    """``text`` with the column's aliased test list made its own (#1496).

    A list another column shares through a YAML alias is not spliced: a test
    added to it would land on every column that aliases it. The column is
    given its own copy instead, the way a flow-style entry is restyled rather
    than refused (#1203). Where it holds an alias, the alias is expanded into
    a flow-style list of the same tests. Where it holds the anchor, the anchor
    moves, with a flow-style copy of the tests, to the first alias of it, so
    every other column parses to the same tests as before.
    """

    tokens = [t for t in yaml.scan(text) if t.start_mark.index >= key.end_mark.index]
    if len(tokens) < 2 or not isinstance(tokens[0], yaml.ValueToken):
        raise RewriteError(f"'{key.value}' is a test list shared through a YAML alias")
    shared = tokens[1]
    copy = "[" + ", ".join(_flow(_node_value(item)) for item in listed.value) + "]"
    span = (shared.start_mark.index, shared.end_mark.index)
    if isinstance(shared, yaml.AliasToken):
        return splice(text, [(*span, copy)])
    if isinstance(shared, yaml.AnchorToken):
        first = next(
            (
                t
                for t in tokens[2:]
                if isinstance(t, (yaml.AliasToken, yaml.AnchorToken))
                and t.value == shared.value
            ),
            None,
        )
        if isinstance(first, yaml.AliasToken):
            mark = (first.start_mark.index, first.end_mark.index)
            start = len(text[: span[0]].rstrip(" "))
            return splice(
                text,
                [(start, span[1], ""), (*mark, f"&{shared.value} {copy}")],
            )
    raise RewriteError(
        f"'{key.value}' is a test list shared through a YAML alias; give the "
        "column its own list"
    )


def _claim_earlier_renders(text: str, locate: Any, specs: list[Any]) -> str:
    """``text`` with a claim id added to each test Dex rendered before claims.

    A listed test is left as written (#1348), with one exception: a test that is
    byte for byte what Dex renders for a spec, flag, arguments and all, but
    carries no claim, is Dex's own earlier render of the same fact, and gains
    that spec's ``config.meta.dex.claim`` and nothing else (#1448). A bare test,
    a test with other arguments or config, and a test that already carries any
    claim are someone's, or a fact since changed, and stay as written (#1350),
    and so does a test whose YAML is aliased elsewhere, which a claim written
    into it would reach through the alias.
    """

    for spec in specs:
        claim = claim_of(spec)
        if claim is None:
            continue
        unclaimed = without_claim(spec)
        root = _compose(text)
        listed = _child(locate(root), "data_tests") or _child(locate(root), "tests")
        if listed is None:
            return text
        index = next(
            (
                i
                for i, item in enumerate(_sequence(listed[1]))
                if isinstance(item, yaml.MappingNode) and _plain(item) == unclaimed
            ),
            None,
        )
        if index is None:
            continue
        item = listed[1].value[index]
        if _carries_anchor(root, listed[1]):
            # An aliased list or item would give this claim to another column or
            # test; the test stays as written rather than claim two facts.
            continue
        [(_name_node, body)] = item.value
        if listed[1].flow_style or item.flow_style:
            # The whole item is Dex's render on one span; restated with the claim.
            text = splice(text, [(*_span(item), _flow(spec))])
        elif isinstance(body, yaml.MappingNode) and body.flow_style:
            text = splice(text, [(*_span(body), _flow(next(iter(spec.values()))))])
        else:

            def body_of(r: Any, index: int = index) -> Any:
                tests = _child(locate(r), "data_tests") or _child(locate(r), "tests")
                return tests[1].value[index].value[0][1] if tests else None

            text = _set_meta(text, body_of, DEX_META_KEY, {CLAIM_KEY: claim})
    return text


def _node_value(node: Any) -> Any:
    return yaml.safe_load(yaml.serialize(node))


def _is_listed(spec: Any, listed: list[Any]) -> bool:
    """Whether a test list already holds ``spec``.

    A built-in is matched by name, so a test the column lists under that name is
    left exactly as written (#1348). A generated test (#1449) is matched by its
    claim, wherever it was renamed, or by the same test with the same arguments,
    which dbt would build into the same node: one kind can check two facts on
    one model (two population filters), so its name alone says nothing.
    """

    name = _spec_name(spec)
    if not gt.is_generated(spec):
        return any(_listed_name(test) == name for test in listed)
    claim = claim_of(spec)
    arguments = _arguments_of(spec)
    for test in listed:
        if claim is not None and claim_of(test) == claim:
            return True
        if _listed_name(test) == name and _arguments_of(test) == arguments:
            return True
    return False


def _listed_name(test: Any) -> str:
    if isinstance(test, str):
        return test
    if isinstance(test, Mapping) and len(test) == 1:
        return str(next(iter(test)))
    return ""


def _arguments_of(test: Any) -> dict[str, Any]:
    """A test's arguments, under ``arguments:`` or at the top of its body (the
    spelling before dbt 1.10), and nothing of its config, name or description."""

    if not isinstance(test, Mapping) or len(test) != 1:
        return {}
    body = next(iter(test.values()))
    if not isinstance(body, Mapping):
        return {}
    out = {
        key: value
        for key, value in body.items()
        if key not in ("arguments", "config", "name", "description", "meta", "tags")
    }
    nested = body.get("arguments")
    if isinstance(nested, Mapping):
        out.update(nested)
    return out


def _spec_name(spec: Any) -> str:
    return spec if isinstance(spec, str) else next(iter(spec))


# --- creating an entry ----------------------------------------------------------


def _default_schema_path(sql_path: str, model: str) -> str:
    """Where the scaffold would put ``model``'s YAML: beside its SQL, named for it."""

    directory = PurePosixPath(sql_path).parent if sql_path else PurePosixPath("models")
    return str(directory / f"{model}.yml")


def _with_entry(content: str | None, model: str) -> str:
    """``content`` holding an entry for ``model``, adding one only if it has none."""

    if content is None or not content.strip():
        return "version: 2\n\nmodels:\n" + _render_item({"name": model}, "  ")
    root = _compose(content)
    if _model_entry(root, model) is not None:
        return content
    models = _child(root, "models")
    if models is None or not _sequence(models[1]):
        if models is not None:
            raise RewriteError("its `models:` key holds no list to add an entry to")
        prefix = "" if content.endswith("\n") else "\n"
        return content + prefix + "models:\n" + _render_item({"name": model}, "  ")
    listed = models[1]
    if listed.flow_style:
        raise RewriteError("its `models:` list is written in flow style")
    first = listed.value[0]
    line_start = content.rfind("\n", 0, first.start_mark.index) + 1
    line = content[line_start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    from .rewrite import _entry_span

    end = max(_entry_span(content, item)[1] for item in listed.value)
    at = before_trailing_blanks(content, end)
    prefix = "" if at == 0 or content[at - 1] == "\n" else "\n"
    return splice(
        content, [(at, at, prefix + _render_item({"name": model}, dash_indent))]
    )


def _ensure_column(text: str, model: str, column: str) -> str:
    """``text`` with a column entry for ``column`` under ``model``."""

    if _column_locator(model, column)(_compose(text)) is not None:
        return text
    anchored = column_anchor(text, model)
    if anchored is not None:
        at, indent = anchored
        bullet = indent[: len(indent) - len(indent.lstrip(" "))]
        return splice(text, [(at, at, _render_item({"name": column}, bullet))])
    return _set_key(
        text, lambda root: _model_entry(root, model), "columns", [{"name": column}]
    )


# --- YAML text ------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Block YAML with sequences indented under their key, the way dbt docs read."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _dump(value: Any, *, flow: bool = False) -> str:
    return yaml.dump(
        value,
        Dumper=_Dumper,
        default_flow_style=flow,
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    )


def _render_pair(key: str, value: Any, indent: str) -> str:
    return "".join(f"{indent}{line}\n" for line in _dump({key: value}).splitlines())


def _render_item(value: Any, dash_indent: str) -> str:
    return "".join(f"{dash_indent}{line}\n" for line in _dump([value]).splitlines())


def _flow(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    return _dump(spec, flow=True).strip()


# --- reading decisions back -----------------------------------------------------


def _carries_declaration(files: Mapping[str, str], model: str) -> bool:
    return any(
        isinstance(entry_meta(entry).get(DEX_META_KEY), dict)
        for _label, entry in _entries(files, {model})
    )


def _entries(
    files: Mapping[str, str], models: set[str]
) -> Iterable[tuple[str, dict[str, Any]]]:
    """``(name, entry)`` for each model entry ``models`` names.

    A version is read as dbt builds it (its effective entry, under its label),
    so a name a version answers to (the label, the bare versioned name, or the
    version's file stem) yields what that version declares (#895).
    """

    wanted = mv.expand(models, mv.cached_version_map(files))
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict):
                continue
            for label, variant in mv.entry_variants(entry):
                if label in wanted:
                    yield label, variant


def decisions(files: Mapping[str, str], models: Iterable[str]) -> list[dict[str, Any]]:
    """Every assumption declared on ``models``, as the project's YAML records it.

    Read from ``config.meta.dex.assumptions`` rather than from any payload, so a
    plan, the apply that writes it and a later build all report the same list.
    """

    out: list[dict[str, Any]] = []
    for label, entry in _entries(files, set(models)):
        dex = entry_meta(entry).get(DEX_META_KEY)
        if not isinstance(dex, dict):
            continue
        for assumption in dex.get("assumptions") or []:
            if not isinstance(assumption, dict):
                continue
            out.append(
                {
                    "model": label,
                    "decision": assumption.get("decision"),
                    "chosen": assumption.get("chosen"),
                    "evidence": assumption.get("evidence"),
                    **(
                        {"column": assumption["column"]}
                        if isinstance(assumption.get("column"), str)
                        else {}
                    ),
                    **(
                        {"detail": assumption["detail"]}
                        if isinstance(assumption.get("detail"), str)
                        and assumption["detail"].strip()
                        else {}
                    ),
                }
            )
    return out


def touched_models(edits: Iterable[PlanEdit], declared: Iterable[str] = ()) -> set[str]:
    """The models a set of edits writes SQL or YAML for, plus any declared."""

    models = set(declared)
    for edit in edits:
        if edit.op is not EditOp.UPSERT or edit.new_content is None:
            continue
        if edit.kind is EditKind.MODEL_SQL and edit.path.endswith(".sql"):
            models.add(PurePosixPath(edit.path).stem)
        elif edit.kind in (EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML):
            for block in yaml_blocks(edit.new_content):
                if block.form == "yaml_model_entry":
                    models.add(block.name)
    return models
