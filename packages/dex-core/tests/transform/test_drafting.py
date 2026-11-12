"""`transform plan` drafts the measured half of each model's declaration (#493).

The unit half drives :func:`drafting.draft` over a hand-built cache, one case
per rule, because each rule is a claim about what the cache can and cannot
support and a hand-built cache states the premise exactly. The end-to-end half
runs the shipped commands over a real DuckDB warehouse, so the cache it drafts
from is the one `explore` writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
    Relationship,
    RelationshipKind,
    ValueCount,
    ValueDomain,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.cumulative import cumulative_columns
from exmergo_dex_core.transform.drafting import draft

pytest.importorskip("sqlglot")


def _key(name: str, data_type: str = "integer") -> ColumnProfile:
    return ColumnProfile(
        name=name,
        data_type=data_type,
        null_fraction=0.0,
        is_unique=True,
        distinct_count_exact=True,
    )


def _col(name: str, data_type: str = "varchar", **fields) -> ColumnProfile:
    return ColumnProfile(name=name, data_type=data_type, **fields)


ORDERS = Dataset(
    identifier="warehouse.main.stg_orders",
    columns=[
        _key("order_id"),
        _col("customer_id", "integer", null_fraction=0.0),
        _col(
            "status",
            null_fraction=0.0,
            value_domain=ValueDomain(
                values=[
                    ValueCount(value="shipped", count=90),
                    ValueCount(value="cancelled", count=10),
                ]
            ),
        ),
        _col(
            "email",
            null_fraction=0.1,
            pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
        ),
        _col(
            "discount_rate", "double", null_fraction=0.0, min_value=0.0, max_value=0.4
        ),
        _col("margin_pct", "double", null_fraction=0.0, min_value=2.0, max_value=61.5),
        _col("is_gift", "integer", null_fraction=0.0, min_value=0, max_value=1),
        _col("amount", "double", null_fraction=0.0, min_value=1.0, max_value=900.0),
    ],
)
CUSTOMERS = Dataset(
    identifier="warehouse.main.customers",
    columns=[_key("id"), _col("region", null_fraction=0.0)],
)
ITEMS = Dataset(
    identifier="warehouse.main.stg_items",
    columns=[_key("item_id"), _col("order_id", "integer", null_fraction=0.0)],
)
BALANCES = Dataset(
    identifier="warehouse.main.stg_balances",
    columns=[
        _col("account_id", "integer", null_fraction=0.0),
        _col("as_of", "date", null_fraction=0.0),
        _col("balance", "double", null_fraction=0.0),
    ],
    composite_keys=[["account_id", "as_of"]],
    data_quality=[
        "balance looks cumulative or a point-in-time snapshot: over 40 "
        "consecutive observations within account_id, ordered by as_of, only "
        "0.0% decreased; summing it across rows multiplies the true value"
    ],
)

CACHE = DexCache(
    datasets=[ORDERS, CUSTOMERS, ITEMS, BALANCES],
    relationships=[
        Relationship(
            from_dataset=ORDERS.identifier,
            from_columns=["customer_id"],
            to_dataset=CUSTOMERS.identifier,
            to_columns=["id"],
            kind=RelationshipKind.INFERRED,
            verified=True,
            orphan_fraction=0.0,
        )
    ],
)

# The shape from #1071: product_id holds one NULL, so the key walk cannot prove
# it, while the PII-flagged email is unique and would draft as the grain.
DIM_PRODUCTS = Dataset(
    identifier="warehouse.main.dim_products",
    columns=[
        _col(
            "product_id",
            "integer",
            null_fraction=0.05,
            is_unique=True,
            distinct_count_exact=True,
        ),
        _col(
            "email",
            null_fraction=0.0,
            is_unique=True,
            distinct_count_exact=True,
            pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
        ),
    ],
)
DIM_USERS = Dataset(
    identifier="warehouse.main.dim_users",
    columns=[
        _col(
            "email",
            null_fraction=0.0,
            is_unique=True,
            distinct_count_exact=True,
            pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
        ),
        _key("user_id"),
    ],
)


def _draft(
    sql: str,
    *,
    yml: str | None = None,
    cache: DexCache | None = CACHE,
    manifest: Mapping[str, Any] | None = None,
    manifest_stale: bool = False,
):
    files = {"models/marts/fct.sql": sql}
    if yml is not None:
        files["models/marts/_marts.yml"] = yml
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        cache,
        dialect="duckdb",
        cumulative={
            d.identifier: cumulative_columns(d.data_quality)
            for d in (cache.datasets if cache else [])
        },
        domain_cap=25,
        manifest=manifest,
        manifest_stale=manifest_stale,
    )
    return drafts["fct"], warnings


# --- grain ------------------------------------------------------------------------


def test_a_group_by_drafts_the_grouping_keys_as_the_grain():
    entry, _ = _draft(
        "select customer_id, status, count(*) as orders\n"
        "from {{ ref('stg_orders') }}\ngroup by 1, 2\n"
    )

    assert entry["drafted"]["grain"]["value"] == ["customer_id", "status"]
    assert "groups by" in entry["drafted"]["grain"]["evidence"]


def test_a_proven_key_carried_through_unchanged_is_the_drafted_grain():
    entry, _ = _draft("select order_id, customer_id from {{ ref('stg_orders') }}\n")

    grain = entry["drafted"]["grain"]
    assert grain["value"] == ["order_id"]
    assert "proven unique key of stg_orders" in grain["evidence"]
    assert entry["drafted"]["columns"]["order_id"]["role"]["value"] == "key"


def test_a_key_survives_a_many_to_one_join_and_its_right_side_turns_nullable():
    entry, _ = _draft(
        "select o.order_id, c.region\n"
        "from {{ ref('stg_orders') }} o\n"
        "left join {{ source('crm', 'customers') }} c on c.id = o.customer_id\n"
    )

    assert entry["drafted"]["grain"]["value"] == ["order_id"]
    # `customers.region` held no NULL when profiled, and the draft still says
    # nullable: an order with no customer carries NULL here.
    region = entry["drafted"]["columns"]["region"]
    assert region["nullable"]["value"] is True
    assert (
        "outer side of the left join to customers on c.id = o.customer_id"
        in region["nullable"]["evidence"]
    )
    assert "dex_relation" not in json.dumps(entry)


def test_a_join_that_can_fan_out_withholds_the_grain_and_says_why():
    entry, warnings = _draft(
        "select o.order_id, i.item_id\n"
        "from {{ ref('stg_orders') }} o\n"
        "join {{ ref('stg_items') }} i on i.order_id = o.order_id\n"
    )

    grain = entry["drafted"]["grain"]
    assert grain["value"] is None
    assert "can fan rows out" in grain["withheld"]
    assert "order_id" not in entry["drafted"]["columns"] or (
        "role" not in entry["drafted"]["columns"]["order_id"]
    )
    assert any(w == "'fct' declares no grain" for w in warnings)


def test_a_key_is_followed_through_a_chain_of_ctes_named_like_their_sources():
    entry, _ = _draft(
        "with stg_orders as (select * from {{ ref('stg_orders') }}),\n"
        "kept as (select order_id, amount from stg_orders where status = 'shipped')\n"
        "select order_id, amount as order_amount from kept\n"
    )

    assert entry["drafted"]["grain"]["value"] == ["order_id"]

    assert entry["drafted"]["grain"]["evidence"].count("carried through unchanged") == 1
    assert entry["drafted"]["columns"]["order_amount"]["from"] == "stg_orders.amount"


def test_drafted_grain_evidence_says_carried_through_unchanged_once():
    """CTE hops must not re-append the carry phrase (#987)."""
    entry, _ = _draft(
        "with source as (select * from {{ ref('stg_orders') }}),\n"
        "renamed as (select order_id, customer_id, status from source)\n"
        "select * from renamed\n"
    )
    evidence = entry["drafted"]["grain"]["evidence"]
    assert entry["drafted"]["grain"]["value"] == ["order_id"]
    assert evidence == (
        "order_id is a proven unique key of stg_orders, carried through unchanged"
    )
    assert evidence.count("carried through unchanged") == 1


def test_an_unaliased_relation_never_leaks_the_parse_placeholder():
    entry, warnings = _draft(
        "select stg_orders.status, count(stg_orders.order_id)\n"
        "from {{ ref('stg_orders') }} group by 1\n"
    )

    assert entry["drafted"]["grain"]["value"] == ["status"]
    assert "dex_relation" not in json.dumps([entry, warnings])
    assert "columns.count(stg_orders.order_id).role" in entry["missing"]
    assert entry["drafted"]["columns"]["status"]["from"] == "stg_orders.status"


def test_a_composite_proven_key_is_drafted_as_a_composite_grain():
    entry, _ = _draft(
        "select account_id, as_of, balance from {{ ref('stg_balances') }}\n"
    )

    assert entry["drafted"]["grain"]["value"] == ["account_id", "as_of"]


# --- column facts -------------------------------------------------------------------


def test_a_verified_join_edge_is_drafted_as_a_reference():
    entry, _ = _draft("select order_id, customer_id from {{ ref('stg_orders') }}\n")

    column = entry["drafted"]["columns"]["customer_id"]
    assert column["role"]["value"] == "foreign_key"
    assert column["references"]["value"] == "customers.id"
    # `customers` is a warehouse relation, not a model of this project, so a
    # `references` declaration would be refused; the draft says so.
    assert "not a model of this project" in column["references"]["evidence"]


def test_a_verified_join_to_a_package_model_is_drafted_naming_the_package():
    """A parent an installed package builds is a ``references`` dex can write
    (#1155), so the draft names the package rather than the caveat."""

    files = {
        "models/marts/fct.sql": (
            "select order_id, customer_id from {{ ref('stg_orders') }}\n"
        )
    }
    drafts, _ = draft(
        ["fct"],
        files,
        ["models"],
        CACHE,
        dialect="duckdb",
        cumulative={},
        domain_cap=25,
        packaged=lambda name: "crm" if name == "customers" else None,
    )

    column = drafts["fct"]["drafted"]["columns"]["customer_id"]
    assert column["references"]["value"] == "customers.id"
    evidence = column["references"]["evidence"]
    assert "defined by the installed package crm" in evidence
    assert "not a model of this project" not in evidence


def test_pii_and_null_facts_follow_the_column_lineage():
    entry, _ = _draft(
        "select order_id, email as contact from {{ ref('stg_orders') }}\n"
    )

    contact = entry["drafted"]["columns"]["contact"]
    assert contact["pii"]["value"] == {"category": "email", "confidence": 0.95}
    assert contact["nullable"]["value"] is True
    assert entry["drafted"]["columns"]["order_id"]["nullable"]["value"] is False


def test_a_flagged_running_total_is_drafted_semi_additive():
    entry, _ = _draft(
        "select account_id, as_of, balance from {{ ref('stg_balances') }}\n"
    )

    additivity = entry["drafted"]["columns"]["balance"]["additivity"]
    assert additivity["value"] == "semi_additive"
    assert "cumulative check" in additivity["evidence"]


def test_a_computed_running_total_window_is_drafted_semi_additive():
    entry, _ = _draft(
        "select order_id,\n"
        "    sum(amount) over (\n"
        "        partition by customer_id order by placed_at\n"
        "    ) as running_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )

    column = entry["drafted"]["columns"]["running_amount"]
    assert column["additivity"]["value"] == "semi_additive"
    assert (
        column["additivity"]["evidence"]
        == "a running sum over customer_id ordered by placed_at"
    )
    assert "from" not in column


def test_running_total_window_frame_variations():
    # Explicit ROWS frame
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id order by placed_at\n"
        "    rows between unbounded preceding and current row\n"
        ") as running_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_amount"]["additivity"]["evidence"]
        == "a running sum over customer_id ordered by placed_at"
    )

    # Explicit RANGE frame
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id order by placed_at\n"
        "    range between unbounded preceding and current row\n"
        ") as running_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_amount"]["additivity"]["evidence"]
        == "a running sum over customer_id ordered by placed_at"
    )

    # No partition by (running sum ordered by placed_at)
    entry, _ = _draft(
        "select order_id, sum(amount) over (order by placed_at) as running_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_amount"]["additivity"]["evidence"]
        == "a running sum ordered by placed_at"
    )

    # Running count with partition by
    entry, _ = _draft(
        "select order_id,\n"
        "    count(*) over (\n"
        "        partition by customer_id order by placed_at\n"
        "    ) as running_count\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_count"]["additivity"]["evidence"]
        == "a running count over customer_id ordered by placed_at"
    )

    # Running count without partition by
    entry, _ = _draft(
        "select order_id, count(order_id) over (order by placed_at) as running_count\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_count"]["additivity"]["evidence"]
        == "a running count ordered by placed_at"
    )

    # Multiple partition and order columns
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id, is_gift order by placed_at, order_id\n"
        ") as running_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert (
        entry["drafted"]["columns"]["running_amount"]["additivity"]["evidence"]
        == "a running sum over customer_id, is_gift ordered by placed_at, order_id"
    )

    # Computed in a CTE
    entry, _ = _draft(
        "with running as (\n"
        "    select customer_id,\n"
        "        sum(amount) over (\n"
        "            partition by customer_id order by placed_at\n"
        "        ) as running_amount\n"
        "    from {{ ref('stg_orders') }}\n"
        ")\n"
        "select customer_id, running_amount from running\n"
    )
    assert (
        entry["drafted"]["columns"]["running_amount"]["additivity"]["evidence"]
        == "a running sum over customer_id ordered by placed_at"
    )


