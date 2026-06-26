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
    """Every (group, subcommand) pair in the surface, as argv lists."""

    argvs: list[list[str]] = []
    for group, subcommands in COMMAND_SURFACE.items():
        if subcommands:
            argvs.extend([group, sub] for sub in subcommands)
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
    assert main(["explore", "inventory"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_implemented"


def test_connect_test_against_duckdb_is_ok(duckdb_file: Path, capsys):
    rc = main(["--path", str(duckdb_file), "connect", "test"])
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
