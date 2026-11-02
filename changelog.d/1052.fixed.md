**`explore profile` no longer counts distinct values of a column whose type its
connector skips.**
A null-free `VARIANT`, `MAP`, `SUPER`, `GEOGRAPHY` or similar column reports no
distinct count, which reads the same as a column the catalog has no statistics
for, so dex sent it to the exact count. On a warehouse that cannot group the
type, one such column could fail the profile of its table, and with it
`explore map` and `explore relationships` (#925). Where the warehouse can count
it, as DuckDB and ClickHouse do, the profile reported the count and could offer
the column as a candidate key.

Such a column now reports no `distinct_count`, and its null fraction where the
connector measures one, and is no longer offered as a candidate key. A table
whose aggregate scan read no rows or was refused gets no exact count either.
Postgres views and unanalyzed tables are still measured, and a degraded column
no longer uses up the columns measured there.

This changes output you may store or compare. Once you profile again, a column
that only held a `candidate_keys` place through this count leaves it, and after
a new `maintain snapshot` `maintain check` stops watching it; declare the grain
to keep it watched.
