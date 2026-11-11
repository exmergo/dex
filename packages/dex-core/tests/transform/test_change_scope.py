# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""`transform plan` says what an edit changes about each model it rewrites beside
the request, and which of those changes no assumption records (#1115).

Two failures motivate it: a filter meant for the aggregates that also lands on a
roster model, and a coverage repair that also changes what an empty window
counts. Both pass every other plan-time check. The unit tests below read the
analysis directly; the last ones drive the CLI over a small DuckDB project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform import change_scope as cs
from exmergo_dex_core.transform.completion import BuildNode, summarize_completion

from .test_build_completion import _evidence, _uid

PATH = "models/marts/site_daily.sql"
PRIOR = (
    "with v as (\n"
    "  select visit_date, count(*) as visits, avg(duration) as avg_duration\n"
    "  from {{ ref('stg_visits') }} group by visit_date\n"
    ")\n"
    "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id\n"
    "from v\n"
)


def _scope(authored: str, assumptions: list | None = None, prior: str = PRIOR):
    return cs.scope_for(PATH, prior, authored, "duckdb", assumptions or [])


def _kinds(scope) -> list[tuple[str, str | None, str | None]]:
    return [(c.kind, c.column, c.status) for c in scope.changes]


# --- outputs ---------------------------------------------------------------------


def test_count_star_to_count_column_in_a_cte_is_an_aggregate_change():
    scope = _scope(PRIOR.replace("count(*)", "count(visit_id)"))
    assert _kinds(scope) == [("aggregate_changed", "visits", "undeclared")]
    (change,) = scope.changes
    assert change.scope == "v"
    assert "count(visit_id)" in change.detail and "count(*)" in change.detail
    assert scope.population == "kept"
    assert scope.outputs == "changed"


def test_a_coalesce_removed_is_a_null_handling_change():
    prior = PRIOR.replace(
        "avg(duration) as avg_duration", "coalesce(avg(duration), 0) as avg_duration"
    )
    scope = _scope(PRIOR, prior=prior)
    assert _kinds(scope) == [("null_handling_changed", "avg_duration", "undeclared")]


def test_a_changed_cast_is_a_type_change():
    scope = _scope(PRIOR.replace("as integer", "as varchar"))
    assert _kinds(scope) == [("type_changed", "site_id", "undeclared")]


def test_a_dropped_column_is_judged_and_an_added_one_is_only_listed():
    authored = PRIOR.replace(
        ", cast(site_id as integer) as site_id", ", 1 as visit_rank"
    )
    scope = _scope(authored)
    assert sorted(_kinds(scope)) == [
        ("column_added", "visit_rank", None),
        ("column_removed", "site_id", "undeclared"),
    ]
    assert scope.undeclared == 1


def test_a_rewritten_formula_is_listed_without_a_verdict():
    scope = _scope(PRIOR.replace("avg(duration)", "avg(duration) / 60.0"))
    assert _kinds(scope) == [("expression_changed", "avg_duration", None)]
    assert scope.undeclared == 0


PARTS_PRIOR = (
    "select p.project_id, count(*) as part_count, max(c.created_at) as last_part_at\n"
    "from {{ ref('stg_projects') }} p\n"
    "join {{ ref('stg_parts') }} c on c.project_id = p.project_id\n"
    "group by p.project_id\n"
)


def _parts_moved(
    count: str = "count(*)", final: str = "pa.part_count", spine: str = ""
) -> str:
    """The aggregates of PARTS_PRIOR extracted into a grouped CTE (#1253)."""
    return (
        "with parts as (\n"
        f"  select project_id, {count} as part_count, max(created_at) as last_part_at\n"
        "  from {{ ref('stg_parts') }} group by project_id\n"
        f"){spine}\n"
        f"select p.project_id, {final} as part_count, pa.last_part_at\n"
        "from {{ ref('stg_projects') }} p\n"
        f"join {'named' if spine else 'parts'} pa on pa.project_id = p.project_id\n"
    )


def _output_kinds(scope) -> list[tuple[str, str | None, str | None]]:
    return [(c.kind, c.column, c.status) for c in scope.changes if c.axis == "output"]


def test_an_aggregate_moved_into_a_cte_is_not_an_aggregate_change():
    scope = _scope(_parts_moved(), prior=PARTS_PRIOR)
    assert _output_kinds(scope) == [
        ("expression_changed", "last_part_at", None),
        ("expression_changed", "part_count", None),
    ]
    assert all(
        "same calculation through a CTE" in c.detail
        for c in scope.changes
        if c.axis == "output"
    )
    # The move still changes the grouping and the join: those stay reported.
    population = {c.kind for c in scope.changes if c.axis == "population"}
    assert {"group_by_changed", "join_removed"} <= population


def test_an_aggregate_is_traced_through_more_than_one_cte():
    spine = ",\nnamed as (\n  select project_id, part_count, last_part_at from parts\n)"
    scope = _scope(_parts_moved(spine=spine), prior=PARTS_PRIOR)
    assert _output_kinds(scope) == [
        ("expression_changed", "last_part_at", None),
        ("expression_changed", "part_count", None),
    ]


@pytest.mark.parametrize(
    "count", ["count(part_id)", "count(distinct part_id)", "sum(1)"]
)
def test_an_aggregate_changed_while_it_moved_is_still_an_aggregate_change(count):
    scope = _scope(_parts_moved(count=count), prior=PARTS_PRIOR)
    assert ("aggregate_changed", "part_count", "undeclared") in _output_kinds(scope)
    assert ("expression_changed", "last_part_at", None) in _output_kinds(scope)


def test_a_coalesce_added_while_the_aggregate_moved_is_a_null_handling_change():
    scope = _scope(_parts_moved(final="coalesce(pa.part_count, 0)"), prior=PARTS_PRIOR)
    assert ("null_handling_changed", "part_count", "undeclared") in _output_kinds(scope)
    assert not any(k == "aggregate_changed" for k, _, _ in _output_kinds(scope))


def test_an_untraceable_column_is_not_taken_as_the_same_aggregate():
    # `part_count` is unqualified over two relations, one of them a CTE, so
    # which one it reads is a schema question: compared as written.
    scope = _scope(_parts_moved(final="part_count"), prior=PARTS_PRIOR)
    assert ("aggregate_changed", "part_count", "undeclared") in _output_kinds(scope)


TOTAL_PRIOR = "select sum(price * quantity) as total from {{ ref('stg_orders') }}\n"


def test_an_expression_traced_into_an_aggregate_needs_no_extra_parentheses():
    moved = (
        "with lines as (\n"
        "  select price * quantity as subtotal from {{ ref('stg_orders') }}\n),\n"
        "totals as (select sum(subtotal) as total from lines)\n"
        "select total from totals\n"
    )
    scope = _scope(moved, prior=TOTAL_PRIOR)
    assert _output_kinds(scope) == [("expression_changed", "total", None)]


@pytest.mark.parametrize(
    "authored",
    [
        # Columns renamed by position: `n` is the sum, not the count.
        "with a(n, x) as (\n"
        "  select sum(price) as x, count(*) as n from {{ ref('stg_orders') }}\n"
        ")\nselect n from a\n",
        # The same through a relation alias's column list.
        "with a as (\n"
        "  select sum(price) as x, count(*) as n from {{ ref('stg_orders') }}\n"
        ")\nselect t.n from a as t(n, x)\n",
        # A nested WITH shadows the top-level `a` inside `b`.
        "with a as (select count(*) as n from {{ ref('stg_orders') }}),\n"
        "b as (\n"
        "  with a as (select sum(price) as n from {{ ref('stg_orders') }})\n"
        "  select n from a\n"
        ")\nselect n from b\n",
    ],
)
def test_a_renamed_or_shadowed_cte_column_is_compared_as_written(authored):
    prior = "select count(*) as n from {{ ref('stg_orders') }}\n"
    scope = _scope(authored, prior=prior)
    assert ("aggregate_changed", "n", "undeclared") in _output_kinds(scope)


SUM_PRIOR = "select sum(price) as n from {{ ref('stg_orders') }}\n"


@pytest.mark.parametrize(
    "authored",
    [
        # A derived table renames `quantity` to `price` on the way in.
        "with t as (\n"
        "  select sum(s.price) as n\n"
        "  from (select quantity as price from {{ ref('stg_orders') }}) as s\n"
        ")\nselect n from t\n",
        # A base relation's columns renamed by position.
        "with t as (\n"
        "  select sum(o.price) as n\n"
        "  from {{ ref('stg_orders') }} as o(quantity, price)\n"
        ")\nselect n from t\n",
        # `a` reads the table `b`, not the CTE defined after it.
        "with a as (select sum(price) as n from b),\n"
        "b as (select quantity as price from {{ ref('stg_orders') }})\n"
        "select n from a\n",
        # Two CTE names that fold to the same text, with `a` reading the later one.
        "with \"B\" as (select count(*) as price from {{ ref('stg_orders') }}),\n"
        "a as (select sum(price) as n from b),\n"
        "b as (select quantity as price from {{ ref('stg_orders') }})\n"
        "select n from a\n",
        # A quoted projection alias next to an unquoted one of the same letters.
        "with a as (\n"
        "  select count(*) as \"N\", sum(price) as n from {{ ref('stg_orders') }}\n"
        ")\n"
        'select "N" as n from a\n',
        # A pivot rewrites which value a column carries.
        "with a as (\n"
        "  select sum(price) as n, sum(quantity) as x from {{ ref('stg_orders') }}\n"
        ")\nselect n from a unpivot (n for k in (n, x))\n",
        # A named window carries part of the calculation.
        "with a as (\n"
        "  select sum(price) over w as n from {{ ref('stg_orders') }}\n"
        "  window w as (partition by category)\n"
        ")\nselect n from a\n",
        # A quoted name that differs from another only in case.
        "with \"A\" as (select count(*) as n from {{ ref('stg_orders') }}),\n"
        "a as (select sum(price) as n from {{ ref('stg_orders') }})\n"
        'select n from "A"\n',
    ],
)
def test_a_column_that_cannot_be_read_by_name_is_compared_as_written(authored):
    scope = _scope(authored, prior=SUM_PRIOR)
    assert ("aggregate_changed", "n", "undeclared") in _output_kinds(scope)


@pytest.mark.parametrize(
    ("computed", "kind"),
    [
        ("coalesce(sum(price), 0)", "null_handling_changed"),
        ("cast(sum(price) as integer)", "type_changed"),
    ],
)
def test_a_cast_or_coalesce_added_inside_the_cte_is_still_found(computed, kind):
    moved = (
        f"with t as (select {computed} as n from {{{{ ref('stg_orders') }}}})\n"
        "select n from t\n"
    )
    kinds = _output_kinds(_scope(moved, prior=SUM_PRIOR))
    assert (kind, "n", "undeclared") in kinds
    assert not any(k == "aggregate_changed" for k, _, _ in kinds)


def test_two_aggregates_moved_into_one_cte_are_the_same_ratio():
    prior = "select sum(price) / count(*) as n from {{ ref('stg_orders') }}\n"
    moved = (
        "with a as (\n"
        "  select sum(price) as s, count(*) as c from {{ ref('stg_orders') }}\n"
        ")\nselect s / c as n from a\n"
    )
    scope = _scope(moved, prior=prior)
    assert [(c.kind, c.detail) for c in scope.changes if c.axis == "output"] == [
        ("expression_changed", "`n` reads the same calculation through a CTE")
    ]


def _parts_reshaped(body: str) -> str:
    """PARTS_PRIOR's aggregates moved into a CTE whose rows differ (#1253)."""
    return (
        "with parts as (\n"
        "  select project_id, count(*) as part_count,\n"
        "    max(created_at) as last_part_at\n"
        f"  {body}\n"
        ")\n"
        "select p.project_id, pa.part_count, pa.last_part_at\n"
        "from {{ ref('stg_projects') }} p\n"
        "join parts pa on pa.project_id = p.project_id\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "from {{ ref('stg_parts') }} where status = 'active' group by project_id",
        "from {{ ref('stg_parts') }} group by project_id, kind",
        "from {{ ref('stg_tasks') }} group by project_id",
        "from {{ ref('stg_parts') }} group by project_id having count(*) > 1",
        "from {{ ref('stg_parts') }} c\n"
        "  join {{ ref('stg_tasks') }} t on t.part_id = c.part_id group by project_id",
        # A filter inside a derived table, which count(*) never reads a column of.
        "from (select * from {{ ref('stg_parts') }} where status = 'active') s\n"
        "  group by project_id",
        # A self-join that fans rows out over a table the old query already read.
        "from {{ ref('stg_parts') }} c\n"
        "  join {{ ref('stg_parts') }} d on d.project_id = c.project_id\n"
        "  group by c.project_id",
        "from {{ ref('stg_parts') }} group by project_id limit 10",
    ],
)
def test_an_aggregate_moved_into_a_cte_with_other_rows_is_not_the_same(body):
    kinds = _output_kinds(_scope(_parts_reshaped(body), prior=PARTS_PRIOR))
    assert ("aggregate_changed", "part_count", "undeclared") in kinds


