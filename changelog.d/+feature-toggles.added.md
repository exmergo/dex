**A feature flag that stays live is checked in every model, on both values.**
"Hide these columns when the flag is false" was easy to finish half-way: gate
the models that feed one mart, and a ledger nothing in the marts reads, a model
that inherits the column through `select *`, or a model whose table is empty
still outputs it with the flag off. A green build and a row comparison cannot
see either. `transform plan` now takes a `toggle` contract beside the edits:
`variable`, `on_value`, `off_value` (both required, never read off a default),
the governed `columns` and any `exempt_models`. `data.toggle` compiles every
model of the project in a throwaway copy at each value, and the tree before the
plan at the on value, probing no table's rows. It names each model that still outputs
or reads a governed column with the flag off, loses one with it on, or loses or
varies a column the toggle does not govern, with a `verdict`, `completeness`,
`contract.producers` and the package readers it leaves out of scope.
`transform compare` compiles both sides at the on value (`data.proposed_vars`),
and `transform build --verify --for-plan` judges each built relation's columns
from metadata at the value the build used. Findings are
`toggle_column_present_when_off` and `toggle_column_missing_when_on`, and the
other value is reported `unchecked` (`branch_not_built`). An off build no longer
reports a governed column that `schema.yml` documents as `column_missing`. The
contract travels in the plan document and its digest. Every other command
refuses a `toggle` key rather than dropping it. New recipe:
`skills/transform/references/feature-toggles.md`. (#1113)
