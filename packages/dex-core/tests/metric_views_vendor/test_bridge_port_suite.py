"""The converter's own example tests, ported (#1260).

Each test is ``OssieConverterSuite`` from apache/ossie-converters
(``databricks/java``, at the commit the bridge pins), restated over dex's port.
The names follow the Java ones so a test can be held beside the one it ports.
Inputs are the Java tests' own YAML; outputs are compared parsed, as the Java
tests compare them.
"""

# The inputs are YAML documents kept as the converter's tests write them, one
# flow mapping per line, so a line can be longer than the code's limit.
# ruff: noqa: E501

from __future__ import annotations

from typing import Any

import pytest

from exmergo_dex_core.metric_views.loader import dump_yaml
from exmergo_dex_core.metric_views.ossie_bridge import (
    ConversionError,
    Kind,
    metric_view_to_ossie,
    ossie_to_metric_view,
)
from exmergo_dex_core.metric_views.ossie_bridge.common import (
    MAX_JOIN_NODES,
    OSSIE_VERSION,
    parse_yaml,
)
from exmergo_dex_core.yaml_items import dump_items


def export(osi: str, source: str | None = None) -> Any:
    return ossie_to_metric_view(osi, source).value


def import_mv(mv: str, name: str | None = None) -> Any:
    return metric_view_to_ossie(mv, name).value


def export_notices(osi: str, source: str | None = None) -> list[str]:
    return ossie_to_metric_view(osi, source).converter_notices()


def import_notices(mv: str) -> list[str]:
    return metric_view_to_ossie(mv).converter_notices()


def ossie_text(mv: str) -> str:
    """The import, written as dex writes an Ossie document."""
    return dump_items(import_mv(mv))


def mv_text(osi: str, source: str | None = None) -> str:
    """The export, written as dex writes a Metric View."""
    return dump_yaml(export(osi, source))


def ossie_model_with_body(body: str) -> str:
    """A flat model at the root: ``body`` de-indented two spaces beside ``name``."""
    lines = [line[2:] if line.startswith("  ") else line for line in body.split("\n")]
    return f"version: '{OSSIE_VERSION}'\nname: m\n" + "\n".join(lines)


def cascade_notice(kind: str, name: str, reference: str) -> str:
    return (
        f"[{kind} '{name}'] references dropped '{reference}'; dropping (downstream of "
        "a dropped field/metric)"
    )


def assert_invalid_ossie_input(yaml_text: str, expected_reason: str) -> None:
    with pytest.raises(ConversionError) as caught:
        ossie_to_metric_view(yaml_text)
    assert caught.value.kind is Kind.INVALID_INPUT
    assert caught.value.reason == expected_reason


def refused(fn: Any, *args: Any) -> ConversionError:
    with pytest.raises(ConversionError) as caught:
        fn(*args)
    return caught.value


# --- export -------------------------------------------------------------------------


def test_fixture_a_star_schema_exports_to_expected_metric_view():
    osi = """\
version: "0.2.0.dev0"
name: sales
description: Sales orders with customer attributes
datasets:
  - name: orders
    source: samples.tpch.orders
    primary_key: [o_orderkey]
    description: One row per order
    fields:
      - name: o_orderkey
        expression:
          dialects:
            - dialect: DATABRICKS
              expression: o_orderkey
        description: Order identifier
      - name: o_orderdate
        expression:
          dialects:
            - dialect: DATABRICKS
              expression: o_orderdate
        label: Order Date
        ai_context:
          synonyms: [order date, date]
  - name: customer
    source: samples.tpch.customer
    primary_key: [c_custkey]
    fields:
      - name: c_name
        expression:
          dialects:
            - dialect: DATABRICKS
              expression: c_name
        description: Customer name
relationships:
  - name: orders_to_customer
    from: orders
    to: customer
    from_columns: [o_custkey]
    to_columns: [c_custkey]
metrics:
  - name: total_revenue
    expression:
      dialects:
        - dialect: DATABRICKS
          expression: SUM(o_totalprice)
    description: Total order revenue
    ai_context:
      synonyms: [revenue, total revenue, sales]
  - name: order_count
    expression:
      dialects:
        - dialect: DATABRICKS
          expression: COUNT(*)
    description: Number of orders
"""
    expected = """\
version: '1.1'
source: samples.tpch.orders
comment: Sales orders with customer attributes
joins:
- name: customer
  source: samples.tpch.customer
  on: source.o_custkey = customer.c_custkey
  rely:
    at_most_one_match: true
dimensions:
- name: o_orderkey
  expr: o_orderkey
  comment: Order identifier
- name: o_orderdate
  expr: o_orderdate
  display_name: Order Date
  synonyms:
  - order date
  - date
- name: c_name
  expr: customer.c_name
  comment: Customer name
measures:
- name: total_revenue
  expr: SUM(o_totalprice)
  comment: Total order revenue
  synonyms:
  - revenue
  - total revenue
  - sales
- name: order_count
  expr: COUNT(*)
  comment: Number of orders
"""
    assert export(osi) == parse_yaml(expected)


def test_measure_display_name_survives_round_trip_via_stash():
    mv = """\
version: '1.1'
source: c.s.fact
dimensions:
- name: region
  expr: region
measures:
- name: total_revenue
  expr: SUM(amount)
  display_name: Total Revenue
"""
    ossie = ossie_text(mv)
    assert "display_name" in ossie
    assert parse_yaml(mv_text(ossie)) == parse_yaml(mv)


def test_unsupported_version_is_rejected():
    error = refused(ossie_to_metric_view, "version: '9.9'\nname: m\n")
    assert "Unsupported Apache Ossie version" in error.message
    assert error.kind is Kind.UNSUPPORTED_VERSION


def test_multiple_candidate_facts_without_source_is_rejected():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- {name: orders, source: c.s.orders}
- {name: returns, source: c.s.returns}
- {name: customer, source: c.s.customer, primary_key: [c_custkey]}
relationships:
- {name: oc, from: orders, to: customer, from_columns: [o_custkey], to_columns: [c_custkey]}
- {name: rc, from: returns, to: customer, from_columns: [re_custkey], to_columns: [c_custkey]}
"""
    assert (
        "multiple candidate fact datasets" in refused(ossie_to_metric_view, osi).message
    )


def test_one_to_many_emits_cardinality():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: orders
  source: c.s.orders
  primary_key: [o_orderkey]
  fields:
  - {name: o_orderstatus, expression: {dialects: [{dialect: DATABRICKS, expression: o_orderstatus}]}}
- name: lineitem
  source: c.s.lineitem
relationships:
- {name: lio, from: lineitem, to: orders, from_columns: [l_orderkey], to_columns: [o_orderkey]}
metrics:
- {name: qty, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(lineitem.l_quantity)}]}}
"""
    assert export(osi, "orders")["joins"][0]["cardinality"] == "one_to_many"