@pytest.mark.parametrize(
    "join",
    [
        "join parts pa on pa.project_id = p.parent_id",
        "left join parts pa on pa.project_id = p.project_id",
    ],
)
def test_a_moved_aggregate_joined_back_differently_is_not_the_same(join):
    moved = _parts_moved().replace(
        "join parts pa on pa.project_id = p.project_id", join
    )
    kinds = _output_kinds(_scope(moved, prior=PARTS_PRIOR))
    assert ("aggregate_changed", "part_count", "undeclared") in kinds


def test_count_star_over_an_outer_join_is_never_called_the_same():
    # A project with no parts counts one row before and NULL after.
    prior = PARTS_PRIOR.replace(
        "join {{ ref('stg_parts') }}", "left join {{ ref('stg_parts') }}"
    )
    moved = _parts_moved().replace("join parts pa", "left join parts pa")
    kinds = _output_kinds(_scope(moved, prior=prior))
    assert ("aggregate_changed", "part_count", "undeclared") in kinds


def test_a_row_distinct_in_the_cte_is_not_the_same_count():
    moved = (
        "with parts as (\n"
        "  select distinct project_id, part_id from {{ ref('stg_parts') }}\n"
        "),\ncounted as (\n"
        "  select project_id, count(*) as part_count from parts group by project_id\n"
        ")\n"
        "select p.project_id, c.part_count\n"
        "from {{ ref('stg_projects') }} p\n"
        "join counted c on c.project_id = p.project_id\n"
    )
    prior = (
        "select p.project_id, count(*) as part_count\n"
        "from {{ ref('stg_projects') }} p\n"
        "join {{ ref('stg_parts') }} c on c.project_id = p.project_id\n"
        "group by p.project_id\n"
    )
    kinds = _output_kinds(_scope(moved, prior=prior))
    assert ("aggregate_changed", "part_count", "undeclared") in kinds


