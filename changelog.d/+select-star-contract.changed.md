**`transform plan` checks the column contract through `select *`.** The comparison
with `schema.yml` was skipped for every model ending in `select * from final`. It now
follows a star through CTEs, subqueries and joins, then to a referenced model's SQL,
then to a cached profile, which the warning names. A skipped comparison says what
stopped it. Existing projects may see new warnings. Two checks that read the same SELECT
list also see through CTEs: a declaration naming a column absent from `final` is now
refused, as it already was for a direct SELECT list, and the resolved-key convention
warning can fire on more models.
