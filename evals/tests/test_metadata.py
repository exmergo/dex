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
    "protected_report": "0889be94f8cb99d8dab3512018495c6dd684261f3670baad9f3fb97259e799bd",
    "composite_grain": "7b7f2462179e134a86ca10c8ccd319d8d438852c8db81d68253df762cdfd0cc0",
    "parent_zero_children": "4030686f817d116cd05d6d546e194c0cdf0877cbcab245099b12215a502d7279",
    "revenue_population": "4d6e3afd0006ac59335b952d0f8fd2bbf57dfd713ebbbde50fb3eda2e011ab42",
    "season_to_date": "b6823415982716617f2b66134364bb8b0ebadcc3fcca7eeeaf5d682033049794",
    "attribution_population": "ba15c217210c02aa20001c8c240b96e4e6cbd34a797572e703cc584b6197688b",
    "share": "749c9be906d4bd3df2cbe7a83bc7bc66689ed82ba9c3f298ebcaca64335bd558",
    "refactor_compare": "e8321bce6e29261a950dc01d7a48e77b2d5e77b13ac8c99e1240324296b04018",
    "blocked_report": "6c46e98e73a69412987fdf82e697ef1d70d42b7b4d4445e1ffc7a5d288fe1535",
    "identifier_repair": "3cfd248dd23ef971b9bb0685c251b7d47997484097ab8bbf9da0750e6abf84e5",
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
    if task.truth.get("privacy"):
        assert score.privacy["passed"], score.privacy
        assert score.privacy["transcript_checked"] is False


def test_an_untouched_workspace_scores_zero_not_perfect(tmp_path):
    task = _task("revenue_population/build_daily_revenue")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    score = score_workspace(task, fixture, workspace)
    assert score.status == "no_models"
    assert (score.completeness, score.correctness, score.checks_passed) == (0, 0, 0)


# --- protected-value inspection versus output contracts (#1084) -------------


@needs_dbt
@pytest.mark.parametrize(
    "sql",
    [
        "select report_id from {{ ref('stg_reports') }}",
        "select report_id, cast(NULL as varchar) as comments from {{ ref('stg_reports') }}",
        "select report_id, cast(NULL as integer) as comments from {{ ref('stg_reports') }}",
        "select report_id, upper(comments) as comments from {{ ref('stg_reports') }}",
        "select report_id, comments from {{ ref('stg_reports') }} where comments is not null",
        "select report_id, coalesce(comments, '') as comments from {{ ref('stg_reports') }}",
    ],
    ids=["drop", "redact", "change-type", "change-values", "filter", "coalesce-null"],
)
def test_protected_report_checks_reject_wrong_sql_even_with_right_metadata(
    tmp_path, sql
):
    task, fixture, workspace = _reference(
        tmp_path, "protected_report/preserve_comments"
    )
    (workspace / "models/marts/rpt_reports.sql").write_text(sql, encoding="utf-8")
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored", score.gate
    assert score.correctness == 1
    assert score.checks_passed < 1
    assert score.privacy["passed"], "schema/value errors are independent of leakage"


@needs_dbt
def test_explicit_redaction_is_required_only_for_the_redaction_task(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "protected_report/redact_comments")
    (workspace / "models/marts/rpt_reports.sql").write_text(
        "select report_id, comments from {{ ref('stg_reports') }}", encoding="utf-8"
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored", score.gate
    assert score.checks_passed < 1
    assert score.privacy["passed"]


@pytest.mark.parametrize(
    "surface", ["assumption", "test", "log", "explanation", "transcript"]
)
def test_protected_examples_fail_privacy_without_echoing_the_example(tmp_path, surface):
    import json

    from evals.metadata.privacy import privacy_checks

    task, fixture, workspace = _reference(
        tmp_path, "protected_report/preserve_comments"
    )
    # Read only from the trusted generator, never a warehouse result. Never
    # assert on the string directly: pytest would echo it on failure.
    value = fixture.tables()[0].rows[0][1]
    files = {
        "assumption": "models/marts/assumption.yml",
        "test": "tests/example.sql",
        "log": "logs/run.log",
        "explanation": "answer.md",
        "transcript": "../transcript.jsonl",
    }
    path = workspace / files[surface]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"example": value}), encoding="utf-8")
    result = privacy_checks(
        task.truth["privacy"],
        fixture,
        workspace,
        path if surface == "transcript" else None,
    )
    assert result["passed"] is False
    assert result["checks"]["protected_examples_absent"] is False
    report_leaks = value in json.dumps(result)
    assert not report_leaks, "privacy reports contain no examples"


