**`population.parent` and `references` can name an installed package's model,
and a package parent is checked from plan to build.** A declaration naming a
package's staging model (a Fivetran one, say) was refused because the project
had "no model, seed or snapshot" of that name, so the population-parent checks
were unusable exactly where marts are built on packages. The project is still
looked up first, so a project override wins; otherwise an installed package's
model, seed or snapshot is the parent. Its column is checked against the
package's SQL or the compiled manifest, a column that cannot be read statically
is a warning naming the package instead of a refusal, and a missing one is
refused naming the package and its file. A source named as a parent is refused
with that said. The plan-time `population_parent` check now walks the package's
own `ref()` lineage, so a mart driven from a package's parts table under its
conversations parent is `contradicted` rather than `unchecked`, and column-only
macros such as `fill_staging_columns` and `dbt_utils.star` no longer hide a
lineage. `transform declare` and drafting propose a `references` to a package
model, and the native semantic routes resolve one. `transform build --verify`
and `maintain verify --target dev` also read the namespaces dbt derives from the
dev namespace (`<dev schema>_<name>`, where a package sets `+schema`), so on a
warehouse the package parent's population is measured instead of reported
unbuilt. No package file is ever written.
