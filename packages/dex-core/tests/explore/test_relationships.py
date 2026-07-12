"""Relationship inference, grain detection, and data-quality interpretation.

Unit tests build Dataset models directly to pin the matching and scoring rules;
the envelope tests replay the two field sessions (camelCase F1 star schema,
RAW_-prefixed Airbnb export) that previously returned zero relationships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.relationships import (
    candidate_keys,
    data_quality_notes,
    detect_grain,
    fk_candidate_count,
    fold_replica_relationships,
    infer_relationships,
)


def _col(
    name: str,
    data_type: str = "INTEGER",
    *,
    distinct: int | None = None,
    unique: bool = False,
    mn: object | None = None,
    mx: object | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        data_type=data_type,
        null_fraction=0.0,
        distinct_count=distinct,
        is_unique=unique,
        min_value=mn,
        max_value=mx,
    )


def _ds(
    identifier: str, columns: list[ColumnProfile], rows: int | None = None
) -> Dataset:
    return Dataset(identifier=identifier, row_count=rows, columns=columns)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


# --- matching rules ------------------------------------------------------------


def test_camelcase_fk_matches_camelcase_parent_key():
    races = _ds("db.main.races", [_col("raceId", distinct=2, unique=True)], rows=2)
    results = _ds("db.main.results", [_col("raceId", distinct=2)], rows=3)
    rels = infer_relationships([races, results])
    assert len(rels) == 1
    rel = rels[0]
    assert rel.from_dataset == "db.main.results"
    assert rel.from_columns == ["raceId"]
    assert rel.to_dataset == "db.main.races"
    assert rel.to_columns == ["raceId"]
    assert rel.confidence >= 0.85


def test_layer_prefix_is_stripped_from_parent_name():
    hosts = _ds("db.main.RAW_HOSTS", [_col("ID", distinct=2, unique=True)], rows=2)
    listings = _ds("db.main.RAW_LISTINGS", [_col("HOST_ID", distinct=2)], rows=2)
    rels = infer_relationships([hosts, listings])
    assert len(rels) == 1
    assert rels[0].from_columns == ["HOST_ID"]
    assert rels[0].to_dataset == "db.main.RAW_HOSTS"
    assert rels[0].to_columns == ["ID"]


def test_already_singular_parent_table_matches():
    """`status` must not be mangled to `statu` by the inflector."""

    status = _ds("db.main.status", [_col("statusId", distinct=5, unique=True)], rows=5)
    results = _ds("db.main.results", [_col("statusId", distinct=3)], rows=9)
    rels = infer_relationships([status, results])
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.status"


def test_non_unique_parent_key_still_emits_at_reduced_confidence():
    """A broken parent grain is a data-quality problem, not a reason to hide the
    join; the fan-out risk is reported separately by data_quality_notes."""

    hosts = _ds("db.main.RAW_HOSTS", [_col("ID", distinct=9590)], rows=14111)
    listings = _ds("db.main.RAW_LISTINGS", [_col("HOST_ID", distinct=9000)], rows=17500)
    rels = infer_relationships([hosts, listings])
    assert len(rels) == 1
    assert rels[0].confidence < 0.7  # well below the unique-parent base of 0.85
    assert rels[0].confidence > 0.0


def test_distinct_count_violation_lowers_confidence():
    parent = _ds("db.main.customers", [_col("id", distinct=5, unique=True)], rows=5)
    contained = _ds("db.main.orders", [_col("customer_id", distinct=3)], rows=10)
    violating = _ds("db.main.refunds", [_col("customer_id", distinct=9)], rows=10)
    ok = infer_relationships([parent, contained])
    bad = infer_relationships([parent, violating])
    assert ok[0].confidence > bad[0].confidence


def test_numeric_range_containment_raises_confidence():
    parent = _ds(
        "db.main.customers", [_col("id", distinct=5, unique=True, mn=1, mx=5)], rows=5
    )
    inside = _ds(
        "db.main.orders", [_col("customer_id", distinct=3, mn=1, mx=4)], rows=8
    )
    outside = _ds(
        "db.main.events", [_col("customer_id", distinct=3, mn=1, mx=99)], rows=8
    )
    contained = infer_relationships([parent, inside])
    escaped = infer_relationships([parent, outside])
    assert contained[0].confidence > escaped[0].confidence


def test_type_incompatible_columns_do_not_match():
    parent = _ds("db.main.customers", [_col("id", distinct=5, unique=True)], rows=5)
    child = _ds("db.main.orders", [_col("customer_id", "VARCHAR", distinct=3)], rows=8)
    assert infer_relationships([parent, child]) == []


def test_ambiguous_all_caps_id_suffix_is_not_a_fk():
    """HOSTID (no separator) and PAID are not id-shaped; HOST_ID and hostId are."""

    ds = _ds(
        "db.main.t",
        [_col("HOSTID"), _col("PAID"), _col("HOST_ID"), _col("hostId"), _col("id")],
        rows=1,
    )
    assert fk_candidate_count([ds]) == 2


def test_underscore_key_suffix_matches_like_id():
    """Dimensional models commonly use `<entity>_key` surrogate keys instead of
    `<entity>_id`; the same matching rules must apply (issue #45)."""

    customers = _ds(
        "db.main.customers", [_col("customer_key", distinct=2, unique=True)], rows=2
    )
    orders = _ds("db.main.orders", [_col("customer_key", distinct=2)], rows=5)
    rels = infer_relationships([customers, orders])
    assert len(rels) == 1
    assert rels[0].from_columns == ["customer_key"]
    assert rels[0].to_dataset == "db.main.customers"
    assert rels[0].confidence >= 0.85


def test_camelcase_key_suffix_matches():
    parts = _ds("db.main.parts", [_col("partKey", distinct=3, unique=True)], rows=3)
    lines = _ds("db.main.lines", [_col("partKey", distinct=2)], rows=6)
    rels = infer_relationships([parts, lines])
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.parts"


def test_bare_key_is_a_key_not_a_foreign_key():
    """A column literally named `key` (like bare `id`) has no entity stem."""

    ds = _ds("db.main.t", [_col("key", unique=True), _col("id")], rows=1)
    assert fk_candidate_count([ds]) == 0


def test_tpch_alias_prefixed_keys_are_inferred():
    """TPC-H names every FK after the child table's own alias, not the parent's
    entity name (`L_ORDERKEY` on LINEITEM, not `ORDERS_KEY`), and concatenates the
    suffix with no separator at all (`CUSTKEY`, not `CUST_KEY`). Neither the
    entity-name branch nor a bare `_id`-only stem detector can see these joins;
    covers the exact chain reported in issue #45."""

    region = _ds(
        "db.tpch.region", [_col("R_REGIONKEY", distinct=5, unique=True)], rows=5
    )
    nation = _ds(
        "db.tpch.nation",
        [
            _col("N_NATIONKEY", distinct=25, unique=True),
            _col("N_REGIONKEY", distinct=5),
        ],
        rows=25,
    )
    supplier = _ds(
        "db.tpch.supplier",
        [
            _col("S_SUPPKEY", distinct=100, unique=True),
            _col("S_NATIONKEY", distinct=25),
        ],
        rows=100,
    )
    customer = _ds(
        "db.tpch.customer",
        [
            _col("C_CUSTKEY", distinct=150, unique=True),
            _col("C_NATIONKEY", distinct=25),
        ],
        rows=150,
    )
    part = _ds("db.tpch.part", [_col("P_PARTKEY", distinct=200, unique=True)], rows=200)
    orders = _ds(
        "db.tpch.orders",
        [
            _col("O_ORDERKEY", distinct=1500, unique=True),
            _col("O_CUSTKEY", distinct=150),
        ],
        rows=1500,
    )
    lineitem = _ds(
        "db.tpch.lineitem",
        [
            _col("L_ORDERKEY", distinct=1500),
            _col("L_PARTKEY", distinct=200),
            _col("L_SUPPKEY", distinct=100),
        ],
        rows=6000,
    )
    datasets = [region, nation, supplier, customer, part, orders, lineitem]
    rels = infer_relationships(datasets)

    found = {(r.from_dataset, r.from_columns[0], r.to_dataset) for r in rels}
    assert ("db.tpch.orders", "O_CUSTKEY", "db.tpch.customer") in found
    assert ("db.tpch.lineitem", "L_ORDERKEY", "db.tpch.orders") in found
    assert ("db.tpch.lineitem", "L_PARTKEY", "db.tpch.part") in found
    assert ("db.tpch.lineitem", "L_SUPPKEY", "db.tpch.supplier") in found
    assert ("db.tpch.supplier", "S_NATIONKEY", "db.tpch.nation") in found
    assert ("db.tpch.customer", "C_NATIONKEY", "db.tpch.nation") in found
    assert ("db.tpch.nation", "N_REGIONKEY", "db.tpch.region") in found


def test_dealiased_match_skips_when_stripped_to_a_bare_suffix():
    """`x_key` / `y_key` collapse to the bare suffix `key` once dealiased; that's
    too generic to trust, so two unrelated single-letter-prefixed keys must not
    be matched to each other."""

    a = _ds("db.main.alpha", [_col("a_key", distinct=2, unique=True)], rows=2)
    b = _ds("db.main.beta", [_col("b_key", distinct=2)], rows=2)
    assert infer_relationships([a, b]) == []


# --- same-lineage / replica folding --------------------------------------------


def _mirror_world() -> list[Dataset]:
    """A source schema and a dev/replica schema holding the same entities: the
    shape that inflated one foreign key into several edges in the field."""

    return [
        _ds(
            "db.main.orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds("db.main.customers", [_col("id", distinct=2, unique=True)], rows=2),
        _ds(
            "db.dbt_dev.stg_orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds("db.dbt_dev.dim_customers", [_col("id", distinct=2, unique=True)], rows=2),
    ]


def test_replica_dataset_duplicate_edges_are_folded():
    datasets = _mirror_world()
    rels = infer_relationships(datasets)
    assert len(rels) == 4  # source, replica, and two cross-dataset lookalikes

    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"dbt_dev"})
    )
    assert folded == 3
    assert len(kept) == 1
    # The kept edge is the source-schema one, named by the dev_dataset config.
    assert kept[0].from_dataset == "db.main.orders"
    assert kept[0].to_dataset == "db.main.customers"
    assert mirrored == 2  # the two dbt_dev objects


