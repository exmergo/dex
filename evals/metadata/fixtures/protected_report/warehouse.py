"""Synthetic protected text, generated only inside the fixture's trusted setup.

No real personal data or literal example belongs in a prompt or assertion.
NULLs, an empty string and distinct nonempty strings distinguish pass-through
from dropping, filtering, replacing or coalescing the protected field.
"""

from __future__ import annotations

import hashlib

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rows = []
    for report_id in range(1, 13):
        digest = hashlib.sha256(f"{seed}:{report_id}".encode()).hexdigest()
        comment = f"Synthetic protected report text {digest}"
        if report_id in (4, 9):
            comment = None
        elif report_id == 12:
            comment = ""
        rows.append((report_id, comment))
    return [
        Table("raw.reports", [("report_id", "integer"), ("comments", "varchar")], rows)
    ]
