**`maintain verify` row-population findings now label a join to a set-operation CTE with the CTE's name.**
When a model cross joins a CTE whose body is a set operation such as `UNION ALL`, the `row_fanout` finding detail fell back to calling it "the cross join to a subquery". dex now preserves the CTE's name in the finding detail, and labels an anonymous set operation as "a set operation" rather than a subquery.

