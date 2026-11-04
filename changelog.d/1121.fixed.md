**`maintain grain` judges keys and joins on databricks tables with no `numrows`.**
databricks tables whose `describe detail` reports no `numrows` have no catalog
row count, and distinct probes previously did not select `count(*) as n_total`.
as a result, `maintain grain` could not verify key uniqueness or join
cardinality and produced warnings. the distinct-count and composite-key
probes now select `count(*) as n_total` in the same priced scan, updating
the cached row count so keys that lost uniqueness and join fanouts are
verified without warnings.