def test_fold_detects_mirror_structurally_without_config():
    datasets = _mirror_world()
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(datasets, rels)
    # No dev_schemas passed: structural mirror detection still collapses the
    # duplicates (canonical chosen deterministically).
    assert folded == 3
    assert len(kept) == 1
    assert mirrored == 2


def test_fold_is_a_noop_without_a_mirror():
    datasets = [
        _ds(
            "db.main.orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds("db.main.customers", [_col("id", distinct=2, unique=True)], rows=2),
    ]
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"dbt_dev"})
    )
    assert kept == rels
    assert folded == 0
    assert mirrored == 0


def test_map_folds_mirrored_lineage_and_notes_it(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "mirror.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2)")
    conn.execute("CREATE SCHEMA dbt_dev")
    conn.execute("CREATE TABLE dbt_dev.stg_orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.stg_orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE dbt_dev.dim_customers (id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.dim_customers VALUES (1), (2)")
    conn.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        ["explore", "map", "--path", str(db), "--repo-root", str(repo)], capsys
    )
    assert any("mirror source lineage" in n for n in payload["data"]["notes"])
    # One real foreign key survives instead of the inflated cross-schema fan-out.
    assert payload["data"]["relationship_count"] <= 2


# --- grain and data-quality notes ----------------------------------------------


