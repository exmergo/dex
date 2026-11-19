"""The query firewall holds the columns of Elementary's schema that carry source
rows (#1310). Pure unit tests over a hand-built cache, as the firewall's own
matrix is: each acceptance criterion is a pair, a case built to break the
guarantee that must be refused and a case built to hold it that must run."""

from __future__ import annotations

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.guards import elementary_values
from exmergo_dex_core.guards.query_firewall import QueryRefusedError, inspect_query

LIMITS = QueryLimits()
SCHEMA = "dev.main_elementary"


def _col(name: str, pii: PIIFlag | None = None, data_type: str = "VARCHAR"):
    return ColumnProfile(name=name, data_type=data_type, pii=pii)


@pytest.fixture
def cache() -> DexCache:
    email = PIIFlag(category="email", confidence=0.95)
    return DexCache(
        datasets=[
            Dataset(
                identifier=f"{SCHEMA}.test_result_rows",
                columns=[
                    _col("elementary_test_results_id"),
                    _col("result_row"),
                    _col("detected_at", data_type="TIMESTAMP"),
                ],
            ),
            Dataset(
                identifier=f"{SCHEMA}.elementary_test_results",
                columns=[
                    _col("id"),
                    _col("status"),
                    _col("failures", data_type="BIGINT"),
                    _col("result_rows"),
                ],
            ),
            Dataset(
                identifier=f"{SCHEMA}.alerts_dbt_tests",
                columns=[_col("alert_id"), _col("result_rows")],
            ),
            Dataset(
                identifier=f"{SCHEMA}.data_monitoring_metrics",
                columns=[
                    _col("metric_name"),
                    _col("metric_value", data_type="DOUBLE"),
                    _col("dimension"),
                    _col("dimension_value"),
                ],
            ),
            # Undocumented: what tests_use_temp_tables leaves behind.
            Dataset(
                identifier=f"{SCHEMA}.test_3f2a_20261118__tmp_123",
                columns=[_col("id", data_type="INTEGER"), _col("email")],
            ),
            Dataset(
                identifier="dev.main.customers",
                columns=[_col("id", data_type="INTEGER"), _col("email", email)],
            ),
            Dataset(
                identifier="dev.main.orders",
                columns=[_col("id", data_type="INTEGER"), _col("country")],
            ),
        ]
    )


def _refused(sql: str, cache: DexCache, **kwargs) -> str:
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query(sql, cache, LIMITS, **kwargs)
    return str(excinfo.value)


# --- result_row and result_rows ---------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT result_row FROM test_result_rows",
        "SELECT * FROM test_result_rows",
        "SELECT MAX(result_row) FROM test_result_rows",
        "SELECT UPPER(result_rows) FROM elementary_test_results",
        "SELECT result_rows FROM alerts_dbt_tests",
        "SELECT x FROM (SELECT result_row AS x FROM test_result_rows) t",
    ],
)
def test_a_projection_of_failing_rows_is_refused(sql: str, cache: DexCache):
    message = _refused(sql, cache)

    assert "failing rows of a test" in message
    assert "pii_overrides entry does not clear" in message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM test_result_rows",
        "SELECT COUNT(result_row) FROM test_result_rows",
        "SELECT AVG(LENGTH(result_rows)) FROM elementary_test_results",
        "SELECT status, COUNT(*) FROM elementary_test_results GROUP BY 1",
        "SELECT SUM(failures) FROM elementary_test_results",
    ],
)
def test_aggregates_over_the_same_tables_run(sql: str, cache: DexCache):
    assert inspect_query(sql, cache, LIMITS).sql


def test_the_hold_does_not_rest_on_the_cache_flag(cache: DexCache):
    """No flag in the cache, and a cleared one, both leave the column held: the
    hold is for where the values come from."""

    rows = cache.datasets[0].columns[1]
    assert rows.pii is None
    _refused("SELECT result_row FROM test_result_rows", cache)
    rows.pii_overridden = "free_text"
    _refused("SELECT result_row FROM test_result_rows", cache)


def test_row_value_columns_are_held_by_model_name_in_any_schema():
    """A project that keeps Elementary's models in its own schema still has the
    columns held, since they are recognised by the model's name."""

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="dev.analytics.test_result_rows",
                columns=[_col("result_row")],
            )
        ]
    )
    _refused("SELECT result_row FROM test_result_rows", cache)


# --- dimension_value --------------------------------------------------------------

DIM = "SELECT dimension_value FROM data_monitoring_metrics"


def test_a_dimension_value_from_a_flagged_column_is_refused(cache: DexCache):
    message = _refused(f"{DIM} WHERE dimension = 'email'", cache)

    assert "customers.email (email)" in message


def test_a_dimension_value_from_an_unflagged_column_runs(cache: DexCache):
    inspected = inspect_query(
        "SELECT dimension, dimension_value, AVG(metric_value) "
        "FROM data_monitoring_metrics WHERE dimension = 'country' "
        "AND metric_name = 'row_count' GROUP BY 1, 2",
        cache,
        LIMITS,
    )

    assert inspected.warnings == []


@pytest.mark.parametrize(
    "where",
    [
        "dimension IN ('country', 'email')",
        "dimension = 'country; email'",  # a row of two dimensions
        "'email' = dimension",
        "m.dimension = 'email'",
    ],
)
def test_any_flagged_dimension_in_the_bound_refuses(cache: DexCache, where: str):
    _refused(
        f"SELECT m.dimension_value FROM data_monitoring_metrics m WHERE {where}",  # noqa: S608
        cache,
    )


@pytest.mark.parametrize(
    "sql",
    [
        DIM,  # rows of every dimension, including ones a removed test wrote
        f"{DIM} WHERE LOWER(dimension) = 'country'",
        f"{DIM} WHERE dimension = 'country' OR metric_value > 0",
        f"{DIM} WHERE dimension LIKE 'coun%'",
        f"{DIM} WHERE dimension = 'region'",  # no profiled column of that name
        "WITH m AS (SELECT * FROM data_monitoring_metrics) "
        "SELECT dimension_value FROM m WHERE dimension = 'country'",
        "SELECT d.dimension_value FROM data_monitoring_metrics d "
        "JOIN orders o ON o.id = 1 WHERE dimension = 'country'",
    ],
)
def test_an_unbounded_dimension_value_is_held(cache: DexCache, sql: str):
    """The bound must sit in the SELECT that reads the table, as plain
    equality or IN over string literals, so it decides which rows exist; a
    bare ``dimension`` beside a second source is not read as the table's."""

    message = _refused(sql, cache)

    assert "bounds its rows to named dimensions" in message


def test_a_count_of_dimension_values_runs_unbounded(cache: DexCache):
    inspect_query(
        "SELECT COUNT(DISTINCT dimension_value) FROM data_monitoring_metrics",
        cache,
        LIMITS,
    )


def test_two_bounds_in_one_where_intersect(cache: DexCache):
    inspect_query(
        f"{DIM} WHERE dimension IN ('country', 'email') AND dimension = 'country'",
        cache,
        LIMITS,
    )


# --- relations the package does not document ----------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM test_3f2a_20261118__tmp_123",
        "SELECT COUNT(*) FROM test_3f2a_20261118__tmp_123",
        "SELECT COUNT(*) FROM orders WHERE id IN "
        "(SELECT id FROM test_3f2a_20261118__tmp_123)",
    ],
)
def test_a_query_over_an_undocumented_relation_in_the_schema_is_refused(
    sql: str, cache: DexCache
):
    assert "none of the package's models" in _refused(sql, cache)


def test_a_documented_model_s_aggregate_columns_run(cache: DexCache):
    inspect_query(
        "SELECT metric_name, AVG(metric_value) FROM data_monitoring_metrics GROUP BY 1",
        cache,
        LIMITS,
    )


@pytest.mark.parametrize(
    ("identifier", "held"),
    [
        ("dev.elementary.tmp_rows", True),
        ("proj.dbt_ci_elementary.tmp_rows", True),
        ('"dev"."MAIN_ELEMENTARY"."TMP_ROWS"', True),
        ("dev.main_elementary.dbt_invocations", False),
        ("dev.main.tmp_rows", False),
        ("dev.elementary_raw.tmp_rows", False),
        ("tmp_rows", False),
    ],
)
def test_the_schema_is_recognised_by_its_name(identifier: str, held: bool):
    assert elementary_values.protected_relation(identifier) is held


