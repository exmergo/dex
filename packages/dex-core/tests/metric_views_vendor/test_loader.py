"""Dex's port of the Metric View YAML 1.1 rules, and how the loader applies them.

Each refusal names the file and the element, and a refused element leaves the
rest of its view readable; a refusal of what the whole view rests on (its
version, source, parameters, materialization) leaves the view out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlglot")

import yaml

from exmergo_dex_core.metric_views.loader import (
    ERROR,
    WARNING,
    load_files,
    validate_view_contents,
)
from exmergo_dex_core.metric_views.validator import RULES, derived_name

from .conftest import ORDERS, view, write


def _load(body: dict[str, Any] | str, name: str = "views/orders.yaml"):
    """Validate one body (a mapping or raw YAML) as `views/orders.yaml`."""
    text = body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False)
    return validate_view_contents({name: text})


def _rules(result) -> dict[str, str]:
    """Each diagnostic's rule, with its severity."""
    return {d.rule: d.severity for d in result.diagnostics}


def _field(**entry: Any) -> dict[str, Any]:
    """A view with one field carrying ``entry``."""
    return view(fields=[{"name": "x", "expr": "x", **entry}])


def _measure(**entry: Any) -> dict[str, Any]:
    """A view with one measure carrying ``entry``."""
    return view(measures=[{"name": "m", "expr": "SUM(x)", **entry}])


def _join(**entry: Any) -> dict[str, Any]:
    """A view with one join carrying ``entry``."""
    body = {"name": "c", "source": "demo.main.c", "on": "source.a = c.a", **entry}
    return view(joins=[body], fields=[{"name": "x", "expr": "x"}])


def test_the_reference_star_reads_with_no_error():
    """`ORDERS` reads whole: three joins, six fields, seven measures."""
    result = _load(ORDERS)

    assert result.errors == []
    (orders,) = result.views
    assert orders.name == "orders"
    assert [j.dotted for j in orders.all_joins()] == [
        "customer",
        "lines",
        "customer.nation",
    ]
    assert len(orders.fields) == 6 and len(orders.measures) == 7


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        ({"source": "a.b.c", "fields": [{"name": "x", "expr": "x"}]}, "version"),
        (view(fields=[{"name": "x", "expr": "x"}]) | {"version": 0.1}, "version"),
        (view(fields=[{"name": "x", "expr": "x"}]) | {"version": "1.2"}, "version"),
        ({"version": 1.1, "fields": [{"name": "x", "expr": "x"}]}, "source"),
        (view(), "empty_view"),
        (
            _field()
            | {
                "parameters": [
                    {"name": "a", "data_type": "int", "default": 1},
                    {"name": "b", "data_type": "int"},
                ]
            },
            "parameter_default_order",
        ),
        (_field() | {"parameters": [{"name": "a"}]}, "parameters"),
        (
            _field()
            | {"materialization": {"mode": "strict", "materialized_views": []}},
            "materialization",
        ),
        (
            _field()
            | {
                "materialization": {
                    "mode": "relaxed",
                    "materialized_views": [
                        {"name": "a", "type": "unaggregated"},
                        {"name": "b", "type": "unaggregated"},
                    ],
                }
            },
            "materialization",
        ),
        ("- just\n- a list\n", "root"),
    ],
)
def test_a_view_level_refusal_leaves_the_view_out(body, rule):
    """What the whole view rests on refuses the view, naming the file."""
    result = _load(body)

    assert result.views == []
    assert _rules(result).get(rule) == ERROR
    assert all(d.file == "views/orders.yaml" for d in result.diagnostics)


def test_the_version_refusal_names_the_version_and_the_way_up():
    """The message names both versions and the re-save."""
    result = _load(_field() | {"version": 0.1})
    (message,) = [d.message for d in result.errors]

    assert "0.1" in message and "1.1" in message and "ALTER VIEW" in message


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        (_field(display_name="x" * 256), "display_name"),
        (_field(synonyms=[f"s{i}" for i in range(11)]), "synonyms"),
        (_field(synonyms="one"), "synonyms"),
        (_field(format={"type": "colour"}), "format"),
        (_field(format={"type": "currency"}), "format"),
        (
            _field(format={"type": "number", "decimal_places": {"type": "max"}}),
            "format",
        ),
        (
            _field(
                format={
                    "type": "date_time",
                    "date_format": "no_date",
                    "time_format": "no_time",
                }
            ),
            "format",
        ),
        (_field(name=""), "empty_name"),
        (_measure(window=[{"order": "d", "range": "fortnightly"}]), "window"),
        (_measure(window=[{"range": "cumulative"}]), "window"),
        (
            _measure(window=[{"order": "d", "range": "current", "offset": "2 weeks"}]),
            "window",
        ),
        (
            _measure(partition={"include": ["x"], "outer_aggregate": "MODE"}),
            "partition",
        ),
        (
            _measure(
                window=[{"order": "d", "range": "cumulative"}],
                partition={"include": ["x"], "outer_aggregate": "SUM"},
            ),
            "window_and_partition",
        ),
    ],
)
def test_a_member_refusal_keeps_the_rest_of_the_view(body, rule):
    """One bad field or measure is refused; its neighbour stays."""
    body.setdefault("fields", []).append({"name": "kept", "expr": "kept"})
    result = _load(body)

    assert _rules(result).get(rule) == ERROR
    (only,) = result.views
    assert [f.name for f in only.fields] == ["kept"]
    assert only.measures == ()


