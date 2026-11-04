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


@pytest.mark.parametrize(
    "sql",
    [
        (
            "with t as ("
            "    select 1 as id, 10 as amount "
            "    union all select 2, 200"
            ") "
            "select id, amount from t where amount > 5"
        ),
        (
            "with t as ("
            "    select 1 as id, 10 as amount "
            "    union select 2, 200"
            ") "
            "select id, amount from t where amount > 5"
        ),
        (
            "with outer_cte as ("
            "    with inner_cte as (select 1 as id) "
            "    select id from inner_cte"
            ") "
            "select id from outer_cte"
        ),
    ],
)
def test_a_cte_with_set_operation_or_nested_is_not_mistaken_for_unmatched_relation(
    sql,
):
    prepared = _prepare(sql)
    assert not any(
        "matches none of this model's declared parents" in note
        for note in prepared.notes
    )


def test_unmatched_relation_still_reported_alongside_set_operation_cte():
    sql = (
        "with t as ("
        "    select 1 as id "
        "    union all select 2"
        ") "
        'select t.id, o.a from t cross join "dev"."main"."not_a_parent" o'
    )
    prepared = _prepare(sql)
    assert any("not_a_parent" in note for note in prepared.notes)
    assert not any("t matches none" in note for note in prepared.notes)


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


def test_the_issues_reproduction_now_finds_the_interval_bound():
    """#1112's parser-only reproduction: a numeric ROWS bound was one site and
    the same window written as a day interval was none."""

    rows = (
        "select sum(v) over (order by d rows between 27 preceding and current row) "
        "from t"
    )
    days = (
        "select sum(v) over (order by d range between interval '27 day' preceding "
        "and current row) from t"
    )
    assert len(mutation._window_sites(sqlglot.parse_one(rows, read="duckdb"))) == 1
    assert len(mutation._window_sites(sqlglot.parse_one(days, read="duckdb"))) == 2


_INTERVAL_FRAME = (
    "select region, d, sum(a) over (partition by region order by d "
    "range between {bound} preceding and current row) as trailing "
    "from t where a > 0"
)


@pytest.mark.parametrize(
    "dialect,bound,widened,narrowed",
    [
        ("duckdb", "interval '6 day'", "INTERVAL '7' DAY", "INTERVAL '5' DAY"),
        ("duckdb", "interval 6 days", "INTERVAL '7' DAYS", "INTERVAL '5' DAYS"),
        ("snowflake", "interval '6 days'", "INTERVAL '7 DAYS'", "INTERVAL '5 DAYS'"),
        ("postgres", "interval '6 days'", "INTERVAL '7 DAYS'", "INTERVAL '5 DAYS'"),
    ],
)
def test_an_interval_bound_is_widened_and_narrowed_keeping_its_unit(
    dialect, bound, widened, narrowed
):
    """Both calendar boundary questions get a mutant of their own, and only the
    quantity moves: the unit as written, the partition, the ordering and every
    unrelated clause are what they were."""

    batch = mutation.enumerate_mutants(
        _prepare(_INTERVAL_FRAME.format(bound=bound), dialect, parents=[])
    )
    frames = _of(batch, "window_frame")
    assert [m.after.split(" preceding")[0].split("BETWEEN ")[1] for m in frames] == [
        widened,
        narrowed,
    ]
    for mutant in frames:
        assert "PARTITION BY region ORDER BY d" in mutant.compiled
        assert "a > 0" in mutant.body
        sqlglot.parse_one(
            mutation._PLACEHOLDER_RE.sub("x", mutant.compiled), read=dialect
        )
    assert "one day further back (6 days became 7 days)" in frames[0].defect
    assert "first day it should exclude" in frames[0].defect
    assert "one day short (6 days became 5 days)" in frames[1].defect
    assert "last day it should include" in frames[1].defect


