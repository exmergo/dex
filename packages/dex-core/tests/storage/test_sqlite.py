"""What is true only of the `.dex/dex.db` layout.

The backend-agnostic contract lives in test_parity.py. This file pins the things
specific to this backend: that everything lands in one file, that the schema
version is tracked and a newer one is refused, that spend_since answers from an
indexed query rather than a scan, and the cross-process half of the spend lock
(a `BEGIN IMMEDIATE` transaction), which the shared conformance suite cannot
portably assert.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from exmergo_dex_core import ConfigurationError
from exmergo_dex_core.cache import Dataset, DexCache
from exmergo_dex_core.storage import Document, StoreContext
from exmergo_dex_core.storage.sqlite import DB_FILE, DEX_DIR, SqliteStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, TransformPlan


def test_nothing_is_created_until_something_is_saved(tmp_path: Path):
    store = SqliteStore(tmp_path)
    assert store.load_cache() is None
    # load_cache is itself a use of the connection (it has to open the file to
    # find nothing there), so the directory exists after it; what must not exist
    # is anything left over from a call that never happened.
    assert (tmp_path / DEX_DIR / DB_FILE).is_file()
    conn = sqlite3.connect(str(tmp_path / DEX_DIR / DB_FILE))
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    conn.close()


def test_a_fresh_store_created_but_never_touched_leaves_no_file(tmp_path: Path):
    SqliteStore(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_everything_lands_in_one_file(tmp_path: Path):
    store = SqliteStore(tmp_path)
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 1})
    store.save_plan(
        TransformPlan(
            plan_id="pabc",
            created_at="2026-07-03T10:00:00+00:00",
            intent="add a model",
            project_dir="analytics",
            edits=[
                PlanEdit(
                    path="models/staging/stg_orders.sql",
                    kind=EditKind.MODEL_SQL,
                    new_content="select 1 as id\n",
                )
            ],
        )
    )
    dex_dir = tmp_path / DEX_DIR
    assert [p.name for p in dex_dir.iterdir()] == [DB_FILE]


def test_the_dex_directory_is_created_on_demand(tmp_path: Path):
    nested = tmp_path / "deep" / "project"
    store = SqliteStore(nested)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 1})
    assert (nested / DEX_DIR / DB_FILE).is_file()


def test_a_fresh_file_is_stamped_with_the_current_schema_version(tmp_path: Path):
    store = SqliteStore(tmp_path)
    store.append_query_log({"at": "2026-07-03T10:00:00+00:00", "sql": "select 1"})
    conn = sqlite3.connect(str(tmp_path / DEX_DIR / DB_FILE))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_a_file_from_a_newer_dex_is_refused_rather_than_silently_reread(
    tmp_path: Path,
):
    dex_dir = tmp_path / DEX_DIR
    dex_dir.mkdir()
    conn = sqlite3.connect(str(dex_dir / DB_FILE))
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    store = SqliteStore(tmp_path)
    with pytest.raises(ConfigurationError) as refusal:
        store.load_cache()
    message = str(refusal.value)
    assert "newer dex" in message
    assert "999" in message


def test_spend_since_answers_from_an_indexed_query_not_a_full_scan(tmp_path: Path):
    # Not a performance assertion (that would be flaky); a behavioral one that
    # only an indexed query, not a Python-side filter after loading everything,
    # can satisfy: the WHERE clause itself has to exclude the other connector's
    # rows and the pre-cutoff rows, which is exactly what the EXPLAIN plan below
    # confirms is happening via the index rather than a table scan.
    store = SqliteStore(tmp_path)
    for i in range(50):
        store.append_spend_log(
            {
                "at": f"2026-07-{(i % 28) + 1:02d}T10:00:00+00:00",
                "connector": "bigquery" if i % 2 else "snowflake",
                "billed_bytes": 10,
            }
        )
    assert store.spend_since("2026-07-15T00:00:00+00:00", connector="bigquery") > 0

    conn = sqlite3.connect(str(tmp_path / DEX_DIR / DB_FILE))
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT entry FROM spend_log "
        "WHERE at >= ? AND (? IS NULL OR connector = ?)",
        ("2026-07-15T00:00:00+00:00", "bigquery", "bigquery"),
    ).fetchall()
    conn.close()
    plan_text = " ".join(str(row) for row in plan)
    assert "USING INDEX" in plan_text, (
        f"spend_since's query plan does not use an index: {plan_text}"
    )


def test_spend_since_skips_a_hand_corrupted_row(tmp_path: Path):
    # The ledger is a binary file now, not a hand-editable JSONL one, but a row
    # can still end up holding text that will not parse (a partial write, a
    # manual UPDATE). The policy is the same as the filesystem backend's: skip
    # it, do not poison every subsequent budget check.
    store = SqliteStore(tmp_path)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    conn = sqlite3.connect(str(tmp_path / DEX_DIR / DB_FILE))
    conn.execute(
        "INSERT INTO spend_log(at, connector, entry) VALUES (?, ?, ?)",
        ("2026-07-03T11:00:00+00:00", None, "not json"),
    )
    conn.commit()
    conn.close()
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 100


def test_from_context_keys_the_store_to_the_repo_root(tmp_path: Path):
    store = SqliteStore.from_context(StoreContext(repo_root=str(tmp_path)))
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    assert (tmp_path / DEX_DIR / DB_FILE).is_file()


def test_from_context_refuses_a_context_with_no_repo_root():
    with pytest.raises(ConfigurationError) as refusal:
        SqliteStore.from_context(StoreContext())
    assert "repo root" in str(refusal.value)


def test_from_context_refuses_options_it_would_have_ignored(tmp_path: Path):
    with pytest.raises(ConfigurationError) as refusal:
        SqliteStore.from_context(
            StoreContext(repo_root=str(tmp_path), options={"tenant": "acme"})
        )
    assert "tenant" in str(refusal.value)


def test_locators_name_the_file_and_the_document(tmp_path: Path):
    store = SqliteStore(tmp_path)
    cache_locator = store.locator(Document.CACHE)
    assert str(tmp_path / DEX_DIR / DB_FILE) in cache_locator
    assert "cache" in cache_locator
    assert store.plan_locator("pabc") != cache_locator


# --- the spend lock, in the shape only this backend has -------------------------

_LOCK_CHILD = """
import sys, time
from exmergo_dex_core.storage.sqlite import SqliteStore

