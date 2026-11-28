**A statement BigQuery will not plan inside a statistics probe reads
`execution_failure`, not `internal`.** The exact distinct-count, composite-key,
value-domain and combined table probes that `explore profile`, `explore map`,
`explore relationships` and the `maintain` commands run on BigQuery let a
`BadRequest` or `NotFound` from the dry run or the job insert escape bare, so
the envelope read `internal`, which a host takes for a crash and may retry. They
now raise the same typed refusal `explore query` already did, carrying
BigQuery's own words and `data.stage: "plan"`: nothing ran and nothing was
billed, so the statement has to change (#1000).

The job insert is typed this way for every BigQuery statement, and a
bytes-billed refusal answered there takes the same widen-and-retry it takes when
the job fails on it, rather than reading as a plan refusal. The combined table
probe still hands back to the separate probes when its own dry run is refused,
and profiling still degrades to metadata when BigQuery refuses an aggregate
batch at the dry run or the insert.
