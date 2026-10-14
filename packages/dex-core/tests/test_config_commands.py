"""`dex config get|validate|set|unset` through the CLI: one envelope each, and a
file that changes only when a person has seen the diff and confirmed it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

CONFIG = """\
# dex settings for this repo. Comments carry the reasoning.
connector: duckdb

budget:
  ceiling: 2000000000   # 2 GB per command
  session_celing: 5     # a typo the loader used to drop without a word

duckdb:
  path: demo.duckdb

query:
  max_rows: 50

cluster:
  sample_seed: null     # a fresh draw per run, on purpose
"""


def _repo(root: Path, text: str = CONFIG) -> Path:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    path = root / ".dex" / "config.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


# --- validate ---------------------------------------------------------------------


def test_validate_passes_a_clean_file(tmp_path, capsys):
    _repo(tmp_path, "connector: duckdb\nduckdb:\n  path: demo.duckdb\n")
    rc, envelope = _run(capsys, tmp_path, "config", "validate")
    assert (rc, envelope["status"]) == (0, "ok")
    assert envelope["data"]["valid"] is True
    assert envelope["data"]["problems"] == []


def test_validate_fails_on_an_unknown_key_so_it_can_gate_ci(tmp_path, capsys):
    _repo(tmp_path)
    rc, envelope = _run(capsys, tmp_path, "config", "validate")
    assert (rc, envelope["status"], envelope["reason"]) == (1, "error", "configuration")
    assert envelope["errors"] == [
        "line 6: budget.session_celing is not a setting dex reads, so it is "
        "ignored; did you mean budget.session_ceiling?"
    ]
    (problem,) = envelope["data"]["problems"]
    # Reported, but not one that stops other commands.
    assert problem["fatal"] is False


def test_validate_names_a_dbt_project_dir_that_holds_no_project(tmp_path, capsys):
    _repo(tmp_path, "connector: duckdb\ndbt_project_dir: no_such_dir\n")
    rc, envelope = _run(capsys, tmp_path, "config", "validate")
    assert rc == 1
    (problem,) = envelope["data"]["problems"]
    assert problem == {
        "kind": "project_not_found",
        "key": "dbt_project_dir",
        "line": 2,
        "message": "names no_such_dir, which holds no dbt_project.yml",
        "suggestion": None,
        "fatal": False,
    }


def test_validate_marks_a_problem_that_stops_every_command(tmp_path, capsys):
    _repo(tmp_path, "query:\n  max_rows: lots\n")
    rc, envelope = _run(capsys, tmp_path, "config", "validate")
    assert rc == 1
    (problem,) = envelope["data"]["problems"]
    assert (problem["key"], problem["line"], problem["fatal"]) == (
        "query.max_rows",
        2,
        True,
    )


# --- get --------------------------------------------------------------------------


def test_get_reports_each_value_with_its_source_and_line(tmp_path, capsys):
    _repo(tmp_path)
    rc, envelope = _run(capsys, tmp_path, "config", "get", "budget")
    assert (rc, envelope["status"]) == (0, "ok")
    rows = {row["key"]: row for row in envelope["data"]["settings"]}
    assert rows["budget.ceiling"] == {
        "key": "budget.ceiling",
        "source": "file",
        "value": 2_000_000_000,
        "line": 5,
    }
    assert rows["budget.session_ceiling"]["source"] == "default"
    # The typo does not stop a read, and it is not silent either.
    assert any("budget.session_celing" in w for w in envelope["warnings"])


def test_get_with_no_key_reports_every_setting(tmp_path, capsys):
    _repo(tmp_path)
    _rc, envelope = _run(capsys, tmp_path, "config", "get")
    rows = {row["key"]: row for row in envelope["data"]["settings"]}
    assert rows["cluster.sample_seed"] == {
        "key": "cluster.sample_seed",
        "source": "file",
        "value": None,
        "line": 15,
    }
    assert rows["profile_top_n"]["source"] == "default"


def test_get_refuses_a_key_dex_does_not_read(tmp_path, capsys):
    _repo(tmp_path)
    rc, envelope = _run(capsys, tmp_path, "config", "get", "budget.celing")
    assert (rc, envelope["reason"]) == (1, "request")
    assert "did you mean budget.ceiling" in envelope["errors"][0]


def test_get_refuses_a_file_no_command_could_load_and_names_why(tmp_path, capsys):
    _repo(tmp_path, "query:\n  max_rows: lots\n")
    rc, envelope = _run(capsys, tmp_path, "config", "get")
    assert (rc, envelope["reason"]) == (1, "configuration")
    assert envelope["data"]["problems"][0]["key"] == "query.max_rows"


@pytest.mark.parametrize(
    "argv",
    [
        ["get"],
        ["validate"],
        ["set", "connector", "duckdb", "--confirm"],
        ["unset", "connector", "--confirm"],
    ],
    ids=["get", "validate", "set", "unset"],
)
def test_every_verb_refuses_where_there_is_no_config_and_creates_none(
    tmp_path, capsys, argv
):
    rc, envelope = _run(capsys, tmp_path, "config", *argv)
    assert (rc, envelope["reason"]) == (1, "configuration")
    assert "dex transform init" in envelope["errors"][0]
    assert list(tmp_path.iterdir()) == []


def test_the_file_is_found_from_a_subdirectory(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    _repo(tmp_path)
    (tmp_path / "models" / "staging").mkdir(parents=True)
    rc, envelope = _run(
        capsys, tmp_path / "models" / "staging", "config", "get", "query.max_rows"
    )
    assert rc == 0
    assert envelope["data"]["settings"][0]["value"] == 50
    assert envelope["data"]["path"] == str(tmp_path / ".dex" / "config.yml")


# --- set --------------------------------------------------------------------------


def test_an_unconfirmed_set_shows_the_diff_and_writes_nothing(tmp_path, capsys):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(capsys, tmp_path, "config", "set", "query.max_rows", "5")
    assert (rc, envelope["status"]) == (0, "needs_confirmation")
    assert envelope["data"]["written"] is False
    assert envelope["data"]["changes"] == [
        {"key": "query.max_rows", "after": 5, "before": 50}
    ]
    (diff,) = envelope["diffs"]
    assert (diff["path"], diff["op"]) == (".dex/config.yml", "update")
    assert (diff["additions"], diff["deletions"]) == (1, 1)
    assert "--confirm" in envelope["data"]["hint"]
    assert path.read_bytes() == before


def test_a_confirmed_set_writes_that_line_and_keeps_every_other(tmp_path, capsys):
    path = _repo(tmp_path)
    rc, envelope = _run(
        capsys, tmp_path, "config", "set", "query.max_rows", "5", "--confirm"
    )
    assert (rc, envelope["status"], envelope["data"]["written"]) == (0, "ok", True)
    assert path.read_text(encoding="utf-8") == CONFIG.replace(
        "max_rows: 50", "max_rows: 5"
    )
    rc, envelope = _run(
        capsys, tmp_path, "config", "set", "query.max_rows", "5", "--confirm"
    )
    assert envelope["data"]["changed"] is False
    assert envelope["diffs"] == []


def test_settings_that_are_only_valid_together_are_set_together(tmp_path, capsys):
    _repo(tmp_path, "connector: duckdb\n")
    rc, envelope = _run(capsys, tmp_path, "config", "set", "semantic.vendor", "ossie")
    assert (rc, envelope["reason"]) == (1, "request")
    assert envelope["data"]["problems"][0]["key"] == "semantic"

    rc, envelope = _run(
        capsys,
        tmp_path,
        "config",
        "set",
        "semantic.vendor",
        "ossie",
        "semantic.ossie.files",
        "[semantics/commerce.ossie.yaml]",
    )
    assert (rc, envelope["status"]) == (0, "needs_confirmation")
    assert [c["key"] for c in envelope["data"]["changes"]] == [
        "semantic.vendor",
        "semantic.ossie.files",
    ]


def test_a_set_that_would_break_the_file_is_refused_and_writes_nothing(
    tmp_path, capsys
):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(
        capsys, tmp_path, "config", "set", "query.max_rows", "lots", "--confirm"
    )
    assert (rc, envelope["reason"]) == (1, "request")
    assert "nothing was changed" in envelope["errors"][0]
    assert path.read_bytes() == before


def test_a_broken_file_can_be_repaired_one_key_at_a_time(tmp_path, capsys):
    # Two fatal problems and a store backend that does not exist: every other
    # command refuses to start, which is exactly when this has to work.
    path = _repo(
        tmp_path,
        "connector: duckdb\ncache:\n  backend: nope\nquery:\n  max_rows: lots\n"
        "  timeout_seconds: soon\n",
    )
    rc, envelope = _run(
        capsys, tmp_path, "config", "set", "query.max_rows", "5", "--confirm"
    )
    assert (rc, envelope["status"]) == (0, "ok")
    # The problem it did not fix is reported, not a reason to refuse the fix.
    assert any("query.timeout_seconds" in w for w in envelope["warnings"])
    assert "max_rows: 5" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "pairs",
    [[], ["query.max_rows"], ["query.max_rows", "5", "query.max_rows", "6"]],
    ids=["none", "odd", "repeated"],
)
def test_set_takes_distinct_key_value_pairs(tmp_path, capsys, pairs):
    _repo(tmp_path)
    rc, envelope = _run(capsys, tmp_path, "config", "set", *pairs)
    assert (rc, envelope["reason"]) == (1, "request")


@pytest.mark.parametrize(
    "flag",
    [
        ["--connector", "bigquery"],
        ["--path", "other.duckdb"],
        ["--scope", "raw"],
        ["--project", "p"],
        ["--dataset", "d"],
        ["--budget", "5"],
        ["--cache-backend", "sqlite"],
        ["--project-format", "dbt"],
        ["--session-ceiling", "5"],
        ["--no-session-ceiling"],
    ],
    ids=lambda flag: flag[0],
)
def test_a_run_override_is_refused_rather_than_silently_dropped(tmp_path, capsys, flag):
    path = _repo(tmp_path)
    before = path.read_bytes()
    # Before the group, where every one of them parses (two exist nowhere else).
    rc, envelope = _run(capsys, tmp_path, *flag, "config", "get")
    assert (rc, envelope["reason"]) == (1, "request")
    assert flag[0] in envelope["errors"][0]
    assert "dex config set" in envelope["errors"][0]
    assert path.read_bytes() == before


def test_a_set_replacing_a_committed_credential_withholds_it_and_still_answers(
    tmp_path, capsys
):
    # The old value is a block holding a credential: reported as a dict, it
    # would trip the boundary scan after the write and end in a traceback.
    path = _repo(
        tmp_path,
        "connector: duckdb\ncache:\n  options:\n    api_token: hunter2\n"
        "    region: eu\n",
    )
    main(
        ["--repo-root", str(tmp_path), "config", "set", "cache.options", "{region: us}"]
    )
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert "hunter2" not in out
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["changes"] == [
        {"key": "cache.options.api_token", "withheld": True},
        {"key": "cache.options.region", "before": "eu", "after": "us"},
    ]
    assert "hunter2" in path.read_text(encoding="utf-8")


# --- unset ------------------------------------------------------------------------


def test_an_unconfirmed_unset_shows_the_diff_and_writes_nothing(tmp_path, capsys):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(capsys, tmp_path, "config", "unset", "budget.session_celing")
    assert (rc, envelope["status"]) == (0, "needs_confirmation")
    assert envelope["data"]["written"] is False
    assert envelope["data"]["changes"] == [
        {"key": "budget.session_celing", "before": 5}
    ]
    (diff,) = envelope["diffs"]
    assert (diff["additions"], diff["deletions"]) == (0, 1)
    assert "--confirm" in envelope["data"]["hint"]
    assert path.read_bytes() == before


def test_a_confirmed_unset_removes_that_line_and_the_warning_stops(tmp_path, capsys):
    path = _repo(tmp_path)
    rc, envelope = _run(
        capsys, tmp_path, "config", "unset", "budget.session_celing", "--confirm"
    )
    assert (rc, envelope["status"], envelope["data"]["written"]) == (0, "ok", True)
    assert envelope["warnings"] == []
    assert path.read_text(encoding="utf-8") == CONFIG.replace(
        "  session_celing: 5     # a typo the loader used to drop without a word\n", ""
    )
    rc, envelope = _run(capsys, tmp_path, "config", "validate")
    assert (rc, envelope["data"]["valid"]) == (0, True)


def test_unsetting_the_last_key_of_a_block_removes_the_block(tmp_path, capsys):
    path = _repo(tmp_path)
    rc, envelope = _run(
        capsys, tmp_path, "config", "unset", "cluster.sample_seed", "--confirm"
    )
    assert rc == 0
    assert envelope["data"]["notes"] == [
        "cluster had no other settings, so it is removed too"
    ]
    assert path.read_text(encoding="utf-8") == CONFIG.replace(
        "\ncluster:\n  sample_seed: null     # a fresh draw per run, on purpose\n", ""
    )


def test_several_keys_and_whole_blocks_are_one_change(tmp_path, capsys):
    path = _repo(tmp_path)
    rc, envelope = _run(
        capsys, tmp_path, "config", "unset", "budget", "query.max_rows", "--confirm"
    )
    assert rc == 0
    assert [c["key"] for c in envelope["data"]["changes"]] == [
        "budget.ceiling",
        "budget.session_celing",
        "query.max_rows",
    ]
    assert path.read_text(encoding="utf-8") == (
        "# dex settings for this repo. Comments carry the reasoning.\n"
        "connector: duckdb\n\nduckdb:\n  path: demo.duckdb\n\n"
        "cluster:\n  sample_seed: null     # a fresh draw per run, on purpose\n"
    )


def test_unsetting_a_key_the_file_does_not_write_names_the_nearest(tmp_path, capsys):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(capsys, tmp_path, "config", "unset", "budget.session_celling")
    assert (rc, envelope["status"], envelope["data"]["changed"]) == (0, "ok", False)
    assert envelope["data"]["notes"] == [
        "budget.session_celling is not in .dex/config.yml, so there is nothing to "
        "remove; did you mean budget.session_celing?"
    ]
    assert envelope["diffs"] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("keys", "match"),
    [
        ([], "one or more keys"),
        (["query.max_rows", "query.max_rows"], "more than once"),
        (["budget", "budget.ceiling"], "inside another key"),
        (["budget..ceiling"], "not a setting key"),
        (["pii_overrides[0].reasn"], "dex config set pii_overrides"),
    ],
    ids=["none", "repeated", "nested", "empty-segment", "list-item"],
)
def test_unset_refuses_keys_it_cannot_remove_as_named(tmp_path, capsys, keys, match):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(capsys, tmp_path, "config", "unset", *keys, "--confirm")
    assert (rc, envelope["reason"]) == (1, "request")
    assert match in envelope["errors"][0]
    assert path.read_bytes() == before


def test_an_unset_that_would_break_the_file_is_refused(tmp_path, capsys):
    text = (
        "connector: duckdb\nsemantic:\n  vendor: ossie\n  ossie:\n"
        "    files: [semantics/commerce.ossie.yaml]\n"
    )
    path = _repo(tmp_path, text)
    rc, envelope = _run(
        capsys, tmp_path, "config", "unset", "semantic.ossie.files", "--confirm"
    )
    assert (rc, envelope["reason"]) == (1, "request")
    assert "nothing was changed" in envelope["errors"][0]
    assert envelope["data"]["problems"][0]["key"] == "semantic"
    assert path.read_text(encoding="utf-8") == text


def test_unset_repairs_a_file_every_other_command_refuses(tmp_path, capsys):
    path = _repo(tmp_path, "connector: duckdb\nquery:\n  max_rows: lots\n")
    rc, envelope = _run(
        capsys, tmp_path, "config", "unset", "query.max_rows", "--confirm"
    )
    assert (rc, envelope["status"]) == (0, "ok")
    assert path.read_text(encoding="utf-8") == "connector: duckdb\n"


def test_a_run_override_is_refused_on_unset_too(tmp_path, capsys):
    path = _repo(tmp_path)
    before = path.read_bytes()
    rc, envelope = _run(
        capsys,
        tmp_path,
        "--budget",
        "5",
        "config",
        "unset",
        "budget.session_celing",
        "--confirm",
    )
    assert (rc, envelope["reason"]) == (1, "request")
    assert "--budget" in envelope["errors"][0]
    assert path.read_bytes() == before


def test_a_bare_unset_is_the_config_verb(tmp_path, capsys):
    _repo(tmp_path)
    rc, envelope = _run(capsys, tmp_path, "unset", "budget.session_celing")
    assert (rc, envelope["status"]) == (0, "needs_confirmation")


def test_a_bare_validate_is_the_config_verb(tmp_path, capsys):
    _repo(tmp_path, "connector: duckdb\n")
    rc, envelope = _run(capsys, tmp_path, "validate")
    assert (rc, envelope["data"]["valid"]) == (0, True)


def test_the_config_verbs_need_no_dialect_engine_or_connector(tmp_path):
    """The lightest install has neither, and it is the one a broken config most
    needs these verbs on."""

    path = _repo(tmp_path)
    probe = (
        "import sys, json; "
        "[sys.modules.__setitem__(m, None) for m in "
        "('sqlglot', 'dbt', 'duckdb', 'metricflow', 'google.cloud.bigquery', "
        "'snowflake.connector')]; "
        "from exmergo_dex_core.cli import main; "
        "root = sys.argv[1]; "
        "codes = [main(['--repo-root', root, 'config', *a]) for a in "
        "(['validate'], ['get', 'query.max_rows'], "
        "['set', 'query.max_rows', '5', '--confirm'], "
        "['unset', 'budget.session_celing', '--confirm'])]; "
        "print(json.dumps(codes))"
    )
    completed = subprocess.run(  # noqa: S603 (fixed interpreter and authored args)
        [sys.executable, "-c", probe, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    envelopes = [json.loads(line) for line in lines[:-1]]
    assert [e["status"] for e in envelopes] == ["error", "ok", "ok", "ok"]
    assert envelopes[0]["data"]["problems"][0]["kind"] == "unknown_key"
    assert json.loads(lines[-1]) == [1, 0, 0, 0]
    written = path.read_text(encoding="utf-8")
    assert "max_rows: 5" in written
    assert "session_celing" not in written