ACTIVE_PARTS_PRIOR = PARTS_PRIOR.replace(
    "group by", "where c.status = 'active'\ngroup by"
)


def test_a_filter_moved_into_the_cte_with_the_aggregate_is_the_same():
    moved = _parts_reshaped(
        "from {{ ref('stg_parts') }} where status = 'active' group by project_id"
    )
    assert _output_kinds(_scope(moved, prior=ACTIVE_PARTS_PRIOR)) == [
        ("expression_changed", "last_part_at", None),
        ("expression_changed", "part_count", None),
    ]


@pytest.mark.parametrize(
    ("prior", "authored", "column"),
    [
        # The filter now reads the projects' status, not the parts'.
        (
            ACTIVE_PARTS_PRIOR,
            _parts_moved() + "where p.status = 'active'\n",
            "part_count",
        ),
        # Rows counted per category before, categories counted after.
        (
            "select count(*) as n from {{ ref('stg_orders') }} group by category\n",
            "with g as (\n"
            "  select category from {{ ref('stg_orders') }} group by category\n"
            "),\nc as (select count(*) as n from g)\nselect n from c\n",
            "n",
        ),
        # The DISTINCT compares fewer columns, so fewer rows are counted.
        (
            "with a as (select distinct id, kind from {{ ref('stg_orders') }})\n"
            "select count(*) as n from a\n",
            "with b as (select distinct id from {{ ref('stg_orders') }}),\n"
            "c as (select count(*) as n from b)\nselect n from c\n",
            "n",
        ),
        # The count no longer reads the table at all.
        (
            "select count(*) as n from {{ ref('stg_orders') }}\n",
            "with c as (select count(*) as n)\nselect n from c\n",
            "n",
        ),
        # `a` reads the table `b`, not the CTE defined after it.
        (
            "select count(*) as n from {{ ref('stg_orders') }}\n",
            "with a as (select count(*) as n from b),\n"
            "b as (select price from {{ ref('stg_orders') }})\nselect n from a\n",
            "n",
        ),
        # The same name summed from the other side of the join.
        (
            "select sum(a.price) as n\n"
            "from {{ ref('stg_a') }} a join {{ ref('stg_b') }} b on a.id = b.id\n",
            "with c as (\n"
            "  select sum(b.price) as n\n"
            "  from {{ ref('stg_a') }} a join {{ ref('stg_b') }} b on a.id = b.id\n"
            ")\nselect n from c\n",
            "n",
        ),
        # The same output name over another filter and another aggregate.
        (
            "with a as (\n"
            "  select sum(price) as n from {{ ref('stg_orders') }} where flag = 1\n"
            ")\nselect n from a\n",
            "with b as (\n"
            "  select count(*) as n from {{ ref('stg_orders') }} where flag = 2\n"
            ")\nselect n from b\n",
            "n",
        ),
        # The HAVING moved from the per-key count to the sum over keys.
        (
            "with a as (\n"
            "  select k, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by k having count(*) > 1\n"
            ")\nselect sum(n) as total from a\n",
            "with b as (\n"
            "  select k, count(*) as n from {{ ref('stg_orders') }} group by k\n"
            "),\n"
            "c as (select sum(n) as total from b having count(*) > 1)\n"
            "select total from c\n",
            "total",
        ),
        # `g` is an alias for another column on each side.
        (
            "select k as g, count(*) as n from {{ ref('stg_orders') }} group by g\n",
            "with c as (\n"
            "  select category as g, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by g\n"
            ")\nselect g, n from c\n",
            "n",
        ),
        # The ordinal points at another column.
        (
            "select k, count(*) as n from {{ ref('stg_orders') }} group by 1\n",
            "with c as (\n"
            "  select category, count(*) as n from {{ ref('stg_orders') }} group by 1\n"
            ")\nselect n from c\n",
            "n",
        ),
        # GROUP BY ALL groups by whatever the select lists.
        (
            "select k, count(*) as n from {{ ref('stg_orders') }} group by all\n",
            "with c as (\n"
            "  select category, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by all\n"
            ")\nselect n from c\n",
            "n",
        ),
        # A DISTINCT over a star compares the columns the star stands for.
        (
            "with a as (select id, kind from {{ ref('stg_orders') }}),\n"
            "d as (select distinct * from a)\nselect count(*) as n from d\n",
            "with b as (select id from {{ ref('stg_orders') }}),\n"
            "e as (select distinct * from b),\n"
            "c as (select count(*) as n from e)\nselect n from c\n",
            "n",
        ),
        # DISTINCT ON keeps the row its ORDER BY puts first.
        (
            "with a as (\n"
            "  select distinct on (k) k, v from {{ ref('stg_orders') }}\n"
            "  order by k, v asc\n"
            ")\nselect sum(v) as n from a\n",
            "with b as (\n"
            "  select distinct on (k) k, v from {{ ref('stg_orders') }}\n"
            "  order by k, v desc\n"
            "),\nc as (select sum(v) as n from b)\nselect n from c\n",
            "n",
        ),
        # A self-join through a CTE: the sum moves to the other copy.
        (
            "with a as (select id, parent_id, price from {{ ref('stg_orders') }})\n"
            "select sum(x.price) as n from a x join a y on x.id = y.parent_id\n",
            "with a as (select id, parent_id, price from {{ ref('stg_orders') }}),\n"
            "c as (\n"
            "  select sum(y.price) as n from a x join a y on x.id = y.parent_id\n"
            ")\nselect n from c\n",
            "n",
        ),
        # A parenthesized alias is still an alias for another column.
        (
            "select k as g, count(*) as n from {{ ref('stg_orders') }} group by (g)\n",
            "with c as (\n"
            "  select category as g, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by (g)\n"
            ")\nselect g, n from c\n",
            "n",
        ),
        # Beside a star, an ordinal names a column the select list does not.
        (
            "with a as (select k from {{ ref('stg_orders') }})\n"
            "select *, count(*) as n from a group by 1\n",
            "with b as (select category from {{ ref('stg_orders') }}),\n"
            "c as (select *, count(*) as n from b group by 1)\n"
            "select n from c\n",
            "n",
        ),
        # A window counted before the filter, then after it.
        (
            "with g as (\n"
            "  select x, count(*) over () as n from {{ ref('stg_orders') }}\n"
            "),\nf as (select x, n from g where x > 0)\nselect n from f\n",
            "with g as (select x from {{ ref('stg_orders') }}),\n"
            "f as (select x, count(*) over () as n from g where x > 0)\n"
            "select n from f\n",
            "n",
        ),
        # The same window moved out of the CTE, past the filter.
        (
            "with w as (select x, count(*) over () as n from {{ ref('stg_orders') }})\n"
            "select n from w where x > 0\n",
            "select count(*) over () as n from {{ ref('stg_orders') }} where x > 0\n",
            "n",
        ),
        # QUALIFY and WHERE swap stages before the sum.
        (
            "with a as (\n"
            "  select k, x from {{ ref('stg_orders') }}\n"
            "  qualify row_number() over (partition by k order by x) = 1\n"
            "),\nb as (select x from a where x > 0)\nselect sum(x) as n from b\n",
            "with a2 as (select k, x from {{ ref('stg_orders') }} where x > 0),\n"
            "b2 as (\n"
            "  select x from a2\n"
            "  qualify row_number() over (partition by k order by x) = 1\n"
            "),\ns as (select sum(x) as n from b2)\nselect n from s\n",
            "n",
        ),
        # A sample taken from the rows, then from the one counted row.
        (
            "select count(*) as n from {{ ref('stg_orders') }} using sample 2 rows\n",
            "with c as (select count(*) as n from {{ ref('stg_orders') }})\n"
            "select n from c using sample 2 rows\n",
            "n",
        ),
        # An ordered aggregate over a CTE whose order changed.
        (
            "with o as (select x from {{ ref('stg_orders') }} order by x desc)\n"
            "select array_agg(x) as n from o\n",
            "with o as (select x from {{ ref('stg_orders') }} order by x asc),\n"
            "s as (select array_agg(x) as n from o)\nselect n from s\n",
            "n",
        ),
        # ROLLUP adds a total row to the counts.
        (
            "select k, count(*) as n from {{ ref('stg_orders') }} group by k\n",
            "with c as (\n"
            "  select k, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by rollup (k)\n"
            ")\nselect k, n from c\n",
            "n",
        ),
    ],
)
def test_a_moved_calculation_that_differs_is_not_the_same(prior, authored, column):
    kinds = _output_kinds(_scope(authored, prior=prior))
    assert ("aggregate_changed", column, "undeclared") in kinds


