**Join coverage measures a CTE grouped by exactly the join's key columns**
(#1324). The common customer rollup, a parent roster left-joined to one row per
child key, was `unchecked` as `filtered_cte`, so verify could not tell "every
parent with a child matched" from "none did". A side read through a scope whose
only reducing clauses are a supported `WHERE` and a `GROUP BY` over exactly the
join's key columns (one input, no `HAVING`, window, `LIMIT` or set operation) is
now probed over its groups, never the ungrouped relation underneath: the
record's `effective_input` names the `grouped_by` key columns and
`measured.group_rows` is the grouped input's own denominator. `maintain verify`
and `transform build --verify` report the same shape, so a LEFT join's parents
with no group stay `satisfied` (`optional_unmatched`), a required reference's
unmatched parents are `join_orphans`, and a grouped input with no groups is
`not_applicable` with `empty_input` rather than a zero-match join. Any other
grouping shape (an extra grouping column, a subset of a composite key, an
expression, a grouping inside another grouping) stays `unchecked` as
`filtered_cte`.
