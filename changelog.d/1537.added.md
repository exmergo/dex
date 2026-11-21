**A declaration can say how fresh a model's data should be.** The column with
role `time` takes `freshness: {max_lag: 6h, basis: business|capture}`.
`maintain verify` and `transform build --verify` read the column's newest value
against the warehouse's own clock and raise `freshness_exceeded` where it trails
by more than `max_lag`, or where the column holds no value (no rows is not
fresh): high on a `capture` basis, medium on `business`, which a backfill can
leave legitimately old. Each declared freshness gets a `data.freshness` record.
With `declarations.render_tests` on it also renders `dex_freshness`, a
count-only column test tagged `dex_every_run` that fails on the same columns on
every connector, and the plan writes its macro as a reviewable diff. dbt's own
`freshness` covers sources only; this one covers a model's data. A changed
`freshness` is high-severity declaration drift in `maintain semantic`, and
`maintain coverage` lists it as a declared fact.