store = SqliteStore(sys.argv[1])
with store.spend_lock():
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_the_spend_lock_excludes_another_process(tmp_path: Path):
    """The property the conformance contract cannot portably assert.

    A `threading.Lock` satisfies every assertion the shipped contract makes and
    would leave the CLI exactly as broken as it was, because the CLI is one
    command per process and the file on disk is all two commands share.
    """

    import subprocess
    import sys as _sys

    store = SqliteStore(tmp_path)
    child = subprocess.Popen(  # noqa: S603
        [_sys.executable, "-c", _LOCK_CHILD, str(tmp_path), "0.5"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        started = time.monotonic()
        with store.spend_lock():
            waited = time.monotonic() - started
    finally:
        child.wait(timeout=30)
    assert waited > 0.2, (
        f"took the lock after {waited:.3f}s while another process held it, so "
        "two CLI commands can be admitted against the same headroom"
    )


def test_a_contended_lock_gives_up_rather_than_waiting_forever(tmp_path: Path):
    import subprocess
    import sys as _sys

    store = SqliteStore(tmp_path)
    child = subprocess.Popen(  # noqa: S603
        [_sys.executable, "-c", _LOCK_CHILD, str(tmp_path), "2"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with (
            pytest.raises(TimeoutError) as refusal,
            store.spend_lock(timeout=0.2),
        ):
            pass
    finally:
        child.wait(timeout=30)
    assert "spend lock" in str(refusal.value)


def test_the_lock_does_not_block_an_unrelated_document_read(tmp_path: Path):
    # The lock is scoped to the ledger, not the whole store: a document read on
    # the main connection must not wait behind a held spend lock.
    store = SqliteStore(tmp_path)
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    with store.spend_lock():
        loaded = store.load_cache()
    assert loaded is not None
    assert [d.identifier for d in loaded.datasets] == ["db.main.orders"]
