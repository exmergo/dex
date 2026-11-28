**`dex maintain diagnose` names a source whose rows arrived after the run
read them.** The pipeline ran on time and the loader was late, so the run
built the model without rows that had not landed yet. Diagnosis now reads that
as a cause, `source_arrived_late`, owned by the loader. It is confirmed when
some rows of the failing run's window landed after that run started, and every
row of the last passing run's window had landed before it started. The arrival
time is the source's dbt `loaded_at_field`, or a column a staging model
declares with `freshness.basis: capture`, and never a business timestamp. The
window is read from the failing test's own time column, or from the time
column the tested model declares. The read is one count statement per source,
priced with the other checks, and returns counts only. Where no arrival time
or window can be read, the answer says `no_capture_time`, `no_run_times` or
`window_unknown` instead of ruling the loader out. `dex demo` now seeds this
case too: a `shipments` source whose last day landed minutes after the newest
build started, so the tour ends at two incidents and a cause for each.
