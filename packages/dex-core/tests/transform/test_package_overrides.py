"""Which project models override a package model the package documents (#1161).

Read from files alone: no dbt, no connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.transform.declarations import (
    DeclarationError,
    refuse_package_documented_entries,
)
from exmergo_dex_core.transform.package_overrides import (
    documented_overrides,
    package_models,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

DISABLED = "models:\n  tiny_pkg:\n    pkg_task:\n      +enabled: false\n"
ENTRY = "version: 2\nmodels:\n  - name: pkg_task\n    description: From the vendor.\n"


def _project(
    root: Path,
    *,
    project_models: str = "",
    package_yml: str | None = ENTRY,
    install: str = "dbt_packages",
    package_paths: str = "[models]",
    folder: str = "",
) -> Path:
    """A project and an installed package that both define ``pkg_task``."""

    root.mkdir(parents=True, exist_ok=True)
    install_line = (
        f"packages-install-path: {install}\n" if install != "dbt_packages" else ""
    )
    (root / "dbt_project.yml").write_text(
        "name: proj\nversion: '1.0'\nconfig-version: 2\nprofile: proj\n"
        f"model-paths: [models]\n{install_line}{project_models}"
    )
    (root / "models").mkdir(exist_ok=True)
    (root / "models" / "pkg_task.sql").write_text("select 1 as id\n")
    package = root / install / "tiny_pkg"
    models = package / "models" / folder if folder else package / "models"
    models.mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        f"name: tiny_pkg\nversion: '1.0'\nmodel-paths: {package_paths}\n"
    )
    (models / "pkg_task.sql").write_text("select 1 as id\n")
    if package_yml is not None:
        (models / "pkg_task.yml").write_text(package_yml)
    return root


def _yml(root: Path) -> str:
    """The project's ``dbt_project.yml`` as it stands."""

    return (root / "dbt_project.yml").read_text()


def test_a_disabled_documented_package_model_is_a_documented_override(tmp_path):
    """Disabled and documented: an override that takes the package's entry."""

    root = _project(tmp_path / "p", project_models=DISABLED)
    [found] = documented_overrides(root, {"pkg_task"}, _yml(root)).values()
    assert (found.model, found.package) == ("pkg_task", "tiny_pkg")
    assert found.package_entry == "dbt_packages/tiny_pkg/models/pkg_task.yml"


def test_a_package_model_left_enabled_is_not_one(tmp_path):
    """An enabled package model keeps its own entry, so nothing is overridden."""

    root = _project(tmp_path / "p")
    assert documented_overrides(root, {"pkg_task"}, _yml(root)) == {}


def test_an_undocumented_package_model_is_not_one(tmp_path):
    """A package that documents nothing leaves the override free to declare."""

    root = _project(tmp_path / "p", project_models=DISABLED, package_yml=None)
    assert documented_overrides(root, {"pkg_task"}, _yml(root)) == {}


def test_a_folder_level_disable_counts(tmp_path):
    """dbt reads ``enabled`` at every level, a folder included."""

    folder_disabled = "models:\n  tiny_pkg:\n    staging:\n      +enabled: false\n"
    root = _project(tmp_path / "p", project_models=folder_disabled, folder="staging")
    [found] = documented_overrides(root, {"pkg_task"}, _yml(root)).values()
    assert found.package_entry == "dbt_packages/tiny_pkg/models/staging/pkg_task.yml"


def test_a_custom_install_path_and_model_path_are_followed(tmp_path):
    """``packages-install-path`` and a package's ``model-paths`` are honoured."""

    root = _project(
        tmp_path / "p",
        project_models=DISABLED,
        install="vendor",
        package_paths="[src]",
    )
    package = root / "vendor" / "tiny_pkg"
    (package / "models").rename(package / "src")
    [found] = documented_overrides(root, {"pkg_task"}, _yml(root)).values()
    assert found.package_entry == "vendor/tiny_pkg/src/pkg_task.yml"


