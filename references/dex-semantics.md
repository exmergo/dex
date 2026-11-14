# Dex Semantics: data that explains itself to your agent

> *Most deadly errors arise from obsolete assumptions.*
>
> Frank Herbert, *Children of Dune*

Your coding agent can write SQL. What it cannot see is what the SQL is supposed
to mean: what one row is, which rows belong, how a number adds up. When that
meaning lives only in someone's head, or in the conversation that wrote the
model, the next schema change breaks it silently. The query still runs. The
numbers are just wrong.

Dex Semantics keeps that meaning with your models, as code. Every model your
agent creates or changes carries a **declaration**. Dex checks the built data
against it, reads it back in the next session, and turns it into the semantic
layer you already use. Three steps:

1. **Declare the rules.** The declaration lives with your models.
2. **Check the results.** Dex holds the built data to the declaration.
3. **Keep the context.** The declaration flows into tests, semantic models and
   the next agent session.

On [ADE-bench](../benchmarks/ade_bench/README.md), the same model (Claude Sonnet
5.5) failed 15 of 75 tasks with Dex and 20 without it: **25% fewer failed
tasks**, 60 of 75 solved against 55. One scored attempt per task; the
comparison is the whole guided Dex workflow, not declarations alone.

The field-by-field contract is
[Declarations: what a model means](../skills/transform/references/declarations.md).
This page is the overview.

## The imperial accounts

An illustrated workflow on synthetic data. The figures are invented and
checked, not taken from a real run.

The Emperor asks his coding agent for production and revenue analytics on
Arrakis. The agent builds the models and records three definitions:

| Term | Meaning |
|---|---|
| Production | Spice extracted |
| Spice sold | Spice shipped |
| Revenue | Sold × agreed price |

Overnight, the source starts splitting each shipment into two lines. The join
still runs, so nothing errors. In the morning the report says production
doubled, from 100 to 200 tonnes, and revenue went from 140k to 280k credits.
The desert produced nothing extra: shipment S01 now arrives as two rows, and
each is counted.

With Dex, the shipment model declares **one row per shipment**. Production
counts harvests; revenue uses the agreed price. When the model is built, Dex
checks it: *S01 and S02: expected 1 row, found 2.* The duplicate shipments are
flagged before anyone reads the report, and the proposed fix (total the lines
per shipment first, then count harvests on their own) comes back as a diff for
the developer to review. With the fix, the report says 100 tonnes again.

