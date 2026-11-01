**Verification measures join coverage and checks a declared parent population.**
A left join can keep every driving row and match nothing, so a model builds with
plausible row counts and every joined field NULL. `maintain verify` and
`transform build --verify` now measure every equality join a model writes, rows
and distinct keys apart, and record each one under `join_coverage` as
satisfied, contradicted, not applicable (an empty input, never reported as full
coverage) or unchecked with a reason code. A join that matches nothing is
`join_no_match`; a join the declaration requires is judged against the contract
rather than a default threshold; and a column a join supplies that came back all
NULL is reported against its declared NULL rule or the cached profile. A model
can now declare `population.parent`, the entity it holds one row per, and
verification counts the parents missing from it and the keys the parent does not
have. The build path runs the join contract for the first time, priced into the
build's estimate as `(join overlap probes)`.
