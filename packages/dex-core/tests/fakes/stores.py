"""A storage backend and its factory, living where a third party's would.

Its job is to be reachable *by name*: the resolver tests point a dotted path at
this module, so what they exercise is the same import-a-string-and-call-it path a
real out-of-tree backend takes. Keeping it out of the test module that uses it is
the point, since a factory defined in the test file could be resolved from a local
name and prove nothing.

Keyed by tenant with no repo root anywhere, because that is the backend shape the
construction contract was widened for and the one a path-shaped contract could not
have expressed.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import ClassVar

from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.errors import ConfigurationError
from exmergo_dex_core.storage import Document, StoreContext, spend_total


class TenantStore:
    """A tenant-keyed explore-tier backend, shared across instances by tenant."""

    _registry: ClassVar[dict[str, dict[str, object]]] = {}

    def __init__(self, tenant: str):
        self.tenant = tenant
        self._docs = self._registry.setdefault(tenant, {})

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()

    def load_cache(self) -> DexCache | None:
        raw = self._docs.get("cache")
        return None if raw is None else DexCache.model_validate_json(str(raw))

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self._docs["cache"] = cache.model_dump_json()
        return self.locator(Document.CACHE)

    def append_query_log(self, entry: dict) -> None:
        self._docs.setdefault("queries", []).append(json.dumps(entry))  # type: ignore[union-attr]

    def append_spend_log(self, entry: dict) -> None:
        self._docs.setdefault("spend", []).append(json.dumps(entry))  # type: ignore[union-attr]

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        raws = self._docs.setdefault("spend", [])
        entries = [json.loads(r) for r in raws]  # type: ignore[union-attr]
        return spend_total(entries, cutoff_iso, field=field, connector=connector)

    def locator(self, document: Document) -> str:
        return f"tenant://{self.tenant}/{document.value}"


def tenant_store(context: StoreContext) -> TenantStore:
    """The function shape of the construction contract."""

    tenant = context.options.get("tenant")
    if not tenant:
        raise ConfigurationError(
            "this backend is keyed by tenant and the context carries none; set "
            "cache.options.tenant"
        )
    return TenantStore(str(tenant))


class ContextBuiltStore(TenantStore):
    """The class shape: ``__init__`` takes the context, so the class is a factory."""

    def __init__(self, context: StoreContext):
        super().__init__(tenant=str(context.options["tenant"]))


def not_a_store(context: StoreContext) -> object:
    """A factory that returns something that is not a store, for the refusal."""

    return object()


def explodes(context: StoreContext) -> TenantStore:
    """A factory that fails for a reason of its own, for the refusal."""

    raise RuntimeError("the tenant directory is unreachable")


NOT_CALLABLE = "a string, not a factory"


class UnreachableLedgerStore(TenantStore):
    """A backend whose documents work and whose spend ledger does not.

    The shape a network-backed store takes during an outage of the ledger alone,
    and the reason it is worth having: a store that fails wholesale is easy to
    reason about, while one whose ledger is unreachable while its cache still
    answers is the case where the question "which commands should die?" has a
    wrong answer that looks fine. Writes are allowed to land so a test can put a
    gate through settlement without the write being the thing that fails.
    """

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        raise ConnectionError("the ledger backend is unreachable")