def test_an_interval_mutant_names_the_boundary_a_fixture_must_reach():
    batch = mutation.enumerate_mutants(
        _prepare(_INTERVAL_FRAME.format(bound="interval '6 day'"), parents=[])
    )
    widen, narrow = _of(batch, "window_frame")
    assert widen.frame == {
        "kind": "range",
        "bound": "start",
        "side": "preceding",
        "value": 6,
        "unit": "day",
        "from": 6,
        "to": 7,
        "last_included": 6,
        "first_excluded": 7,
    }
    assert narrow.frame["to"] == 5
    assert widen.payload()["frame"] == widen.frame
    for mutant in (widen, narrow):
        assert "6 days before the current row" in mutant.suggested_test
        assert "7 days before it" in mutant.suggested_test
        assert "gap" in mutant.suggested_test
    # The label does not replace the calendar advice: a data assertion rarely
    # pins a boundary, whichever way the dev data fell.
    for label in ("equivalent", "distinguishable", None):
        assert "gap" in mutation.suggested_test(
            "window_frame", label, frame=widen.frame
        )


def test_a_following_interval_bound_says_ahead():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by d range between current row and "
            "interval '3 day' following) from t",
            parents=[],
        )
    )
    widen, _narrow = _of(batch, "window_frame")
    assert "one day further ahead" in widen.defect
    assert "3 days after the current row" in widen.suggested_test


def test_a_zero_interval_is_widened_but_never_narrowed_below_zero():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by d range between interval '0 day' "
            "preceding and current row) from t",
            parents=[],
        )
    )
    frames = _of(batch, "window_frame")
    assert len(frames) == 1 and frames[0].frame["to"] == 1


def test_a_numeric_frame_keeps_its_single_mutant_and_its_wording():
    """The numeric behavior #1112 asks to keep: one +1 mutant per frame, and a
    ROWS sentence that counts rows. A numeric RANGE bound counts the ordering
    value, so it no longer claims a row."""

    rows = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by d rows between 2 preceding and "
            "current row) from t"
        )
    )
    ranged = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by n range between 2 preceding and "
            "current row) from t"
        )
    )
    assert len(_of(rows, "window_frame")) == 1
    assert "one row further" in _of(rows, "window_frame")[0].defect
    assert (
        _of(rows, "window_frame")[0].suggested_test
        == (mutation._SUGGESTED_TEST["window_frame"])
    )
    (ranged_mutant,) = _of(ranged, "window_frame")
    assert "one unit of the ordering value" in ranged_mutant.defect
    assert "row further" not in ranged_mutant.defect


@pytest.mark.parametrize(
    "bound,code",
    [
        ("interval (lookback) day", "non_literal_offset"),
        ("interval '1.5' day", "fractional_offset"),
        ("interval '1 day 2 hours'", "compound_interval"),
        ("'6'", "string_offset"),
        ("2.5", "fractional_offset"),
        ("interval '-2' day", "negative_offset"),
        ("to_days(6)", "non_literal_offset"),
    ],
)
def test_a_bound_dex_cannot_shift_is_listed_and_never_mutated(bound, code):
    """#1112: an unsupported bound never implies its boundary was tested. It is
    no mutant and no part of the cap, and it is named with its reason."""

    batch = mutation.enumerate_mutants(
        _prepare(_INTERVAL_FRAME.format(bound=bound), parents=[])
    )
    assert "window_frame" not in _operators(batch)
    assert [entry["reason_code"] for entry in batch.unsupported] == [code]
    entry = batch.unsupported[0]
    assert entry["operator"] == "window_frame"
    assert entry["scope"] == "(final select)"
    assert entry["side"] == "preceding"
    assert entry["reason"]
    assert batch.considered == len(batch.mutants)


def test_the_bound_a_frame_does_not_shift_is_listed_too():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select sum(a) over (order by d rows between 2 preceding and 3 following) "
            "from t"
        )
    )
    assert len(_of(batch, "window_frame")) == 1
    assert "2 became 3" in _of(batch, "window_frame")[0].defect
    assert [(e["side"], e["reason_code"]) for e in batch.unsupported] == [
        ("following", "other_bound_of_mutated_frame")
    ]


