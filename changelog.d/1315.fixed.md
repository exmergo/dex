**`maintain reconcile` advice follows the semantic layer's claim, and no longer
promises a test edit with no project to put it in.**

- A lapsed key a semantic layer declares used to get the dbt advice ("change
  the declared grain"). It now names the layer's route to change the key.
- A Snowflake relationship's arity is no longer advised as a cardinality to
  declare. It is inferred from the referenced table's key.
- In a repository whose only layer is a semantic one, a lost key no longer
  warns that a `unique` test has nowhere to go.
