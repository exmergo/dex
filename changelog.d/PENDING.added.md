**`dex maintain coverage` reports last-run status, recorded mutation strength
and declared-fact coverage beside the test inventory.** Each test carries its
status in the newest `run_results.json` a dbt build wrote (Dex's or a plain
`dbt build`'s), `not_run` where that run did not include it, with the run's
invocation and age. `transform test --mutate` now records each model's result
in `.dex/`, and coverage reads it back as the model's `strength`: `fresh`,
`stale` (naming whether the model's SQL, its tests or a downstream test it ran
changed), `missing` or `untestable`, counted per status with score bands and
no project score. A `declarations` section names, for each declared fact, the
dbt tests and `maintain verify` checks that can judge it, or that nothing does.
All of it is free and connectionless. Store backends gain an optional
`MutationRecords` capability, held to the conformance suite as
`MutationRecordsContract`.
