"""CLI: run a skill's Tier-2 eval suite.

    python -m evals skills/explore                 # full suite (live Claude)
    python -m evals skills/explore --triggering    # triggering only (cheaper)
    python -m evals skills/explore --json          # machine-readable report

The default backend drives Claude Code headless; it needs the ``claude`` CLI and
a workspace with the dex plugin installed. Exit code is non-zero if the suite does
not pass (clean triggering and no regression versus baseline), so the same command
serves both local runs and the release gate.
"""

from __future__ import annotations

import argparse
import json
import sys

from .claude_agent import ClaudeCliAgent, ClaudeCliJudge, ClaudeNotAvailableError
from .runner import run_suite, run_triggering
from .suite import load_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description="Tier-2 skill evals")
    parser.add_argument("skill", help="skill dir or path to evals.json")
    parser.add_argument("--model", default=None, help="model override for the agent")
    parser.add_argument(
        "--triggering", action="store_true", help="run only the triggering check"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--timeout", type=int, default=180, help="per-agent-call timeout (seconds)"
    )
    args = parser.parse_args(argv)

    suite = load_suite(args.skill)
    agent = ClaudeCliAgent(
        skill_name=suite.skill_name, model=args.model, timeout=args.timeout
    )

    try:
        if args.triggering:
            trig = run_triggering(suite, agent)
            if args.json:
                print(json.dumps(trig.to_dict(), indent=2))
            else:
                _print_triggering(suite.skill_name, trig)
            return 0 if trig.passed else 1

        judge = ClaudeCliJudge(model=args.model, timeout=args.timeout)
        report = run_suite(suite, agent, judge)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report)
        return 0 if report.passed else 1
    except ClaudeNotAvailableError as exc:
        print(f"cannot run live evals: {exc}", file=sys.stderr)
        return 2


def _print_triggering(skill: str, report) -> None:
    fired = f"{report.positives_fired}/{report.positives_total} positives fired"
    print(f"[{skill}] triggering")
    print(f"  recall    {report.recall:.0%}  ({fired})")
    print(f"  precision {report.precision:.0%}  ({report.negatives_fired} false)")
    for missed in report.missed_triggers:
        print(f"  MISSED  {missed!r}")
    for false in report.false_triggers:
        print(f"  FALSE   {false!r}")


def _print_report(report) -> None:
    _print_triggering(report.skill_name, report.triggering)
    print(f"[{report.skill_name}] output quality {report.quality_pass_rate:.0%}")
    for case in report.quality:
        mark = "ok " if case.passed else "FAIL"
        print(f"  {mark} #{case.id} {case.prompt[:60]!r}")
        for a in case.assertions:
            if not a.passed:
                print(f"        - failed: {a.assertion}")
    print(f"[{report.skill_name}] uplift vs baseline {report.uplift_score:+.0%}")
    print(f"[{report.skill_name}] {'PASS' if report.passed else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
