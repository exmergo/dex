"""What the derivation hook builds a declaration from, before a vendor reads it (#927).

A vendor's derivation reads only its request, so the facts a declaration does not
state (a column's warehouse type, the parent a foreign key joins) are gathered
here, from what is free: the SQL, the stored cache and the compiled manifest.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache
from exmergo_dex_core.transform.semantic_hook import column_data_types, declaration

pytest.importorskip("sqlglot")

SOURCE = Dataset(
    identifier="warehouse.main.customers",
    columns=[
        ColumnProfile(name="id", data_type="INTEGER"),
        ColumnProfile(name="country_code", data_type="VARCHAR"),
    ],
)
CACHE = DexCache(datasets=[SOURCE])

MODELS = {
    "stg_customers": "select id as customer_id, country_code from customers\n",
}

MART_SQL = (
    "select customer_id, country_code, cast(signed_up as date) as signed_up_on,\n"
    "  date_trunc('month', signed_up) as signup_month\n"
    "from {{ ref('stg_customers') }}\n"
)


def test_a_type_is_followed_through_the_projects_own_unprofiled_models():
    found = column_data_types(
        MART_SQL,
        CACHE,
        ["customer_id", "country_code", "signed_up_on", "signup_month"],
        relation=None,
        dialect="duckdb",
        vars={},
        model_sql=MODELS.get,
    )

    # Through stg_customers, which nobody profiled, to the profiled source.
    assert found["customer_id"] == "INTEGER"
    assert found["country_code"] == "VARCHAR"
    # Stated by the mart's own SELECT list.
    assert found["signed_up_on"].upper() == "DATE"
    # DuckDB's date_trunc reads as a timestamp truncation, as the MetricFlow
    # derivation reads it too.
    assert found["signup_month"] == "TIMESTAMP"


def test_without_the_upstream_sql_a_type_is_absent_rather_than_guessed():
    found = column_data_types(
        MART_SQL,
        CACHE,
        ["customer_id"],
        relation=None,
        dialect="duckdb",
        vars={},
        model_sql=None,
    )

    assert "customer_id" not in found


def test_a_foreign_key_resolves_to_its_parents_compiled_relation():
    built = declaration(
        "fct_orders",
        {
            "grain": ["order_id"],
            "columns": {
                "order_id": {"role": "key"},
                "customer_id": {
                    "role": "foreign_key",
                    "references": "dim_customers.customer_id",
                },
            },
        },
        sql="select order_id, customer_id from {{ ref('stg_orders') }}\n",
        sql_path="models/marts/fct_orders.sql",
        cache=None,
        dialect="duckdb",
        vars={},
        relations={
            "fct_orders": ("db.marts.fct_orders", "fct_orders"),
            "dim_customers": ("db.marts.customers", "customers"),
        },
    )

    assert built.relation == "db.marts.fct_orders"
    (reference,) = built.references
    assert reference.parent_model == "dim_customers"
    assert reference.parent_column == "customer_id"
    # The alias, which is what a layer that names tables matches on.
    assert reference.parent_identifier == "customers"
    assert reference.parent_relation == "db.marts.customers"