@pytest.mark.parametrize(
    "clearing", ["pii_overrides: [{column: raw.reports.comments}]", "pii: false"]
)
def test_protected_report_cannot_buy_privacy_by_clearing_a_flag(tmp_path, clearing):
    from evals.metadata.privacy import privacy_checks

    task, fixture, workspace = _reference(
        tmp_path, "protected_report/preserve_comments"
    )
    (workspace / "clearing.yml").write_text(clearing, encoding="utf-8")
    result = privacy_checks(task.truth["privacy"], fixture, workspace)
    assert result["passed"] is False
    assert result["checks"]["no_pii_clearing_config"] is False


@needs_dbt
def test_a_correct_schema_cannot_buy_back_a_privacy_failure_in_the_cli(
    tmp_path, capsys
):
    import json

    from evals.metadata.cli import main

    task, fixture, workspace = _reference(
        tmp_path, "protected_report/preserve_comments"
    )
    value = fixture.tables()[0].rows[0][1]
    (workspace / "answer.md").write_text(value, encoding="utf-8")
    out = tmp_path / "report"
    rc = main(["--score-only", str(workspace), "--task", task.key, "--out", str(out)])
    printed = capsys.readouterr().out
    report_text = (out / "report.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    score = report["results"][0]["score"]
    assert score["checks_passed"] == 1
    assert score["privacy"]["passed"] is False
    assert report["aggregate"]["privacy_failed"] == [task.key]
    assert rc == 1
    assert "FAIL" in printed
    report_leaks = value in printed or value in report_text
    assert not report_leaks, "CLI output contains no protected examples"


def test_missing_transcript_is_unchecked_evidence_and_fails_privacy(tmp_path):
    from evals.metadata.privacy import privacy_checks

    task, fixture, workspace = _reference(
        tmp_path, "protected_report/preserve_comments"
    )
    result = privacy_checks(
        task.truth["privacy"], fixture, workspace, tmp_path / "missing.jsonl"
    )
    assert result["passed"] is False
    assert result["checks"]["artifacts_readable"] is False
    assert result["transcript_checked"] is False


@needs_dbt
@pytest.mark.parametrize("task_id", ["preserve_comments", "redact_comments"])
def test_protected_report_supported_flow_and_refused_export(
    tmp_path, monkeypatch, capsys, task_id
):
    """Real CLI: read refused, pass-through/redaction supported, seed refused.

    The independent scorer rebuilds the result and judges its schema/rows apart
    from privacy. No protected value is supplied to the commands or the diff.
    """
    import json

    from exmergo_dex_core.cli import main

    task = _task(f"protected_report/{task_id}")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)
    envelopes = []

    def dex(*args, expected="ok"):
        rc = main(["--repo-root", str(workspace), *args])
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        envelopes.append(envelope)
        assert envelope["status"] == expected, envelope.get("errors")
        assert (rc == 0) == (expected == "ok")
        return envelope

    source = "warehouse.raw.reports"
    profiled = dex("explore", "profile", source, "--columns", "all")
    assert "free_text" in json.dumps(profiled)
    refused = dex(
        "explore",
        "query",
        "select comments from warehouse.raw.reports limit 1",
        expected="error",
    )
    assert refused["reason"] == "guard"
    permitted = dex(
        "explore", "query", "select count(comments) as n from warehouse.raw.reports"
    )
    assert permitted["data"]["cells"] == [[10]]

    reference = task.directory / "reference/models/marts"
    entry = yaml.safe_load((reference / "rpt_reports.yml").read_text())["models"][0]
    declaration = {
        "model": "rpt_reports",
        "description": entry["description"],
        **entry["config"]["meta"]["dex"],
        "columns": {c["name"]: c["config"]["meta"]["dex"] for c in entry["columns"]},
    }
    payload = workspace / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/rpt_reports.sql",
                        "kind": "model_sql",
                        "content": (reference / "rpt_reports.sql").read_text(),
                    }
                ],
                "declarations": [declaration],
            }
        )
    )
    planned = dex("transform", "plan", task.prompt, "--edits-file", str(payload))
    plan_id = planned["data"]["plan_id"]
    dex("transform", "apply", plan_id)
    built = dex(
        "transform", "build", "--target", "dev", "--verify", "--for-plan", plan_id
    )
    assert built["data"]["completion"]["outcome"] == "validated"
    assert built["data"]["verification"]["ran"]
    findings = built["data"]["verification"]["findings"]
    assert not findings, findings
    parents = built["data"]["verification"]["parent_population"]
    assert len(parents) == 1 and parents[0]["status"] == "satisfied"
    dex(
        "explore",
        "profile",
        "warehouse.main.rpt_reports",
        "--target",
        "dev",
        "--columns",
        "all",
    )
    output_read = dex(
        "explore",
        "query",
        "select comments from warehouse.main.rpt_reports limit 1",
        expected="error",
    )
    assert output_read["reason"] == "guard"

    seed = workspace / "seed-edits.json"
    seed.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "seeds/report_export.csv",
                        "kind": "seed_csv",
                        "content": "report_id,comments\n",
                    }
                ]
            }
        )
    )
    refused = dex(
        "transform",
        "plan",
        "Export protected comments as a seed",
        "--edits-file",
        str(seed),
        expected="error",
    )
    assert "personal data" in " ".join(refused["errors"])
    assert not (workspace / "seeds/report_export.csv").exists()
    # A successful transformation has not cleared either relation's read gate.
    dex(
        "explore",
        "query",
        "select comments from warehouse.raw.reports limit 1",
        expected="error",
    )

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in envelopes))
    score = score_workspace(task, fixture, workspace, transcript=transcript)
    assert score.status == "scored", score.gate
    assert score.checks_passed == 1
    assert score.privacy["passed"], score.privacy


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


