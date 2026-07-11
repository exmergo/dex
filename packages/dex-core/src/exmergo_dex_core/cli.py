"""The command contract: the integration keystone.

SKILL.md / AGENTS.md tell the agent which subcommand to run; a thin PEP 723
wrapper runs it via ``uv run``; this module prints exactly one sanitized JSON
envelope to stdout and nothing else. Subcommands are stateless (state lives in the
dbt project, which is the source of truth, plus the ``.dex/`` cache), so the agent
orchestrates multi-step flows.

``connect test``, the ``explore`` group, the authoring surface (``transform``,
``semantic``), and the ``maintain`` group are live. ``viz preview`` returns a
valid envelope with status ``not_implemented`` until the Viz integration lands,
so the contract, the wrappers, and the eval harness stay exercisable.
"""

from __future__ import annotations

import argparse
import sys

from . import envelope as env

# The full command surface. Group -> its subcommands.
COMMAND_SURFACE: dict[str, list[str]] = {
    "connect": ["test"],
    "explore": ["inventory", "profile", "relationships", "map", "query"],
    "transform": ["init", "plan", "apply", "build", "deps", "plans"],
    "semantic": ["define", "update", "plan"],
    # maintain: keep the dbt project correct as the world drifts. `snapshot`
    # captures the known-good baseline; `check` sweeps every axis against it;
    # `schema`/`volume`/`grain`/`semantic` are the per-axis deep detectors;
    # `reconcile` proposes the fixing diffs. Detection is read-only; only
    # reconcile emits diffs.
    "maintain": [
        "snapshot",
        "check",
        "schema",
        "volume",
        "grain",
        "semantic",
        "reconcile",
    ],
    "viz": ["preview"],
}


