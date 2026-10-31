#!/usr/bin/env python3
"""Build the Dex section of the documentation site from the repository.

Usage:
    uv run --project packages/dex-core python scripts/build_docs_site.py \
        [--out DIR] [--release vX.Y.Z] [--require-help] [--site DOCS_CHECKOUT] \
        [--latest vX.Y.Z] [--archive vX.Y.Z=DIR ...]

docs.exmergo.com is served from exmergo/docs, which owns the site's settings and
home page; each product publishes one folder of it. This script builds the Dex
folder, `dex/`, served at docs.exmergo.com/dex: every page, plus the menu
(`dex/navigation.json`) and redirects (`dex/redirects.json`) that exmergo/docs's
`docs.json` reads with `$ref`. With `--site`, it writes the whole site instead:
that exmergo/docs checkout with this section in place of its `dex/`, which is
what CI validates and `mint dev` previews.

It reads four sources and writes nothing back into the repository:

  - `docs/`: hand-written pages, and the section's navigation and redirects
    (`section.json`).
  - `references/`: the canonical reference documents, each published at the
    route the mapping below gives it.
  - the CLI parser: one reference page per command group, rendered from the
    same argparse tree `dex --help` prints.
  - `CHANGELOG.md`: the most recent releases.

With `--archive`, older minor releases stay readable beside the latest one, in a
version switcher: latest at the routes it has always had, and each archive under
its label (`/dex/v1.14/...`). An archive is the `dex/` folder that release's own
script built from its own tag, so it shows exactly what was published for it.

`references/` stays the one place a fact changes. The pages it publishes are
transformed copies (MDX escaping, links rewritten to site routes, the H1 lifted
into frontmatter), which is why they are built rather than committed: a second
committed copy would double every reference diff and trip the documentation
policy tests, which allow the PII threshold and the auto-profile pricing
paragraph in exactly one file each.

Every problem is collected before anything is reported, then printed sorted as
`path:line: message`. Exit 0 when clean, 1 when there are problems, 2 on a
usage error.

Anchors are checked against GitHub's heading IDs, because a reference document
is read on GitHub too and a link must work there. Mintlify derives heading IDs
differently for a few headings (a `/` or an apostrophe in the text, and
repeated headings), so CI also runs `mint broken-links --check-anchors` on the
output; an anchor has to resolve on both hosts. On Windows that command reports
every link to a page two folders deep (`/dex/connectors/bigquery`) as broken: it
compares a link's `/`-separated folder with the `\\`-separated one Windows gives
it. Trust its result on Linux, which is what CI runs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "dex-core" / "src"))

REPOSITORY = "exmergo/dex"
GITHUB = f"https://github.com/{REPOSITORY}"
SITE = "https://docs.exmergo.com"
# The site is shared: exmergo/docs owns docs.exmergo.com and its settings, and
# each product repository publishes one section of it. This repository's section
# is the dex/ folder, served at docs.exmergo.com/dex.
SECTION = "dex"
SECTION_CONFIG = "section.json"
DESCRIPTION_LENGTH = range(140, 161)
CHANGELOG_RELEASES = 10
DEFAULT_OUT = REPO_ROOT / "build" / "docs-site"

# The first line of the README written into the section of every build. `write`
# recognizes a previous build by it (at <out>/dex/README.md) and refuses to clear
# any directory that lacks it. In exmergo/docs the same file tells a reader the
# folder is generated.
OUT_MARKER = "# Generated documentation section"

# Components a hand-written page may use. Anything else that opens with `<` in
# prose is a typo or raw HTML, and Mintlify would either reject it or render it
# as something nobody meant.
ALLOWED_COMPONENTS = frozenset(
    {
        "Accordion",
        "AccordionGroup",
        "Card",
        "CardGroup",
        "Columns",
        "Info",
        "Note",
        "Snippet",
        "Step",
        "Steps",
        "Tab",
        "Tabs",
        "Tip",
        "Update",
        "Warning",
    }
)

# Text that must never reach a public page: private planning material, ticket
# keys, and a claim the product does not support.
LEAK_PATTERNS = (
    (re.compile(r"unversioned-docs"), "names the private planning folder"),
    (re.compile(r"\bSCRUM-\d+"), "names a ticket key"),
    (re.compile("§"), "uses a section sign, which cites a planning document"),
    (re.compile(r"(?i)free Viz preview"), "promises a Viz preview that does not ship"),
)


@dataclass(frozen=True)
class PageMeta:
    title: str
    description: str
    sidebar_title: str | None = None


@dataclass(frozen=True)
class ReferencePage:
    source: str  # a file name under references/
    page: str  # the page id: its route without the leading slash
    meta: PageMeta


# Every file in references/ has a row, and every row has a file: the build fails
# either way, so a new reference cannot ship unpublished by accident and a
# renamed one cannot leave a dead route.
REFERENCE_PAGES: tuple[ReferencePage, ...] = (
    ReferencePage(
        "duckdb.md",
        "connectors/duckdb",
        PageMeta(
            "DuckDB connector",
            "Run dex on DuckDB with no credentials: the zero-setup on-ramp, the "
            "demo warehouse, the dev and CI engine, and the read-only, "
            "resource-bounded defaults.",
            "DuckDB",
        ),
    ),
    ReferencePage(
        "bigquery.md",
        "connectors/bigquery",
        PageMeta(
            "BigQuery connector",
            "Connect dex to BigQuery with Application Default Credentials. Every "
            "scan is priced by a free dry run, capped with maximum_bytes_billed, "
            "and read-only.",
            "BigQuery",
        ),
    ),
    ReferencePage(
        "snowflake.md",
        "connectors/snowflake",
        PageMeta(
            "Snowflake connector",
            "Connect dex to Snowflake through connections.toml, environment "
            "variables, or a dbt profile. Warehouse-seconds pricing, credits "
            "shown, read-only by design.",
            "Snowflake",
        ),
    ),
    ReferencePage(
        "databricks.md",
        "connectors/databricks",
        PageMeta(
            "Databricks connector",
            "Connect dex to Databricks SQL warehouses over Unity Catalog: unified "
            "SDK auth, warehouse-seconds pricing with DBUs alongside, and "
            "read-only enforcement.",
            "Databricks",
        ),
    ),
    ReferencePage(
        "redshift.md",
        "connectors/redshift",
        PageMeta(
            "Amazon Redshift connector",
            "Connect dex to Amazon Redshift Serverless or provisioned clusters: "
            "IAM temporary credentials, compute-seconds priced as RPU-hours, "
            "read-only access.",
            "Redshift",
        ),
    ),
    ReferencePage(
        "postgres.md",
        "connectors/postgres",
        PageMeta(
            "PostgreSQL connector",
            "Connect dex to PostgreSQL: credential discovery from pg_service.conf "
            "or the environment, database-seconds cost gating, and per-statement "
            "timeouts.",
            "PostgreSQL",
        ),
    ),
    ReferencePage(
        "clickhouse.md",
        "connectors/clickhouse",
        PageMeta(
            "ClickHouse connector",
            "Connect dex to self-hosted ClickHouse or ClickHouse Cloud: two-part "
            "namespaces, database-seconds or compute-seconds pricing, and "
            "read-only server caps.",
            "ClickHouse",
        ),
    ),
    ReferencePage(
        "trino.md",
        "connectors/trino",
        PageMeta(
            "Trino connector",
            "Connect dex to self-hosted Trino, Starburst Enterprise, or Trino on "
            "EMR: three-part names, cluster-seconds priced from EXPLAIN, and "
            "server-side caps.",
            "Trino",
        ),
    ),
    ReferencePage(
        "semantic-layer.md",
        "semantic-layers/overview",
        PageMeta(
            "Querying the semantic layer",
            "Query a semantic layer with dex explore semantic: list metrics, read "
            "dimension values, and run governed metric queries on dbt, dbt Cloud, "
            "or Apache Ossie.",
            "Overview",
        ),
    ),
    ReferencePage(
        "ossie-compatibility.md",
        "semantic-layers/ossie",
        PageMeta(
            "Apache Ossie compatibility",
            "What dex accepts from a native Apache Ossie semantic document, what "
            "it validates, what it derives for exploration and drift, and what it "
            "declines to claim.",
            "Apache Ossie",
        ),
    ),
    ReferencePage(
        "cube-compatibility.md",
        "semantic-layers/cube",
        PageMeta(
            "Cube compatibility",
            "How dex reads, validates and authors Cube YAML model files with no "
            "Cube server: the ported rules, compiled-only files, the catalog "
            "mapping and derived cubes.",
            "Cube",
        ),
    ),
    ReferencePage(
        "ossie-walkthrough.md",
        "guides/ossie-semantic-layer",
        PageMeta(
            "Apache Ossie tutorial: a semantic layer end to end",
            "Walk one Apache Ossie semantic document through every dex command on "
            "a local DuckDB warehouse: configure, read, query, detect drift, and "
            "author changes.",
            "Ossie tutorial",
        ),
    ),
    ReferencePage(
        "cost-controls.md",
        "guardrails/cost-controls",
        PageMeta(
            "Cost controls",
            "How dex prices warehouse work before it runs: the confirm and budget "
            "handshake, per-command and daily ceilings, the spend ledger, and "
            "auto-profile pricing.",
        ),
    ),
    ReferencePage(
        "pii-policy.md",
        "guardrails/pii-policy",
        PageMeta(
            "The PII policy",
            "How dex flags personal data without surfacing it: what a PII flag "
            "means, the blocking threshold, the query firewall rule, and how a "
            "human clears a flag.",
            "PII policy",
        ),
    ),
    ReferencePage(
        "values-free-mode.md",
        "guardrails/values-free-mode",
        PageMeta(
            "Values-free mode",
            "Explore a warehouse with no table value in any result: how a host "
            "pins the mode, the aggregate-only query rule, code-only errors, and "
            "the conformance suite.",
            "Values-free mode",
        ),
    ),
    ReferencePage(
        "canonical-model.md",
        "concepts/source-of-truth",
        PageMeta(
            "The source of truth and the .dex/ cache",
            "Why the repository is the source of truth for dex: dbt models and the "
            "semantic layer stay canonical, and the .dex/ directory is only a "
            "disposable cache.",
            "Source of truth",
        ),
    ),
    ReferencePage(
        "methodology.md",
        "concepts/methodology",
        PageMeta(
            "Exploration methodology",
            "How dex makes sense of a warehouse without enumerating it: one-pass "
            "inventory, ranking, aggregate-only profiling, join inference, and the "
            "persisted draft map.",
            "Methodology",
        ),
    ),
    ReferencePage(
        "dbt-project.md",
        "concepts/dbt-project",
        PageMeta(
            "dbt projects: what dex reads and writes",
            "What dex reads from a dbt project, how it writes changes back as "
            "reviewable diffs, and how it runs dbt build, deps, and parse against "
            "a dev target only.",
            "dbt projects",
        ),
    ),
    ReferencePage(
        "command-contract.md",
        "reference/command-contract",
        PageMeta(
            "The command contract",
            "The dex command contract: every subcommand, the single sanitized JSON "
            "envelope it prints, the query firewall, and the rules an agent relies "
            "on between calls.",
            "Command contract",
        ),
    ),
    ReferencePage(
        "project.md",
        "reference/project-formats",
        PageMeta(
            "Project formats",
            "The project seam behind dex: the three tiers a project format can "
            "satisfy, where edits land, and how to write and prove a new format "
            "with conformance tests.",
        ),
    ),
    ReferencePage(
        "storage.md",
        "reference/storage-backends",
        PageMeta(
            "Storage backends",
            "Where dex keeps its scratch state and how to replace it: the storage "
            "tiers, the spend ledger contract, serialized spend admission, and "
            "conformance tests.",
        ),
    ),
    ReferencePage(
        "host-integration.md",
        "reference/host-integration",
        PageMeta(
            "Running the lifecycle across processes",
            "Split the dex lifecycle across processes: export a verifiable plan, "
            "inspect dependencies and edits, apply offline, and read build "
            "evidence in a sandbox safely.",
            "Host integration",
        ),
    ),
    ReferencePage(
        "evaluation.md",
        "reference/evaluation",
        PageMeta(
            "Evaluation",
            "How dex is evaluated: unit and safety tests on every change, agent "
            "evals against a no-skill baseline, and published results on external "
            "analytics benchmarks.",
        ),
    ),
)

# One row per generated page. A CLI group with no row fails the build, so adding
# a group to COMMAND_SURFACE forces it a title, a description and a nav entry.
GENERATED_PAGES: dict[str, PageMeta] = {
    "cli/overview": PageMeta(
        "dex CLI overview",
        "Every dex CLI command at a glance: global options, connection flags "
        "shared by all subcommands, and where to start, generated from the "
        "released argparse tree.",
        "Overview",
    ),
    "cli/connect": PageMeta(
        "dex connect",
        "Reference for dex connect test: check that a connector can reach its "
        "warehouse with discovered credentials and report what it can do, before "
        "any real work.",
        "connect",
    ),
    "cli/explore": PageMeta(
        "dex explore",
        "Reference for dex explore: every read-only subcommand for inventory, "
        "profiling, joins, diagrams, guarded queries, clustering, and semantic "
        "layers, with flags.",
        "explore",
    ),
    "cli/transform": PageMeta(
        "dex transform",
        "Reference for dex transform: every subcommand that authors, plans, "
        "applies, builds, and tests dbt changes, with each flag, default, and "
        "positional argument.",
        "transform",
    ),
    "cli/semantic": PageMeta(
        "dex semantic",
        "Reference for dex semantic: define, update, and plan dbt semantic models "
        "or native Apache Ossie documents as reviewable diffs, with every flag "
        "documented.",
        "semantic",
    ),
    "cli/maintain": PageMeta(
        "dex maintain",
        "Reference for dex maintain: snapshot a baseline, check schema, volume, "
        "grain, and semantic drift, verify a project, and reconcile with proposed "
        "diffs.",
        "maintain",
    ),
    "cli/config": PageMeta(
        "dex config",
        "Reference for dex config: read every effective setting, validate "
        ".dex/config.yml by line, and set or unset keys as reviewable in-place "
        "edits that keep comments.",
        "config",
    ),
    "cli/demo": PageMeta(
        "dex demo",
        "Reference for dex demo: create a seeded local DuckDB warehouse with "
        "realistic data quality problems, no credentials or network, to try every "
        "dex command on.",
        "demo",
    ),
    "cli/procedure": PageMeta(
        "dex procedure",
        "Reference for dex procedure: print the agent procedure every dex skill "
        "carries, the block to paste into CLAUDE.md or AGENTS.md, and a stale check.",
        "procedure",
    ),
    "changelog": PageMeta(
        "Changelog",
        "Release notes for exmergo-dex-core and the dex skills: what was added, "
        "changed, and fixed in each of the recent releases, with links to the pull "
        "requests.",
    ),
}


@dataclass(frozen=True, order=True)
class Problem:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


# --- Markdown to MDX --------------------------------------------------------

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(([^)\s]+)\)")
PULL_REF = re.compile(r"\[#(\d+)\](?![(\[:])")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# Resolves one link target. Returns the URL to emit, or None after recording why
# the target is not acceptable.
LinkResolver = Callable[[str, int], "str | None"]


class _Slots:
    """Stand-ins for spans that later passes must not touch.

    The token is made of control characters only, so no escaping rule (which
    acts on `<`, `{`, `}` and `$`) can rewrite one, and no prose contains one.
    """

    def __init__(self) -> None:
        self._values: list[str] = []

    def hold(self, value: str) -> str:
        self._values.append(value)
        return f"\x00{len(self._values) - 1}\x01"

    def restore(self, text: str) -> str:
        # Repeated, because a held span can itself hold one: a link whose text
        # is a code span is held with that code span's token inside it, and a
        # single pass would leave the inner token in the page.
        token = re.compile(r"\x00(\d+)\x01")
        while token.search(text):
            text = token.sub(lambda m: self._values[int(m.group(1))], text)
        return text


def _escape(text: str) -> str:
    """Escape the characters MDX gives meaning to in prose."""

    return (
        text.replace("<", "&lt;")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("$", "\\$")
    )


def _convert_prose(
    block: str,
    line: int,
    resolve_link: LinkResolver | None,
    link_pull_requests: bool,
) -> str:
    """Convert one paragraph (or one table row) of Markdown prose to MDX."""

    slots = _Slots()
    block = CODE_SPAN.sub(lambda m: slots.hold(m.group(0)), block)
    block = HTML_COMMENT.sub("", block)
    block = AUTOLINK.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", block)

    def link(match: re.Match[str]) -> str:
        bang, text, target = match.groups()
        url = resolve_link(target, line) if resolve_link else target
        if url is None:
            return match.group(0)
        return slots.hold(f"{bang}[{_escape(text)}]({url})")

    block = LINK.sub(link, block)
    if link_pull_requests:
        block = PULL_REF.sub(
            lambda m: slots.hold(f"[#{m.group(1)}]({GITHUB}/pull/{m.group(1)})"),
            block,
        )
    return slots.restore(_escape(block))


def escape_prose(text: str) -> str:
    """Make a short piece of prose (a help string, an epilog) safe for MDX."""

    return _convert_prose(text, 0, None, False)


def to_mdx(
    text: str,
    *,
    source: str,
    resolve_link: LinkResolver | None = None,
    link_pull_requests: bool = False,
    lift_h1: bool = False,
) -> tuple[str, str | None, list[Problem]]:
    """Convert a Markdown document to MDX.

    Fenced blocks are copied untouched. Prose is converted a paragraph at a time,
    so an inline code span that wraps across lines is still recognized as code.
    With `lift_h1` the document must open with exactly one H1, which is removed
    and returned so the caller can decide the page title.
    Returns (body, h1 text or None, problems).
    """

    problems: list[Problem] = []
    out: list[str] = []
    block: list[str] = []
    block_start = 0
    fence: str | None = None
    h1: str | None = None
    seen_content = False

    def flush() -> None:
        nonlocal block
        if block:
            out.append(
                _convert_prose(
                    "\n".join(block), block_start, resolve_link, link_pull_requests
                )
            )
        block = []

    lines = text.splitlines()
    skip_blank_after_h1 = False
    for number, raw in enumerate(lines, start=1):
        marker = FENCE.match(raw)
        if fence is not None:
            out.append(raw)
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
            ):
                fence = None
            continue
        if marker:
            flush()
            fence = marker.group(1)
            out.append(raw)
            seen_content = True
            continue
        if skip_blank_after_h1:
            skip_blank_after_h1 = False
            if not raw.strip():
                continue
        heading = HEADING.match(raw)
        if lift_h1 and heading and len(heading.group(1)) == 1:
            if h1 is None and not seen_content:
                h1 = heading.group(2)
                skip_blank_after_h1 = True
                continue
            flush()
            problems.append(Problem(source, number, "a second H1 outside a code fence"))
            out.append(raw)
            continue
        if raw.strip():
            if lift_h1 and h1 is None and not seen_content:
                problems.append(
                    Problem(source, number, "the document must open with an H1")
                )
            seen_content = True
        if not raw.strip():
            flush()
            out.append(raw)
            continue
        # A table row is converted on its own, so a stray backtick in one cell
        # cannot pair with one in the next row and swallow prose between them.
        if raw.lstrip().startswith("|"):
            flush()
            block, block_start = [raw], number
            flush()
            continue
        if not block:
            block_start = number
        block.append(raw)
    flush()
    if fence is not None:
        problems.append(Problem(source, len(lines), "a code fence is never closed"))
    if lift_h1 and h1 is None and not any(p.message.endswith("H1") for p in problems):
        problems.append(Problem(source, 1, "the document must open with an H1"))
    body = "\n".join(out).strip("\n") + "\n"
    return body, h1, problems


def github_heading_ids(text: str) -> set[str]:
    """The anchor IDs GitHub gives a Markdown document's headings."""

    ids: set[str] = set()
    counts: dict[str, int] = {}
    fence: str | None = None
    for raw in text.splitlines():
        marker = FENCE.match(raw)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0]:
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            continue
        heading = HEADING.match(raw)
        if not heading:
            continue
        title = re.sub(r"`([^`]*)`", r"\1", heading.group(2))
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        ids.add(slug if seen == 0 else f"{slug}-{seen}")
    return ids


