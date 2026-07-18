"""The command contract: every subcommand prints exactly one parseable envelope
with a valid status and nothing else on stdout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cli import COMMAND_SURFACE, main

_VALID_STATUSES = {s.value for s in env.Status}


def _all_commands() -> list[list[str]]:
    """Every (group, subcommand) pair in the surface, as argv lists.

    Commands with a required positional get a placeholder so argparse accepts
    them; with no connection target they return a valid error envelope, which
    still satisfies the contract (one parseable envelope, valid status).
    """

    argvs: list[list[str]] = []
    for group, subcommands in COMMAND_SURFACE.items():
        if subcommands:
            for sub in subcommands:
                argv = [group, sub]
                if group == "explore" and sub == "profile":
                    argv.append("some_table")
                if group == "explore" and sub == "query":
                    # A missing repo-root means no .dex cache, so the firewall
                    # refuses cleanly and nothing is written anywhere.
                    argv += ["SELECT 1", "--repo-root", "missing-dex-fixture-dir"]
                if group == "explore" and sub == "cluster":
                    argv += ["some_table", "--repo-root", "missing-dex-fixture-dir"]
                argvs.append(argv)
        else:
            argvs.append([group])
    return argvs


@pytest.mark.parametrize("argv", _all_commands(), ids=lambda a: " ".join(a))
def test_every_command_emits_one_valid_envelope(argv, capsys):
    # connect test needs a target; without one it returns a valid error envelope,
    # which still satisfies the contract (one parseable envelope, valid status).
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    payload = json.loads(out)
    assert payload["status"] in _VALID_STATUSES
    assert set(payload) == {"status", "data", "cost", "warnings", "diffs", "errors"}
    assert rc in (0, 1)


def test_unbuilt_commands_report_not_implemented(capsys):
    # The Viz preview is the one remaining stub; it lands later as an
    # integration with the Viz product.
    assert main(["viz", "preview"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_implemented"


def test_connect_test_against_duckdb_is_ok(duckdb_file: Path, capsys):
    # The contract documents the flag AFTER the subcommand (connect test --path X).
    rc = main(["connect", "test", "--path", str(duckdb_file)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["data"]["read_only"] is True


@pytest.mark.parametrize(
    "argv_builder",
    [
        lambda f: ["--path", f, "connect", "test"],  # global flag before
        lambda f: ["connect", "test", "--path", f],  # global flag after
    ],
    ids=["flag-before-subcommand", "flag-after-subcommand"],
)
def test_global_options_work_in_either_position(argv_builder, duckdb_file, capsys):
    rc = main(argv_builder(str(duckdb_file)))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["data"]["read_only"] is True


def test_connect_test_without_path_is_clean_error(capsys, tmp_path):
    rc = main(["--repo-root", str(tmp_path), "connect", "test"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["errors"]


@pytest.mark.parametrize(
    "argv_builder",
    [
        lambda: ["--scope", "raw", "explore", "inventory"],
        lambda: ["explore", "inventory", "--scope", "raw"],
        lambda: ["--scope", "raw", "--scope", "staging", "explore", "inventory"],
    ],
    ids=["before-subcommand", "after-subcommand", "repeatable"],
)
def test_scope_parses_in_either_position_and_repeats(argv_builder, tmp_path, capsys):
    """`--scope` is a connection option like `--path`, so it has to work on both
    sides of the subcommand and accumulate."""

    # An explicit --connector, so this exercises scope parsing rather than the
    # no-config refusal: DuckDB has no scope, so a parsed --scope is a clean
    # refusal naming the flag, which proves argparse accepted it in this position.
    rc = main(["--repo-root", str(tmp_path), "--connector", "duckdb", *argv_builder()])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "error"
    assert "--scope" in payload["errors"][0]


def test_scope_is_accepted_on_every_subcommand():
    """A scoping flag missing from one subcommand is how `--dataset` came to be
    silently dropped in the first place."""

    from exmergo_dex_core.cli import _build_parser

    parser = _build_parser()
    for group, subcommands in COMMAND_SURFACE.items():
        for sub in subcommands:
            argv = [group, sub, "--scope", "raw"]
            if group == "explore" and sub in {"profile", "cluster"}:
                argv.append("t")
            elif group == "explore" and sub == "query":
                argv.append("select 1")
            args = parser.parse_args(argv)
            assert args.scope == ["raw"], f"{group} {sub} dropped --scope"