def test_one_to_many_nested_under_many_to_one_is_rejected():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: d
  source: c.s.d
  fields:
  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}
- name: f
  source: c.s.f
  primary_key: [fk]
- name: g
  source: c.s.g
relationships:
- {name: df, from: d, to: f, from_columns: [fk], to_columns: [fk]}
- {name: gf, from: g, to: f, from_columns: [fk], to_columns: [fk]}
metrics:
- {name: c, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(1)}]}}
"""
    assert (
        "share the same cardinality" in refused(ossie_to_metric_view, osi, "d").message
    )


def test_directed_cycle_with_no_equidistant_edge_is_rejected():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: a
  source: c.s.a
  fields:
  - {name: acol, expression: {dialects: [{dialect: DATABRICKS, expression: acol}]}}
- name: b
  source: c.s.b
- name: c
  source: c.s.c
- name: d
  source: c.s.d
- name: e
  source: c.s.e
relationships:
- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}
- {name: bc, from: b, to: c, from_columns: [k], to_columns: [k]}
- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}
- {name: de, from: d, to: e, from_columns: [k], to_columns: [k]}
- {name: eb, from: e, to: b, from_columns: [k], to_columns: [k]}
"""
    message = refused(ossie_to_metric_view, osi, "a").message
    assert "directed cycle" in message
    assert "b -> c -> d -> e -> b" in message


_DIAMOND = """\
version: 0.2.0.dev0
name: m
datasets:
- name: a
  source: c.s.a
- name: b
  source: c.s.b
- name: c
  source: c.s.c
- name: d
  source: c.s.d
  fields:
  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}
relationships:
- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}
- {name: ac, from: a, to: c, from_columns: [k], to_columns: [k]}
- {name: bd, from: b, to: d, from_columns: [k], to_columns: [k]}
- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}
"""


def test_diamond_is_still_accepted_by_the_cycle_check():
    view = export(_DIAMOND, "a")
    assert view["source"] == "c.s.a"
    assert len(view["dimensions"]) == 2


def test_measure_naming_a_diamond_dataset_is_dropped_with_notice():
    osi = _DIAMOND + (
        "metrics:\n"
        "- {name: dsum, expression: {dialects: [{dialect: DATABRICKS, expression: "
        "SUM(d.dcol)}]}}\n"
    )
    result = ossie_to_metric_view(osi, "a")
    assert len(result.value["dimensions"]) == 2
    assert "measures" not in result.value
    expected = (
        "expression references dataset 'd', reached by more than one join path "
        "(diamond); cannot be unambiguously qualified; dropped"
    )
    assert any(expected in n for n in result.converter_notices())


def test_nested_join_column_in_measure_gets_full_alias_path():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: orders
  source: c.s.orders
- name: customer
  source: c.s.customer
  primary_key: [c_custkey]
- name: nation
  source: c.s.nation
  primary_key: [n_nationkey]
  fields:
  - {name: population, expression: {dialects: [{dialect: DATABRICKS, expression: population}]}}
relationships:
- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}
- {name: cn, from: customer, to: nation, from_columns: [n_nationkey], to_columns: [n_nationkey]}
metrics:
- {name: pop, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(nation.population)}]}}
"""
    view = export(osi, "orders")
    assert view["dimensions"][0]["expr"] == "customer.nation.population"
    assert view["measures"][0]["expr"] == "SUM(customer.nation.population)"


def test_model_with_no_fields_or_metrics_is_rejected():
    osi = "version: 0.2.0.dev0\nname: m\ndatasets:\n- name: d\n  source: c.s.d\n"
    assert "no dimensions or measures" in refused(ossie_to_metric_view, osi).message


def test_empty_after_drops_names_the_dropped_columns():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: d
  source: c.s.d
metrics:
- {name: only_metric, expression: {dialects: [{dialect: SNOWFLAKE, expression: SUM(x)}]}}
"""
    message = refused(ossie_to_metric_view, osi).message
    assert "no dimensions or measures" in message
    assert "measures [only_metric]" in message


def test_duplicate_dimension_name_is_rejected():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: orders
  source: c.s.orders
  fields:
  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}
- name: customer
  source: c.s.customer
  fields:
  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}
relationships:
- {name: r, from: orders, to: customer, from_columns: [cid], to_columns: [id]}
"""
    assert "collides" in refused(ossie_to_metric_view, osi).message


def test_foreign_vendor_extension_dropped_with_notice():
    osi = """\
version: 0.2.0.dev0
name: m
custom_extensions:
- {vendor_name: SNOWFLAKE, data: '{}'}
datasets:
- name: orders
  source: c.s.orders
  fields:
  - {name: s, expression: {dialects: [{dialect: DATABRICKS, expression: s}]}}
metrics:
- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}
"""
    assert "[model] foreign-vendor custom_extensions dropped" in export_notices(osi)


def test_datatype_dropped_with_notice():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: f
  source: c.s.f
  fields:
  - {name: d, datatype: String, expression: {dialects: [{dialect: DATABRICKS, expression: d}]}}
metrics:
- {name: n, datatype: Integer, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}
"""
    notices = export_notices(osi)
    assert "[field 'd'] datatype has no Metric View counterpart; dropped" in notices
    assert "[metric 'n'] datatype has no Metric View counterpart; dropped" in notices


def test_relationship_foreign_vendor_extension_dropped_with_notice():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: o
  source: c.s.o
  fields:
  - {name: a, expression: {dialects: [{dialect: DATABRICKS, expression: a}]}}
- {name: c, source: c.s.c, primary_key: [k]}
relationships:
- {name: oc, from: o, to: c, from_columns: [k], to_columns: [k], custom_extensions: [{vendor_name: SNOWFLAKE, data: '{}'}]}
metrics:
- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}
"""
    assert (
        "[relationship 'oc'] foreign-vendor custom_extensions dropped"
        in export_notices(osi)
    )


def test_scalar_join_columns_rejected_as_must_be_lists():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- {name: a, source: c.s.a, fields: [{name: x, expression: {dialects: [{dialect: DATABRICKS, expression: x}]}}]}
- {name: b, source: c.s.b}
relationships:
- {name: ab, from: a, to: b, from_columns: cid, to_columns: id}
"""
    assert "must be lists" in refused(ossie_to_metric_view, osi).message


