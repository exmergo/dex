**Declarations reach native Ossie layers.** The Ossie layer implements the
derivation hook the other semantic layers do. In a repository that pairs a dbt
project with an Ossie layer, `transform plan` and `transform declare` on a
declared mart derive an Ossie dataset for it: a field per declared column with
its `datatype`, `primary_key` on every grain field, declared aggregations as
dataset-scoped metrics `<column>_<agg>`, a relationship from each foreign key to
the parent's field, descriptions native, and population, assumptions, unit, NULL
rule and additivity in `ai_context` (prose `instructions` plus a structured `dex`
key; never `custom_extensions`, and never a filter). It is appended to the
configured document holding the datasets its foreign keys reference, every other
byte kept, and stored as a plan of its own beside the mart's; otherwise it is
proposed as a document of its own with the `dex config set` that adopts it.
`semantic ossie define|update|plan` take a `declarations` payload too, annotating
a dataset field by field.
