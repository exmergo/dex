"""Reading Semantic View files: YAML, confinement and naming (#1291).

The rules themselves are held to Snowflake's own verdicts in `test_corpus.py`;
this file covers what sits around them: the strict YAML loader shared with
Metric Views, the repository confinement, and how a view is named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.semantic_views.loader import (
    load_files,
    read_yaml,
    validate_view_contents,
)

from .conftest import SHOP, sv, table, write

ORDERS = sv(
    "orders_view",
    table(
        "orders",
        "demo.main.orders",
        ["order_id"],
        metrics=[{"name": "n", "expr": "COUNT(order_id)"}],
    ),
)


def _rules(loaded) -> set[str]:
    return {d.rule for d in loaded.diagnostics}


def test_yaml_one_point_two_keeps_on_and_yes_as_text() -> None:
    """A YAML 1.1 reader turns them into booleans; Snowflake does not."""
    assert read_yaml("a: on\nb: yes\nc: true\n") == {"a": "on", "b": "yes", "c": True}


def test_a_duplicate_key_is_refused_with_the_file(tmp_path: Path) -> None:
    """Snowflake keeps one and accepts the file; which one is not dex's guess."""
    names = [write(tmp_path, "views/v.yaml", raw="name: a\nname: b\ntables: []\n")]
    loaded = load_files(tmp_path, names)

    (error,) = loaded.errors
    assert (error.file, error.rule) == ("views/v.yaml", "malformed")
    assert "written twice" in error.message


def test_a_key_that_is_not_text_is_refused(tmp_path: Path) -> None:
    """Every Semantic View key is a name."""
    names = [write(tmp_path, "views/v.yaml", raw="name: v\n1: x\ntables: []\n")]

    (error,) = load_files(tmp_path, names).errors
    assert "every Semantic View key is" in error.message


def test_a_recursive_alias_is_refused_rather_than_walked(tmp_path: Path) -> None:
    """A definition with no end refuses its file, never stalls the layer."""
    raw = "name: v\ntables: &t [*t]\n"
    names = [write(tmp_path, "views/v.yaml", raw=raw)]

    (error,) = load_files(tmp_path, names).errors
    assert error.rule == "malformed" and "no end" in error.message


def test_an_absolute_path_and_an_escape_are_refused(tmp_path: Path) -> None:
    """Confinement is checked on the resolved path."""
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("name: x\n", encoding="utf-8")
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "link.yaml").symlink_to(outside)

    loaded = load_files(tmp_path, [str(outside), "views/link.yaml", "views/none.yaml"])

    assert [d.rule for d in loaded.diagnostics] == [
        "confinement",
        "confinement",
        "missing",
    ]


def test_an_empty_file_warns_and_declares_nothing(tmp_path: Path) -> None:
    """Not an error: the file declares no view yet."""
    loaded = load_files(tmp_path, [write(tmp_path, "views/v.yaml", raw="")])

    assert loaded.empty and loaded.errors == []
    assert _rules(loaded) == {"empty_file"}


def test_the_view_is_named_by_its_name_and_a_stem_that_disagrees_warns(
    tmp_path: Path,
) -> None:
    """What Snowflake creates is the document's `name`."""
    loaded = load_files(tmp_path, [write(tmp_path, "views/other.yaml", ORDERS)])

    assert [v.name for v in loaded.views] == ["orders_view"]
    assert _rules(loaded) == {"file_name"}


def test_two_files_declaring_one_view_refuse_the_second(tmp_path: Path) -> None:
    """Names compared ignoring case, as Snowflake compares identifiers."""
    shouted = {**ORDERS, "name": "ORDERS_VIEW"}
    names = [
        write(tmp_path, "views/orders_view.yaml", ORDERS),
        write(tmp_path, "views/orders_view_copy.yaml", shouted),
    ]
    loaded = load_files(tmp_path, names)

    assert [v.file for v in loaded.views] == ["views/orders_view.yaml"]
    (error,) = loaded.errors
    assert (error.file, error.rule) == ("views/orders_view_copy.yaml", "duplicate_view")


def test_a_shape_no_rule_anticipated_refuses_its_file_only(tmp_path: Path) -> None:
    """The other files still read."""
    names = [
        write(tmp_path, "views/bad.yaml", raw="name: bad\ntables: [[1, 2]]\n"),
        write(tmp_path, "views/shop.yaml", SHOP),
    ]
    loaded = load_files(tmp_path, names)

    assert [v.name for v in loaded.views] == ["shop"]
    assert {d.file for d in loaded.errors} == {"views/bad.yaml"}


def test_an_unknown_key_warns_and_is_carried(tmp_path: Path) -> None:
    """Read, not refused: Snowflake's own unknown-key handling is not dex's to
    second-guess on read."""
    body = {**ORDERS, "owner_team": "analytics"}
    loaded = load_files(tmp_path, [write(tmp_path, "views/orders_view.yaml", body)])

    assert loaded.errors == []
    assert _rules(loaded) == {"unknown_key"}


