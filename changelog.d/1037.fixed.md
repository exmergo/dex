**equivalent column data type aliases are no longer reported as mismatches.**
the column contract in `maintain verify` and `transform build --verify`
previously compared declared and actual types with a raw case-insensitive
string match, reporting equivalent spellings like bigquery `int64` against
reported `INTEGER` or duckdb `int` against `INTEGER` as `column_type_mismatch`.
type comparison now normalizes connector-specific aliases across integer,
string, float, boolean, and temporal families, treats parameterized types
compatibly when metadata reflection omits parameters, and falls back to exact
matching for unnormalizable types.
