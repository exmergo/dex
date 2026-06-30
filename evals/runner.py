"""The eval scoring core: triggering, output quality, and uplift over baseline.

Deterministic and backend-agnostic. It takes an :class:`AgentRunner` (drives a
model on a prompt) and a :class:`Judge` (decides whether an output meets an
assertion) by dependency injection, so the scoring logic is unit-tested with fakes
and the live model backend is a swappable detail (see ``claude_agent.py``).

The two through-lines from the evaluation design are computed here directly:
triggering (does the skill fire on the right intent and stay quiet on siblings)
and uplift over baseline (is the agent plus the skill better than the agent alone).

Stdlib only by design (see this directory's README): models are dataclasses and
JSON output is hand-built via :meth:`to_dict`, so the harness needs no third-party
runtime dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .suite import EvalCase, EvalSuite


@dataclass
class AgentResult:
    """One agent run: its final output, whether the skill fired, and any error.

    ``triggered`` is what the triggering metric reads; ``output`` is what the judge
    grades. Backends populate ``envelopes`` when they can capture the engine's
    stdout envelopes, which lets assertions check the sanitized boundary directly.
    """

    output: str = ""
    triggered: bool = False
    envelopes: list[dict] = field(default_factory=list)
    error: str | None = None


@runtime_checkable
class AgentRunner(Protocol):
    """Drives an agent on one prompt. ``skill_enabled`` selects the skill-active
    arm versus the no-skill baseline arm used for uplift."""

    def run(self, prompt: str, *, skill_enabled: bool) -> AgentResult: ...


@runtime_checkable
class Judge(Protocol):
    """Decides whether an agent result satisfies one assertion."""

    def grade(self, case: EvalCase, assertion: str, result: AgentResult) -> bool: ...


@dataclass
class TriggeringReport:
    positives_total: int
    positives_fired: int
    negatives_total: int
    negatives_fired: int
    false_triggers: list[str] = field(default_factory=list)
    missed_triggers: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        if not self.positives_total:
            return 1.0
        return round(self.positives_fired / self.positives_total, 4)

    @property
    def precision(self) -> float:
        fired = self.positives_fired + self.negatives_fired
        return round(self.positives_fired / fired, 4) if fired else 1.0

    @property
    def passed(self) -> bool:
        # A clean triggering result fires on every positive and no negative.
        return not self.false_triggers and not self.missed_triggers

    def to_dict(self) -> dict[str, Any]:
        return {
            "positives_total": self.positives_total,
            "positives_fired": self.positives_fired,
            "negatives_total": self.negatives_total,
            "negatives_fired": self.negatives_fired,
            "false_triggers": self.false_triggers,
            "missed_triggers": self.missed_triggers,
            "recall": self.recall,
            "precision": self.precision,
            "passed": self.passed,
        }


@dataclass
class AssertionResult:
    assertion: str
    passed: bool


@dataclass
class CaseReport:
    id: int
    prompt: str
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.assertions) and all(a.passed for a in self.assertions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "passed": self.passed,
            "error": self.error,
            "assertions": [
                {"assertion": a.assertion, "passed": a.passed} for a in self.assertions
            ],
        }


@dataclass
class UpliftCaseReport:
    id: int
    passed_with_skill: bool
    passed_without_skill: bool


@dataclass
class SuiteReport:
    skill_name: str
    triggering: TriggeringReport
    quality: list[CaseReport] = field(default_factory=list)
    uplift_cases: list[UpliftCaseReport] = field(default_factory=list)

    @property
    def quality_pass_rate(self) -> float:
        return _rate([c.passed for c in self.quality])

    @property
    def uplift_score(self) -> float:
        """Net fraction of cases the skill turns from failing to passing."""

        with_skill = _rate([u.passed_with_skill for u in self.uplift_cases])
        without = _rate([u.passed_without_skill for u in self.uplift_cases])
        return round(with_skill - without, 4)

    @property
    def passed(self) -> bool:
        # Triggering must be clean and the skill must not regress against baseline.
        return self.triggering.passed and self.uplift_score >= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "triggering": self.triggering.to_dict(),
            "quality": [c.to_dict() for c in self.quality],
            "quality_pass_rate": self.quality_pass_rate,
            "uplift_cases": [
                {
                    "id": u.id,
                    "passed_with_skill": u.passed_with_skill,
                    "passed_without_skill": u.passed_without_skill,
                }
                for u in self.uplift_cases
            ],
            "uplift_score": self.uplift_score,
            "passed": self.passed,
        }


def run_triggering(suite: EvalSuite, runner: AgentRunner) -> TriggeringReport:
    positives = {
        p: runner.run(p, skill_enabled=True).triggered
        for p in suite.triggering.positive
    }
    negatives = {
        n: runner.run(n, skill_enabled=True).triggered
        for n in suite.triggering.negative
    }
    return TriggeringReport(
        positives_total=len(positives),
        positives_fired=sum(positives.values()),
        negatives_total=len(negatives),
        negatives_fired=sum(negatives.values()),
        false_triggers=[n for n, fired in negatives.items() if fired],
        missed_triggers=[p for p, fired in positives.items() if not fired],
    )


def run_quality(
    suite: EvalSuite, runner: AgentRunner, judge: Judge
) -> list[CaseReport]:
    reports: list[CaseReport] = []
    for case in suite.evals:
        result = runner.run(case.prompt, skill_enabled=True)
        reports.append(
            CaseReport(
                id=case.id,
                prompt=case.prompt,
                error=result.error,
                assertions=[
                    AssertionResult(assertion=a, passed=judge.grade(case, a, result))
                    for a in case.assertions
                ],
            )
        )
    return reports


def run_uplift(
    suite: EvalSuite, runner: AgentRunner, judge: Judge
) -> list[UpliftCaseReport]:
    return [
        UpliftCaseReport(
            id=case.id,
            passed_with_skill=_all_pass(case, runner, judge, skill=True),
            passed_without_skill=_all_pass(case, runner, judge, skill=False),
        )
        for case in suite.evals
    ]


def run_suite(suite: EvalSuite, runner: AgentRunner, judge: Judge) -> SuiteReport:
    """Run all three concerns and return a single report."""

    return SuiteReport(
        skill_name=suite.skill_name,
        triggering=run_triggering(suite, runner),
        quality=run_quality(suite, runner, judge),
        uplift_cases=run_uplift(suite, runner, judge),
    )


def _all_pass(
    case: EvalCase, runner: AgentRunner, judge: Judge, *, skill: bool
) -> bool:
    result = runner.run(case.prompt, skill_enabled=skill)
    return bool(case.assertions) and all(
        judge.grade(case, a, result) for a in case.assertions
    )


def _rate(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 4) if flags else 0.0