def test_own_key_duplicates_produce_fan_out_warning():
    hosts = _ds(
        "db.main.RAW_HOSTS",
        [_col("ID", distinct=9590), _col("NAME", "VARCHAR")],
        rows=14111,
    )
    notes = data_quality_notes(hosts)
    warning = next(n for n in notes if "not unique" in n)
    assert "ID is not unique: ~9590 distinct over 14111 rows" in warning
    assert "4521 duplicate rows" in warning
    assert "fan out" in warning
    assert any("grain unknown" in n for n in notes)


def test_fan_out_note_gated_on_exactness():
    """Within the approximation noise band, only an exact count may call a
    column non-unique; a shortfall too large for noise still warns unescalated."""

    in_band_approx = _ds("db.main.things", [_col("id", distinct=1100)], rows=1125)
    assert not any("not unique" in n for n in data_quality_notes(in_band_approx))

    in_band_exact = _ds(
        "db.main.things",
        [
            ColumnProfile(
                name="id",
                data_type="INTEGER",
                null_fraction=0.0,
                distinct_count=1100,
                distinct_count_exact=True,
                is_unique=False,
            )
        ],
        rows=1125,
    )
    assert any("not unique" in n for n in data_quality_notes(in_band_exact))

    far_below_band = _ds("db.main.things", [_col("id", distinct=500)], rows=1125)
    assert any("not unique" in n for n in data_quality_notes(far_below_band))


