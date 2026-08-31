"""The skill wrapper's extra resolution.

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
        (["maintain", "check"], []),
        # scikit-learn, and only where clustering actually runs.
        (["explore", "cluster", "orders"], ["cluster"]),
        # `list` renders no statement on either backend, so it never needs
        # MetricFlow. Bare `explore semantic` is `list`.
        (["explore", "semantic"], ["semantic-api"]),
        (["explore", "semantic", "list"], ["semantic-api"]),
        (["explore", "semantic", "list", "--local"], ["semantic-api"]),
        (["explore", "semantic", "list", "--api"], ["semantic-api"]),
        # dbt Cloud renders and executes, so --api never needs MetricFlow either.
        (["explore", "semantic", "values", "user__tier", "--api"], ["semantic-api"]),
        (["explore", "semantic", "query", "orders", "--api"], ["semantic-api"]),
        # A statement rendered here needs the renderer.
        (
            ["explore", "semantic", "values", "user__tier", "--local"],
            ["semantic-api", "semantic"],
        ),
        (
            ["explore", "semantic", "query", "orders", "--local"],
            ["semantic-api", "semantic"],
        ),
        # No flag means the backend is ambient, and the wrapper does not read the
        # nested config that decides it: both go in, so the command cannot refuse
        # for want of a dependency.
        (
            ["explore", "semantic", "values", "user__tier"],
            ["semantic-api", "semantic"],
        ),
        (["explore", "semantic", "query", "orders"], ["semantic-api", "semantic"]),
    ],
)
def test_the_extras_follow_the_command(wrapper, argv, expected):
    assert wrapper._feature_extras(argv) == expected


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
    assert wrapper._extras(["--connector", "bigquery", "maintain", "check"]) == [
        "bigquery"
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
        "semantic-api"
    ]
    assert wrapper._feature_extras(
        ["explore", "semantic", "list", "--for-dimension", "values"]
    ) == ["semantic-api"]
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
