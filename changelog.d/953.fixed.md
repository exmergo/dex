**`additivity_contradicted` ignores ties when detecting running totals.**
Consecutive observations whose values do not change no longer count as evidence
that an additive measure is a running total or snapshot, avoiding false
contradictions on sparse daily counts.
