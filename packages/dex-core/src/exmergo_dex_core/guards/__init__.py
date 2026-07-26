"""Cross-cutting safety guards: SELECT-only SQL validation and cost preflight.

These enforce hard constraints the rest of the engine leans on (read-only against
data, cost surfaced before spend), independent of any single connector or
capability.

This module holds the guards' shared policy and nothing else, so it must stay
importable with no dialect engine and no warehouse client present. Surfaces that
govern without parsing SQL depend on that: the hosted semantic layer screens PII
by dimension name and never sees a statement, so it reads the threshold below
without paying for a SQL parser it has no use for.
"""

from __future__ import annotations

# A flag at or above this confidence blocks projection; below it, the query runs
# and the envelope carries a warning naming the column, category, and number.
# Hard-coded engine policy, uniform across categories: a configurable threshold
# would let a one-line config edit quietly widen the PII boundary. At today's base
# confidences everything blocks; only evidence-de-rated flags fall below.
#
# It lives here rather than in the query firewall because it is policy shared by
# every surface that gates on PII, and two of those surfaces parse no SQL at all.
PII_BLOCK_CONFIDENCE = 0.5
