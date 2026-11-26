"""`semantic.vendor: databricks` configuration (#1257)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig


def _config(semantic: dict) -> DexConfig:
    """A config with this `semantic` block, validated."""
    return DexConfig.model_validate({"connector": "duckdb", "semantic": semantic})


def test_the_vendor_reads_its_file_list():
    """With `local` as its default deployment."""
    config = _config(
        {"vendor": "databricks", "databricks": {"files": ["views/orders.yaml"]}}
    )

    assert config.semantic.vendor == "databricks"
    assert config.semantic.deployment == "local"
    assert config.semantic.databricks.files == ["views/orders.yaml"]


@pytest.mark.parametrize(
    ("files", "said"),
    [
        (["views/orders.sql"], "CREATE VIEW ... WITH METRICS"),
        (["views/orders.json"], "not a Metric View YAML file"),
        (["views/*.yaml"], "is a pattern"),
        (["/abs/orders.yaml"], "inside the repository"),
        (["../orders.yaml"], "inside the repository"),
        (["views/a.yaml", "views/a.yaml"], "the same file twice"),
        (["views/orders.yaml", "other/Orders.yml"], "one Metric View twice"),
    ],
)
def test_a_listing_dex_will_not_read_is_refused_by_name(files, said):
    """Each refusal names the setting and the reason."""
    with pytest.raises(ValueError, match=r"semantic\.databricks\.files") as caught:
        _config({"vendor": "databricks", "databricks": {"files": files}})

    assert said in str(caught.value)


def test_the_vendor_needs_its_listing():
    """A vendor with no files names the missing key."""
    with pytest.raises(ValueError, match=r"needs semantic\.databricks\.files"):
        _config({"vendor": "databricks"})


def test_the_section_is_refused_under_another_vendor():
    """Coordinates that would be ignored are refused instead."""
    with pytest.raises(
        ValueError, match=r"only valid for semantic\.vendor: databricks"
    ):
        _config({"vendor": "dbt", "databricks": {"files": ["views/orders.yaml"]}})


def test_there_is_no_hosted_deployment():
    """The files in this repository and Unity Catalog, read by dex, are the
    deployments; no vendor executes a Metric View query for dex."""
    with pytest.raises(
        ValueError, match="deployment must be one of local, databricks_workspace"
    ):
        _config(
            {
                "vendor": "databricks",
                "deployment": "dbt_cloud",
                "databricks": {"files": ["views/orders.yaml"]},
            }
        )


def test_the_connector_section_and_the_vendor_section_do_not_collide():
    """`databricks:` at the top is the connector's target; under `semantic:` it is
    the Metric View listing. Each is read where it is written."""

    config = DexConfig.model_validate(
        {
            "connector": "duckdb",
            "semantic": {"vendor": "databricks", "databricks": {"files": ["v.yaml"]}},
        }
    )
    assert config.databricks is None
    assert config.semantic.databricks.files == ["v.yaml"]


def _repo(root: Path, body: str) -> Path:
    """A repository whose `.dex/config.yml` is ``body``."""
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(body, encoding="utf-8")
    return root


def _run(capsys, root: Path, *argv: str):
    """Run the CLI against ``root`` and return its exit code and envelope."""
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_config_validate_names_a_bad_listing_at_its_line(tmp_path, capsys):
    """The DDL refusal reaches `config validate`."""
    root = _repo(
        tmp_path,
        "connector: duckdb\nsemantic:\n  vendor: databricks\n  databricks:\n"
        "    files: [views/orders.sql]\n",
    )

    _, envelope = _run(capsys, root, "config", "validate")

    assert envelope["status"] == "error"
    assert "CREATE VIEW ... WITH METRICS" in json.dumps(envelope)


def test_config_get_reports_the_listing(tmp_path, capsys):
    """The file list, as written."""
    root = _repo(
        tmp_path,
        "connector: duckdb\nsemantic:\n  vendor: databricks\n  databricks:\n"
        "    files: [views/orders.yaml]\n",
    )

    _, envelope = _run(capsys, root, "config", "get", "semantic.databricks")

    assert envelope["status"] == "ok"
    rows = {row["key"]: row for row in envelope["data"]["settings"]}
    assert rows["semantic.databricks.files"]["value"] == ["views/orders.yaml"]


# --- semantic.deployment: databricks_workspace (#1364) -------------------------------


def _workspace(**semantic) -> DexConfig:
    return DexConfig.model_validate(
        {
            "connector": "databricks",
            "semantic": {
                "vendor": "databricks",
                "deployment": "databricks_workspace",
                **semantic,
            },
        }
    )


def test_the_workspace_deployment_needs_no_files():
    """The catalog is Unity Catalog's, so the listing is optional there."""
    config = _workspace()

    assert config.semantic.deployment == "databricks_workspace"
    assert config.semantic.backend == "databricks_workspace"
    assert config.semantic.databricks.files == []


def test_the_workspace_deployment_keeps_configured_files():
    """As a local dbt project sits beside dbt_cloud: maintain, transform and
    --local keep reading them."""
    config = _workspace(databricks={"files": ["views/orders.yaml"]})

    assert config.semantic.databricks.files == ["views/orders.yaml"]


def test_local_still_needs_its_files():
    """Never a fallback when the listing is empty."""
    with pytest.raises(ValueError, match=r"needs semantic\.databricks\.files"):
        _config({"vendor": "databricks", "deployment": "local"})


def test_the_released_backend_spelling_agrees_or_is_refused():
    config = DexConfig.model_validate(
        {"semantic": {"vendor": "databricks", "backend": "databricks_workspace"}}
    )
    assert config.semantic.deployment == "databricks_workspace"

    with pytest.raises(ValueError, match="name different deployments"):
        _workspace(backend="local", databricks={"files": ["a.yaml"]})


@pytest.mark.parametrize(
    "vendor", ["dbt", "snowflake", "ossie", "slayer", "cube"], ids=str
)
def test_the_workspace_deployment_belongs_to_databricks_only(vendor):
    with pytest.raises(ValueError, match="deployment must be one of"):
        DexConfig.model_validate(
            {"semantic": {"vendor": vendor, "deployment": "databricks_workspace"}}
        )


def test_the_dbt_cloud_coordinates_are_still_refused():
    with pytest.raises(ValueError, match="hosted dbt"):
        _workspace(host="sl.cloud.getdbt.com")


def test_config_validate_accepts_the_workspace_with_no_files(tmp_path, capsys):
    root = _repo(
        tmp_path,
        "connector: databricks\nsemantic:\n  vendor: databricks\n"
        "  deployment: databricks_workspace\n",
    )

    _, envelope = _run(capsys, root, "config", "validate")

    assert envelope["status"] == "ok", envelope
