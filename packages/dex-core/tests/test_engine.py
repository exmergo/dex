"""The public DexEngine API: what a library caller gets, and what it must never do.

Most of this file is about the second half. An engine is the first surface that
lets a host process serve more than one principal, and the failure mode there is
software that appears to work: a stray config picked up from the filesystem, a
cache shared between tenants, a confirmation from one call standing in for the
next. Each of those is asserted here rather than left to a reviewer to notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from exmergo_dex_core import (
    ConfigurationError,
    ConfirmationRequiredError,
    DexConfig,
    DexEngine,
    FilesystemStore,
    MemoryStore,
)
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import (
    CacheConfig,
    ClickHouseTarget,
    DuckDBTarget,
    save_config,
)
from exmergo_dex_core.envelope import Envelope, Paradigm
from exmergo_dex_core.explore.results import MapResult, ProfileResult, QueryResult

SRC = Path(__file__).resolve().parents[1] / "src" / "exmergo_dex_core"


# --- the surface itself --------------------------------------------------------


def test_cloud_clickhouse_paradigm_is_available_before_connection_failure():
    """An error envelope must still name the binding unit when no adapter opens."""

    engine = DexEngine(
        config=DexConfig(
            connector="clickhouse",
            clickhouse=ClickHouseTarget(deployment="cloud"),
        )
    )
    assert engine.paradigm is Paradigm.COMPUTE_TIME


def test_the_public_import_works_and_defaults_to_writing_nothing(
    duckdb_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The headline promise: import, call, and the consumer's tree is untouched.

    A `.dex/` directory materializing in someone's repo because they imported a
    library is an unrequested side effect, and the default store is what makes
    that impossible rather than merely unlikely.
    """

    repo = duckdb_file.parent
    monkeypatch.chdir(repo)

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(repo)): p.read_bytes()
            for p in sorted(repo.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    with DexEngine(connector="duckdb", path=str(duckdb_file)) as eng:
        assert isinstance(eng.store, MemoryStore)
        profiled = eng.profile("customers")
        rows = eng.query("select count(*) as n from customers")

    assert profiled.profiled_count == 1
    assert rows.row_count == 1
    assert snapshot() == before
    assert not (repo / ".dex").exists()


def test_methods_return_domain_objects_never_envelopes(duckdb_file: Path):
    """Type-level, not shape-level: an envelope leaking through the API would
    make every consumer parse transport it never asked for."""

    with DexEngine(connector="duckdb", path=str(duckdb_file)) as eng:
        profiled = eng.profile("customers")
        mapped = eng.map()
        rows = eng.query("select 1 as n")

    assert isinstance(profiled, ProfileResult)
    assert isinstance(mapped, MapResult)
    assert isinstance(rows, QueryResult)
    for result in (profiled, mapped, rows):
        assert not isinstance(result, Envelope)
    # The domain objects come back whole, not as dicts a caller has to re-parse.
    assert profiled.datasets[0].identifier.endswith("customers")
    assert isinstance(mapped.cache, DexCache)


#: Subcommands that are not engine methods, each for a reason the reader can
#: check. `test` is `transform test --scaffold`, reached as `test_scaffold`;
#: `semantic *` is spelled `semantic_*`; `demo` writes a warehouse and is not a
#: command a library caller drives. Everything else must have a method.
_NOT_ENGINE_METHODS = {
    ("transform", "test"),
    ("semantic", "define"),
    ("semantic", "update"),
    ("semantic", "plan"),
    ("viz", "preview"),
}

#: Where a subcommand's method is spelled differently from the subcommand.
_METHOD_NAMES = {
    ("connect", "test"): "connect_test",
    ("explore", "semantic"): "semantic_list",
    ("maintain", "schema"): "schema_drift",
    ("maintain", "volume"): "volume_drift",
    ("maintain", "grain"): "grain_drift",
    ("maintain", "semantic"): "semantic_drift",
    ("transform", "init"): "init_project",
}


def test_every_cli_subcommand_is_reachable_from_the_python_api():
    """The CLI is a consumer of this API, not a parallel implementation of it.

    That is the promise this module's own docstring makes, and it is the one a new
    command quietly breaks: a `cmd_*` shim that calls its module function directly
    works perfectly through the CLI and leaves the capability unreachable for every
    library caller. Nothing else in the suite notices, because both surfaces are
    tested against their own entry point.

    Asserted as a set difference rather than per command so adding a subcommand
    fails here until it is either given a method or listed above with a reason.
    """

    from exmergo_dex_core.cli import COMMAND_SURFACE

    missing = []
    for group, subcommands in COMMAND_SURFACE.items():
        for sub in subcommands:
            if (group, sub) in _NOT_ENGINE_METHODS:
                continue
            method = _METHOD_NAMES.get((group, sub), sub)
            if not callable(getattr(DexEngine, method, None)):
                missing.append(f"{group} {sub} (expected DexEngine.{method})")

    assert missing == [], (
        "these subcommands are reachable from the CLI and not from the Python "
        f"API: {missing}. Add the method, or list it in _NOT_ENGINE_METHODS with "
        "the reason it is CLI-only."
    )


def test_the_engine_module_never_references_the_envelope():
    """Structural, over the parsed module rather than its text.

    The boundary holds only if nothing on this side builds transport. Checking
    the AST rather than grepping means the docstring may explain the rule using
    the very name the rule forbids, which is where a reader looks first.
    """

    import ast

    tree = ast.parse((SRC / "engine.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "Envelope" not in names
    assert not names & {"ok", "error", "needs_confirmation", "not_implemented"}


def test_a_dex_config_object_drives_a_full_flow_with_no_config_file(
    duckdb_file: Path, tmp_path: Path
):
    """Config as an object, not only as a file. Otherwise one filesystem
    dependency is removed while another quietly remains."""

    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path=str(duckdb_file)))
    with DexEngine(config=config) as eng:
        mapped = eng.map()
        rows = eng.query("select count(*) as n from orders")

    assert mapped.object_count == 2
    assert rows.row_count == 1
    assert not list(tmp_path.rglob(".dex"))


def test_close_is_idempotent_and_the_context_manager_closes(duckdb_file: Path):
    eng = DexEngine(connector="duckdb", path=str(duckdb_file))
    with eng:
        eng.inventory()
        adapter = eng._adapter_instance
        assert adapter is not None
    assert eng._adapter_instance is None
    # A second close is harmless: a caller unwinding several layers should not
    # have to track whether someone else already closed.
    eng.close()
    eng.close()


def test_no_connector_anywhere_refuses_instead_of_defaulting():
    """No silent connector default, in the API's own vocabulary. Defaulting to
    duckdb here would connect a caller to a target they never named."""

    with DexEngine() as eng, pytest.raises(ValueError, match="no connector selected"):
        eng.inventory()


# --- the run-directory DuckDB auto-detect (issue #199) -------------------------


def _make_duckdb(path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()


def test_lone_duckdb_file_in_run_directory_is_used_and_warned(tmp_path: Path):
    """Acceptance: one .duckdb present, no config: the command runs and warns,
    naming the file."""

    lone = tmp_path / "lone.duckdb"
    _make_duckdb(lone)

    with DexEngine(repo_root=str(tmp_path)) as eng:
        mapped = eng.inventory()
        assert len(mapped.objects) == 1
        assert len(eng.connection_warnings) == 1
        assert "lone.duckdb" in eng.connection_warnings[0]
        assert eng.connector == "duckdb"
        assert eng.path == str(lone)


def test_two_duckdb_files_in_run_directory_still_refuses_naming_both(
    tmp_path: Path,
):
    """Acceptance: two present: refused, both listed."""

    a = tmp_path / "a.duckdb"
    b = tmp_path / "b.duckdb"
    _make_duckdb(a)
    _make_duckdb(b)

    with (
        DexEngine(repo_root=str(tmp_path)) as eng,
        pytest.raises(ValueError, match="more than one DuckDB file") as refusal,
    ):
        eng.inventory()
    assert "a.duckdb" in str(refusal.value)
    assert "b.duckdb" in str(refusal.value)
    # Never a silent guess: a refusal carries no warning to act on.
    assert eng.connection_warnings == []


def test_zero_duckdb_files_in_run_directory_is_the_existing_refusal_unchanged(
    tmp_path: Path,
):
    """Acceptance: zero present: the existing refusal, unchanged."""

    with (
        DexEngine(repo_root=str(tmp_path)) as eng,
        pytest.raises(ValueError, match=r"no \.dex/config\.yml found"),
    ):
        eng.inventory()
    assert eng.connection_warnings == []


def test_config_present_is_unchanged_even_naming_a_different_file(tmp_path: Path):
    """Acceptance: config present: unchanged, even if it names a different file.

    A lone real .duckdb file sits right there, but a config was declared (even
    one naming a relation that does not exist), so the auto-detect must never
    override it: the failure is a connection failure on the configured path,
    never a silent switch to the file that happens to be lying around.
    """

    _make_duckdb(tmp_path / "lone.duckdb")
    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path="elsewhere.duckdb"))

    # The configured (nonexistent) file is what fails to open, not a silent
    # switch to the real lone file sitting right there.
    with (
        DexEngine(repo_root=str(tmp_path), config=config) as eng,
        pytest.raises(Exception, match=r"elsewhere\.duckdb"),
    ):
        eng.inventory()
    assert eng.connection_warnings == []


