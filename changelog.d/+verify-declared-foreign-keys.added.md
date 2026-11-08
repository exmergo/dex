**`maintain verify` judges a project format's declared foreign keys without dbt
artifacts.** A project format other than dbt (one named under `project.format`)
can declare foreign keys, but nothing measured them: verify suppressed the join
contract and parent population for want of a dbt `target/`, and dbt's own
`relationships` test result was the only judge. Each declared foreign key is now
measured against its parent with the join contract's coverage probe, placed
where the format's `model_relations` puts each side, and reported as one record
per key under `data.foreign_key_coverage` (`model`, `column`, `to_model`,
`to_column`, and a `status` of `satisfied`, `contradicted`, `not_applicable` or
`unchecked` with a `reason_code`). Any non-NULL child value the parent lacks is a
high `foreign_key_orphans` finding, since a foreign key states containment. A
model the format places in no relation is `unchecked` with
`no_declared_relation`, never found by name. On a metered connector the probe is
priced in verify's one offer. A dbt project is unchanged: dbt still judges these
through its own `relationships` tests.
