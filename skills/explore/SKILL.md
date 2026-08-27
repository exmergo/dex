---
name: explore
description: 'Use this whenever you need to know what is actually in a database, warehouse, or DuckDB file before you trust it: ranked inventory of what exists, column profiles, PII detection, grain and data-quality problems, verified join inference, Mermaid ER diagrams, guarded ad-hoc SQL probes, and k-means segmentation, producing a draft map without dumping the whole schema into context. Trigger it on an unmet precondition, not on any particular phrasing: if you are about to write or fix SQL against tables whose columns, types, grain, or join keys you have not verified in this session, use this FIRST. That includes dbt work: building a staging or mart model, fixing a broken model, or debugging wrong numbers, whenever the ticket names source tables without spelling out their schema. It also applies mid-task: if you are partway through and hit a table you have not inspected, stop and use this rather than guessing column names or firing off one-off SELECTs. Also use it for direct questions like "what''s in my duckdb", "which tables matter", "how do these tables relate", "is this data any good", "any PII in here", "how many orders have no customer", or "cluster my customers". Explore is read-only and writes nothing but the .dex/ cache. It does not author the model: pair it with transform, which writes the change once you know what you are writing against. To reconcile a project that has fallen out of sync, use maintain.'
---

# Explore

Make sense of a warehouse or a local DuckDB database the way an analytics
engineer does: rank what matters, drill selectively, and persist a draft map.
This is the flagship, fully read-only skill. It absorbs profiling and
relationship inference as capabilities; they are not separate skills.

## How to drive it

Run the engine through the wrapper. It prints one sanitized JSON envelope and
nothing else; read the envelope and decide the next step.

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

dex runs its engine through `uv`, which is a prerequisite and is not installed by
Claude Code. If the shell reports `uv: command not found`, stop and tell the user
to install it (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or
`brew install uv`, or `pipx install uv`), then re-run. Never fall back to raw
Python, `pip`, or a database CLI to do the work another way: the guardrails live in
the engine, so any other path is unguarded.

If the user has no warehouse to point at and wants to see what dex does, `demo`
generates one: a seeded local DuckDB warehouse plus the `.dex/config.yml` for it,
with no credentials and no network, so every subcommand below then runs with no
flags. It only ever creates, so it refuses rather than touch a file that already
exists. Offer it rather than assuming it: a user who does have a warehouse wants
that one read, not a fixture built beside it.

Subcommands, in the usual order:

1. `connect test --path <file.duckdb>` confirms a read-only connection and
   reports capabilities.
2. `explore inventory --rank` returns a ranked object summary (counts and sizes,
   never rows).
3. `explore profile <objects>` (space- or comma-separated) returns column
   profiles, PII flags recorded as (column, category, confidence) and never
   example values, plus candidate keys, the likely grain, and data-quality
   warnings (e.g. a non-unique id that will fan out on joins). A generic
   `*_name` flag's confidence is refined by value-shape evidence from the same
   scan, in both directions: person-shaped values corroborate it, a closed
   reference vocabulary or long labels de-rate it below the firewall's blocking
   threshold, and missing evidence changes nothing (the flag itself is never
   removed). Distinct counts
   are approximate for scale, but any column that looks unique within
   approximation noise is escalated to an exact COUNT(DISTINCT)
   (`distinct_count_exact: true`), so uniqueness and grain verdicts rest on
   proof; a `~` prefix in a warning marks a count that is still approximate.
   A requested object whose cached profile is still fresh (same connector,
   schema unchanged, within `profile_freshness_hours`, default 24) is served
   from the cache (`cache_hit_count`) instead of re-scanned, so profiling a
   table `map` just wrote costs nothing to spend; pass `--refresh` to force a
   re-scan when the source changed in a way the free metadata check cannot see.
4. `explore relationships` returns inferred and declared joins with confidences,
   plus notes explaining what the inference examined (so an empty list is
   meaningful). Add `--verify` to measure each inferred join with an aggregate
   overlap probe (orphan fraction, confidence adjusted). A declared join has two
   sources: a `relationships` test, and (with `--use-project`) an entity two
   semantic models share, which the layer states outright with the key named per
   model. `declared_by` on an edge names that entity, `semantic_join_count` says
   how many came that way, and the notes call out the ones name-based inference
   did not find, which is the interesting set: a semantic layer routinely joins
   columns that share no name at all.
