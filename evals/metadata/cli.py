"""``python -m evals metadata``: run, score and report the metadata-quality eval.

    # Free: score every task's reference answer (the scorer's own self-test).
    uv run --python 3.13 --project packages/dex-core --extra duckdb \
        python -m evals metadata --reference

    # Paid: one headless Claude Code session per task, then score.
    uv run --python 3.13 --project packages/dex-core --extra duckdb \\
        python -m evals metadata --model claude-sonnet-5-5 \\
        --out evals/metadata/runs/<name>

    # Re-score a workspace a run left behind.
    ... python -m evals metadata --score-only <workspace> --task <fixture/task>

The build gate is not a threshold: a run where any task fails it exits 1.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .fixtures import find_tasks, fixture_of, load_fixtures, materialize
from .score import score_workspace


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scored tasks without treating ungraded checks as passes."""

    scored = [r["score"] for r in rows]
    n = len(scored) or 1
    gate_failed = [s["task"] for s in scored if s["status"] == "gate_failed"]
    no_models = [s["task"] for s in scored if s["status"] == "no_models"]
    blocked = [s["task"] for s in scored if s["status"] == "blocked_reported"]
    costs = [
        r["session"]["cost_usd"]
        for r in rows
        if r.get("session") and r["session"]["cost_usd"]
    ]
    attributed = [s["attribution"] for s in scored if s.get("attribution") is not None]
    scoped = [s["scope"] for s in scored if s.get("scope") is not None]
    procedural = [s["procedure"] for s in scored if s.get("procedure") is not None]
    private = [s for s in scored if s.get("privacy") is not None]
    reported = [s["report"]["passed"] for s in scored if s.get("report") is not None]
    diagnosed = [
        s["diagnosis"]["passed"] for s in scored if s.get("diagnosis") is not None
    ]
    # A diagnose task authors no declaration, so it has none to average in.
    declared = [s for s in scored if s.get("completeness") is not None]
    tested = [
        s["incremental_test"]["passed"]
        for s in scored
        if s.get("incremental_test") is not None
    ]
    caught = [
        s["defects_caught"] for s in scored if s.get("defects_caught") is not None
    ]
    return {
        "tasks": len(scored),
        "build_gate_pass_rate": round(1 - len(gate_failed) / n, 4),
        "gate_failed": gate_failed,
        "no_models": no_models,
        # Left blocked on the prerequisite the task allows, and reported (#1083).
        "blocked_reported": blocked,
        "completeness": (
            round(sum(s["completeness"] for s in declared) / len(declared), 4)
            if declared
            else None
        ),
        "correctness": (
            round(sum(s["correctness"] for s in declared) / len(declared), 4)
            if declared
            else None
        ),
        "checks_passed": round(sum(s["checks_passed"] for s in scored) / n, 4),
        # Over the tasks that grade it, so the others cannot inflate or dilute it.
        "attribution": (
            round(sum(attributed) / len(attributed), 4) if attributed else None
        ),
        "procedure": (
            round(sum(procedural) / len(procedural), 4) if procedural else None
        ),
        # Over the tasks that grade it (#1115): the population built is the one
        # declared, and one the request allows.
        "scope": round(sum(scoped) / len(scoped), 4) if scoped else None,
        "cost_usd": round(sum(costs), 4) if costs else None,
        "privacy_failed": [s["task"] for s in private if not s["privacy"]["passed"]],
        "privacy_pass_rate": (
            sum(s["privacy"]["passed"] for s in private) / len(private)
            if private
            else None
        ),
        # The agent's own warehouse: a source changed that the task protects,
        # or a requested change that never reached dev (#1083).
        "workspace_failed": [
            s["task"]
            for s in scored
            if any(not c["passed"] for c in s.get("workspace_checks") or [])
        ],
        "report": round(sum(reported) / len(reported), 4) if reported else None,
        # Localized, evidenced and qualified diagnoses (#1048, #1116), over the
        # tasks that grade one, apart from whether anything built.
        "diagnosis": round(sum(diagnosed) / len(diagnosed), 4) if diagnosed else None,
        # An incremental model's next run tested at all (#1111), apart from
        # whether the replays found its rows right.
        "incremental_test": (round(sum(tested) / len(tested), 4) if tested else None),
        # Planted defects the agent's tests caught (#1112), over the tasks that
        # plant any: a statement about the tests, never about the model.
        "defects_caught": round(sum(caught) / len(caught), 4) if caught else None,
    }