def test_window_not_drafted_as_running_total():
    # Window with no order by (partition total)
    entry, _ = _draft(
        "select order_id,\n"
        "    sum(amount) over (partition by customer_id) as partition_total\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert "partition_total" not in entry["drafted"].get("columns", {})

    # Bounded rolling window (6 preceding)
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id order by placed_at\n"
        "    rows between 6 preceding and current row\n"
        ") as rolling_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert "rolling_amount" not in entry["drafted"].get("columns", {})

    # Bounded rolling window (1 preceding)
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id order by placed_at\n"
        "    rows between 1 preceding and current row\n"
        ") as rolling_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert "rolling_amount" not in entry["drafted"].get("columns", {})

    # Frame with unbounded following
    entry, _ = _draft(
        "select order_id, sum(amount) over (\n"
        "    partition by customer_id order by placed_at\n"
        "    rows between unbounded preceding and unbounded following\n"
        ") as full_amount\n"
        "from {{ ref('stg_orders') }}\n"
    )
    assert "full_amount" not in entry["drafted"].get("columns", {})

    # Other aggregates: avg, max, min, row_number
    entry, _ = _draft(
        "select order_id,\n"
        "    avg(amount) over (partition by customer_id order by placed_at) as a,\n"
        "    max(amount) over (partition by customer_id order by placed_at) as m,\n"
        "    min(amount) over (partition by customer_id order by placed_at) as mi,\n"
        "    row_number() over (partition by customer_id order by placed_at) as rn\n"
        "from {{ ref('stg_orders') }}\n"
    )
    columns = entry["drafted"].get("columns", {})
    assert "a" not in columns
    assert "m" not in columns
    assert "mi" not in columns
    assert "rn" not in columns


def test_unit_shapes_are_drafted_as_hypotheses_and_never_from_a_flag_column():
    entry, _ = _draft(
        "select order_id, discount_rate, margin_pct, is_gift, amount\n"
        "from {{ ref('stg_orders') }}\n"
    )

    columns = entry["drafted"]["columns"]
    assert columns["discount_rate"]["unit"]["value"] == "fraction"
    assert "hypothesis" in columns["discount_rate"]["unit"]["evidence"]
    assert columns["margin_pct"]["unit"]["value"] == "percent"
    # An integer 0/1 column is a flag, not a fraction; a range past 1 is neither.
    assert "unit" not in columns["is_gift"]
    assert "unit" not in columns["amount"]


def test_a_computed_column_carries_no_drafted_fact():
    entry, _ = _draft(
        "select order_id, amount * 1.2 as gross from {{ ref('stg_orders') }}\n"
    )

    assert "gross" not in entry["drafted"]["columns"]


def test_a_verified_join_with_orphans_is_withheld_as_a_reference():
    orphaned = CACHE.model_copy(deep=True)
    orphaned.relationships[0].orphan_fraction = 1.0

    entry, _ = _draft(
        "select order_id, customer_id from {{ ref('stg_orders') }}\n",
        cache=orphaned,
    )

    column = entry["drafted"]["columns"]["customer_id"]
    assert "role" not in column
    assert column["references"]["value"] is None
    assert "100.0% of its values unmatched" in column["references"]["withheld"]


def _edge(to: str, column: str, orphans: float | None = 0.0) -> Relationship:
    return Relationship(
        from_dataset=ORDERS.identifier,
        from_columns=["customer_id"],
        to_dataset=to,
        to_columns=[column],
        kind=RelationshipKind.INFERRED,
        verified=True,
        orphan_fraction=orphans,
    )


def _project_draft(cache: DexCache, model: str = "fct") -> dict:
    """``model`` in ``models/marts`` reading stg_orders, beside a dimension and a
    staging model over the same customers."""

    files = {
        f"models/marts/{model}.sql": (
            "select order_id, customer_id from {{ ref('stg_orders') }}\n"
        ),
        "models/marts/dim_customers.sql": "select id from {{ ref('stg_customers') }}\n",
        "models/staging/stg_customers.sql": (
            "select id from {{ source('raw', 'customers') }}\n"
        ),
    }
    drafts, _ = draft([model], files, ["models"], cache, dialect="duckdb")
    return drafts[model]["drafted"]["columns"]["customer_id"]


def test_a_reference_prefers_a_project_model_and_the_one_nearest_the_model():
    # Found dogfooding #501: once the project is built, a column joins cleanly to
    # the raw table, its staging model and the dimension. The first edge listed
    # used to win, which proposed `references` to a raw table no `ref()` reaches.
    cache = CACHE.model_copy(deep=True)
    cache.datasets += [
        Dataset(identifier="warehouse.main.stg_customers", columns=[_key("id")]),
        Dataset(identifier="warehouse.main.dim_customers", columns=[_key("id")]),
    ]
    cache.relationships += [
        _edge("warehouse.main.stg_customers", "id"),
        _edge("warehouse.main.dim_customers", "id"),
    ]

    column = _project_draft(cache)

    assert column["references"]["value"] == "dim_customers.id"
    assert "also clean: customers, stg_customers" in column["references"]["evidence"]


def test_a_reference_skips_a_parent_whose_column_is_not_unique_and_the_model_itself():
    cache = CACHE.model_copy(deep=True)
    cache.relationships = [
        _edge("warehouse.main.fct", "customer_id"),
        _edge("warehouse.main.stg_items", "order_id"),
    ]
    cache.datasets[2] = Dataset(
        identifier="warehouse.main.stg_items",
        columns=[_col("order_id", "integer", is_unique=False)],
    )

    column = _project_draft(cache)

    assert "role" not in column
    assert column["references"]["value"] is None
    assert "not unique in warehouse.main.stg_items" in column["references"]["withheld"]


def test_a_reference_finds_the_project_model_a_folding_warehouse_reports_in_capitals():
    # Found dogfooding #501 on Snowflake: `DEX_501.STG_CUSTOMERS` is the model
    # the project writes as `stg_customers.sql`, and an exact-case lookup took it
    # for a relation no `ref()` reaches.
    cache = CACHE.model_copy(deep=True)
    cache.datasets.append(
        Dataset(
            identifier="DB.DEX_501.STG_CUSTOMERS",
            columns=[_key("ID")],
        )
    )
    cache.relationships = [_edge("DB.DEX_501.STG_CUSTOMERS", "ID")]

    column = _project_draft(cache)

    assert column["references"]["value"] == "stg_customers.id"
    assert "not a model of this project" not in column["references"]["evidence"]


def test_a_clean_join_wins_over_an_orphaned_one_listed_first():
    cache = CACHE.model_copy(deep=True)
    cache.datasets.append(
        Dataset(identifier="warehouse.main.dim_customers", columns=[_key("id")])
    )
    cache.relationships = [
        _edge("warehouse.main.dim_customers", "id", orphans=0.2),
        _edge("warehouse.main.customers", "id"),
    ]

    column = _project_draft(cache)

    assert column["role"]["value"] == "foreign_key"
    assert column["references"]["value"] == "customers.id"


def _staging_draft(cache: DexCache) -> dict:
    """A staging model over a raw table, beside a staging model over the
    customers that table's foreign key names."""

    files = {
        "models/staging/stg_payments.sql": (
            "select payment_id, customer_id from {{ source('raw', 'payments') }}\n"
        ),
        "models/staging/stg_customers.sql": (
            "select id from {{ source('raw', 'customers') }}\n"
        ),
    }
    drafts, _ = draft(["stg_payments"], files, ["models"], cache, dialect="duckdb")
    return drafts["stg_payments"]["drafted"]["columns"]["customer_id"]


def _payments_cache(*, built: bool) -> DexCache:
    """Raw sources in `raw` and the build in `dbt_dev`, as on every warehouse
    (#892): the raw foreign key joins only the raw parent."""

    def payments(identifier: str) -> Dataset:
        return Dataset(
            identifier=identifier,
            columns=[
                _key("payment_id"),
                _col("customer_id", "integer", null_fraction=0.0),
            ],
        )

    datasets = [
        payments("warehouse.raw.payments"),
        Dataset(identifier="warehouse.raw.customers", columns=[_key("id")]),
    ]
    relationships = [
        Relationship(
            from_dataset="warehouse.raw.payments",
            from_columns=["customer_id"],
            to_dataset="warehouse.raw.customers",
            to_columns=["id"],
            kind=RelationshipKind.INFERRED,
            verified=True,
            orphan_fraction=0.0,
        )
    ]
    if built:
        datasets += [
            payments("warehouse.dbt_dev.stg_payments"),
            Dataset(identifier="warehouse.dbt_dev.stg_customers", columns=[_key("id")]),
        ]
        relationships.append(
            Relationship(
                from_dataset="warehouse.dbt_dev.stg_payments",
                from_columns=["customer_id"],
                to_dataset="warehouse.dbt_dev.stg_customers",
                to_columns=["id"],
                kind=RelationshipKind.INFERRED,
                verified=True,
                orphan_fraction=0.0,
            )
        )
    return DexCache(datasets=datasets, relationships=relationships)


def test_a_built_staging_model_references_the_model_its_own_relation_joins():
    # Found dogfooding #892: a staging model's column comes from a raw table,
    # whose joins on a warehouse reach only the raw parent, so the draft
    # proposed a foreign key with no `references`. The model's own built
    # relation joins the staging model beside it, and that is the reference.
    column = _staging_draft(_payments_cache(built=True))

    assert column["role"]["value"] == "foreign_key"
    assert column["references"]["value"] == "stg_customers.id"
    assert (
        "warehouse.dbt_dev.stg_payments.customer_id -> "
        "warehouse.dbt_dev.stg_customers.id" in column["references"]["evidence"]
    )


def test_an_unbuilt_staging_model_keeps_the_raw_join_as_evidence():
    column = _staging_draft(_payments_cache(built=False))

    assert column["role"]["value"] == "foreign_key"
    assert column["references"]["value"] == "customers.id"
    assert "not a model of this project" in column["references"]["evidence"]


# --- value domains ------------------------------------------------------------------


def test_a_filtered_column_s_domain_is_drafted_and_a_pii_one_is_withheld():
    entry, _ = _draft(
        "select order_id from {{ ref('stg_orders') }}\n"
        "where status <> 'cancelled' and email is not null\n"
    )

    domains = {d["column"]: d for d in entry["drafted"]["domains"]}
    status = domains["stg_orders.status"]
    assert status["values"] == ["shipped", "cancelled"]
    assert status["complete"] is True
    assert status["used_in"] == ["where"]
    email = domains["stg_orders.email"]
    assert email["values"] is None
    assert "never drafted" in email["withheld"]


def test_a_column_a_case_branches_on_is_drafted_too():
    entry, _ = _draft(
        "select order_id,\n"
        "  case when status = 'shipped' then 1 else 0 end as shipped\n"
        "from {{ ref('stg_orders') }}\n"
    )

    [domain] = entry["drafted"]["domains"]
    assert domain["column"] == "stg_orders.status"
    assert domain["used_in"] == ["case"]


def test_a_domain_is_capped_with_the_rest_counted():
    big = DexCache(
        datasets=[
            Dataset(
                identifier="warehouse.main.stg_orders",
                columns=[
                    _key("order_id"),
                    _col(
                        "status",
                        value_domain=ValueDomain(
                            values=[
                                ValueCount(value=f"s{i}", count=1) for i in range(5)
                            ],
                            elided=3,
                        ),
                    ),
                ],
            )
        ]
    )
    files = {
        "models/marts/fct.sql": "select order_id from {{ ref('stg_orders') }} "
        "where status = 's1'\n"
    }

    drafts, _ = draft(["fct"], files, ["models"], big, dialect="duckdb", domain_cap=2)

    [domain] = drafts["fct"]["drafted"]["domains"]
    assert domain["values"] == ["s0", "s1"]
    assert domain["elided"] == 6
    assert domain["complete"] is False


# --- honest gaps ------------------------------------------------------------------


def test_an_unprofiled_input_is_named_with_the_command_that_profiles_it():
    entry, warnings = _draft("select id from {{ ref('stg_refunds') }}\n")

    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input == {
        "relation": "stg_refunds",
        "profiled": False,
        "reason": "the cache has not profiled it",
        "command": "explore profile stg_refunds",
    }
    assert any("`explore profile stg_refunds`" in w for w in warnings)


def _dev_draft(cache: DexCache, *, alias: str | None = None):
    """A mart over a project staging model whose dev build is outside the
    allowlist (#893): the drafter names the dev namespace it must read."""

    staging = "select 1 as id\n"
    if alias is not None:
        staging = f"{{{{ config(alias='{alias}') }}}}\n" + staging
    files = {
        "models/marts/fct.sql": "select id from {{ ref('stg_refunds') }}\n",
        "models/staging/stg_refunds.sql": staging,
    }
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        cache,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
    )
    return drafts["fct"], warnings


