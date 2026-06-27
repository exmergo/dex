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
    # A child table so relationship inference has a customer_id -> customers.id
    # foreign key to find, and a numeric column with a safe min/max.
    conn.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, total DECIMAL(10,2))"
    )
    conn.execute("INSERT INTO orders VALUES (1, 1, 9.99), (2, 1, 5.00), (3, 2, 12.50)")
    conn.close()
    return path
