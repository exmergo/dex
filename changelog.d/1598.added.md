**`maintain diagnose` names who reads what broke.** Each diagnosis carries
`semantic_readers`: the semantic models whose entities, dimensions and measures
read the failing test's columns, and the metrics built on those measures, read
from dbt's semantic layer with no warehouse query. A member whose expression is
computed is listed as `unresolved` rather than guessed, and a project with no
semantic layer reads `absent`, not an empty list.
