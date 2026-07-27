# Storage: where dex keeps its scratch state

dex writes two kinds of things, and only one of them is a storage backend's
business.

The **source of truth** is the dbt project: model SQL, `schema.yml`, semantic
definitions. It is a git-reviewable filesystem artifact by design, it stays one,
and it never moves into a datastore. No backend choice changes that.

The **scratch state** is everything dex learns along the way: the exploration
cache, the reconcile baseline, the last drift report, the append-only query and
spend ledgers, and the stored transform plans. Delete all of it and nothing
canonical is lost. That is what a `Store` holds.

Two backends ship. `FilesystemStore` writes plain files under `.dex/` and is what
the CLI uses, so persistence is git and a reviewer can read the state in a pull
request. `MemoryStore` writes nothing and is the library default, which is why
`import exmergo_dex_core` cannot leave a `.dex/` directory in a consumer's repo.

Secrets never live in any backend.

## Why you would write your own

One reason, and it is a real one: a process that serves more than one end user.
Such a host federates state per user in its own datastore, so that user A's cache
is not user B's, and it wants that state to outlive a single request without
putting anything on a shared disk.

Note that `MemoryStore` does not cover this. It gives you "nothing on disk" but
not "durable across requests", and those are different requirements. The
distinction matters more than it looks: the query firewall resolves every table
and column reference against the exploration cache, so cache membership decides
what a query is permitted to name and under whose PII policy. A `map()` in one
request must still be visible to a `query()` in the next, or the firewall refuses
work the user has already paid to make legal.

## The three tiers

The contract is three nested protocols. Implement the one that matches what your
host actually does, and stop there: a narrower implementation is a complete
implementation of a declared contract, not a partial one.

| Tier | Members | For a host that |
|---|---|---|
| `ExploreStore` | `load_cache`, `save_cache`, `append_query_log`, `append_spend_log`, `spend_since`, `locator` | explores, profiles, and queries |
| `MaintainStore` | the above, plus `load_snapshot`, `save_snapshot`, `load_drift`, `save_drift` | also detects drift and reconciles |
| `Store` | the above, plus `save_plan`, `load_plan`, `list_plans`, `latest_plan`, `plan_locator` | also authors dbt changes |

`ExploreStore` is six methods, which is the useful thing to know before starting:
a host embedding dex for read-only sense-making is not signing up for sixteen.

The transform surface is the one place the widest tier is required, and it checks
at runtime. Passing an explore-only store to a transform command refuses with a
message naming the tier and the missing members, rather than failing on a missing
attribute several frames down.

## Writing one

```python
from datetime import datetime

from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.storage import Document, spend_total


class MyStore:
    def __init__(self, tenant: str):
        self.tenant = tenant

    def load_cache(self) -> DexCache | None:
        raw = self._fetch("cache")
        return None if raw is None else DexCache.model_validate_json(raw)

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self._put("cache", cache.model_dump_json())
        return self.locator(Document.CACHE)

    def spend_since(self, cutoff_iso, *, field="billed_bytes", connector=None):
        return spend_total(self._entries("spend"), cutoff_iso,
                           field=field, connector=connector)

    def locator(self, document: Document) -> str:
        return f"mystore://{self.tenant}/{document.value}"

    # ... append_query_log, append_spend_log
```

There is no base class to inherit and no registration step. The protocols are
structural: a class with the right methods is a `Store`, and
`isinstance(store, ExploreStore)` confirms it. Pass the instance to the engine:

```python
engine = DexEngine(connector="bigquery", config=cfg, store=MyStore(tenant))
```

Use `spend_total` rather than reimplementing the ledger arithmetic. It is exported
for exactly this reason: every backend then answers the session-budget question
identically, which is a property you want in a guard.

## The contracts that are not obvious from the signatures

Each of these is stated on the protocol member it governs in `storage/base.py`,
and each is asserted by the conformance suite. They are collected here because
they are the ones an implementer gets wrong.

**Documents do not alias the caller's object, in either direction.** What a caller
saves and what a later `load_*` returns must be independent objects. The
filesystem backend gets this free by serializing; the memory backend deep-copies
both ways specifically to match. A backend that hands back a live reference lets
a mutation after a save silently rewrite history.

