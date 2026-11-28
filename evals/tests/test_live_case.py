"""A case that runs in a workspace of its own and is graded from its tool calls.

Three things the live backend did not do before #1480: give a case a fresh
working directory, set up by ``dex`` commands the agent never sees; start the
agent with the case's tool permissions and with nothing from the machine's own
settings; and capture the ``stream-json`` transcript, so assertions about which
commands ran, and in what order, are graded deterministically next to the
judged ones. ``subprocess.run`` is patched out, so no model and no engine run.

The transcripts are written here, in the shape a real headless run has: an
``init`` event naming the plugins that loaded, assistant ``tool_use`` blocks,
user ``tool_result`` blocks, and a closing ``result`` event. The failed sign-in
is the shape a real one had: subtype ``success``, with only ``is_error`` and
``terminal_reason`` saying the run never happened.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from evals import checks, claude_agent
from evals.__main__ import main
from evals.claude_agent import agent_args
from evals.runner import AgentResult, Verdict, read_verdict, run_quality, run_uplift
from evals.suite import (
    AgentConfig,
    Check,
    EvalCase,
    EvalSuite,
    InvalidSuiteError,
    _suite_from_dict,
    load_suite,
)

TEST = "unique_stg_order_items_order_item_id"
WRAP = 'uv run --no-project --script "/plugin/skills/maintain/scripts/run.py"'
ELEMENTARY = "main_elementary|elementary_test_results|dbt_invocations"
# Text an agent's tool call carries; nothing here runs it.
READ_ORDER_ITEMS = "select count(*) from main.order_items"
READ_ELEMENTARY = "select * from main_elementary.dbt_invocations"

FIRST = Check(
    check="first_dex_command",
    assertion="Called `dex maintain incidents` before any other `maintain` command",
    group="maintain",
    command="maintain incidents",
)
NO_ELEMENTARY = Check(
    check="no_dex_command",
    assertion="Did not query Elementary's tables with `explore query`",
    command="explore query",
    matching=ELEMENTARY,
)
NO_WRITES = Check(check="no_file_writes", assertion="Wrote nothing")
CHECKS = [FIRST, NO_ELEMENTARY, NO_WRITES]


# --- transcripts ---------------------------------------------------------------


def _init(plugin_path: str | None = "/plugin") -> dict:
    plugins = [{"name": "cc-plugin-telemetry", "path": "builtin"}]
    if plugin_path is not None:
        plugins.insert(0, {"name": "dex", "path": plugin_path, "version": "1.19.1"})
    return {
        "type": "system",
        "subtype": "init",
        "cwd": "/work",
        "model": "claude-opus-5-5",
        "permissionMode": "acceptEdits",
        "plugins": plugins,
        "skills": ["dex:explore", "dex:maintain", "dex:transform"],
    }


def _use(n: int, name: str, payload: dict) -> dict:
    block = {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": payload}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def _bash(n: int, command: str) -> dict:
    return _use(n, "Bash", {"command": command})


def _res(n: int, text: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": text}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _done(text: str, **extra) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "num_turns": 6,
        "result": text,
        **extra,
    }


ANSWER = (
    f"The `{TEST}` test started failing on 2026-11-20. The loader delivered rows of "
    "order_items more than once. The order_items semantic model's entity order_item "
    "reads the column. The fix is for transform to author: keep one row per id."
)


def passing(plugin_path: str = "/plugin") -> list[dict]:
    """The procedure followed: incidents first, then diagnose, nothing written."""

    return [
        _init(plugin_path),
        _use(1, "Skill", {"skill": "dex:maintain"}),
        _res(1, "Launching skill: dex:maintain"),
        _bash(2, f"{WRAP} maintain incidents stg_order_items"),
        _res(2, '{"status": "ok"}'),
        _bash(3, f"{WRAP} maintain diagnose stg_order_items"),
        _res(3, '{"status": "ok"}'),
        _bash(4, f'{WRAP} explore query "{READ_ORDER_ITEMS}"'),
        _res(4, '{"status": "ok"}'),
        _use(5, "Read", {"file_path": "/work/demo_project/dbt/models/x.sql"}),
        _res(5, "select 1"),
        _done(ANSWER),
    ]


def failing(plugin_path: str = "/plugin") -> list[dict]:
    """Every check broken: `maintain check` first, Elementary read, a write."""

    return [
        _init(plugin_path),
        _bash(1, f"{WRAP} maintain check"),
        _res(1, '{"status": "error"}'),
        _bash(2, f'{WRAP} explore query "{READ_ELEMENTARY}"'),
        _res(2, '{"status": "ok"}'),
        _bash(3, f"{WRAP} maintain incidents"),
        _res(3, '{"status": "ok"}'),
        _use(4, "Write", {"file_path": "/work/demo_project/edits.json"}),
        _res(4, "File created"),
        _done("A uniqueness test is failing; I could not tell why."),
    ]


def signin_failed(plugin_path: str = "/plugin") -> list[dict]:
    """A run that never signed in. Its result still reads subtype success."""

    return [
        _init(plugin_path),
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "terminal_reason": "api_error",
            "num_turns": 1,
            "total_cost_usd": 0,
            "result": "Failed to authenticate: OAuth session expired",
        },
    ]


def _jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _case(**extra) -> EvalCase:
    return EvalCase(
        id=7,
        prompt=f"the {TEST} test started failing; work out why",
        assertions=["Named the cause"],
        setup=[["demo"]],
        agent=AgentConfig(
            permission_mode="acceptEdits",
            allowed_tools=["Bash(uv run *)"],
            disallowed_tools=["PowerShell"],
            timeout=900,
        ),
        checks=list(CHECKS),
        **extra,
    )


# --- the suite shape -----------------------------------------------------------


def _entry(**extra) -> dict:
    return {"id": 7, "prompt": "p", "assertions": ["a"], **extra}


def test_a_case_with_setup_agent_and_checks_loads_as_written(tmp_path):
    suite = _suite_from_dict(
        {
            "skill_name": "maintain",
            "evals": [
                _entry(
                    setup=[["demo"]],
                    agent={"permission_mode": "acceptEdits", "allowed_tools": ["Bash"]},
                    checks=[
                        {
                            "check": "no_dex_command",
                            "assertion": "x",
                            "command": "explore query",
                            "matching": ELEMENTARY,
                        }
                    ],
                ),
                _entry(id=8),
            ],
        },
        tmp_path / "evals.json",
    )
    case, plain = suite.evals
    assert case.setup == [["demo"]]
    assert case.agent.permission_mode == "acceptEdits"
    assert case.checks[0].matching == ELEMENTARY
    assert case.streamed and not plain.streamed


@pytest.mark.parametrize(
    "extra",
    [
        {"setup": "demo"},
        {"setup": [[]]},
        {"agent": {"permision_mode": "acceptEdits"}},
        {"agent": {"allowed_tools": "Bash"}},
        {"checks": [{"check": "first_command", "assertion": "x"}]},
        {"checks": [{"check": "no_file_writes"}]},
        {
            "checks": [
                {"check": "first_dex_command", "assertion": "x", "group": "maintain"}
            ]
        },
        {
            "checks": [
                {
                    "check": "no_dex_command",
                    "assertion": "x",
                    "command": "q",
                    "matching": "(",
                }
            ]
        },
        {"checks": [{"check": "no_file_writes", "assertion": "x", "colour": "red"}]},
        {"agent": {"timeout": "600"}},
        {"agent": {"max_turns": 0}},
        {"agent": {"permission_mode": 1}},
        {
            "checks": [
                {
                    "check": "first_dex_command",
                    "assertion": "x",
                    "group": "maintain",
                    "command": "dex maintain incidents",
                }
            ]
        },
        {
            "checks": [
                {
                    "check": "no_dex_command",
                    "assertion": "x",
                    "command": "explore query",
                    "matching": ["main_elementary"],
                }
            ]
        },
    ],
)
def test_a_malformed_case_is_refused_with_its_place(tmp_path, extra):
    with pytest.raises(InvalidSuiteError, match=r"evals\[0\]"):
        _suite_from_dict(
            {"skill_name": "maintain", "evals": [_entry(**extra)]}, tmp_path
        )


# --- the checks, read from the tool calls --------------------------------------


def test_the_procedure_followed_passes_every_check():
    events = passing()
    assert (
        checks.run_error(events, skill_enabled=True, plugin_dir=Path("/plugin")) is None
    )
    for check in CHECKS:
        assert checks.grade(check, events)[0], check.check


def test_each_check_fails_on_the_transcript_that_breaks_it():
    events = failing()
    assert checks.grade(FIRST, events) == (
        False,
        "the first `maintain` command is `maintain check` (Dex call 1)",
    )
    assert checks.grade(NO_ELEMENTARY, events) == (
        False,
        f"1 `explore query` call(s) match `{ELEMENTARY}`",
    )
    assert checks.grade(NO_WRITES, events) == (False, "wrote with ['Write']")


def test_a_transform_apply_counts_as_a_write():
    events = [*passing()[:-1], _bash(9, f"{WRAP} transform apply"), _done(ANSWER)]
    passed, detail = checks.grade(NO_WRITES, events)
    assert not passed and "transform apply" in detail


def test_the_order_is_the_shells_within_one_command():
    events = [
        _init(),
        _bash(1, f"{WRAP} maintain check; {WRAP} maintain incidents"),
        _done(ANSWER),
    ]
    assert not checks.grade(FIRST, events)[0]


def test_a_dex_command_that_is_only_quoted_text_is_not_a_call():
    events = [
        _init(),
        _bash(1, 'echo "run dex maintain check later"'),
        _bash(2, f"{WRAP} maintain incidents"),
        _done(ANSWER),
    ]
    assert checks.grade(FIRST, events)[0]


def test_a_run_that_made_no_dex_call_cannot_pass_a_forbidden_call_check():
    events = [_init(), _done("I could not run anything.")]
    assert checks.grade(NO_ELEMENTARY, events) == (
        False,
        "no Dex command ran, so nothing was checked",
    )
    assert checks.grade(FIRST, events) == (False, "no `maintain` command ran")


def test_an_explore_query_of_another_table_is_not_an_elementary_read():
    passed, detail = checks.grade(NO_ELEMENTARY, passing())
    assert passed and detail == "1 `explore query` call(s), none matching"


# --- a run that did not happen is an error, never a pass -----------------------


class _PassEverything:
    def grade(self, case, assertion, result):
        return True


def test_a_failed_sign_in_is_an_error_and_passes_nothing():
    events = signin_failed()
    error = checks.run_error(events, skill_enabled=True, plugin_dir=Path("/plugin"))
    assert error and "api_error" in error

    class Agent:
        def run(self, prompt, *, skill_enabled, case=None):
            return AgentResult(
                output=checks.final_text(events), events=events, error=error
            )

    suite = EvalSuite(skill_name="maintain", evals=[_case()])
    [report] = run_quality(suite, Agent(), _PassEverything())
    assert not report.passed
    assert report.error == error
    graded = {a.assertion: a for a in report.assertions}
    assert all(not graded[c.assertion].passed for c in CHECKS)


@pytest.mark.parametrize(
    "events, words",
    [
        ([], "no init event"),
        ([_init()], "no result event"),
        ([_init(), _done("x", subtype="error_max_turns")], "error_max_turns"),
        (
            [_init(), _done("x", subtype="error_max_budget_usd")],
            "error_max_budget_usd",
        ),
        (
            [_init(), _done("x", terminal_reason="api_error", is_error=False)],
            "api_error",
        ),
    ],
)
def test_a_transcript_that_does_not_describe_a_finished_run_is_an_error(events, words):
    assert words in checks.run_error(events, skill_enabled=True)


def test_the_skill_arm_must_load_the_plugin_under_test():
    error = checks.run_error(
        passing("/other/dex"), skill_enabled=True, plugin_dir=Path("/plugin")
    )
    assert error and "did not load" in error


def test_the_baseline_arm_must_load_no_dex_plugin():
    error = checks.run_error(passing("/installed/dex"), skill_enabled=False)
    assert error and "baseline arm loaded" in error
    assert checks.run_error(passing(None), skill_enabled=False) is None


# --- the live backend: workspace, setup, flags, transcript ---------------------


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    wrapper = root / "skills" / "maintain" / "scripts" / "run.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("", encoding="utf-8")
    return root


@pytest.fixture
def cli(monkeypatch, plugin):
    """Patch out ``subprocess.run``: ``uv`` writes the demo's file into its cwd
    and answers ok; ``claude`` answers with the transcript ``cli.events``
    holds (a passing run that loaded ``plugin``), and records its cwd."""

    class Recorder:
        calls: ClassVar[list[dict]] = []
        events: ClassVar[list[dict]] = passing(str(plugin))
        setup_status = "ok"
        judge_answer: str | dict | Exception = "PASS"
        baseline_events: ClassVar[list[dict] | None] = None

    def fake_run(cmd, **kwargs):
        cwd = kwargs.get("cwd")
        Recorder.calls.append(
            {
                "cmd": cmd,
                "cwd": cwd,
                "seen": sorted(p.name for p in Path(cwd).iterdir()) if cwd else None,
                "input": kwargs.get("input"),
            }
        )
        if cmd[0] == "uv":
            (Path(cwd) / "dex_demo.duckdb").write_text("", encoding="utf-8")
            out = json.dumps({"status": Recorder.setup_status, "errors": ["no room"]})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if "stream-json" in cmd:
            events = Recorder.events
            if Recorder.baseline_events is not None and "--plugin-dir" not in cmd:
                events = Recorder.baseline_events
            return subprocess.CompletedProcess(cmd, 0, stdout=_jsonl(events), stderr="")
        if isinstance(Recorder.judge_answer, Exception):
            raise Recorder.judge_answer
        answer = Recorder.judge_answer
        out = json.dumps(answer if isinstance(answer, dict) else {"result": answer})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(claude_agent.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(claude_agent.subprocess, "run", fake_run)
    Recorder.calls = []
    return Recorder


def _agent(plugin: Path, **extra) -> claude_agent.ClaudeCliAgent:
    return claude_agent.ClaudeCliAgent(
        skill_name="maintain",
        skill_args=["--plugin-dir", str(plugin)],
        plugin_dir=plugin,
        **extra,
    )


def test_setup_runs_the_plugins_wrapper_in_a_fresh_directory_the_agent_then_starts_in(
    cli, plugin, tmp_path
):
    result = _agent(plugin).run("why", skill_enabled=True, case=_case())

    setup, agent = cli.calls
    wrapper = plugin / "skills" / "maintain" / "scripts" / "run.py"
    assert setup["cmd"] == [
        "uv",
        "run",
        "--no-project",
        "--script",
        str(wrapper),
        "demo",
    ]
    assert setup["seen"] == [], "setup starts in an empty directory"
    assert agent["cwd"] == setup["cwd"]
    assert agent["seen"] == ["dex_demo.duckdb"], "the agent starts on the set-up demo"
    assert Path(agent["cwd"]) != Path.cwd()
    assert not Path(agent["cwd"]).exists(), "the workspace is removed afterwards"
    assert result.error is None


def test_each_run_gets_its_own_workspace(cli, plugin):
    agent = _agent(plugin)
    agent.run("why", skill_enabled=True, case=_case())
    agent.run("why", skill_enabled=True, case=_case())
    assert cli.calls[1]["cwd"] != cli.calls[3]["cwd"]
    assert cli.calls[3]["seen"] == ["dex_demo.duckdb"]


def test_keep_workspaces_keeps_the_directory(cli, plugin):
    _agent(plugin, keep_workspaces=True).run("why", skill_enabled=True, case=_case())
    assert Path(cli.calls[1]["cwd"]).is_dir()


def test_a_failed_setup_is_an_error_and_the_agent_never_starts(cli, plugin):
    cli.setup_status = "error"
    result = _agent(plugin).run("why", skill_enabled=True, case=_case())
    assert result.error == "setup `dex demo` returned error: no room"
    assert len(cli.calls) == 1


def _flag(cmd: list[str], name: str) -> list[str]:
    """The values that follow ``name`` up to the next flag."""

    start = cmd.index(name) + 1
    end = next((i for i in range(start, len(cmd)) if cmd[i].startswith("--")), len(cmd))
    return cmd[start:end]


@pytest.mark.parametrize("skill_enabled", [True, False])
def test_both_arms_start_isolated_with_the_cases_permissions_and_a_stream(
    cli, plugin, skill_enabled
):
    if not skill_enabled:
        cli.events = passing(None)
    _agent(plugin).run("why", skill_enabled=skill_enabled, case=_case())
    cmd = cli.calls[1]["cmd"]
    assert _flag(cmd, "--output-format") == ["stream-json"]
    assert "--verbose" in cmd
    assert _flag(cmd, "--setting-sources") == ["project"]
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert _flag(cmd, "--permission-mode") == ["acceptEdits"]
    assert _flag(cmd, "--allowedTools") == ["Bash(uv run *)"]
    assert _flag(cmd, "--disallowedTools") == ["PowerShell"]
    assert ("--plugin-dir" in cmd) is skill_enabled


def test_the_transcript_is_parsed_into_events_output_and_the_trigger(cli, plugin):
    result = _agent(plugin, trigger_marker="never-printed").run(
        "why", skill_enabled=True, case=_case()
    )
    assert result.output == ANSWER
    assert result.events == cli.events
    assert result.triggered, "the Skill tool call is the trigger"


def test_the_transcript_is_written_where_asked(cli, plugin, tmp_path):
    out = tmp_path / "transcripts"
    agent = _agent(plugin, transcripts=out)
    agent.run("why", skill_enabled=True, case=_case())
    agent.run("why", skill_enabled=True, case=_case())
    assert sorted(p.name for p in out.iterdir()) == [
        "case-7-skill-2.jsonl",
        "case-7-skill.jsonl",
    ]
    assert (
        checks.parse_stream((out / "case-7-skill.jsonl").read_text(encoding="utf-8"))
        == cli.events
    )


def test_a_failed_sign_in_through_the_backend_is_an_error(cli, plugin):
    cli.events = signin_failed(str(plugin))
    result = _agent(plugin).run("why", skill_enabled=True, case=_case())
    assert result.error and "api_error" in result.error


def test_a_timed_out_run_keeps_what_it_wrote(monkeypatch, plugin, tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "uv":
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"status": "ok"}', stderr=""
            )
        raise subprocess.TimeoutExpired(cmd, 900, output=_jsonl([_init(str(plugin))]))

    monkeypatch.setattr(claude_agent.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(claude_agent.subprocess, "run", fake_run)
    out = tmp_path / "t"
    result = _agent(plugin, transcripts=out).run(
        "why", skill_enabled=True, case=_case()
    )
    assert result.error == "timed out after 900s"
    assert (
        (out / "case-7-skill.jsonl")
        .read_text(encoding="utf-8")
        .startswith('{"type": "system"')
    )


def test_a_case_without_a_workspace_still_runs_as_before(cli, plugin):
    plain = EvalCase(id=1, prompt="hi", assertions=["a"])
    _agent(plugin).run("hi", skill_enabled=True, case=plain)
    [call] = cli.calls
    assert _flag(call["cmd"], "--output-format") == ["json"]
    assert call["cwd"] is None
    assert "--setting-sources" not in call["cmd"]


# --- through the scoring core and the CLI --------------------------------------


def test_the_core_grades_checks_from_the_backends_transcript(cli, plugin):
    suite = EvalSuite(skill_name="maintain", evals=[_case()])
    [report] = run_quality(suite, _agent(plugin), _PassEverything())
    assert report.passed
    kinds = [(a.graded_by, a.passed) for a in report.assertions]
    assert kinds == [("judge", True)] + [("check", True)] * 3

    cli.events = failing(str(plugin))
    [report] = run_quality(suite, _agent(plugin), _PassEverything())
    assert [a.passed for a in report.assertions if a.graded_by == "check"] == [
        False
    ] * 3


def test_case_with_uplift_runs_the_case_once_in_each_arm(cli, plugin, tmp_path):
    cli.baseline_events = failing(None)
    suite = _suite_file(tmp_path, plugin)
    code = main([str(suite), "--case", "7", "--uplift", "--plugin-dir", str(plugin)])
    agents = [c["cmd"] for c in cli.calls if "stream-json" in c["cmd"]]
    assert [("--plugin-dir" in cmd) for cmd in agents] == [True, False]
    # The baseline ran, with no dex, and wrote a file: a failed arm, not an
    # errored one, so the uplift is the skill arm's pass over it.
    assert code == 0


def test_an_errored_baseline_leaves_the_uplift_unmeasured_and_fails(
    cli, plugin, tmp_path, capsys
):
    """The baseline's transcript lists a dex plugin, so that arm is an error:
    scored as a failed arm it would credit the skill with +100%."""

    suite = _suite_file(tmp_path, plugin)
    code = main([str(suite), "--case", "7", "--uplift", "--plugin-dir", str(plugin)])
    printed = capsys.readouterr().out
    assert code == 1
    assert (
        "uplift not measured for #7: baseline arm: the baseline arm loaded" in printed
    )
    assert "uplift vs baseline: not measured" in printed


def test_uplift_without_case_is_an_error(cli):
    with pytest.raises(SystemExit) as exc:
        main(["skills/maintain", "--uplift"])
    assert exc.value.code == 2


# --- the committed case (#1480) ------------------------------------------------


def _demo_case() -> EvalCase:
    suite = load_suite(Path(__file__).resolve().parents[2] / "skills" / "maintain")
    [case] = [c for c in suite.evals if c.setup == [["demo"]]]
    return case


def test_the_committed_demo_case_runs_on_a_fresh_demo_with_the_records_flags():
    case = _demo_case()
    assert case.prompt == f"the {TEST} test started failing; work out why"
    assert case.agent.timeout == 900
    assert agent_args(case.agent) == [
        "--permission-mode",
        "acceptEdits",
        "--max-budget-usd",
        "5",
        "--allowedTools",
        "Bash(uv run *)",
        "--disallowedTools",
        "PowerShell",
    ]
    # The points about tool calls are checks; the rest is for the judge.
    kinds = sorted(c.check for c in case.checks)
    assert kinds == [
        "first_dex_command",
        "no_dex_command",
        "no_dex_command",
        "no_file_writes",
    ]


def _committed(case: EvalCase, events: list[dict]) -> dict[str, bool]:
    return {c.assertion: checks.grade(c, events)[0] for c in case.checks}


def test_the_committed_checks_pass_the_procedure_and_fail_each_departure():
    case = _demo_case()
    assert all(_committed(case, passing()).values())
    first, elementary, writes, plan = (c.assertion for c in case.checks)
    assert _committed(case, failing()) == {
        first: False,
        elementary: False,
        writes: False,
        plan: True,
    }
    planned = [
        *passing()[:-1],
        _bash(9, f'{WRAP} transform plan "keep one row" --edits-file -'),
        _done(ANSWER),
    ]
    # Storing a plan writes `.dex/plans`, so it fails "writes nothing" too.
    assert _committed(case, planned) == {
        first: True,
        elementary: True,
        writes: False,
        plan: False,
    }


@pytest.mark.parametrize(
    "table",
    [
        "dbt_invocations",
        "main_elementary.model_runs",
        "elementary_test_results",
        "dbt_run_results",
        "test_result_rows",
        "data_monitoring_metrics",
    ],
)
def test_the_committed_pattern_catches_each_of_elementarys_tables(table):
    case = _demo_case()
    [check] = [c for c in case.checks if c.command == "explore query"]
    read = " ".join(("select count(*) from", table))
    events = [
        *passing()[:-1],
        _bash(9, f'{WRAP} explore query "{read}"'),
        _done(ANSWER),
    ]
    assert not checks.grade(check, events)[0]


@pytest.mark.parametrize(
    "path, counted",
    [
        # The first live run's scratch file, in the workspace root.
        ("/work/.dex_probe.sql", False),
        ("/work/demo_project/dbt/models/staging/stg_order_items.sql", True),
        ("/work/.dex/config.yml", True),
    ],
)
def test_the_committed_write_check_counts_only_the_project(path, counted):
    case = _demo_case()
    [check] = [c for c in case.checks if c.check == "no_file_writes"]
    events = [
        *passing()[:-1],
        _use(9, "Write", {"file_path": path, "content": "select 1"}),
        _done(ANSWER),
    ]
    assert checks.grade(check, events)[0] is not counted
    assert bool(checks.information(check, events)) is not counted


# --- review follow-ups (#1625) ---------------------------------------------------


def test_an_errored_run_fails_a_case_whose_judge_would_pass_it():
    """A streamed case with judged assertions only: the error decides, in both
    the quality report and the uplift row."""

    case = _case()
    case.checks = []

    class Agent:
        def run(self, prompt, *, skill_enabled, case=None):
            return AgentResult(output="looks right", error="the baseline arm loaded")

    suite = EvalSuite(skill_name="maintain", evals=[case])
    [report] = run_quality(suite, Agent(), _PassEverything())
    assert not report.passed
    [row] = run_uplift(suite, Agent(), _PassEverything())
    assert not row.passed_with_skill and not row.passed_without_skill


def test_dex_calls_through_powershell_are_read_too():
    events = [
        _init(),
        _use(1, "PowerShell", {"command": f"{WRAP} maintain check"}),
        _bash(2, f"{WRAP} maintain incidents"),
        _done(ANSWER),
    ]
    assert checks.grade(FIRST, events) == (
        False,
        "the first `maintain` command is `maintain check` (Dex call 1)",
    )


@pytest.mark.parametrize(
    "command, writes",
    [
        ("transform init analytics --connector duckdb", True),
        ("transform deps", True),
        ("config set budget.ceiling 10 --confirm", True),
        ("config set budget.ceiling 10", False),
    ],
)
def test_a_dex_command_that_writes_the_project_counts_as_a_write(command, writes):
    events = [*passing()[:-1], _bash(9, f"{WRAP} {command}"), _done(ANSWER)]
    assert checks.grade(NO_WRITES, events)[0] is not writes


# --- the judge's verdict, made visible (#1480's first live run) ------------------


@pytest.mark.parametrize(
    "answer, passed, error",
    [
        ("PASS", True, False),
        ("**PASS**", True, False),
        ("Pass.", True, False),
        ("pass\n\nThe answer names the loader.", True, False),
        ("FAIL", False, False),
        ("**Fail**: no cause named", False, False),
        ("I would need more context.", False, True),
        ("", False, True),
    ],
)
def test_a_verdict_is_read_from_its_first_word(answer, passed, error):
    said = read_verdict(answer)
    assert (said.passed, said.error is not None, said.text) == (passed, error, answer)


class _SaysJudge:
    """A judge that answers one fixed text, or raises."""

    def __init__(self, answer):
        self.answer = answer

    def grade(self, case, assertion, result):
        raise AssertionError("the core reads verdict() where a judge has one")

    def verdict(self, case, assertion, result):
        if isinstance(self.answer, Exception):
            return Verdict(passed=False, error=f"{self.answer}")
        return read_verdict(self.answer)


class _Answers:
    def run(self, prompt, *, skill_enabled):
        return AgentResult(output="the loader delivered rows twice")


_PLAIN = EvalSuite(
    skill_name="maintain", evals=[EvalCase(id=1, prompt="why", assertions=["a"])]
)


@pytest.mark.parametrize("skill_enabled", [True, False], ids=["skill", "baseline"])
def test_a_judge_that_gives_no_verdict_is_a_run_error_in_each_arm(skill_enabled):
    judge = _SaysJudge(RuntimeError("judge timed out"))
    [report] = run_quality(_PLAIN, _Answers(), judge, skill_enabled=skill_enabled)
    assert not report.passed
    assert report.error == "the judge gave no verdict: judge timed out on 'a'"
    [graded] = report.assertions
    assert graded.detail == "no verdict: judge timed out"


@pytest.mark.parametrize("skill_enabled", [True, False], ids=["skill", "baseline"])
def test_a_bold_pass_passes_and_its_text_is_kept_in_each_arm(skill_enabled):
    [report] = run_quality(
        _PLAIN, _Answers(), _SaysJudge("**PASS**"), skill_enabled=skill_enabled
    )
    assert report.passed and report.error is None
    assert report.to_dict()["assertions"][0]["verdict"] == "**PASS**"


@pytest.mark.parametrize("skill_enabled", [True, False], ids=["skill", "baseline"])
def test_a_plain_fail_fails_with_no_error_in_each_arm(skill_enabled):
    [report] = run_quality(
        _PLAIN, _Answers(), _SaysJudge("FAIL"), skill_enabled=skill_enabled
    )
    assert not report.passed and report.error is None
    [graded] = report.assertions
    assert (graded.passed, graded.verdict, graded.detail) == (False, "FAIL", "")


def test_a_judge_that_gives_no_verdict_makes_the_uplift_row_an_error():
    [row] = run_uplift(_PLAIN, _Answers(), _SaysJudge("Hmm."))
    assert not row.passed_with_skill and not row.passed_without_skill
    assert row.error.startswith("skill arm: the judge gave no verdict")
    assert "; baseline arm: the judge gave no verdict" in row.error


def test_the_live_judge_sends_its_whole_prompt_on_stdin(cli):
    """cmd.exe cuts an argv argument at its first newline, so a judge prompt
    in argv reached the model as its first line, and the flags after it were
    dropped."""

    judge = claude_agent.ClaudeCliJudge(model="claude-opus-5-5")
    result = AgentResult(output="line one\nthe loader delivered rows twice")
    said = judge.verdict(_case(), "Names the cause", result)
    assert said == Verdict(passed=True, text="PASS")
    [call] = cli.calls
    assert not any("\n" in arg for arg in call["cmd"])
    assert _flag(call["cmd"], "--model") == ["claude-opus-5-5"]
    assert "Requirement:\nNames the cause" in call["input"]
    assert "the loader delivered rows twice" in call["input"]


def test_the_agent_sends_its_prompt_on_stdin(cli, plugin):
    _agent(plugin).run("why\nand how", skill_enabled=True, case=_case())
    agent = cli.calls[1]
    assert agent["input"] == "why\nand how"
    assert not any("\n" in arg for arg in agent["cmd"])


def test_the_live_judge_reports_an_exception_as_no_verdict(cli):
    cli.judge_answer = subprocess.TimeoutExpired(["claude"], 120)
    said = claude_agent.ClaudeCliJudge().verdict(_case(), "a", AgentResult(output="x"))
    assert not said.passed and said.error.startswith("TimeoutExpired")


def _suite_file(tmp_path: Path, plugin: Path) -> Path:
    path = tmp_path / "evals.json"
    entry = _entry(
        setup=[["demo"]],
        agent={"permission_mode": "acceptEdits"},
        checks=[{"check": "no_file_writes", "assertion": "Wrote nothing"}],
    )
    path.write_text(
        json.dumps({"skill_name": "maintain", "evals": [entry]}), encoding="utf-8"
    )
    (plugin / ".claude-plugin").mkdir(exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    return path


def test_the_cli_shows_the_judges_answer_and_writes_it_beside_the_transcript(
    cli, plugin, tmp_path, capsys
):
    cli.judge_answer = "**FAIL**: the cause is not named"
    out = tmp_path / "t"
    suite = _suite_file(tmp_path, plugin)
    args = [str(suite), "--case", "7", "--uplift", "--plugin-dir", str(plugin)]
    code = main([*args, "--transcripts", str(out)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "[judge answered: '**FAIL**: the cause is not named']" in printed
    names = sorted(p.name for p in out.iterdir())
    assert "case-7-skill.judge.json" in names
    assert "case-7-baseline.judge.json" in names
    graded = json.loads((out / "case-7-skill.judge.json").read_text(encoding="utf-8"))
    [judged] = [a for a in graded["assertions"] if a["graded_by"] == "judge"]
    assert judged["verdict"] == "**FAIL**: the cause is not named"


def test_the_cli_reports_a_judge_that_failed_as_a_run_error(
    cli, plugin, tmp_path, capsys
):
    cli.judge_answer = RuntimeError("OAuth session expired")
    suite = _suite_file(tmp_path, plugin)
    code = main([str(suite), "--case", "7", "--plugin-dir", str(plugin)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "run error: the judge gave no verdict: RuntimeError" in printed


# --- writes inside the project only (Dave's call after the first live run) -------

PROJECT_ONLY = Check(
    check="no_file_writes",
    assertion=(
        "Writes nothing to the project: no file edit there and no `transform apply`"
    ),
    within=["demo_project", ".dex"],
)


def _wrote(*uses: dict, cwd: str = "/work") -> list[dict]:
    init = {**_init(), "cwd": cwd} if cwd else {**_init(), "cwd": None}
    return [init, _bash(1, f"{WRAP} maintain incidents"), *uses, _done(ANSWER)]


def _write(n: int, path: str) -> dict:
    return _use(n, "Write", {"file_path": path, "content": "select 1"})


@pytest.mark.parametrize(
    "path",
    [
        "/work/demo_project/dbt/models/staging/stg_order_items.sql",
        "/work/.dex/config.yml",
        "demo_project/dbt/models/x.sql",
    ],
)
def test_a_write_inside_the_project_fails(path):
    passed, detail = checks.grade(PROJECT_ONLY, _wrote(_write(2, path)))
    assert not passed and "Write" in detail


def test_a_scratch_write_in_the_workspace_root_passes_and_is_listed():
    events = _wrote(_write(2, "/work/.dex_probe.sql"))
    assert checks.grade(PROJECT_ONLY, events) == (
        True,
        "no file-writing tool call in the project and no Dex write",
    )
    assert checks.information(PROJECT_ONLY, events) == (
        "wrote outside the project: ['.dex_probe.sql']"
    )


def test_a_windows_transcript_is_placed_the_same_on_any_os():
    sep = "\\"
    root = sep.join(["C:", "Users", "u", "AppData", "Local", "Temp", "dex-eval-7-ab"])
    scratch = _wrote(_write(2, sep.join([root, ".dex_probe.sql"])), cwd=root)
    assert checks.grade(PROJECT_ONLY, scratch)[0]
    assert ".dex_probe.sql" in checks.information(PROJECT_ONLY, scratch)
    model = sep.join([root, "demo_project", "dbt", "models", "x.sql"])
    assert not checks.grade(PROJECT_ONLY, _wrote(_write(2, model), cwd=root))[0]


def test_a_transform_apply_fails_wherever_the_files_are():
    events = _wrote(_bash(2, f"{WRAP} transform apply"))
    passed, detail = checks.grade(PROJECT_ONLY, events)
    assert not passed and "transform apply" in detail


def test_with_no_working_directory_to_read_every_write_counts():
    events = _wrote(_write(2, "/work/.dex_probe.sql"), cwd="")
    assert not checks.grade(PROJECT_ONLY, events)[0]


def test_without_within_every_write_still_counts():
    assert not checks.grade(NO_WRITES, _wrote(_write(2, "/work/.dex_probe.sql")))[0]


class _Replays:
    def __init__(self, events):
        self.events = events

    def run(self, prompt, *, skill_enabled, case=None):
        return AgentResult(output=ANSWER, events=self.events)


def _project_case() -> EvalSuite:
    case = _case()
    case.checks = [PROJECT_ONLY]
    return EvalSuite(skill_name="maintain", evals=[case])


@pytest.mark.parametrize("skill_enabled", [True, False], ids=["skill", "baseline"])
@pytest.mark.parametrize(
    "uses, passed, info",
    [
        ((_use(2, "Edit", {"file_path": "/work/demo_project/x.sql"}),), False, ""),
        (
            (_use(2, "Write", {"file_path": "/work/.dex_probe.sql"}),),
            True,
            "wrote outside the project: ['.dex_probe.sql']",
        ),
        ((_bash(2, f"{WRAP} transform apply"),), False, ""),
    ],
    ids=["project_write", "scratch_write", "transform_apply"],
)
def test_project_only_writes_in_each_arm(skill_enabled, uses, passed, info):
    suite = _project_case()
    agent = _Replays(_wrote(*uses))
    [report] = run_quality(suite, agent, _PassEverything(), skill_enabled=skill_enabled)
    assert report.passed is passed
    [check] = [a for a in report.to_dict()["assertions"] if a["graded_by"] == "check"]
    assert check.get("info", "") == info


def test_the_cli_prints_a_write_outside_the_project_as_a_note(capsys):
    from evals.__main__ import _print_quality

    events = _wrote(_write(2, "/work/.dex_probe.sql"))
    [report] = run_quality(_project_case(), _Replays(events), _PassEverything())
    _print_quality("maintain", [report])
    printed = capsys.readouterr().out
    assert "ok " in printed
    assert "note (Writes nothing to the project" in printed
    assert "['.dex_probe.sql']" in printed


@pytest.mark.parametrize(
    "raw",
    [
        {
            "check": "first_dex_command",
            "assertion": "x",
            "group": "maintain",
            "command": "maintain incidents",
            "within": ["demo_project"],
        },
        {"check": "no_file_writes", "assertion": "x", "within": "demo_project"},
        {"check": "no_file_writes", "assertion": "x", "within": [""]},
    ],
)
def test_within_is_a_list_of_paths_on_no_file_writes_only(tmp_path, raw):
    with pytest.raises(InvalidSuiteError, match="within"):
        _suite_from_dict(
            {"skill_name": "maintain", "evals": [_entry(checks=[raw])]}, tmp_path
        )


# --- the second cold review of #1625 ---------------------------------------------

BS = chr(92)
WIN_ROOT = BS.join(["C:", "Users", "u", "AppData", "Local", "Temp", "dex-eval-7-abc"])


@pytest.mark.parametrize(
    "answer",
    ["Failed to authenticate. API Error: 401", "PASSED", "Failure", "PASSING"],
)
def test_only_pass_or_fail_itself_is_a_verdict(answer):
    assert read_verdict(answer).error is not None


def test_a_judge_whose_cli_reports_an_error_gives_no_verdict(cli):
    cli.judge_answer = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Failed to authenticate. API Error: 401",
    }
    said = claude_agent.ClaudeCliJudge().verdict(_case(), "a", AgentResult(output="x"))
    assert not said.passed
    assert said.error.startswith("RuntimeError: claude reported an error (api_error)")


@pytest.mark.parametrize(
    "path",
    [
        # Git Bash's spelling of the same directory.
        "/c/Users/u/AppData/Local/Temp/dex-eval-7-abc/demo_project/x.sql",
        # An 8.3 short name above the workspace.
        BS.join(["C:", "Users", "U~1", "AppData", "Local", "Temp", "dex-eval-7-abc"])
        + BS
        + BS.join(["demo_project", "x.sql"]),
        # Another case.
        WIN_ROOT.upper() + BS + BS.join(["DEMO_PROJECT", "X.SQL"]),
    ],
    ids=["git_bash", "short_name", "case"],
)
def test_a_project_write_is_placed_however_the_root_is_spelled(path):
    events = _wrote(_write(2, path), cwd=WIN_ROOT)
    assert not checks.grade(PROJECT_ONLY, events)[0]


@pytest.mark.parametrize(
    "path, shown",
    [
        ("/srv/x.sql", "/srv/x.sql"),
        (BS.join(["D:", "x.sql"]), BS.join(["D:", "x.sql"])),
        ("../elsewhere.sql", "../elsewhere.sql"),
        (
            "/c/Users/u/AppData/Local/Temp/dex-eval-7-abc/.dex_probe.sql",
            ".dex_probe.sql",
        ),
    ],
    ids=["outside_root", "other_drive", "parent", "git_bash_scratch"],
)
def test_a_write_outside_the_project_passes_and_is_listed(path, shown):
    events = _wrote(_write(2, path), cwd=WIN_ROOT)
    assert checks.grade(PROJECT_ONLY, events)[0]
    assert checks.information(PROJECT_ONLY, events) == (
        f"wrote outside the project: [{shown!r}]"
    )


def test_a_refused_write_in_the_project_wrote_nothing():
    refused = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": "Permission to use Write has been denied.",
                    "is_error": True,
                }
            ]
        },
    }
    events = _wrote(_write(2, "/work/demo_project/x.sql"), refused)
    assert checks.grade(PROJECT_ONLY, events)[0]


def test_a_whitespace_command_is_refused_with_its_place(tmp_path):
    raw = {"check": "first_dex_command", "assertion": "x", "group": "maintain"}
    with pytest.raises(InvalidSuiteError, match=r"needs \['command'\]"):
        _suite_from_dict(
            {
                "skill_name": "maintain",
                "evals": [_entry(checks=[{**raw, "command": "  "}])],
            },
            tmp_path,
        )


def test_a_suite_whose_uplift_row_errored_does_not_pass():
    from evals.runner import SuiteReport, TriggeringReport, UpliftCaseReport

    clean = TriggeringReport(
        positives_total=0, positives_fired=0, negatives_total=0, negatives_fired=0
    )
    row = UpliftCaseReport(
        id=7,
        passed_with_skill=True,
        passed_without_skill=False,
        error="baseline arm: x",
    )
    report = SuiteReport(skill_name="maintain", triggering=clean, uplift_cases=[row])
    assert report.uplift_score == 0.0
    assert not report.passed
    assert report.to_dict()["uplift_cases"][0]["error"] == "baseline arm: x"


# --- the second cold review of #1626: making "nothing to the project" hold -------

BOOKKEEPING = [".dex/cache.json", ".dex/cache.lock", ".dex/queries.jsonl"]
PROJECT_ONLY_DIFFED = Check(
    check="no_file_writes",
    assertion="Writes nothing to the project",
    within=["demo_project", ".dex"],
    ignore=BOOKKEEPING,
)


@pytest.mark.parametrize(
    "changes, passed",
    [
        (["demo_project/dbt/models/staging/stg_order_items.sql"], False),
        ([".dex/snapshot.json"], False),
        ([".dex/plans/abc.json"], False),
        (BOOKKEEPING, True),
        (["q.sql"], True),
        ([], True),
    ],
    ids=["model", "snapshot", "plan", "bookkeeping", "scratch", "nothing"],
)
def test_a_change_in_the_project_fails_however_it_was_made(changes, passed):
    # A Bash command made the change: no file tool shows it.
    events = _wrote(_bash(2, "uv run python -c \"open('x','w')\""))
    assert checks.grade(PROJECT_ONLY_DIFFED, events, changes)[0] is passed


def test_a_scratch_change_is_listed_and_bookkeeping_is_not():
    events = _wrote()
    info = checks.information(PROJECT_ONLY_DIFFED, events, ["q.sql", *BOOKKEEPING])
    assert info == "wrote outside the project: ['q.sql']"


def test_the_backend_records_what_the_run_changed_in_its_workspace(
    cli, plugin, monkeypatch
):
    original = claude_agent.subprocess.run

    def run_and_write(cmd, **kwargs):
        if "stream-json" in cmd:
            cwd = Path(kwargs["cwd"])
            (cwd / "demo_project").mkdir(exist_ok=True)
            (cwd / "demo_project" / "x.sql").write_text("x", encoding="utf-8")
            (cwd / "q.sql").write_text("select 1", encoding="utf-8")
        return original(cmd, **kwargs)

    monkeypatch.setattr(claude_agent.subprocess, "run", run_and_write)
    result = _agent(plugin).run("why", skill_enabled=True, case=_case())
    assert result.changes == ["demo_project/x.sql", "q.sql"]
    assert not checks.grade(PROJECT_ONLY_DIFFED, result.events, result.changes)[0]


ELEMENTARY_SQL = " ".join(("select * from", "main_elementary.elementary_test_results"))


@pytest.mark.parametrize(
    "uses, passed",
    [
        (
            (
                _use(
                    2, "Write", {"file_path": "/work/q.sql", "content": ELEMENTARY_SQL}
                ),
                _bash(3, f"{WRAP} explore query --sql-file q.sql"),
            ),
            False,
        ),
        (
            (
                _use(2, "Write", {"file_path": "/work/q.sql", "content": "select 1"}),
                _bash(3, f"{WRAP} explore query --sql-file q.sql"),
            ),
            True,
        ),
        (
            (
                _bash(2, f"cat > q.sql <<'SQL'\n{ELEMENTARY_SQL}\nSQL"),
                _bash(3, f"{WRAP} explore query --sql-file q.sql"),
            ),
            False,
        ),
        ((_bash(3, f"{WRAP} explore query --sql-file q.sql"),), False),
    ],
    ids=["written_elementary", "written_clean", "heredoc", "unread_file"],
)
def test_a_sql_file_is_read_for_the_elementary_check(uses, passed):
    assert checks.grade(NO_ELEMENTARY, _wrote(*uses))[0] is passed


PLAN = Check(
    check="no_dex_command",
    assertion="Stores no plan",
    command="transform plan",
    matching=".",
)


def test_a_help_call_is_neither_a_plan_nor_a_write():
    events = _wrote(_bash(2, f"{WRAP} transform plan --help"))
    assert checks.grade(PLAN, events)[0]
    assert checks.grade(NO_WRITES, events)[0]


@pytest.mark.parametrize(
    "command, writes",
    [
        ("maintain snapshot", True),
        ("maintain reconcile", True),
        ("transform repair stg_x", True),
        ("transform place c --targets a,b --expr x", True),
        ("transform place c --targets a,b --expr x --explain", False),
        ("transform test --mutate stg_x", True),
        ("transform test stg_x", True),
        ("semantic define --edits-file e.json", True),
        ("transform references order_item_id", False),
    ],
)
def test_the_dex_calls_that_store_a_plan_or_snapshot_are_writes(command, writes):
    events = _wrote(_bash(2, f"{WRAP} {command}"))
    assert checks.grade(NO_WRITES, events)[0] is not writes


# --- CodeRabbit's review of #1625 and #1626 --------------------------------------


def _q(n: int, path: str = "q.sql") -> dict:
    return _bash(n, f"{WRAP} explore query --sql-file {path}")


def _w(n: int, path: str, content: str) -> dict:
    return _use(n, "Write", {"file_path": path, "content": content})


def test_a_sql_file_is_read_as_it_stood_when_the_query_ran():
    # Elementary SQL queried, then overwritten with clean SQL: still a read.
    events = _wrote(
        _w(2, "/work/q.sql", ELEMENTARY_SQL), _q(3), _w(4, "/work/q.sql", "select 1")
    )
    assert not checks.grade(NO_ELEMENTARY, events)[0]
    # Clean SQL queried, then overwritten with Elementary SQL: not a read.
    events = _wrote(
        _w(2, "/work/q.sql", "select 1"), _q(3), _w(4, "/work/q.sql", ELEMENTARY_SQL)
    )
    assert checks.grade(NO_ELEMENTARY, events)[0]
    # Queried before any write showed it: unread, so a match.
    events = _wrote(_q(2), _w(3, "/work/q.sql", "select 1"))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_a_sql_file_is_matched_by_its_path_not_its_name():
    events = _wrote(_w(2, "/work/scratch/q.sql", "select 1"), _q(3, "private/q.sql"))
    assert not checks.grade(NO_ELEMENTARY, events)[0]
    events = _wrote(_w(2, "/work/private/q.sql", "select 1"), _q(3, "private/q.sql"))
    assert checks.grade(NO_ELEMENTARY, events)[0]


def test_a_mention_of_the_file_is_not_its_contents():
    events = _wrote(_bash(2, "ls q.sql"), _q(3))
    assert not checks.grade(NO_ELEMENTARY, events)[0]
    # A shell command that writes it leaves the contents unknown too.
    events = _wrote(_bash(2, "echo 'select 1' > q.sql"), _q(3))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def _denial(n: int, tool: str) -> dict:
    return {
        "type": "system",
        "subtype": "permission_denied",
        "tool_name": tool,
        "tool_use_id": f"toolu_{n}",
    }


def test_a_refused_write_establishes_nothing():
    events = _wrote(_w(2, "/work/q.sql", "select 1"), _denial(2, "Write"), _q(3))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_a_denied_shell_request_is_not_a_call():
    events = [
        _init(),
        _bash(1, f"{WRAP} maintain incidents"),
        _denial(1, "Bash"),
        _bash(2, f"{WRAP} maintain diagnose"),
        _done(ANSWER),
    ]
    assert checks.grade(FIRST, events) == (
        False,
        "the first `maintain` command is `maintain diagnose` (Dex call 1)",
    )
    # Listed only in the result's permission_denials: the same.
    listed = {**_done(ANSWER), "permission_denials": [{"tool_use_id": "toolu_1"}]}
    events = [
        _init(),
        _bash(1, f"{WRAP} maintain incidents"),
        _bash(2, f"{WRAP} maintain diagnose"),
        listed,
    ]
    assert not checks.grade(FIRST, events)[0]


def test_a_call_that_ran_and_errored_still_counts():
    errored = _res(1, '{"status": "error"}')
    errored["message"]["content"][0]["is_error"] = True
    events = [_init(), _bash(1, f"{WRAP} maintain incidents"), errored, _done(ANSWER)]
    assert checks.grade(FIRST, events)[0]


def test_a_quoted_wrapper_path_with_spaces_is_one_token():
    parts = ["C:", "Program Files", "dex", "skills", "maintain", "scripts", "run.py"]
    path = chr(92).join(parts)
    command = f"& uv run --no-project --script '{path}' explore query --sql-file q.sql"
    events = [_init(), _use(1, "PowerShell", {"command": command}), _done(ANSWER)]
    [call] = checks.dex_calls(events)
    assert call["label"] == "explore query"
    assert "sql-file" in call["flags"]


def test_an_unscoped_write_check_counts_every_change_but_its_ignore_list():
    unscoped = Check(check="no_file_writes", assertion="x", ignore=[".dex/cache.json"])
    assert not checks.grade(unscoped, _wrote(), ["x.sql"])[0]
    assert checks.grade(unscoped, _wrote(), [".dex/cache.json"])[0]
    assert checks.grade(unscoped, _wrote(), [])[0]


def test_ignore_without_within_is_accepted(tmp_path):
    raw = {"check": "no_file_writes", "assertion": "x", "ignore": [".dex/cache.json"]}
    suite = _suite_from_dict(
        {"skill_name": "maintain", "evals": [_entry(checks=[raw])]}, tmp_path
    )
    assert suite.evals[0].checks[0].ignore == [".dex/cache.json"]


def test_within_dot_is_the_whole_workspace():
    whole = Check(check="no_file_writes", assertion="x", within=["."])
    assert not checks.grade(whole, _wrote(), ["x.sql"])[0]
    assert not checks.grade(whole, _wrote(_write(2, "/work/x.sql")))[0]
    assert checks.grade(whole, _wrote(_write(2, "/srv/x.sql")))[0]


def test_an_unreadable_file_is_not_shown_unchanged():
    before = {"a": "1", "b": "unreadable"}
    assert claude_agent.changed_files(before, dict(before)) == ["b"]


def test_a_streamed_launch_failure_is_a_case_error(monkeypatch, plugin):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "uv":
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"status": "ok"}', stderr=""
            )
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude_agent.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(claude_agent.subprocess, "run", fake_run)
    result = _agent(plugin).run("why", skill_enabled=True, case=_case())
    assert result.error.startswith("could not start claude")


@pytest.mark.parametrize("answer", ["PASS123", "PASS_FAIL", "FAIL2"])
def test_a_word_with_digits_or_underscores_is_no_verdict(answer):
    assert read_verdict(answer).error is not None


def test_a_suite_whose_quality_run_errored_does_not_pass():
    from evals.runner import CaseReport, SuiteReport, TriggeringReport

    clean = TriggeringReport(
        positives_total=0, positives_fired=0, negatives_total=0, negatives_fired=0
    )
    errored = CaseReport(id=7, prompt="p", error="timed out after 900s")
    report = SuiteReport(skill_name="maintain", triggering=clean, quality=[errored])
    assert not report.passed


def test_a_runner_without_a_case_keyword_cannot_run_a_streamed_case():
    class Plain:
        def run(self, prompt, *, skill_enabled):
            raise AssertionError("never called")

    suite = EvalSuite(skill_name="maintain", evals=[_case()])
    [report] = run_quality(suite, Plain(), _PassEverything())
    assert report.error.startswith("Plain.run takes no `case`")


def test_a_suite_reusing_an_id_is_refused(tmp_path):
    raw = {"skill_name": "maintain", "evals": [_entry(), _entry()]}
    with pytest.raises(InvalidSuiteError, match="reuses id"):
        _suite_from_dict(raw, tmp_path)


def test_a_null_checks_list_is_refused_with_its_place(tmp_path):
    raw = {"skill_name": "maintain", "evals": [_entry(checks=None)]}
    with pytest.raises(InvalidSuiteError, match=r"evals\[0\]\.checks must be a list"):
        _suite_from_dict(raw, tmp_path)


def test_a_command_is_stored_as_the_transcript_spells_it(tmp_path):
    raw = {
        "check": "first_dex_command",
        "assertion": "x",
        "group": "maintain",
        "command": "maintain   incidents",
    }
    suite = _suite_from_dict(
        {"skill_name": "maintain", "evals": [_entry(checks=[raw])]}, tmp_path
    )
    [check] = suite.evals[0].checks
    assert check.command == "maintain incidents"
    assert checks.grade(check, passing())[0]


def test_the_verdict_file_takes_its_transcripts_stem(cli, plugin, tmp_path):
    out = tmp_path / "t"
    out.mkdir()
    (out / "case-7-skill.jsonl").write_text("", encoding="utf-8")
    suite = _suite_file(tmp_path, plugin)
    args = [str(suite), "--case", "7", "--plugin-dir", str(plugin)]
    main([*args, "--transcripts", str(out)])
    names = sorted(p.name for p in out.iterdir())
    assert "case-7-skill-2.jsonl" in names
    assert "case-7-skill-2.judge.json" in names
    assert "case-7-skill.judge.json" not in names


def test_a_run_with_no_transcript_gets_a_verdict_file_of_its_own(cli, plugin, tmp_path):
    cli.setup_status = "error"
    out = tmp_path / "t"
    suite = _suite_file(tmp_path, plugin)
    args = [str(suite), "--case", "7", "--plugin-dir", str(plugin)]
    main([*args, "--transcripts", str(out)])
    names = sorted(p.name for p in out.iterdir())
    assert names == ["case-7-skill-no-transcript.judge.json"]


# --- the committed case against the second review's routes (#1626) --------------


def _committed_check(kind: str, command: str = "") -> Check:
    case = _demo_case()
    [check] = [
        c
        for c in case.checks
        if c.check == kind and (not command or c.command == command)
    ]
    return check


@pytest.mark.parametrize(
    "changes, passed",
    [
        ([".dex/snapshot.json"], False),
        (["demo_project/dbt/models/staging/stg_order_items.sql"], False),
        ([".dex/cache.json", ".dex/cache.lock", ".dex/queries.jsonl"], True),
        ([".dex/spend.jsonl"], True),
        ([".dex_probe.sql"], True),
    ],
    ids=["snapshot", "model_by_shell", "bookkeeping", "ledger", "scratch"],
)
def test_the_committed_write_check_reads_the_workspace(changes, passed):
    check = _committed_check("no_file_writes")
    assert checks.grade(check, passing(), changes)[0] is passed


def test_the_committed_elementary_check_reads_a_sql_file():
    check = _committed_check("no_dex_command", "explore query")
    sql = " ".join(("select * from", "main_elementary.elementary_test_results"))
    events = [
        *passing()[:-1],
        _use(9, "Write", {"file_path": "/work/q.sql", "content": sql}),
        _bash(10, f"{WRAP} explore query --sql-file q.sql"),
        _done(ANSWER),
    ]
    assert not checks.grade(check, events)[0]


@pytest.mark.parametrize(
    "command", ["transform repair stg_x", "transform place c --targets a --expr x"]
)
def test_the_committed_case_fails_a_plan_stored_by_another_command(command):
    events = [*passing()[:-1], _bash(9, f"{WRAP} {command}"), _done(ANSWER)]
    assert not checks.grade(_committed_check("no_file_writes"), events)[0]


def test_the_committed_plan_check_ignores_a_help_call():
    events = [*passing()[:-1], _bash(9, f"{WRAP} transform plan --help"), _done(ANSWER)]
    assert checks.grade(_committed_check("no_dex_command", "transform plan"), events)[0]


# --- CodeRabbit's second review: fail closed, and anchor at the workspace --------


def _err(n: int) -> dict:
    result = _res(n, "exit 1")
    result["message"]["content"][0]["is_error"] = True
    return result


def test_each_sql_file_call_reads_its_own_argument():
    both = (
        f"{WRAP} explore query --sql-file clean.sql && "
        f"{WRAP} explore query --sql-file forbidden.sql"
    )
    writes = (
        _w(2, "/work/clean.sql", "select 1"),
        _w(3, "/work/forbidden.sql", ELEMENTARY_SQL),
    )
    events = _wrote(*writes, _bash(4, both))
    assert [c["sql_file"] for c in checks.dex_calls(events)[1:]] == [
        "clean.sql",
        "forbidden.sql",
    ]
    assert not checks.grade(NO_ELEMENTARY, events)[0]
    reverse = (
        f"{WRAP} explore query --sql-file forbidden.sql && "
        f"{WRAP} explore query --sql-file clean.sql"
    )
    assert not checks.grade(NO_ELEMENTARY, _wrote(*writes, _bash(4, reverse)))[0]
    clean = (_w(2, "/work/clean.sql", "select 1"), _w(3, "/work/other.sql", "select 2"))
    two_clean = both.replace("forbidden.sql", "other.sql")
    assert checks.grade(NO_ELEMENTARY, _wrote(*clean, _bash(4, two_clean)))[0]


@pytest.mark.parametrize(
    "touch",
    [
        "printf '\\n' >> q.sql",
        "echo x | tee -a q.sql",
        "cat source.sql > q.sql",
        "cat q.sql",
    ],
    ids=["append", "tee_append", "cat_into", "cat_out"],
)
def test_a_shell_command_naming_the_file_leaves_it_unknown(touch):
    for first in ("select 1", ELEMENTARY_SQL):
        events = _wrote(_w(2, "/work/q.sql", first), _bash(3, touch), _q(4))
        assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_an_edit_after_the_write_leaves_the_file_unknown():
    edit = _use(
        3, "Edit", {"file_path": "/work/q.sql", "old_string": "1", "new_string": "2"}
    )
    events = _wrote(_w(2, "/work/q.sql", "select 1"), edit, _q(4))
    assert not checks.grade(NO_ELEMENTARY, events)[0]
    # A whole-file Write after the Edit establishes it again.
    events = _wrote(
        _w(2, "/work/q.sql", "select 1"), edit, _w(5, "/work/q.sql", "select 3"), _q(6)
    )
    assert checks.grade(NO_ELEMENTARY, events)[0]


def test_a_failed_redirect_leaves_the_file_unknown():
    failed = _bash(3, "printf 'select * from dbt_invocations;' > q.sql; exit 1")
    events = _wrote(_w(2, "/work/q.sql", "select 1"), failed, _err(3), _q(4))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_the_query_command_writing_the_file_too_leaves_it_unknown():
    same = f"echo 'select 1' > q.sql && {WRAP} explore query --sql-file q.sql"
    events = _wrote(_w(2, "/work/q.sql", "select 1"), _bash(3, same))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_a_write_under_a_directory_named_like_the_workspace_is_not_the_file():
    nested = _w(2, "/work/scratch/work/private/q.sql", "select 1")
    assert not checks.grade(NO_ELEMENTARY, _wrote(nested, _q(3, "private/q.sql")))[0]
    right = _w(2, "/work/private/q.sql", "select 1")
    assert checks.grade(NO_ELEMENTARY, _wrote(right, _q(3, "private/q.sql")))[0]


def test_a_path_outside_the_workspace_is_outside_whatever_its_name():
    whole = Check(check="no_file_writes", assertion="x", within=["."])
    elsewhere = _wrote(_write(2, "/srv/work/x.sql"))
    assert checks.grade(whole, elsewhere)[0]
    assert checks.information(whole, elsewhere) == (
        "wrote outside the project: ['/srv/work/x.sql']"
    )
    assert not checks.grade(whole, _wrote(_write(2, "/work/x.sql")))[0]


def test_a_posix_path_keeps_its_case_and_a_windows_path_does_not():
    upper = _w(2, "/work/Q.sql", "select 1")
    assert not checks.grade(NO_ELEMENTARY, _wrote(upper, _q(3)))[0]
    win_upper = _w(2, BS.join([WIN_ROOT, "Q.sql"]), "select 1")
    assert checks.grade(NO_ELEMENTARY, _wrote(win_upper, _q(3), cwd=WIN_ROOT))[0]


def test_a_refused_write_is_not_a_write_to_the_project():
    events = _wrote(_write(2, "/work/demo_project/x.sql"), _denial(2, "Write"))
    assert checks.grade(PROJECT_ONLY, events)[0]


def test_home_is_this_users_home_directory():
    home = Path.home()
    root = str(home / "AppData" / "Local" / "Temp" / "dex-eval-7-abc")
    inside = "~/AppData/Local/Temp/dex-eval-7-abc/demo_project/x.sql"
    assert not checks.grade(PROJECT_ONLY, _wrote(_write(2, inside), cwd=root))[0]
    other = "~/elsewhere/dex-eval-7-abc/demo_project/x.sql"
    assert checks.grade(PROJECT_ONLY, _wrote(_write(2, other), cwd=root))[0]


def test_a_same_name_write_that_may_alias_the_file_leaves_it_unknown():
    # A spelling outside the workspace's known ones (a UNC path, a link) may
    # still be the file, so a write there makes its contents unknown.
    unc = _w(3, "//server/share/work/q.sql", "select 1")
    events = _wrote(_w(2, "/work/q.sql", "select 1"), unc, _q(4))
    assert not checks.grade(NO_ELEMENTARY, events)[0]


# --- `.` and `..` resolved after the join (CodeRabbit, #1626 checks.py:317) ------


def test_a_parent_step_out_of_the_workspace_is_outside():
    whole = Check(check="no_file_writes", assertion="x", within=["."])
    events = _wrote(_write(2, "../elsewhere.sql"))
    assert checks.grade(whole, events)[0]
    assert "../elsewhere.sql" in checks.information(whole, events)


def test_a_path_through_the_project_and_back_is_in_the_workspace_not_the_project():
    events = _wrote(_write(2, "demo_project/../x.sql"))
    assert checks.grade(PROJECT_ONLY, events)[0]
    assert checks.information(PROJECT_ONLY, events) == (
        "wrote outside the project: ['x.sql']"
    )


def test_a_dotted_spelling_is_the_same_file_for_the_sql_file_reader():
    clean = _w(2, "/work/demo_project/sub/../q.sql", "select 1")
    assert checks.grade(NO_ELEMENTARY, _wrote(clean, _q(3, "demo_project/q.sql")))[0]
    # A forbidden overwrite at a `..` spelling of the same file is seen.
    events = _wrote(
        _w(2, "/work/q.sql", "select 1"), _w(3, "../work/q.sql", ELEMENTARY_SQL), _q(4)
    )
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_a_path_that_climbs_above_the_root_stays_outside():
    whole = Check(check="no_file_writes", assertion="x", within=["."])
    assert checks.grade(whole, _wrote(_write(2, "../../../work/x.sql")))[0]
    assert checks.grade(whole, _wrote(_write(2, "/../work/x.sql")))[0]
    assert not checks.grade(whole, _wrote(_write(2, "/work/./a/../x.sql")))[0]


# --- an 8.3 short name places a path, never proves identity (#1625 :351) --------


def _short(*parts: str) -> str:
    return chr(92).join(["C:", "work", *parts])


def test_a_short_name_prefix_does_not_prove_sql_file_identity():
    # CodeRabbit's case: FOOBAR~1 may be foobaralpha, not foobarbeta.
    events = _wrote(
        _w(2, _short("FOOBAR~1", "q.sql"), "select 1"),
        _q(3, "foobarbeta/q.sql"),
        cwd=_short(),
    )
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_a_write_at_a_short_name_that_may_alias_the_file_leaves_it_unknown():
    # A clean Write at the long name, then a write at a short name that may be
    # the same directory: the contents are no longer known.
    events = _wrote(
        _w(2, _short("foobarbeta", "q.sql"), "select 1"),
        _w(3, _short("FOOBAR~1", "q.sql"), ELEMENTARY_SQL),
        _q(4, "foobarbeta/q.sql"),
        cwd=_short(),
    )
    assert not checks.grade(NO_ELEMENTARY, events)[0]


def test_the_long_name_spelled_exactly_still_proves_identity():
    events = _wrote(
        _w(2, _short("FooBarBeta", "q.sql"), "select 1"),
        _q(3, "foobarbeta/q.sql"),
        cwd=_short(),
    )
    assert checks.grade(NO_ELEMENTARY, events)[0]