@pytest.mark.parametrize(
    "expr",
    [
        "trailing 7 day",
        "leading 3 month inclusive",
        "trailing 30 exclusive",
        "trailing days_back day",
        "current",
        "cumulative",
        "all",
    ],
)
def test_every_documented_window_range_reads(expr):
    """Each `range` form the specification documents; `days_back` is a declared
    integer parameter standing for the number."""
    body = _measure(window=[{"order": "d", "range": expr, "offset": "-1 month"}])
    body["parameters"] = [{"name": "days_back", "data_type": "INT", "default": 7}]
    result = _load(body)

    assert result.errors == []


def test_names_are_unique_across_fields_and_measures_ignoring_case():
    """The later of two is refused, so the earlier keeps its readers."""
    result = _load(
        view(
            fields=[{"name": "Revenue", "expr": "x"}],
            measures=[{"name": "revenue", "expr": "SUM(x)"}],
        )
    )

    assert _rules(result)["duplicate_name"] == ERROR
    (only,) = result.views
    assert [f.name for f in only.fields] == ["Revenue"] and only.measures == ()


@pytest.mark.parametrize(
    ("entry", "rule"),
    [
        ({"on": None}, "join_condition"),
        ({"on": None, "using": []}, "join_condition"),
        ({"cardinality": "many_to_many"}, "cardinality"),
        ({"rely": {"at_most_one_match": "yes"}}, "rely"),
        ({"name": "source"}, "join"),
        ({"name": "has space"}, "join"),
        ({"source": ""}, "join"),
    ],
)
def test_a_malformed_join_is_refused_by_name(entry, rule):
    """Each join rule refuses the join and leaves the view."""
    body = _join(**entry)
    if entry.get("on", "") is None:
        del body["joins"][0]["on"]
    result = _load(body)

    assert _rules(result).get(rule) == ERROR
    (only,) = result.views
    assert only.joins == ()


def test_a_nested_join_keeps_its_parents_cardinality():
    """Databricks refuses a cardinality mix down a join tree."""
    body = _join(
        cardinality="one_to_many",
        joins=[{"name": "d", "source": "demo.main.d", "using": ["k"]}],
    )
    result = _load(body)

    assert _rules(result)["cardinality_mix"] == ERROR
    (only,) = result.views
    assert [j.dotted for j in only.all_joins()] == ["c"]


def test_sibling_joins_need_distinct_names():
    """A join's name is a column prefix, so siblings cannot share one."""
    body = view(
        joins=[
            {"name": "c", "source": "demo.main.c", "using": ["a"]},
            {"name": "C", "source": "demo.main.d", "using": ["a"]},
        ],
        fields=[{"name": "x", "expr": "x"}],
    )
    result = _load(body)

    assert _rules(result)["duplicate_join"] == ERROR
    assert [j.name for j in result.views[0].joins] == ["c"]


def test_on_beside_using_warns_and_on_wins():
    """As in Databricks, which reads `on`."""
    result = _load(_join(using=["a"]))

    assert _rules(result)["on_and_using"] == WARNING
    (join,) = result.views[0].joins
    assert join.on and join.using is None


def test_a_field_cannot_read_a_one_to_many_join():
    """A measure may aggregate over one; a field may not read it."""
    body = _join(cardinality="one_to_many")
    body["fields"].append({"name": "y", "expr": "c.y"})
    body["measures"] = [{"name": "total", "expr": "SUM(c.y)"}]
    result = _load(body)

    assert _rules(result)["one_to_many_field"] == ERROR
    (only,) = result.views
    assert [f.name for f in only.fields] == ["x"]
    assert [m.name for m in only.measures] == ["total"]


