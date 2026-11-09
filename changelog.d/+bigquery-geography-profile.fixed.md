**bigquery non-groupable types are skipped from distinct queries and single-object
profile failures are noted instead of failing explore runs (#925).** nested and
non-groupable column types like geography, json, and struct are excluded from
distinct count and combination queries in bigquery, and explore map and explore
relationships turn per-object profile failures into notes while reporting the
remaining objects.