def test_the_same_column_read_through_a_renamed_cte_is_traced():
    prior = "with a as (select sum(price) as n from {{ ref('stg_orders') }})\n"
    renamed = "with b as (select count(*) as n from {{ ref('stg_orders') }})\n"
    scope = _scope(renamed + "select n from b\n", prior=prior + "select n from a\n")
    assert _output_kinds(scope) == [("aggregate_changed", "n", "undeclared")]
    # Through the same CTE, the change is reported once, in that CTE.
    kept = renamed.replace("with b", "with a") + "select n from a\n"
    scope = _scope(kept, prior=prior + "select n from a\n")
    assert [(c.kind, c.scope) for c in scope.changes if c.axis == "output"] == [
        ("aggregate_changed", "a")
    ]


@pytest.mark.parametrize(
    ("prior", "authored", "column"),
    [
        # `count(*)` names no column, but reads another CTE's rows.
        (
            "with a as (select k from {{ ref('stg_orders') }} where flag = 1)\n"
            "select count(*) as n from a\n",
            "with b as (select k from {{ ref('stg_orders') }} where flag = 2)\n"
            "select count(*) as n from b\n",
            "n",
        ),
        # The HAVING moved from the count to the sum, both grouped by `k`.
        (
            "with a as (\n"
            "  select k, count(*) as n from {{ ref('stg_orders') }}\n"
            "  group by k having count(*) > 1\n"
            "),\nb as (select k, sum(n) as total from a group by k)\n"
            "select total from b\n",
            "with c as (\n"
            "  select k, count(*) as n from {{ ref('stg_orders') }} group by k\n"
            "),\nd as (\n"
            "  select k, sum(n) as total from c group by k having count(*) > 1\n"
            ")\nselect total from d\n",
            "total",
        ),
    ],
)
def test_the_same_text_over_other_rows_is_left_to_population(prior, authored, column):
    # Which rows reach an unchanged calculation is population's question: it
    # cannot read renamed CTEs, so it says unchecked, never kept.
    scope = _scope(authored, prior=prior)
    assert scope.population == "unchecked"
    assert not [c for c in scope.changes if c.axis == "output" and c.column == column]


