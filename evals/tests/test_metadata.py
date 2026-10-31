"""The metadata-quality eval's scorer and fixtures (#492), with no agent and no spend.

Needs DuckDB, PyYAML and dbt, so it runs in the engine's environment and skips
under the stdlib-only ``uvx pytest evals``:

    uv run --project packages/dex-core --extra duckdb pytest evals/tests/test_metadata.py

What is held:

- every fixture's data is pinned by a digest, so a change to a generator is a
  reviewed change to the ground truth;
- every task's reference answer scores 1.0 on every axis and passes the gate,
  which is the scorer's own self-test;
- the gate fails a duplicate YAML entry and YAML dbt cannot parse, and a gate
  failure zeroes the task;
- a workspace the agent did not touch scores zero, never a vacuous perfect;
- fluent prose cannot buy back a wrong population, and a missing unit lowers
  completeness while the SQL still builds;
- a correct declaration on wrong SQL still fails the data checks.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
yaml = pytest.importorskip("yaml")

from evals.metadata.fixtures import (  # noqa: E402
    find_tasks,
    fixture_of,
    load_fixtures,
    materialize,
    row_digest,
)
from evals.metadata.score import score_workspace  # noqa: E402

try:
    from evals.metadata.score import _dbt

    _dbt()
    HAS_DBT = True
except RuntimeError:
    HAS_DBT = False

needs_dbt = pytest.mark.skipif(not HAS_DBT, reason="dbt is not installed here")

FIXTURES = load_fixtures()
TASKS = [t for fx in FIXTURES for t in fx.tasks]

# Change only together with the generator, as a reviewed change to the truth.
DIGESTS = {
    "composite_grain": "7b7f2462179e134a86ca10c8ccd319d8d438852c8db81d68253df762cdfd0cc0",
    "parent_zero_children": "4030686f817d116cd05d6d546e194c0cdf0877cbcab245099b12215a502d7279",
    "revenue_population": "4d6e3afd0006ac59335b952d0f8fd2bbf57dfd713ebbbde50fb3eda2e011ab42",
    "season_to_date": "b6823415982716617f2b66134364bb8b0ebadcc3fcca7eeeaf5d682033049794",
    "share": "749c9be906d4bd3df2cbe7a83bc7bc66689ed82ba9c3f298ebcaca64335bd558",
}


def _task(key: str):
    return find_tasks(FIXTURES, fixture=None, task=[key])[0]


def _reference(tmp_path: Path, key: str) -> tuple:
    task = _task(key)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws", reference=True)
    return task, fixture, workspace


def _edit_yaml(path: Path, edit) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_the_suite_covers_the_issues_five_cases_with_build_and_fix_tasks():
    assert {fx.name for fx in FIXTURES} == set(DIGESTS)
    assert len(TASKS) >= 7
    assert {t.kind for t in TASKS} == {"build", "fix"}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fx: fx.name)
def test_fixture_data_is_pinned(fixture):
    first, second = fixture.tables(), fixture.tables()
    assert row_digest(first) == row_digest(second), "the generator is deterministic"
    assert row_digest(first) == DIGESTS[fixture.name], (
        f"{fixture.name}'s data changed; if deliberate, update DIGESTS and re-check "
        "every truth.json that depends on it"
    )


@needs_dbt
@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.key)
def test_every_reference_answer_scores_perfect(task, tmp_path):
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws", reference=True)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored", score.gate
    assert (score.completeness, score.correctness, score.checks_passed) == (1, 1, 1), (
        score.to_dict()
    )


def test_an_untouched_workspace_scores_zero_not_perfect(tmp_path):
    task = _task("revenue_population/build_daily_revenue")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    score = score_workspace(task, fixture, workspace)
    assert score.status == "no_models"
    assert (score.completeness, score.correctness, score.checks_passed) == (0, 0, 0)


@needs_dbt
def test_two_yaml_entries_for_one_model_fail_the_gate(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "revenue_population/build_daily_revenue"
    )
    shutil.copyfile(
        workspace / "models/marts/daily_revenue.yml",
        workspace / "models/marts/daily_revenue_again.yml",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed" and score.gate["stage"] == "duplicates"
    assert score.completeness == score.correctness == 0


@needs_dbt
def test_yaml_dbt_cannot_parse_fails_the_gate(tmp_path):
    """A declaration that breaks the parse fails the task outright. (A test that
    names a missing model does not: dbt 1.12 warns and disables that test, so
    the gate cannot be used to catch it, and the agent's build is what does.)"""

    task, fixture, workspace = _reference(
        tmp_path, "parent_zero_children/build_account_summary"
    )
    path = workspace / "models/marts/account_ticket_summary.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("columns:", "columns:\n  - [", 1),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed"
    assert score.gate["stage"] == "parse"
    assert score.completeness == score.correctness == 0


@needs_dbt
def test_fluent_prose_cannot_buy_back_a_wrong_population(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "revenue_population/build_daily_revenue"
    )

    def wrong(document):
        dex = document["models"][0]["config"]["meta"]["dex"]
        dex["population"]["filters"] = [{"column": "status", "excludes": ["cancelled"]}]
        document["models"][0]["description"] = (
            "A meticulously reconciled daily revenue ledger covering every kept order."
        )

    _edit_yaml(workspace / "models/marts/daily_revenue.yml", wrong)
    score = score_workspace(task, fixture, workspace)
    assert score.completeness == 1
    assert score.models["daily_revenue"]["correctness"]["population"] is False
    assert score.correctness < 1


@needs_dbt
def test_an_includes_filter_scores_the_same_as_the_excludes_it_implies(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "revenue_population/build_daily_revenue"
    )

    def includes(document):
        dex = document["models"][0]["config"]["meta"]["dex"]
        dex["population"]["filters"] = [
            {"column": "status", "includes": ["placed", "shipped", "completed"]}
        ]

    _edit_yaml(workspace / "models/marts/daily_revenue.yml", includes)
    assert score_workspace(task, fixture, workspace).correctness == 1


@needs_dbt
def test_a_missing_measure_unit_lowers_completeness_while_the_sql_builds(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "share/build_product_share")

    def drop_unit(document):
        for column in document["models"][0]["columns"]:
            if column["name"] == "share_of_region":
                del column["config"]["meta"]["dex"]["unit"]

    _edit_yaml(workspace / "models/marts/product_region_share.yml", drop_unit)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored"
    assert (
        "unit:share_of_region"
        in score.models["product_region_share"]["completeness"]["missing"]
    )
    assert score.completeness < 1


@needs_dbt
def test_a_right_declaration_on_wrong_sql_still_fails_the_checks(tmp_path):
    """The fix task's starting SQL (driven from tickets) with the reference's
    declaration: the YAML is perfect and the rows are wrong."""

    task, fixture, workspace = _reference(
        tmp_path, "parent_zero_children/fix_account_summary"
    )
    shutil.copyfile(
        task.directory / "setup/models/marts/account_ticket_summary.sql",
        workspace / "models/marts/account_ticket_summary.sql",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored"
    assert score.completeness == 1 and score.correctness == 1
    assert score.checks_passed < 1


def test_the_transcript_summary_counts_dex_commands_on_every_route():
    """The CLI, the engine module and the skill wrapper (also through a shell
    variable) all count, and only for real Dex groups."""

    from evals.metadata.live import REPO, Session, summarize

    def bash(command):
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": command}}
                ]
            },
        }

    events = [
        {
            "type": "system",
            "subtype": "init",
            "plugins": [{"name": "dex", "path": str(REPO)}],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "dex:explore"},
                    }
                ]
            },
        },
        bash("dex explore profile orders"),
        bash(
            'R=/x/run.py; uv run --no-project --script $R transform plan "i" --edits-file e'
        ),
        bash('uv run --no-project --script "/x/run.py" maintain verify'),
        bash("python -m exmergo_dex_core --repo-root . transform build --target dev"),
        bash("ls models; uv run pytest"),
        {"type": "result", "total_cost_usd": 0.5, "num_turns": 7},
    ]
    session = summarize(events, Session())
    assert session.skills_triggered == ["dex:explore"]
    assert session.dex_commands == {
        "explore profile": 1,
        "transform plan": 1,
        "maintain verify": 1,
        "transform build": 1,
    }
    assert session.candidate_plugin_loaded is True
    assert (session.cost_usd, session.turns) == (0.5, 7)


