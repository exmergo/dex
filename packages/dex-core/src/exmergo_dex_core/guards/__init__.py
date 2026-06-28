"""Cross-cutting safety guards: SELECT-only SQL validation and cost preflight.

These enforce hard constraints the rest of the engine leans on (read-only against
data, cost surfaced before spend), independent of any single connector or
capability.
"""

from __future__ import annotations
