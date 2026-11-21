**An INNER join a `references` requires renders a test, `dex_join_coverage`.**
With `declarations.render_tests` on, a model whose key column declares a
`references` naming the model it INNER-joins renders a count-only model test,
tagged `dex_scheduled`, that fails with the join's driving rows that find no
parent row. An INNER join drops those rows, so the model's output, and the
`relationships` test on it, never shows them: the test reads the join's two
inputs and the filters the model applies to each, as the plan reads them from
the model's SQL, and agrees with `maintain verify`'s join contract count for
count on every connector. A NULL key is a missing reference, counted only where
the column is declared `null_rule: never`. A required LEFT join renders nothing
(`covered_by_builtin`, since `relationships` already counts those rows), nor
does a key conversion or a grouped side (`join_shape_unsupported`) or SQL whose
joins the plan cannot read (`join_unread`). A listed test whose join the SQL no
longer writes is reported `stale` with a warning.