The same story is told as a 60-second film,
[The imperial accounts](https://www.youtube.com/watch?v=-iYwYuzZFg0).

## 1. Declare the rules

A declaration travels beside the SQL in the payload of `transform plan` (and of
`semantic define|update|plan`). It states what one row is (`grain`), which rows
are in (`population`), what each column is (`role`, `unit`, `aggregation`,
`additivity`, `null_rule`, `references`), and the decisions the model rests on
(`assumptions`, each with its evidence):

```json
{"model": "fct_shipments",
 "description": "One row per shipment.",
 "grain": ["shipment_id"],
 "population": {"rule": "Every shipment that left Arrakis."},
 "columns": {
   "shipment_id": {"role": "key"},
   "tonnes":      {"role": "measure", "aggregation": "sum",
                   "additivity": "additive", "unit": "tonnes"},
   "revenue":     {"role": "measure", "aggregation": "sum",
                   "additivity": "additive", "unit": "credits"}},
 "assumptions": [
   {"decision": "Which price revenue uses",
    "chosen": "the agreed contract price",
    "evidence": "request"}]}
```

Dex writes the declaration into the model's existing YAML entry, as a
reviewable diff. Where dbt has a construct, it uses it; the rest goes under
`config.meta.dex`:

| Declared | Written as |
|---|---|
| a single-column `grain` | `unique` and `not_null` tests on that column |
| `null_rule: never` | a `not_null` test |
| a `foreign_key` with `references` | a `relationships` test |
| `description` | dbt's own `description` |
| everything else | `config.meta.dex` on the model or the column |

You do not write declarations from nothing. Every plan carries
`data.declarations.<model>`: what Dex **drafted** from what it measured (the
grain a key proves, foreign keys from verified joins, running totals, unit
shapes, and what sibling models do with the same columns), what is
**declared**, and what is still **missing**. Measured facts are drafted with
their evidence; intent (the description, the population, the assumptions) is
never invented, only asked for.

An ambiguous choice does not stop the agent. It decides on the best evidence,
records the choice as an assumption with its tier (`request`, `contract`,
`precedent`, `data` or `default`), and continues. Every assumption comes back
under `data.decisions`, so the reviewer sees each decision the model rests on.

## 2. Check the results

A green build says dbt ran. It does not say the rows are right.
`transform build --verify` and `maintain verify` hold each declaration to the
relation that was actually built, with aggregate-only statements (no row
leaves the warehouse):

| Declared | What Dex reports when the data disagrees |
|---|---|
| `grain` | `grain_broken`, with the duplicate count |
| `population.filters` | `population_contradicted`, with the count of rows that should not be there |
| `population.parent` | `parent_population_missing` and `parent_population_unexpected`, as distinct-key counts |
| `unit: fraction` or `percent` | `unit_out_of_range`, with the observed range |
| `additivity: additive` on a running total | `additivity_contradicted` |
| `history` on a snapshot-derived model | the temporal contract findings, as entity counts |

A change to an existing model is also read against its declaration before it
is applied: a removed population filter, a dropped grain column or a `SUM` of a
column declared `semi_additive` is named under `contradictions` on the plan,
with the side that looks stale.

Dex checks the meaning you recorded. It does not infer your business policy
from the data, and it never applies a fix on its own: every repair is a plan
you review.

## 3. Keep the context

The declaration is part of the repository, so it outlives the conversation that
wrote it.

**Tests.** The grain, NULL rule and foreign keys render as dbt tests, which
every build runs.

**The next agent session.** `explore profile --use-project` and
`explore map --use-project` read each model's declaration back from the YAML
on every run and never from a cache, so a new session starts from the rule the
project records rather than one remembered from a chat. A declared
`references` is also a join, and a declared grain is compared with the keys Dex
measures (`grain_status`).

**Drift.** `maintain snapshot` fingerprints every declaration, and
`maintain semantic` reports a changed population, grain, unit or aggregation as
drift, even when the SQL did not change.

**Semantic models.** A declared mart (a model under `transform.marts_paths`,
default `models/marts`) gets the semantic definition its declaration already
states, in whichever layer the repository reads:

| `semantic.vendor` | What a declared mart gets |
|---|---|
| `dbt` | a MetricFlow semantic model: the grain as the primary entity, foreign keys as entities, dimensions, and measures with their aggregation |
| `slayer` | a SLayer model file: `primary_key`, `many_to_one` joins and saved measures |
| `cube` | a Cube model file: `primary_key`, dimensions, measures and joins |
| `ossie` | an Apache Ossie dataset: fields, `primary_key`, dataset metrics and relationships |
| `databricks` | a Databricks Metric View: fields, measures, formats from the unit, and joins |
| `snowflake` | a Snowflake Semantic View logical table: `primary_key`, dimensions, facts, metrics and relationships |

Three rules hold for every layer. **Metrics are never derived**: a measure is
what a column holds, a metric is a business definition, and it stays yours.
**Nothing is guessed or renamed**: a field a layer cannot carry is named under
`not_carried`, with why. **A population is metadata, never a filter**: the
mart's SQL already enforces it, so a second copy in the layer would drift the
day the two disagree. It is carried in the layer's metadata where the format
has a metadata key, and as the last sentence of the description everywhere.

Snowflake Semantic Views and Databricks Metric Views are written as files in
the repository. Dex deploys nothing to either platform. Which layers Dex can
also query is in the [semantic layers table](../README.md#semantic-layers).

## Adopting it on an existing project

Models written before declarations existed have none, and Dex never adds them
unasked. `transform declare` proposes, in one capped plan, the measured facts
each selected model does not yet state (the grain, keys, foreign keys and
running totals), each with its evidence:

```bash
dex transform declare fct_orders dim_customers
dex transform declare --edits-file intent.json   # every model, with your intent
```

It never overwrites a fact the YAML already states, and it accounts for every
model it skipped, with the reason. Apply the plan, run the same command again,
and the next batch is proposed. It reads the repository and the `.dex/` cache
only, so it is free on every connector.

## Further reading

- [Declarations: what a model means](../skills/transform/references/declarations.md):
  every field, what it renders into, and every check.
- [Querying the semantic layer](semantic-layer.md): the catalog and governed
  queries over the layer a declaration feeds.
- [The command contract](command-contract.md): `transform plan`,
  `transform declare`, `transform build --verify` and `maintain verify` in full.
- [ADE-bench results](../benchmarks/ade_bench/README.md): the run behind the
  25% figure.
