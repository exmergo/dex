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
``semantic``), and the ``maintain`` group are live.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from . import command_args
from . import envelope as env
from .config import SEMANTIC_DOCUMENT_AUTHORING
from .engine import DexEngine
from .guards.cost_guard import ConfirmationRequiredError, CostGuardError
from .guards.dialect import DialectDependencyError
from .guards.dialect import ensure_available as ensure_dialect_available
from .results import BudgetExhaustedError
from .session_cap import (
    SESSION_CEILING_CAP_ENV,
    SessionCeilingCap,
    parse_session_ceiling_cap,
)
from .values_free import (
    ResultMode,
    call_withholding,
    parse_result_mode,
    require_allowed,
)

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
        # Declarations for models that already exist (#501): opt-in, one capped
        # plan per call, repo-and-cache only.
        "declare",
        "test",
        # Whether a stored plan keeps a model's results (#856).
        "compare",
        # The host boundary (#441): a plan a second process can carry and check,
        # what that plan depends on, what its edits actually contain, and what
        # the warehouse will enforce on the build that validates it. Every one is
        # repo-only and free except `preflight`, which opens no connection either.
        "export",
        "ground",
        "classify",
        "preflight",
    ],
    # `define`/`update`/`plan` author the dbt semantic layer; each vendor whose
    # native documents dex authors gets a subcommand of its own name for them.
    "semantic": ["define", "update", "plan", *SEMANTIC_DOCUMENT_AUTHORING],
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
        # verify: is the project correct right now, no baseline required
        # (#224), unlike every other maintain subcommand above.
        "verify",
    ],
    # config: read, check and change `.dex/config.yml` itself. Routed in `main`
    # before an engine is built, because every engine is built from that file
    # and a file that cannot build one is exactly when these are needed.
    "config": ["get", "validate", "set", "unset"],
    # demo: the on-ramp. No subcommands, because there is exactly one thing to
    # do and it is the first command a new user runs. It is also the only verb
    # that creates a data file, which is why it lives on its own path (see
    # `demo/warehouse.py`) rather than anywhere near a connector.
    "demo": [],
    # procedure: the agent procedure (#502), the one text every SKILL.md and
    # AGENTS.md carries between markers. No subcommands, and routed in `main`
    # before an engine is built, like `config`: an agent with only the package,
    # or a harness with no project yet, must be able to get it.
    "procedure": [],
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
            "--session-ceiling",
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


#: The environment variable a host that wraps the CLI pins values-free mode with
#: (#591). An environment variable rather than config, because the process that
#: launches dex is the host and `.dex/config.yml` is a file the agent may write.
RESULT_MODE_ENV = "DEX_RESULT_MODE"

#: Help for the options every command accepts. One table for both places they
#: are declared (the top-level parser and each subparser's parent), so the two
#: copies of an option cannot describe it differently.
_CONNECTION_OPTION_HELP: dict[str, str] = {
    "--connector": "warehouse to use, overriding connector: in .dex/config.yml",
    "--path": "DuckDB database file to read",
    "--scope": "narrow the committed source allowlist to this namespace; repeatable",
    "--project": "BigQuery project (the older spelling of --scope)",
    "--dataset": "BigQuery dataset, repeatable (the older spelling of --scope)",
    "--repo-root": "directory to resolve .dex/config.yml from, walking up (default: .)",
    "--confirm": "run a billed command after its cost was quoted",
    "--budget": "spend this command may use, in the connector's cost unit",
    "--session-ceiling": (
        "answer the one-time ask: commit a daily ceiling "
        f"({SESSION_CEILING_CAP_ENV} caps)"
    ),
    "--no-session-ceiling": "answer the one-time ask: record that no ceiling applies",
    "--values-free": f"return no table value in any result ({RESULT_MODE_ENV} pins it)",
}


