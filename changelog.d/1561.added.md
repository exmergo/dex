**A coverage timeline that never reads an unobserved interval as good.**
`exmergo_dex_core.maintain.timeline` lays one source's runs out in invocation
order on a named heartbeat (`dbt_invocations`, else the orchestrator's run
history, else the `maintain snapshot` series, and it says which and why), and
marks every interval that recorded no outcome as unobserved with its reason: a
gap longer than the expected schedule allows, a run with Elementary's hooks or
test uploads off, a missed snapshot, the time before retention, or a run that
did not check the test. Each run carries its commit, or `unknown`. Per test, the
history gives the last verified run and each break, after the last observed
pass up to and including the first observed failure, widened by any unobserved
interval between them. Incidents now carry their coverage on that timeline and
their `onset` break.
