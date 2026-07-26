"""The command contract: the integration keystone.

SKILL.md / AGENTS.md tell the agent which subcommand to run; a thin PEP 723
wrapper runs it via ``uv run``; this module prints exactly one sanitized JSON
envelope to stdout and nothing else. Subcommands are stateless (state lives in the
dbt project, which is the source of truth, plus the scratch state the store holds),
so the agent orchestrates multi-step flows.

The CLI is the first consumer of :class:`~.engine.Engine` rather than a parallel
implementation of it: ``main`` parses arguments, builds one engine from the
resolved repo root (filesystem store, config read from ``.dex/``), and every
command runs against that engine and hands back a result the shim wraps in an
envelope. Dogfooding the API this way is what stops the two surfaces drifting.

``connect test``, the ``explore`` group, the authoring surface (``transform``,
``semantic``), and the ``maintain`` group are live. ``viz preview`` returns a
valid envelope with status ``not_implemented`` until the Viz integration lands,
so the contract, the wrappers, and the eval harness stay exercisable.
"""

from __future__ import annotations

import argparse
import sys

from . import command_args
from . import envelope as env
from .engine import Engine
from .guards.cost_guard import ConfirmationRequiredError
from .results import BudgetExhaustedError

# The full command surface. Group -> its subcommands.
COMMAND_SURFACE: dict[str, list[str]] = {
    "connect": ["test"],
    "explore": [
        "inventory",
        "profile",
        "relationships",
        "map",
        "query",
        "cluster",
        "semantic",
    ],
    "transform": ["init", "plan", "apply", "build", "deps", "plans", "macro"],
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
                if group == "explore" and name == "cluster":
                    sp.add_argument("object")
                    sp.add_argument(
                        "--features", action="append", default=argparse.SUPPRESS
                    )
                    sp.add_argument(
                        "-k",
                        "--clusters",
                        dest="k",
                        type=int,
                        default=argparse.SUPPRESS,
                    )
                # `explore semantic list|query` queries the dbt semantic layer
                # (distinct from the top-level `semantic` group, which authors it).
                # The backend (local MetricFlow vs a hosted dbt Cloud deployment) is
                # ambient: the .dex config `semantic.backend`, overridable here with
                # --local / --api.
                if group == "explore" and name == "semantic":
                    # Bare `explore semantic` lists (discovery is first-class);
                    # `explore semantic query` runs a metric query.
                    sp.add_argument(
                        "mode", nargs="?", choices=["list", "query"], default="list"
                    )
                    sp.add_argument("--metric", action="append", default=None)
                    sp.add_argument("--group-by", action="append", default=None)
                    sp.add_argument("--where", action="append", default=None)
                    sp.add_argument("--order-by", action="append", default=None)
                    sp.add_argument("--grain", default=None)
                    sp.add_argument("--limit", type=int, default=None)
                    sp.add_argument("--local", action="store_true", default=False)
                    sp.add_argument("--api", action="store_true", default=False)
                if group == "explore" and name == "map":
                    sp.add_argument(
                        "--full", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "explore" and name in {"relationships", "map"}:
                    sp.add_argument(
                        "--verify", action="store_true", default=argparse.SUPPRESS
                    )
                # Force a full re-profile even when the cache holds a fresh,
                # schema-matching profile for a requested object (the default is
                # skip-if-cached; --refresh is the escape hatch when the source
                # changed in a way the cheap metadata check cannot see).
                if group == "explore" and name in {"profile", "map", "relationships"}:
                    sp.add_argument(
                        "--refresh", action="store_true", default=argparse.SUPPRESS
                    )
                # Exploration starts bare: warehouse truth, independent of
                # whatever repo dex runs from. --use-project opts in to folding
                # the project's declared definitions (joins, grain, metric
                # lineage) into the result.
                if group == "explore" and name in {"profile", "relationships", "map"}:
                    sp.add_argument(
                        "--use-project",
                        action="store_true",
                        default=argparse.SUPPRESS,
                    )
                # transform init takes the project name; plan the intent; apply
                # the plan id; macro the shipped-macro name (none lists them).
                if group == "transform" and name in {"init", "plan", "apply", "macro"}:
                    sp.add_argument("argument", nargs="?", default=None)
                if group == "transform" and name == "init":
                    sp.add_argument(
                        "--layered-schemas", action="store_true", default=False
                    )
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


def dispatch(args: argparse.Namespace, engine: Engine) -> env.Envelope:
    """Route one parsed command to its handler and return exactly one envelope.

    Total by construction: the two refusals the engine raises rather than returns
    (an unmet confirmation, a mid-run budget exhaustion) are transport concerns
    at this boundary and become envelopes here, so no handler has to know about
    them and every path out of dispatch is an envelope.
    """

    try:
        return _run(args, engine)
    except ConfirmationRequiredError as exc:
        return env.needs_confirmation(
            exc.request.data, cost=exc.request.cost, warnings=exc.request.warnings
        )
    except BudgetExhaustedError as exc:
        # Partial completion: an error, but one that reports what the attempt
        # actually cost, because that is what a caller needs to size the re-run.
        return env.error(
            env.redact(str(exc)), data={"spend": exc.spend} if exc.spend else {}
        )


def _run(args: argparse.Namespace, engine: Engine) -> env.Envelope:
    if args.group == "connect" and args.subcommand == "test":
        from .results import to_envelope

        return to_envelope(engine.connect_test())

    if args.group == "explore":
        from .explore import commands as explore_cmds

        handlers = {
            "inventory": explore_cmds.cmd_inventory,
            "profile": explore_cmds.cmd_profile,
            "relationships": explore_cmds.cmd_relationships,
            "map": explore_cmds.cmd_map,
            "query": explore_cmds.cmd_query,
            "cluster": explore_cmds.cmd_cluster,
            "semantic": explore_cmds.cmd_semantic,
        }
        return handlers[args.subcommand](args, engine)

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
        return handlers[args.subcommand](args, engine)

    # The transform skill fronts the authoring surface (transform, semantic);
    # they share one plan store and one write path, so one command module serves
    # them all. Handlers are named rather than referenced so the surface is
    # listed once and the module is still imported only on a hit: it pulls the
    # dbt reader and the dialect engine, which live behind a connector extra, so
    # importing it for a command that never needed it breaks a lighter install.
    authoring = {
        ("transform", "init"): "cmd_init",
        ("transform", "plan"): "cmd_plan",
        ("transform", "apply"): "cmd_apply",
        ("transform", "build"): "cmd_build",
        ("transform", "deps"): "cmd_deps",
        ("transform", "plans"): "cmd_plans",
        ("transform", "macro"): "cmd_macro",
        ("semantic", "define"): "cmd_semantic_define",
        ("semantic", "update"): "cmd_semantic_update",
        ("semantic", "plan"): "cmd_semantic_plan",
    }
    handler = authoring.get((args.group, args.subcommand))
    if handler is not None:
        from .transform import commands as transform_cmds

        return getattr(transform_cmds, handler)(args, engine)

    # Everything else is scaffolded against the contract but not yet built.
    return env.not_implemented(command_args.command_name(args))


def main(argv: list[str] | None = None) -> int:
    from .command_args import repo_root

    parser = _build_parser()
    args = parser.parse_args(argv)
    # One engine per process, built from the resolved repo root. That choice of
    # backend is what lets the subcommands stay stateless across invocations:
    # `.dex/` on disk is how the exploration cache and the cumulative session
    # budget survive from one command to the next.
    engine = Engine.from_repo(
        repo_root(args),
        connector=getattr(args, "connector", None),
        path=getattr(args, "path", None),
        project=getattr(args, "project", None),
        datasets=getattr(args, "dataset", None),
        scopes=getattr(args, "scope", None),
        budget=getattr(args, "budget", None),
        confirmed=getattr(args, "confirm", False),
    )
    try:
        try:
            envelope = dispatch(args, engine)
        finally:
            engine.close()
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
