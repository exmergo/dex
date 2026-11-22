"""The two edits ``dex init`` installs the Elementary package with (#1464)."""

from __future__ import annotations

import pytest
import yaml

from exmergo_dex_core.transform import elementary_install as install
from exmergo_dex_core.transform.package_repair import set_nested

from .elementary_fixture import CURRENT_VERSION


def test_the_pinned_release_is_the_newest_the_fixture_runs():
    assert install.INSTALL_RELEASE == CURRENT_VERSION
    major, minor, _ = (int(part) for part in install.INSTALL_RELEASE.split("."))
    # 0.20.0 is the first release that honours the per-test sample flag.
    assert (major, minor) >= (0, 20)


def test_a_project_with_no_packages_file_gets_one():
    packages, _ = install.install_texts(None, "packages.yml", "name: a\n", "dev")
    assert yaml.safe_load(packages) == {
        "packages": [{"package": install.HUB_PACKAGE, "version": CURRENT_VERSION}]
    }


def test_an_existing_packages_list_keeps_every_comment_and_entry():
    text = (
        "# the project's packages\n"
        "packages:\n"
        "  - package: dbt-labs/dbt_utils\n"
        "    version: 1.3.0  # pinned on purpose\n"
        "\n"
        "# trailing note\n"
    )
    packages, _ = install.install_texts(text, "packages.yml", "name: a\n", "dev")
    assert packages == (
        "# the project's packages\n"
        "packages:\n"
        "  - package: dbt-labs/dbt_utils\n"
        "    version: 1.3.0  # pinned on purpose\n"
        f"  - package: {install.HUB_PACKAGE}\n"
        f"    version: {CURRENT_VERSION}\n"
        "\n"
        "# trailing note\n"
    )


def test_an_indentless_packages_list_stays_indentless():
    text = "packages:\n- package: dbt-labs/dbt_utils\n  version: 1.3.0\n"
    packages, _ = install.install_texts(text, "packages.yml", "name: a\n", "dev")
    assert packages.endswith(
        f"- package: {install.HUB_PACKAGE}\n  version: {CURRENT_VERSION}\n"
    )
    assert len(yaml.safe_load(packages)["packages"]) == 2


def test_a_dependencies_file_without_packages_gains_the_key():
    text = "projects:\n  - name: upstream\n"
    packages, _ = install.install_texts(text, "dependencies.yml", "name: a\n", "dev")
    parsed = yaml.safe_load(packages)
    assert parsed["projects"] == [{"name": "upstream"}]
    assert parsed["packages"][0]["package"] == install.HUB_PACKAGE


def test_a_flow_style_packages_list_is_refused_with_the_manual_edit():
    with pytest.raises(install.InstallRefusedError, match="by hand"):
        install.install_texts(
            "packages: [{package: dbt-labs/dbt_utils, version: 1.3.0}]\n",
            "packages.yml",
            "name: a\n",
            "dev",
        )


def test_dbt_project_disables_the_package_on_the_dev_target_only():
    text = (
        "name: a\n"
        "# models are views by default\n"
        "models:\n"
        "  a:\n"
        "    +materialized: view\n"
        "vars:\n"
        "  start_date: '2024-01-01'  # backfill floor\n"
    )
    _, project = install.install_texts(None, "packages.yml", text, "development")
    assert "# models are views by default" in project
    assert "# backfill floor" in project
    parsed = yaml.safe_load(project)
    assert parsed["models"]["a"] == {"+materialized": "view"}
    assert parsed["models"]["elementary"] == {
        "+schema": "elementary",
        "+enabled": "{{ target.name != 'development' }}",
    }
    assert parsed["vars"] == {"start_date": "2024-01-01", "test_sample_row_count": 0}


def test_the_dev_target_name_is_quoted_for_jinja():
    assert install.enabled_expression("it's") == "{{ target.name != 'it\\'s' }}"


def test_the_install_is_idempotent():
    text = "name: a\nmodel-paths:\n- models\n"
    _, once = install.install_texts(None, "packages.yml", text, "dev")
    assert install.project_yml(once, "dev") == once


def test_set_nested_appends_after_a_trailing_indentless_sequence():
    """``yaml.safe_dump`` writes a list at its key's column, and an insertion
    after it has to land after the whole list, not between the key and it."""

    text = "name: a\nmodel-paths:\n- models\n- more\n"
    out = set_nested(text, ["vars", "x"], 0)
    assert yaml.safe_load(out) == {
        "name": "a",
        "model-paths": ["models", "more"],
        "vars": {"x": 0},
    }


def _project(tmp_path, packages: str | None = None, installed: str | None = None):
    (tmp_path / "dbt_project.yml").write_text("name: a\n")
    if packages is not None:
        (tmp_path / "packages.yml").write_text(packages)
    if installed is not None:
        package = tmp_path / "dbt_packages" / "elementary"
        package.mkdir(parents=True)
        (package / "dbt_project.yml").write_text(
            f"name: elementary\nversion: {installed}\n"
        )
    return tmp_path


def test_detect_reads_an_absent_package(tmp_path):
    report = install.detect(_project(tmp_path))
    assert report == {
        "declared": False,
        "declared_in": None,
        "declared_version": None,
        "source": None,
        "installed": False,
        "release": None,
    }


def test_detect_reads_a_declared_and_installed_release(tmp_path):
    report = install.detect(
        _project(
            tmp_path,
            "packages:\n  - package: elementary-data/elementary\n    version: 0.16.4\n",
            installed="0.16.4",
        )
    )
    assert report["declared"] and report["installed"]
    assert report["declared_in"] == "packages.yml"
    assert report["source"] == "hub"
    assert report["declared_version"] == "0.16.4"
    assert report["release"] == "0.16.4"


def test_detect_reads_a_git_declaration_not_yet_installed(tmp_path):
    report = install.detect(
        _project(
            tmp_path,
            "packages:\n"
            "  - git: https://github.com/elementary-data/dbt-data-reliability.git\n"
            "    revision: 0.26.1\n",
        )
    )
    assert report["declared"] and report["source"] == "git"
    assert not report["installed"]


def test_install_edits_are_exactly_the_two_files(tmp_path):
    edits = install.install_edits(_project(tmp_path), "dev")
    assert [(e.path, e.kind.value) for e in edits] == [
        ("packages.yml", "packages_yml"),
        ("dbt_project.yml", "project_yml"),
    ]


def test_a_comment_shallower_than_an_entry_does_not_split_it():
    text = (
        "packages:\n"
        "  - package: dbt-labs/dbt_utils\n"
        "# Keep this version pinned.\n"
        "    version: 1.3.0\n"
    )
    packages, _ = install.install_texts(text, "packages.yml", "name: a\n", "dev")
    assert yaml.safe_load(packages)["packages"] == [
        {"package": "dbt-labs/dbt_utils", "version": "1.3.0"},
        {"package": install.HUB_PACKAGE, "version": CURRENT_VERSION},
    ]


def test_set_nested_steps_over_a_comment_inside_an_indentless_sequence():
    text = "name: a\nmodel-paths:\n- models\n# Additional model directory.\n- more\n"
    out = set_nested(text, ["vars", "x"], 0)
    assert yaml.safe_load(out)["model-paths"] == ["models", "more"]
    assert yaml.safe_load(out)["vars"] == {"x": 0}


def test_detect_reads_a_local_declaration_of_the_package(tmp_path):
    report = install.detect(
        _project(
            tmp_path,
            "packages:\n  - local: ../vendor/elementary\n",
            installed="0.26.1",
        )
    )
    assert report["declared"] and report["source"] == "local"
