# Querying the semantic layer (`explore semantic`)

dex can author the dbt semantic layer (`transform` / `semantic define|update`) and
detect drift in it (`maintain semantic`). `explore semantic` is the third piece:
it *queries* the layer, so an agent can discover metrics and run governed metric
queries. Two backends answer the same commands through one abstraction, and the
difference between them is load-bearing, so it is spelled out here.

## The two commands

- `explore semantic list` returns the catalog in one shape from either backend:
  metrics (name, type, label, description, and the dimensions each can be grouped
  by), dimensions, and entities. This is the discovery surface an agent reads to
  decide what to query.
- `explore semantic query` runs a metric query and returns a capped, columnar
  result, the same envelope shape as `explore query`. It takes `--metric <m>`
  (repeatable), and optionally `--group-by <entity__dim>` (repeatable),
  `--where "<jinja>"`, `--order-by <c>`, `--grain <g>`, and `--limit N`.

The query grammar is identical across backends: entity-qualified group-by tokens
(`user__pricing_tier`, `metric_time`), the Jinja filter dialect in `--where`
(`{{ Dimension('session__is_deleted') }} = false`), and a `--grain` that applies
to `metric_time`.

## Choosing a backend (ambient, like a connector)

The backend is not a per-command mode you must remember; it resolves the way the
warehouse connector does. The default is `.dex/config.yml`:

```yaml
semantic:
  backend: local          # or dbt_cloud
  host: <account>.semantic-layer.<region>.dbt.com   # hosted only, not secret
  environment_id: "70506183145969"                  # hosted only, not secret
```

`--local` and `--api` override the default for one command, which is what lets you
run the same metric both ways and compare (a local build against the deployed
production layer, for instance).

## Local backend (`--local`)

A dbt project must be present, the way DuckDB needs a local file. MetricFlow's
`explain()` renders the metric SQL through a renderer-only client that can never
open a connection or see a credential, and dex then runs that SQL through its own
spine, in order:

1. **PII request-gate.** Each grouped or filtered dimension is resolved through the
   manifest to its physical column, and that column's `.dex/` cache flag decides
   (with `pii_overrides` from `.dex/config.yml` applied). Evidence rules in both
   directions: a dimension whose name reads innocuous is refused when its column is
   flagged, and a profiled, cleared column is not re-blocked by a PII-shaped name.
   When the cache cannot speak to a dimension (never profiled, or a computed
   expression rather than a bare column), the name heuristic is the fail-closed
   floor, so silence never clears.
2. **Relation pre-check.** The rendered SQL bakes in `relation_name` from the
   compiled manifest, which routinely disagrees with the connection when the
   project was compiled elsewhere. Each relation is resolved against the cached
   inventory, and a relation this connection does not have is refused with a
   precise message before the cost handshake, so a namespace mismatch never bills
   a failed job. A same-named table in another database does not satisfy the
   check. Without a cached inventory there is nothing to check against, and the
   query proceeds.
3. **SELECT-only assertion**, then the **cost-before-spend handshake**, then the
   active connector.

**dex owns execution here, so the full cost guard applies** exactly as it does for
`explore query`. `list` is a pure read-view over `target/semantic_manifest.json`
and needs no extra; `query` needs the `[semantic]` extra (MetricFlow) and a
compiled manifest (`dbt parse`).

## Hosted backend (`--api`, dbt Cloud Semantic Layer)

Needs no local dbt project, the way BigQuery needs no local DuckDB: only a host,
an environment id, and a service token. dex talks to the dbt Cloud Semantic Layer
GraphQL API (`createQuery` then poll then read the result). The token is
discovered from `DBT_SL_TOKEN` (then `~/.dbt/dbt_cloud.yml`), held only for the
`Authorization` header, and never written to config or an envelope. Needs the
`[semantic-api]` extra (an httpx client, nothing heavier).

**The cost guard is unavailable on this backend, and dex says so on every
result.** dbt Cloud owns the warehouse connection and executes the query
server-side under its own credential, so dex cannot dry-run to estimate cost and
cannot set a byte or credit ceiling. The hosted backend therefore does not ask for
a `--confirm` (a confirmation dex could not back with a ceiling would be
dishonest); it runs, and it attaches a warning to every result stating that dbt
Cloud, not dex, governs the spend, with the cost paradigm reported as `hosted` and
no estimate or ceiling. Spend is bounded only by the dbt Cloud environment's own
limits.

PII is still screened before the query is sent: a dimension the layer's own
metadata marks as PII is refused, and a name heuristic (the same detector the
profiler uses) is the fail-closed floor for a layer that carries no such metadata.
Grouping or filtering by a PII-shaped dimension (`user__email`) is refused with a
recovery hint before anything reaches dbt Cloud.

## The asymmetry at a glance

| | Local (`--local`) | Hosted (`--api`) |
|---|---|---|
| Renders the SQL | dex, via MetricFlow `explain()` | dbt Cloud |
| Executes the SQL | dex, through the active connector | dbt Cloud, server-side |
| Needs a local dbt project | yes | no |
| Cost surfaced before spend | yes, the full handshake | no: cost guard unavailable, warns on every result |
| Ceiling enforced by dex | yes (`maximum_bytes_billed` / timeout) | no: the dbt Cloud environment's own limits |
| `--confirm` required | yes, on billed connectors | no (nothing dex can gate) |
| PII gate | `.dex/` cache flags on the resolved physical column, name heuristic as the floor | layer metadata plus a name heuristic |
| Namespace mismatch | refused before spend, against the cached inventory | dbt Cloud resolves its own relations |
| Credentials | the connector's, never in context | a dbt Cloud service token, never in context |
| Extra | `[semantic]` (query); none for `list` | `[semantic-api]` |
