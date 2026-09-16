# The PII policy

dex flags personal data and never surfaces it. This file is the whole policy:
what a flag is, what each surface does with one, and how a human clears one.
Every other document links here rather than restating it.

How a flag is *produced* (the name-pattern table, the categories, the
value-shape statistics) belongs to [`methodology.md`](methodology.md). This file
owns what a flag *means* and who can clear it.

## What a flag is

A flag is recorded strictly as `(column, category, confidence)` with no example
value, and that triple is what propagates downstream into emitted dbt. Detection
runs on column names and aggregate shape, never by inspecting values, so a
column named `code` full of email addresses is not flagged anywhere in dex.

A flag is never removed by evidence. Value-shape statistics computed during
profiling move its confidence in both directions, and they fail closed: when the
evidence is missing or ambiguous the name-derived confidence stands, because
absence of evidence never weakens a flag. Only a human decision removes a flag,
and only through the config entries below.

## Presence versus threshold

This is the distinction that decides every surface's behavior, and it is why two
commands can disagree about the same column without either being wrong. Some
consumers act on a flag's *presence* at any confidence; others compare its
confidence against the blocking threshold.

| Surface | Gate | Effect |
|---|---|---|
| min and max suppression | presence, any confidence | the extremes are never computed, so no raw value leaves the engine |
| dbt `meta` stamping | presence, any confidence | the flag is stamped into model and column `meta` |
| cluster feature selection | presence, any confidence | flagged columns are excluded; naming one is opt-in and mean only |
| the query firewall | threshold | at or above, projection is refused; below, it runs with a warning |
| the seed header gate | threshold | a seed column at or above the threshold is refused |
| the semantic request gate | presence, any confidence | a flagged dimension refuses the whole command |

A weak flag therefore still suppresses min and max and still stamps `meta`,
while allowing a query that projects the column. What a weak flag means is the
consumer's decision, and the consumers differ on purpose.

## The threshold

A flag at confidence **0.5** or above blocks projection. The threshold is a
hard-coded engine constant, uniform across categories, and deliberately not
configurable: a configurable threshold would let a one-line config edit quietly
widen the PII boundary. At today's base confidences everything blocks, so only a
flag de-rated by value-shape evidence at profile time falls below.

This section is the only place in the repository that states the number.

## The firewall rule

Output may carry values only from profiled columns whose flag is absent or below
the threshold. Every value path from a blocking column must pass through a
measuring aggregate (COUNT, APPROX_COUNT_DISTINCT, AVG, SUM, STDDEV, and the
like). Value-carrying aggregates (MIN, MAX, ANY_VALUE, STRING_AGG) do not
qualify, unknown functions fail closed, and `SELECT *` is refused when the
expansion includes a blocking column.

Filters, join conditions, GROUP BY and ORDER BY are unrestricted: values flow
in, not out. A count projects no column, which is why a filter over a flagged
column is still attributable.

Projecting a column whose flag sits below the threshold runs, with an envelope
warning naming the column, category, and confidence. Treat that warning as
information for the user, not an error to fix.

Unnested JSON and array outputs inherit the source column's flags.

## The stricter surface

Where a command's result *is* the values rather than an aggregate those values
slice, the gate moves from threshold to presence and from dropping a column to
refusing the command.

A metric query returns aggregates that a dimension merely groups, so a flagged
dimension can be dropped from the grouping and the query still answers
something. Listing a dimension's values cannot degrade that way, so **any flag
refuses the command outright**. The refusal names both clearing routes.

Evidence rules run in both directions: a dimension whose name reads innocuous is
refused when its resolved column is flagged, and a profiled, cleared column is
not re-blocked by a PII-shaped name. Where a dimension resolves to no physical
column, or to a relation that was never profiled, the name heuristic is the
fail-closed floor, so silence never clears. A result that ran on the floor alone
says so.

Which evidence each backend can reach, and why a computed expression resolves to
no column, is in [`semantic-layer.md`](semantic-layer.md) and
[`ossie-compatibility.md`](ossie-compatibility.md).

## Clearing a flag

Two durable routes, both reviewable in git and both re-applied on every profile
so they survive re-profiling:

1. A `pii_overrides` entry in `.dex/config.yml`.
2. `meta: {pii: false}` on the dimension in the project, for a semantic layer.

An override takes effect at query time immediately, without re-profiling, and
leaves an audit trail in the cache recording which category the detector had
matched. **Never hand-edit `.dex/cache.json` to clear a flag**, and never
suggest weakening detection: neither survives the next profile, and neither is
reviewable.

A `pii_overrides` entry takes one of two mutually exclusive shapes, enforced at
load:

```yaml
pii_overrides:
  # Exact: one reviewed column, fully qualified with the cache's
  # connector-normalized identifier, so it can never silently widen to a
  # same-named column elsewhere.
  - column: MY_DB.PUBLIC.REGION.R_NAME
    reason: region label, not a person

  # Pattern: one reviewed decision about a structurally identical column that
  # exists by construction on many tables, where the exact form would cost one
  # entry per table per environment. Glob scope, case-insensitive.
  - column_name: document_name
    scope: analytics.cdc_*.*
    reason: resource path in this CDC export, not a person name
```

Both forms are typo-guarded at profile time. An exact entry naming a column its
table does not have warns, and so does a pattern entry whose scope matches
profiled tables when none of them carries the named column. A scope matching no
table yet stays silent, since new entities landing later under the same scope is
the point of the pattern form. Both `explore profile` and `explore map` emit
this warning with the same text, so a scheduled `map` is enough to catch a
rename that left an override pointing at nothing.

## Where the policy is enforced

[`command-contract.md`](command-contract.md) carries the surfaces that gate a
statement: the query firewall, the auto-profile that makes a column adjudicable
at all, the seed header gate on values entering git, and row-attribution counts.
Profiling's own suppression and the diagram renderer are in
[`methodology.md`](methodology.md), `meta` stamping on emitted models is in
[`project.md`](project.md), and the per-backend request gate is in
[`semantic-layer.md`](semantic-layer.md).