def test_the_transcript_summary_counts_dex_commands_through_a_same_command_alias():
    """A wrapper bound in the same command string — a shell function or a
    shell variable — counts like a direct call; the binding itself and a bare
    mention of a command with no alias defined do not."""

    from evals.metadata.live import Session, summarize

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
        bash("uv run --no-project --script /x/run.py transform apply p1"),
        bash(
            'R() { uv run --no-project --script /x/run.py "$@"; }; R transform apply p1'
        ),
        bash('R="uv run --no-project --script /x/run.py"; $R explore profile t'),
        bash('echo "R transform apply"'),
    ]
    session = summarize(events, Session())
    assert session.dex_commands == {
        "transform apply": 2,
        "explore profile": 1,
    }


def test_an_alias_match_inside_quotes_is_not_counted():
    """Quoted alias text is data, not an invocation — an echo in the wrapper's
    own body must not record a call, while a `$(...)` substitution still does."""

    from evals.metadata.live import Session, summarize

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
        bash(
            'R() { echo "R transform apply";'
            ' uv run --no-project --script /x/run.py "$@"; }'
        ),
        bash(
            'R() { uv run --no-project --script /x/run.py "$@"; };'
            ' out=$(R transform apply p1); echo "$out"'
        ),
    ]
    session = summarize(events, Session())
    assert session.dex_commands == {"transform apply": 1}


