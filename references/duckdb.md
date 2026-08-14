# Connector: DuckDB

DuckDB is a first-class product connector and, at the same time, the engine behind
dex's evals and benchmarks. One adapter, three uses: the zero-credential
on-ramp, the dev and CI engine, and the eval and benchmark engine. `dex demo`
supplies the on-ramp's content, so that first use needs no warehouse of your own.

## Auth and target

No credentials. The target is a local DuckDB file (or a directory of Parquet/CSV).
Set it in `.dex/config.yml`:

```yaml
connector: duckdb
duckdb:
  path: ./warehouse.duckdb
```

or pass `--path ./warehouse.duckdb` on any command.

A relative `path:` in `.dex/config.yml` resolves against the project root the
config lives in, not the shell cwd, so the same file opens whether you run from the
project root or a subdirectory (config is found by walking up to the git root). A
live `--path` is typed in your shell, so it stays relative to the current directory.

## Read-only and resource bounds

DuckDB is always opened **read-only** (`read_only=True`). A read-only open of a
nonexistent file fails by design: the adapter attaches to an existing analytical
store, it never creates one, and there is no writable fallback to relax. Because the
work is free and local, there is no cost ceiling, only resource bounds: a memory
limit and a thread cap (defaults: 2GB, 4 threads), overridable from config. The
adapter (`adapters/duckdb.py`) owns all of this.

`dex demo` is the one place a DuckDB file is created, and it is deliberately not the
adapter (see the next section).

## The demo warehouse

```bash
dex demo            # -> ./dex_demo.duckdb and ./.dex/config.yml
dex explore map     # no flags: the config demo wrote points at the file it made
```

`dex demo` generates a small e-commerce warehouse locally: 7 tables and 29,512 rows
(`customers` 1,200, `products` 300, `orders` 5,000, `order_items` 14,000,
`web_events` 9,000, `warehouse_locations` 12, and `returns`, which an interrupted
load left empty). No credentials, no cloud account, no network. The path is
positional and resolves against the working directory; `--path` is refused there,
because everywhere else it names the warehouse dex reads.

**Generated, not committed.** DuckDB's storage format has broken backward
compatibility across releases before, so a committed file could stop opening for a
user on a newer duckdb, and that failure would land on the first command a stranger
ever runs. A generator builds against whatever duckdb resolved and cannot rot; it
also weighs a few KB in the wheel rather than a binary that does not
delta-compress.

**Deterministic.** One pinned seed, a random stream restricted to primitives that
are stable across CPython releases, and no wall-clock anywhere: every date is
measured back from a fixed anchor. A test pins a sha256 over every generated cell,
so a change that would silently move a count quoted in the READMEs fails CI instead.

**Create-only, and off the connector path.** The generator writes a new file and
refuses rather than replace one, with no `--confirm` that can talk past that, and it
never creates a parent directory; it will not write a `.dex/config.yml` where a
project at or above the target already has one, so it cannot shadow a real config
with its own. It lives in `demo/warehouse.py`, imports `duckdb` directly, and
reaches neither the adapter nor the SQL guard, so the read-only rule above keeps no
branch that could be relaxed. The safety spine asserts that mechanically: one test
scans the package for every `duckdb.connect(` and requires `read_only=True`
everywhere except the generator, another opens a freshly generated file through the
adapter and confirms a write is still refused.

**Seeded to be realistically broken**, because a first run that reports a clean bill
of health teaches nothing. `order_item_id` lost its uniqueness to a double-loaded
batch; `sku` mixes numeric and md5-shaped ids from a merged catalogue;
`web_events.customer_id` shares the CRM's column name and none of its values, so
`--verify` collapses the inferred join at 100% orphans; `orders.placed_at` is a
VARCHAR holding timestamps and `web_events.occurred_at` a BIGINT holding epoch
milliseconds; `customers.email` and `full_name` are personal data, while
`warehouse_locations.city` and its coordinates are PII false positives on a
building, and `site_name` is de-rated below the block threshold by its own values.

`generate_demo_warehouse` is exported from the package, so the Python API and
`examples/quickstart.py` build the same fixture.

## Exact distinct counts

Profiling reads distinct counts approximately (`approx_count_distinct`) for scale,
but the adapter also exposes `exact_distinct_counts(identifier, columns)`: one
batched, read-only `COUNT(DISTINCT ...)` that the engine calls to escalate the few
columns sitting near unique, so a real key is never lost to approximation error.
The escalation policy lives in the engine, not the adapter; the adapter only
answers the exact query it is asked for (an empty column list runs nothing).

## Dev-target seeding convention

`transform build` runs against the `dev` target with cwd pinned to the project dir,
so a relative `path:` in `profiles.yml` resolves to a database inside the project,
never a stray file at the caller's shell cwd. If that dev DuckDB file does not yet
exist and the project reads from `sources`, build refuses with an actionable
message rather than letting dbt create an empty database: seed the dev target first
(copy the shared source warehouse, or point the dev `path:` at an existing file). A
source-less project is allowed to create its dev database on first build, with a
warning.

The dev target being the source file is also why `transform init`'s content
preflight skips DuckDB's base namespace: "the file already holds objects" is
true of every working setup, so warning on it would only teach users to skim.
With `--layered-schemas` the layer schemas inside the file (`staging_dev`,
`intermediate_dev`, `marts_dev` on the `dev` target) are genuinely dbt-owned,
and those are checked and warned about when populated.

## Capabilities probe

```bash
uv run python -m exmergo_dex_core --path ./warehouse.duckdb connect test
```

returns an envelope whose `data` reports `connector`, `dialect`, `read_only:
true`, `paradigm: free_local`, the engine version, and the resource bounds.

## Why it anchors v0.1

The full Explore, Transform, Maintain loop is built and proven on DuckDB first, with
no cloud accounts, fully deterministic in CI. The cloud connectors and their cost
paradigms layer onto the proven loop at v0.2. It also matches ADE-bench's
DuckDB mode and Spider2.0-DBT's dbt engine, so the test engine and the benchmark
engine are the same engine.