def test_empty_explicit_source_falls_back_like_absent():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: orders
  source: c.s.orders
  fields:
  - {name: amount, expression: {dialects: [{dialect: DATABRICKS, expression: amount}]}}
- name: customer
  source: c.s.customer
  fields:
  - {name: c_name, expression: {dialects: [{dialect: DATABRICKS, expression: c_name}]}}
relationships:
- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}
metrics:
- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}
"""
    assert export(osi, None) == export(osi, "")


# --- import -------------------------------------------------------------------------


def test_empty_model_name_falls_back_like_absent():
    mv = """\
version: '1.1'
source: c.s.orders
dimensions:
- name: amount
  expr: amount
measures:
- name: n
  expr: COUNT(*)
"""
    assert import_mv(mv, None) == import_mv(mv, "")


def metric_view_with_joins(join_count: int) -> str:
    text = "version: '1.1'\nsource: c.s.fact\njoins:\n"
    for i in range(1, join_count + 1):
        text += f"- name: j{i}\n  source: c.s.j{i}\n  on: source.k = j{i}.k\n"
    return text


def test_import_decomposes_join_into_relationship():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- name: customer
  source: c.s.customer
  on: source.o_custkey = customer.c_custkey
  rely: {at_most_one_match: true}
dimensions:
- {name: o_status, expr: o_orderstatus}
- {name: c_name, expr: customer.c_name}
measures:
- {name: revenue, expr: SUM(o_totalprice)}
"""
    (rel,) = import_mv(mv)["relationships"]
    assert (rel["from"], rel["to"]) == ("orders", "customer")
    assert (rel["from_columns"], rel["to_columns"]) == (["o_custkey"], ["c_custkey"])


def test_export_rejects_more_joins_than_import_can_rebuild():
    message = refused(
        metric_view_to_ossie, metric_view_with_joins(MAX_JOIN_NODES)
    ).message
    assert f"at most {MAX_JOIN_NODES} are supported" in message


def test_export_at_the_join_limit_still_succeeds():
    out = import_mv(metric_view_with_joins(MAX_JOIN_NODES - 1))
    assert len(out["datasets"]) == MAX_JOIN_NODES


def test_non_equi_on_rejected():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- name: customer
  source: c.s.customer
  on: source.o_custkey >= customer.c_custkey
"""
    assert "non-equi" in refused(metric_view_to_ossie, mv).message


@pytest.mark.parametrize(
    "cond", ["source.a = dim.b OR source.c = dim.d", "source.a = dim.b + 1"]
)
def test_complex_equi_on_rejected(cond):
    mv = (
        "version: '1.1'\nsource: c.s.fact\njoins:\n- name: dim\n  source: c.s.dim\n"
        f"  on: {cond}\n"
    )
    assert "non-equi" in refused(metric_view_to_ossie, mv).message


def test_equi_join_on_preserved_verbatim_when_non_canonical():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- name: customer
  source: c.s.customer
  on: orders.o_custkey = customer.c_custkey
measures:
- name: revenue
  expr: SUM(o_totalprice)
"""
    assert "orders.o_custkey = customer.c_custkey" in mv_text(ossie_text(mv))


def test_equi_join_on_over_equal_columns_stays_on_not_using():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- name: customer
  source: c.s.customer
  on: source.custkey = customer.custkey
measures:
- name: cnt
  expr: COUNT(1)
"""
    back = mv_text(ossie_text(mv))
    assert "source.custkey = customer.custkey" in back
    assert "using" not in back


def test_import_rejects_cross_join():
    mv = "version: '1.1'\nsource: c.s.orders\njoins:\n- name: customer\n  source: c.s.customer\n"
    assert "no join condition" in refused(metric_view_to_ossie, mv).message


def test_import_rejects_unsupported_version():
    error = refused(metric_view_to_ossie, "version: '0.1'\nsource: c.s.t\n")
    assert "Unsupported Metric View version" in error.message
    assert error.kind is Kind.UNSUPPORTED_VERSION


def test_mv_to_ossie_to_mv_round_trips_stash():
    mv = """\
version: '1.1'
source: c.s.orders
filter: o_orderstatus = 'F'
joins:
- name: customer
  source: c.s.customer
  on: source.o_custkey = customer.c_custkey
  rely:
    at_most_one_match: true
dimensions:
- name: net
  expr: o_totalprice
  format:
    type: currency
    currency_code: USD
measures:
- name: running
  expr: SUM(o_totalprice)
  window:
  - order: net
    semiadditive: last
    range: cumulative
"""
    assert parse_yaml(mv_text(ossie_text(mv))) == parse_yaml(mv)


# --- dialects -----------------------------------------------------------------------


def _one_field(dialects: str) -> str:
    return f"""\
version: 0.2.0.dev0
name: m
datasets:
- name: f
  source: c.s.f
  fields:
  - name: d
    expression:
      dialects:
{dialects}
metrics:
- {{name: n, expression: {{dialects: [{{dialect: DATABRICKS, expression: COUNT(*)}}]}}}}
"""


def test_pick_expression_falls_through_empty_databricks_to_ansi():
    osi = _one_field(
        "      - {dialect: DATABRICKS, expression: ''}\n"
        "      - {dialect: ANSI_SQL, expression: ansi_col}"
    )
    assert export(osi)["dimensions"][0]["expr"] == "ansi_col"


def test_ossie_sql_2026_dimension_exported_not_dropped():
    osi = _one_field("      - {dialect: OSSIE_SQL_2026, expression: o_region}")
    assert export(osi)["dimensions"][0]["expr"] == "o_region"


def test_ossie_sql_2026_measure_exported_not_dropped():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: f
  source: c.s.f
  fields:
  - {name: region, expression: {dialects: [{dialect: DATABRICKS, expression: region}]}}
metrics:
- {name: rev, expression: {dialects: [{dialect: OSSIE_SQL_2026, expression: SUM(amount)}]}}
"""
    assert export(osi)["measures"][0]["expr"] == "SUM(amount)"


