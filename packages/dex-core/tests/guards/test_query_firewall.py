"""The query firewall's decision matrix: what agent SQL may run, refuse, or be
rewritten. Pure unit tests over a hand-built cache; no database is touched, which
is the point: the policy is static analysis."""

from __future__ import annotations

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.guards.query_firewall import (
    QueryRefusedError,
    inspect_query,
)


@pytest.fixture
def cache() -> DexCache:
    """Airbnb-shaped: RAW_HOSTS.NAME is flagged, RAW_LISTINGS is fully clear,
    and one inventory-only (unprofiled) table exists."""

    return DexCache(
        datasets=[
            Dataset(
                identifier="db.main.RAW_HOSTS",
                columns=[
                    ColumnProfile(name="ID", data_type="INTEGER"),
                    ColumnProfile(
                        name="NAME",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=0.6),
                    ),
                ],
            ),
            Dataset(
                identifier="db.main.RAW_LISTINGS",
                columns=[
                    ColumnProfile(name="ID", data_type="INTEGER"),
                    ColumnProfile(name="HOST_ID", data_type="INTEGER"),
                ],
            ),
            Dataset(identifier="db.main.UNPROFILED"),  # inventory-only
        ]
    )


LIMITS = QueryLimits()


def _refusal(sql: str, cache: DexCache) -> str:
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query(sql, cache, LIMITS)
    return str(excinfo.value)


# --- allowed: measuring aggregates and cleared values ---------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT COUNT(*) FROM RAW_HOSTS",
        "SELECT COUNT(NAME) FROM RAW_HOSTS",
        "SELECT COUNT(DISTINCT NAME) FROM RAW_HOSTS",
        "SELECT APPROX_COUNT_DISTINCT(NAME) FROM RAW_HOSTS",
        "SELECT AVG(LENGTH(NAME)) FROM RAW_HOSTS",
        "SELECT HOST_ID, COUNT(*) AS n FROM RAW_LISTINGS GROUP BY 1 ORDER BY 2 DESC",
        "SELECT * FROM RAW_LISTINGS",
        "SELECT MIN(ID), MAX(ID) FROM RAW_LISTINGS",  # min/max fine on cleared cols
        "SELECT COUNT(*) FROM RAW_HOSTS WHERE NAME = 'Ada'",  # values flow in
        "SELECT COUNT(*) FROM RAW_HOSTS GROUP BY NAME",  # group key not projected
        "SELECT COUNT(*) FILTER (WHERE NAME LIKE 'A%') FROM RAW_HOSTS",
        "WITH x AS (SELECT NAME FROM RAW_HOSTS) SELECT COUNT(NAME) FROM x",
        "SELECT ID FROM (SELECT ID, NAME FROM RAW_HOSTS) t",
        "SELECT l.HOST_ID FROM RAW_LISTINGS l JOIN RAW_HOSTS h ON l.HOST_ID = h.ID",
        "SELECT ID FROM RAW_LISTINGS UNION ALL SELECT ID FROM RAW_HOSTS",
        "SELECT COUNT(*) OVER () FROM RAW_LISTINGS",
        "SELECT main.RAW_LISTINGS.ID FROM main.RAW_LISTINGS",  # qualified table
    ],
)
def test_allowed(sql: str, cache: DexCache):
    inspected = inspect_query(sql, cache, LIMITS)
    assert inspected.sql
    assert inspected.row_cap <= LIMITS.max_rows


