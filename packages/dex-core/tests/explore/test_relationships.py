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
    data_quality_notes,
    detect_grain,
    fk_candidate_count,
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


# --- grain and data-quality notes ----------------------------------------------


def test_own_key_duplicates_produce_fan_out_warning():
    hosts = _ds(
        "db.main.RAW_HOSTS",
        [_col("ID", distinct=9590), _col("NAME", "VARCHAR")],
        rows=14111,
    )
    notes = data_quality_notes(hosts)
    warning = next(n for n in notes if "not unique" in n)
    assert "ID is not unique: 9590 distinct over 14111 rows" in warning
    assert "4521 duplicate rows" in warning
    assert "fan out" in warning
    assert any("grain unknown" in n for n in notes)


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
