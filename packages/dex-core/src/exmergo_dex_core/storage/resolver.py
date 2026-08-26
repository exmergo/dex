"""Turning a backend's *name* into a store: the open registry.

A name reaches here from `.dex/config.yml` (`cache.backend`) or from
``--cache-backend``, and leaves as a constructed store. Three kinds of name
resolve, in this order:

- a **shipped name** (``filesystem``), which can never be shadowed by anything
  installed, so a config that says ``filesystem`` means dex's own backend on
  every machine that reads it;
- a **dotted path**, ``mypkg.stores:my_store``, which needs no packaging work at
  all and is what a host reaches for first;
- an **entry-point name**, registered by an installed distribution under
  ``exmergo_dex_core.stores``, which is how a backend published as its own
  package becomes selectable by a short name.

An open registry rather than a closed enum, because the alternative would make
out-of-tree backends library-only permanently, and opening a released config
schema afterwards costs a deprecation.

What a name resolves *to* is a :class:`~.base.StoreFactory`, and what it is called
with is a :class:`~.base.StoreContext`. Neither is this module's invention; see
``references/storage.md``. This module only chooses.

Every refusal here is a :class:`~..errors.ConfigurationError` naming the fix,
because every one of them is a wiring mistake in how the engine was configured
rather than a bad request from a user.
"""

from __future__ import annotations

from ..errors import ConfigurationError
from .base import ExploreStore, StoreContext, StoreFactory
from .filesystem import FilesystemStore
from .sqlite import SqliteStore

ENTRY_POINT_GROUP = "exmergo_dex_core.stores"

#: The backends dex ships and will construct by name. Checked before anything
#: installed, so no third-party registration can take over a shipped name.
SHIPPED: dict[str, StoreFactory] = {
    "filesystem": FilesystemStore.from_context,
    "sqlite": SqliteStore.from_context,
}

#: Shipped backends that are deliberately *not* selectable, and why. Refusing by
#: name with the real reason beats letting them through into a tool that appears
#: broken, and beats an "unknown backend" message for something that plainly
#: exists.
NOT_SELECTABLE = {
    "memory": (
        "the memory store keeps state in process attributes, and each CLI command "
        "runs as its own process, so nothing it wrote would survive to the next "
        "one: `explore query` would refuse with 'run `explore map` first' having "
        "just run it. It is the default for a library caller, where one process "
        "holds the engine (DexEngine() selects it automatically), and it is not "
        "selectable here. For durable state outside the repo, name a backend of "
        "your own"
    ),
}

# The narrowest tier, and so the floor for anything selected: a store that cannot
# serve explore cannot serve anything. Which *wider* tier a given command needs is
# checked where the command runs (see DexEngine.require_full_store), because that
# is where the answer depends on the command rather than on the configuration.
_EXPLORE_MEMBERS = (
    "load_cache",
    "save_cache",
    "append_query_log",
    "append_spend_log",
    "spend_since",
    "locator",
)


def resolve_store_factory(name: str) -> StoreFactory:
    """The factory ``name`` selects, or a refusal naming what to fix.

    Resolution only. The factory is not called here, so a caller that wants to
    inspect or wrap what a name selects can, and :func:`build_store` is the one
    that constructs.
    """

    selected = (name or "").strip()
    if not selected:
        raise ConfigurationError(
            "cache.backend is empty; name a shipped backend "
            f"({', '.join(sorted(SHIPPED))}), a dotted path such as "
            "'mypkg.stores:my_store', or an installed entry-point name"
        )

    if selected in SHIPPED:
        return SHIPPED[selected]

    if selected in NOT_SELECTABLE:
        raise ConfigurationError(
            f"the '{selected}' store cannot be selected from configuration: "
            f"{NOT_SELECTABLE[selected]}"
        )

    if ":" in selected:
        return _load_dotted_path(selected)

    registered = _load_entry_point(selected)
    if registered is not None:
        return registered

    raise ConfigurationError(
        f"unknown cache backend '{selected}'. dex ships "
        f"{', '.join(sorted(SHIPPED))}; anything else is either a dotted path "
        "('mypkg.stores:my_store', note the colon between module and name) or a "
        f"name registered by an installed distribution under the "
        f"'{ENTRY_POINT_GROUP}' entry-point group. Nothing installed registers "
        f"'{selected}'"
    )


def build_store(
    name: str,
    context: StoreContext,
) -> ExploreStore:
    """Resolve ``name`` and construct it, checking what comes back is a store.

    The tier check is on the constructed object rather than on the factory,
    deliberately: a factory is a callable, and a callable protocol can only
    verify that ``__call__`` exists, which every callable satisfies. The store
    tiers are genuinely structural, so building first is what makes the check
    mean anything.
    """

    factory = resolve_store_factory(name)
    try:
        store = factory(context)
    except ConfigurationError:
        # The backend refused its own coordinates and said why. Re-wrapping would
        # bury the specific message under a generic one.
        raise
    except Exception as exc:
        raise ConfigurationError(
            f"the cache backend '{name}' failed to build: {exc}. A factory takes "
            "one StoreContext and returns a store; check that it accepts the "
            "context dex passes and reads its coordinates out of context.options"
        ) from exc

    if not isinstance(store, ExploreStore):
        missing = [m for m in _EXPLORE_MEMBERS if not hasattr(store, m)]
        raise ConfigurationError(
            f"the cache backend '{name}' built a {type(store).__name__}, which is "
            f"not a store: it is missing {', '.join(missing)}. Every backend has "
            "to satisfy at least exmergo_dex_core.storage.ExploreStore; run the "
            "shipped conformance suite against it "
            "(exmergo_dex_core.storage.conformance)"
        )
    return store


def _load_dotted_path(path: str) -> StoreFactory:
    from importlib import import_module

    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ConfigurationError(
            f"'{path}' is not a usable dotted path; write it as "
            "'module.path:name', with a colon between the module and the factory"
        )
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"cache backend '{path}' names a module that will not import: {exc}. "
            "Check the spelling, and that the distribution providing it is "
            "installed in the environment running dex"
        ) from exc

    resolved = module
    for part in attribute.split("."):
        try:
            resolved = getattr(resolved, part)
        except AttributeError as exc:
            raise ConfigurationError(
                f"cache backend '{path}' imported {module_name} but it has no "
                f"'{attribute}'"
            ) from exc

    if not callable(resolved):
        raise ConfigurationError(
            f"cache backend '{path}' resolved to a {type(resolved).__name__}, "
            "which cannot be called. Name a factory: a function taking a "
            "StoreContext, a class whose __init__ takes one, or a classmethod "
            "like FilesystemStore.from_context"
        )
    return resolved


def _load_entry_point(name: str) -> StoreFactory | None:
    """The factory an installed distribution registered under ``name``, if any.

    Imported here rather than at module scope: the CLI runs as a fresh process
    per command, and scanning installed distributions is work no invocation that
    selects a shipped backend should pay for.
    """

    from importlib.metadata import entry_points

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name != name:
            continue
        try:
            loaded = entry.load()
        except Exception as exc:
            raise ConfigurationError(
                f"cache backend '{name}' is registered under "
                f"'{ENTRY_POINT_GROUP}' by an installed distribution, but loading "
                f"it failed: {exc}. That is a problem with the distribution "
                "providing the backend, not with this configuration"
            ) from exc
        if not callable(loaded):
            raise ConfigurationError(
                f"cache backend '{name}' is registered under "
                f"'{ENTRY_POINT_GROUP}' but points at a "
                f"{type(loaded).__name__}, which cannot be called. The entry "
                "point has to name a factory taking a StoreContext"
            )
        return loaded
    return None
