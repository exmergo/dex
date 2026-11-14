**A build says what the plan introduced, and an unfinished change no longer reads as validated.**
A `transform build --verify --for-plan` now classifies each finding as
`introduced`, `unattributed` or `existing`, judged against the last `--verify`
build recorded before the plan was applied and against the manifest's lineage,
and lists the introduced ones first, in `data.verification.introduced` and at the
head of `data.completion.verification`, so a new high-severity finding is never
cut or buried below inherited ones. Where every deliverable built but part of the
change is still open (a rewrite applied uncompared while its baseline was ready,
an undeclared population change, or an introduced high-severity finding),
`data.outcome` reads `built_unverified` with each reason under
`data.completion.unverified`; `data.evidence.outcome` keeps `validated`.
`transform apply` now refuses a plan whose change scope records an undeclared
population change, naming each under `data.undeclared_population_changes`, until
an assumption records it or `--accept-population-change "<reason>"` accepts it,
which is recorded on the plan.
