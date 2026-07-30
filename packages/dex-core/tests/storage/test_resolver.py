"""Turning a configured name into a store, and refusing usefully when it cannot.

Every refusal here is something a human wrote in `.dex/config.yml` or typed on the
command line, so the assertions are mostly about the message: whether it names the
fix. A resolver that refuses correctly with an unusable message costs a support
conversation for every backend anyone tries to write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import ConfigurationError
from exmergo_dex_core.storage import (
    ExploreStore,
    FilesystemStore,
    StoreContext,
    build_store,
    resolve_store_factory,
)
from exmergo_dex_core.storage.resolver import ENTRY_POINT_GROUP

from ..fakes.stores import TenantStore

FAKES = "tests.fakes.stores"


@pytest.fixture(autouse=True)
def _clean_registry():
    TenantStore.reset()
    yield
    TenantStore.reset()


# --- the shipped names ---------------------------------------------------------


def test_the_default_name_builds_the_filesystem_backend(tmp_path: Path):
    store = build_store("filesystem", StoreContext(repo_root=str(tmp_path)))
    assert isinstance(store, FilesystemStore)
    assert store.root == tmp_path


def test_memory_is_refused_by_naming_the_process_boundary():
    # Not an "unknown backend": it plainly exists, it is simply wrong here, and a
    # user who selected it needs to know why rather than doubt the spelling.
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("memory")
    message = str(refusal.value)
    assert "its own process" in message
    assert "explore map" in message


def test_an_unknown_name_lists_what_exists_and_the_two_open_forms():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("postgres")
    message = str(refusal.value)
    assert "filesystem" in message
    assert "mypkg.stores:my_store" in message
    assert ENTRY_POINT_GROUP in message


def test_an_empty_name_refuses_rather_than_defaulting():
    # Silently falling back would make a typo'd or half-written config look like a
    # deliberate choice of the default.
    with pytest.raises(ConfigurationError):
        resolve_store_factory("   ")


# --- a dotted path to a backend defined outside the package --------------------


def test_a_dotted_path_resolves_a_function_factory():
    store = build_store(
        f"{FAKES}:tenant_store", StoreContext(options={"tenant": "acme"})
    )
    assert isinstance(store, TenantStore)
    assert store.tenant == "acme"
    assert isinstance(store, ExploreStore)


def test_a_dotted_path_resolves_a_class_whose_init_takes_the_context():
    store = build_store(
        f"{FAKES}:ContextBuiltStore", StoreContext(options={"tenant": "acme"})
    )
    assert store.tenant == "acme"


def test_a_dotted_path_resolves_a_classmethod():
    # The shape the shipped backends use, reachable by name like any other.
    store = build_store(
        "exmergo_dex_core.storage:FilesystemStore.from_context",
        StoreContext(repo_root="/somewhere"),
    )
    assert isinstance(store, FilesystemStore)


def test_options_reach_the_factory_verbatim():
    # dex does not interpret options, so a backend's own keys arrive untouched.
    first = build_store(f"{FAKES}:tenant_store", StoreContext(options={"tenant": "a"}))
    second = build_store(f"{FAKES}:tenant_store", StoreContext(options={"tenant": "b"}))
    assert (first.tenant, second.tenant) == ("a", "b")


def test_a_backend_selected_by_name_is_isolated_across_keys():
    from exmergo_dex_core.storage.conformance import a_cache

    one = build_store(f"{FAKES}:tenant_store", StoreContext(options={"tenant": "one"}))
    two = build_store(f"{FAKES}:tenant_store", StoreContext(options={"tenant": "two"}))
    one.save_cache(a_cache("db.one.orders"))
    assert two.load_cache() is None


# --- the refusals a bad dotted path earns --------------------------------------


def test_a_path_without_a_colon_says_where_the_colon_goes():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("mypkg.stores.my_store")
    # It falls through to the unknown-name refusal, which is the one that explains
    # both open forms, so the colon has to be visible there.
    assert "colon" in str(refusal.value)


def test_a_module_that_does_not_import_names_the_environment():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("no_such_module_anywhere:my_store")
    message = str(refusal.value)
    assert "will not import" in message
    assert "installed in the environment" in message


def test_a_missing_attribute_names_the_module_it_looked_in():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory(f"{FAKES}:no_such_factory")
    message = str(refusal.value)
    assert FAKES in message
    assert "no_such_factory" in message


def test_something_that_is_not_callable_is_refused_as_not_a_factory():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory(f"{FAKES}:NOT_CALLABLE")
    message = str(refusal.value)
    assert "cannot be called" in message
    assert "StoreContext" in message


def test_a_factory_returning_something_that_is_not_a_store_names_the_members():
    # The check is on what was built, not on the factory: a callable protocol can
    # only verify __call__ exists, which every callable satisfies.
    with pytest.raises(ConfigurationError) as refusal:
        build_store(f"{FAKES}:not_a_store", StoreContext())
    message = str(refusal.value)
    assert "not a store" in message
    assert "load_cache" in message
    assert "conformance" in message


def test_a_factory_that_fails_on_its_own_terms_says_so_and_keeps_the_cause():
    with pytest.raises(ConfigurationError) as refusal:
        build_store(f"{FAKES}:explodes", StoreContext(options={"tenant": "acme"}))
    message = str(refusal.value)
    assert "failed to build" in message
    assert "tenant directory is unreachable" in message
    assert isinstance(refusal.value.__cause__, RuntimeError)


def test_a_backends_own_refusal_is_not_buried_under_a_generic_one():
    # The backend said exactly what coordinate was missing. Wrapping that in "the
    # factory failed" would cost the only actionable part of the message.
    with pytest.raises(ConfigurationError) as refusal:
        build_store(f"{FAKES}:tenant_store", StoreContext(repo_root="/somewhere"))
    message = str(refusal.value)
    assert "cache.options.tenant" in message
    assert "failed to build" not in message


# --- the entry-point group -----------------------------------------------------


class _FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _patch_entry_points(monkeypatch, entries):
    import importlib.metadata

    def fake(group=None):
        assert group == ENTRY_POINT_GROUP
        return entries

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)


def test_an_entry_point_name_resolves_like_a_shipped_one(monkeypatch):
    from ..fakes.stores import tenant_store

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("acme", tenant_store)])
    store = build_store("acme", StoreContext(options={"tenant": "acme-inc"}))
    assert isinstance(store, TenantStore)


def test_a_shipped_name_cannot_be_shadowed_by_a_registration(monkeypatch):
    # Otherwise installing a package could silently change where an existing repo's
    # state lands, with nothing in the config file changing.
    from ..fakes.stores import tenant_store

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("filesystem", tenant_store)])
    store = build_store("filesystem", StoreContext(repo_root="/somewhere"))
    assert isinstance(store, FilesystemStore)


def test_an_entry_point_that_fails_to_load_blames_the_distribution(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("acme", ImportError("no module named acme"))]
    )
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("acme")
    assert "problem with the distribution" in str(refusal.value)


def test_an_entry_point_pointing_at_a_non_callable_is_refused(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("acme", "not a factory")])
    with pytest.raises(ConfigurationError) as refusal:
        resolve_store_factory("acme")
    assert "cannot be called" in str(refusal.value)
