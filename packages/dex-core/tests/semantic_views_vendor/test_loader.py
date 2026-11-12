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
