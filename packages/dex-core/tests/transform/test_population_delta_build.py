"""A model with no declared parent is measured against a drafted one (#1228).

The three shapes the issue names, each from plan to `build --verify` on real
dbt over DuckDB, with no `population.parent` anywhere:

- an aggregate driven from the child table keeps a deleted parent's children
  (an orphan key) and loses a parent with no children: drafted parent, measured
  orphans and missing parents, a `high` finding that asks;
- the same aggregate driven from the parent keeps a parent with no children as
  a zero: no orphans, no missing parent, rows with no child counted, and the
  question settled by an assumption at `request` tier;
- an extraction refactor that moves the aggregate into an intermediate with an
  inner join loses the parents with no children there: a `medium` finding on
  the intermediate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

pytest.importorskip("dbt.adapters.duckdb")

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
"""

FROM_PARTS = """
with parts as (select * from {{ ref('stg_parts') }})
select conversation_id, count(*) as part_count
from parts
group by conversation_id
"""

FROM_PARENT = """
with parts as (
    select conversation_id, count(*) as part_count
    from {{ ref('stg_parts') }}
    group by conversation_id
)
select c.conversation_id, coalesce(p.part_count, 0) as part_count
from {{ ref('stg_conversations') }} as c
left join parts as p on p.conversation_id = c.conversation_id
"""

EXTRACTED = """
select c.conversation_id, count(*) as part_count
from {{ ref('stg_conversations') }} as c
inner join {{ ref('stg_parts') }} as p on p.conversation_id = c.conversation_id
group by c.conversation_id
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A DuckDB project with conversations 1-4 and parts of 1, 2, 3 and the
    deleted 9. Conversation 4 has no parts."""

    root = tmp_path / "proj"
    for folder in ("models/staging", "models/marts", "seeds", ".dex", ".git"):
        (root / folder).mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        "name: proj\nversion: '1.0'\nconfig-version: 2\nprofile: proj\n"
        "model-paths: [models]\nseed-paths: [seeds]\n"
        "models:\n  proj:\n    +materialized: table\n"
    )
    (root / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ./dev.duckdb\n"
    )
    (root / "seeds" / "raw_conversations.csv").write_text(
        "conversation_id,state\n1,open\n2,closed\n3,open\n4,open\n"
    )
    (root / "seeds" / "raw_parts.csv").write_text(
        "part_id,conversation_id\n1,1\n2,1\n3,2\n4,3\n5,9\n"
    )
    (root / "models" / "staging" / "_staging.yml").write_text(STAGING_YAML)
    (root / "models" / "staging" / "stg_conversations.sql").write_text(
        "select conversation_id, state from {{ ref('raw_conversations') }}\n"
    )
    (root / "models" / "staging" / "stg_parts.sql").write_text(
        "select part_id, conversation_id from {{ ref('raw_parts') }}\n"
    )
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: dev.duckdb\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


def _dex(capsys, *argv: str) -> dict:
    main(list(argv))
    return json.loads(capsys.readouterr().out)


def _plan(capsys, root: Path, edits: list[tuple[str, str]], declarations=None):
    payload: dict = {
        "edits": [
            {"path": path, "kind": "model_sql", "content": sql} for path, sql in edits
        ]
    }
    if declarations:
        payload["declarations"] = declarations
    path = root / "edits.json"
    path.write_text(json.dumps(payload))
    planned = _dex(capsys, "transform", "plan", "change", "--edits-file", str(path))
    assert planned["status"] == "ok", planned["errors"]
    return planned


def _apply_and_build(capsys, planned: dict) -> dict:
    plan_id = planned["data"]["plan_id"]
    assert _dex(capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _dex(
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan",
        plan_id,
    )
    assert built["status"] == "ok", (built["errors"], built["warnings"])
    assert built["data"]["verification"]["ran"], built["data"]["verification"]
    return built


def _delta(built: dict, model: str) -> dict:
    [record] = [
        r
        for r in built["data"]["verification"].get("population_delta") or []
        if r["model"] == model
    ]
    return record


def _baseline(capsys) -> None:
    assert _dex(capsys, "transform", "build", "--target", "dev")["status"] == "ok"


def test_orphan_child_keys_and_a_dropped_parent_are_flagged(project, capsys):
    _baseline(capsys)
    planned = _plan(
        capsys, project, [("models/marts/conversation_summary.sql", FROM_PARTS)]
    )
    drafted = planned["data"]["declarations"]["conversation_summary"]["drafted"]
    assert drafted["population_parent"]["value"] == {
        "model": "stg_conversations",
        "key": {"conversation_id": "conversation_id"},
    }
    assert drafted["population_parent"]["drives_from_parent"] is False

    built = _apply_and_build(capsys, planned)
    record = _delta(built, "conversation_summary")
    assert record["status"] == "needs_decision"
    assert record["drafted"]["parent"] == "stg_conversations"
    assert record["measured"]["orphan_keys"] == 1
    assert record["measured"]["missing_parents"] == 1
    assert record["asks"] == ["orphan_keys", "missing_parents"]
    [finding] = [
        f
        for f in built["data"]["verification"]["findings"]
        if f["code"] == "population_delta"
    ]
    assert finding["severity"] == "high"
    # Counts and key names only: no key value reaches the envelope.
    assert "9" not in finding["detail"].replace("conversation_id", "")
    obligations = built["data"]["completion"]["obligations"]
    assert [o["model"] for o in obligations["needs_decision"]] == [
        "conversation_summary"
    ]
    # Never a suppressed class, whatever else was suppressed.
    assert "population_delta" not in built["data"]["verification"]["suppressed"]


def test_zero_child_parents_ask_and_an_assumption_at_request_tier_settles(
    project, capsys
):
    _baseline(capsys)
    planned = _plan(
        capsys, project, [("models/marts/conversation_summary.sql", FROM_PARENT)]
    )
    assert (
        planned["data"]["declarations"]["conversation_summary"]["drafted"][
            "population_parent"
        ]["drives_from_parent"]
        is True
    )
    built = _apply_and_build(capsys, planned)
    record = _delta(built, "conversation_summary")
    assert record["status"] == "needs_decision"
    assert record["measured"]["orphan_keys"] == 0
    assert record["measured"]["missing_parents"] == 0
    assert record["measured"]["empty_rows"] == 1
    assert record["measured"]["empty_columns"] == ["part_count"]
    assert record["asks"] == ["empty_rows"]
    [finding] = [
        f
        for f in built["data"]["verification"]["findings"]
        if f["code"] == "population_delta"
    ]
    assert finding["severity"] == "low"

    settled = _plan(
        capsys,
        project,
        [("models/marts/conversation_summary.sql", FROM_PARENT)],
        declarations=[
            {
                "model": "conversation_summary",
                "description": "One row per conversation.",
                "assumptions": [
                    {
                        "decision": "Whether conversations with no parts belong",
                        "chosen": "every conversation, with zero parts counted",
                        "evidence": "request",
                        "column": "conversation_id",
                    }
                ],
            }
        ],
    )
    built = _apply_and_build(capsys, settled)
    record = _delta(built, "conversation_summary")
    assert record["status"] == "settled"
    assert record["settled_by"]["evidence"] == "request"
    assert not [
        f
        for f in built["data"]["verification"]["findings"]
        if f["code"] == "population_delta"
    ]
    assert "needs_decision" not in built["data"]["completion"]["obligations"]


def test_an_extraction_that_drops_parents_is_flagged(project, capsys):
    _baseline(capsys)
    built = _apply_and_build(
        capsys,
        _plan(
            capsys, project, [("models/marts/conversation_summary.sql", FROM_PARENT)]
        ),
    )
    assert _delta(built, "conversation_summary")["measured"]["missing_parents"] == 0

    planned = _plan(
        capsys,
        project,
        [
            ("models/marts/int_conversation_parts.sql", EXTRACTED),
            (
                "models/marts/conversation_summary.sql",
                "select conversation_id, part_count "
                "from {{ ref('int_conversation_parts') }}\n",
            ),
        ],
    )
    built = _apply_and_build(capsys, planned)
    record = _delta(built, "int_conversation_parts")
    assert record["status"] == "needs_decision"
    assert record["measured"]["missing_parents"] == 1
    assert record["measured"]["orphan_keys"] == 0
    severities = {
        f["identifier"]: f["severity"]
        for f in built["data"]["verification"]["findings"]
        if f["code"] == "population_delta"
    }
    assert severities["int_conversation_parts"] == "medium"


def _redriven(planned: dict, model: str) -> list[dict]:
    scope = planned["data"].get("change_scope") or {}
    return [
        change
        for entry in scope.get("models") or []
        if entry["model"] == model
        for change in entry["changes"]
        if change["kind"] == "redriven"
    ]


def test_redriving_an_applied_model_needs_a_request_or_contract(project, capsys):
    _baseline(capsys)
    _apply_and_build(
        capsys,
        _plan(
            capsys, project, [("models/marts/conversation_summary.sql", FROM_PARENT)]
        ),
    )

    planned = _plan(
        capsys, project, [("models/marts/conversation_summary.sql", FROM_PARTS)]
    )
    [change] = _redriven(planned, "conversation_summary")
    assert change["status"] == "undeclared"
    assert (change["prior"], change["authored"]) == ("stg_conversations", "stg_parts")
    assert (
        "conversation_summary"
        in planned["data"]["change_scope"]["population"]["changed"]
    )
    built = _apply_and_build(capsys, planned)
    scope = built["data"]["completion"]["obligations"]["change_scope"]
    assert {"model": "conversation_summary", "change": "redriven"}.items() <= next(
        o for o in scope if o.get("change") == "redriven"
    ).items()


def test_redriving_a_model_an_earlier_plan_wrote_is_flagged(project, capsys):
    _baseline(capsys)
    # Planned from the parent, never applied: the session's earlier answer.
    _plan(capsys, project, [("models/marts/conversation_summary.sql", FROM_PARENT)])
    planned = _plan(
        capsys, project, [("models/marts/conversation_summary.sql", FROM_PARTS)]
    )
    [change] = _redriven(planned, "conversation_summary")
    assert change["status"] == "undeclared"
    entry = next(
        e
        for e in planned["data"]["change_scope"]["models"]
        if e["model"] == "conversation_summary"
    )
    assert entry["against"] == "earlier_plan"
