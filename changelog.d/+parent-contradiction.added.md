**`transform plan` warns when a model declaring `population.parent` is driven from
an unrelated relation, on a new model as well as an edited one.** The warning names
the driving relation, the parent and both fixes, under
`data.declarations.<model>.contradictions` (`check: population_parent`). A relation
in the parent's lineage, either direction, stays silent, as does a RIGHT, FULL or
CROSS join that keeps the parent's rows. A driver Dex cannot read is `unchecked`.
`population_contract` says whether a population is `prose` or `executable`, and
`data.completion.obligations.prose_population` names required models nothing can
check.
