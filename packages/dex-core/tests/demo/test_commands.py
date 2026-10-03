"""`dex demo` through the CLI: one envelope, two artifacts, five refusals.

Driven through `main(argv)` rather than the generator, because everything under
test here is the command's own behavior: what it wires up, what it declines to
wire up, and what it tells the caller to run next. The tests that write files run
twice through `run_from`, once standing in the target directory and once from
elsewhere with `--repo-root`, and both runs have to agree.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.demo import DEMO_FILENAME

pytest.importorskip("duckdb")


# A constant for the reason the shim keeps one: the SELECT is printed text the
# envelope carries, never a statement this module builds.
_PII_PROBE = 'dex explore query "select email from customers"'


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    assert rc == (1 if payload["status"] == "error" else 0), payload
    return payload


def _files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_demo_creates_the_warehouse_and_wires_it_up(run_from, capsys):
    payload = _run(run_from.argv("demo"), capsys)

    assert payload["status"] == "ok"
    data = payload["data"]
    # Relative to the run directory in both modes, the way every writer reports
    # the files it wrote under its root.
    assert data["path"] == DEMO_FILENAME
    assert data["object_count"] == 7
    assert data["row_count"] == 29512
    assert data["created"] == [DEMO_FILENAME, ".dex/config.yml"]
    assert (run_from.root / DEMO_FILENAME).is_file()

    # The config is what lets every printed command run with no flags at all,
    # which is the difference between a loose file and a working project.
    from exmergo_dex_core.config import load_config

    config = load_config(run_from.root)
    assert config is not None
    assert config.connector == "duckdb"
    assert config.duckdb is not None and config.duckdb.path == DEMO_FILENAME
    assert [d["op"] for d in payload["diffs"]] == ["create"]
    assert [d["path"] for d in payload["diffs"]] == [".dex/config.yml"]

    assert [step["command"] for step in data["next_steps"]] == [
        f"dex explore map{run_from.rerun}",
        f"dex explore profile order_items products{run_from.rerun}",
        f"dex explore relationships --verify{run_from.rerun}",
        _PII_PROBE + run_from.rerun,
    ]
    assert all(step["shows"] for step in data["next_steps"])


def test_demo_creates_nothing_but_what_it_reports(run_from, capsys):
    """No write-ahead log left behind, no cache, no plans directory: the two
    artifacts named in `created` are exactly the two that exist. From outside,
    `run_from` also holds the caller's own directory to nothing at all."""

    payload = _run(run_from.argv("demo"), capsys)

    assert _files_under(run_from.root) == sorted(payload["data"]["created"])


def test_repo_root_after_the_subcommand_lands_the_same(tmp_path: Path, capsys):
    """`--repo-root` is accepted on either side of `demo`, like every other
    global flag, and both spellings write into the root."""

    project = tmp_path / "project"
    project.mkdir()

    for spelling in (
        ["--repo-root", str(project), "demo"],
        ["demo", "--repo-root", str(project)],
    ):
        for leftover in project.rglob("*"):
            if leftover.is_file():
                leftover.unlink()
        payload = _run(spelling, capsys)
        assert payload["status"] == "ok", spelling
        assert _files_under(project) == [".dex/config.yml", DEMO_FILENAME]
        # The cwd is `tmp_path` itself, so this is also "nothing in the cwd".
        assert _files_under(tmp_path) == [
            "project/.dex/config.yml",
            f"project/{DEMO_FILENAME}",
        ]


def test_demo_names_no_paradigm_because_it_resolved_no_connector(capsys):
    """`free_local` is DuckDB's positive answer about a connector that was
    actually resolved, never a stand-in for having nothing to say. The demo
    opens no connection at all, so it claims nothing."""

    payload = _run(["demo"], capsys)
    assert payload["cost"] == {
        "paradigm": None,
        "estimate": None,
        "ceiling": None,
        # Both absent for the same reason `paradigm` is: nothing was priced,
        # which is a different state from priced-and-unknowable.
        "estimate_quality": None,
        "unit": None,
    }


