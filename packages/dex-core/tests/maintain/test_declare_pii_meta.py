"""`maintain reconcile` writes the PII stamp under `config.meta` and reads both (#490).

A column that appears upstream is spliced into a declaration a person wrote, so
the stamp has to land where dbt now expects it. The model-level check reads both
locations, because a project scaffolded by an older dex carries its stamp at the
top level and one brought up to date carries it under `config`.
"""

from __future__ import annotations

import pytest
import yaml

from exmergo_dex_core.cache import ColumnProfile, PIICategory, PIIFlag
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.maintain.declare import DeclarationEdits, Placed

PATH = "models/staging/stg_customers.yml"


def _edits(model_fields: str) -> tuple[DeclarationEdits, Placed]:
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        f"{model_fields}"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [not_null]\n"
    )
    view = DbtProjectView(
        root=".",
        project_name="p",
        profile_name="p",
        files={PATH: SourceFile(path=PATH, content=content, sha256="x")},
    )
    edits = DeclarationEdits(view, None)
    placed = edits.resolve("customers")
    assert isinstance(placed, Placed), placed
    return edits, placed


def _email() -> ColumnProfile:
    return ColumnProfile(
        name="email",
        data_type="VARCHAR",
        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
    )


def test_a_new_pii_column_is_stamped_under_config_meta():
    edits, placed = _edits("    config:\n      meta:\n        contains_pii: true\n")

    assert edits.add_column(placed, _email()) is None
    [edit] = edits.edits()
    entry = yaml.safe_load(edit.new_content)["models"][0]
    email = next(c for c in entry["columns"] if c["name"] == "email")

    assert "meta" not in email
    assert email["config"]["meta"] == {"contains_pii": True, "pii_category": "email"}


@pytest.mark.parametrize(
    "model_fields",
    [
        "    meta:\n      contains_pii: true\n",
        "    config:\n      meta:\n        contains_pii: true\n",
    ],
    ids=["top_level_from_an_older_dex", "config_meta"],
)
def test_a_model_already_stamped_in_either_location_needs_no_warning(model_fields):
    edits, placed = _edits(model_fields)

    edits.add_column(placed, _email())

    assert edits.warnings == []


def test_an_unstamped_model_is_told_to_stamp_under_config_meta():
    edits, placed = _edits("")

    edits.add_column(placed, _email())

    [warning] = edits.warnings
    assert "under its config.meta" in warning