def _add_connection_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Declare the shared options on ``parser``, with real or SUPPRESS defaults."""

    def add(flag: str, default: Any = None, **kwargs: Any) -> None:
        parser.add_argument(
            flag,
            default=argparse.SUPPRESS if suppress else default,
            help=_CONNECTION_OPTION_HELP[flag],
            **kwargs,
        )

    add("--connector", metavar="NAME")
    add("--path", metavar="FILE")
    # The portable source-scope override, repeatable: each connector reads it in
    # its own namespace vocabulary. Nothing is written to config, and a scope may
    # only narrow a committed allowlist, never widen it.
    add("--scope", action="append", metavar="NAMESPACE")
    # BigQuery's older spelling of --scope, kept because `connect test --project X
    # --dataset Y` is how a BigQuery connection is smoke-tested before a
    # .dex/config.yml bigquery block exists. Both error on other connectors.
    add("--project", metavar="PROJECT")
    add("--dataset", action="append", metavar="DATASET")
    add("--repo-root", default=".", metavar="DIR")
    add("--confirm", default=False, action="store_true")
    add("--budget", type=float, metavar="AMOUNT")
    # The two answers to the one-time cumulative-ceiling ask (issue #283). Both
    # write `.dex/config.yml` once and then never matter again, which is why
    # they sit beside `--budget` rather than under a subcommand of their own:
    # the ask fires from a billed command, and the answer belongs on the re-run
    # of that same command.
    add("--session-ceiling", type=float, metavar="AMOUNT")
    add("--no-session-ceiling", default=False, action="store_true")
    # Values-free mode (#591). An on switch only: there is no flag that turns it
    # off, so `DEX_RESULT_MODE=values_free` set by a host cannot be undone by
    # anything an agent adds to the command line.
    add("--values-free", default=False, action="store_true")


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
    _add_connection_options(common, suppress=True)
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
    "semantic": "define dbt or native semantic layers as reviewable diffs",
    "maintain": "detect drift against the last snapshot and propose the fix",
    "config": "read, check and change .dex/config.yml, as reviewable diffs",
    "demo": "create a seeded local DuckDB warehouse to try dex against "
    "(no credentials, no network)",
    "procedure": "print the agent procedure, or the block to paste into "
    "CLAUDE.md / AGENTS.md (no connection)",
}

#: (group, subcommand) -> (summary, description). The summary is the one line
#: `dex <group> --help` lists; the description heads `dex <group> <sub> --help`
#: and names what the command costs. Descriptive, never procedural: when to run
#: what is the skills' job, and the full contract is the command reference.
_SUBCOMMAND_HELP: dict[tuple[str, str], tuple[str, str]] = {
    ("connect", "test"): (
        "check the connection, its capabilities and cost unit",
        "Open the resolved connection read-only and report the dialect, the "
        "capabilities, and the cost paradigm. Free: it runs no billed query.",
    ),
    ("explore", "inventory"): (
        "list warehouse objects with counts and sizes, no rows",
        "Summarize the objects in scope from metadata. --rank orders them by "
        "what matters and caps the list. Free metadata on every connector.",
    ),
    ("explore", "profile"): (
        "profile columns: grain, keys, PII flags, data quality",
        "Profile the named objects from aggregates, never rows: candidate keys, "
        "grain, PII flags and data-quality findings. Billed on metered "
        "connectors, so it quotes the cost first.",
    ),
    ("explore", "relationships"): (
        "infer how tables join, optionally verified",
        "Infer join edges with confidences. --verify measures each join with an "
        "overlap probe, which is billed on metered connectors.",
    ),
    ("explore", "map"): (
        "rank, profile and relate a warehouse into .dex/",
        "Build or refresh the draft map in .dex/: the top-ranked objects with "
        "grain, keys and PII flags, plus the join edges. Billed on metered "
        "connectors, so it quotes the cost first.",
    ),
    ("explore", "diagram"): (
        "draw the cached map as a Mermaid ER diagram",
        "Serialize the .dex/ map as a Mermaid erDiagram. Free and "
        "connectionless: it reads only the cache.",
    ),
    ("explore", "query"): (
        "run guarded SELECTs through the query firewall",
        "Run one or more SELECT statements through the query firewall, which "
        "returns values only from profiled columns cleared of PII. Billed on "
        "metered connectors.",
    ),
    ("explore", "cluster"): (
        "k-means over a sampled scan of numeric columns",
        "Cluster one object's numeric columns from a bounded sample and return "
        "sizes, centroids and silhouette, never rows. Needs the [cluster] "
        "extra; billed on metered connectors.",
    ),
    ("explore", "semantic"): (
        "list, read or query the semantic layer",
        "Discover the semantic layer's metrics, dimensions and entities (list), "
        "read one dimension's values (values), or run a governed metric query "
        "(query). Listing is free; values and query run SQL.",
    ),
    ("transform", "init"): (
        "bootstrap a dbt project skeleton",
        "Propose a new dbt project as create diffs, for the connector named by "
        "--connector or .dex/config.yml. Refuses if a dbt project exists. Free.",
    ),
    ("transform", "plan"): (
        "validate authored dbt edits and store them as a plan",
        "Validate the edits in --edits-file, diff them against the project, and "
        "store a plan. Nothing is written until transform apply. Repo-only.",
    ),
    ("transform", "apply"): (
        "write a stored plan or plan document into the project",
        "Write a plan's diffs into the project. With no id, applies the latest "
        "unapplied plan. A file edited since planning is a conflict, never an "
        "overwrite. Repo-only.",
    ),
    ("transform", "build"): (
        "run dbt build against a dev target",
        "Run dbt build on a dev target only, after a free preflight and a cost "
        "estimate. It runs only with --confirm and --budget.",
    ),
    ("transform", "deps"): (
        "install or refresh dbt packages",
        "Run dbt deps inside the project. Repo-confined, with no warehouse spend.",
    ),
    ("transform", "plans"): (
        "list stored plans, newest first",
        "List the plans in .dex/plans, pending and applied, newest first. Free.",
    ),
    ("transform", "macro"): (
        "list shipped dbt macros, or plan one into the project",
        "With no name, list the dbt macros dex ships. With a name, plan "
        "scaffolding that macro into the project's macro directory. Repo-only.",
    ),
    ("transform", "references"): (
        "find where names are used across the project",
        "Report every use of each name across model SQL, YAML, macros, seeds "
        "and installed packages, and say how complete the answer is. With "
        "--usage, also how other models filter, branch on or aggregate each "
        "model.column. Repo-only and free.",
    ),
    ("transform", "rename"): (
        "plan every edit a rename needs",
        "Generate one plan renaming a column, var, model, seed, snapshot, "
        "macro or source everywhere it is used. Refuses rather than partially "
        "applying. Repo-only and free.",
    ),
    ("transform", "remove"): (
        "plan a definition's removal, refusing while reads remain",
        "Plan the removal of a definition and refuse while any read survives, "
        "naming each one. Pass the read edits with --edits-file. Repo-only.",
    ),
    ("transform", "place"): (
        "plan where a shared derived column should be defined",
        "Find the lowest common model that can define the column for every "
        "target, define it there, and thread it down. --explain stores no "
        "plan. Repo-only and free.",
    ),
    ("transform", "declare"): (
        "propose declarations for models that already exist",
        "Store one plan proposing the measured facts (grain, keys, joins, "
        "running totals) each selected model does not yet declare, plus any "
        "intent the --edits-file declarations supply. Capped at 25 models; "
        "every skipped model is named. Repo-only and free.",
    ),
    ("transform", "test"): (
        "scaffold a unit test, or mutation-test existing tests",
        "--scaffold plans a unit_tests skeleton for a model. --mutate plants "
        "one defect at a time and reports which ones no test caught; "
        "--downstream also counts the tests of the models that read it. "
        "Mutation runs dbt on the dev target and is billed on metered connectors.",
    ),
    ("transform", "compare"): (
        "compare a model before and after a stored plan",
        "Compile the model with and without the plan, then compare output "
        "columns and the exact row multiset on the same dev inputs. Returns "
        "counts, never rows. equivalent holds for the compared inputs only. "
        "Scans go through --confirm --budget on metered connectors.",
    ),
    ("transform", "export"): (
        "export a stored plan as a portable, digested document",
        "Print one stored plan as a document another checkout can verify and "
        "apply, with a digest over the whole plan. Repo-only and free.",
    ),
    ("transform", "ground"): (
        "report what a plan depends on and how complete that is",
        "List the models, sources, columns and relations a plan reads, what "
        "could not be resolved, and a fingerprint of the inputs. Repo-only and "
        "free.",
    ),
    ("transform", "classify"): (
        "classify what each edit's content actually contains",
        "Read each edit's content and label it declarative, executable, "
        "authority-bearing, data or unknown, naming the signal. Repo-only and "
        "free.",
    ),
    ("transform", "preflight"): (
        "report what the warehouse will enforce on the next build",
        "Read the rendered profiles.yml for provider-side controls on the dev "
        "target and say whether any of them binds. Free and connectionless.",
    ),
    ("semantic", "define"): (
        "plan new dbt semantic models or metrics",
        "Validate new semantic models or metrics, including with dbt's own "
        "parser, and store them as a plan. Refuses a removal. Repo-only.",
    ),
    ("semantic", "update"): (
        "plan changes to existing semantic definitions",
        "Validate changed semantic definitions, removals included, and store "
        "them as a plan. Repo-only.",
    ),
    ("semantic", "plan"): (
        "plan a mix of semantic additions, changes and removals",
        "Validate a payload of semantic definitions and classify each as "
        "defined, updated, unchanged or removed, then store a plan. Repo-only.",
    ),
    ("semantic", "ossie"): (
        "plan whole-document edits to a native Apache Ossie layer",
        "Validate whole Ossie documents at the paths semantic.ossie.files "
        "configures, then store a plan. Needs the [ossie] extra. Repo-only.",
    ),
    ("semantic", "slayer"): (
        "plan whole-file edits to the configured SLayer model files",
        "Validate whole SLayer model files at the paths semantic.slayer.models "
        "configures, with SLayer's own model, then store a plan. Needs the "
        "[slayer] extra. Repo-only.",
    ),
    ("semantic", "cube"): (
        "plan whole-file edits to the configured Cube model files",
        "Validate whole plain-YAML Cube model files at the paths "
        "semantic.cube.files configures, with dex's port of Cube's model rules, "
        "then store a plan. Needs the [cube] extra. No Cube server, no "
        "warehouse. Repo-only.",
    ),
    ("maintain", "snapshot"): (
        "capture the known-good baseline in .dex/snapshot.json",
        "Pin the current map and fingerprint the project layers as the "
        "baseline drift is measured against. --project-only opens no "
        "connection.",
    ),
    ("maintain", "check"): (
        "sweep every drift axis against the snapshot",
        "Run every drift axis against the snapshot and rank the findings. The "
        "free axes always run; the scanning axes are offered with an estimate.",
    ),
    ("maintain", "schema"): (
        "detect added, dropped, retyped or renamed columns",
        "Compare structure against the snapshot: columns, tables, nullability, "
        "dangling sources and changed models. Free metadata.",
    ),
    ("maintain", "volume"): (
        "detect row counts that collapsed, emptied or spiked",
        "Compare row counts against the snapshot from free metadata. Objects "
        "with no maintained count, such as views, are named as not compared.",
    ),
    ("maintain", "grain"): (
        "detect lost key uniqueness and changed grain",
        "Re-measure key uniqueness, grain and join fanout, plus declared "
        "grains. Scans the data, so it is billed on metered connectors.",
    ),
    ("maintain", "semantic"): (
        "detect semantic definition drift and dangling references",
        "Report changed, added and removed semantic definitions and references "
        "that no longer resolve, for free. Dimension cardinality change is a "
        "scan, offered with an estimate.",
    ),
    ("maintain", "reconcile"): (
        "propose the edits that reconcile detected drift",
        "Store the reconciling edits as a plan of diffs tagged mechanical or "
        "advisory. Never applied here; use transform apply. Repo-only.",
    ),
    ("maintain", "verify"): (
        "check the project is correct now, with no baseline",
        "Check build status, column contracts, row population, grain and join "
        "contracts against the built relations. Mostly free metadata; view row "
        "counts and join probes are offered with an estimate.",
    ),
    ("config", "get"): (
        "show effective settings and where each came from",
        "Print every setting, or those under a key, with its value and source. "
        "Reads only the file: no connection, no extra needed.",
    ),
    ("config", "validate"): (
        "list every problem in .dex/config.yml",
        "Report syntax errors, refused values, unknown or duplicated keys and "
        "credential-like keys, by setting and line. Free.",
    ),
    ("config", "set"): (
        "change settings as a reviewable in-place edit",
        "Propose the change as a diff that keeps every comment, and write it "
        "with --confirm. Refuses credential-like keys and values. Free.",
    ),
    ("config", "unset"): (
        "remove settings as a reviewable in-place edit",
        "Propose removing keys, including ones dex does not read, as a diff, "
        "and write it with --confirm. Free.",
    ),
}

#: Authoring subcommands that read and report rather than author SQL, so the
#: dialect gate does not apply. `transform references` is routed around the table
#: entirely for the stronger version of the same reason (it must not even import
#: the authoring module); these four can import it, and only need not be refused
#: on an install with no connector extra.
_NEEDS_NO_DIALECT = frozenset(
    {
        ("transform", "export"),
        ("transform", "classify"),
        ("transform", "preflight"),
        # Grounding reads columns through the dialect engine where one is
        # installed and names its absence as a limit where it is not, exactly as
        # `transform references` does, so refusing here would turn a degraded
        # answer into no answer.
        ("transform", "ground"),
    }
)


_EPILOG = """\
Point it at data with --connector/--path, or commit a connector: block to
.dex/config.yml (found by walking up from the working directory).

