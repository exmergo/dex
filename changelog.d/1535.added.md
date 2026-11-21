`transform plan` renders `dex_temporal_contract` for each measure a declared
`history` names, where `declarations.render_tests` turns it on: a count-only
model-level test, tagged `dex_scheduled`, that fails with the entities whose
column disagrees with what the snapshot's versions and the declared policy give,
the same count `maintain verify`'s `temporal_transition_count_mismatch`,
`temporal_latest_transition_mismatch` or `temporal_first_attainment_mismatch`
states. A measure `maintain verify` would leave unchecked renders none, with its
reason: `snapshot_not_ancestor`, `lineage_unavailable`, `hard_delete_records`,
`policy_missing`, or `snapshot_config_unread` where the snapshot's config is an
expression. `transform declare` proposes it for a history declared before the
switch was on.
