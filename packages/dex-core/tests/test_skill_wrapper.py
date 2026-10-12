"""The skill wrapper's extra resolution, and what it hands the engine.

`skills/<skill>/scripts/run.py` runs before the engine is installed, so it decides
which packaging extras `uv` resolves. That decision is invisible from inside the
engine: every other test imports `exmergo_dex_core` from the source tree, where
every extra is already present, so a wrapper that installs the wrong set fails only
for a real user and only at the moment they run the command.

It failed exactly that way. `explore semantic` shipped in 1.8.0 and the wrapper
never installed either semantic extra, so through the skill anything but
`list --local` refused for want of a dependency, and nothing here noticed.

The wrapper is stdlib-only and lives outside the package, so it is loaded from its
path rather than imported.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[3] / "skills"
WRAPPERS = sorted(SKILLS.glob("*/scripts/run.py"))


def _wrapper():
    spec = importlib.util.spec_from_file_location(
        "_dex_skill_wrapper", SKILLS / "explore" / "scripts" / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wrapper():
    return _wrapper()


def test_the_three_wrappers_stay_byte_identical():
    """One file, copied. A fix applied to one skill and not the others is the
    failure mode this catches, and it is what makes testing one of them enough."""

    assert len(WRAPPERS) == 3
    contents = {path.read_bytes() for path in WRAPPERS}
    assert len(contents) == 1, [path.as_posix() for path in WRAPPERS]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Nothing but the connector for the commands that need nothing more.
        (["explore", "inventory"], []),
        (["explore", "query", "select 1"], []),
        (["transform", "plan", "add a model"], []),
        (["maintain", "schema"], []),
        (["maintain", "reconcile"], []),
        # dbt semantic authoring reads no Ossie document.
        (["semantic", "define", "--edits-file", "edits.json"], []),
        # scikit-learn, and only where clustering actually runs.
        (["explore", "cluster", "orders"], ["cluster"]),
        # jsonschema for every command that can read a native Ossie layer (#536):
        # whether the layer is Ossie is `semantic.vendor`, in the nested config
        # the wrapper does not parse, so the extra follows the command.
        (["semantic", "ossie", "define", "--edits-file", "edits.json"], ["ossie"]),
        (["semantic", "ossie", "update", "--edits-file", "edits.json"], ["ossie"]),
        (["semantic", "ossie", "plan", "--edits-file", "edits.json"], ["ossie"]),
        (["maintain", "semantic"], ["ossie"]),
        (["maintain", "check"], ["ossie"]),
        (["maintain", "snapshot"], ["ossie"]),
        (["--connector", "bigquery", "maintain", "check"], ["ossie"]),
        # `list` renders no statement on either backend, so it never needs
        # MetricFlow. Bare `explore semantic` is `list`. It can read an Ossie
        # layer, so it takes `ossie` as well.
        (["explore", "semantic"], ["ossie", "semantic-api"]),
        (["explore", "semantic", "list"], ["ossie", "semantic-api"]),
        (["explore", "semantic", "list", "--local"], ["ossie", "semantic-api"]),
        (["explore", "semantic", "list", "--api"], ["ossie", "semantic-api"]),
        # dbt Cloud renders and executes, so --api never needs MetricFlow either.
        (
            ["explore", "semantic", "values", "user__tier", "--api"],
            ["ossie", "semantic-api"],
        ),
        (
            ["explore", "semantic", "query", "orders", "--api"],
            ["ossie", "semantic-api"],
        ),
        # A statement rendered here needs the renderer.
        (
            ["explore", "semantic", "values", "user__tier", "--local"],
            ["ossie", "semantic-api", "semantic"],
        ),
        (
            ["explore", "semantic", "query", "orders", "--local"],
            ["ossie", "semantic-api", "semantic"],
        ),
        # No flag means the backend is ambient, and the wrapper does not read the
        # nested config that decides it: both go in, so the command cannot refuse
        # for want of a dependency.
        (
            ["explore", "semantic", "values", "user__tier"],
            ["ossie", "semantic-api", "semantic"],
        ),
        (
            ["explore", "semantic", "query", "orders"],
            ["ossie", "semantic-api", "semantic"],
        ),
    ],
)
def test_the_extras_follow_the_command(wrapper, argv, expected):
    assert wrapper._feature_extras(argv) == expected


def test_every_ossie_reading_command_takes_the_ossie_extra(wrapper):
    """Structural: the table in the wrapper is the claim, and each row of it
    resolves to the extra whatever flags surround the command (#536)."""

    for group, subcommand in wrapper._OSSIE_READING_COMMANDS:
        argv = ["--connector", "snowflake", group, subcommand, "--repo-root", "."]
        assert "ossie" in wrapper._feature_extras(argv), (group, subcommand)


def test_the_extras_are_the_connector_plus_whatever_the_command_needs(
    wrapper, tmp_path, monkeypatch
):
    """`_extras` is the one seam a real run and `--warm` share.

    Warming an environment that disagrees with the one the next command resolves
    would leave the cold install exactly where it was, so the two must not have
    separate answers to compare."""

    # An empty directory, so nothing but the argv decides: `_extras` reads the
    # cwd for a `.dex/config.yml` the way the engine does.
    monkeypatch.chdir(tmp_path)

    assert wrapper._extras(["explore", "inventory"]) == ["duckdb"]
    assert wrapper._extras(["explore", "cluster", "orders"]) == ["duckdb", "cluster"]
    assert wrapper._extras(["--connector", "bigquery", "maintain", "schema"]) == [
        "bigquery"
    ]
    # `maintain check` can read an Ossie layer, so it takes `ossie` on every
    # connector, including the one whose own tree happens to carry jsonschema.
    assert wrapper._extras(["--connector", "bigquery", "maintain", "check"]) == [
        "bigquery",
        "ossie",
    ]
    assert wrapper._extras(["--connector", "snowflake", "explore", "cluster", "o"]) == [
        "snowflake",
        "cluster",
    ]


def test_a_flag_value_is_never_read_as_the_mode(wrapper):
    """The peek has to consume value-taking flags, or `--metric query` reads as
    `explore semantic query` and installs MetricFlow for a catalog read. Worse in
    the other direction: a value that lands where the mode goes can hide a mode
    that needed the renderer."""

    assert wrapper._feature_extras(["explore", "semantic", "--metric", "query"]) == [
        "ossie",
        "semantic-api",
    ]
    assert wrapper._feature_extras(
        ["explore", "semantic", "list", "--for-dimension", "values"]
    ) == ["ossie", "semantic-api"]
    # And the connection flags still do not shift the group and subcommand.
    assert wrapper._feature_extras(
        ["--connector", "bigquery", "explore", "cluster", "orders"]
    ) == ["cluster"]


def test_every_value_flag_the_semantic_parser_defines_is_consumed(wrapper):
    """Structural, against the real parser rather than a list kept by hand.

    A value-taking flag added to `explore semantic` and forgotten here does not
    break anything visibly: it shifts the bare tokens by one, and the wrapper picks
    its extras from the wrong word. Reading the parser is what makes that a failing
    test rather than a silent wrong install.
    """

    from exmergo_dex_core.cli import _build_parser

    parser = _build_parser()
    subparser = _semantic_subparser(parser)
    missing = [
        option
        for action in subparser._actions
        if action.nargs != 0 and action.option_strings
        for option in action.option_strings
        if option.startswith("--") and option not in wrapper._VALUE_FLAGS
    ]
    assert missing == [], (
        f"these `explore semantic` flags take a value and the wrapper does not "
        f"consume them, so their values can be read as the mode: {missing}"
    )


def _semantic_subparser(parser):
    """The `explore semantic` subparser, dug out of the built parser.

    Two levels of subparsers (group, then subcommand), and argparse exposes them
    only through the action's `choices`, which is why this is a walk rather than a
    lookup.
    """

    import argparse

    def subactions(target):
        return [a for a in target._actions if isinstance(a, argparse._SubParsersAction)]

    for action in subactions(parser):
        group = action.choices.get("explore")
        if group is None:
            continue
        for inner in subactions(group):
            if "semantic" in inner.choices:
                return inner.choices["semantic"]
    raise AssertionError("no `explore semantic` subparser found")


# --- what the wrapper hands the engine -----------------------------------------
#
# The wrapper is the one process that sits between the agent's environment and
# the engine, so it is where the engine's inputs get narrowed: the package it
# installs comes from where the wrapper lives, and the environment it passes on
# leaves out credentials the engine never reads.


def test_a_skill_dir_from_the_environment_cannot_redirect_the_engine(
    wrapper, tmp_path, monkeypatch
):
    """`CLAUDE_SKILL_DIR` pointing at another tree with a `packages/dex-core` two
    levels up changes nothing: the harness already used it to find this file, and
    reading it again would let the environment choose which code uv installs."""

    decoy_skill = tmp_path / "decoy" / "skills" / "explore"
    decoy_skill.mkdir(parents=True)
    (tmp_path / "decoy" / "packages" / "dex-core").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_SKILL_DIR", str(decoy_skill))

    spec = wrapper._engine_spec("duckdb")

    real = (SKILLS.parent / "packages" / "dex-core").resolve()
    assert spec == ["--with", f"exmergo-dex-core[duckdb] @ {real.as_uri()}"]


def test_the_child_env_withholds_unread_credentials_and_keeps_discovery(
    wrapper, monkeypatch
):
    for name, value in {
        "CLICKHOUSE_CLOUD_API_KEY": "control-plane-key",
        "GITHUB_TOKEN": "ghp_not_real",
        "VIRTUAL_ENV": "/somewhere/.venv",
        "DATABASE_URL": "postgresql://dex_ro@localhost/app",
        "SNOWFLAKE_PRIVATE_KEY_FILE": "/keys/k.p8",
    }.items():
        monkeypatch.setenv(name, value)

    env = wrapper._child_env()

    for withheld in ("CLICKHOUSE_CLOUD_API_KEY", "GITHUB_TOKEN", "VIRTUAL_ENV"):
        assert withheld not in env
    assert env["DATABASE_URL"] == "postgresql://dex_ro@localhost/app"
    assert env["SNOWFLAKE_PRIVATE_KEY_FILE"] == "/keys/k.p8"
    assert env["PATH"] == os.environ["PATH"]
    # A copy: the wrapper's own environment is left as it was found.
    assert os.environ["GITHUB_TOKEN"] == "ghp_not_real"  # noqa: S105 (a dummy)
    assert os.environ["VIRTUAL_ENV"] == "/somewhere/.venv"


@pytest.mark.parametrize("argv", [["explore", "inventory"], ["--warm"]])
def test_both_ways_into_uv_carry_the_child_env(argv, monkeypatch):
    """The exec that runs a command and the warm-up's subprocess both get the
    narrowed environment. A path that fell back to inheriting would hand the
    engine, or the uv resolving it, everything the agent's shell holds."""

    module = _wrapper()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_real")
    monkeypatch.setenv("DATABASE_URL", "postgresql://dex_ro@localhost/app")
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/local/bin/uv")
    envs: list[dict[str, str]] = []
    # The wrapper execs on POSIX and spawns through `subprocess.call` elsewhere,
    # and warms through `subprocess.run` everywhere. All three are intercepted,
    # so the environment is asserted on whichever branch this platform takes,
    # rather than forcing one by setting `os.name`, which `module.os` shares
    # with the whole process and pathlib reads to choose its path class.
    monkeypatch.setattr(
        module.os, "execvp", lambda *_a: pytest.fail("exec bypassed the child env")
    )
    monkeypatch.setattr(
        module.os, "execvpe", lambda _file, _cmd, env: envs.append(env) or None
    )
    monkeypatch.setattr(
        module.subprocess,
        "call",
        lambda _cmd, **kwargs: envs.append(kwargs.get("env")) or 0,
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda cmd, **kwargs: (
            envs.append(kwargs.get("env"))
            or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(module.sys, "argv", ["run.py", *argv])

    assert module.main() == 0
    assert len(envs) == 1 and envs[0] is not None
    assert "GITHUB_TOKEN" not in envs[0]
    assert envs[0]["DATABASE_URL"] == "postgresql://dex_ro@localhost/app"
