# exmergo-dex-core

The portable, Apache-2.0 analytics-engineering engine behind
[dex](https://github.com/exmergo/dex). All non-trivial logic lives here; the
Claude Code skills and the cross-agent `AGENTS.md` are thin wrappers that drive it
through one stable command contract.

dex is the agent-native analytics engineering toolkit: explore an unfamiliar
warehouse, transform raw data into clean dbt models and a semantic layer on top,
and maintain all of it as the data underneath changes. Read-only against your data;
every change is a reviewable diff.

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
exmergo-dex-core[all]          # every connector at once
```

## The command contract

Every subcommand prints exactly one sanitized JSON envelope to stdout and nothing
else; nothing reaches agent context except through that envelope. Credentials
never cross it, and data values cross only from profiled, PII-cleared columns,
bounded and capped by the query firewall. State persists in `.dex/`, so
subcommands are stateless and the agent orchestrates multi-step flows.

```
dex connect test --path data.duckdb
```

See [`references/command-contract.md`](../../references/command-contract.md) for
the full surface and the envelope spec.

## Status

Early and under active development; expect pre-release versions. Today the engine
runs the Explore stage on DuckDB end to end: it ranks what matters in an unfamiliar
warehouse, profiles columns selectively, flags PII, surfaces grain and
data-quality warnings, infers joins and verifies them with overlap probes
(`--verify`), and executes agent-authored ad-hoc SELECTs behind a PII-aware query
firewall (`explore query`), all read-only. Transform (dbt models and the semantic
layer) and Maintain (drift and
reconcile), and the cloud connectors (BigQuery, Snowflake, Databricks,
PostgreSQL), are in progress and report `not_implemented` until they land. The
foundations are in place: the command contract, the
canonical model and `.dex/` layout, the OSI validator against a pinned schema,
and the eval and safety spine.

## License

Apache-2.0.