5. `explore map` writes or updates the `.dex/` cache and returns the map
   (`--verify` works here too). Alongside the counts, `data.objects` gives each
   top-ranked object its row count, detected grain, candidate key, notable
   columns (each carrying the role that earned it a place: `grain`, `key`,
   `join`, or a PII flag) and data-quality findings, and `data.edges` gives the
   join edges in the same shape `explore relationships` returns. With
   `--use-project` each object also carries `semantic_models`, the semantic models
   that sit on that relation, which is what separates a load-bearing table from a
   merely large one: empty means nothing in the layer reads it. **Read that
   payload instead of chaining `profile` and `relationships` to re-derive it**;
   go to those two when you need one object in full, or a value domain, which
   `map` never carries. It is budgeted: 25 objects by rank, 12 columns per
   object, 40 edges, 5 findings per object. Every cap binds in every mode and
   every elision is counted in `notes` and in an `elided_*` field, so an empty
   `notes` means nothing was cut. `--detail` widens the selection to every column
   and to objects that were inventoried but never profiled, and lifts no cap; it
   spends nothing, unlike `--full`. Past 50 objects it profiles only the top 25
   by rank and says so in `notes` (with `skipped_count`); pass `--full` to
   profile everything. On a re-map, objects skipped this run keep their prior profiles
   (`carried_forward_count`), each stamped with its own `profiled_at` so
   staleness is visible instead of column detail silently vanishing. A selected
   object whose cached profile is still fresh (same connector, schema unchanged,
   profiled within `profile_freshness_hours`, default 24) is reused without a
   re-scan (`cache_hit_count`), so re-runs cost nothing to spend; pass
   `--refresh` to force a full re-profile when the source changed in a way the
   free metadata check cannot see (e.g. rows changed but the schema did not).
   `explore relationships` and the standalone `explore profile` reuse fresh
   profiles the same way.