def test_unbounded_and_current_row_are_neither_sites_nor_unsupported():
    batch = mutation.enumerate_mutants(
        _prepare(
            "select count(a) over (order by d rows between unbounded preceding and "
            "current row) from t"
        )
    )
    assert batch.mutants == [] and batch.unsupported == []


def test_interval_mutants_share_the_cap_like_any_other_class():
    """Two mutants per interval frame are two cap slots, spread round robin
    and counted per class when the cap cuts them."""

    sql = (
        "select sum(a) over (order by d range between interval '6 day' preceding "
        "and current row) as w1, sum(a) over (order by d range between "
        "interval '13 day' preceding and current row) as w2 from t where a > 1"
    )
    full = mutation.enumerate_mutants(_prepare(sql, parents=[]))
    assert len(_of(full, "window_frame")) == 4
    assert full.considered == len(full.mutants)
    capped = mutation.enumerate_mutants(_prepare(sql, parents=[]), cap=3)
    assert "window_frame" in _operators(capped)
    assert capped.elided_total == full.considered - 3
    assert capped.elided.get("window_frame", 0) >= 2


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


# --- equivalence on the dev data -----------------------------------------------


def _as_sql(node: str) -> str:
    """The comparison node with its jinja replaced by plain relation names."""

    import re

    return re.sub(r"\{\{\s*ref\('([^']+)'\)\s*\}\}", r"\1", node.split("{{ config")[0])


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery", "snowflake", "postgres"])
def test_the_comparison_projects_four_counts_and_nothing_else(dialect):
    """The structural half of the no-row-value guarantee: whatever the model
    holds, the statement can only return the four aggregates it projects."""

    node = mutation.equivalence_model("{{ ref('fct') }}", dialect)
    assert node is not None
    assert "{{ ref('fct') }}" in node
    assert f"{{{{ ref('{mutation.BASELINE_TWIN}') }}}}" in node
    assert node.rstrip().endswith(mutation.EPHEMERAL_HEADER)
    tree = sqlglot.parse_one(_as_sql(node), read=dialect)
    assert tuple(tree.named_selects) == mutation.EQUIVALENCE_COUNTS
    for projection in tree.expressions:
        assert projection.find(sqlglot.exp.AggFunc) is not None


@pytest.mark.parametrize("dialect", ["redshift", "databricks", "clickhouse", "trino"])
def test_a_warehouse_without_a_verified_fingerprint_gets_no_comparison(dialect):
    """A fingerprint that silently skipped a column would label a mutant that
    changes it as equivalent, so an unverified one is never guessed at."""

    assert mutation.equivalence_model("{{ ref('fct') }}", dialect) is None


def test_the_twin_carries_the_models_group():
    """The identity is compiled SQL, so the model's own config() calls are gone,
    and a twin outside the group cannot ref a parent private to it."""

    assert mutation.twin_model("select 1\n") == "select 1\n"
    twin = mutation.twin_model("select 1\n", group="finance")
    assert twin.endswith('{{ config(group="finance") }}\n')


def _row(**overrides):
    row = {
        "mutant_rows": 5,
        "baseline_rows": 4,
        "only_in_mutant": 1,
        "only_in_baseline": 0,
    }
    row.update(overrides)
    return [row]


def test_a_zero_difference_is_equivalent_and_carries_its_counts():
    label = mutation.read_equivalence(
        _row(mutant_rows=4, only_in_mutant=0, baseline_rows=4)
    )
    assert label is not None and label.status == "equivalent"
    assert label.payload()["baseline_rows"] == 4
    assert label.payload()["only_in_mutant"] == 0


def test_any_difference_is_distinguishable():
    label = mutation.read_equivalence(_row())
    assert label is not None and label.status == "distinguishable"
    changed = mutation.read_equivalence(
        _row(mutant_rows=4, only_in_mutant=2, only_in_baseline=2)
    )
    assert changed is not None and changed.status == "distinguishable"


