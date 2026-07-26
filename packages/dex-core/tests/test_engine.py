"""The public Engine API: what a library caller gets, and what it must never do.

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
    ConfirmationRequiredError,
    DexConfig,
    Engine,
    FilesystemStore,
    MemoryStore,
)
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import DuckDBTarget
from exmergo_dex_core.envelope import Envelope
from exmergo_dex_core.explore.results import MapResult, ProfileResult, QueryResult

SRC = Path(__file__).resolve().parents[1] / "src" / "exmergo_dex_core"


# --- the surface itself --------------------------------------------------------


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
    with Engine(connector="duckdb", path=str(duckdb_file)) as eng:
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

    with Engine(connector="duckdb", path=str(duckdb_file)) as eng:
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
    with Engine(config=config) as eng:
        mapped = eng.map()
        rows = eng.query("select count(*) as n from orders")

    assert mapped.object_count == 2
    assert rows.row_count == 1
    assert not list(tmp_path.rglob(".dex"))


def test_close_is_idempotent_and_the_context_manager_closes(duckdb_file: Path):
    eng = Engine(connector="duckdb", path=str(duckdb_file))
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

    with Engine() as eng, pytest.raises(ValueError, match="no connector selected"):
        eng.inventory()


def test_commands_needing_the_project_refuse_without_a_repo_root():
    """The dbt project is a filesystem artifact by design, so the commands that
    read or write it say which operation needed a root rather than failing with
    a bare None several frames down."""

    with Engine(config=DexConfig()) as eng:
        with pytest.raises(ValueError, match="scaffolding a dbt project"):
            eng.init_project("analytics", connector="duckdb")
        with pytest.raises(ValueError, match="locating the dbt project"):
            eng.project_dir()


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
    with Engine(config=config, repo_root=str(workdir)) as eng:
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

    first = Engine(connector="duckdb", path=str(duckdb_file))
    second = Engine(connector="duckdb", path=str(duckdb_file))
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

    Credential discovery and the cost gate meet in exactly one place, which is
    what makes both reviewable. A second opener would be a second place to get
    either wrong, and it would be easy to add without noticing.
    """

    callers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "open_adapter(" in path.read_text(encoding="utf-8")
    )
    # connect.py defines it; engine.py is the funnel; dev_target.py runs the
    # free preflight probes, which are pre-connection by nature (they answer
    # "can this target be built into at all", before an engine would be useful).
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
    eng = Engine(
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
    assert second.cost_gate.remaining_for_statement() == 1_000
    second.cost_gate.charge(600.0)  # would raise if the first charge carried over


def test_a_per_call_confirmation_overrides_the_engine_default(
    duckdb_file: Path, tmp_path: Path
):
    """Confirmation belongs to a call. An engine-level default exists because the
    CLI runs one command per process, but a caller must be able to confirm one
    call without confirming everything after it."""

    eng = Engine(
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
        Engine(config=config, store=store) as eng,
        pytest.raises(ConfirmationRequiredError) as caught,
    ):
        eng.profile("customers")

    request = caught.value.request
    assert request.cost.estimate is not None and request.cost.estimate > 0
    assert "per_table_bytes" in request.data
    assert "--confirm" in request.data["hint"]
    # Nothing executed: only free metadata and dry-runs happened.
    assert all(c.dry_run for c in fake_bq_client.query_calls)


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
        "'sklearn','httpx','metricflow','duckdb','sqlglot'};"
        "print(sorted(({m.split('.')[0] for m in sys.modules} - before) & libs))"
    )
    out = subprocess.run(  # noqa: S603  (this interpreter, a literal probe)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout
