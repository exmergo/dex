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

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from . import checks
from .suite import Corpus, EvalCase, EvalSuite


@dataclass
class AgentResult:
    """One agent run: its final output, whether the skill fired, and any error.

    ``triggered`` is what the triggering metric reads; ``output`` is what the judge
    grades. Backends populate ``envelopes`` when they can capture the engine's
    stdout envelopes, which lets assertions check the sanitized boundary directly.
    ``events`` is the run's ``stream-json`` transcript, which a case's
    deterministic ``checks`` are graded from (``evals/checks.py``).
    """

    output: str = ""
    triggered: bool = False
    envelopes: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    error: str | None = None


@runtime_checkable
class AgentRunner(Protocol):
    """Drives an agent on one prompt. ``skill_enabled`` selects the skill-active
    arm versus the no-skill baseline arm used for uplift.

    A case that needs more than its prompt (``EvalCase.streamed``: a working
    directory of its own, tool permissions, or checks read from the tool calls)
    is run as ``run(prompt, skill_enabled=..., case=case)``; every other call
    passes the prompt alone, so a runner that takes no ``case`` still runs
    every other case.
    """

    def run(self, prompt: str, *, skill_enabled: bool) -> AgentResult: ...


@runtime_checkable
class Judge(Protocol):
    """Decides whether an agent result satisfies one assertion.

    A judge that can say what it answered also has ``verdict(case, assertion,
    result) -> Verdict``, which the core reads in place of ``grade``: a judge
    that gave no verdict is then the case's error rather than a failed
    assertion, and its text reaches the report.
    """

    def grade(self, case: EvalCase, assertion: str, result: AgentResult) -> bool: ...


@dataclass
class Verdict:
    """What a judge said about one assertion: ``passed``, the ``text`` it
    answered, and ``error`` when it gave no verdict (it failed, timed out, or
    answered neither PASS nor FAIL)."""

    passed: bool
    text: str = ""
    error: str | None = None


_WORD = re.compile(r"[A-Za-z]+")


def read_verdict(text: str) -> Verdict:
    """A judge's answer, read tolerantly: the first word, whatever markdown or
    punctuation surrounds it and in any case (``**PASS**``, ``Pass.``)."""

    found = _WORD.search(text)
    word = found.group(0).upper() if found else ""
    if word in ("PASS", "PASSED"):
        return Verdict(passed=True, text=text)
    if word in ("FAIL", "FAILED"):
        return Verdict(passed=False, text=text)
    shown = text.strip()[:200] or "nothing"
    return Verdict(
        passed=False, text=text, error=f"answered neither PASS nor FAIL: {shown!r}"
    )


@dataclass
class ClassifyResult:
    """One classification call: every skill marker found, or the error if the
    call itself failed.

    Mirrors :class:`AgentResult`'s ``error`` field, for the same reason: a
    failed invocation (a crash, a timeout) and one that genuinely found no
    skill both have nowhere else to be told apart, and collapsing them would
    let infrastructure noise masquerade as a real classification. A
    :class:`Classifier` implementation is expected to catch its own
    per-*call* failures into ``error`` here (see :class:`ClaudeCliAgent.run`
    for the pattern) and reserve a raised exception for a setup problem that
    makes every subsequent call pointless too (the ``claude`` binary missing,
    say) -- :func:`run_corpus` does not catch anything, so that kind of
    failure aborts the whole run immediately rather than being recorded once
    per case.

    ``fired_skills`` may hold more than one name: a prompt two skills both
    claim is real evidence of cross-skill contamination, exactly what this
    corpus exists to catch, and picking a single "winner" would silently
    discard the other one's finding.
    """

    fired_skills: frozenset[str] = field(default_factory=frozenset)
    error: str | None = None


@runtime_checkable
class Classifier(Protocol):
    """Runs one prompt with every skill available and reports which fired.

    Deliberately not :class:`AgentRunner`: that protocol toggles one named
    skill on or off for the uplift measurement, which presumes the question
    is "does *this* skill help." A cross-skill corpus asks a different
    question, "which skills, if any, claim this prompt out of all of them at
    once," so it needs the names of whichever fired rather than a bool scoped
    to one. One live call per prompt instead of one per (prompt, skill) pair,
    too.
    """

    def classify(self, prompt: str) -> ClassifyResult: ...


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
    """One assertion's verdict. ``graded_by`` is ``judge`` for a judged
    assertion and ``check`` for one read from the tool calls. ``detail`` says
    what a check found, or why a judge gave no verdict; ``verdict`` is the text
    a judge answered, where it can report one."""

    assertion: str
    passed: bool
    graded_by: str = "judge"
    detail: str = ""
    verdict: str | None = None
    #: What a check saw that does not decide it (writes outside the project).
    info: str = ""