DBT_PROFILES_DIR only locates dbt profiles.yml for dbt operations and the
last-resort credential fallback; it does not select dex's connector or override
--connector/--path or .dex/config.yml.

No warehouse yet: `dex demo` seeds a local one, no credentials needed.
Have one already: `dex explore map` is the command to run first. It
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
    _add_connection_options(parser, suppress=False)
    # Top-level only: these choose how the engine is built, so they must come
    # before the group rather than after the subcommand.
    parser.add_argument(
        "--cache-backend",
        default=None,
        metavar="NAME",
        help="where .dex/ state is kept for this run; must precede the group",
    )
    parser.add_argument(
        "--project-format",
        default=None,
        metavar="NAME",
        help="transformation project format for this run; must precede the group",
    )

    common = _sub_connection_options()
    groups = parser.add_subparsers(dest="group", required=True)
    for group, subcommands in COMMAND_SURFACE.items():
        gp = groups.add_parser(
            group,
            parents=[common],
            help=_GROUP_HELP.get(group),
            description=_GROUP_HELP.get(group),
        )
        if group == "demo":
            # Positional rather than --path: --path names the warehouse dex
            # reads, everywhere, and this is the one command that writes one.
            gp.add_argument(
                "target",
                nargs="?",
                default=None,
                help="DuckDB file to create, refused if it exists "
                "(default: dex_demo.duckdb)",
            )
        if group == "procedure":
            gp.add_argument(
                "--block",
                action="store_true",
                help="also return data.block, ready to paste into CLAUDE.md or "
                "AGENTS.md",
            )
            gp.add_argument(
                "--check",
                action="append",
                metavar="FILE",
                default=None,
                help="report whether FILE carries the current procedure (repeatable)",
            )
        if subcommands:
            sub = gp.add_subparsers(dest="subcommand", required=True)
            for name in subcommands:
                summary, description = _SUBCOMMAND_HELP[(group, name)]
                sp = sub.add_parser(
                    name, parents=[common], help=summary, description=description
                )
                if group == "config" and name == "get":
                    sp.add_argument(
                        "key",
                        nargs="?",
                        default=None,
                        help="show only the settings under this key",
                    )
                if group == "config" and name == "set":
                    # Pairs rather than one KEY VALUE, because some settings are
                    # only valid together. Counted in the handler, not by
                    # argparse, so a malformed call still gets an envelope.
                    sp.add_argument(
                        "pairs",
                        nargs="*",
                        default=None,
                        metavar="key value",
                        help=(
                            "one or more key and YAML value pairs, applied as one "
                            "change"
                        ),
                    )
                if group == "config" and name == "unset":
                    # Counted in the handler too, so a call naming no key still
                    # gets an envelope.
                    sp.add_argument(
                        "keys",
                        nargs="*",
                        default=None,
                        metavar="key",
                        help=(
                            "setting or block to remove, including keys dex does not "
                            "read"
                        ),
                    )
                    sp.add_argument(
                        "--duplicate",
                        action="store_true",
                        default=False,
                        help="remove only ignored duplicate occurrences of key(s)",
                    )
                if group == "maintain" and name == "snapshot":
                    sp.add_argument(
                        "--project-only",
                        action="store_true",
                        default=False,
                        help=(
                            "refingerprint only the project layers; opens no connection"
                        ),
                    )
                if group == "explore" and name == "inventory":
                    sp.add_argument(
                        "--rank",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="order objects by what matters, capped at 30 by default",
                    )
                    # Ranked inventory is capped by default (#289); --limit widens
                    # it, --all lifts the cap entirely. Both are no-ops without
                    # --rank, since the unranked list carries no order to cut from.
                    sp.add_argument(
                        "--limit",
                        type=int,
                        default=None,
                        metavar="N",
                        help="widen the ranked list to N objects (with --rank)",
                    )
                    sp.add_argument(
                        "--all",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="lift the ranked list's cap (with --rank)",
                    )
                if group == "explore" and name == "profile":
                    sp.add_argument(
                        "objects",
                        nargs="+",
                        metavar="object",
                        help="table or view to profile, as the connector qualifies it",
                    )
                # Variadic like `profile` above it: an agent asking a chain of
                # small questions should pay one call, not one per question. Zero
                # positionals is legal here rather than an argparse usage error so
                # `--sql-file` can carry the batch instead; the shim refuses an
                # empty call with an envelope, which is the contract.
                if group == "explore" and name == "query":
                    sp.add_argument(
                        "sql",
                        nargs="*",
                        metavar="statement",
                        help=(
                            "one SELECT per argument; each is checked and run on its "
                            "own"
                        ),
                    )
                    sp.add_argument(
                        "--sql-file",
                        default=argparse.SUPPRESS,
                        metavar="FILE",
                        help="read statements from a file, one per line or ;-separated",
                    )
                    # One probe across mirrored datasets (#545): each statement
                    # names its dataset as `{ds}` and runs once per target, in
                    # one handshake. Comma-separated and repeatable, like
                    # `transform place --targets`.
                    sp.add_argument(
                        "--across",
                        action="append",
                        default=argparse.SUPPRESS,
                        metavar="DATASETS",
                        help=(
                            "run each statement once per dataset, substituted for {ds}"
                        ),
                    )
                if group == "explore" and name == "cluster":
                    sp.add_argument("object", help="table or view to cluster")
                # An off switch only. Profiling an object the connection has but
                # the cache cannot speak for is the default, so the flag that
                # needs a spelling is the one that restores the strict
                # prerequisite; `auto_profile: false` in config does it durably.
                if group == "explore" and name in {"query", "cluster"}:
                    sp.add_argument(
                        "--no-auto-profile",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help=(
                            "refuse unprofiled objects instead of profiling them first"
                        ),
                    )
                if group == "explore" and name == "cluster":
                    sp.add_argument(
                        "--features",
                        action="append",
                        default=argparse.SUPPRESS,
                        metavar="COLUMNS",
                        help="numeric columns to cluster on, comma-separated",
                    )
                    sp.add_argument(
                        "-k",
                        "--clusters",
                        dest="k",
                        type=int,
                        default=argparse.SUPPRESS,
                        metavar="N",
                        help="number of clusters (chosen automatically when omitted)",
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
                        help="list the catalog (default), read values, or run a query",
                    )
                    # Named metrics mean something in both modes, which is why one
                    # pair of spellings serves both: in `query` they are what to
                    # measure, in `list` they scope the catalog to those metrics
                    # and what they reach. A whole layer's catalog is one payload
                    # and mostly about something else, so a caller that already
                    # knows the metric should not have to read past it.
                    # In `list` and `query` these are metrics; in `values` the
                    # one positional is the dimension, and `--metric` scopes it.
                    sp.add_argument(
                        "metrics",
                        nargs="*",
                        metavar="name",
                        help=(
                            "metrics, comma-separated; in values mode, the one "
                            "dimension"
                        ),
                    )
                    sp.add_argument(
                        "--metric",
                        action="append",
                        default=None,
                        metavar="METRICS",
                        help=(
                            "metrics to query or scope to, comma-separated; repeatable"
                        ),
                    )
                    # The reverse of a metric's dimension list: which metrics can
                    # be grouped by all of these. `list` only, and refused rather
                    # than dropped in the other two modes.
                    sp.add_argument(
                        "--for-dimension",
                        action="append",
                        default=None,
                        metavar="DIMENSIONS",
                        help="list mode: metrics groupable by all these dimensions",
                    )
                    # A word rather than a name, matched against every element's
                    # own name and the project's words about it. `list` only, and
                    # refused in the other two modes rather than dropped.
                    sp.add_argument(
                        "--search",
                        action="append",
                        default=None,
                        metavar="TERM",
                        help="list mode: match names, labels and descriptions",
                    )
                    # Lifts the catalog's payload caps. Deliberately `--full`
                    # rather than `--detail`: nothing here scans, so the word
                    # carries its usual sense on this surface (stop selecting,
                    # take everything) with no cost attached to it.
                    sp.add_argument(
                        "--full",
                        action="store_true",
                        default=False,
                        help="list mode: lift the catalog's payload caps",
                    )
                    sp.add_argument(
                        "--group-by",
                        action="append",
                        default=None,
                        metavar="DIMENSIONS",
                        help="query mode: dimensions to group by, comma-separated",
                    )
                    sp.add_argument(
                        "--where",
                        action="append",
                        default=None,
                        metavar="FILTER",
                        help="query mode: a filter in the layer's own dialect",
                    )
                    sp.add_argument(
                        "--order-by",
                        action="append",
                        default=None,
                        metavar="COLUMNS",
                        help="query mode: columns to order by, comma-separated",
                    )
                    sp.add_argument(
                        "--grain",
                        default=None,
                        help="query mode: time grain, checked against the layer",
                    )
                    sp.add_argument(
                        "--limit",
                        type=int,
                        default=None,
                        metavar="N",
                        help="query mode: return at most N rows",
                    )
                    sp.add_argument(
                        "--local",
                        action="store_true",
                        default=False,
                        help="dex renders and runs the SQL, under the cost guard",
                    )
                    sp.add_argument(
                        "--api",
                        action="store_true",
                        default=False,
                        help="dbt Cloud runs the query; no cost guard applies",
                    )
                # `map --full` profiles every object rather than the top-ranked;
                # `diagram --full` draws every eligible object and column rather
                # than the connected-and-profiled default. Same word, same sense
                # (stop selecting, take everything), different subject.
                if group == "explore" and name in {"map", "diagram"}:
                    sp.add_argument(
                        "--full",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help=(
                            "profile every object, not only the top-ranked (billed)"
                            if name == "map"
                            else "draw every eligible object and column"
                        ),
                    )
                # Deliberately not spelled `--full`, which on `map` decides how
                # much gets *scanned* and therefore what the run costs. This one
                # decides only how much of what was found comes back, spends
                # nothing, and lifts none of the payload caps.
                if group == "explore" and name == "map":
                    sp.add_argument(
                        "--detail",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help=(
                            "return every column and unprofiled object; scans "
                            "nothing more"
                        ),
                    )
                if group == "explore" and name in {"relationships", "map"}:
                    sp.add_argument(
                        "--verify",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="measure each join with an overlap probe (billed)",
                    )
                    # A column no name-based rule matched still gets a chance:
                    # opt-in and priced like --verify, this sweeps key-shaped
                    # columns for real value containment (issue #220).
                    sp.add_argument(
                        "--infer-by-overlap",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help=(
                            "also find joins by value overlap of key-shaped columns "
                            "(billed)"
                        ),
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
                        help=(
                            "detect running totals and snapshots with a window scan "
                            "(billed)"
                        ),
                    )
                    # A model whose only build is the dev one lives where dex
                    # does not read (#893); this reads it for one command, as
                    # `maintain verify --target dev` does.
                    sp.add_argument(
                        "--target",
                        default=None,
                        help=(
                            "also read this dbt dev target's namespace; "
                            "prod-looking targets are refused"
                        ),
                    )
                    # Default summarizes each dataset's columns to the ones that
                    # carry a finding (#288); --columns all restores the full list.
                    sp.add_argument(
                        "--columns",
                        choices=["all"],
                        default=argparse.SUPPRESS,
                        help="return every column, not only those with a finding",
                    )
                # Force a full re-profile even when the cache holds a fresh,
                # schema-matching profile for a requested object (the default is
                # skip-if-cached; --refresh is the escape hatch when the source
                # changed in a way the cheap metadata check cannot see).
                if group == "explore" and name in {"profile", "map", "relationships"}:
                    sp.add_argument(
                        "--refresh",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="re-profile even when the cached profile is fresh",
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
                        help=(
                            "fold in the project's declared joins, grain and semantics"
                        ),
                    )
                if group == "explore" and name in {"relationships", "map"}:
                    sp.add_argument(
                        "--use-hosted-semantic-layer",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="also read the configured hosted semantic layer's catalog",
                    )
                # transform init takes the project name; plan the intent; apply
                # the plan id; macro the shipped-macro name (none lists them).
                # One `argument` dest across these, so the handlers read one
                # attribute; the metavar names what each one actually takes.
                transform_positional = {
                    "init": ("name", "name of the dbt project to create"),
                    "plan": ("intent", "one line saying what the edits are for"),
                    "apply": ("plan-id", "plan to apply (default: latest unapplied)"),
                    "macro": ("macro", "shipped macro to scaffold (omit to list them)"),
                    "export": ("plan-id", "plan to export (default: latest unapplied)"),
                    "ground": ("plan-id", "plan to ground (default: latest unapplied)"),
                    "classify": (
                        "plan-id",
                        "plan to classify (default: latest unapplied)",
                    ),
                }
                if group == "transform" and name in transform_positional:
                    metavar, positional_help = transform_positional[name]
                    sp.add_argument(
                        "argument",
                        nargs="?",
                        default=None,
                        metavar=metavar,
                        help=positional_help,
                    )
                if group == "transform" and name == "apply":
                    # The offline half of the lifecycle: apply a plan document
                    # this checkout's store has never seen. `--expect-digest` is
                    # where authenticity enters, and it is separate from the file
                    # on purpose, because the point is that it arrives by a
                    # different route than the document does.
                    sp.add_argument(
                        "--plan-file",
                        default=None,
                        metavar="FILE",
                        help="apply an exported plan document instead of a stored plan",
                    )
                    sp.add_argument(
                        "--expect-digest",
                        default=None,
                        metavar="HEX",
                        help="refuse a plan document whose digest is not this one",
                    )
                if group == "transform" and name == "classify":
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="classify an edits payload as JSON; '-' reads stdin",
                    )
                if group == "transform" and name == "preflight":
                    sp.add_argument(
                        "--target",
                        default=None,
                        help=(
                            "profiles.yml target to read (default: the configured "
                            "dev target)"
                        ),
                    )
                if group == "transform" and name == "init":
                    sp.add_argument(
                        "--layered-schemas",
                        action="store_true",
                        default=False,
                        help="build each layer into its own schema",
                    )
                    sp.add_argument(
                        "--in-place",
                        action="store_true",
                        default=False,
                        help="scaffold into the current directory instead of <name>/",
                    )
                if group == "transform" and name == "plan":
                    # The agent-authored edits payload: a JSON file, or - for stdin.
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="the edits payload as JSON; '-' reads stdin",
                    )
                    sp.add_argument(
                        "--scaffold",
                        action="append",
                        default=None,
                        metavar="TABLE",
                        help="add a staging model skeleton for a profiled table",
                    )
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
                    sp.add_argument(
                        "--target",
                        default=None,
                        help="dbt target to build; prod-looking targets are refused",
                    )
                    sp.add_argument(
                        "--select",
                        default=None,
                        metavar="SELECTOR",
                        help="dbt node selection, passed through to dbt",
                    )
                    # Opt-in on every connector, free ones included. Verifying
                    # on DuckDB costs nothing, so defaulting it on there was
                    # available; one flag meaning one thing everywhere is worth
                    # more than saving the flag on one connector.
                    sp.add_argument(
                        "--verify",
                        action="store_true",
                        default=False,
                        help="run the maintain verify sweep over the built nodes",
                    )
                    # Which change this build is meant to validate, so the
                    # evidence can report coverage rather than leaving a caller to
                    # infer it from node names.
                    sp.add_argument(
                        "--for-plan",
                        default=None,
                        metavar="PLAN-ID",
                        help="report whether the build covered this stored plan",
                    )
                    # The same change, as the document a sandbox holds instead of
                    # the store id the authoring process holds.
                    sp.add_argument(
                        "--for-plan-file",
                        default=None,
                        metavar="FILE",
                        help="report coverage of an exported plan document",
                    )
                    # An off switch only, like `--no-auto-profile`: installing
                    # missing packages is what an interactive build should do, and
                    # the spelling that needs a flag is the sandbox's refusal.
                    sp.add_argument(
                        "--no-install-deps",
                        action="store_true",
                        default=False,
                        help="refuse missing dbt packages instead of installing them",
                    )
                    # dbt's own flag, for the one case an incremental build
                    # cannot handle: its logic changed, and the rows the old
                    # logic wrote stay until the model is rebuilt (#1015).
                    sp.add_argument(
                        "--full-refresh",
                        action="store_true",
                        default=False,
                        help="rebuild selected incremental models after their "
                        "logic changed",
                    )
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
                    sp.add_argument(
                        "names",
                        nargs="+",
                        metavar="name",
                        help="name to look up; model.column resolves through ref()",
                    )
                    sp.add_argument(
                        "--kind",
                        default=None,
                        help="narrow to one kind, such as model, column, var or metric",
                    )
                    sp.add_argument(
                        "--full",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="lift the cap of 200 occurrences across 50 files",
                    )
                    sp.add_argument(
                        "--usage",
                        action="store_true",
                        default=False,
                        help="also report how other models use each model.column",
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
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="hand-authored edits to store in the same plan",
                    )
                kind_help = "column, var, model, seed, snapshot, macro or source"
                if group == "transform" and name == "rename":
                    sp.add_argument("kind", help=kind_help)
                    sp.add_argument(
                        "old", help="current name; a column is named model.column"
                    )
                    sp.add_argument("new", help="new name")
                if group == "transform" and name == "remove":
                    sp.add_argument("kind", help=kind_help)
                    sp.add_argument(
                        "name", help="name to remove; a column is named model.column"
                    )
                if group == "transform" and name == "place":
                    # The column to define, then where it has to appear and what
                    # computes it. `--targets` repeats and splits on commas, the
                    # spelling `explore semantic query` already uses for its own
                    # lists.
                    sp.add_argument(
                        "argument",
                        nargs="?",
                        default=None,
                        metavar="column",
                        help="name of the derived column to define",
                    )
                    sp.add_argument(
                        "--targets",
                        action="append",
                        default=None,
                        metavar="MODELS",
                        help="models that need the column, comma-separated",
                    )
                    sp.add_argument(
                        "--expr",
                        default=None,
                        metavar="SQL",
                        help="SQL expression that computes the column",
                    )
                    # Answer without storing a plan. The proposal is only worth
                    # calling one if a caller can ask for it cheaply and disagree.
                    sp.add_argument(
                        "--explain",
                        action="store_true",
                        default=False,
                        help="return the reasoning without storing a plan",
                    )
                if group == "transform" and name == "declare":
                    # The selector `maintain verify` takes: model names, repeated
                    # or comma-separated; none selects every model.
                    sp.add_argument(
                        "objects",
                        nargs="*",
                        metavar="model",
                        help="model to declare (default: every model in the project)",
                    )
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="payload whose `declarations` supply intent for the plan",
                    )
                    # Only ever narrows, like --max-mutants: the ceiling is what
                    # keeps a project-wide selector a diff somebody can review.
                    sp.add_argument(
                        "--limit",
                        type=int,
                        default=None,
                        metavar="N",
                        help="propose at most N models (can only lower the cap of 25)",
                    )
                    sp.add_argument(
                        "--intent",
                        default=None,
                        help="one line saying what the plan is for",
                    )
                if group == "transform" and name == "compare":
                    sp.add_argument(
                        "plan_id",
                        nargs="?",
                        default=None,
                        help="stored plan id; defaults to the latest unapplied plan",
                    )
                    sp.add_argument(
                        "--model",
                        default=None,
                        metavar="MODEL",
                        help="the model whose results should not change",
                    )
                    sp.add_argument(
                        "--map",
                        action="append",
                        default=None,
                        metavar="OLD=NEW",
                        help="a baseline output column the plan renames (repeatable)",
                    )
                    sp.add_argument(
                        "--key",
                        action="append",
                        default=None,
                        metavar="COLUMN",
                        help="baseline key column, repeatable; default: declared grain",
                    )
                    sp.add_argument(
                        "--accept-type",
                        action="append",
                        default=None,
                        metavar="COLUMN",
                        help="baseline column whose type may change in its family",
                    )
                    sp.add_argument(
                        "--target",
                        default=None,
                        help="dbt dev target to read; prod-looking targets are refused",
                    )
                if group == "transform" and name == "test":
                    # Two modes, and they are opposites: `--scaffold` writes a
                    # unit test, `--mutate` measures the tests that already
                    # exist. Mutually exclusive rather than ordered, because
                    # asking for both in one call names no coherent outcome. No
                    # bare `transform test` mode exists, unlike `macro`'s
                    # list-when-bare shape.
                    mode = sp.add_mutually_exclusive_group()
                    mode.add_argument(
                        "--scaffold",
                        default=None,
                        metavar="MODEL",
                        help="plan a unit test skeleton for this model",
                    )
                    mode.add_argument(
                        "--mutate",
                        default=None,
                        metavar="MODEL",
                        help="measure which planted defects this model's tests miss",
                    )
                    # Only ever narrows: the engine ceiling is what keeps a run
                    # that invokes dbt once per mutant predictable.
                    sp.add_argument(
                        "--max-mutants",
                        type=int,
                        default=None,
                        metavar="N",
                        help="run at most N mutants (can only lower the cap of 20)",
                    )
                    sp.add_argument(
                        "--target",
                        default=None,
                        help="dbt dev target to mutate against",
                    )
                    # Tri-state, and the default is the connector's, as with
                    # --attribute-rows: comparing a survivor with the model on
                    # the dev data is free on DuckDB and a scan elsewhere, so it
                    # runs unasked only where it bills nothing.
                    equivalence = sp.add_mutually_exclusive_group()
                    equivalence.add_argument(
                        "--check-equivalence",
                        dest="check_equivalence",
                        action="store_true",
                        default=None,
                        help="compare each survivor with the model on the dev data",
                    )
                    equivalence.add_argument(
                        "--no-check-equivalence",
                        dest="check_equivalence",
                        action="store_false",
                        help="report survivors without comparing them",
                    )
                    # Tri-state for the same reason: routing each mutant
                    # through the models that read it recomputes the chain in
                    # every one of their tests, which is free only on DuckDB.
                    reach = sp.add_mutually_exclusive_group()
                    reach.add_argument(
                        "--downstream",
                        dest="downstream",
                        action="store_true",
                        default=None,
                        help="also run the tests of the models downstream of it",
                    )
                    reach.add_argument(
                        "--no-downstream",
                        dest="downstream",
                        action="store_false",
                        help="run only the model's own tests",
                    )
                if group == "semantic" and name in {"define", "update", "plan"}:
                    sp.add_argument(
                        "argument",
                        nargs="?",
                        default=None,
                        metavar="intent",
                        help="one line saying what the change is for",
                    )
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="whole-file edits payload as JSON; '-' reads stdin",
                    )
                    # The per-definition payload: name only what changes, and the
                    # engine writes it into the file that holds it. An entry's `op`
                    # removes one instead, which is the only way a definition goes
                    # away: never by going unmentioned.
                    sp.add_argument(
                        "--definitions-file",
                        default=None,
                        metavar="FILE",
                        help="definitions written in place, as JSON; '-' reads stdin",
                    )
                    sp.add_argument(
                        "--no-parse",
                        action="store_true",
                        default=False,
                        help="skip validation with dbt's own parser",
                    )
                if group == "semantic" and name in SEMANTIC_DOCUMENT_AUTHORING:
                    # Whole documents in, so no `--definitions-file`: a native
                    # document is authored as a unit, and the mode the dbt
                    # routes carry in their subcommand name is an argument here.
                    sp.add_argument(
                        "mode",
                        nargs="?",
                        choices=["define", "update", "plan"],
                        default=None,
                        help=(
                            "define new documents, update existing ones, or plan a mix"
                        ),
                    )
                    sp.add_argument(
                        "argument",
                        nargs="?",
                        default=None,
                        metavar="intent",
                        help="one line saying what the change is for",
                    )
                    sp.add_argument(
                        "--edits-file",
                        default=None,
                        metavar="FILE",
                        help="whole-document edits payload as JSON; '-' reads stdin",
                    )
                # maintain detectors take an optional object scope (default: whole
                # project); reconcile takes an optional drift class to fix.
                if group == "maintain" and name in {
                    "check",
                    "schema",
                    "volume",
                    "grain",
                    "semantic",
                    "verify",
                }:
                    sp.add_argument(
                        "objects",
                        nargs="*",
                        metavar="object",
                        help=(
                            "dbt selector to verify (default: the whole project)"
                            if name == "verify"
                            else "object to check (default: the whole project)"
                        ),
                    )
                if group == "maintain" and name == "verify":
                    sp.add_argument(
                        "--target",
                        default=None,
                        help=(
                            "dbt dev target to verify against; "
                            "prod-looking targets are refused"
                        ),
                    )
                if group == "maintain" and name == "reconcile":
                    sp.add_argument(
                        "drift_class",
                        nargs="?",
                        choices=["schema", "volume", "grain", "semantic"],
                        default=None,
                        help="reconcile only this drift class (default: all)",
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
        # In values-free mode every error leaving a command is held to it here,
        # before the handlers below turn it into an envelope (#591).
        mode = getattr(engine, "result_mode", ResultMode.FULL)
        return call_withholding(mode, _run_allowed, args, engine)
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


def _run_allowed(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    mode = getattr(engine, "result_mode", ResultMode.FULL)
    if mode is ResultMode.VALUES_FREE:
        require_allowed(mode, _command_label(args))
    return _run(args, engine)


def _command_label(args: argparse.Namespace) -> str:
    """The command as the values-free allowlist spells it: ``explore profile``,
    and ``explore semantic list`` for the semantic modes (bare means list)."""

    label = f"{args.group} {getattr(args, 'subcommand', '')}".strip()
    if args.group == "explore" and getattr(args, "subcommand", None) == "semantic":
        label += f" {getattr(args, 'mode', None) or 'list'}"
    return label


def _result_mode(args: argparse.Namespace) -> ResultMode:
    """The mode this process runs in: the flag, or the host's pin, whichever asks
    for values-free. Neither can ask for less than the other, and an unknown
    value in the environment refuses rather than reading as full."""

    pinned = parse_result_mode(os.environ.get(RESULT_MODE_ENV) or None)
    if getattr(args, "values_free", False) or pinned is ResultMode.VALUES_FREE:
        return ResultMode.VALUES_FREE
    return ResultMode.FULL


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
        # Project-only snapshot refreshes use only the store and project seam;
        # requiring a warehouse dialect here would make a zero-connection path
        # depend on the very connector it deliberately does not touch.
        if not (args.subcommand == "snapshot" and getattr(args, "project_only", False)):
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
            "verify": maintain_cmds.cmd_verify,
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
        ("transform", "declare"): "cmd_declare",
        ("transform", "test"): "cmd_test",
        ("transform", "compare"): "cmd_compare",
        ("transform", "export"): "cmd_export",
        ("transform", "ground"): "cmd_ground",
        ("transform", "classify"): "cmd_classify",
        ("transform", "preflight"): "cmd_preflight",
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

    # Native semantic authoring is routed around the table for the same trade,
    # and without `ensure_dialect_available`: it authors whole documents through
    # the vendor's own reader, so structure and integrity need only that
    # vendor's extra. Parsing a SQL expression inside a document is optional and
    # names its own skip when the dialect engine is absent, which is a weaker
    # floor than the dbt authoring routes below can accept.
    if args.group == "semantic" and args.subcommand in SEMANTIC_DOCUMENT_AUTHORING:
        from .transform.native_semantic import cmd_semantic_native

        return cmd_semantic_native(args, engine)

    # `transform apply` writes bytes a plan already validated, so what it needs
    # depends on the plan rather than on the verb. A semantic-document plan
    # authors no SQL and reaches no dialect engine; gating it on sqlglot anyway
    # would let an install that carries only a semantic reader author a plan it
    # could never apply, which is the one command that install exists to run.
    # The stored plan says which it is, so ask it and gate only what needs it.
    if args.group == "transform" and args.subcommand == "apply":
        # A plan document carries its own edits, so there is no stored plan to
        # ask and, more to the point, nothing on this path parses SQL at all:
        # gating it on the dialect engine would make the offline apply need a
        # dependency it never uses, in the one environment least able to install
        # one.
        if getattr(args, "plan_file", None):
            from .transform.commands import cmd_apply_document

            return cmd_apply_document(args, engine)
        if _apply_authors_sql(args, engine):
            ensure_dialect_available()
        from .transform.commands import cmd_apply

        return cmd_apply(args, engine)

    handler = authoring.get((args.group, args.subcommand))
    if handler is not None:
        if (args.group, args.subcommand) not in _NEEDS_NO_DIALECT:
            ensure_dialect_available()
        from .transform import commands as transform_cmds

        return getattr(transform_cmds, handler)(args, engine)

    # Everything else is scaffolded against the contract but not yet built.
    return env.not_implemented(command_args.command_name(args))


def _apply_authors_sql(args: argparse.Namespace, engine: Any) -> bool:
    """Whether the plan this apply would write reaches the dialect engine.

    Fails toward gating. Every reason this cannot answer (no store, no plan, an
    unreadable one) is a reason the apply is about to refuse anyway, and it
    should refuse with the message it always did rather than with a new one from
    a route that was only trying to decide whether to check a dependency.
    """

    try:
        store = engine.require_full_store("applying a plan")
        plan_id = getattr(args, "argument", None)
        if not plan_id:
            latest = store.latest_plan(None)
            if latest is None:
                return True
            plan_id = latest.plan_id
        return store.load_plan(plan_id).edit_target != "semantic"
    except Exception:
        return True


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
                    "--session-ceiling",
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
    connection = env.Connection()
    # Resolved before anything can refuse, so even a refusal states the mode. An
    # unknown DEX_RESULT_MODE is itself a refusal, rendered below like any other.
    result_mode: ResultMode | None = None
    # The host's cap on the daily ceiling (#787), resolved alongside the mode for
    # the same reason: a refusal should still say it, and a malformed one is
    # itself a refusal.
    session_cap: SessionCeilingCap | None = None
    # Building the engine is inside the handler, not before it: it reads the
    # config file and constructs the configured storage backend, and both can
    # refuse. Every agent wrapper expects exactly one envelope on stdout, so a
    # refusal raised here has to render as one like any other rather than as a
    # traceback the wrapper cannot parse.
    try:
        result_mode = _result_mode(args)
        session_cap = parse_session_ceiling_cap(
            os.environ.get(SESSION_CEILING_CAP_ENV), source="environment"
        )
        if args.group == "config":
            # No engine, on purpose: see `config_commands`. With none, nothing
            # below has a paradigm, a spend, or a connection to stamp, which is
            # the truth for a verb that opens nothing.
            from .config_commands import cmd_config

            require_allowed(result_mode, _command_label(args))
            envelope = cmd_config(args)
        elif args.group == "procedure":
            # No engine either: the procedure is package data, and it must reach
            # an agent before any project, config or connector exists (#502).
            from .procedure.commands import cmd_procedure

            require_allowed(result_mode, _command_label(args))
            envelope = cmd_procedure(args)
        else:
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
                session_ceiling=getattr(args, "session_ceiling", None),
                decline_session_ceiling=getattr(args, "no_session_ceiling", False),
                result_mode=result_mode,
                session_ceiling_cap=session_cap,
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
                connection = engine.connection_provenance()
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
        envelope.warnings = [
            w for w in engine.connection_warnings if w not in envelope.warnings
        ] + list(envelope.warnings)

    # What the config file says that no command refuses (a key the schema does
    # not know, a key written twice) rides out on every envelope the same way,
    # since a typo in a setting is otherwise a setting that silently does
    # nothing. The config verbs carry their own copy and build no engine.
    if engine is not None and engine.config is not None:
        envelope.warnings = list(engine.config.load_warnings) + list(envelope.warnings)

    # A project that pins its own dex-core beside the one running here shares
    # `.dex/` with it, and nothing else would say so (#731). Read from the
    # project's own files with no connection, so it rides every envelope, the
    # config verbs included; best-effort, since a pin dex cannot read is not a
    # reason to lose the command's answer.
    with contextlib.suppress(Exception):
        from .engine_version import pin_warning

        pin = pin_warning(Path(repo_root(args)))
        if pin is not None:
            envelope.warnings = [pin, *envelope.warnings]

    # A config amendment dex performed on the caller's behalf (the answer to the
    # cumulative-ceiling ask, issue #283) rides out the same way, as a diff:
    # the write happened before the command did, so even a command that failed
    # afterwards has to report it or the file changed with nothing saying so.
    if engine is not None and engine.config_diffs:
        envelope.diffs = [
            d for d in engine.config_diffs if d not in envelope.diffs
        ] + list(envelope.diffs)
    # And what a host's cap made of that amendment (#787): written, but moot.
    if engine is not None and engine.budget_warnings:
        envelope.warnings = [
            w for w in engine.budget_warnings if w not in envelope.warnings
        ] + list(envelope.warnings)

    # Every command runs against a connector or against none, so every envelope
    # can name the paradigm a later billed command would spend in. Filled only
    # where nothing claimed one, so a deliberate label survives: `explore
    # semantic query --api` sets `hosted` because dbt Cloud owns that spend, and
    # a genuine "no connector resolved" stays null rather than borrowing
    # `free_local`, which is DuckDB's answer and not a way to say nothing.
    if envelope.cost.paradigm is None and paradigm is not None:
        envelope.cost.paradigm = paradigm

    # The connection identity is stamped centrally, just like cost paradigm:
    # handlers cannot drift, and filling it performs no extra warehouse read.
    envelope.connection = connection

    # Every envelope a values-free process prints says so, refusals and asks
    # included, so a host can assert the mode on whatever comes back (#591).
    if result_mode is ResultMode.VALUES_FREE:
        envelope.data["result_mode"] = result_mode.value
    # And every envelope a capped process prints names the cap (#787), for the
    # same reason: a host asserts the bound on whatever comes back.
    if session_cap is not None:
        envelope.data["session_ceiling_cap"] = session_cap.payload(envelope.cost.unit)

    env.emit(envelope)
    return 0 if envelope.status != env.Status.ERROR else 1


if __name__ == "__main__":
    sys.exit(main())
