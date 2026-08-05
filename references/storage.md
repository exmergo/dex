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

Two capabilities sit alongside the tiers rather than inside them, and both are
optional: `SpendLock`, so the cumulative spend ceiling binds when two commands
overlap, and the construction contract, so your backend can be named in
configuration. A backend is a complete backend without either, and the first is
one every concurrent host wants.

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

## Serializing the spend admission

One optional capability, and the only place in this seam where skipping something
costs correctness rather than convenience. If your backend can be reached by more
than one command at a time, implement it.

```python
from contextlib import contextmanager


class MyStore:
    @contextmanager
    def spend_lock(self, *, timeout: float = 30.0):
        with self._lock_for(self.tenant, timeout=timeout):
            yield
```

**What dex does with it.** The cost gate reads the day's spend, decides whether
the command fits under `budget.session_ceiling`, and books that much headroom in
the ledger, all inside one `spend_lock`. It is held for the decision only, never
across a warehouse query, so it costs microseconds and never serializes work that
has no reason to wait.

**Three properties, and the third is the one most often missed.**

- **It excludes.** A second holder waits.
- **It is re-entrant for one holder.** dex settles a gate from more than one
  funnel, so a settle inside a section already held is expected rather than a
  deadlock to design around.
- **It is scoped to the ledger this store reads, not to the backend.** A single
  global mutex satisfies the first two and quietly serializes every tenant in the
  deployment against every other. That is the same isolation the tier contracts
  already require, and a lock is the easy place to forget it, because a
  single-tenant test cannot see the difference.

Raise the builtin `TimeoutError` when `timeout` elapses; dex turns it into a
named refusal carrying the cost. The builtin is what the protocol asks for so
your backend needs no import from dex to satisfy it.

**Without it your backend still works.** dex books the headroom regardless, which
narrows the race from the duration of a warehouse query to the microseconds
around the ledger read, and every billed command carries a warning that the
cumulative ceiling is advisory. That is a warning rather than a refusal on
purpose, matching the call already made for a `session_ceiling` nobody set:
refusing would break a backend written before this existed. It is not a reason to
skip the lock on anything serving concurrent requests.

`SpendLockContract` in the conformance suite is the executable version of all of
the above.

## Constructing one

