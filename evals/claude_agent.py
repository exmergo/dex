"""Live backend: drive Claude Code headless to run a skill's evals.

This is the one place that knows about a concrete agent. It implements the
:class:`~evals.runner.AgentRunner` and :class:`~evals.runner.Judge` protocols by
shelling out to the ``claude`` CLI in print mode, so the deterministic scoring
core never depends on any model. A non-Claude agent (Codex, Gemini) becomes a
second backend implementing the same two protocols, with no change to the core,
which is what backs the cross-agent portability claim.

Requires the ``claude`` CLI on PATH and a configured workspace where the dex
plugin is installed. The exact knobs for enabling/disabling the skill per arm and
for detecting that the skill fired are environment-specific; they are isolated
here behind small, overridable hooks so calibrating them never touches the core.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

from .runner import AgentResult
from .suite import EvalCase


class ClaudeNotAvailableError(RuntimeError):
    """Raised when the ``claude`` CLI is not on PATH."""


def _require_claude(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise ClaudeNotAvailableError(
            f"'{binary}' not found on PATH; install Claude Code to run live evals"
        )
    return resolved


def _invoke(binary: str, args: list[str], prompt: str, timeout: int) -> str:
    """Run ``claude -p`` and return its textual result, or raise on failure."""

    proc = subprocess.run(  # noqa: S603 - binary is PATH-resolved via shutil.which; args are internal, not user shell input
        [binary, "-p", prompt, "--output-format", "json", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"claude exited {proc.returncode}")
    # Headless JSON wraps the assistant result; fall back to raw stdout if the
    # shape changes so a CLI update degrades to "gradeable text", not a crash.
    try:
        return str(json.loads(proc.stdout).get("result", proc.stdout))
    except json.JSONDecodeError:
        return proc.stdout


@dataclass
class ClaudeCliAgent:
    """Drives Claude Code on a prompt, with a skill-on and a baseline arm.

    ``skill_args`` / ``baseline_args`` are the per-arm CLI flags that install or
    suppress the skill in your workspace (calibrated to your setup). ``triggered``
    is detected by ``trigger_marker`` appearing in the run output, defaulting to
    the namespaced skill invocation (e.g. ``/dex:explore``); override it if your
    transcript surfaces tool use differently.
    """

    skill_name: str
    binary: str = "claude"
    model: str | None = None
    timeout: int = 180
    skill_args: list[str] = field(default_factory=list)
    baseline_args: list[str] = field(default_factory=list)
    trigger_marker: str | None = None

    def run(self, prompt: str, *, skill_enabled: bool) -> AgentResult:
        binary = _require_claude(self.binary)
        args = list(self.skill_args if skill_enabled else self.baseline_args)
        if self.model:
            args += ["--model", self.model]
        try:
            output = _invoke(binary, args, prompt, self.timeout)
        except Exception as exc:
            # Any agent failure becomes a gradeable result, not a crashed run.
            return AgentResult(error=str(exc))
        marker = self.trigger_marker or f"/{self._plugin}:{self.skill_name}"
        return AgentResult(output=output, triggered=marker in output)

    @property
    def _plugin(self) -> str:
        return "dex"


@dataclass
class ClaudeCliJudge:
    """Grades one assertion against an agent result with an LLM yes/no judge."""

    binary: str = "claude"
    model: str | None = None
    timeout: int = 120

    def grade(self, case: EvalCase, assertion: str, result: AgentResult) -> bool:
        if result.error:
            return False
        binary = _require_claude(self.binary)
        prompt = (
            "You are grading an agent's output against one requirement. "
            "Answer with exactly 'PASS' or 'FAIL' and nothing else.\n\n"
            f"Task given to the agent:\n{case.prompt}\n\n"
            f"Requirement:\n{assertion}\n\n"
            f"Agent output:\n{result.output}"
        )
        args = ["--model", self.model] if self.model else []
        try:
            verdict = _invoke(binary, args, prompt, self.timeout)
        except Exception:
            return False  # a judge failure is a non-pass, never a crash
        return verdict.strip().upper().startswith("PASS")
