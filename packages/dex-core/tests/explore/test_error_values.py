"""Explore surfaces never return the value a warehouse error quotes (#1070).

A real DuckDB warehouse holds one key, ``zq_sentinel_7``, that does not convert to
an integer. Every surface below makes a statement fail on that conversion and
asserts the key appears nowhere in the serialized envelope, the cache or the
query log, while the warehouse's diagnosis (what failed, which type) survives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

SENTINEL = "zq_sentinel_7"
MASKED = "Could not convert string <value withheld> to INT32"


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE events AS SELECT i AS event_id, "
        "CAST((i % 9) + 1 AS VARCHAR) AS account_ref FROM range(1, 50) t(i)"
    )
    conn.execute("INSERT INTO events VALUES (999, ?)", [SENTINEL])
    conn.close()
    return path


def _dex(argv: list[str], capsys) -> dict:
    main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    return json.loads(out)


def _leaks(repo: Path, *payloads: dict) -> list[str]:
    places = [json.dumps(p) for p in payloads]
    places += [p.read_text() for p in (repo / ".dex").rglob("*") if p.is_file()]
    return [p[:200] for p in places if SENTINEL in p]


def _args(warehouse: Path, repo: Path) -> list[str]:
    return ["--path", str(warehouse), "--repo-root", str(repo)]


def test_a_query_that_fails_on_a_cast_names_the_failure_not_the_value(
    warehouse, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _dex(["explore", "map", *_args(warehouse, repo)], capsys)
    single = _dex(
        [
            "explore",
            "query",
            "select cast(account_ref as integer) from events where event_id = 999",
            *_args(warehouse, repo),
        ],
        capsys,
    )
    assert single["status"] == "error"
    assert single["reason"] == "execution_failure"
    assert MASKED in single["errors"][0]

    batch = _dex(
        [
            "explore",
            "query",
            "select count(*) from events",
            "select cast(account_ref as integer) from events where event_id = 999",
            *_args(warehouse, repo),
        ],
        capsys,
    )
    failed = batch["data"]["results"][1]
    assert failed["status"] == "failed"
    assert MASKED in failed["error"]
    assert batch["data"]["results"][0]["status"] == "ok"
    assert _leaks(repo, single, batch) == []


def test_a_profile_that_fails_on_a_cast_names_the_failure_not_the_value(
    warehouse, tmp_path, capsys
):
    import duckdb

    conn = duckdb.connect(str(warehouse))
    conn.execute(
        "CREATE VIEW events_cast AS SELECT event_id, "
        "CAST(account_ref AS INTEGER) AS account_id FROM events"
    )
    conn.close()
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _dex(
        ["explore", "profile", "events_cast", *_args(warehouse, repo)], capsys
    )
    assert payload["status"] == "error"
    assert payload["reason"] == "execution_failure"
    assert MASKED in payload["errors"][0]
    assert _leaks(repo, payload) == []
