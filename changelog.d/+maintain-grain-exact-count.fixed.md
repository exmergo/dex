**`maintain verify` measures grain on tables the catalog has no count for.**
On Postgres, a table nobody has analyzed (and every view) reports no row
count, so the grain check used to come back unmeasured and blame the
connector. It now takes the exact row count from the same statement that
computes the distinct count, so a broken unique key is reported as
`grain_broken` with `exact: true`, and no grain verdict ever compares a
distinct count against the planner's estimate. When the count genuinely
cannot be supplied, the note says so instead of blaming the connector.
