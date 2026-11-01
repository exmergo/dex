**A temporal contract no longer reads as `satisfied` for a model that does not
descend from its declared snapshot.** Verification walks the manifest's dependencies, free and
with no connection. A model that does not descend from `history.snapshot` is
`unchecked` with `reason_code: snapshot_not_ancestor`, naming the snapshot and any
other snapshot it descends from, and no mismatch is reported against a history it
never read. A manifest that cannot answer is `lineage_unavailable`.
