**`semantic.vendor: databricks` reads Databricks Metric View files as the
semantic layer.** Name the Metric View YAML files in `semantic.databricks.files`
and `explore semantic list` (with `--metric`, `--for-dimension` and `--search`)
answers from them, read statically with no workspace connection and validated by
dex's own port of the Metric View YAML 1.1 rules. Every refusal names the file and
the element. Each file is one view, named after its stem, and a `.sql`
`CREATE VIEW ... WITH METRICS` file is refused by name. Fields are dimensions keyed
`view.field`, single-aggregate measures are measures and metrics, and
`MEASURE(a) / MEASURE(b)` is a ratio. `explore relationships|map|profile
--use-project` carry each join and each `rely.at_most_one_match` key at
confidence 1.0, `maintain snapshot` fingerprints the layer, and `maintain grain`
measures those keys and each join's declared arity. `explore semantic list
--search` also matches the `synonyms` a field or measure declares. Governed
`query` and `values` are not available yet and refuse by name. Install with
`exmergo-dex-core[metric-views]`.
