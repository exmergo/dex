**`dex demo` now shows the incident from Elementary's tables, with when it began.**
The demo seeds the history of seven weekly builds of its dbt project.

- **Elementary's tables**, in `main_elementary`: `dbt_invocations` and
  `elementary_test_results`, built from the reader's recorded 0.26.1 layout, and
  the layout's two other tables, empty. One week the package's hooks were off,
  and it holds no row.
- **The package**, declared at 0.26.1 in `packages.yml`, with that release
  recorded where the resolver reads it.

`unique_stg_order_items_order_item_id` passes in four builds and fails in the last
two, because of the batch the demo loads twice. So `dex maintain incidents`
returns the incident from the `elementary` source, with the build it was first
observed in, and the week with the hooks off read as a missed run inside its
onset. `dex maintain diagnose` names the loader's duplicate delivery over that
break.

- The builds are dated back from when `dex demo` ran, so the incident stays open
  for about three weeks. Every other date in the demo is still fixed.
- `explore map` leaves the package's tables out of the ranking, so the tour's
  counts are unchanged.
- The demo still runs no dbt, installs no package and opens no network
  connection.
