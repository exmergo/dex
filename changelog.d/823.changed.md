**A key that only lost uniqueness because a tenant arrived reads `medium`.**
`maintain check` and `maintain grain` used to report `key_lost_uniqueness` at
`high` whenever a single-column key from the baseline stopped being unique, even
when it was never the grain and the rows had only been added along another
column, such as a second repository on a per-repository, per-day table. When the
key is not the grain and is still unique together with a column the baseline
read as constant, the finding now reads `medium`, its detail says so, and
`data.still_unique_as` names the combination. A real duplicate repeats that
combination, so it stays `high`, as does a lost grain. A host that gates on
`high` stops blocking on the first case. The extra combinations are planned and
priced with the axis: on BigQuery they ride the table's one batched statement,
and elsewhere they run only for a table where a key was lost.
