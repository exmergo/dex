**Join coverage is measured through a cast key.** `maintain verify` and
`transform build --verify` used to report a join such as
`try_cast(a.k as int) = b.k` as an unchecked `non_equality_join`. They now probe
it through the same conversion (a hard cast is measured as its safe form) and
record it under `key_conversion`. Keys the conversion fails on stay unmatched and
are counted as `conversion_failed_rows` and `conversion_failed_keys`. A key
rewritten by any other function is `unsupported_key_expression`. `explore profile`
also restates a candidate key mixing value shapes as the finding
`mixed_identifier_shapes` (#861).
