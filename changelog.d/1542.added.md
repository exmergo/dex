**A plan says what each generated test costs per production run.** Generated
tests run in production, where Dex never connects, so every one a plan leaves in
the YAML now carries a `cost`: an estimate in the connector's unit, built from
the relations the test reads (a view followed to the tables it reads) and their
sizes in production's catalog, or
`unknown` with the reason. `data.generated_tests.cost.per_run` totals the
`dex_every_run` and `dex_scheduled` tests apart, and says when a total is
partial. Production is named in committed config: `<connector>.prod`, the
namespace the project's models build into there (spelled as the dev namespace
is, never equal to it), and `dbt_prod_target`, the production dbt target. `prod`
is not a source allowlist: explore reads exactly what it read before, and Dex
reads only `prod`'s catalog metadata, never a row. Every guarded dbt call now
refuses `dbt_prod_target` by name, including a name the hard production
denylist misses.
