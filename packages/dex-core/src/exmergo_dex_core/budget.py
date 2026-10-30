"""Shared helpers for session-budget ceiling arithmetic."""

from __future__ import annotations

import os

ENV_CEILING_CAP = "DEX_SESSION_CEILING_CAP"


def effective_session_ceiling(
    file_ceiling: int | None,
) -> tuple[int | None, str]:
    """Return (ceiling_in_force, source).

    source is one of: "none", "config", "environment".
    - "none": no ceiling anywhere
    - "config": the file's budget.session_ceiling wins
    - "environment": DEX_SESSION_CEILING_CAP is set and is lower
    """

    cap_raw = os.environ.get(ENV_CEILING_CAP)
    cap: int | None = int(cap_raw) if cap_raw is not None else None

    if file_ceiling is None and cap is None:
        return None, "none"
    if cap is None:
        return file_ceiling, "config"
    if file_ceiling is None:
        return cap, "environment"
    if cap < file_ceiling:
        return cap, "environment"
    return file_ceiling, "config"
