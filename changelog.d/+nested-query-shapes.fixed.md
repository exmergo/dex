**Nested `DESCRIBE` queries now return an actionable refusal.** Queries with
unsupported nested statement shapes are rejected before warehouse access rather
than raising an internal planning error. The refusal directs metadata requests
to `explore inventory` or `explore profile`; ordinary SELECTs and existing
privacy and read-only guards keep their behavior.
