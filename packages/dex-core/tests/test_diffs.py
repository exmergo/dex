"""Unified-diff rendering for the envelope's ``diffs`` field."""

from __future__ import annotations

import pytest

from exmergo_dex_core.diffs import file_diff


def test_update_diff_carries_unified_text_and_counts():
    d = file_diff("models/stg.sql", "select 1\n", "select 1\nselect 2\n")
    assert d["op"] == "update"
    assert d["path"] == "models/stg.sql"
    assert "--- a/models/stg.sql" in d["unified"]
    assert "+++ b/models/stg.sql" in d["unified"]
    assert "+select 2" in d["unified"]
    assert d["additions"] == 1
    assert d["deletions"] == 0


def test_create_diff_uses_dev_null_source():
    d = file_diff("models/new.sql", None, "select 1\n")
    assert d["op"] == "create"
    assert "--- /dev/null" in d["unified"]
    assert d["additions"] == 1


def test_delete_diff_uses_dev_null_target():
    d = file_diff("models/old.sql", "select 1\n", None)
    assert d["op"] == "delete"
    assert "+++ /dev/null" in d["unified"]
    assert d["deletions"] == 1


def test_identical_content_yields_empty_unified_text():
    d = file_diff("models/same.sql", "select 1\n", "select 1\n")
    assert d["unified"] == ""
    assert d["additions"] == 0
    assert d["deletions"] == 0


def test_requires_at_least_one_side():
    with pytest.raises(ValueError):
        file_diff("models/none.sql", None, None)
