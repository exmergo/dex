**`transform build --full-refresh --verify` checks the rows of the incremental
models it rebuilt.** A rebuilt model holding materially fewer or more rows than its
driving parent is now reported as `row_loss` or `row_fanout`, as a table is, and
the parent's row count is priced in the build's one estimate. An incremental
model this build did not rebuild, one whose `full_refresh: false` made dbt ignore
the flag, and a microbatch model keep the skip, named in `warnings`. A microbatch
model is no longer told to run `dbt compile`, which would not have helped.
`maintain verify` compares no incremental model, as before.
