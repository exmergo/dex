**The Elementary reader holds the package's tables to a recorded layout, and refuses one it does not know.**
Dex reads `elementary_test_results`, `dbt_invocations`, `test_result_rows` and
`data_monitoring_metrics` as evidence, and their columns can change between
releases without the package calling it a breaking change.
`exmergo_dex_core.maintain.elementary_layouts` records each table's columns at
Elementary 0.26.1, copied from the package's model files at that tag, and
checks the warehouse's catalog before a read touches the tables:

- A release with no recorded layout is refused with `elementary_release_untested`
  (or `elementary_release_unknown` where none can be read), before the catalog
  is asked. The reader's releases are held to the ones the Elementary fixture
  runs, so they can be fewer and never more.
- A column the reader uses that is gone is `elementary_column_missing`, one in
  another type family is `elementary_column_retyped`, and a table the catalog
  lists nothing for is `elementary_table_missing`, each naming the table and the
  column. A catalog listing past 500 columns is refused as
  `elementary_catalog_too_large`.

The refusal is `ElementaryLayoutError`, importable from `exmergo_dex_core`. No
command reads the tables yet: the incident reader (#1471) and
`dex maintain incidents` (#1472) build on this check.
