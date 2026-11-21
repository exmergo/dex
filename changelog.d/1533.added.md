**`transform plan --no-parse` stores a plan without running dbt.** The library
form is `DexEngine.plan(..., no_parse=True)`. It is for a host that must not run
repository-controlled code where it authors and parses the plan in a sandbox of
its own, as `semantic_plan(no_parse=True)` already allowed for the semantic
layer. No parse of the post-change project runs, none of a declared mart's
derived semantic model (reported `unvalidated`, left out of the plan and offered
for `semantic plan`), and no
compile. Every structural check still runs. A model with Jinja control flow is
refused, since only dbt's compile can check it, and so is a `toggle` contract.
