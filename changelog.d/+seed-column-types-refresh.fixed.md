**`transform build` applies a changed seed `+column_types` on DuckDB.** dbt
reloads a seed into the table it already built, so a `column_types` entry added
or changed after the first build was ignored and the column kept the type
inferred from the CSV, with a green build. The pre-build seed check now also
compares each selected seed's declared column types with its table in the dev
target, through the connector's type aliases, and rebuilds a seed that differs
with `dbt seed --full-refresh`. `data.full_refresh` names it and a warning says
why.
