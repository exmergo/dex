"""CLI: run a skill's Tier-2 eval suite, or the cross-skill triggering corpus.

    python -m evals skills/explore                 # full suite (live Claude)
    python -m evals skills/explore --triggering    # triggering only (cheaper)
    python -m evals skills/explore --json          # machine-readable report
    python -m evals skills/explore --case 7 --case 8
                                                     # only those quality cases
    python -m evals --corpus evals/corpus/ade_bench_triggering.json
                                                     # cross-skill corpus (#216)

The default backend drives Claude Code headless; it needs the ``claude`` CLI. The
skill under test loads from ``--plugin-dir`` (default: this checkout), not from
whatever dex plugin the machine has installed. Exit code is non-zero if the suite does
not pass (clean triggering and no regression versus baseline), so the same command
serves both local runs and the release gate. ``--corpus`` always exits 0: it is a
measurement (per-skill precision/recall against externally authored prompts), not
a pass/fail gate, per the issue that added it (#216) -- the initial numbers are
expected to be low, and that is the signal, not a bug to chase away.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .claude_agent import (
    ClaudeCliAgent,
    ClaudeCliClassifier,
    ClaudeCliJudge,
    ClaudeNotAvailableError,
)
from .runner import CorpusReport, run_corpus, run_quality, run_suite, run_triggering
from .suite import load_corpus, load_suite

_REPO = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["metadata"]:
        # The metadata-quality eval (#492) needs DuckDB, PyYAML and dbt, so it is
        # imported only when asked for and runs in the engine's environment; the
        # skill evals below stay stdlib-only.
        from .metadata.cli import main as metadata_main

        return metadata_main(raw[1:])
    parser = argparse.ArgumentParser(prog="evals", description="Tier-2 skill evals")
    parser.add_argument(
        "skill", nargs="?", default=None, help="skill dir or path to evals.json"
    )
    parser.add_argument("--model", default=None, help="model override for the agent")
    parser.add_argument(
        "--triggering", action="store_true", help="run only the triggering check"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--timeout", type=int, default=180, help="per-agent-call timeout (seconds)"
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help=(
            "path to a cross-skill triggering corpus (see evals/corpus/); runs "
            "independently of the skill argument, one live call per prompt"
        ),
    )
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=_REPO,
        help="dex plugin to evaluate (default: this checkout, %(default)s)",
    )
    parser.add_argument(
        "--case",
        type=int,
        action="append",
        dest="cases",
        metavar="ID",
        help=(
            "run only this output-quality case (repeatable); skips triggering "
            "and the uplift baseline"
        ),
    )
    args = parser.parse_args(argv)

    plugin_dir = args.plugin_dir.resolve()
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        parser.error(f"--plugin-dir {plugin_dir} has no .claude-plugin/plugin.json")
    if args.cases and (args.triggering or args.corpus):
        parser.error("--case only applies to a skill's output-quality cases")
    plugin_args = ["--plugin-dir", str(plugin_dir)]
    # stderr, so --json output stays parseable.
    print(f"evaluating the dex plugin at {plugin_dir}", file=sys.stderr)

    try:
        if args.corpus:
            corpus = load_corpus(args.corpus)
            classifier = ClaudeCliClassifier(
                model=args.model, timeout=args.timeout, args=plugin_args
            )
            report = run_corpus(corpus, classifier)
            if args.json:
                print(json.dumps(report.to_dict(), indent=2))
            else:
                _print_corpus(report)
            return 0  # a measurement, never a gate; see the module docstring

        if args.skill is None:
            parser.error("skill is required unless --corpus is given")

        suite = load_suite(args.skill)
        agent = ClaudeCliAgent(
            skill_name=suite.skill_name,
            model=args.model,
            timeout=args.timeout,
            skill_args=plugin_args,
        )

        if args.cases:
            unknown = sorted(set(args.cases) - {c.id for c in suite.evals})
            if unknown:
                parser.error(f"{suite.skill_name} has no case(s) {unknown}")
            suite.evals = [c for c in suite.evals if c.id in args.cases]
            judge = ClaudeCliJudge(model=args.model, timeout=args.timeout)
            cases = run_quality(suite, agent, judge)
            if args.json:
                print(json.dumps([c.to_dict() for c in cases], indent=2))
            else:
                _print_quality(suite.skill_name, cases)
            return 0 if all(c.passed for c in cases) else 1

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


def _print_corpus(report: CorpusReport) -> None:
    print(
        f"[{report.corpus_name}] {len(report.results)} cases, "
        f"accuracy {report.accuracy:.0%}"
    )
    for skill in sorted(report.per_skill):
        pr = report.per_skill[skill]
        print(
            f"  {skill:10s} precision {pr.precision:.0%}  recall {pr.recall:.0%}  "
            f"f1 {pr.f1:.0%}  (tp={pr.true_positives} fp={pr.false_positives} "
            f"fn={pr.false_negatives})"
        )
    errors = [r for r in report.results if r.error is not None]
    misses = [r for r in report.results if not r.correct and r.error is None]
    if misses:
        print(f"  {len(misses)} miss(es):")
        for r in misses:
            fired = sorted(r.fired_skills) or ["none"]
            print(f"    {r.task_id}: expected {r.expected_skill!r}, got {fired}")
    if errors:
        print(f"  {len(errors)} call(s) failed (excluded from precision/recall):")
        for r in errors:
            print(f"    {r.task_id}: {r.error}")


def _print_triggering(skill: str, report) -> None:
    fired = f"{report.positives_fired}/{report.positives_total} positives fired"
    print(f"[{skill}] triggering")
    print(f"  recall    {report.recall:.0%}  ({fired})")
    print(f"  precision {report.precision:.0%}  ({report.negatives_fired} false)")
    for missed in report.missed_triggers:
        print(f"  MISSED  {missed!r}")
    for false in report.false_triggers:
        print(f"  FALSE   {false!r}")


def _print_quality(skill: str, cases) -> None:
    rate = sum(c.passed for c in cases) / len(cases) if cases else 0.0
    print(f"[{skill}] output quality {rate:.0%}")
    for case in cases:
        mark = "ok " if case.passed else "FAIL"
        print(f"  {mark} #{case.id} {case.prompt[:60]!r}")
        for a in case.assertions:
            if not a.passed:
                print(f"        - failed: {a.assertion}")


def _print_report(report) -> None:
    _print_triggering(report.skill_name, report.triggering)
    _print_quality(report.skill_name, report.quality)
    print(f"[{report.skill_name}] uplift vs baseline {report.uplift_score:+.0%}")
    print(f"[{report.skill_name}] {'PASS' if report.passed else 'FAIL'}")


if __name__ == "__main__":
    sys.exit(main())