def test_an_upper_cased_or_float_spelled_row_is_still_read():
    """Snowflake upper-cases an unquoted alias, and a preview may print a whole
    number as a float."""

    rows = [
        {
            "MUTANT_ROWS": 4.0,
            "BASELINE_ROWS": 4,
            "ONLY_IN_MUTANT": 0,
            "ONLY_IN_BASELINE": 0.0,
        }
    ]
    label = mutation.read_equivalence(rows)
    assert label is not None and label.status == "equivalent"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        None,
        "not rows",
        _row() + _row(),
        [{**_row()[0], "email": "someone@example.com"}],
        _row(only_in_mutant="someone@example.com"),
        _row(only_in_mutant=-1),
        _row(only_in_mutant=True),
        _row(only_in_mutant=1.5),
        _row(mutant_rows=9),
    ],
)
def test_anything_but_four_consistent_counts_is_refused(rows):
    """Anything that is not exactly the comparison's own shape is treated as a
    comparison that did not run, never surfaced: that is what keeps a value
    that is not a count out of the envelope even if the SQL were wrong."""

    assert mutation.read_equivalence(rows) is None


def test_every_operator_suggests_a_fixture_or_an_assertion_by_its_label():
    """An equivalent survivor needs a fixture that reaches the case; one the data
    already tells apart needs an assertion over that data. The same sentence
    for both would send half the readers to write the wrong test."""

    for operator in (op.name for op in mutation._OPERATORS):
        fixture = mutation.suggested_test(operator, "equivalent")
        assertion = mutation.suggested_test(operator, "distinguishable")
        assert fixture and assertion and fixture != assertion
        assert "unit test" in fixture
        assert (
            mutation.suggested_test(operator, None)
            == mutation._SUGGESTED_TEST[operator]
        )


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


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery", "snowflake"])
def test_every_mutant_splices_into_a_test_as_the_sql_the_warehouse_runs(dialect):
    """The mutant's dbt file text is not SQL: it carries `{% raw %}` blocks and
    `{{ ref() }}` calls, so splicing it failed to parse and every mutant was
    priced at the baseline's cost. Its compiled spelling names the physical
    relations the test SQL around it already uses."""

    orders = _parents(dialect)[0].rendered
    model = f"select a from {orders} where a > 1 and b > 2"  # noqa: S608
    prepared = _prepare(model, dialect)
    batch = mutation.enumerate_mutants(prepared)
    test_sql = f"with __dbt__cte__fct as ({model}) select 1 from __dbt__cte__fct"  # noqa: S608
    assert batch.mutants
    for mutant in batch.mutants:
        spliced = mutation.inline_into_test(
            test_sql, model_name="fct", body=mutant.compiled, dialect=dialect
        )
        assert spliced is not None, mutant.id
        assert "{%" not in spliced and "__dex_ref_" not in spliced
        assert "stg_orders" in spliced.lower()
    dropped = _of(batch, "predicate_drop")[0]
    assert "a > 1" not in dropped.compiled.lower()
    assert "a > 1" in batch.identity_compiled.lower()


def test_the_compiled_spelling_names_an_ephemeral_parent_by_its_hoisted_cte():
    """dbt hoists every ephemeral ancestor into each test as a sibling CTE, so
    the compiled mutant reads the parent under that alias rather than inlining
    a copy of it."""

    prepared = _prepare(
        "with __dbt__cte__stg_orders as (select 1 as a) "
        "select a from __dbt__cte__stg_orders where a > 0",
        parents=[
            mutation.ParentRelation(
                rendered="__dbt__cte__stg_orders", jinja=ORDERS, ephemeral=True
            )
        ],
    )
    compiled = prepared.compiled()
    assert "__dbt__cte__stg_orders" in compiled
    assert "select 1" not in compiled.lower()
    assert "{{" not in compiled


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


# --- downstream tests (#648) ---------------------------------------------------


def test_a_catch_only_downstream_is_kept_apart_from_a_kill():
    """The defect could not ship, but the model's own suite missed it, and the
    per-model score has to keep saying so."""

    baseline = {"own": "pass", "mart": "pass"}
    verdict = mutation.classify(
        baseline, {"own": "pass", "mart": "fail"}, downstream={"mart"}
    )
    assert verdict.outcome == "caught_downstream"
    assert verdict.caught_by == []
    assert verdict.caught_downstream == ["mart"]


