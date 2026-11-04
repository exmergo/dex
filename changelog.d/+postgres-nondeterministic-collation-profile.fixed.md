**A Postgres table with a text column under a nondeterministic collation can be profiled.**
Postgres refuses a regular expression and `replace()` under a nondeterministic
collation (an ICU collation created with `deterministic = false`, such as a
case-insensitive one), and the value-shape probes in `explore profile` use
both, so one such column failed the profile statement for the whole table, and
with it `explore map` and the on-demand profile behind `explore query`. The
probes now read the column as `text` under the `C` collation. The patterns are
ASCII, so matches on text columns are unchanged. Two string types were matched
wrongly before and are now read as text: a `char(n)` value shorter than `n`
no longer has its padding defeat the patterns' end anchors, and a `citext`
column is matched case-sensitively, as on every other connector.