def test_reformatting_and_renaming_an_alias_are_silent():
    authored = (
        "WITH v AS (SELECT s.visit_date, COUNT(*) AS visits,\n"
        "  AVG(s.duration) AS avg_duration FROM {{ ref('stg_visits') }} AS s\n"
        "  GROUP BY s.visit_date)\n"
        "SELECT visit_date, visits, avg_duration, CAST(site_id AS INTEGER) AS site_id "
        "FROM v"
    )
    scope = _scope(authored)
    assert scope.changes == []
    assert (scope.population, scope.outputs) == ("kept", "kept")


@pytest.mark.parametrize(
    ("before", "after", "population"),
    [
        ("visit_date", "s.visit_date", "kept"),
        ("t.visit_date", "s.visit_date", "kept"),
        ("visit_date", "month", "changed"),
        ("visit_date", "visit_date, site_id", "changed"),
        ("visit_date, site_id", "visit_date", "changed"),
        ("visit_date, site_id", "s.visit_date, s.site_id", "kept"),
    ],
)
def test_group_by_qualifier_changes_do_not_change_population(
    before: str, after: str, population: str
):
    prior = f"select visit_date, count(*) as visits from visits t group by {before}"
    authored = f"select s.visit_date, count(*) as visits from visits s group by {after}"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == population
    assert scope.outputs == "kept"
    assert ("group_by_changed" in {change.kind for change in scope.changes}) is (
        population == "changed"
    )


def test_group_by_alias_rename_keeps_relation_identity():
    prior = "select s.visit_date from visits as s group by s.visit_date"
    authored = "select t.visit_date from visits as t group by t.visit_date"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "kept"
    assert "group_by_changed" not in {change.kind for change in scope.changes}


def test_projection_alias_is_not_rewritten_as_same_named_source_column():
    """BigQuery's projected alias can represent a different grouping value."""

    prior = "select upper(last_name) as last_name from singers group by last_name"
    authored = (
        "select upper(last_name) as last_name from singers group by singers.last_name"
    )

    scope = cs.scope_for(PATH, prior, authored, "bigquery", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


def test_bigquery_group_by_alias_for_same_source_column_resolves_before_matching():
    """A direct projection alias still resolves to its underlying source column."""

    prior = "select singers.last_name as last_name from singers group by last_name"
    authored = (
        "select singers.last_name as last_name from singers group by singers.last_name"
    )

    scope = cs.scope_for(PATH, prior, authored, "bigquery", [])

    assert scope.population == "kept"
    assert "group_by_changed" not in {change.kind for change in scope.changes}


def test_source_replacement_is_reported_without_duplicate_group_change():
    """A relation swap has its own finding while stable grouping stays unchanged."""

    prior = "select o.status from orders o group by o.status"
    authored = "select o.status from orders_v2 o group by o.status"

    scope = _scope(authored, prior=prior)

    assert scope.population == "changed"
    kinds = {change.kind for change in scope.changes}
    assert "source_changed" in kinds
    assert "group_by_changed" not in kinds


def test_qualifier_insensitive_grouping_with_multiple_join_sources():
    """Renaming both aliases preserves each source position in a multi-join."""

    prior = (
        "select a.id from customers a cross join orders b group by a.id, b.customer_id"
    )
    authored = (
        "select x.id from customers x cross join orders y group by x.id, y.customer_id"
    )

    scope = _scope(authored, prior=prior)

    assert scope.population == "kept"
    assert "group_by_changed" not in {change.kind for change in scope.changes}


def test_group_by_different_joined_relations_remains_a_change():
    """Columns from different joined source positions are not interchangeable."""

    prior = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by a.id"
    )
    authored = (
        "select b.id from customers as a "
        "join orders as b on a.id = b.customer_id group by b.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}
    (change,) = [
        change for change in scope.changes if change.kind == "group_by_changed"
    ]
    assert "a.id" in change.detail and "b.id" in change.detail


def test_group_by_self_join_aliases_are_not_assumed_equivalent():
    """A self-join's two aliases continue to identify separate source instances."""

    prior = (
        "select a.id from customers as a "
        "join customers as b on a.parent_id = b.id group by a.id"
    )
    authored = (
        "select b.id from customers as a "
        "join customers as b on a.parent_id = b.id group by b.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


def test_bare_group_by_column_in_join_is_not_assumed_unambiguous():
    """Without schema metadata, a bare joined column cannot be resolved safely."""

    prior = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by id"
    )
    authored = (
        "select a.id from customers as a "
        "join orders as b on a.id = b.customer_id group by a.id"
    )

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


def test_derived_source_aliases_remain_conservative():
    """Derived relations are left unresolved instead of guessing their identity."""

    prior = "select x.id from (select id from customers) as x group by x.id"
    authored = "select y.id from (select id from customers) as y group by y.id"

    scope = _scope(authored, prior=prior)

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "catalog_a.schema_a.visits.visit_date",
            "catalog_b.schema_a.visits.visit_date",
        ),
        (
            "catalog_a.schema_a.visits.visit_date",
            "catalog_a.schema_b.visits.visit_date",
        ),
    ],
)
def test_group_by_keeps_catalog_and_database_qualifiers(before: str, after: str):
    """Changing a catalog or database qualifier remains a semantic difference."""

    prior = f"select s.visit_date from visits as s group by {before}"
    authored = f"select s.visit_date from visits as s group by {after}"

    scope = cs.scope_for(PATH, prior, authored, "duckdb", [])

    assert scope.population == "changed"
    assert "group_by_changed" in {change.kind for change in scope.changes}


