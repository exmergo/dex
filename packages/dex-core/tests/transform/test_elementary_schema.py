"""The schema the Elementary package writes in production, worked out without
running dbt, and the allowlist entry that reaches it (#1473).

Each guarantee is a pair: a case built to break it (a naming, a value or a
missing setting dex cannot read) that must come back unresolved, and a case
built to hold it that must name the schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.config import (
    BigQueryTarget,
    DatabricksTarget,
    DexConfig,
    PostgresTarget,
    SnowflakeTarget,
)
from exmergo_dex_core.transform import elementary_schema as schema
from exmergo_dex_core.transform.elementary_schema import Located, Resolved, Unresolved

LAYERED = (
    Path(schema.__file__).parent / "assets" / "macros" / "generate_schema_name.sql"
)


def _project(
    root: Path,
    *,
    models: dict | None = None,
    macro: str | None = None,
    package_models: dict | None = None,
    profile: dict | None = None,
) -> Path:
    project = {"name": "shop", "profile": "shop"}
    if models is not None:
        project["models"] = models
    (root / "dbt_project.yml").write_text(yaml.safe_dump(project))
    if macro is not None:
        (root / "macros").mkdir(exist_ok=True)
        (root / "macros" / "generate_schema_name.sql").write_text(macro)
    if package_models is not None:
        package = root / "dbt_packages" / "elementary"
        package.mkdir(parents=True)
        (package / "dbt_project.yml").write_text(
            yaml.safe_dump({"name": "elementary", "models": package_models})
        )
    if profile is not None:
        (root / "profiles.yml").write_text(
            yaml.safe_dump({"shop": {"target": "dev", "outputs": profile}})
        )
    return root


def _bq(prod: str | None = "analytics", **extra) -> DexConfig:
    return DexConfig(
        connector="bigquery",
        bigquery=BigQueryTarget(
            project="acme", datasets=["raw"], dev="dbt_dev", prod=prod
        ),
        **extra,
    )


ELEMENTARY = {"elementary": {"+schema": "elementary"}}


# --- naming ---------------------------------------------------------------------


def test_default_naming_appends_the_custom_schema_to_production(tmp_path):
    found = schema.production_namespace(
        _project(tmp_path, models=ELEMENTARY), _bq(), "bigquery"
    )
    assert found == Resolved(Located("acme", "analytics_elementary"), "prod")


def test_no_custom_schema_builds_in_production_itself(tmp_path):
    found = schema.production_namespace(_project(tmp_path), _bq(), "bigquery")
    assert found == Resolved(Located("acme", "analytics"), "prod")


def test_the_layered_override_names_the_schema_after_the_prod_target(tmp_path):
    root = _project(tmp_path, models=ELEMENTARY, macro=LAYERED.read_text())
    found = schema.production_namespace(root, _bq(dbt_prod_target="prod"), "bigquery")
    assert found == Resolved(Located("acme", "elementary_prod"), "prod")


def test_the_layered_override_without_a_prod_target_asks_for_one(tmp_path):
    root = _project(tmp_path, models=ELEMENTARY, macro=LAYERED.read_text())
    found = schema.production_namespace(root, _bq(), "bigquery")
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.PROD_TARGET_UNSET
    assert found.ask is not None and found.ask.startswith(
        "dex config set dbt_prod_target"
    )


def test_a_project_generate_schema_name_is_not_run(tmp_path):
    macro = (
        "{% macro generate_schema_name(custom_schema_name, node) %}"
        "{{ custom_schema_name or target.schema }}{% endmacro %}"
    )
    root = _project(tmp_path, models=ELEMENTARY, macro=macro)
    found = schema.production_namespace(root, _bq(), "bigquery")
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.CUSTOM_SCHEMA_NAMING
    # A template for the user to fill in, keeping what is committed.
    assert found.ask is not None
    assert "bigquery.datasets '[raw, <the schema" in found.ask
    assert "--comment" in found.ask


# --- the configuration dex reads -------------------------------------------------


@pytest.mark.parametrize(
    "models",
    [
        {
            "elementary": {
                "+schema": "{{ 'elementary' if target.name == 'prod' else 'x' }}"
            }
        },
        {"elementary": {"+schema": "elementary", "+database": "{{ env_var('DB') }}"}},
    ],
)
def test_jinja_in_the_package_config_is_not_rendered(tmp_path, models):
    found = schema.production_namespace(
        _project(tmp_path, models=models), _bq(), "bigquery"
    )
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.TEMPLATED_CONFIG


def test_a_folder_setting_its_own_schema_is_not_guessed(tmp_path):
    models = {"elementary": {"+schema": "elementary", "edr": {"+schema": "other"}}}
    found = schema.production_namespace(
        _project(tmp_path, models=models), _bq(), "bigquery"
    )
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.NESTED_SCHEMA_CONFIG


def test_the_package_block_wins_over_a_root_level_schema(tmp_path):
    root = _project(tmp_path, models={"+schema": "everything", **ELEMENTARY})
    found = schema.production_namespace(root, _bq(), "bigquery")
    assert found == Resolved(Located("acme", "analytics_elementary"), "prod")


def test_a_root_level_schema_applies_to_the_package(tmp_path):
    root = _project(tmp_path, models={"+schema": "everything"})
    found = schema.production_namespace(root, _bq(), "bigquery")
    assert found == Resolved(Located("acme", "analytics_everything"), "prod")


def test_the_package_own_project_file_is_read_last(tmp_path):
    root = _project(tmp_path, package_models={"elementary": {"+schema": "edr"}})
    found = schema.production_namespace(root, _bq(), "bigquery")
    assert found == Resolved(Located("acme", "analytics_edr"), "prod")


def test_a_planned_install_supplies_its_schema(tmp_path):
    found = schema.production_namespace(
        _project(tmp_path), _bq(), "bigquery", planned_schema="elementary"
    )
    assert found == Resolved(Located("acme", "analytics_elementary"), "prod")


def test_a_literal_database_moves_the_package(tmp_path):
    models = {"elementary": {"+schema": "elementary", "+database": "observability"}}
    found = schema.production_namespace(
        _project(tmp_path, models=models), _bq(), "bigquery"
    )
    assert found == Resolved(Located("observability", "analytics_elementary"), "prod")


# --- production's own schema ---------------------------------------------------------


def test_prod_unset_with_a_dev_namespace_is_not_derived_from_dev(tmp_path):
    """The dev schema is never a guess at production's."""

    root = _project(tmp_path, models=ELEMENTARY)
    found = schema.production_namespace(root, _bq(prod=None), "bigquery")
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.PROD_UNSET
    assert found.ask == "dex config set bigquery.prod '<dataset or project.dataset>'"
    assert "dbt_dev" not in found.detail


