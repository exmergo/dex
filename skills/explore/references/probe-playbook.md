# Probe playbook: effective shapes for `explore query`

A probe is one agent-authored SELECT run through the engine's query firewall. The
firewall guarantees safety; this playbook is about effectiveness: asking the
question in a shape that returns a small, decisive answer instead of a wall of
rows. Profile first (`explore map`), probe second; the profile usually already
holds the answer (null fractions, distinct counts, min/max, candidate keys), and
probes exist for the questions it does not.

Two habits pay for everything else:

- **One probe, one question.** Decide what you are testing before writing SQL,
  and name the output columns after the answer (`orphans`, `dupes`, `coverage`).
- **Batch related measures.** Eight aggregates in one SELECT cost one round trip;
  eight probes cost eight. Combine counts that share a FROM clause.

Firewall rules that shape your SQL: values may be projected only from profiled,
PII-cleared columns; over a flagged column use a measuring aggregate (COUNT,
COUNT(DISTINCT ...), COUNTIF/COUNT_IF, APPROX_COUNT_DISTINCT, AVG, SUM, STDDEV),
never a value-carrying one (MIN, MAX, ANY_VALUE, STRING_AGG, ARRAY_AGG). Filters and
join conditions may reference anything. Results are capped (rows, cell width,
bytes), and every cut is announced in `notes`, so aggregate first rather than
paging.

## The recipes

**1. Join-key overlap.** Does `child.fk` really point at `parent.key`? (Or run
`explore relationships --verify`, which is this probe productized.)

```sql
SELECT COUNT(c.fk)                                   AS nonnull_fk,
       COUNT(DISTINCT c.fk)                          AS distinct_fk,
       COUNT(*) FILTER (WHERE c.fk IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM parent p WHERE p.key = c.fk)) AS orphans
FROM child c
```

Zero orphans confirms the join; a high orphan fraction says the name-based guess
was wrong or the parent is incomplete.

**2. Duplicate / grain check.** How badly is a key broken, and what does the
duplication look like?

```sql
SELECT COUNT(*)                          AS rows,
       COUNT(DISTINCT id)                AS distinct_ids,
       COUNT(*) - COUNT(DISTINCT id)     AS surplus_rows,
       MAX(cnt)                          AS worst_repeat
FROM (SELECT id, COUNT(*) AS cnt FROM t GROUP BY id)
```

**3. Top-K categorical distribution.** What values dominate a (non-flagged)
column, and how concentrated is it?

```sql
SELECT status, COUNT(*) AS n
FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 10
```

For a PII-flagged column, take the measuring route instead: `COUNT(DISTINCT x)`
tells you the cardinality story without surfacing a value.

**4. Null / blank breakdown.** Nulls are profiled already; blanks and sentinels
are not.

```sql
SELECT COUNT(*)                                        AS rows,
       COUNT(*) FILTER (WHERE TRIM(col) = '')          AS blank,
       COUNT(*) FILTER (WHERE col IN ('N/A', 'none'))  AS sentinel
FROM t
```

**5. Date coverage.** Is the table continuous, and where does it end?

```sql
SELECT MIN(created_at)                    AS first_day,
       MAX(created_at)                    AS last_day,
       COUNT(DISTINCT CAST(created_at AS DATE)) AS days_present,
       DATEDIFF('day', MIN(created_at), MAX(created_at)) + 1 AS days_span
FROM t
```

`days_present` well below `days_span` means gaps; probe the suspect range with a
bucketed count (recipe 7).

**6. Orphan / coverage rate between layers.** What fraction of entity A ever
appears in fact B?

```sql
SELECT COUNT(*)                                          AS customers,
       COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM orders o WHERE o.customer_id = c.id)) AS with_orders
FROM customers c
```

**7. Distribution sketch via buckets.** The shape of a numeric column without
pulling rows.

```sql
SELECT WIDTH_BUCKET(amount, 0, 1000, 10) AS bucket,
       COUNT(*)                          AS n,
       AVG(amount)                       AS bucket_avg
FROM payments GROUP BY 1 ORDER BY 1
```

**8. Sensitive-column shape check.** Everything useful about a flagged column
that can cross the envelope:

```sql
SELECT COUNT(email)                    AS present,
       COUNT(DISTINCT email)           AS distinct_vals,
       AVG(LENGTH(email))              AS avg_len,
       COUNT(*) FILTER (WHERE email NOT LIKE '%@%') AS shape_violations
FROM users
```

Recipes 4, 6, and 8 above use `FILTER (WHERE ...)`, which is not available on
BigQuery BigQuery's engine will reject that clause outright, and the firewall
will not catch it first (it parses fine). On BigQuery, spell the same batched
filtered count as `COUNTIF(cond)` instead, e.g. recipe 4's blank/sentinel
breakdown becomes:

```sql
SELECT COUNT(*)                            AS rows,
       COUNTIF(TRIM(col) = '')             AS blank,
       COUNTIF(col IN ('N/A', 'none'))     AS sentinel
FROM t
```

`COUNTIF(cond)` is equivalent to `COUNT(*) FILTER (WHERE cond)` and passes the
firewall the same way: the condition is a filter, not a projected value.

## When a probe is refused

The refusal names the column, its PII category, and the fix. Rewrite once: swap
the value-carrying expression for a measuring one, or drop the column from the
projection. Do not retry the same shape, do not route around the engine with
Python or a database CLI, and if the refusal says a table is not profiled, run
`explore profile <table>` (or `explore map`) and probe again.

Two newer paths the refusal may name:

- A refusal on a column the user says is not personal data (a region label, a
  product line) is resolved with a `pii_overrides` entry in `.dex/config.yml`
  naming the fully qualified column, with an optional reason. It takes effect on
  the next query without re-profiling. Recommend the entry; never edit the
  cache.
- If the refusal suggests re-profiling, the cache predates value-shape
  profiling: one `explore profile <table>` recomputes the flag's confidence
  with shape evidence and may clear the block on its own.

## When a probe runs with a PII warning

A projection of a column whose flag sits below the blocking threshold runs and
adds a warning to the envelope naming the column, category, and confidence.
That is the designed behavior for de-rated reference columns (a region or
nation dimension), not an error: pass the caveat on to the user alongside the
result, and if they confirm the column is personal data after all, drop it from
later probes.
