# Querying the semantic layer (`explore semantic`)

dex can author the dbt semantic layer (`transform` / `semantic define|update`) and
detect drift in it (`maintain semantic`). `explore semantic` is the third piece:
it *queries* the layer, so an agent can discover metrics and run governed metric
queries. Two backends answer the same commands through one abstraction, and the
difference between them is load-bearing, so it is spelled out here.

## The three commands

Every result and catalog names which layer answered on all four: `backend` (the
released one-value spelling), plus `vendor`, `deployment`, and `execution`.

- `explore semantic list` returns the layer's objects in one shape from either
  backend: semantic models, metrics, dimensions, entities, and measures. This is
  the discovery surface an agent reads to decide what to query, and the schema
  section below is the field-by-field contract. Every label and description is the
  dbt project's own words, so an undocumented project returns identifiers and
  types alone; an unset field is omitted from the payload rather than returned as
  a null. `--metric <m>` narrows the catalog to those metrics and what they reach,
  which is the whole layer's worth of context down to the part a caller came for,
  and `--for-dimension <d>` asks the reverse question, "what can I slice by this".
- `explore semantic values <dimension> [--metric <m>]` returns one dimension's
  value domain: what a filter on it may be filtered to. It takes exactly one
  dimension, accepts a grain suffix (`user__created_at__month`), and is the only
  dex command that can answer this on a hosted layer at all.
- `explore semantic query` runs a metric query and returns a capped, row-major
  result, the same envelope shape as `explore query`. It takes a metric
  positionally after the explicit `query` mode, keeps `--metric <m>` as a
  repeatable backwards-compatible spelling, and optionally takes
  `--group-by <entity__dim>` (repeatable),
  `--where "<jinja>"`, `--order-by <c>`, `--grain <g>`, and `--limit N`.