6. `explore diagram [--full]` renders the cached map as a Mermaid ER diagram in
   `data.mermaid`. Free and connectionless (it reads the cache, never the
   warehouse), so it is safe to re-run while shaping the picture. **Reproduce the
   string verbatim in a fenced ```mermaid block so the human can see it, and
   write it to a `.mmd` or a markdown file when they want one on disk: the
   engine deliberately writes no file.** Never redraw or "tidy up" the diagram
   by hand. The glyphs are claims the engine derived from evidence, and a
   plausible-looking cardinality you supplied is exactly the overclaim this
   command exists to prevent: declared joins are solid, inferred dotted, and an
   unverified inference never says "exactly one". A solid line labelled with a
   semantic entity is a join the semantic layer declares; look the entity up with
   `explore semantic list`. Read `notes` before presenting
   it, since it states any object or column that was left out; `--full` widens
   from the default (profiled, joined objects and their grain, key, join, and
   PII columns) to everything eligible.
7. `explore query "<SELECT ...>" ["<SELECT ...>" ...]` answers ad-hoc questions
   the fixed commands don't cover: you write the SQL, the engine's query firewall
   refuses or bounds it. Pass a statement per argument, or `--sql-file <path>`
   for a longer list, and ask a whole chain of questions in one call rather than
   one call each; each statement is judged and answered on its own, so a refusal
   on one does not cost you the others, and `data.results` carries one entry per
   statement. A table you have not profiled, including a model you just built, is
   profiled for you and the statement then runs, so probing something new is one
   call rather than three; the envelope says what it profiled, and on a metered
   connector that profile is priced into the same confirmation as the statements.
   Results come back row-major and capped; a refusal names the offending column
   and the fix, so one rewrite is enough. Read `${CLAUDE_SKILL_DIR}/references/probe-playbook.md` before
   writing a probe: it maps common questions to effective probe shapes.
8. `explore cluster <object> [--features a,b,c] [-k N]` runs k-means over a
   bounded sample of the object's numeric columns and returns the segment
   structure: per-cluster sizes and fractions, centroids (each coordinate is a
   cluster's mean of that feature, an aggregate), the silhouette score, and,
   when `-k` is omitted, the k it picked plus the silhouette sweep it chose from.
   Requires the `.dex/` cache (run `map`/`profile` first) so features can be
   auto-selected from profiled numeric, non-PII, non-key columns; pass
   `--features` to choose them yourself (naming a PII column, or a key, opts it
   in deliberately, and only its mean is ever reported). A key is never a
   feature: its mean is meaningless, and a fact table is mostly keys plus a
   handful of measures, so clustering on them just partitions surrogate ranges.
   Keys are the unique columns, the columns that join out (from the joins `map`
   inferred), and the columns named like one; prefer `map` over a bare
   `profile` here, because without inferred joins a foreign key is caught only
   if its name gives it away. The notes name every excluded column, so check
   them before trusting a result. Two things the silhouette alone will not tell
   you, both of which the notes will. A cluster holding under 1% of the sample
   is an outlier pocket, not a segment, and it pushes the score up precisely
   because it sits so far out: report that as outlier detection, or re-run with
   `-k` to split the bulk. And on connectors that cannot seed a sample the draw
   changes per run, so two runs can disagree on k; the envelope's
   `sample_repeatable` says which case you are in, and comparing runs across
   different draws is meaningless. Only aggregates cross the
   boundary: the sample rows are clustered in-process and never enter context.
   On a metered connector it takes the same cost handshake as the scanning
   commands below (only the feature columns are scanned, and a dialect-aware
   sample clause reads a fraction), so surface the estimate and get a budget
   first. Needs the `[cluster]` extra (scikit-learn); the wrapper installs it
   automatically for this subcommand.
9. `explore semantic list` and `explore semantic query` reach the dbt semantic
   layer. `list` is discovery, and it returns the layer's objects rather than
   three lists of names: semantic models (the unit the layer is organized
   around, each with the transformation model it sits on and its default time
   dimension), metrics (which dimensions each can be grouped by, the measures it
   reads, a ratio's two sides, any filter that makes it a subset, the grains it
   can be queried at, and the time column a time grouping resolves to),
   dimensions (the token to group by, plus the definition, owning model, and
   queryable grains behind it),
   entities (one declaration per semantic model, each with its own join key, so
   the declared join graph is readable), and measures (the aggregation and
   expression the number is made of, which is often a conditional rather than a
   column). It also reaches the warehouse the rest of `explore` describes: a
   semantic model carries the `relation` it sits on and each dimension, entity
   declaration and measure carries its `column`, so "which table is behind this
   metric" is the metric's `semantic_models` followed to their relations, and
   `explore profile <relation>` is the next call. An element defined as an
   expression carries no column rather than a guessed one. The hosted backend
   exposes no relation at all and declares that in `unavailable`, so use `--local`
   when you need the physical side. Read a metric's `input_measures` through to those measures before
   trusting what a number counts, and read its `time_axis` before trusting a time
   series: `metric_time` is not one column but each metric's own aggregation time
   dimension, so two entries there mean the metric's measures bucket by different
   timestamps and grouping by `metric_time` splits the number between them. On a large layer, `list --metric <m>` narrows
   the catalog to those metrics and what they reach, at no extra cost; the
   payload names the scope in `scoped_to`, so a scoped catalog is never mistaken
   for the whole layer. `list --for-dimension <d>` asks the reverse question,
   returning the metrics groupable by all the named tokens: use it when you know
   the slice you want rather than the metric, and to find the metrics that can go
   on one chart against one axis. It inverts the dimension list each metric
   already carries, so it costs nothing extra and refuses an unknown token by
   name rather than answering "no metrics". Two payload fields carry differences
   between the
   backends rather than leaving them to be inferred: `dimension_scope` says
   whether a dimension row is one declaration or one groupable path (which is
   why the two backends report different dimension counts for one layer), and
   `unavailable` names fields a backend structurally cannot supply. `--local`
   resolves the join graph through MetricFlow where the `[semantic]` extra is
   installed, which is what makes its dimension lists the tokens a query can
   actually use; without it the payload says `declarations` and a note names the
   extra. `query` takes a positional
   metric after the explicit mode (with `--metric` kept for compatibility) and a
   `--group-by <entity__dim>` (plus optional `--where`, `--grain`, and
   `--limit`) and returns a metric's values as a capped, columnar result. Name
   flags take a comma-separated list or a repeated flag (`--group-by a,b` is
   `--group-by a --group-by b`); `--where` is never split. `--grain` is checked
   against the grains the layer reports for the metrics queried, so a refusal
   names the ones that metric has. `values <dimension>` returns that dimension's
   value domain, which is what you need before you can write a `--where` filter
   and the one thing no other dex command can reach on a hosted layer (`profile`
   cannot see a semantic dimension). Read `scoped_to` on the result before
   trusting the list: empty means these are the column's own values, and a metric
   name means dex had to reach the dimension through that metric because it is
   only reachable through a join, so the values are the ones present for that
   metric. Pass `--metric` to choose it yourself. A PII-flagged dimension refuses
   this command outright rather than being screened, because the whole output is
   values. Two backends answer
   these, chosen by `.dex/config.yml` `semantic.vendor` and `semantic.deployment`
   (the older `semantic.backend` spelling of the two still works), and overridable
   with `--local` / `--api`. Those two flags name who executes, not which vendor:
   every result reports it as `execution` (`dex` or `vendor`). `--local` renders
   the SQL with MetricFlow and executes it through dex's own connector and cost
   handshake, so cost is surfaced before spend (needs a dbt project parsed at
   least once, and for `query` the `[semantic]` extra; `list` reads the project
   and needs no extra). `--api` sends the query to a hosted
   dbt Cloud deployment (needs only a host, an environment id, and a
   `DBT_SL_TOKEN`, plus the `[semantic-api]` extra, no local project). The hosted
   backend is the one place the cost guard cannot apply: dbt Cloud executes
   server-side, so the result carries an explicit warning that spend is governed
   there, not by dex, and no `--confirm` is asked. Either way a PII-shaped grouped
   or filtered dimension (e.g. `user__email`) is refused before the query runs,
   and on `--api` the layer's own PII metadata is fetched per metric so a
   multi-metric query stays authoritative rather than falling back to names.
   This queries the layer; authoring it is `transform`'s job.

Rules of engagement for `query`: prefer the fixed commands when they answer the
question; one probe answers one question; batch related measures into a single
query rather than issuing many; aggregates over PII-flagged columns must be
measuring (COUNT, APPROX_COUNT_DISTINCT, AVG(LENGTH(...))), never value-carrying
(MIN, ANY_VALUE, STRING_AGG). The FROM clause may unnest JSON and array
columns in the connector's native idiom, which is the right way to explore
schemaless data (for example "which keys appear across every row of this JSON
column"): BigQuery `t, UNNEST(JSON_KEYS(doc)) AS k`, Snowflake
`t, LATERAL FLATTEN(input => doc) f`, Databricks
`t LATERAL VIEW EXPLODE(json_object_keys(doc)) x AS k`, Postgres
`t, jsonb_object_keys(doc) AS k`, Redshift `t, UNPIVOT t.doc AS v AT k`,
DuckDB `t, UNNEST(json_keys(doc)) AS u(k)`, ClickHouse
`t ARRAY JOIN JSONExtractKeysAndValuesRaw(doc) AS kv` (there is no lateral
join; ARRAY JOIN is the expansion). The unnested value must come from
a column of a table in the query (bare, or through a JSON/array function);
unnesting a subquery, another table, a literal, or a generator is refused,
and the unnest's outputs inherit the source column's PII flags. A column whose flag was de-rated below the 0.5
blocking threshold projects normally, with an envelope warning naming it; treat
the warning as information for the user, not an error to fix. If the user says a
refused column is not personal data, recommend a `pii_overrides` entry in
`.dex/config.yml` (fully qualified column, optional reason): it unblocks
querying immediately, survives re-profiles, and is reviewable in git. Never
hand-edit `.dex/cache.json` to clear a flag. Never fall back to raw Python or a
database CLI to run SQL; the firewall path is the only sanctioned one.

## Cloud and database targets (BigQuery, Snowflake, Databricks, Postgres, Redshift, ClickHouse)

A remote warehouse or database replaces `--path` with connector config. Start
with `connect test --connector <name>` (or set `connector:` plus the matching
block in `.dex/config.yml`: `bigquery:` with `project` and a `datasets`
allowlist, `snowflake:` with the pinned `warehouse` and a `databases`
allowlist, `databricks:` with the pinned SQL `warehouse` and a `catalogs`
allowlist, `postgres:` with a `schemas` allowlist, `redshift:` with the
Serverless `workgroup` and a `schemas` allowlist). Credentials are
discovered, never asked for: if the envelope reports missing or expired
credentials, relay the fix it names (for BigQuery
`gcloud auth application-default login`; for Snowflake a `connections.toml`
entry or `SNOWFLAKE_*` env; for Databricks `databricks auth login` or
`DATABRICKS_*` env; for Postgres `DATABASE_URL`, `PG*` env, or a
`pg_service.conf` entry; for Redshift the AWS credential chain
(`aws configure`, `AWS_*` env) or `REDSHIFT_*` env) and never ask the user to
paste a key, token, or password.

On a metered connector, scanning commands (`profile`, `map`, `relationships`,
`query`) run a two-step handshake. The first call returns
`needs_confirmation` with an estimate in `cost.estimate` (and a per-table
breakdown where relevant): an exact dry-run byte figure on BigQuery, a
heuristic labeled `estimate_quality: "heuristic"` in warehouse-seconds on
Snowflake (credits alongside), a floor labeled `estimate_quality: "low"` in
warehouse-seconds on Databricks (DBUs alongside; it sharpens itself inside
the confirmed budget), a heuristic in compute-seconds on Redshift (RPU-hours
alongside; Serverless estimates carry the 60-second wake minimum once), and
database-seconds on Postgres (no dollars; the guarded quantity is load on
the operational database) and on ClickHouse (self-hosted, also no dollars;
estimated free by the non-executing `EXPLAIN ESTIMATE`, which prices after
primary-key pruning, and reporting `estimate_basis` so you can tell a pruned
plan estimate from a whole-relation fallback). Surface the
estimate to the user in human units, get an explicit budget from them, and
re-issue the same command with `--confirm` and `--budget <magnitude>` in the
paradigm's unit. Never invent a budget the user did not agree to, and never
retry with a raised budget on an over-ceiling refusal without asking.
Metadata is free (`connect test`, `inventory` run immediately), and OK
envelopes report actual spend under `data.spend`.

On BigQuery a profiling estimate holds a 10 MB floor per table for each
escalation query a profile may still issue after its aggregate scan, so on a
warehouse of many small tables most of the number can be reserve for work that
never happens. Both the handshake and the over-ceiling refusal report that split
(`reserved_bytes` and `reserved_queries`, and in the prose). Pass it on when you
surface the estimate: whether a number is scan or reserve changes whether
raising the budget is buying work or headroom.

When an estimate is larger than the work deserves, narrow the scope rather than
raise the budget. `--scope` (repeatable) bounds a command to part of the
configured source allowlist, in the connector's own vocabulary: a dataset on
BigQuery, a `schema` or `database.schema` on Snowflake, a `catalog.schema` on
Databricks, a schema on Postgres or Redshift, a database on ClickHouse (whose
identifiers are two-part `database.table`: there is no catalog level). It is
free to resolve, it can only narrow what
`.dex/config.yml` already allows, and a scope that names nothing is refused with
the schemas that do exist listed. So `explore map --scope <schema>` is the first
thing to reach for on a warehouse whose full map would be expensive.

## Guardrails (enforced in the engine, not here)

- Read-only against data. The connection is opened read-only and generated SQL is
  SELECT-only. Never propose a write to source data.
- Sense-making, not enumeration. Rank and drill selectively; never paste a full
  schema into context.
- Profile, don't exfiltrate. Understanding comes from aggregates. PII is flagged,
  never surfaced, and the query firewall enforces it on your own SQL: values
  cross the envelope only from profiled columns whose flag is absent or below
  the blocking threshold, bounded and capped. Only a human's `pii_overrides`
  entry clears a flag entirely; never suggest weakening the detection.
