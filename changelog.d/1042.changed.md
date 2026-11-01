**`transform plan` reads set operations from their first branch.**
a model whose outermost query is a `UNION`, `UNION ALL`, `EXCEPT`, or
`INTERSECT` previously skipped contract comparison because set operations were
treated as unresolvable. `transform plan` now reads the model's output columns
from its first branch (or from the union of branches for `UNION ALL BY NAME`).
declarations pick this up, refusing a declared column absent from a union as
they do for a direct SELECT list.