# --- refused: value-carrying paths from flagged columns -------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT NAME FROM RAW_HOSTS",
        "SELECT MIN(NAME) FROM RAW_HOSTS",
        "SELECT MAX(NAME) FROM RAW_HOSTS",
        "SELECT ANY_VALUE(NAME) FROM RAW_HOSTS",
        "SELECT STRING_AGG(NAME, ',') FROM RAW_HOSTS",
        "SELECT ARRAY_AGG(NAME) FROM RAW_HOSTS",
        "SELECT SUBSTR(NAME, 1, 1) FROM RAW_HOSTS",
        "SELECT UPPER(NAME) FROM RAW_HOSTS",
        "SELECT NAME, COUNT(*) FROM RAW_HOSTS GROUP BY 1",  # projected group key
        "SELECT NAME LIKE 'A%' FROM RAW_HOSTS",  # per-row predicate is derived PII
        "SELECT CASE WHEN NAME IS NULL THEN 1 ELSE 0 END FROM RAW_HOSTS",
        "SELECT * FROM RAW_HOSTS",  # expansion includes the flagged column
        "SELECT RAW_HOSTS.* FROM RAW_HOSTS",
        "SELECT h.NAME FROM RAW_LISTINGS l JOIN RAW_HOSTS h ON l.HOST_ID = h.ID",
        "WITH x AS (SELECT NAME FROM RAW_HOSTS) SELECT NAME FROM x",  # CTE smuggle
        "WITH x AS (SELECT NAME AS n FROM RAW_HOSTS) SELECT n FROM x",  # aliased
        "SELECT t.NAME FROM (SELECT ID, NAME FROM RAW_HOSTS) t",  # subquery smuggle
        "SELECT (SELECT MIN(NAME) FROM RAW_HOSTS)",  # scalar subquery
        "SELECT ID FROM RAW_LISTINGS UNION ALL SELECT NAME FROM RAW_HOSTS",
        "SELECT some_unknown_udf(NAME) FROM RAW_HOSTS",  # unknown fn: fail closed
    ],
)
def test_refused_pii_carrying(sql: str, cache: DexCache):
    message = _refusal(sql, cache)
    assert "NAME" in message or "PII" in message


def test_refusal_names_column_category_and_fix(cache: DexCache):
    message = _refusal("SELECT MIN(NAME) FROM RAW_HOSTS", cache)
    assert "RAW_HOSTS.NAME" in message
    assert "(name)" in message  # the flag category
    assert "COUNT" in message  # the fix


@pytest.fixture
def twin_cache() -> DexCache:
    """A flagged entity-name column with an unflagged equivalent one table over,
    the shape that made the firewall a dead end in the field."""

    return DexCache(
        datasets=[
            Dataset(
                identifier="db.main.PRODUCTS",
                columns=[
                    ColumnProfile(name="ID", data_type="INTEGER"),
                    ColumnProfile(
                        name="NAME",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=0.6),
                    ),
                ],
            ),
            Dataset(
                identifier="db.main.INVENTORY_ITEMS",
                columns=[
                    ColumnProfile(name="ID", data_type="INTEGER"),
                    ColumnProfile(name="PRODUCT_NAME", data_type="VARCHAR"),
                ],
            ),
        ]
    )


def test_pii_refusal_points_to_an_unflagged_equivalent_column(twin_cache: DexCache):
    message = _refusal("SELECT NAME FROM PRODUCTS", twin_cache)
    assert "PRODUCTS.NAME" in message  # still refused, flag intact
    assert "INVENTORY_ITEMS.PRODUCT_NAME" in message  # the lawful alternative
    assert "unflagged column may carry" in message


def test_pii_refusal_without_a_twin_still_refuses_cleanly(cache: DexCache):
    message = _refusal("SELECT NAME FROM RAW_HOSTS", cache)
    assert "RAW_HOSTS.NAME" in message
    assert "COUNT" in message  # the fix is still named
    assert "unflagged column may carry" not in message  # no false suggestion


# --- refused: shape, resolution, and introspection -------------------------------


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("SELECT nope FROM RAW_HOSTS", "nope"),
        ("SELECT ID FROM missing_table", "not in the .dex cache"),
        ("SELECT ID FROM UNPROFILED", "not profiled"),
        ("SELECT ID FROM RAW_LISTINGS l, RAW_HOSTS h", "ambiguous"),
        ("PRAGMA database_list", "Pragma"),
        ("DESCRIBE RAW_HOSTS", "Describe"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("INSERT INTO RAW_HOSTS VALUES (1, 'x')", "SELECT"),
        ("DELETE FROM RAW_HOSTS", "SELECT"),
        ("DROP TABLE RAW_HOSTS", "SELECT"),
        (
            "WITH x AS (SELECT 1) INSERT INTO RAW_HOSTS SELECT * FROM x",
            "SELECT",
        ),
    ],
)
def test_refused_shape_and_resolution(sql: str, fragment: str, cache: DexCache):
    assert fragment.lower() in _refusal(sql, cache).lower()


# --- LIMIT rewriting -------------------------------------------------------------


def test_limit_injected_when_absent(cache: DexCache):
    inspected = inspect_query("SELECT ID FROM RAW_LISTINGS", cache, LIMITS)
    assert f"LIMIT {LIMITS.max_rows + 1}" in inspected.sql
    assert inspected.row_cap == LIMITS.max_rows
    assert inspected.capped_by_engine is True