# --- The CLI reference ------------------------------------------------------


def _is_help(action: argparse.Action) -> bool:
    return isinstance(action, argparse._HelpAction)


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _display_default(action: argparse.Action) -> str:
    default = action.default
    if default in (None, argparse.SUPPRESS, False) or isinstance(
        action, argparse._StoreConstAction
    ):
        return ""
    return f"`{default}`"


def _positional_token(action: argparse.Action) -> str:
    if action.choices and not action.metavar:
        name = "{" + ",".join(str(c) for c in action.choices) + "}"
    else:
        name = f"<{action.metavar or action.dest}>"
    return {
        None: name,
        "?": f"[{name}]",
        "*": f"[{name} ...]",
        "+": f"{name} [{name} ...]",
    }.get(action.nargs, name)


def _option_token(action: argparse.Action) -> str:
    flag = action.option_strings[-1]
    if action.nargs == 0:
        return flag
    if action.choices:
        return f"{flag} {{{','.join(str(c) for c in action.choices)}}}"
    return f"{flag} {action.metavar or action.dest.upper()}"


def _synopsis(command: str, parser: argparse.ArgumentParser, skip: set[str]) -> str:
    exclusive = {
        id(a): group
        for group in parser._mutually_exclusive_groups
        for a in group._group_actions
    }
    tokens: list[str] = []
    emitted: set[int] = set()
    for action in parser._actions:
        if _is_help(action) or action.dest in skip or not action.option_strings:
            continue
        group = exclusive.get(id(action))
        if group is not None:
            if id(group) in emitted:
                continue
            emitted.add(id(group))
            tokens.append(
                "[" + " | ".join(_option_token(a) for a in group._group_actions) + "]"
            )
        else:
            tokens.append(f"[{_option_token(action)}]")
    tokens += [
        _positional_token(a)
        for a in parser._actions
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
    ]
    lines, current = [], command
    for token in tokens:
        if len(current) + 1 + len(token) > 78:
            lines.append(current)
            current = " " * (len(command.split(" ")[0]) + 1) + token
        else:
            current += " " + token
    lines.append(current)
    return "```text\n" + "\n".join(lines) + "\n```"


