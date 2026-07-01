"""PII detection and profile-level interpretation (grain and data-quality notes).

The fixtures mirror the shapes that produced false negatives in the field: an
Airbnb-style raw export with bare `NAME`, `REVIEWER_NAME`, and free-text
`COMMENTS` columns, and a non-unique `ID` on an un-deduplicated snapshot feed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import PIICategory
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.profile import detect_pii, is_min_max_safe


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


# --- detect_pii: name patterns ------------------------------------------------


@pytest.mark.parametrize(
    ("column", "data_type", "category"),
    [
        # Exact person tokens, any type, unchanged behavior.
        ("first_name", "VARCHAR", PIICategory.NAME),
        ("surname", "VARCHAR", PIICategory.NAME),
        ("dob", "DATE", PIICategory.DOB),
        ("email", "VARCHAR", PIICategory.EMAIL),
        # camelCase matches the same patterns as snake_case.
        ("firstName", "VARCHAR", PIICategory.NAME),
        ("emailAddress", "VARCHAR", PIICategory.EMAIL),
        # Generic name columns: the field false negatives.
        ("NAME", "VARCHAR", PIICategory.NAME),
        ("REVIEWER_NAME", "VARCHAR", PIICategory.NAME),
        ("host_name", "VARCHAR", PIICategory.NAME),
        # Free-text fields reliably carry PII in their values.
        ("COMMENTS", "VARCHAR", PIICategory.FREE_TEXT),
        ("comment", "TEXT", PIICategory.FREE_TEXT),
        ("notes", "VARCHAR", PIICategory.FREE_TEXT),
        ("review_text", "VARCHAR", PIICategory.FREE_TEXT),
        ("feedback", "STRING", PIICategory.FREE_TEXT),
    ],
)
def test_detect_pii_flags(column: str, data_type: str, category: PIICategory):
    flag = detect_pii(column, data_type)
    assert flag is not None, column
    assert flag.category == category
    assert 0 < flag.confidence <= 0.95


@pytest.mark.parametrize(
    ("column", "data_type"),
    [
        # Technical/organizational qualifiers are not person names.
        ("table_name", "VARCHAR"),
        ("column_name", "VARCHAR"),
        ("file_name", "VARCHAR"),
        ("product_name", "VARCHAR"),
        ("model_name", "VARCHAR"),
        # The weak patterns are string-only: a numeric `comments` is a count.
        ("comments", "INTEGER"),
        ("name", "INTEGER"),
        # Substrings without a word boundary do not over-trigger.
        ("username_hash", "VARCHAR"),
        ("emailable", "BOOLEAN"),
        ("filename", "VARCHAR"),
        ("total", "DECIMAL(10,2)"),
    ],
)
def test_detect_pii_does_not_flag(column: str, data_type: str):
    assert detect_pii(column, data_type) is None, column


def test_generic_name_is_weaker_than_exact_person_tokens():
    exact = detect_pii("last_name", "VARCHAR")
    generic = detect_pii("reviewer_name", "VARCHAR")
    free_text = detect_pii("comments", "VARCHAR")
    assert exact.confidence > generic.confidence > free_text.confidence


def test_new_flags_suppress_min_max():
    """Broader detection must tighten the envelope: every newly flagged column
    loses its min/max, same as the exact-token categories always did."""

    for column in ("NAME", "REVIEWER_NAME", "COMMENTS"):
        flag = detect_pii(column, "VARCHAR")
        assert flag is not None
        assert not is_min_max_safe("VARCHAR", flag)


# --- envelope: the Airbnb-shaped session --------------------------------------


def test_airbnb_pii_columns_are_flagged_with_min_max_suppressed(
    airbnb_duckdb: Path, capsys
):
    payload = _run(
        [
            "explore",
            "profile",
            "RAW_HOSTS",
            "RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
        ],
        capsys,
    )
    ds = {d["identifier"].split(".")[-1]: d for d in payload["data"]["datasets"]}
    hosts = {c["name"]: c for c in ds["RAW_HOSTS"]["columns"]}
    reviews = {c["name"]: c for c in ds["RAW_REVIEWS"]["columns"]}

    assert hosts["NAME"]["pii"]["category"] == "name"
    assert reviews["REVIEWER_NAME"]["pii"]["category"] == "name"
    assert reviews["COMMENTS"]["pii"]["category"] == "free_text"
    for col in (hosts["NAME"], reviews["REVIEWER_NAME"], reviews["COMMENTS"]):
        assert col["min_value"] is None and col["max_value"] is None
        assert set(col["pii"]) == {"category", "confidence"}  # never a value


def test_non_unique_id_gets_fan_out_warning(airbnb_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "RAW_HOSTS", "--path", str(airbnb_duckdb)], capsys
    )
    hosts = payload["data"]["datasets"][0]
    warning = next(n for n in hosts["data_quality"] if "not unique" in n)
    assert "ID is not unique: 2 distinct over 3 rows" in warning
    assert "fan out" in warning
    # With no unique column at all, the grain is explicitly unknown, not silent.
    assert any("grain unknown" in n for n in hosts["data_quality"])
    assert hosts["candidate_keys"] == []
    assert hosts["grain"] is None


def test_clean_table_gets_no_warnings_and_a_grain(airbnb_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "RAW_LISTINGS", "--path", str(airbnb_duckdb)], capsys
    )
    listings = payload["data"]["datasets"][0]
    assert listings["data_quality"] == []
    assert ["ID"] in listings["candidate_keys"]
    assert listings["grain"] == ["ID"]


def test_repeated_foreign_key_is_not_warned_as_broken_grain(duckdb_file: Path, capsys):
    """orders.customer_id repeats by design (a child table); only the table's own
    key column may trigger the fan-out warning."""

    payload = _run(["explore", "profile", "orders", "--path", str(duckdb_file)], capsys)
    orders = payload["data"]["datasets"][0]
    assert not any("customer_id" in n for n in orders["data_quality"])


def test_profile_accepts_comma_separated_objects(airbnb_duckdb: Path, capsys):
    payload = _run(
        [
            "explore",
            "profile",
            "RAW_HOSTS,RAW_LISTINGS, RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
        ],
        capsys,
    )
    names = {d["identifier"].split(".")[-1] for d in payload["data"]["datasets"]}
    assert names == {"RAW_HOSTS", "RAW_LISTINGS", "RAW_REVIEWS"}
