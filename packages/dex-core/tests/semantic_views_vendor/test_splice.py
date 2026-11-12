"""Byte-keeping edits inside one logical table (#1292).

Each helper changes exactly the lines it names and keeps every other byte,
comments inside the table included, and refuses a layout it cannot bound rather
than rewrite lines it did not author.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.semantic_views.loader import read_yaml
from exmergo_dex_core.semantic_views.splice import (
    UnwritableError,
    append_member,
    replace_member,
    set_member_scalar,
    set_table_scalar,
    table_names,
)

TEXT = (
    "name: shop  # the view\n"
    "tables:\n"
    "  - name: Orders\n"
    "    base_table: {database: d, schema: s, table: orders}\n"
    "    # the order key\n"
    "    primary_key: {columns: [order_id]}\n"
    "    dimensions:\n"
    "      - name: status   # lifecycle\n"
    "        expr: status\n"
    "\n"
    "      - name: region\n"
    "        expr: region\n"
    "  - name: customers\n"
    "    base_table: {database: d, schema: s, table: customers}\n"
    "    description: Customers.\n"
    "    dimensions:\n"
    "      - name: segment\n"
    "        expr: segment\n"
)


def _lines_changed(before: str, after: str) -> tuple[list[str], list[str]]:
    old, new = before.splitlines(), after.splitlines()
    return [x for x in new if x not in old], [x for x in old if x not in new]


def test_table_names_lists_block_tables():
    assert table_names(TEXT) == ["Orders", "customers"]


def test_a_new_description_lands_after_the_base_table():
    after = set_table_scalar(TEXT, "orders", "description", "Placed orders.")
    added, removed = _lines_changed(TEXT, after)
    assert added == ["    description: Placed orders."]
    assert removed == []
    assert after.index("description: Placed") < after.index("# the order key")
    assert read_yaml(after)["tables"][0]["description"] == "Placed orders."


def test_an_existing_description_is_replaced_alone():
    after = set_table_scalar(TEXT, "customers", "description", "Buyers.")
    added, removed = _lines_changed(TEXT, after)
    assert (added, removed) == (
        ["    description: Buyers."],
        ["    description: Customers."],
    )


def test_a_member_is_replaced_and_its_neighbours_kept():
    after = replace_member(
        TEXT,
        "ORDERS",
        "dimensions",
        "region",
        {"name": "region", "expr": "region", "description": "Sales region."},
    )
    added, removed = _lines_changed(TEXT, after)
    assert added == ["        description: Sales region."]
    assert removed == []
    assert "      - name: status   # lifecycle\n" in after


def test_a_member_key_is_set_and_its_other_lines_kept_as_written():
    text = TEXT.replace(
        "        expr: region\n", "        expr: region\n        synonyms: [area]\n"
    )
    after = set_member_scalar(
        text, "orders", "dimensions", "REGION", "description", "Sales region."
    )
    added, removed = _lines_changed(text, after)
    assert (added, removed) == (["        description: Sales region."], [])
    assert "        synonyms: [area]\n" in after
    again = set_member_scalar(
        after, "orders", "dimensions", "region", "description", "Region."
    )
    added, removed = _lines_changed(after, again)
    assert added == ["        description: Region."]
    assert removed == ["        description: Sales region."]


def test_a_member_is_appended_after_the_last_one():
    after = append_member(TEXT, "orders", "dimensions", {"name": "x", "expr": "x"})
    dims = read_yaml(after)["tables"][0]["dimensions"]
    assert [d["name"] for d in dims] == ["status", "region", "x"]
    assert after.startswith(TEXT.split("  - name: customers")[0].rstrip("\n"))


def test_a_missing_section_is_added_at_the_end_of_the_table():
    after = append_member(
        TEXT, "orders", "metrics", {"name": "n", "expr": "COUNT(order_id)"}
    )
    body = read_yaml(after)
    assert body["tables"][0]["metrics"] == [{"name": "n", "expr": "COUNT(order_id)"}]
    assert body["tables"][1]["name"] == "customers"
    added, removed = _lines_changed(TEXT, after)
    assert removed == []
    assert added == ["    metrics:", "      - name: n", "        expr: COUNT(order_id)"]


@pytest.mark.parametrize(
    "text",
    [
        "name: v\ntables: [{name: t, base_table: {table: t}}]\n",
        "name: v\ntables:\n  - {name: t, base_table: {table: t}}\n",
    ],
)
def test_a_flow_style_table_is_refused(text):
    with pytest.raises(UnwritableError):
        set_table_scalar(text, "t", "description", "x")


def test_a_flow_style_member_list_is_refused():
    text = TEXT.replace(
        "    dimensions:\n      - name: segment\n        expr: segment\n",
        "    dimensions: [{name: segment, expr: segment}]\n",
    )
    with pytest.raises(UnwritableError):
        append_member(text, "customers", "dimensions", {"name": "x", "expr": "x"})


def test_an_unknown_table_is_refused():
    with pytest.raises(UnwritableError, match="nowhere"):
        replace_member(TEXT, "nowhere", "dimensions", "x", {"name": "x"})
