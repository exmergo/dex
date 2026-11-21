**`maintain verify` measures a join read through a filtering CTE on Snowflake.**
The join contract carries a filtering CTE's `WHERE` into its coverage probe, and
respelled the probe's key columns the way the relation stores them, but not the
filter's columns: on Snowflake a lower-case `status` named no column of a
relation storing `STATUS`, the probe was refused, and the join was left
unchecked. The filter's columns are now respelled the same way.
