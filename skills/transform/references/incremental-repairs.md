# Repairing an incremental model so the next run is right

An incremental model is two programs in one file. The full build selects every
row; the incremental run selects only the rows inside its `{% if is_incremental() %}`
branch and adds or merges them into what earlier runs loaded. A first build and
every `--full-refresh` run only the full program. So a full refresh that matches
the source row for row says nothing about the next incremental run, and a defect
in the branch (rows it skips, rows it loads twice) is still there afterwards.

Treat a request about rows that "never show up after the nightly run", "go
missing on incremental runs" or "appear twice" as a request about the incremental
program, and do not call it done on full-refresh evidence alone.

## 0. Read both programs before choosing a repair

Read the model's SQL and config, and write down:

| Fact | Where it is | Why it matters |
|---|---|---|
| Materialization and `incremental_strategy` | The model's `config()` or `dbt_project.yml` | `append` inserts every selected row; `merge` and `delete+insert` replace rows by `unique_key`. A strategy the adapter lacks fails at run time |
| `unique_key` | The same config | Without one, any row the branch selects twice is duplicated |
| The watermark comparison | The `is_incremental()` branch, usually `col > (select max(col) from {{ this }})` | `>` skips every row that arrives later with the current maximum value. `>=` keeps them but selects already-loaded rows again |
| What the bound does on an empty target | The same subquery | `max()` over no rows is NULL, and `col > NULL` is never true: an incremental run into an empty table loads nothing |
| Rows whose watermark column is NULL | Profile the source | No comparison with NULL is true, so only a full build ever loads them |
| `full_refresh: false` | The model's config | A `--full-refresh` leaves this model as it is |

`dex transform test --scaffold <model>` reports what it read as
`data.incremental.watermark` (`column`, `operator`, `input`, `this_column`). It is
`null` where no single comparison with a subquery over `{{ this }}` was found, and
then you read the branch yourself.

Measure what the data says with aggregates, never rows:

```bash
dex explore profile <dev namespace>.<model> --target dev --refresh
dex explore query "select max(<col>) as watermark, count(*) as rows from <source>"
dex explore query "select count(*) as on_watermark from <source> where <col> = <that max>"
```

The profile gives the built model's `row_count` and whether the key is still a
candidate key (a repeated key drops out of `candidate_keys` and shows in
`data_quality`). `explore query` reads the source; it does not reach a dev
relation outside the source allowlist, so take the model's counts from the
profile. The last count is how many source rows share the watermark the next run
starts from. Under `>`, every such row that arrives after the run that set the
watermark is skipped forever.

## 1. Choose the repair

| Defect | Repair | What it needs |
|---|---|---|
| Rows arriving with the current maximum value are skipped | `>=` instead of `>` | A `unique_key` with `merge` or `delete+insert`, otherwise every row on the watermark loads twice |
| Rows arriving with an earlier value (late data) are skipped | A lookback: `col >= (select max(col) from {{ this }}) - <window>` | A `unique_key`, and a stated window. Rows older than the window still need a full refresh |
| An empty target loads nothing | `coalesce((select max(col) from {{ this }}), <lowest value>)` | A literal of the column's type |
| Rows load twice | A `unique_key` and a merging strategy, or deduplication in the model | The key the grain declares |
| Rows with a NULL watermark never load incrementally | A policy: keep them for the full build only, coalesce to another timestamp, or exclude them | An assumption saying which |

Record each choice that changes which rows exist as an `assumptions` entry with its
evidence tier, including the horizon: "rows arriving with a value at or after the
current watermark load on the next run; rows older than that need a full refresh"
is a horizon. A lookback of three days is another. Never promise that arbitrary
backfill is handled.

## 2. Prove the incremental program with a unit test

```bash
dex transform test --scaffold <model>
```

On a model with an `is_incremental()` branch, this plans two dbt unit tests in one
file:

- `test_<model>`: the full build, with dbt's `is_incremental: false` override (dbt
  refuses a unit test on an incremental model that leaves the override out).
- `test_<model>_incremental`: `is_incremental: true` and a `this` fixture standing
  for what earlier runs loaded. Where the watermark was read, the watermark's input
  has one row before, on and after the current watermark plus one NULL, each with a
  comment naming what it probes. `this` is typed SQL (`format: sql`), so the test
  runs on a dev target where the model was never built.

Both `expect:` blocks are empty stubs that fail until you fill them in. For the
incremental test, expect the rows this run inserts or merges, not the whole table:
with `>=`, the rows on and after the watermark; with `>`, only the row after it.
Add rows for what the request is about (a key already in `this`, to see it merged
rather than duplicated). Then plan and apply the test with the repair, and build:

```bash
dex transform apply <plan-id>
dex transform build --target dev --verify --for-plan <plan-id>
```

The unit tests run inside the build, so a failing incremental test fails the
build: `data.completion.blockers[].message` carries dbt's diff of actual against
expected. `dex transform test --mutate <model>` measures the tests against the
full program only: it plants defects in the SQL a full build compiles, which has
no `is_incremental()` branch, so it never flips the watermark comparison. The
boundary rows in the incremental test are what pin that comparison.

## 3. Rebuild the history, then run the incremental program

1. `dex transform build --target dev --select <model> --full-refresh --verify`
   rewrites what the old logic loaded. It is evidence about the full program only.
2. Build again without `--full-refresh`. Nothing new arrived, so the second run has
   to change nothing: the row count holds and no key repeats. A declared grain (a
   `unique` test or `"grain"` in the declaration) makes `--verify` count the
   duplicates; `maintain verify` skips the row-population comparison for an
   incremental model, so the grain is the check that applies.
3. Where the project owns the input (a seed it loads), a second batch is fair
   evidence: change the seed in a plan, build without `--full-refresh`, and check
   the rows on the watermark arrived and nothing repeated.

## 4. What each piece of evidence shows

| Evidence | Shows | Does not show |
|---|---|---|
| A green `--full-refresh` build, row parity with the source | The full program is right on today's data | Anything about the next incremental run |
| The incremental unit test passing | The branch selects the rows the expectation names, for the fixture's boundary cases | That the strategy merges correctly in the warehouse |
| A second plain build changing nothing, grain held | Re-running the branch on loaded data duplicates nothing | That new rows on the watermark are picked up |
| A second batch through a project-owned input | The whole incremental path on real data | Late rows outside the stated horizon |

Report which of these you have. A repair backed only by the first row is incomplete.

## 5. A source the project does not own

Never insert, update or delete rows in a loader's source to manufacture a second
batch, through `explore query`, a raw database client or a hand-edited database
file. Dex is read-only against data. The unit test is the evidence available
there; say that no real second batch was run, and state the horizon the repair
supports.
