"""The cache lock every shipped backend offers.

A cache write re-reads the stored cache, folds its own work in and saves, all
under this lock, so a command that loaded the cache at its start cannot
overwrite what another command saved since. These pin the lock's own
properties: it excludes a second holder, threads and processes alike, it is
re-entrant for one holder, and it never contends with the spend lock.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from exmergo_dex_core.storage import (
    CacheLock,
    FilesystemStore,
    MemoryStore,
    SqliteStore,
)
from exmergo_dex_core.storage.filesystem import DEX_DIR

BACKENDS = {
    "filesystem": FilesystemStore,
    "sqlite": SqliteStore,
    "memory": lambda _root: MemoryStore(),
}


@pytest.fixture(params=sorted(BACKENDS))
def store(request, tmp_path: Path):
    return BACKENDS[request.param](tmp_path)


def test_every_shipped_backend_offers_the_cache_lock(store):
    assert isinstance(store, CacheLock)


def test_the_cache_lock_excludes_another_thread(store):
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with store.cache_lock():
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(10)
        acquired = threading.Event()

        def contend():
            with store.cache_lock():
                acquired.set()

        contender = threading.Thread(target=contend)
        contender.start()
        assert not acquired.wait(0.3), "a second thread took a held cache lock"
        release.set()
        assert acquired.wait(10), "the waiter never got the lock after release"
        contender.join(10)
    finally:
        release.set()
        holder.join(10)


def test_the_cache_lock_is_reentrant_for_one_holder(store):
    with store.cache_lock(), store.cache_lock(timeout=0.05):
        pass


def test_the_cache_lock_does_not_contend_with_the_spend_lock(store):
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with store.spend_lock():
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(10)
        with store.cache_lock(timeout=1.0):
            pass
    finally:
        release.set()
        holder.join(10)


_LOCK_CHILD = """
import sys, time
from exmergo_dex_core.storage import FilesystemStore, SqliteStore

store = {"filesystem": FilesystemStore, "sqlite": SqliteStore}[sys.argv[1]](sys.argv[2])
with store.cache_lock():
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
"""


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
def test_the_cache_lock_excludes_another_process(tmp_path: Path, backend: str):
    """The CLI runs one command per process, so threads alone prove nothing."""

    store = BACKENDS[backend](tmp_path)
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _LOCK_CHILD, backend, str(tmp_path), "0.5"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        started = time.monotonic()
        with store.cache_lock():
            waited = time.monotonic() - started
    finally:
        child.wait(timeout=30)
    assert waited > 0.2, (
        f"took the cache lock after {waited:.3f}s while another process held it"
    )


def test_a_contended_cache_lock_gives_up_with_the_builtin_timeout(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _LOCK_CHILD, "filesystem", str(tmp_path), "2"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(TimeoutError) as refusal, store.cache_lock(timeout=0.05):
            pass
    finally:
        child.wait(timeout=30)
    assert "cache lock" in str(refusal.value)


def test_the_cache_lock_file_is_its_own_empty_file(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    with store.cache_lock():
        pass
    lock = tmp_path / DEX_DIR / "cache.lock"
    assert lock.is_file()
    assert lock.read_text(encoding="utf-8") == ""
