`maintain verify`'s temporal contract check runs on ClickHouse: its statement
read each entity's previous version with `lag()`, which ClickHouse does not
have, and now uses `lagInFrame` over an explicit frame with a NULL default, as
the cumulative detector already did.
