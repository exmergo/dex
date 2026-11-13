The `converter_*.yaml` files are the Metric View fixtures of the `databricks`
spoke of [apache/ossie-converters](https://github.com/apache/ossie-converters)
(`databricks/java/src/test/resources/ossie_{fixtureA,fixtureB,tpcds}_metric_view.yaml`,
at commit `82006ba2`), copied unchanged under the Apache License 2.0 their headers
carry. dex ports the converter's construct mapping; these hold the catalog to the
same reading of each construct the converter's Ossie renderings state.