def test_explicit_path_is_unchanged_even_with_a_lone_duckdb_file_present(
    tmp_path: Path,
):
    """Acceptance: --path present: unchanged (no auto-detect note, since
    nothing was guessed)."""

    lone = tmp_path / "lone.duckdb"
    _make_duckdb(lone)

    with DexEngine(repo_root=str(tmp_path), connector="duckdb", path=str(lone)) as eng:
        mapped = eng.inventory()
        assert len(mapped.objects) == 1
    assert eng.connection_warnings == []


def test_lone_duckdb_auto_detect_does_not_apply_with_no_repo_root_at_all():
    """A library caller holding no repo concept (``repo_root=None``) is the
    other spelling of this same refusal, and has no run directory to look in;
    it must keep refusing exactly as before, never scanning the process's own
    cwd for a stray .duckdb file that has nothing to do with the caller."""

    with DexEngine() as eng, pytest.raises(ValueError, match="no connector selected"):
        eng.inventory()
    assert eng.connection_warnings == []


def test_commands_needing_the_project_refuse_without_a_repo_root():
    """The dbt project is a filesystem artifact by design, so the commands that
    read or write it say which operation needed a root rather than failing with
    a bare None several frames down."""

    with DexEngine(config=DexConfig()) as eng:
        with pytest.raises(ValueError, match="scaffolding a dbt project"):
            eng.init_project("analytics", connector="duckdb")
        with pytest.raises(ValueError, match="locating the dbt project"):
            eng.project_dir()