def test_measure_references_resolve_and_a_refusal_retracts_its_readers():
    """Unknown, self-referencing, and retracted measures."""
    body = view(
        measures=[
            {"name": "a", "expr": "MEASURE(missing) + 1"},
            {"name": "b", "expr": "MEASURE(a) * 2"},
            {"name": "c", "expr": "MEASURE(c)"},
            {"name": "d", "expr": "SUM(x)"},
            {"name": "e", "expr": "MEASURE(D) / 2"},
        ]
    )
    result = _load(body)

    refused = {d.element for d in result.errors}
    assert refused == {"measures['a']", "measures['b']", "measures['c']"}
    assert {d.rule for d in result.errors} == {"unknown_measure", "measure_cycle"}
    assert [m.name for m in result.views[0].measures] == ["d", "e"]


def test_an_unknown_key_warns_and_reads_on():
    """Databricks refuses it; reading, dex only warns."""
    body = _field(colour="red") | {"owner": "me"}
    result = _load(body)

    assert _rules(result) == {"unknown_key": WARNING}
    assert result.views[0].fields[0].name == "x"


def test_a_wildcard_is_named_and_never_expanded():
    """Its members come from the warehouse schema, which dex does not read."""
    body = view(
        fields=[
            {"expr": "source.*"},
            {"expr": "c.* EXCEPT (a, b)"},
            {"name": "x", "expr": "x"},
        ]
    )
    result = _load(body)

    (only,) = result.views
    assert [w.expr for w in only.wildcards] == ["source.*", "c.* EXCEPT (a, b)"]
    assert [f.name for f in only.fields] == ["x"]
    assert _rules(result)["wildcard"] == WARNING


def test_a_wildcard_takes_no_metadata():
    """A wildcard with a `name` is refused, as Databricks refuses it."""
    result = _load(
        view(fields=[{"name": "all", "expr": "source.*"}, {"name": "x", "expr": "x"}])
    )

    assert _rules(result)["wildcard_metadata"] == ERROR


@pytest.mark.parametrize(
    ("expr", "name"),
    [
        ("o_orderkey", "o_orderkey"),
        ("customer.nation.n_name", "n_name"),
        ("CAST(source.amount AS DECIMAL(10, 2))", "amount"),
        ("SUM(a + b)", "SUM(a+b)"),
    ],
)
def test_a_missing_name_is_derived_the_way_databricks_derives_it(expr, name):
    """Checked against the names Databricks gives the same expressions."""
    assert derived_name(expr) == name
    result = _load(view(fields=[{"expr": expr}]))
    assert result.views[0].fields[0].name == name
    assert result.views[0].fields[0].derived_name
    assert _rules(result)["derived_name"] == WARNING


def test_yaml_1_2_booleans_keep_a_bare_on_key():
    """YAML 1.1 reads `on:` as True; Databricks does not, and neither does dex."""

    text = (
        "version: 1.1\nsource: demo.main.orders\n"
        "joins:\n  - name: c\n    source: demo.main.c\n    on: source.a = c.a\n"
        "fields:\n  - name: x\n    expr: x\n"
    )
    result = _load(text)

    assert result.errors == []
    assert result.views[0].joins[0].on == "source.a = c.a"


def test_a_key_written_twice_is_refused():
    """Which of two keys a reader keeps is not something dex guesses."""
    result = _load("version: 1.1\nsource: a.b.c\nsource: d.e.f\nfields: []\n")

    assert result.views == []
    assert _rules(result) == {"malformed": ERROR}
    assert "written twice" in result.diagnostics[0].message


def test_dimensions_is_read_as_the_older_spelling_of_fields():
    """And noted as such."""
    result = _load(view(dimensions=[{"name": "x", "expr": "x"}]))

    assert result.errors == []
    assert result.views[0].fields_key == "dimensions"
    assert result.views[0].fields[0].name == "x"


def test_every_rule_is_in_the_table_with_a_severity():
    """Each rule carries an error or a warning."""
    assert set(RULES.values()) == {ERROR, WARNING}


