"""Fixtures shaped like the field sessions that exposed the explore gaps: an
Airbnb-style raw export (RAW_ prefixes, bare NAME/COMMENTS columns, a non-unique
host ID) and an F1-style star schema keyed on camelCase <entity>Id columns."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`explore profile` and `explore relationships` persist to `.dex/` under the
    repo root, which defaults to the CWD; tests that omit --repo-root must land
    that write in tmp_path, never in the checkout."""

    monkeypatch.chdir(tmp_path)


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
def near_unique_duckdb(tmp_path: Path) -> Path:
    """Tables big enough for approx_count_distinct to genuinely err: a 50k-row
    table with an exactly-unique key (the field failure: HLL noise made every
    real key read non-unique) and one with true near-threshold duplication."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "near_unique.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE results AS "
        'SELECT range::INTEGER AS "resultId", 1.0::DOUBLE AS points '
        "FROM range(50000)"
    )
    conn.execute(
        "CREATE TABLE dupes AS SELECT (range % 45000)::INTEGER AS id FROM range(50000)"
    )
    conn.close()
    return path


@pytest.fixture
def many_tables_duckdb(tmp_path: Path) -> Path:
    """Sixty tiny tables, enough to push `explore map` past the auto-profile-all
    threshold and into the ranked top-N cutoff."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "many_tables.duckdb"
    conn = duckdb.connect(str(path))
    for i in range(60):
        conn.execute(f"CREATE TABLE t_{i:02d} (id INTEGER, v INTEGER)")
        conn.execute(f"INSERT INTO t_{i:02d} VALUES (1, {i})")  # noqa: S608
    conn.close()
    return path


@pytest.fixture
def composite_grain_duckdb(tmp_path: Path) -> Path:
    """A TPCH-shaped pair: a fact table whose only key is the composite
    (order_key, line_number), where line_number alone has tiny cardinality
    (the shape single-column detection can never resolve), next to a parent
    with a clean surrogate key."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "composite.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE orders AS "
        "SELECT range::INTEGER AS order_key, 'open' AS status FROM range(1, 501)"
    )
    conn.execute(
        "CREATE TABLE line_items AS "
        "SELECT o.range::INTEGER AS order_key, l.range::INTEGER AS line_number, "
        "(l.range % 2)::INTEGER AS quantity "
        "FROM range(1, 501) o, range(1, 5) l"
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
