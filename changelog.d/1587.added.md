**`dex demo` now shows an incident and its cause, offline.** Beside the warehouse
and its config, the demo writes a small dbt project under `analytics/`: a source
over `orders` and `order_items`, a staging model over each, and a `unique` and a
`not_null` test on each key. Its `target/` holds the artifacts of a seeded
`dbt build` in which `unique_stg_order_items_order_item_id` failed, because of
the batch the demo loads twice.

- `dex maintain incidents` then returns that incident from dbt's run results,
  with `first_observed: unknown`, since one build is all they hold.
- `dex maintain diagnose` names the loader's duplicate delivery of
  `order_item_id` as the cause, measured on the source the model reads.
- The printed next steps end with both commands.
- The demo still runs no dbt, installs no package and opens no network
  connection; the artifacts are generated and pinned by a digest.
- An existing `analytics/` beside the target is refused before anything is
  written. Under an existing `.dex/config.yml` no project is written, as no
  config is.