def test_a_profile_withholds_every_value_bearing_column():
    names = ["id", "result_row", "dimension_value", "status"]

    assert elementary_values.withheld_columns(f"{SCHEMA}.test_result_rows", names) == {
        "result_row",
        "dimension_value",
    }
    assert elementary_values.withheld_columns(f"{SCHEMA}.tmp_x", names) == set(names)
    assert elementary_values.withheld_columns("dev.main.orders", names) == set()


# --- review: reads the value paths skip, and names that repeat ---------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM((SELECT COUNT(*) FROM test_3f2a_20261118__tmp_123))",
        "SELECT COUNT(*) FROM orders WHERE EXISTS "
        "(SELECT 1 FROM test_3f2a_20261118__tmp_123)",
    ],
)
def test_a_held_relation_inside_a_measuring_aggregate_is_refused(
    sql: str, cache: DexCache
):
    assert "none of the package's models" in _refused(sql, cache)


def test_a_repeated_output_name_keeps_every_taint(cache: DexCache):
    _refused(
        "SELECT result_row AS x, elementary_test_results_id AS x FROM test_result_rows",
        cache,
    )
    inspect_query("SELECT id AS x, id AS x FROM orders", cache, LIMITS)


def test_a_dotted_relation_name_stays_in_the_schema():
    """DuckDB joins the parts unquoted, so a table named ``tmp.rows`` reads as
    four parts; the schema is still the package's."""

    assert elementary_values.protected_relation("dev.main_elementary.tmp.rows")
    assert not elementary_values.protected_relation("dev.main.tmp.rows")


# --- review, second pass -----------------------------------------------------------


def test_a_held_relation_the_cache_never_saw_is_refused_inside_an_aggregate(
    cache: DexCache,
):
    message = _refused(
        "SELECT SUM((SELECT id FROM dev.main_elementary.new_test_result LIMIT 1))",
        cache,
    )

    assert "none of the package's models" in message


def test_a_subquery_inside_a_measuring_aggregate_keeps_its_taint(cache: DexCache):
    """One value of a flagged column, summed, is that value."""

    _refused("SELECT SUM((SELECT email FROM customers LIMIT 1))", cache)
    _refused("SELECT MAX(LENGTH((SELECT result_row FROM test_result_rows)))", cache)
    inspect_query("SELECT SUM((SELECT COUNT(*) FROM customers))", cache, LIMITS)
    inspect_query("SELECT COUNT(DISTINCT email) FROM customers", cache, LIMITS)


def test_a_set_operation_over_repeated_names_pairs_nothing_by_name(cache: DexCache):
    """Both sides collapse to three names over four columns, so pairing by those
    names would hand the held third column to `c` and leave `b` clean."""

    _refused(
        "SELECT b FROM ("
        "SELECT 'ok' AS a, 'ok' AS a, 'ok' AS b, 'ok' AS c "
        "UNION ALL "
        "SELECT 'ok' AS x, 'ok' AS y, result_row AS z, 'ok' AS z "
        "FROM test_result_rows) AS q",
        cache,
    )
    inspect_query(
        "SELECT b FROM (SELECT 1 AS a, 2 AS b UNION ALL SELECT id, id FROM orders) q",
        cache,
        LIMITS,
    )


def test_a_cached_profile_never_serves_a_held_value():
    """A profile written before the profiler withheld them, or carried through
    a merge, is cleaned wherever a dataset is built or loaded."""

    from exmergo_dex_core.cache import ValueCount, ValueDomain

    legacy = {
        "identifier": f"{SCHEMA}.test_result_rows",
        "columns": [
            {
                "name": "result_row",
                "data_type": "VARCHAR",
                "min_value": "{'email': 'a@x.io'}",
                "max_value": "{'email': 'z@x.io'}",
                "value_domain": ValueDomain(
                    values=[ValueCount(value="{'email': 'a@x.io'}", count=1)]
                ).model_dump(),
            },
            {"name": "status", "data_type": "VARCHAR", "min_value": "fail"},
        ],
    }
    loaded = DexCache.model_validate({"schema_version": 4, "datasets": [legacy]})
    [dataset] = loaded.datasets
    rows, status = dataset.columns

    assert (rows.min_value, rows.max_value, rows.value_domain) == (None, None, None)
    assert status.min_value == "fail"
    # Carried into a newly built cache, as a merge does.
    rebuilt = DexCache(datasets=[Dataset.model_validate(legacy)])
    assert rebuilt.datasets[0].columns[0].min_value is None
