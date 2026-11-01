**`transform build --verify` reports the column contract `maintain verify` does.**
A verified build could return no findings while `maintain verify` reported
`column_undeclared` on the same models. The build now reports `column_missing`,
`column_undeclared` and `column_type_mismatch` for the models it built, from
metadata, with no added statement or estimate. `data.verification.column_contract`
names the models checked, those declaring no columns, and those not checked with a
reason, such as a failed model whose older relation is still in place. Findings do
not change the build's status. `maintain verify` now reports a relation whose
metadata cannot be read and carries on, instead of stopping.