def test_databricks_dialect_preferred_over_ossie_sql_2026():
    osi = _one_field(
        "      - {dialect: OSSIE_SQL_2026, expression: portable_region}\n"
        "      - {dialect: DATABRICKS, expression: dbx_region}"
    )
    assert export(osi)["dimensions"][0]["expr"] == "dbx_region"


def test_unsupported_dialect_only_dropped_with_warning():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: f
  source: c.s.f
  fields:
  - {name: kept, expression: {dialects: [{dialect: DATABRICKS, expression: kept}]}}
  - {name: region, expression: {dialects: [{dialect: SNOWFLAKE, expression: o_region}]}}
metrics:
- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}
"""
    assert (
        "[field 'region'] no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect; dropping field"
        in export_notices(osi)
    )


def test_pick_expression_rejects_non_string_expression():
    osi = _one_field("      - {dialect: DATABRICKS, expression: 123}")
    message = refused(ossie_to_metric_view, osi).message
    assert message == "expression must be a string, got Integer"


def test_bare_on_off_values_stay_strings_not_booleans():
    assert parse_yaml("a: on\nb: off\nc: yes\nd: no\n") == {
        "a": "on",
        "b": "off",
        "c": "yes",
        "d": "no",
    }


def test_import_drops_empty_optional_fields():
    mv = """\
version: '1.1'
source: c.s.orders
comment: ''
dimensions:
- {name: o_status, expr: o_orderstatus, comment: '', display_name: '', synonyms: []}
measures:
- {name: revenue, expr: SUM(o_totalprice), comment: '', synonyms: []}
"""
    model = import_mv(mv)
    assert "description" not in model
    field = model["datasets"][0]["fields"][0]
    assert not {"description", "label", "ai_context"} & set(field)
    metric = model["metrics"][0]
    assert not {"description", "ai_context"} & set(metric)


# --- the stash ----------------------------------------------------------------------


def test_stash_escapes_non_ascii_as_lowercase_hex():
    mv = """\
version: '1.1'
source: c.s.orders
filter: "region = 'caf\u00e9'"
dimensions:
- {name: o_status, expr: o_orderstatus}
"""
    ossie = ossie_text(mv)
    blob = parse_yaml(ossie)["custom_extensions"][0]["data"]
    assert blob == '{"_v": 1, "filter": "region = \'caf\\u00e9\'"}'
    assert parse_yaml(mv_text(ossie)) == parse_yaml(mv)


def test_stash_preserves_a_value_containing_a_literal_unicode_escape():
    mv = (
        "version: '1.1'\nsource: c.s.orders\nfilter: 'tag = \\uABCD'\n"
        "dimensions:\n- {name: o_status, expr: o_orderstatus}\n"
    )
    original = parse_yaml(mv)["filter"]
    assert "\\" in original
    restored = parse_yaml(mv_text(ossie_text(mv)))
    assert restored["filter"] == original


# --- joins ----------------------------------------------------------------------------


def test_join_on_takes_precedence_over_using():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- name: cust
  source: c.s.customer
  on: source.o_custkey = cust.c_custkey
  using: [nation_key]
dimensions:
- {name: c_name, expr: cust.c_name}
measures:
- {name: cnt, expr: COUNT(1)}
"""
    (rel,) = import_mv(mv)["relationships"]
    assert rel["from_columns"] == ["o_custkey"]
    assert rel["to_columns"] == ["c_custkey"]


def test_measure_rewrite_leaves_string_literals_alone():
    mv = """\
version: '1.1'
source: c.s.orders
dimensions:
- {name: o_status, expr: o_orderstatus}
measures:
- name: tagged
  expr: "SUM(IF(source.region = 'source.us', 1, 0))"
"""
    ossie = ossie_text(mv)
    assert "'source.us'" in ossie
    assert "'source.us'" in mv_text(ossie)


def nested_join_model(metric_expr: str) -> str:
    return f"""\
version: "0.2.0.dev0"
name: sales
datasets:
  - name: lineitem
    source: cat.sch.lineitem
    fields:
      - name: l_key
        expression:
          dialects: [{{dialect: DATABRICKS, expression: l_key}}]
  - name: customer
    source: cat.sch.customer
    primary_key: [c_key]
  - name: region
    source: cat.sch.region
    primary_key: [r_key]
relationships:
  - name: l_to_c
    from: lineitem
    to: customer
    from_columns: [c_key]
    to_columns: [c_key]
  - name: c_to_r
    from: customer
    to: region
    from_columns: [r_key]
    to_columns: [r_key]
metrics:
  - name: pop
    expression:
      dialects: [{{dialect: DATABRICKS, expression: "{metric_expr}"}}]
"""


def first_measure_expr(view: dict[str, Any]) -> str:
    assert view.get("measures"), view
    return view["measures"][0]["expr"]


def test_export_qualifies_a_nested_measure_with_the_full_join_path():
    view = export(nested_join_model("SUM(region.population)"))
    assert first_measure_expr(view) == "SUM(customer.region.population)"


def test_export_does_not_qualify_a_measure_path_twice():
    view = export(nested_join_model("SUM(customer.region.population)"))
    assert first_measure_expr(view) == "SUM(customer.region.population)"


def test_nested_qualified_measure_round_trips_both_ways():
    mv = """\
version: '1.1'
source: cat.sch.lineitem
joins:
- name: customer
  source: cat.sch.customer
  using: [c_key]
  joins:
  - name: region
    source: cat.sch.region
    using: [r_key]
dimensions:
- {name: l_key, expr: l_key}
measures:
- {name: pop, expr: SUM(customer.region.population)}
"""
    ossie = ossie_text(mv)
    assert "SUM(region.population)" in ossie
    assert (
        first_measure_expr(parse_yaml(mv_text(ossie)))
        == "SUM(customer.region.population)"
    )


# --- the cascade ----------------------------------------------------------------------


def _model(fields: str, metrics: str = "") -> str:
    text = (
        'version: "0.2.0.dev0"\nname: m\ndatasets:\n  - name: d\n'
        f"    source: cat.sch.t\n    fields:\n{fields}"
    )
    return text + (f"metrics:\n{metrics}" if metrics else "")


def _field(name: str, dialect: str, expr: str) -> str:
    return (
        f"      - name: {name}\n        expression:\n"
        f"          dialects: [{{dialect: {dialect}, expression: {expr}}}]\n"
    )


