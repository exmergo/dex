"""The CLI and the live backend's process boundary, with ``subprocess.run``
patched out so no model is called: which plugin a run loads, the ``--case``
filter, and the environment a ``claude -p`` child starts with (#791).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals import claude_agent
from evals.__main__ import main
from evals.suite import load_suite

_REPO = Path(__file__).resolve().parents[2]
_EXPLORE = str(_REPO / "skills" / "explore")


@pytest.fixture
def calls(monkeypatch):
    """Record every ``claude`` invocation; each one answers PASS."""

    recorded: list[dict] = []

    def fake_run(cmd, **kwargs):
        recorded.append({"cmd": cmd, "env": kwargs.get("env")})
        out = json.dumps({"result": "PASS /dex:explore"})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(claude_agent.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(claude_agent.subprocess, "run", fake_run)
    return recorded


def _prompt(call: dict) -> str:
    cmd = call["cmd"]
    return cmd[cmd.index("-p") + 1]


def test_plugin_dir_defaults_to_this_checkout(calls, capsys):
    assert main([_EXPLORE, "--case", "0"]) == 0
    agent_call = calls[0]["cmd"]
    assert agent_call[agent_call.index("--plugin-dir") + 1] == str(_REPO)
    assert f"evaluating the dex plugin at {_REPO}" in capsys.readouterr().err


def test_plugin_dir_override_reaches_the_skill_arm(calls, tmp_path):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    assert main([_EXPLORE, "--case", "0", "--plugin-dir", str(tmp_path)]) == 0
    agent_call = calls[0]["cmd"]
    assert agent_call[agent_call.index("--plugin-dir") + 1] == str(tmp_path)


def test_plugin_dir_without_a_manifest_is_an_error(calls, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([_EXPLORE, "--plugin-dir", str(tmp_path)])
    assert exc.value.code == 2
    assert calls == []


def test_case_runs_only_the_selected_cases(calls):
    suite = load_suite(_EXPLORE)
    by_id = {c.id: c for c in suite.evals}
    assert main([_EXPLORE, "--case", "2", "--case", "5", "--json"]) == 0
    agent_prompts = [_prompt(c) for c in calls if "--plugin-dir" in c["cmd"]]
    # One skill-on run per selected case: no triggering prompts, no baseline arm.
    assert agent_prompts == [by_id[2].prompt, by_id[5].prompt]


def test_case_with_an_unknown_id_is_an_error(calls):
    with pytest.raises(SystemExit) as exc:
        main([_EXPLORE, "--case", "999"])
    assert exc.value.code == 2
    assert calls == []


def test_child_env_drops_session_variables_but_keeps_login(calls, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    claude_agent._invoke("claude", [], "hi", timeout=5)
    env = calls[0]["env"]
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_PID" not in env
    kept = {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_API_KEY",
    }
    assert kept <= env.keys()
