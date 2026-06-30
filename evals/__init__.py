"""Tier-2 agent evals: do the skills trigger correctly and help over baseline?

This is a repo-level harness, deliberately NOT part of the published
``exmergo-dex-core`` engine. The engine is the portable, agent-agnostic runtime;
this tool drives a concrete agent (Claude today, others later) to test the skills,
so it lives with the skills it tests, alongside the Tier-3 ``benchmarks/`` harness.

The scoring core (:mod:`evals.runner`) is deterministic and unit-tested with
fakes; the live model backend (:mod:`evals.claude_agent`) is a thin, swappable
implementation behind the :class:`~evals.runner.AgentRunner` and
:class:`~evals.runner.Judge` protocols, which is what lets a non-Claude agent be
plugged in for the portability benchmark without touching the scoring logic.
"""

from __future__ import annotations

from .runner import (
    AgentResult,
    AgentRunner,
    Judge,
    SuiteReport,
    run_suite,
)
from .suite import EvalCase, EvalSuite, load_suite

__all__ = [
    "AgentResult",
    "AgentRunner",
    "EvalCase",
    "EvalSuite",
    "Judge",
    "SuiteReport",
    "load_suite",
    "run_suite",
]