def test_a_model_that_was_not_built_cannot_be_complete():
    from evals.metadata.score import Declared, completeness

    declared_only = Declared(
        description="d",
        grain=["id"],
        population={"rule": "all"},
        assumptions=[{"evidence": "default"}],
    )
    result = completeness(declared_only, None)
    assert "built_relation" in result["missing"]
    assert result["score"] < 1


def test_a_dbt_stage_that_hangs_fails_the_gate(tmp_path, monkeypatch):
    import subprocess

    from evals.metadata import score

    _task, fixture, workspace = _reference(
        tmp_path, "revenue_population/build_daily_revenue"
    )

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(score, "_dbt", lambda: "dbt")
    monkeypatch.setattr(score.subprocess, "run", hang)
    gate = score.build_gate(workspace, fixture, tmp_path / "built")
    assert (gate.passed, gate.stage) == (False, "parse")
    assert "timed out" in gate.detail[0]


def test_a_failed_process_stays_failed_whatever_its_result_event_says(
    tmp_path, monkeypatch
):
    import json
    import subprocess

    from evals.metadata import live

    result = {
        "type": "result",
        "is_error": False,
        "total_cost_usd": 0.2,
        "num_turns": 3,
    }

    def exits_nonzero(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, json.dumps(result) + "\n", "boom")

    monkeypatch.setattr(live, "_require_claude", lambda binary: binary)
    monkeypatch.setattr(live.subprocess, "run", exits_nonzero)
    session = live.run_session(
        "p",
        tmp_path,
        tmp_path / "t.jsonl",
        model="m",
        max_turns=1,
        max_budget_usd=1,
        timeout=5,
    )
    assert session.is_error is True and session.error == "boom"
    assert (session.cost_usd, session.turns) == (0.2, 3)
