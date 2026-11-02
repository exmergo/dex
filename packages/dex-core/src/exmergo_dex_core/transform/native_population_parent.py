"""Refuse a ``population.parent`` the native authoring route cannot resolve (#1030).

The dbt route checks a parent against the project in ``transform.declarations``.
``semantic ossie|slayer|cube`` only ran ``parse_declarations``, so a parent that
named nothing was written into a reviewed document. This module is the one check
the native route runs for every vendor: a parent must name the declared model's
own columns, and either a model the layer (or the same payload) defines or a
dbt model/seed/snapshot the project can resolve.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

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
    """

    if engine.repo_root is None:
        return False
    try:
        from ..dbt_project import DbtProjectError, load
        from .declarations import _is_seed_or_snapshot, model_sql_path
        from .plans import select_columns
    except ImportError:
        return False

    try:
        project_dir = engine.project_dir()
        view = load(project_dir)
    except DbtProjectError:
        return False

    files = {path: source.content for path, source in view.files.items()}
    parent_sql = model_sql_path(files, view.model_paths, parent_model)
    if parent_sql is not None:
        produced = select_columns(files[parent_sql])
        if produced is None:
            return True
        return all(c.lower() in produced for c in parent_columns)
    return _is_seed_or_snapshot(files, view, parent_model)
