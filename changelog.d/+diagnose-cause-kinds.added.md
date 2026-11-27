**`dex maintain diagnose` checks every cause owner, not only a duplicate
delivery.** Each open incident now gets an answer for each owner. All are
judged over the failing test's lineage, at the runs that bound the break:

- the loader retyping a source column, from the schema axis between the
  bounding snapshot and the warehouse after it;
- the project changing a model in the commit range, from `maintain verify` on
  the changed model;
- a package upgrade, from the versions `package-lock.yml` pins at the two
  commits;
- an edited seed, from the seed file's git hash at the two commits;
- the schedule building a parent model after its child, from both models'
  build times in Elementary's `dbt_run_results`.

The build-times read is one bounded statement of ids and times, priced with
the other checks. A profile of `dbt_run_results` withholds its compiled SQL,
messages and adapter responses. Where the evidence is missing, a candidate is
`not_checked` with a reason, such as `unpinned` with no lock committed or
`no_measured_before` with no snapshot. A `no_cause` answer therefore names
what it could not rule out.