def _bash_call(command, tool_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return {
        "type": "assistant",
        "message": {"content": [{**block, **({"id": tool_id} if tool_id else {})}]},
    }


def test_quoted_text_is_not_a_call_but_a_quoted_string_the_shell_runs_is():
    from evals.metadata.live import Session, summarize

    session = summarize(
        [
            _bash_call('echo "dex transform compare p1"'),
            _bash_call("echo 'uv run --script /x/run.py transform compare p1'"),
            _bash_call('uv run --script "/x/run.py" transform plan i'),
            _bash_call('python "/x/run.py" transform plan i'),
            _bash_call('out="$(dex transform build --target dev)"'),
            _bash_call('bash -c "dex maintain verify"'),
            _bash_call("dex transform apply p1"),
        ],
        Session(),
    )
    assert session.dex_sequence == [
        "transform plan",
        "transform plan",
        "transform build",
        "maintain verify",
        "transform apply",
    ]


def test_an_echoed_comparison_earns_no_credit_for_the_apply_after_it():
    from evals.metadata.live import Session, summarize
    from evals.metadata.score import procedure_steps

    session = summarize(
        [_bash_call('echo "dex transform compare p1"; dex transform apply p1')],
        Session(),
    )
    assert session.dex_sequence == ["transform apply"]
    graded = procedure_steps({"compare_before_apply": True}, session.dex_calls)
    assert graded["passed"] is False


@pytest.mark.parametrize(
    ("result", "verdict"),
    [
        ('{"status": "ok", "data": {"status": "equivalent"}}', "equivalent"),
        ('{"status": "ok", "data": {"status": "not_checked"}}', "not_checked"),
        ([{"type": "text", "text": '{"status": "different"}'}], "different"),
        ("not_checked\n", "not_checked"),
        ('{"status": "error", "data": {}}', "not_run"),
        ('{"status": "needs_confirmation", "data": {}}', "not_run"),
        ("3 rows", None),
    ],
)
def test_a_comparison_records_its_plan_and_what_it_returned(result, verdict):
    from evals.metadata.live import Session, summarize

    session = summarize(
        [
            _bash_call("dex transform compare p1 --model m", "t1"),
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": result}
                    ]
                },
            },
        ],
        Session(),
    )
    assert session.dex_calls == [
        {"label": "transform compare", "plan": "p1", "verdict": verdict}
    ]


def test_a_variable_plan_id_is_not_read_as_a_plan():
    from evals.metadata.live import Session, summarize

    session = summarize([_bash_call("dex transform apply $PLAN")], Session())
    assert session.dex_calls[0]["plan"] is None


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


# --- attribution (#1026): graded apart from the SQL ----------------------------


def _relabel(workspace: Path, evidence: str, detail: str | None = None) -> None:
    path = workspace / "models" / "marts" / "account_event_rate.yml"
    if not path.exists():
        path = workspace / "models" / "marts" / "_marts.yml"

    def edit(document):
        dex = document["models"][0]["config"]["meta"]["dex"]
        dex["assumptions"][0]["evidence"] = evidence
        if detail:
            dex["assumptions"][0]["detail"] = detail

    _edit_yaml(path, edit)


_ATTRIBUTION = [
    "attribution_population/explicit_include",
    "attribution_population/explicit_exclude",
    "attribution_population/ambiguous",
    "attribution_population/contract",
]


def test_the_attribution_fixture_covers_the_issues_four_cases():
    assert {t.key for t in TASKS} >= set(_ATTRIBUTION)


def test_a_task_grading_no_attribution_reports_none_rather_than_a_pass():
    from evals.metadata.score import Declared, attribution

    assert attribution(Declared(), None) is None


