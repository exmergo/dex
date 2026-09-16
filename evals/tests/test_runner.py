"""Eval scoring core, exercised with fake backends.

These are deterministic and free (no model in the loop), so they run in CI and
guard the metric math: triggering precision/recall, output-quality pass rate, and
uplift over baseline. The live Claude backend is intentionally not tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner import (
    AgentResult,
    ClassifyResult,
    run_corpus,
    run_suite,
    run_triggering,
)
from evals.suite import (
    Corpus,
    CorpusCase,
    EvalCase,
    EvalSuite,
    InvalidSuiteError,
    TriggeringCases,
    load_corpus,
    load_suite,
)

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


# --- the cross-skill corpus (#216) ----------------------------------------------


class FakeClassifier:
    """Reports every skill whose trigger word appears in the prompt."""

    def __init__(self, triggers: dict[str, str]):
        self.triggers = triggers  # word -> skill

    def classify(self, prompt: str) -> ClassifyResult:
        lowered = prompt.lower()
        fired = frozenset(
            skill for word, skill in self.triggers.items() if word in lowered
        )
        return ClassifyResult(fired_skills=fired)


def _corpus() -> Corpus:
    return Corpus(
        name="fake",
        source={"repo": "https://example.invalid/x", "license": "Apache-2.0"},
        cases=[
            CorpusCase(
                task_id="a", prompt="explore the warehouse", expected_skill="explore"
            ),
            CorpusCase(task_id="b", prompt="build a model", expected_skill="transform"),
            CorpusCase(
                task_id="c", prompt="did anything drift", expected_skill="maintain"
            ),
            CorpusCase(task_id="d", prompt="say hello", expected_skill=None),
        ],
    )


def test_run_corpus_is_perfect_when_every_case_classifies_correctly():
    classifier = FakeClassifier(
        {"explore": "explore", "build": "transform", "drift": "maintain"}
    )
    report = run_corpus(_corpus(), classifier)
    assert report.accuracy == 1.0
    assert report.per_skill["explore"].precision == 1.0
    assert report.per_skill["explore"].recall == 1.0
    assert report.per_skill["transform"].f1 == 1.0
    assert report.per_skill["maintain"].true_positives == 1
    # "none" never appears in fired_skills by construction (an empty set is
    # how a classifier reports nothing fired), so it earns no entry in
    # per_skill: there is no skill named "none" to score.
    assert "none" not in report.per_skill


def test_run_corpus_counts_a_false_trigger_against_precision_not_recall():
    # The classifier fires "transform" on the maintain case too (a false
    # positive for transform) and stays silent on the real transform case (a
    # false negative for transform), so both metrics move, in the direction
    # each is supposed to.
    classifier = FakeClassifier({"explore": "explore", "drift": "transform"})
    report = run_corpus(_corpus(), classifier)
    transform = report.per_skill["transform"]
    assert transform.false_positives == 1
    assert transform.false_negatives == 1
    assert transform.precision == 0.0
    assert transform.recall == 0.0
    maintain = report.per_skill["maintain"]
    assert maintain.false_negatives == 1
    assert maintain.recall == 0.0


def test_run_corpus_result_rows_say_which_cases_missed():
    classifier = FakeClassifier({"explore": "explore"})
    report = run_corpus(_corpus(), classifier)
    wrong = [r for r in report.results if not r.correct]
    assert {r.task_id for r in wrong} == {"b", "c"}
    assert all(r.actual_skill is None for r in wrong)


def test_run_corpus_never_hides_a_second_marker_behind_the_first():
    # "did" spuriously fires explore on the maintain case, on top of "drift"
    # correctly firing maintain there. A classifier that reported only the
    # first name in some fixed order would pick one and hide the other, and
    # this genuine cross-skill contamination would never show up -- the exact
    # failure mode #216 exists to catch.
    classifier = FakeClassifier(
        {"explore": "explore", "did": "explore", "drift": "maintain"}
    )
    report = run_corpus(_corpus(), classifier)
    contaminated = next(r for r in report.results if r.task_id == "c")
    assert contaminated.fired_skills == frozenset({"explore", "maintain"})
    assert contaminated.actual_skill is None  # ambiguous: more than one fired
    assert contaminated.correct is False
    # explore fired where it should not have (a real false positive for it,
    # even though maintain -- the expected skill -- also fired on this case).
    assert report.per_skill["explore"].false_positives >= 1
    assert report.per_skill["maintain"].true_positives == 1


class _FlakyClassifier:
    """Reports a per-call failure the way a well-behaved Classifier should:
    via ClassifyResult.error, not by raising (a raise means the whole run
    should abort, see test_run_corpus_lets_a_setup_failure_abort_the_run)."""

    def __init__(self, fails_on: str, otherwise):
        self.fails_on = fails_on
        self.otherwise = otherwise

    def classify(self, prompt: str) -> ClassifyResult:
        if prompt == self.fails_on:
            return ClassifyResult(error="claude exited 1")
        return self.otherwise.classify(prompt)


def test_run_corpus_keeps_a_failed_call_out_of_precision_and_recall():
    # The maintain case's call fails outright. A failed call is neither "fired"
    # nor "did not fire": folding it into maintain's false-negative count would
    # blame the skill description for an infrastructure failure it had nothing
    # to do with, and folding it into "correctly classified none" would hide
    # the failure entirely.
    classifier = _FlakyClassifier(
        fails_on="did anything drift",
        otherwise=FakeClassifier(
            {"explore": "explore", "build": "transform", "drift": "maintain"}
        ),
    )
    report = run_corpus(_corpus(), classifier)
    failed = next(r for r in report.results if r.task_id == "c")
    assert failed.error is not None
    assert failed.actual_skill is None
    assert failed.correct is False
    assert "maintain" not in report.per_skill or (
        report.per_skill["maintain"].true_positives == 0
        and report.per_skill["maintain"].false_negatives == 0
    )
    # The other three cases are unaffected.
    assert report.per_skill["explore"].recall == 1.0
    assert report.per_skill["transform"].recall == 1.0


class _UnavailableClassifier:
    """Raises on the very first call and never gets a chance to run again,
    modeling ClaudeNotAvailableError: the CLI binary is missing, so every
    call would fail identically, and there is nothing case-specific about
    it."""

    def classify(self, prompt: str) -> ClassifyResult:
        raise RuntimeError("'claude' not found on PATH")


def test_run_corpus_lets_a_setup_failure_abort_the_run():
    # Unlike a per-call failure (reported via ClassifyResult.error and kept
    # in the results list), a classifier that raises is a setup problem the
    # whole run cannot proceed past. run_corpus does not catch it: it is left
    # to propagate to the caller (main() catches ClaudeNotAvailableError
    # specifically and exits 2), rather than being recorded once per corpus
    # case with the CLI still reporting a clean, misleading exit.
    with pytest.raises(RuntimeError, match="not found on PATH"):
        run_corpus(_corpus(), _UnavailableClassifier())


def test_the_committed_ade_bench_corpus_loads_and_validates():
    corpus = load_corpus(_REPO / "evals" / "corpus" / "ade_bench_triggering.json")
    assert corpus.name == "ade_bench_triggering"
    assert corpus.source["license"] == "Apache-2.0"
    assert corpus.source["repo"]
    assert len(corpus.cases) >= 20
    valid = {"explore", "transform", "maintain", None}
    for case in corpus.cases:
        assert case.task_id
        assert case.prompt
        assert case.expected_skill in valid


def test_corpus_requires_provenance(tmp_path):
    import json

    path = tmp_path / "no_source.json"
    path.write_text(
        json.dumps({"name": "x", "cases": [{"prompt": "p", "expected_skill": "none"}]}),
        encoding="utf-8",
    )
    with pytest.raises(InvalidSuiteError, match="source"):
        load_corpus(path)


def test_corpus_case_requires_expected_skill(tmp_path):
    import json

    path = tmp_path / "no_label.json"
    path.write_text(
        json.dumps(
            {
                "name": "x",
                "source": {"repo": "https://example.invalid", "license": "MIT"},
                "cases": [{"prompt": "p"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(InvalidSuiteError, match="expected_skill"):
        load_corpus(path)


def test_cli_requires_a_skill_unless_corpus_is_given():
    from evals.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