def test_a_refused_member_retracts_the_members_that_read_it(tmp_path: Path) -> None:
    """To a fixed point, each named."""
    view = sv(
        "chain",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            facts=[{"name": "bad", "expr": "MAX(amount)"}],
            metrics=[
                {"name": "a", "expr": "SUM(orders.bad)"},
                {"name": "b", "expr": "orders.a * 2"},
            ],
        ),
    )
    loaded = validate_view_contents({"views/chain.yaml": _dump(view)})

    refused = {d.element: d.rule for d in loaded.errors}
    assert refused == {
        "tables['orders'].facts['bad']": "row_level",
        "tables['orders'].metrics['a']": "reference",
        "tables['orders'].metrics['b']": "reference",
    }
    assert [t.metrics for t in loaded.views[0].tables] == [()]


def test_an_expression_dex_cannot_parse_warns_and_keeps_the_member() -> None:
    """A level-of-detail expression: carried, its rules unchecked, and said."""
    view = sv(
        "lod",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            metrics=[
                {"name": "fixed", "expr": "SUM(amount) FIXED DIMENSIONS (region)"}
            ],
        ),
    )
    loaded = validate_view_contents({"views/lod.yaml": _dump(view)})

    assert loaded.errors == []
    assert _rules(loaded) == {"unparsed_expression"}
    assert loaded.views[0].tables[0].metrics[0].name == "fixed"


def _dump(body) -> str:
    import yaml

    return yaml.safe_dump(body, sort_keys=False)


def test_a_name_no_reference_takes_is_unresolved_not_dropped() -> None:
    """A three-part name leaves the expression unparsed, never read as no read."""
    view = sv(
        "three",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            metrics=[{"name": "total", "expr": "SUM(db.other.amount)"}],
        ),
    )
    loaded = validate_view_contents({"views/three.yaml": _dump(view)})

    assert _rules(loaded) == {"unparsed_expression"}


def test_a_refused_member_is_never_read_as_a_column_of_its_name() -> None:
    """A fact refused for want of an `expr` refuses the metric reading it."""
    view = sv(
        "gone",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            facts=[{"name": "net"}],
            metrics=[{"name": "total", "expr": "SUM(net)"}],
        ),
    )
    loaded = validate_view_contents({"views/gone.yaml": _dump(view)})

    refused = {d.element: d.rule for d in loaded.errors}
    assert refused["tables['orders'].metrics['total']"] == "reference"
    assert loaded.views[0].tables[0].metrics == ()


def test_a_metric_loses_a_refused_non_additive_dimension() -> None:
    """The dimension it is non-additive over is part of what it reads."""
    view = sv(
        "semi",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            dimensions=[{"name": "bad", "expr": "MAX(amount)"}],
            metrics=[
                {
                    "name": "balance",
                    "expr": "SUM(amount)",
                    "non_additive_dimensions": [
                        {"table": "orders", "dimension": "bad"}
                    ],
                }
            ],
        ),
    )
    loaded = validate_view_contents({"views/semi.yaml": _dump(view)})

    refused = {d.element: d.rule for d in loaded.errors}
    assert refused["tables['orders'].metrics['balance']"] == "reference"


def test_a_dense_graph_with_an_unreachable_table_reads_quickly() -> None:
    """Reachability first, then a bounded search: an unrelated reference is
    refused without walking every path of the connected part."""
    import time

    names = [f"t{i}" for i in range(12)]
    tables = [
        table(n, f"demo.main.{n}", ["id"], facts=[{"name": "v", "expr": "v"}])
        for n in names
    ]
    tables.append(
        table(
            "lonely",
            "demo.main.lonely",
            ["id"],
            metrics=[{"name": "m", "expr": "SUM(t0.v)"}],
        )
    )
    rels = [
        {
            "name": f"r_{a}_{b}",
            "left_table": a,
            "right_table": b,
            "relationship_columns": [{"left_column": f"k_{b}", "right_column": "id"}],
        }
        for i, a in enumerate(names)
        for b in names[i + 1 :]
    ]
    started = time.monotonic()
    loaded = validate_view_contents(
        {"views/dense.yaml": _dump(sv("dense", *tables, relationships=rels))}
    )

    assert time.monotonic() - started < 10
    refused = {d.element: d.rule for d in loaded.errors}
    assert refused["tables['lonely'].metrics['m']"] == "reference"


def test_using_relationships_is_judged_by_the_path_it_names() -> None:
    """Three relationships from lines to orders: naming the third, which the
    bounded path search never lists, still selects a path."""

    rels = [
        {
            "name": name,
            "left_table": "lines",
            "right_table": "orders",
            "relationship_columns": [
                {"left_column": column, "right_column": "order_id"}
            ],
        }
        for name, column in (("a", "order_id"), ("b", "return_id"), ("c", "swap_id"))
    ]
    view = sv(
        "thrice",
        table(
            "lines",
            "demo.main.order_lines",
            ["line_id"],
            metrics=[
                {
                    "name": "n",
                    "expr": "COUNT(orders.key)",
                    "using_relationships": ["c"],
                }
            ],
        ),
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            facts=[{"name": "key", "expr": "order_id"}],
        ),
        relationships=rels,
    )
    loaded = validate_view_contents({"views/thrice.yaml": _dump(view)})

    assert loaded.errors == [], loaded.notes()
