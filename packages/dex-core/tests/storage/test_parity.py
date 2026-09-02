"""One contract, every backend: the assertions each store owes its callers.

The contract itself lives in `exmergo_dex_core.storage.conformance`, shipped in
the wheel so a backend outside this distribution runs exactly these assertions.
This file is the in-repo consumer of it, and it is deliberately the same three
lines a third party writes: if driving the shipped contract were awkward here, it
would be awkward there.

Backend-specific behavior lives in test_filesystem.py and test_memory.py. What is
here is only "do the backends agree".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.storage import FilesystemStore, MemoryStore, SqliteStore
from exmergo_dex_core.storage.conformance import (
    SpendHistoryContract,
    SpendLockContract,
    StoreContract,
)


class TestFilesystemStore(SpendHistoryContract, SpendLockContract, StoreContract):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path):
        # One root per test, with the key as a subdirectory: two keys are two
        # directories, which is what "shares nothing" means for this backend.
        self.root = tmp_path

    def make_store(self, key: str) -> FilesystemStore:
        return FilesystemStore(self.root / key)


class TestMemoryStore(SpendHistoryContract, SpendLockContract, StoreContract):
    def make_store(self, key: str) -> MemoryStore:
        # Nothing to key on: a MemoryStore's state is the instance, so a distinct
        # instance is a distinct key by construction.
        return MemoryStore()


class TestSqliteStore(SpendHistoryContract, SpendLockContract, StoreContract):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path):
        # Same convention as TestFilesystemStore: one root per test, the key as a
        # subdirectory, so each gets its own dex.db.
        self.root = tmp_path

    def make_store(self, key: str) -> SqliteStore:
        return SqliteStore(self.root / key)