# --- population ------------------------------------------------------------------


def test_an_added_predicate_changes_the_population_and_names_its_columns():
    scope = _scope(PRIOR + "where visit_date >= '2024-01-01'\n")
    assert scope.population == "changed"
    (change,) = scope.changes
    assert (change.axis, change.kind, change.status) == (
        "population",
        "predicate_added",
        "undeclared",
    )
    assert change.columns == ["visit_date"]


def test_a_byte_identical_edit_is_not_reported():
    assert cs.analyze([(PATH, PRIOR, PRIOR)], [], "duckdb") is None


def test_a_broad_filter_report_says_which_populations_change_and_which_stay():
    roster = "select account_id, region, is_sandbox from {{ ref('stg_accounts') }}\n"
    summary = (
        "select region, count(*) as accounts from {{ ref('stg_accounts') }} "
        "group by region\n"
    )
    report = cs.analyze(
        [
            ("models/marts/dim_accounts.sql", roster, roster + "-- the directory\n"),
            (
                "models/marts/region_summary.sql",
                summary,
                summary.replace("group by", "where not is_sandbox group by"),
            ),
        ],
        [],
        "duckdb",
    )
    assert report is not None
    # The comment-only edit is byte-different but changes nothing it returns.
    assert report["population"] == {
        "changed": ["region_summary"],
        "kept": ["dim_accounts"],
        "unchecked": [],
    }
    assert report["undeclared"] == 1


# --- settling --------------------------------------------------------------------


def test_an_assumption_naming_the_column_declares_the_change():
    assumption = {
        "model": "site_daily",
        "decision": "What an empty day counts",
        "chosen": "zero visits",
        "evidence": "default",
        "column": "visits",
    }
    scope = _scope(
        PRIOR.replace("count(*)", "coalesce(count(visit_id), 0)"), [assumption]
    )
    assert {c.kind for c in scope.changes} == {
        "aggregate_changed",
        "null_handling_changed",
    }
    for change in scope.changes:
        assert change.status == "declared"
        assert change.assumption == {
            "decision": "What an empty day counts",
            "chosen": "zero visits",
            "evidence": "default",
            "column": "visits",
        }
        assert change.note and "default" in change.note
    assert scope.undeclared == 0


def test_an_assumption_on_another_column_does_not_declare_it():
    scope = _scope(
        PRIOR.replace("count(*)", "count(visit_id)"),
        [{"decision": "d", "chosen": "c", "evidence": "request", "column": "site_id"}],
    )
    assert scope.changes[0].status == "undeclared"


def test_a_population_change_is_declared_by_an_assumption_on_its_predicate_column():
    scope = _scope(
        PRIOR + "where not is_sandbox\n",
        [
            {
                "decision": "Sandbox sites",
                "chosen": "excluded",
                "evidence": "request",
                "column": "is_sandbox",
            }
        ],
    )
    (change,) = scope.changes
    assert change.status == "declared"


def test_a_qualified_assumption_column_matches_its_bare_name():
    scope = _scope(
        PRIOR.replace("count(*)", "count(visit_id)"),
        [
            {
                "decision": "d",
                "chosen": "c",
                "evidence": "contract",
                "column": "site_daily.visits",
            }
        ],
    )
    assert scope.changes[0].status == "declared"


def test_analyze_settles_against_the_model_the_assumption_belongs_to():
    report = cs.analyze(
        [(PATH, PRIOR, PRIOR.replace("count(*)", "count(visit_id)"))],
        [
            {"model": "other", "column": "visits", "evidence": "request"},
        ],
        "duckdb",
    )
    assert report["models"][0]["changes"][0]["status"] == "undeclared"


# --- what it cannot read ---------------------------------------------------------


def test_a_macro_leaves_the_model_unchecked_with_its_reason():
    authored = PRIOR.replace("count(*)", "{{ count_rows() }}")
    scope = _scope(authored)
    assert (scope.population, scope.outputs) == ("unchecked", "unchecked")
    assert scope.reason and "count_rows" in scope.reason
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_a_star_leaves_removal_unread_but_still_compares_the_ctes():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(prior.replace("count(*)", "count(visit_id)"), prior=prior)
    assert scope.outputs == "changed"
    assert scope.reason and "star" in scope.reason
    assert _kinds(scope) == [("aggregate_changed", "visits", "undeclared")]


def test_an_unread_star_with_nothing_else_changed_is_stored_unchecked():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(prior + "-- reviewed\n", prior=prior)
    assert (scope.population, scope.outputs) == ("kept", "unchecked")
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_a_star_with_a_changed_cte_is_still_stored_unread():
    prior = PRIOR.replace(
        "select visit_date, visits, avg_duration, cast(site_id as integer) as site_id",
        "select *",
    )
    scope = _scope(
        prior.replace("count(*)", "count(visit_id)"),
        [{"decision": "d", "chosen": "c", "evidence": "request", "column": "visits"}],
        prior=prior,
    )
    assert scope.outputs == "changed" and scope.outputs_unread is True
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {
        "site_daily": [
            {"kind": "aggregate_changed", "column": "visits", "status": "declared"},
            {"kind": "unread", "status": "unchecked"},
        ]
    }


def test_what_the_caps_cut_is_stored_unchecked():
    columns = ", ".join(f"cast(c{i} as integer) as c{i}" for i in range(12))
    prior = "select " + columns + " from {{ ref('t') }}"
    edits = [
        (f"models/m{i:02d}.sql", prior, prior.replace("as integer", "as varchar"))
        for i in range(cs.MAX_MODELS + 2)
    ]
    report = cs.analyze(edits, [], "duckdb")
    assert report["elided"] == {"models": 2}
    assert report["beyond_cap"] == ["m25", "m26"]
    stored = cs.stored_form(report)
    assert stored["m00"][-1] == {"kind": "elided", "status": "unchecked"}
    assert stored["m26"] == [{"kind": "elided", "status": "unchecked"}]