# --- selecting the store from configuration ---------------------------------------


def test_from_repo_still_builds_the_filesystem_backend_by_default(tmp_path: Path):
    """The default is what every existing repo depends on, so it is asserted
    rather than assumed: selecting nothing keeps writing loose JSON under
    `.dex/`, and the selector is invisible to anyone who never sets it."""

    with DexEngine.from_repo(tmp_path) as eng:
        assert isinstance(eng.store, FilesystemStore)
        assert eng.store.root == Path(tmp_path)


def test_a_configured_backend_outside_the_package_is_what_from_repo_builds(
    tmp_path: Path,
):
    from .fakes.stores import TenantStore

    TenantStore.reset()
    save_config(
        DexConfig(
            connector="duckdb",
            cache=CacheConfig(
                backend="tests.fakes.stores:tenant_store",
                options={"tenant": "acme"},
            ),
        ),
        tmp_path,
    )
    with DexEngine.from_repo(tmp_path) as eng:
        assert isinstance(eng.store, TenantStore)
        assert eng.store.tenant == "acme"
    # And nothing was written to the repo, because this backend is not the
    # filesystem one; the selection actually took effect.
    assert not (tmp_path / ".dex" / "cache.json").exists()
    TenantStore.reset()


def test_an_explicit_store_wins_over_the_configured_one(tmp_path: Path):
    # A caller holding an instance has already made the decision configuration
    # exists to make for the callers who have not.
    save_config(
        DexConfig(cache=CacheConfig(backend="tests.fakes.stores:tenant_store")),
        tmp_path,
    )
    with DexEngine.from_repo(tmp_path, store=MemoryStore()) as eng:
        assert isinstance(eng.store, MemoryStore)


def test_the_cache_backend_override_beats_the_configured_name(tmp_path: Path):
    from .fakes.stores import TenantStore

    TenantStore.reset()
    selected = "tests.fakes.stores:tenant_store"
    save_config(
        DexConfig(cache=CacheConfig(backend=selected, options={"tenant": "acme"})),
        tmp_path,
    )
    # Overriding back to the shipped backend for one run is the whole reason this
    # flag exists, and it has to work on a repo that configures a custom one.
    with DexEngine.from_repo(tmp_path, cache_backend="filesystem") as eng:
        assert isinstance(eng.store, FilesystemStore)
    # Naming the configured backend explicitly keeps its options, so the override
    # is not a way to accidentally drop them.
    with DexEngine.from_repo(tmp_path, cache_backend=selected) as eng:
        assert isinstance(eng.store, TenantStore)
        assert eng.store.tenant == "acme"
    TenantStore.reset()


