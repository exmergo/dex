"""The cases every connector's refactor comparison has to get right (#899).

Literal statements, written in DuckDB's dialect and transpiled for each
connector, so running one reads no table and costs next to nothing. The DuckDB
run lives in ``test_refactor_conformance.py``; the warehouses run the same list
in ``tests/integration/test_refactor_compare_live.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot

from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.transform.multiset_compare import compare_refactors

_ONE = "select 10 as team_id, cast(1 as bigint) as event_count"


@dataclass(frozen=True)
class Case:
    name: str
    baseline: str
    proposed: str
    status: str
    reason: str
    counts: tuple[int, int, int, int] | None = None
    keyed: tuple[int, int, int] | None = None
    keyed_reason: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


CASES = [
    Case(
        "join_extraction_loses_a_count",
        _ONE,
        "select 10 as team_id, cast(0 as bigint) as event_count",
        "different",
        "rows_differ",
        (1, 1, 1, 1),
    ),
    Case(
        "duplicate_removal",
        "select 1 as id from (select 1 as x union all select 2 as x) s",
        "select distinct 1 as id from (select 1 as x union all select 2 as x) s",
        "different",
        "rows_differ",
        (2, 1, 1, 0),
    ),
    Case(
        "changed_measure_keeps_row_count",
        "select 1 as id, 10 as amount",
        "select 1 as id, 11 as amount",
        "different",
        "rows_differ",
        (1, 1, 1, 1),
    ),
    Case(
        "reordering_is_equivalent",
        "select 1 as id, 10 as amount union all select 2 as id, 20 as amount",
        "select 20 as amount, 2 as id union all select 10 as amount, 1 as id",
        "equivalent",
        "equivalent_on_compared_inputs",
        (2, 2, 0, 0),
    ),
    Case(
        "null_is_not_empty_string",
        "select 1 as id, cast(null as varchar) as name",
        "select 1 as id, cast('' as varchar) as name",
        "different",
        "rows_differ",
        (1, 1, 1, 1),
    ),
    Case(
        "null_is_not_zero",
        "select 1 as id, cast(null as bigint) as n",
        "select 1 as id, cast(0 as bigint) as n",
        "different",
        "rows_differ",
        (1, 1, 1, 1),
    ),
    Case(
        "null_matches_null",
        "select 1 as id, cast(null as varchar) as name",
        "select 1 as id, cast(null as varchar) as name",
        "equivalent",
        "equivalent_on_compared_inputs",
        (1, 1, 0, 0),
    ),
    Case(
        "rename_with_a_mapping",
        "select 1 as team_id, 10 as events",
        "select 1 as team, 10 as event_total",
        "equivalent",
        "equivalent_on_compared_inputs",
        (1, 1, 0, 0),
        kwargs={"column_mapping": {"team_id": "team", "events": "event_total"}},
    ),
    Case(
        "rename_without_a_mapping",
        "select 1 as team_id",
        "select 1 as team",
        "different",
        "schemas_differ",
    ),
    Case(
        "type_change",
        "select 1 as id, cast(1 as bigint) as n",
        "select 1 as id, cast('1' as varchar) as n",
        "different",
        "schemas_differ",
    ),
    Case(
        "equivalent_refactor",
        "select id, sum(n) as n from (select 1 as id, 2 as n union all "
        "select 1 as id, 3 as n) s group by id",
        "select id, sum(n) as n from (select 1 as id, 3 as n union all "
        "select 1 as id, 2 as n) t group by id",
        "equivalent",
        "equivalent_on_compared_inputs",
        (1, 1, 0, 0),
    ),
    Case(
        "null_keys_match",
        "select cast(null as bigint) as id, 1 as n",
        "select cast(null as bigint) as id, 2 as n",
        "different",
        "rows_differ",
        (1, 1, 1, 1),
        keyed=(0, 0, 1),
        kwargs={"key_columns": ["id"]},
    ),
    Case(
        "non_unique_key_keeps_the_multiset_verdict",
        "select 1 as id, 1 as n union all select 1 as id, 2 as n",
        "select 1 as id, 1 as n union all select 1 as id, 3 as n",
        "different",
        "rows_differ",
        (2, 2, 1, 1),
        keyed_reason="key_not_unique",
        kwargs={"key_columns": ["id"]},
    ),
]


def run_case(case: Case, adapter: Any, cache: DexCache | None = None):
    def spelled(sql: str) -> str:
        return sqlglot.transpile(sql, read="duckdb", write=adapter.dialect)[0]

    return compare_refactors(
        spelled(case.baseline),
        spelled(case.proposed),
        adapter=adapter,
        cache=cache or DexCache(),
        limits=QueryLimits(),
        command="transform compare",
        **case.kwargs,
    )


def assert_case(case: Case, result: Any) -> None:
    assert result.status == case.status, (case.name, result.reason_code, result.detail)
    assert result.reason_code == case.reason, (case.name, result.detail)
    if case.counts is not None:
        multiset = result.multiset
        assert (
            multiset.left_rows,
            multiset.right_rows,
            multiset.only_in_left,
            multiset.only_in_right,
        ) == case.counts, case.name
    if case.keyed is not None:
        keyed = result.keyed
        assert (keyed.only_in_left, keyed.only_in_right, keyed.changed) == case.keyed
    assert result.keyed_reason == case.keyed_reason, case.name
