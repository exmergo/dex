"""The snowflake adapter (stub). Not yet implemented: metadata model, SQLGlot
dialect, cheap-metadata path, and the connector-specific cost strategy
(credits, warehouse size by time).
"""

from __future__ import annotations

# Cost paradigm for this connector: credits, warehouse size by time.
PARADIGM = "compute_time"
DIALECT = "snowflake"
