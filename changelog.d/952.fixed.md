**On DuckDB, `transform build` finishes a seed change that adds, drops or
reorders a column.** dbt loads a seed into the table it already built, so a CSV
that gained or lost a column failed to load, every model reading the seed was
skipped, and the only way out was a `dbt seed --full-refresh` run outside Dex.
A CSV that only reordered its columns loaded each value under the wrong name.
`transform build` now compares each seed's header with the columns of its
relation in the dev target and rebuilds a seed that differs with
`dbt seed --full-refresh` before the build, only when the build selects it.
The envelope names the rebuilt seeds under `data.full_refresh` and says why in
`warnings`.
