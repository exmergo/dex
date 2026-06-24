# Connector: Snowflake (stub)

Implemented in Phase 4. The dominant dbt and AE warehouse. Cost paradigm: credits
(warehouse size times time), so minimize warehouse runtime, not bytes. Auth
discovered (never asked) from `connections.toml`, key-pair (RSA), SSO
(externalbrowser), the dbt `profiles.yml` Snowflake target, and `SNOWFLAKE_*` env.
Namespace: `database.schema.table`. See v8 system design sections 9 and 11.
