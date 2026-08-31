"""The command contract: the integration keystone.

SKILL.md / AGENTS.md tell the agent which subcommand to run; a thin PEP 723
wrapper runs it via ``uv run``; this module prints exactly one sanitized JSON
envelope to stdout and nothing else. Subcommands are stateless (state lives in the
dbt project, which is the source of truth, plus the scratch state the store holds),
so the agent orchestrates multi-step flows.

The CLI is the first consumer of :class:`~.engine.DexEngine` rather than a parallel
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
import contextlib
import sys

from . import command_args
from . import envelope as env
from .engine import DexEngine
from .guards.cost_guard import ConfirmationRequiredError, CostGuardError
from .guards.dialect import DialectDependencyError
from .guards.dialect import ensure_available as ensure_dialect_available
from .results import BudgetExhaustedError

# The full command surface. Group -> its subcommands.
#
# Adding a subcommand here means adding it to `DexEngine` too (#344): the CLI is
# a wrapper over the engine, not a parallel implementation of it, so every entry
# below needs a `DexEngine` method offering the same capability, unless it is
# named in `_SUBCOMMAND_PARITY`'s CLI-only allowlist with a reason (see
# `packages/dex-core/tests/test_cli_contract.py`, the section on CLI/DexEngine
# parity). That test fails on a subcommand with no method and on one whose
# method cannot express something the CLI can, which is what keeps the two
# surfaces from drifting apart the way `DexEngine.check()` once had (it dropped
# the object-scope argument all four of its sibling detectors accept).
COMMAND_SURFACE: dict[str, list[str]] = {
    "connect": ["test"],
    "explore": [
        "inventory",
        "profile",
        "relationships",
        "map",
        "diagram",
        "query",
        "cluster",
        "semantic",
    ],
    "transform": [
        "init",
        "plan",
        "apply",
        "build",
        "deps",
        "plans",
        "macro",
        "references",
        "rename",
        "remove",
        "place",
        "test",
    ],
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
    # demo: the on-ramp. No subcommands, because there is exactly one thing to
    # do and it is the first command a new user runs. It is also the only verb
    # that creates a data file, which is why it lives on its own path (see
    # `demo/warehouse.py`) rather than anywhere near a connector.
    "demo": [],
}


def _bare_subcommand_index() -> dict[str, list[str]]:
    """Map each subcommand name to the groups that define it."""
    index: dict[str, list[str]] = {}
    for group, names in COMMAND_SURFACE.items():
        for name in names:
            index.setdefault(name, []).append(group)
    return index


def _rewrite_unambiguous_bare_subcommand(argv: list[str]) -> list[str]:
    """Insert the group for an unambiguous bare subcommand; else return argv."""
    if not argv:
        return argv
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in {
            "--connector",
            "--path",
            "--scope",
            "--project",
            "--dataset",
            "--repo-root",
            "--cache-backend",
            "--project-format",
            "--budget",
        }:
            i += 2
            continue
        i += 1
    if i >= len(argv):
        return argv
    token = argv[i]
    if token in COMMAND_SURFACE:
        return argv
    groups = _bare_subcommand_index().get(token, [])
    if len(groups) == 1:
        return [*argv[:i], groups[0], token, *argv[i + 1 :]]
    return argv


def _bare_subcommand_suggestion(token: str) -> str | None:
    groups = _bare_subcommand_index().get(token, [])
    if not groups:
        return None
    if len(groups) == 1:
        return f"did you mean '{groups[0]} {token}'?"
    listed = ", ".join(f"'{g} {token}'" for g in groups)
    return f"subcommand '{token}' is ambiguous; try one of: {listed}"


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


#: One line per group, shown both in --help's subcommand list and as the
#: onboarding orientation (#296): `dex --help` is where a stranger's first
#: contact lands, and a bare argparse flag/subcommand dump answered none of
#: "what do the three verbs do", "how do I point this at data", or "what do
#: I run first", which is exactly what a caller piping the output into
#: `head -30` is looking for and not finding.
_GROUP_HELP: dict[str, str] = {
    "connect": "check a connector's own credentials and capabilities",
    "explore": "make sense of a warehouse: rank objects, profile columns, infer joins",
    "transform": "author and refactor dbt models, tests, and the semantic layer",
    "semantic": "define dbt semantic models and metrics as reviewable diffs",
    "maintain": "detect drift against the last snapshot and propose the fix",
    "viz": "preview the semantic layer (not yet implemented)",
    "demo": "create a seeded local DuckDB warehouse to try dex against "
    "(no credentials, no network)",
}

_EPILOG = """\
Point it at data with --connector/--path, or commit a connector: block to
.dex/config.yml (found by walking up from the working directory).

