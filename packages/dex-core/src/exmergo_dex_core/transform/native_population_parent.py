"""Refuse a ``population.parent`` the native authoring route cannot resolve (#1030).

The dbt route checks a parent against the project in ``transform.declarations``.
``semantic ossie|slayer|cube`` only ran ``parse_declarations``, so a parent that
named nothing was written into a reviewed document. This module is the one check
the native route runs for every vendor: a parent must name the declared model's
own columns, and either a model the layer (or the same payload) defines or a
dbt model/seed/snapshot the project, or a package it installs, can resolve.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from ..engine import DexEngine
    from ..semantic_declarations import Declaration


def refuse_population_parents(
    declarations: list[Any],
    inventory: Mapping[str, set[str]],
    engine: DexEngine,
) -> None:
    """Raise ``ValueError`` naming the fix when any declaration's parent is bad.

    ``inventory`` maps lowercased model/dataset/cube names to the lowercased
    column (or field/member) names each exposes in the prospective documents.
    """

    for declaration in declarations:
        population = declaration.population
        if population is None or population.parent is None:
            continue
        reason = _refuse_one(declaration, population.parent, inventory, engine)
        if reason is not None:
            raise ValueError(reason)


def _refuse_one(
    declaration: Declaration,
    parent: Any,
    inventory: Mapping[str, set[str]],
    engine: DexEngine,
) -> str | None:
    model = declaration.model
    if parent.model.lower() == model.lower():
        return (
            f"'{model}' declares itself as its population.parent; name the model "
            "the layer defines (or a dbt model, seed or snapshot) its rows are "
            "drawn from"
        )

    produced = inventory.get(model.lower())
    if produced is not None:
        unknown = [c for c in parent.key if c.lower() not in produced]
        if unknown:
            return (
                f"'{model}' declares population.parent key column(s) "
                f"{', '.join(unknown)} that the layer model does not expose; the "
                "key maps output columns to the parent's"
            )

    parent_cols = inventory.get(parent.model.lower())
    if parent_cols is not None:
        missing = [c for c in parent.key.values() if c.lower() not in parent_cols]
        if missing:
            return (
                f"'{model}' declares population.parent '{parent.model}' keyed to "
                f"column(s) {', '.join(missing)}, and that layer model does not "
                "expose them"
            )
        return None

    if _dbt_parent_resolvable(engine, parent.model, list(parent.key.values())):
        return None

    return (
        f"'{model}' declares population.parent '{parent.model}', and the layer "
        "defines no dataset, SLayer model or cube by that name (including ones "
        "this payload adds); name a parent the layer or the dbt project can "
        "resolve"
    )


def _dbt_parent_resolvable(
    engine: DexEngine, parent_model: str, parent_columns: list[str]
) -> bool:
    """True when the repository's dbt project can resolve the parent columns.

    Native-only installs and repositories without a dbt project return False
    without importing the dbt authoring surface beyond a cheap probe.

    Parent *existence* (model SQL / seed / snapshot) must not depend on the
    ``sql`` extra: ``transform.declarations`` imports the rewriter and sqlglot
    at module import, so an ``[ossie]``-only install would otherwise treat every
    real dbt parent as unresolved. Column parsing still needs sqlglot; when that
    surface is absent, an existing parent is accepted the same way an
    unparseable SELECT is.
    """

    if engine.repo_root is None:
        return False
    try:
        from ..dbt_project import DbtProjectError, load
    except ImportError:
        return False

    try:
        project_dir = engine.project_dir()
        view = load(project_dir)
    except DbtProjectError:
        return False

    files = {path: source.content for path, source in view.files.items()}
    parent_sql = _model_sql_path(files, view.model_paths, parent_model)
    if parent_sql is not None:
        produced = _select_columns_if_available(files[parent_sql])
        if produced is None:
            return True
        return all(c.lower() in produced for c in parent_columns)
    if _is_seed_or_snapshot(files, view, parent_model):
        return True
    # An installed package's model, seed or snapshot, when the project has none
    # of that name (#1155). Columns that cannot be read are accepted, as an
    # unparseable project SELECT is.
    from .package_targets import find_package_target

    packaged = find_package_target(
        view.root,
        parent_model,
        project_yml=files.get("dbt_project.yml"),
        manifest=view.manifest,
    )
    if packaged is None or not packaged.enabled:
        return False
    if packaged.columns is None:
        return True
    return all(c.lower() in packaged.columns for c in parent_columns)


def _model_sql_path(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> str | None:
    """The ``.sql`` file that defines ``model``, if the project has one.

    Local copy of ``transform.declarations.model_sql_path`` so this module never
    imports the rewriter (and sqlglot) just to resolve a parent name.
    """

    roots = [PurePosixPath(p) for p in model_paths]
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix != ".sql" or candidate.stem != model:
            continue
        if any(root == candidate or root in candidate.parents for root in roots):
            return path
    return None


def _is_seed_or_snapshot(files: Mapping[str, str], view: Any, name: str) -> bool:
    """True when ``name`` is a seed CSV or a snapshot the project defines."""

    seed_roots = [PurePosixPath(p) for p in view.seed_paths]
    for path in files:
        candidate = PurePosixPath(path)
        if (
            candidate.suffix == ".csv"
            and candidate.stem == name
            and any(root in candidate.parents for root in seed_roots)
        ):
            return True

    roots = [PurePosixPath(p) for p in view.snapshot_paths]
    block = re.compile(rf"{{%-?\s*snapshot\s+{re.escape(name)}\s*-?%}}")
    for path, content in files.items():
        candidate = PurePosixPath(path)
        if candidate.suffix == ".sql":
            if any(r in candidate.parents for r in roots) and block.search(content):
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


def _select_columns_if_available(sql: str) -> set[str] | None:
    """Outer SELECT column names, or ``None`` when sqlglot/parsing is unavailable."""

    try:
        from .plans import select_columns
    except ImportError:
        return None
    try:
        return select_columns(sql)
    except ImportError:
        return None
