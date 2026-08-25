# Querying the semantic layer (`explore semantic`)

dex can author the dbt semantic layer (`transform` / `semantic define|update`) and
detect drift in it (`maintain semantic`). `explore semantic` is the third piece:
it *queries* the layer, so an agent can discover metrics and run governed metric
queries. Two backends answer the same commands through one abstraction, and the
difference between them is load-bearing, so it is spelled out here.

## The two commands

- `explore semantic list` returns the catalog in one shape from either backend:
  metrics (name, type, label, description, and the dimensions each can be grouped
  by), dimensions (name, type, label, description), and entities (name, type,
  label, description). This is the discovery surface an agent reads to decide what
  to query. Every label and description is the dbt project's own words, so an
  undocumented project returns identifiers and types alone; an unset field is
  omitted from the payload rather than returned as a null. One divergence: the dbt
  Cloud API exposes no label on entities, so an entity label arrives only from
  `--local`, and a hosted catalog that has entities says so in a note.
- `explore semantic query` runs a metric query and returns a capped, row-major
  result, the same envelope shape as `explore query`. It takes a metric
  positionally after the explicit `query` mode, keeps `--metric <m>` as a
  repeatable backwards-compatible spelling, and optionally takes
  `--group-by <entity__dim>` (repeatable),
  `--where "<jinja>"`, `--order-by <c>`, `--grain <g>`, and `--limit N`.

The query grammar is identical across backends: entity-qualified group-by tokens
(`user__pricing_tier`, `metric_time`), the Jinja filter dialect in `--where`
(`{{ Dimension('session__is_deleted') }} = false`), and a `--grain` that applies
to `metric_time`. Positional metrics and the `--metric`, `--group-by`, and
`--order-by` flags each accept comma-separated lists. The flags may also repeat,
and the two forms mix freely: `--group-by a,b` and
`--group-by a --group-by b` are the same query. `--where` never splits, because a
filter clause carries commas of its own.

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
2. **SELECT-only assertion.** Before anything else touches the statement or the
   connection, the rendered SQL is proven read-only.
3. **Relation pre-check.** The rendered SQL bakes in `relation_name` from the
   compiled manifest, which routinely disagrees with the connection when the
   project was compiled elsewhere. The authority is the connection itself: each
   relation is resolved against the `.dex/` cache first, because that costs
   nothing, and anything the cache cannot resolve is resolved against the live
   inventory, the same listing `explore profile` resolves its arguments against. A
   relation this connection does not have is refused with a precise message before
   the cost handshake, so a namespace mismatch never bills a failed job. A
   same-named table in another database does not satisfy the check.

   The cache records what has been *profiled*, which is a different question from
   what exists, so it can only clear a relation and never condemn one: a model
   `transform build` created minutes ago is in the warehouse and not in the cache,
   and it is queryable straight away. What that costs is PII evidence, not access,
   so a result whose relations carry no profile says which ones and names
   `explore profile` as the fix, rather than the screening quietly weakening to the
   name heuristic with nothing said.

   A refusal is scoped to what the listing can actually settle. A relation in a
   database this connection does not carry is refused: no allowlist could bring it
   into scope, so this is the compiled-elsewhere mismatch. A relation in a listed
   schema that the listing did not contain is refused too, and says so differently,
   because that is a model not built into this target rather than a wrong
   namespace. A relation in an unlisted schema of a connected database is not
   refused: the dataset allowlist is narrower than the dbt project, dex never
   looked there, and refusing would answer a question it did not ask. An inventory
   that cannot be read settles nothing either, and the query proceeds.
4. The **cost-before-spend handshake**, then the active connector.

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
`[semantic-api]` extra, which is an httpx client and nothing heavier: no warehouse
client, no dbt-core, and no SQL parser, because dbt Cloud renders and executes the
query and dex never sees a statement to validate. That makes this the one surface a
pure-remote deployment can run on its own, and the packaging suite holds it to that
by installing the extra alone and asserting the parser is absent.

A library caller can supply the token instead of having it discovered, with
`SemanticSource` on the engine. That matters for a process serving several end
users, where the ambient sources are process-wide and so cannot express one
principal per request; it also makes this the one dex surface that reaches a
warehouse with nothing on the filesystem, since it needs no project, no store, and
no connector. When a token is supplied, nothing ambient is read, the coordinates
included, so a stray `DBT_SL_HOST` cannot redirect the request. The CLI is
unaffected and discovers exactly as described above.

Selecting this backend is explicit: `semantic.backend` defaults to `local`, so a
deployment with no dbt project sets `semantic.backend: dbt_cloud` in config or
passes `--api`. Leaving the default in place there is refused with that fix named,
rather than failing further in on a missing project.

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

Where the floor was all that ran, the result says so, the same way the local
backend discloses an unprofiled relation. The two silences are reported separately
because their fixes differ: a layer that answered and carries no PII metadata for a
dimension wants `meta: {pii: true}` on it in the dbt project, while a
dimension-metadata call that never answered (which degrades every ref to the
heuristic at once) wants retrying.

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
| When only the floor ran | disclosed on the result, naming the unprofiled relations | disclosed on the result, naming the dimensions the layer said nothing about |
| Namespace mismatch | refused before spend, against the connection's own inventory | dbt Cloud resolves its own relations |
| Credentials | the connector's, never in context | a dbt Cloud service token, never in context |
| Host-supplied credential | `ConnectionSource` (the connector's) | `SemanticSource` (the service token) |
| Entity labels on `list` | yes, from the compiled manifest | no: the API's `Entity` type has none, noted on the catalog |
| Extra | `[semantic]` (query); none for `list` | `[semantic-api]` |
