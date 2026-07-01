"""Fixtures shaped like the field sessions that exposed the explore gaps: an
Airbnb-style raw export (RAW_ prefixes, bare NAME/COMMENTS columns, a non-unique
host ID) and an F1-style star schema keyed on camelCase <entity>Id columns."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def airbnb_duckdb(tmp_path: Path) -> Path:
    """Three raw tables: person-name and free-text columns that must be flagged,
    a hosts feed whose ID is not unique, and joins hidden behind RAW_ prefixes."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "airbnb.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE RAW_HOSTS (ID INTEGER, NAME VARCHAR)")
    conn.execute("INSERT INTO RAW_HOSTS VALUES (1, 'Ada'), (1, 'Ada'), (2, 'Bob')")
    conn.execute("CREATE TABLE RAW_LISTINGS (ID INTEGER, HOST_ID INTEGER)")
    conn.execute("INSERT INTO RAW_LISTINGS VALUES (10, 1), (11, 2)")
    conn.execute(
        "CREATE TABLE RAW_REVIEWS "
        "(ID INTEGER, LISTING_ID INTEGER, REVIEWER_NAME VARCHAR, COMMENTS VARCHAR)"
    )
    conn.execute(
        "INSERT INTO RAW_REVIEWS VALUES "
        "(100, 10, 'Grace', 'lovely stay'), (101, 11, 'Alan', 'would return')"
    )
    conn.close()
    return path


@pytest.fixture
def f1_duckdb(tmp_path: Path) -> Path:
    """A camelCase star schema: parents key on <entity>Id (not `id`), and the
    fact table's foreign keys use the same camelCase names."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "f1.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute('CREATE TABLE races ("raceId" INTEGER, year INTEGER)')
    conn.execute("INSERT INTO races VALUES (1, 2024), (2, 2024)")
    conn.execute('CREATE TABLE drivers ("driverId" INTEGER, surname VARCHAR)')
    conn.execute("INSERT INTO drivers VALUES (10, 'Senna'), (11, 'Prost')")
    conn.execute(
        "CREATE TABLE results "
        '("resultId" INTEGER, "raceId" INTEGER, "driverId" INTEGER, points DOUBLE)'
    )
    conn.execute(
        "INSERT INTO results VALUES (100, 1, 10, 25.0), (101, 1, 11, 18.0), "
        "(102, 2, 10, 25.0)"
    )
    conn.close()
    return path
