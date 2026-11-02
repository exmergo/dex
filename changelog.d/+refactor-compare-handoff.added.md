**A rewritten model's plan now points to `transform compare` before apply, and apply reports what it found.**
The plan's `data.refactor_comparison_offer` gives each rewritten model its own
command and `static_attribution`, which says whether row attribution established
anything (it cannot when the SQL calls a macro). `data.next` names the comparison
ahead of `transform apply`. Each verdict is recorded on the plan, and
`transform apply` and `transform build --for-plan` report it, or report
`not_compared`, without refusing anything. `transform compare` now returns
`inputs_unsynchronized` when the last build left a parent errored or skipped,
because both sides would read that parent's outdated relation. The shared
procedure tells agents to compare a change meant to keep results, and how to act
on each verdict.
