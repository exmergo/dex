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
    """With `local` as its only deployment."""
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
    """Only the files in this repository are a deployment."""
    with pytest.raises(ValueError, match="deployment must be one of local"):
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
