"""Live backend: drive Claude Code headless to run a skill's evals.

This is the one place that knows about a concrete agent. It implements the
:class:`~evals.runner.AgentRunner` and :class:`~evals.runner.Judge` protocols by
shelling out to the ``claude`` CLI in print mode, so the deterministic scoring
core never depends on any model. A non-Claude agent (Codex, Gemini) becomes a
second backend implementing the same two protocols, with no change to the core,
which is what backs the cross-agent portability claim.

Requires the ``claude`` CLI on PATH. The CLI passes the plugin under test in
with ``--plugin-dir`` (see ``evals/__main__.py``). The exact knobs for
enabling/disabling the skill per arm and for detecting that the skill fired are
environment-specific; they are isolated here behind small, overridable hooks so
calibrating them never touches the core.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import checks
from .runner import AgentResult, ClassifyResult, Verdict, read_verdict
from .suite import AgentConfig, EvalCase


class ClaudeNotAvailableError(RuntimeError):
    """Raised when the ``claude`` CLI is not on PATH."""


def _require_claude(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise ClaudeNotAvailableError(
            f"'{binary}' not found on PATH; install Claude Code to run live evals"
        )
    return resolved


# Login and provider settings a child still needs; every other CLAUDE* variable
# is dropped, since run from inside a Claude Code session they describe that
# session and a child that inherits them can answer as part of it (#791).
_KEEP_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
_KEEP_ENV_PREFIXES = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")


def _child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE")
        or k in _KEEP_ENV
        or k.startswith(_KEEP_ENV_PREFIXES)
    }


def _invoke(binary: str, args: list[str], prompt: str, timeout: int) -> str:
    """Run ``claude -p`` and return its textual result, or raise on failure.

    The prompt goes on stdin, never in argv: on Windows ``claude`` is usually
    npm's ``claude.cmd`` shim, and cmd.exe ends an argument at its first
    newline, so a multi-line prompt (every judge prompt) reached the model as
    its first line, and every flag after it was dropped.
    """

    proc = subprocess.run(
        [binary, "-p", "--output-format", "json", *args],
        input=prompt,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_child_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"claude exited {proc.returncode}")
    # Headless JSON wraps the assistant result; fall back to raw stdout if the
    # shape changes so a CLI update degrades to "gradeable text", not a crash.
    try:
        answer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if not isinstance(answer, dict):
        return proc.stdout
    if answer.get("is_error"):
        # A failed sign-in exits 0 and says so only here; its text ("Failed
        # to authenticate") must never be read as an answer.
        said = str(answer.get("result") or "").strip().splitlines()
        raise RuntimeError(
            f"claude reported an error ({answer.get('terminal_reason') or 'is_error'})"
            + (f": {said[0][:200]}" if said else "")
        )
    return str(answer.get("result", proc.stdout))


#: For a case with its own workspace, both arms load only the project's settings
#: (a fresh workspace has none), so the machine's installed plugins, the user's
#: hooks and memory stay out, and the baseline arm is the agent with no dex at
#: all. The same isolation the metadata eval's live runs use (#791).
ISOLATION_ARGS = [
    "--setting-sources",
    "project",
    "--strict-mcp-config",
    "--no-session-persistence",
]
#: How long one setup command may take: the first one can install the engine.
SETUP_TIMEOUT = 900


def agent_args(config: AgentConfig | None) -> list[str]:
    """The CLI flags a case's ``agent`` block asks for."""

    if config is None:
        return []
    args: list[str] = []
    if config.permission_mode:
        args += ["--permission-mode", config.permission_mode]
    if config.max_turns is not None:
        args += ["--max-turns", str(config.max_turns)]
    if config.max_budget_usd is not None:
        args += ["--max-budget-usd", str(config.max_budget_usd)]
    # Variadic in the CLI, so each list is closed by the next flag.
    if config.allowed_tools:
        args += ["--allowedTools", *config.allowed_tools]
    if config.disallowed_tools:
        args += ["--disallowedTools", *config.disallowed_tools]
    return args


