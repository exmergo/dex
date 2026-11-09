**A declaration on a project override of a package model is refused in Dex's
words when the package documents that model, and the override no longer owes
one.** dbt gives a package's YAML entry for a model the project disables to the
project's override of it, so any project entry for the same name failed dbt's
parse as two entries for one resource, with a message cut before the file names.
`transform plan` now refuses the declaration, or a hand-written entry, before dbt
runs, naming the package's file and the project's, and says to declare the models
that read the override instead. The override gets no "declares nothing" warning
and no missing intent, and `data.declarations.<model>.package_documented` names
the package's entry. `transform declare` skips it as `package_documented`.
`data.prerequisites` names the package's file as `package_entry` on the
`project_override` route. The prerequisite-repair recipe, the declaration
guidance and the procedure now say the same thing.
