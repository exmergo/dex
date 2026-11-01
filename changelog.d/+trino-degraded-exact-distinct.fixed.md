**Trino profiling no longer counts distinct values of nested and sketch columns.**
Since the exact-count pass began measuring columns with no distinct count,
`explore profile` and `explore map` on Trino could run `COUNT(DISTINCT)` on columns
such as `array(varchar)`, `json` or `hyperloglog`, which report only a non-null
count. Those columns are now skipped, so they keep no distinct count and cannot
fail the scan for the rest of the table.
