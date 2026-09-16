"""The defect library: what it plants, what it refuses, and how it reads.

Everything here is pure. No dbt, no warehouse, no filesystem: a mutation is SQL
in and SQL out, which is what lets the defect taxonomy be checked exhaustively
and in three dialects without spending anything.

The assertions are deliberately about the *report* as much as the SQL. A mutant
whose defect sentence a reader cannot act on has failed at its job even if the
SQL it generated is perfect, so the wording is tested like any other output.
"""

from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")

from exmergo_dex_core.transform import mutation  # noqa: E402

ORDERS = "{{ ref('stg_orders') }}"
CUSTOMERS = "{{ ref('stg_customers') }}"


def _parents(dialect: str = "duckdb"):
    rendered = {
        "duckdb": ('"dev"."main"."stg_orders"', '"dev"."main"."stg_customers"'),
        "bigquery": ("`proj`.`dev`.`stg_orders`", "`proj`.`dev`.`stg_customers`"),
        "snowflake": ("DEV.MAIN.STG_ORDERS", "DEV.MAIN.STG_CUSTOMERS"),
    }[dialect]
    return [
        mutation.ParentRelation(rendered=rendered[0], jinja=ORDERS),
        mutation.ParentRelation(rendered=rendered[1], jinja=CUSTOMERS),
    ]


def _prepare(sql: str, dialect: str = "duckdb", parents=None):
    return mutation.prepare(
        sql,
        dialect=dialect,
        parents=_parents(dialect) if parents is None else parents,
    )


def _operators(batch):
    return [m.operator for m in batch.mutants]


def _of(batch, operator: str):
    return [m for m in batch.mutants if m.operator == operator]


# --- restoring references ------------------------------------------------------


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("duckdb", 'select a from "dev"."main"."stg_orders"'),
        ("bigquery", "select a from `proj`.`dev`.`stg_orders`"),
        ("snowflake", "select a from DEV.MAIN.STG_ORDERS"),
    ],
)
def test_a_compiled_relation_becomes_the_ref_it_came_from(dialect, sql):
    """dbt compiles a ref() into a physical name, and a unit test fixture binds
    to the ref. Without this the mutant would read the warehouse in a test that
    was meant to be reading fixtures, and would pass for the wrong reason."""

    prepared = _prepare(sql, dialect)
    assert ORDERS in mutation.render(prepared)
    assert "stg_orders" not in mutation.render(prepared).replace(ORDERS, "")


def test_an_unmatched_relation_is_left_alone_and_said_so():
    """dex would rather read a hardcoded table honestly than invent a reference
    the project never declared."""

    prepared = _prepare('select a from "dev"."main"."not_a_parent"')
    assert prepared.refs == {}
    assert any("not_a_parent" in note for note in prepared.notes)


def test_a_cte_name_is_not_mistaken_for_an_unmatched_relation():
    prepared = _prepare(
        'with base as (select a from "dev"."main"."stg_orders") select * from base'
    )
    assert prepared.notes == []


def test_an_inlined_ephemeral_parent_is_stripped_and_restored():
    """dbt hoists an ephemeral parent into the model's own SQL as a CTE. Left
    there, the mutant would carry a copy of the parent that no fixture can
    replace."""

    prepared = _prepare(
        "with __dbt__cte__stg_orders as (select 1 as a) "
        "select a from __dbt__cte__stg_orders",
        parents=[
            mutation.ParentRelation(
                rendered="__dbt__cte__stg_orders", jinja=ORDERS, ephemeral=True
            )
        ],
    )
    rendered = mutation.render(prepared)
    assert "__dbt__cte__" not in rendered
    assert ORDERS in rendered


def test_an_inlined_cte_dex_cannot_place_is_refused():
    with pytest.raises(mutation.MutationError, match="cannot match"):
        _prepare(
            "with __dbt__cte__mystery as (select 1 as a) "
            "select a from __dbt__cte__mystery",
            parents=[],
        )


def test_sql_that_is_not_one_query_is_refused():
    with pytest.raises(mutation.MutationError):
        _prepare("create table t as select 1")


def test_sql_that_will_not_parse_is_refused():
    with pytest.raises(mutation.MutationError, match="could not parse"):
        _prepare("select from where )(")