def _metric(name: str, expr: str) -> str:
    return (
        f"  - name: {name}\n    expression:\n"
        f"      dialects: [{{dialect: DATABRICKS, expression: {expr}}}]\n"
    )


def test_cascade_drop_ignores_a_dropped_name_inside_a_string_literal():
    osi = _model(
        _field("amount", "DATABRICKS", "amount") + _field("us", "SNOWFLAKE", "us_col"),
        _metric("amt_us", "\"SUM(IF(region = 'us', amount, 0))\""),
    )
    result = ossie_to_metric_view(osi)
    assert first_measure_expr(result.value) == "SUM(IF(region = 'us', amount, 0))"
    assert not any(
        "amt_us" in n and "references dropped" in n for n in result.converter_notices()
    )


def test_cascade_drop_matches_a_dropped_name_case_insensitively():
    osi = _model(
        _field("id", "DATABRICKS", "id")
        + _field("region_name", "T_SQL", "region_name"),
        _metric("region_count", "COUNT(DISTINCT REGION_NAME)"),
    )
    result = ossie_to_metric_view(osi)
    assert "measures" not in result.value
    assert len(result.value["dimensions"]) == 1
    assert (
        cascade_notice("measure", "region_count", "region_name")
        in result.converter_notices()
    )


def test_cascade_drop_keeps_a_function_call_named_like_a_dropped_field():
    osi = _model(
        _field("order_date", "DATABRICKS", "order_date")
        + _field("year", "T_SQL", "year"),
        _metric("orders_2026", "COUNT_IF(YEAR(order_date) = 2026)"),
    )
    measures = export(osi)["measures"]
    assert [m["name"] for m in measures] == ["orders_2026"]


def test_cascade_drop_keeps_a_survivor_differing_from_a_dropped_field_only_in_case():
    osi = _model(
        _field("REGION", "T_SQL", "REGION") + _field("region", "DATABRICKS", "region")
    )
    assert [d["name"] for d in export(osi)["dimensions"]] == ["region"]


def test_cascade_drop_drops_a_measure_named_like_a_dropped_dimension():
    osi = _model(
        _field("id", "DATABRICKS", "id") + _field("region", "T_SQL", "region"),
        _metric("region", "SUM(region)"),
    )
    result = ossie_to_metric_view(osi)
    assert "measures" not in result.value
    assert cascade_notice("measure", "region", "region") in result.converter_notices()


def test_stash_preserves_a_backslash_before_a_unicode_escape():
    value = "tag = \\\u00e9"
    mv = (
        f"version: '1.1'\nsource: c.s.orders\nfilter: '{value}'\n"
        "dimensions:\n- {name: o_status, expr: o_orderstatus}\n"
    )
    assert parse_yaml(mv_text(ossie_text(mv)))["filter"] == value


def test_export_warns_when_a_column_ai_context_object_is_dropped():
    osi = """\
version: "0.2.0.dev0"
name: sales
datasets:
  - name: orders
    source: cat.sch.orders
    fields:
      - name: amount
        expression:
          dialects: [{dialect: DATABRICKS, expression: amount}]
        ai_context:
          instructions: do not use this column
          synonyms: [amt]
metrics:
  - name: total
    expression:
      dialects: [{dialect: DATABRICKS, expression: SUM(amount)}]
    ai_context:
      examples: [how much did we sell]
    custom_extensions:
      - vendor_name: SNOWFLAKE
        data: "{}"
"""
    notices = export_notices(osi)
    assert (
        "[field 'amount'] ai_context [instructions] dropped (only 'synonyms' has a "
        "Metric View counterpart)" in notices
    )
    assert (
        "[metric 'total'] ai_context [examples] dropped (only 'synonyms' has a Metric "
        "View counterpart)" in notices
    )
    assert "[metric 'total'] foreign-vendor custom_extensions dropped" in notices


def test_import_validates_a_join_source_the_way_the_export_does():
    mv = """\
version: '1.1'
source: cat.sch.orders
joins:
- name: d
  source: sch.d
  using: [k]
dimensions:
- {name: o_status, expr: o_orderstatus}
"""
    assert "3-part" in refused(metric_view_to_ossie, mv).message


def test_import_accepts_fields_as_alias_for_dimensions():
    mv = "version: '1.1'\nsource: c.s.orders\nfields:\n- {name: region, expr: region}\n"
    fields = import_mv(mv)["datasets"][0]["fields"]
    assert [f["name"] for f in fields] == ["region"]


def test_import_warns_and_uses_dimensions_when_both_dimensions_and_fields_set():
    mv = """\
version: '1.1'
source: c.s.orders
dimensions:
- {name: kept, expr: kept}
fields:
- {name: ignored, expr: ignored}
"""
    result = metric_view_to_ossie(mv)
    assert any(
        "both 'dimensions' and 'fields' are set" in n
        for n in result.converter_notices()
    )
    assert [f["name"] for f in result.value["datasets"][0]["fields"]] == ["kept"]


@pytest.mark.parametrize("src", ["a.b", ".s.t", "c..t", "c.s.", "cat . sch . tbl"])
def test_import_rejects_malformed_source(src):
    mv = f"version: '1.1'\nsource: {src}\n"
    assert "must be a 3-part" in refused(metric_view_to_ossie, mv).message


def test_import_accepts_a_with_subquery_source():
    mv = "version: '1.1'\nsource: WITH(t AS (SELECT 1 AS a)) SELECT a FROM t\n"
    assert import_mv(mv)["datasets"][0]["source"].startswith("WITH(")


@pytest.mark.parametrize("name", ["source", "SOURCE", "Source"])
def test_import_rejects_a_join_named_source(name):
    mv = (
        "version: '1.1'\nsource: c.s.fact\n"
        f"joins:\n- {{name: {name}, source: c.s.dim, using: [id]}}\n"
    )
    assert "reserved" in refused(metric_view_to_ossie, mv).message


def test_import_rejects_a_duplicate_join_name():
    mv = (
        "version: '1.1'\nsource: c.s.fact\n"
        "joins:\n- {name: fact, source: c.s.other, using: [id]}\n"
    )
    assert "Duplicate dataset/join name" in refused(metric_view_to_ossie, mv).message


def test_import_flips_one_to_many_join_and_stashes_the_grain():
    mv = (
        "version: '1.1'\nsource: c.s.orders\njoins:\n"
        "- {name: line_items, source: c.s.line_items, "
        "on: 'source.order_id = line_items.l_order_id', cardinality: one_to_many}\n"
        "measures:\n- {name: order_count, expr: COUNT(*)}\n"
    )
    ossie = ossie_text(mv)
    rel = parse_yaml(ossie)["relationships"][0]
    assert (rel["from"], rel["to"]) == ("line_items", "orders")
    assert (rel["from_columns"], rel["to_columns"]) == (["l_order_id"], ["order_id"])
    assert parse_yaml(mv_text(ossie))["joins"][0]["cardinality"] == "one_to_many"


def test_import_recovers_unique_key_from_at_most_one_match():
    mv = (
        "version: '1.1'\nsource: c.s.orders\njoins:\n"
        "- {name: customer, source: c.s.customer, on: 'source.cid = customer.id', "
        "rely: {at_most_one_match: true}}\n"
        "dimensions:\n- {name: cn, expr: customer.name}\n"
    )
    customer = next(d for d in import_mv(mv)["datasets"] if d["name"] == "customer")
    assert customer["unique_keys"] == [["id"]]


def test_import_rejects_non_string_scalars_where_strings_required():
    bad_join_name = "version: '1.1'\nsource: c.s.f\njoins:\n- {name: 5, source: c.s.d, using: [id]}\n"
    message = refused(metric_view_to_ossie, bad_join_name).message
    assert message == "join: 'name' must be a string, got Integer"
    bad_expr = "version: '1.1'\nsource: c.s.o\nmeasures:\n- {name: rev, expr: 5}\n"
    assert "must be a string" in refused(metric_view_to_ossie, bad_expr).message


# --- import: expression dialects -------------------------------------------------------


def dialect_of(column: dict[str, Any]) -> tuple[str, str]:
    (d,) = column["expression"]["dialects"]
    return d["dialect"], d["expression"]


def dialects_by_name(mv: str) -> dict[str, str]:
    model = import_mv(mv)
    columns = [f for ds in model["datasets"] for f in ds.get("fields", [])]
    columns += model.get("metrics", [])
    return {c["name"]: dialect_of(c)[0] for c in columns}


def test_import_labels_each_expression_with_its_most_portable_dialect():
    mv = """\
version: '1.1'
source: c.s.orders
dimensions:
- {name: status, expr: o_status}
- {name: name_length, expr: CHAR_LENGTH(o_name)}
- {name: order_month, expr: "date_format(o_date, 'yyyy-MM')"}
measures:
- {name: revenue, expr: SUM(o_amount)}
"""
    model = import_mv(mv)
    fields = model["datasets"][0]["fields"]
    assert dialect_of(fields[0]) == ("OSSIE_SQL_2026", "o_status")
    assert dialect_of(fields[1]) == ("ANSI_SQL", "CHAR_LENGTH(o_name)")
    assert dialect_of(fields[2]) == ("DATABRICKS", "date_format(o_date, 'yyyy-MM')")
    assert dialect_of(model["metrics"][0]) == ("OSSIE_SQL_2026", "SUM(o_amount)")


def test_import_keeps_window_and_partition_measures_databricks():
    mv = """\
version: '1.1'
source: c.s.orders
dimensions:
- {name: order_date, expr: o_date}
measures:
- {name: revenue, expr: SUM(o_amount)}
- name: running_revenue
  expr: SUM(o_amount)
  window:
  - {order: order_date, range: cumulative, semiadditive: last}
- name: daily_revenue
  expr: SUM(o_amount)
  partition: {include: [order_date], outer_aggregate: SUM}
"""
    assert dialects_by_name(mv) == {
        "order_date": "OSSIE_SQL_2026",
        "revenue": "OSSIE_SQL_2026",
        "running_revenue": "DATABRICKS",
        "daily_revenue": "DATABRICKS",
    }


def test_import_keeps_every_measure_of_a_filtered_view_databricks():
    mv = """\
version: '1.1'
source: c.s.orders
filter: o_status = 'done'
dimensions:
- {name: status, expr: o_status}
measures:
- {name: revenue, expr: SUM(o_amount)}
"""
    assert dialects_by_name(mv) == {"status": "OSSIE_SQL_2026", "revenue": "DATABRICKS"}


def test_import_keeps_names_defined_by_the_view_databricks():
    mv = """\
version: '1.1'
source: c.s.orders
parameters:
- {name: min_amount, data_type: DOUBLE}
joins:
- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}
dimensions:
- {name: status, expr: UPPER(status)}
- {name: status_code, expr: 'LEFT(status, 1)'}
- {name: source_status, expr: UPPER(source.o_status)}
- {name: city, expr: address.city}
- {name: customer_name, expr: UPPER(customer.c_name)}
measures:
- {name: big_orders, expr: 'COUNT(IF(o_amount > min_amount, 1, NULL))'}
- {name: revenue, expr: SUM(o_amount)}
"""
    assert dialects_by_name(mv) == {
        "status": "OSSIE_SQL_2026",
        "status_code": "DATABRICKS",
        "source_status": "DATABRICKS",
        "city": "DATABRICKS",
        "customer_name": "OSSIE_SQL_2026",
        "big_orders": "DATABRICKS",
        "revenue": "OSSIE_SQL_2026",
    }


def test_import_keeps_measure_references_and_shadowed_qualified_columns_databricks():
    mv = """\
version: '1.1'
source: c.s.orders
joins:
- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}
dimensions:
- {name: order_date, expr: o_date}
- {name: o_amount, expr: o_amount * 100}
- {name: c_acctbal, expr: customer.c_acctbal * 2}
measures:
- {name: cost, expr: SUM(cost)}
- {name: row_count, expr: COUNT(*)}
- {name: avg_cost, expr: cost / row_count}
- name: running_cost
  expr: SUM(cost)
  window:
  - {order: order_date, range: cumulative, semiadditive: last}
- {name: running_x2, expr: running_cost * 2}
- {name: raw_amount, expr: SUM(source.o_amount)}
- {name: raw_balance, expr: SUM(customer.c_acctbal)}
"""
    assert dialects_by_name(mv) == {
        "order_date": "OSSIE_SQL_2026",
        "o_amount": "OSSIE_SQL_2026",
        "c_acctbal": "OSSIE_SQL_2026",
        "cost": "OSSIE_SQL_2026",
        "row_count": "OSSIE_SQL_2026",
        "avg_cost": "DATABRICKS",
        "running_cost": "DATABRICKS",
        "running_x2": "DATABRICKS",
        "raw_amount": "DATABRICKS",
        "raw_balance": "DATABRICKS",
    }


