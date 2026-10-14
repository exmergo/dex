"""`semantic.vendor: slayer` configuration (#596).

The coordinates are an exact allowlist of SLayer model files under a storage
root inside the repository. Every malformed variant is refused where it is
written, by name, and the per-vendor section loop that already guards
`semantic.ossie` guards `semantic.slayer` without a vendor-specific condition.
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
  vendor: slayer
  slayer:
    base_dir: semantics/slayer
    models:
      - models/warehouse/orders.yaml
      - models/warehouse/customers.yaml
"""

MODELS = ["models/warehouse/orders.yaml", "models/warehouse/customers.yaml"]


def _slayer(**section) -> SemanticConfig:
    return SemanticConfig(vendor="slayer", slayer=section)


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
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


# --- the tables -------------------------------------------------------------------


def test_slayer_is_a_vendor_with_one_local_deployment():
    assert SEMANTIC_DEPLOYMENTS["slayer"] == ("local",)


def test_slayer_has_its_own_semantic_source_and_no_authoring_route_yet():
    """The source replaces MetricFlow as the catalog; `semantic slayer` authoring
    is not registered until it has a route of its own, so it cannot fall into
    the Ossie validator."""

    assert SEMANTIC_SOURCE_FACTORIES["slayer"] == (
        "exmergo_dex_core.slayer.project:build_semantic_layer"
    )
    assert "slayer" not in SEMANTIC_DOCUMENT_AUTHORING
    assert set(SEMANTIC_DOCUMENT_AUTHORING) <= set(SEMANTIC_SOURCE_FACTORIES)


# --- accepted ---------------------------------------------------------------------


def test_the_documented_block_loads(tmp_path):
    config = load_config(_repo(tmp_path, ISSUE_BLOCK))

    assert config.semantic.vendor == "slayer"
    assert config.semantic.deployment == "local"
    assert config.semantic.slayer.base_dir == "semantics/slayer"
    assert config.semantic.slayer.models == MODELS


def test_yml_models_and_a_root_storage_dir_are_accepted():
    config = _slayer(base_dir=".", models=["models/pg/orders.yml"])
    assert config.slayer.models == ["models/pg/orders.yml"]


def test_an_empty_slayer_section_stays_valid_under_any_vendor():
    """Spelling out an empty section says nothing, as it always has for Ossie."""

    SemanticConfig(vendor="dbt", ossie={"files": []}, slayer={"models": []})


# --- refused, by name ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ({}, "semantic.vendor: slayer needs semantic.slayer.models"),
        (
            {"base_dir": "semantics/slayer"},
            "semantic.vendor: slayer needs semantic.slayer.models",
        ),
        ({"models": MODELS}, "which is not set"),
        (
            {"base_dir": "s", "models": ["models/warehouse/*.yaml"]},
            "'models/warehouse/*.yaml' is a pattern",
        ),
        (
            {"base_dir": "s", "models": ["models/warehouse/[ab].yaml"]},
            "is a pattern",
        ),
        (
            {"base_dir": "/srv/slayer", "models": MODELS},
            "semantic.slayer.base_dir: '/srv/slayer' has to name a path inside "
            "the repository",
        ),
        (
            {"base_dir": "../elsewhere", "models": MODELS},
            "semantic.slayer.base_dir: '../elsewhere'",
        ),
        (
            {"base_dir": "s", "models": ["/models/warehouse/orders.yaml"]},
            "has to name a path inside semantic.slayer.base_dir",
        ),
        (
            {"base_dir": "s", "models": ["models/../models/w/orders.yaml"]},
            "has to name a path inside semantic.slayer.base_dir",
        ),
        (
            {"base_dir": "s", "models": ["models\\warehouse\\orders.yaml"]},
            "forward slashes",
        ),
        (
            {"base_dir": "s", "models": [*MODELS, MODELS[0]]},
            "semantic.slayer.models names the same model twice: "
            "models/warehouse/orders.yaml",
        ),
        (
            {"base_dir": "s", "models": ["models/./warehouse/orders.yaml"]},
            "write 'models/./warehouse/orders.yaml' as 'models/warehouse/orders.yaml'",
        ),
        (
            {"base_dir": "s", "models": ["datasources/warehouse.yaml"]},
            "'datasources/warehouse.yaml' is not a model",
        ),
        (
            {"base_dir": "s", "models": ["priority.yaml"]},
            "'priority.yaml' is not a model",
        ),
        (
            {"base_dir": "s", "models": ["models/orders.yaml"]},
            "write it as models/<data_source>/<name>.yaml",
        ),
        (
            {"base_dir": "s", "models": ["models/warehouse/orders.json"]},
            "write it as models/<data_source>/<name>.yaml",
        ),
        ({"base_dir": "s", "models": [""]}, "an empty path names nothing"),
    ],
)
def test_a_malformed_slayer_section_is_refused_by_name(section, named):
    assert named in _refusal(vendor="slayer", slayer=section)


