"""The eval suite: the on-disk ``evals.json`` shape, loaded and validated.

One suite per skill at ``skills/<skill>/evals/evals.json``. The shape follows the
proven format: triggering cases (positive and must-not-trigger negatives) and a
list of output-quality cases, each with a prompt, the expected output, and the
hard-constraint assertions to check.

Stdlib only by design (see this directory's README): the models are plain
dataclasses and loading does light validation by hand, so the harness pulls in no
third-party runtime dependency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class InvalidSuiteError(ValueError):
    """Raised when an ``evals.json`` is malformed, with a pointer to the problem."""


@dataclass
class TriggeringCases:
    """Prompts that should fire the skill, and siblings that must not."""

    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)


#: The deterministic checks a case can name, graded from the agent's tool calls
#: (``evals/checks.py``) rather than by the judge, which reads only the final text.
CHECK_KINDS = ("first_dex_command", "no_dex_command", "no_file_writes")


@dataclass
class Check:
    """One assertion graded from the transcript's tool calls, not by the judge.

    ``assertion`` is the sentence the report prints. ``first_dex_command``
    needs ``group`` and ``command`` (the first ``group`` command the agent ran
    is ``command``); ``no_dex_command`` needs ``command`` and ``matching``, a
    regular expression no such call may hold. ``no_file_writes`` may take
    ``within``, paths relative to the run's working directory: only a write
    under one of them fails the check, and a write elsewhere is listed as
    information. Without ``within``, every write fails it.
    """

    check: str
    assertion: str
    group: str = ""
    command: str = ""
    matching: str = ""
    within: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    """How a case's agent is started: its tool permissions and its limits.

    The permission fields become ``--permission-mode``, ``--allowedTools`` and
    ``--disallowedTools``; ``timeout`` (seconds) replaces the CLI's
    ``--timeout`` for this case's agent runs, not for the judge.
    """

    permission_mode: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    timeout: int | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None


@dataclass
class EvalCase:
    """One output-quality case: a prompt and the assertions its output must pass.

    A case with ``setup`` runs in a fresh, empty working directory of its own:
    each entry is one ``dex`` command (its arguments, without ``dex``) the
    harness runs there before the agent starts, so its output never reaches the
    agent. ``checks`` are graded from the tool calls, next to ``assertions``,
    which the judge grades from the final text.
    """

    id: int
    prompt: str
    expected_output: str = ""
    files: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    setup: list[list[str]] = field(default_factory=list)
    agent: AgentConfig | None = None
    checks: list[Check] = field(default_factory=list)

    @property
    def streamed(self) -> bool:
        """Whether this case needs the transcript: its own workspace, or a
        check that reads the tool calls."""

        return bool(self.setup or self.checks or self.agent)


@dataclass
class EvalSuite:
    skill_name: str
    triggering: TriggeringCases = field(default_factory=TriggeringCases)
    evals: list[EvalCase] = field(default_factory=list)


@dataclass
class CorpusCase:
    """One externally authored prompt, labeled with the skill it should fire
    (or ``None`` where no skill should fire)."""

    task_id: str
    prompt: str
    expected_skill: str | None


@dataclass
class Corpus:
    """An externally authored triggering fixture: real requests written by
    someone with no knowledge of the skill descriptions, each hand-labeled.

    Unlike :class:`EvalSuite`, a corpus is not scoped to one skill: it is run
    against every skill at once (see ``evals.runner.run_corpus``), which is
    what makes it able to catch a case a per-skill positive/negative list
    cannot: two skills' descriptions both plausibly firing on the same prompt,
    or the right skill losing to a sibling's wording.

    ``source`` carries provenance: where the prompts came from and under what
    license, since these are someone else's words, reproduced verbatim.
    """

    name: str
    source: dict[str, Any]
    cases: list[CorpusCase] = field(default_factory=list)


def load_suite(skill_or_file: Path | str) -> EvalSuite:
    """Load a suite from a skill directory or a direct path to ``evals.json``."""

    path = Path(skill_or_file)
    if path.is_dir():
        candidate = path / "evals" / "evals.json"
        path = candidate if candidate.is_file() else path / "evals.json"
    if not path.is_file():
        raise FileNotFoundError(f"no evals.json found at {skill_or_file}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidSuiteError(f"{path}: top level must be an object")
    return _suite_from_dict(raw, path)


def _suite_from_dict(raw: dict[str, Any], path: Path) -> EvalSuite:
    skill_name = raw.get("skill_name")
    if not isinstance(skill_name, str) or not skill_name:
        raise InvalidSuiteError(f"{path}: 'skill_name' is required")

    trig = raw.get("triggering") or {}
    triggering = TriggeringCases(
        positive=list(trig.get("positive", [])),
        negative=list(trig.get("negative", [])),
    )

    cases: list[EvalCase] = []
    for i, entry in enumerate(raw.get("evals", [])):
        if "prompt" not in entry:
            raise InvalidSuiteError(f"{path}: evals[{i}] has no 'prompt'")
        where = f"{path}: evals[{i}]"
        cases.append(
            EvalCase(
                id=entry.get("id", i),
                prompt=entry["prompt"],
                expected_output=entry.get("expected_output", ""),
                files=list(entry.get("files", [])),
                assertions=list(entry.get("assertions", [])),
                setup=_setup_from(entry.get("setup", []), where),
                agent=_agent_from(entry.get("agent"), where),
                checks=[
                    _check_from(c, f"{where}.checks[{j}]")
                    for j, c in enumerate(entry.get("checks", []))
                ],
            )
        )
    return EvalSuite(skill_name=skill_name, triggering=triggering, evals=cases)


def _setup_from(raw: Any, where: str) -> list[list[str]]:
    if not isinstance(raw, list) or not all(
        isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)
        for cmd in raw
    ):
        raise InvalidSuiteError(
            f"{where}.setup must be a list of dex commands, each a non-empty "
            'list of arguments such as ["demo"]'
        )
    return [list(cmd) for cmd in raw]


def _agent_from(raw: Any, where: str) -> AgentConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidSuiteError(f"{where}.agent must be an object")
    unknown = sorted(set(raw) - set(AgentConfig.__dataclass_fields__))
    if unknown:
        raise InvalidSuiteError(f"{where}.agent has unknown key(s) {unknown}")
    for key in ("allowed_tools", "disallowed_tools"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            raise InvalidSuiteError(f"{where}.agent.{key} must be a list of strings")
    if not isinstance(raw.get("permission_mode", ""), str):
        raise InvalidSuiteError(f"{where}.agent.permission_mode must be a string")
    for key, kinds in (
        ("timeout", (int,)),
        ("max_turns", (int,)),
        ("max_budget_usd", (int, float)),
    ):
        value = raw.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, kinds) or value <= 0
        ):
            raise InvalidSuiteError(f"{where}.agent.{key} must be a positive number")
    return AgentConfig(**raw)


def _check_from(raw: Any, where: str) -> Check:
    if not isinstance(raw, dict):
        raise InvalidSuiteError(f"{where} must be an object")
    kind = raw.get("check")
    if kind not in CHECK_KINDS:
        raise InvalidSuiteError(f"{where}.check must be one of {list(CHECK_KINDS)}")
    if not isinstance(raw.get("assertion"), str) or not raw["assertion"]:
        raise InvalidSuiteError(f"{where} needs an 'assertion' to report")
    required = {
        "first_dex_command": ("group", "command"),
        "no_dex_command": ("command", "matching"),
        "no_file_writes": (),
    }[kind]
    missing = [
        key
        for key in required
        if not (raw.get(key).strip() if isinstance(raw.get(key), str) else raw.get(key))
    ]
    if missing:
        raise InvalidSuiteError(f"{where} ({kind}) needs {missing}")
    for key in ("group", "command", "matching"):
        if not isinstance(raw.get(key, ""), str):
            raise InvalidSuiteError(f"{where}.{key} must be a string")
    within = raw.get("within", [])
    well_formed = isinstance(within, list) and all(
        isinstance(p, str) and p.strip() for p in within
    )
    if not well_formed or (within and kind != "no_file_writes"):
        raise InvalidSuiteError(
            f"{where}.within must be a list of paths, and only on no_file_writes"
        )
    if kind == "first_dex_command" and raw["command"].split()[0] != raw["group"]:
        raise InvalidSuiteError(
            f"{where}.command must be a `{raw['group']}` command, written "
            "without `dex` (`maintain incidents`)"
        )
    if raw.get("matching"):
        try:
            re.compile(raw["matching"])
        except re.error as exc:
            raise InvalidSuiteError(
                f"{where}.matching does not compile: {exc}"
            ) from None
    unknown = sorted(set(raw) - set(Check.__dataclass_fields__))
    if unknown:
        raise InvalidSuiteError(f"{where} has unknown key(s) {unknown}")
    return Check(**raw)


def load_corpus(path: Path | str) -> Corpus:
    """Load an externally authored triggering corpus (see :class:`Corpus`)."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no corpus file at {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidSuiteError(f"{path}: top level must be an object")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidSuiteError(f"{path}: 'name' is required")
    source = raw.get("source")
    if not isinstance(source, dict) or not source:
        raise InvalidSuiteError(
            f"{path}: 'source' is required (provenance and license for the "
            "externally authored prompts)"
        )

    cases: list[CorpusCase] = []
    for i, entry in enumerate(raw.get("cases", [])):
        if "prompt" not in entry:
            raise InvalidSuiteError(f"{path}: cases[{i}] has no 'prompt'")
        if "expected_skill" not in entry:
            raise InvalidSuiteError(f"{path}: cases[{i}] has no 'expected_skill'")
        # "none" is the on-disk spelling (JSON has nothing better to write for a
        # case where no skill should fire); a Classifier reports the same
        # absence as Python None, so the two are reconciled here rather than
        # everywhere a case gets compared against a classifier's answer.
        label = entry["expected_skill"]
        cases.append(
            CorpusCase(
                task_id=entry.get("task_id", str(i)),
                prompt=entry["prompt"],
                expected_skill=None if label == "none" else label,
            )
        )
    if not cases:
        raise InvalidSuiteError(f"{path}: 'cases' is empty")
    return Corpus(name=name, source=source, cases=cases)