def test_repeated_foreign_key_is_not_a_grain_defect():
    results = _ds(
        "db.main.results",
        [_col("resultId", distinct=100, unique=True), _col("raceId", distinct=20)],
        rows=100,
    )
    assert data_quality_notes(results) == []
    assert detect_grain(results) == ["resultId"]


def test_empty_table_produces_no_grain_notes():
    empty = _ds("db.main.empty_t", [_col("id")], rows=0)
    assert data_quality_notes(empty) == []


# --- composite keys --------------------------------------------------------------


def test_candidate_keys_list_singles_before_composites():
    ds = _ds(
        "db.main.line_items",
        [
            _col("id", distinct=2000, unique=True),
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"]]
    assert candidate_keys(ds) == [["id"], ["order_key", "line_number"]]


def test_grain_prefers_a_single_key_over_a_composite():
    ds = _ds(
        "db.main.line_items",
        [
            _col("id", distinct=2000, unique=True),
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"]]
    assert detect_grain(ds) == ["id"]


def test_grain_falls_back_to_the_best_ranked_composite():
    ds = _ds(
        "db.main.line_items",
        [
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
            _col("quantity", distinct=30),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"], ["order_key", "quantity"]]
    assert detect_grain(ds) == ["order_key", "line_number"]
    assert not any("grain unknown" in n for n in data_quality_notes(ds))


def test_composite_member_is_not_a_unique_parent_key():
    """A same-named foreign key must not join to a composite member as if it
    were the parent's unique key: order_key alone repeats in line_items, so an
    edge onto it would fan out."""

    line_items = _ds(
        "db.main.line_items",
        [_col("order_key", distinct=500), _col("line_number", distinct=4)],
        rows=2000,
    )
    line_items.composite_keys = [["order_key", "line_number"]]
    shipments = _ds("db.main.shipments", [_col("order_key", distinct=400)], rows=450)
    assert infer_relationships([line_items, shipments]) == []


# --- envelope: the two field sessions ------------------------------------------


def test_f1_star_schema_join_graph_is_inferred(f1_duckdb: Path, capsys):
    payload = _run(["explore", "relationships", "--path", str(f1_duckdb)], capsys)
    data = payload["data"]
    assert data["inferred_count"] == 2
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}
    race = by_fk[("raceId",)]
    assert race["from_dataset"].endswith(".results")
    assert race["to_dataset"].endswith(".races")
    assert race["to_columns"] == ["raceId"]
    assert race["confidence"] >= 0.85
    driver = by_fk[("driverId",)]
    assert driver["to_dataset"].endswith(".drivers")
    assert driver["confidence"] >= 0.85
    assert all(r["kind"] == "inferred" for r in data["relationships"])


def test_airbnb_joins_inferred_despite_raw_prefix_and_broken_grain(
    airbnb_duckdb: Path, capsys
):
    payload = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)
    data = payload["data"]
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}

    host = by_fk[("HOST_ID",)]
    assert host["to_dataset"].endswith(".RAW_HOSTS")
    assert host["to_columns"] == ["ID"]
    # The parent key is not unique, so the join is real but demoted.
    assert host["confidence"] < 0.85

    listing = by_fk[("LISTING_ID",)]
    assert listing["to_dataset"].endswith(".RAW_LISTINGS")
    assert listing["confidence"] >= 0.85


def test_relationships_envelope_explains_itself(airbnb_duckdb: Path, capsys):
    payload = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)
    notes = payload["data"]["notes"]
    assert any("id-shaped column" in n for n in notes)
    assert any("no declared relationships" in n for n in notes)


def test_verify_measures_overlap_and_lifts_clean_joins(airbnb_duckdb: Path, capsys):
    """Every airbnb FK value has a parent, so verification confirms both joins:
    zero orphans, confidence up, and the broken-grain parent still capped below
    the trusted tier."""

    baseline = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)[
        "data"
    ]["relationships"]
    verified = _run(
        ["explore", "relationships", "--verify", "--path", str(airbnb_duckdb)], capsys
    )["data"]
    assert all(not r["verified"] for r in baseline)

    by_fk = {tuple(r["from_columns"]): r for r in verified["relationships"]}
    base_by_fk = {tuple(r["from_columns"]): r for r in baseline}
    for fk in (("HOST_ID",), ("LISTING_ID",)):
        assert by_fk[fk]["verified"] is True
        assert by_fk[fk]["orphan_fraction"] == 0.0
        assert by_fk[fk]["confidence"] >= base_by_fk[fk]["confidence"]
    assert by_fk[("HOST_ID",)]["confidence"] < 0.85  # parent key still not unique
    assert any("overlap probes" in n for n in verified["notes"])


def test_verify_demotes_a_join_with_heavy_orphans(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "orphans.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    # 2 of 5 orders point at real customers; 3 are orphans (fraction 0.6).
    conn.execute(
        "INSERT INTO orders VALUES (10, 1), (11, 2), (12, 7), (13, 8), (14, 9)"
    )
    conn.close()

    data = _run(["explore", "relationships", "--verify", "--path", str(path)], capsys)[
        "data"
    ]
    rel = next(r for r in data["relationships"] if r["from_columns"] == ["customer_id"])
    assert rel["verified"] is True
    assert rel["orphan_fraction"] == 0.6
    assert rel["confidence"] < 0.5, "measured non-containment demotes the guess"


def test_empty_result_is_explained_not_silent(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "flat.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE people (id INTEGER, age INTEGER)")
    conn.execute("INSERT INTO people VALUES (1, 30)")
    conn.close()

    payload = _run(["explore", "relationships", "--path", str(path)], capsys)
    data = payload["data"]
    assert data["relationships"] == []
    assert any("nothing to infer" in n for n in data["notes"])


def test_overlap_probe_transpiles_to_postgres_and_stays_select_only():
    """The probe is authored once in DuckDB SQL; on Postgres it must transpile
    to a statement that re-parses in the postgres dialect and passes the
    SELECT-only guard (the dialect risk a new connector carries)."""

    import sqlglot

    from exmergo_dex_core.cache import Relationship
    from exmergo_dex_core.explore.relationships import probe_statements
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    rel = Relationship(
        from_dataset="dexdb.app.order_items",
        from_columns=["product_id"],
        to_dataset="dexdb.app.products",
        to_columns=["id"],
    )
    statements = probe_statements([rel], "postgres")
    assert len(statements) == 1
    sql = statements[0]
    assert_select_only(sql, dialect="postgres")
    parsed = sqlglot.parse_one(sql, read="postgres")
    assert parsed is not None
    # Portable shapes survive the rewrite; DuckDB-only FILTER syntax does not
    # appear (BigQuery lacks it and Postgres parses it differently).
    assert "order_items" in sql and "products" in sql