def test_limit_clamped_when_above_cap(cache: DexCache):
    inspected = inspect_query("SELECT ID FROM RAW_LISTINGS LIMIT 5000", cache, LIMITS)
    assert f"LIMIT {LIMITS.max_rows + 1}" in inspected.sql
    assert inspected.capped_by_engine is True


def test_agent_limit_at_or_under_cap_is_respected(cache: DexCache):
    inspected = inspect_query("SELECT ID FROM RAW_LISTINGS LIMIT 5", cache, LIMITS)
    assert "LIMIT 5" in inspected.sql
    assert inspected.row_cap == 5
    assert inspected.capped_by_engine is False


def test_tables_are_reported_for_the_query_log(cache: DexCache):
    inspected = inspect_query(
        "SELECT l.HOST_ID FROM RAW_LISTINGS l JOIN RAW_HOSTS h ON l.HOST_ID = h.ID",
        cache,
        LIMITS,
    )
    assert inspected.tables == ["db.main.RAW_HOSTS", "db.main.RAW_LISTINGS"]


# --- the confidence threshold: sub-threshold flags warn, never block -------------


def _cache_with_confidence(confidence: float) -> DexCache:
    return DexCache(
        datasets=[
            Dataset(
                identifier="db.main.REGION",
                columns=[
                    ColumnProfile(name="R_REGIONKEY", data_type="INTEGER"),
                    ColumnProfile(
                        name="R_NAME",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=confidence),
                    ),
                ],
            ),
        ]
    )


def test_sub_threshold_flag_projects_with_a_warning():
    inspected = inspect_query(
        "SELECT R_NAME FROM REGION", _cache_with_confidence(0.3), LIMITS
    )
    assert inspected.sql
    (warning,) = inspected.warnings
    assert "REGION.R_NAME" in warning
    assert "(name)" in warning
    assert "0.3" in warning and "0.5" in warning
    assert "pii_overrides" in warning


def test_threshold_boundary_is_inclusive():
    # 0.5 exactly blocks; just below allows with a warning. The boundary is >=.
    with pytest.raises(QueryRefusedError):
        inspect_query("SELECT R_NAME FROM REGION", _cache_with_confidence(0.5), LIMITS)
    inspected = inspect_query(
        "SELECT R_NAME FROM REGION", _cache_with_confidence(0.49), LIMITS
    )
    assert inspected.warnings


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM REGION",
        "WITH x AS (SELECT R_NAME FROM REGION) SELECT R_NAME FROM x",
        "SELECT t.R_NAME FROM (SELECT R_NAME FROM REGION) t",
    ],
)
def test_sub_threshold_warnings_survive_star_cte_and_subquery(sql: str):
    inspected = inspect_query(sql, _cache_with_confidence(0.3), LIMITS)
    assert any("REGION.R_NAME" in w for w in inspected.warnings)


def test_measuring_aggregate_over_sub_threshold_flag_carries_no_warning():
    # The statistic path is as clean as ever: no value crosses, nothing to flag.
    inspected = inspect_query(
        "SELECT COUNT(DISTINCT R_NAME) FROM REGION", _cache_with_confidence(0.3), LIMITS
    )
    assert inspected.warnings == []


def test_refusal_points_at_the_override_path(cache: DexCache):
    message = _refusal("SELECT NAME FROM RAW_HOSTS", cache)
    assert "pii_overrides" in message
    assert ".dex/config.yml" in message


def test_stale_cache_refusal_hints_at_reprofiling():
    from exmergo_dex_core.cache import CACHE_SCHEMA_VERSION

    stale = _cache_with_confidence(0.6)
    stale.schema_version = CACHE_SCHEMA_VERSION - 1
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query("SELECT R_NAME FROM REGION", stale, LIMITS)
    assert "re-profile" in str(excinfo.value)

    current = _cache_with_confidence(0.6)
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query("SELECT R_NAME FROM REGION", current, LIMITS)
    assert "re-profile" not in str(excinfo.value)


def test_sub_threshold_column_qualifies_as_a_recovery_twin():
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="db.main.PRODUCTS",
                columns=[
                    ColumnProfile(
                        name="NAME",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=0.6),
                    ),
                ],
            ),
            Dataset(
                identifier="db.main.INVENTORY_ITEMS",
                columns=[
                    ColumnProfile(
                        name="PRODUCT_NAME",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=0.3),
                    ),
                ],
            ),
        ]
    )
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query("SELECT NAME FROM PRODUCTS", cache, LIMITS)
    assert "INVENTORY_ITEMS.PRODUCT_NAME" in str(excinfo.value)