def test_a_missing_file_and_one_outside_the_repository_are_named(tmp_path: Path):
    """A symlink out of the repository is caught on the resolved path."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text(yaml.safe_dump(ORDERS), encoding="utf-8")
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "escape.yaml").symlink_to(outside)

    result = load_files(tmp_path, ["views/missing.yaml", "views/escape.yaml"])

    assert {(d.file, d.rule) for d in result.diagnostics} == {
        ("views/missing.yaml", "missing"),
        ("views/escape.yaml", "confinement"),
    }
    assert result.views == []


def test_reading_writes_nothing(tmp_path: Path):
    """The repository is the same before and after a read."""
    write(tmp_path, "views/orders.yaml", ORDERS)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    load_files(tmp_path, ["views/orders.yaml"])

    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_a_key_that_is_not_text_is_refused_naming_the_on_rewrite():
    """What PyYAML's `safe_load` then `safe_dump` leaves of a join: `true:`."""

    text = (
        "version: 1.1\nsource: demo.main.orders\n"
        "joins:\n  - name: c\n    source: demo.main.c\n    true: source.a = c.a\n"
        "fields:\n  - name: x\n    expr: x\n"
    )
    result = _load(text)

    assert result.views == []
    (diagnostic,) = result.diagnostics
    assert diagnostic.rule == "malformed"
    assert "`on:`" in diagnostic.message


def test_with_fields_and_dimensions_both_written_dimensions_is_read():
    """What Databricks does (checked against CREATE VIEW ... WITH METRICS: the
    view's columns are the `dimensions` ones), and what the converter does."""

    result = _load(
        view(
            fields=[{"name": "x", "expr": "x"}],
            dimensions=[{"name": "y", "expr": "y"}],
        )
    )

    assert _rules(result) == {"fields_and_dimensions": WARNING}
    (only,) = result.views
    assert [f.name for f in only.fields] == ["y"]
    assert only.fields_key == "dimensions"


def test_a_measure_reads_only_the_measures_before_it():
    """A forward `MEASURE()` is unresolved in Databricks."""
    result = _load(
        view(
            measures=[
                {"name": "ratio", "expr": "MEASURE(revenue) / MEASURE(qty)"},
                {"name": "revenue", "expr": "SUM(price)"},
                {"name": "qty", "expr": "SUM(quantity)"},
                {"name": "aov", "expr": "MEASURE(revenue) / MEASURE(qty)"},
            ]
        )
    )

    assert {(d.element, d.rule) for d in result.errors} == {
        ("measures['ratio']", "forward_measure")
    }
    assert [m.name for m in result.views[0].measures] == ["revenue", "qty", "aov"]


def test_a_field_reading_a_later_fields_name_warns():
    """Databricks reads it as a source column, which only the warehouse confirms."""
    result = _load(
        view(
            fields=[
                {"name": "upper_mode", "expr": "UPPER(mode)"},
                {"name": "mode", "expr": "l_shipmode"},
                {"name": "lower_mode", "expr": "LOWER(mode)"},
            ]
        )
    )

    assert {(d.element, d.rule) for d in result.diagnostics} == {
        ("fields['upper_mode']", "forward_field")
    }
    assert len(result.views[0].fields) == 3


def test_a_filter_reading_a_fields_name_warns():
    """A view `filter` reads source columns only."""
    result = _load(
        view(filter="mode != 'AIR'", fields=[{"name": "mode", "expr": "l_shipmode"}])
    )

    assert _rules(result) == {"filter_reads_field": WARNING}


def test_a_nested_join_is_read_through_its_whole_path():
    """`customer.segment` under `orders` is unresolved in Databricks."""
    body = view(
        joins=[
            {
                "name": "orders",
                "source": "demo.main.orders",
                "on": "source.order_id = orders.order_id",
                "joins": [
                    {
                        "name": "customer",
                        "source": "demo.main.customers",
                        "on": "orders.customer_id = customer.id",
                    }
                ],
            }
        ],
        fields=[
            {"name": "seg", "expr": "customer.segment"},
            {"name": "seg_ok", "expr": "orders.customer.segment"},
        ],
        measures=[{"name": "n", "expr": "COUNT(DISTINCT customer.id)"}],
    )
    result = _load(body)

    refused = {(d.element, d.rule) for d in result.errors}
    assert refused == {
        ("fields['seg']", "nested_join_path"),
        ("measures['n']", "nested_join_path"),
    }
    assert "orders.customer.segment" in result.errors[0].message
    assert [f.name for f in result.views[0].fields] == ["seg_ok"]


@pytest.mark.parametrize(
    ("window", "ok"),
    [
        ({"range": "trailing days_back day", "offset": "days_back day"}, True),
        ({"range": "current", "offset": "days_back"}, True),
        ({"range": "trailing undeclared day"}, False),
        ({"range": "current", "offset": "undeclared day"}, False),
        ({"range": "trailing label day"}, False),  # a string parameter
    ],
)
def test_a_window_number_may_be_a_declared_integer_parameter(window, ok):
    """The specification lets an integer parameter stand for a window's number,
    by its bare name; one naming no integer parameter of the view is refused."""

    body = _measure(window=[{"order": "d", **window}])
    body["parameters"] = [
        {"name": "days_back", "data_type": "int", "default": 7},
        {"name": "label", "data_type": "string", "default": "'x'"},
    ]
    result = _load(body)

    assert (result.errors == []) is ok, result.errors
    assert bool(result.views[0].measures) is ok


