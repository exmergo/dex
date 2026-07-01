# dbt to OSI export mapping (stub)

A future mapping for the dormant OSI exporter. Because dbt is the source of truth
and OSI is not emitted in v1, this describes how the dbt semantic model (MetricFlow)
will project to an OSI document when the exporter is switched on: entities,
dimensions, measures, and metrics map to OSI's core, and anything OSI cannot
express rides in `custom_extensions` under the `DEX` vendor name. See the v9 system
design, §8.
