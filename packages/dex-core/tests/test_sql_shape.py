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


def test_scopes_includes_set_operation_ctes():
    sql = (
        "with named as (select 1 as a), "
        "residual as (select 2 as a), "
        "combined as (select * from named union all select * from residual) "
        "select * from combined"
    )
    scopes = sql_shape.scopes(_parse(sql))
    assert set(scopes) == {"named", "residual", "combined", sql_shape.MAIN_SCOPE}
    assert isinstance(scopes["combined"], sql_shape.SET_OPERATIONS)


def test_select_branches_flattens_set_operations():
    plain = _parse("select 1")
    assert len(sql_shape.select_branches(plain)) == 1

    union = _parse("select 1 union all select 2 union all select 3")
    branches = sql_shape.select_branches(union)
    assert len(branches) == 3
    assert [b.sql() for b in branches] == ["SELECT 1", "SELECT 2", "SELECT 3"]

    assert sql_shape.select_branches(None) == []


def test_from_relation_handles_non_select():
    assert sql_shape.from_relation(None) is None
    union = _parse("select 1 union all select 2")
    assert sql_shape.from_relation(union) is None


# --- a guarded cast where the dialect has no safe one (#1075) -----------------


@pytest.mark.parametrize(
    ("target", "low", "high"),
    [
        ("SMALLINT", -32768, 32767),
        ("INT", -2147483648, 2147483647),
        ("BIGINT", -9223372036854775808, 9223372036854775807),
    ],
)
def test_a_postgres_integer_cast_is_guarded_by_shape_and_range(target, low, high):
    sql = f"select try_cast(k as {target}) from t"  # noqa: S608
    tree = sqlglot.parse_one(sql, read="duckdb")
    rendered = sql_shape.with_safe_casts(tree, "postgres").sql(dialect="postgres")
    assert "TRY_CAST" not in rendered
    assert f"BETWEEN {low} AND {high}" in rendered
    assert "~ '" in rendered


def test_a_cast_nested_in_a_guarded_one_is_guarded_too():
    tree = sqlglot.parse_one(
        "select try_cast(try_cast(k as bigint) as int) from t", read="duckdb"
    )
    rendered = sql_shape.with_safe_casts(tree, "postgres").sql(dialect="postgres")
    assert "CAST(k AS BIGINT)" not in rendered
    assert "BETWEEN -9223372036854775808 AND 9223372036854775807" in rendered
    dated = sqlglot.parse_one(
        "select try_cast(try_cast(k as date) as int) from t", read="duckdb"
    )
    with pytest.raises(sql_shape.NoSafeCastError):
        sql_shape.with_safe_casts(dated, "postgres")


def test_a_postgres_cast_to_another_type_still_raises():
    for target in ("DATE", "DECIMAL(10, 2)", "DOUBLE", "TINYINT"):
        sql = f"select try_cast(k as {target}) from t"  # noqa: S608
        tree = sqlglot.parse_one(sql, read="duckdb")
        with pytest.raises(sql_shape.NoSafeCastError):
            sql_shape.with_safe_casts(tree, "postgres")


def test_the_range_test_casts_a_total_argument():
    """The NUMERIC cast inside the range test reads a CASE that falls back to
    '0', so a value the shape test rejects never reaches it (#307)."""

    tree = sqlglot.parse_one("select try_cast(k as int) from t", read="duckdb")
    guarded = sql_shape.with_safe_casts(tree, "postgres")
    (between,) = list(guarded.find_all(sqlglot.exp.Between))
    inner = between.this
    assert isinstance(inner, sqlglot.exp.Cast)
    assert inner.args["to"].is_type("decimal")
    assert isinstance(inner.this, sqlglot.exp.Case)
    assert inner.this.args["default"] == sqlglot.exp.Literal.string("0")


_SHARED_VALUES = [
    ("7", 7),
    (" -0042 ", -42),
    ("99999999999999999999999", None),
    ("12a", None),
    ("1.5", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize(
    ("target", "low", "high"),
    [
        ("SMALLINT", -(2**15), 2**15 - 1),
        ("INT", -(2**31), 2**31 - 1),
        ("BIGINT", -(2**63), 2**63 - 1),
    ],
)
def test_the_guarded_cast_returns_null_for_what_would_not_convert(target, low, high):
    """Evaluated on DuckDB for the CASE and range logic: what the cast would
    reject or overflow on is NULL, and every integer in range converts, up to
    each bound and not one past it. DuckDB's regex classes are ASCII only,
    unlike Postgres's locale-aware ones, so whitespace handling is proven by
    the live Postgres integration suite."""

    duckdb = pytest.importorskip("duckdb")
    values = [
        *_SHARED_VALUES,
        (f"+{high}", high),
        (str(low), low),
        (str(high + 1), None),
        (str(low - 1), None),
    ]
    sql = f"select try_cast(k as {target}) from t"  # noqa: S608
    tree = sqlglot.parse_one(sql, read="duckdb")
    guarded = sql_shape.with_safe_casts(tree, "postgres").sql(dialect="duckdb")
    # Postgres NUMERIC has no precision limit; DuckDB's bare DECIMAL is (18, 3).
    guarded = guarded.replace("AS DECIMAL)", "AS DECIMAL(38, 0))")
    rows = ", ".join("(NULL)" if v is None else f"('{v}')" for v, _ in values)
    sql = guarded.replace("FROM t", f"FROM (VALUES {rows}) AS t(k)")
    got = [row[0] for row in duckdb.connect().execute(sql).fetchall()]
    assert got == [expected for _, expected in values]