def _number(value: float | None, width: int) -> str:
    """A score to two places, or ``-`` where the task grades none."""

    return f"{value:{width}.2f}" if value is not None else f"{'-':>{width}}"


def _verdict(passed: bool) -> str:
    """Render one boolean rubric result for the evaluation table."""

    return "pass" if passed else "FAIL"


def _print_table(report: dict[str, Any]) -> None:
    """Print build, workspace, procedure and final-report scores per task."""

    print(
        f"{'task':48} {'status':12} {'complete':>8} {'correct':>8} {'checks':>7} "
        f"{'attrib':>7} {'scope':>5} {'proc':>5} {'privacy':>7} {'wspace':>6} "
        f"{'report':>6} {'diagnose':>8} {'inctest':>7} {'defects':>7}"
    )
    for row in report["results"]:
        s = row["score"]
        print(
            f"{s['task']:48} {s['status']:12} {_number(s['completeness'], 8)} "
            f"{_number(s['correctness'], 8)} {s['checks_passed']:7.2f} "
            + (
                f"{s['attribution']:7.2f}"
                if s.get("attribution") is not None
                else f"{'-':>7}"
            )
            + (f" {s['scope']:5.2f}" if s.get("scope") is not None else f" {'-':>5}")
            + (
                f" {s['procedure']:5.2f}"
                if s.get("procedure") is not None
                else f" {'-':>5}"
            )
            + (
                f" {'pass' if s['privacy']['passed'] else 'FAIL':>7}"
                if s.get("privacy") is not None
                else f" {'-':>7}"
            )
            + (
                f" {_verdict(all(c['passed'] for c in s['workspace_checks'])):>6}"
                if s.get("workspace_checks")
                else f" {'-':>6}"
            )
            + (
                f" {'pass' if s['report']['passed'] else 'FAIL':>6}"
                if s.get("report") is not None
                else f" {'-':>6}"
            )
            + (
                f" {_verdict(s['diagnosis']['passed']):>8}"
                if s.get("diagnosis") is not None
                else f" {'-':>8}"
            )
            + (
                f" {_verdict(s['incremental_test']['passed']):>7}"
                if s.get("incremental_test") is not None
                else f" {'-':>7}"
            )
            + (
                f" {s['defects_caught']:7.2f}"
                if s.get("defects_caught") is not None
                else f" {'-':>7}"
            )
        )
    agg = report["aggregate"]
    print(
        f"\n{agg['tasks']} tasks; build gate {agg['build_gate_pass_rate']:.0%}; "
        f"completeness {_number(agg['completeness'], 0)}; "
        f"correctness {_number(agg['correctness'], 0)}; "
        f"checks {agg['checks_passed']:.2f}"
        + (
            f"; attribution {agg['attribution']:.2f}"
            if agg.get("attribution") is not None
            else ""
        )
        + (f"; scope {agg['scope']:.2f}" if agg.get("scope") is not None else "")
        + (
            f"; procedure {agg['procedure']:.2f}"
            if agg.get("procedure") is not None
            else ""
        )
        + (f"; cost ${agg['cost_usd']:.2f}" if agg.get("cost_usd") is not None else "")
        + (
            f"; privacy {agg['privacy_pass_rate']:.0%}"
            if agg.get("privacy_pass_rate") is not None
            else ""
        )
        + (
            f"; workspace failed {len(agg['workspace_failed'])}"
            if agg.get("workspace_failed")
            else ""
        )
        + (f"; report {agg['report']:.2f}" if agg.get("report") is not None else "")
        + (
            f"; diagnosis {agg['diagnosis']:.2f}"
            if agg.get("diagnosis") is not None
            else ""
        )
        + (
            f"; defects caught {agg['defects_caught']:.2f}"
            if agg.get("defects_caught") is not None
            else ""
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Run the requested metadata evaluation mode and report its results."""

    parser = argparse.ArgumentParser(
        prog="evals metadata", description="Metadata-quality eval (#492)"
    )
    parser.add_argument(
        "--fixture", action="append", help="limit to a fixture (repeatable)"
    )
    parser.add_argument(
        "--task", action="append", help="limit to a task id or fixture/task"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--reference", action="store_true", help="score the reference answers"
    )
    mode.add_argument(
        "--score-only", metavar="WORKSPACE", help="score an existing workspace"
    )
    parser.add_argument(
        "--model", default="claude-sonnet-5-5", help="model for live runs"
    )
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-budget-usd", type=float, default=5.0, help="per task")
    parser.add_argument("--timeout", type=int, default=2400, help="per task, seconds")
    parser.add_argument(
        "--out", default=None, help="directory for workspaces and the report"
    )
    parser.add_argument(
        "--procedure-file",
        default=None,
        help=(
            "live runs: give the agent this file instead of the engine's procedure, "
            "to measure a baseline against an older text; recorded in the report"
        ),
    )
    parser.add_argument(
        "--plugin-dir",
        default=None,
        help=(
            "live runs: load the plugin from this directory rather than the working "
            "tree. A baseline needs it, because the skills carry the procedure too"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    fixtures = load_fixtures()
    tasks = find_tasks(fixtures, fixture=args.fixture, task=args.task)
    if not tasks:
        parser.error("no task matches")
    if args.score_only and len(tasks) != 1:
        parser.error("--score-only scores one task: name it with --task")

    out = Path(args.out or tempfile.mkdtemp(prefix="dex-metadata-run-"))
    out.mkdir(parents=True, exist_ok=True)
    live = not (args.reference or args.score_only)
    report: dict[str, Any] = {
        "suite": "metadata-quality (#492)",
        "mode": "live" if live else ("reference" if args.reference else "score-only"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": [],
    }
    agent_config = None
    if live:
        from .live import procedure_envelope, provenance, run_session

        procedure_text = (
            Path(args.procedure_file).read_text(encoding="utf-8")
            if args.procedure_file
            else None
        )
        report["provenance"] = provenance(args.plugin_dir, procedure_text)
        report["settings"] = {
            "model": args.model,
            "max_turns": args.max_turns,
            "max_budget_usd": args.max_budget_usd,
        }
        agent_config = (
            procedure_envelope()["block"] if procedure_text is None else procedure_text
        )

    for task in tasks:
        fixture = fixture_of(task, fixtures)
        row: dict[str, Any] = {"task": task.key}
        if args.score_only:
            workspace = Path(args.score_only)
        else:
            workspace = materialize(
                task,
                fixture,
                out / task.key.replace("/", "__"),
                agent_config=agent_config,
                reference=args.reference,
            )
        if live:
            print(f"running {task.key} ...", file=sys.stderr, flush=True)
            session = run_session(
                task.prompt,
                workspace,
                out / f"{task.key.replace('/', '__')}.transcript.jsonl",
                model=args.model,
                max_turns=args.max_turns,
                max_budget_usd=args.max_budget_usd,
                timeout=args.timeout,
                plugin_dir=Path(args.plugin_dir) if args.plugin_dir else None,
            )
            row["session"] = session.__dict__
        row["workspace"] = str(workspace)
        row["score"] = score_workspace(
            task,
            fixture,
            workspace,
            calls=row["session"]["dex_calls"] if live else None,
            transcript=out / f"{task.key.replace('/', '__')}.transcript.jsonl"
            if live
            else None,
            final=row["session"]["final"] if live else None,
            reference=args.reference,
        ).to_dict()
        report["results"].append(row)

    report["aggregate"] = _aggregate(report["results"])
    (out / "report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_table(report)
        print(f"report: {out / 'report.json'}")
    if (
        report["aggregate"]["gate_failed"]
        or report["aggregate"]["privacy_failed"]
        or report["aggregate"]["workspace_failed"]
    ):
        return 1
    if args.reference and any(
        r["score"]["status"] != "scored"
        or min(
            1 if r["score"]["completeness"] is None else r["score"]["completeness"],
            1 if r["score"]["correctness"] is None else r["score"]["correctness"],
            r["score"]["checks_passed"],
            1 if r["score"]["attribution"] is None else r["score"]["attribution"],
            1 if r["score"].get("scope") is None else r["score"]["scope"],
            1
            if r["score"].get("defects_caught") is None
            else r["score"]["defects_caught"],
        )
        < 1
        for r in report["results"]
    ):
        return 1
    return 0
