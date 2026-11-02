**`transform build` leads its payload with a bounded `data.completion` summary.**
The summary comes first and stays under 4096 bytes, so a reader whose harness cuts
the output after a few kilobytes still sees whether the change was done. With
`--for-plan` it lists the required deliverables that were built, failed, skipped,
missing or stale, and what blocked each one. A skipped model is never counted as
built, even when dev still holds a relation from an earlier run. It also shows
which deliverables anything checked (`asserted` or `build_only`), what
verification ran, found and suppressed, and the declared obligations by status.
Without a plan, coverage is `unknown` rather than empty. It appears on
failed builds too, and Python callers get the same typed `BuildCompletion`.
`success`, `outcome` and every existing field are unchanged.
