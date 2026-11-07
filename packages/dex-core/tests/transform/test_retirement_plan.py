"""Retirement planning contracts on a built dbt project, before apply/build."""

from __future__ import annotations

import json

import pytest

from .test_retirement_build import (
    EMPTY,
    FLAG,
    HAS_FLAG,
    PERMANENT,
    RETIRE,
    RETIRE_SQL,
    _build_verify,
    _records,
    _remove,
    _run,
    _write,
)
from .test_retirement_build import repo as repo

pytestmark = pytest.mark.live_open_adapter


def test_the_mirror_failure_is_caught_too(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, PERMANENT)
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert {"expected_output_missing", "dropped_retained_read"} <= checks


def test_a_contract_naming_the_wrong_branch_is_caught(repo, capsys):
    """Correct SQL and outputs, with the default's value stated by habit."""

    planned = _remove(repo, capsys, RETIRE_SQL, {**RETIRE, "retained_value": True})
    assert planned["data"]["retirement"]["verdict"] == "contradicted"
    assert "matches_retained" in _records(planned)["rpt_orders"]
    assert _records(planned)["rpt_orders"]["matches_retained"] is False


def test_a_join_kept_without_its_column_is_caught(repo, capsys):
    kept = {
        **RETIRE_SQL,
        "rpt_orders": (
            "with orders as (select * from {{ ref('stg_orders') }})\n"
            ", regions as (select * from {{ ref('lookup_regions') }})\n"
            "select o.order_id, o.amount\nfrom orders o\n"
            "left join regions r on o.region_id = r.region_id\n"
        ),
    }
    planned = _remove(repo, capsys, kept, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "reads_outside_retained_branch" in checks


def test_an_unread_cte_and_an_orphaned_model_are_named(repo, capsys):
    leftover = {
        **RETIRE_SQL,
        "rpt_orders": (
            "with orders as (select * from {{ ref('stg_orders') }})\n"
            ", unused as (select 1 as x)\n"
            "select o.order_id, o.amount\nfrom orders o\n"
        ),
    }
    planned = _remove(repo, capsys, leftover, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "unused_cte" in checks
    assert planned["data"]["retirement"]["orphaned_models"] == ["lookup_regions"]


def test_a_removed_column_still_read_downstream_is_named(repo, capsys):
    _write(
        repo / "shop/models/rpt_downstream.sql",
        "select region_label from {{ ref('rpt_orders') }}\n",
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    checks = {c["check"] for c in _records(planned)["rpt_orders"]["checks"]}
    assert "removed_column_still_read" in checks


def test_a_semantic_or_schema_reference_to_a_removed_column_is_named(repo, capsys):
    _write(
        repo / "shop/models/schema.yml",
        "models:\n  - name: rpt_orders\n    columns:\n      - name: region_label\n",
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    found = [
        c
        for c in _records(planned)["rpt_orders"]["checks"]
        if c["check"] == "removed_column_still_read"
    ]
    assert found
    assert "models/schema.yml" in found[0]["detail"]
    # The model's own SELECT never counts: that is what `outputs` judges.
    assert "rpt_orders.sql" not in found[0]["detail"]


def test_a_stale_plan_is_not_compared_and_a_conflict_is_not_overwritten(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    plan_id = planned["data"]["plan_id"]
    _write(repo / "shop/models/rpt_empty.sql", EMPTY + "-- a human edit\n")
    compared = _run(
        repo, capsys, "transform", "compare", plan_id, "--model", "rpt_orders"
    )
    assert compared["data"]["reason_code"] == "plan_stale"
    applied = _run(repo, capsys, "transform", "apply", plan_id)
    assert applied["status"] == "needs_confirmation"
    # Atomic: the conflict withheld every file.
    assert HAS_FLAG in (repo / "shop/models/rpt_orders.sql").read_text()
    assert FLAG in (repo / "shop/dbt_project.yml").read_text()


def test_a_project_scoped_definition_left_behind_is_named(repo, capsys):
    project = repo / "shop/dbt_project.yml"
    text = project.read_text(encoding="utf-8").replace(
        f"vars:\n  {FLAG}: true\n", f"vars:\n  shop:\n    {FLAG}: true\n"
    )
    project.write_text(text, encoding="utf-8")
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    assert planned["data"]["retirement"]["project_scoped_definition_left"] is True
    assert any("own `vars:` scope" in w for w in planned["warnings"])


def test_the_contract_travels_in_the_plan_document(repo, capsys):
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    plan_id = planned["data"]["plan_id"]
    exported = _run(repo, capsys, "transform", "export", plan_id)["data"]
    document = exported.get("plan") or exported
    assert document["retirement"]["retained_value"] is False

    from exmergo_dex_core.transform.portable import plan_digest

    honest = plan_digest(document)
    swapped = json.loads(json.dumps(document))
    swapped["retirement"]["retained_value"] = True
    assert plan_digest(swapped) != honest
    without = {k: v for k, v in document.items() if k != "retirement"}
    assert plan_digest(without) != honest


def test_a_star_over_an_untouched_model_and_unprofiled_source(repo, capsys):
    """a star over an untouched model is resolved; an unprofiled source stays
    unchecked naming explore profile."""

    _write(
        repo / "shop/models/rpt_wide.sql",
        "select * from {{ ref('stg_orders') }}\n",
    )
    _write(
        repo / "shop/models/sources.yml",
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: events\n",
    )
    _write(
        repo / "shop/models/rpt_events.sql",
        "select * from {{ source('raw', 'events') }}\n",
    )
    planned = _remove(
        repo,
        capsys,
        {
            **RETIRE_SQL,
            "rpt_wide": "select * from {{ ref('stg_orders') }}\n",
            "rpt_events": "select * from {{ source('raw', 'events') }}\n",
        },
        {
            **RETIRE,
            "outputs": {
                **RETIRE["outputs"],
                "rpt_wide": {
                    "expected": ["order_id", "region_id", "amount"],
                    "removed": [],
                },
                "rpt_events": {
                    "expected": ["event_id"],
                    "removed": [],
                },
            },
        },
    )
    records = _records(planned)
    assert records["rpt_wide"]["status"] == "satisfied"
    assert not any(
        c["check"] == "unreadable_select" for c in records["rpt_wide"]["checks"]
    )
    assert records["rpt_events"]["status"] == "unchecked"
    assert records["rpt_events"]["reason_code"] == "unreadable_select"
    assert "dex explore profile raw.events" in records["rpt_events"]["reason"]


# --- column-parse limits alone run the full contract (#1172) -----------------

#: A conditional group-by count: blanking the Jinja keeps both branches
#: (``group by 1, 2 1``), so the source scan cannot parse this file, while its
#: compiled SQL is plain. It reads no var, so only its columns are in doubt.
UNPARSED = (
    "select {cols}, count(*) as n\nfrom {{{{ ref('rpt_orders') }}}}\n"
    "group by {{% if target.name == 'dev' %}}{full}{{% else %}}1{{% endif %}}\n"
)


def test_column_parse_limits_alone_plan_the_full_contract(repo, capsys):
    """Judged from compiled SQL where it compiles; unchecked only where not."""

    shop = repo / "shop"
    _write(shop / "models/rpt_by_order.sql", UNPARSED.format(cols="order_id", full="1"))
    _write(
        shop / "models/rpt_disabled_reader.sql",
        "{{ config(enabled=false) }}\n" + UNPARSED.format(cols="order_id", full="1"),
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    assert planned["status"] == "ok", planned["errors"]
    retirement = planned["data"]["retirement"]
    assert "discovery" not in retirement
    reads = retirement["column_reads"]
    assert reads["read_from_compiled"] == ["models/rpt_by_order.sql"]
    assert reads["coverage"] == "partial"
    assert reads["unchecked"] == ["models/rpt_disabled_reader.sql"]
    assert retirement["verdict"] == "unchecked"
    assert any(
        "models/rpt_disabled_reader.sql" in w and "unchecked" in w
        for w in planned["warnings"]
    )
    # The build owes the unread file.
    built = _build_verify(repo, capsys, planned["data"]["plan_id"])
    records = built["data"]["verification"]["retirement"]
    (owed,) = [r for r in records if r.get("check") == "removed_column_reads"]
    assert owed["status"] == "unchecked"
    assert owed["unchecked"] == ["models/rpt_disabled_reader.sql"]


def test_a_removed_column_read_where_the_source_does_not_parse_is_caught(repo, capsys):
    """The compiled SQL finds the read the jinja-blanked scan could not."""

    _write(
        repo / "shop/models/rpt_by_region.sql",
        UNPARSED.format(cols="order_id, region_label", full="1, 2"),
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    assert planned["status"] == "ok", planned["errors"]
    retirement = planned["data"]["retirement"]
    assert retirement["column_reads"]["coverage"] == "complete"
    assert retirement["verdict"] == "contradicted"
    checks = _records(planned)["rpt_orders"]["checks"]
    (still,) = [c for c in checks if c["check"] == "removed_column_still_read"]
    assert "models/rpt_by_region.sql" in still["detail"]


def test_a_rename_still_refuses_on_a_column_parse_limit(repo, capsys):
    _write(
        repo / "shop/models/rpt_by_order.sql",
        UNPARSED.format(cols="order_id", full="1"),
    )
    refused = _run(repo, capsys, "transform", "rename", "var", FLAG, "use_regions")
    assert refused["status"] == "error"
    assert "could not parse the SQL for columns" in " ".join(refused["errors"])


def test_an_unparsed_model_outside_the_lineage_leaves_coverage_complete(repo, capsys):
    """The proposal compiles the declared models and their descendants only, so
    an unrelated model has no compiled SQL; it cannot read a removed column, and
    is not counted as unchecked (CodeRabbit on #1192)."""

    _write(
        repo / "shop/models/rpt_unrelated.sql",
        "select order_id, count(*) as n\nfrom {{ ref('stg_orders') }}\n"
        "group by {% if target.name == 'dev' %}1{% else %}1, 1{% endif %}\n",
    )
    planned = _remove(repo, capsys, RETIRE_SQL, RETIRE)
    assert planned["status"] == "ok", planned["errors"]
    reads = planned["data"]["retirement"]["column_reads"]
    assert reads["coverage"] == "complete", reads
    assert "models/rpt_unrelated.sql" not in reads["read_from_compiled"]
