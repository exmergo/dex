"""`transform plan` drafts the measured half of each model's declaration (#493).

The unit half drives :func:`drafting.draft` over a hand-built cache, one case
per rule, because each rule is a claim about what the cache can and cannot
support and a hand-built cache states the premise exactly. The end-to-end half
runs the shipped commands over a real DuckDB warehouse, so the cache it drafts
from is the one `explore` writes.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _draft(sql: str, *, yml: str | None = None, cache: DexCache | None = CACHE):
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
    assert entry["drafted"]["columns"]["order_amount"]["from"] == "stg_orders.amount"


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
