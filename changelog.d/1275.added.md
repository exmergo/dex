**The Ossie bridge proposes a Databricks Metric View from an Ossie document, and an
Ossie document from a Metric View.**
`semantic databricks define|update|plan --from-ossie <file>` and
`semantic ossie define|update|plan --from-metric-view <file>` convert one file of
the other format into the configured layer, through
dex's port of the apache/ossie-converters `databricks` spoke. The port is held to
the converter's fixtures in both directions, with the same notices and the stash
JSON byte for byte, and to its example, dialect and round-trip suites. Every
dropped or rewritten element is named in `data.bridge`, each notice marked as the
converter's or dex's. Dex adds only what its own validators need: a field marked
`key_only` for a join column no field exposes, which its export leaves out again;
a key or metric naming a field reads its column, and an imported metric names
the fields its columns are exposed by; `expression.sql` is read; and a fact
dataset's metrics become measures. Each flag takes several files, stored as one
plan. `--dataset-metrics` writes fact-only measures
as dataset-scoped metrics on import (#957). `--fact` names the dataset a view is
on, and `--into` places the file. A configured output path is validated and
stored as a plan for `transform apply`. Any other path stores no plan and comes
back with the `dex config set` that adopts it. Nothing is deployed.
