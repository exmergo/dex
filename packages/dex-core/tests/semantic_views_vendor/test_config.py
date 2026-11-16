"""`semantic.vendor: snowflake` configuration (#1291)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from exmergo_dex_core.config import (
    SEMANTIC_DEPLOYMENTS,
    SEMANTIC_DOCUMENT_AUTHORING,
    DexConfig,
)


def _config(**snowflake) -> DexConfig:
    return DexConfig.model_validate(
        {
            "connector": "snowflake",
            "semantic": {"vendor": "snowflake", "snowflake": snowflake},
        }
    )


def test_the_vendor_reads_its_files() -> None:
    """Local, or read from the account (#1357); authored through `semantic
    snowflake` (#1292)."""
    config = _config(files=["semantic/orders.yaml", "semantic/lines.yml"])

    assert config.semantic.snowflake.files == [
        "semantic/orders.yaml",
        "semantic/lines.yml",
    ]
    assert SEMANTIC_DEPLOYMENTS["snowflake"] == ("local", "snowflake_account")
    assert config.semantic.deployment == "local"
    assert "snowflake" in SEMANTIC_DOCUMENT_AUTHORING


def test_the_vendor_needs_its_files() -> None:
    """Naming the setting."""
    with pytest.raises(ValidationError, match=r"semantic\.snowflake\.files"):
        _config()


@pytest.mark.parametrize(
    ("name", "says"),
    [
        ("semantic/orders.sql", "CREATE SEMANTIC VIEW"),
        ("semantic/orders.json", "named .yaml or .yml"),
        ("/abs/orders.yaml", "relative"),
        ("../orders.yaml", ".."),
        ("semantic/*.yaml", "pattern"),
    ],
)
def test_a_file_dex_cannot_read_is_refused_by_name(name: str, says: str) -> None:
    """DDL by name; the rest by the allowlist's rules."""
    with pytest.raises(ValidationError) as caught:
        _config(files=[name])
    assert says.lower() in str(caught.value).lower()


def test_a_file_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="twice"):
        _config(files=["a.yaml", "a.yaml"])


def test_two_stems_may_share_a_name_since_the_view_is_named_inside() -> None:
    """Unlike Metric Views, the YAML names its view."""
    config = _config(files=["one/orders.yaml", "two/orders.yaml"])
    assert len(config.semantic.snowflake.files) == 2


def test_the_files_are_only_valid_for_this_vendor() -> None:
    with pytest.raises(ValidationError, match=r"semantic\.snowflake\.files"):
        DexConfig.model_validate(
            {"semantic": {"vendor": "dbt", "snowflake": {"files": ["a.yaml"]}}}
        )


# --- semantic.deployment: snowflake_account (#1357) ---------------------------------


def _account(**semantic) -> DexConfig:
    return DexConfig.model_validate(
        {
            "connector": "snowflake",
            "semantic": {
                "vendor": "snowflake",
                "deployment": "snowflake_account",
                **semantic,
            },
        }
    )


def test_the_account_deployment_needs_no_files() -> None:
    """The catalog is the account's, so the listing is optional there."""
    config = _account()

    assert config.semantic.deployment == "snowflake_account"
    assert config.semantic.backend == "snowflake_account"
    assert config.semantic.snowflake.files == []


def test_the_account_deployment_keeps_configured_files() -> None:
    """As a local dbt project sits beside dbt_cloud: maintain, transform and
    --local keep reading them."""
    config = _account(snowflake={"files": ["views/sales.yaml"]})

    assert config.semantic.snowflake.files == ["views/sales.yaml"]


def test_local_still_needs_its_files() -> None:
    """Never a fallback when the listing is empty."""
    with pytest.raises(ValidationError, match=r"semantic\.snowflake\.files"):
        DexConfig.model_validate(
            {"semantic": {"vendor": "snowflake", "deployment": "local"}}
        )


def test_the_released_backend_spelling_agrees_or_is_refused() -> None:
    config = DexConfig.model_validate(
        {"semantic": {"vendor": "snowflake", "backend": "snowflake_account"}}
    )
    assert config.semantic.deployment == "snowflake_account"

    with pytest.raises(ValidationError, match="name different deployments"):
        _account(backend="local", snowflake={"files": ["a.yaml"]})


@pytest.mark.parametrize("deployment", ["dbt_cloud", "account"])
def test_another_deployment_is_refused_by_name(deployment: str) -> None:
    with pytest.raises(ValidationError, match="snowflake_account"):
        DexConfig.model_validate(
            {"semantic": {"vendor": "snowflake", "deployment": deployment}}
        )


def test_the_account_deployment_belongs_to_snowflake_only() -> None:
    with pytest.raises(ValidationError, match="local, dbt_cloud"):
        DexConfig.model_validate(
            {"semantic": {"vendor": "dbt", "deployment": "snowflake_account"}}
        )


def test_the_dbt_cloud_coordinates_are_still_refused() -> None:
    with pytest.raises(ValidationError, match="hosted dbt"):
        _account(host="sl.cloud.getdbt.com")