def test_an_edit_inside_a_union_is_unchecked_not_kept():
    prior = (
        "with both as (\n"
        "  select account_id from {{ ref('a') }}\n"
        "  union all\n"
        "  select account_id from {{ ref('b') }}\n"
        ")\nselect account_id from both\n"
    )
    authored = prior.replace("{{ ref('b') }}", "{{ ref('b') }} where active")
    scope = _scope(authored, prior=prior)
    assert scope.population == "unchecked"
    assert scope.reason and "`both`" in scope.reason
    assert cs.stored_form(
        {"models": [scope.model_dump(mode="json", exclude_none=True)]}
    ) == {"site_daily": [{"kind": "unread", "status": "unchecked"}]}


def test_a_changed_union_is_named_once_beside_row_attribution():
    """Row attribution now names a changed set operation too (#1163); the
    reason still says it once."""

    prior = (
        "with both as (\n"
        "  select account_id from {{ ref('a') }}\n"
        "  union all\n"
        "  select account_id from {{ ref('b') }}\n"
        ")\nselect account_id from both\n"
    )
    authored = prior.replace("{{ ref('b') }}", "{{ ref('b') }} where active")
    scope = _scope(authored, prior=prior)
    assert scope.population == "unchecked"
    assert scope.reason == (
        "the set operation in `both` changed and its branches were not compared"
    )


def test_a_recased_string_literal_in_a_union_is_unchecked_not_kept():
    """Only identifier case is folded: ``'OPEN'`` edited to ``'open'`` can
    select different rows."""

    prior = (
        "with both as (\n"
        "  select account_id from {{ ref('a') }} where status = 'OPEN'\n"
        "  union all\n"
        "  select account_id from {{ ref('b') }}\n"
        ")\nselect account_id from both\n"
    )
    scope = _scope(prior.replace("'OPEN'", "'open'"), prior=prior)
    assert scope.population == "unchecked"
    assert scope.reason and "`both`" in scope.reason


def test_the_caps_count_what_they_cut():
    columns = ", ".join(f"cast(c{i} as integer) as c{i}" for i in range(12))
    prior = "select " + columns + " from {{ ref('t') }}"
    scope = _scope(prior.replace("as integer", "as varchar"), prior=prior)
    assert len(scope.changes) == cs.MAX_CHANGES_PER_MODEL
    assert scope.elided == 12 - cs.MAX_CHANGES_PER_MODEL
    assert scope.undeclared == 12


def test_the_warning_names_the_change_and_both_fixes():
    report = cs.analyze(
        [(PATH, PRIOR, PRIOR.replace("count(*)", "count(visit_id)"))], [], "duckdb"
    )
    (warning,) = cs.warnings(report)
    assert warning.startswith("'site_daily':")
    assert "count(visit_id)" in warning
    assert "assumption" in warning and "undo it" in warning


# --- completion ------------------------------------------------------------------


def test_a_for_plan_build_carries_undeclared_changes_as_obligations():
    report = "rpt_sites"
    nodes = [BuildNode(unique_id=_uid(report), name=report, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=[report]),
        None,
        change_scope={
            report: [
                {
                    "kind": "aggregate_changed",
                    "column": "visits",
                    "status": "undeclared",
                },
                {"kind": "type_changed", "column": "site_id", "status": "declared"},
                {"kind": "unread", "status": "unchecked"},
            ],
            "not_required": [{"kind": "column_removed", "status": "undeclared"}],
        },
    )
    assert completion.data()["obligations"]["change_scope"] == [
        {
            "model": report,
            "kind": "change_scope",
            "status": "undeclared",
            "column": "visits",
            "change": "aggregate_changed",
        },
        {
            "model": report,
            "kind": "change_scope",
            "status": "unchecked",
            "change": "unread",
        },
    ]


# --- the CLI ---------------------------------------------------------------------


ROSTER = "select account_id, region, is_sandbox from {{ source('raw', 'accounts') }}\n"
SUMMARY = (
    "select region, count(*) as accounts\n"
    "from {{ source('raw', 'accounts') }}\ngroup by region\n"
)


@pytest.fixture
def accounts_project(tmp_path: Path) -> tuple[Path, Path]:
    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "accounts.duckdb"
    conn = duckdb.connect(str(warehouse))
    conn.execute(
        "CREATE TABLE accounts AS SELECT range AS account_id, "
        "CASE WHEN range % 2 = 0 THEN 'emea' ELSE 'amer' END AS region, "
        "range % 5 = 0 AS is_sandbox FROM range(20)"
    )
    conn.close()
    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: acct\nversion: "1.0.0"\nprofile: acct\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "acct:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: accounts\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "dim_accounts.sql").write_text(ROSTER)
    (project / "models" / "marts" / "region_summary.sql").write_text(SUMMARY)
    return tmp_path, warehouse


def _plan(repo: Path, warehouse: Path, body: dict, capsys) -> dict:
    payload = repo / "edits.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "exclude sandbox accounts",
            "--edits-file",
            str(payload),
            "--connector",
            "duckdb",
            "--path",
            str(warehouse),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    return envelope


def _edit(name: str, content: str) -> dict:
    return {"path": f"models/marts/{name}.sql", "kind": "model_sql", "content": content}


def test_plan_reports_an_undeclared_roster_filter_and_stores_it(
    accounts_project, capsys
):
    repo, warehouse = accounts_project
    filtered = ROSTER.rstrip() + " where not is_sandbox\n"
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit("dim_accounts", filtered),
                _edit(
                    "region_summary",
                    SUMMARY.replace("group by", "where not is_sandbox\ngroup by"),
                ),
            ]
        },
        capsys,
    )
    scope = envelope["data"]["change_scope"]
    assert scope["population"]["changed"] == ["dim_accounts", "region_summary"]
    assert scope["undeclared"] == 2
    assert any(
        w.startswith("'dim_accounts': this plan changes more")
        for w in envelope["warnings"]
    )
    plan_id = envelope["data"]["plan_id"]
    stored = json.loads(
        (repo / ".dex" / "plans" / f"{plan_id}.json").read_text(encoding="utf-8")
    )
    assert stored["change_scope"]["dim_accounts"] == [
        {"kind": "predicate_added", "status": "undeclared"}
    ]


