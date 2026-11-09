"""Tests for the guarded aggregate multiset comparator (#856)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.transform.multiset_compare import MultisetDiff, compare_selects


@pytest.fixture
def adapter(tmp_path: Path):
    path = tmp_path / "compare.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("create table people (id integer, email varchar)")
    connection.close()
    opened = DuckDBAdapter(path)
    try:
        yield opened
    finally:
        opened.close()


def _counts(left: str, right: str, adapter: DuckDBAdapter) -> MultisetDiff:
    return compare_selects(
        left,
        right,
        adapter=adapter,
        cache=DexCache(),
        limits=QueryLimits(),
        command="transform plan",
    )


# Issue #856 minimal synthetic case: left-join coalesce stays 1; extracting an
# intermediate inner join of event_counts with user_names turns it into 0.
_BASELINE = """
with teams as (select 10 as team_id),
event_counts as (select 10 as team_id, 1 as event_count),
user_names as (select 20 as team_id, 'someone' as user_name)
select t.team_id, coalesce(c.event_count, 0) as event_count
from teams t
left join event_counts c on c.team_id = t.team_id
left join user_names n on n.team_id = t.team_id
"""

_EXTRACTED = """
with teams as (select 10 as team_id),
event_counts as (select 10 as team_id, 1 as event_count),
user_names as (select 20 as team_id, 'someone' as user_name),
joined as (
    select c.team_id, c.event_count
    from event_counts c
    inner join user_names n on n.team_id = c.team_id
)
select t.team_id, coalesce(j.event_count, 0) as event_count
from teams t
left join joined j on j.team_id = t.team_id
"""


def test_join_extraction_is_not_equivalent(adapter):
    diff = _counts(_BASELINE, _EXTRACTED, adapter)
    assert diff == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=1, only_in_right=1
    )
    assert not diff.equivalent


def test_identical_selects_are_equivalent(adapter):
    diff = _counts(_BASELINE, _BASELINE, adapter)
    assert diff.equivalent
    assert diff == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=0, only_in_right=0
    )


def test_duplicate_multiplicity_is_detected(adapter):
    left = "select 1 as n union all select 1 as n"
    right = "select 1 as n"
    diff = _counts(left, right, adapter)
    assert diff == MultisetDiff(
        left_rows=2, right_rows=1, only_in_left=1, only_in_right=0
    )
    assert not diff.equivalent


def test_null_and_empty_string_differ(adapter):
    left = "select cast(NULL as varchar) as label"
    right = "select '' as label"
    diff = _counts(left, right, adapter)
    assert not diff.equivalent
    assert diff.only_in_left == 1
    assert diff.only_in_right == 1


def test_empty_sql_is_refused(adapter):
    with pytest.raises(ValueError, match="required"):
        _counts("   ", "select 1 as n", adapter)