The query grammar is identical across backends: entity-qualified group-by tokens
(`user__pricing_tier`, `metric_time`), the answering layer's own filter dialect in
`--where` (dbt's is a Jinja call,
`{{ Dimension('session__is_deleted') }} = false`), and a `--grain` that applies to
`metric_time`. A grain is checked against the grains the layer reports for the
metrics being queried, not against a list dex keeps, so a refusal names what that
metric actually offers and a granularity the project defined for itself is a grain
like any other. Positional metrics and the `--metric`, `--group-by`, and
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
| `semantic_models` | `name`, `label`, `description`, `model_ref` (the transformation-layer model it sits on), `agg_time_dimension` (the model's default time dimension), `primary_entity`, `relation` (the physical relation underneath) |
| `metrics` | `name`, `type`, `label`, `description`, `dimensions` (the tokens it can be grouped by), `semantic_models`, `input_measures` (resolved through any ratio or derived chain), `composition`, `filter`, `time_axis` (what a time grouping resolves to), `queryable_granularities`, `vendor_params` |
| `dimensions` | `name` (the token a query groups by), `type`, `label`, `description`, `definition` (the bare dimension name), `semantic_model`, `queryable_granularities`, `column` |
| `entities` | `name`, `type` (**derived**, see below), `label`, `description`, `roles` |
| `measures` | `name`, `agg`, `expr`, `agg_time_dimension`, `label`, `description`, `semantic_model`, `column` |

`composition` is what a metric is built out of, in portable terms: `measure`,
`numerator`, `denominator`, `expr`, `input_metrics`. It is sparse, and an absent key
means this metric type has no such part rather than that the value is unknown. A
ratio metric therefore arrives with both of its sides, which is what decides
whether a group-by is valid on both and whether the ratio is additive.

`time_axis` is what `metric_time` resolves to for this metric, and it is the field
to read before trusting a time series. `metric_time` is not a dimension of the
layer: it resolves per metric to that metric's measures' own aggregation time
dimension, so on a layer of a dozen semantic models one token stands for a dozen
different physical columns. **More than one entry means the metric's measures
disagree**, which happens whenever a ratio's two sides sit in different models: part
of the number is then bucketed by one timestamp and the rest by another, invisibly,
in a result that looks like any other. The disagreement is reported rather than
resolved, because picking one column would be right about half the number, and the
catalog carries a note naming the metrics it affects.

`queryable_granularities` is the grains a time grouping may ask for, per metric and
per dimension, in the layer's own vocabulary. An **empty list is an answer**: a
categorical dimension has no grain, which is what stops an agent asking one for a
month. An absent field means the backend could not say, which is a different
statement.

`vendor_params` is the boundary of that portability, declared rather than blurred.
MetricFlow's cumulative `window`, its `grain_to_date`, a derived metric's per-input
`offset_windows`, and `requires_metric_time` (a metric that accumulates along a
time axis cannot be queried without one) are real and only mean something under
this vendor's semantics, so they travel under one key instead of being promoted
into the shared shape. `requires_metric_time` is written only when true, so an
absent key is a false.

`roles` is one entry per `(entity, semantic model)` declaration, each carrying
`semantic_model`, `type`, `expr`, `role`, `description`, `column`. That is the unit the layer
declares, and it is why an entity cannot be reduced to a single record: an entity is
`primary` in the one model that keys it and `foreign` in every model that joins to
it, `expr` is the physical join key and differs per model for the same entity, and
each declaration is where a project documents that model's own join, including a
nullable key. `column` is that key resolved to a plain column, which is what makes a
declared entity a drawable join. The top-level `type` is kept and is **derived**:
primary wherever any declaration is primary. Read `roles` for the join graph; read
`type` for a summary.

### The physical link

A semantic layer describes a warehouse the rest of `explore` also describes, and
the catalog carries the join between the two views.

**The relation is on the semantic model. The column is on the element.** A
dimension, an entity declaration and a measure each carry `column`, the physical
column behind them, and each already names its `semantic_model`; that model
carries `relation`. So the address of any element is its column plus its model's
relation, and a relation appears once per model rather than once per element,
which is what keeps the link from dominating a catalog whose byte budget is still
open work.

Which relation backs a metric is therefore two hops, and deliberately so: a
metric names its `semantic_models`, and each of those names its `relation`. A
metric spanning several models has several, which is a fact worth seeing rather
than a value to pick one from.

**A computed expression carries no column.** A measure defined as
`gross - discounts`, or a dimension defined as `base_rate * 2`, has no single
column to name, so `column` is absent rather than guessed. That is not tidiness:
the PII request-gate resolves a dimension to a column and reads that column's
profiled evidence, so a column guessed out of an expression makes the gate screen
the wrong column and report the verdict as evidence-backed. `column` is absent for
the same reason on a dimension row that no single declaration explains: a path
reached through two joins, or `metric_time`, which is one token over as many
columns as the layer has time dimensions.

**A measure's `column` can differ between the backends, and the reason is real.**
`--local` reads the expression the project's author wrote, so a `count` measure on
a plain column carries that column. The hosted API returns the expression dbt
compiled, which for the same measure is a `CASE WHEN ... IS NOT NULL THEN 1 ELSE 0
END`, and that has no single column. Both answers are correct about what they read;
neither backend is guessing. Dimensions and entity declarations do not diverge this
way, because their expressions are passed through rather than compiled.

The link runs in the other direction too. `explore map --use-project` marks each
object with the semantic models that sit on it (`semantic_models` on a map
object), and folds the layer's declared entity graph into the map's join edges;
see [the command contract](command-contract.md). Both are opt-in, because
exploration starts bare.

### Two things the two backends legitimately disagree about

Both are stated in the payload rather than left to be inferred, because a caller
that cannot tell a structural absence from an undeclared field will read the first
as the second.

**`dimension_scope`.** `queryable_paths` means one row per token a query may group
by, join-resolved, so a dimension reached through a join appears once per path that
reaches it. `declarations` means one row per dimension the project declares,
entity-qualified single-hop, which names fewer tokens than a query can actually
use. `--api` is always the first. `--local` is the first too where the join graph
could be resolved, and the second where it could not, which is an install without
the `[semantic]` extra or a compiled manifest that extra's resolver will not read;
that read also carries a note naming the fix. Neither scope is wrong, and the
difference is in the payload because a caller comparing counts across backends
needs to know which it is holding. `definition` plus `semantic_model` is what lets
it see that several paths reach one declaration.

**`unavailable`.** The hosted `SemanticModel` GraphQL type carries only a name, its
`Entity` type has no `label` at all, and its `Measure` type carries no words, so a
hosted catalog declares those gaps per element kind. `relation` is in that list,
which makes the physical link the sharpest asymmetry between the two backends: the
hosted API returns `expr` on dimensions, entities and measures, so a hosted catalog
names the column behind every element and cannot say which table that column is in.
Read `--local` for the relations. A hosted catalog is also reached metric by metric,
so a measure, entity declaration or semantic model that no metric draws on is absent
from it; that one is a note, because it is a property of the layer's shape rather
than a field that cannot exist.

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

### The reverse lookup

`explore semantic list --for-dimension <d>` (repeatable, comma-separated) answers
the question the catalog does not: not "what can this metric be grouped by" but "I
want to slice by pricing tier, what can I slice". It resolves to the metrics
groupable by **all** the named tokens and then narrows the catalog exactly as
`--metric` does, so what comes back is a catalog rather than a list of names. The
two compose, and a named metric that cannot be grouped that way is dropped with a
note naming it rather than silently.

The intersection rather than the union, because metrics that share a group-by are
the ones that can go on one chart against one axis, which is what the question is
usually for.

It is an **inversion of the `dimensions` list each metric already carries**, not a
second call to the layer. That is worth stating because the dbt Cloud API has a
field for this (`metricsForDimensions`) and dex does not use it: the field answers
the empty list both for a name the layer does not have and for a real dimension no
metric shares, so a typo would come back indistinguishable from a fact about the
layer, and it does not accept a metric's own time token at all. Inverting the
catalog closes both, refuses an unknown token by name, and gives the same answer on
either backend. On the layer this was measured against, the inversion reproduced
that field exactly on every case tried.

A token reached only through a join is in the list only when the read resolved the
join graph, so on `--local` without the `[semantic]` extra `--for-dimension` can
refuse a token `--api` accepts; `dimension_scope` is what says which read you have.

Nothing in `list` costs a warehouse query on either backend: one GraphQL round trip
hosted, one compiled-artifact read locally.

## What `values` returns

`explore semantic values <dimension>` returns the distinct values of one semantic
dimension, capped and columnar like every other value-carrying result. It exists
because naming a value is the precondition for writing a filter and nothing else in
dex could reach one: `explore profile` cannot see a semantic dimension, and on a
hosted layer there is no SQL path at all, since dbt Cloud is not a connector.

The payload leads with what was asked:

| field | meaning |
|---|---|
| `dimension` | the token that was asked for, grain suffix included |
| `scoped_to` | the metrics the values were reached through, empty when none were needed |
| `columns`, `types`, `cells`, `row_count`, `truncated` | the domain, capped like `explore query` |
| `backend`, `vendor`, `deployment`, `execution` | which layer answered |
| `query_id` | dbt Cloud's handle for the executed query, hosted only |

**The dimension is resolved before it is asked for.** Both backends look the token
up in the layer's own catalog first, so a misspelling is refused by name (and the
refusal says the token is entity-qualified, which is the likelier mistake), and an
unknown metric passed to `--metric` is refused as a metric rather than surfacing as
a resolution error about the dimension. A trailing grain is split off for that
lookup and put back for the query, against the grains the layer reports rather than
a list dex keeps, so a granularity a project defined for itself works like any
other.

