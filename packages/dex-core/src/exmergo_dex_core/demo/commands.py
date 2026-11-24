"""The `dex demo` shim: generate the warehouse, wire it up, name what to run next.

Three jobs beyond the generation itself. It writes a `.dex/config.yml` beside the
new file, so the commands it prints need no flags at all and the run reads like a
working project rather than a loose file. It writes the demo's dbt project
(:mod:`.project`) beside them, so the tour ends at an incident and its cause. And
it refuses to write the config or the project where a config already exists at
or above the target, because a second config in a subdirectory would silently
shadow the user's real one for every command run there; in that case the
warehouse is still created and the printed commands switch to the explicit
``--path`` form.
"""

from __future__ import annotations

import argparse
import contextlib
import shlex
from pathlib import Path
from typing import Any

from pydantic import Field

from .. import envelope as env
from ..command_args import resolve_dex_root
from ..config import CONFIG_FILE, DexConfig, DuckDBTarget, config_yaml
from ..diffs import file_diff
from ..engine import DexEngine
from ..results import Result, to_envelope
from ..storage import DEX_DIR
from .project import (
    FAILING_TEST,
    PROJECT_DIR,
    build_project,
    refuse_collision,
    refuse_target_name,
    write_project,
)
from .warehouse import (
    DEMO_FILENAME,
    DemoPathError,
    DemoTargetExistsError,
    generate_demo_warehouse,
)

#: The failing test as a person reads it: its name, not its hashed id.
_FAILING_TEST_NAME = FAILING_TEST.split(".")[2]


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


def _next_steps(flags: str, rooted: str | None = None) -> list[dict[str, str]]:
    """The tour. Three commands that each end in a finding, plus the refusal,
    then the incident the dbt project's last build recorded.

    Ordered so the run builds on itself: map first because everything downstream
    reads the cache it writes, then the two findings that need no argument, then
    the refusal, which only means anything once profiling has flagged the column.
    The incident and its diagnosis come last, and only where the dbt project was
    written: ``rooted`` is the flag that roots a command at the config's root,
    ``None`` where it was not.
    """

    incident = (
        [
            {
                "command": f"dex maintain incidents{rooted}",
                "shows": (
                    f"one open incident, read from dbt's run results: the test "
                    f"{_FAILING_TEST_NAME} failed in the project's last build, "
                    "because of the batch loaded twice. One build is all dbt's "
                    "run results hold, so when the failure began is unknown "
                    "(first_observed: unknown), and the envelope says so"
                ),
            },
            {
                "command": f"dex maintain diagnose{rooted}",
                "shows": (
                    "the cause: the loader delivered order_item_id more than "
                    "once, measured on the source stg_order_items reads, with "
                    "what was not checked and a transform plan to hand the "
                    "repair to. Diagnosis proposes the repair; it writes nothing"
                ),
            },
        ]
        if rooted is not None
        else []
    )
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
        *incident,
    ]


