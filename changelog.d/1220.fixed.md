duckdb: carry catalog components on object metadata and quote each
component rather than splitting the flat identifier, so relations with a
dot in schema or table name resolve correctly (#1210).
