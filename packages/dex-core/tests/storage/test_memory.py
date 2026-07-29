"""What is true only of the in-process backend.

The backend-agnostic contract lives in test_parity.py. This file pins the two
properties MemoryStore exists for: it writes nothing, and it retains everything for
the life of the instance (a store that dropped writes would break the second call
of any real flow, since `explore query` and `explore cluster` read the cache back).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import ConfigurationError
from exmergo_dex_core.cache import Dataset, DexCache
from exmergo_dex_core.storage import Document, MemoryStore, StoreContext


def test_nothing_reaches_the_filesystem(tmp_path: Path, monkeypatch):
    # cwd is the trap: a backend that quietly resolved a relative `.dex/` would
    # write into whatever directory the caller happened to be in.
    monkeypatch.chdir(tmp_path)
    store = MemoryStore()
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    store.append_query_log({"at": "2026-07-03T10:00:00+00:00", "sql": "select 1"})
    assert list(tmp_path.rglob("*")) == []


def test_state_survives_across_calls_on_one_instance(tmp_path: Path):
    # The retention requirement: profile then query has to see the same cache.
    store = MemoryStore()
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    for _ in range(3):
        loaded = store.load_cache()
        assert loaded is not None
        assert [d.identifier for d in loaded.datasets] == ["db.main.orders"]


def test_state_does_not_leak_between_instances():
    # Two library callers in one process must not see each other's state.
    first = MemoryStore()
    first.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    assert MemoryStore().load_cache() is None


def test_a_loaded_document_is_a_copy_the_caller_can_mutate_freely():
    store = MemoryStore()
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    loaded = store.load_cache()
    assert loaded is not None
    loaded.datasets[0].identifier = "mutated.after.load"
    reloaded = store.load_cache()
    assert reloaded is not None
    assert [d.identifier for d in reloaded.datasets] == ["db.main.orders"]


def test_from_context_ignores_a_repo_root_but_refuses_options(tmp_path: Path):
    # A repo root is present in every context dex builds, and carrying one is not a
    # request to use it; an option this backend has no meaning for is a mistake.
    built = MemoryStore.from_context(StoreContext(repo_root=str(tmp_path)))
    built.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    assert list(tmp_path.rglob("*")) == []

    with pytest.raises(ConfigurationError) as refusal:
        MemoryStore.from_context(StoreContext(options={"tenant": "acme"}))
    assert "tenant" in str(refusal.value)


def test_locators_name_the_backend_rather_than_a_path():
    # An agent reading `cache_path` should be able to tell at a glance that there
    # is no file to open.
    store = MemoryStore()
    assert store.locator(Document.CACHE) == "memory:cache"
    assert store.plan_locator("pabc") == "memory:plans/pabc"
