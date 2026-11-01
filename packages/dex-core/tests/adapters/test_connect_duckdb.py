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


class _RelationWithoutFetchone:
    """A DuckDB 2.0 relation: it keeps no result, so it has no fetchone()."""

    def __getattr__(self, name: str):
        if name == "fetchone":
            raise AttributeError("Relations no longer have fetchone()")
        raise AttributeError(name)


class _Duckdb2Connection:
    """The locked 1.x connection with its sql() answering as 2.0 does (#797)."""

    def __init__(self, real):
        self._real = real

    def sql(self, _query: str):
        return _RelationWithoutFetchone()

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_capabilities_do_not_fetch_from_a_relation(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    real = adapter._conn
    try:
        adapter._conn = _Duckdb2Connection(real)
        assert adapter.capabilities()["engine_version"]
    finally:
        adapter._conn = real
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


def test_committed_duckdb_path_resolves_against_repo_root_not_cwd(
    duckdb_file: Path, tmp_path: Path, monkeypatch
):
    # A relative duckdb.path in .dex/config.yml is a committed target, so it must
    # resolve against the project root the config lives in, not the process cwd.
    # This is what lets a command run from a subdirectory open the same file.
    import shutil

    from exmergo_dex_core.config import DexConfig, DuckDBTarget, save_config

    shutil.copy(duckdb_file, tmp_path / "w.duckdb")
    save_config(
        DexConfig(connector="duckdb", duckdb=DuckDBTarget(path="w.duckdb")), tmp_path
    )
    elsewhere = tmp_path / "sub" / "dir"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    adapter = open_adapter(repo_root=str(tmp_path))
    try:
        assert adapter.capabilities()["read_only"] is True
    finally:
        adapter.close()


def test_list_namespace_objects_lists_one_schema(tmp_path):
    import duckdb

    path = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("CREATE SCHEMA staging_dev")
    conn.execute("CREATE TABLE staging_dev.stg_leftover (id INTEGER)")
    conn.execute("CREATE VIEW staging_dev.v_leftover AS SELECT * FROM customers")
    conn.close()

    adapter = DuckDBAdapter(path)
    assert adapter.list_namespace_objects("staging_dev") == [
        "stg_leftover",
        "v_leftover",
    ]
    assert adapter.list_namespace_objects("not_there") == []
    adapter.close()


def test_a_host_cap_gates_nothing_on_duckdb(duckdb_file: Path):
    """#787: DuckDB bills nothing and carries no cost gate, so a host's cap is
    reported on every result and never applied."""

    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine

    engine = DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=DexConfig(connector="duckdb"),
        session_ceiling_cap=1.0,
    )
    result = engine.inventory()
    assert result.session_ceiling_cap == {
        "value": 1.0,
        "unit": None,
        "source": "host",
    }
    assert getattr(engine._adapter(), "cost_gate", None) is None


def test_duckdb_in_scope_matches_attached_database(duckdb_file: Path):
    """#984: duckdb inspects the attached database file, so namespaces in that
    database are within scope and other databases are not."""
    adapter = DuckDBAdapter(duckdb_file)
    try:
        db = adapter._current_database()
        assert adapter.in_scope(f"{db}.legacy") is True
        assert adapter.in_scope("legacy") is True
        assert adapter.in_scope("other_warehouse.legacy") is False
    finally:
        adapter.close()
