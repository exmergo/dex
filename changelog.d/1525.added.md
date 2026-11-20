**A declared parent population renders a test, `dex_parent_population`, in both
directions.** With `declarations.render_tests` on, each `population.parent`
renders two count-only model tests, tagged `dex_scheduled`: `missing` fails
with the number of eligible parent keys the model lacks, and `unexpected` with
the number of the model's keys the parent lacks, and the plan writes its macro
as a reviewable diff. Each agrees with `maintain verify`'s
`parent_population_missing` and `parent_population_unexpected` count for count
on every connector, with the declared `population.filters` scoping which parent
keys are owed and a filter on a column the parent lacks skipped, as
`maintain verify` skips it. A grain or key column the model does not output now
renders no model-level test and says `column_not_output`. Each
`data.generated_tests` row of a kind that renders several tests on one model
names which one it is under `variant` (`missing` or `unexpected`, `includes` or
`excludes`).