def test_a_dev_only_model_is_named_with_a_command_that_reads_the_dev_target():
    """#893: a plain `explore profile stg_refunds` cannot find a model whose
    only build is in a dev namespace the allowlist does not reach."""

    entry, warnings = _dev_draft(CACHE)
    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input == {
        "relation": "stg_refunds",
        "profiled": False,
        "reason": (
            "the cache has not profiled it, and its dev build in SHOP.DBT_DEV is "
            "outside the source allowlist"
        ),
        "command": "explore profile SHOP.DBT_DEV.stg_refunds --target dev",
    }
    assert any(
        "`explore profile SHOP.DBT_DEV.stg_refunds --target dev`" in w for w in warnings
    )


def test_the_dev_command_names_the_relation_a_literal_alias_builds():
    entry, _ = _dev_draft(CACHE, alias="refunds")
    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input["command"] == (
        "explore profile SHOP.DBT_DEV.refunds --target dev"
    )


def test_a_source_is_never_sent_to_the_dev_target():
    """Only a project model is built into the dev namespace; a source lives
    where the allowlist already reads."""

    drafts, _ = draft(
        ["fct"],
        {"models/marts/fct.sql": "select id from {{ source('raw', 'refunds') }}\n"},
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert missing_input["command"] == "explore profile refunds"


def test_an_ambiguous_input_names_no_command_and_says_why():
    """#893: when the name matches several profiled relations, profiling again
    settles nothing, so no command is offered rather than one that fails."""

    twin = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = CACHE.model_copy(update={"datasets": [*CACHE.datasets, twin]})
    entry, warnings = _draft(
        "select order_id from {{ ref('stg_orders') }}\n", cache=cache
    )
    [missing_input] = entry["drafted"]["inputs"]
    assert "command" not in missing_input
    assert missing_input["reason"] == (
        "it matches 2 relations in the cache (warehouse.dbt_dev.stg_orders, "
        "warehouse.main.stg_orders), and a ref() is read by name, so no profile "
        "run can settle which one it means"
    )
    [warning] = [w for w in warnings if "stg_orders" in w and "profiled" in w]
    assert "explore profile" not in warning
    assert "no profile run can settle" in warning


def test_an_ambiguous_input_resolves_through_compiled_manifest():
    """#989: when the input matches several profiled relations, resolve it through
    the compiled manifest's relation_name for the compiled target."""

    twin = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = CACHE.model_copy(update={"datasets": [*CACHE.datasets, twin]})
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."stg_orders"',
            }
        },
    }
    entry, warnings = _draft(
        "select order_id from {{ ref('stg_orders') }}\n",
        cache=cache,
        manifest=manifest,
    )
    [input_entry] = entry["drafted"]["inputs"]
    assert input_entry["profiled"] is True
    assert input_entry["identifier"] == "warehouse.dbt_dev.stg_orders"
    assert input_entry["evidence"] == (
        "resolved through the compiled manifest's relation for target `dev`"
    )
    assert entry["drafted"]["grain"]["value"] == ["order_id"]
    assert not any("profile run can settle" in w for w in warnings)


