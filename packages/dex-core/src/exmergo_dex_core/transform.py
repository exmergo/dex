"""Transform: author and refactor dbt model SQL, tests, and docs; dependency-aware
edits; dev-target build orchestration (gated, cost-guarded); diff presentation.
Writes only to the dbt project, as reviewable diffs. Not yet implemented.
"""

from __future__ import annotations

from typing import Any


def plan(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError


def apply(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError


def build(*args: Any, **kwargs: Any) -> Any:
    # Cost preflight first; runs only with --confirm and a budget. Dev-target only;
    # prod-target execution is never initiated by dex.
    raise NotImplementedError
