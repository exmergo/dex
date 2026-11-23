**`explore semantic query|values` on a Cube layer now runs `--local` on ClickHouse and Trino.**
Cube renders the SQL and Dex executes it through its own connector, under the PII gate and the cost guard, as on DuckDB, Postgres, BigQuery, Snowflake and Redshift.
Both dialects are held to a recorded golden of what Cube Core v1.8.14 renders.
The statement runs in a UTC session on both, so a timestamp with no zone means what Cube means whatever the machine's or the server's time zone.
A rolling window is the exception on ClickHouse: Cube renders a join on inequalities alone, which ClickHouse 25.3 refuses.
Databricks is still refused by name on `--local`.