def _create_config(config: DexConfig, path: Path) -> str:
    """Write the demo's config to ``path`` exclusively and return its text.

    Not ``save_config``, which replaces a file already there: the demo creates
    and never replaces, so a config that appeared since the demo looked (a
    second ``dex demo`` in the same directory) is refused, untouched. A write
    that fails part way removes the file it opened, so the caller's rollback
    has nothing of this function's to find.
    """

    text = config_yaml(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # "x", and no newline argument: the same text and line endings
        # `save_config` would write, only never over an existing file.
        handle = path.open("x", encoding="utf-8")
    except FileExistsError:
        raise DemoTargetExistsError(
            f"'{path.as_posix()}' appeared while dex demo ran, so another run "
            "is likely writing to this directory. It was left as it is, and "
            "nothing this run made was kept"
        ) from None
    try:
        with handle:
            handle.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return text


def cmd_demo(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    """Build the demo warehouse and return the one envelope the contract expects.

    Takes the engine for signature consistency with every other command shim and
    deliberately does not use it: demo reaches no warehouse, no store, and no
    project, so anything it read off the engine would be a connection it has no
    business resolving. The directory it writes into is ``--repo-root`` (the
    shell cwd by default), read straight off ``args`` for the same reason.
    """

    read_path = getattr(args, "path", None)
    if read_path:
        raise DemoPathError(
            "dex demo takes the file to create as a positional argument, not "
            "--path: everywhere else --path names the warehouse dex reads, and "
            f"this is the one command that writes one. Use `dex demo {read_path}`"
        )

    # The raw run directory, not `repo_root(args)` and not `engine.repo_root`:
    # both walk up to whichever config owns the tree, and `dex --repo-root R demo`
    # has to land exactly where `cd R && dex demo` does, so that the shadowing
    # check below still sees a config above R as one to leave alone. An absolute
    # target ignores the run directory, because pathlib's join drops the left
    # side when the right one is absolute.
    run_dir = Path(getattr(args, "repo_root", None) or ".")
    named = Path(getattr(args, "target", None) or DEMO_FILENAME)
    target = run_dir / named

    # The config goes beside the warehouse, and only where nothing above already
    # owns one: `resolve_dex_root` is the same walk every command uses to find
    # its project, so agreeing with it here is what stops the demo shadowing a
    # real config with one of its own. What gets reported is relative to the run
    # directory, the way every writer reports the files it wrote under its root.
    # Spelled with forward slashes on every platform, the rule `_relative` in
    # `transform/dev_target.py` follows: these are labels a caller compares
    # against paths in configuration, and `.dex/config.yml` is one everywhere.
    root = target.parent
    config_path = root / DEX_DIR / CONFIG_FILE
    config_rel = (named.parent / DEX_DIR / CONFIG_FILE).as_posix()
    existing_root = resolve_dex_root(root)
    # The dbt project is wiring too, so it lands only where the config does: a
    # project under someone else's config would be found by no command. Its
    # directory is refused before the warehouse is created, so a refused call
    # has written nothing at all.
    if existing_root is None and root.is_dir():
        refuse_target_name(target)
        refuse_collision(root)
    warehouse = generate_demo_warehouse(target)
    created = [named.as_posix()]
    diffs: list[dict[str, Any]] = []
    warnings: list[str] = []

    # The printed commands are typed into the caller's shell, wherever this one
    # ran from. So they carry the same --repo-root, or the cache `explore map`
    # writes would land in the cwd, and a --path is spelled from the cwd too,
    # because a live --path resolves against the shell rather than the root.
    # Forward slashes again: a backslash path comes out in single quotes, which
    # cmd passes through as part of the path, while the POSIX form of an
    # ordinary path needs no quoting and types the same into any shell.
    rerun = (
        "" if run_dir == Path() else f" --repo-root {shlex.quote(run_dir.as_posix())}"
    )
    explicit = f"{rerun} --path {shlex.quote(target.as_posix())}"
    # The incident commands read the dbt project, which is found from the
    # config's root, so they name that root rather than the file.
    rooted: str | None = None
    if existing_root is None:
        made_dex_dir = not config_path.parent.exists()
        config_made = False
        project = build_project(warehouse.tables, target.name)
        # BaseException, not Exception: an interrupt part way is cleaned up
        # like a failure, or the next run would refuse the warehouse it left.
        try:
            config_text = _create_config(
                DexConfig(
                    connector="duckdb",
                    duckdb=DuckDBTarget(path=target.name),
                    dbt_project_dir=PROJECT_DIR,
                ),
                config_path,
            )
            config_made = True
            written = write_project(root, project)
        except BaseException:
            # Nothing half-made is left for a later command to read as real,
            # and nothing this run did not make is removed: a config another
            # run wrote since the check above stays, and so does a .dex/ that
            # was there before (a cache).
            if config_made:
                config_path.unlink(missing_ok=True)
            if made_dex_dir:
                with contextlib.suppress(OSError):
                    config_path.parent.rmdir()
            target.unlink(missing_ok=True)
            raise
        created.append(config_rel)
        diffs.append(file_diff(config_rel, None, config_text))
        prefix = named.parent
        created.extend((prefix / path).as_posix() for path in written)
        diffs.extend(
            file_diff((prefix / PROJECT_DIR / path).as_posix(), None, text)
            for path, text in project.sources
        )
        # The config only resolves for commands rooted where it landed, so the
        # tour names the file unless that is the run directory itself.
        flags = rerun if named.parent == Path() else explicit
        rooted = (
            rerun
            if named.parent == Path()
            else f" --repo-root {shlex.quote((run_dir / named.parent).as_posix())}"
        )
    else:
        warnings.append(
            f"'{existing_root}' already has a .dex/config.yml and it was left "
            "untouched; the commands below name the demo warehouse explicitly "
            "rather than repointing your project at it. The demo's dbt project, "
            "and the incident it holds, need a directory of their own: run dex "
            "demo outside that project to see them"
        )
        flags = explicit

    return to_envelope(
        DemoResult(
            path=named.as_posix(),
            seed=warehouse.seed,
            row_count=warehouse.row_count,
            tables=[
                {"name": t.name, "row_count": len(t.rows), "note": t.note}
                for t in warehouse.tables
            ],
            created=created,
            next_steps=_next_steps(flags, rooted),
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
                *(
                    [
                        f"{PROJECT_DIR}/ is a dbt project over two of the tables, "
                        "with the artifacts its last dbt build left in "
                        f"{PROJECT_DIR}/target/. They are seeded, not built: the "
                        "demo runs no dbt and installs no package. In that build "
                        f"the test {_FAILING_TEST_NAME} failed, because of the "
                        "batch loaded twice. Its two staging views were not "
                        "built, so `dex maintain verify` reports them as "
                        "no_relation until `dex transform build --target dev` "
                        "builds them"
                    ]
                    if rooted is not None
                    else []
                ),
            ],
        )
    )
