"""DuckDB connect: opened read-only, capabilities reported, writes refused
(Principle 3, read-only against data)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.adapters.duckdb import DuckDBAdapter, DuckDBReadOnlyError
from exmergo_dex_core.connect import open_adapter


def test_capabilities_report_read_only(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        caps = adapter.capabilities()
        assert caps["read_only"] is True
        assert caps["dialect"] == "duckdb"
        assert caps["paradigm"] == "free_local"
        assert "engine_version" in caps
    finally:
        adapter.close()


def test_write_is_refused_on_read_only_connection(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        with pytest.raises(Exception):
            adapter._conn.execute("CREATE TABLE intruder (x INTEGER)")
    finally:
        adapter.close()


def test_opening_nonexistent_file_read_only_fails(tmp_path: Path):
    # dex attaches to an existing store; it never creates one. A read-only open of
    # a missing file must fail loudly rather than silently create a writable db.
    pytest.importorskip("duckdb")
    with pytest.raises(DuckDBReadOnlyError):
        DuckDBAdapter(tmp_path / "does-not-exist.duckdb")


def test_open_adapter_resolves_path_argument(duckdb_file: Path):
    adapter = open_adapter(connector="duckdb", path=str(duckdb_file))
    try:
        assert adapter.capabilities()["read_only"] is True
    finally:
        adapter.close()


def test_exact_distinct_counts_is_exact_and_batched(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        counts = adapter.exact_distinct_counts(
            "warehouse.main.orders", ["id", "customer_id"]
        )
        assert counts == {"id": 3, "customer_id": 2}
        assert adapter.exact_distinct_counts("warehouse.main.orders", []) == {}
    finally:
        adapter.close()


def test_distinct_combination_counts_are_exact(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        counts = adapter.distinct_combination_counts(
            "warehouse.main.orders", [["customer_id", "total"], ["id", "customer_id"]]
        )
        assert counts == {("customer_id", "total"): 3, ("id", "customer_id"): 3}
        assert adapter.distinct_combination_counts("warehouse.main.orders", []) == {}
    finally:
        adapter.close()


def test_open_adapter_requires_a_path():
    with pytest.raises(ValueError):
        open_adapter(connector="duckdb", path=None, repo_root="/nonexistent-root")
