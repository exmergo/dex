"""A concurrent cache write is never reverted by a stale copy.

`explore query` auto-profiles an object the cache does not cover, and used to
merge that profile into the cache it loaded when it started, then save. Any
profile another command saved in between was overwritten, including a PII flag
a refresh had just raised, so the firewall admitted a projection it had
refused. Each test here pauses one command inside its scan, runs another writer
to completion, then lets the first finish, which makes the interleaving
deterministic.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from exmergo_dex_core import QueryRefusedError  # noqa: E402
from exmergo_dex_core.engine import DexEngine  # noqa: E402
from exmergo_dex_core.explore import profile as profile_mod  # noqa: E402

PAUSED_THREAD = "paused-writer"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """Pause the profile scan on the paused thread until released."""

    state = {"entered": threading.Event(), "release": threading.Event()}
    real = profile_mod.profile

    def paused(adapter, identifiers, **kwargs):
        if threading.current_thread().name == PAUSED_THREAD:
            state["entered"].set()
            state["release"].wait(30)
        return real(adapter, identifiers, **kwargs)

    monkeypatch.setattr(profile_mod, "profile", paused)
    yield state
    state["release"].set()


def _root(tmp_path: Path) -> Path:
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table a(id integer, x double)")
    con.execute("insert into a values (1, 1.0)")
    con.execute("create table b(id integer, city varchar)")
    con.execute(
        "insert into b values (1,'Oslo'),(2,'Bergen'),(3,'Oslo'),(4,'Trondheim')"
    )
    con.execute("create table c(id integer, z double)")
    con.execute("insert into c values (1, 3.0)")
    con.close()
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db.as_posix()}\n", encoding="utf-8"
    )
    return tmp_path


def _cached(root: Path) -> dict[str, dict]:
    doc = json.loads((root / ".dex" / "cache.json").read_text(encoding="utf-8"))
    return {d["identifier"].rsplit(".", 1)[-1]: d for d in doc["datasets"]}


def _city_confidence(root: Path) -> float | None:
    for column in _cached(root)["b"]["columns"]:
        if column["name"] == "city":
            return (column.get("pii") or {}).get("confidence")
    return None


def _firewall_admits_city(root: Path) -> bool:
    with DexEngine.from_repo(root) as engine:
        try:
            engine.query("select city from b", auto_profile=False)
        except QueryRefusedError:
            return False
    return True


def _run_paused(gate, target, writer) -> None:
    """Start ``target`` paused inside its scan, run ``writer``, then release."""

    errors: list[BaseException] = []

    def run():
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name=PAUSED_THREAD)
    thread.start()
    assert gate["entered"].wait(30), "the paused command never reached its scan"
    writer()
    gate["release"].set()
    thread.join(30)
    assert not errors, errors


def _query_c(root: Path):
    def run():
        with DexEngine.from_repo(root) as engine:
            engine.query("select id, z from c")

    return run


def _profile_b(root: Path, *, refresh: bool = True):
    def run():
        with DexEngine.from_repo(root) as engine:
            engine.profile("b", refresh=refresh)

    return run


def test_a_raised_pii_flag_survives_a_concurrent_auto_profile(tmp_path, gate):
    root = _root(tmp_path)
    with DexEngine.from_repo(root) as engine:
        engine.profile("a")
        engine.profile("b")
    seeded = _city_confidence(root)
    assert seeded is not None
    assert _firewall_admits_city(root), "precondition: few distinct cities pass"

    # More distinct cities, same columns: a refresh raises the flag past the
    # blocking threshold without changing the column signature.
    con = duckdb.connect(str(root / "w.duckdb"))
    for i in range(10):
        con.execute("insert into b values (?, ?)", [100 + i, f"Town{i}"])
    con.close()

    raised: dict = {}

    def refresh_b():
        _profile_b(root)()
        raised["confidence"] = _city_confidence(root)

    _run_paused(gate, _query_c(root), refresh_b)

    assert raised["confidence"] > seeded
    assert _city_confidence(root) == raised["confidence"], (
        "the auto-profiling query saved its stale copy over the raised flag"
    )
    assert not _firewall_admits_city(root)
    assert {"a", "b", "c"} <= set(_cached(root))


def test_a_profile_saved_mid_query_is_not_dropped(tmp_path, gate):
    root = _root(tmp_path)
    with DexEngine.from_repo(root) as engine:
        engine.profile("a")

    _run_paused(gate, _query_c(root), _profile_b(root, refresh=False))

    assert {"a", "b", "c"} <= set(_cached(root))


def test_a_query_saved_mid_profile_is_not_dropped(tmp_path, gate):
    """The other direction: the paused writer is the deliberate profile."""

    root = _root(tmp_path)
    with DexEngine.from_repo(root) as engine:
        engine.profile("a")

    _run_paused(gate, _profile_b(root, refresh=False), _query_c(root))

    assert {"a", "b", "c"} <= set(_cached(root))


def test_map_keeps_a_newer_profile_saved_while_it_ran(tmp_path, gate):
    root = _root(tmp_path)
    with DexEngine.from_repo(root) as engine:
        engine.profile("b")
    before = _cached(root)["b"]["profiled_at"]

    def run_map():
        with DexEngine.from_repo(root) as engine:
            engine.map()

    _run_paused(gate, run_map, _profile_b(root))

    assert _cached(root)["b"]["profiled_at"] > before
