# Connector: PostgreSQL (stub)

Implemented in Phase 4. The operational database AEs pull from. Cost paradigm:
database load (not dollars), guarded with `statement_timeout`, `LIMIT`, a
read-only role, and `SET TRANSACTION READ ONLY`. Auth from `DATABASE_URL` / `PG*`
/ `pg_service.conf` / dbt `profiles.yml`. Namespace: `database.schema.table`. See
v8 system design sections 9 and 11.