FLAG_IN_PROSE = re.compile(r"(?<![\w`-])(--[a-z][\w-]*)")


def _cli_prose(text: str) -> str:
    """Help prose for a page: one line, flag names set as code, MDX-escaped.

    Help strings name flags bare (`--rank orders them`), which reads fine in a
    terminal. On a page a flag is code, and leaving it bare would also read as a
    double-hyphen dash to the em-dash rule the build enforces.
    """

    parts = re.split(r"(`[^`]*`)", " ".join(text.split()))
    return escape_prose(
        "".join(
            part if part.startswith("`") else FLAG_IN_PROSE.sub(r"`\1`", part)
            for part in parts
        )
    )


def _cell(text: str) -> str:
    return _cli_prose(text).replace("|", "\\|")


def _tables(
    parser: argparse.ArgumentParser, skip: set[str], command: str, gaps: list[str]
) -> list[str]:
    arguments, options = [], []
    for action in parser._actions:
        if _is_help(action) or action.dest in skip:
            continue
        if isinstance(action, argparse._SubParsersAction):
            continue
        help_text = action.help if action.help not in (None, argparse.SUPPRESS) else ""
        if not help_text:
            label = action.option_strings[-1] if action.option_strings else action.dest
            gaps.append(f"{command} {label}: no help")
        if action.option_strings:
            flags = ", ".join(
                f"`{flag}`"
                if flag != action.option_strings[-1]
                else f"`{_option_token(action)}`"
                for flag in action.option_strings
            )
            options.append(
                f"| {flags} | {_cell(help_text)} | {_display_default(action)} |"
            )
        else:
            arguments.append(
                f"| `{_positional_token(action)}` | {_cell(help_text)} | "
                f"{_display_default(action)} |"
            )
    blocks = []
    if arguments:
        blocks.append(
            "**Arguments**\n\n"
            "| Argument | Description | Default |\n| --- | --- | --- |\n"
            + "\n".join(arguments)
        )
    if options:
        blocks.append(
            "**Options**\n\n| Option | Description | Default |\n| --- | --- | --- |\n"
            + "\n".join(options)
        )
    return blocks


