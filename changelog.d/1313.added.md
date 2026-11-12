**`semantic.vendor: snowflake` reads Snowflake Semantic View files as the
semantic layer.** Name the Semantic View YAML files in
`semantic.snowflake.files` (the form `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` takes
and `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returns) and `explore semantic list`
(with `--metric`, `--for-dimension` and `--search`, which also matches
synonyms) answers from them. The files are read statically with no Snowflake
connection, and validated by dex's own port of Snowflake's specification and
validation rules, qualified against `verify_only` on a live account. Every
refusal names the file and the element, and a `.sql` file and a legacy Cortex
Analyst model are refused by name.

Every name carries its view:
- logical tables are semantic models `view.table`;
- dimensions, time dimensions and facts are `view.table.member`, with facts
  marked never groupable;
- a metric that is one aggregate is a measure and a simple metric, and one
  metric over another is a ratio.

Primary keys are entities, and every primary and unique key and every
relationship (with its arity) reach `explore relationships|map|profile
--use-project` at confidence 1.0. `maintain snapshot` fingerprints the layer so
that a view read back from Snowflake matches its file, and `maintain grain`
measures the keys and arities. Sample values are never surfaced. Governed `query`
and `values` are not available yet and refuse by name. Install with
`exmergo-dex-core[semantic-views]`.

A deployed semantic view is never inventoried or probed: `SHOW VIEWS` does not
list one, and the inventory skips its members in `SHOW COLUMNS`. The dev-namespace
check still counts it as content.