def run_setup(
    case: EvalCase, workspace: Path, plugin_dir: Path | None, skill_name: str
) -> str | None:
    """Run the case's setup commands in ``workspace``; why they failed, or
    ``None``.

    Each runs through the plugin's own skill wrapper, the command the skill
    tells the agent to run, so the setup and the agent use one engine. Their
    output is read here and never reaches the agent: ``dex demo`` prints the
    defect it plants.
    """

    if not case.setup:
        return None
    if plugin_dir is None:
        return "a case with setup needs the plugin directory (--plugin-dir)"
    wrapper = Path(plugin_dir) / "skills" / skill_name / "scripts" / "run.py"
    if not wrapper.is_file():
        return f"no skill wrapper at {wrapper}"
    uv = shutil.which("uv")
    if uv is None:
        return "setup runs dex through uv, which was not found on PATH"
    for argv in case.setup:
        shown = "dex " + " ".join(argv)
        try:
            proc = subprocess.run(
                [uv, "run", "--no-project", "--script", str(wrapper), *argv],
                cwd=workspace,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=SETUP_TIMEOUT,
                env=_child_env(),
            )
        except subprocess.TimeoutExpired:
            return f"setup `{shown}` timed out after {SETUP_TIMEOUT}s"
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {}
        status = envelope.get("status") if isinstance(envelope, dict) else None
        if proc.returncode != 0 or status != "ok":
            errors = (
                envelope.get("errors") if isinstance(envelope, dict) else None
            ) or [proc.stderr.strip()[-300:]]
            outcome = status or f"exit {proc.returncode}"
            return f"setup `{shown}` returned {outcome}: {errors[0]}"
    return None


