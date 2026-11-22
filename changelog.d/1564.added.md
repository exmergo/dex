**The engine reads test outcomes from Elementary's tables as incident observations.**
`exmergo_dex_core.maintain.elementary_incidents` reads `dbt_invocations` and
`elementary_test_results` in the schema the package writes, and returns the
observations and run history `fold_incidents` and `build_timeline` take. It
selects ids, status and severity tokens, an anomaly test's column and metric,
each run's times, commit and package version, and counts. It never selects a
sampled row or a value a test failed on.

- `plan_read` holds both tables to the release's recorded layout before writing
  a statement, and refuses where the layout check refuses.
- `read_elementary` sends every statement through the query firewall, the
  source allowlist, the cost handshake and the query ledger, as `explore query`
  does. The first read on a fresh cache profiles the two tables and reports
  `first_read` with what the profiling was priced at.
- One call reads at most 2,000 invocations and 100,000 result rows. It counts
  first and refuses past either bound (`elementary_read_too_large`) before it
  reads a row. `since` reads a later window.
- Result rows are deduplicated on `id`, and each run gets one place in the
  order, by `run_started_at` and never by `created_at` alone. A result whose
  run has no `dbt_invocations` row is counted and not observed.
- It refuses, each with its own code: an empty history, an invocation with no
  readable start, an invocation written twice with different starts, a run
  written by a release other than the one checked, a status it does not map,
  one result id holding two records, and an id too long to hold.

The refusal is `ElementaryReadError`, importable from `exmergo_dex_core`. No
command calls the reader yet: `dex maintain incidents` (#1472) builds on it.
