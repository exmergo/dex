**`transform plan` no longer refuses a model that lists a relation's columns while
compiling.** `adapter.get_columns_in_relation`, which `dbt_utils.star`,
`dbt_utils.get_filtered_columns_in_relation` and Fivetran's `fill_staging_columns`
call, was refused as an unsupported `Describe` on DuckDB and as direct database
access on BigQuery, so a control-flow model using them could not be planned. That
includes the project override of a package staging model that the
prerequisite-repair procedure recommends. Dex now answers it from the scoped
inventory's column metadata (names and types, no rows), built by the adapter's own
dbt column class so a nested BigQuery column keeps its `STRUCT<...>` type, counts
each listing toward `query.max_statements` and names it in `warnings`. A raw `DESCRIBE`, `SUMMARIZE`,
`SHOW`, `PRAGMA` or `information_schema` read stays refused, and the refusal now
names `adapter.get_columns_in_relation` rather than `explore profile`.
