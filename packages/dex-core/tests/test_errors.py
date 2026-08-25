"""The refusal surface a library consumer maps onto its own responses.

The CLI renders every refusal into an error envelope behind a catch-all, so it
never needed a hierarchy and would not notice if one regressed. A library
consumer cannot do that: catching ``Exception`` at an API boundary swallows real
bugs alongside deliberate refusals, so the guarantee these pin is that everything
dex refuses on purpose is reachable as ``DexError`` and nothing requires reaching
into a private module to catch.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import exmergo_dex_core as public
from exmergo_dex_core import (
    ConfigurationError,
    DexConfig,
    DexEngine,
    DexError,
    MemoryStore,
    NoConnectorSelectedError,
    RepoRootRequiredError,
    RequestError,
    StoreRequiredError,
)
from exmergo_dex_core.errors import ConfigurationError as _ConfigurationError


def _every_refusal_class() -> list[type]:
    """Every exception class the engine defines, found by walking the package.

    Walked rather than listed: a new refusal added in a future change is caught
    here without anyone remembering to update a list, which is the only way this
    assertion keeps its value.
    """

    import importlib
    import pkgutil

    found: list[type] = []
    package = importlib.import_module("exmergo_dex_core")
    for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        if info.name.endswith(".conformance"):
            continue  # ships pytest-dependent test code, not engine code
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue  # a module behind an extra that is not installed
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseException)
                and obj.__module__.startswith("exmergo_dex_core")
                and obj is not DexError
            ):
                found.append(obj)
    return sorted(set(found), key=lambda c: (c.__module__, c.__name__))


def test_every_refusal_the_engine_defines_roots_on_dex_error():
    stragglers = [c for c in _every_refusal_class() if not issubclass(c, DexError)]
    assert stragglers == [], (
        "these refusals cannot be caught as DexError, so a library consumer has "
        f"to catch Exception to see them: {[c.__name__ for c in stragglers]}"
    )


# Refusals a library consumer is not expected to catch by name, each for a
# stated reason. Everything else must be importable from the package root, and
# the test below enforces that, so adding a refusal forces a deliberate choice
# here rather than a silent omission. That omission is exactly how
# CacheRequiredError and NoBaselineError stayed unreachable long enough for
# someone embedding the engine to have to match on prose instead.
INTERNAL_REFUSALS = {
    # Connector-specific connection failures. A host catches the ConnectorError
    # family instead of importing seven names, one per warehouse it does not use.
    "BigQueryConnectionError",
    "ClickHouseConnectionError",
    "DatabricksConnectionError",
    "DuckDBReadOnlyError",
    "PostgresConnectionError",
    "RedshiftConnectionError",
    "SnowflakeConnectionError",
    # Wrapped before a caller sees it: the firewall turns this into
    # QueryRefusedError, which is what carries the refusal to the boundary.
    "NotSelectOnlyError",
    # An envelope-layer invariant, raised at the CLI boundary rather than
    # returned from the engine.
    "SanitizationError",
    # Control flow inside row-population attribution, never a boundary event:
    # every one is caught and rendered as the `reason` on a finding that reports
    # it could not attribute a change. A consumer reads that field, not this type.
    "UnattributableError",
    # The transform surface. Every one of these needs a repo_root, so they sit
    # outside "reachable from the public API without a repo_root". Worth
    # revisiting as a family if a host ever drives transform programmatically.
    "BuildFailedError",
    "DbtParseError",
    "DbtProjectError",
    "DbtRunError",
    "DevTargetError",
    "EditValidationError",
    "InitError",
    "ProdTargetRefusedError",
    "ScaffoldError",
    "TestScaffoldError",
}


def test_every_refusal_a_host_can_hit_is_importable_from_the_package_root():
    """Typed is not enough; a consumer has to be able to name the type.

    A refusal that is a distinct class but not exported leaves a host matching on
    prose, and prose is not an interface anyone owes stability on. Rewording an
    error message then silently changes which branch a consumer takes, which is a
    behavior change nothing in this repo would catch.
    """

    missing = sorted(
        cls.__name__
        for cls in _every_refusal_class()
        if cls.__name__ not in INTERNAL_REFUSALS and cls.__name__ not in public.__all__
    )
    assert missing == [], (
        "these refusals are typed but not importable from exmergo_dex_core, so a "
        "consumer has to match on message prose to branch on them: "
        f"{missing}. Export them, or add them to INTERNAL_REFUSALS with a reason."
    )


def test_the_prerequisite_family_is_what_a_host_retries_on():
    """The distinction the export gap cost a consumer real money to discover.

    A prerequisite refusal means state is missing and a named command creates it,
    so a caller can resolve it automatically and retry. A connector failure means
    the warehouse is unreachable. Both are DexError, and catching DexError cannot
    tell them apart, which is the whole reason these two families exist.
    """

    from exmergo_dex_core import (
        CacheRequiredError,
        ConnectorError,
        NoBaselineError,
        PrerequisiteError,
    )

    for cls in (CacheRequiredError, NoBaselineError):
        assert issubclass(cls, PrerequisiteError), cls
        assert not issubclass(cls, ConnectorError), cls

    from exmergo_dex_core.adapters.duckdb import DuckDBReadOnlyError

    assert issubclass(DuckDBReadOnlyError, ConnectorError)
    assert not issubclass(DuckDBReadOnlyError, PrerequisiteError)


def test_a_missing_credential_is_not_a_connector_failure():
    """Deliberate, and the opposite of what the family names suggest at a glance.

    A host backs off and retries on ConnectorError. A credential that was never
    configured will not appear on retry, and the message names the config that
    fixes it, so filing it under the retry family would send hosts down a loop
    that cannot terminate.
    """

    from exmergo_dex_core import ConnectorError, CredentialDiscoveryError, DexError

    assert issubclass(CredentialDiscoveryError, DexError)
    assert not issubclass(CredentialDiscoveryError, ConnectorError)


def test_a_backend_author_can_import_what_the_protocol_tells_them_to_raise():
    """`Store.load_plan` is documented as raising PlanNotFoundError.

    A third-party backend cannot satisfy that from a private module, so the name
    has to be public for the storage contract to be implementable at all.
    """

    from exmergo_dex_core import PlanNotFoundError
    from exmergo_dex_core.storage.base import Store

    assert "PlanNotFoundError" in Store.load_plan.__doc__
    assert issubclass(PlanNotFoundError, DexError)


def test_the_hosted_semantic_refusals_are_importable_from_the_package_root():
    # The documented catch for the hosted semantic path. Reaching them through
    # exmergo_dex_core.explore.semantic makes a consumer depend on a module
    # layout that is not public surface.
    assert issubclass(public.SemanticQueryRefusedError, public.SemanticBackendError)
    assert issubclass(public.SemanticBackendError, DexError)


def test_the_configuration_family_still_catches_as_value_error():
    # These raised bare ValueError before they were typed. A consumer written
    # against that API keeps working; that is the point of adding the type rather
    # than trading one breaking change for another.
    for cls in (
        ConfigurationError,
        RequestError,
        NoConnectorSelectedError,
        RepoRootRequiredError,
        StoreRequiredError,
    ):
        assert issubclass(cls, ValueError), cls
        assert issubclass(cls, DexError), cls


def test_configuration_error_is_the_same_class_through_both_import_paths():
    assert ConfigurationError is _ConfigurationError


# --- the refusals reachable from the public API without a repo root -----------


def test_no_connector_selected_is_typed():
    engine = DexEngine(store=MemoryStore())
    with pytest.raises(NoConnectorSelectedError) as refusal:
        engine.connect_test()
    assert "connector" in str(refusal.value)


def test_a_missing_repo_root_names_what_needed_it():
    engine = DexEngine(
        connector="duckdb", path="x.duckdb", config=DexConfig(connector="duckdb")
    )
    with pytest.raises(RepoRootRequiredError) as refusal:
        engine.project_dir()
    assert "repo root" in str(refusal.value)


def test_a_metered_connector_with_no_store_is_typed():
    from exmergo_dex_core.connect import _require_store

    with pytest.raises(StoreRequiredError) as refusal:
        _require_store(None, "bigquery")
    assert "spend ledger" in str(refusal.value)


def test_an_unusable_column_in_the_call_is_a_request_error(duckdb_file: Path):
    pytest.importorskip("sklearn")
    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=DexConfig(connector="duckdb"),
        store=MemoryStore(),
    ) as engine:
        engine.map()
        with pytest.raises(RequestError) as refusal:
            engine.cluster("customers", features=["email"])
    # Bad input rather than missing input: the object is profiled and the column
    # exists, the call just asked to cluster on text.
    assert "not numeric" in str(refusal.value)


def test_an_unprofiled_object_is_still_catchable_as_dex_error(duckdb_file: Path):
    # Deliberately not a RequestError: the fix is a dex command (`explore map`),
    # not a corrected argument, and the refusal says so. What matters to a
    # consumer is that one catch covers both.
    pytest.importorskip("sklearn")
    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=DexConfig(connector="duckdb"),
        store=MemoryStore(),
    ) as engine:
        engine.map()
        with pytest.raises(DexError) as refusal:
            engine.cluster("no_such_table")
    assert "no_such_table" in str(refusal.value)


def test_an_injected_connection_for_duckdb_is_a_configuration_error():
    from exmergo_dex_core import ConnectionSource

    engine = DexEngine(
        connector="duckdb",
        path="x.duckdb",
        config=DexConfig(connector="duckdb"),
        store=MemoryStore(),
        connection=ConnectionSource(connect=lambda: object()),
    )
    with pytest.raises(ConfigurationError) as refusal:
        engine.connect_test()
    assert "duckdb" in str(refusal.value)
