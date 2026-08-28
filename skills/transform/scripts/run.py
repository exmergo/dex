# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Thin PEP 723 wrapper that drives dex-core via the command contract.

The skill never re-implements logic. It forwards its arguments to the pinned
`dex-core` engine and lets the engine print the sanitized JSON envelope. Run it
with `uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <dex subcommand> ...`.

`uv` is a hard prerequisite: it is what installs and runs the engine. When it is
absent this wrapper refuses with an error envelope naming the install command,
rather than letting the exec fail with a traceback. Invoked through `uv run` the
shell fails first (`uv: command not found`), which is why each SKILL.md also tells
the agent what that message means.

Two execution modes, chosen automatically:
  - Monorepo checkout (this repo): `packages/dex-core` is found above the skill,
    so the engine runs from an editable local install. This is what makes the
    wrapper work before the package is published.
  - Installed plugin: no local package is present, so the pinned PyPI release is
    installed hermetically by uv.

The engine version is pinned; the *extras* are chosen at runtime, so one published
release serves every warehouse and the default install stays light. The connector
extra comes from the active connector; two commands need more than a warehouse
client and say so by being run (see _feature_extras). This wrapper is
stdlib-only and runs before the engine is installed, so it resolves the connector
itself (it cannot import the engine) with the same precedence the engine uses:
an explicit --connector flag, then the top-level `connector:` in the
`.dex/config.yml` found by walking up from the run directory to the git root (the
way git and dbt find their project), then DuckDB. The walk-up must mirror the
engine's: if it did not, a run from a subdirectory would install the DuckDB extra
while the engine resolves the project's real connector and then fails for want of
that connector's deps. The guess only picks which extra to install; the full argv
is still forwarded, so the engine stays authoritative for the actual connection
and a wrong guess surfaces as a clean error envelope.

`DEX_CORE_VERSION` is the single line bumped at release time, by
scripts/prepare_release.sh before the tag; nothing else here changes per release.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Rewritten by scripts/prepare_release.sh to the tagged version. The connector
# extra is deliberately NOT part of this pin: it is chosen at runtime (see
# _resolve_connector), so a release artifact is connector-neutral.
DEX_CORE_VERSION = "1.9.0"

# Connector id -> packaging extra. The engine's connector ids and the pyproject
# extras share names, so this is the identity set today. An unknown or unset
# connector falls back to the light DuckDB on-ramp and lets the installed engine
# emit the canonical error rather than the wrapper guessing wrong.
_KNOWN_CONNECTORS = (
    "duckdb",
    "snowflake",
    "bigquery",
    "databricks",
    "postgres",
    "redshift",
    "clickhouse",
)
_DEFAULT_CONNECTOR = "duckdb"


def _connector_from_config(config_path: Path) -> str | None:
    """Read the top-level scalar `connector:` from .dex/config.yml, stdlib only.

    This bootstrap script has no YAML dependency and only needs enough to pick the
    right extra to install, so it scans for a single unindented `connector:` key.
    The engine remains the source of truth for the full config; anything richer or
    malformed here just falls through to the DuckDB default.
    """

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("connector:"):  # top-level only; indented keys ignored
            value = line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
            return value or None
    return None


def _find_config(start: Path) -> Path | None:
    """Nearest ancestor `.dex/config.yml` at or above `start`, mirroring the
    engine's resolution: walk up to the enclosing git repo (the ceiling), and
    without one do not walk above `start`. Anchors on the file so a subdirectory
    holding only a `.dex/` cache never shadows the real config higher up."""

    start = start.resolve()
    ceiling = start
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            ceiling = directory
            break
    for directory in (start, *start.parents):
        candidate = directory / ".dex" / "config.yml"
        if candidate.is_file():
            return candidate
        if directory == ceiling:
            break
    return None


def _resolve_connector(argv: list[str], cwd: Path) -> str:
    """Pick the connector whose extra we install, mirroring the engine's order:
    explicit --connector, then the walked-up .dex/config.yml, then DuckDB."""

    # allow_abbrev=False and parse_known_args so we only peek at these two flags
    # and never consume or reorder the argv that is forwarded to the engine.
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--connector")
    parser.add_argument("--repo-root", default=".")
    known, _ = parser.parse_known_args(argv)

    connector = known.connector
    if connector is None:
        config_path = _find_config(cwd / known.repo_root)
        if config_path is not None:
            connector = _connector_from_config(config_path)
    return connector if connector in _KNOWN_CONNECTORS else _DEFAULT_CONNECTOR


