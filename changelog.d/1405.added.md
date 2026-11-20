**`transform test --mutate` measures one version of a versioned model.** Name it
by its label, `<model>.v<N>`, the spelling `transform declare` takes. The bare
name measures the version an unversioned `ref()` resolves to (its
`latest_version`), with a warning, and `data.model` names the version measured.
Each version keeps its own mutation record, which `maintain coverage` reads as
that version's strength, and `--downstream` routes a mutant only to the models
that `ref()` that version. A version the model does not list is refused naming
the versions it has, and a version's file stem naming its label.

`maintain coverage --mutate` now refreshes each version of a versioned model by
its label instead of listing it under `refresh.skipped` with `versioned_model`.

The batch price splices each mutant into the CTE dbt names after the model's
alias, which for a version is `<name>_v<N>`, so a mutant of a version or of any
model with a custom alias is no longer priced at the unmutated baseline. An
ephemeral parent with an alias is matched the same way.