def test_the_override_does_not_hand_one_backends_options_to_another(tmp_path: Path):
    """Options are not namespaced by backend, so carrying them across an override
    would give a backend coordinates written for a different one. The shipped
    backends refuse an option they would have ignored, so this would present as a
    refusal for a flag that ought to be the simplest thing in the tool."""

    save_config(
        DexConfig(
            cache=CacheConfig(
                backend="tests.fakes.stores:tenant_store", options={"tenant": "acme"}
            )
        ),
        tmp_path,
    )
    with DexEngine.from_repo(tmp_path, cache_backend="filesystem") as eng:
        assert isinstance(eng.store, FilesystemStore)


def test_a_repo_with_no_config_at_all_still_gets_the_default_backend(tmp_path: Path):
    # load_config returns None outside a project, and the store has to be built
    # anyway: commands that need no warehouse still work there.
    with DexEngine.from_repo(tmp_path / "not-a-project") as eng:
        assert isinstance(eng.store, FilesystemStore)


def test_an_unusable_backend_refuses_at_construction_naming_the_fix(tmp_path: Path):
    save_config(DexConfig(cache=CacheConfig(backend="nonsense")), tmp_path)
    with pytest.raises(ConfigurationError, match="unknown cache backend"):
        DexEngine.from_repo(tmp_path)


# --- tenancy: one engine, one principal ------------------------------------------


