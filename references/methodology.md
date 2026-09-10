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

The row count is an estimate where the catalog keeps one and unknown where it does
not, and the two are never collapsed into each other. A warehouse maintains no
count for a view, and BigQuery maintains none for an external table either;
several report that as a zero, which is why the count is decided from the object's
kind rather than taken at face value. Unknown means the size signal cannot
participate, so an uncounted object ranks as if it were small until something
counts it. Empty means the object holds nothing, which is a finding. Reading the
first as the second was how an external table over object storage came to be
reported as an empty table with correct column statistics beside the claim.

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
a uniqueness signal. The distinct count is approximate for scale, so uniqueness
starts as a candidate signal, never a proven key.

Because an approximate count can overshoot a genuinely unique column and turn a
real key into a false "grain unknown", profiling then escalates: any column whose
approximate distinct count sits near its non-null count (within a bounded band) is
re-counted with an exact `COUNT(DISTINCT)` in one batched, read-only statement,
capped to the few closest candidates per table so the escalation stays cheap. An
escalated count is marked exact, and only an exact count is allowed to confirm a
key or a table's grain; downstream consumers (relationship fan-out notes included)
never draw a hard conclusion from an approximation.

When no single column proves unique (the shape of a fact table, whose grain is
exactly what a profile must answer), profiling probes 2-column composite keys. A
pair can only be a key if the product of its members' distinct counts reaches the
row count, so pairs are pruned on that necessary condition using the counts
already in hand, then ranked (id-shaped members first, smallest product next) and
capped to a few probes issued as one exact distinct-combination statement. A pair
that reuses a column from a better-ranked pair at a similar product is the same
hypothesis with different filler, so it ranks behind every pair that is not, but
it is never removed from the running: the cap is the spend guard, and while it
has room a discarded candidate costs a grain and saves nothing. A pair whose
combination count equals the row count is a proven composite key: it enters the
candidate keys and, absent any single-column key, becomes the reported grain,
smallest product first so the tightest proven key is the one reported. On metered
connectors the probe spends only inside the already-confirmed budget, narrowing to
the best-ranked pairs the budget covers, and skipping with an explanatory note
only when it cannot cover one.

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

For the weakest signal, a generic `*_name` string column, the profiling scan also
computes three **value-shape statistics** as in-engine SQL aggregates: the
fraction of values that are all-caps tokens, the fraction shaped like a given
name plus surname, and the average token count. These are regex predicates
inside measuring aggregates, so only numeric fractions leave the engine, never a
value. The evidence moves confidence in both directions: a person-shaped
distribution corroborates the flag up to the exact-token level, while a tiny
closed all-caps vocabulary (a region or nation dimension) or long multi-token
labels (part and product descriptions) de-rate it to reference-data confidence.
When the evidence is missing or ambiguous, the name-derived confidence stands:
absence of evidence never weakens a flag.

The flag itself is never removed by evidence. What a weak flag means is the
consumer's decision: the query firewall blocks projection at confidence 0.5 and
above (a hard-coded engine constant) and allows lower-confidence columns with an
envelope warning, while min/max suppression and dbt `meta` stamping remain
presence-based at any confidence. The only way to clear a flag entirely is a
human decision recorded as a `pii_overrides` entry in `.dex/config.yml` (fully
qualified column plus an optional reason). An override is re-applied on every
profile, so it survives re-profiling, takes effect at query time immediately,
and leaves an audit trail in the cache recording which category the detector had
matched.

## Relationships: joins from metadata, not scans

Relationship inference reads the profiles already gathered and never scans data to
verify referential integrity, so it stays free and read-only at the cost of
certainty, which is why every inferred join carries a confidence. The opt-in
verification pass is the one place joins are measured, and it batches: the joins
sharing a child relation are answered by one read of it, so what verification
costs follows the relations it touches rather than the number of joins between
them.