def test_import_keeps_views_with_unseen_dimension_names_databricks():
    head = """\
version: '1.1'
source: c.s.orders
joins:
- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}
dimensions:
- {name: status, expr: o_status}
"""
    measures = "measures:\n- {name: commenters, expr: COUNT(DISTINCT comment)}\n"
    hidden = {"status": "DATABRICKS", "commenters": "DATABRICKS"}
    assert dialects_by_name(head + "- expr: customer.*\n" + measures) == hidden
    assert dialects_by_name(head + "- expr: customer.comment\n" + measures) == hidden
    assert dialects_by_name(head + "- expr: source.*\n" + measures) == {
        "status": "OSSIE_SQL_2026",
        "commenters": "OSSIE_SQL_2026",
    }


def hello_world_model(dialect: str) -> str:
    return f"""\
version: 0.2.0.dev0
name: hello_world
datasets:
- name: sales
  source: hello.public.sales
  fields:
  - {{name: amount, expression: {{dialects: [{{dialect: {dialect}, expression: amount}}]}}}}
metrics:
- {{name: total_amount, expression: {{dialects: [{{dialect: {dialect}, expression: SUM(sales.amount)}}]}}}}
"""


@pytest.mark.parametrize("dialect", ["OSSIE_SQL_2026", "ANSI_SQL"])
def test_portable_dialect_survives_ossie_to_metric_view_to_ossie(dialect):
    back = import_mv(mv_text(hello_world_model(dialect)))
    amount = back["datasets"][0]["fields"][0]
    total = back["metrics"][0]
    assert dialect_of(amount)[0] == "OSSIE_SQL_2026"
    assert dialect_of(total)[0] == "OSSIE_SQL_2026"


# --- hardening: strict schema and stash parsing ------------------------------------------


def test_bare_null_optional_collections_convert_like_absent():
    osi = f"""\
version: '{OSSIE_VERSION}'
name: m
datasets:
- name: d
  source: c.s.t
  fields:
  - name: region
    expression: {{dialects: [{{dialect: DATABRICKS, expression: region}}]}}
relationships:
metrics:
- name: cnt
  expression: {{dialects: [{{dialect: DATABRICKS, expression: 'count(*)'}}]}}
"""
    assert "cnt" in mv_text(osi)


def test_invalid_ossie_input_has_structured_failure_details():
    assert_invalid_ossie_input("just a scalar", "it is not a mapping at the root")


_DATASET = "  datasets:\n  - name: d\n    source: c.s.t\n"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            f"version: '{OSSIE_VERSION}'\nsemantic_model:\n- name: m\n",
            "Legacy 'semantic_model' wrappers are not supported; place the model "
            "properties directly at the document root",
        ),
        (
            f"version: '{OSSIE_VERSION}'\ndatasets: []\n",
            "Apache Ossie model requires a string 'name' at the document root",
        ),
        (
            ossie_model_with_body("  datasets: nope\n"),
            "Model 'm': 'datasets' must be a list",
        ),
        (
            ossie_model_with_body("  datasets:\n  - nope\n"),
            "Model 'm': 'datasets[0]' must be a mapping",
        ),
        (
            ossie_model_with_body(_DATASET + "  relationships: nope\n"),
            "Model 'm': 'relationships' must be a list",
        ),
        (
            ossie_model_with_body(_DATASET + "  relationships:\n  - nope\n"),
            "Model 'm': 'relationships[0]' must be a mapping",
        ),
        (
            ossie_model_with_body(_DATASET + "    fields: nope\n"),
            "Dataset 'd': 'fields' must be a list",
        ),
        (
            ossie_model_with_body(_DATASET + "    fields:\n    - nope\n"),
            "Dataset 'd': 'fields[0]' must be a mapping",
        ),
        (
            ossie_model_with_body(_DATASET + "  metrics: nope\n"),
            "Model 'm': 'metrics' must be a list",
        ),
        (
            ossie_model_with_body(_DATASET + "  metrics:\n  - nope\n"),
            "Model 'm': 'metrics[0]' must be a mapping",
        ),
    ],
)
def test_malformed_schema_collections_are_rejected(text, reason):
    assert_invalid_ossie_input(text, reason)


_FIELD_PREFIX = _DATASET + "    fields:\n    - name: id\n"
_VALID_FIELD = (
    _FIELD_PREFIX + "      expression:\n        dialects:\n"
    "        - {dialect: DATABRICKS, expression: id}\n"
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            ossie_model_with_body(_FIELD_PREFIX + "      expression: nope\n"),
            "field 'id': 'expression' must be a mapping",
        ),
        (
            ossie_model_with_body(
                _FIELD_PREFIX + "      expression: {dialects: nope}\n"
            ),
            "field 'id': 'expression.dialects' must be a list",
        ),
        (
            ossie_model_with_body(
                _FIELD_PREFIX + "      expression: {dialects: [nope]}\n"
            ),
            "field 'id': 'expression.dialects[0]' must be a mapping",
        ),
        (
            ossie_model_with_body(
                _VALID_FIELD + "  metrics:\n  - name: count_id\n"
                "    expression: {dialects: nope}\n"
            ),
            "metric 'count_id': 'expression.dialects' must be a list",
        ),
    ],
)
def test_malformed_expression_collections_are_rejected(text, reason):
    assert_invalid_ossie_input(text, reason)


_VALID_BODY = (
    "  datasets:\n  - name: d\n    source: c.s.t\n    fields:\n    - name: id\n"
    "      expression:\n        dialects:\n        - {dialect: DATABRICKS, expression: id}\n"
)


@pytest.mark.parametrize(
    ("extensions", "reason"),
    [
        (
            "  custom_extensions: {vendor_name: DATABRICKS, data: '{}'}\n",
            "'custom_extensions' must be a list",
        ),
        ("  custom_extensions: [nope]\n", "'custom_extensions[0]' must be a mapping"),
        (
            "  custom_extensions:\n  - {vendor_name: DATABRICKS, data: 123}\n",
            "DATABRICKS custom_extensions data must be a string",
        ),
        (
            "  custom_extensions:\n  - {vendor_name: DATABRICKS, data: '[]'}\n",
            "DATABRICKS custom_extensions data must be a JSON object",
        ),
    ],
)
def test_malformed_custom_extensions_are_rejected(extensions, reason):
    assert_invalid_ossie_input(ossie_model_with_body(extensions + _VALID_BODY), reason)