@pytest.mark.parametrize(
    "package_yml",
    [
        "models: [unclosed\n",
        "version: 2\nmodels:\n  - name: pkg_task\n    versions:\n      - v: 1\n",
        "version: 2\nmodels: not-a-list\n",
    ],
)
def test_an_unreadable_or_versioned_package_entry_is_not_one(tmp_path, package_yml):
    """Unreadable YAML and a versioned entry claim nothing."""

    root = _project(tmp_path / "p", project_models=DISABLED, package_yml=package_yml)
    assert documented_overrides(root, {"pkg_task"}, _yml(root)) == {}


def test_the_plan_s_dbt_project_decides_what_is_disabled(tmp_path):
    """The disable a plan writes is what is judged, not the one on disk."""

    root = _project(tmp_path / "p")
    after = _yml(root) + DISABLED
    assert "pkg_task" in documented_overrides(root, {"pkg_task"}, after)
    [model] = package_models(root, {"pkg_task"}, after)
    assert model.enabled is False
    [model] = package_models(root, {"pkg_task"}, _yml(root))
    assert model.enabled is True


def _entry_edit(path: str, name: str = "pkg_task") -> PlanEdit:
    """A ``schema_yml`` edit holding one entry for ``name``."""

    return PlanEdit(
        path=path,
        kind=EditKind.SCHEMA_YML,
        new_content=f"version: 2\nmodels:\n  - name: {name}\n",
    )


def test_a_plan_writing_an_entry_for_a_documented_override_is_refused(tmp_path):
    """A new project entry is refused, naming both files."""

    root = _project(tmp_path / "p", project_models=DISABLED)
    view = load_project(root)
    with pytest.raises(DeclarationError) as raised:
        refuse_package_documented_entries(view, [_entry_edit("models/mine.yml")])
    message = str(raised.value)
    assert "dbt_packages/tiny_pkg/models/pkg_task.yml" in message
    assert "`models/mine.yml`" in message


def test_a_plan_elsewhere_is_not_refused_for_an_existing_entry(tmp_path):
    """Only names the plan touches are judged."""

    root = _project(tmp_path / "p", project_models=DISABLED)
    (root / "models" / "mine.yml").write_text(
        "version: 2\nmodels:\n  - name: pkg_task\n"
    )
    (root / "models" / "other.sql").write_text("select 1 as id\n")
    view = load_project(root)
    other = PlanEdit(
        path="models/other.sql", kind=EditKind.MODEL_SQL, new_content="select 2 as id\n"
    )
    refuse_package_documented_entries(view, [other])
    # Touching the override's SQL, or the project config, is judged.
    touched = PlanEdit(
        path="models/pkg_task.sql", kind=EditKind.MODEL_SQL, new_content="select 2\n"
    )
    with pytest.raises(DeclarationError):
        refuse_package_documented_entries(view, [touched])


def test_disabling_the_package_model_under_an_existing_entry_is_refused(tmp_path):
    """Disabling the package model makes an existing entry a duplicate."""

    root = _project(tmp_path / "p")
    (root / "models" / "mine.yml").write_text(
        "version: 2\nmodels:\n  - name: pkg_task\n"
    )
    view = load_project(root)
    disable = PlanEdit(
        path="dbt_project.yml",
        kind=EditKind.PROJECT_YML,
        new_content=_yml(root) + DISABLED,
    )
    with pytest.raises(DeclarationError, match=r"models/mine\.yml"):
        refuse_package_documented_entries(view, [disable])


def test_an_existing_python_override_with_an_entry_is_refused(tmp_path):
    """A Python override is not on dex's editable surface, but it is one."""

    root = _project(tmp_path / "p", project_models=DISABLED)
    (root / "models" / "pkg_task.sql").unlink()
    (root / "models" / "pkg_task.py").write_text("def model(dbt, session):\n    pass\n")
    view = load_project(root)
    with pytest.raises(DeclarationError, match=r"models/mine\.yml"):
        refuse_package_documented_entries(view, [_entry_edit("models/mine.yml")])
