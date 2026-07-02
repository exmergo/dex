"""The command contract: the integration keystone.

SKILL.md / AGENTS.md tell the agent which subcommand to run; a thin PEP 723
wrapper runs it via ``uv run``; this module prints exactly one sanitized JSON
envelope to stdout and nothing else. Subcommands are stateless (state lives in the
dbt project, which is the source of truth, plus the ``.dex/`` cache), so the agent
orchestrates multi-step flows.

``connect test``, the ``explore`` group, and the authoring surface (``transform``,
``semantic``, ``emit dbt``) are live. The ``maintain`` group, ``emit osi``, and
``viz preview`` return a valid envelope with status ``not_implemented`` so the
contract, the wrappers, and the eval harness can be exercised before their engine
logic exists.
"""

from __future__ import annotations

import argparse
import sys

from . import envelope as env

# The full command surface. Group -> its subcommands.
COMMAND_SURFACE: dict[str, list[str]] = {
    "connect": ["test"],
    "explore": ["inventory", "profile", "relationships", "map", "query"],
    "transform": ["plan", "apply", "build"],
    "semantic": ["define", "update"],
    "emit": ["dbt", "osi"],
    # maintain: keep the dbt project correct as the world drifts. `snapshot`
    # captures the known-good baseline; `check` sweeps every axis against it;
    # `schema`/`grain`/`semantic` are the per-axis deep detectors; `reconcile`
    # proposes the fixing diffs. Detection is read-only; only reconcile emits diffs.
    "maintain": ["snapshot", "check", "schema", "grain", "semantic", "reconcile"],
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
                # transform plan takes the intent; apply takes the plan id.
                if group == "transform" and name in {"plan", "apply"}:
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
                if group == "emit" and name == "dbt":
                    sp.add_argument("plan_id", nargs="?", default=None)
                # maintain detectors take an optional object scope (default: whole
                # project); reconcile takes an optional drift class to fix.
                if group == "maintain" and name in {"schema", "grain", "semantic"}:
                    sp.add_argument("objects", nargs="*")
                if group == "maintain" and name == "reconcile":
                    sp.add_argument(
                        "drift_class",
                        nargs="?",
                        choices=["schema", "grain", "semantic"],
                        default=None,
                    )
    return parser


def _connect_test(args: argparse.Namespace) -> env.Envelope:
    from .command_args import open_from_args

    adapter = open_from_args(args)
    try:
        return env.ok(adapter.capabilities())
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

    # The transform skill fronts the authoring surface (transform, semantic,
    # emit dbt); they share one plan store and one write path, so one command
    # module serves them all. `emit osi` stays reserved for the dormant exporter,
    # and `viz preview` stays a stub until the Viz integration lands.
    transform_surface = {
        ("transform", "plan"),
        ("transform", "apply"),
        ("transform", "build"),
        ("semantic", "define"),
        ("semantic", "update"),
        ("emit", "dbt"),
    }
    if (args.group, args.subcommand) in transform_surface:
        from .transform import commands as transform_cmds

        handlers = {
            ("transform", "plan"): transform_cmds.cmd_plan,
            ("transform", "apply"): transform_cmds.cmd_apply,
            ("transform", "build"): transform_cmds.cmd_build,
            ("semantic", "define"): transform_cmds.cmd_semantic_define,
            ("semantic", "update"): transform_cmds.cmd_semantic_update,
            ("emit", "dbt"): transform_cmds.cmd_emit_dbt,
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
