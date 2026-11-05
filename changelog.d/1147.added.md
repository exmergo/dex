**Cube relative date ranges are recorded and documented.**
An `inDateRange` filter with a relative value (such as `"last 7 days"`) is moved
into the time dimension's `dateRange` when that dimension is grouped, and is
resolved to timestamps by Cube at render time using the Cube server's clock.
The resolved timestamps come back as bound parameters so Dex binds and prices
exactly what runs. A recorded case in `tests/cube_vendor/cube_cases.py` now
exercises this path, and `references/cube-compatibility.md` documents that a
server in another time zone or with a skewed clock resolves it differently (#972).