def test_a_parameterized_view_cannot_be_materialized():
    """Databricks binds parameters per query, so it refuses to materialize one."""

    body = _field() | {
        "parameters": [{"name": "region", "data_type": "string", "default": "'EU'"}],
        "materialization": {
            "mode": "relaxed",
            "materialized_views": [
                {"name": "m", "type": "aggregated", "dimensions": ["x"]}
            ],
        },
    }
    result = _load(body)

    assert result.views == []
    assert _rules(result) == {"materialization": ERROR}


def test_an_unhashable_format_type_refuses_the_member_and_not_the_layer():
    """`type: [number]` is a member's problem; the other files still read."""

    body = _field(format={"type": ["number"]})
    body["fields"].append({"name": "kept", "expr": "kept"})
    result = validate_view_contents(
        {
            "views/orders.yaml": yaml.safe_dump(body),
            "views/other.yaml": yaml.safe_dump(
                view(fields=[{"name": "y", "expr": "y"}])
            ),
        }
    )

    assert {(d.file, d.rule) for d in result.errors} == {
        ("views/orders.yaml", "format")
    }
    assert [[f.name for f in v.fields] for v in result.views] == [["kept"], ["y"]]


def test_a_window_size_parameter_named_after_a_window_keyword_is_refused():
    """`trailing day day` reads either way; Databricks refuses the name."""

    body = _measure(window=[{"order": "d", "range": "trailing day day"}])
    body["parameters"] = [{"name": "day", "data_type": "int", "default": 7}]
    result = _load(body)

    assert _rules(result) == {"window": ERROR}
    assert result.views[0].measures == ()


def test_a_recursive_yaml_alias_refuses_its_file_and_not_the_layer():
    """`&loop [*loop]` has no end: it would stop the fingerprint's hash."""

    text = (
        "version: 1.1\nsource: demo.main.orders\nextra: &loop [*loop]\n"
        "fields:\n  - name: x\n    expr: x\n"
    )
    result = validate_view_contents(
        {
            "views/orders.yaml": text,
            "views/other.yaml": yaml.safe_dump(
                view(fields=[{"name": "y", "expr": "y"}])
            ),
        }
    )

    assert {(d.file, d.rule) for d in result.errors} == {
        ("views/orders.yaml", "malformed")
    }
    assert [v.name for v in result.views] == ["other"]


def test_aliases_that_expand_exponentially_refuse_their_file_quickly():
    """Thirty levels of `[*prev, *prev]` are 2**30 values once followed; the
    check sizes each alias once, and the file is refused rather than stalling."""

    import time

    lines = ["version: 1.1", "source: demo.main.orders", "a0: &a0 [0]"]
    lines += [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 31)]
    lines += ["fields:", "  - name: x", "    expr: x"]
    started = time.monotonic()
    result = _load("\n".join(lines) + "\n")

    assert time.monotonic() - started < 5
    assert result.views == []
    (diagnostic,) = result.diagnostics
    assert diagnostic.rule == "malformed" and "expand" in diagnostic.message


def test_a_shared_alias_within_bounds_still_reads():
    """An alias reused a few times is ordinary YAML."""

    text = (
        "version: 1.1\nsource: demo.main.orders\n"
        "fields:\n  - &f {name: x, expr: x}\n"
        "measures:\n  - {name: n, expr: COUNT(1)}\n"
    )
    result = _load(text)

    assert result.errors == []


def test_an_oversized_alias_file_is_named_and_the_next_file_still_reads():
    """The count saturates at the cap, so even a document whose expansion has
    more digits than Python formats is named, and the next file still reads."""

    lines = ["version: 1.1", "source: demo.main.orders", "a0: &a0 [0]"]
    lines += [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 200)]
    lines += ["fields:", "  - name: x", "    expr: x"]
    result = validate_view_contents(
        {
            "views/orders.yaml": "\n".join(lines) + "\n",
            "views/other.yaml": yaml.safe_dump(
                view(fields=[{"name": "y", "expr": "y"}])
            ),
        }
    )

    (diagnostic,) = result.diagnostics
    assert (diagnostic.file, diagnostic.rule) == ("views/orders.yaml", "malformed")
    assert "more than 100000" in diagnostic.message
    assert [v.name for v in result.views] == ["other"]