def test_the_profile_output_for_the_prod_target_is_read(tmp_path):
    profile = {
        "dev": {"type": "bigquery", "project": "acme", "dataset": "dbt_dev"},
        "prod": {"type": "bigquery", "project": "acme-prod", "dataset": "analytics"},
    }
    root = _project(tmp_path, models=ELEMENTARY, profile=profile)
    found = schema.production_namespace(
        root, _bq(prod=None, dbt_prod_target="prod"), "bigquery"
    )
    assert found == Resolved(Located("acme-prod", "analytics_elementary"), "profile")


def test_a_templated_profile_output_is_not_read(tmp_path):
    profile = {
        "dev": {"type": "bigquery", "project": "acme", "dataset": "dbt_dev"},
        "prod": {
            "type": "bigquery",
            "project": "acme",
            "dataset": "{{ env_var('PROD_DATASET') }}",
        },
    }
    root = _project(tmp_path, models=ELEMENTARY, profile=profile)
    found = schema.production_namespace(
        root, _bq(prod=None, dbt_prod_target="prod"), "bigquery"
    )
    assert isinstance(found, Unresolved)
    assert found.reason_code == schema.PROD_UNSET


def test_committed_prod_wins_over_the_profile(tmp_path):
    profile = {"prod": {"type": "bigquery", "project": "acme", "dataset": "other"}}
    root = _project(tmp_path, models=ELEMENTARY, profile=profile)
    found = schema.production_namespace(root, _bq(dbt_prod_target="prod"), "bigquery")
    assert found == Resolved(Located("acme", "analytics_elementary"), "prod")


# --- spelling, per connector ----------------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "config", "expected"),
    [
        (
            "snowflake",
            DexConfig(
                connector="snowflake",
                snowflake=SnowflakeTarget(databases=["RAW"], prod="ANALYTICS.PROD"),
            ),
            ["ANALYTICS.PROD_elementary", "PROD_elementary"],
        ),
        (
            "databricks",
            DexConfig(
                connector="databricks",
                databricks=DatabricksTarget(catalogs=["raw"], prod="main.prod"),
            ),
            ["main.prod_elementary"],
        ),
        (
            "postgres",
            DexConfig(
                connector="postgres",
                postgres=PostgresTarget(schemas=["raw"], prod="analytics"),
            ),
            ["analytics_elementary"],
        ),
        (
            "bigquery",
            _bq(),
            ["acme.analytics_elementary", "analytics_elementary"],
        ),
    ],
)
def test_each_connector_spells_the_entry_its_allowlist_takes(
    tmp_path, connector, config, expected
):
    found = schema.production_namespace(
        _project(tmp_path, models=ELEMENTARY), config, connector
    )
    assert isinstance(found, Resolved)
    assert schema.entries(connector, found.located) == expected


def test_the_command_keeps_what_is_committed_and_carries_the_comment():
    command = schema.allowlist_command("bigquery", ["raw"], "acme.analytics_elementary")
    assert command == (
        "dex config set bigquery.datasets '[raw, acme.analytics_elementary]' "
        f"--comment '{schema.COMMENT}'"
    )
    # Single-quoted in the shell, so the comment holds no single quote.
    assert "'" not in schema.COMMENT
