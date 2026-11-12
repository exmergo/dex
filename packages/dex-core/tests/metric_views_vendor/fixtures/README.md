The `converter_*.yaml` files are the fixtures of the `databricks` spoke of
[apache/ossie-converters](https://github.com/apache/ossie-converters)
(`databricks/java/src/test/resources/ossie_{fixtureA,fixtureB,tpcds}_{metric_view,ossie}.yaml`,
at commit `82006ba2`), copied unchanged under the Apache License 2.0 their headers
carry: `converter_<name>.yaml` is the Metric View side and
`converter_<name>_ossie.yaml` the Ossie side. dex ports the converter's construct
mapping, so the catalog has to read each construct the way the Ossie side states
it. dex also ports the converter itself (the Ossie bridge, #1260), so each pair has
to convert into the other in the direction the converter pins: fixture A and tpcds
export, fixture B imports.

The Ossie side is deliberately not named `*.ossie.yaml`. Those files are dex's own
Ossie corpus, and the converter's documents name key columns that are not
declared fields, which dex's Ossie reader refuses.
