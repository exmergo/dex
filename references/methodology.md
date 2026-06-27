# Methodology: making sense of a warehouse without enumerating it

dex explores the way an analytics engineer does: it ranks what matters, drills in
selectively, builds understanding from aggregates rather than rows, infers how
tables relate, and persists a draft map. The guiding constraint is sense-making,
not enumeration: dex never dumps a full schema into context. Everything below is
read-only against the data, and on DuckDB it is free and resource-bounded rather
than cost-bounded.

## Inventory: one cheap pass

Inventory is a single catalog round-trip with no table scans. For each object it
records the cheap facts the catalog already knows: object type (table or view),
an estimated row count, and a column count. Byte size is left unknown rather than
fabricated, because there is no cheap per-object byte size to read; the row
estimate is the size signal that feeds ranking. This pass is what makes selective
drill-down possible: you cannot rank what you have not listed, and you should not
scan what you have not yet decided is worth scanning.

## Ranking: turn a list into a shortlist

Ranking scores every object in [0, 1] from cheap signals so attention goes to the
objects that matter first. The score blends four normalized signals:

- **Size** (log-damped row estimate): bigger tables matter more, but the log keeps
  one giant log table from crushing everything else.
- **Connectivity** (degree in the inferred-join graph): a hub table that many
  tables reference is central to the model.
- **Naming**: a boost for analytics-engineering conventions (fact, dimension,
  staging, mart prefixes) and entity-shaped names; a penalty for scratch, backup,
  temp, and test names. Configured `ranking_hints` add explicit boosts.
- **Shape**: a moderate column count reads as a real modeled entity; extremely
  wide or single-column tables are damped.

Ranking is a pure function over metadata, so it costs nothing to run and re-run as
relationships are discovered.

## Profiling: understanding from aggregates, never rows

Profiling builds a column-level picture from SQL aggregates only. For each object
it issues one batched aggregate query (a non-null count, an approximate distinct
count, and conditionally a min and max), batching wide tables so a single
statement never balloons. From those it derives null fraction, distinct count, and
a uniqueness signal. The distinct count is approximate for scale, so uniqueness is
treated as a candidate signal, never a proven key.

Two safety rules are enforced at the source, in the SQL that is generated:

- **min and max are surfaced only where the extreme value is not itself
  sensitive**: numeric and temporal columns that carry no PII flag. For any string
  column, or any column flagged as PII, min and max are never even computed, so a
  raw or sensitive value never leaves the engine.
- **All generated SQL is read-only.** Beyond the read-only connection, every
  statement is parsed and refused if it is not a single read-only SELECT.

### PII: flagged, never surfaced

PII is detected from column names and aggregate shape, never by inspecting values.
A name-pattern table maps a column to a category (email, phone, name, address,
government_id, financial, credential, location, date_of_birth) with a base
confidence, which aggregate signals then nudge (a near-unique text column on an
email-like name strengthens the flag; very low cardinality on a location-like name
weakens it). The result is recorded strictly as (column, category, confidence)
with no example value, and that flag is what propagates downstream into emitted
dbt.

## Relationships: joins from metadata, not scans

Relationship inference reads the profiles already gathered and never scans data to
verify referential integrity, so it stays free and read-only at the cost of
certainty, which is why every inferred join carries a confidence. A join is
proposed when a foreign-key-shaped column name matches a parent object whose
corresponding column is a candidate key and the types are compatible; confidence
reflects how strong the name and key signals are. Single-column candidate keys and
the most likely grain are derived from the uniqueness signals. Declared joins come
from the dbt project when one is present; absent a dbt project, declared joins are
simply empty, which is expected because explore is designed to work without one.

## The draft map: composing and persisting

`explore map` composes the above into the `.dex/` cache (never the source of
truth; see `canonical-model.md`). It ranks first on cheap signals, profiles a
selective top set by default (with a `--full` option to profile everything, and an
automatic profile-all on small warehouses), infers relationships among the
profiled set, then re-ranks with connectivity for the final scores. The cache is
re-derived and replaced on each run so dropped objects disappear, while the
original creation timestamp is preserved. What is printed back is a counts-level
summary, never the cache contents, keeping the output sense-making rather than a
dump.

## What the agent sees

Every command prints exactly one sanitized JSON envelope (see
`command-contract.md`); credentials and raw rows can never cross that boundary,
and a leak is a hard failure rather than a silent scrub. The agent reads the
envelope and decides the next step, so multi-step exploration is the agent
orchestrating stateless subcommands over the dbt project and the `.dex/` cache.
