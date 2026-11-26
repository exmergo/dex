**A unit or a measure's additivity now writes an Elementary column monitor.**
With `monitors.vendor: elementary`, a column declared `unit: fraction` or
`unit: percent` gets an `elementary.column_anomalies` under the column with
the monitors `min` and `max`, which watch it shift inside the bound
`dex_unit_range` already checks. A column declared `role: measure` gets one
with the monitors its `additivity` makes meaningful: `sum`, `average` and
`zero_count` for `additive`, `min`, `max` and `average` for `semi_additive`,
and `average` for `non_additive`, so `sum` is never written where adding the
column means nothing. A column both rows reach gets one test whose list is
the union. Each is written on the model's one time column, carries the
population's `where_expression`, is reported in `data.monitors` with the
declaration fields it came from, and names them in `data.decisions`. A
hand-written `column_anomalies` on the same column and time column is left
as written, and a column flagged as personal data gets none.