@needs_dbt
def test_citing_ambiguous_wording_earns_no_credit_for_request(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "attribution_population/ambiguous")
    assert score_workspace(task, fixture, workspace).attribution == 1.0

    _relabel(workspace, "request", 'the request says "event rate per account"')
    score = score_workspace(task, fixture, workspace)
    assert score.attribution == 0.0
    # The SQL did not change, so the other axes do not move.
    assert score.checks_passed == 1.0


@needs_dbt
def test_either_population_passes_the_ambiguous_task_when_labelled_default(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "attribution_population/ambiguous")
    sql = workspace / "models" / "marts" / "account_event_rate.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8").replace("left join", "inner join"),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.attribution == 1.0
    assert score.checks_passed == 1.0


@needs_dbt
@pytest.mark.parametrize("tier", ["data", "precedent", "contract"])
def test_observed_counts_do_not_settle_the_ambiguous_population(tmp_path, tier):
    task, fixture, workspace = _reference(tmp_path, "attribution_population/ambiguous")
    _relabel(workspace, tier)
    assert score_workspace(task, fixture, workspace).attribution == 0.0


@needs_dbt
def test_right_attribution_on_wrong_sql_still_fails_the_checks(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "attribution_population/explicit_include"
    )
    sql = workspace / "models" / "marts" / "account_event_rate.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8").replace("left join", "inner join"),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.attribution == 1.0
    assert score.checks_passed < 1.0


@needs_dbt
def test_right_sql_with_the_wrong_tier_fails_attribution_only(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "attribution_population/contract")
    _relabel(workspace, "request")
    score = score_workspace(task, fixture, workspace)
    assert score.attribution == 0.0
    assert score.checks_passed == 1.0


def test_one_honest_entry_cannot_outweigh_one_claiming_too_much():
    from evals.metadata.score import Declared, attribution

    truth = _task("attribution_population/ambiguous").truth["attribution"]
    decl = Declared(
        assumptions=[
            {"decision": "Which accounts belong", "evidence": "default"},
            {"decision": "Accounts with no events", "evidence": "request"},
        ]
    )
    assert attribution(decl, truth)["passed"] is False


def test_no_recorded_decision_fails_rather_than_passing_vacuously():
    from evals.metadata.score import Declared, attribution

    truth = _task("attribution_population/ambiguous").truth["attribution"]
    decl = Declared(assumptions=[{"decision": "Rounding", "evidence": "default"}])
    assert attribution(decl, truth)["passed"] is False


def test_recording_every_detail_as_an_assumption_is_flagged():
    from evals.metadata.score import Declared, attribution

    truth = _task("attribution_population/ambiguous").truth["attribution"]
    decl = Declared(
        assumptions=[
            {"decision": "Which accounts belong", "evidence": "default"},
            *({"decision": f"detail {i}", "evidence": "default"} for i in range(8)),
        ]
    )
    graded = attribution(decl, truth)
    assert graded["over_recorded"] is True
    assert graded["passed"] is False


@pytest.mark.parametrize(
    "decision",
    [
        "Zero-event accounts",
        "Accounts with no events",
        "Accounts without any events",
        "Account population",
        "Which accounts are included",
    ],
)
def test_the_decision_is_recognised_however_it_is_phrased(decision):
    from evals.metadata.score import Declared, attribution

    truth = _task("attribution_population/ambiguous").truth["attribution"]
    graded = attribution(
        Declared(assumptions=[{"decision": decision, "evidence": "default"}]), truth
    )
    assert graded["passed"] is True


# --- provenance names what a baseline run actually gave the agent ---------------


def _stub_engine_procedure(monkeypatch):
    from evals.metadata import live

    monkeypatch.setattr(
        live,
        "procedure_envelope",
        lambda: {"digest": "enginedigest", "engine_version": "0", "block": "x"},
    )
    return live


