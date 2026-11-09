**`transform plan` compiles models using column-listing macros to verify their
SELECT list against `schema.yml`.** A model using column-introspection macros
(`dbt_utils.star`, `dbt_utils.get_filtered_columns_in_relation`,
`adapter.get_columns_in_relation`, or `fill_staging_columns`) is now compiled
alongside control-flow models so its expanded SELECT list is checked against
declared column contracts. Where compilation is not possible, the plan reports
that the SELECT list could not be checked statically.