The section above is the whole contract if your host builds the store itself. If
you also want your backend **named** in configuration and built by dex (see
[Selecting a backend](#selecting-a-backend)), there is a second contract, and it is
deliberately separate from `Store`.

It is separate because the shipped backends disagree about what they are built
from, and the disagreement does not resolve by picking a winner:

| Backend | Built from |
|---|---|
| `FilesystemStore` | a repo root |
| `MemoryStore` | nothing |
| a tenant-keyed backend | a tenant id, with no repository at all |

So construction takes one argument, a `StoreContext`, and a factory is anything
callable that turns one into a store:

```python
@dataclass(frozen=True)
class StoreContext:
    repo_root: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
```

`repo_root` is the directory dex was pointed at, or `None` when there is no
repository in the picture. Your backend is free to ignore it, and a tenant-keyed
one will. `options` is your own non-secret coordinates, passed through verbatim;
dex does not interpret it, so the keys are yours to define and yours to validate.

Three shapes qualify, so you can use whichever your backend already has:

```python
# a function
def my_store(context: StoreContext) -> MyStore:
    tenant = context.options.get("tenant")
    if not tenant:
        raise ConfigurationError("this backend needs cache.options.tenant")
    return MyStore(tenant=str(tenant))


# a class whose __init__ takes the context
class MyStore:
    def __init__(self, context: StoreContext): ...


# a classmethod, which is how the shipped backends do it
FilesystemStore.from_context
MemoryStore.from_context
```

Two obligations come with it.

**Refuse an option you cannot honor.** Accepted-and-ignored is worse than
rejected, because the caller believes a setting took effect and nothing in the
output says otherwise. Both shipped backends refuse an unknown option rather than
dropping it, and `FilesystemStore.from_context` refuses a context with no repo
root rather than falling back to the working directory, which would write one
project's exploration cache into wherever the process happened to start. Raise
`ConfigurationError` so a host catches your refusal with the same `except` it
already uses for dex's.

**No secret ever reaches a `StoreContext`.** `.dex/config.yml` is committed, so a
password, key, token, or connection string in `options` is a credential in version
control. Read your credential the way the rest of the engine does, from the
environment at construction time. If your credentials arrive per request rather
than per process, skip this contract entirely and hand the engine a store you
built yourself: `DexEngine(store=...)` is the right shape for that, and it always
wins over anything named in configuration.

One note if you are writing the code that resolves a name: `StoreFactory` is
`runtime_checkable` for symmetry with the store tiers, but a callable protocol can
only check that `__call__` exists, which every callable satisfies. Build the store
and check the result against the tier you need. The tiers are genuinely
`isinstance`-checkable; the factory is not.

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

**The ledger read-then-write has to be atomic for the cumulative ceiling to
bind**, and `SpendLock` below is how a backend provides it. The cost gate reads
`spend_since`, decides whether the command fits under `budget.session_ceiling`,
and appends, and two commands running that sequence at once read the same total
and both decide yes. Implement the lock and dex serializes the sequence through
it. Without one dex still books the headroom, which narrows the window from the
length of a warehouse query to the microseconds around the read, and warns on
every billed command that the ceiling is advisory on this backend.

**Entries are stored, not interpreted.** Each carries an `entry` kind
(`reservation`, `settlement`, `release`) and a `reservation_id` tying the three
together, because the ceiling has to hold headroom for a command that has been
admitted and has not finished paying. A release carries a **negative**
magnitude, and that is the one thing to know here: a backend that clamps or
filters on sign would leak held headroom for the rest of the UTC day. Sum what
you are given.

Two properties follow, and neither required a change to any backend written
before reservations existed:

- **A day that has finished sums to actual spend**, exactly as it always did, so
  nothing summing settled spend saw a different number.
- **A day with commands still running sums to actual spend plus the headroom
  they hold.** That is not an accounting error; it is the number a concurrent
  command has to see.

A process killed outright cannot release, so its reservation stands until the
UTC rollover. That is deliberate, and it is the safe direction: the alternative
is expiring a hold, which would mean teaching every backend's `spend_since` to
tell the entry kinds apart and reason about time.

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

The extra brings pytest and the dialect engine, because the plan-tier assertions
build a `TransformPlan` and that reaches the SQL guard. Implementing only
`ExploreStore` needs neither the plans nor the engine, and that install stays
viable: nothing in the narrow tier touches SQL.

### What the suite guarantees about the key

**It never hands the same key to two assertions**, within a run or across runs. So
you need no reset hook, no truncate step, and no fixture of your own to keep one
assertion's writes out of the next.

That matters because of what it leaves you free to do. Two calls to `make_store`
with the *same* key may return two views of one shared state, which is exactly what
a durable backend is and what a hosted deployment depends on. The contract
constrains you in one direction only: two *different* keys must share nothing. It
says nothing about the same key twice, and the suite never depends on an answer.

Nothing is cleaned up when the run ends, so a durable backend accumulates one
namespace per run. Pruning them is yours; the alternative was a teardown hook, which
is a second thing to implement and get wrong for a guarantee the suite does not
need.

**If your backend is meant to be named in configuration**, mix in
`StoreFactoryContract` as well. It routes `make_store` through your own factory, so
everything above then runs against stores built the way dex builds them:

```python
from exmergo_dex_core.storage import Store, StoreContext
from exmergo_dex_core.storage.conformance import StoreContract, StoreFactoryContract


class TestMyStore(StoreFactoryContract, StoreContract):
    tier = Store

    def build(self, context):
        return my_store(context)

    def context_for(self, key):
        return StoreContext(options={"tenant": key})
```

This is why construction is not a second, unchecked obligation: with it, "the
conformance suite is green" still means your backend is both correct and
constructable. Without it, the suite proves behavior only, and a backend can pass
every assertion here and still fail the moment configuration names it.

**If your backend serves concurrent requests**, mix in `SpendLockContract` the
same way, and it checks all three properties from
[Serializing the spend admission](#serializing-the-spend-admission), including
the scope one:

```python
from exmergo_dex_core.storage.conformance import ExploreStoreContract, SpendLockContract


class TestMyStore(SpendLockContract, ExploreStoreContract):
    def make_store(self, key):
        return MyStore(tenant=key)
```

It exercises threads, because that is the population a suite can portably create.
If your backend spans processes or hosts, the lock has to reach whatever your own
substrate offers (an advisory file lock, a transaction, a conditional write) and
this contract cannot tell the difference. That gap is real, and it is why the two
shipped backends carry cross-process assertions of their own rather than treating
a green contract as the whole answer.

## Which calls need nothing on the filesystem

A host with no project on disk can run the whole explore surface: `inventory`,
`profile`, `relationships`, `map`, `query`, and `cluster` need a connector and a
store and nothing else. `diagram` needs less still: it reads the cache out of the
store and renders it, so a host holding a populated store can call it with no
connector resolved and no credential in play. Hosted semantic-layer calls
(`semantic_list` and
`semantic_query` against dbt Cloud) need even less: no store, no connector, no
repo root.

Everything in transform needs `repo_root`, because the dbt project is a
filesystem artifact and stays one. So does `explore map --use-project`, which
reads the dbt project to rank and annotate what it found. Those refuse with a
message naming what needed the root rather than inventing one.

## Selecting a backend

Two ways in. A library caller passes an instance, `DexEngine(store=...)`, and that
always wins: a caller holding a store has already made the decision configuration
exists to make for the callers who have not. Everyone else names one in
`.dex/config.yml`:

```yaml
cache:
  backend: mypkg.stores:my_store
  options:
    tenant: acme
```

`options` reaches your factory verbatim, and `repo_root` carries whatever directory
dex was pointed at. `--cache-backend` overrides the name for one run, the way
`--connector` overrides the configured connector; naming a different backend that
way leaves `options` behind, since one backend's coordinates are not another's and
the usual reason to reach for the flag is falling back to `filesystem` for a single
command.

It is an **open registry**, not a closed enum, so a backend published as its own
package is selectable without a change to dex. Three kinds of name resolve, in this
order:

| Name | Example | For |
|---|---|---|
| shipped | `filesystem` | dex's own backends, and never shadowable by anything installed |
| dotted path | `mypkg.stores:my_store` | a factory reachable by import, with no packaging work |
| entry point | `acme` | a name an installed distribution registered under `exmergo_dex_core.stores` |

The entry point is what makes a published backend feel like a shipped one:

```toml
# in your own pyproject.toml
[project.entry-points."exmergo_dex_core.stores"]
acme = "dex_acme_store:acme_store"
```

Install it beside dex and `backend: acme` resolves. A shipped name always wins over
a registration, so installing a package can never silently move where an existing
repo's state lands.

`memory` is deliberately not selectable. Each CLI command runs as its own process,
so a `MemoryStore` would drop the cache between `explore map` and `explore query`,
and the second command would refuse with "run `explore map` first" having just run
it. That reads as a broken tool rather than a chosen backend, so the refusal
explains the process boundary instead. It remains the default for a library caller,
where one process holds the engine.

Every failure here refuses with a `ConfigurationError` naming the fix: an unknown
name lists what exists and both open forms, a dotted path that will not import says
so and points at the environment, and a factory that builds something which is not
a store names the members it is missing. The tier check is on the constructed store
rather than on the factory, because a callable protocol can only verify `__call__`
exists.

### Two rejected alternatives

Recorded so they are not re-argued.

**Always pass `repo_root`.** It builds both shipped backends and would build an
opt-in SQLite file, and it cannot build a tenant-keyed backend at all. That is the
class of backend this seam was made public for, and widening a released config
schema afterwards costs a deprecation.

**Require a `from_config` classmethod on the store.** It puts an obligation on the
structural protocol, which is what makes "no base class to inherit and no
registration step" true, and it hands every backend author dex's whole
configuration model to bind against. The factory contract keeps the two concerns
apart: a store is still just a class with the right methods, and construction is a
separate thing you implement only if you want it.
