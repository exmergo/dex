"""The snapshot and drift engine powering the maintain skill.

Maintenance is one job done on several axes: compare a known-good baseline (the
`.dex/` snapshot, plus the assumptions the dbt project encodes) against current
reality (the warehouse and the dbt project), classify what drifted, and propose
the reconciling edit. The axes differ in where they read from and what they look
for, which is why they are distinct entry points rather than one flag:

- `snapshot`  captures/refreshes the baseline in `.dex/snapshot.json`: a fingerprint
              of the warehouse schema, the dbt manifest state, and the declared grain
              and semantic assumptions. The reference drift is measured against.
- `check`     sweeps every axis against the snapshot and returns a categorized,
              blast-radius-ranked drift report. Read-only; proposes nothing.
- `schema`    structural drift: columns and tables added, dropped, retyped, or
              renamed; nullability changes; sources that no longer match the
              warehouse. Metadata only.
- `grain`     cardinality and identity drift: declared keys that lost uniqueness,
              changed row-per-entity cardinality, increased join fanout. Uses
              SQL aggregates.
- `semantic`  business-definition drift: metric, measure, dimension, and entity
              definitions versus the baseline; new categorical dimension values;
              semantic references that no longer resolve to a model or column.
- `reconcile` proposes the dbt edits that bring the project back in sync with
              detected drift, as reviewable diffs (never applied); optionally
              scoped to one class.

Detection (`check`, `schema`, `grain`, `semantic`) is read-only against data.
Only `reconcile` emits diffs, and even then it proposes; it never writes silently
(propose-don't-impose). Not yet implemented.
"""

from __future__ import annotations

from typing import Any


def snapshot(*args: Any, **kwargs: Any) -> Any:
    """Capture or refresh the known-good baseline in `.dex/snapshot.json`."""
    raise NotImplementedError


def check(*args: Any, **kwargs: Any) -> Any:
    """Sweep every drift axis against the snapshot; return a ranked report."""
    raise NotImplementedError


def schema_drift(*args: Any, **kwargs: Any) -> Any:
    """Detect structural drift between the warehouse and the dbt project."""
    raise NotImplementedError


def grain_drift(*args: Any, **kwargs: Any) -> Any:
    """Detect cardinality and identity drift against declared grain assumptions."""
    raise NotImplementedError


def semantic_drift(*args: Any, **kwargs: Any) -> Any:
    """Detect definition drift in the semantic layer against the baseline."""
    raise NotImplementedError


def reconcile(*args: Any, **kwargs: Any) -> Any:
    """Propose the dbt edits that reconcile detected drift, as reviewable diffs."""
    raise NotImplementedError
