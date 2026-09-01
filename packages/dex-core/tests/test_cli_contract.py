"""The command contract: every subcommand prints exactly one parseable envelope
with a valid status and nothing else on stdout."""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cli import COMMAND_SURFACE, main
from exmergo_dex_core.engine import DexEngine

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
                if group == "transform" and sub == "references":
                    argv.append("some_name")
                if group == "transform" and sub == "rename":
                    argv += ["column", "some_model.some_column", "renamed"]
                if group == "transform" and sub == "remove":
                    argv += ["var", "some_var"]
                if group == "transform" and sub == "place":
                    argv += [
                        "some_column",
                        "--targets",
                        "a,b",
                        "--expr",
                        "upper(x)",
                    ]
                argvs.append(argv)
        elif group == "demo":
            # The one verb that creates a file. Pointed at a directory that does
            # not exist so the contract is exercised through its clean refusal
            # rather than by seeding a warehouse into the checkout.
            argvs.append([group, "missing-dex-fixture-dir/demo.duckdb"])
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
        "connection",
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
    assert payload["connection"] == {
        "connector": "duckdb",
        "target": {"path": str(duckdb_file)},
        "source": "flag",
    }


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
    assert "DBT_PROFILES_DIR" in payload["errors"][0]


def test_help_says_dbt_profiles_dir_does_not_select_the_connection(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "DBT_PROFILES_DIR only locates dbt profiles.yml" in help_text
    assert "does not select dex's connector" in help_text


def test_committed_duckdb_target_reports_config_source(
    duckdb_file: Path, tmp_path: Path, capsys
):
    from exmergo_dex_core.config import DexConfig, DuckDBTarget, save_config

    target = duckdb_file
    save_config(
        DexConfig(connector="duckdb", duckdb=DuckDBTarget(path=target.name)),
        tmp_path,
    )

    assert main(["--repo-root", str(tmp_path), "connect", "test"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["connection"] == {
        "connector": "duckdb",
        "target": {"path": str(target)},
        "source": ".dex/config.yml",
    }


def test_a_lone_duckdb_file_in_the_run_directory_is_used_and_warned_in_the_envelope(
    capsys, tmp_path
):
    """Issue #199, end to end through the CLI: the auto-detect's warning has to
    reach the actual envelope a caller reads, not just the engine that resolved
    it, since a guess dex makes on a caller's behalf must never be silent."""

    duckdb = pytest.importorskip("duckdb")
    lone = tmp_path / "lone.duckdb"
    conn = duckdb.connect(str(lone))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()

    rc = main(["--repo-root", str(tmp_path), "explore", "inventory"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok"
    assert any("lone.duckdb" in w for w in payload["warnings"])
    assert payload["cost"]["paradigm"] == "free_local"
    assert payload["connection"] == {
        "connector": "duckdb",
        "target": {"path": str(lone)},
        "source": "directory-local inference",
    }


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
            elif group == "transform" and sub == "references":
                argv.append("some_name")
            elif group == "transform" and sub == "rename":
                argv += ["column", "m.c", "renamed"]
            elif group == "transform" and sub == "remove":
                argv += ["var", "some_var"]
            args = parser.parse_args(argv)
            assert args.scope == ["raw"], f"{group} {sub} dropped --scope"


def test_query_takes_several_statements_or_a_file():
    """`explore query`'s positional is variadic like `explore profile`'s, and zero
    of them parses so `--sql-file` can carry the batch instead. The empty call is
    refused by the shim with an envelope rather than by argparse with a usage
    string, because one envelope out is the contract."""

    from exmergo_dex_core.cli import _build_parser

    parser = _build_parser()
    assert parser.parse_args(["explore", "query", "select 1", "select 2"]).sql == [
        "select 1",
        "select 2",
    ]
    args = parser.parse_args(["explore", "query", "--sql-file", "probes.sql"])
    assert args.sql == [] and args.sql_file == "probes.sql"
    assert parser.parse_args(["explore", "query"]).sql == []


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


# --- the top-level orientation (#296) --------------------------------------------
#
# `dex --help` is where a stranger's first contact lands, and a bare argparse
# flag/subcommand dump answered none of "what do the three verbs do", "how do I
# point this at data", or "what do I run first". A bare `dex` used to spend that
# same first keystroke on a useless "the following arguments are required: group".


def test_bare_invocation_shows_the_orientation_instead_of_an_argparse_error(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "required: group" not in out
    assert "explore map" in out
    assert "dex demo" in out


def test_help_names_the_three_verbs_and_the_first_command_to_run(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for verb in ("explore", "transform", "maintain"):
        assert verb in out
    assert "dex demo" in out
    assert "dex explore map" in out


def test_every_group_carries_its_own_help_text():
    """Each group's help line renders in the top-level listing, not just its own
    --help, since that listing is the one a stranger with no prior context sees.
    `demo` already had one; #296 is every other group catching up."""

    from exmergo_dex_core.cli import _GROUP_HELP, _build_parser

    assert set(_GROUP_HELP) == set(COMMAND_SURFACE)
    # Whitespace-normalized: argparse wraps a long help string across lines.
    normalized_out = " ".join(_build_parser().format_help().split())
    for help_text in _GROUP_HELP.values():
        assert " ".join(help_text.split()) in normalized_out


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


# --- CLI/DexEngine parity (#344) --------------------------------------------
#
# `DexEngine` and the CLI are two independent surfaces over the same
# implementation (a `cmd_*` handler and its `DexEngine` sibling both call the
# same module-level function), and nothing else keeps them in step: each is
# tested through its own entry point, so a capability that exists on one and
# not the other passes both suites. `DexEngine.check()` was exactly that until
# this test existed, hard-coding no object scope while all four sibling
# detectors accepted one.
#
# A naive comparison of argparse dests against method parameters reports mostly
# false positives, because the translation between the two surfaces is
# deliberate: one generic `argument` positional is reused across several
# subcommands and means a different keyword on each; a negating flag pair
# collapses into one tri-state parameter; a `--edits-file`/`--sql-file` path
# becomes parsed content, because a library caller holds data rather than a
# path; and one subcommand can fan out to more than one method
# (`explore query` to `query`/`query_batch` by statement count, `explore
# semantic` to `semantic_list`/`semantic_values`/`semantic_query` by `mode`).
# `_TRANSLATED` marks a dest as accounted for without asserting an exact
# keyword match, for exactly these cases.
_TRANSLATED = object()

# (group, subcommand) -> either a CLI-only reason, or the method(s) it wraps
# plus every one of its own argparse dests (excluding the shared connection
# options every subcommand takes) mapped to the `DexEngine` keyword it
# expresses, or `_TRANSLATED`.
_SUBCOMMAND_PARITY: dict[tuple[str, str | None], dict] = {
    ("connect", "test"): {"method": "connect_test", "args": {}},
    ("explore", "inventory"): {
        "method": "inventory",
        "args": {"rank": "rank", "limit": "limit", "all": "show_all"},
    },
    ("explore", "profile"): {
        "method": "profile",
        "args": {
            "objects": "objects",
            "refresh": "refresh",
            "use_project": "use_project",
            "check_cumulative": "check_cumulative",
        },
    },
    ("explore", "relationships"): {
        "method": "relationships",
        "args": {
            "verify": "verify",
            "infer_by_overlap": "infer_by_overlap",
            "refresh": "refresh",
            "use_project": "use_project",
        },
    },
    ("explore", "map"): {
        "method": "map",
        "args": {
            "full": "full",
            "detail": "detail",
            "verify": "verify",
            "infer_by_overlap": "infer_by_overlap",
            "refresh": "refresh",
            "use_project": "use_project",
        },
    },
    ("explore", "diagram"): {"method": "diagram", "args": {"full": "full"}},
    ("explore", "query"): {
        "method": ("query", "query_batch"),
        "args": {
            "sql": _TRANSLATED,
            "sql_file": _TRANSLATED,
            "no_auto_profile": _TRANSLATED,
        },
    },
    ("explore", "cluster"): {
        "method": "cluster",
        "args": {
            "object": "obj",
            "features": "features",
            "k": "k",
            "no_auto_profile": _TRANSLATED,
        },
    },
    ("explore", "semantic"): {
        "method": ("semantic_list", "semantic_values", "semantic_query"),
        "args": dict.fromkeys(
            [
                "mode",
                "metrics",
                "metric",
                "for_dimension",
                "search",
                "full",
                "group_by",
                "where",
                "order_by",
                "grain",
                "limit",
                "local",
                "api",
            ],
            _TRANSLATED,
        ),
    },
    ("transform", "init"): {
        "method": "init_project",
        "args": {
            "argument": _TRANSLATED,
            "layered_schemas": "layered_schemas",
            "in_place": "in_place",
        },
    },
    ("transform", "plan"): {
        "method": "plan",
        "args": {
            "argument": _TRANSLATED,
            "edits_file": _TRANSLATED,
            "scaffold": "scaffold",
            "attribute_rows": "attribute_rows",
        },
    },
    ("transform", "apply"): {"method": "apply", "args": {"argument": _TRANSLATED}},
    ("transform", "build"): {
        "method": "build",
        "args": {"target": "target", "select": "select"},
    },
    ("transform", "deps"): {"method": "deps", "args": {}},
    ("transform", "plans"): {"method": "plans", "args": {}},
    ("transform", "macro"): {"method": "macro", "args": {"argument": _TRANSLATED}},
    ("transform", "references"): {
        "method": "references",
        "args": {"names": "names", "kind": "kind", "full": "full"},
    },
    ("transform", "rename"): {
        "method": "rename",
        "args": {
            "kind": "kind",
            "old": "old",
            "new": "new",
            "edits_file": "edits_file",
        },
    },
    ("transform", "remove"): {
        "method": "remove",
        "args": {"kind": "kind", "name": "name", "edits_file": "edits_file"},
    },
    ("transform", "place"): {
        "method": "place",
        "args": {
            "argument": _TRANSLATED,
            "targets": "targets",
            "expr": "expression",
            "explain": "explain",
        },
    },
    ("transform", "test"): {
        "reason": (
            "scaffold-only; reachable as "
            "exmergo_dex_core.transform.test_scaffold.test_scaffold(engine, "
            "scaffold), not a DexEngine method"
        ),
    },
    ("semantic", "define"): {
        "method": "semantic_define",
        "args": {
            "argument": _TRANSLATED,
            "edits_file": _TRANSLATED,
            "no_parse": "no_parse",
        },
    },
    ("semantic", "update"): {
        "method": "semantic_update",
        "args": {
            "argument": _TRANSLATED,
            "edits_file": _TRANSLATED,
            "no_parse": "no_parse",
        },
    },
    ("semantic", "plan"): {
        "method": "semantic_plan",
        "args": {
            "argument": _TRANSLATED,
            "edits_file": _TRANSLATED,
            "no_parse": "no_parse",
        },
    },
    ("maintain", "snapshot"): {"method": "snapshot", "args": {}},
    ("maintain", "check"): {"method": "check", "args": {"objects": "objects"}},
    ("maintain", "schema"): {"method": "schema_drift", "args": {"objects": "objects"}},
    ("maintain", "volume"): {"method": "volume_drift", "args": {"objects": "objects"}},
    ("maintain", "grain"): {"method": "grain_drift", "args": {"objects": "objects"}},
    ("maintain", "semantic"): {
        "method": "semantic_drift",
        "args": {"objects": "objects"},
    },
    ("maintain", "reconcile"): {
        "method": "reconcile",
        "args": {"drift_class": "drift_class"},
    },
    ("viz", "preview"): {
        "reason": (
            "not yet implemented; returns a not_implemented envelope until the "
            "Viz integration lands"
        ),
    },
    ("demo", None): {
        "reason": (
            "creates a warehouse file on disk via demo/warehouse.py; not a "
            "command a library caller drives through DexEngine"
        ),
    },
}

# Dests every subcommand inherits from `_sub_connection_options()`, plus
# argparse's own `-h`/`--help`: engine-construction concerns, not per-command
# capability, so they are excluded from the per-subcommand comparison.
_CONNECTION_DESTS = {
    "connector",
    "path",
    "scope",
    "project",
    "dataset",
    "repo_root",
    "confirm",
    "budget",
    "help",
}


def _find_subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError(f"no subparsers action on {parser.prog}")


def _subparser(
    parser: argparse.ArgumentParser, group: str, sub: str | None
) -> argparse.ArgumentParser:
    gp = _find_subparsers_action(parser).choices[group]
    if sub is None:
        return gp
    return _find_subparsers_action(gp).choices[sub]


def _own_dests(sp: argparse.ArgumentParser) -> set[str]:
    return {a.dest for a in sp._actions if a.dest not in _CONNECTION_DESTS}


def _all_group_subcommand_pairs() -> list[tuple[str, str | None]]:
    return [
        (group, sub)
        for group, subcommands in COMMAND_SURFACE.items()
        for sub in (subcommands or [None])
    ]


@pytest.mark.parametrize(
    "pair", _all_group_subcommand_pairs(), ids=lambda p: f"{p[0]} {p[1] or ''}".strip()
)
def test_every_subcommand_is_accounted_for_in_the_parity_map(pair):
    """A subcommand missing from `_SUBCOMMAND_PARITY` is exactly the failure
    mode #344 describes: added to the CLI, and nothing notices it was never
    given (or deliberately denied) a `DexEngine` equivalent."""

    assert pair in _SUBCOMMAND_PARITY, (
        f"{pair} has no entry in _SUBCOMMAND_PARITY: add a `DexEngine` method "
        "for it, or allowlist it there with a reason"
    )


@pytest.mark.parametrize(
    "pair", list(_SUBCOMMAND_PARITY), ids=lambda p: f"{p[0]} {p[1] or ''}".strip()
)
def test_every_mapped_subcommand_has_a_real_method_with_the_stated_capability(pair):
    spec = _SUBCOMMAND_PARITY[pair]
    if "reason" in spec:
        assert spec["reason"]
        return

    methods = spec["method"]
    methods = (methods,) if isinstance(methods, str) else methods
    signatures = []
    for name in methods:
        assert hasattr(DexEngine, name), f"DexEngine has no method {name!r} for {pair}"
        signatures.append(inspect.signature(getattr(DexEngine, name)))

    parser = _build_parser_for_test()
    actual_dests = _own_dests(_subparser(parser, pair[0], pair[1]))
    mapped_dests = set(spec["args"])
    assert actual_dests == mapped_dests, (
        f"{pair}: argparse has {actual_dests}, _SUBCOMMAND_PARITY maps "
        f"{mapped_dests}. A CLI flag with no entry could be silently dropped "
        "by every DexEngine method behind this subcommand"
    )

    for dest, kwarg in spec["args"].items():
        if kwarg is _TRANSLATED:
            continue
        assert any(kwarg in sig.parameters for sig in signatures), (
            f"{pair}: dest {dest!r} maps to {kwarg!r}, which is not a parameter "
            f"of {methods}"
        )


def _build_parser_for_test() -> argparse.ArgumentParser:
    from exmergo_dex_core.cli import _build_parser

    return _build_parser()


def test_maintain_check_accepts_an_object_scope_like_its_sibling_detectors():
    """The one concrete gap #344 named: `maintain check <objects>` reached the
    CLI, and `DexEngine.check()` dropped it on the floor rather than passing it
    through like `schema_drift`/`volume_drift`/`grain_drift`/`semantic_drift`."""

    assert "objects" in inspect.signature(DexEngine.check).parameters
