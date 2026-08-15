"""`dex demo` through the CLI: one envelope, two artifacts, four refusals.

Driven through `main(argv)` rather than the generator, because everything under
test here is the command's own behavior: what it wires up, what it declines to
wire up, and what it tells the caller to run next.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.demo import DEMO_FILENAME

pytest.importorskip("duckdb")


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    assert rc == (1 if payload["status"] == "error" else 0), payload
    return payload


def test_demo_creates_the_warehouse_and_wires_it_up(tmp_path: Path, capsys):
    payload = _run(["demo"], capsys)

    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["path"] == DEMO_FILENAME
    assert data["object_count"] == 7
    assert data["row_count"] == 29512
    assert data["created"] == [DEMO_FILENAME, ".dex/config.yml"]
    assert (tmp_path / DEMO_FILENAME).is_file()

    # The config is what lets every printed command run with no flags at all,
    # which is the difference between a loose file and a working project.
    from exmergo_dex_core.config import load_config

    config = load_config(tmp_path)
    assert config is not None
    assert config.connector == "duckdb"
    assert config.duckdb is not None and config.duckdb.path == DEMO_FILENAME
    assert [d["op"] for d in payload["diffs"]] == ["create"]

    assert [step["command"] for step in data["next_steps"]] == [
        "dex explore map",
        "dex explore profile order_items products",
        "dex explore relationships --verify",
        'dex explore query "select email from customers"',
    ]
    assert all(step["shows"] for step in data["next_steps"])


def test_demo_creates_nothing_but_what_it_reports(tmp_path: Path, capsys):
    """No write-ahead log left behind, no cache, no plans directory: the two
    artifacts named in `created` are exactly the two that exist."""

    payload = _run(["demo"], capsys)

    on_disk = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    )
    assert on_disk == sorted(payload["data"]["created"])


def test_demo_names_no_paradigm_because_it_resolved_no_connector(capsys):
    """`free_local` is DuckDB's positive answer about a connector that was
    actually resolved, never a stand-in for having nothing to say. The demo
    opens no connection at all, so it claims nothing."""

    payload = _run(["demo"], capsys)
    assert payload["cost"] == {"paradigm": None, "estimate": None, "ceiling": None}


def test_demo_takes_a_target_path(tmp_path: Path, capsys):
    (tmp_path / "sandbox").mkdir()
    payload = _run(["demo", "sandbox/shop.duckdb"], capsys)

    assert payload["data"]["path"] == "sandbox/shop.duckdb"
    assert (tmp_path / "sandbox" / "shop.duckdb").is_file()
    # The config lands beside the warehouse, so the commands have to name the
    # file: a bare `explore map` run from here would not find that config.
    assert all(
        "--path sandbox/shop.duckdb" in step["command"]
        for step in payload["data"]["next_steps"]
    )


def test_a_second_demo_refuses_rather_than_overwriting(tmp_path: Path, capsys):
    _run(["demo"], capsys)
    before = (tmp_path / DEMO_FILENAME).read_bytes()

    payload = _run(["demo"], capsys)
    assert payload["status"] == "error"
    assert payload["reason"] == "guard"
    assert "already exists" in payload["errors"][0]
    assert (tmp_path / DEMO_FILENAME).read_bytes() == before


def test_confirm_cannot_buy_through_the_overwrite_refusal(tmp_path: Path, capsys):
    """Deliberately unconfirmable. A `--confirm` that could talk past this would
    put a real warehouse one typo away from being replaced."""

    _run(["demo"], capsys)
    payload = _run(["demo", "--confirm"], capsys)
    assert payload["status"] == "error"
    assert payload["reason"] == "guard"


def test_a_missing_parent_directory_is_a_clean_refusal(tmp_path: Path, capsys):
    payload = _run(["demo", "nope/shop.duckdb"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    assert "creates no directories" in payload["errors"][0]
    assert not (tmp_path / "nope").exists()


def test_path_is_refused_rather_than_silently_ignored(capsys):
    """`--path` names the warehouse dex reads, everywhere else. Honoring it here
    would blur the one distinction this command exists to keep sharp, and
    ignoring it would be worse: a flag accepted and dropped reads as a setting
    that took effect."""

    payload = _run(["demo", "--path", "shop.duckdb"], capsys)

    assert payload["status"] == "error"
    assert payload["reason"] == "request"
    assert "dex demo shop.duckdb" in payload["errors"][0]


def test_an_existing_config_above_the_target_is_left_alone(tmp_path: Path, capsys):
    """A second config in a subdirectory would shadow the user's real one for
    every command run there, so the demo declines to write one and says so."""

    (tmp_path / ".git").mkdir()
    (tmp_path / ".dex").mkdir()
    committed = tmp_path / ".dex" / "config.yml"
    committed.write_text(
        "connector: duckdb\nduckdb:\n  path: production.duckdb\n", encoding="utf-8"
    )
    (tmp_path / "scratch").mkdir()

    payload = _run(["demo", "scratch/shop.duckdb"], capsys)

    assert payload["status"] == "ok"
    assert payload["data"]["created"] == ["scratch/shop.duckdb"]
    assert not (tmp_path / "scratch" / ".dex").exists()
    assert "production.duckdb" in committed.read_text(encoding="utf-8")
    assert any("left untouched" in w for w in payload["warnings"])
    assert payload["diffs"] == []
    assert all(
        "--path scratch/shop.duckdb" in step["command"]
        for step in payload["data"]["next_steps"]
    )


def test_the_generated_warehouse_drives_the_whole_explore_tour(tmp_path: Path, capsys):
    """The claim the documentation makes, run end to end: every command the
    envelope prints works, and each one lands the finding it promises."""

    _run(["demo"], capsys)

    mapped = _run(["explore", "map"], capsys)["data"]
    assert mapped["object_count"] == 7
    assert mapped["pii_column_count"] == 6
    assert mapped["relationship_count"] == 5

    profiled = _run(["explore", "profile", "order_items", "products"], capsys)
    notes = [n for d in profiled["data"]["datasets"] for n in d["data_quality"]]
    assert any("order_item_id is not unique" in n for n in notes)
    assert any("mixes value shapes" in n for n in notes)

    verified = _run(["explore", "relationships", "--verify"], capsys)["data"]
    edges = {
        (r["from_dataset"].split(".")[-1], r["from_columns"][0]): r
        for r in verified["relationships"]
    }
    assert edges[("orders", "customer_id")]["orphan_fraction"] == 0.0
    assert edges[("web_events", "customer_id")]["orphan_fraction"] == 1.0
    assert any("is not evidence of a shared key" in n for n in verified["notes"])

    refused = _run(["explore", "query", "select email from customers"], capsys)
    assert refused["reason"] == "guard"
    assert "PII-flagged" in refused["errors"][0]
    # And the refusal is policy rather than breakage: the same table answers an
    # aggregate over the same column.
    counted = _run(
        ["explore", "query", "select count(distinct email) as n from customers"], capsys
    )
    assert counted["data"]["cells"] == [[1200]]
