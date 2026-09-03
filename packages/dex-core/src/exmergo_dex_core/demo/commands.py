"""The `dex demo` shim: generate the warehouse, wire it up, name what to run next.

Two jobs beyond the generation itself. It writes a `.dex/config.yml` beside the
new file, so the commands it prints need no flags at all and the run reads like a
working project rather than a loose file. And it refuses to write that config
where one already exists at or above the target, because a second config in a
subdirectory would silently shadow the user's real one for every command run
there; in that case the warehouse is still created and the printed commands
switch to the explicit ``--path`` form.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pydantic import Field

from .. import envelope as env
from ..command_args import resolve_dex_root
from ..config import CONFIG_FILE, DexConfig, DuckDBTarget, save_config
from ..diffs import file_diff
from ..engine import DexEngine
from ..results import Result, to_envelope
from ..storage import DEX_DIR
from .warehouse import DEMO_FILENAME, DemoPathError, generate_demo_warehouse


class DemoResult(Result):
    """What `dex demo` built, and what to run against it."""

    path: str
    seed: int
    row_count: int
    tables: list[dict[str, Any]] = Field(default_factory=list)
    created: list[str] = Field(default_factory=list)
    next_steps: list[dict[str, str]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "seed": self.seed,
            "object_count": len(self.tables),
            "row_count": self.row_count,
            "tables": self.tables,
            "created": self.created,
            "next_steps": self.next_steps,
        }


# Spelled out here rather than inline below so the SELECT reads as the printed
# text it is: nothing in this module ever executes a statement.
_PII_PROBE = 'dex explore query "select email from customers"'


def _next_steps(flags: str) -> list[dict[str, str]]:
    """The tour. Three commands that each end in a finding, plus the refusal.

    Ordered so the run builds on itself: map first because everything downstream
    reads the cache it writes, then the two findings that need no argument, then
    the refusal, which only means anything once profiling has flagged the column.
    """

    return [
        {
            "command": f"dex explore map{flags}",
            "shows": (
                "ranks the seven objects, profiles them, flags the personal data, "
                "and infers the joins. Free and local: DuckDB bills nothing, so "
                "nothing here asks you to confirm a spend. On BigQuery or "
                "Snowflake this same command returns an estimate first and runs "
                "only once you agree to it"
            ),
        },
        {
            "command": f"dex explore profile order_items products{flags}",
            "shows": (
                "order_item_id is not unique, so any join on it fans out; sku is a "
                "key that mixes two id schemes, so casting it to a number would "
                "silently drop rows"
            ),
        },
        {
            "command": f"dex explore relationships --verify{flags}",
            "shows": (
                "orders joins cleanly to customers, and web_events.customer_id "
                "matches the name but none of the values, so the inference "
                "collapses instead of shipping a join that returns nothing"
            ),
        },
        {
            "command": _PII_PROBE + flags,
            "shows": (
                "refused: the query firewall does not let personal data cross into "
                "context. Count it or aggregate it instead, and that runs"
            ),
        },
    ]


def cmd_demo(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    """Build the demo warehouse and return the one envelope the contract expects.

    Takes the engine for signature consistency with every other command shim and
    deliberately does not use it: demo reaches no warehouse, no store, and no
    project, so anything it read off the engine would be a connection it has no
    business resolving.
    """

    read_path = getattr(args, "path", None)
    if read_path:
        raise DemoPathError(
            "dex demo takes the file to create as a positional argument, not "
            "--path: everywhere else --path names the warehouse dex reads, and "
            f"this is the one command that writes one. Use `dex demo {read_path}`"
        )

    target = Path(getattr(args, "target", None) or DEMO_FILENAME)
    warehouse = generate_demo_warehouse(target)

    # The config goes beside the warehouse, and only where nothing above already
    # owns one: `resolve_dex_root` is the same walk every command uses to find
    # its project, so agreeing with it here is what stops the demo shadowing a
    # real config with one of its own.
    root = target.parent
    config_path = root / DEX_DIR / CONFIG_FILE
    config_rel = str(config_path)
    existing_root = resolve_dex_root(root)
    created = [str(target)]
    diffs: list[dict[str, Any]] = []
    warnings: list[str] = []
    if existing_root is None:
        save_config(
            DexConfig(connector="duckdb", duckdb=DuckDBTarget(path=target.name)), root
        )
        created.append(config_rel)
        diffs.append(
            file_diff(config_rel, None, config_path.read_text(encoding="utf-8"))
        )
        # Bare commands only work from the directory the config landed in, so the
        # flagless tour is offered only when that is where the caller already is.
        flags = "" if root == Path() else f" --path {target}"
    else:
        warnings.append(
            f"'{existing_root}' already has a .dex/config.yml and it was left "
            "untouched; the commands below name the demo warehouse explicitly "
            "rather than repointing your project at it"
        )
        flags = f" --path {target}"

    return to_envelope(
        DemoResult(
            path=str(target),
            seed=warehouse.seed,
            row_count=warehouse.row_count,
            tables=[
                {"name": t.name, "row_count": len(t.rows), "note": t.note}
                for t in warehouse.tables
            ],
            created=created,
            next_steps=_next_steps(flags),
            diffs=diffs,
            warnings=warnings,
            notes=[
                "the data is generated from a pinned seed, so every run of this "
                "command produces the same rows and the counts quoted in the docs "
                "are the counts you have",
                "the flaws are deliberate: a broken grain, a key that mixes id "
                "schemes, a join with no overlap, an empty table, two columns "
                "whose declared type contradicts their content, and personal data "
                "alongside two false positives",
            ],
        )
    )
