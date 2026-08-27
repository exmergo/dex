# Querying the semantic layer (`explore semantic`)

dex can author the dbt semantic layer (`transform` / `semantic define|update`) and
detect drift in it (`maintain semantic`). `explore semantic` is the third piece:
it *queries* the layer, so an agent can discover metrics and run governed metric
queries. Two backends answer the same commands through one abstraction, and the
difference between them is load-bearing, so it is spelled out here.

## The two commands

Every result and catalog names which layer answered on all four: `backend` (the
released one-value spelling), plus `vendor`, `deployment`, and `execution`.

- `explore semantic list` returns the layer's objects in one shape from either
  backend: semantic models, metrics, dimensions, entities, and measures. This is
  the discovery surface an agent reads to decide what to query, and the schema
  section below is the field-by-field contract. Every label and description is the
  dbt project's own words, so an undocumented project returns identifiers and
  types alone; an unset field is omitted from the payload rather than returned as
  a null. `--metric <m>` narrows the catalog to those metrics and what they reach,
  which is the whole layer's worth of context down to the part a caller came for.
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

## What `list` returns

A semantic layer is a graph, not three lists of names: semantic models each sit on
one physical relation and own the entities they join on, the dimensions they can be
sliced by, and the measures their metrics are built from, and a metric is composed
out of those measures and may span several models. The catalog carries that graph
as flat lists with provenance on every element, rather than nesting elements inside
their model, because a flat lookup is what the PII gate and every consumer already
do and it is also the shape a non-dbt format can satisfy.

Five scalars lead the payload, so a caller reading a truncated result sees them
first:

| field | meaning |
|---|---|
| `backend`, `vendor`, `deployment`, `execution` | which layer answered, on the axes described below |
| `dimension_scope` | what one `dimensions` row is: `declarations` or `queryable_paths` |
| `unavailable` | fields this backend structurally cannot supply, per element kind |
| `scoped_to` | present only when `--metric` narrowed the catalog, naming the metrics |

Then the five lists. Every field below is optional except `name` and `type`, and an
unset one is absent rather than null:

| list | fields |
|---|---|
| `semantic_models` | `name`, `label`, `description`, `model_ref` (the transformation-layer model it sits on), `agg_time_dimension` (the model's default time dimension), `primary_entity` |
| `metrics` | `name`, `type`, `label`, `description`, `dimensions` (the tokens it can be grouped by), `semantic_models`, `input_measures` (resolved through any ratio or derived chain), `composition`, `filter`, `vendor_params` |
| `dimensions` | `name` (the token a query groups by), `type`, `label`, `description`, `definition` (the bare dimension name), `semantic_model` |
| `entities` | `name`, `type` (**derived**, see below), `label`, `description`, `roles` |
| `measures` | `name`, `agg`, `expr`, `agg_time_dimension`, `label`, `description`, `semantic_model` |

`composition` is what a metric is built out of, in portable terms: `measure`,
`numerator`, `denominator`, `expr`, `input_metrics`. It is sparse, and an absent key
means this metric type has no such part rather than that the value is unknown. A
ratio metric therefore arrives with both of its sides, which is what decides
whether a group-by is valid on both and whether the ratio is additive.

`vendor_params` is the boundary of that portability, declared rather than blurred.
MetricFlow's cumulative `window`, its `grain_to_date`, and a derived metric's
per-input `offset_windows` are real and only mean something under this vendor's
semantics, so they travel under one key instead of being promoted into the shared
shape.

`roles` is one entry per `(entity, semantic model)` declaration, each carrying
`semantic_model`, `type`, `expr`, `role`, `description`. That is the unit the layer
declares, and it is why an entity cannot be reduced to a single record: an entity is
`primary` in the one model that keys it and `foreign` in every model that joins to
it, `expr` is the physical join key and differs per model for the same entity, and
each declaration is where a project documents that model's own join, including a
nullable key. The top-level `type` is kept and is **derived**: primary wherever any
declaration is primary. Read `roles` for the join graph; read `type` for a summary.

### Two things the two backends legitimately disagree about

Both are stated in the payload rather than left to be inferred, because a caller
that cannot tell a structural absence from an undeclared field will read the first
as the second.

**`dimension_scope`.** `--local` returns one row per declaration, entity-qualified
and single-hop, so the count is the number of dimensions the project declares.
`--api` returns one row per token a query may group by, join-resolved, so a
dimension reached through a join appears once per path that reaches it and a layer
with joins reports substantially more rows. Neither is wrong. `definition` plus
`semantic_model` is what lets a caller see that several paths reach one
declaration.

**`unavailable`.** The hosted `SemanticModel` GraphQL type carries only a name, its
`Entity` type has no `label` at all, and its `Measure` type carries no words, so a
hosted catalog declares those gaps per element kind. A hosted catalog is also
reached metric by metric, so a measure, entity declaration or semantic model that no
metric draws on is absent from it; that one is a note, because it is a property of
the layer's shape rather than a field that cannot exist.

### Scoping a large layer

`explore semantic list --metric <m>` keeps the named metrics and everything
reachable from them: the measures they read, the semantic models those live in, the
dimensions they can be grouped by, and the entities declared in any surviving
model. It costs no extra round trip and no warehouse query, the shape is unchanged,
and `scoped_to` names the metrics so a subset is never mistaken for the layer. An
entity keeps **all** of its declarations even where the scope dropped the model
they name, because pruning them would turn a primary entity into a foreign one,
which is a false statement about the layer rather than a smaller one. A metric name
the layer does not have is refused by name rather than returning an empty catalog.

Nothing in `list` costs a warehouse query on either backend: one GraphQL round trip
hosted, one compiled-artifact read locally.

## Choosing a backend (ambient, like a connector)

The backend is not a per-command mode you must remember; it resolves the way the
warehouse connector does. The default is `.dex/config.yml`:

```yaml
semantic:
  vendor: dbt             # which semantic-layer format answers
  deployment: local       # or dbt_cloud
  host: <account>.semantic-layer.<region>.dbt.com   # hosted only, not secret
  environment_id: "70506183145969"                  # hosted only, not secret
```

Two configured axes, and one that is derived:

| axis | values | what it decides | set how |
|---|---|---|---|
| `vendor` | `dbt` | which semantic-layer format answers | configured, ambient per repo |
| `deployment` | `local`, `dbt_cloud` | which endpoint or artifact is read | configured |
| `execution` | `dex`, `vendor` | **whether dex's cost guard applies** | derived, never configured |

`execution` is the one the guards read, so it is reported on every result rather
than left to be inferred from a backend name. `dex` means dex rendered the
statement and ran it through its own connector, so the full cost handshake
applied; `vendor` means the semantic layer owns the warehouse connection and dex
never held a statement it could price or cap.

`semantic.backend` is the released spelling of the first two axes as one value and
is still accepted: `local` reads as dbt plus the local deployment, `dbt_cloud` as
dbt plus the hosted one. Setting `backend` and `deployment` together is fine while
they agree and refused when they contradict, because a config dex accepts and then
ignores is worse than one it refuses.

`--local` and `--api` override the **execution** axis for one command, which is
what lets you run the same metric both ways and compare (a local build against the
deployed production layer, for instance). They are not vendor flags: a repo has one
semantic layer, chosen once, exactly as it has one connector.

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
`explore query`. `list` reads the catalog through the project seam
(`SemanticCatalogProject.semantic_catalog()`, described in
[`project.md`](project.md)) rather than parsing dbt's artifacts itself, so it needs
no extra and a second project format inherits a working local read path instead of
needing its own parser. For the dbt format that read is
`target/semantic_manifest.json`, so `list` still wants a project compiled at least
as far as `dbt parse`, and says so by name when it is not. `query` needs the
`[semantic]` extra (MetricFlow) and the same compiled manifest.

The PII gate's column resolution comes through the same seam: the format maps every
dimension and entity token to the `(relation, column)` behind it, and a token whose
reference is a computed expression is absent rather than guessed, since guessing a
column out of an expression would make the gate over-claim.

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

Selecting this backend is explicit: the deployment defaults to `local`, so a
deployment with no dbt project sets `semantic.deployment: dbt_cloud` in config or
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

That metadata is fetched one metric at a time and unioned, in a single request
that carries one aliased field per metric. The API's `dimensions(metrics:)` field
returns the dimensions common to **all** the metrics listed, not their union, so
asking about a whole multi-metric query at once shrinks the authoritative map as
the query grows and drops everything outside the intersection to the name
heuristic. Asking per metric is what keeps the layer authoritative for every
dimension a query touches, and the aliases keep it to one round trip. A group-by
token that carries a time grain (`user__created_at__month`) is looked up under the
dimension name too, since no dimension name carries a grain.

Where the floor was all that ran, the result says so, the same way the local
backend discloses an unprofiled relation. The two silences are reported separately
because their fixes differ: a layer that answered and carries no PII metadata for a
dimension wants `meta: {pii: true}` on it in the dbt project, while a
dimension-metadata call that never answered (which degrades every ref to the
heuristic at once) wants retrying.

## The asymmetry at a glance

| | Local (`--local`) | Hosted (`--api`) |
|---|---|---|
| `execution` reported | `dex` | `vendor` |
| Renders the SQL | dex, via MetricFlow `explain()` | dbt Cloud |
| Executes the SQL | dex, through the active connector | dbt Cloud, server-side |
| Needs a local dbt project | yes | no |
| Cost surfaced before spend | yes, the full handshake | no: cost guard unavailable, warns on every result |
| Ceiling enforced by dex | yes (`maximum_bytes_billed` / timeout) | no: the dbt Cloud environment's own limits |
| `--confirm` required | yes, on billed connectors | no (nothing dex can gate) |
| PII gate | `.dex/` cache flags on the resolved physical column, name heuristic as the floor | layer metadata, fetched per metric and unioned, plus a name heuristic |
| When only the floor ran | disclosed on the result, naming the unprofiled relations | disclosed on the result, naming the dimensions the layer said nothing about |
| Namespace mismatch | refused before spend, against the connection's own inventory | dbt Cloud resolves its own relations |
| Credentials | the connector's, never in context | a dbt Cloud service token, never in context |
| Host-supplied credential | `ConnectionSource` (the connector's) | `SemanticSource` (the service token) |
| Entity labels on `list` | yes, from the compiled manifest | no: the API's `Entity` type has none, declared in `unavailable` |
| Semantic model metadata on `list` | label, description, `model_ref`, default time dimension | name only: the API's `SemanticModel` type carries nothing else |
| `dimension_scope` | `declarations`: one row per declaration, single-hop qualified | `queryable_paths`: one row per groupable token, join-resolved |
| Catalog completeness | the layer as the project declares it | reached metric by metric, so an element no metric draws on is absent |
| Where `list` reads from | the project seam, so a non-dbt format needs no new parser | the dbt Cloud GraphQL API, one round trip |
| Extra | `[semantic]` (query); none for `list` | `[semantic-api]` |
