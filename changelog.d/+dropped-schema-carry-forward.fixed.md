**dropped schemas are no longer carried forward as out-of-scope.** when a
schema holding cached relations was dropped from the warehouse or left empty,
`explore map` treated it as an unobserved namespace and carried its relations
forward indefinitely, causing recurring `table_dropped` findings after every
snapshot. observed namespaces are now derived from what the adapter examined,
so relations in dropped schemas are dropped from the cache while namespaces
outside a narrowed `--scope` continue to be carried forward untouched.