def render_cli_reference(
    parser: argparse.ArgumentParser,
    shared_options: argparse.ArgumentParser,
    *,
    group_order: Iterable[str],
    base: str = "",
) -> tuple[dict[str, str], list[str]]:
    """Render the CLI reference: an overview page plus one page per group.

    `base` is the route the pages sit under (`/dex`), which every link between
    them carries. Returns ({page id: MDX body}, gaps), where each gap names a
    subcommand or an argument that carries no help text.
    """

    gaps: list[str] = []
    shared = {a.dest for a in shared_options._actions if not _is_help(a)}
    groups = _subparsers(parser)
    if groups is None:
        raise ValueError("the top-level parser has no command groups")

    rows = []
    for action in parser._actions:
        if _is_help(action) or not action.option_strings:
            continue
        where = (
            "before the group or after the subcommand"
            if action.dest in shared
            else "before the group only"
        )
        rows.append(
            f"| `{_option_token(action)}` | {_cell(action.help or '')} | {where} |"
        )
        if not action.help:
            gaps.append(f"dex {action.option_strings[-1]}: no help")
    summaries = {c.dest: c.help or "" for c in groups._choices_actions}
    group_rows = [
        f"| [`dex {name}`]({base}/cli/{name}) | {_cell(summaries.get(name, ''))} |"
        for name in group_order
    ]
    overview = "\n\n".join(
        [
            _cli_prose(parser.description or ""),
            "## Getting oriented",
            "\n\n".join(
                _cli_prose(paragraph)
                for paragraph in (parser.epilog or "").strip().split("\n\n")
            ),
            "## Command groups",
            "| Group | What it does |\n| --- | --- |\n" + "\n".join(group_rows),
            "## Global options",
            "Every command accepts these. The last column says where on the command "
            "line each one may go.",
            "| Option | Description | Position |\n| --- | --- | --- |\n"
            + "\n".join(rows),
        ]
    )
    pages = {"cli/overview": overview + "\n"}

    footer = (
        "Every command here also accepts the "
        f"[global options]({base}/cli/overview#global-options)."
    )
    for name in group_order:
        group_parser = groups.choices[name]
        sections = [_cli_prose(summaries.get(name, ""))]
        subs = _subparsers(group_parser)
        if subs is None:
            sections.append(_synopsis(f"dex {name}", group_parser, shared))
            sections += _tables(group_parser, shared, f"dex {name}", gaps)
        else:
            sub_summaries = {c.dest: c.help or "" for c in subs._choices_actions}
            for sub_name, sub_parser in subs.choices.items():
                command = f"dex {name} {sub_name}"
                if not sub_summaries.get(sub_name):
                    gaps.append(f"{command}: no summary")
                if not sub_parser.description:
                    gaps.append(f"{command}: no description")
                sections.append(f"## {command}")
                if sub_parser.description:
                    sections.append(_cli_prose(sub_parser.description))
                sections.append(_synopsis(command, sub_parser, shared))
                sections += _tables(sub_parser, shared, command, gaps)
        sections.append(footer)
        pages[f"cli/{name}"] = "\n\n".join(s for s in sections if s) + "\n"
    return pages, gaps


# --- The changelog ----------------------------------------------------------

RELEASE_HEADING = re.compile(r"^## \[([^\]]+)\](?:\s+-\s+(\S+))?\s*$", re.MULTILINE)


def render_changelog(
    text: str, *, release: str | None, limit: int = CHANGELOG_RELEASES
) -> tuple[str, list[Problem]]:
    """Render the newest released sections of CHANGELOG.md as Mintlify updates."""

    problems: list[Problem] = []
    headings = list(RELEASE_HEADING.finditer(text))
    released = [m for m in headings if m.group(1).lower() != "unreleased"]
    if not released:
        return "", [Problem("CHANGELOG.md", 1, "no released section")]

    def line_of(match: re.Match[str]) -> int:
        return text.count("\n", 0, match.start()) + 1

    newest = released[0]
    if not newest.group(2):
        problems.append(
            Problem("CHANGELOG.md", line_of(newest), "the newest release has no date")
        )
    if release is not None and newest.group(1) != release.removeprefix("v"):
        problems.append(
            Problem(
                "CHANGELOG.md",
                line_of(newest),
                f"the newest released section is {newest.group(1)}, "
                f"not the release being published ({release})",
            )
        )

    updates = []
    for match in released[:limit]:
        following = headings.index(match) + 1
        end = headings[following].start() if following < len(headings) else len(text)
        section = text[match.end() : end]
        # Link-reference definitions at the bottom of the file belong to no
        # release, so the last section stops before them.
        section = re.split(r"\n\[[^\]]+\]:\s+\S+", section)[0]
        body, _, section_problems = to_mdx(
            section, source="CHANGELOG.md", link_pull_requests=True
        )
        problems += [
            Problem(p.path, p.line + line_of(match), p.message)
            for p in section_problems
        ]
        label = match.group(1)
        date = match.group(2) or ""
        updates.append(
            f'<Update label="{label}" description="{date}">\n\n{body}\n</Update>'
        )
    older = (
        "Older releases are in the full "
        f"[CHANGELOG.md]({GITHUB}/blob/main/CHANGELOG.md) "
        f"and on [GitHub Releases]({GITHUB}/releases)."
    )
    return "\n\n".join([*updates, older]) + "\n", problems


# --- The site ---------------------------------------------------------------


