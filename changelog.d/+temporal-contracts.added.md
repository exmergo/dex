**Verification checks the temporal contract of a snapshot-backed status history.**
A snapshot can build while a model over it disagrees about whether the first
observation is a transition, whether an unrelated edit counts, or whether a
timestamp is business or capture time. A model can now declare `history`: its
snapshot, entity and status, and those policies. `maintain verify` and
`transform build --verify` derive each entity's expected transition count, latest
transition and first attainment from the snapshot's versions and the declared
policy, and report disagreements as entity counts (`temporal_*`). Each contract
gets a `temporal_contracts` record keeping the snapshot's execution, the assertions
checked, and what stayed unchecked apart. A policy left out, late-arriving data
whose business and capture order disagree, or a run in which no entity made a
transition is never reported as clean. Supported today: one dbt snapshot, one status
column, one output row per entity.
