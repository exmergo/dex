# exmergo-dex-core

The portable, Apache-2.0 analytics-engineering engine behind
[dex](https://github.com/exmergo/dex). All non-trivial logic lives here; the
Claude Code skills and the cross-agent `AGENTS.md` are thin wrappers that drive it
through one stable command contract.

dex is the agent-native analytics engineering toolkit: explore an unfamiliar
warehouse, transform raw data into clean dbt models, and maintain a semantic model
on top, then keep all three in sync as the data underneath changes. Read-only
against your data; every change is a reviewable diff.

## Install

```
pip install "exmergo-dex-core[duckdb]"
```

Connector client libraries live behind extras, so the zero-credential DuckDB
on-ramp installs only `duckdb` and `sqlglot`:

```
exmergo-dex-core[duckdb]       # the on-ramp and the eval/benchmark engine
exmergo-dex-core[snowflake]
exmergo-dex-core[bigquery]
exmergo-dex-core[databricks]
exmergo-dex-core[postgres]
```

## The command contract

Every subcommand prints exactly one sanitized JSON envelope to stdout and nothing
else; credentials and raw rows never cross that boundary. State persists in
`.dex/`, so subcommands are stateless and the agent orchestrates multi-step flows.

```
dex connect test --path data.duckdb
```

See [`references/command-contract.md`](../../references/command-contract.md) for
the full surface and the envelope spec.

## Status

v0.1 is the full Explore. Transform. Model. loop on DuckDB. This is the Phase 0
foundation: the command contract, the dex-native canonical model and `.dex/`
layout, the OSI validator against a pinned schema, and the eval + safety spine.
The explore/transform/model/reconcile engines are filled in Phases 1 through 3.

## License

Apache-2.0.