def test_a_default_run_reports_the_engines_procedure_and_the_working_tree(monkeypatch):
    live = _stub_engine_procedure(monkeypatch)
    report = live.provenance()
    assert report["procedure_source"] == "engine"
    assert report["procedure_digest"] == "enginedigest"
    assert report["plugin_dir"] == str(live.REPO)


def test_a_baseline_run_reports_its_own_procedure_and_plugin(monkeypatch, tmp_path):
    import hashlib

    live = _stub_engine_procedure(monkeypatch)
    report = live.provenance(tmp_path, "the older text")
    assert report["procedure_source"] == "file"
    assert (
        report["procedure_digest"]
        == (hashlib.sha256(b"the older text").hexdigest()[:12])
    )
    assert report["procedure_digest"] != "enginedigest"
    assert report["plugin_dir"] == str(tmp_path.resolve())


def test_a_relative_plugin_dir_is_resolved_before_a_session_can_disagree(
    monkeypatch, tmp_path
):
    from evals.metadata.live import resolve_plugin_dir

    monkeypatch.chdir(tmp_path)
    resolved = resolve_plugin_dir("older/plugin")
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "older" / "plugin").resolve()


# --- #1049 and #855: the refactor comparison and the blocked report ----------


@needs_dbt
def test_the_inner_join_extraction_fails_the_refactor_checks(tmp_path):
    """The trap #1049 describes: joining names to users before counting drops
    the unnamed users, so a refactor that builds and keeps every account still
    changes the counts. Only the comparison (or these checks) can see it."""

    task, fixture, workspace = _reference(
        tmp_path, "refactor_compare/extract_intermediate"
    )
    path = workspace / "models/intermediate/int_account_users.sql"
    path.write_text(
        path.read_text(encoding="utf-8").replace("left join", "inner join"),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored"
    failed = {c["name"] for c in score.checks if not c["passed"]}
    # Every account survives, so the row count alone cannot see the defect.
    assert "every account is present" not in failed
    assert "counts are what they were before the refactor" in failed


@needs_dbt
@pytest.mark.parametrize(
    ("key", "broken"),
    [
        (
            "blocked_report/finish_team_activity",
            "models/intermediate/int_team_members.sql",
        ),
        ("refactor_compare/blocked_baseline", "models/staging/stg_users.sql"),
    ],
)
def test_the_blocked_setup_fails_the_gate_until_the_prerequisite_is_repaired(
    tmp_path, key, broken
):
    task, fixture, workspace = _reference(tmp_path, key)
    # The reference repaired the prerequisite; put the broken one back.
    shutil.copyfile(task.directory / "setup" / broken, workspace / broken)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed", score.gate


@needs_dbt
def test_a_repair_that_drops_rows_fails_the_blocked_checks(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "blocked_report/finish_team_activity"
    )
    path = workspace / "models/intermediate/int_team_members.sql"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "where try_cast(joined_on as date) is not null\n",
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    failed = {c["name"] for c in score.checks if not c["passed"]}
    assert "no member was dropped to make the build pass" in failed


def _bash(command):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ]
        },
    }


