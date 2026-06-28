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


@pytest.fixture
def sketch_duckdb(tmp_path: Path) -> Path:
    """A warehouse for the categorical sketch gate.

    ``catalog`` has one low-cardinality non-PII text column that SHOULD sketch
    (``status``, with skewed counts, an equal-count tie, and an empty string), a
    ``tier`` column whose values contain a secret-like substring (must still sketch
    and survive sanitization), and columns that must NOT sketch: ``email`` (PII),
    ``diagnosis`` (deny-listed name the PII patterns miss), ``note`` (a value over
    the length cap), and ``id`` (numeric). ``bulk.code`` is high-cardinality.
    """

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "sketch.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE catalog ("
        "id INTEGER, status VARCHAR, tier VARCHAR, email VARCHAR, "
        "diagnosis VARCHAR, note VARCHAR)"
    )
    conn.execute(
        "INSERT INTO catalog VALUES "
        "(1,'active','token_pro','u1@x.com','flu','ok'),"
        "(2,'active','token_pro','u2@x.com','flu','ok'),"
        "(3,'active','token_pro','u3@x.com','flu','ok'),"
        "(4,'archived','token_pro','u4@x.com','flu','ok'),"
        "(5,'archived','token_pro','u5@x.com','cold','ok'),"
        "(6,'pending','token_basic','u6@x.com','cold',repeat('z',80)),"
        "(7,'pending','token_basic','u7@x.com','cold','ok'),"
        "(8,'','token_basic','u8@x.com','cold','ok')"
    )
    # High-cardinality text column (60 distinct > the categorical cap).
    conn.execute(
        "CREATE TABLE bulk AS SELECT i AS id, 'code_' || i AS code FROM range(60) t(i)"
    )
    conn.close()
    return path
