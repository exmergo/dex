`transform build --full-refresh` passes the flag through to dbt, so a seed whose columns changed can rebuild under Dex instead of requiring an out-of-band warehouse rebuild.