def test_an_ambiguous_input_with_stale_manifest_keeps_unprofiled_reason():
    """#989: when the manifest is stale, keep the unprofiled reason."""

    twin = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = CACHE.model_copy(update={"datasets": [*CACHE.datasets, twin]})
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."stg_orders"',
            }
        },
    }
    entry, _warnings = _draft(
        "select order_id from {{ ref('stg_orders') }}\n",
        cache=cache,
        manifest=manifest,
        manifest_stale=True,
    )
    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input["profiled"] is False
    assert "command" not in missing_input
    assert "no profile run can settle which one it means" in missing_input["reason"]


def test_an_ambiguous_input_with_manifest_matching_no_candidate_keeps_unprofiled():
    """#989: when the manifest relation matches no candidate, keep unprofiled."""

    twin = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = CACHE.model_copy(update={"datasets": [*CACHE.datasets, twin]})
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."other_schema"."stg_orders"',
            }
        },
    }
    entry, _warnings = _draft(
        "select order_id from {{ ref('stg_orders') }}\n",
        cache=cache,
        manifest=manifest,
    )
    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input["profiled"] is False
    assert "command" not in missing_input
    assert "no profile run can settle which one it means" in missing_input["reason"]


def test_literal_table_does_not_inherit_manifest_relation():
    """#989: literal table name does not resolve through compiled manifest."""

    twin = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = CACHE.model_copy(update={"datasets": [*CACHE.datasets, twin]})
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."stg_orders"',
            }
        },
    }
    entry, _warnings = _draft(
        "select order_id from stg_orders\n",
        cache=cache,
        manifest=manifest,
    )
    [input_entry] = entry["drafted"]["inputs"]
    assert input_entry["profiled"] is False
    assert "resolved through the compiled manifest" not in input_entry.get(
        "evidence", ""
    )
    assert "no profile run can settle which one it means" in input_entry["reason"]


def test_model_names_resolve_before_aliases_and_collisions_remain_ambiguous():
    """#989: model names take precedence over aliases and collisions stay ambiguous."""

    legacy = ORDERS.model_copy(update={"identifier": "warehouse.legacy.stg_orders"})
    dev = ORDERS.model_copy(update={"identifier": "warehouse.dbt_dev.stg_orders"})
    cache = DexCache(datasets=[legacy, dev])
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.old_orders": {
                "name": "old_orders",
                "alias": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."legacy"."stg_orders"',
            },
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."stg_orders"',
            },
        },
    }
    entry, _warnings = _draft(
        "select order_id from {{ ref('stg_orders') }}\n",
        cache=cache,
        manifest=manifest,
    )
    [input_entry] = entry["drafted"]["inputs"]
    assert input_entry["profiled"] is True
    assert input_entry["identifier"] == "warehouse.dbt_dev.stg_orders"
    assert "resolved through the compiled manifest" in input_entry["evidence"]


def test_query_using_both_ref_and_literal_table_preserves_distinct_provenance():
    """#989: when a query uses both ref('model') and literal 'model',
    provenance is preserved."""

    main_orders = Dataset(
        identifier="warehouse.main.stg_orders",
        columns=[
            _col("order_id", "integer", null_fraction=0.5),
            _col("status", "varchar"),
        ],
    )
    dev_orders = Dataset(
        identifier="warehouse.dbt_dev.stg_orders",
        columns=[
            _key("order_id"),
            _col("amount", "double"),
        ],
    )
    cache = DexCache(datasets=[main_orders, dev_orders])
    manifest = {
        "metadata": {"project_name": "shop", "target_name": "dev"},
        "nodes": {
            "model.shop.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."stg_orders"',
            }
        },
    }
    entry, _warnings = _draft(
        "select r.order_id, l.status\n"
        "from {{ ref('stg_orders') }} r\n"
        "join stg_orders l on r.order_id = l.order_id\n"
        "where r.order_id > 10\n",
        cache=cache,
        manifest=manifest,
    )
    inputs = entry["drafted"]["inputs"]
    assert len(inputs) == 2
    ref_input = next(i for i in inputs if i.get("profiled") is True)
    literal_input = next(i for i in inputs if i.get("profiled") is False)

    assert ref_input["identifier"] == "warehouse.dbt_dev.stg_orders"
    assert "resolved through the compiled manifest" in ref_input["evidence"]

    assert "no profile run can settle" in literal_input["reason"]

    # order_id comes from ref('stg_orders') and resolves to dev_orders
    # where null_fraction=0.0
    assert entry["drafted"]["columns"]["order_id"]["nullable"]["value"] is False

    # status comes from literal stg_orders which is ambiguous without manifest
    assert "nullable" not in entry["drafted"]["columns"].get("status", {})

    # domain resolves for ref column
    [domain] = entry["drafted"]["domains"]
    assert domain["column"] == "stg_orders.order_id"


