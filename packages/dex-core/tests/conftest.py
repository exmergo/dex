"""Shared fixtures. DuckDB is the test engine: in-process, free, fast."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def duckdb_file(tmp_path: Path) -> Path:
    """A real on-disk DuckDB file with one tiny table.

    Created via a writable connection in the fixture, then handed to the engine,
    which must open it read-only. duckdb is required (the [duckdb] extra); the
    test is skipped if it is not installed so the rest of the suite still runs.
    """

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, email VARCHAR)")
    conn.execute(
        "INSERT INTO customers VALUES (1, 'a@example.com'), (2, 'b@example.com')"
    )
    conn.close()
    return path
