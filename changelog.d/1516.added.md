**A declared population filter renders a test, `dex_population_filter`.** With
`declarations.render_tests` on, each `includes` or `excludes` list in a
model's `population.filters` renders a count-only column test, tagged
`dex_every_run`, that fails on every run where a row breaks the filter, and the
plan writes its macro as a reviewable diff. It agrees with `maintain verify`'s
`population_contradicted` count for count on every connector: a NULL breaks an
`includes` list and never an `excludes` one. A filter on a column the model
does not output renders nothing and says `column_not_output`.