@dataclass
class CaseReport:
    id: int
    prompt: str
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        # A run that errored is no result, whatever its graders said about it.
        return (
            not self.error
            and bool(self.assertions)
            and all(a.passed for a in self.assertions)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "passed": self.passed,
            "error": self.error,
            "assertions": [
                {
                    "assertion": a.assertion,
                    "passed": a.passed,
                    "graded_by": a.graded_by,
                    **({"detail": a.detail} if a.detail else {}),
                    **({"verdict": a.verdict} if a.verdict is not None else {}),
                    **({"info": a.info} if a.info else {}),
                }
                for a in self.assertions
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

        return uplift_score(self.uplift_cases)

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
    suite: EvalSuite, runner: AgentRunner, judge: Judge, *, skill_enabled: bool = True
) -> list[CaseReport]:
    """Run every case once and grade it. ``skill_enabled=False`` runs the
    baseline arm, which is how ``--case`` records a case against the agent
    alone (``--uplift``)."""

    reports: list[CaseReport] = []
    for case in suite.evals:
        result = _run_case(runner, case, skill_enabled)
        graded = _grade(case, result, judge)
        reports.append(
            CaseReport(
                id=case.id,
                prompt=case.prompt,
                error=result.error or _judge_error(graded),
                assertions=graded,
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


def uplift_from(
    with_skill: list[CaseReport], without_skill: list[CaseReport]
) -> list[UpliftCaseReport]:
    """Pair each case's two arms, already run, into uplift rows."""

    baseline = {c.id: c.passed for c in without_skill}
    return [
        UpliftCaseReport(
            id=c.id, passed_with_skill=c.passed, passed_without_skill=baseline[c.id]
        )
        for c in with_skill
    ]


def uplift_score(cases: list[UpliftCaseReport]) -> float:
    """Net fraction of cases the skill turns from failing to passing."""

    with_skill = _rate([u.passed_with_skill for u in cases])
    without = _rate([u.passed_without_skill for u in cases])
    return round(with_skill - without, 4)


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
    result = _run_case(runner, case, skill)
    graded = _grade(case, result, judge)
    # A judge that gave no verdict has already failed its assertion.
    return not result.error and bool(graded) and all(a.passed for a in graded)


def _judge_error(graded: list[AssertionResult]) -> str | None:
    """The first judge that gave no verdict, as the case's error: a judge
    that failed is no evidence about the agent, so it is not a FAIL."""

    for a in graded:
        if a.graded_by == "judge" and a.detail.startswith("no verdict"):
            return f"the judge gave {a.detail} on {a.assertion!r}"
    return None


def _judged(judge: Judge, case: EvalCase, assertion: str, result: AgentResult):
    reads = getattr(judge, "verdict", None)
    if reads is None:
        passed = judge.grade(case, assertion, result)
        return AssertionResult(assertion=assertion, passed=passed)
    said = reads(case, assertion, result)
    if said.error:
        return AssertionResult(
            assertion=assertion,
            passed=False,
            detail=f"no verdict: {said.error}",
            verdict=said.text,
        )
    return AssertionResult(
        assertion=assertion, passed=said.passed, verdict=said.text or None
    )


def _run_case(runner: AgentRunner, case: EvalCase, skill: bool) -> AgentResult:
    if case.streamed:
        return runner.run(case.prompt, skill_enabled=skill, case=case)
    return runner.run(case.prompt, skill_enabled=skill)


def _grade(case: EvalCase, result: AgentResult, judge: Judge) -> list[AssertionResult]:
    """The judged assertions, then the checks read from the tool calls. A run
    that errored passes no check: an empty transcript holds no forbidden call,
    which is no evidence the agent avoided one."""

    graded = [_judged(judge, case, a, result) for a in case.assertions]
    for check in case.checks:
        if result.error:
            passed, detail, info = False, f"not graded: {result.error}", ""
        else:
            passed, detail = checks.grade(check, result.events)
            info = checks.information(check, result.events)
        graded.append(
            AssertionResult(
                assertion=check.assertion,
                passed=passed,
                graded_by="check",
                detail=detail,
                info=info,
            )
        )
    return graded


def _rate(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 4) if flags else 0.0


@dataclass
class CorpusCaseResult:
    """One corpus prompt classified: what it should fire, what it did.

    ``fired_skills`` is every marker the classifier found, not a single
    winner: a case where two skills both fired is a real cross-skill
    contamination finding, and this keeps it visible instead of quietly
    picking one and discarding the other's evidence.

    ``error`` is set instead of trusting ``fired_skills`` when the call
    itself failed (CLI crash, timeout, malformed output): a failed call and a
    call that genuinely found nothing both surface as an empty set from a
    :class:`~evals.runner.Classifier`, and collapsing them would let
    infrastructure noise masquerade as "correctly classified none" or as an
    ordinary miss. A case with ``error`` set is never ``correct`` and is kept
    out of every skill's precision/recall (see :func:`run_corpus`).
    """

    task_id: str
    prompt: str
    expected_skill: str | None
    fired_skills: frozenset[str] = field(default_factory=frozenset)
    error: str | None = None

    @property
    def actual_skill(self) -> str | None:
        """The one fired skill, when exactly one did; ``None`` when none or
        more than one did (``fired_skills`` carries the full picture either
        way -- this is a display convenience for the common, unambiguous
        case, not the source of truth ``correct`` and the per-skill scoring
        below read from)."""
        return next(iter(self.fired_skills)) if len(self.fired_skills) == 1 else None

    @property
    def correct(self) -> bool:
        if self.error is not None:
            return False
        expected = {self.expected_skill} if self.expected_skill is not None else set()
        return set(self.fired_skills) == expected

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "expected_skill": self.expected_skill,
            "fired_skills": sorted(self.fired_skills),
            "actual_skill": self.actual_skill,
            "correct": self.correct,
            "error": self.error,
        }


@dataclass
class SkillPrecisionRecall:
    """Precision/recall for one skill, computed as a one-vs-rest split of the
    corpus: a true positive is this skill correctly firing, a false positive
    is this skill firing when something else (or nothing) should have, a
    false negative is this skill staying quiet when it should have fired."""

    skill: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        fired = self.true_positives + self.false_positives
        return round(self.true_positives / fired, 4) if fired else 1.0

    @property
    def recall(self) -> float:
        expected = self.true_positives + self.false_negatives
        return round(self.true_positives / expected, 4) if expected else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass
class CorpusReport:
    corpus_name: str
    results: list[CorpusCaseResult] = field(default_factory=list)
    per_skill: dict[str, SkillPrecisionRecall] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return _rate([r.correct for r in self.results])

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_name": self.corpus_name,
            "case_count": len(self.results),
            "accuracy": self.accuracy,
            "per_skill": {name: pr.to_dict() for name, pr in self.per_skill.items()},
            "results": [r.to_dict() for r in self.results],
        }


def run_corpus(corpus: Corpus, classifier: Classifier) -> CorpusReport:
    """Classify every corpus prompt and score per-skill precision/recall.

    One live call per prompt, with every skill available at once, is the
    point: it is the shape a real request actually arrives in, unlike the
    per-skill positive/negative lists in ``skills/<skill>/evals/evals.json``,
    which each only ever see one skill enabled at a time and so cannot catch
    a case where a *sibling's* description fires instead of the right one.

    Nothing here is wrapped in a ``try``: a :class:`Classifier` is expected
    to catch its own per-call failures into :class:`ClassifyResult.error`
    (see that class), the same contract :class:`AgentRunner` already has with
    :func:`run_triggering`/:func:`run_quality`. A call that raises anyway is
    a setup problem, not a one-off flake (the ``claude`` binary missing is
    the concrete case this matters for), and every case after it would fail
    identically, so it is left to propagate to the caller immediately rather
    than being recorded 30 times over.
    """

    results = []
    for case in corpus.cases:
        outcome = classifier.classify(case.prompt)
        results.append(
            CorpusCaseResult(
                task_id=case.task_id,
                prompt=case.prompt,
                expected_skill=case.expected_skill,
                fired_skills=outcome.fired_skills,
                error=outcome.error,
            )
        )

    scored = [r for r in results if r.error is None]
    skills = sorted(
        {r.expected_skill for r in scored if r.expected_skill is not None}
        | {skill for r in scored for skill in r.fired_skills}
    )
    per_skill: dict[str, SkillPrecisionRecall] = {}
    for skill in skills:
        pr = SkillPrecisionRecall(skill=skill)
        for r in scored:
            expected = r.expected_skill == skill
            actual = skill in r.fired_skills
            if expected and actual:
                pr.true_positives += 1
            elif actual and not expected:
                pr.false_positives += 1
            elif expected and not actual:
                pr.false_negatives += 1
        per_skill[skill] = pr

    return CorpusReport(corpus_name=corpus.name, results=results, per_skill=per_skill)