def test_every_literal_segment_is_wrapped_so_dbt_renders_none_of_it():
    """Compiled SQL is data. A `{{` inside a string literal would otherwise be
    rendered a second time by dbt, which is both a wrong mutant and a way to make
    dbt evaluate text that came out of a warehouse."""

    prepared = _prepare(
        'select \'{{ this_is_data }}\' as tag from "dev"."main"."stg_orders"'
    )
    rendered = mutation.render(prepared)
    assert "{% raw %}" in rendered and "{% endraw %}" in rendered
    body = rendered.split(ORDERS)[0]
    assert body.index("{% raw %}") < body.index("{{ this_is_data }}")


def test_sql_dex_cannot_quote_is_refused():
    prepared = _prepare("select 'endraw' as x")
    with pytest.raises(mutation.MutationError, match="endraw"):
        mutation.render(prepared)


def test_the_rendered_model_carries_the_ephemeral_header_last():
    """Appended, not prepended: dbt gives a scalar config key to the last
    config() call, so a model with its own materialized= would otherwise win and
    the mutant would build a relation."""

    rendered = mutation.render(_prepare("select 1 as a"))
    assert rendered.rstrip().endswith(mutation.EPHEMERAL_HEADER)
    assert "ephemeral" in mutation.EPHEMERAL_HEADER
    assert "'enforced': false" in mutation.EPHEMERAL_HEADER
    assert "access='protected'" in mutation.EPHEMERAL_HEADER


# --- the operators -------------------------------------------------------------


def test_a_boundary_comparison_flips_to_its_neighbour():
    batch = mutation.enumerate_mutants(_prepare("select a from t where a > 10"))
    mutant = _of(batch, "comparison")[0]
    assert mutant.before == "a > 10" and mutant.after == "a >= 10"
    assert "includes the edge value" in mutant.defect


def test_an_equality_is_not_treated_as_a_boundary():
    """Flipping = to <> is a different defect, and in a join condition it is one
    the warehouse rejects at once, so a run would be spent learning nothing."""

    batch = mutation.enumerate_mutants(_prepare("select a from t where a = 10"))
    assert "comparison" not in _operators(batch)


@pytest.mark.parametrize("clause", ["where", "having", "qualify"])
def test_a_predicate_is_dropped_and_negated_in_every_clause_that_filters(clause):
    batch = mutation.enumerate_mutants(
        _prepare(f"select a from t {clause} a <> 'x' and b > 1")  # noqa: S608
    )
    dropped = _of(batch, "predicate_drop")
    assert any(m.before == "a <> 'x'" and m.after == "" for m in dropped)
    negated = _of(batch, "predicate_negate")
    assert any(m.after.startswith("NOT (") for m in negated)


def test_dropping_the_only_predicate_removes_the_clause():
    batch = mutation.enumerate_mutants(_prepare("select a from t where a > 1"))
    body = _of(batch, "predicate_drop")[0].body
    assert "WHERE" not in body.upper()


@pytest.mark.parametrize(
    "written,expected_before,expected_after",
    [
        ("inner join", "inner join", "left join"),
        ("left join", "left join", "inner join"),
    ],
)
def test_a_join_swaps_type_in_both_directions(written, expected_before, expected_after):
    batch = mutation.enumerate_mutants(
        _prepare(f"select a from x {written} y on x.id = y.id")  # noqa: S608
    )
    mutant = _of(batch, "join_type")[0]
    assert mutant.before.startswith(expected_before)
    assert mutant.after.startswith(expected_after)


def test_a_join_defect_names_the_relation_not_an_internal_placeholder():
    """The placeholder is how a reference survives SQL generation. A finding
    that named it would tell the reader nothing they can act on."""

    batch = mutation.enumerate_mutants(
        _prepare(
            'select a from "dev"."main"."stg_orders" o '
            'inner join "dev"."main"."stg_customers" c on o.cid = c.id'
        )
    )
    mutant = _of(batch, "join_type")[0]
    assert "__dex_ref" not in mutant.defect
    assert "__dex_ref" not in mutant.before
    assert "stg_customers" in mutant.defect


def test_a_dropped_case_branch_does_not_suggest_accepted_values():
    """The dogfood case: a dropped branch sends its rows to a category that is
    still in the allowed list, so `accepted_values` passes and suggesting it
    would point the reader at a test they may already have."""

    batch = mutation.enumerate_mutants(
        _prepare("select case when a > 1 then 'x' else 'y' end as b from t")
    )
    suggestion = _of(batch, "case_branch")[0].suggested_test
    assert "accepted_values" not in suggestion
    assert "unit test" in suggestion