**`scoped_to` changes what the answer means, which is why it is a field and not a
note.** A dimension of one semantic model is answerable on its own: the layer reads
the distinct values of that one relation, and `scoped_to` is empty. A dimension
reached through a join is not answerable that way at all. There is no measure to
join from, so MetricFlow refuses the distinct-values query and dbt Cloud refuses the
same request for the same reason, and the only rendering that exists is one scoped
to a metric that reaches it. That rendering answers a slightly narrower question,
the values present for that metric rather than the domain of the column.

So dex renders the cheap form first and escalates once, to the first metric that
reaches the dimension in name order. Rendering costs nothing on either backend
(nothing is priced until the handshake, and dbt Cloud charges nothing to refuse a
request), so the second attempt is free. The metric it settled on is in `scoped_to`
and named in a note along with the alternatives and the `--metric` flag that
overrides the choice, because the narrowing must never be silent.

**PII is screened harder here than on a metric query.** A metric query returns
aggregates that a dimension merely slices, so a flagged dimension can be dropped
from the grouping and the query still answers something. Here the result *is* the
values, so a flagged dimension refuses the command, and the refusal names the
durable ways to clear a dimension reviewed as not PII (a `pii_overrides` entry in
`.dex/config.yml`, or `meta: {pii: false}` in the project). The evidence is the same
on each backend as it is for a query: the `.dex/` cache's flag on the resolved
physical column locally, the layer's own `config.meta` hosted, fetched one metric at
a time and unioned across every metric that reaches the dimension, with the name
heuristic as the fail-closed floor and disclosed on the result when it was all that
ran.