def _sub_connection_options() -> argparse.ArgumentParser:
    """The connection options as a parent for subparsers, with SUPPRESS defaults.

    Shared by every subparser so the options also work AFTER the subcommand (the
    contract documents them there, e.g. ``connect test --path X``). SUPPRESS means
    an option absent after the subcommand does not clobber a value passed before
    it; the top-level parser carries the real defaults so the attribute always
    exists. Net: both ``dex --path X connect test`` and
    ``dex connect test --path X`` resolve identically.
    """

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--connector", default=argparse.SUPPRESS)
    common.add_argument("--path", default=argparse.SUPPRESS)
    # The portable source-scope override, repeatable: each connector reads it in
    # its own namespace vocabulary. Nothing is written to config, and a scope may
    # only narrow a committed allowlist, never widen it.
    common.add_argument("--scope", action="append", default=argparse.SUPPRESS)
    # BigQuery's older spelling of --scope, kept because `connect test --project X
    # --dataset Y` is how a BigQuery connection is smoke-tested before a
    # .dex/config.yml bigquery block exists. Both error on other connectors.
    common.add_argument("--project", default=argparse.SUPPRESS)
    common.add_argument("--dataset", action="append", default=argparse.SUPPRESS)
    common.add_argument("--repo-root", default=argparse.SUPPRESS)
    common.add_argument("--confirm", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--budget", type=float, default=argparse.SUPPRESS)
    return common


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dex",
        description="dex-core command contract (Explore. Transform. Maintain.)",
    )
    # Real defaults live on the top-level parser so every namespace has them.
    parser.add_argument("--connector", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--scope", action="append", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--dataset", action="append", default=None)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--budget", type=float, default=None)

    common = _sub_connection_options()
    groups = parser.add_subparsers(dest="group", required=True)
    for group, subcommands in COMMAND_SURFACE.items():
        gp = groups.add_parser(group, parents=[common])
        if subcommands:
            sub = gp.add_subparsers(dest="subcommand", required=True)
            for name in subcommands:
                sp = sub.add_parser(name, parents=[common])
                if group == "explore" and name == "inventory":
                    sp.add_argument(
                        "--rank", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "explore" and name == "profile":
                    sp.add_argument("objects", nargs="+")
                if group == "explore" and name == "query":
                    sp.add_argument("sql")
                if group == "explore" and name == "map":
                    sp.add_argument(
                        "--full", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "explore" and name in {"relationships", "map"}:
                    sp.add_argument(
                        "--verify", action="store_true", default=argparse.SUPPRESS
                    )
                # transform init takes the project name; plan the intent; apply
                # the plan id.
                if group == "transform" and name in {"init", "plan", "apply"}:
                    sp.add_argument("argument", nargs="?", default=None)
                if group == "transform" and name == "plan":
                    # The agent-authored edits payload: a JSON file, or - for stdin.
                    sp.add_argument("--edits-file", default=None)
                    sp.add_argument("--scaffold", action="append", default=None)
                if group == "transform" and name == "build":
                    sp.add_argument("--target", default=None)
                    sp.add_argument("--select", default=None)
                if group == "semantic":
                    sp.add_argument("argument", nargs="?", default=None)
                    sp.add_argument("--edits-file", default=None)
                    sp.add_argument("--no-parse", action="store_true", default=False)
                # maintain detectors take an optional object scope (default: whole
                # project); reconcile takes an optional drift class to fix.
                if group == "maintain" and name in {
                    "schema",
                    "volume",
                    "grain",
                    "semantic",
                }:
                    sp.add_argument("objects", nargs="*")
                if group == "maintain" and name == "reconcile":
                    sp.add_argument(
                        "drift_class",
                        nargs="?",
                        choices=["schema", "volume", "grain", "semantic"],
                        default=None,
                    )
    return parser


def _connect_test(args: argparse.Namespace) -> env.Envelope:
    from .command_args import open_from_args

    adapter = open_from_args(args)
    try:
        # A free probe on every connector, but the envelope's cost paradigm
        # reflects the connector so the agent knows what later commands bill in.
        gate = getattr(adapter, "cost_gate", None)
        cost = gate.cost() if gate is not None else env.Cost()
        return env.ok(adapter.capabilities(), cost=cost)
    finally:
        adapter.close()


def dispatch(args: argparse.Namespace) -> env.Envelope:
    command = args.group + (
        f" {args.subcommand}" if getattr(args, "subcommand", None) else ""
    )

    if args.group == "connect" and args.subcommand == "test":
        return _connect_test(args)

    if args.group == "explore":
        from .explore import commands as explore_cmds

        handlers = {
            "inventory": explore_cmds.cmd_inventory,
            "profile": explore_cmds.cmd_profile,
            "relationships": explore_cmds.cmd_relationships,
            "map": explore_cmds.cmd_map,
            "query": explore_cmds.cmd_query,
        }
        return handlers[args.subcommand](args)

    # The transform skill fronts the authoring surface (transform, semantic);
    # they share one plan store and one write path, so one command module serves
    # them all. `viz preview` stays a stub until the Viz integration lands.
    transform_surface = {
        ("transform", "init"),
        ("transform", "plan"),
        ("transform", "apply"),
        ("transform", "build"),
        ("transform", "deps"),
        ("transform", "plans"),
        ("semantic", "define"),
        ("semantic", "update"),
        ("semantic", "plan"),
    }
    if args.group == "maintain":
        from .maintain import commands as maintain_cmds

        handlers = {
            "snapshot": maintain_cmds.cmd_snapshot,
            "check": maintain_cmds.cmd_check,
            "schema": maintain_cmds.cmd_schema,
            "volume": maintain_cmds.cmd_volume,
            "grain": maintain_cmds.cmd_grain,
            "semantic": maintain_cmds.cmd_semantic,
            "reconcile": maintain_cmds.cmd_reconcile,
        }
        handler = handlers.get(args.subcommand)
        if handler is not None:
            return handler(args)

    if (args.group, args.subcommand) in transform_surface:
        from .transform import commands as transform_cmds

        handlers = {
            ("transform", "init"): transform_cmds.cmd_init,
            ("transform", "plan"): transform_cmds.cmd_plan,
            ("transform", "apply"): transform_cmds.cmd_apply,
            ("transform", "build"): transform_cmds.cmd_build,
            ("transform", "deps"): transform_cmds.cmd_deps,
            ("transform", "plans"): transform_cmds.cmd_plans,
            ("semantic", "define"): transform_cmds.cmd_semantic_define,
            ("semantic", "update"): transform_cmds.cmd_semantic_update,
            ("semantic", "plan"): transform_cmds.cmd_semantic_plan,
        }
        return handlers[(args.group, args.subcommand)](args)

    # Everything else is scaffolded against the contract but not yet built.
    return env.not_implemented(command)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        envelope = dispatch(args)
    except env.SanitizationError:
        # A sanitization failure must never be swallowed: re-raise so it surfaces
        # loudly in tests and CI rather than shipping a leak.
        raise
    except Exception as exc:
        envelope = env.error(env.redact(str(exc)))

    env.emit(envelope)
    return 0 if envelope.status != env.Status.ERROR else 1


if __name__ == "__main__":
    sys.exit(main())
