"""Terminal questions and completion; all setup behavior stays in the engine."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from . import envelope as env
from .onboarding import discover, setup_envelope
from .onboarding_docs import ENTERPRISE


def _ask(prompt: str, default: str = "") -> str:
    print(
        f"{prompt}{f' [{default}]' if default else ''}: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = input().strip()
    return answer or default


def _choose(prompt: str, choices: list[str], labels: list[str] | None = None) -> str:
    print(prompt, file=sys.stderr)
    for index, label in enumerate(labels or choices, 1):
        print(f"  {index}. {label}", file=sys.stderr)
    while True:
        answer = _ask("Choose", "1")
        if answer in choices:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print("Choose one of the listed options.", file=sys.stderr)


def cmd_init(args: argparse.Namespace, engine) -> env.Envelope:
    interactive = not args.no_interactive and sys.stdin.isatty() and sys.stderr.isatty()
    name, mode, project_dir = args.name, args.mode, args.project_dir
    agent_files = args.agent_file
    root = Path(engine.repo_root).resolve()
    try:
        if interactive:
            from .config import load_config
            from .transform.init import VALID_CONNECTORS

            facts = discover(root)
            config = load_config(root)
            if (
                mode is None
                and not name
                and not facts["projects"]
                and not facts["semantic"]
                and not facts["semantic_files"]
                and not (root / ".dex/setup.json").is_file()
            ):
                mode = _choose(
                    "Set up Dex",
                    ["new", "demo", "explore"],
                    [
                        "Create a Dex project (recommended)",
                        "Try the demo",
                        "Explore without dbt",
                    ],
                )
            if (
                len(facts["projects"]) > 1
                and not project_dir
                and not (config and config.dbt_project_dir)
            ):
                project_dir = _choose(
                    "Select the existing dbt project", facts["projects"]
                )
            if mode in {"new", "demo"} and not name and not facts["projects"]:
                name = _ask(
                    "dbt project name (use . for the current directory)", "analytics"
                )
            if (
                mode != "demo"
                and not engine.connector
                and not config
                and not facts["projects"]
            ):
                engine.connector = _choose(
                    "Choose your data connector", list(VALID_CONNECTORS)
                )
            connector = (
                "duckdb"
                if mode == "demo"
                else engine.connector
                or (
                    config.connector
                    if config and "connector" in config.model_fields_set
                    else None
                )
            )
            existing_project = project_dir or (
                config.dbt_project_dir if config else None
            )
            if not existing_project and len(facts["projects"]) == 1:
                existing_project = facts["projects"][0]
            if connector is None and existing_project:
                from .dbt_project import resolve_target

                with contextlib.suppress(Exception):
                    connector = resolve_target(
                        root / existing_project,
                        args.target or (config.dbt_target if config else None),
                    ).type
            if (
                connector == "duckdb"
                and mode != "demo"
                and not engine.path
                and not (config and config.duckdb)
                and not existing_project
            ):
                engine.path = _ask("Path to your existing DuckDB warehouse")
            elif connector and connector != "duckdb":
                from .connect import _SCOPE_FIELDS

                block = getattr(config, connector) if config else None
                if (
                    connector == "bigquery"
                    and not engine.project
                    and not (block and block.project)
                ):
                    engine.project = _ask("BigQuery billing project")
                if (
                    not engine.scopes
                    and not engine.datasets
                    and not (block and getattr(block, _SCOPE_FIELDS[connector], []))
                ):
                    scope = _ask(
                        "Source scopes, comma-separated (blank means all "
                        "visible sources)"
                    )
                    engine.scopes = [
                        s.strip() for s in scope.split(",") if s.strip()
                    ] or None
                if (
                    mode != "explore"
                    and not args.dev_destination
                    and not facts["projects"]
                ):
                    args.dev_destination = (
                        _ask(
                            "Separate dev destination (blank uses the connector's "
                            "dev default)"
                        )
                        or None
                    )
            if (
                len(facts["agent_files"]) == 2
                and agent_files is None
                and not args.no_agent_instructions
            ):
                choice = _choose(
                    "Install the procedure into",
                    ["both", "AGENTS.md", "CLAUDE.md"],
                    [
                        "Both existing agent files (recommended)",
                        "AGENTS.md",
                        "CLAUDE.md",
                    ],
                )
                agent_files = facts["agent_files"] if choice == "both" else [choice]
        kwargs = {
            "mode": mode,
            "project_dir": project_dir,
            "target": args.target,
            "dev_destination": args.dev_destination,
            "agent_files": agent_files,
            "no_agent_instructions": args.no_agent_instructions,
            "layered_schemas": args.layered_schemas,
            "in_place": args.in_place,
        }
        result = engine.initialize(name, **kwargs)
        if interactive and result.pending_confirmation:
            for diff in result.diffs:
                print(diff.get("unified") or diff, file=sys.stderr)
            if _ask("Apply this configuration change? (yes/no)", "no").lower() in {
                "yes",
                "y",
            }:
                engine.confirmed = True
                result = engine.initialize(name, **kwargs)
        envelope = setup_envelope(result)
        if interactive:
            print(
                f"Dex setup: {len(result.created)} files created, "
                f"{len(result.updated)} updated.",
                file=sys.stderr,
            )
            for check, state in result.readiness.items():
                print(f"  {check}: {state['status']}", file=sys.stderr)
            for problem in envelope.errors:
                print(problem, file=sys.stderr)
            for step in result.next_steps:
                print(f"Next: {step['command']} — {step['purpose']}", file=sys.stderr)
            if envelope.status is env.Status.OK:
                print(f"\n{ENTERPRISE}", file=sys.stderr)
        return envelope
    except (EOFError, KeyboardInterrupt):
        return env.error(
            "setup cancelled; run dex init to resume",
            reason=env.Reason.REQUEST,
            data={"cancelled": True},
        )
