**Postgres refactor comparison supports `citext` columns.** Postgres extension
types that psycopg reports only by OID are now named in comparison schemas and
diagnostics. `citext` is compared as case-sensitive text, while extension types
without a safe grouping operator class remain refused with their readable name.
