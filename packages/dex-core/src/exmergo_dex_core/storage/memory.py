"""In-process state that never touches disk: the library backend.

This is what makes `import exmergo_dex_core` side-effect free. A consumer calling
the engine from its own Python process gets a store that materializes no `.dex/`
directory in its repo, so nothing has to be gitignored, cleaned up, or explained.

Two properties are load-bearing:

- It **retains**, it does not discard. `explore query` and `explore cluster` read
  the cache back to derive their PII policy and feature selection, so a store that
  dropped writes would break the second call of any real flow.
- State lives for the process, not beyond it. The spend ledger is therefore a
  per-process ledger, so a cumulative session ceiling binds within one process and
  resets with the next. That is right for a library call and wrong for the CLI,
  which is why the CLI keeps :class:`~.filesystem.FilesystemStore`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from ..cache import DexCache
from ..errors import ConfigurationError
from .base import Document, StoreContext, spend_total

if TYPE_CHECKING:
    from ..maintain.drift import DriftReport
    from ..maintain.snapshot import Snapshot
    from ..transform.plans import EditKind, TransformPlan


class MemoryStore:
    """Holds every `.dex/` document in instance attributes for the process life.

    Documents are deep-copied on the way in and on the way out. A filesystem
    backend round-trips through JSON, so a caller cannot reach back into a
    document it already saved; holding live references here would diverge from
    that in ways that only show up as a confusing aliasing bug much later.
    """

    def __init__(self) -> None:
        self._cache: DexCache | None = None
        self._snapshot: Snapshot | None = None
        self._drift: DriftReport | None = None
        self._plans: dict[str, TransformPlan] = {}
        self._queries: list[dict] = []
        self._spend: list[dict] = []
        self._spend_lock = threading.RLock()

    @classmethod
    def from_context(cls, context: StoreContext) -> MemoryStore:
        """Build from a :class:`~.base.StoreContext`, which for this backend means
        ignoring the repo root: the reference implementation of a store that needs
        nothing to construct.

        ``repo_root`` is ignored rather than refused, because it is present in
        every context dex builds and carrying one is not a request to use it.
        Options are refused, for the reason the filesystem backend refuses them.
        """

        if context.options:
            raise ConfigurationError(
                "the memory store takes no options and this context carries "
                f"{', '.join(sorted(context.options))}. It holds state in process "
                "attributes and has nothing to configure; an option it silently "
                "ignored would look like a setting that took effect"
            )
        return cls()

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        return self._cache.model_copy(deep=True) if self._cache is not None else None

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self._cache = cache.model_copy(deep=True)
        return self.locator(Document.CACHE)

    def load_snapshot(self) -> Snapshot | None:
        return (
            self._snapshot.model_copy(deep=True) if self._snapshot is not None else None
        )

    def save_snapshot(self, snapshot: Snapshot) -> str:
        self._snapshot = snapshot.model_copy(deep=True)
        return self.locator(Document.SNAPSHOT)

    def load_drift(self) -> DriftReport | None:
        return self._drift.model_copy(deep=True) if self._drift is not None else None

    def save_drift(self, report: DriftReport) -> str:
        self._drift = report.model_copy(deep=True)
        return self.locator(Document.DRIFT)

    # --- ledgers --------------------------------------------------------------

    def append_query_log(self, entry: dict) -> None:
        self._queries.append(dict(entry))

    def append_spend_log(self, entry: dict) -> None:
        self._spend.append(dict(entry))

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        return spend_total(self._spend, cutoff_iso, field=field, connector=connector)

    @contextmanager
    def spend_lock(self, *, timeout: float = 30.0) -> Iterator[None]:
        """Serialize the spend admission across threads sharing this instance.

        The ledger lives in one process, so the whole population that could race
        is the threads holding this object and a plain lock is genuinely atomic
        rather than advisory. ``timeout`` is accepted and not enforced: honoring
        it would mean reporting a lock contended by a section that only spans a
        ledger read and a list append, which cannot outlast any sane timeout, and
        a spurious refusal there would be worse than the wait.
        """

        with self._spend_lock:
            yield

    # --- plans ----------------------------------------------------------------

    def save_plan(self, plan: TransformPlan) -> str:
        self._plans[plan.plan_id] = plan.model_copy(deep=True)
        return self.plan_locator(plan.plan_id)

    def load_plan(self, plan_id: str) -> TransformPlan:
        from ..transform.plans import PlanNotFoundError

        stored = self._plans.get(plan_id)
        if stored is None:
            raise PlanNotFoundError(
                f"no plan '{plan_id}' in this session; run `transform plan` first "
                "or check the id"
            )
        return stored.model_copy(deep=True)

    def list_plans(self) -> list[TransformPlan]:
        return sorted(
            (plan.model_copy(deep=True) for plan in self._plans.values()),
            key=lambda p: p.created_at,
            reverse=True,
        )

    def latest_plan(self, kind: EditKind | None = None) -> TransformPlan | None:
        candidates = [
            plan
            for plan in self._plans.values()
            if plan.applied_at is None
            and (kind is None or all(e.kind is kind for e in plan.edits))
        ]
        latest = max(candidates, key=lambda p: p.created_at, default=None)
        return latest.model_copy(deep=True) if latest is not None else None

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        return f"memory:{document.value}"

    def plan_locator(self, plan_id: str) -> str:
        return f"memory:plans/{plan_id}"
