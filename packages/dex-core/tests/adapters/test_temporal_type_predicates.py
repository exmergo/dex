"""Every temporal spelling the connectors actually report, held against the
shared type predicates in one table.

``is_date_only_type`` is a substring rule, and a substring rule is what
produced the defect it now guards (#188): ``DATETIME`` contains ``DATE`` and
not ``TIMESTAMP``, so BigQuery ``DATETIME`` and ClickHouse ``DateTime`` were
read as bare calendar dates and their hour-grain continuity was never
computed. Nothing about that was loud -- the column kept reporting day and
month gaps and simply never reported an hour one, which reads as a clean
result rather than a skipped one.

So the rule is pinned against spellings, not against intent, and each row
names the metadata source it comes from: that is what has to be re-read when a
connector changes how it reports a type, and a connector added later owes this
file its own rows. The live proof that the hour grain is really computed lives
per connector (tests/integration/test_bigquery_explore.py and
test_clickhouse_explore.py both profile a column with a known hole); what is
here is the offline classification every one of them depends on.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.adapters.base import (
    TEMPORAL_UNITS_DATE_ONLY,
    TEMPORAL_UNITS_WITH_TIME,
    is_date_only_type,
    is_temporal_type,
    temporal_units_for,
)

# (metadata source, spelling, temporal, date-only)
#
# "temporal" is eligibility for span/gap analysis at all (explore.profile picks
# its temporal columns with `is_temporal_type`); "date-only" is whether the
# hour grain is skipped for it. A non-temporal spelling never reaches the
# date-only question, and is listed with False/False.
SPELLINGS = [
    # BigQuery: SchemaField.field_type, which reports the legacy type names.
    # DATETIME is the one that carried the bug.
    ("bigquery", "DATE", True, True),
    ("bigquery", "DATETIME", True, False),
    ("bigquery", "TIMESTAMP", True, False),
    ("bigquery", "TIME", False, False),
    # Snowflake: the `type` field of SHOW COLUMNS' data_type JSON. DATETIME is
    # an alias Snowflake resolves to TIMESTAMP_NTZ, so that spelling never
    # arrives from this source -- and is classified correctly if it ever does.
    ("snowflake", "DATE", True, True),
    ("snowflake", "TIMESTAMP_NTZ", True, False),
    ("snowflake", "TIMESTAMP_LTZ", True, False),
    ("snowflake", "TIMESTAMP_TZ", True, False),
    ("snowflake", "TIME", False, False),
    # Databricks: ColumnInfo.type_text (SQL text, lower case) falling back to
    # type_name (the SDK enum, upper case), so both cases reach the predicate.
    ("databricks", "date", True, True),
    ("databricks", "DATE", True, True),
    ("databricks", "timestamp", True, False),
    ("databricks", "TIMESTAMP", True, False),
    ("databricks", "timestamp_ntz", True, False),
    ("databricks", "TIMESTAMP_NTZ", True, False),
    ("databricks", "interval day to second", False, False),
    # Postgres: pg_catalog.format_type, which spells out the qualifiers and
    # carries the precision inside the name.
    ("postgres", "date", True, True),
    ("postgres", "timestamp without time zone", True, False),
    ("postgres", "timestamp with time zone", True, False),
    ("postgres", "timestamp(3) without time zone", True, False),
    ("postgres", "time without time zone", False, False),
    ("postgres", "time with time zone", False, False),
    ("postgres", "interval", False, False),
    # Redshift: SVV_COLUMNS.data_type, which follows the same Postgres
    # spellings (timestamptz is reported in its expanded form).
    ("redshift", "date", True, True),
    ("redshift", "timestamp without time zone", True, False),
    ("redshift", "timestamp with time zone", True, False),
    ("redshift", "time without time zone", False, False),
    # DuckDB: duckdb_columns().data_type, including the sub-second variants.
    ("duckdb", "DATE", True, True),
    ("duckdb", "TIMESTAMP", True, False),
    ("duckdb", "TIMESTAMP WITH TIME ZONE", True, False),
    ("duckdb", "TIMESTAMP_NS", True, False),
    ("duckdb", "TIMESTAMP_MS", True, False),
    ("duckdb", "TIMESTAMP_S", True, False),
    ("duckdb", "TIME", False, False),
    ("duckdb", "INTERVAL", False, False),
    # ClickHouse: system.columns.type, mixed case and wrapped. The adapter
    # unwraps before asking, but the wrapped spelling reaches `is_temporal_type`
    # through explore.profile's eligibility pass, so both forms are pinned.
    ("clickhouse", "Date", True, True),
    ("clickhouse", "Date32", True, True),
    ("clickhouse", "DateTime", True, False),
    ("clickhouse", "DateTime64(3)", True, False),
    ("clickhouse", "Nullable(Date)", True, True),
    ("clickhouse", "Nullable(DateTime)", True, False),
    ("clickhouse", "LowCardinality(Nullable(DateTime64(3)))", True, False),
]


@pytest.mark.parametrize(
    ("source", "spelling", "temporal", "date_only"),
    [pytest.param(*row, id=f"{row[0]}-{row[1]}") for row in SPELLINGS],
)
def test_every_reported_spelling_is_classified(
    source: str, spelling: str, temporal: bool, date_only: bool
):
    assert is_temporal_type(spelling) is temporal, source
    if not temporal:
        return
    assert is_date_only_type(spelling) is date_only, source
    expected = TEMPORAL_UNITS_DATE_ONLY if date_only else TEMPORAL_UNITS_WITH_TIME
    assert temporal_units_for(spelling) == expected


def test_no_timestamp_spelling_is_ever_read_as_a_bare_date():
    """The rule, stated once over the whole table: a spelling that carries a
    time component must never be date-only, whatever the dialect calls it.

    A connector added later that reports, say, ``SMALLDATETIME`` gets the
    correct answer from the ``DATETIME`` exclusion; one that invents a spelling
    with a time component and neither substring in it does not, and is what
    this assertion exists to make someone think about.
    """

    for _source, spelling, temporal, _date_only in SPELLINGS:
        upper = spelling.upper()
        if temporal and ("TIMESTAMP" in upper or "DATETIME" in upper):
            assert not is_date_only_type(spelling), spelling
            assert "hour" in temporal_units_for(spelling)


def test_the_hour_grain_is_skipped_only_for_a_bare_date():
    """The other direction, which is not a silent failure but an error: BigQuery
    ``DATE_TRUNC`` is the one truncation function with no HOUR unit, so over-
    correcting the predicate would make a DATE column's profile fail outright.
    """

    for _source, spelling, temporal, date_only in SPELLINGS:
        if temporal and date_only:
            assert temporal_units_for(spelling) == TEMPORAL_UNITS_DATE_ONLY
            assert "hour" not in temporal_units_for(spelling)
