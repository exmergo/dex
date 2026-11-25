**`dex demo` writes a semantic layer that reads its failing column.** The demo's
dbt project gains `models/semantic/`: the time spine dbt requires, an `orders`
semantic model with the metrics `orders`, `revenue` and `average_order_value`,
and an `order_items` semantic model whose primary entity is `order_item_id`,
the column the seeded test fails on. `maintain diagnose` names that entity
beside the cause, and the seeded `target/semantic_manifest.json` lets
`explore semantic list --local` read the layer with no `dbt parse` first.