@pytest.mark.parametrize(
    "data", ["filter: x = 1", '{"filter": "x", "filter": "y"}', '{"filter": "x"} true']
)
def test_malformed_stash_json_is_rejected(data):
    text = ossie_model_with_body(
        f"  custom_extensions:\n  - vendor_name: DATABRICKS\n    data: '{data}'\n"
        + _VALID_BODY
    )
    error = refused(ossie_to_metric_view, text)
    assert error.kind is Kind.INVALID_INPUT
    assert error.reason.startswith(
        "DATABRICKS custom_extensions data is not valid JSON:"
    )


def test_duplicate_databricks_stash_keeps_first_and_warns():
    osi = ossie_model_with_body(
        "  custom_extensions:\n"
        '  - {vendor_name: DATABRICKS, data: \'{"filter": "a = 1"}\'}\n'
        '  - {vendor_name: DATABRICKS, data: \'{"filter": "b = 2"}\'}\n'
        "  datasets:\n  - name: d\n    source: c.s.t\n    fields:\n"
        "    - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
    )
    result = ossie_to_metric_view(osi)
    assert result.value["filter"] == "a = 1"
    assert (
        "[Model 'm'] found more than one DATABRICKS custom_extensions entry; using the "
        "first and ignoring the rest" in result.converter_notices()
    )


def test_oversized_dataset_graph_is_rejected_before_recursive_traversal():
    body = "  datasets:\n" + "".join(
        f"  - {{name: d{i}, source: c.s.t{i}}}\n" for i in range(MAX_JOIN_NODES + 1)
    )
    body += "  relationships:\n" + "".join(
        f"  - {{name: r{i}, from: d{i}, to: d{i + 1}, from_columns: [id], to_columns: [id]}}\n"
        for i in range(MAX_JOIN_NODES)
    )
    message = refused(ossie_to_metric_view, ossie_model_with_body(body)).message
    assert "at most 200 are supported" in message


@pytest.mark.parametrize(
    "text",
    [
        f"version: '{OSSIE_VERSION}'\nversion: '{OSSIE_VERSION}'\nname: m\n",
        ossie_model_with_body(
            "  datasets:\n  - name: d\n    source: c.s.first\n    source: c.s.second\n"
        ),
    ],
)
def test_duplicate_yaml_keys_are_rejected_at_every_depth(text):
    error = refused(ossie_to_metric_view, text)
    assert error.kind is Kind.INVALID_INPUT
    assert "duplicate key" in error.reason


def test_reverse_ordered_drop_chain_is_propagated_without_repeated_full_scans():
    chain_length = 400
    body = "  datasets:\n  - name: d\n    source: c.s.d\n    fields:\n"
    for index in range(chain_length, 0, -1):
        body += (
            f"    - name: d{index}\n      expression:\n        dialects:\n"
            f"        - {{dialect: DATABRICKS, expression: d{index - 1}}}\n"
        )
    body += (
        "    - name: d0\n      expression:\n        dialects:\n"
        "        - {dialect: SNOWFLAKE, expression: d0}\n"
        "  metrics:\n  - name: row_count\n    expression:\n      dialects:\n"
        "      - {dialect: DATABRICKS, expression: count(*)}\n"
    )
    result = ossie_to_metric_view(ossie_model_with_body(body))
    assert "dimensions" not in result.value
    assert len(result.value["measures"]) == 1
    notices = result.converter_notices()
    assert len(notices) == chain_length + 1
    cascade = [n for n in notices if "downstream of a dropped field/metric" in n]
    assert cascade == [
        cascade_notice("dimension", f"d{i}", f"d{i - 1}")
        for i in range(1, chain_length + 1)
    ]


def test_cascade_drop_preserves_dimension_then_measure_phase_order():
    osi = """\
version: 0.2.0.dev0
name: m
datasets:
- name: d
  source: c.s.d
  fields:
    - {name: d1, expression: {dialects: [{dialect: DATABRICKS, expression: bad_dim}]}}
    - {name: d0, expression: {dialects: [{dialect: DATABRICKS, expression: 'measure(m1)'}]}}
    - {name: bad_dim, expression: {dialects: [{dialect: SNOWFLAKE, expression: bad_dim}]}}
metrics:
- {name: m1, expression: {dialects: [{dialect: DATABRICKS, expression: 'measure(bad_measure)'}]}}
- {name: m0, expression: {dialects: [{dialect: DATABRICKS, expression: d0}]}}
- {name: bad_measure, expression: {dialects: [{dialect: SNOWFLAKE, expression: bad_measure}]}}
- {name: keep, expression: {dialects: [{dialect: DATABRICKS, expression: 'count(*)'}]}}
"""
    cascade = [
        n for n in export_notices(osi) if "downstream of a dropped field/metric" in n
    ]
    assert cascade == [
        cascade_notice("dimension", "d1", "bad_dim"),
        cascade_notice("measure", "m1", "bad_measure"),
        cascade_notice("dimension", "d0", "m1"),
        cascade_notice("measure", "m0", "d0"),
    ]


def test_invalid_metric_view_input_has_structured_failure_details():
    scalar = refused(metric_view_to_ossie, "just a scalar")
    assert (scalar.kind, scalar.reason) == (
        Kind.INVALID_INPUT,
        "it is not a mapping at the root",
    )
    parse = refused(metric_view_to_ossie, "version: [1")
    assert parse.kind is Kind.INVALID_INPUT
    assert parse.reason.startswith("failed to parse YAML:")
    empty = refused(metric_view_to_ossie, "   ")
    assert (empty.kind, empty.reason) == (Kind.INVALID_INPUT, "the input is empty")
    missing = refused(metric_view_to_ossie, "source: c.s.t")
    assert missing.kind is Kind.INVALID_INPUT
    assert missing.reason == "it is missing the required 'version' field"


def test_public_yaml_helpers_have_structured_failure_kinds():
    error = refused(parse_yaml, "version: [1")
    assert error.kind is Kind.INVALID_INPUT
    assert error.message == error.reason
    assert error.__cause__ is not None


# `reverseDirectionSerializationFailuresAreInternalErrors` is not ported: it makes
# Jackson fail on a Java bean, which a parsed YAML value can never be. The port
# writes plain dicts, lists and scalars, which always serialize.