def test_a_package_input_names_derived_dev_namespace_on_warehouse():
    """#1199: a package model with custom schema derives the namespace."""

    from types import SimpleNamespace

    target = SimpleNamespace(schema="inbox", package="tiny_inbox")
    drafts, warnings = draft(
        ["fct"],
        {"models/marts/fct.sql": "select id from {{ ref('stg_inbox') }}\n"},
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
        package_target=lambda name: target if name == "stg_inbox" else None,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert missing_input == {
        "relation": "stg_inbox",
        "profiled": False,
        "reason": (
            "the cache has not profiled it, and its dev build in "
            "SHOP.DBT_DEV_inbox is outside the source allowlist"
        ),
        "command": "explore profile SHOP.DBT_DEV_inbox.stg_inbox --target dev",
    }
    assert any(
        "`explore profile SHOP.DBT_DEV_inbox.stg_inbox --target dev`" in w
        for w in warnings
    )


def test_a_package_input_without_custom_schema_names_dev_namespace():
    """#1199: a package model without custom schema uses the dev namespace."""

    drafts, warnings = draft(
        ["fct"],
        {"models/marts/fct.sql": "select id from {{ ref('stg_inbox') }}\n"},
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
        packaged=lambda name: "tiny_inbox" if name == "stg_inbox" else None,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert missing_input["command"] == (
        "explore profile SHOP.DBT_DEV.stg_inbox --target dev"
    )
    assert any(
        "`explore profile SHOP.DBT_DEV.stg_inbox --target dev`" in w for w in warnings
    )


def test_a_package_input_with_custom_schema_withholds_command_when_macro_overridden():
    """#1199: an override of generate_schema_name withholds the profile hint."""

    from types import SimpleNamespace

    target = SimpleNamespace(schema="inbox", package="tiny_inbox")
    drafts, warnings = draft(
        ["fct"],
        {
            "models/marts/fct.sql": "select id from {{ ref('stg_inbox') }}\n",
            "macros/generate_schema_name.sql": (
                "{% macro generate_schema_name(custom_schema_name, node) %}"
                "{{ custom_schema_name }}_{{ target.name }}{% endmacro %}\n"
            ),
        },
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
        package_target=lambda name: target if name == "stg_inbox" else None,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert "command" not in missing_input
    assert "project overrides generate_schema_name" in missing_input["reason"]
    assert any("generate_schema_name" in w for w in warnings)


def test_commands_draft_package_model_with_custom_schema(tmp_path, monkeypatch):
    """#1199: commands._draft resolves package schema through lookup target."""

    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.dbt_project import load as load_project
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.storage.memory import MemoryStore
    from exmergo_dex_core.transform import commands
    from exmergo_dex_core.transform.plans import EditKind, EditOp, PlanEdit

    pkg_dbt = (
        "name: tiny_inbox\nversion: '1.0'\nconfig-version: 2\n"
        "model-paths: [models]\nmodels:\n  tiny_inbox:\n    +schema: inbox\n"
    )
    pkg_model = "select 1 as id\n"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\n"
    )
    (project_dir / "models").mkdir()
    pkg_dir = project_dir / "dbt_packages" / "tiny_inbox"
    (pkg_dir / "models").mkdir(parents=True)
    (pkg_dir / "dbt_project.yml").write_text(pkg_dbt)
    (pkg_dir / "models" / "stg_inbox.sql").write_text(pkg_model)

    view = load_project(project_dir)
    engine = DexEngine(
        config=DexConfig(connector="snowflake"),
        store=MemoryStore(),
        repo_root=str(tmp_path),
    )
    monkeypatch.setattr(commands, "_dev_namespace", lambda _eng: "SHOP.DBT_DEV")

    edits = [
        PlanEdit(
            path="models/fct.sql",
            kind=EditKind.MODEL_SQL,
            op=EditOp.UPSERT,
            new_content="select id from {{ ref('stg_inbox') }}\n",
        )
    ]
    drafts, warnings = commands._draft(engine, view, edits, ["fct"])
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert missing_input["command"] == (
        "explore profile SHOP.DBT_DEV_inbox.stg_inbox --target dev"
    )
    assert any("SHOP.DBT_DEV_inbox" in w for w in warnings)


def test_an_input_created_by_plan_names_post_build_command_and_has_no_command():
    """#1199: an input created by the plan carries no command and names post-build."""

    files = {
        "models/marts/fct.sql": "select id from {{ ref('time_spine') }}\n",
        "models/staging/time_spine.sql": "select 1 as id\n",
    }
    prior = {"models/marts/fct.sql": "select 1 as id\n"}
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
        prior=prior,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert "command" not in missing_input
    assert missing_input["profiled"] is False
    assert missing_input["reason"] == (
        "it is built by this plan; run `transform build --target dev`, then "
        "`explore profile SHOP.DBT_DEV.time_spine --target dev`"
    )
    [warning] = [w for w in warnings if "time_spine" in w and "profiled" in w]
    assert "it is built by this plan" in warning
    assert (
        "run `transform build --target dev`, then "
        "`explore profile SHOP.DBT_DEV.time_spine --target dev`"
    ) in warning


def test_an_input_created_by_plan_on_duckdb_names_plain_post_build_command():
    """#1199: on duckdb an input created by the plan names post-build without dev."""

    files = {
        "models/marts/fct.sql": "select id from {{ ref('time_spine') }}\n",
        "models/staging/time_spine.sql": "select 1 as id\n",
    }
    prior = {}
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        CACHE,
        dialect="duckdb",
        prior=prior,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert "command" not in missing_input
    assert missing_input["profiled"] is False
    assert missing_input["reason"] == (
        "it is built by this plan; run `transform build`, then "
        "`explore profile time_spine`"
    )
    [warning] = [w for w in warnings if "time_spine" in w and "profiled" in w]
    assert "it is built by this plan" in warning
    assert "run `transform build`, then `explore profile time_spine`" in warning


def test_an_input_created_by_plan_with_alias_names_alias_in_post_build_command():
    """#1199: an input created by the plan with an alias uses the alias."""

    files = {
        "models/marts/fct.sql": "select id from {{ ref('time_spine') }}\n",
        "models/staging/time_spine.sql": (
            "{{ config(alias='dim_time') }}\nselect 1 as id\n"
        ),
    }
    prior = {}
    drafts, warnings = draft(
        ["fct"],
        files,
        ["models"],
        CACHE,
        dialect="snowflake",
        dev_namespace="SHOP.DBT_DEV",
        prior=prior,
    )
    [missing_input] = drafts["fct"]["drafted"]["inputs"]
    assert "command" not in missing_input
    assert missing_input["reason"] == (
        "it is built by this plan; run `transform build --target dev`, then "
        "`explore profile SHOP.DBT_DEV.dim_time --target dev`"
    )
    [warning] = [w for w in warnings if "time_spine" in w and "profiled" in w]
    assert "it is built by this plan" in warning
    assert "dim_time" in warning


def test_a_time_spine_from_range_is_not_told_to_profile_an_empty_relation():
    """#832: MetricFlow's time spine is ``select ... from range(...)``. sqlglot
    reads that table function as a table with no name, and the draft must not
    tell the reader to profile a relation that does not exist."""

    sql = (
        "select cast(range as date) as date_day "
        "from range(date '2024-01-01', date '2031-01-01', interval 1 day)\n"
    )
    drafts, warnings = draft(
        ["metricflow_time_spine"],
        {"models/metricflow_time_spine.sql": sql},
        ["models"],
        CACHE,
        dialect="duckdb",
    )

    entry = drafts["metricflow_time_spine"]
    assert "inputs" not in entry["drafted"]
    assert entry["drafted"]["grain"] == {
        "value": None,
        "withheld": "it is generated in SQL, so no key of it is proven",
    }
    assert (
        "'metricflow_time_spine' is generated in SQL; nothing to draft from" in warnings
    )
    assert not any("explore profile" in w or "reads ," in w for w in warnings)


def test_a_values_clause_is_generated_sql_with_nothing_to_draft_from():
    entry, warnings = _draft(
        "select date_day from (\n"
        "  values (date '2020-01-01'), (date '2020-01-02')\n"
        ") as t(date_day)\n"
    )

    assert "inputs" not in entry["drafted"]
    assert "'fct' is generated in SQL; nothing to draft from" in warnings
    assert not any("explore profile" in w for w in warnings)


def test_a_table_function_with_no_parent_is_not_told_to_profile_an_empty_name():
    entry, warnings = _draft("select * from read_csv_auto('data.csv')\n")

    assert "inputs" not in entry["drafted"]
    assert "'fct' is generated in SQL; nothing to draft from" in warnings
    assert not any("explore profile" in w or "reads ," in w for w in warnings)


def test_a_named_relation_beside_range_still_names_the_profile_command():
    entry, warnings = _draft(
        "select order_id from {{ ref('stg_refunds') }}\n"
        "cross join range(date '2024-01-01', date '2024-01-03', interval 1 day)\n"
    )

    [missing_input] = entry["drafted"]["inputs"]
    assert missing_input["relation"] == "stg_refunds"
    assert missing_input["command"] == "explore profile stg_refunds"
    assert any("`explore profile stg_refunds`" in w for w in warnings)
    assert not any("generated in SQL" in w or "reads ," in w for w in warnings)


def test_no_cache_at_all_still_drafts_nothing_and_names_every_input():
    files = {"models/marts/fct.sql": "select id from {{ ref('stg_orders') }}\n"}

    drafts, _ = draft(
        ["fct"],
        files,
        ["models"],
        None,
        dialect="duckdb",
        cache_note="there is no exploration cache yet; run `explore map` first",
    )

    [entry] = drafts["fct"]["drafted"]["inputs"]
    assert entry["profiled"] is False
    assert "explore map" in entry["reason"]
    assert "grain" not in drafts["fct"]["drafted"] or (
        drafts["fct"]["drafted"]["grain"]["value"] is None
    )


def test_a_macro_reports_incomplete_inference_and_manufactures_nothing():
    entry, _ = _draft(
        "select order_id, {{ cents_to_dollars('amount') }} as amount\n"
        "from {{ ref('stg_orders') }}\n"
    )

    assert "columns" not in entry["drafted"]
    assert "grain" not in entry["drafted"]
    [reason] = entry["drafted"]["incomplete"]
    assert "cents_to_dollars" in reason


def test_a_set_operation_carries_no_key():
    entry, _ = _draft(
        "select order_id from {{ ref('stg_orders') }}\n"
        "union all\nselect item_id from {{ ref('stg_items') }}\n"
    )

    assert "grain" not in entry["drafted"]
    assert any("set operation" in r for r in entry["drafted"]["incomplete"])


# --- declared and missing -----------------------------------------------------------

DECLARED_YML = """version: 2
models:
  - name: fct
    description: One row per shipped order.
    config:
      meta:
        dex:
          grain: [customer_id]
    columns:
      - name: order_id
        config:
          meta:
            dex:
              role: key
      - name: amount
        config:
          meta:
            dex:
              role: measure
              aggregation: sum
"""


def test_declared_is_read_from_the_yaml_and_missing_names_the_rest():
    entry, warnings = _draft(
        "select order_id, amount from {{ ref('stg_orders') }} "
        "where status = 'shipped'\n",
        yml=DECLARED_YML,
    )

    assert entry["declared"]["description"] == "One row per shipped order."
    assert entry["declared"]["columns"]["amount"] == {
        "role": "measure",
        "aggregation": "sum",
    }
    assert entry["missing"] == [
        "population",
        "assumptions",
        "columns.amount.additivity",
        "columns.amount.unit",
    ]
    assert "'fct' declares no population rule; its SQL filters on status" in warnings
    assert "'fct' declares measure amount without its additivity, unit" in warnings
    # The YAML says customer_id, the SQL and the cache say order_id.
    assert any(
        "declares grain [customer_id] and dex drafted [order_id]" in w for w in warnings
    )


def test_a_declared_grain_matching_a_proven_key_is_not_a_disagreement():
    """A proven key the draft passed over still holds as a declaration (#1079).

    email and user_id are both proven keys and the draft picks user_id because
    email is flagged as PII; declaring [email] matches a proven key, so warning
    that it disagrees with the drafted grain would contradict the contradiction
    check, which already accepts it.
    """
    yml = """version: 2
models:
  - name: fct
    config:
      meta:
        dex:
          grain: [email]
"""
    entry, warnings = _draft(
        "select email, user_id from {{ ref('dim_users') }}\n",
        yml=yml,
        cache=DexCache(datasets=[DIM_USERS]),
    )

    assert entry["drafted"]["grain"]["value"] == ["user_id"]
    assert not any("declares grain" in w for w in warnings)


def test_measure_warnings_group_only_columns_with_the_same_missing_fields():
    yml = """version: 2
models:
  - name: fct
    columns:
      - name: amount
        config:
          meta:
            dex: {role: measure, aggregation: sum, unit: USD}
      - name: discount_rate
        config:
          meta:
            dex: {role: measure, aggregation: sum, unit: fraction}
      - name: margin_pct
        config:
          meta:
            dex: {role: measure}
"""
    entry, warnings = _draft(
        "select amount, discount_rate, margin_pct from {{ ref('stg_orders') }}\n",
        yml=yml,
    )
    assert entry["missing"][-5:] == [
        "columns.amount.additivity",
        "columns.discount_rate.additivity",
        "columns.margin_pct.aggregation",
        "columns.margin_pct.additivity",
        "columns.margin_pct.unit",
    ]
    assert [w for w in warnings if "declares measure" in w] == [
        "'fct' declares measures amount, discount_rate without their additivity",
        "'fct' declares measure margin_pct without its aggregation, additivity, unit",
    ]


def test_a_model_with_nothing_declared_misses_every_intent_field():
    entry, warnings = _draft("select order_id from {{ ref('stg_orders') }}\n")

    assert entry["declared"] == {}
    assert entry["missing"] == [
        "description",
        "grain",
        "population",
        "assumptions",
        "columns.order_id.role",
    ]
    assert any("declares no grain; dex drafted [order_id]" in w for w in warnings)


# --- end to end -------------------------------------------------------------------


@pytest.fixture
def shop(tmp_path: Path, capsys) -> tuple[Path, Path]:
    """A dbt project over a DuckDB warehouse, explored and join-verified."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "shop.duckdb"
    conn = duckdb.connect(str(warehouse))
    conn.execute(
        "create table raw_orders as select i as order_id, (i % 20) + 1 as "
        "customer_id, case when i % 9 = 0 then 'cancelled' else 'shipped' end "
        "as status, (i % 50) / 100.0 as discount_rate, i * 3.5 as amount "
        "from range(1, 401) t(i)"
    )
    conn.execute(
        "create table customers as select i as customer_id, 'r' || (i % 3) as "
        "region from range(1, 21) t(i)"
    )
    conn.close()

    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: raw_orders\n      - name: customers\n",
        encoding="utf-8",
    )
    for argv in (
        ["explore", "map", "--full"],
        ["explore", "relationships", "--verify"],
    ):
        rc = main(
            [
                *argv,
                "--connector",
                "duckdb",
                "--path",
                str(warehouse),
                "--repo-root",
                str(tmp_path),
            ]
        )
        capsys.readouterr()
        assert rc == 0, argv
    return tmp_path, warehouse


MART = """select o.order_id, o.customer_id, o.amount, o.discount_rate, c.region
from {{ source('raw', 'raw_orders') }} o
left join {{ source('raw', 'customers') }} c on c.customer_id = o.customer_id
where o.status <> 'cancelled'
"""


def _plan(repo: Path, warehouse: Path, body: dict, capsys) -> dict:
    payload = repo / "payload.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "orders mart",
            "--edits-file",
            str(payload),
            "--connector",
            "duckdb",
            "--path",
            str(warehouse),
        ]
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    envelope = json.loads(out)
    assert rc == 0, envelope
    return envelope


def test_a_plan_drafts_from_the_cache_and_opens_no_connection(
    shop, capsys, monkeypatch
):
    import exmergo_dex_core.connect as connect
    from exmergo_dex_core.engine import DexEngine

    repo, warehouse = shop

    def refuse(*_args, **_kwargs):
        raise AssertionError("drafting opened a warehouse connection")

    monkeypatch.setattr(connect, "open_adapter", refuse)
    monkeypatch.setattr(DexEngine, "_adapter", refuse)
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                {
                    "path": "models/marts/fct_orders.sql",
                    "kind": "model_sql",
                    "content": MART,
                }
            ]
        },
        capsys,
    )

    entry = envelope["data"]["declarations"]["fct_orders"]
    drafted = entry["drafted"]
    assert drafted["grain"]["value"] == ["order_id"]
    assert drafted["columns"]["customer_id"]["references"]["value"] == (
        "customers.customer_id"
    )
    assert drafted["columns"]["region"]["nullable"]["value"] is True
    assert drafted["columns"]["discount_rate"]["unit"]["value"] == "fraction"
    [status] = drafted["domains"]
    assert set(status["values"]) == {"shipped", "cancelled"}
    assert all(i["profiled"] for i in drafted["inputs"])
    assert entry["declared"] == {}
    assert "population" in entry["missing"]
    assert (
        "'fct_orders' declares no population rule; its SQL filters on status"
        in envelope["warnings"]
    )
    # Propose, don't impose: nothing reaches the project before apply.
    assert not (repo / "analytics" / "models" / "marts" / "fct_orders.sql").exists()


def test_a_declaration_in_the_same_plan_moves_fields_from_missing_to_declared(
    shop, capsys
):
    repo, warehouse = shop
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                {
                    "path": "models/marts/fct_orders.sql",
                    "kind": "model_sql",
                    "content": MART,
                }
            ],
            "declarations": [
                {
                    "model": "fct_orders",
                    "description": "One row per order that was not cancelled.",
                    "grain": ["order_id"],
                    "population": {
                        "rule": "Cancelled orders are excluded.",
                        "filters": [{"column": "status", "excludes": ["cancelled"]}],
                    },
                }
            ],
        },
        capsys,
    )

    entry = envelope["data"]["declarations"]["fct_orders"]
    assert entry["declared"]["grain"] == ["order_id"]
    assert entry["declared"]["population"]["filters"] == [
        {"column": "status", "excludes": ["cancelled"]}
    ]
    assert "population" not in entry["missing"]
    assert "grain" not in entry["missing"]
    assert entry["missing"][0] == "assumptions"
    # The draft agrees with the declaration, so no disagreement is raised.
    assert not any("one of them is wrong" in w for w in envelope["warnings"])


def test_a_plan_that_touches_no_model_carries_no_declarations(shop, capsys):
    repo, warehouse = shop
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                {
                    "path": "macros/cents.sql",
                    "kind": "macro_sql",
                    "content": "{% macro cents(c) %}{{ c }} * 100{% endmacro %}\n",
                }
            ]
        },
        capsys,
    )

    assert "declarations" not in envelope["data"]


def test_a_cte_whose_body_is_a_union_is_not_read_as_a_physical_relation():
    import sqlglot

    from exmergo_dex_core.transform.drafting import _Lineage, _Reading

    sql = (
        "with named as (select order_id, amount from raw_orders), "
        "residual as (select order_id, amount from archived_orders), "
        "combined as ( "
        "    select order_id, amount from named "
        "    union all "
        "    select order_id, amount from residual "
        ") "
        "select combined.order_id, combined.amount, c.region "
        "from combined "
        "cross join configured "
        "left join customers c on combined.order_id = c.customer_id"
    )
    tree = sqlglot.parse_one(sql, read="duckdb")
    reading = _Reading()
    lineage = _Lineage(tree, {}, None, reading)
    lineage.run()
    assert "combined" not in reading.relations
    assert "named" not in reading.relations
    assert "residual" not in reading.relations
    assert set(reading.relations) == {
        "raw_orders",
        "archived_orders",
        "configured",
        "customers",
    }


def test_plan_does_not_name_union_cte_as_unprofiled_relation(shop, capsys):
    repo, warehouse = shop
    model_sql = (
        "with named as (\n"
        "    select order_id, amount from {{ source('raw', 'raw_orders') }}\n"
        "), residual as (\n"
        "    select order_id, amount from {{ source('raw', 'raw_orders') }}\n"
        "), combined as (\n"
        "    select order_id, amount from named\n"
        "    union all\n"
        "    select order_id, amount from residual\n"
        ")\n"
        "select combined.order_id, combined.amount, c.region\n"
        "from combined\n"
        "left join {{ source('raw', 'customers') }} c\n"
        "    on combined.order_id = c.customer_id\n"
    )
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                {
                    "path": "models/marts/fct_daily.sql",
                    "kind": "model_sql",
                    "content": model_sql,
                }
            ]
        },
        capsys,
    )

    # Drafting should not report 'combined' as an unprofiled relation.
    assert not any("combined" in w for w in envelope.get("warnings", []))
    assert "fct_daily" in envelope["data"]["declarations"]


def test_a_pii_flagged_key_is_not_silently_the_grain_when_a_plain_key_exists():
    entry, _ = _draft(
        "select email, user_id from {{ ref('dim_users') }}\n",
        cache=DexCache(datasets=[DIM_USERS]),
    )

    grain = entry["drafted"]["grain"]
    assert grain["value"] == ["user_id"]
    assert "flagged as PII" not in grain["evidence"]


def test_an_all_pii_grain_still_drafts_and_names_the_flag():
    entry, _ = _draft(
        "select product_id, email from {{ ref('dim_products') }}\n",
        cache=DexCache(datasets=[DIM_PRODUCTS]),
    )

    grain = entry["drafted"]["grain"]
    assert grain["value"] == ["email"]
    assert "flagged as PII" in grain["evidence"]


def test_a_pii_flag_under_a_configured_alias_is_still_seen():
    """The profile can live under the model's alias, not its dbt name (#1079).

    A DISTINCT proves the key from the statement alone, so the alias lookup in
    the PII check is the one chance to read the flag.
    """
    users = Dataset(
        identifier="warehouse.main.users_clean",
        columns=[
            _col(
                "email",
                null_fraction=0.0,
                is_unique=True,
                distinct_count_exact=True,
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
            ),
            _key("user_id"),
        ],
    )
    files = {
        "models/marts/fct.sql": "select distinct email from {{ ref('stg_users') }}\n",
        "models/staging/stg_users.sql": (
            "{{ config(alias='users_clean') }}\n"
            "select email, user_id from {{ source('raw', 'users') }}\n"
        ),
    }
    drafts, _ = draft(
        ["fct"], files, ["models"], DexCache(datasets=[users]), dialect="duckdb"
    )

    grain = drafts["fct"]["drafted"]["grain"]
    assert grain["value"] == ["email"]
    assert "flagged as PII" in grain["evidence"]


def test_a_literal_alias_is_recognized_as_a_project_model():
    """A warehouse relation named by config(alias=…) maps back to the model."""
    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        Relationship,
        RelationshipKind,
    )
    from exmergo_dex_core.transform.drafting import draft

    def key(n: str) -> ColumnProfile:
        return ColumnProfile(
            name=n,
            data_type="integer",
            null_fraction=0.0,
            is_unique=True,
            distinct_count_exact=True,
        )

    def col(n: str) -> ColumnProfile:
        return ColumnProfile(name=n, data_type="integer", null_fraction=0.0)

    files = {
        "models/marts/fct.sql": (
            "select order_id, customer_id from {{ ref('stg_orders') }}\n"
        ),
        "models/staging/stg_orders.sql": (
            "select order_id, customer_id from {{ source('raw','orders') }}\n"
        ),
        "models/staging/stg_customers.sql": (
            "{{ config(alias='customers_clean') }}\n"
            "select customer_id from {{ source('raw','customers') }}\n"
        ),
    }
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="w.dev.stg_orders",
                columns=[key("order_id"), col("customer_id")],
            ),
            Dataset(
                identifier="w.dev.customers_clean",
                columns=[key("customer_id")],
            ),
        ],
        relationships=[
            Relationship(
                from_dataset="w.dev.stg_orders",
                from_columns=["customer_id"],
                to_dataset="w.dev.customers_clean",
                to_columns=["customer_id"],
                kind=RelationshipKind.INFERRED,
                verified=True,
                orphan_fraction=0.0,
            )
        ],
    )
    drafts, _ = draft(["fct"], files, ["models"], cache, dialect="duckdb")
    refs = drafts["fct"]["drafted"]["columns"]["customer_id"]["references"]
    assert refs["value"] == "stg_customers.customer_id"
    assert "not a model of this project" not in refs["evidence"]


def test_an_aliased_input_is_found_in_the_cache_under_its_alias():
    """dataset lookup by model name resolves the relation the alias built."""
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache
    from exmergo_dex_core.transform.drafting import draft

    def key(n: str) -> ColumnProfile:
        return ColumnProfile(
            name=n,
            data_type="integer",
            null_fraction=0.0,
            is_unique=True,
            distinct_count_exact=True,
        )

    files = {
        "models/marts/fct.sql": (
            "select customer_id from {{ ref('stg_customers') }}\n"
        ),
        "models/staging/stg_customers.sql": (
            "{{ config(alias='customers_clean') }}\n"
            "select customer_id from {{ source('raw','customers') }}\n"
        ),
    }
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="w.dev.customers_clean",
                columns=[key("customer_id")],
            )
        ],
    )
    drafts, warnings = draft(["fct"], files, ["models"], cache, dialect="duckdb")
    inputs = {i["relation"]: i for i in drafts["fct"]["drafted"]["inputs"]}
    assert inputs["stg_customers"]["profiled"] is True
    assert not any("stg_customers" in w and "not profiled" in w for w in warnings)


def test_a_yaml_literal_alias_is_recognized_the_same_way():
    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        Relationship,
        RelationshipKind,
    )
    from exmergo_dex_core.transform.drafting import draft

    def key(n: str) -> ColumnProfile:
        return ColumnProfile(
            name=n,
            data_type="integer",
            null_fraction=0.0,
            is_unique=True,
            distinct_count_exact=True,
        )

    def col(n: str) -> ColumnProfile:
        return ColumnProfile(name=n, data_type="integer", null_fraction=0.0)

    files = {
        "models/marts/fct.sql": (
            "select order_id, customer_id from {{ ref('stg_orders') }}\n"
        ),
        "models/staging/stg_orders.sql": (
            "select order_id, customer_id from {{ source('raw','orders') }}\n"
        ),
        "models/staging/stg_customers.sql": (
            "select customer_id from {{ source('raw','customers') }}\n"
        ),
        "models/staging/schema.yml": (
            "models:\n"
            "  - name: stg_customers\n"
            "    config:\n"
            "      alias: customers_clean\n"
        ),
    }
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="w.dev.stg_orders",
                columns=[key("order_id"), col("customer_id")],
            ),
            Dataset(
                identifier="w.dev.customers_clean",
                columns=[key("customer_id")],
            ),
        ],
        relationships=[
            Relationship(
                from_dataset="w.dev.stg_orders",
                from_columns=["customer_id"],
                to_dataset="w.dev.customers_clean",
                to_columns=["customer_id"],
                kind=RelationshipKind.INFERRED,
                verified=True,
                orphan_fraction=0.0,
            )
        ],
    )
    drafts, _ = draft(["fct"], files, ["models"], cache, dialect="duckdb")
    refs = drafts["fct"]["drafted"]["columns"]["customer_id"]["references"]
    assert refs["value"] == "stg_customers.customer_id"


def test_a_grain_narrower_than_the_group_by_is_named_as_a_condition_not_an_error():
    from exmergo_dex_core.transform.drafting import _grain_disagreement

    narrower = _grain_disagreement(
        "customer_orders", ["customer_id"], ["customer_id", "country_code"]
    )
    assert "narrower than the [customer_id, country_code]" in narrower
    assert "determines country_code" in narrower
    assert "is wrong" not in narrower
    wider = _grain_disagreement("m", ["order_id", "status"], ["order_id"])
    assert "wider than the [order_id]" in wider
    disjoint = _grain_disagreement("m", ["a"], ["b"])
    assert disjoint.endswith("one of them is wrong")


def test_a_star_hands_on_only_the_columns_its_exclusions_leave():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select * exclude (extra) from (select 1 as id, 2 as extra) t",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["id"]

    reading = drafting.read(
        "select * except (extra) from (select 1 as id, 2 as extra) t",
        None,
        dialect="bigquery",
        vars={},
    )
    assert reading.produced == ["id"]


def test_a_qualified_star_exclusion_drops_the_column_too():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select t.* exclude (extra) from (select 1 as id, 2 as extra) t",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["id"]

    reading = drafting.read(
        "select t.* except (extra) from (select 1 as id, 2 as extra) t",
        None,
        dialect="bigquery",
        vars={},
    )
    assert reading.produced == ["id"]


def test_a_star_replace_keeps_the_name_and_a_rename_leaves_the_old_one():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select * replace (extra + 1 as extra) from (select 1 as id, 2 as extra) t",
        None,
        dialect="bigquery",
        vars={},
    )
    assert reading.produced == ["id", "extra"]

    reading = drafting.read(
        "select * rename (extra as e2) from (select 1 as id, 2 as extra) t",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["id", "e2"]


def test_a_qualified_exclusion_only_drops_its_own_tables_column():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select * exclude (a.id) from"
        " (select 1 as id, 2 as extra) a"
        " join (select 1 as id, 3 as e2) b on a.id = b.id",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["extra", "id", "e2"]

    reading = drafting.read(
        "select * exclude (id) from"
        " (select 1 as id, 2 as extra) a"
        " join (select 1 as id, 3 as e2) b on a.id = b.id",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["extra", "e2"]


def test_a_qualified_rename_only_moves_its_own_tables_column():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select * rename (a.id as a_id) from"
        " (select 1 as id, 2 as extra) a"
        " join (select 1 as id, 3 as e2) b on a.id = b.id",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["a_id", "extra", "id", "e2"]


def test_a_renamed_column_is_traced_through_its_star_qualifier():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select a.* rename (id as a_id) from"
        " (select 1 as id, 2 as extra) a"
        " join (select 1 as id, 3 as e2) b on a.id = b.id",
        None,
        dialect="duckdb",
        vars={},
    )
    assert reading.produced == ["a_id", "extra"]
    assert reading.incomplete == []


def test_a_replace_and_a_rename_on_one_column_stack():
    from exmergo_dex_core.transform import drafting

    reading = drafting.read(
        "select * replace (0 as id) rename (id as new_id) from"
        " (select 1 as id, 2 as extra) t",
        None,
        dialect="snowflake",
        vars={},
    )
    assert reading.produced == ["new_id", "extra"]
    assert reading.expressions["new_id"].sql() == "0 AS new_id"


def test_an_input_with_unique_test_drafts_grain_without_warehouse_profile():
    """#1252: unique test on an unprofiled input drafts grain."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
        data_tests:
          - unique
""",
        "models/marts/fct_projects.sql": (
            "select project_id, project_name from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, warnings = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    assert drafted["grain"]["value"] == ["project_id"]
    evidence = drafted["grain"]["evidence"]
    assert (
        "project_id has a unique test on stg_projects, carried through unchanged"
        in evidence
    )
    assert any("declares no grain; dex drafted [project_id]" in w for w in warnings)


def test_an_input_with_unique_combination_of_columns_drafts_composite_grain():
    """#1252: unique_combination_of_columns drafts composite grain."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_memberships
    tests:
      - unique_combination_of_columns:
          combination_of_columns: [org_id, user_id]
""",
        "models/marts/fct_memberships.sql": (
            "select org_id, user_id, role from {{ ref('stg_memberships') }}\n"
        ),
    }
    drafts, warnings = draft(
        ["fct_memberships"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_memberships"]["drafted"]
    assert drafted["grain"]["value"] == ["org_id", "user_id"]
    evidence = drafted["grain"]["evidence"]
    assert (
        "org_id, user_id has a unique_combination_of_columns test on "
        "stg_memberships, carried through unchanged" in evidence
    )
    assert any(
        "declares no grain; dex drafted [org_id, user_id]" in w for w in warnings
    )


def test_an_input_with_declared_grain_drafts_grain_without_warehouse_profile():
    """#1252: declared meta.dex.grain on unprofiled input drafts grain."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_accounts
    meta:
      dex:
        grain: [account_id]
""",
        "models/marts/fct_accounts.sql": (
            "select account_id, name from {{ ref('stg_accounts') }}\n"
        ),
    }
    drafts, warnings = draft(
        ["fct_accounts"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_accounts"]["drafted"]
    assert drafted["grain"]["value"] == ["account_id"]
    evidence = drafted["grain"]["evidence"]
    assert (
        "account_id is the declared grain of stg_accounts, carried through unchanged"
        in evidence
    )
    assert any("declares no grain; dex drafted [account_id]" in w for w in warnings)


def test_draft_reports_drafted_grain_instead_of_unresolved_grain_warning():
    """#1252: draft indicates drafted grain in warning when drafted from key."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    meta:
      dex:
        grain: [project_id]
""",
        "models/marts/fct_projects.sql": (
            "select project_id, project_name from {{ ref('stg_projects') }}\n"
        ),
    }
    _drafts, warnings = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    assert not any(w == "'fct_projects' declares no grain" for w in warnings)
    assert any("declares no grain; dex drafted [project_id]" in w for w in warnings)


def test_an_input_with_no_key_evidence_withholds_grain():
    """#1252: an unprofiled input with no tests or declared grain withholds grain."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
      - name: project_name
""",
        "models/marts/fct_projects.sql": (
            "select project_id, project_name from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, warnings = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    assert drafted.get("grain", {}).get("value") is None
    assert "not profiled" in drafted.get("grain", {}).get("withheld", "")
    assert any(
        "'fct_projects' declares no grain" in w and "dex drafted" not in w
        for w in warnings
    )


def test_transformed_key_column_does_not_inherit_grain():
    """#1252: a transformed key expression does not carry grain forward."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
        data_tests: [unique]
""",
        "models/marts/fct_projects.sql": (
            "select coalesce(project_id, 0) as project_id "
            "from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, _ = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    assert drafted.get("grain", {}).get("value") is None
    assert "not selected unchanged" in drafted.get("grain", {}).get("withheld", "")


def test_grain_evidence_identifies_exact_source_unambiguously():
    """#1252: evidence identifies exact source and never implies test execution."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
        tests: [unique]
""",
        "models/marts/fct_projects.sql": (
            "select project_id from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, _ = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    evidence = drafted["grain"]["evidence"]
    assert "passed" not in evidence.lower()
    assert "ran" not in evidence.lower()
    assert (
        evidence
        == "project_id has a unique test on stg_projects, carried through unchanged"
    )


def test_unprofiled_input_with_aliased_key_column_drafts_aliased_grain():
    """#1252: aliased key column carries forward the new alias as grain."""

    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
        tests: [unique]
""",
        "models/marts/fct_projects.sql": (
            "select project_id as pid, project_name from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, _ = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=None,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    assert drafted["grain"]["value"] == ["pid"]
    assert (
        drafted["grain"]["evidence"]
        == "project_id has a unique test on stg_projects, carried through unchanged"
    )


def test_contradictory_measured_profile_takes_precedence_over_declared_key():
    """#1252: profiled dataset with non-unique key does not fall back to stated key."""

    dataset = Dataset(
        identifier="warehouse.main.stg_projects",
        columns=[
            _col("project_id", "integer", is_unique=False, null_fraction=0.1),
        ],
    )
    cache = DexCache(datasets=[dataset])
    files = {
        "models/staging/_staging.yml": """
version: 2
models:
  - name: stg_projects
    columns:
      - name: project_id
        tests: [unique]
""",
        "models/marts/fct_projects.sql": (
            "select project_id from {{ ref('stg_projects') }}\n"
        ),
    }
    drafts, warnings = draft(
        ["fct_projects"],
        files,
        ["models"],
        cache=cache,
        dialect="duckdb",
    )
    drafted = drafts["fct_projects"]["drafted"]
    assert drafted.get("grain", {}).get("value") is None
    assert "no proven unique key" in drafted.get("grain", {}).get("withheld", "")
    assert any(
        "'fct_projects' declares no grain" in w and "dex drafted" not in w
        for w in warnings
    )
