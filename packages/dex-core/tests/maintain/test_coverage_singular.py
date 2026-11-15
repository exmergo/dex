"""`maintain coverage`'s `referenced` tier: what singular tests read (#1282)."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.maintain import coverage as coverage_mod
from exmergo_dex_core.maintain.coverage_singular import singular_reads
from exmergo_dex_core.transform.drafting import read

PROJECT = "shop"
ORDERS = f"model.{PROJECT}.orders"
CUSTOMERS = f"model.{PROJECT}.customers"
RAW = f"source.{PROJECT}.raw.payments"


def _model(name: str, *columns: str) -> dict:
    return {
        "resource_type": "model",
        "name": name,
        "package_name": PROJECT,
        "original_file_path": f"models/{name}.sql",
        "raw_code": "select 1",
        "language": "sql",
        "relation_name": f'"dev"."main"."{name}"',
        "config": {"materialized": "table", "enabled": True},
        "columns": {c: {"name": c} for c in columns},
        "depends_on": {"nodes": []},
    }


def _test(name: str, sql: str, *reads: str, checksum: str = "a") -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "package_name": PROJECT,
        "raw_code": sql,
        "checksum": {"name": "sha256", "checksum": checksum},
        "config": {"severity": "ERROR", "enabled": True},
        "depends_on": {"nodes": list(reads)},
    }


def _manifest(**tests: dict) -> dict:
    return {
        "metadata": {"project_name": PROJECT},
        "nodes": {
            ORDERS: _model("orders", "id", "amount", "cid", "status"),
            CUSTOMERS: _model("customers", "id", "region"),
            "test.shop.not_null_orders_id": {
                "resource_type": "test",
                "name": "not_null_orders_id",
                "package_name": PROJECT,
                "attached_node": ORDERS,
                "column_name": "id",
                "test_metadata": {"name": "not_null", "kwargs": {}},
                "config": {"severity": "ERROR", "enabled": True},
                "depends_on": {"nodes": [ORDERS]},
            },
            **{f"test.shop.{k}": v for k, v in tests.items()},
        },
        "sources": {
            RAW: {
                "resource_type": "source",
                "name": "payments",
                "source_name": "raw",
                "identifier": "payments",
                "package_name": PROJECT,
                "relation_name": '"dev"."raw"."payments"',
                "config": {"enabled": True},
                "columns": {"amount": {"name": "amount"}},
            }
        },
        "unit_tests": {},
        "disabled": {},
    }


def _roles(reads, uid):
    return {column: sorted(entry["roles"]) for column, entry in reads[uid].items()}


def test_a_filter_is_a_predicate_read():
    manifest = _manifest(
        negative=_test(
            "negative", "select * from {{ ref('orders') }} where amount < 0", ORDERS
        )
    )
    reads, block = singular_reads(manifest, dialect="duckdb")
    assert _roles(reads, ORDERS) == {"amount": ["predicate"]}
    assert block["read"] == {"resolved": 1, "partial": 0, "unresolved": 0}
    assert block["read_from"]["raw_code"] == 1


def test_a_join_key_reads_as_a_join_not_a_predicate_and_lands_on_both_models():
    sql = (
        "select o.id from {{ ref('orders') }} o join {{ ref('customers') }} c "
        "on o.cid = c.id where c.region is null"
    )
    reads, _ = singular_reads(
        _manifest(orphans=_test("orphans", sql, ORDERS, CUSTOMERS)), dialect="duckdb"
    )
    assert _roles(reads, ORDERS) == {"cid": ["join"], "id": ["projection"]}
    assert _roles(reads, CUSTOMERS) == {"id": ["join"], "region": ["predicate"]}


def test_an_aggregate_through_a_cte_and_a_union_of_checks():
    sql = (
        "with s as (select * from {{ ref('orders') }})\n"
        "select 'bad' as problem from s where not (status = 'x' and amount > 0)\n"
        "union all\n"
        "select 'count' from (select sum(amount) as total from s) t where total < 0"
    )
    reads, block = singular_reads(
        _manifest(checks=_test("checks", sql, ORDERS)), dialect="duckdb"
    )
    roles = _roles(reads, ORDERS)
    assert roles["status"] == ["predicate"]
    assert "aggregation" in roles["amount"] and "predicate" in roles["amount"]
    assert block["read"]["resolved"] == 1


def test_a_source_resolves_through_the_tests_dependencies():
    reads, _ = singular_reads(
        _manifest(
            paid=_test(
                "paid",
                "select 1 from {{ source('raw', 'payments') }} where amount < 0",
                RAW,
            )
        ),
        dialect="duckdb",
    )
    assert _roles(reads, RAW) == {"amount": ["predicate"]}


def test_sql_that_does_not_parse_is_unresolved_with_a_code():
    reads, block = singular_reads(
        _manifest(broken=_test("broken", "select from from where", ORDERS)),
        dialect="duckdb",
    )
    assert reads == {}
    assert block["unresolved"] == [{"test": "broken", "reason_code": "unparseable"}]


def test_a_macro_generated_test_is_unresolved_until_a_matching_compile_exists():
    test = _test("by_macro", "{{ assert_positive(ref('orders'), 'amount') }}", ORDERS)
    manifest = _manifest(by_macro=test)
    reads, block = singular_reads(manifest, dialect="duckdb")
    assert block["unresolved"] == [
        {"test": "by_macro", "reason_code": "macro_generated"}
    ]

    compiled = {
        "nodes": {
            "test.shop.by_macro": {
                **test,
                "compiled_code": (
                    'select * from "dev"."main"."orders" where amount <= 0'
                ),
            }
        }
    }
    reads, block = singular_reads(manifest, artifact=compiled, dialect="duckdb")
    assert _roles(reads, ORDERS) == {"amount": ["predicate"]}
    assert block["read_from"] == {"raw_code": 0, "compiled_artifact": 1}

    # A compile of an older version of the test is not this test's SQL.
    compiled["nodes"]["test.shop.by_macro"]["checksum"] = {"checksum": "older"}
    reads, block = singular_reads(manifest, artifact=compiled, dialect="duckdb")
    assert reads == {}
    assert block["read"]["unresolved"] == 1


def test_an_ambiguous_name_is_partial_never_guessed():
    other = {**_model("orders", "amount"), "package_name": "other"}
    manifest = _manifest(
        dup=_test(
            "dup",
            "select * from {{ ref('orders') }} where amount < 0",
            ORDERS,
            "model.other.orders",
        )
    )
    manifest["nodes"]["model.other.orders"] = other
    reads, block = singular_reads(manifest, dialect="duckdb")
    assert reads == {}
    assert block["partial"] == [{"test": "dup", "reason_code": "ambiguous_relation"}]


def test_referenced_never_counts_toward_asserted():
    manifest = _manifest(
        negative=_test(
            "negative",
            "select * from {{ ref('orders') }} where amount < 0 and id is null",
            ORDERS,
        )
    )
    reads, _ = singular_reads(manifest, dialect="duckdb")
    found = coverage_mod.inventory(manifest, referenced=reads, full=True)
    record = next(r for r in found.sections["models"] if r["name"] == "orders")
    columns = record["columns"]
    assert columns["asserted"] == 1  # only the not_null test's column
    assert columns["referenced"] == 2
    assert columns["referenced_only"] == 1
    listed = {e["column"]: e for e in columns["referenced_columns"]}
    assert listed["amount"]["asserted"] is False
    assert listed["amount"]["tests"] == ["negative"]
    assert listed["id"]["asserted"] is True
    # The referenced-only column sorts first.
    assert columns["referenced_columns"][0]["column"] == "amount"

    models = found.summary["models"]
    assert models["columns_asserted"]["numerator"] == 1
    assert models["columns_referenced_only"]["numerator"] == 1
    assert models["columns_referenced_only"]["denominator"] == 6


def test_without_reads_no_record_lists_referenced_columns():
    found = coverage_mod.inventory(_manifest())
    record = found.sections["models"][0]
    assert "referenced_columns" not in record["columns"]
    assert "columns_referenced_only" not in found.summary["models"]


def test_the_reader_off_by_default_records_no_references():
    reading = read(
        "select a from {{ ref('x') }} where b > 0", None, dialect="duckdb", vars=None
    )
    assert reading.referenced == {}


def test_using_keys_are_join_reads_on_both_sides():
    sql = (
        "select o.amount from {{ ref('orders') }} o join {{ ref('customers') }} c "
        "using (id) where o.amount < 0"
    )
    reads, _ = singular_reads(
        _manifest(using=_test("using", sql, ORDERS, CUSTOMERS)), dialect="duckdb"
    )
    assert _roles(reads, ORDERS)["id"] == ["join"]
    assert _roles(reads, CUSTOMERS) == {"id": ["join"]}


def test_an_alias_read_in_having_is_the_expression_behind_it():
    sql = (
        "select status, sum(amount) as total from {{ ref('orders') }} "
        "group by status having total < 0"
    )
    reads, block = singular_reads(
        _manifest(alias=_test("alias", sql, ORDERS)), dialect="duckdb"
    )
    roles = _roles(reads, ORDERS)
    assert "total" not in roles
    assert roles["amount"] == ["aggregation", "predicate"]
    assert block["read"]["resolved"] == 1


def test_a_using_key_a_cte_computes_traces_to_its_column():
    reading = read(
        "with o as (select lower(code) as k, amount from {{ ref('orders') }}) "
        "select o.amount from o join {{ ref('customers') }} c using (k)",
        None,
        dialect="duckdb",
        vars=None,
        references=True,
    )
    keys = {f"{r}.{c}": sorted(v) for (r, c, _), v in reading.referenced.items()}
    assert "join" in keys["orders.code"]
    assert keys["customers.k"] == ["join"]


def test_a_using_key_the_left_side_cannot_place_is_untraced_not_spread():
    reading = read(
        "select a.x from {{ ref('a') }} a join {{ ref('b') }} b on a.id = b.id "
        "join {{ ref('c') }} c using (k)",
        None,
        dialect="duckdb",
        vars=None,
        references=True,
    )
    keys = {f"{r}.{c}" for (r, c, _), _v in reading.referenced.items()}
    assert "c.k" in keys
    assert "a.k" not in keys and "b.k" not in keys
    assert "k" in reading.unreferenced


def test_a_using_key_between_unaliased_refs_reaches_both_sides():
    reading = read(
        "select orders.amount from {{ ref('orders') }} "
        "join {{ ref('customers') }} using (id) where orders.amount < 0",
        None,
        dialect="duckdb",
        vars=None,
        references=True,
    )
    keys = {f"{r}.{c}": sorted(v) for (r, c, _), v in reading.referenced.items()}
    assert keys["orders.id"] == ["join"]
    assert keys["customers.id"] == ["join"]
    assert "id" not in reading.unreferenced
