**Ossie and SLayer record declared columns the way the warehouse folded them.**
`maintain semantic` compared each fingerprinted column with the warehouse's
columns by exact string match, so a semantic layer that recorded a column
exactly as its file wrote it reported every column as gone wherever the
warehouse folds unquoted identifiers: upper case on Snowflake, lower case on
Postgres, Redshift, Databricks and Trino for mixed-case authored names. Cube
already folded; Ossie and SLayer now fold their bare-identifier columns, keys
and joins through the same `adapters.fold_identifier` rule. A quoted identifier
is exact, so it still resolves to nothing rather than to a guess.