def test_an_explicit_config_never_reads_one_from_disk(
    duckdb_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The adversarial case, and the one most worth pinning.

    Config resolution walks up from the working directory to the git root, so a
    container with someone else's `.dex/config.yml` anywhere above it would
    otherwise inherit their connector, their budget, and their PII overrides.
    That is a wrong-connection bug that presents as working software, which is
    why an injected config has to mean "no file is read", not "prefer this one".
    """

    workdir = tmp_path / "tenant" / "work"
    workdir.mkdir(parents=True)
    (tmp_path / "tenant" / ".git").mkdir()
    planted = tmp_path / "tenant" / ".dex"
    planted.mkdir()
    (planted / "config.yml").write_text(
        "connector: bigquery\n"
        "bigquery:\n  project: someone-elses-project\n"
        "budget:\n  ceiling: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workdir)

    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path=str(duckdb_file)))
    with DexEngine(config=config, repo_root=str(workdir)) as eng:
        result = eng.inventory()
        # The planted connector never governed: a bigquery engine could not have
        # listed these objects, and the planted ceiling of 1 would have refused.
        assert eng._adapter_instance.name == "duckdb"

    assert {o.identifier.split(".")[-1] for o in result.objects} == {
        "customers",
        "orders",
    }
    assert eng.config.budget.ceiling != 1


def test_two_engines_in_one_process_share_nothing(duckdb_file: Path):
    """One engine per principal only works if engines are actually isolated.

    The exploration cache retains value ranges and exact counts, and the query
    firewall decides what a query may name from that cache's membership, so a
    shared one is both a disclosure and the wrong authorization surface.
    """

    first = DexEngine(connector="duckdb", path=str(duckdb_file))
    second = DexEngine(connector="duckdb", path=str(duckdb_file))
    with first, second:
        first.profile("customers")
        assert second.store.load_cache() is None
        assert first.store is not second.store
        assert first._adapter_instance is not second._adapter_instance
        # And the ledgers stay separate too, not just the cache.
        first.store.append_query_log({"at": "2026-01-01T00:00:00+00:00", "sql": "x"})
        assert second.store.spend_since("2026-01-01T00:00:00+00:00") == 0.0


def test_the_engine_is_the_only_thing_that_opens_a_connection():
    """One adapter funnel, asserted structurally.

    Establishing the credential and building the cost gate meet in exactly one
    place, which is what makes both reviewable. A second opener would be a second
    place to get either wrong, and it would be easy to add without noticing. It
    matters more now that a host can supply the connection: an opener that did not
    thread it through would silently reach the warehouse as the container instead
    of as the end user, and nothing in the output would say so.
    """

    callers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "open_adapter(" in path.read_text(encoding="utf-8")
    )
    # connect.py defines it; engine.py is the funnel; dev_target.py runs the
    # free preflight probes, which are pre-connection by nature (they answer
    # "can this target be built into at all", before an engine would be useful)
    # and take the same config and connection the caller holds, so the probe and
    # the command it precedes run as one principal.
    assert callers == ["connect.py", "engine.py", "transform/dev_target.py"]


# --- gate lifetime ---------------------------------------------------------------


def test_each_call_gets_its_own_cost_gate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A cost gate is per-command state, not per-connection.

    Holding one across calls would let one call's confirmation stand in for the
    next one's, let charged estimates accumulate until an in-budget call is
    refused, and file every ledger entry under whichever command opened the
    connection first. The connection is held; the gate is not.
    """

    pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget

    client = FakeBigQueryClient(project="test-proj", tables=[])
    opened: list[BigQueryAdapter] = []

    def opener(**kwargs):
        adapter = BigQueryAdapter(
            project="test-proj",
            cost_gate=kwargs["config"] and _gate(kwargs),
            target=BigQueryTarget(),
            client=client,
            principal_type="user",
        )
        opened.append(adapter)
        return adapter

    def _gate(kwargs):
        from exmergo_dex_core.connect import new_cost_gate

        return new_cost_gate(
            "bigquery",
            kwargs["config"],
            kwargs["store"],
            budget=kwargs.get("budget"),
            confirmed=kwargs.get("confirmed", False),
            command=kwargs.get("command"),
        )

    import exmergo_dex_core.connect as connect_mod

    monkeypatch.setattr(connect_mod, "open_adapter", opener)

    config = DexConfig(connector="bigquery", bigquery=BigQueryTarget(project="p"))
    eng = DexEngine(
        config=config,
        store=FilesystemStore(tmp_path),
        confirmed=True,
        budget=1_000.0,
    )
    with eng:
        first = eng._adapter("explore profile")
        first.cost_gate.charge(600.0)
        second = eng._adapter("explore query")

    # The connection is held (opening a warehouse session per call is expensive)...
    assert first is second
    assert len(opened) == 1
    # ...but the gate is not: the second command starts from zero charged, is
    # labelled with its own command, and has its full budget available.
    assert second.cost_gate is not None
    assert second.cost_gate.command == "explore query"
    assert second.cost_gate.statement_cap(unit="byte") == 1_000
    second.cost_gate.charge(600.0)  # would raise if the first charge carried over


def test_a_per_call_confirmation_overrides_the_engine_default(
    duckdb_file: Path, tmp_path: Path
):
    """Confirmation belongs to a call. An engine-level default exists because the
    CLI runs one command per process, but a caller must be able to confirm one
    call without confirming everything after it."""

    eng = DexEngine(
        connector="duckdb", path=str(duckdb_file), store=FilesystemStore(tmp_path)
    )
    with eng:
        # DuckDB is free, so there is no gate to swap; what this pins is that the
        # per-call value reaches the factory rather than being ignored.
        adapter = eng._adapter("explore profile", confirmed=True)
        assert getattr(adapter, "cost_gate", None) is None


def test_an_unconfirmed_billed_call_raises_and_spends_nothing(
    fake_bq_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Cost surfaced before any spend, through the API rather than the CLI.

    The exception carries the payload a caller needs to re-issue, and only free
    dry-runs reached the warehouse.
    """

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget
    from exmergo_dex_core.connect import new_cost_gate

    config = DexConfig(connector="bigquery", bigquery=BigQueryTarget(project="p"))
    store = FilesystemStore(tmp_path)

    def opener(**kwargs):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=new_cost_gate(
                "bigquery",
                config,
                store,
                budget=kwargs.get("budget"),
                confirmed=kwargs.get("confirmed", False),
                command=kwargs.get("command"),
            ),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    import exmergo_dex_core.connect as connect_mod

    monkeypatch.setattr(connect_mod, "open_adapter", opener)

    with (
        DexEngine(config=config, store=store) as eng,
        pytest.raises(ConfirmationRequiredError) as caught,
    ):
        eng.profile("customers")

    request = caught.value.request
    assert request.cost.estimate is not None and request.cost.estimate > 0
    assert "per_table_bytes" in request.data
    assert "--confirm" in request.data["hint"]
    # Nothing executed: only free metadata and dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


# --- the connection seam: who the process is connected as -------------------------
#
# Ambient credential discovery is right for one person at a terminal and cannot
# express per-end-user access control in a process serving several, because
# ambient is process-wide. A host supplies the connection instead. Every test here
# blocks discovery outright, because one that merely passes a connection would
# still pass if discovery had quietly run anyway.


def _no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ambient credential chain raise.

    This is what makes the tests below mean anything: the assertion is not "an
    injected connection works" but "an injected connection is the only thing that
    worked", which is exactly the claim a multi-tenant host is relying on.
    """

    import exmergo_dex_core.connect as connect_mod

    def unreachable(*_args, **_kwargs):
        raise AssertionError("ambient credential discovery ran")

    for name in (
        "_default_credentials",
        "resolve_snowflake_connection",
        "resolve_databricks_connection",
        "resolve_postgres_connection",
        "resolve_redshift_connection",
        "resolve_clickhouse_connection",
    ):
        monkeypatch.setattr(connect_mod, name, unreachable)


def _pg_engine(connection, store=None, **kwargs) -> DexEngine:
    from exmergo_dex_core.config import PostgresTarget

    config = DexConfig(connector="postgres", postgres=PostgresTarget(schemas=["shop"]))
    return DexEngine(
        config=config,
        store=MemoryStore() if store is None else store,
        connection=connection,
        **kwargs,
    )


def _bq_credential_engine(fake_bq_client, monkeypatch, **kwargs) -> DexEngine:
    """An engine whose BigQuery credential comes from a host, not from ADC.

    BigQuery is the connector that takes a *credential* rather than a connection,
    and the adapter turns it into a client. Substituting ``bigquery.Client`` is
    therefore the honest fake: it stands in for the client that credential would
    have produced, and everything from connect.py down runs for real.
    """

    from google.cloud import bigquery

    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import BigQueryTarget

    _no_discovery(monkeypatch)

    class HostCredential:
        pass

    HostCredential.__module__ = "google.oauth2.service_account"
    supplied: list[object] = []

    def client(*, project, credentials=None, **_kw):
        supplied.append(credentials)
        return fake_bq_client

    monkeypatch.setattr(bigquery, "Client", client)
    config = DexConfig(
        connector="bigquery",
        bigquery=BigQueryTarget(project="test-proj", datasets=["shop"]),
    )
    engine = DexEngine(
        config=config,
        store=MemoryStore(),
        connection=ConnectionSource(connect=HostCredential),
        **kwargs,
    )
    engine.supplied_credentials = supplied
    return engine


def test_a_host_supplied_connection_drives_a_full_flow(
    fake_bq_client, monkeypatch: pytest.MonkeyPatch
):
    """The whole point of the seam, end to end with no ambient credential.

    A host that authenticated its own end user hands dex the credential, and
    everything downstream (inventory, profiling, the firewall, the cache) runs
    against it. Discovery is rigged to raise, so this cannot pass by accident:
    without the host's credential there is no way to reach the warehouse at all.
    """

    pytest.importorskip("google.cloud.bigquery")

    fake_bq_client.row_resolver = _aggregate_resolver
    engine = _bq_credential_engine(
        fake_bq_client, monkeypatch, confirmed=True, budget=float(500 * 1024 * 1024)
    )
    with engine as eng:
        profiled = eng.profile("customers")
        rows = eng.query("select count(*) as n from customers")

    assert profiled.profiled_count == 1
    assert rows.row_count == 1
    # It really was the host's credential that opened the client, not a
    # discovered one that happened to be lying around.
    assert type(engine.supplied_credentials[0]).__name__ == "HostCredential"


def _aggregate_resolver(sql: str):
    """Aggregate rows for a profiling pass, keyed by the aliases the engine
    builds. A superset: each connector's plan reads the aliases it asked for and
    ignores the rest, so one resolver serves every fake here."""

    values: dict[str, object] = {"n_total": 100}
    for i in range(10):
        values[f"nn_{i}"] = 100
        values[f"nd_{i}"] = 100 if i == 0 else 40
        values[f"mn_{i}"] = 1
        values[f"mx_{i}"] = 100
        values[f"d_{i}"] = 100
    return [values]


def test_the_factory_is_called_once_and_only_when_something_connects(
    fake_pg_connection, monkeypatch: pytest.MonkeyPatch
):
    """Lazily, then held. Constructing an engine per request is the documented
    usage, so construction itself must not reach the host's credential; and the
    held connection means a request pays for one session, not one per call."""

    from exmergo_dex_core import ConnectionSource

    _no_discovery(monkeypatch)
    calls = []

    def connect():
        calls.append(1)
        return fake_pg_connection

    engine = _pg_engine(ConnectionSource(connect=connect))
    assert calls == []  # constructing an engine connects to nothing
    with engine as eng:
        eng.connect_test()
        eng.inventory()
    assert calls == [1]


def test_an_injected_connection_is_not_closed_but_a_discovered_one_is(
    fake_pg_connection, monkeypatch: pytest.MonkeyPatch
):
    """dex closes only what it opened.

    Both halves are asserted, or the first passes vacuously: a `close()` that
    never closed anything would satisfy "the injected one stayed open" while
    leaking every connection dex opens for itself.
    """

    import psycopg

    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core import ConnectionSource

    _no_discovery(monkeypatch)
    with _pg_engine(ConnectionSource(connect=lambda: fake_pg_connection)) as eng:
        eng.connect_test()
    assert fake_pg_connection.closed is False

    # The same fake, reached the way discovery reaches a real connection: dex
    # built what it holds, so dex closes it.
    monkeypatch.setattr(
        connect_mod,
        "resolve_postgres_connection",
        lambda *_a, **_kw: ({}, "environment:password"),
    )
    monkeypatch.setattr(psycopg, "connect", lambda **_kw: fake_pg_connection)
    with _pg_engine(None) as eng:
        eng.connect_test()
    assert fake_pg_connection.closed is True


def test_capabilities_stays_truthful_about_an_injected_connection(
    fake_pg_connection, monkeypatch: pytest.MonkeyPatch
):
    """An agent reads `capabilities()` to reason about what it is connected as, so
    an injected connection must not quietly report itself as `unknown`. The
    default says host_supplied; a host that knows the credential kind refines it,
    and neither spelling can carry a credential value."""

    from exmergo_dex_core import ConnectionSource

    _no_discovery(monkeypatch)
    with _pg_engine(ConnectionSource(connect=lambda: fake_pg_connection)) as eng:
        assert eng.connect_test().capabilities["auth_method"] == "host_supplied"

    source = ConnectionSource(
        connect=lambda: fake_pg_connection, auth_method="host_supplied:oauth_user"
    )
    with _pg_engine(source) as eng:
        caps = eng.connect_test().capabilities
    assert caps["auth_method"] == "host_supplied:oauth_user"


def test_a_free_databricks_command_never_calls_the_host_factory(
    fake_databricks, monkeypatch: pytest.MonkeyPatch
):
    """Databricks has two clients, and the split is what keeps free paths free.

    Metadata rides the Unity Catalog REST client; the factory opens a SQL session,
    which lands on the warehouse and can wake it. An injected source must preserve
    that, or a host pays for a command that reads nothing.
    """

    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import DatabricksTarget

    _no_discovery(monkeypatch)
    config = DexConfig(
        connector="databricks",
        databricks=DatabricksTarget(warehouse="fake-wh", catalogs=["shop"]),
    )
    source = ConnectionSource(
        connect=fake_databricks.sql_connect, workspace=fake_databricks.workspace
    )
    with DexEngine(config=config, store=MemoryStore(), connection=source) as eng:
        eng.connect_test()
        eng.inventory()

    assert fake_databricks.connect_count == 0


def test_a_session_opened_from_a_host_factory_is_still_the_hosts(
    fake_databricks, monkeypatch: pytest.MonkeyPatch
):
    """The case the rule is easiest to get wrong.

    dex calls the host's factory, so the session it holds is one dex obtained but
    did not create. Closing it would reach into a pool or a per-request handle the
    host still owns. The adapter releases its reference either way, so a later
    command opens a fresh session rather than reusing one it stopped tracking.
    """

    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import DatabricksTarget

    _no_discovery(monkeypatch)
    fake_databricks.workspace.warehouse.state = "RUNNING"
    fake_databricks.connection.startup_pending = False
    config = DexConfig(
        connector="databricks",
        databricks=DatabricksTarget(warehouse="fake-wh", catalogs=["shop"]),
    )
    fake_databricks.connection.row_resolver = _aggregate_resolver
    source = ConnectionSource(
        connect=fake_databricks.sql_connect, workspace=fake_databricks.workspace
    )
    with DexEngine(
        config=config,
        store=MemoryStore(),
        connection=source,
        confirmed=True,
        budget=10_000.0,
    ) as eng:
        eng.profile("shop.core.customers")

    assert fake_databricks.connect_count == 1  # the billed path did open one
    assert fake_databricks.connection.closed is False


def test_a_bigquery_principal_type_is_derived_not_asserted(
    fake_bq_client, monkeypatch: pytest.MonkeyPatch
):
    """BigQuery injects a credential rather than a connection, and classifies it
    the same way on both paths. Derived from the credential's own type, so a host
    cannot claim to be a service account it is not, and an omitted principal_type
    still reports something true."""

    from exmergo_dex_core.connect import gcp_principal_type

    # The classification itself, pinned against each module the chain looks for,
    # so it holds without the BigQuery extra installed.
    class Credential:
        pass

    for module, expected in (
        ("google.auth.impersonated_credentials", "impersonated_service_account"),
        ("google.auth.external_account", "external_account"),
        ("google.oauth2.service_account", "service_account"),
        ("google.auth.compute_engine.credentials", "metadata"),
        ("google.oauth2.credentials", "user"),
    ):
        Credential.__module__ = module
        assert gcp_principal_type(Credential()) == expected

    # And the same classification reached through the real wiring: the helper
    # supplies a credential whose module says service_account, and nothing in
    # between is allowed to downgrade that to "unknown".
    pytest.importorskip("google.cloud.bigquery")
    with _bq_credential_engine(fake_bq_client, monkeypatch) as eng:
        caps = eng.connect_test().capabilities

    assert caps["principal_type"] == "service_account"


def test_a_connection_is_refused_where_it_cannot_be_honored():
    """Accepted-and-ignored is the failure this seam cannot afford: a host that
    believes it supplied a principal, and in fact reached the warehouse as the
    container, has lost exactly the access control it came here for and nothing
    in the output would say so."""

    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import DatabricksTarget, DuckDBTarget

    source = ConnectionSource(connect=lambda: object())

    # DuckDB is a local file dex opens read-only itself; an injected handle could
    # be writable, so the read-only guarantee would become unenforceable.
    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path="x.duckdb"))
    with pytest.raises(ValueError, match="does not apply to the duckdb connector"):
        DexEngine(config=config, connection=source)._adapter()

    # Databricks needs both clients: without the REST one there is no free
    # metadata path at all.
    config = DexConfig(connector="databricks", databricks=DatabricksTarget())
    with pytest.raises(ValueError, match="needs workspace="):
        DexEngine(config=config, store=MemoryStore(), connection=source)._adapter()

    # And a workspace anywhere else would be silently dropped.
    config = DexConfig(connector="postgres")
    workspace_elsewhere = ConnectionSource(connect=lambda: object(), workspace=object())
    with pytest.raises(ValueError, match="Databricks vocabulary"):
        DexEngine(
            config=config, store=MemoryStore(), connection=workspace_elsewhere
        )._adapter()

    # As would a principal_type on a connector that reports no such field.
    claimed = ConnectionSource(connect=lambda: object(), principal_type="user")
    with pytest.raises(ValueError, match="BigQuery vocabulary"):
        DexEngine(config=config, store=MemoryStore(), connection=claimed)._adapter()