def test_demo_takes_a_target_path(run_from, capsys):
    (run_from.root / "sandbox").mkdir()
    payload = _run(run_from.argv("demo", "sandbox/shop.duckdb"), capsys)

    assert payload["data"]["path"] == "sandbox/shop.duckdb"
    assert payload["data"]["created"] == [
        "sandbox/shop.duckdb",
        "sandbox/.dex/config.yml",
    ]
    assert (run_from.root / "sandbox" / "shop.duckdb").is_file()
    # The config lands beside the warehouse, so the commands have to name the
    # file: a bare `explore map` run from here would not find that config. The
    # name is spelled from the caller's shell, because a live --path is.
    named = f"{run_from.rerun} --path {run_from.spell('sandbox/shop.duckdb')}"
    assert all(
        step["command"].endswith(named) for step in payload["data"]["next_steps"]
    )


@pytest.mark.parametrize("with_repo_root", [False, True])
def test_an_absolute_target_is_written_where_it_names(
    tmp_path: Path, monkeypatch, capsys, with_repo_root: bool
):
    """An absolute path is the caller saying exactly where, so `--repo-root`
    moves nothing; it only travels on in the printed commands."""

    project, caller, elsewhere = (tmp_path / d for d in ("project", "caller", "here"))
    for directory in (project, caller, elsewhere):
        directory.mkdir()
    monkeypatch.chdir(caller)
    target = elsewhere / "shop.duckdb"
    prefix = ["--repo-root", str(project)] if with_repo_root else []

    payload = _run([*prefix, "demo", str(target)], capsys)

    assert payload["data"]["path"] == str(target)
    assert payload["data"]["created"] == [
        str(target),
        str(elsewhere / ".dex" / "config.yml"),
    ]
    assert _files_under(elsewhere) == [".dex/config.yml", "shop.duckdb"]
    assert _files_under(project) == []
    assert _files_under(caller) == []
    rerun = f" --repo-root {project}" if with_repo_root else ""
    assert all(
        step["command"].endswith(f"{rerun} --path {target}")
        for step in payload["data"]["next_steps"]
    )


def test_a_second_demo_refuses_rather_than_overwriting(run_from, capsys):
    _run(run_from.argv("demo"), capsys)
    before = (run_from.root / DEMO_FILENAME).read_bytes()

    payload = _run(run_from.argv("demo"), capsys)
    assert payload["status"] == "error"
    assert payload["reason"] == "guard"
    assert "already exists" in payload["errors"][0]
    assert (run_from.root / DEMO_FILENAME).read_bytes() == before


def test_confirm_cannot_buy_through_the_overwrite_refusal(tmp_path: Path, capsys):
    """Deliberately unconfirmable. A `--confirm` that could talk past this would
    put a real warehouse one typo away from being replaced."""

    _run(["demo"], capsys)
    payload = _run(["demo", "--confirm"], capsys)
    assert payload["status"] == "error"
    assert payload["reason"] == "guard"


