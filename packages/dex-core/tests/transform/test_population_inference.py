"""A model with no declared parent gets a drafted one (#1228).

Each case is a small dbt project on disk, read the way `transform plan` reads
it: the files, the reference index and the key facts its YAML states, with a
hand-built cache where a case needs profiled evidence. Nothing runs dbt and
nothing opens a connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    Relationship,
    RelationshipKind,
)
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.references import ReferenceIndex
from exmergo_dex_core.transform.drafting import draft
from exmergo_dex_core.transform.population_inference import (
    facts_for,
    head_noun,
)

pytest.importorskip("sqlglot")

PROJECT = """
name: shop
version: "1.0"
profile: shop
model-paths: ["models"]
"""

SOURCES = """
version: 2
sources:
  - name: raw
    tables:
      - name: conversations
      - name: parts
"""

STAGING_YAML = """
version: 2
models:
  - name: stg_conversations
    columns:
      - name: conversation_id
        data_tests: [unique, not_null]
  - name: stg_parts
    columns:
      - name: part_id
        data_tests: [unique]
      - name: conversation_id
        data_tests:
          - relationships:
              arguments:
                to: ref('stg_conversations')
                field: conversation_id
"""

FILES = {
    "dbt_project.yml": PROJECT,
    "models/staging/_sources.yml": SOURCES,
    "models/staging/_staging.yml": STAGING_YAML,
    "models/staging/stg_conversations.sql": (
        "select id as conversation_id, state from {{ source('raw', 'conversations') }}"
    ),
    "models/staging/stg_parts.sql": (
        "select id as part_id, conversation_id, created_at "
        "from {{ source('raw', 'parts') }}"
    ),
}

FROM_PARTS = """
with parts as (select * from {{ ref('stg_parts') }})
select conversation_id, count(*) as part_count, max(created_at) as last_part_at
from parts
group by conversation_id
"""

FROM_PARENT = """
with parts as (
    select conversation_id, count(*) as part_count
    from {{ ref('stg_parts') }}
    group by conversation_id
)
select c.conversation_id, c.state, coalesce(p.part_count, 0) as part_count
from {{ ref('stg_conversations') }} c
left join parts p on p.conversation_id = c.conversation_id
"""


def _project(tmp_path: Path, extra: dict[str, str]) -> Path:
    for path, content in {**FILES, **extra}.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def _draft(tmp_path: Path, model: str, extra: dict[str, str], cache=None):
    root = _project(tmp_path, extra)
    view = load(root)
    files = {path: source.content for path, source in view.files.items()}
    drafts, warnings = draft(
        [model],
        files,
        view.model_paths,
        cache,
        dialect="duckdb",
        vars=view.vars,
        index=lambda: ReferenceIndex(view),
        key_facts=lambda: facts_for(view, None),
    )
    return drafts[model], warnings


def test_an_aggregate_over_the_children_drafts_the_parent_its_key_joins_to(tmp_path):
    entry, warnings = _draft(
        tmp_path,
        "conversation_summary",
        {"models/marts/conversation_summary.sql": FROM_PARTS},
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"] == {
        "model": "stg_conversations",
        "key": {"conversation_id": "conversation_id"},
    }
    assert parent["drafted"] is True
    # Unique on the grain, and the key it carries joins there.
    assert parent["supported_by"] == ["unique_key", "relationship"]
    assert parent["evidence"] == "unique_key"
    assert parent["drives_from_parent"] is False
    assert parent["driving_relation"] == "stg_parts"
    sentence = next(w for w in warnings if "dex drafts stg_conversations" in w)
    assert "outside stg_conversations's lineage" in sentence


def test_a_model_driven_from_its_parent_says_so(tmp_path):
    entry, warnings = _draft(
        tmp_path,
        "conversation_summary",
        {
            "models/marts/conversation_summary.sql": FROM_PARENT,
            "models/marts/_marts.yml": (
                "version: 2\nmodels:\n  - name: conversation_summary\n"
                "    columns:\n      - name: conversation_id\n"
                "        data_tests: [unique]\n"
            ),
        },
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"]["model"] == "stg_conversations"
    assert parent["drives_from_parent"] is True
    assert "driving" in parent["supported_by"]
    assert not any("outside" in w for w in warnings if "dex drafts" in w)


def test_a_declared_parent_always_wins(tmp_path):
    entry, _ = _draft(
        tmp_path,
        "conversation_summary",
        {
            "models/marts/conversation_summary.sql": FROM_PARTS,
            "models/marts/_marts.yml": (
                "version: 2\nmodels:\n  - name: conversation_summary\n"
                "    config:\n      meta:\n        dex:\n          population:\n"
                "            parent:\n              model: stg_parts\n"
                "              key: {conversation_id: conversation_id}\n"
            ),
        },
    )

    assert "population_parent" not in entry["drafted"]


def test_no_grain_withholds_the_draft_with_the_reason(tmp_path):
    entry, _ = _draft(
        tmp_path,
        "parts_copy",
        {"models/marts/parts_copy.sql": "select * from {{ ref('stg_parts') }}"},
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"] is None
    assert "grain is not known" in parent["withheld"]


def test_a_staging_model_over_a_source_has_no_parent_to_draft(tmp_path):
    entry, _ = _draft(tmp_path, "stg_conversations", {})

    parent = entry["drafted"]["population_parent"]
    assert parent["value"] is None
    assert "ref() reaches" in parent["withheld"]


def test_a_model_downstream_of_this_one_is_never_its_parent(tmp_path):
    entry, _ = _draft(
        tmp_path,
        "conversation_summary",
        {
            "models/marts/conversation_summary.sql": FROM_PARTS,
            "models/marts/conversation_report.sql": (
                "select * from {{ ref('conversation_summary') }}"
            ),
            "models/marts/_marts.yml": (
                "version: 2\nmodels:\n  - name: conversation_report\n"
                "    columns:\n      - name: conversation_id\n"
                "        data_tests: [unique]\n"
            ),
        },
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"]["model"] == "stg_conversations"
    assert all(
        a["model"] != "conversation_report" for a in parent.get("alternatives", [])
    )


def test_a_time_spine_is_never_a_parent(tmp_path):
    entry, _ = _draft(
        tmp_path,
        "daily_parts",
        {
            "models/marts/daily_parts.sql": (
                "select d.date_day, count(p.part_id) as parts "
                "from {{ ref('metricflow_time_spine') }} d "
                "left join {{ ref('stg_parts') }} p on p.created_at = d.date_day "
                "group by d.date_day"
            ),
            "models/marts/metricflow_time_spine.sql": (
                "select * from range(date '2024-01-01', date '2024-12-31', "
                "interval 1 day) t(date_day)"
            ),
            "models/marts/_marts.yml": (
                "version: 2\nmodels:\n  - name: metricflow_time_spine\n"
                "    columns:\n      - name: date_day\n"
                "        data_tests: [unique]\n"
            ),
        },
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"] is None


def test_profiled_keys_and_a_verified_join_draft_without_any_test(tmp_path):
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="dev.main.stg_conversations",
                columns=[
                    ColumnProfile(
                        name="conversation_id",
                        data_type="integer",
                        null_fraction=0.0,
                        is_unique=True,
                        distinct_count_exact=True,
                    )
                ],
            ),
            Dataset(
                identifier="dev.main.stg_parts",
                columns=[
                    ColumnProfile(
                        name="conversation_id", data_type="integer", is_unique=False
                    )
                ],
            ),
        ],
        relationships=[
            Relationship(
                from_dataset="dev.main.stg_parts",
                from_columns=["conversation_id"],
                to_dataset="dev.main.stg_conversations",
                to_columns=["conversation_id"],
                kind=RelationshipKind.INFERRED,
                verified=True,
                orphan_fraction=0.0,
            )
        ],
    )
    entry, _ = _draft(
        tmp_path,
        "conversation_summary",
        {
            "models/staging/_staging.yml": "version: 2\nmodels: []\n",
            "models/marts/conversation_summary.sql": FROM_PARTS,
        },
        cache=cache,
    )

    parent = entry["drafted"]["population_parent"]
    assert parent["value"]["model"] == "stg_conversations"
    assert parent["supported_by"] == ["unique_key", "relationship"]
    assert "exploration cache" in parent["detail"]


def test_the_description_names_the_entity_among_tied_candidates(tmp_path):
    two = {
        "models/staging/_staging.yml": STAGING_YAML
        + (
            "  - name: stg_threads\n    columns:\n      - name: conversation_id\n"
            "        data_tests: [unique]\n"
        ),
        "models/staging/stg_threads.sql": (
            "select id as conversation_id from {{ source('raw', 'conversations') }}"
        ),
        "models/marts/thread_summary.sql": (
            "select conversation_id, count(*) as n from {{ ref('stg_parts') }} "
            "group by conversation_id"
        ),
        "models/marts/_marts.yml": (
            "version: 2\nmodels:\n  - name: thread_summary\n"
            "    description: One row per thread, with its part count.\n"
        ),
    }
    entry, _ = _draft(tmp_path, "thread_summary", two)

    parent = entry["drafted"]["population_parent"]
    # stg_conversations also has the relationships edge, so it outranks.
    assert parent["value"]["model"] == "stg_conversations"
    alternatives = {a["model"] for a in parent["alternatives"]}
    assert "stg_threads" in alternatives


@pytest.mark.parametrize(
    ("description", "noun"),
    [
        ("One row per project.", "project"),
        ("One row per support account, with its ticket count", "account"),
        ("a single row for each listing", "listing"),
        ("Each row per order_item.", None),
        ("Daily totals", None),
        (None, None),
        ("One row per companies", "company"),
    ],
)
def test_the_head_noun_of_a_description(description, noun):
    if description == "Each row per order_item.":
        # "each row per" reads as "one row per": the last word names it.
        assert head_noun(description) == "item"
        return
    assert head_noun(description) == noun


def test_a_key_and_the_attribute_it_determines_draft_the_keys_owner(tmp_path):
    entry, _ = _draft(
        tmp_path,
        "conversation_summary",
        {
            "models/marts/conversation_summary.sql": (
                "select p.conversation_id, c.state, count(*) as part_count\n"
                "from {{ ref('stg_parts') }} as p\n"
                "left join {{ ref('stg_conversations') }} as c "
                "using (conversation_id)\n"
                "group by 1, 2\n"
            )
        },
    )

    parent = entry["drafted"]["population_parent"]
    # The grain is (conversation_id, state); state is read off the parent.
    assert parent["value"] == {
        "model": "stg_conversations",
        "key": {"conversation_id": "conversation_id"},
    }
    assert "state in the grain is carried from stg_conversations" in parent["detail"]
