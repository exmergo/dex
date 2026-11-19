"""`semantic.vendor: cube` configuration (#910).

The coordinates are an exact allowlist of Cube model files inside the
repository, a non-secret API base, and the Cube data source dex is connected to.
Every malformed variant is refused where it is written, by name, and the
per-vendor section loop guards `semantic.cube` without a vendor-specific
condition. None of this needs the `[cube]` extra.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import (
    SEMANTIC_DEPLOYMENTS,
    SEMANTIC_DOCUMENT_AUTHORING,
    SEMANTIC_SOURCE_FACTORIES,
    SemanticConfig,
    load_config,
)

ISSUE_BLOCK = """connector: duckdb
semantic:
  vendor: cube
  cube:
    files:
      - model/cubes/orders.yml
      - model/views/sales.yaml
      - model/cubes/legacy.js
    url: https://cube.example.com/cubejs-api
    data_source: default
"""

FILES = ["model/cubes/orders.yml", "model/views/sales.yaml", "model/cubes/legacy.js"]


def _refusal(**config) -> str:
    with pytest.raises(ValidationError) as raised:
        SemanticConfig(**config)
    return str(raised.value)


def _repo(root: Path, text: str) -> Path:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(text, encoding="utf-8")
    return root


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_cube_is_a_vendor_with_a_local_and_a_cloud_deployment_and_its_own_source():
    assert SEMANTIC_DEPLOYMENTS["cube"] == ("local", "cube_cloud")
    assert SEMANTIC_SOURCE_FACTORIES["cube"] == (
        "exmergo_dex_core.cube.project:build_semantic_layer"
    )
    # Authored through `semantic cube` (#911), validated by the layer itself.
    assert "cube" in SEMANTIC_DOCUMENT_AUTHORING
    assert set(SEMANTIC_DOCUMENT_AUTHORING) <= set(SEMANTIC_SOURCE_FACTORIES)


def test_the_documented_block_loads(tmp_path):
    config = load_config(_repo(tmp_path, ISSUE_BLOCK))

    assert config.semantic.vendor == "cube"
    assert config.semantic.deployment == "local"
    assert config.semantic.cube.files == FILES
    assert config.semantic.cube.url == "https://cube.example.com/cubejs-api"
    assert config.semantic.cube.data_source == "default"


def test_data_source_defaults_to_cube_s_own_default():
    assert SemanticConfig(
        vendor="cube", cube={"files": ["m.yml"]}
    ).cube.data_source == ("default")


def test_an_empty_cube_section_stays_valid_under_any_vendor():
    SemanticConfig(vendor="dbt", cube={"files": []})


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ({}, "semantic.vendor: cube needs semantic.cube.files"),
        ({"files": ["model/*.yml"]}, "is a pattern"),
        ({"files": ["/abs/orders.yml"]}, "has to name a path inside the repository"),
        ({"files": ["../elsewhere/orders.yml"]}, "has to name a path inside"),
        ({"files": ["model/./orders.yml"]}, "write 'model/./orders.yml' as"),
        ({"files": ["model/orders.json"]}, "is not a Cube model file"),
        ({"files": ["model/globals.py"]}, "configures a Cube deployment"),
        ({"files": ["cube.js"]}, "configures a Cube deployment"),
        ({"files": ["a.yml", "a.yml"]}, "names the same file twice"),
        ({"files": ["a.yml"], "url": "ftp://cube.example.com"}, "http or https URL"),
        (
            {"files": ["a.yml"], "url": "https://user:secret@cube.example.com"},
            "carries a user or password",
        ),
        (
            {"files": ["a.yml"], "url": "https://cube.example.com/api?token=abc"},
            "query string or fragment",
        ),
        ({"files": ["a.yml"], "data_source": "my-source"}, "is not a Cube data source"),
    ],
)
def test_a_malformed_cube_section_is_refused_by_name(section, named):
    assert named in _refusal(vendor="cube", cube=section)


@pytest.mark.parametrize(
    "argv",
    [("config", "validate"), ("config", "get"), ("explore", "semantic", "list")],
)
def test_a_password_in_the_url_never_reaches_an_envelope(tmp_path, capsys, argv):
    """Refused, and refused without quoting it back: the envelope is what an
    agent reads, and the refusal has to name the problem without the secret."""

    text = ISSUE_BLOCK.replace("https://cube", "https://dex:hunter2pw@cube")
    rc, envelope = _run(capsys, _repo(tmp_path, text), *argv)

    assert rc != 0
    assert "hunter2pw" not in json.dumps(envelope)


@pytest.mark.parametrize(
    ("config", "named"),
    [
        (
            {"vendor": "dbt", "cube": {"files": ["a.yml"]}},
            "semantic.cube.files is only valid for semantic.vendor: cube",
        ),
        (
            {
                "vendor": "slayer",
                "slayer": {"base_dir": "s", "models": ["models/w/a.yaml"]},
                "cube": {"data_source": "x"},
            },
            "semantic.cube.data_source is only valid for semantic.vendor: cube",
        ),
        (
            {"vendor": "cube", "deployment": "cube_cloud"},
            "semantic.vendor: cube needs semantic.cube.files",
        ),
        (
            {
                "vendor": "cube",
                "deployment": "dbt_cloud",
                "cube": {"files": ["a.yml"]},
            },
            "semantic.deployment must be one of local, cube_cloud for vendor 'cube'",
        ),
        (
            {
                "vendor": "cube",
                "host": "x.cloud.getdbt.com",
                "cube": {"files": ["a.yml"]},
            },
            "semantic.vendor: cube has no hosted deployment",
        ),
    ],
)
def test_the_section_loop_holds_cube_to_its_own_vendor(config, named):
    assert named in _refusal(**config)


def test_config_validate_reads_the_block(tmp_path, capsys):
    rc, envelope = _run(capsys, _repo(tmp_path, ISSUE_BLOCK), "config", "validate")

    assert rc == 0, envelope
    assert envelope["data"]["problems"] == []


def test_config_get_reports_the_cube_settings(tmp_path, capsys):
    rc, envelope = _run(
        capsys, _repo(tmp_path, ISSUE_BLOCK), "config", "get", "semantic.cube"
    )

    assert rc == 0, envelope
    settings = {row["key"]: row for row in envelope["data"]["settings"]}
    assert (
        settings["semantic.cube.url"]["value"] == "https://cube.example.com/cubejs-api"
    )
    assert settings["semantic.cube.data_source"]["source"] == "file"


def test_config_get_reports_dex_cube_url_beating_the_file(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("DEX_CUBE_URL", "https://d.aws-us-east-1.cubecloudapp.dev/x")
    rc, envelope = _run(
        capsys, _repo(tmp_path, ISSUE_BLOCK), "config", "get", "semantic.cube.url"
    )

    assert rc == 0, envelope
    (row,) = envelope["data"]["settings"]
    assert row["source"] == "environment"
    assert row["variable"] == "DEX_CUBE_URL"
    assert row["value"] == "https://d.aws-us-east-1.cubecloudapp.dev/x"


def test_dbt_cloud_s_variables_do_not_override_a_cube_layer(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("DBT_SL_HOST", "x.cloud.getdbt.com")
    rc, envelope = _run(capsys, _repo(tmp_path, ISSUE_BLOCK), "config", "get")

    assert rc == 0, envelope
    assert not [
        row for row in envelope["data"]["settings"] if row["source"] == "environment"
    ]


def test_cube_cloud_loads_with_its_url(tmp_path):
    text = ISSUE_BLOCK.replace("  cube:\n", "  deployment: cube_cloud\n  cube:\n")
    config = load_config(_repo(tmp_path, text))

    assert config.semantic.deployment == "cube_cloud"
    assert config.semantic.cube.files == FILES


def test_config_validate_refuses_a_bad_file_at_its_line(tmp_path, capsys):
    text = ISSUE_BLOCK.replace("model/views/sales.yaml", "model/views/*.yaml")
    rc, envelope = _run(capsys, _repo(tmp_path, text), "config", "validate")

    assert rc != 0
    assert envelope["reason"] == "configuration"
    problems = envelope["data"]["problems"]
    assert any("is a pattern" in p["message"] and p["fatal"] for p in problems)