**`save_cache(cache, now=...)` stamps the caller's object too**, not only the
stored copy. The command layer reports the timestamp it just persisted, so a
backend that stamps only what it writes makes the reported and stored times
disagree.

**Documents are pydantic models.** Round-trip them with `model_validate_json` and
`model_dump_json`. A backend storing its own hand-rolled dict shape diverges on
the first schema change.

**`save_cache` is a whole-document, atomic write.** Cache membership decides what
a query may name, so a half-written cache is a security-relevant state rather than
merely an inconsistent one. A backend whose store caps document size chunks
internally and presents one logical document. There is deliberately no
per-dataset write seam, because that seam is what would let a reader observe half
a cache.

**A corrupt document raises; a corrupt ledger line is skipped.** Two opposite
policies, both deliberate. An unreadable cache means dex does not know what it may
query, and treating that as "nothing explored yet" would silently re-profile a
warehouse and bill for it. An unreadable ledger line means one spend record is
lost, and refusing every subsequent billed command over one bad line is the worse
failure.

**A stored `schema_version` is not the store's to police.** Documents carry one and
the engine reads it. Load what you were given.

**The ledger is scoped to the store instance, so store granularity is ceiling
granularity.** One principal spanning two stores has two independent session
ceilings and nothing bounds their sum. Nothing warns about it and it errs
permissive, which makes it worth stating plainly: key stores exactly as you key
principals. A host that splits stores for some other reason, one per repo root
say, has split the budget too and will not be told.

**The ledger read-then-write is not atomic at the protocol level.** The cost gate
calls `spend_since` and then `append_spend_log`. The cumulative session ceiling
therefore binds exactly when commands are serialized, which is the CLI's
one-command-per-process shape; under genuinely concurrent commands the overshoot
is bounded by the sum of the concurrent estimates. A backend that can make the
pair atomic for one key should, and a multi-tenant backend serving concurrent
requests per tenant is where that stops being optional.

**The protocol is synchronous.** A backend whose client is async wraps it, running
the coroutine to completion inside the method. Every caller in the engine is
synchronous, and a second async surface would double the contract for no caller
that exists.

## Proving it works

The contract ships as an executable suite. Install it and subclass the class for
your tier:

```
pip install "exmergo-dex-core[storage-conformance]"
```

```python
from exmergo_dex_core.storage.conformance import ExploreStoreContract


class TestMyStore(ExploreStoreContract):
    def make_store(self, key):
        return MyStore(tenant=key)
```

That is the whole integration. pytest collects the inherited tests and runs the
contract against your backend, including the isolation assertions: `make_store`
must return a store that shares nothing with a store built from a different key.
Whether the key is a directory, a tenant id, or a table prefix is your business.

Two tenants leaking into each other is the failure this seam exists to prevent, so
those assertions are the ones worth reading if any fail.

## Which calls need nothing on the filesystem

A host with no project on disk can run the whole explore surface: `inventory`,
`profile`, `relationships`, `map`, `query`, and `cluster` need a connector and a
store and nothing else. Hosted semantic-layer calls (`semantic_list` and
`semantic_query` against dbt Cloud) need even less: no store, no connector, no
repo root.

Everything in transform needs `repo_root`, because the dbt project is a
filesystem artifact and stays one. So does `explore map --use-project`, which
reads the dbt project to rank and annotate what it found. Those refuse with a
message naming what needed the root rather than inventing one.

## Selecting a backend

Today a backend is passed to the engine directly, `DexEngine(store=...)`, which is
the library path and the only path. There is no way to select one from the CLI.

The decided direction, when a CLI selector arrives: it is an **open registry**, not
a closed enum. A `cache.backend` setting will accept the names dex ships plus
either a dotted `module:ClassName` path or a name registered through an
`exmergo_dex_core.stores` entry-point group, so a backend published as its own
package is selectable without a change to dex. Recording it here because the
alternative, a closed set of shipped names, would make out-of-tree backends
library-only permanently and opening it later would be a config-schema change.
