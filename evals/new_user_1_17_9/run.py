#!/usr/bin/env python3
"""Black-box acceptance test against published skills and PyPI, never local Dex.

Run with Python's standard library; npx, git and uv must already be installed.
The destination must not exist. Every command and envelope is retained there.
"""

import argparse
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(destination):
    for executable in ("npx", "uv", "git"):
        require(shutil.which(executable), f"Install prerequisite: {executable}")
    destination.mkdir(parents=True, exist_ok=False)
    evidence = destination / "evidence"
    evidence.mkdir()
    payloads = destination / "payloads"
    payloads.mkdir()
    sequence = 0

    def command(argv, label):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:02d}-{label}"
        started = time.monotonic()
        # Commands are fixed by this harness and use argv without a shell.
        result = subprocess.run(  # noqa: S603
            argv, cwd=destination, capture_output=True, text=True
        )
        (evidence / f"{stem}.stdout").write_text(result.stdout)
        (evidence / f"{stem}.stderr").write_text(result.stderr)
        with (evidence / "commands.jsonl").open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "command": argv,
                        "label": label,
                        "exit_code": result.returncode,
                        "seconds": time.monotonic() - started,
                    }
                )
                + "\n"
            )
        require(result.returncode == 0, f"{label} failed; inspect {evidence / stem}")
        return result.stdout

    command(["git", "init"], "git-init")
    # Pin the skill tag as well as checking its runtime pin, so future releases
    # cannot silently turn this historical acceptance test into a different one.
    command(
        [
            "npx",
            "--yes",
            "skills",
            "add",
            "exmergo/dex@v1.17.9",
            "--yes",
            "--agent",
            "codex",
        ],
        "install-skills",
    )
    wrapper = destination / ".agents/skills/transform/scripts/run.py"
    require(wrapper.exists(), "Installer did not create the transform wrapper")
    require('DEX_CORE_VERSION = "1.17.9"' in wrapper.read_text(), "Wrong engine pin")
    require(
        not (destination / "packages/dex-core").exists(), "Local source shadows PyPI"
    )
    for skill in ("explore", "transform", "maintain"):
        require(
            (destination / f".agents/skills/{skill}/SKILL.md").exists(),
            f"Missing installed skill: {skill}",
        )

    def dex(label, *args):
        print(label, flush=True)
        result = json.loads(
            command(
                ["uv", "run", "--no-project", "--script", str(wrapper), *args], label
            )
        )
        require(result["status"] == "ok", f"{label}: {result}")
        require(not result.get("errors"), f"{label}: {result['errors']}")
        return result["data"]

    def payload(name, content):
        path = payloads / name
        path.write_text(json.dumps(content, indent=2))
        return str(path)

    def apply(plan):
        dex("apply", "transform", "apply", plan["plan_id"])

    def build(plan):
        data = dex(
            "build",
            "transform",
            "build",
            "--target",
            "dev",
            "--verify",
            "--for-plan",
            plan["plan_id"],
        )
        completion = data["completion"]
        require(completion["process"]["success"], "dbt process failed")
        require(not completion["blockers"], "Completion blockers remain")
        for state in ("failed", "skipped", "missing", "stale"):
            require(
                completion["deliverables"][state]["count"] == 0,
                f"Unfinished deliverables: {state}",
            )
        require(data["verification"]["ran"], "Verification did not run")
        require(not data["verification"]["findings"], "Verification found defects")
        require(
            not completion["obligations"].get("contradicted"),
            "Contradicted obligations",
        )
        return data

    setup = dex(
        "init",
        "init",
        "demo_analytics",
        "--mode",
        "demo",
        "--layered-schemas",
        "--agent-file",
        "AGENTS.md",
        "--no-interactive",
    )
    require(not setup["missing_inputs"], "Init needs additional inputs")
    require(setup["readiness"]["connection"]["status"] == "passed", "Connection failed")
    mapping = dex("map", "explore", "map")
    require(mapping["profiled_count"] == 7, "Demo did not profile all seven tables")
    dex(
        "profile",
        "explore",
        "profile",
        "main.orders",
        "main.customers",
        "--columns",
        "all",
    )
    joins = dex("verify-relationships", "explore", "relationships", "--verify")
    edge = next(
        e
        for e in joins["relationships"]
        if e["from_dataset"].endswith(".orders")
        and e["to_dataset"].endswith(".customers")
    )
    require(
        edge["verified"] and edge["orphan_fraction"] == 0,
        "Order/customer join is unsafe",
    )

    model_payload = payload(
        "models.json", json.loads((FIXTURES / "models.json").read_text())
    )
    models = dex(
        "plan-models",
        "transform",
        "plan",
        "Layered all-status order analytics",
        "--edits-file",
        model_payload,
    )
    for name, declaration in models["declarations"].items():
        require(not declaration["missing"], f"Missing intent for {name}")
        require(not declaration.get("contradictions"), f"Contradiction for {name}")
    apply(models)
    built = build(models)
    require(
        built["completion"]["deliverables"]["built"]["count"] == 6,
        "Not all six models built",
    )

    # Use the engine's derived model; author metrics on its measures separately.
    definitions = [
        d
        for d in models["semantic_proposal"]["definitions"]
        if d["path"].endswith("fct_orders_semantic.yml")
    ]
    require(len(definitions) == 1, "Order semantic proposal missing")
    saved = json.loads((FIXTURES / "semantics.json").read_text())["definitions"]
    definitions += [d for d in saved if d["kind"] == "metric"]
    semantic = dex(
        "plan-semantics",
        "semantic",
        "plan",
        "--definitions-file",
        payload("semantics.json", {"definitions": definitions}),
    )
    apply(semantic)
    build(semantic)
    catalog = dex("catalog", "explore", "semantic", "list")
    require(
        {m["name"] for m in catalog["metrics"]}
        == {"orders", "booked_amount", "average_order_value"},
        "Metric catalog differs",
    )

    dex(
        "profile-built",
        "explore",
        "profile",
        "staging_dev.stg_orders",
        "staging_dev.stg_customers",
        "intermediate_dev.int_orders_enriched",
        "--target",
        "dev",
    )
    dex("scaffold-unit-test", "transform", "test", "--scaffold", "dim_customers")
    tests = dex(
        "plan-tests",
        "transform",
        "plan",
        "Customer boundary fixtures and reconciliation",
        "--edits-file",
        payload("tests.json", json.loads((FIXTURES / "tests.json").read_text())),
    )
    apply(tests)
    build(tests)
    # Anchor final deliverables to the original model plan: test-only plans in
    # 1.17.9 report coverage_undetermined even when their tests pass.
    final = build(models)
    require(
        final["completion"]["outcome"] == "validated", "Model completion not validated"
    )
    require(
        final["completion"]["process"]["node_counts"]["pass"] == 29,
        "Expected 29 passing tests",
    )
    mutation = dex("mutation", "transform", "test", "--mutate", "dim_customers")
    require(mutation["counts"]["generated"] == 2, "Mutation sites changed")
    require(mutation["counts"]["killed"] == 2, "Rollup defects escaped tests")
    require(
        not mutation["baseline"]["excluded"], "Mutation excluded failing baseline tests"
    )

    total = dex(
        "metric-total",
        "explore",
        "semantic",
        "query",
        "orders",
        "booked_amount",
        "average_order_value",
        "--local",
    )
    require(not total["truncated"], "Metric total truncated")
    values = dict(zip(total["columns"], total["cells"][0], strict=True))
    require(values["orders"] == 5000, "Order metric differs")
    require(
        math.isclose(values["booked_amount"], 2338243.40, abs_tol=0.005),
        "Amount metric differs",
    )
    require(
        math.isclose(values["average_order_value"], 467.64868), "Ratio metric differs"
    )
    for label, dimension, extra in (
        ("country", "order__country_code", []),
        ("month", "metric_time", ["--grain", "month"]),
    ):
        grouped = dex(
            f"metric-{label}",
            "explore",
            "semantic",
            "query",
            "orders",
            "booked_amount",
            "--local",
            "--group-by",
            dimension,
            *extra,
        )
        require(not grouped["truncated"], f"{label} groups truncated")
        rows = [
            dict(zip(grouped["columns"], row, strict=True)) for row in grouped["cells"]
        ]
        require(
            sum(row["orders"] for row in rows) == values["orders"],
            f"{label} count does not reconcile",
        )
        require(
            math.isclose(
                sum(row["booked_amount"] for row in rows),
                values["booked_amount"],
                abs_tol=0.005,
            ),
            f"{label} amount differs",
        )
    roster = dex(
        "customer-aggregate",
        "explore",
        "query",
        "--target",
        "dev",
        "select count(*) as customers, sum(order_count) as orders, "
        "count(*) filter (where order_count = 0) as empty_customers, "
        "sum(booked_amount) as booked_amount from marts_dev.dim_customers",
    )
    # One statement returns cells directly; batched statements use results[].
    require(roster["cells"][0][:3] == [1200, 5000, 18], "Customer population differs")

    verified = dex("maintain-verify", "maintain", "verify")
    require(not verified["findings"], "Project maintenance verification found defects")
    dex("final-map", "explore", "map", "--use-project", "--full")
    dex("snapshot", "maintain", "snapshot")

    def check_baseline(label):
        data = dex(label, "maintain", "check")
        unexpected = [
            f
            for f in data["findings"]
            if not (
                f["code"] == "no_rows" and f["identifier"].endswith(".main.returns")
            )
        ]
        require(not unexpected, f"Unexpected baseline findings: {unexpected}")

    check_baseline("baseline-check")
    original = next(d for d in definitions if d["content"].startswith("name: orders\n"))
    changed = {
        **original,
        "content": original["content"].replace(
            "label: Orders", "label: Orders drift probe"
        ),
    }
    drift = dex(
        "plan-drift",
        "semantic",
        "plan",
        "--definitions-file",
        payload("drift.json", {"definitions": [changed]}),
    )
    apply(drift)
    detected = dex("detect-drift", "maintain", "check")
    require(
        any(
            f["axis"] == "semantic" and f["code"] == "definition_changed"
            for f in detected["findings"]
        ),
        "Semantic change went undetected",
    )
    restore = dex(
        "plan-restore",
        "semantic",
        "plan",
        "--definitions-file",
        payload("restore.json", {"definitions": [original]}),
    )
    apply(restore)
    check_baseline("restored-check")
    summary = {
        "status": "passed",
        "version": "1.17.9",
        "models": 6,
        "tests": 29,
        "mutants_killed": 2,
        "metrics": values,
        "limitations": final["completion"]["obligations"],
        "project": str(destination / "demo_analytics"),
    }
    (evidence / "acceptance.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", type=Path, help="New directory outside a Dex source checkout"
    )
    options = parser.parse_args()
    run(options.destination.resolve())
