**`parent_population` now reports `parent_not_built` when a parent is missing.**
before, a declared parent omitted from a build reported `not_measured` with
`no single relation in the warehouse matched the parent`, confusing an unbuilt
parent with ambiguous matching. when no warehouse relation matches, the record
now reports `parent_not_built`. if a build selection is present, the message
names the selection, suggests adding the parent to `--select`, and notes that
a parent that is not an ancestor is never built by `+<model>`. multiple matches
continue to report `not_measured`.
