"""The postgres adapter (stub). Not yet implemented: metadata model, SQLGlot
dialect, cheap-metadata path, and the connector-specific cost strategy
(database load).
"""

from __future__ import annotations

# Cost paradigm for this connector: database load.
PARADIGM = "db_load"
DIALECT = "postgres"
