"""Per-kind edit validation, including the profiles.yml secret-guard."""

from __future__ import annotations

import pytest

from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import (
    EditValidationError,
    find_inlined_secret,
    validate_edit,
)


def test_find_inlined_secret_flags_a_literal_value():
    assert find_inlined_secret("o:\n  password: hunter2\n") == "password"


def test_find_inlined_secret_allows_an_env_var_reference():
    assert (
        find_inlined_secret("o:\n  password: \"{{ env_var('PGPASSWORD') }}\"\n") is None
    )


def test_find_inlined_secret_allows_a_key_path_and_method():
    # A path is not a secret, and a substring match must not misfire on it.
    assert find_inlined_secret("o:\n  private_key_path: /keys/rsa.p8\n") is None
    assert find_inlined_secret("o:\n  method: externalbrowser\n") is None


def test_find_inlined_secret_walks_nested_outputs():
    content = (
        "profile:\n  outputs:\n    dev:\n      type: snowflake\n"
        "      token: raw-token-value\n"
    )
    assert find_inlined_secret(content) == "token"


def test_validate_project_yml_requires_a_name():
    edit = PlanEdit(
        path="dbt_project.yml",
        new_content='version: "1.0.0"\nprofile: p\n',
        kind=EditKind.PROJECT_YML,
    )
    with pytest.raises(EditValidationError):
        validate_edit(edit)


def test_validate_project_yml_with_a_name_passes():
    edit = PlanEdit(
        path="dbt_project.yml",
        new_content="name: analytics\nprofile: analytics\n",
        kind=EditKind.PROJECT_YML,
    )
    assert validate_edit(edit) == []


def test_validate_profiles_yml_refuses_a_literal_secret():
    edit = PlanEdit(
        path="profiles.yml",
        new_content="p:\n  outputs:\n    dev:\n      password: s3cr3t-value\n",
        kind=EditKind.PROFILES_YML,
    )
    with pytest.raises(EditValidationError) as exc:
        validate_edit(edit)
    assert "s3cr3t-value" not in str(exc.value)  # names the key, never the value
    assert "password" in str(exc.value)


def test_validate_profiles_yml_accepts_env_var_indirection():
    edit = PlanEdit(
        path="profiles.yml",
        new_content=(
            "p:\n  outputs:\n    dev:\n      type: postgres\n"
            "      password: \"{{ env_var('PGPASSWORD') }}\"\n"
        ),
        kind=EditKind.PROFILES_YML,
    )
    assert validate_edit(edit) == []