def test_a_missing_parent_directory_is_a_clean_refusal(run_from, capsys):
    payload = _run(run_from.argv("demo", "nope/shop.duckdb"), capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    # Named as the caller's shell would find it, so the fix is where it says.
    assert (
        f"'{run_from.spell('nope')}' is not an existing directory"
        in (payload["errors"][0])
    )
    assert "creates no directories" in payload["errors"][0]
    assert not (run_from.root / "nope").exists()


def test_a_repo_root_that_does_not_exist_is_a_clean_refusal(tmp_path: Path, capsys):
    """The demo never creates directories, and the root is one."""

    missing = tmp_path / "missing"
    payload = _run(["--repo-root", str(missing), "demo"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    assert "creates no directories" in payload["errors"][0]
    assert not missing.exists()
    assert _files_under(tmp_path) == []


def test_path_is_refused_rather_than_silently_ignored(capsys):
    """`--path` names the warehouse dex reads, everywhere else. Honoring it here
    would blur the one distinction this command exists to keep sharp, and
    ignoring it would be worse: a flag accepted and dropped reads as a setting
    that took effect."""

    payload = _run(["demo", "--path", "shop.duckdb"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    assert "dex demo shop.duckdb" in payload["errors"][0]


def test_an_existing_config_above_the_target_is_left_alone(run_from, capsys):
    """A second config in a subdirectory would shadow the user's real one for
    every command run there, so the demo declines to write one and says so."""

    root = run_from.root
    (root / ".git").mkdir()
    (root / ".dex").mkdir()
    committed = root / ".dex" / "config.yml"
    committed.write_text(
        "connector: duckdb\nduckdb:\n  path: production.duckdb\n", encoding="utf-8"
    )
    (root / "scratch").mkdir()

    payload = _run(run_from.argv("demo", "scratch/shop.duckdb"), capsys)

    assert payload["status"] == "ok"
    assert payload["data"]["created"] == ["scratch/shop.duckdb"]
    assert (root / "scratch" / "shop.duckdb").is_file()
    assert not (root / "scratch" / ".dex").exists()
    assert "production.duckdb" in committed.read_text(encoding="utf-8")
    assert any("left untouched" in w for w in payload["warnings"])
    assert payload["diffs"] == []
    named = f"{run_from.rerun} --path {run_from.spell('scratch/shop.duckdb')}"
    assert all(
        step["command"].endswith(named) for step in payload["data"]["next_steps"]
    )


def test_the_generated_warehouse_drives_the_whole_explore_tour(run_from, capsys):
    """The claim the documentation makes, run end to end: every command the
    envelope prints works exactly as printed, from wherever the demo was run,
    and each one lands the finding it promises."""

    printed = [
        shlex.split(step["command"])[1:]
        for step in _run(run_from.argv("demo"), capsys)["data"]["next_steps"]
    ]
    map_argv, profile_argv, relationships_argv, query_argv = printed

    mapped = _run(map_argv, capsys)["data"]
    assert mapped["object_count"] == 7
    assert mapped["pii_column_count"] == 6
    assert mapped["relationship_count"] == 5
    # The cache the tour depends on is written under the root, beside the
    # config, which is also why the caller's own directory stays empty.
    assert set(_files_under(run_from.root / ".dex")) > {"config.yml"}

    profiled = _run(profile_argv, capsys)
    notes = [n for d in profiled["data"]["datasets"] for n in d["data_quality"]]
    assert any("order_item_id is not unique" in n for n in notes)
    assert any("mixes value shapes" in n for n in notes)

    # The shipped-artifact regression for the double-loaded batch: this table's
    # story is 1,000 duplicate order_item_ids, and it used to be told as a
    # composite grain of (unit_price, order_id), a money column paired with a
    # foreign key. Nothing is reported as a key now, and the duplicates are.
    items = next(
        d
        for d in profiled["data"]["datasets"]
        if d["identifier"].endswith("order_items")
    )
    assert items["grain"] is None
    assert items["candidate_keys"] == []
    assert not any("unit_price" in e["reason"] for e in items["key_evidence"])
    assert any(
        "1000 rows would have to be removed for it to be unique" in n
        for n in items["data_quality"]
    )

    verified = _run(relationships_argv, capsys)["data"]
    edges = {
        (r["from_dataset"].split(".")[-1], r["from_columns"][0]): r
        for r in verified["relationships"]
    }
    assert edges[("orders", "customer_id")]["orphan_fraction"] == 0.0
    assert edges[("web_events", "customer_id")]["orphan_fraction"] == 1.0
    assert any("is not evidence of a shared key" in n for n in verified["notes"])

    refused = _run(query_argv, capsys)
    assert refused["reason"] == "guard"
    assert "PII-flagged" in refused["errors"][0]
    # And the refusal is policy rather than breakage: the same table answers an
    # aggregate over the same column.
    counted = _run(
        run_from.argv(
            "explore", "query", "select count(distinct email) as n from customers"
        ),
        capsys,
    )
    assert counted["data"]["cells"] == [[1200]]