**The cardinality reported is what came back.** Neither backend's primitive takes a
limit, and an exact distinct count costs a second scan of the same table on every
connector, so dex does not report a number it would have to bill for. A
high-cardinality dimension comes back cut at `query.max_rows` with `truncated` true
and a note saying so, which is the same capping every other columnar result takes.

Cost follows the backend, unchanged: `--local` renders the SQL and runs it through
the full handshake (and needs the `[semantic]` extra, unlike `list`), `--api` is
executed by dbt Cloud and carries the cost-guard-unavailable warning on every
result.

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

`--grain` is checked before any of that, against the grains the project declares
for the metrics in the query, so an impossible grain is named as such rather than
surfacing as a MetricFlow resolution error further in.

**dex owns execution here, so the full cost guard applies** exactly as it does for
`explore query`. `list` reads the catalog through the project seam
(`SemanticCatalogProject.semantic_catalog()`, described in
[`project.md`](project.md)) rather than parsing dbt's artifacts itself, so it needs
no extra and a second project format inherits a working local read path instead of
needing its own parser. For the dbt format that read is
`target/semantic_manifest.json`, so `list` still wants a project compiled at least
as far as `dbt parse`, and says so by name when it is not. `query` needs the
`[semantic]` extra (MetricFlow) and the same compiled manifest.

**The join graph is resolved where it can be.** A metric can be grouped by the
dimensions of every model its own models join to, and computing that from the
shared-entity rule is MetricFlow's job rather than something dex should restate, so
the read asks MetricFlow when the `[semantic]` extra is present. What comes back is
the same set the hosted API returns for the same layer, including paths through two
joins that a single-hop qualification scheme cannot express at all.

Without that extra, `list --local` stays what it has always been, a
dependency-free read of a compiled artifact, and reports the dimensions the project
declares. That is the narrower answer, so it is declared rather than left to look
complete: `dimension_scope` says `declarations`, and a note names the extra. The
same degradation covers a compiled manifest the resolver refuses, because it
validates the whole artifact against its own schema and can reject one dex read
without trouble; losing the catalog over the joins would be the worse outcome.

A dimension the project declares but no metric can reach stays in the list either
way, which is the half of a layer a hosted read cannot see at all.

The PII gate's column resolution comes through the same seam: the format maps every
dimension and entity token to the `(relation, column)` behind it, resolved paths
included, so a token the join resolution added is adjudicated from its column's
evidence rather than dropping to the name heuristic. A token whose reference is a
computed expression is absent rather than guessed, since guessing a column out of
an expression would make the gate over-claim.

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

Both backends screen the group-by tokens and the dimensions a filter clause names.
Reading a clause is the **backend's** job, not the shared gate's, because the
dialect is the layer's: dbt's clauses are Jinja calls and another format's are not,
and a shared parser that matched nothing against a second dialect would screen half
its input while disclosing nothing (a query succeeds, no dimension is blocked, and
no note is emitted, because nothing was found to adjudicate). A backend that cannot
read its own filter dialect therefore refuses filtered queries instead of passing
them.

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

The same request carries the layer's queryable grains per metric, which is what
`--grain` is validated against. It is one more field on a document that was already
being posted, so the pre-query metadata still costs one round trip and nothing
billable.

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
| Physical relation on a semantic model | yes, from `node_relation` | no: declared in `unavailable`, so a hosted catalog cannot say which table a metric reads |
| Physical column on an element | yes, on dimensions, entity declarations and measures | yes, from the API's `expr`, on all three |
| A measure's column | resolved from the expression the author wrote, so a plain `count` carries its column | resolved from the expression dbt compiled, so a plain `count` is a `CASE WHEN` and carries none |
| `dimension_scope` | `queryable_paths` with the `[semantic]` extra, `declarations` without it (declared, with a note) | `queryable_paths`: one row per groupable token, join-resolved |
| Join graph resolved | by MetricFlow, where the `[semantic]` extra is installed | by the API, always |
| `--grain` validated against | the grains the project declares for the metric | the grains the layer reports for the metric |
| `values` renders with | MetricFlow's own distinct-values query, executed here under the full handshake | `createDimensionValuesQuery`, executed by dbt Cloud |
| `values` on a joined dimension | escalated to a metric that reaches it, and said so | escalated the same way, for the same refusal |
| Catalog completeness | the layer as the project declares it | reached metric by metric, so an element no metric draws on is absent |
| Where `list` reads from | the project seam, so a non-dbt format needs no new parser | the dbt Cloud GraphQL API, one round trip |
| Extra | `[semantic]` (query); none for `list` | `[semantic-api]` |
