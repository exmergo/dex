**`transform test --scaffold` tests an incremental model's next run, not only its full build.**
A full build and every `--full-refresh` run only an incremental model's full
program, so a repair checked that way could leave the `is_incremental()` branch
skipping rows on the watermark. On a model whose only jinja control flow is
`{% if is_incremental() %}` (with an optional `{% else %}`), the scaffold now plans
two dbt unit tests: `test_<model>` with dbt's `is_incremental: false` override
(which dbt requires on every unit test of an incremental model, so the scaffold
previously could not be built there), and `test_<model>_incremental` with
`is_incremental: true` and a `this` fixture written as typed SQL, so it runs
before the model was ever built. Where the branch compares one column with a
subquery over `{{ this }}`, a new `data.incremental.watermark` names the column,
the comparison, the input and the `this` column, and the fixture puts rows
before, on and after that watermark plus a NULL. A `this` column no input passes
through is typed from the cached profile of the model's relation, or the scaffold
refuses and names the profile to run. A failing unit test's build error now
reads `actual differs from expected` instead of an empty `dbt build failed: `,
and a node's message no longer carries dbt's color codes. The shared procedure
says a full refresh does not show the next incremental run is right. New recipe:
`skills/transform/references/incremental-repairs.md`.
