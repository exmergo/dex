"""Which members a Cube JSON filter names, fail-closed (#913)."""

from __future__ import annotations

import pytest

from exmergo_dex_core.cube_dialect import (
    OPERATORS,
    CubeFilterError,
    filter_refs,
    parse_where,
)


def refs(*clauses: str):
    return filter_refs(parse_where(list(clauses)))


def test_a_leaf_names_its_member():
    assert refs(
        '{"member": "orders.status", "operator": "equals", "values": ["paid"]}'
    ) == ["orders.status"]


def test_groups_are_walked_and_members_deduplicated_in_order():
    clause = (
        '{"and": [{"member": "orders.status", "operator": "set"},'
        ' {"or": [{"member": "customers.city", "operator": "equals", "values": ["x"]},'
        ' {"member": "orders.status", "operator": "notSet"}]}]}'
    )
    assert refs(clause) == ["orders.status", "customers.city"]


def test_a_list_and_several_clauses_are_one_conjunction():
    assert refs(
        '[{"member": "a.x", "operator": "set"}, {"member": "b.y", "operator": "set"}]',
        '{"member": "c.z", "operator": "gt", "values": [1]}',
    ) == ["a.x", "b.y", "c.z"]


def test_the_older_dimension_key_is_read_as_member():
    assert refs('{"dimension": "orders.status", "operator": "set"}') == [
        "orders.status"
    ]


def test_a_measure_filter_names_the_measure():
    assert refs('{"member": "orders.count", "operator": "gt", "values": ["2"]}') == [
        "orders.count"
    ]


@pytest.mark.parametrize(
    "clause",
    [
        '{"member": "orders.status", "operator": "between", "values": ["a"]}',
        '{"member": "orders.status", "operator": "equals", "values": "paid"}',
        '{"member": "orders.status", "operator": "equals", "values": [null]}',
        '{"member": "orders.status", "operator": "equals", "values": [["x"]]}',
        '{"member": "orders.status", "operator": "equals", "sql": "1 = 1"}',
        '{"member": "orders.status", "dimension": "orders.status", "operator": "set"}',
        '{"member": "status", "operator": "set"}',
        '{"member": "orders.status.month", "operator": "set"}',
        '{"member": 7, "operator": "set"}',
        '{"operator": "set"}',
        '{"and": []}',
        '{"or": {"member": "orders.status", "operator": "set"}}',
        '{"and": [{"member": "a.x", "operator": "set"}], "member": "b.y"}',
        "{}",
    ],
)
def test_anything_unread_is_none(clause):
    assert refs(clause) is None


@pytest.mark.parametrize("clause", ["status = 'paid'", "[]", "[1]", '"orders.status"'])
def test_a_clause_that_is_not_a_filter_is_refused_by_name(clause):
    with pytest.raises(CubeFilterError, match="Cube filter"):
        parse_where([clause])


def test_the_operator_set_is_the_pinned_release_s():
    assert {"equals", "inDateRange", "measureFilter", "notSet"} <= OPERATORS
    assert len(OPERATORS) == 21
