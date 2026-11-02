**`transform plan` reports what an identifier conversion does to a model's
entities.** Each `cast`, safe cast (`try_cast`, `safe_cast`, `try_to_number`),
`lower`, `upper` or `trim` that a plan adds to an identifier column is listed under
`data.identifier_conversions`. It covers filters, joins and projections, on new and
edited models. Aggregate counts show how many distinct identifiers reach the
conversion, how many fail it (NULL keys counted apart), how many merge into one key
(`7` and `007`), how many change spelling, and how many still match across a join.
On an edited model, the prior and authored identity sets are compared too, so an
identity change shows even when the row count is unchanged. Each count states its
denominator. An intentional filter elsewhere in the model is respected. Status is
`lossless`, `declared`, `undeclared` (with a warning listing the
contract-compatible options) or `unchecked` with a `reason_code`. Assumptions
take an optional `column`, which is how a deliberate exclusion becomes
`declared`. The counts are free on DuckDB and priced with row attribution
elsewhere. No value is returned. An unsettled conversion on a required model
appears under `data.completion.obligations.identifier_conversions` on a
`--for-plan` build (#861).
With `--attribute-rows`, a plan for a new model can now spend on these counts, and a
cost-guard refusal is raised as it is for row attribution.