def test_a_kill_by_the_models_own_test_still_names_the_downstream_catch():
    verdict = mutation.classify(
        {"own": "pass", "mart": "pass"},
        {"own": "fail", "mart": "fail"},
        downstream={"mart"},
    )
    assert verdict.outcome == "killed"
    assert verdict.caught_by == ["own"]
    assert verdict.caught_downstream == ["mart"]


def test_a_downstream_warning_is_a_warn_only_catch():
    verdict = mutation.classify(
        {"own": "pass", "mart": "pass"},
        {"own": "pass", "mart": "warn"},
        downstream={"mart"},
    )
    assert verdict.outcome == "caught_downstream"
    assert verdict.warn_only is True


def test_a_downstream_error_with_no_catch_is_rejected_not_survived():
    """A mutant a downstream model cannot even run on would fail the build."""

    verdict = mutation.classify(
        {"own": "pass", "mart": "pass"},
        {"own": "pass", "mart": "error"},
        downstream={"mart"},
    )
    assert verdict.outcome == "rejected"


def test_without_downstream_tests_classify_is_what_it_was():
    verdict = mutation.classify({"a": "pass", "b": "pass"}, {"a": "pass", "b": "fail"})
    assert verdict.outcome == "killed"
    assert verdict.caught_downstream == []


def _graph(nodes: dict[str, dict], edges: dict[str, list[str]]) -> dict:
    full = {
        uid: {
            "name": uid.split(".")[-1],
            "resource_type": uid.split(".")[0],
            "package_name": "p",
            "language": "sql",
            "config": {"materialized": "table"},
            **extra,
        }
        for uid, extra in nodes.items()
    }
    return {"metadata": {"project_name": "p"}, "nodes": full, "child_map": edges}


def test_the_closure_walks_nearest_first_and_visits_a_diamond_once():
    manifest = _graph(
        {"model.p.int": {}, "model.p.b": {}, "model.p.a": {}, "model.p.mart": {}},
        {
            "model.p.int": ["model.p.b", "model.p.a", "test.p.t1"],
            "model.p.a": ["model.p.mart"],
            "model.p.b": ["model.p.mart"],
        },
    )
    closure = mutation.downstream_closure(manifest, "model.p.int")
    assert closure.eligible == ["model.p.a", "model.p.b", "model.p.mart"]
    assert closure.cut == [] and closure.elided == 0


@pytest.mark.parametrize(
    "uid,extra,reason",
    [
        ("snapshot.p.snap", {}, "snapshot"),
        ("model.p.py", {"language": "python"}, "Python model"),
        ("model.p.pkg", {"package_name": "other"}, "installed package"),
        ("model.p.hdr", {"config": {"sql_header": "set x = 1"}}, "sql_header"),
    ],
)
def test_a_node_that_cannot_carry_the_mutant_cuts_its_whole_branch(uid, extra, reason):
    """Below it, every model reads its relation in the warehouse, which holds
    the unmutated rows, so walking on would route nothing."""

    manifest = _graph(
        {"model.p.int": {}, uid: extra, "model.p.below": {}},
        {"model.p.int": [uid], uid: ["model.p.below"]},
    )
    closure = mutation.downstream_closure(manifest, "model.p.int")
    assert closure.eligible == []
    assert [entry["unique_id"] for entry in closure.cut] == [uid]
    assert reason in closure.cut[0]["reason"]


def test_the_cap_keeps_the_nearest_models_and_counts_the_rest():
    manifest = _graph(
        {"model.p.int": {}, "model.p.a": {}, "model.p.b": {}, "model.p.deep": {}},
        {"model.p.int": ["model.p.a", "model.p.b"], "model.p.a": ["model.p.deep"]},
    )
    closure = mutation.downstream_closure(manifest, "model.p.int", cap=2)
    assert closure.eligible == ["model.p.a", "model.p.b"]
    assert closure.elided == 1
    assert closure.cut[0]["name"] == "deep"
    assert "cap of 2" in closure.cut[0]["reason"]