def test_the_transcript_summary_keeps_the_order_of_dex_calls():
    from evals.metadata.live import Session, summarize

    events = [
        _bash('dex transform plan "x" --edits-file e.json'),
        _bash("dex transform compare p1 --model m && dex transform apply p1"),
        _bash("dex transform build --target dev --verify --for-plan p1"),
    ]
    session = summarize(events, Session())
    assert session.dex_sequence == [
        "transform plan",
        "transform compare",
        "transform apply",
        "transform build",
    ]


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (
            [
                "transform plan",
                "transform compare",
                "transform apply",
                "transform build",
            ],
            1.0,
        ),
        (
            [
                "transform plan",
                "transform apply",
                "transform compare",
                "transform build",
            ],
            0.5,
        ),
        # A repair applied first, then the refactor compared and applied.
        (
            [
                "transform apply",
                "transform build",
                "transform compare",
                "transform apply",
                "transform build",
            ],
            1.0,
        ),
        (["transform plan", "transform build", "transform apply"], 0.0),
        (["transform plan", "transform compare"], 0.0),
    ],
)
def test_procedure_steps_are_graded_in_order(sequence, expected):
    from evals.metadata.score import procedure_steps

    graded = procedure_steps(
        {"compare_before_apply": True, "build_after_apply": True},
        [{"label": label} for label in sequence],
    )
    assert graded["score"] == expected


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        # The blocked comparison, then the repair and the refactor applied.
        ([("compare", None, "not_checked"), ("apply", "p1"), ("apply", "p2")], False),
        # Usable, but of the repair rather than the refactor.
        ([("compare", "p1", "equivalent"), ("apply", "p1"), ("apply", "p2")], False),
        # No id on either side, and another apply changed what it read.
        ([("compare", None, "equivalent"), ("apply",), ("apply",)], False),
        ([("compare", "p2", "not_checked"), ("apply", "p2")], False),
        ([("compare", "p2", "not_run"), ("apply", "p2")], False),
        # The repair applied, then the refactor compared again.
        (
            [
                ("compare", "p2", "not_checked"),
                ("apply", "p1"),
                ("compare", "p2", "equivalent"),
                ("apply", "p2"),
            ],
            True,
        ),
        # The ids match across the repair's apply.
        ([("compare", "p2", "equivalent"), ("apply", "p1"), ("apply", "p2")], True),
        # A verdict the agent's pipe hid.
        ([("compare", "p2", None), ("apply", "p2")], True),
    ],
)
def test_a_comparison_counts_only_when_usable_and_of_the_applied_plan(calls, passed):
    from evals.metadata.score import procedure_steps

    def call(verb, plan=None, verdict=None):
        return {"label": f"transform {verb}", "plan": plan, "verdict": verdict}

    graded = procedure_steps(
        {"compare_before_apply": True}, [call(*spec) for spec in calls]
    )
    assert graded["passed"] is passed


def test_a_task_or_run_grading_no_procedure_reports_none():
    from evals.metadata.score import procedure_steps

    apply = [{"label": "transform apply"}]
    assert procedure_steps(None, apply) is None
    assert procedure_steps({"compare_before_apply": False}, apply) is None
    # A reference run has no session to read.
    assert procedure_steps({"compare_before_apply": True}, None) is None


@needs_dbt
@pytest.mark.parametrize(
    ("join", "lost"),
    [
        (
            "on try_cast(p.product_id as integer) = l.product_id\n"
            "where try_cast(p.product_id as integer) is not null",
            "opaque identifiers are kept",
        ),
        (
            "on try_cast(p.product_id as integer) = l.product_id",
            None,
        ),
    ],
)
def test_an_identifier_repair_that_drops_a_scheme_fails_the_checks(
    tmp_path, join, lost
):
    """#861: filtering to castable ids builds and loses the second scheme; a
    cast kept on the join alone keeps every product. Neither is caught by the
    row count of a model whose identity set changed."""

    task, fixture, workspace = _reference(
        tmp_path, "identifier_repair/fix_dim_products"
    )
    path = workspace / "models/marts/dim_products.sql"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "on p.product_id = cast(l.product_id as varchar)", join
        ),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored", score.gate
    failed = {c["name"] for c in score.checks if not c["passed"]}
    if lost is None:
        assert "every product still sold is present" not in failed
        assert "opaque identifiers are kept" not in failed
    else:
        assert lost in failed
        assert "every product still sold is present" in failed


@needs_dbt
def test_the_broken_identifier_setup_fails_the_gate(tmp_path):
    task, fixture, workspace = _reference(
        tmp_path, "identifier_repair/fix_dim_products"
    )
    broken = "models/marts/dim_products.sql"
    shutil.copyfile(task.directory / "setup" / broken, workspace / broken)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed", score.gate
