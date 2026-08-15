"""The generated warehouse: deterministic, create-only, and flawed on purpose.

Two kinds of assertion here. The first pins determinism, because the READMEs
quote counts and column names out of this data and a silent drift would make the
documentation wrong exactly where a new user is reading it. The second pins each
seeded flaw at the level of the data itself, so a change that quietly healed one
fails here rather than in the explore suite, where it would read as a detector
regression instead of a fixture regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from exmergo_dex_core.demo import (
    DEMO_FILENAME,
    DEMO_SEED,
    DemoDependencyError,
    DemoPathError,
    DemoTargetExistsError,
    build_tables,
    generate_demo_warehouse,
    row_digest,
)

# The pinned digest. If an edit to the generator moves this, the counts and
# column names quoted in README.md, packages/dex-core/README.md, and
# references/duckdb.md have to be re-checked against real output and updated in
# the same change; then, and only then, update this value.
GOLDEN_DIGEST = "bb3dc590fd38eca798b0bfd28619d3e974f66107ec88535e8fb577806126a37f"

EXPECTED_ROWS = {
    "customers": 1200,
    "products": 300,
    "orders": 5000,
    "order_items": 14000,
    "web_events": 9000,
    "warehouse_locations": 12,
    "returns": 0,
}


def _table(name: str):
    return next(t for t in build_tables() if t.name == name)


def _column(table, name: str) -> int:
    return next(i for i, (col, _type) in enumerate(table.columns) if col == name)


def test_the_generated_data_is_byte_for_byte_the_documented_data():
    """The determinism contract, as one number.

    A generator whose output drifts would make every quoted row count in the
    documentation quietly wrong, which for a tool whose claim is precision is
    worse than having no quickstart at all.
    """

    tables = build_tables()
    assert row_digest(tables) == GOLDEN_DIGEST
    assert {t.name: len(t.rows) for t in tables} == EXPECTED_ROWS
    assert sum(len(t.rows) for t in tables) == 29512
    # Two runs in the same process, and a re-seeded run, agree: the stream is
    # pinned rather than merely happening to start from the same place.
    assert row_digest(build_tables()) == GOLDEN_DIGEST
    assert row_digest(build_tables(DEMO_SEED)) == GOLDEN_DIGEST


def test_a_different_seed_produces_different_data():
    """The digest is measuring the data, not a constant that ignores its input."""

    assert row_digest(build_tables(DEMO_SEED + 1)) != GOLDEN_DIGEST


def test_no_generated_value_depends_on_the_wall_clock():
    """Every date is measured back from a fixed anchor.

    A single `date.today()` would make the file change daily, which is the same
    documentation failure as an unpinned seed but harder to notice, since it
    reproduces perfectly within one day.
    """

    from exmergo_dex_core.demo import warehouse

    source = Path(warehouse.__file__).read_text(encoding="utf-8")
    for forbidden in ("date.today", "datetime.now", "datetime.utcnow", "time.time"):
        assert forbidden not in source, forbidden


# --- the seeded flaws, each pinned in the data itself ---------------------------


def test_the_order_item_key_repeats_a_double_loaded_batch():
    items = _table("order_items")
    ids = [row[_column(items, "order_item_id")] for row in items.rows]
    assert len(ids) == 14000
    assert len(set(ids)) == 13000, "1000 rows come from a batch loaded twice"


def test_the_sku_key_is_unique_but_mixes_two_id_schemes():
    """Both halves matter. Uniqueness is what makes `sku` a candidate key, which
    is the only thing profiling checks value shapes on; the mix is the finding."""

    products = _table("products")
    skus = [row[_column(products, "sku")] for row in products.rows]
    assert len(set(skus)) == len(skus) == 300
    numeric = [s for s in skus if s.isdigit()]
    hexed = [s for s in skus if not s.isdigit()]
    assert len(numeric) == 270 and len(hexed) == 30
    assert all(len(s) == 32 and re.fullmatch(r"[0-9a-f]{32}", s) for s in hexed)


def test_the_web_event_customer_ids_overlap_the_crm_by_nothing():
    customers = _table("customers")
    events = _table("web_events")
    known = {row[_column(customers, "customer_id")] for row in customers.rows}
    referenced = {row[_column(events, "customer_id")] for row in events.rows}
    assert known and referenced
    assert not (known & referenced), "the join has to be declined, not demoted"


def test_the_returns_table_exists_and_is_empty():
    returns = _table("returns")
    assert returns.rows == ()
    assert [name for name, _type in returns.columns] == [
        "return_id",
        "order_id",
        "reason_code",
        "returned_at",
    ]


def test_two_columns_contradict_their_declared_type():
    """One string-encoded timestamp and one epoch-in-milliseconds integer: the
    two shapes `explore profile` reports, and the two that silently break a
    downstream cast."""

    orders = _table("orders")
    assert dict(orders.columns)["placed_at"] == "VARCHAR"
    placed = [row[_column(orders, "placed_at")] for row in orders.rows]
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v) for v in placed)

    events = _table("web_events")
    assert dict(events.columns)["occurred_at"] == "BIGINT"
    occurred = [row[_column(events, "occurred_at")] for row in events.rows]
    # Thirteen digits: inside the plausible epoch-milliseconds window, and well
    # outside the epoch-seconds one, so the reported unit cannot be ambiguous.
    assert all(1_700_000_000_000 < v < 1_800_000_000_000 for v in occurred)


def test_the_personal_data_and_the_false_positives_are_both_present():
    """The demo has to carry real PII and real false positives, because the
    false positives are designed behavior and the first run is where meeting
    them is cheapest."""

    customers = _table("customers")
    assert {"email", "full_name"} <= {name for name, _ in customers.columns}
    emails = [row[_column(customers, "email")] for row in customers.rows]
    assert len(set(emails)) == 1200, "unique, so the flag is raised to 0.95"

    locations = _table("warehouse_locations")
    names = {name for name, _ in locations.columns}
    assert {"site_name", "city", "latitude", "longitude"} <= names
    sites = [row[_column(locations, "site_name")] for row in locations.rows]
    # An all-caps closed vocabulary is what profiling recognizes as reference
    # data, de-rating the generic name flag below the block threshold.
    assert len(set(sites)) == 12
    assert all(re.fullmatch(r"[A-Z]+( [A-Z]+)*", s) for s in sites)
    cities = [row[_column(locations, "city")] for row in locations.rows]
    assert len(set(cities)) == 12, "five or fewer would be de-rated instead"

    # And the true negative: `product` is a non-person qualifier, so this one
    # must not be flagged. Meeting it beside the false positives is the lesson.
    assert "product_name" in {name for name, _ in _table("products").columns}


# --- create-only ----------------------------------------------------------------


def test_generating_creates_the_file_and_nothing_else(tmp_path: Path):
    duckdb = pytest.importorskip("duckdb")
    target = tmp_path / DEMO_FILENAME
    warehouse = generate_demo_warehouse(target)

    assert warehouse.path == target
    assert warehouse.row_count == 29512
    assert sorted(p.name for p in tmp_path.iterdir()) == [DEMO_FILENAME]

    # Read back through a read-only connection, which also proves the file is a
    # real, checkpointed database rather than something only the writer can open.
    counts: dict[str, int] = {}
    connection = duckdb.connect(str(target), read_only=True)
    try:
        for name in EXPECTED_ROWS:
            # The table names are this test's own constants, never input.
            sql = f"SELECT COUNT(*) FROM {name}"  # noqa: S608
            counts[name] = connection.execute(sql).fetchone()[0]
    finally:
        connection.close()
    assert counts == EXPECTED_ROWS


def test_an_existing_target_is_refused_and_left_untouched(tmp_path: Path):
    """The one promise this command cannot break: it never opens, inspects, or
    replaces a warehouse it did not create."""

    pytest.importorskip("duckdb")
    target = tmp_path / "already-here.duckdb"
    target.write_bytes(b"not really a database")

    with pytest.raises(DemoTargetExistsError, match="already exists"):
        generate_demo_warehouse(target)
    assert target.read_bytes() == b"not really a database"


def test_a_directory_at_the_target_path_is_refused(tmp_path: Path):
    pytest.importorskip("duckdb")
    target = tmp_path / "warehouse.duckdb"
    target.mkdir()

    with pytest.raises(DemoTargetExistsError):
        generate_demo_warehouse(target)


def test_a_missing_parent_directory_is_refused_rather_than_created(tmp_path: Path):
    with pytest.raises(DemoPathError, match="not an existing directory"):
        generate_demo_warehouse(tmp_path / "nope" / "demo.duckdb")
    assert not (tmp_path / "nope").exists()


def test_a_missing_duckdb_client_names_the_extra_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """This is very likely someone's first dex command, so a bare ImportError
    would land on a person who does not yet know connector clients live behind
    extras. The refusal names the install line instead."""

    import sys

    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(DemoDependencyError, match=r"exmergo-dex-core\[duckdb\]"):
        generate_demo_warehouse(tmp_path / DEMO_FILENAME)
    assert not (tmp_path / DEMO_FILENAME).exists()


def test_the_path_refusals_run_before_duckdb_is_even_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Order matters: a refused call must have opened nothing, and the only way
    to prove nothing was opened is to refuse while the client is unavailable."""

    import sys

    monkeypatch.setitem(sys.modules, "duckdb", None)
    existing = tmp_path / "demo.duckdb"
    existing.touch()
    with pytest.raises(DemoTargetExistsError):
        generate_demo_warehouse(existing)
    with pytest.raises(DemoPathError):
        generate_demo_warehouse(tmp_path / "nope" / "demo.duckdb")