def test_a_case_branch_is_dropped_and_named_as_a_branch():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select case when a > 1 then 'x' when a > 2 then 'y' else 'z' end from t"
        )
    )
    mutant = _of(batch, "case_branch")[0]
    assert mutant.before == "when a > 1 then 'x'"
    assert not mutant.before.upper().startswith("CASE")


def test_the_only_case_branch_collapses_to_the_default():
    """An empty CASE is not printable, so the expression has to become whatever
    it would have fallen through to."""

    batch = mutation.enumerate_mutants(
        _prepare("select case when a > 1 then 'x' else 'z' end as b from t")
    )
    mutant = _of(batch, "case_branch")[0]
    assert mutant.after == "'z'"
    assert "CASE" not in mutant.body.upper()


def test_a_case_with_no_default_collapses_to_null():
    batch = mutation.enumerate_mutants(
        _prepare("select case when a > 1 then 'x' end as b from t")
    )
    assert _of(batch, "case_branch")[0].after.upper() == "NULL"


def test_a_division_is_inverted():
    batch = mutation.enumerate_mutants(_prepare("select num / den as rate from t"))
    mutant = _of(batch, "division")[0]
    assert mutant.after.replace(" ", "") == "den/num"
    assert "reciprocal" in mutant.defect


def test_a_guarded_division_moves_its_zero_check_with_the_denominator():
    """Snowflake's DIV0 does not survive a parse and a print: it comes back as a
    conditional testing the divisor by name. Swapping the operands underneath
    that guard would leave it checking a column that is no longer the divisor."""

    batch = mutation.enumerate_mutants(
        _prepare("select DIV0(num, den) as rate from t", "snowflake"),
        cap=20,
    )
    mutant = _of(batch, "division")[0]
    body = mutant.body
    assert "den = 0" not in body, body
    assert "num = 0" in body, body


def test_a_window_frame_bound_shifts_by_one():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over "
            "(order by d rows between 2 preceding and current row) from t"
        )
    )
    mutant = _of(batch, "window_frame")[0]
    assert "2 became 3" in mutant.defect


def test_a_frame_with_no_numeric_bound_is_not_a_site():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by d rows between unbounded preceding "
            "and current row) from t"
        )
    )
    assert "window_frame" not in _operators(batch)


@pytest.mark.parametrize(
    "written,expected", [("sum(a)", "MAX(a)"), ("max(a)", "SUM(a)")]
)
def test_an_aggregate_swaps_with_its_counterpart(written, expected):
    batch = mutation.enumerate_mutants(
        _prepare(f"select {written} from t group by b")  # noqa: S608
    )
    mutant = _of(batch, "aggregate")[0]
    assert mutant.after == expected
    assert "exactly one row" in mutant.defect


# --- ordering, the cap, and scope ----------------------------------------------


def test_the_cap_spreads_across_defect_classes_rather_than_truncating_one():
    """A model with many comparisons and one join must not spend a whole
    capped run on comparisons: the join defect is the more expensive bug."""

    sql = (
        "select a from x inner join y on x.id = y.id "
        "where a > 1 and b > 2 and c > 3 and d > 4 and e > 5"
    )
    batch = mutation.enumerate_mutants(_prepare(sql), cap=3)
    assert len(batch.mutants) == 3
    assert "join_type" in _operators(batch)
    assert len(set(_operators(batch))) == 3


def test_a_cap_that_binds_reports_what_it_cut_per_class():
    """A capped run that said nothing about the cap would read as 'everything
    was covered', and a total alone cannot tell 'no joins here' from 'the joins
    were cut off'."""

    sql = "select a from t where a > 1 and b > 2 and c > 3"
    full = mutation.enumerate_mutants(_prepare(sql))
    capped = mutation.enumerate_mutants(_prepare(sql), cap=2)
    assert capped.elided_total == len(full.mutants) - 2
    assert set(capped.elided) <= {"comparison", "predicate_drop", "predicate_negate"}


def test_the_cap_can_never_be_raised_above_the_engine_ceiling():
    """Every mutant is a dbt invocation, so the ceiling bounds wall time and
    spend; a caller may narrow it and nothing may widen it."""

    sql = "select a from t where " + " and ".join(  # noqa: S608
        f"c{i} > {i}" for i in range(40)
    )
    batch = mutation.enumerate_mutants(_prepare(sql), cap=999)
    assert len(batch.mutants) == mutation.MAX_MUTANTS


def test_mutants_never_compound():
    """Mutant seven has to be the model with one defect, not with seven, or a
    survivor says nothing about the defect it is named for."""

    batch = mutation.enumerate_mutants(
        _prepare("select a from t where a > 1 and b > 2")
    )
    for mutant in _of(batch, "comparison"):
        assert mutant.body.count(">=") == 1


