**A key cast to an integer is measured on Postgres.** Postgres has no cast that
returns NULL on failure, so `transform plan` (`data.identifier_conversions`)
and `maintain verify` (join coverage) reported every cast key there as
`unchecked` with `reason_code: no_safe_cast_on_connector`. A cast to
`smallint`, `integer` or `bigint` is now measured through a guarded cast: a
value that is not an integer, or is out of range for the type, counts as
failed instead of failing the probe. A cast to any other type still reports
`no_safe_cast_on_connector`.
