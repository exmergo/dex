# Connector: DuckDB

DuckDB is a first-class product connector and, at the same time, the engine behind
dex's evals and benchmarks (v9 §9). One adapter, three uses: the zero-credential
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

## Capabilities probe

```bash
uv run python -m exmergo_dex_core --path ./warehouse.duckdb connect test
```

returns an envelope whose `data` reports `connector`, `dialect`, `read_only:
true`, `paradigm: free_local`, the engine version, and the resource bounds.

## Why it anchors v0.1

The full Explore, Transform, Maintain loop is built and proven on DuckDB first, with
no cloud accounts, fully deterministic in CI. The cloud connectors and their cost
paradigms layer onto the proven loop in Phase 4. It also matches ADE-bench's
DuckDB mode and Spider2.0-DBT's dbt engine, so the test engine and the benchmark
engine are the same engine.
