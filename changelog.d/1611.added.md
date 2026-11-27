**A declared daily deadline now writes an Elementary `data_freshness_sla`, and
`maintain verify` judges it.** A time column's `freshness` may state a
`deadline` (`"07:00"`), its required IANA `timezone`, and optional `days`,
beside or instead of a `max_lag`; a lag is never read as a deadline. With
`monitors.vendor: elementary`, the plan writes `elementary.data_freshness_sla`
with that `timestamp_column`, `sla_time`, `timezone` and `day_of_week`, carrying
the population's `where_expression`, reported in `data.monitors` and named in
`data.decisions` by its `freshness.deadline` field (withheld below Elementary
0.23.1, which first ships it, and where a population value holds a quote on a
connector that escapes it with a backslash, which the package's own message
re-escaping breaks). `maintain verify` and `transform build --verify`
raise `deadline_missed` once the deadline has passed with no value from today,
by counts alone, with one `data.freshness` record per check. A deadline on a
`business` basis column is written with a warning: the test needs a value from
today, which a load of the previous day's events never holds.