def workspace_files(root: Path) -> dict[str, str]:
    """Every file under ``root``, by its path relative to it, with a digest of
    its bytes. A file that cannot be read (held open by another process) is
    recorded as unreadable, so a change to it is not missed."""

    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        try:
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            found[name] = "unreadable"
    return found


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The files added, changed or removed between two ``workspace_files``."""

    return sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    )


def _invoke_stream(
    binary: str, args: list[str], prompt: str, timeout: int, cwd: Path
) -> tuple[str, str | None]:
    """Run ``claude -p`` in ``cwd`` with a ``stream-json`` transcript; return
    the transcript and why the process failed (``None`` if it did not). A
    timed-out run keeps what it wrote, which says how far it got. The prompt
    goes on stdin, as in ``_invoke``."""

    try:
        proc = subprocess.run(
            [binary, "-p", "--output-format", "stream-json", "--verbose", *args],
            input=prompt,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return partial, f"timed out after {timeout}s"
    if proc.returncode != 0:
        return proc.stdout, proc.stderr.strip()[
            -2000:
        ] or f"claude exited {proc.returncode}"
    return proc.stdout, None


@dataclass
class ClaudeCliAgent:
    """Drives Claude Code on a prompt, with a skill-on and a baseline arm.

    ``skill_args`` / ``baseline_args`` are the per-arm CLI flags that install or
    suppress the skill in your workspace (calibrated to your setup). ``triggered``
    is detected by ``trigger_marker`` appearing in the run output, defaulting to
    the namespaced skill invocation (e.g. ``/dex:explore``); override it if your
    transcript surfaces tool use differently.

    A case that needs more than its prompt (``EvalCase.streamed``) runs in a
    fresh temporary directory, set up by its ``setup`` commands and removed
    afterwards unless ``keep_workspaces``, with ``ISOLATION_ARGS`` and its
    ``agent`` flags, and its ``stream-json`` transcript parsed into
    ``AgentResult.events`` and written to ``transcripts`` when that is set.
    ``plugin_dir`` is the plugin under test: setup runs its wrapper, and the
    skill arm's transcript must show it loaded.
    """

    skill_name: str
    binary: str = "claude"
    model: str | None = None
    timeout: int = 180
    skill_args: list[str] = field(default_factory=list)
    baseline_args: list[str] = field(default_factory=list)
    trigger_marker: str | None = None
    plugin_dir: Path | None = None
    transcripts: Path | None = None
    keep_workspaces: bool = False

    def run(
        self, prompt: str, *, skill_enabled: bool, case: EvalCase | None = None
    ) -> AgentResult:
        if case is not None and case.streamed:
            return self._run_case(prompt, case, skill_enabled)
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

    def _run_case(
        self, prompt: str, case: EvalCase, skill_enabled: bool
    ) -> AgentResult:
        binary = _require_claude(self.binary)
        args = list(self.skill_args if skill_enabled else self.baseline_args)
        args += ISOLATION_ARGS
        if self.model:
            args += ["--model", self.model]
        args += agent_args(case.agent)
        timeout = (case.agent.timeout if case.agent else None) or self.timeout
        workspace = Path(tempfile.mkdtemp(prefix=f"dex-eval-{case.id}-"))
        try:
            failed = run_setup(case, workspace, self.plugin_dir, self.skill_name)
            if failed:
                return AgentResult(error=failed)
            before = workspace_files(workspace)
            text, failed = _invoke_stream(binary, args, prompt, timeout, workspace)
            changes = changed_files(before, workspace_files(workspace))
            self._keep_transcript(case, skill_enabled, text)
            events = checks.parse_stream(text)
            output = checks.final_text(events)
            error = failed or checks.run_error(
                events,
                skill_enabled=skill_enabled,
                plugin=self._plugin,
                plugin_dir=self.plugin_dir,
            )
            marker = self.trigger_marker or f"/{self._plugin}:{self.skill_name}"
            triggered = marker in output or checks.skill_invoked(
                events, f"{self._plugin}:{self.skill_name}"
            )
            return AgentResult(
                output=output,
                triggered=triggered,
                events=events,
                error=error,
                changes=changes,
            )
        finally:
            if self.keep_workspaces:
                print(f"case {case.id} workspace kept at {workspace}", file=sys.stderr)
            else:
                shutil.rmtree(workspace, ignore_errors=True)

    def _keep_transcript(self, case: EvalCase, skill_enabled: bool, text: str) -> None:
        if self.transcripts is None:
            return
        self.transcripts.mkdir(parents=True, exist_ok=True)
        stem = f"case-{case.id}-{'skill' if skill_enabled else 'baseline'}"
        path = self.transcripts / f"{stem}.jsonl"
        n = 2
        while path.exists():
            path = self.transcripts / f"{stem}-{n}.jsonl"
            n += 1
        path.write_text(text, encoding="utf-8", newline="")
        print(f"case {case.id} transcript written to {path}", file=sys.stderr)

    @property
    def _plugin(self) -> str:
        return "dex"


@dataclass
class ClaudeCliClassifier:
    """Runs one prompt with every dex skill available and reports every
    marker that showed up, for the cross-skill corpus (``run_corpus``).

    Unlike :class:`ClaudeCliAgent`, this never suppresses a skill: ``args``
    is whatever invokes Claude Code with the plugin installed normally, the
    same condition a real request arrives in. Reports *every* marker found,
    not just one: picking a single "winner" when two fire would hide the
    unwanted one entirely, which is exactly the cross-skill contamination
    this corpus exists to catch, not a detail to summarize away.
    """

    skill_names: tuple[str, ...] = ("explore", "transform", "maintain")
    binary: str = "claude"
    model: str | None = None
    timeout: int = 180
    args: list[str] = field(default_factory=list)
    plugin: str = "dex"

    def classify(self, prompt: str) -> ClassifyResult:
        # _require_claude runs outside the try, same as ClaudeCliAgent.run:
        # a missing binary is a setup problem every subsequent call would hit
        # identically, so it raises here and propagates out of run_corpus
        # rather than being caught into one case's error.
        binary = _require_claude(self.binary)
        args = list(self.args)
        if self.model:
            args += ["--model", self.model]
        try:
            output = _invoke(binary, args, prompt, self.timeout)
        except Exception as exc:
            return ClassifyResult(error=str(exc))
        fired = frozenset(
            name for name in self.skill_names if f"/{self.plugin}:{name}" in output
        )
        return ClassifyResult(fired_skills=fired)


@dataclass
class ClaudeCliJudge:
    """Grades one assertion against an agent result with an LLM yes/no judge."""

    binary: str = "claude"
    model: str | None = None
    timeout: int = 120

    def grade(self, case: EvalCase, assertion: str, result: AgentResult) -> bool:
        return self.verdict(case, assertion, result).passed

    def verdict(self, case: EvalCase, assertion: str, result: AgentResult) -> Verdict:
        """The judge's answer, read tolerantly; a judge that failed or
        answered neither PASS nor FAIL comes back as ``error``, never as a
        FAIL, since it says nothing about the agent."""

        if result.error:
            # The run is the case's error already; there is nothing to judge.
            return Verdict(passed=False)
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
            answer = _invoke(binary, args, prompt, self.timeout)
        except Exception as exc:
            # A judge failure is no verdict, never a crash and never a FAIL.
            return Verdict(passed=False, error=f"{type(exc).__name__}: {exc}"[:500])
        return read_verdict(answer)
