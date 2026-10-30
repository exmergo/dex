**Native Ossie documents follow upstream's current schema: one flat model per
document, open dialects, and `OSSIE_SQL_2026`.** dex now pins Apache Ossie at
`7a8de20`, whose schema changed under the same `0.2.0.dev0` version string, so
dex read no current upstream document and upstream refused every one dex wrote.

- **A document is one semantic model, flat at its root** (`version`, `name`,
  `datasets`, `relationships`, `metrics`). A document in the earlier wrapped
  `semantic_model: [...]` shape is refused by a rule of its own,
  `wrapped_document`, whose message names the migration: lift the model to the
  root, drop root-level `dialects` and `vendors`, one document per model. Catalog
  names stay `<model>.<dataset>`, so a migrated document reads under the names it
  had.
- **`Dialect` is an open string.** `OSSIE_SQL_2026` is read and parsed as SQL,
  and an expression's top-level `sql` is read as it, ahead of `ANSI_SQL` and over
  an `OSSIE_SQL_2026` dialect entry. Any other token (`DAX`, `THOUGHTSPOT`, or
  one no registry names) is accepted, kept verbatim, never parsed or linked, and
  named in the `non_sql_dialect` note.
- **Dataset-scoped metrics** (`datasets[].metrics`) are listed by
  `explore semantic list` as `dataset.metric`, with lineage to their dataset.
- **`maintain snapshot` records the pinned schema's hash** as the layer's
  `format_version`, so a baseline taken under another pin reports
  `semantic_format_changed` rather than a layer of changed definitions.
- Every upstream example and portability-compliance document at the pin loads
  with no error and no warning, and the corpus records the verdicts that moved:
  `post_pin_thoughtspot_is_refused` is now `opaque_dialects_are_metadata`
  (accepted, with a note), and unequal relationship arity is now upstream's rule
  as well as dex's.
