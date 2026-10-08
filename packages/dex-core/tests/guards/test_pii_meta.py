"""The PII marks dex stamps into dbt `meta`: one key list, both locations (#490)."""

from __future__ import annotations

import pytest
import yaml

from exmergo_dex_core.guards.pii_meta import (
    CONTAINS_PII,
    PII_CATEGORY,
    PII_MARK_KEYS,
    entry_meta,
    says_pii,
    stamp_lines,
)
from exmergo_dex_core.transform.validate import (
    EditValidationError,
    assert_no_doubled_keys,
)


def test_the_stamp_is_written_under_config_meta_with_the_unchanged_key_names():
    stamped = yaml.safe_load("\n".join(["name: email", *stamp_lines("", "email")]))

    assert stamped == {
        "name": "email",
        "config": {"meta": {"contains_pii": True, "pii_category": "email"}},
    }


def test_a_model_level_stamp_carries_no_category():
    assert yaml.safe_load("\n".join(stamp_lines(""))) == {
        "config": {"meta": {"contains_pii": True}}
    }


def test_every_key_a_writer_emits_is_one_a_reader_accepts():
    """The drift this module exists to end: a writer and a reader each holding
    their own list."""

    assert {CONTAINS_PII, PII_CATEGORY} <= set(PII_MARK_KEYS)


@pytest.mark.parametrize(
    "entry",
    [
        # Scaffolded by an older dex, never touched since.
        {"name": "stg_customers", "meta": {"contains_pii": True}},
        # Brought up to date by hand, or scaffolded by this dex.
        {"name": "stg_customers", "config": {"meta": {"contains_pii": True}}},
    ],
    ids=["top_level", "config_meta"],
)
def test_a_reader_finds_the_stamp_in_either_location(entry):
    assert says_pii(entry_meta(entry))


def test_config_meta_wins_where_the_two_disagree():
    """dbt's own precedence when it merges them, so dex reads what dbt reads."""

    entry = {
        "meta": {"contains_pii": True, "owner": "a"},
        "config": {"meta": {"contains_pii": False}},
    }

    assert entry_meta(entry) == {"contains_pii": False, "owner": "a"}


def test_the_semantic_gate_names_the_category_dex_stamps():
    from exmergo_dex_core.explore.semantic.policy import screen_dimension_refs

    meta = {CONTAINS_PII: True, PII_CATEGORY: "email"}

    blocked = screen_dimension_refs(["user__contact"], meta_lookup=lambda _ref: meta)

    assert blocked == [("user__contact", "email (profiled and flagged)")]


@pytest.mark.parametrize("entry", [None, "stg_x", {}, {"config": "nope"}])
def test_an_entry_with_no_readable_meta_reads_as_empty(entry):
    assert entry_meta(entry) == {}


def test_a_model_with_both_meta_forms_is_refused_with_the_fix():
    parsed = {
        "models": [
            {
                "name": "stg_customers",
                "meta": {"contains_pii": True},
                "config": {"meta": {"owner": "data-team"}},
            }
        ]
    }

    with pytest.raises(EditValidationError, match="Move the keys"):
        assert_no_doubled_keys("models/stg_customers.yml", parsed)


def test_a_column_with_both_meta_forms_passes_because_dbt_merges_it():
    parsed = {
        "models": [
            {
                "name": "stg_customers",
                "columns": [
                    {
                        "name": "email",
                        "meta": {"contains_pii": True},
                        "config": {"meta": {"pii_category": "email"}},
                    }
                ],
            }
        ]
    }

    assert_no_doubled_keys("models/stg_customers.yml", parsed)


@pytest.mark.parametrize(
    "parsed",
    [
        {"models": [{"name": "m", "tests": ["a"], "data_tests": ["b"]}]},
        {
            "models": [
                {
                    "name": "m",
                    "columns": [
                        {"name": "id", "tests": ["unique"], "data_tests": ["not_null"]}
                    ],
                }
            ]
        },
    ],
    ids=["model", "column"],
)
def test_both_test_spellings_on_one_entry_is_refused(parsed):
    with pytest.raises(EditValidationError, match="both 'tests' and 'data_tests'"):
        assert_no_doubled_keys("models/m.yml", parsed)


def test_either_spelling_alone_passes():
    parsed = {
        "models": [
            {"name": "a", "columns": [{"name": "id", "tests": ["unique"]}]},
            {"name": "b", "columns": [{"name": "id", "data_tests": ["unique"]}]},
        ]
    }

    assert_no_doubled_keys("models/m.yml", parsed)
