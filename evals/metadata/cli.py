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
    scored = [r["score"] for r in rows]
    n = len(scored) or 1
    gate_failed = [s["task"] for s in scored if s["status"] == "gate_failed"]
    no_models = [s["task"] for s in scored if s["status"] == "no_models"]
    costs = [
        r["session"]["cost_usd"]
        for r in rows
        if r.get("session") and r["session"]["cost_usd"]
    ]
    return {
        "tasks": len(scored),
        "build_gate_pass_rate": round(1 - len(gate_failed) / n, 4),
        "gate_failed": gate_failed,
        "no_models": no_models,
        "completeness": round(sum(s["completeness"] for s in scored) / n, 4),
        "correctness": round(sum(s["correctness"] for s in scored) / n, 4),
        "checks_passed": round(sum(s["checks_passed"] for s in scored) / n, 4),
        "cost_usd": round(sum(costs), 4) if costs else None,
    }


def _print_table(report: dict[str, Any]) -> None:
    print(f"{'task':48} {'status':12} {'complete':>8} {'correct':>8} {'checks':>7}")
    for row in report["results"]:
        s = row["score"]
        print(
            f"{s['task']:48} {s['status']:12} {s['completeness']:8.2f} "
            f"{s['correctness']:8.2f} {s['checks_passed']:7.2f}"
        )
    agg = report["aggregate"]
    print(
        f"\n{agg['tasks']} tasks; build gate {agg['build_gate_pass_rate']:.0%}; "
        f"completeness {agg['completeness']:.2f}; "
        f"correctness {agg['correctness']:.2f}; "
        f"checks {agg['checks_passed']:.2f}"
        + (f"; cost ${agg['cost_usd']:.2f}" if agg.get("cost_usd") is not None else "")
    )


def main(argv: list[str] | None = None) -> int:
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

        report["provenance"] = provenance()
        report["settings"] = {
            "model": args.model,
            "max_turns": args.max_turns,
            "max_budget_usd": args.max_budget_usd,
        }
        agent_config = procedure_envelope()["block"]

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
            )
            row["session"] = session.__dict__
        row["workspace"] = str(workspace)
        row["score"] = score_workspace(task, fixture, workspace).to_dict()
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
    if report["aggregate"]["gate_failed"]:
        return 1
    if args.reference and any(
        r["score"]["status"] != "scored"
        or min(
            r["score"]["completeness"],
            r["score"]["correctness"],
            r["score"]["checks_passed"],
        )
        < 1
        for r in report["results"]
    ):
        return 1
    return 0
