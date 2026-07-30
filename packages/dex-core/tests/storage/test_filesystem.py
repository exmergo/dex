"""What is true only of the on-disk layout under `.dex/`.

The backend-agnostic contract lives in test_parity.py. This file pins the things a
human or a git diff depends on: which file holds what, that documents are readable
JSON rather than a blob, and that a hand-mangled ledger line degrades instead of
poisoning the session budget.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import ConfigurationError
from exmergo_dex_core.cache import Dataset, DexCache
from exmergo_dex_core.storage import Document, FilesystemStore, StoreContext
from exmergo_dex_core.storage.filesystem import (
    CACHE_FILE,
    DEX_DIR,
    DRIFT_FILE,
    PLANS_DIR,
    SNAPSHOT_FILE,
    SPEND_FILE,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, TransformPlan


def test_nothing_is_created_until_something_is_saved(tmp_path: Path):
    # Constructing a store must not materialize a directory: `explore query` builds
    # one before deciding the cache is missing, and a refusal should leave no trace.
    store = FilesystemStore(tmp_path)
    assert store.load_cache() is None
    assert list(tmp_path.iterdir()) == []


def test_documents_land_in_their_named_files(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    dex = tmp_path / DEX_DIR
    assert (dex / CACHE_FILE).is_file()
    assert not (dex / SNAPSHOT_FILE).exists()
    assert not (dex / DRIFT_FILE).exists()


def test_a_saved_document_is_reviewable_json(tmp_path: Path):
    # `.dex/` is committed to the user's repo, so a document has to read as a diff
    # a human can follow: indented, one key per line, newline-terminated.
    store = FilesystemStore(tmp_path)
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    raw = (tmp_path / DEX_DIR / CACHE_FILE).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert '\n  "datasets": [' in raw
    assert json.loads(raw)["datasets"][0]["identifier"] == "db.main.orders"


def test_the_dex_directory_is_created_on_demand(tmp_path: Path):
    nested = tmp_path / "deep" / "project"
    store = FilesystemStore(nested)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 1})
    assert (nested / DEX_DIR / SPEND_FILE).is_file()


def test_the_spend_ledger_is_append_only_jsonl(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    store.append_spend_log({"at": "2026-07-03T11:00:00+00:00", "billed_bytes": 250})
    lines = (tmp_path / DEX_DIR / SPEND_FILE).read_text().splitlines()
    assert [json.loads(line)["billed_bytes"] for line in lines] == [100, 250]


def test_spend_since_skips_malformed_lines(tmp_path: Path):
    # The ledger is a plain text file in the user's repo, so it can be hand-edited
    # or truncated mid-write. A damaged line must not poison the budget check.
    store = FilesystemStore(tmp_path)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    path = tmp_path / DEX_DIR / SPEND_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"at": None, "billed_bytes": 50}) + "\n")
        handle.write(json.dumps({"at": "2026-07-03T11:00:00+00:00"}) + "\n")
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 100


def test_plans_live_one_file_per_plan(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    plan = TransformPlan(
        plan_id="pabc123",
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
    store.save_plan(plan)
    path = tmp_path / DEX_DIR / PLANS_DIR / "pabc123.json"
    assert path.is_file()
    assert json.loads(path.read_text())["intent"] == "add a model"


def test_from_context_keys_the_store_to_the_repo_root(tmp_path: Path):
    store = FilesystemStore.from_context(StoreContext(repo_root=str(tmp_path)))
    store.save_cache(DexCache(datasets=[Dataset(identifier="db.main.orders")]))
    assert (tmp_path / DEX_DIR / CACHE_FILE).is_file()


def test_from_context_refuses_a_context_with_no_repo_root():
    # The dangerous fallback is defaulting to the working directory, which would
    # write one project's cache into wherever the process happened to start.
    with pytest.raises(ConfigurationError) as refusal:
        FilesystemStore.from_context(StoreContext())
    assert "repo root" in str(refusal.value)


def test_from_context_refuses_options_it_would_have_ignored(tmp_path: Path):
    # Accepted-and-ignored is worse than rejected: the caller believes the setting
    # took effect and nothing in the output says otherwise.
    with pytest.raises(ConfigurationError) as refusal:
        FilesystemStore.from_context(
            StoreContext(repo_root=str(tmp_path), options={"tenant": "acme"})
        )
    assert "tenant" in str(refusal.value)


def test_locators_are_absolute_paths_under_the_repo_root(tmp_path: Path):
    # The locator is what the envelope shows a human, so on this backend it has to
    # be a path they can open.
    store = FilesystemStore(tmp_path)
    cache_locator = store.locator(Document.CACHE)
    assert Path(cache_locator) == tmp_path / DEX_DIR / CACHE_FILE
    assert Path(store.plan_locator("pabc")) == (
        tmp_path / DEX_DIR / PLANS_DIR / "pabc.json"
    )
