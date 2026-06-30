"""Eval scoring core, exercised with fake backends.

These are deterministic and free (no model in the loop), so they run in CI and
guard the metric math: triggering precision/recall, output-quality pass rate, and
uplift over baseline. The live Claude backend is intentionally not tested here.
"""

from __future__ import annotations

from pathlib import Path

from evals.runner import AgentResult, run_suite, run_triggering
from evals.suite import EvalCase, EvalSuite, TriggeringCases, load_suite

_REPO = Path(__file__).resolve().parents[2]


class FakeAgent:
    """Fires only on prompts containing any trigger word; output echoes the arm."""

    def __init__(self, triggers: tuple[str, ...], *, helps: bool = True):
        self.triggers = triggers
        self.helps = helps

    def run(self, prompt: str, *, skill_enabled: bool) -> AgentResult:
        fired = any(t in prompt.lower() for t in self.triggers)
        # With the skill the output carries the marker the judge looks for; the
        # baseline arm omits it unless the agent helps even without the skill.
        good = skill_enabled or not self.helps
        return AgentResult(output="GOOD" if good else "bare", triggered=fired)


class FakeJudge:
    """Passes an assertion iff the output is the 'GOOD' marker."""

    def grade(self, case: EvalCase, assertion: str, result: AgentResult) -> bool:
        return result.output == "GOOD"


def _suite() -> EvalSuite:
    return EvalSuite(
        skill_name="explore",
        triggering=TriggeringCases(
            positive=["explore this warehouse", "profile the table"],
            negative=["build a dbt model", "define a metric"],
        ),
        evals=[
            EvalCase(id=0, prompt="explore it", assertions=["is sense-making"]),
            EvalCase(id=1, prompt="profile it", assertions=["aggregates only"]),
        ],
    )


def test_real_explore_suite_loads_and_validates():
    suite = load_suite(_REPO / "skills" / "explore")
    assert suite.skill_name == "explore"
    assert suite.triggering.positive and suite.triggering.negative
    assert all(case.assertions for case in suite.evals)


def test_triggering_is_clean_when_agent_fires_correctly():
    agent = FakeAgent(triggers=("explore", "profile"))
    report = run_triggering(_suite(), agent)
    assert report.passed
    assert report.recall == 1.0
    assert report.precision == 1.0
    assert not report.false_triggers and not report.missed_triggers


def test_triggering_flags_false_and_missed():
    # Fires on the wrong intent ("metric") and misses a real one ("profile").
    agent = FakeAgent(triggers=("explore", "metric"))
    report = run_triggering(_suite(), agent)
    assert not report.passed
    assert "define a metric" in report.false_triggers
    assert "profile the table" in report.missed_triggers
    assert report.recall == 0.5


def test_quality_and_uplift_when_skill_helps():
    agent = FakeAgent(triggers=("explore", "profile"), helps=True)
    report = run_suite(_suite(), agent, FakeJudge())
    assert report.quality_pass_rate == 1.0
    # Skill arm passes, baseline arm fails -> positive uplift.
    assert report.uplift_score == 1.0
    assert report.passed


def test_no_uplift_when_baseline_already_passes():
    # The agent helps even without the skill (the outgrowth case).
    agent = FakeAgent(triggers=("explore", "profile"), helps=False)
    report = run_suite(_suite(), agent, FakeJudge())
    assert report.quality_pass_rate == 1.0
    assert report.uplift_score == 0.0  # no lift over baseline
    assert report.passed  # still passes: clean triggering, no regression


def test_failure_surfaces_failed_assertion():
    class NeverHelps:
        def run(self, prompt: str, *, skill_enabled: bool) -> AgentResult:
            return AgentResult(output="bare", triggered=True)

    report = run_suite(_suite(), NeverHelps(), FakeJudge())
    assert report.quality_pass_rate == 0.0
    assert all(not c.passed for c in report.quality)