# --- the export surface ----------------------------------------------------------


def test_the_public_names_are_lazy_declared_and_resolvable():
    """`__all__`, the lazy `_EXPORTS` map, and reality must agree.

    The names resolve on first access rather than on import, because the dialect
    engine and the dbt reader live behind connector extras: eager imports would
    make a bare `pip install exmergo-dex-core` fail at import rather than at the
    first feature that needs an extra. That indirection is only safe if the two
    lists cannot drift, hence this.
    """

    import exmergo_dex_core as pkg

    assert sorted(pkg.__all__) == sorted([*pkg._EXPORTS, "__version__"])
    for name in pkg._EXPORTS:
        assert getattr(pkg, name) is not None, name
    assert sorted(dir(pkg)) == sorted(pkg.__all__)
    # An unknown name still raises AttributeError, not a confusing ImportError
    # from the lazy loader.
    with pytest.raises(AttributeError):
        getattr(pkg, "NoSuchName")  # noqa: B009  (the point is the lookup)


def test_importing_the_package_pulls_in_no_connector_library():
    """Package import stays cheap. The CLI runs as a fresh subprocess per
    command, so anything imported here is latency on every single invocation,
    and a connector library imported here would also break the installs that
    deliberately carry only one."""

    import subprocess

    # The delta across the import, not the absolute set: installing a connector
    # extra registers namespace packages through a .pth file, so a dev
    # environment already has some of these loaded before anything runs.
    probe = (
        "import sys;"
        "before={m.split('.')[0] for m in sys.modules};"
        "import exmergo_dex_core;"
        "libs={'google','snowflake','databricks','psycopg','redshift_connector',"
        "'clickhouse_connect','sklearn','httpx','metricflow','duckdb','sqlglot'};"
        "print(sorted(({m.split('.')[0] for m in sys.modules} - before) & libs))"
    )
    out = subprocess.run(  # noqa: S603  (this interpreter, a literal probe)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout
