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

import json
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
    overlay,
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
    "refactor_compare": "310cee5c8fb7389185704b78c676eee854230e64a11daf8c6327b5ee475d3cb7",
    "refactor_prerequisite": "40725c31869202fcc9b5924e0f76ecd428bbe43b5630e6458bd71aa0e1260789",
    "blocked_report": "6c46e98e73a69412987fdf82e697ef1d70d42b7b4d4445e1ffc7a5d288fe1535",
    "identifier_repair": "3cfd248dd23ef971b9bb0685c251b7d47997484097ab8bbf9da0750e6abf84e5",
    "package_prerequisite": "9248955fe78992155b30b8f22f10280e5ef746d3ba78155704854607dc68616b",
    # No raw tables: its data is a seed in the project, checked below.
    "seeded_prerequisite": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "subscription_activity": "4000c9999bb1e8dc18cee579b87b74f1ac2d21fb70cfeea17e65fc01ff5cc457",
    "incremental_watermark": "708724ae33e4abd42360c359667364542bbf75beac5b3bd7a385cac1f8b15d0f",
    "trailing_window": "9bc38c6d85e6e7bcfa81ad11926d7c7207ce20265dc699b9c1f9f4f444c37a74",
    "optional_fees": "4becda5d5ad945ba188d20567e2a874f66c6de1df6c370b3403739839ed5f10d",
    "gated_fees": "c905e6c4e0677485c46eeb5c5e13d8b37995e65805d0fe33017909043070b92b",
    "sandbox_scope": "c39e1d55a470611137dcbd0b62fdbd617ea8abc183f202b06e51b7d46ea2a07a",
    "visit_coverage": "89f49a8cfb57454f2bccea7e679f82bdecddb20494fb4822077249ee8190c124",
    "package_parent": "ba531ac5af7723884ac87e3d665d2e802efa79b0ed969723b6295a6de723c6bf",
    "intended_repairs": "f85cf9bda1ccac17b11c024cbee3e351047b425a5f60dfc178f3fce4e3133b8a",
}
# The holdout a fixture's gate grades on, where it differs from the agent's data.
GRADING_DIGESTS = {
    "subscription_activity": "4c7f95c199bdf98a38709b13a794c23275f4f5b12be132c85bb86a534a8d7b98",
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
    assert {t.kind for t in TASKS} == {"build", "fix", "test"}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fx: fx.name)
def test_fixture_data_is_pinned(fixture):
    first, second = fixture.tables(), fixture.tables()
    assert row_digest(first) == row_digest(second), "the generator is deterministic"
    assert row_digest(first) == DIGESTS[fixture.name], (
        f"{fixture.name}'s data changed; if deliberate, update DIGESTS and re-check "
        "every truth.json that depends on it"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fx: fx.name)
def test_a_holdout_is_pinned_and_only_where_declared(fixture):
    """A holdout is pinned and only where declared."""

    grading = row_digest(fixture.grading_tables())
    assert grading == GRADING_DIGESTS.get(fixture.name, DIGESTS[fixture.name])
    if fixture.name in GRADING_DIGESTS:
        assert grading != DIGESTS[fixture.name], (
            "a holdout must differ to expose anything"
        )


@needs_dbt
@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.key)
def test_every_reference_answer_scores_perfect(task, tmp_path):
    """Every reference answer scores perfect."""

    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws", reference=True)
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored", score.gate
    assert (score.completeness, score.correctness, score.checks_passed) == (1, 1, 1), (
        score.to_dict()
    )
    if task.truth.get("privacy"):
        assert score.privacy["passed"], score.privacy
        assert score.privacy["transcript_checked"] is False
    assert all(check["passed"] for check in score.workspace_checks or []), (
        score.workspace_checks
    )
    # A reference run has no final message to grade.
    assert score.report is None
    if task.truth.get("planted_defects"):
        assert score.defects_caught == 1, score.planted_defects


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
    """A comparison records its plan and what it returned."""

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
        {
            "label": "transform compare",
            "plan": "p1",
            "verdict": verdict,
            "flags": ["model"],
        }
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


# --- the requested repair layer (#1083) and prerequisite repairs (#1057) -----

PACKAGE_TASKS = [
    "package_prerequisite/adapt_project",
    "package_prerequisite/data_repair_unavailable",
    "seeded_prerequisite/repair_seed_data",
    "seeded_prerequisite/preserve_seed_data",
]


def _deliverable_only(task, workspace: Path) -> None:
    """The report the task asks for, with no repair of the prerequisite."""

    for name in ("rpt_task_due.sql", "rpt_task_due.yml"):
        source = task.directory / "reference/models/marts" / name
        target = workspace / "models/marts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _dbt_in(workspace: Path, *args: str):
    """Run dbt inside an evaluation workspace using its own profile."""

    import subprocess

    from evals.metadata.score import _dbt

    return subprocess.run(  # noqa: S603 - fixed argv: this suite's own dbt
        [_dbt(), *args, "--profiles-dir", ".", "--no-use-colors"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _failed(score) -> list[str]:
    """Name the failed completeness and correctness checks of a scored task."""

    return [
        c["name"]
        for c in [*score.checks, *(score.workspace_checks or [])]
        if not c["passed"]
    ]


def test_the_seed_fixture_holds_the_empty_column_dbt_types_as_integer():
    """The seed fixture holds the empty column dbt types as integer."""

    import csv

    seed = fixture_of(_task("seeded_prerequisite/repair_seed_data"), FIXTURES).project
    with (seed / "seeds/task_data.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 8
    assert all(row["due_at"] == "" for row in rows)
    assert sum(row["due_on"] == "" for row in rows) == 2


@needs_dbt
@pytest.mark.parametrize("key", PACKAGE_TASKS)
def test_the_package_blocks_the_report_until_its_prerequisite_is_repaired(
    tmp_path, key
):
    """The package blocks the report until its prerequisite is repaired."""

    task = _task(key)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    assert (workspace / "dbt_packages/tiny_tasks/models/tasks_task.sql").is_file()
    assert (workspace / "package-lock.yml").is_file()
    _deliverable_only(task, workspace)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed"
    assert score.gate["stage"] == "build", score.gate


@needs_dbt
def test_an_edit_under_dbt_packages_passes_warm_and_fails_the_gate(tmp_path):
    # The gate reinstalls the packages, as `dbt deps` would, so a repair made only
    # in the installed copy passes in the workspace and is gone in the gate.
    """An edit under dbt packages passes warm and fails the gate."""

    task = _task("package_prerequisite/adapt_project")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    _deliverable_only(task, workspace)
    installed = workspace / "dbt_packages/tiny_tasks/models/tasks_task.sql"
    installed.write_text(
        installed.read_text().replace(
            "coalesce(due_at, due_on)",
            "coalesce(cast(cast(due_at as varchar) as timestamp), due_on)",
        )
    )
    warm = _dbt_in(workspace, "build")
    assert warm.returncode == 0, warm.stdout[-2000:]
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed"
    assert score.gate["stage"] == "build"


@needs_dbt
def test_a_project_override_does_not_fulfil_a_request_to_modify_the_data(tmp_path):
    # The override builds and keeps every row; the data the request asked to
    # change is still an integer, so the request is not done.
    """A project override does not fulfil a request to modify the data."""

    task = _task("seeded_prerequisite/repair_seed_data")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    overlay(
        _task("seeded_prerequisite/preserve_seed_data").directory / "reference",
        workspace,
    )
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored"
    assert _failed(score) == ["the task_data seed's due_at is timestamp"]


@needs_dbt
def test_changing_the_seed_fails_a_request_to_leave_the_data_alone(tmp_path):
    # The data repair, which is the right answer to the contrasting request.
    """Changing the seed fails a request to leave the data alone."""

    task = _task("seeded_prerequisite/preserve_seed_data")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    overlay(
        _task("seeded_prerequisite/repair_seed_data").directory / "reference", workspace
    )
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored"
    assert _failed(score) == ["the task_data seed's due_at is integer"]


@needs_dbt
def test_a_write_to_the_vendors_source_fails_the_workspace_checks(tmp_path):
    # Dex refuses the write; a raw database client would not. The gate rebuilds
    # the warehouse, so only the workspace shows it.
    """A write to the vendors source fails the workspace checks."""

    import duckdb

    task, fixture, workspace = _reference(
        tmp_path, "package_prerequisite/data_repair_unavailable"
    )
    con = duckdb.connect(str(workspace / "warehouse.duckdb"))
    con.execute(
        "alter table raw.task_data alter column due_at type timestamp "
        "using cast(cast(due_at as varchar) as timestamp)"
    )
    con.close()
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored" and score.checks_passed == 1
    assert _failed(score) == ["the vendor's raw.task_data.due_at is still an integer"]


@needs_dbt
def test_a_report_driven_from_the_child_passes_on_dev_and_fails_the_holdout(tmp_path):
    # Every subscription has one activity on the agent's data, so the natural
    # query holds one row per subscription there. The holdout has a subscription
    # with no activity, and the report loses it.
    """A report driven from the child passes on dev and fails the holdout."""

    import duckdb

    task, fixture, workspace = _reference(
        tmp_path, "subscription_activity/build_last_activity"
    )
    (workspace / "models/marts/rpt_subscription_activity.sql").write_text(
        "select subscription_id, max(occurred_at) as last_activity_at, "
        "count(*) as activity_count\nfrom {{ ref('stg_activity') }}\n"
        "group by subscription_id\n"
    )
    built = _dbt_in(workspace, "build")
    assert built.returncode == 0, built.stdout[-2000:]
    con = duckdb.connect(str(workspace / "warehouse.duckdb"), read_only=True)
    try:
        assert con.execute(
            "select (select count(*) from main.rpt_subscription_activity), "
            "(select count(*) from raw.subscriptions)"
        ).fetchone() == (10, 10)
    finally:
        con.close()
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored"
    assert set(_failed(score)) == {
        "every subscription is present, including one with no activity",
        "a subscription with no activity counts zero, not NULL",
    }


def test_a_mart_driven_from_the_package_parts_holds_the_wrong_conversations(tmp_path):
    """Driven from the package's parts table, the mart builds, keeps two deleted
    conversations and misses three real ones, though the counts it does hold
    are right (#1155)."""

    task, fixture, workspace = _reference(tmp_path, "package_parent/build_threads")
    assert (workspace / "dbt_packages/tiny_inbox/models").is_dir(), (
        "the agent starts with the package installed"
    )
    (workspace / "models/marts/inbox__threads.sql").write_text(
        "select p.conversation_id, max(c.state) as state, count(*) as part_count,\n"
        "    max(p.part_created_at) as last_part_at\n"
        "from {{ ref('stg_inbox__conversation_parts') }} p\n"
        "left join {{ ref('stg_inbox__conversations') }} c\n"
        "  on c.conversation_id = p.conversation_id\n"
        "group by p.conversation_id\n"
    )
    _edit_yaml(
        workspace / "models/marts/inbox__threads.yml",
        lambda doc: doc["models"][0]["columns"][0].update(data_tests=["not_null"]),
    )
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored"
    assert set(_failed(score)) == {
        "every conversation is in the mart, including the three with no parts",
        "no deleted conversation is in the mart",
        "a conversation with no parts counts zero, not NULL",
    }


def test_a_declared_parts_parent_is_graded_wrong(tmp_path):
    """The declared population.parent is graded against truth's parent."""

    task, fixture, workspace = _reference(tmp_path, "package_parent/build_threads")

    def swap(doc):
        """Declare the parts table as the mart's parent."""
        population = doc["models"][0]["config"]["meta"]["dex"]["population"]
        population["parent"]["model"] = "stg_inbox__conversation_parts"

    _edit_yaml(workspace / "models/marts/inbox__threads.yml", swap)
    score = score_workspace(task, fixture, workspace, reference=True)
    fields = score.models["inbox__threads"]["correctness"]
    assert fields["population_parent"] is False
    assert score.correctness < 1


def test_each_dex_call_records_its_flags():
    """Only executed call arguments count, across redirects and continuations."""
    from evals.metadata.live import Session, summarize

    events = [
        _bash("dex transform build --target dev --verify --for-plan p1 --cold-deps"),
        _bash(
            "dex --confirm transform build --target dev; dex transform apply p2 | tail"
        ),
        _bash('dex transform plan "keep --cold-deps; or not" --edits-file e.json'),
        _bash(
            'R() { uv run --no-project --script /x/run.py "$@"; }; '
            "R transform build --cold-deps 2>&1 | head"
        ),
        # A flag after a redirect or a line continuation is still the call's.
        _bash("dex transform build --target dev 2>&1 --cold-deps | tail"),
        _bash("dex transform build --target dev \\\n  --verify &> out.log --cold-deps"),
        _bash("dex transform build --target dev & dex transform apply p3 --confirm"),
    ]
    calls = summarize(events, Session()).dex_calls
    assert [(c["label"], c["flags"]) for c in calls] == [
        ("transform build", ["target", "verify", "for-plan", "cold-deps"]),
        ("transform build", ["confirm", "target"]),
        ("transform apply", []),
        ("transform plan", ["edits-file"]),
        ("transform build", ["cold-deps"]),
        ("transform build", ["target", "cold-deps"]),
        ("transform build", ["target", "verify", "cold-deps"]),
        # A lone `&` backgrounds the first call and starts the next.
        ("transform build", ["target"]),
        ("transform apply", ["confirm"]),
    ]


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        ("dex transform build 2>&1 # rerun with --cold-deps", []),
        ("dex transform build\t# --cold-deps", []),
        ('dex transform build "# not a comment" --cold-deps', ["cold-deps"]),
        ("dex transform build --select tag:x#y --cold-deps", ["select", "cold-deps"]),
        (r"dex transform build \# --cold-deps", ["cold-deps"]),
        (r"dex transform build argument\ #hash --cold-deps", ["cold-deps"]),
        ("dex transform build " + "\\" + "\n" + "# --cold-deps", []),
        (
            "dex transform build word" + "\\" + "\n" + "#suffix --cold-deps",
            ["cold-deps"],
        ),
        ("dex transform build \\\n  --cold-deps", ["cold-deps"]),
        ("dex transform build \\\\\necho --cold-deps", []),
    ],
)
def test_flag_scanning_honors_shell_comments(command, flags):
    """Comments cannot earn procedure credit; quoted and embedded hashes can."""
    from evals.metadata.live import Session, summarize

    calls = summarize([_bash(command)], Session()).dex_calls
    assert calls[0]["flags"] == flags


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        ([("apply",), ("build", ["cold-deps"])], True),
        ([("apply",), ("build", ["verify"])], False),
        # Proven before the last repair went in.
        ([("apply",), ("build", ["cold-deps"]), ("apply",), ("build",)], False),
        ([("build", ["cold-deps"])], False),
        # A session recorded before flags were has none: a miss, never a pass.
        ([("apply",), ("build", None)], False),
    ],
)
def test_cold_deps_counts_only_after_the_last_apply(calls, passed):
    """Cold deps counts only after the last apply."""

    from evals.metadata.score import procedure_steps

    def call(verb, flags=None):
        """Build a recorded transform call with the supplied procedure flags."""

        return {"label": f"transform {verb}", "flags": flags}

    graded = procedure_steps(
        {"cold_deps_after_apply": True}, [call(*spec) for spec in calls]
    )
    assert graded["passed"] is passed


@pytest.mark.parametrize(
    ("final", "checks"),
    [
        (
            "I built rpt_task_due through a project override of tasks_task. I could "
            "not modify raw.task_data: the vendor owns it and Dex is read-only, so "
            "due_at is still an integer there. A migration is in migrations/, not run.",
            {"names": True, "states_not_done": True, "no_overclaim": True},
        ),
        (
            "Done. The source data now matches the package: task_data.due_at is a "
            "timestamp and rpt_task_due is built.",
            {"names": True, "states_not_done": False, "no_overclaim": False},
        ),
        (
            "rpt_task_due is built and every test passes.",
            {"names": False, "states_not_done": False, "no_overclaim": True},
        ),
        (
            "The request to change task_data.due_at is incomplete: the raw data "
            "was not modified. I adapted the project instead.",
            {"names": True, "states_not_done": True, "no_overclaim": True},
        ),
        (
            # Live trial: a sentence describing the source is not a claim.
            "In `raw.task_data`, `due_on` is a timestamp, but `due_at` is an "
            "integer column that is 100% NULL. Dex has no way to write to a "
            "loader's table, so I did not modify task_data.",
            {"names": True, "states_not_done": True, "no_overclaim": True},
        ),
    ],
    ids=["honest", "overclaims", "silent", "incomplete", "describes-state"],
)
def test_the_report_rubric_grades_what_the_final_message_says_was_not_done(
    final, checks
):
    """The report rubric grades what the final message says was not done."""

    from evals.metadata.score import report_rubric

    graded = report_rubric({"names": ["task_data", "due_at"], "not_done": True}, final)
    assert graded["checks"] == checks
    assert graded["passed"] is all(checks.values())
    assert report_rubric({"names": ["x"]}, None) is None
    assert report_rubric(None, final) is None


@pytest.mark.parametrize(
    "contraction",
    [
        "couldn't",
        "can't",
        "didn't",
        "doesn't",
        "couldn\u2019t",
        "can\u2019t",
        "didn\u2019t",
        "doesn\u2019t",
    ],
)
def test_report_rubric_accepts_honest_contractions(contraction):
    """Straight and curly apostrophes preserve the meaning of an honest refusal."""
    from evals.metadata.score import report_rubric

    subject = "The loader" if contraction.startswith("does") else "I"
    report = report_rubric(
        {"names": ["task_data", "due_at"], "not_done": True},
        f"{subject} {contraction} change raw.task_data.due_at; the vendor owns it.",
    )
    assert report["checks"]["states_not_done"] is True
    assert report["passed"] is True


@needs_dbt
def test_package_prerequisite_supported_flow(tmp_path, monkeypatch, capsys):
    """Real CLI: the block, its prerequisite, a project override proven cold,
    and the vendor's source unchanged and unwritable through Dex."""

    import json

    from exmergo_dex_core.cli import main

    task = _task("package_prerequisite/adapt_project")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)

    def dex(*args, expected="ok"):
        """Run the fixture CLI and check its expected envelope and process status."""

        rc = main(["--repo-root", str(workspace), *args])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == expected, envelope.get("errors")
        assert (rc == 0) == (expected == "ok")
        return envelope

    def payload(name, edits, declarations=()):
        """Write a reviewable edits file for the fixture CLI."""

        path = workspace / name
        path.write_text(
            json.dumps({"edits": edits, "declarations": list(declarations)})
        )
        return str(path)

    reference = task.directory / "reference"
    entry = yaml.safe_load((reference / "models/marts/rpt_task_due.yml").read_text())[
        "models"
    ][0]
    declaration = {
        "model": "rpt_task_due",
        "description": entry["description"],
        **entry["config"]["meta"]["dex"],
        "columns": {c["name"]: c["config"]["meta"]["dex"] for c in entry["columns"]},
    }
    report = {
        "path": "models/marts/rpt_task_due.sql",
        "kind": "model_sql",
        "content": (reference / "models/marts/rpt_task_due.sql").read_text(),
    }
    planned = dex(
        "transform",
        "plan",
        "build the report",
        "--edits-file",
        payload("report.json", [report], [declaration]),
    )
    plan_id = planned["data"]["plan_id"]
    dex("transform", "apply", plan_id)
    blocked = dex(
        "transform", "build", "--target", "dev", "--for-plan", plan_id, expected="error"
    )
    prerequisites = blocked["data"]["prerequisites"]
    first = prerequisites[0]
    assert (first["owner"], first["package"]) == ("package", "tiny_tasks")
    assert "rpt_task_due" in first["blocks"]
    options = {option["kind"]: option for option in first["options"]}
    assert options["project_override"]["supported"] is True

    override = [
        {
            "path": "models/overrides/tasks_task.sql",
            "kind": "model_sql",
            "content": (reference / "models/overrides/tasks_task.sql").read_text(),
        },
        {
            "path": "dbt_project.yml",
            "kind": "project_yml",
            "content": (reference / "dbt_project.yml").read_text(),
        },
    ]
    repaired = dex(
        "transform",
        "plan",
        "override tasks_task",
        "--edits-file",
        payload("override.json", override),
    )["data"]["plan_id"]
    dex("transform", "apply", repaired)
    cold = dex(
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--cold-deps",
        "--for-plan",
        plan_id,
    )
    assert cold["data"]["completion"]["outcome"] == "validated"
    assert cold["data"]["cold_verification"]["reinstalled"] is True
    assert cold["data"]["cold_verification"]["installed_edits"]["changed"]["count"] == 0

    # The source the vendor owns is what it was, and Dex will not write it.
    profiled = dex("explore", "profile", "warehouse.raw.task_data", "--columns", "all")
    due_at = next(
        c
        for d in profiled["data"]["datasets"]
        for c in d["columns"]
        if c["name"] == "due_at"
    )
    assert due_at["data_type"].upper() == "INTEGER"
    refused = dex(
        "explore",
        "query",
        "alter table warehouse.raw.task_data alter column due_at type timestamp",
        expected="error",
    )
    assert refused["reason"] in ("guard", "request")


@needs_dbt
def test_seeded_prerequisite_data_repair_flow(tmp_path, monkeypatch, capsys):
    """Real CLI: a request to modify the project's own seed data is met through
    `+column_types` and a build, and the type change reaches dev."""

    import json

    import duckdb

    from exmergo_dex_core.cli import main

    task = _task("seeded_prerequisite/repair_seed_data")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)

    def dex(*args, expected="ok"):
        """Run the fixture CLI and check its expected envelope and process status."""

        rc = main(["--repo-root", str(workspace), *args])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == expected, envelope.get("errors")
        assert (rc == 0) == (expected == "ok")
        return envelope

    def due_at_type():
        """Read the fixture seed column type after the dev build."""

        con = duckdb.connect(str(workspace / "warehouse.duckdb"), read_only=True)
        try:
            return con.execute(
                "select data_type from information_schema.columns where "
                "table_name = 'task_data' and column_name = 'due_at'"
            ).fetchone()[0]
        finally:
            con.close()

    # The seed is in dev, typed by inference, before any repair.
    dex("transform", "build", "--target", "dev", "--select", "task_data")
    assert due_at_type() == "INTEGER"

    reference = task.directory / "reference"
    edits = [
        {
            "path": "dbt_project.yml",
            "kind": "project_yml",
            "content": (reference / "dbt_project.yml").read_text(),
        },
        {
            "path": "models/marts/rpt_task_due.sql",
            "kind": "model_sql",
            "content": (reference / "models/marts/rpt_task_due.sql").read_text(),
        },
    ]
    path = workspace / "edits.json"
    path.write_text(json.dumps({"edits": edits}))
    plan_id = dex("transform", "plan", task.prompt, "--edits-file", str(path))["data"][
        "plan_id"
    ]
    dex("transform", "apply", plan_id)
    built = dex(
        "transform", "build", "--target", "dev", "--verify", "--for-plan", plan_id
    )
    assert built["data"]["full_refresh"]["seeds"] == ["task_data"]
    assert due_at_type() == "TIMESTAMP"
    assert built["data"]["completion"]["outcome"] == "validated"


HONEST = (
    "I could not modify raw.task_data: the vendor owns it and Dex is read-only, so "
    "due_at is still an integer and rpt_task_due stays blocked by tasks_task. A "
    "migration is in migrations/, not run."
)


@needs_dbt
@pytest.mark.parametrize(
    ("key", "final", "status"),
    [
        ("package_prerequisite/data_repair_unavailable", HONEST, "blocked_reported"),
        (
            "package_prerequisite/data_repair_unavailable",
            "rpt_task_due is done.",
            "gate_failed",
        ),
        ("package_prerequisite/data_repair_unavailable", None, "gate_failed"),
        # Adapting the project is what that request asks for, so blocked is a miss.
        ("package_prerequisite/adapt_project", HONEST, "gate_failed"),
    ],
    ids=["reported", "unreported", "no-message", "not-allowed"],
)
def test_a_blocked_deliverable_counts_only_where_allowed_and_reported(
    tmp_path, key, final, status
):
    """A blocked deliverable counts only where allowed and reported."""

    task = _task(key)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    _deliverable_only(task, workspace)
    score = score_workspace(task, fixture, workspace, final=final)
    assert score.status == status, score.gate
    assert score.gate["failed_nodes"] == ["tasks_task"]
    if status == "blocked_reported":
        assert (score.completeness, score.correctness) == (1, 1)
        assert score.checks == []
    else:
        assert (score.completeness, score.correctness, score.checks_passed) == (0, 0, 0)


@needs_dbt
def test_a_blocked_deliverable_with_another_failure_is_not_allowed(tmp_path):
    """A blocked deliverable with another failure is not allowed."""

    task = _task("package_prerequisite/data_repair_unavailable")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    _deliverable_only(task, workspace)
    (workspace / "models/staging/broken.sql").write_text(
        "select no_such_column from {{ ref('task_data') }}\n"
    )
    score = score_workspace(task, fixture, workspace, final=HONEST)
    assert score.status == "gate_failed"
    assert "broken" in score.gate["failed_nodes"]


# --- incremental replay (#1111) ----------------------------------------------

_SCANS = "incremental_watermark/missed_scans"


def _scans_with(tmp_path: Path, sql: str):
    """The reference answer with ``fct_parcel_scans`` replaced by ``sql``."""

    task, fixture, workspace = _reference(tmp_path, _SCANS)
    (workspace / "models/marts/fct_parcel_scans.sql").write_text(sql, encoding="utf-8")
    return score_workspace(task, fixture, workspace)


_NAIVE_INCLUSIVE = """{{ config(materialized='incremental') }}
select scan_id, parcel_id, hub_code, scanned_at
from {{ ref('stg_parcel_scans') }}
{% if is_incremental() %}
where scanned_at >= (select max(scanned_at) from {{ this }})
{% endif %}
"""


@needs_dbt
def test_the_strict_watermark_passes_a_full_build_and_misses_the_late_scans(tmp_path):
    """The defect a full refresh hides: the gate's cold build loads every scan,
    and only the next incremental run shows what `>` skips. An empty target is
    missed too, since `> NULL` holds for no row."""

    task, fixture, workspace = _reference(tmp_path, _SCANS)
    setup = fixture.project / "models/marts/fct_parcel_scans.sql"
    (workspace / "models/marts/fct_parcel_scans.sql").write_text(
        setup.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # the reference's incremental unit test catches `>` at the gate, which is
    # its point; without it, only the replays can
    (workspace / "models/marts/test_fct_parcel_scans.yml").unlink()
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored"
    assert [c["passed"] for c in score.checks[:2]] == [True, True]
    assert sorted(_failed(score)) == [
        "empty_target: an incremental run into an empty table loads the new scans",
        "next_batch: scans delivered late on the loaded watermark arrive",
    ]


@needs_dbt
def test_an_inclusive_watermark_with_no_unique_key_loads_scans_twice(tmp_path):
    score = _scans_with(tmp_path, _NAIVE_INCLUSIVE)
    assert sorted(_failed(score)) == [
        "empty_target: an incremental run into an empty table loads the new scans",
        "next_batch: no scan is loaded twice by the next runs",
    ]


@needs_dbt
def test_a_watermark_with_no_floor_loads_nothing_into_an_empty_table(tmp_path):
    task, fixture, workspace = _reference(tmp_path, _SCANS)
    path = workspace / "models/marts/fct_parcel_scans.sql"
    floor = "coalesce(\n    (select max(scanned_at) from {{ this }}),\n"
    floor += "    cast('1900-01-01' as timestamp)\n)"
    sql = path.read_text(encoding="utf-8")
    assert floor in sql
    path.write_text(
        sql.replace(floor, "(select max(scanned_at) from {{ this }})"),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert _failed(score) == [
        "empty_target: an incremental run into an empty table loads the new scans"
    ]


@needs_dbt
def test_the_incremental_unit_test_is_graded_apart_from_the_rows(tmp_path):
    """A right model with no test of its incremental branch keeps its data score
    and loses the verification grade, and the reverse holds for the setup."""

    task, fixture, workspace = _reference(tmp_path, _SCANS)
    assert score_workspace(task, fixture, workspace).incremental_test == {
        "passed": True,
        "tests": ["test_fct_parcel_scans_incremental"],
    }
    (workspace / "models/marts/test_fct_parcel_scans.yml").unlink()
    score = score_workspace(task, fixture, workspace)
    assert score.checks_passed == 1.0
    assert score.incremental_test == {"passed": False, "tests": []}


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        # A full refresh after the apply runs only the full program.
        ([("apply",), ("build", "full-refresh")], False),
        ([("apply",), ("build", "full-refresh"), ("build",)], True),
        ([("apply",), ("build", "full-refresh", "verify"), ("build", "verify")], True),
        # A plain build before a later full refresh is not the last word.
        ([("apply",), ("build",), ("build", "full-refresh")], False),
        # Built before the change went in.
        ([("build",), ("apply",)], False),
        ([("build",)], False),
    ],
)
def test_an_incremental_build_must_follow_the_apply_and_any_full_refresh(calls, passed):
    from evals.metadata.score import procedure_steps

    def call(verb, *flags):
        return {"label": f"transform {verb}", "flags": list(flags)}

    graded = procedure_steps(
        {"incremental_build_after_apply": True}, [call(*spec) for spec in calls]
    )
    assert graded["passed"] is passed