# Every value-taking flag this peek has to consume, so a flag's *value* is never
# mistaken for the group, subcommand, or mode being run. The global connection
# flags, plus the ones `explore semantic` takes, because a metric read as a mode
# picks the wrong extras. `tests/test_skill_wrapper.py` holds this list to the
# real parser, since a flag added there and forgotten here fails silently.
_VALUE_FLAGS = (
    "--connector",
    "--path",
    "--scope",
    "--project",
    "--dataset",
    "--repo-root",
    "--budget",
    "--metric",
    "--for-dimension",
    "--search",
    "--group-by",
    "--where",
    "--order-by",
    "--grain",
    "--limit",
)

# The `explore semantic` modes that can render a statement here. Bare
# `explore semantic` lists, so an absent mode is `list`, which renders none on
# either backend and therefore never needs the renderer.
_SEMANTIC_RENDERING_MODES = ("values", "query")


def _positionals(argv: list[str]) -> list[str]:
    """The bare tokens of an invocation: group, subcommand, and whatever follows.

    Flag-position agnostic, which is the point: the value flags above are consumed
    so the group and subcommand are the first two bare tokens wherever the
    connection flags sit."""

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    for flag in _VALUE_FLAGS:
        parser.add_argument(flag)
    _, remaining = parser.parse_known_args(argv)
    return [tok for tok in remaining if not tok.startswith("-")]


def _feature_extras(argv: list[str]) -> list[str]:
    """The extras this invocation needs on top of the connector's.

    Two commands need more than a warehouse client, and both are resolved from the
    command rather than installed always, so a repo that never clusters and never
    queries a semantic layer resolves neither scikit-learn nor MetricFlow.

    `explore semantic` needs the hosted client on any of its subcommands, which is
    an httpx and nothing heavier. It needs MetricFlow only where a statement might
    be rendered *here*: `list` renders none on either backend, and `--api` sends
    the request to dbt Cloud, which renders and executes it there.

    Where the flag is absent the backend is ambient, chosen by `semantic.deployment`
    in a nested config block this bootstrap deliberately does not parse, so a mode
    that could execute either way takes both. That errs toward a heavier install
    and never toward a command that refuses for want of a dependency, which is the
    failure this exists to prevent.
    """

    positionals = _positionals(argv)
    if positionals[:2] == ["explore", "cluster"]:
        return ["cluster"]
    if positionals[:2] != ["explore", "semantic"]:
        return []
    mode = positionals[2] if len(positionals) > 2 else "list"
    if mode in _SEMANTIC_RENDERING_MODES and "--api" not in argv:
        return ["semantic-api", "semantic"]
    return ["semantic-api"]


def _engine_spec(extras: str, skill_dir: Path | None = None) -> list[str]:
    skill_dir = skill_dir or Path(
        os.environ.get("CLAUDE_SKILL_DIR", Path(__file__).resolve().parent.parent)
    )
    local_pkg = (skill_dir / ".." / ".." / "packages" / "dex-core").resolve()
    if local_pkg.is_dir():
        # Resolve the local package WITH the resolved extras (a plain path drops
        # extras). Non-editable is fine: the engine is imported fresh each run.
        return ["--with", f"exmergo-dex-core[{extras}] @ {local_pkg.as_uri()}"]
    return ["--with", f"exmergo-dex-core[{extras}]=={DEX_CORE_VERSION}"]


def main() -> int:
    argv = sys.argv[1:]
    if shutil.which("uv") is None:
        # The one refusal that happens before the engine exists, so the envelope
        # is hand-built: `exmergo_dex_core.envelope` is precisely what is not
        # installed yet. The keys mirror Envelope/Cost and have to stay in step
        # with them, and `prerequisite` is the engine's own classification for a
        # missing dependency the user installs and retries (the same one
        # DemoDependencyError and DialectDependencyError carry). A caller reads
        # this exactly like any other refusal instead of parsing a traceback.
        print(
            json.dumps(
                {
                    "status": "error",
                    "data": {},
                    "cost": {"paradigm": None, "estimate": None, "ceiling": None},
                    "warnings": [],
                    "diffs": [],
                    "errors": [
                        "dex runs its engine through uv, which was not found on "
                        "PATH. Install it with: "
                        "curl -LsSf https://astral.sh/uv/install.sh | sh "
                        "(or `brew install uv`, or `pipx install uv`), then re-run."
                    ],
                    "reason": "prerequisite",
                }
            )
        )
        return 1
    connector = _resolve_connector(argv, Path.cwd())
    # The connector extra is always installed; `explore cluster` and
    # `explore semantic` add what only they need, so the default install stays
    # light for every repo that runs neither.
    extras = ",".join([connector, *_feature_extras(argv)])
    cmd = [
        "uv",
        "run",
        *_engine_spec(extras),
        "python",
        "-m",
        "exmergo_dex_core",
        *argv,
    ]
    # The engine runs in uv's own ephemeral environment, so an inherited
    # VIRTUAL_ENV (e.g. the user's activated venv) is irrelevant here and only
    # makes uv print a mismatch warning on every call. Drop it.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