def _git_tracked_files() -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    listed = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [git, "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in listed.split("\0") if p]


def _load_em_dash_check() -> Callable[[str], list[tuple]]:
    path = REPO_ROOT / "scripts" / "check_no_em_dashes.py"
    spec = importlib.util.spec_from_file_location("check_no_em_dashes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def _cli_parser() -> argparse.ArgumentParser:
    from exmergo_dex_core.cli import _build_parser

    return _build_parser()


def _cli_shared_options() -> argparse.ArgumentParser:
    from exmergo_dex_core.cli import _sub_connection_options

    return _sub_connection_options()


def _cli_group_order() -> list[str]:
    from exmergo_dex_core.cli import COMMAND_SURFACE

    return list(COMMAND_SURFACE)


def _navigation_pages(node: Any) -> list[str]:
    """Every page id in a docs.json navigation tree, in order, duplicates kept."""

    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [page for item in node for page in _navigation_pages(item)]
    if isinstance(node, dict):
        return [
            page
            for key in ("tabs", "groups", "pages", "anchors", "dropdowns")
            if key in node
            for page in _navigation_pages(node[key])
        ]
    return []


def _prefix_pages(node: Any, prefix: str) -> Any:
    """A navigation tree with every page id moved under `prefix`.

    Sources name pages as the section sees them (`connectors/bigquery`); the
    shared site names them by their path in exmergo/docs (`dex/connectors/bigquery`).
    """

    if isinstance(node, str):
        return prefix + node
    if isinstance(node, list):
        return [_prefix_pages(item, prefix) for item in node]
    if isinstance(node, dict):
        return {
            key: _prefix_pages(value, prefix)
            if key in ("tabs", "groups", "pages", "anchors", "dropdowns")
            else value
            for key, value in node.items()
        }
    return node


def _relocate_pages(node: Any, label: str) -> Any:
    """An archived navigation tree with every page id moved into `dex/<label>/`."""

    if isinstance(node, str):
        return f"{SECTION}/{label}/{node.removeprefix(f'{SECTION}/')}"
    if isinstance(node, list):
        return [_relocate_pages(item, label) for item in node]
    if isinstance(node, dict):
        return {
            key: _relocate_pages(value, label) if key in NAVIGATION_BODY else value
            for key, value in node.items()
        }
    return node


#: The keys of a navigation entry that hold its pages; the others (`product`,
#: `description`) name the entry, and stay on the product when it gains versions.
NAVIGATION_BODY = ("tabs", "groups", "pages", "anchors", "dropdowns")

#: A release as the version menu names it: `v1.14.1`.
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+")


def _minor(release: str) -> str:
    """The minor release a tag belongs to: `v1.14` for `v1.14.1`."""

    return ".".join(release.split(".")[:2])


#: A link to a route of this section, in Markdown or in a component's `href`.
#: An archived page's links are moved into its own version, so a reader who
#: picked v1.14 stays in v1.14 as they follow them.
SECTION_LINK = re.compile(r'(\]\(|href=")/' + SECTION + r'(?=[/#)"])')


@dataclass(frozen=True)
class Archive:
    """An older minor release's section, readable beside the latest one.

    `path` is the `dex/` folder that release's own `build_docs_site.py` wrote
    from its own tag, so an archive shows exactly what was published for it.
    Nothing is stored between releases: CI rebuilds every archive on each run,
    which is also what drops a minor release once it falls out of range.

    `release` is the exact tag it was built from, which its pages' notice
    names; the menu, its folder and its route use that release's minor, so a
    link to `/dex/v1.14/` survives a patch release.
    """

    release: str
    path: Path

    @property
    def label(self) -> str:
        return _minor(self.release)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Split a page into (frontmatter, body, the line the body starts on)."""

    if not text.startswith("---\n"):
        return {}, text, 1
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text, 1
    data = yaml.safe_load(text[4:end]) or {}
    body = text[end + 5 :]
    return data, body, text.count("\n", 0, end + 5) + 1


#: Appended to the title a shared link shows. The site's name is Exmergo, which
#: Mintlify appends to the browser title; a link to a Dex page should also say
#: which product it is about, and "dex" alone is a crowded word to search for.
SOCIAL_TITLE_SUFFIX = " · Exmergo Dex"


def _social_titles(page: Page) -> dict[str, str]:
    """The `og:title` and `twitter:title` a shared link to this page shows.

    A page's own frontmatter wins, so a hand-written page can still choose.
    """

    title = page.meta.get("title") or ""
    social = "Exmergo Dex" if page.page == "index" else f"{title}{SOCIAL_TITLE_SUFFIX}"
    return {key: page.meta.get(key, social) for key in ("og:title", "twitter:title")}


def _render_page(meta: dict[str, Any], body: str) -> str:
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{head}---\n\n{body.rstrip()}\n"


@dataclass
class Page:
    page: str  # page id, e.g. "connectors/bigquery"; "index" is the home page
    source: str  # where it came from, for problem reports
    meta: dict[str, Any]
    body: str
    hand_written: bool = False


@dataclass
class SiteBuilder:
    """Collects the site, checks it, and writes it.

    The collaborators that touch the world are constructor arguments, so a test
    can build a synthetic repository without git, the engine, or the real CLI.
    """

    repo_root: Path = REPO_ROOT
    ref: str = "main"
    release: str | None = None
    require_help: bool = False
    parser_factory: Callable[[], argparse.ArgumentParser] = _cli_parser
    shared_options_factory: Callable[[], argparse.ArgumentParser] = _cli_shared_options
    group_order_factory: Callable[[], list[str]] = _cli_group_order
    list_tracked_files: Callable[[], list[str]] = _git_tracked_files
    em_dash_check: Callable[[str], list[tuple]] | None = None
    reference_pages: tuple[ReferencePage, ...] = REFERENCE_PAGES
    generated_pages: dict[str, PageMeta] = field(
        default_factory=lambda: dict(GENERATED_PAGES)
    )
    #: Older minor releases, newest first, each shown as a version in the menu.
    archives: tuple[Archive, ...] = ()
    #: The release this build is (`v1.15.2`), which names it in the menu.
    latest: str | None = None

    def __post_init__(self) -> None:
        self.pages: dict[str, Page] = {}
        self.assets: dict[str, Path] = {}
        self.config: dict[str, Any] = {}
        self.problems: list[Problem] = []
        self.help_gaps: list[str] = []
        self._headings: dict[str, set[str]] = {}

    # Routes and links ------------------------------------------------------

    @property
    def docs_dir(self) -> Path:
        return self.repo_root / "docs"

    @property
    def references_dir(self) -> Path:
        return self.repo_root / "references"

    @staticmethod
    def route_for(page: str) -> str:
        """The site route of a page in this section: `/dex/connectors/bigquery`."""

        return f"/{SECTION}" if page == "index" else f"/{SECTION}/{page}"

    def _section_page(self, route: str) -> str | None:
        """The page a route names, when it is inside this section."""

        path = route.split("#")[0].rstrip("/")
        if path == f"/{SECTION}":
            return "index"
        if path.startswith(f"/{SECTION}/"):
            return path[len(SECTION) + 2 :]
        return None

    def _route_exists(self, route: str) -> bool:
        """Whether a route resolves. The site home (`/`) belongs to exmergo/docs
        and always exists; any other route outside this section is unknown here,
        so it does not resolve."""

        if route.split("#")[0] in ("", "/"):
            return True
        page = self._section_page(route)
        return page is not None and page in self.pages

    def _heading_ids(self, path: Path) -> set[str]:
        key = str(path)
        if key not in self._headings:
            self._headings[key] = github_heading_ids(path.read_text(encoding="utf-8"))
        return self._headings[key]

    def _reference_resolver(self, source: Path) -> LinkResolver:
        by_file = {r.source: r for r in self.reference_pages}
        rel_source = source.relative_to(self.repo_root).as_posix()

        def resolve(target: str, line: int) -> str | None:
            def problem(message: str) -> None:
                self.problems.append(Problem(rel_source, line, message))

            if re.match(r"^(https?:|mailto:)", target):
                if target.startswith(SITE):
                    route = target[len(SITE) :].split("#")[0] or "/"
                    if self._section_page(route) is not None and not (
                        self._route_exists(route)
                    ):
                        problem(f"links to {target}, which is not a page on the site")
                return target
            path_part, _, anchor = target.partition("#")
            if not path_part:
                if anchor and anchor not in self._heading_ids(source):
                    problem(f"links to #{anchor}, which is not a heading here")
                return target
            resolved = (source.parent / path_part).resolve()
            try:
                rel = resolved.relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                problem(f"links outside the repository: {target}")
                return None
            if (
                resolved.parent == self.references_dir.resolve()
                and resolved.name in by_file
            ):
                if anchor and anchor not in self._heading_ids(resolved):
                    problem(
                        f"links to {target}, but {resolved.name} has no such heading"
                    )
                route = self.route_for(by_file[resolved.name].page)
                return route + (f"#{anchor}" if anchor else "")
            if resolved.is_dir():
                return f"{GITHUB}/tree/{self.ref}/{rel}"
            if resolved.is_file():
                return f"{GITHUB}/blob/{self.ref}/{rel}" + (
                    f"#{anchor}" if anchor else ""
                )
            problem(f"links to {target}, which does not exist")
            return None

        return resolve

    def _source_footer(self, *sources: str) -> str:
        links = ", ".join(f"[`{s}`]({GITHUB}/blob/{self.ref}/{s})" for s in sources)
        edit = sources[0]
        return (
            f"Source: {links} ([edit on main]({GITHUB}/edit/main/{edit})). "
            "This page is generated; change the source, not the site."
        )

    # Collect ---------------------------------------------------------------

    def collect(self) -> None:
        self._collect_config()
        self._collect_hand_written()
        self._collect_references()
        self._collect_generated()

    def _add(self, page: Page) -> None:
        if page.page in self.pages:
            self.problems.append(
                Problem(
                    page.source,
                    1,
                    f"the route /{page.page} is already taken by "
                    f"{self.pages[page.page].source}",
                )
            )
            return
        self.pages[page.page] = page

    def _collect_config(self) -> None:
        """Read `docs/section.json`: this section's navigation and redirects.

        The site's own settings (name, theme, colours, navbar, analytics) live in
        exmergo/docs, so nothing here can change them for every product at once.
        """

        path = self.docs_dir / SECTION_CONFIG
        source = f"docs/{SECTION_CONFIG}"
        if not path.is_file():
            self.problems.append(Problem(source, 1, "missing"))
            return
        try:
            self.config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.problems.append(Problem(source, exc.lineno, exc.msg))
            return
        unexpected = sorted(set(self.config) - {"navigation", "redirects"})
        if unexpected:
            self.problems.append(
                Problem(
                    source,
                    1,
                    f"holds {', '.join(unexpected)}; a section carries only its "
                    "navigation and redirects, and site settings live in exmergo/docs",
                )
            )

    def _collect_hand_written(self) -> None:
        if not self.docs_dir.is_dir():
            return
        for path in sorted(self.docs_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.docs_dir).as_posix()
            if rel == SECTION_CONFIG:
                continue
            if path.suffix == ".mdx" and not rel.startswith("snippets/"):
                text = path.read_text(encoding="utf-8")
                meta, body, _ = _frontmatter(text)
                self._add(
                    Page(
                        page=rel.removesuffix(".mdx"),
                        source=f"docs/{rel}",
                        meta=meta,
                        body=body,
                        hand_written=True,
                    )
                )
            else:
                self.assets[rel] = path

    def _collect_references(self) -> None:
        mapped = {r.source for r in self.reference_pages}
        present = (
            {p.name for p in self.references_dir.glob("*.md")}
            if self.references_dir.is_dir()
            else set()
        )
        for name in sorted(present - mapped):
            self.problems.append(
                Problem(
                    f"references/{name}",
                    1,
                    "has no row in REFERENCE_PAGES, so it would not be published",
                )
            )
        for name in sorted(mapped - present):
            self.problems.append(
                Problem(
                    "scripts/build_docs_site.py",
                    1,
                    f"REFERENCE_PAGES maps references/{name}, which does not exist",
                )
            )
        # Every reference page is registered before any is converted, so a link
        # between two references resolves whatever order they are read in.
        for ref in self.reference_pages:
            if ref.source in present:
                self._add(Page(ref.page, f"references/{ref.source}", {}, ""))
        for ref in self.reference_pages:
            if self.pages.get(ref.page, Page("", "", {}, "")).source != (
                f"references/{ref.source}"
            ):
                continue  # missing, or the route was already taken (reported)
            path = self.references_dir / ref.source
            body, h1, problems = to_mdx(
                path.read_text(encoding="utf-8"),
                source=f"references/{ref.source}",
                resolve_link=self._reference_resolver(path),
                lift_h1=True,
            )
            self.problems += problems
            meta: dict[str, Any] = {
                "title": ref.meta.title or (h1 or "").replace("`", "")
            }
            if ref.meta.sidebar_title:
                meta["sidebarTitle"] = ref.meta.sidebar_title
            meta["description"] = ref.meta.description
            footer = self._source_footer(f"references/{ref.source}")
            body = f"{body.rstrip()}\n\n{footer}\n"
            self.pages[ref.page] = Page(
                ref.page, f"references/{ref.source}", meta, body
            )

    def _collect_generated(self) -> None:
        def meta_for(page: str) -> dict[str, Any]:
            info = self.generated_pages[page]
            meta: dict[str, Any] = {"title": info.title}
            if info.sidebar_title:
                meta["sidebarTitle"] = info.sidebar_title
            meta["description"] = info.description
            return meta

        cli_pages, self.help_gaps = render_cli_reference(
            self.parser_factory(),
            self.shared_options_factory(),
            group_order=self.group_order_factory(),
            base=f"/{SECTION}",
        )
        if self.require_help:
            self.problems += [
                Problem("packages/dex-core/src/exmergo_dex_core/cli.py", 1, gap)
                for gap in self.help_gaps
            ]
        cli_source = "packages/dex-core/src/exmergo_dex_core/cli.py"
        for page, body in cli_pages.items():
            if page not in self.generated_pages:
                self.problems.append(
                    Problem(
                        "scripts/build_docs_site.py",
                        1,
                        f"the CLI renders /{page}, which has no row in GENERATED_PAGES",
                    )
                )
                continue
            footer = self._source_footer(cli_source)
            self._add(Page(page, cli_source, meta_for(page), f"{body}\n{footer}\n"))

        changelog_path = self.repo_root / "CHANGELOG.md"
        if "changelog" in self.generated_pages and changelog_path.is_file():
            body, problems = render_changelog(
                changelog_path.read_text(encoding="utf-8"), release=self.release
            )
            self.problems += problems
            meta = {**meta_for("changelog"), "rss": True}
            self._add(Page("changelog", "CHANGELOG.md", meta, body))

    # Validate --------------------------------------------------------------

    def redirects(self) -> list[dict[str, str]]:
        """The configured redirects plus one per reference file path.

        A repository path pasted after the section (`/dex/references/storage.md`)
        lands on the page it became. Every source is inside the section, because
        the rest of the site's routes belong to exmergo/docs and to other products.
        """

        generated = [
            {"source": source, "destination": self.route_for(ref.page)}
            for ref in self.reference_pages
            for source in (
                f"/{SECTION}/references/{ref.source.removesuffix('.md')}",
                f"/{SECTION}/references/{ref.source}",
            )
        ]
        return [*self.config.get("redirects", []), *generated]

    def validate(self) -> None:
        self._validate_pages()
        self._validate_navigation()
        self._validate_redirects()
        self._validate_tracked_docs_urls()

    def _validate_pages(self) -> None:
        for page in self.pages.values():
            if not page.meta.get("title"):
                self.problems.append(Problem(page.source, 1, "has no title"))
            description = page.meta.get("description") or ""
            if len(description) not in DESCRIPTION_LENGTH:
                self.problems.append(
                    Problem(
                        page.source,
                        1,
                        f"description is {len(description)} characters; it must be "
                        f"{DESCRIPTION_LENGTH.start} to {DESCRIPTION_LENGTH.stop - 1}",
                    )
                )
            if page.hand_written:
                self._validate_hand_written(page)

    def _validate_hand_written(self, page: Page) -> None:
        _, _, offset = _frontmatter(
            (self.repo_root / page.source).read_text(encoding="utf-8")
        )
        fence: str | None = None
        for number, raw in enumerate(page.body.splitlines(), start=offset):
            marker = FENCE.match(raw)
            if fence is not None:
                if marker and marker.group(1)[0] == fence[0]:
                    fence = None
                continue
            if marker:
                fence = marker.group(1)
                continue
            prose = re.sub(r"`[^`]*`", "", raw)
            for match in re.finditer(r"</?([A-Za-z][\w.]*)", prose):
                if match.group(1) not in ALLOWED_COMPONENTS:
                    self.problems.append(
                        Problem(
                            page.source,
                            number,
                            f"<{match.group(1)}> is not an allowed component; "
                            "write &lt; for a literal angle bracket",
                        )
                    )
            targets = [m.group(3) for m in LINK.finditer(prose)]
            targets += re.findall(r'href="([^"]+)"', prose)
            for target in targets:
                if re.match(r"^(https?:|mailto:|#)", target):
                    if target.startswith(SITE):
                        route = target[len(SITE) :].split("#")[0] or "/"
                        if self._section_page(route) is not None and not (
                            self._route_exists(route)
                        ):
                            self.problems.append(
                                Problem(
                                    page.source,
                                    number,
                                    f"links to {target}, not a page",
                                )
                            )
                    continue
                if not target.startswith("/"):
                    self.problems.append(
                        Problem(
                            page.source,
                            number,
                            f"links to {target}; pages under docs/ link by "
                            f"site route, such as /{SECTION}/guardrails/cost-controls",
                        )
                    )
                    continue
                inside = self._section_page(target)
                asset = inside is not None and (self.docs_dir / inside).is_file()
                if not self._route_exists(target) and not asset:
                    why = (
                        "not a page"
                        if inside is not None
                        else f"outside the /{SECTION} section; link to another part "
                        f"of the site by its full {SITE} URL"
                    )
                    self.problems.append(
                        Problem(page.source, number, f"links to {target}, {why}")
                    )

    def _validate_navigation(self) -> None:
        config = f"docs/{SECTION_CONFIG}"
        navigation = _navigation_pages(self.config.get("navigation", {}))
        seen: set[str] = set()
        for page in navigation:
            if page in seen:
                self.problems.append(
                    Problem(config, 1, f"{page} is in navigation twice")
                )
            seen.add(page)
        hidden = {p for p, page in self.pages.items() if page.meta.get("hidden")}
        for page in sorted(seen - set(self.pages)):
            self.problems.append(
                Problem(config, 1, f"navigation lists {page}, which no source builds")
            )
        for page in sorted(set(self.pages) - seen - hidden):
            self.problems.append(
                Problem(
                    self.pages[page].source,
                    1,
                    f"builds {self.route_for(page)}, which is not in the section's "
                    "navigation",
                )
            )

    def _validate_redirects(self) -> None:
        config = f"docs/{SECTION_CONFIG}"
        for redirect in self.redirects():
            source, destination = redirect["source"], redirect["destination"]
            if self._section_page(source) is None:
                self.problems.append(
                    Problem(
                        config,
                        1,
                        f"redirect source {source} is outside /{SECTION}; the rest "
                        "of the site's routes belong to exmergo/docs",
                    )
                )
            if not self._route_exists(destination):
                self.problems.append(
                    Problem(config, 1, f"redirect {source} goes to a missing page")
                )
            if self._route_exists(source):
                self.problems.append(
                    Problem(config, 1, f"redirect source {source} is also a page")
                )

    def _validate_tracked_docs_urls(self) -> None:
        pattern = re.compile(re.escape(SITE) + r"(/[^\s)\"'`>\]]*)?")
        for rel in self.list_tracked_files():
            path = self.repo_root / rel
            if path.suffix not in {
                ".md",
                ".mdx",
                ".py",
                ".toml",
                ".json",
                ".yml",
                ".yaml",
                ".txt",
            }:
                continue
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                for match in pattern.finditer(line):
                    route = (match.group(1) or "/").split("#")[0].rstrip(".,;:")
                    # Only this section's routes are checkable here; the rest of
                    # the site is exmergo/docs's to answer for.
                    if self._section_page(route) is None:
                        continue
                    if not self._route_exists(route):
                        self.problems.append(
                            Problem(
                                rel,
                                number,
                                f"{match.group(0).rstrip('.,;:')} is not a page "
                                "on the site",
                            )
                        )

    # Write -----------------------------------------------------------------

    def rendered(self) -> dict[str, str]:
        """Every text file the build emits, keyed by its path in the output."""

        files = {
            f"{SECTION}/{page.page}.mdx": _render_page(
                {**page.meta, **_social_titles(page)}, page.body
            )
            for page in self.pages.values()
        }
        # The two files exmergo/docs's docs.json reads with `$ref`, so this
        # section owns its own menu and redirects without editing the shared file.
        navigation = _prefix_pages(self.config.get("navigation", {}), f"{SECTION}/")
        if self.archives:
            navigation = self.versioned_navigation(navigation)
        files[f"{SECTION}/navigation.json"] = _json(navigation)
        files[f"{SECTION}/redirects.json"] = _json(self.redirects())
        files[f"{SECTION}/README.md"] = (
            f"{OUT_MARKER}\n\n"
            f"Built by `scripts/build_docs_site.py` in {GITHUB} at `{self.ref}`,\n"
            f"and published here by its release workflow. Do not edit this folder:\n"
            "every release replaces it. Change `docs/`, `references/`,\n"
            f"`CHANGELOG.md` or the CLI in {GITHUB} instead.\n"
        )
        return files

    # Versions ----------------------------------------------------------------

    def versioned_navigation(self, navigation: dict[str, Any]) -> dict[str, Any]:
        """The section's menu with a version switcher: latest, then each archive.

        Latest keeps the routes it has always had, so every link already shared
        still lands on the current docs; an archive lives under its label.
        """

        def body(entry: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in entry.items() if k in NAVIGATION_BODY}

        # The menu names minor releases, as most versioned docs do: a patch
        # changes little, and the archived pages' notice names the exact tag.
        # "Latest" is the version's tag, the badge the menu shows beside its
        # name, so the name stays a plain release like every other entry.
        versions = [
            {
                "version": _minor(self.latest) if self.latest else "Latest",
                "default": True,
                **({"tag": "Latest"} if self.latest else {}),
                **body(navigation),
            }
        ]
        for archive in self.archives:
            path = archive.path / "navigation.json"
            if not path.is_file():
                continue  # reported by _validate_archives
            menu = json.loads(path.read_text(encoding="utf-8"))
            versions.append(
                {"version": archive.label, **body(_relocate_pages(menu, archive.label))}
            )
        head = {k: v for k, v in navigation.items() if k not in NAVIGATION_BODY}
        return {**head, "versions": versions}

    def archived(self) -> dict[str, bytes]:
        """Every file the archives add, keyed by its path in the output.

        Pages are kept as their release built them, with three changes: their
        links stay inside their version, `noindex` keeps them out of search, so a
        search always lands on the latest docs, and a notice at the top names the
        release and links to the latest docs, since a reader who arrives from an
        old link never saw the version menu. Each archive's own menu is folded
        into the section's, and its redirects and README are left behind.
        """

        files: dict[str, bytes] = {}
        for archive in self.archives:
            for source in sorted(archive.path.rglob("*")):
                rel = source.relative_to(archive.path).as_posix()
                if not source.is_file() or rel in {
                    "navigation.json",
                    "redirects.json",
                    "README.md",
                }:
                    continue
                name = f"{SECTION}/{archive.label}/{rel}"
                if source.suffix != ".mdx":
                    files[name] = source.read_bytes()
                    continue
                text = source.read_text(encoding="utf-8")
                text = SECTION_LINK.sub(rf"\1/{SECTION}/{archive.label}", text)
                notice = self._archive_notice(archive, rel.removesuffix(".mdx"))
                if text.startswith("---\n"):
                    end = text.find("\n---\n", 4)
                    if end != -1:
                        end += len("\n---\n")
                        text = f"{text[:end]}\n{notice}\n{text[end:]}"
                    text = "---\nnoindex: true\n" + text[4:]
                files[name] = text.encode("utf-8")
        return files

    def _archive_notice(self, archive: Archive, page: str) -> str:
        """The callout an archived page opens with: which release it shows, and
        the same page in the latest docs, or the section's home where the latest
        release no longer has it."""

        target = self.route_for(page if page in self.pages else "index")
        return (
            f"<Note>\n  You are reading the docs for dex {archive.label} "
            f"({archive.release}). See the [latest version]({target}).\n</Note>\n"
        )

    def _validate_archives(self) -> None:
        if self.latest is not None and not RELEASE_TAG.fullmatch(self.latest):
            self.problems.append(
                Problem("--latest", 1, "the latest version is a release: vX.Y.Z")
            )
        labels = [archive.label for archive in self.archives]
        for archive in self.archives:
            where = f"--archive {archive.release}"
            if not RELEASE_TAG.fullmatch(archive.release):
                self.problems.append(
                    Problem(where, 1, "an archive is named by its release: vX.Y.Z")
                )
                continue
            if (
                self.latest is not None and archive.label == _minor(self.latest)
            ) or labels.count(archive.label) > 1:
                self.problems.append(
                    Problem(where, 1, f"{archive.label} is listed more than once")
                )
            menu = archive.path / "navigation.json"
            if not menu.is_file():
                self.problems.append(
                    Problem(where, 1, f"{archive.path} holds no built section")
                )
                continue
            pages = _navigation_pages(json.loads(menu.read_text(encoding="utf-8")))
            for page in pages:
                rel = page.removeprefix(f"{SECTION}/")
                if not (archive.path / f"{rel}.mdx").is_file():
                    self.problems.append(
                        Problem(where, 1, f"its menu lists {page}, which it lacks")
                    )

    def check_output(self) -> None:
        """Checks that read the emitted text rather than the sources."""

        check = self.em_dash_check or _load_em_dash_check()
        for name, text in sorted(self.rendered().items()):
            for hit in check(text):
                self.problems.append(
                    Problem(f"(built) {name}", hit[0], f"em-dash rule: {hit[2]!r}")
                )
            # Nothing a page should contain is a control character, and one
            # renders as nothing at all, so a lost placeholder or stray byte
            # would otherwise ship as silently missing text.
            for match in re.finditer(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
                line = text.count("\n", 0, match.start()) + 1
                self.problems.append(
                    Problem(
                        f"(built) {name}",
                        line,
                        f"contains the control character {match.group()!r}",
                    )
                )
            for pattern, why in LEAK_PATTERNS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    self.problems.append(Problem(f"(built) {name}", line, why))

    def write(self, out: Path, site: Path | None = None) -> list[str]:
        """Write the section to `out/dex/`.

        With `site`, a checkout of exmergo/docs, `out` becomes the whole site:
        that checkout's files with this section in place of its `dex/`, which is
        what CI validates and what `mint dev` previews.
        """

        # A checkout of exmergo/docs carries the same marker in its dex/ folder,
        # so the marker alone would let `--out ../docs` clear a real repository,
        # history included. Refuse any git checkout, and the site itself.
        if (out / ".git").exists() or (
            site is not None and out.resolve() == site.resolve()
        ):
            raise SystemExit(
                f"refusing to write into {out}: it is a git checkout. Pass --out "
                "with a new or empty directory, and --site for the exmergo/docs "
                "checkout to read from."
            )
        if site is not None and site.resolve().is_relative_to(out.resolve()):
            raise SystemExit(f"refusing to write into {out}: it contains --site {site}")
        if out.exists() and any(out.iterdir()):
            readme = out / SECTION / "README.md"
            if not (
                readme.is_file()
                and readme.read_text(encoding="utf-8").startswith(OUT_MARKER)
            ):
                raise SystemExit(
                    f"refusing to clear {out}: it is not empty and holds no previous "
                    "build. Pass --out with a new or empty directory."
                )
            for child in out.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        out.mkdir(parents=True, exist_ok=True)
        if site is not None:
            root = str(site.resolve())

            def skip_at_root(directory: str, names: list[str]) -> set[str]:
                # Only the site's own .git and its copy of this section; a
                # deeper folder that happens to be called dex/ is content.
                if str(Path(directory).resolve()) != root:
                    return set()
                return {name for name in names if name in (".git", SECTION)}

            shutil.copytree(site, out, ignore=skip_at_root, dirs_exist_ok=True)
        written = []
        for name, text in sorted(self.rendered().items()):
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
            written.append(name)
        for name, source in sorted(self.assets.items()):
            target = out / SECTION / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            written.append(name)
        for name, data in sorted(self.archived().items()):
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(name)
        return written

    def build(self) -> list[Problem]:
        self.collect()
        self.validate()
        self._validate_archives()
        self.check_output()
        return sorted(set(self.problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_docs_site.py",
        description="Assemble the documentation site from the repository.",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="output directory"
    )
    parser.add_argument(
        "--release",
        help="the tag being published; source links point at it, and the newest "
        "CHANGELOG section must be this version",
    )
    parser.add_argument(
        "--require-help",
        action="store_true",
        help="treat a CLI argument or subcommand without help text as a problem",
    )
    parser.add_argument(
        "--site",
        type=Path,
        help="a checkout of exmergo/docs: write the whole site, with this section "
        "in place of its dex/ folder, for validating and previewing",
    )
    parser.add_argument(
        "--archive",
        action="append",
        default=[],
        metavar="vX.Y.Z=DIR",
        help="an older minor release to keep readable: the tag it was built from "
        "and the dex/ folder that build wrote. Repeatable, newest first; each "
        "becomes a version in the menu, served under /dex/vX.Y/",
    )
    parser.add_argument(
        "--latest",
        metavar="vX.Y.Z",
        help="the release this build is, which names it in the version menu; "
        "defaults to --release",
    )
    args = parser.parse_args(argv)
    if args.site is not None and not (args.site / "docs.json").is_file():
        parser.error(f"--site {args.site} holds no docs.json")
    archives = []
    for spec in args.archive:
        release, sep, directory = spec.partition("=")
        if not sep or not directory:
            parser.error(f"--archive {spec}: expected vX.Y.Z=DIR")
        archives.append(Archive(release, Path(directory)))
    latest = args.latest or args.release

    builder = SiteBuilder(
        ref=args.release or "main",
        release=args.release,
        require_help=args.require_help,
        archives=tuple(archives),
        latest=latest,
    )
    problems = builder.build()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} problem(s); nothing written.", file=sys.stderr)
        return 1
    written = builder.write(args.out, site=args.site)
    print(
        f"Wrote {len(written)} files ({len(builder.pages)} pages) to {args.out}. "
        f"CLI help gaps: {len(builder.help_gaps)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
