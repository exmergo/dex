"""The row-affecting shape of one SELECT: what drives it, what it joins, and
what gives it an honest reason to hold a different number of rows.

Two callers depend on these answers agreeing (`transform plan`'s row-population
attribution and `maintain verify`'s row-loss check), and both are one sqlglot
major away from reading a renamed argument key, which is the hazard this module
exists to absorb. So the assertions here are deliberately about the shapes
rather than about either caller's use of them.
"""

from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")

from exmergo_dex_core import sql_shape  # noqa: E402

DBT_SHAPED = """
with source as (
    select * from "wh"."main"."orders"
),
filtered as (
    select * from source where status <> 'cancelled'
),
final as (
    select f.order_id, c.name
    from filtered f
    left join "wh"."main"."customers" c on f.customer_id = c.id
)
select * from final
"""


def _parse(sql: str):
    return sqlglot.parse_one(sql, read="duckdb")


def test_scopes_names_every_top_level_cte_and_the_final_select():
    scopes = sql_shape.scopes(_parse(DBT_SHAPED))
    assert set(scopes) == {"source", "filtered", "final", sql_shape.MAIN_SCOPE}


def test_from_relation_reads_the_driving_relation():
    """The assertion that survives the `from` to `from_` rename. A version that
    reads only one spelling returns None here and every caller goes quiet."""

    tree = _parse('select * from "wh"."main"."orders"')
    assert sql_shape.from_relation(tree) is not None
    assert sql_shape.from_relation(tree).name == "orders"


def test_from_relation_of_a_dbt_shaped_model_names_the_cte_not_a_table():
    """Why callers have to walk rather than read once: the final select of a
    normal dbt model names an internal CTE, never the source table."""

    tree = _parse(DBT_SHAPED)
    assert sql_shape.from_relation(tree).name == "final"


def test_joins_carry_their_side_and_condition():
    joins = sql_shape.joins(sql_shape.scopes(_parse(DBT_SHAPED))["final"])
    assert [j.side for j in joins] == ["left"]
    assert joins[0].relation.name == "customers"
    assert sql_shape.equality_columns(joins[0].on) == ["f.customer_id = c.id"]


def test_a_join_with_no_stated_side_is_an_inner_join():
    tree = _parse("select * from a join b on a.id = b.id")
    assert sql_shape.joins(tree)[0].side == "inner"


def test_equality_columns_keeps_the_key_and_drops_the_rest():
    """An ON carrying a range predicate beside the key would otherwise report
    the noise alongside the one part that identifies the grain."""

    tree = _parse("select * from a join b on a.id = b.id and b.valid_from < a.at")
    assert sql_shape.equality_columns(sql_shape.joins(tree)[0].on) == ["a.id = b.id"]


def test_equality_columns_of_no_condition_is_empty():
    assert sql_shape.equality_columns(None) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select * from t where a = 1", "a WHERE filter"),
        ("select a, count(*) from t group by a", "a GROUP BY"),
        ("select a, count(*) from t group by a having count(*) > 1", "a HAVING filter"),
        ("select distinct a from t", "a DISTINCT"),
        ("select * from t limit 10", "a LIMIT"),
        (
            "select * from t qualify row_number() over (order by a) = 1",
            "a QUALIFY filter",
        ),
        ("select * from t semi join u on t.id = u.id", "a semi join"),
        ("select * from t anti join u on t.id = u.id", "a anti join"),
    ],
)
def test_reducing_clauses_names_every_honest_reason_to_hold_fewer_rows(
    sql: str, expected: str
):
    assert expected in sql_shape.reducing_clauses(_parse(sql))


def test_a_plain_projection_has_no_reason_to_hold_fewer_rows():
    assert sql_shape.reducing_clauses(_parse("select a, b from t")) == []


def test_an_ordinary_join_is_not_a_reducing_clause():
    """An inner join can drop rows and frequently does, but it is not a
    declaration that it should: that is the whole finding."""

    assert (
        sql_shape.reducing_clauses(_parse("select * from t join u on t.a = u.a")) == []
    )


def test_a_row_multiplier_is_recognized():
    tree = _parse("select t.id, x from t cross join unnest(t.tags) as x")
    assert sql_shape.multiplies_rows(tree) is True
    assert sql_shape.multiplies_rows(_parse("select * from t")) is False


def test_set_operations_are_recognized_under_either_class_layout():
    """sqlglot collapsed Union/Intersect/Except under one base between majors,
    so this asserts the tuple is populated at all, then that it matches."""

    assert sql_shape.SET_OPERATIONS
    assert isinstance(
        _parse("select a from t union all select a from u"), sql_shape.SET_OPERATIONS
    )


def test_relation_key_ignores_an_alias():
    aliased = sql_shape.from_relation(_parse("select * from orders o"))
    bare = sql_shape.from_relation(_parse("select * from orders"))
    assert sql_shape.relation_key(aliased) == sql_shape.relation_key(bare)


def test_names_a_cte_separates_an_internal_name_from_a_table():
    tree = _parse(DBT_SHAPED)
    ctes = set(sql_shape.scopes(tree)) - {sql_shape.MAIN_SCOPE}
    assert sql_shape.names_a_cte(sql_shape.from_relation(tree), ctes) is True
    final = sql_shape.scopes(tree)["final"]
    assert sql_shape.names_a_cte(sql_shape.joins(final)[0].relation, ctes) is False


def test_predicates_flatten_across_top_level_ands():
    tree = _parse("select * from t where a = 1 and b = 2 and c = 3")
    assert len(sql_shape.predicates(tree, "where")) == 3


def test_set_predicates_round_trips_what_predicates_flattened():
    """The pair has to compose: whatever the reader splits, the writer must put
    back unchanged, or a caller that drops one predicate silently rewrites the
    rest of the clause too."""

    tree = _parse("select * from t where a = 1 and b = 2 and c = 3")
    sql_shape.set_predicates(tree, "where", sql_shape.predicates(tree, "where"))
    assert [sql_shape.text(p) for p in sql_shape.predicates(tree, "where")] == [
        "a = 1",
        "b = 2",
        "c = 3",
    ]


def test_set_predicates_removes_the_clause_when_nothing_is_left():
    """An empty WHERE wrapper is not printable, so dropping the last predicate
    has to drop the clause itself."""

    tree = _parse("select * from t where a = 1")
    sql_shape.set_predicates(tree, "where", [])
    assert "where" not in tree.sql().lower()


@pytest.mark.parametrize("clause", ["where", "having", "qualify"])
def test_set_predicates_rebuilds_each_clause_it_can_read(clause):
    tree = _parse(f"select a from t {clause} a = 1 and b = 2")  # noqa: S608
    preds = sql_shape.predicates(tree, clause)
    sql_shape.set_predicates(tree, clause, [preds[0]])
    assert [sql_shape.text(p) for p in sql_shape.predicates(tree, clause)] == ["a = 1"]


def test_group_by_reports_its_expressions_as_written():
    tree = _parse("select a, count(*) from t group by a")
    assert sql_shape.group_by(tree) == ["a"]
