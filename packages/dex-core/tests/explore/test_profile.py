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
    assert "ID is not unique: ~2 distinct over 3 rows" in warning
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


# --- exact-count escalation ----------------------------------------------------


def test_near_unique_key_escalates_to_exact_and_confirms_grain(
    near_unique_duckdb: Path, capsys
):
    payload = _run(
        ["explore", "profile", "results", "--path", str(near_unique_duckdb)], capsys
    )
    results = payload["data"]["datasets"][0]
    key = {c["name"]: c for c in results["columns"]}["resultId"]
    assert key["distinct_count"] == 50000
    assert key["distinct_count_exact"] is True
    assert key["is_unique"] is True
    assert ["resultId"] in results["candidate_keys"]
    assert results["grain"] == ["resultId"]
    assert not any("not unique" in n for n in results["data_quality"])
    assert not any("grain unknown" in n for n in results["data_quality"])


def test_true_duplicates_still_warn_with_exact_counts(near_unique_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "dupes", "--path", str(near_unique_duckdb)], capsys
    )
    dupes = payload["data"]["datasets"][0]
    warning = next(n for n in dupes["data_quality"] if "not unique" in n)
    assert "id is not unique: 45000 distinct over 50000 rows" in warning
    assert "fan out" in warning


class _StubAdapter:
    """Metadata-only double: crafted approximate aggregates, recorded escalations."""

    name = "stub"
    dialect = "duckdb"

    def __init__(self, rows: int, approx: dict[str, int]):
        self.rows = rows
        self.approx = approx
        self.calls: list[list[str]] = []

    def table_metadata(self, identifier):
        from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta

        meta = ObjectMeta(
            identifier=identifier,
            object_type="table",
            schema="s",
            name=identifier.rsplit(".", 1)[-1],
            row_count=self.rows,
            byte_size=None,
            column_count=len(self.approx),
        )
        columns = [
            ColumnMeta(name=n, data_type="INTEGER", nullable=True, ordinal=i)
            for i, n in enumerate(self.approx)
        ]
        return meta, columns

    def column_aggregates(self, identifier, columns, *, safe_min_max=None):
        from exmergo_dex_core.adapters.base import ColumnAggregate

        return [
            ColumnAggregate(
                name=c.name,
                null_fraction=0.0,
                distinct_count=self.approx[c.name],
                is_unique=False,
                min_value=None,
                max_value=None,
            )
            for c in columns
        ]

    def exact_distinct_counts(self, identifier, columns):
        self.calls.append(list(columns))
        return {n: (self.rows if n == "overshoot" else self.rows - 10) for n in columns}


def test_escalation_policy_is_bounded_and_targeted():
    from exmergo_dex_core.explore import profile as profile_mod

    # An approx overshooting the row count (the field signature of a real key),
    # ten in-band columns to overflow the cap, and one far below the band.
    approx = {
        "overshoot": 1010,
        **{f"near_{i}": 950 + i for i in range(10)},
        "low": 600,
    }
    adapter = _StubAdapter(rows=1000, approx=approx)
    datasets = profile_mod.profile(adapter, ["db.s.t"])

    assert len(adapter.calls) == 1, "all escalations batch into one adapter call"
    chosen = adapter.calls[0]
    assert len(chosen) == 8
    assert "overshoot" in chosen, "smallest gaps win and overshoot's gap is 10"
    assert "low" not in chosen
    assert "near_0" not in chosen and "near_1" not in chosen

    cols = {c.name: c for c in datasets[0].columns}
    assert cols["overshoot"].distinct_count == 1000
    assert cols["overshoot"].distinct_count_exact is True
    assert cols["overshoot"].is_unique is True
    assert cols["near_9"].distinct_count == 990
    assert cols["near_9"].is_unique is False
    assert cols["low"].distinct_count == 600
    assert cols["low"].distinct_count_exact is False


def test_adapter_without_exact_counts_degrades_gracefully():
    from exmergo_dex_core.explore import profile as profile_mod

    adapter = _StubAdapter(rows=1000, approx={"id": 990})
    adapter.exact_distinct_counts = None  # shadow the method: adapter can't escalate
    datasets = profile_mod.profile(adapter, ["db.s.t"])
    col = datasets[0].columns[0]
    assert col.distinct_count == 990
    assert col.distinct_count_exact is False
    # In the noise band and unproven: no non-uniqueness verdict.
    assert not any("not unique" in n for n in datasets[0].data_quality)


def test_row_count_refreshes_after_the_aggregate_scan():
    """Adapters whose free row counts are planner estimates (Postgres
    reltuples) upgrade to the exact COUNT(*) the aggregate scan paid for; the
    profile engine must re-read the metadata so uniqueness proofs and the
    dataset row count compare against real rows, not the estimate."""

    from exmergo_dex_core.adapters.base import (
        ColumnAggregate,
        ColumnMeta,
        ObjectMeta,
    )
    from exmergo_dex_core.explore import profile as profile_mod

    class EstimatingAdapter:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.scanned = False

        def table_metadata(self, identifier):
            rows = 1000 if self.scanned else 1200  # estimate is stale-high
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name="t",
                row_count=rows,
                byte_size=None,
                column_count=1,
            )
            return meta, [
                ColumnMeta(name="id", data_type="INTEGER", nullable=False, ordinal=0)
            ]

        def column_aggregates(self, identifier, columns, *, safe_min_max=None):
            self.scanned = True
            return [
                ColumnAggregate(
                    name="id",
                    null_fraction=0.0,
                    distinct_count=990,  # near-unique against the REAL count
                    is_unique=None,
                    min_value=None,
                    max_value=None,
                )
            ]

        def exact_distinct_counts(self, identifier, columns):
            return dict.fromkeys(columns, 1000)

    datasets = profile_mod.profile(EstimatingAdapter(), ["db.s.t"])
    assert datasets[0].row_count == 1000  # the exact count, not the estimate
    id_col = datasets[0].columns[0]
    # 990 approx over 1000 real rows is in the escalation band; the exact scan
    # returns 1000 == 1000, a proof that would be missed against 1200.
    assert id_col.distinct_count == 1000
    assert id_col.distinct_count_exact is True
    assert id_col.is_unique is True
