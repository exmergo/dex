**A small-domain dimension column writes an Elementary `dimension_anomalies`.**
With `monitors.vendor: elementary`, a column declared `role: dimension` whose
cached domain is at most 20 distinct values gets its own
`elementary.dimension_anomalies` (`dimensions: [<column>]`) on the model's one
time column, carrying the population's `where_expression`. Each column gets its
own test, because the package groups a `dimensions` list by the columns'
combination. The domain is read from the exploration cache, never measured. A
column over the bound is `domain_too_large`. A column with no cached count is
`not_profiled`, and a warning names the `explore profile` that would supply
one. A column Dex flags as personal data is `pii_flagged` at any size. A
monitor Dex wrote on a column that no longer qualifies is reported `stale` and
kept. `transform declare` now also returns `data.monitors`.