A join is proposed when a foreign-key-shaped column name matches a parent object whose
corresponding column is a candidate key and the types are compatible; confidence
reflects how strong the name and key signals are. Candidate keys and the most
likely grain come from the uniqueness signals: single columns proven unique, plus
the composite keys proven at profile time; a single-column key is always
preferred as the grain, and a member of a composite key is never treated as
unique on its own. Declared joins come
from whichever channels declare one: the dbt project when one is present, and the
semantic layer when one is configured, which need not be dbt's. Absent both,
declared joins are simply empty, which is expected because explore is designed to
work without either. A declared join carries every column pair it was written
with, so a composite is one declaration measured as one complete tuple rather
than a first-column proxy that would report a healthy join three times over while
no tuple matched. Where two channels declare a join between the same pair of
relations on different columns, both are kept and the disagreement is reported;
dex does not pick a winner between two things the repository asserts.

Declarations refine those verdicts without entering them. A declared
grain fills in where measurement found none, and is noted where it disagrees, but
a measurement-proven single column still wins the reported grain and the candidate
keys stay measurement-only: an unmeasured declared key is a claim, and the cache is
a drift baseline rather than a record of what the project asserts. The consequence
is that a declared grain needs verifying somewhere else, which is what `maintain
grain` does with it, and why that axis reports two different things about
uniqueness. A key measurement proved unique that no longer is has a before and an
after, so it lapsed. A declared combination that does not hold has neither: nothing
changed, the project is asserting a grain the data never had, and the fix is to the
declaration rather than to the data.

## Row population: a model against the relation it is built from

Everything above measures a relation on its own terms. A transformation project
raises a question none of it answers: not what this relation looks like, but
whether it holds the rows it should, given the relation it was built from. That
is where the expensive errors live, because they raise nothing. An inner join
written where a left join was meant, a filter that quietly excludes NULLs, a
de-duplication keyed on the wrong column: each drops rows, none is an error, and
uniqueness and not-null tests all still pass over the smaller result.

The comparison needs one thing the warehouse cannot supply, which is the model's
**driving parent**: the relation in its FROM clause, as distinct from the ones it
joins. Those are different roles. The driving relation sets how many rows the
model starts with, and a join can only reduce that number or multiply it. So the
parent is read out of the compiled SQL rather than out of the dependency graph,
which knows a model's parents but not which of them drives it, and it is followed
through the chain of CTEs a dbt model compiles to, since the FROM of a compiled
model's final select names an internal CTE almost every time.

**What the SQL says about itself decides whether the numbers mean anything.** A
model carrying a filter, an aggregate, a de-duplication, or a set operation was
written to hold fewer rows than its parent, and no amount of arithmetic
distinguishes a filter that removed the rows it should from one that removed too
many. There is no principled bound to compute, so those models are not reported
at all rather than reported with a caveat. The cost of that choice is real: a
filtered model that also lost rows to a bad join says nothing. The cost of the
other choice is worse, because a detector that fires on every aggregate in a
project is one nobody reads twice. The same logic runs the other way for growth,
where an `UNNEST` or a lateral is a construct whose entire purpose is turning one
row into several.

Row counts themselves come from the cheapest source that can answer, and which
one answered changes what the finding claims. A stored catalog count is free but
is treated as an estimate, so the finding is not marked exact; an actual count is
a proof. Where counting bills nothing, everything is counted, because a verdict
about a ten percent difference has no business resting on an estimate. Where it
bills something, it is priced and offered rather than taken, and the models it
would have covered are named as uncompared. That last part matters more than it
looks: a warehouse keeps no row count for a view, and a view is dbt's default
materialization, so on a metered connector the models this can judge for free and
the ones it cannot are split down exactly that line.

## Test strength: measuring the tests rather than the data

Everything above measures data. A dbt project also carries assertions about that
data, and those assertions are themselves unmeasured: a suite of twenty tests
that all pass proves the tests ran, not that any of them would object if the
model were wrong. The two are routinely confused, because the only number the
ecosystem reports is a count, and a count cannot distinguish a `not_null` on a
surrogate key from a unit test pinning the arithmetic.

