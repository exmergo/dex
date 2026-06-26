"""The command contract: the integration keystone.

SKILL.md / AGENTS.md tell the agent which subcommand to run; a thin PEP 723
wrapper runs it via ``uv run``; this module prints exactly one sanitized JSON
envelope to stdout and nothing else. Subcommands are stateless (state lives in the
dbt project, which is the source of truth, plus the ``.dex/`` cache), so the agent
orchestrates multi-step flows.

Only ``connect test`` does real work today (a read-only DuckDB probe). Every other
subcommand returns a valid envelope with status ``not_implemented`` so the
contract, the wrappers, and the eval harness can all be exercised before the
engine logic exists. Capabilities, not final spelling.
"""

from __future__ import annotations

import argparse
import sys

from . import envelope as env

# The full command surface. Group -> its subcommands. ``connect test`` is
# special-cased as the only live command today.
COMMAND_SURFACE: dict[str, list[str]] = {
    "connect": ["test"],
    "explore": ["inventory", "profile", "relationships", "map"],
    "transform": ["plan", "apply", "build"],
    "model": ["define", "maintain"],
    "emit": ["dbt", "osi"],
    "reconcile": [],
    "viz": ["preview"],
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dex",
        description="dex-core command contract (Explore. Transform. Model.)",
    )
    # Global, connection-oriented options so every command shares one resolution
    # path. Cost-spending commands additionally require --confirm + --budget.
    parser.add_argument(
        "--connector",
        default=None,
        help="connector name (default: from .dex/config.yml)",
    )
    parser.add_argument(
        "--path", default=None, help="DuckDB file path (DuckDB connector)"
    )
    parser.add_argument("--repo-root", default=".", help="repo root holding .dex/")
    parser.add_argument(
        "--confirm", action="store_true", help="confirm a command that would spend"
    )
    parser.add_argument(
        "--budget", type=float, default=None, help="per-session cost ceiling"
    )

    groups = parser.add_subparsers(dest="group", required=True)
    for group, subcommands in COMMAND_SURFACE.items():
        gp = groups.add_parser(group)
        if subcommands:
            sub = gp.add_subparsers(dest="subcommand", required=True)
            for name in subcommands:
                sp = sub.add_parser(name)
                # transform plan/apply take a positional argument in later phases.
                if group == "transform" and name in {"plan", "apply"}:
                    sp.add_argument("argument", nargs="?", default=None)
    return parser


def _connect_test(args: argparse.Namespace) -> env.Envelope:
    from .connect import open_adapter

    adapter = open_adapter(
        connector=args.connector, path=args.path, repo_root=args.repo_root
    )
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