def test_datasource_files_are_refused_for_what_they_carry():
    message = _refusal(
        vendor="slayer", slayer={"base_dir": "s", "models": ["datasources/pg.yaml"]}
    )
    assert "connection credentials" in message


@pytest.mark.parametrize(
    ("config", "named"),
    [
        (
            {"vendor": "dbt", "slayer": {"base_dir": "semantics/slayer"}},
            "semantic.slayer.base_dir is only valid for semantic.vendor: slayer",
        ),
        (
            {"slayer": {"base_dir": "s", "models": MODELS}},
            "semantic.slayer.base_dir and semantic.slayer.models are only valid "
            "for semantic.vendor: slayer",
        ),
        (
            {
                "vendor": "ossie",
                "ossie": {"files": ["layer.ossie.yaml"]},
                "slayer": {"base_dir": "s", "models": MODELS},
            },
            "are only valid for semantic.vendor: slayer",
        ),
        (
            {"vendor": "slayer", "ossie": {"files": ["layer.ossie.yaml"]}},
            "semantic.ossie.files is only valid for semantic.vendor: ossie",
        ),
        (
            {
                "vendor": "slayer",
                "deployment": "dbt_cloud",
                "slayer": {"base_dir": "s", "models": MODELS},
            },
            "semantic.deployment must be one of local for vendor 'slayer'",
        ),
        (
            {
                "vendor": "slayer",
                "host": "semantic-layer.cloud.getdbt.com",
                "slayer": {"base_dir": "s", "models": MODELS},
            },
            "semantic.vendor: slayer has no hosted deployment",
        ),
    ],
)
def test_the_section_loop_holds_slayer_to_its_own_vendor(config, named):
    assert named in _refusal(**config)


def test_the_ossie_refusals_read_as_they_did():
    """Generalizing the loop must not move the messages Ossie users already see."""

    assert "semantic.vendor: ossie needs semantic.ossie.files: the native " in (
        _refusal(vendor="ossie")
    )
    assert "semantic.ossie.files is only valid for semantic.vendor: ossie" in (
        _refusal(vendor="dbt", ossie={"files": ["a.ossie.yaml"]})
    )


# --- through `dex config validate` --------------------------------------------------


def test_config_validate_accepts_the_documented_block(tmp_path, capsys):
    rc, envelope = _run(capsys, _repo(tmp_path, ISSUE_BLOCK), "config", "validate")

    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["problems"] == []


@pytest.mark.parametrize(
    ("replace", "named"),
    [
        (
            ("      - models/warehouse/customers.yaml", "      - datasources/pg.yaml"),
            "'datasources/pg.yaml' is not a model",
        ),
        (
            (
                "      - models/warehouse/customers.yaml",
                "      - models/warehouse/*.yaml",
            ),
            "is a pattern",
        ),
        (
            ("  vendor: slayer", "  vendor: dbt"),
            "only valid for semantic.vendor: slayer",
        ),
        (
            ("    base_dir: semantics/slayer", "    base_dir: /abs/slayer"),
            "semantic.slayer.base_dir: '/abs/slayer'",
        ),
    ],
)
def test_config_validate_refuses_each_malformed_variant_by_name(
    tmp_path, capsys, replace, named
):
    text = ISSUE_BLOCK.replace(*replace)
    assert text != ISSUE_BLOCK
    rc, envelope = _run(capsys, _repo(tmp_path, text), "config", "validate")

    assert rc != 0
    assert envelope["status"] == "error"
    assert envelope["reason"] == "configuration"
    problems = envelope["data"]["problems"]
    assert any(named in p["message"] and p["fatal"] for p in problems), problems
    assert all(p["key"].startswith("semantic") for p in problems), problems
