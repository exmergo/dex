"""The Elementary reader checks table layouts and refuses one it does not know (#1469).

Each acceptance item is a pair: a catalog built to break the layout and the
recorded layout passing. The fixture's own tables are read back in
``tests/transform/test_elementary_fixture.py``, which needs the package.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from exmergo_dex_core.adapters.base import ColumnMeta
from exmergo_dex_core.maintain import elementary_layouts as layouts
from exmergo_dex_core.maintain.elementary_layouts import (
    CATALOG_TOO_LARGE,
    COLUMN_MISSING,
    COLUMN_RETYPED,
    MAX_CATALOG_COLUMNS,
    READER_RELEASES,
    TABLE_MISSING,
    ElementaryLayoutError,
    adapter_catalog,
    check_layouts,
    layout_for,
    parse_layouts,
    reader_status,
    type_family,
)
from exmergo_dex_core.transform.elementary import (
    NEWEST_TESTED,
    RELEASE_UNKNOWN,
    RELEASE_UNTESTED,
    TESTED_RELEASES,
    release_key,
)

RELEASE = "0.26.1"
TABLES = (
    "elementary_test_results",
    "dbt_invocations",
    "test_result_rows",
    "data_monitoring_metrics",
)

#: What dbt-duckdb builds each of the package's type names as, at 0.26.1: its
#: ``edr_type_*`` macros fall to the defaults (``varchar(4096)`` for both
#: strings, dbt's own ``timestamp``, ``integer``, ``bigint``, ``float`` and
#: ``boolean``). The fixture test reads the real catalog.
DUCKDB_TYPES = {
    "long_string": "VARCHAR",
    "string": "VARCHAR",
    "timestamp": "TIMESTAMP",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
}


def _columns(table: str, release: str = RELEASE) -> list[ColumnMeta]:
    layout = layout_for(release).tables[table]
    return [
        ColumnMeta(name=name, data_type=DUCKDB_TYPES[kind], nullable=True, ordinal=i)
        for i, (name, kind) in enumerate(layout.columns)
    ]


class FakeCatalog:
    """The recorded layout as a catalog lists it, with edits, and every call kept."""

    def __init__(self, tables: dict[str, list[ColumnMeta]] | None = None) -> None:
        self.tables = (
            {table: _columns(table) for table in TABLES} if tables is None else tables
        )
        self.calls: list[str] = []

    def __call__(self, table: str) -> list[ColumnMeta]:
        self.calls.append(table)
        return self.tables.get(table, [])

    def drop(self, table: str, column: str) -> FakeCatalog:
        self.tables[table] = [c for c in self.tables[table] if c.name != column]
        return self

    def retype(self, table: str, column: str, data_type: str) -> FakeCatalog:
        self.tables[table] = [
            ColumnMeta(c.name, data_type, c.nullable, c.ordinal)
            if c.name == column
            else c
            for c in self.tables[table]
        ]
        return self


def _refusal(catalog: FakeCatalog, release: str | None = RELEASE):
    with pytest.raises(ElementaryLayoutError) as caught:
        check_layouts(release, catalog)
    return caught.value


# --- a missing column -------------------------------------------------------------


def test_the_recorded_layout_passes_and_names_every_column_it_reads():
    check = check_layouts(RELEASE, FakeCatalog())

    assert check.release == RELEASE
    assert [table.table for table in check.tables] == list(TABLES)
    for table in check.tables:
        assert set(table.columns) == set(layout_for(RELEASE).tables[table.table].reads)


def test_a_missing_column_refuses_naming_the_table_and_the_column():
    error = _refusal(FakeCatalog().drop("dbt_invocations", "created_at"))

    assert error.code == COLUMN_MISSING
    [problem] = error.problems
    assert (problem.code, problem.table, problem.column) == (
        COLUMN_MISSING,
        "dbt_invocations",
        "created_at",
    )
    assert "dbt_invocations.created_at" in str(error)


def test_a_column_the_reader_does_not_use_may_be_absent():
    # `result_rows` holds sampled rows, which the reader never selects.
    check_layouts(RELEASE, FakeCatalog().drop("elementary_test_results", "result_rows"))


def test_every_problem_is_named_not_only_the_first():
    catalog = (
        FakeCatalog()
        .drop("elementary_test_results", "status")
        .retype("data_monitoring_metrics", "bucket_end", "VARCHAR")
    )
    error = _refusal(catalog)

    assert [(p.code, p.table, p.column) for p in error.problems] == [
        (COLUMN_MISSING, "elementary_test_results", "status"),
        (COLUMN_RETYPED, "data_monitoring_metrics", "bucket_end"),
    ]


# --- a retyped column -------------------------------------------------------------


def test_a_timestamp_retyped_to_a_string_refuses():
    error = _refusal(
        FakeCatalog().retype("elementary_test_results", "detected_at", "VARCHAR")
    )

    assert error.code == COLUMN_RETYPED
    [problem] = error.problems
    assert (problem.table, problem.column) == ("elementary_test_results", "detected_at")
    assert (problem.expected, problem.found) == ("timestamp", "VARCHAR")


def test_the_recorded_type_passes_as_each_connector_spells_it():
    spellings = {
        "timestamp without time zone": "Postgres, Redshift",
        "TIMESTAMP_NTZ": "Snowflake",
        "timestamp(6)": "Trino",
        "Nullable(DateTime64(6, 'UTC'))": "ClickHouse",
    }
    for spelling in spellings:
        catalog = FakeCatalog().retype(
            "elementary_test_results", "detected_at", spelling
        )
        check_layouts(RELEASE, catalog)


def test_dbt_invocations_run_times_are_strings_at_0_26_1():
    # The package declares run_started_at as `string`, so a timestamp there is a
    # layout the reader does not know, and the text it holds is what it reads.
    error = _refusal(
        FakeCatalog().retype("dbt_invocations", "run_started_at", "TIMESTAMP")
    )

    [problem] = error.problems
    assert (problem.column, problem.expected, problem.found) == (
        "run_started_at",
        "text",
        "TIMESTAMP",
    )


@pytest.mark.parametrize(
    ("spelling", "family"),
    [
        ("VARCHAR", "text"),
        ("character varying(4096)", "text"),
        ("String", "text"),
        ("Nullable(String)", "text"),
        ("TEXT", "text"),
        ("STRING", "text"),
        ("BIGINT", "integer"),
        ("INT64", "integer"),
        ("Int32", "integer"),
        ("NUMBER(38,0)", "integer"),
        ("FIXED", "integer"),
        ("FLOAT", "float"),
        ("double precision", "float"),
        ("FLOAT64", "float"),
        ("REAL", "float"),
        ("TIMESTAMP", "timestamp"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("DateTime", "timestamp"),
        ("BOOLEAN", "boolean"),
        ("Bool", "boolean"),
        ("DECIMAL(10,2)", None),
        ("DATE", None),
        ("ARRAY<STRING>", None),
        ("VARIANT", None),
    ],
)
def test_a_catalog_type_falls_in_one_family_or_none(spelling: str, family: str | None):
    assert type_family(spelling) == family


def test_a_type_in_no_family_refuses_rather_than_passing():
    error = _refusal(
        FakeCatalog().retype("elementary_test_results", "failures", "VARIANT")
    )

    assert error.problems[0].code == COLUMN_RETYPED


# --- the release, before the catalog ----------------------------------------------


def _patch_above(release: str) -> str:
    major, minor, patch = release.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


@pytest.mark.parametrize(
    "release", [_patch_above(NEWEST_TESTED), "0.27.0", "1.0.0", "0.23.0", "0.16.4"]
)
def test_a_release_outside_the_reader_s_range_refuses_without_querying_the_catalog(
    release: str,
):
    catalog = FakeCatalog()
    error = _refusal(catalog, release)

    assert error.code == RELEASE_UNTESTED
    assert error.problems == ()
    assert catalog.calls == []


@pytest.mark.parametrize("release", [None, "", "{{ var('x') }}", "latest"])
def test_a_release_that_cannot_be_read_refuses_without_querying_the_catalog(
    release: str | None,
):
    catalog = FakeCatalog()
    error = _refusal(catalog, release)

    assert error.code == RELEASE_UNKNOWN
    assert catalog.calls == []


@pytest.mark.parametrize("release", ["0.26.1", "v0.26.1"])
def test_a_release_inside_the_range_runs_the_check(release: str):
    catalog = FakeCatalog()

    check_layouts(release, catalog)

    assert catalog.calls == list(TABLES)


def test_the_check_reads_only_the_tables_a_caller_names():
    catalog = FakeCatalog()

    check = check_layouts(
        RELEASE, catalog, tables=["elementary_test_results", "dbt_invocations"]
    )

    assert catalog.calls == ["elementary_test_results", "dbt_invocations"]
    assert len(check.tables) == 2
    with pytest.raises(ValueError, match="no recorded layout"):
        check_layouts(RELEASE, catalog, tables=["dbt_models"])


# --- the reader's range is tied to TESTED_RELEASES ---------------------------------


def test_every_reader_release_is_one_the_fixture_runs():
    tested = {release_key(release) for release in TESTED_RELEASES}
    assert READER_RELEASES == ("0.26.1",)
    assert all(release_key(release) in tested for release in READER_RELEASES)
    assert all(reader_status(release) == "supported" for release in READER_RELEASES)


def _one_release(release: str, reads: str = "[id]") -> str:
    return (
        f'releases:\n  "{release}":\n    tables:\n      t:\n        source: s\n'
        f"        columns:\n          - [id, long_string]\n        reads: {reads}\n"
    )


def test_a_layout_for_a_release_the_fixture_does_not_run_is_refused_on_load():
    assert parse_layouts(_one_release("0.26.1"))
    with pytest.raises(ValueError, match="not in TESTED_RELEASES"):
        parse_layouts(_one_release("0.20.0"))


def test_a_layout_that_reads_an_undeclared_column_is_refused_on_load():
    with pytest.raises(ValueError, match="undeclared"):
        parse_layouts(_one_release("0.26.1", reads="[id, status]"))


def test_a_layout_with_a_type_name_the_package_does_not_use_is_refused_on_load():
    misspelled = _one_release("0.26.1").replace("long_string", "timestmp")
    with pytest.raises(ValueError, match="timestmp"):
        parse_layouts(misspelled)


def test_no_recorded_read_holds_a_sampled_row_or_a_failing_value():
    held = {"result_rows", "result_row", "dimension_value", "source_value"}
    for table in layout_for(RELEASE).tables.values():
        assert held.isdisjoint(table.reads), table.table
        for name in table.reads:
            assert table.family(name) in {
                "text",
                "integer",
                "float",
                "timestamp",
                "boolean",
            }


# --- an empty answer, and a bounded one --------------------------------------------


def test_a_table_the_catalog_lists_nothing_for_is_missing_never_clean():
    catalog = FakeCatalog()
    catalog.tables["test_result_rows"] = []

    error = _refusal(catalog)

    assert [(p.code, p.table, p.column) for p in error.problems] == [
        (TABLE_MISSING, "test_result_rows", None)
    ]


def test_a_lookup_error_from_the_catalog_reads_as_a_missing_table():
    def catalog(table: str) -> list[ColumnMeta]:
        if table == "dbt_invocations":
            raise LookupError(table)
        return _columns(table)

    with pytest.raises(ElementaryLayoutError) as caught:
        check_layouts(RELEASE, catalog)

    assert caught.value.code == TABLE_MISSING


def test_a_listing_past_the_bound_refuses_without_reading_the_rest():
    pulled = 0

    def endless(table: str) -> Iterator[ColumnMeta]:
        nonlocal pulled
        yield from _columns(table)
        while True:
            pulled += 1
            yield ColumnMeta(f"extra_{pulled}", "VARCHAR", True, pulled)

    with pytest.raises(ElementaryLayoutError) as caught:
        check_layouts(RELEASE, endless, tables=["test_result_rows"])

    assert caught.value.code == CATALOG_TOO_LARGE
    assert pulled == MAX_CATALOG_COLUMNS + 1 - len(_columns("test_result_rows"))


def test_names_match_case_insensitively_and_the_catalog_s_spelling_is_kept():
    catalog = FakeCatalog(
        {
            table: [
                ColumnMeta(c.name.upper(), c.data_type, c.nullable, c.ordinal)
                for c in _columns(table)
            ]
            for table in TABLES
        }
    )

    check = check_layouts(RELEASE, catalog)

    [results] = [t for t in check.tables if t.table == "elementary_test_results"]
    assert results.columns["detected_at"] == "DETECTED_AT"


# --- through Dex's own DuckDB adapter ----------------------------------------------


def _warehouse(path: Path, *, skip: tuple[str, str] | None = None) -> Path:
    """A DuckDB file holding the four tables, built from the recorded layout."""

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    try:
        con.execute("create schema main_elementary")
        for table in TABLES:
            columns = ", ".join(
                f'"{name}" {DUCKDB_TYPES[kind]}'
                for name, kind in layout_for(RELEASE).tables[table].columns
                if (table, name) != skip
            )
            con.execute(f"create table main_elementary.{table} ({columns})")
    finally:
        con.close()
    return path


def test_the_layout_check_reads_the_catalog_through_the_duckdb_adapter(tmp_path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(_warehouse(tmp_path / "dev.duckdb"))
    try:
        check = check_layouts(RELEASE, adapter_catalog(adapter, "dev.main_elementary"))
        assert len(check.tables) == len(TABLES)
        # A schema that holds none of the tables lists nothing for any of them.
        with pytest.raises(ElementaryLayoutError) as caught:
            check_layouts(RELEASE, adapter_catalog(adapter, "dev.main"))
        assert {p.code for p in caught.value.problems} == {TABLE_MISSING}
        assert len(caught.value.problems) == len(TABLES)
    finally:
        adapter.close()


def test_a_column_dropped_in_the_warehouse_refuses_through_the_adapter(tmp_path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    path = _warehouse(
        tmp_path / "dev.duckdb", skip=("dbt_invocations", "invocation_id")
    )
    adapter = DuckDBAdapter(path)
    try:
        with pytest.raises(ElementaryLayoutError) as caught:
            check_layouts(RELEASE, adapter_catalog(adapter, "dev.main_elementary"))
    finally:
        adapter.close()

    [problem] = caught.value.problems
    assert (problem.code, problem.table, problem.column) == (
        COLUMN_MISSING,
        "dbt_invocations",
        "invocation_id",
    )


def test_the_shipped_file_loads_once():
    assert layouts.layouts() is layouts.layouts()
