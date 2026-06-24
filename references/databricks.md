# Connector: Databricks (stub)

Implemented in Phase 4. Lakehouse plus dbt; Unity Catalog governance. Cost
paradigm: DBUs (compute times time). Auth discovered from `~/.databrickscfg`, the
OAuth / SDK default chain, the dbt `profiles.yml` databricks target, and
`DATABRICKS_*` env. Namespace: Unity Catalog `catalog.schema.table` (three-level).
See v8 system design sections 9 and 11.
