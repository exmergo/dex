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


@dataclass
class EvalCase:
    """One output-quality case: a prompt and the assertions its output must pass."""

    id: int
    prompt: str
    expected_output: str = ""
    files: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)


@dataclass
class EvalSuite:
    skill_name: str
    triggering: TriggeringCases = field(default_factory=TriggeringCases)
    evals: list[EvalCase] = field(default_factory=list)


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
        cases.append(
            EvalCase(
                id=entry.get("id", i),
                prompt=entry["prompt"],
                expected_output=entry.get("expected_output", ""),
                files=list(entry.get("files", [])),
                assertions=list(entry.get("assertions", [])),
            )
        )
    return EvalSuite(skill_name=skill_name, triggering=triggering, evals=cases)