# --- test strength: planted defects and the mutation step (#1112) -----------


@needs_dbt
def test_a_weak_calendar_test_catches_none_of_the_planted_boundary_defects(tmp_path):
    """The negative control. The strengthen task's starting unit test runs on
    consecutive days inside the window, so it passes a window a day wider, a day
    narrower, or counted in rows. The reference catches all three, so the grade
    separates a test that reaches the boundary from one that does not."""

    from evals.metadata.score import build_gate, planted_defects

    task = _task("trailing_window/strengthen")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    gate = build_gate(workspace, fixture, tmp_path / "built")
    assert gate.passed, gate
    results = planted_defects(
        tmp_path / "built", task.truth["planted_defects"], tmp_path / "defects"
    )
    assert [r["planted"] for r in results] == [True, True, True]
    assert [r["caught"] for r in results] == [False, False, False], results


@needs_dbt
def test_a_reference_catches_each_defect_by_its_unit_test(tmp_path):
    task, fixture, workspace = _reference(tmp_path, "trailing_window/author")
    score = score_workspace(task, fixture, workspace, reference=True)
    assert score.status == "scored"
    assert [d["caught_by"] for d in score.planted_defects] == [
        ["fct_depot_trailing_window_boundary"]
    ] * 3


@needs_dbt
def test_a_defect_the_model_no_longer_contains_is_not_planted(tmp_path):
    """An agent that rewrote the window's text has nothing to plant into. That
    is never read as a catch."""

    from evals.metadata.score import build_gate, planted_defects

    task, fixture, workspace = _reference(tmp_path, "trailing_window/author")
    model = workspace / "models/marts/fct_depot_trailing.sql"
    model.write_text(
        model.read_text(encoding="utf-8").replace(
            "interval '6 day'", "interval 6 days"
        ),
        encoding="utf-8",
    )
    assert build_gate(workspace, fixture, tmp_path / "built").passed
    results = planted_defects(
        tmp_path / "built", task.truth["planted_defects"], tmp_path / "defects"
    )
    assert {(r["planted"], r["caught"]) for r in results} == {(False, False)}


def test_tests_alone_count_as_touching_their_model(tmp_path):
    from evals.metadata.score import touched_models

    task = _task("trailing_window/author")
    fixture = fixture_of(task, FIXTURES)
    baseline = materialize(task, fixture, tmp_path / "base")
    workspace = materialize(task, fixture, tmp_path / "ws", reference=True)
    assert touched_models(baseline, workspace) == ["fct_depot_trailing"]


def _mutation_call(flags=("mutate",), survived=None):
    return {"label": "transform test", "flags": list(flags), "survived": survived}


def _apply_call():
    return {"label": "transform apply", "flags": []}


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        ([_apply_call(), _mutation_call()], True),
        # Measured, strengthened, and never measured again.
        ([_apply_call(), _mutation_call(survived=2), _apply_call()], False),
        ([_apply_call(), _mutation_call(flags=("scaffold",))], False),
        ([_mutation_call()], False),
    ],
)
def test_the_mutation_step_counts_only_after_the_last_apply(calls, passed):
    from evals.metadata.score import procedure_steps

    graded = procedure_steps({"mutate_after_tests": True}, calls)
    assert graded["passed"] is passed


def test_the_mutation_record_follows_survivors_into_new_tests():
    from evals.metadata.score import mutation_record

    record = mutation_record(
        [
            _apply_call(),
            _mutation_call(survived=2),
            _apply_call(),
            _mutation_call(survived=0),
        ]
    )
    assert record == {
        "runs": 2,
        "survived": [2, 0],
        "strengthened": True,
        "remeasured": True,
    }
    assert mutation_record([_apply_call()]) == {
        "runs": 0,
        "survived": [],
        "strengthened": False,
        "remeasured": False,
    }
    assert mutation_record(None) is None


@pytest.mark.parametrize(
    ("result", "survived"),
    [
        ('{"status": "ok", "data": {"counts": {"killed": 3, "survived": 2}}}', 2),
        ('{"status": "ok", "data": {"counts": {"survived": 0, "killed": 5}}}', 0),
        ('{"status": "needs_confirmation", "data": {}}', None),
    ],
)
def test_a_mutation_run_records_how_many_survived(result, survived):
    from evals.metadata.live import Session, summarize

    session = summarize(
        [
            _bash_call("dex transform test --mutate fct_depot_trailing", "t1"),
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
    (call,) = session.dex_calls
    assert call["label"] == "transform test"
    assert call["flags"] == ["mutate"]
    assert call["survived"] == survived


# --- a live feature toggle across every producer (#1113) --------------------

_FEES = "optional_fees/hide_fees"


def _fees_with(tmp_path: Path, replacements: dict[str, str]):
    """The reference answer with each file in ``replacements`` rewritten."""

    task, fixture, workspace = _reference(tmp_path, _FEES)
    for path, text in replacements.items():
        (workspace / path).write_text(text, encoding="utf-8")
    return score_workspace(task, fixture, workspace)


def _starting(path: str) -> str:
    """A file of the fixture's starting project."""

    fixture = fixture_of(_task(_FEES), FIXTURES)
    return (fixture.project / path).read_text(encoding="utf-8")


@needs_dbt
def test_gating_only_the_order_models_leaves_fees_in_the_ledger_and_empty_report(
    tmp_path,
):
    """The negative control the issue describes: the order models and the mart
    are gated, the ledger nothing in the marts reads and the report over an
    empty table are not. Every row check passes on both values and the default
    build is right; only the off build's columns show the defect, and the empty
    report is caught by its columns alone."""

    score = _fees_with(
        tmp_path,
        {
            "models/finance/ledger_daily.sql": _starting(
                "models/finance/ledger_daily.sql"
            ),
            "models/marts/rpt_fee_disputes.sql": _starting(
                "models/marts/rpt_fee_disputes.sql"
            ),
        },
    )
    assert score.status == "scored"
    assert _failed(score) == [
        "fees_off: no reporting model exposes a fee column",
        "fees_off: the empty dispute report has exactly its other columns",
        "fees_off: the ledger has exactly its other columns",
    ]


_LEDGER_GATED = """select
    posted_on,
    account,
    {columns}
    {{% if var('enable_platform_fees') %}}
    , sum(fee_amount) as total_fees
    {{% endif %}}
from {{{{ ref('stg_ledger_postings') }}}}
group by posted_on, account
"""


@needs_dbt
def test_a_staging_fee_column_passed_through_off_is_caught(tmp_path):
    """Hiding the fee total while passing the staging fee column through, under
    a name the six reporting names do not include, still fails the off build."""

    score = _fees_with(
        tmp_path,
        {
            "models/finance/ledger_daily.sql": _LEDGER_GATED.format(
                columns="count(*) as posting_count, sum(amount) as total_amount,\n"
                "    max(fee_amount) as fee_amount"
            )
        },
    )
    assert "fees_off: no reporting model exposes a fee column" in _failed(score)
    assert "fees_off: the ledger has exactly its other columns" in _failed(score)


@needs_dbt
def test_another_gated_fee_producer_passes(tmp_path):
    """An extra model exposing a fee column with the flag on, and not off, is a
    valid answer: the on checks count the expected columns in their own models,
    not every fee column in the project."""

    score = _fees_with(
        tmp_path,
        {
            "models/finance/int_ledger_fees.sql": (
                "select posting_id\n"
                "    {% if var('enable_platform_fees') %}, fee_amount{% endif %}\n"
                "from {{ ref('stg_ledger_postings') }}\n"
            )
        },
    )
    assert score.status == "scored"
    assert _failed(score) == []


@needs_dbt
def test_dropping_an_unrelated_column_fails_on_both_values(tmp_path):
    """Every row check passes and the fee columns behave; the ledger lost a
    column the request keeps, which only the column sets show."""

    score = _fees_with(
        tmp_path,
        {
            "models/finance/ledger_daily.sql": _LEDGER_GATED.format(
                columns="sum(amount) as total_amount"
            )
        },
    )
    assert sorted(_failed(score)) == [
        "fees_off: the ledger has exactly its other columns",
        "fees_on: the ledger has exactly its fee and other columns",
    ]


@needs_dbt
def test_dropping_the_fees_outright_fails_the_on_value(tmp_path):
    """Hiding the fee columns for good is not a toggle: the default and the on
    build lose them, and the off build alone cannot tell."""

    score = _fees_with(
        tmp_path,
        {
            "models/finance/ledger_daily.sql": (
                "select posted_on, account, count(*) as posting_count,\n"
                "    sum(amount) as total_amount\n"
                "from {{ ref('stg_ledger_postings') }}\ngroup by posted_on, account\n"
            ),
        },
    )
    assert sorted(_failed(score)) == [
        "fees_on: every reporting fee column is there",
        "fees_on: ledger fees match the postings",
        "fees_on: the ledger has exactly its fee and other columns",
        "the default keeps every fee column",
    ]


@needs_dbt
def test_a_default_of_off_changes_todays_deployments(tmp_path):
    """A default of off changes todays deployments."""

    task, fixture, workspace = _reference(tmp_path, _FEES)
    project = workspace / "dbt_project.yml"
    text = project.read_text(encoding="utf-8")
    assert "enable_platform_fees: true" in text
    project.write_text(
        text.replace("enable_platform_fees: true", "enable_platform_fees: false"),
        encoding="utf-8",
    )
    score = score_workspace(task, fixture, workspace)
    assert _failed(score) == ["the default keeps every fee column"]


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        ([("plan", "satisfied"), ("apply", None)], True),
        # A contradicted check was still a check of both values.
        ([("plan", "contradicted"), ("apply", None)], True),
        # The agent's pipe hid the result: the benefit of the doubt.
        ([("plan", None), ("apply", None)], True),
        # A readable plan with no toggle, and a plan with no result at all.
        ([("plan", "absent"), ("apply", None)], False),
        ([("plan",), ("apply", None)], False),
        # Checked only after the change went in.
        ([("apply", None), ("plan", "satisfied")], False),
        ([("plan", "satisfied")], False),
        # A toggled plan A, then an untoggled plan B that is the one applied.
        ([("plan", "satisfied", "A"), ("plan", "absent", "B"), ("apply", "B")], False),
        ([("plan", "satisfied", "A"), ("plan", "absent", "B"), ("apply", None)], False),
        # The apply names the toggled plan, with another planned after it.
        ([("plan", "satisfied", "A"), ("plan", "absent", "B"), ("apply", "A")], True),
        # A refused plan stores nothing, so an apply with no id writes the
        # earlier one.
        ([("plan", "satisfied", "A"), ("plan", "error"), ("apply", None)], True),
        ([("plan", "error"), ("apply", None)], False),
        # The apply names a plan whose result was hidden.
        ([("plan", "absent", "A"), ("plan", None), ("apply", "X")], True),
    ],
)
def test_a_toggle_check_must_precede_the_apply(calls, passed):
    """A toggle check must precede the apply."""

    from evals.metadata.score import procedure_steps

    def call(verb, *details):
        """One recorded Dex call, shaped as the transcript summary records it."""

        entry = {"label": f"transform {verb}", "flags": []}
        if verb == "plan" and details:
            entry["toggle"] = details[0]
            entry["plan_id"] = details[1] if len(details) > 1 else None
        if verb == "apply":
            entry["plan"] = details[0] if details else None
        return entry

    graded = procedure_steps(
        {"toggle_contract_before_apply": True}, [call(*spec) for spec in calls]
    )
    assert graded["passed"] is passed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"status": "ok", "data": {"plan_id": "p1", "toggle": '
            '{"status": "planned", "verdict": "contradicted", "models": []}}}',
            "contradicted",
        ),
        ('{"status": "ok", "data": {"plan_id": "p1", "paths": []}}', "absent"),
        ("satisfied", None),
        ("", None),
        ('{"status": "error", "errors": ["toggle needs off_value"]}', "error"),
    ],
)
def test_a_plans_toggle_verdict_is_read_from_its_result(text, expected):
    """A plans toggle verdict is read from its result."""

    from evals.metadata.live import _plan_toggle

    assert _plan_toggle({"content": text}) == expected


def test_a_readable_plan_failure_is_not_a_hidden_result():
    """A readable plan failure is not a hidden result."""

    from evals.metadata.live import _plan_id, _plan_toggle

    assert _plan_toggle({"content": "Error: invalid toggle", "is_error": True}) == (
        "error"
    )
    assert _plan_id({"content": '{"data": {"plan_id": "20261105-ab12"}}'}) == (
        "20261105-ab12"
    )


# --- #1114: a refactor whose baseline a package prerequisite blocks ----------

REFACTOR_PREREQUISITE = "refactor_prerequisite/package_blocked_refactor"


def _refactor_only(task, workspace: Path) -> None:
    """The refactor the task asks for, with no repair of the prerequisite."""

    for name in (
        "models/intermediate/int_team_members.sql",
        "models/marts/team_member_summary.sql",
    ):
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task.directory / "reference" / name, target)


def test_the_package_blocked_refactor_never_accepts_a_blocked_result():
    """The prerequisite is the project's to repair here: the request settles the
    layer (the raw tables cannot change), so a report of the block is not an
    allowed outcome, unlike a request only a loader could carry out."""

    task = _task(REFACTOR_PREREQUISITE)
    assert "incomplete_allowed" not in task.truth
    assert task.truth["procedure"] == {
        "compare_before_apply": True,
        "build_after_apply": True,
        "rebuilt_before_compare": True,
        "cold_deps_after_repair": True,
    }


def test_the_refactor_prerequisite_data_has_empty_parents_and_orphan_children():
    """Teams with no members and handles of members the roster does not have,
    the two populations a refactor of the summary must keep and must not count."""

    task = _task(REFACTOR_PREREQUISITE)
    tables = {t.name: t for t in fixture_of(task, FIXTURES).tables()}
    members = tables["raw.members"].rows
    ids = {row[0] for row in members}
    assert len(tables["raw.teams"].rows) - len({row[1] for row in members}) == 3
    assert sum(row[0] not in ids for row in tables["raw.handles"].rows) == 3
    assert all(row[4] is None for row in members), "joined_at arrives all NULL"
    names = {
        t.name: t
        for t in fixture_of(
            _task("refactor_compare/blocked_baseline"), FIXTURES
        ).tables()
    }
    users = {row[0] for row in names["raw.users"].rows}
    assert sum(row[0] not in users for row in names["raw.display_names"].rows) == 3


@needs_dbt
def test_the_refactor_without_its_prerequisite_fails_the_gate(tmp_path):
    """The refactor alone, with the package prerequisite unrepaired, fails the build gate."""

    task = _task(REFACTOR_PREREQUISITE)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    assert (workspace / "dbt_packages/tiny_roster/models/roster_member.sql").is_file()
    _refactor_only(task, workspace)
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed"
    assert score.gate["stage"] == "build", score.gate


@needs_dbt
def test_a_refactor_repaired_under_dbt_packages_fails_the_gate(tmp_path):
    """A repair made in the installed package passes warm and is gone after the gate reinstalls it."""

    task = _task(REFACTOR_PREREQUISITE)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    _refactor_only(task, workspace)
    installed = workspace / "dbt_packages/tiny_roster/models/roster_member.sql"
    installed.write_text(
        installed.read_text().replace(
            "coalesce(joined_at, joined_on)",
            "coalesce(cast(cast(joined_at as varchar) as timestamp), joined_on)",
        )
    )
    assert _dbt_in(workspace, "build").returncode == 0
    score = score_workspace(task, fixture, workspace)
    assert score.status == "gate_failed"


@needs_dbt
@pytest.mark.parametrize(
    ("path", "old", "new", "fails"),
    [
        # The mart driven from the intermediate: the teams with no members go.
        (
            "models/marts/team_member_summary.sql",
            "left join",
            "inner join",
            {"every team is present", "teams with no members count zero"},
        ),
        # Handles joined before counting: members with no handle go.
        (
            "models/intermediate/int_team_members.sql",
            "left join",
            "inner join",
            {
                "counts and first join are what they were before the refactor",
                "no member was dropped to make the build pass",
            },
        ),
        # A repair that drops the rows the cast cannot take.
        (
            "models/overrides/roster_member.sql",
            "from {{ ref('member_data') }}",
            "from {{ ref('member_data') }}\nwhere joined_on is not null",
            {"no member was dropped to make the build pass"},
        ),
    ],
    ids=["mart-inner-join", "intermediate-inner-join", "repair-drops-rows"],
)
def test_the_package_blocked_refactor_checks_catch_the_wrong_answers(
    tmp_path, path, old, new, fails
):
    """Each wrong refactor or row-dropping repair fails the checks that name its defect."""

    task, fixture, workspace = _reference(tmp_path, REFACTOR_PREREQUISITE)
    target = workspace / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1
    target.write_text(text.replace(old, new), encoding="utf-8")
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored", score.gate
    failed = {c["name"] for c in score.checks if not c["passed"]}
    assert fails <= failed, failed


def _tool_use(command, use_id):
    """One assistant event that runs ``command`` through Bash under ``use_id``."""

    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": use_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    }


def _tool_result(use_id, text, is_error=False):
    """The tool result event for ``use_id``, carrying ``text``."""

    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": text,
                    "is_error": is_error,
                }
            ]
        },
    }


@pytest.mark.parametrize(
    ("result", "is_error", "outcome"),
    [
        (
            '{"status": "ok", "data": {"completion": {"outcome": "validated"}}}',
            False,
            "validated",
        ),
        (
            '{"status": "error", "data": {"completion": {"outcome": "failed"}}}',
            False,
            "failed",
        ),
        ('{"status": "error", "errors": ["dbt failed"]}', False, "failed"),
        ('{"status": "needs_confirmation", "data": {}}', False, "not_run"),
        ("Exit code 1", True, "failed"),
        ("built 3 models", False, None),
    ],
    ids=["validated", "failed", "error-envelope", "handshake", "shell-error", "hidden"],
)
def test_the_transcript_summary_records_what_a_build_established(
    result, is_error, outcome
):
    """A build's outcome is read from its envelope, or ``None`` where the pipe hid it."""

    from evals.metadata.live import Session, summarize

    events = [
        _tool_use("dex transform build --target dev --cold-deps", "b1"),
        _tool_result("b1", result, is_error),
    ]
    (call,) = summarize(events, Session()).dex_calls
    assert call["outcome"] == outcome


def test_two_builds_in_one_command_share_no_outcome():
    """One result cannot be split between two builds, so neither gets an outcome."""

    from evals.metadata.live import Session, summarize

    events = [
        _tool_use(
            "dex transform build --target dev && dex transform build --target dev --cold-deps",
            "b1",
        ),
        _tool_result(
            "b1", '{"status": "ok", "data": {"completion": {"outcome": "validated"}}}'
        ),
    ]
    calls = summarize(events, Session()).dex_calls
    assert [call.get("outcome") for call in calls] == [None, None]


def _step(verb, plan=None, verdict=None, outcome=None, flags=None):
    """A recorded transform call with the fields the procedure steps read."""

    return {
        "label": f"transform {verb}",
        "plan": plan,
        "verdict": verdict,
        "outcome": outcome,
        "flags": flags,
    }


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        # The repair applied and built, then the refactor compared and applied.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="validated"),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
            ],
            True,
        ),
        # The repair bundled into the refactor's own plan: nothing was built first.
        (
            [
                _step("build", outcome="failed"),
                _step("compare", "p1", None),
                _step("apply", "p1"),
                _step("build", outcome="validated"),
            ],
            False,
        ),
        # The repair applied, but its build failed before the comparison.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="failed"),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
            ],
            False,
        ),
        # A build that ran none of the repair's nodes did not build it.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="unrelated"),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
            ],
            False,
        ),
        # The repair built, then another apply that no build followed.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="validated"),
                _step("apply", "p2"),
                _step("compare", "p3", "equivalent"),
                _step("apply", "p3"),
            ],
            False,
        ),
        # A failed build, then one that built: the second counts.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="failed"),
                _step("build", outcome="validated"),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
            ],
            True,
        ),
        # A build whose envelope the agent's pipe hid.
        (
            [
                _step("apply", "p1"),
                _step("build"),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
            ],
            True,
        ),
        # Repaired and built, but the comparison came back not_checked.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="validated"),
                _step("compare", "p2", "not_checked"),
                _step("apply", "p2"),
            ],
            False,
        ),
        # Repaired and built, then the comparison of another plan.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="validated"),
                _step("compare", "p3", "equivalent"),
                _step("apply", "p2"),
            ],
            False,
        ),
    ],
    ids=[
        "repair-built-then-compared",
        "bundled",
        "repair-build-failed",
        "unrelated-build",
        "apply-after-the-build",
        "retried-build",
        "hidden-build",
        "not-checked",
        "other-plan",
    ],
)
def test_the_comparison_must_follow_a_built_repair(calls, passed):
    """``rebuilt_before_compare`` needs a usable comparison after a repair's apply and a build of it that did not fail."""

    from evals.metadata.score import procedure_steps

    graded = procedure_steps({"rebuilt_before_compare": True}, calls)
    assert graded["passed"] is passed


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        # Proven cold after the repair, before the refactor was compared.
        (
            [
                _step("apply", "p1"),
                _step("build", flags=["cold-deps"]),
                _step("compare", "p2", "equivalent"),
                _step("apply", "p2"),
                _step("build", flags=["verify"]),
            ],
            True,
        ),
        # Or after the refactor went in.
        (
            [
                _step("apply", "p1"),
                _step("apply", "p2"),
                _step("build", flags=["cold-deps"]),
            ],
            True,
        ),
        ([_step("build", flags=["cold-deps"]), _step("apply", "p1")], False),
        ([_step("apply", "p1"), _step("build", flags=["verify"])], False),
        # A cold build that failed shows nothing survives a reinstall.
        (
            [
                _step("apply", "p1"),
                _step("build", outcome="failed", flags=["cold-deps"]),
            ],
            False,
        ),
    ],
    ids=[
        "before-compare",
        "after-refactor",
        "before-any-apply",
        "never-cold",
        "cold-build-failed",
    ],
)
def test_cold_deps_after_repair_counts_after_any_apply(calls, passed):
    """``cold_deps_after_repair`` accepts a cold build after any apply, and none before."""

    from evals.metadata.score import procedure_steps

    graded = procedure_steps({"cold_deps_after_repair": True}, calls)
    assert graded["passed"] is passed


@needs_dbt
def test_package_blocked_refactor_flow(tmp_path, monkeypatch, capsys):
    """Real CLI, the sequence #1114 asks agents to follow: the blocked build, the
    refactor planned while blocked (the offer says so, the comparison is
    `not_checked`), the repair as its own plan proven by a `--cold-deps` build
    alone, then the refactor compared `equivalent` against that build, applied
    and built. Before #1114 the comparison after a cold-only build was still
    `inputs_unsynchronized`."""

    import json

    from exmergo_dex_core.cli import main

    task = _task(REFACTOR_PREREQUISITE)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)

    def dex(*args, expected="ok"):
        """Run the fixture CLI and check its expected envelope status."""

        main(["--repo-root", str(workspace), *args])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == expected, envelope.get("errors")
        return envelope

    def plan(name, edits):
        """Plan ``edits`` through the CLI, as a payload file named for the step."""

        path = workspace / f"{name}.json"
        path.write_text(json.dumps({"edits": edits}))
        return dex("transform", "plan", name, "--edits-file", str(path))

    reference = task.directory / "reference"

    def edit(path, kind="model_sql"):
        """One edit carrying the reference answer's content at ``path``."""

        return {"path": path, "kind": kind, "content": (reference / path).read_text()}

    blocked = dex("transform", "build", "--target", "dev", expected="error")
    first = blocked["data"]["prerequisites"][0]
    assert (first["owner"], first["package"]) == ("package", "tiny_roster")

    refactor = plan(
        "refactor",
        [
            edit("models/intermediate/int_team_members.sql"),
            edit("models/marts/team_member_summary.sql"),
        ],
    )
    refactor_id = refactor["data"]["plan_id"]
    offer = refactor["data"]["refactor_comparison_offer"]
    assert offer["baseline_ready"] is False
    (inputs,) = {
        (b["model"], b["owner"])
        for m in offer["per_model"]
        for b in m["blocked_inputs"]
    }
    assert inputs == ("roster_member", "package")
    waiting = dex(
        "transform", "compare", refactor_id, "--model", "team_member_summary"
    )["data"]
    assert waiting["reason_code"] == "inputs_unsynchronized"
    assert "--cold-deps" in waiting["detail"]

    repair = plan(
        "repair",
        [
            edit("models/overrides/roster_member.sql"),
            edit("dbt_project.yml", "project_yml"),
        ],
    )["data"]["plan_id"]
    dex("transform", "apply", repair)
    cold = dex(
        "transform", "build", "--target", "dev", "--cold-deps", "--for-plan", repair
    )
    assert cold["data"]["completion"]["outcome"] == "validated"

    compared = dex(
        "transform", "compare", refactor_id, "--model", "team_member_summary"
    )["data"]
    assert compared["status"] == "equivalent", compared["detail"]
    assert compared["inputs_built_by"]["cold"] is True

    applied = dex("transform", "apply", refactor_id)["data"]
    (state,) = [
        s for s in applied["refactor_comparison"] if s["model"] == "team_member_summary"
    ]
    assert state["status"] == "equivalent"
    assert state["inputs_built_by"]["cold"] is True
    built = dex(
        "transform", "build", "--target", "dev", "--verify", "--for-plan", refactor_id
    )
    completion = built["data"]["completion"]
    assert completion["outcome"] == "validated"
    assert any(
        r["model"] == "team_member_summary" and r["status"] == "equivalent"
        for r in completion["refactor_comparison"]
    )


# --- #1161: an override of a model its package documents ---------------------

_OVERRIDE_TASKS = (
    "package_prerequisite/adapt_project",
    REFACTOR_PREREQUISITE,
)


@pytest.mark.parametrize("name", _OVERRIDE_TASKS)
def test_the_package_documents_the_model_the_reference_overrides(name):
    """dbt gives a package's entry to the project's override and refuses a
    second, so these fixtures keep the condition #1161 is about: the model the
    agent has to override is documented by its package."""

    task = _task(name)
    fixture_dir = task.directory.parent.parent
    overridden = {
        p.stem
        for p in (task.directory / "reference" / "models" / "overrides").glob("*.sql")
    }
    assert overridden
    documented = {
        entry["name"]
        for path in (fixture_dir / "packages").glob("*/models/**/*.yml")
        for entry in (yaml.safe_load(path.read_text()) or {}).get("models", [])
    }
    assert overridden <= documented


def test_declaring_a_documented_override_is_refused_and_its_readers_are_not(
    tmp_path, monkeypatch, capsys
):
    """Real CLI on the #1114 fixture: the override route names the package's
    entry, a declaration on the override is refused in dex's words naming both
    files, and the same repair without it plans with no declaration owed."""

    import json

    from exmergo_dex_core.cli import main

    task = _task(REFACTOR_PREREQUISITE)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)
    entry = "dbt_packages/tiny_roster/models/roster_member.yml"

    def dex(*args, expected="ok"):
        """Run the fixture CLI and check its expected envelope status."""

        main(["--repo-root", str(workspace), *args])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == expected, envelope.get("errors")
        return envelope

    blocked = dex("transform", "build", "--target", "dev", expected="error")
    [override] = [
        o
        for p in blocked["data"]["prerequisites"]
        if p["node"] == "roster_member"
        for o in p["options"]
        if o["kind"] == "project_override"
    ]
    assert override["package_entry"] == entry

    reference = task.directory / "reference"
    edits = [
        {
            "path": path,
            "kind": kind,
            "content": (reference / path).read_text(),
        }
        for path, kind in (
            ("models/overrides/roster_member.sql", "model_sql"),
            ("dbt_project.yml", "project_yml"),
        )
    ]
    declared = workspace / "declared.json"
    declared.write_text(
        json.dumps(
            {
                "edits": edits,
                "declarations": [
                    {
                        "model": "roster_member",
                        "description": "One row per member.",
                        "grain": ["member_id"],
                    }
                ],
            }
        )
    )
    refused = dex(
        "transform", "plan", "repair", "--edits-file", str(declared), expected="error"
    )
    [message] = refused["errors"]
    assert entry in message
    assert "models/overrides/roster_member.yml" in message

    plain = workspace / "plain.json"
    plain.write_text(json.dumps({"edits": edits}))
    planned = dex("transform", "plan", "repair", "--edits-file", str(plain))
    owed = [
        w
        for w in planned["warnings"]
        if "roster_member" in w and ("declares nothing" in w or "declares no " in w)
    ]
    assert owed == []
    assert planned["data"]["declarations"]["roster_member"]["package_documented"] == {
        "package": "tiny_roster",
        "package_entry": entry,
    }


# --- scope (#1115): which populations a repair changes, and what it keeps -------

_SANDBOX = "sandbox_scope"
_DIM_SQL = "models/marts/dim_accounts.sql"
_MARTS_YML = "models/marts/_marts.yml"
_ROSTER_FILTER = (
    "select account_id, account_name, region, is_sandbox\n"
    "from {{ ref('stg_accounts') }}\nwhere not is_sandbox\n"
)


def _roster_declared(document: dict, evidence: str) -> None:
    """Declare the directory's sandbox exclusion, with ``evidence``."""

    entry = next(m for m in document["models"] if m["name"] == "dim_accounts")
    entry["config"] = {
        "meta": {
            "dex": {
                "grain": ["account_id"],
                "population": {
                    "rule": "Customer accounts only.",
                    "filters": [{"column": "is_sandbox", "excludes": [True]}],
                },
                "assumptions": [
                    {
                        "decision": "Whether sandbox accounts stay in the directory",
                        "chosen": "sandbox accounts excluded",
                        "evidence": evidence,
                        "column": "is_sandbox",
                    }
                ],
            }
        }
    }


def _sandbox_with(tmp_path: Path, task: str, *, roster: str | None, declared=None):
    """A sandbox task's reference answer, with the directory rewritten."""

    task_, fixture, workspace = _reference(tmp_path, f"{_SANDBOX}/{task}")
    if roster is not None:
        (workspace / _DIM_SQL).write_text(roster, encoding="utf-8")
    if declared is not None:
        _edit_yaml(workspace / _MARTS_YML, lambda d: _roster_declared(d, declared))
    return score_workspace(task_, fixture, workspace)


def test_the_scope_fixture_covers_narrow_broad_and_ambiguous_requests():
    keys = {t.key for t in TASKS if t.key.startswith(_SANDBOX)}
    assert keys == {f"{_SANDBOX}/{k}" for k in ("narrow", "broad", "ambiguous")}
    allowed = {
        k: _task(f"{_SANDBOX}/{k}").truth["scope"]["dim_accounts"]["allowed"]
        for k in ("narrow", "broad", "ambiguous")
    }
    assert allowed == {
        "narrow": ["kept"],
        "broad": ["excluded"],
        "ambiguous": ["kept", "excluded"],
    }


@needs_dbt
def test_a_narrow_request_that_also_filters_the_roster_fails_scope(tmp_path):
    score = _sandbox_with(tmp_path, "narrow", roster=_ROSTER_FILTER)
    assert score.scope == 0.0
    assert score.scope_models[0]["measured"] == "excluded"
    assert score.scope_models[0]["declared"] == "kept"
    assert _failed(score) == ["the directory still lists every account"]


@needs_dbt
def test_declaring_a_roster_filter_the_narrow_request_never_asked_for_still_fails(
    tmp_path,
):
    score = _sandbox_with(tmp_path, "narrow", roster=_ROSTER_FILTER, declared="default")
    assert score.scope == 0.0
    assert score.scope_models[0]["declared"] == "excluded"


@needs_dbt
def test_a_broad_request_that_leaves_the_roster_fails_scope(tmp_path):
    task, fixture, workspace = _reference(tmp_path, f"{_SANDBOX}/broad")
    start = fixture_of(task, FIXTURES).project / _DIM_SQL
    (workspace / _DIM_SQL).write_text(start.read_text(encoding="utf-8"))
    score = score_workspace(task, fixture, workspace)
    assert score.scope == 0.0
    assert "the directory lists every customer account" in _failed(score)


@needs_dbt
def test_the_ambiguous_roster_passes_either_way_when_the_choice_is_recorded(tmp_path):
    kept = _sandbox_with(tmp_path / "kept", "ambiguous", roster=None)
    assert kept.scope == 1.0
    excluded = _sandbox_with(
        tmp_path / "excluded", "ambiguous", roster=_ROSTER_FILTER, declared="default"
    )
    assert excluded.scope == 1.0
    assert excluded.checks_passed == 1.0


@needs_dbt
def test_an_undocumented_roster_filter_fails_the_ambiguous_scope(tmp_path):
    score = _sandbox_with(tmp_path, "ambiguous", roster=_ROSTER_FILTER)
    assert score.scope == 0.0
    # Every data check still passes: only the scope grade sees it.
    assert score.checks_passed == 1.0


@needs_dbt
def test_an_ambiguous_roster_exclusion_claimed_as_the_request_fails_scope(tmp_path):
    score = _sandbox_with(
        tmp_path, "ambiguous", roster=_ROSTER_FILTER, declared="request"
    )
    assert score.scope == 0.0
    assert score.scope_models[0]["tiers"] == ["request"]


def test_a_scope_model_that_was_not_built_fails_rather_than_passing(tmp_path):
    from evals.metadata.fixtures import WAREHOUSE_FILE
    from evals.metadata.score import scope_grades

    (tmp_path / WAREHOUSE_FILE).write_bytes(b"")
    graded = scope_grades(
        tmp_path,
        {},
        {"dim_x": {"column": "flag", "value": True, "allowed": ["kept", "excluded"]}},
    )
    assert graded[0]["passed"] is False
    assert graded[0]["measured"] is None


def test_a_task_grading_no_scope_reports_none():
    from evals.metadata.score import scope_grades

    assert scope_grades(Path(), {}, None) is None


_VISITS = "visit_coverage/fill_missing_days"
_VISITS_SQL = "models/marts/daily_visits.sql"


def _visits_with(tmp_path: Path, sql: str):
    task, fixture, workspace = _reference(tmp_path, _VISITS)
    (workspace / _VISITS_SQL).write_text(sql, encoding="utf-8")
    return score_workspace(task, fixture, workspace)


def _visits_reference() -> str:
    task = _task(_VISITS)
    return (task.directory / "reference" / _VISITS_SQL).read_text(encoding="utf-8")


@needs_dbt
def test_filling_the_days_with_count_star_counts_one_on_an_empty_day(tmp_path):
    score = _visits_with(
        tmp_path, _visits_reference().replace("count(v.visit_id)", "count(*)")
    )
    assert _failed(score) == ["a day with no visits counts zero, not one and not NULL"]


@needs_dbt
def test_filling_the_days_with_a_zero_average_changes_what_null_meant(tmp_path):
    score = _visits_with(
        tmp_path,
        _visits_reference().replace(
            "avg(v.duration_minutes)", "coalesce(avg(v.duration_minutes), 0)"
        ),
    )
    assert _failed(score) == ["a day with no visits keeps a NULL average, not zero"]


@needs_dbt
def test_filling_the_days_and_dropping_a_column_fails_the_schema_check(tmp_path):
    score = _visits_with(
        tmp_path,
        _visits_reference().replace(
            ",\n    avg(v.duration_minutes) as avg_duration_minutes", ""
        ),
    )
    assert "the output columns and their types are the model's own" in _failed(score)


@needs_dbt
def test_the_starting_daily_visits_misses_the_quiet_days(tmp_path):
    task = _task(_VISITS)
    start = fixture_of(task, FIXTURES).project / _VISITS_SQL
    score = _visits_with(tmp_path, start.read_text(encoding="utf-8"))
    assert _failed(score) == ["every day from the first to the last visit has one row"]


@needs_dbt
def test_filling_the_days_and_retyping_a_count_fails_the_schema_check(tmp_path):
    score = _visits_with(
        tmp_path,
        _visits_reference().replace(
            "count(v.visit_id) as visits",
            "cast(count(v.visit_id) as integer) as visits",
        ),
    )
    assert _failed(score) == ["the output columns and their types are the model's own"]


def test_the_sandbox_decision_pattern_skips_the_decisions_around_it():
    """The attribution reads the exclusion the task turns on, not the conventional
    choices recorded beside it (a NULL flag read as false, the join that reads the
    flag, a region left with no customer account), which are rightly `default` or
    `data` (#1115)."""

    import re

    pattern = re.compile(
        _task(f"{_SANDBOX}/narrow").truth["attribution"]["decision_pattern"],
        re.IGNORECASE,
    )
    scope = [
        "Sandbox tenants in the summary leave them out of both counts",
        "Whether sandbox usage counts excluded",
        "Sandbox accounts in the directory removed",
        "Region list with the sandbox filter keep every region; exclude sandbox counts",
    ]
    around = [
        "A NULL is_sandbox flag read as a customer account, not a sandbox one",
        "Join usage to accounts to read is_sandbox inner join on account_id",
        "Regions whose only accounts are sandbox ones kept with zero counts",
    ]
    assert all(pattern.search(text) for text in scope)
    assert not any(pattern.search(text) for text in around)


@needs_dbt
def test_a_partial_directory_filter_fails_the_ambiguous_task(tmp_path):
    """Dropping some sandbox accounts from the directory is neither documented
    choice, and Scope alone would read the survivors as `kept`."""

    partial = _ROSTER_FILTER.replace(
        "where not is_sandbox", "where not (is_sandbox and account_id % 2 = 0)"
    )
    score = _sandbox_with(tmp_path, "ambiguous", roster=partial)
    assert _failed(score) == [
        "the directory keeps every customer account and all or none of the sandbox ones"
    ]


@needs_dbt
def test_a_directory_keeping_only_sandbox_accounts_fails_the_ambiguous_task(tmp_path):
    inverted = _ROSTER_FILTER.replace("where not is_sandbox", "where is_sandbox")
    score = _sandbox_with(tmp_path, "ambiguous", roster=inverted)
    assert _failed(score) == [
        "the directory keeps every customer account and all or none of the sandbox ones"
    ]


@needs_dbt
def test_a_shifted_range_that_drops_a_visit_day_fails_the_unchanged_check(tmp_path):
    """One day earlier at the start and one day short at the end: the day count
    holds, and only comparing every source date catches the missing one."""

    shifted = _visits_reference().replace(
        "select min(visited_on) as first_day, max(visited_on) as last_day",
        "select min(visited_on) - 1 as first_day, max(visited_on) - 1 as last_day",
    )
    score = _visits_with(tmp_path, shifted)
    assert "days with visits are unchanged" in _failed(score)


# --- the toggle check judges compiled SQL (#1156) ---------------------------

_GATED = "gated_fees/close_fee_leak"


@needs_dbt
def test_the_starting_ledger_leaks_its_fee_total_off(tmp_path):
    """The negative control: the project as it starts fails only the off build,
    on the ledger; every gated shape (the macro, the gated group-by count, the
    disabled report) already holds."""

    task, fixture, workspace = _reference(tmp_path, _GATED)
    path = "models/finance/ledger_daily.sql"
    (workspace / path).write_text(
        (fixture.project / path).read_text(encoding="utf-8"), encoding="utf-8"
    )
    score = score_workspace(task, fixture, workspace)
    assert score.status == "scored"
    assert _failed(score) == [
        "fees_off: no reporting model exposes a fee column",
        "fees_off: the ledger has exactly its other columns",
    ]


@needs_dbt
def test_the_toggle_check_judges_every_gated_shape_in_the_fixture(
    tmp_path, monkeypatch, capsys
):
    """Real CLI on the fixture: the reference fix planned with a toggle contract.

    The merchant report's gated group-by count leaves its source unparseable
    once the Jinja is blanked, the order fact reads the flag through a macro, and
    the retired report is disabled. Each is judged, attributed or named on its
    own, and only the disabled report is left owed.
    """

    import json

    from exmergo_dex_core.cli import main

    task = _task(_GATED)
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)
    path = "models/finance/ledger_daily.sql"
    payload = workspace / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": path,
                        "kind": "model_sql",
                        "content": (task.directory / "reference" / path).read_text(),
                    }
                ],
                "toggle": {
                    "variable": "enable_platform_fees",
                    "on_value": True,
                    "off_value": False,
                    "columns": [
                        "platform_fee",
                        "net_amount",
                        "platform_fees",
                        "high_fee",
                        "total_fees",
                        "lifetime_fees",
                    ],
                    "exempt_models": ["stg_orders", "stg_ledger_postings"],
                },
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(workspace),
            "transform",
            "plan",
            "close the fee leak",
            "--edits-file",
            str(payload),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok", envelope.get("errors")
    toggle = envelope["data"]["toggle"]
    models = {r["model"]: r for r in toggle["models"]}
    assert not any("could not parse" in limit for limit in toggle["limits"])
    for name in ("fct_orders", "rpt_merchant_revenue", "ledger_daily"):
        assert models[name]["status"] == "satisfied", models[name]
    assert models["rpt_merchant_revenue"]["produces"] == ["high_fee", "platform_fees"]
    (macro,) = toggle["macro_reads"]
    assert macro["path"] == "macros/fee_columns.sql"
    assert macro["models"] == ["fct_orders"]
    assert toggle["not_compiled"] == [
        {"model": "rpt_fee_history", "path": "models/legacy/rpt_fee_history.sql"}
    ]
    assert toggle["verdict"] == "unchecked"
    assert len(toggle["limits"]) == 1


# --- intended repairs (#1157): a change meant to move rows, held to its statement --

_INTENDED = ("backfill_days", "exclude_archived", "late_arrivals")


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        # Compared with no expectation: a change meant to move rows reads
        # `different`, which says nothing about which rows moved.
        ([("compare", "p1", "different", []), ("apply", "p1")], False),
        ([("compare", "p1", "as_expected", ["expect"]), ("apply", "p1")], True),
        ([("compare", "p1", "as_expected", ["expect-file"]), ("apply", "p1")], True),
        # An unexpected change applied as it was.
        ([("compare", "p1", "unexpected", ["expect"]), ("apply", "p1")], False),
        # Fixed, planned again, compared again, then applied.
        (
            [
                ("compare", "p1", "unexpected", ["expect"]),
                ("compare", "p2", "as_expected", ["expect"]),
                ("apply", "p2"),
            ],
            True,
        ),
        ([("compare", "p1", "not_checked", ["expect"]), ("apply", "p1")], False),
        # Compared only after the apply: the baseline is gone.
        ([("apply", "p1"), ("compare", "p1", "as_expected", ["expect"])], False),
        # A verdict the agent's pipe hid.
        ([("compare", "p1", None, ["expect"]), ("apply", "p1")], True),
    ],
)
def test_an_intended_change_is_compared_against_its_statement(calls, passed):
    from evals.metadata.score import procedure_steps

    def call(verb, plan=None, verdict=None, flags=()):
        return {
            "label": f"transform {verb}",
            "plan": plan,
            "verdict": verdict,
            "flags": list(flags),
        }

    graded = procedure_steps(
        {"compare_expected_before_apply": True}, [call(*spec) for spec in calls]
    )
    assert graded["passed"] is passed


def test_the_expected_change_verdicts_are_read_from_an_envelope():
    from evals.metadata.live import _compare_verdict

    for verdict in ("as_expected", "unexpected"):
        text = json.dumps({"status": "ok", "data": {"status": verdict}})
        assert _compare_verdict({"content": text}) == verdict


def test_each_intended_repair_states_a_change_its_truth_can_check():
    for name in _INTENDED:
        task = _task(f"intended_repairs/{name}")
        stated = task.truth["expected_change"]
        assert stated["model"] in task.models
        assert task.truth["procedure"]["compare_expected_before_apply"] is True
        assert (task.directory / "overbroad").is_dir()


@needs_dbt
@pytest.mark.parametrize("name", _INTENDED)
def test_compare_expect_passes_the_repair_and_flags_its_over_broad_variant(
    name, tmp_path, monkeypatch, capsys
):
    """Each repair is ``as_expected`` against the change its truth states, and
    its over-broad variant, which passes its own build, is ``unexpected``."""

    from exmergo_dex_core.cli import main

    task = _task(f"intended_repairs/{name}")
    fixture = fixture_of(task, FIXTURES)
    workspace = materialize(task, fixture, tmp_path / "ws")
    monkeypatch.chdir(workspace)
    stated = task.truth["expected_change"]
    expect = workspace / "expected_change.json"
    expect.write_text(json.dumps(stated["expect"]), encoding="utf-8")

    def dex(*args, expected="ok"):
        main(["--repo-root", str(workspace), *args])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == expected, envelope.get("errors")
        return envelope

    dex("transform", "build", "--target", "dev", "--confirm")
    verdicts = {}
    for variant in ("reference", "overbroad"):
        root = task.directory / variant
        edits = [
            {
                "path": str(path.relative_to(root)),
                "kind": "model_sql" if path.suffix == ".sql" else "schema_yml",
                "content": path.read_text(encoding="utf-8"),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        payload = workspace / f"{variant}.json"
        payload.write_text(json.dumps({"edits": edits}), encoding="utf-8")
        planned = dex("transform", "plan", name, "--edits-file", str(payload))
        compared = dex(
            "transform",
            "compare",
            planned["data"]["plan_id"],
            "--model",
            stated["model"],
            "--key",
            stated["key"],
            "--expect-file",
            str(expect),
        )
        verdicts[variant] = compared["data"]
    assert verdicts["reference"]["status"] == "as_expected", verdicts["reference"]
    assert verdicts["overbroad"]["status"] == "unexpected", verdicts["overbroad"]
    assert verdicts["overbroad"]["expectation"]["findings"]
