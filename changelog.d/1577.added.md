**`explore semantic query|values` on a Cube layer now runs `--local` on Databricks.**
Cube renders the SQL and Dex executes it through its Databricks connector, under the PII gate and the cost guard, as on the other seven connectors.
The dialect is held to a recorded golden of what Cube Core v1.8.14 renders.
The statement runs in a UTC session, so a time bucket and a date range mean what Cube means whatever the workspace's or the warehouse's time zone.
