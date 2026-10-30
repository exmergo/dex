**`explore semantic query` and `values` answer on a Cube layer: Cube renders,
Dex executes.** Under `semantic.vendor: cube`, Dex asks the Cube server at
`semantic.cube.url` (a local Cube Core by default) to render the query over
`/v1/sql`, which plans without executing, and runs the SQL through its own
connector under the PII gate and the cost guard (`execution: dex`). It works on
DuckDB, Postgres, BigQuery, Snowflake and Redshift.

- **Before anything is sent**, the PII gate screens every grouped and filtered
  member against the profiled columns behind it.
- **Before anything is rendered**, every member the query names is held to the
  server's `/v1/meta`: its kind, aggregation, SQL, filters and custom
  granularities. A server still serving a model the repository has since
  changed is refused, naming the member. Cube outside dev mode never reloads an
  edited file, so this is the normal state after an edit until the server
  restarts.
- **Before anything runs**, Dex refuses a plan Cube would answer from a
  pre-aggregation (Cube matches a rollup it never built, and the SQL reads that
  table), a plan of more than one statement, an external store, another data
  source, a relation no cube in the repository sits on, and SQL rendered for
  another warehouse. Cube's parameters are written in as literals of the
  connector's dialect, and the statement runs in a UTC session, the setting
  Cube's own drivers use.
- **The dialect is Cube's.** Members are `cube.member`; a time grouping is
  `--group-by orders.ordered_at__month` (custom granularities included);
  `--where` takes Cube's JSON filter, and a date range on the grouped time
  dimension bounds a rolling window.
- **The server is a renderer, nothing more.** Dex calls `/v1/meta` and `/v1/sql`
  only, never `/v1/load`, and gives Cube no warehouse credential. It signs each
  request with a minute-long token minted from `CUBEJS_API_SECRET`, read from
  the environment only and never shown. A Cube server other than one on this
  machine must be named in `DEX_CUBE_ALLOWED_HOSTS`.
- **Needs `[cube]` and `[semantic-api]`**, which the skill wrappers already
  select. `scripts/setup_cube_dev.sh` starts a hardened, render-only Cube Core
  for local use.
