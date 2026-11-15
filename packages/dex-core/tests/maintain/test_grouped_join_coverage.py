# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""Join coverage through a grouped CTE, measured over its groups (#1324).

The fixture is the common customer rollup: a parent roster left-joins one row
per child key, computed by a CTE grouped by that key. Each case builds real
DuckDB relations, writes the manifest dbt would have written and runs
``maintain verify`` end to end. A grouped shape dex does not carry stays
unchecked with its reason, and a supported one reports the grouped input's own
denominator (groups) beside the parent's rows.
"""

from __future__ import annotations

import pytest
import sqlglot

from exmergo_dex_core import sql_shape
from exmergo_dex_core.maintain import verify as verify_mod

from .test_join_coverage import W, _codes, _node, _records, _source_model, _verify
from .test_verify import _write_artifacts


@pytest.fixture(autouse=True)
def _assume_the_project_compiles(monkeypatch: pytest.MonkeyPatch):
    """These tests write the manifest dbt would have, so dbt's own parse of
    the fixture project is not what is under test."""

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )


_GROUPED = (
    "with orders_by_customer as ("
    "select customer_id, count(*) as order_count, sum(amount) as total_amount "
    f"from {W}.child_orders where status = 'paid' group by customer_id) "
    "select c.id as customer_id, c.name, o.order_count, o.total_amount "
    f"from {W}.roster c left join orders_by_customer o on c.id = o.customer_id"
)


def _sql(body: str) -> str:
    """The rollup model over ``body`` in place of the supported CTE body."""

    return (
        f"with orders_by_customer as ({body}) "
        "select c.id as customer_id, c.name, o.order_count, o.total_amount "
        f"from {W}.roster c left join orders_by_customer o on c.id = o.customer_id"
    )


def _values(rows: list[tuple]) -> str:
    return ", ".join(
        "(" + ", ".join("NULL" if value is None else repr(value) for value in row) + ")"
        for row in rows
    )


def _world(
    repo,
    *,
    roster: list[tuple],
    orders: list[tuple],
) -> None:
    """``roster``: (id, name); ``orders``: (customer_id, amount, status)."""

    repo.sql(
        "CREATE OR REPLACE TABLE roster (id INTEGER, name VARCHAR)",
        "CREATE OR REPLACE TABLE child_orders "
        "(customer_id INTEGER, amount DOUBLE, status VARCHAR)",
    )
    if roster:
        repo.sql(f"INSERT INTO roster VALUES {_values(roster)}")
    if orders:
        repo.sql(f"INSERT INTO child_orders VALUES {_values(orders)}")


def _model(repo, sql: str, **node) -> None:
    repo.sql(f"CREATE OR REPLACE TABLE rpt_customers AS {sql}")
    _write_artifacts(
        repo.project_dir,
        nodes={
            "model.maintain_test.rpt_customers": _node("rpt_customers", sql, **node)
        },
        results=[],
    )


def _joins(sql: str, dialect: str = "duckdb"):
    return verify_mod._model_joins(
        sqlglot.parse_one(sql, read=dialect), sql_shape, dialect
    )


# --- the shape: the grouped-key form is carried, other groupings stay unchecked --


def test_a_cte_grouped_by_the_join_key_is_carried_as_a_grouped_side():
    (candidate,), skipped = _joins(_GROUPED)
    assert skipped == []
    assert candidate.from_relation == "warehouse.main.roster"
    assert candidate.to_relation == "warehouse.main.child_orders"
    assert candidate.from_grouped is False
    assert candidate.to_grouped is True
    assert candidate.to_scopes == ("CTE `orders_by_customer`",)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (
            "select customer_id, count(*) as order_count, sum(amount) as total_amount "
            f"from {W}.child_orders where status = 'paid' "
            "group by customer_id having count(*) > 0",
            "HAVING",
        ),
        (
            "select customer_id, count(*) as order_count, sum(amount) as total_amount "
            f"from {W}.child_orders where status = 'paid' "
            "group by customer_id limit 10",
            "LIMIT",
        ),
        (
            "select customer_id, count(*) as order_count, sum(amount) as total_amount, "
            "row_number() over (order by customer_id) as rn "
            f"from {W}.child_orders where status = 'paid' group by customer_id",
            "window",
        ),
        (
            "select customer_id, status, count(*) as order_count, "
            f"sum(amount) as total_amount from {W}.child_orders "
            "where status = 'paid' group by customer_id, status",
            "grouping",
        ),
        (
            "select abs(customer_id) as customer_id, count(*) as order_count, "
            f"sum(amount) as total_amount from {W}.child_orders "
            "where status = 'paid' group by abs(customer_id)",
            "grouping expression",
        ),
    ],
)
def test_an_unsupported_grouped_shape_stays_unchecked(body: str, said: str):
    candidates, skipped = _joins(_sql(body))
    assert candidates == []
    assert len(skipped) == 1
    assert skipped[0].code == "filtered_cte"
    assert said.lower() in skipped[0].reason.lower()


def test_a_group_by_ordinal_and_a_projection_alias_resolve_to_the_key_column():
    sql = (
        "with grouped as (select customer_id as cid, count(*) as n "
        f"from {W}.child_orders where status = 'paid' group by 1) "
        "select c.id as customer_id, g.n "
        f"from {W}.roster c left join grouped g on c.id = g.cid"
    )
    (candidate,), skipped = _joins(sql)
    assert skipped == []
    assert candidate.to_grouped is True
    assert candidate.to_columns == ("customer_id",)

    aliased = sql.replace("group by 1", "group by cid")
    (candidate,), skipped = _joins(aliased)
    assert skipped == []
    assert candidate.to_grouped is True
    assert candidate.to_columns == ("customer_id",)


def test_a_grouping_over_a_filtered_input_carries_both_shapes():
    sql = (
        "with paid as ("
        f"select * from {W}.child_orders where status = 'paid'), "
        "grouped as ("
        "select customer_id, count(*) as n from paid group by customer_id) "
        "select c.id as customer_id, g.n "
        f"from {W}.roster c left join grouped g on c.id = g.customer_id"
    )
    (candidate,), skipped = _joins(sql)
    assert skipped == []
    assert candidate.to_grouped is True
    assert candidate.to_filters == ("\"status\" = 'paid'",)


# --- measured over groups, with the grouped input's own denominator --------------


def test_optional_parents_without_a_group_are_satisfied_and_named(
    maintain_repo, _assume_the_project_compiles
):
    _world(
        maintain_repo,
        roster=[(1, "Ada"), (2, "Grace"), (3, "Edsger")],
        orders=[(1, 10.0, "paid"), (1, 5.0, "paid"), (2, 7.0, "cancelled")],
    )
    _model(maintain_repo, _GROUPED)

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["status"] == "satisfied"
    assert record["optional_unmatched"] is True
    assert record["measured"]["rows"] == 3
    assert record["measured"]["matched_rows"] == 1
    assert record["measured"]["eligible_keys"] == 3
    assert record["measured"]["matched_keys"] == 1
    # The grouped side's denominator is groups, never the child's rows.
    assert record["measured"]["group_rows"] == 1
    assert record["effective_input"]["to"]["grouped_by"] == ["customer_id"]
    assert record["effective_input"]["to"]["relation"] == "warehouse.main.child_orders"
    assert _codes(data, "rpt_customers") == {}


def test_a_model_driven_from_a_grouped_cte_counts_its_groups(
    maintain_repo, _assume_the_project_compiles
):
    sql = (
        "with per_customer as ("
        "select customer_id, count(*) as order_count "
        f"from {W}.child_orders where status = 'paid' group by customer_id) "
        "select g.customer_id, g.order_count, c.name "
        f"from per_customer g left join {W}.roster c on g.customer_id = c.id"
    )
    _world(
        maintain_repo,
        roster=[(1, "Ada"), (2, "Grace")],
        orders=[(1, 10.0, "paid"), (1, 5.0, "paid"), (9, 4.0, "paid")],
    )
    maintain_repo.sql(f"CREATE OR REPLACE TABLE rpt_customers AS {sql}")
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.rpt_customers": _node("rpt_customers", sql)},
        results=[],
    )

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["status"] == "satisfied"
    assert record["optional_unmatched"] is True
    assert record["measured"]["rows"] == 2
    assert record["measured"]["group_rows"] == 2
    assert record["measured"]["matched_rows"] == 1
    assert record["effective_input"]["from"]["grouped_by"] == ["customer_id"]


def test_a_grouped_input_with_no_groups_is_not_applicable(
    maintain_repo, _assume_the_project_compiles
):
    _world(
        maintain_repo,
        roster=[(1, "Ada"), (2, "Grace")],
        orders=[(1, 10.0, "cancelled")],
    )
    _model(maintain_repo, _GROUPED)

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["status"] == "not_applicable"
    assert record["reason_code"] == "empty_input"
    assert record["measured"]["group_rows"] == 0
    assert _codes(data, "rpt_customers") == {}


def test_a_required_reference_counts_unmatched_parents_as_orphans(
    maintain_repo, _assume_the_project_compiles
):
    _world(
        maintain_repo,
        roster=[(1, "Ada"), (2, "Grace"), (3, "Edsger")],
        orders=[(1, 10.0, "paid")],
    )
    _model(
        maintain_repo,
        _GROUPED,
        columns={
            "customer_id": {
                "role": "foreign_key",
                "references": "child_orders.customer_id",
            }
        },
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.child_orders": _source_model("child_orders"),
            "model.maintain_test.rpt_customers": _node(
                "rpt_customers",
                _GROUPED,
                columns={
                    "customer_id": {
                        "role": "foreign_key",
                        "references": "child_orders.customer_id",
                    }
                },
            ),
        },
        results=[],
    )

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["declared"]["required"] is True
    assert record["status"] == "contradicted"
    assert _codes(data, "rpt_customers")["join_orphans"]["severity"] == "high"


def test_groups_whose_keys_the_roster_lacks_are_a_zero_match_join(
    maintain_repo, _assume_the_project_compiles
):
    _world(
        maintain_repo,
        roster=[(1, "Ada"), (2, "Grace")],
        orders=[(9, 10.0, "paid"), (8, 4.0, "paid")],
    )
    _model(maintain_repo, _GROUPED)

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["status"] == "contradicted"
    assert record["measured"]["group_rows"] == 2
    assert record["measured"]["matched_rows"] == 0
    assert _codes(data, "rpt_customers")["join_no_match"]["severity"] == "high"


def test_a_composite_grouped_key_is_carried_when_every_key_column_is_grouped(
    maintain_repo, _assume_the_project_compiles
):
    sql = (
        "with orders_by_customer as ("
        "select customer_id, region, count(*) as order_count "
        f"from {W}.child_orders where status = 'paid' "
        "group by customer_id, region) "
        "select c.id as customer_id, c.name, o.order_count "
        f"from {W}.roster c left join orders_by_customer o "
        "on c.id = o.customer_id and c.region = o.region"
    )
    maintain_repo.sql(
        "CREATE OR REPLACE TABLE roster (id INTEGER, name VARCHAR, region VARCHAR)",
        "CREATE OR REPLACE TABLE child_orders "
        "(customer_id INTEGER, region VARCHAR, amount DOUBLE, status VARCHAR)",
        "INSERT INTO roster VALUES (1, 'Ada', 'eu'), (2, 'Grace', 'us')",
        "INSERT INTO child_orders VALUES "
        "(1, 'eu', 10.0, 'paid'), (1, 'us', 5.0, 'paid'), "
        "(2, 'eu', 7.0, 'paid')",
        "CREATE OR REPLACE TABLE rpt_customers AS " + sql,
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.rpt_customers": _node("rpt_customers", sql)},
        results=[],
    )

    data = _verify(maintain_repo, "rpt_customers")
    (record,) = _records(data, "rpt_customers")

    assert record["status"] == "satisfied"
    assert record["effective_input"]["to"]["grouped_by"] == ["customer_id", "region"]
    assert record["measured"]["group_rows"] == 3
    assert record["measured"]["matched_rows"] == 1


def test_the_probe_measures_the_grouped_inputs_groups_and_matches():
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.cache import Relationship, RelationshipKind
    from exmergo_dex_core.explore import relationships as rel_mod

    conn = duckdb.connect()
    conn.execute("CREATE TABLE p (id INTEGER)")
    conn.execute("INSERT INTO p VALUES (1), (2), (3)")
    conn.execute("CREATE TABLE c (pid INTEGER, amount DOUBLE, status VARCHAR)")
    conn.execute(
        "INSERT INTO c VALUES "
        "(1, 10.0, 'paid'), (1, 5.0, 'paid'), (2, 7.0, 'cancelled'), "
        "(9, 1.0, 'paid')"
    )
    rel = Relationship(
        from_dataset="p",
        from_columns=["id"],
        to_dataset="c",
        to_columns=["pid"],
        kind=RelationshipKind.DECLARED,
    )
    sql = rel_mod.coverage_probe_sql(
        [rel],
        grouped_sides={id(rel): (False, True)},
        scope_filters={id(rel): (None, "\"status\" = 'paid'")},
    )
    row = dict(
        zip(
            [description[0] for description in conn.execute(sql).description],
            conn.execute(sql).fetchone(),
            strict=True,
        )
    )
    assert row["dex_cov_rows_0"] == 3
    assert row["dex_cov_matched_0"] == 1
    assert row["dex_cov_to_groups_0"] == 2


def test_a_grouping_on_only_one_key_column_of_a_composite_join_is_unchecked():
    sql = (
        "with orders_by_customer as ("
        "select customer_id, region, count(*) as order_count "
        f"from {W}.child_orders where status = 'paid' group by customer_id) "
        "select c.id as customer_id, c.name, o.order_count "
        f"from {W}.roster c left join orders_by_customer o "
        "on c.id = o.customer_id and c.region = o.region"
    )
    candidates, skipped = _joins(sql)
    assert candidates == []
    assert skipped[0].code == "filtered_cte"
    assert "exactly the join's key columns" in skipped[0].reason