No warehouse yet: `dex demo` seeds a local one, no credentials needed.
Have one already: `dex explore map` is the command to run first -- it
ranks what matters, profiles it, and infers how the tables join.

`dex <group> --help` lists a group's own subcommands.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dex",
        description=(
            "Explore an unfamiliar warehouse, transform it with reviewable\n"
            "dbt edits, and maintain it as the world drifts."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Real defaults live on the top-level parser so every namespace has them.
    parser.add_argument("--connector", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--scope", action="append", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--dataset", action="append", default=None)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--cache-backend", default=None)
    parser.add_argument("--project-format", default=None)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--budget", type=float, default=None)

    common = _sub_connection_options()
    groups = parser.add_subparsers(dest="group", required=True)
    for group, subcommands in COMMAND_SURFACE.items():
        gp = groups.add_parser(group, parents=[common], help=_GROUP_HELP.get(group))
        if group == "demo":
            # Positional rather than --path: --path names the warehouse dex
            # reads, everywhere, and this is the one command that writes one.
            gp.add_argument("target", nargs="?", default=None)
        if subcommands:
            sub = gp.add_subparsers(dest="subcommand", required=True)
            for name in subcommands:
                sp = sub.add_parser(name, parents=[common])
                if group == "explore" and name == "inventory":
                    sp.add_argument(
                        "--rank", action="store_true", default=argparse.SUPPRESS
                    )
                    # Ranked inventory is capped by default (#289); --limit widens
                    # it, --all lifts the cap entirely. Both are no-ops without
                    # --rank, since the unranked list carries no order to cut from.
                    sp.add_argument("--limit", type=int, default=None)
                    sp.add_argument(
                        "--all", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "explore" and name == "profile":
                    sp.add_argument("objects", nargs="+")
                # Variadic like `profile` above it: an agent asking a chain of
                # small questions should pay one call, not one per question. Zero
                # positionals is legal here rather than an argparse usage error so
                # `--sql-file` can carry the batch instead; the shim refuses an
                # empty call with an envelope, which is the contract.
                if group == "explore" and name == "query":
                    sp.add_argument("sql", nargs="*")
                    sp.add_argument("--sql-file", default=argparse.SUPPRESS)
                if group == "explore" and name == "cluster":
                    sp.add_argument("object")
                # An off switch only. Profiling an object the connection has but
                # the cache cannot speak for is the default, so the flag that
                # needs a spelling is the one that restores the strict
                # prerequisite; `auto_profile: false` in config does it durably.
                if group == "explore" and name in {"query", "cluster"}:
                    sp.add_argument(
                        "--no-auto-profile",
                        action="store_true",
                        default=argparse.SUPPRESS,
                    )
                if group == "explore" and name == "cluster":
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
                # `explore semantic list|query` reads and queries the dbt
                # semantic layer (distinct from the top-level `semantic` group,
                # which authors it). Which layer answers is ambient: the .dex
                # config `semantic.vendor` and `semantic.deployment` (or the
                # released `semantic.backend` spelling of the two), overridable
                # here with --local / --api.
                if group == "explore" and name == "semantic":
                    # Bare `explore semantic` lists (discovery is first-class);
                    # `values` returns one dimension's value domain, and `query`
                    # runs a metric query.
                    sp.add_argument(
                        "mode",
                        nargs="?",
                        choices=["list", "values", "query"],
                        default="list",
                    )
                    # Named metrics mean something in both modes, which is why one
                    # pair of spellings serves both: in `query` they are what to
                    # measure, in `list` they scope the catalog to those metrics
                    # and what they reach. A whole layer's catalog is one payload
                    # and mostly about something else, so a caller that already
                    # knows the metric should not have to read past it.
                    # In `list` and `query` these are metrics; in `values` the
                    # one positional is the dimension, and `--metric` scopes it.
                    sp.add_argument("metrics", nargs="*")
                    sp.add_argument("--metric", action="append", default=None)
                    # The reverse of a metric's dimension list: which metrics can
                    # be grouped by all of these. `list` only, and refused rather
                    # than dropped in the other two modes.
                    sp.add_argument("--for-dimension", action="append", default=None)
                    # A word rather than a name, matched against every element's
                    # own name and the project's words about it. `list` only, and
                    # refused in the other two modes rather than dropped.
                    sp.add_argument("--search", action="append", default=None)
                    # Lifts the catalog's payload caps. Deliberately `--full`
                    # rather than `--detail`: nothing here scans, so the word
                    # carries its usual sense on this surface (stop selecting,
                    # take everything) with no cost attached to it.
                    sp.add_argument("--full", action="store_true", default=False)
                    sp.add_argument("--group-by", action="append", default=None)
                    sp.add_argument("--where", action="append", default=None)
                    sp.add_argument("--order-by", action="append", default=None)
                    sp.add_argument("--grain", default=None)
                    sp.add_argument("--limit", type=int, default=None)
                    sp.add_argument("--local", action="store_true", default=False)
                    sp.add_argument("--api", action="store_true", default=False)
                # `map --full` profiles every object rather than the top-ranked;
                # `diagram --full` draws every eligible object and column rather
                # than the connected-and-profiled default. Same word, same sense
                # (stop selecting, take everything), different subject.
                if group == "explore" and name in {"map", "diagram"}:
                    sp.add_argument(
                        "--full", action="store_true", default=argparse.SUPPRESS
                    )
                # Deliberately not spelled `--full`, which on `map` decides how
                # much gets *scanned* and therefore what the run costs. This one
                # decides only how much of what was found comes back, spends
                # nothing, and lifts none of the payload caps.
                if group == "explore" and name == "map":
                    sp.add_argument(
                        "--detail", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "explore" and name in {"relationships", "map"}:
                    sp.add_argument(
                        "--verify", action="store_true", default=argparse.SUPPRESS
                    )
                    # A column no name-based rule matched still gets a chance:
                    # opt-in and priced like --verify, this sweeps key-shaped
                    # columns for real value containment (issue #220).
                    sp.add_argument(
                        "--infer-by-overlap",
                        action="store_true",
                        default=argparse.SUPPRESS,
                    )
                # A running total or point-in-time snapshot profiles identically
                # to a per-row increment; telling them apart needs a window-
                # function scan over the table, so it is opt-in and priced like
                # --verify rather than part of the always-free base profile.
                if group == "explore" and name == "profile":
                    sp.add_argument(
                        "--check-cumulative",
                        action="store_true",
                        default=argparse.SUPPRESS,
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
                    sp.add_argument(
                        "--in-place",
                        action="store_true",
                        default=False,
                        help="scaffold into the current directory instead of <name>/",
                    )
                if group == "transform" and name == "plan":
                    # The agent-authored edits payload: a JSON file, or - for stdin.
                    sp.add_argument("--edits-file", default=None)
                    sp.add_argument("--scaffold", action="append", default=None)
                    # Tri-state, and the default is the connector's: naming a
                    # row-affecting change is free, measuring one is a scan. So
                    # counting runs unasked only where it bills nothing, and the
                    # flag is how a caller overrides that in either direction.
                    rows = sp.add_mutually_exclusive_group()
                    rows.add_argument(
                        "--attribute-rows",
                        dest="attribute_rows",
                        action="store_true",
                        default=None,
                        help="measure the row-population delta of each change",
                    )
                    rows.add_argument(
                        "--no-attribute-rows",
                        dest="attribute_rows",
                        action="store_false",
                        help="name row-affecting changes without measuring them",
                    )
                if group == "transform" and name == "build":
                    sp.add_argument("--target", default=None)
                    sp.add_argument("--select", default=None)
                if group == "transform" and name == "references":
                    # Variadic like `explore query`: one call answers "where is
                    # each of these used", which is the shape of a rename. `--kind`
                    # narrows; omitting it reports every kind the name is used as,
                    # because a caller usually knows the name and not what the
                    # project calls it.
                    #
                    # `--kind` is validated in the command rather than by argparse
                    # `choices`: a rejected choice exits before an envelope is
                    # built, so a typo would return no JSON line at all, and every
                    # command owes the caller exactly one.
                    sp.add_argument("names", nargs="+")
                    sp.add_argument("--kind", default=None)
                    sp.add_argument(
                        "--full", action="store_true", default=argparse.SUPPRESS
                    )
                if group == "transform" and name in {"rename", "remove"}:
                    # The write half of `references`, so it sits behind the
                    # dialect gate the read half is routed around: these author
                    # SQL and need the engine that parses it.
                    #
                    # `--edits-file` on both propagation verbs, for the same
                    # payload `transform plan` takes. A removal needs it (dex
                    # removes a declaration and refuses while a read survives,
                    # and only the caller knows what a read should become); a
                    # rename accepts it so a related hand-authored change can
                    # ride in the same atomic plan. Not `place` (#344): it takes
                    # no edits_file parameter and never read the flag, so the
                    # parser accepted it as a silent no-op.
                    sp.add_argument("--edits-file", default=None)
                if group == "transform" and name == "rename":
                    sp.add_argument("kind")
                    sp.add_argument("old")
                    sp.add_argument("new")
                if group == "transform" and name == "remove":
                    sp.add_argument("kind")
                    sp.add_argument("name")
                if group == "transform" and name == "place":
                    # The column to define, then where it has to appear and what
                    # computes it. `--targets` repeats and splits on commas, the
                    # spelling `explore semantic query` already uses for its own
                    # lists.
                    sp.add_argument("argument", nargs="?", default=None)
                    sp.add_argument("--targets", action="append", default=None)
                    sp.add_argument("--expr", default=None)
                    # Answer without storing a plan. The proposal is only worth
                    # calling one if a caller can ask for it cheaply and disagree.
                    sp.add_argument("--explain", action="store_true", default=False)
                if group == "transform" and name == "test":
                    # `test` is scaffold-only for now: the model to derive a
                    # unit_tests: skeleton from. No bare `transform test`
                    # mode exists yet, unlike `macro`'s list-when-bare shape.
                    sp.add_argument("--scaffold", default=None)
                if group == "semantic":
                    sp.add_argument("argument", nargs="?", default=None)
                    sp.add_argument("--edits-file", default=None)
                    sp.add_argument("--no-parse", action="store_true", default=False)
                # maintain detectors take an optional object scope (default: whole
                # project); reconcile takes an optional drift class to fix.
                if group == "maintain" and name in {
                    "check",
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


def dispatch(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    """Route one parsed command to its handler and return exactly one envelope.

    Total by construction: the refusals the engine raises rather than returns (an
    unmet confirmation, a mid-run budget exhaustion, an install that cannot parse
    SQL) are transport concerns at this boundary and become envelopes here, so no
    handler has to know about them and every path out of dispatch is an envelope.
    """

    try:
        return _run(args, engine)
    except DialectDependencyError as exc:
        # A missing extra, caught before the command imported anything that needed
        # it, so nothing has been opened or priced. The message names the install.
        return env.error_for(exc)
    except ConfirmationRequiredError as exc:
        return env.needs_confirmation(
            exc.request.data, cost=exc.request.cost, warnings=exc.request.warnings
        )
    except BudgetExhaustedError as exc:
        # Partial completion: an error, but one that reports what the attempt
        # actually cost, because that is what a caller needs to size the re-run.
        return env.error_for(
            exc,
            env.redact(str(exc)),
            data={"spend": exc.spend} if exc.spend else {},
            cost=exc.cost or env.Cost(),
        )
    except CostGuardError as exc:
        # An over-ceiling or no-ceiling refusal. It carries the gate's own cost,
        # so the refusal reports the paradigm it was denominated in and the two
        # numbers the prose names, rather than leaving a caller to parse them
        # back out of the message. Caught here and not left to `main`'s
        # catch-all for the reason this function exists: a refusal that escapes
        # arrives with no cost at all, and an empty cost block on a spend
        # refusal reads as a claim that nothing was going to be spent.
        return env.error_for(exc, env.redact(str(exc)), cost=exc.cost or env.Cost())


def _run(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    # First, and without `ensure_dialect_available`: demo authors no SQL a guard
    # has to clear, and it must stay reachable on the lightest install that can
    # run it, which is the one a first-time user has.
    if args.group == "demo":
        from .demo.commands import cmd_demo

        return cmd_demo(args, engine)

    if args.group == "connect" and args.subcommand == "test":
        from .results import to_envelope

        return to_envelope(engine.connect_test())

    if args.group == "explore":
        # `explore semantic` is routed before the rest of the group and from a
        # different module on purpose: on the hosted backend dbt Cloud renders and
        # executes the SQL, so the command needs no dialect engine, and a
        # pure-remote install ([semantic-api], no connector) must be able to reach
        # it. Importing the module below would pull the query firewall and defeat
        # that. `--local` lands here too: `list` reads the catalog through the
        # project seam and parses no SQL, and a local `query` reaches the dialect
        # engine through MetricFlow's own path.
        if args.subcommand == "semantic":
            from .explore.semantic.commands import cmd_semantic

            return cmd_semantic(args, engine)

        ensure_dialect_available()
        from .explore import commands as explore_cmds

        handlers = {
            "inventory": explore_cmds.cmd_inventory,
            "profile": explore_cmds.cmd_profile,
            "relationships": explore_cmds.cmd_relationships,
            "map": explore_cmds.cmd_map,
            "diagram": explore_cmds.cmd_diagram,
            "query": explore_cmds.cmd_query,
            "cluster": explore_cmds.cmd_cluster,
        }
        return handlers[args.subcommand](args, engine)

    if args.group == "maintain":
        ensure_dialect_available()
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
        ("transform", "rename"): "cmd_rename",
        ("transform", "remove"): "cmd_remove",
        ("transform", "place"): "cmd_place",
        ("transform", "test"): "cmd_test",
        ("semantic", "define"): "cmd_semantic_define",
        ("semantic", "update"): "cmd_semantic_update",
        ("semantic", "plan"): "cmd_semantic_plan",
    }
    # `transform references` is routed before the authoring table and from its
    # own module on purpose, the same trade `explore semantic` makes above. It
    # reads the project's files and nothing else: no warehouse, no dialect
    # engine. Importing the command module below would pull the plan store and
    # sqlglot with it, and a bare install (no connector extra) could not run a
    # command that only ever reads text off disk.
    if args.group == "transform" and args.subcommand == "references":
        from .references import cmd_references

        return cmd_references(args, engine)

    handler = authoring.get((args.group, args.subcommand))
    if handler is not None:
        ensure_dialect_available()
        from .transform import commands as transform_cmds

        return getattr(transform_cmds, handler)(args, engine)

    # Everything else is scaffolded against the contract but not yet built.
    return env.not_implemented(command_args.command_name(args))


def main(argv: list[str] | None = None) -> int:
    from .command_args import repo_root
    from .connect import paradigm_for

    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        # A bare `dex` still has to pick a group (required=True below), but
        # argparse's error for that is "the following arguments are required:
        # group": true, useless to someone who does not know the groups exist
        # yet. Route it to the same orientation --help gives instead.
        parser.print_help()
        return 0
    rewritten = _rewrite_unambiguous_bare_subcommand(raw)
    try:
        args = parser.parse_args(rewritten)
    except SystemExit as exc:
        # argparse already printed usage; add a bare-subcommand suggestion when useful.
        if exc.code not in (0, None):
            # Find first positional token for suggestion.
            token = None
            j = 0
            while j < len(raw) and raw[j].startswith("-"):
                if raw[j] in {
                    "--connector",
                    "--path",
                    "--scope",
                    "--project",
                    "--dataset",
                    "--repo-root",
                    "--cache-backend",
                    "--project-format",
                    "--budget",
                }:
                    j += 2
                    continue
                j += 1
            if j < len(raw):
                token = raw[j]
            tip = _bare_subcommand_suggestion(token) if token else None
            if tip:
                print(f"dex: {tip}", file=sys.stderr)
        raise
    # The connector in play, for envelopes that never priced anything and so
    # never stamped a paradigm of their own. Read off the engine as soon as it
    # exists, because `close()` runs before the handlers below and drops the
    # adapter; a name is enough, and unlike an adapter it survives a connection
    # that could not be opened. Stays None when nothing selected a connector.
    paradigm: env.Paradigm | None = None
    # Read back after dispatch, not just after construction: the run-directory
    # DuckDB auto-detect (issue #199) only fires on the first `_adapter()` call
    # inside the handler, so a read taken right after `from_repo` would still
    # see no connector even on a run that went on to pick one.
    engine: DexEngine | None = None
    # What the command actually billed, captured on the way out for the same
    # reason the paradigm is: `close()` drops the adapter the gate hangs off,
    # and it runs before the handlers below.
    spend: dict | None = None
    # Building the engine is inside the handler, not before it: it reads the
    # config file and constructs the configured storage backend, and both can
    # refuse. Every agent wrapper expects exactly one envelope on stdout, so a
    # refusal raised here has to render as one like any other rather than as a
    # traceback the wrapper cannot parse.
    try:
        # One engine per process, built from the resolved repo root. The default
        # backend is what lets the subcommands stay stateless across invocations:
        # `.dex/` on disk is how the exploration cache and the cumulative session
        # budget survive from one command to the next, and any backend selected
        # here has to be durable across processes for the same reason.
        engine = DexEngine.from_repo(
            repo_root(args),
            cache_backend=getattr(args, "cache_backend", None),
            project_format_name=getattr(args, "project_format", None),
            connector=getattr(args, "connector", None),
            path=getattr(args, "path", None),
            project=getattr(args, "project", None),
            datasets=getattr(args, "dataset", None),
            scopes=getattr(args, "scope", None),
            budget=getattr(args, "budget", None),
            confirmed=getattr(args, "confirm", False),
        )
        try:
            envelope = dispatch(args, engine)
        finally:
            # Unlike an adapter, the paradigm survives a connection that could
            # not be opened, so this still gives a refusal envelope something
            # to name; taken in the `finally` so it reflects whatever the
            # handler resolved, auto-detect included, not just construction.
            paradigm = engine.paradigm
            # Best-effort by design: a ledger that cannot be read must not
            # replace the exception on its way out with a bookkeeping failure,
            # which would lose the reason the command failed to report a
            # number about it.
            with contextlib.suppress(Exception):
                spend = engine.settled_spend()
            engine.close()
    except env.SanitizationError:
        # A sanitization failure must never be swallowed: re-raise so it surfaces
        # loudly in tests and CI rather than shipping a leak.
        raise
    except Exception as exc:
        # An engine that could not be built leaves the flag as the only evidence
        # of a connector, which still beats saying nothing on a refusal a host
        # has to decide whether to retry.
        if paradigm is None and getattr(args, "connector", None):
            paradigm = paradigm_for(args.connector)
        envelope = env.error_for(exc, env.redact(str(exc)))

    # A command that failed still spent what it spent. Only the two-phase
    # refusals used to say so (budget exhaustion carries its own spend), so a
    # statement that died mid-scan on a metered connector reported `data: {}`
    # and read as a failure that cost nothing. Filled only where the handler
    # did not already report it, so the richer partial-completion payload wins.
    if (
        envelope.status is env.Status.ERROR
        and spend is not None
        and "spend" not in envelope.data
    ):
        envelope.data["spend"] = spend

    # A guess the engine made on the caller's behalf (issue #199's run-directory
    # DuckDB auto-detect) must never be silent, so it rides into every envelope
    # this way rather than through whatever warnings a handler happened to
    # collect; an engine that never got this far (construction itself refused)
    # has nothing to carry.
    if engine is not None and engine.connection_warnings:
        envelope.warnings = list(engine.connection_warnings) + list(envelope.warnings)

    # Every command runs against a connector or against none, so every envelope
    # can name the paradigm a later billed command would spend in. Filled only
    # where nothing claimed one, so a deliberate label survives: `explore
    # semantic query --api` sets `hosted` because dbt Cloud owns that spend, and
    # a genuine "no connector resolved" stays null rather than borrowing
    # `free_local`, which is DuckDB's answer and not a way to say nothing.
    if envelope.cost.paradigm is None and paradigm is not None:
        envelope.cost.paradigm = paradigm

    env.emit(envelope)
    return 0 if envelope.status != env.Status.ERROR else 1


if __name__ == "__main__":
    sys.exit(main())
