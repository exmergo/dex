# Connector: DuckDB

DuckDB is a first-class product connector and, at the same time, the engine behind
dex's evals and benchmarks. One adapter, three uses: the zero-credential
on-ramp, the dev and CI engine, and the eval and benchmark engine.

## Auth and target

No credentials. The target is a local DuckDB file (or a directory of Parquet/CSV).
Set it in `.dex/config.yml`:

```yaml
connector: duckdb
duckdb:
  path: ./warehouse.duckdb
```

or pass `--path ./warehouse.duckdb` on any command.

## Read-only and resource bounds

DuckDB is always opened **read-only** (`read_only=True`). A read-only open of a
nonexistent file fails by design: dex attaches to an existing analytical store, it
never creates one. Because the work is free and local, there is no cost ceiling,
only resource bounds: a memory limit and a thread cap (defaults: 2GB, 4 threads),
overridable from config. The adapter (`adapters/duckdb.py`) owns all of this.

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