def test_plan_settles_a_declared_filter_and_relays_the_detail(accounts_project, capsys):
    repo, warehouse = accounts_project
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit(
                    "region_summary",
                    SUMMARY.replace("group by", "where not is_sandbox\ngroup by"),
                )
            ],
            "declarations": [
                {
                    "model": "region_summary",
                    "assumptions": [
                        {
                            "decision": "Sandbox accounts in the summary",
                            "chosen": "excluded",
                            "evidence": "request",
                            "column": "is_sandbox",
                            "detail": "the request names the region summary",
                        }
                    ],
                }
            ],
        },
        capsys,
    )
    data = envelope["data"]
    (model,) = data["change_scope"]["models"]
    assert model["changes"][0]["status"] == "declared"
    assert data["change_scope"]["undeclared"] == 0
    assert not any("this plan changes more" in w for w in envelope["warnings"])
    (decision,) = data["decisions"]
    assert decision["detail"] == "the request names the region summary"


def test_plan_does_not_call_an_aggregate_moved_into_a_cte_changed(
    accounts_project, capsys
):
    repo, warehouse = accounts_project
    moved = (
        "with counted as (\n"
        "  select region, count(*) as accounts\n"
        "  from {{ source('raw', 'accounts') }}\n  group by region\n"
        ")\nselect region, accounts from counted\n"
    )
    envelope = _plan(
        repo, warehouse, {"edits": [_edit("region_summary", moved)]}, capsys
    )
    (model,) = envelope["data"]["change_scope"]["models"]
    outputs = [c for c in model["changes"] if c["axis"] == "output"]
    assert [(c["kind"], c["column"]) for c in outputs] == [
        ("expression_changed", "accounts")
    ]
    assert "status" not in outputs[0]


def test_a_new_model_has_no_change_scope(accounts_project, capsys):
    repo, warehouse = accounts_project
    envelope = _plan(
        repo,
        warehouse,
        {
            "edits": [
                _edit(
                    "account_count",
                    "select count(*) as n from {{ ref('dim_accounts') }}\n",
                )
            ]
        },
        capsys,
    )
    assert "change_scope" not in envelope["data"]


# --- re-driving (#1228) ------------------------------------------------------------

REDRIVE_FROM_PARENT = (
    "with parts as (\n"
    "  select conversation_id, count(*) as part_count\n"
    "  from {{ ref('stg_parts') }} group by conversation_id\n"
    ")\n"
    "select c.conversation_id, coalesce(p.part_count, 0) as part_count\n"
    "from {{ ref('stg_conversations') }} as c\n"
    "left join parts as p on p.conversation_id = c.conversation_id\n"
)
REDRIVE_FROM_PARTS = (
    "select conversation_id, count(*) as part_count\n"
    "from {{ ref('stg_parts') }} group by conversation_id\n"
)
REDRIVE_PATH = "models/marts/conversation_summary.sql"


def _redrive(prior, authored, assumptions=None, parents=None):
    return cs.scope_for(
        REDRIVE_PATH, prior, authored, "duckdb", assumptions or [], parents=parents
    )


def test_a_new_driving_relation_is_redriven_and_undeclared():
    scope = _redrive(REDRIVE_FROM_PARENT, REDRIVE_FROM_PARTS)
    [change] = [c for c in scope.changes if c.kind == "redriven"]
    assert scope.population == "changed"
    assert change.reason == "redriven"
    assert change.prior == "stg_conversations"
    assert change.authored == "stg_parts"
    assert change.status == "undeclared"
    assert "join key" in change.note


@pytest.mark.parametrize(
    ("evidence", "status"),
    [
        ("request", "declared"),
        ("contract", "declared"),
        ("precedent", "undeclared"),
        ("data", "undeclared"),
        ("default", "undeclared"),
    ],
)
def test_only_request_or_contract_settles_a_redrive(evidence, status):
    assumption = {
        "model": "conversation_summary",
        "decision": "which conversations belong",
        "chosen": "only conversations with parts",
        "evidence": evidence,
    }
    scope = _redrive(REDRIVE_FROM_PARENT, REDRIVE_FROM_PARTS, [assumption])
    [change] = [c for c in scope.changes if c.kind == "redriven"]
    assert change.status == status
    if status == "undeclared":
        assert f"at {evidence} tier" in change.note


def test_a_removed_parent_is_redriven_even_with_the_sql_unchanged():
    report = cs.analyze(
        [(REDRIVE_PATH, REDRIVE_FROM_PARENT, REDRIVE_FROM_PARENT)],
        [],
        "duckdb",
        parents={REDRIVE_PATH: ("stg_conversations", None)},
    )
    [entry] = report["models"]
    assert entry["population"] == "changed"
    [change] = entry["changes"]
    assert change["kind"] == "redriven"
    assert "was removed" in change["detail"]
    assert cs.stored_form(report)["conversation_summary"] == [
        {"kind": "redriven", "status": "undeclared", "reason": "redriven"}
    ]


def test_an_unchanged_driver_and_parent_are_not_redriven():
    scope = _redrive(
        REDRIVE_FROM_PARENT,
        REDRIVE_FROM_PARENT.replace("coalesce(p.part_count, 0)", "p.part_count"),
        parents=("stg_conversations", "stg_conversations"),
    )
    assert not [c for c in scope.changes if c.kind == "redriven"]


def test_an_earlier_plan_is_named_as_what_was_compared():
    report = cs.analyze(
        [(REDRIVE_PATH, REDRIVE_FROM_PARENT, REDRIVE_FROM_PARTS)],
        [],
        "duckdb",
        against={REDRIVE_PATH: "earlier_plan"},
    )
    assert report["models"][0]["against"] == "earlier_plan"


def test_a_redrive_leads_so_no_cap_cuts_it():
    # Four output changes beside the redrive: a completion list capped at five
    # still has the redrive, because it sorts first among the undeclared.
    scope = _redrive(REDRIVE_FROM_PARTS, REDRIVE_FROM_PARENT)
    assert len([c for c in scope.changes if c.status == "undeclared"]) > 1
    assert scope.changes[0].kind == "redriven"