def test_mutant_ids_are_stable_and_ordered():
    batch = mutation.enumerate_mutants(
        _prepare("select a from t where a > 1 and b > 2")
    )
    assert [m.id for m in batch.mutants] == [
        f"m{i:02d}" for i in range(1, len(batch.mutants) + 1)
    ]


def test_a_site_is_labelled_with_the_scope_its_author_would_name():
    batch = mutation.enumerate_mutants(
        _prepare(
            "with base as (select a from t where a > 1) select a from base where a > 2"
        )
    )
    scopes = {m.scope for m in _of(batch, "comparison")}
    assert scopes == {"base", "(final select)"}


def test_a_union_branch_is_named_by_its_position():
    batch = mutation.enumerate_mutants(
        _prepare("select a from t where a > 1 union all select a from u where a > 2")
    )
    assert any("branch" in m.scope for m in batch.mutants)


def test_every_mutant_suggests_the_test_that_would_catch_it():
    """The reader's next action is to write a test, so a finding that stops at
    'this survived' has stopped one step short of useful."""

    sql = (
        "select case when a > 1 then 'x' else 'y' end as b, sum(a) as t, n / d as r "
        "from x inner join y on x.id = y.id where a > 1"
    )
    batch = mutation.enumerate_mutants(_prepare(sql))
    assert batch.mutants
    for mutant in batch.mutants:
        assert mutant.suggested_test
        assert mutant.defect.strip()


def test_a_model_with_no_mutable_site_generates_nothing():
    batch = mutation.enumerate_mutants(_prepare("select a, b from t"))
    assert batch.mutants == [] and batch.considered == 0


# --- verdicts ------------------------------------------------------------------


def test_only_tests_that_passed_at_baseline_can_testify():
    """Counting an already-failing test as a catch would report a suite as
    strong precisely because it was broken."""

    baseline = {"a": "pass", "b": "fail"}
    verdict = mutation.classify(baseline, {"a": "pass", "b": "fail"})
    assert verdict.outcome == "survived"


def test_a_failing_test_kills_and_is_named():
    verdict = mutation.classify({"a": "pass", "b": "pass"}, {"a": "fail", "b": "pass"})
    assert verdict.outcome == "killed" and verdict.caught_by == ["a"]


def test_a_warning_test_kills_but_says_it_only_warned():
    verdict = mutation.classify({"a": "pass"}, {"a": "warn"})
    assert verdict.outcome == "killed" and verdict.warn_only


def test_a_mutant_the_warehouse_refused_is_rejected_rather_than_killed():
    """A mutant every test errors on is one a build would have failed on
    outright, so it is not evidence the tests would have caught it."""

    verdict = mutation.classify({"a": "pass"}, {"a": "error"})
    assert verdict.outcome == "rejected"


def test_a_run_that_produced_nothing_is_not_run():
    assert mutation.classify({"a": "pass"}, {}).outcome == "not_run"
    assert mutation.classify({"a": "pass"}, None).outcome == "not_run"


def test_a_run_missing_a_test_that_should_have_reported_is_not_run():
    """A test that vanished from the results was not answered, and reading its
    absence as silence would count it as failing to catch the defect."""

    verdict = mutation.classify({"a": "pass", "b": "pass"}, {"a": "pass"})
    assert verdict.outcome == "not_run"


# --- pricing support -----------------------------------------------------------


def test_a_mutant_is_spliced_into_the_test_that_reads_it():
    """Pricing needs the statement the warehouse will actually run: a mutant
    that drops a partition predicate scans more than the model it came from."""

    test_sql = (
        "with __dbt__cte__fct_orders as (select a from t where a > 1) "
        "select count(*) from __dbt__cte__fct_orders"
    )
    out = mutation.inline_into_test(
        test_sql, model_name="fct_orders", body="select a from t", dialect="duckdb"
    )
    assert out is not None and "a > 1" not in out


def test_splicing_a_test_that_does_not_read_the_model_returns_nothing():
    out = mutation.inline_into_test(
        "select 1", model_name="fct_orders", body="select a from t", dialect="duckdb"
    )
    assert out is None


def test_splicing_unparseable_test_sql_returns_nothing():
    """The caller prices that test at its baseline instead, which is the
    partial-floor convention the build estimate already follows."""

    out = mutation.inline_into_test(
        "not sql )(", model_name="fct_orders", body="select 1", dialect="duckdb"
    )
    assert out is None
