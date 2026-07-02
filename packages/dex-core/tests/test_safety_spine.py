"""The safety spine: the five safety-critical assertion families.

A regression on any of these is a release blocker regardless of benchmark score.
The harness is wired in full now: families whose engine already exists are real
tests; families whose engine is not yet built are explicit ``xfail`` placeholders
so the spine is visible and complete in CI from day one and turns green as the
logic arrives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import ColumnProfile, PIIFlag

# --- Family 1: read-only against data; SELECT-only; prod-target refused -------


def test_read_only_duckdb_refuses_writes(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        with pytest.raises(Exception):
            adapter._conn.execute("INSERT INTO customers VALUES (3, 'c@example.com')")
    finally:
        adapter.close()


def test_generated_sql_is_select_only(duckdb_file: Path):
    # The profiling SQL the adapter generates must parse as a single read-only
    # SELECT. Built without executing, so the generator itself is what is asserted.
    from exmergo_dex_core.adapters.base import ColumnMeta
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = DuckDBAdapter(duckdb_file)
    try:
        sql, _plan = adapter._build_aggregate_sql(
            "memory.main.customers",
            [
                ColumnMeta("id", "INTEGER", True, 0),
                ColumnMeta("email", "VARCHAR", True, 1),
            ],
            safe={"id"},
        )
    finally:
        adapter.close()
    assert sql.lstrip().upper().startswith("SELECT")
    # Idempotent: passing it through the guard again must not raise.
    assert assert_select_only(sql) == sql


def test_select_only_guard_rejects_writes():
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "DELETE FROM customers",
        "INSERT INTO customers VALUES (3, 'c@example.com')",
        "DROP TABLE customers",
        "SELECT 1; DROP TABLE customers",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad)


def _firewall_cache():
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache

    return DexCache(
        datasets=[
            Dataset(
                identifier="db.main.customers",
                columns=[
                    ColumnProfile(name="id", data_type="INTEGER"),
                    ColumnProfile(
                        name="email",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="email", confidence=0.9),
                    ),
                ],
            )
        ]
    )


def test_query_firewall_refuses_writes_pragmas_and_multistatement():
    # Agent-authored SQL gets a stricter gate than engine SQL: even the
    # read-only introspection roots (PRAGMA/DESCRIBE) are refused.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "DELETE FROM customers",
        "INSERT INTO customers VALUES (3, 'x')",
        "SELECT 1; DROP TABLE customers",
        "PRAGMA database_list",
        "DESCRIBE customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits())


@pytest.mark.xfail(
    reason="dev-target build / prod-target refusal not yet implemented", strict=False
)
def test_prod_target_execution_is_refused():
    from exmergo_dex_core import transform

    transform.build(target="prod")  # must refuse; raises NotImplementedError today


# --- Family 2: cost-guard binds ----------------------------------------------


@pytest.mark.xfail(
    reason="connector-aware cost guard not yet implemented", strict=False
)
def test_cost_guard_blocks_over_ceiling():
    from exmergo_dex_core.guards import cost_guard

    cost_guard.preflight(estimate=10_000, ceiling=10)  # must block; not built yet


# --- Family 3: PII flagged, never surfaced -----------------------------------


def test_pii_flag_cannot_carry_an_example_value():
    # Structural guarantee: the flag type has no field for a sample value, so PII
    # can be recorded as (column, category, confidence) but never surfaced.
    assert set(PIIFlag.model_fields) == {"category", "confidence"}
    assert "value" not in ColumnProfile.model_fields


def test_pii_flag_lives_on_the_column_profile():
    col = ColumnProfile(
        name="email", data_type="VARCHAR", pii=PIIFlag(category="email", confidence=0.9)
    )
    assert col.pii is not None and col.pii.category.value == "email"


def test_query_firewall_enforces_pii_flagged_never_surfaced():
    # The flag is not just metadata: any expression that would carry a flagged
    # column's values into the projection is refused, including through
    # aggregates that return values (MIN) and through CTE laundering.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT email FROM customers",
        "SELECT MIN(email) FROM customers",
        "SELECT * FROM customers",
        "WITH x AS (SELECT email AS e FROM customers) SELECT e FROM x",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits())
    # Measuring the flagged column is fine: a statistic is not a value.
    inspect_query("SELECT COUNT(DISTINCT email) FROM customers", cache, QueryLimits())


# --- Family 4: propose-don't-impose ------------------------------------------


@pytest.mark.xfail(
    reason="diff-based apply (never silent overwrite) not yet implemented", strict=False
)
def test_changes_are_diffs_not_silent_writes():
    from exmergo_dex_core import transform

    result = transform.apply(plan_id="x")  # must return reviewable diffs, not write
    assert result.diffs


# --- Family 5: credentials and raw rows never enter stdout data ---------------


def test_envelope_blocks_secrets_in_data():
    with pytest.raises(env.SanitizationError):
        env.emit(env.ok({"connection": {"password": "hunter2"}}))


def test_envelope_blocks_raw_rows_in_data():
    with pytest.raises(env.SanitizationError):
        env.emit(env.ok({"rows": [{"id": 1, "email": "a@example.com"}]}))


def test_query_results_are_columnar_and_pass_the_sanitizer(capsys):
    # The query path's list-of-lists shape crosses cleanly; the dict-row rule
    # above still guards every other command against accidental record dumps.
    env.emit(env.ok({"columns": ["id", "n"], "cells": [[1, 3], [2, 5]]}))
    assert capsys.readouterr().out
