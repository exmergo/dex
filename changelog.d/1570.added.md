**The engine reads test outcomes from dbt's `run_results.json` as incident observations.**
`exmergo_dex_core.maintain.run_results_incidents` turns the test results in the
newest `run_results.json` a dbt `build`, `test`, `run`, `seed` or `snapshot`
left into the observations and run history `fold_incidents` and
`build_timeline` take, under the source name `dbt_run_results`. It needs no
warehouse connection and no Elementary package.

- A later run that ran no test (a `dbt run`, a `dbt seed`) is passed over, with
  a note, so it does not hide the failures of the build before it. Runs that
  never tested read `missing` (`no_test_results`), not clean.
- One invocation is all the file holds, so every incident read from it alone
  has `first_observed: unknown`, and every interval before the run is
  unobserved.
- `pass` is observed good; `fail`, `error` and `warn` are observed bad, a
  warning keeping severity `warn`; `skipped` is unobserved, never good.
- Only a test the current manifest still has, enabled, is observed. A test
  carrying a claim id in `config.meta.dex.claim` is keyed by it.
- A `compile`, `parse` or `ls` is never read. A run no dbt log beside the file
  names is refused (`unlogged_invocation`), and a file naming no invocation is
  refused (`no_invocation_id`).
- From a result row it reads the test's id and status only, never its message,
  compiled SQL or failure count.

`exmergo_dex_core.transform.claims.manifest_claims` reads each test's claim id
from a compiled manifest. No command calls the adapter yet: `dex maintain
incidents` builds on it.
