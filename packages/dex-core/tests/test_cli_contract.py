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
    assert set(payload) == {
        "status",
        "data",
        "cost",
        "warnings",
        "diffs",
        "errors",
        "reason",
    }
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


# --- selecting the storage backend ------------------------------------------------


def test_a_configured_backend_carries_state_from_one_command_to_the_next(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """The point of a CLI selector: a backend dex does not ship, driving a real
    multi-step flow.

    The two commands are the whole test. `explore query` resolves every table
    reference against the cache `explore map` wrote, so the second one can only
    succeed if state actually reached the selected backend and came back out of
    it. A backend that quietly did nothing would fail here and nowhere else.
    """

    from exmergo_dex_core.config import CacheConfig, DexConfig, save_config

    from .fakes.stores import TenantStore

    TenantStore.reset()
    save_config(
        DexConfig(
            connector="duckdb",
            cache=CacheConfig(
                backend="tests.fakes.stores:tenant_store",
                options={"tenant": "acme"},
            ),
        ),
        tmp_path,
    )
    base = ["--repo-root", str(tmp_path), "--path", str(duckdb_file)]

    assert main([*base, "explore", "map"]) == 0
    mapped = json.loads(capsys.readouterr().out)
    assert mapped["status"] == "ok"
    # The locator an agent surfaces is the selected backend's, not a path.
    assert mapped["data"]["cache_path"].startswith("tenant://acme/")

    assert main([*base, "explore", "query", "select count(*) as n from customers"]) == 0
    queried = json.loads(capsys.readouterr().out)
    assert queried["status"] == "ok"
    assert queried["data"]["cells"] == [[2]]

    # And the filesystem backend was genuinely not used: only the config file the
    # test wrote is under `.dex/`.
    assert [p.name for p in (tmp_path / ".dex").iterdir()] == ["config.yml"]
    TenantStore.reset()


def test_the_cache_backend_flag_overrides_the_configured_name(
    duckdb_file: Path, tmp_path: Path, capsys
):
    from .fakes.stores import TenantStore

    TenantStore.reset()
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--path",
            str(duckdb_file),
            "--cache-backend",
            "tests.fakes.stores:ContextBuiltStore",
            "explore",
            "map",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    # No config file at all, so options carry no tenant and the class-shaped
    # factory raises on the missing key: what matters here is that the flag was
    # honored rather than ignored, and that it failed as one envelope.
    assert rc == 1
    assert payload["status"] == "error"
    assert "failed to build" in payload["errors"][0]
    TenantStore.reset()


def test_selecting_the_memory_backend_refuses_by_naming_the_process_boundary(
    tmp_path: Path, capsys
):
    """A `MemoryStore` behind the CLI would drop the cache between commands and
    make the tool look broken. The refusal has to explain that rather than let
    someone debug it."""

    rc = main(
        ["--repo-root", str(tmp_path), "--cache-backend", "memory", "connect", "test"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "error"
    assert "its own process" in payload["errors"][0]


def test_an_unresolvable_backend_still_emits_exactly_one_envelope(
    tmp_path: Path, capsys
):
    """The engine is built before any command runs, and it can refuse. Every agent
    wrapper reads exactly one envelope from stdout, so a refusal there has to be
    rendered like any other rather than escaping as a traceback."""

    rc = main(
        ["--repo-root", str(tmp_path), "--cache-backend", "nope", "connect", "test"]
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert rc == 1
    assert payload["status"] == "error"
    assert "unknown cache backend" in payload["errors"][0]


# --- the cost paradigm every envelope carries ------------------------------------
#
# `cost.paradigm` names the connector the command ran against, so a caller
# reading a free command still learns what a billed one will cost in. It is
# absent when nothing selected a connector. `free_local` is DuckDB's own answer
# and never a stand-in for having nothing to say, which is the distinction a
# host branching on this field to ask "was this refusal about money?" depends on.


def test_a_duckdb_command_reports_free_local(duckdb_file: Path, capsys):
    assert main(["connect", "test", "--path", str(duckdb_file)]) == 0
    assert json.loads(capsys.readouterr().out)["cost"]["paradigm"] == "free_local"


def test_a_repo_only_command_reports_the_configured_connector(tmp_path: Path, capsys):
    """`transform plans` reads the plan store and touches no warehouse, but the
    repo is configured for BigQuery, so the next billed command bills in bytes
    and the envelope says so."""

    from exmergo_dex_core.config import DexConfig, save_config

    save_config(DexConfig(connector="bigquery"), tmp_path)
    assert main(["--repo-root", str(tmp_path), "transform", "plans"]) == 0
    assert json.loads(capsys.readouterr().out)["cost"]["paradigm"] == "bytes_scanned"


def test_a_command_with_no_connector_claims_no_paradigm(tmp_path: Path, capsys):
    # No config, no --connector, no --path: nothing chose a connector, so there
    # is no paradigm to report and the envelope must not invent one.
    assert main(["--repo-root", str(tmp_path), "connect", "test"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["cost"]["paradigm"] is None


def test_a_refusal_before_the_engine_exists_reports_the_flagged_connector(
    tmp_path: Path, capsys
):
    # The store refuses while the engine is being built, so there is no engine
    # to ask. The flag is the only evidence of a connector left, and it is still
    # better than telling a host the failed BigQuery run was free and local.
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "--cache-backend",
            "nope",
            "connect",
            "test",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["cost"]["paradigm"] == "bytes_scanned"