The measurement that does distinguish them is to break the model deliberately and
see whether anything complains. `transform test --mutate` plants one defect at a
time, drawn from the same taxonomy the rest of this document is organised around:
a boundary comparison that now includes or excludes its edge, a filter dropped or
inverted, an inner join where a left join was meant, a missing `CASE` branch, an
inverted ratio, a window frame off by one, a `sum` reporting a `max`. Each is
built and the model's own tests are run against it. A defect nothing catches is
reported as a gap in the suite, described as the defect rather than as a diff,
because the reader's next step is to write a test and not to reread SQL they
already know.

Three properties keep the result honest. Every verdict is relative to the tests
that passed against the *unmutated* model, so a suite measured against its own
already-failing tests cannot come back looking clean. A mutant the warehouse
refuses outright is reported separately from one the tests caught, since a build
would have failed on it anyway and counting it would flatter the suite. And a
mutant that survives is a statement about detection, not about correctness: some
survivors are defects the current data cannot distinguish at all, such as an
inner join where every key happens to match, which is exactly why the finding
names the test that would catch it rather than claiming the model is wrong.

## The draft map: composing and persisting

`explore map` composes the above into the `.dex/` cache (never the source of
truth; see `canonical-model.md`). It ranks first on cheap signals, profiles a
selective top set by default (with a `--full` option to profile everything, and an
automatic profile-all on small warehouses), infers relationships among the
profiled set, then re-ranks with connectivity for the final scores. The selective
pass never caps silently: the summary states how many objects were profiled versus
skipped and the rule that drew the line. On a re-map, an object that falls outside
this run's top set keeps its prior profile (carried forward, each dataset stamped
with its own `profiled_at`) rather than dropping to inventory-only, so coverage
accumulates across runs. The cache is re-derived and replaced on each run so
dropped objects disappear, while the original creation timestamp is preserved. What
is printed back is a counts-level summary, never the cache contents, keeping the
output sense-making rather than a dump.

## Diagrams: serializing structure, never drawing values

`explore diagram` renders the cached map as a Mermaid `erDiagram`. It is a pure
function of the cache: it opens no connection, needs no credential, spends
nothing, and can be re-run freely while a diagram is being shaped.

**The rule that bounds it, and every renderer after it: dex may serialize
structure it has already computed into a text format, and dex never renders data
values into a visual encoding.** An entity-relationship diagram of the objects,
keys, and joins is structure. A chart of null fractions or value distributions is
a picture of the data itself, which is a different product's job. In practice
that admits `erDiagram`, `flowchart`, and `classDiagram`, and rules out `pie`,
`xychart`, `sankey`, and `quadrantChart`. No column value reaches a diagram: the
cached min and max are never read, and a PII flag renders as its category and
confidence, because flagged-not-hidden is the posture everywhere else too.

A diagram is trusted more readily than the JSON it came from, so the cardinality
rules are strict. Mermaid requires a glyph on every edge and dex's relationship
record carries no cardinality, so the renderer derives one only from what was
proven. It claims "exactly one" on the parent side only when the parent key is a
proven key **and** the join was declared in the repository or measured with no
orphans; it degrades to "zero or one" when uniqueness is proven but nothing
measured the overlap; and it degrades to "zero or many" when uniqueness was never
established at all. Declared joins are drawn solid and inferred joins dotted, and
the edge label carries the kind, the confidence, and the orphan fraction, so an
unverified guess says so on its face.

Selection follows the same rule as the rest of explore: a fully attributed
diagram of a large warehouse is a schema dump, so the default draws objects that
were profiled and participate in a join, carrying their grain, key, join, and
PII-flagged columns. `--full` widens to every eligible object and column, an
entity cap binds in both modes, and everything left out is counted in the notes.

Rendering is deterministic, so the same cache produces byte-identical output and
a regenerated file diffs cleanly. dex writes no diagram file: the text comes back
in the envelope and choosing where it lands is the caller's decision.

## What the agent sees

Every command prints exactly one sanitized JSON envelope (see
`command-contract.md`); credentials and raw rows can never cross that boundary,
and a leak is a hard failure rather than a silent scrub. The agent reads the
envelope and decides the next step, so multi-step exploration is the agent
orchestrating stateless subcommands over the repository and the `.dex/` cache.
