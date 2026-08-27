"""The safety spine: the five safety-critical assertion families.

A regression on any of these is a release blocker regardless of benchmark score.
The harness is wired in full now: families whose engine already exists are real
tests; families whose engine is not yet built are explicit ``xfail`` placeholders
so the spine is visible and complete in CI from day one and turns green as the
logic arrives.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import ColumnProfile, PIIFlag
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.diagram import render_er_mermaid
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.storage import FilesystemStore, MemoryStore


def _memory_engine(**kwargs) -> DexEngine:
    """An engine that touches no disk, for the units that need one to exist."""

    return DexEngine(config=DexConfig(), store=MemoryStore(), **kwargs)


def cast_arguments(sql: str) -> list[str]:
    """Every CAST argument in one generated statement.

    Matched by parenthesis balance rather than by regex: the arguments nest
    (a CASE holding a SUBSTR), so a non-greedy match would stop at the wrong
    close paren.
    """

    arguments = []
    for start in (i for i in range(len(sql)) if sql.startswith("CAST(", i)):
        depth, i = 1, start + len("CAST(")
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        arguments.append(sql[start + len("CAST(") : i - 1].rsplit(" AS ", 1)[0])
    return arguments


def assert_every_cast_is_total(sql: str) -> None:
    """#310: every CAST in a generated aggregate must be castable for every row
    of the column, not only for the rows a surrounding CASE would select.

    Redshift evaluates a CASE branch's cast for rows the WHEN never selects,
    so the shape that reads as safe (``CASE WHEN <digits> THEN CAST(col AS
    BIGINT) END``) died server-side on any table with one non-numeric string
    in a profiled column. The invariant that replaced it is structural and
    dialect-independent: the cast's argument is itself a CASE that yields
    digits on every row, so no evaluation order can reach the cast with
    something it cannot parse.
    """

    from exmergo_dex_core.adapters.base import _CAST_SENTINEL

    for argument in cast_arguments(sql):
        assert argument.startswith("CASE WHEN "), (
            f"a CAST argument is not shape-guarded, so a row that fails the "
            f"shape predicate reaches it raw: {argument}"
        )
        assert argument.endswith(f"ELSE {_CAST_SENTINEL} END"), (
            f"a CAST argument has no digit fallback, so it is NULL-or-value "
            f"rather than total: {argument}"
        )


# --- Family 1: read-only against data; SELECT-only; prod-target refused -------


def test_read_only_duckdb_refuses_writes(duckdb_file: Path):
    adapter = DuckDBAdapter(duckdb_file)
    try:
        with pytest.raises(Exception):
            adapter._conn.execute("INSERT INTO customers VALUES (3, 'c@example.com')")
    finally:
        adapter.close()


def test_generated_sql_is_select_only(duckdb_file: Path):
    # The profiling SQL the adapter generates must parse as a single read-only
    # SELECT. Built without executing, so the generator itself is what is asserted.
    from exmergo_dex_core.adapters.base import ColumnMeta
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = DuckDBAdapter(duckdb_file)
    try:
        sql, _plan = adapter._build_aggregate_sql(
            "memory.main.customers",
            [
                ColumnMeta("id", "INTEGER", True, 0),
                ColumnMeta("email", "VARCHAR", True, 1),
                ColumnMeta("signup_date", "TIMESTAMP", True, 2),
            ],
            safe={"id", "signup_date"},
            shape={"email"},
            type_req={"id", "email"},
            key_shape_req={"email"},
            temporal_req={"signup_date"},
        )
    finally:
        adapter.close()
    assert sql.lstrip().upper().startswith("SELECT")
    # Shape statistics ride the same guarded statement (regex predicates inside
    # measuring aggregates, never a raw value in the projection).
    assert "su_1" in sql and "sp_1" in sql and "st_1" in sql
    # Declared-type-vs-content statistics (#204) ride the same statement too:
    # string-eligible fractions on the VARCHAR column, epoch fractions on both.
    assert "ts_ns_1" in sql and "ts_sl1_1" in sql
    assert "ts_ep_s_0" in sql and "ts_ep_s_1" in sql
    # Heterogeneous-key-shape statistics (#205) ride the same statement too.
    assert "ks_uuid_1" in sql and "ks_hex_1" in sql
    # Temporal-continuity statistics (#206) ride the same statement too: the
    # alignment fractions and all three granularity variants for the date column.
    assert "tc_da_2" in sql and "tc_ma_2" in sql
    assert "tp_d_2" in sql and "tg_d_2" in sql
    assert "tp_m_2" in sql and "tg_m_2" in sql
    assert "tp_h_2" in sql and "tg_h_2" in sql
    assert_every_cast_is_total(sql)
    # Idempotent: passing it through the guard again must not raise.
    assert assert_select_only(sql) == sql


def test_a_shape_gated_cast_never_reaches_a_non_numeric_value(tmp_path: Path):
    """#310: the profiling aggregate must not depend on lazy CASE evaluation.

    Redshift evaluates a CASE branch's cast for rows the WHEN never selects,
    which killed `explore map` and `explore profile` on any table holding one
    ordinary varchar status column, and no offline test could catch it because
    every other dialect honors the guard. This reproduces that engine here: it
    lifts each CAST argument out of the generated statement and casts it over
    every row, which is exactly the work Redshift does eagerly. A raise is the
    bug; the fixed shape casts digits on every row and cannot raise anywhere.
    """

    import duckdb

    from exmergo_dex_core.adapters.base import ColumnMeta

    path = tmp_path / "statuses.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE orders (id INTEGER, status VARCHAR)")
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?)",
        [(1, "pending"), (2, "shipped"), (3, None), (4, "1700000000")],
    )
    conn.close()

    adapter = DuckDBAdapter(path)
    try:
        sql, _plan = adapter._build_aggregate_sql(
            "statuses.main.orders",
            [
                ColumnMeta("id", "INTEGER", True, 0),
                ColumnMeta("status", "VARCHAR", True, 1),
            ],
            safe={"id"},
            shape=set(),
            type_req={"id", "status"},
            key_shape_req={"status"},
            temporal_req=set(),
        )
        arguments = cast_arguments(sql)
        assert arguments, "the aggregate should carry the epoch/slash casts to check"
        for argument in arguments:
            # No CASE around it: every row of the column reaches the cast, the
            # way the hostile dialect does it. The interpolation is the
            # generator's own output, which is what this asserts about (S608).
            adapter._run_select(f"SELECT CAST({argument} AS BIGINT) FROM orders")  # noqa: S608
        # The statement itself still runs, and still measures: 1 of the 3
        # non-null values is epoch-shaped and in range.
        (row,) = adapter._run_select(sql)
        values = dict(zip([d[0] for d in adapter._conn.description], row, strict=True))
        assert values["ts_ep_s_1"] == 1.0  # of the epoch-shaped rows, all in range
        assert values["ts_ns_1"] == pytest.approx(1 / 3)  # of the non-null rows
    finally:
        adapter.close()


def test_type_contradiction_note_carries_no_raw_value(tmp_path: Path):
    """#204: the concrete epoch integer, and the concrete date-shaped string,
    must never reach the generated data-quality note text -- only the
    fraction, the format/unit name, and (for epoch) the *translated* calendar
    date, which is derived from an aggregate MIN/MAX, not a row value."""

    import duckdb

    from exmergo_dex_core.explore.profile import profile

    path = tmp_path / "epoch.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE events (id INTEGER, crt_ts_epoch VARCHAR)")
    conn.executemany(
        "INSERT INTO events VALUES (?, ?)",
        [(i, str(1_700_000_000 + i)) for i in range(50)],
    )
    conn.close()

    adapter = DuckDBAdapter(path)
    try:
        (dataset,) = profile(adapter, ["epoch.main.events"])
    finally:
        adapter.close()

    notes = " ".join(dataset.data_quality)
    assert "epoch" in notes.lower()
    for i in range(50):
        assert str(1_700_000_000 + i) not in notes, (
            "no concrete epoch value may appear in note text"
        )
    # The translated calendar date is what appears, not the integer.
    assert "2023-11-14" in notes


def test_heterogeneous_key_note_carries_no_raw_value(tmp_path: Path):
    """#205: neither a concrete numeric id nor a concrete hash string may
    reach the generated data-quality note text -- only fractions and a
    length-derived shape label."""

    import hashlib

    import duckdb

    from exmergo_dex_core.explore.profile import profile

    path = tmp_path / "heterogeneous_key.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (id VARCHAR PRIMARY KEY)")
    rows = [(str(1000 + i),) for i in range(90)]
    # md5-shaped test fixture data only, not a security use.
    hashes = [
        hashlib.md5(str(i).encode(), usedforsecurity=False).hexdigest()
        for i in range(10)
    ]
    rows += [(h,) for h in hashes]
    conn.executemany("INSERT INTO t VALUES (?)", rows)
    conn.close()

    adapter = DuckDBAdapter(path)
    try:
        (dataset,) = profile(adapter, ["heterogeneous_key.main.t"])
    finally:
        adapter.close()

    notes = " ".join(dataset.data_quality)
    assert "mixes value shapes" in notes
    for i in range(90):
        assert str(1000 + i) not in notes, "no concrete numeric id in note text"
    for h in hashes:
        assert h not in notes, "no concrete hash value in note text"
    # Only the fraction and the length-derived shape label appear.
    assert "90% numeric" in notes and "md5-shaped" in notes


# `dex demo` is the one verb that creates a data file, so the read-only rule has
# to hold around it in a way a reader can check rather than take on trust. Two
# tests do that: the generator is structurally sealed off from the connector, and
# the file it produces is opened read-only like any other the moment it exists.


def test_the_demo_generator_is_the_only_writable_duckdb_open():
    """No writable DuckDB connection exists in the engine outside the generator.

    Asserted over the source rather than at runtime because the claim is about
    what the code can do, not about what one path happened to do: a future
    `read_only=False` anywhere in the adapter tree is the regression, and it
    would pass every behavioral test until someone pointed dex at a warehouse
    they cared about.
    """

    import exmergo_dex_core

    package_root = Path(exmergo_dex_core.__file__).parent
    generator = package_root / "demo" / "warehouse.py"
    openers = [
        path
        for path in package_root.rglob("*.py")
        if "duckdb.connect(" in path.read_text(encoding="utf-8")
    ]
    assert generator in openers, "the generator is the one that writes"
    for path in openers:
        if path == generator:
            continue
        source = path.read_text(encoding="utf-8")
        assert "read_only=True" in source, f"{path} opens DuckDB without read_only"
        assert "read_only=False" not in source, f"{path} opens DuckDB writable"

    # And the generator imports neither the adapter nor the guards, so the
    # read-only open and the SELECT-only guard have no branch it could take.
    # Read off the import statements rather than the file text, so the prose
    # explaining the separation cannot be mistaken for a violation of it.
    import ast

    imported: set[str] = set()
    for node in ast.walk(ast.parse(generator.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("adapters" in name or "guards" in name for name in imported), (
        f"the generator must stay off the connector path: {sorted(imported)}"
    )


def test_a_generated_demo_warehouse_is_opened_read_only(tmp_path: Path):
    """The loop closed: the moment the demo file exists it is user data, and the
    adapter treats it exactly like a warehouse dex did not create."""

    pytest.importorskip("duckdb")
    from exmergo_dex_core.demo import generate_demo_warehouse

    warehouse = generate_demo_warehouse(tmp_path / "demo.duckdb")
    adapter = DuckDBAdapter(warehouse.path)
    try:
        with pytest.raises(Exception):
            adapter._conn.execute("DELETE FROM customers")
    finally:
        adapter.close()


def test_combination_probe_sql_is_select_only_in_every_dialect():
    # The composite-key probe shares one SQL builder across the adapters; the
    # statement must parse as a single read-only SELECT in each dialect.
    from exmergo_dex_core.adapters.base import distinct_combination_sql
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    sql = distinct_combination_sql(
        '"db"."main"."line_items"',
        [["order_key", "line_number"], ["order_key", "quantity"]],
        quote,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    dialects = (
        "duckdb",
        "bigquery",
        "snowflake",
        "databricks",
        "postgres",
        "redshift",
        "clickhouse",
    )
    for dialect in dialects:
        assert assert_select_only(sql, dialect=dialect) == sql


def test_select_only_guard_rejects_writes():
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "DELETE FROM customers",
        "INSERT INTO customers VALUES (3, 'c@example.com')",
        "DROP TABLE customers",
        "SELECT 1; DROP TABLE customers",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad)


def test_every_authored_sql_kind_runs_through_the_select_only_guard():
    """No edit kind carrying SQL may reach the repo without the guard.

    dex writes SQL a human reviews and dbt later runs, so the read-only
    guarantee has to hold at the point of authorship, not only at the point of
    execution. The failure mode this pins is a new kind added with its own
    validation branch that forgets the guard: the file lands, the diff reads
    fine, and a DELETE is sitting in the project waiting for someone to run it.

    An analysis is the sharpest case. dbt never runs one, so nothing downstream
    would object, and it is still refused: compiled SQL in ``target/`` is one
    copy-paste from a warehouse, and the guarantee is about what dex writes, not
    about who presses the button.
    """

    from exmergo_dex_core import transform
    from exmergo_dex_core.transform.validate import EditValidationError, validate_edit

    carriers = {
        transform.EditKind.MODEL_SQL: "models/staging/stg_x.sql",
        transform.EditKind.TEST_SQL: "tests/assert_x.sql",
        transform.EditKind.ANALYSIS_SQL: "analyses/scratch.sql",
    }
    for kind, path in carriers.items():
        for bad in ("delete from customers", "drop table customers"):
            with pytest.raises(EditValidationError, match="read-only SELECT"):
                validate_edit(transform.PlanEdit(path=path, kind=kind, new_content=bad))

    # A snapshot carries its query inside the block, and the body gets the same
    # guard as a standalone one.
    with pytest.raises(EditValidationError, match="read-only SELECT"):
        validate_edit(
            transform.PlanEdit(
                path="snapshots/snap_x.sql",
                kind=transform.EditKind.SNAPSHOT_SQL,
                new_content=(
                    "{% snapshot snap_x %}\n"
                    "{{ config(unique_key='id', strategy='timestamp', "
                    "updated_at='updated_at') }}\n"
                    "delete from customers\n"
                    "{% endsnapshot %}\n"
                ),
            )
        )


def _firewall_cache():
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache

    return DexCache(
        datasets=[
            Dataset(
                identifier="db.main.customers",
                columns=[
                    ColumnProfile(name="id", data_type="INTEGER"),
                    ColumnProfile(
                        name="email",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="email", confidence=0.9),
                    ),
                ],
            )
        ]
    )


def test_query_firewall_refuses_writes_pragmas_and_multistatement():
    # Agent-authored SQL gets a stricter gate than engine SQL: even the
    # read-only introspection roots (PRAGMA/DESCRIBE) are refused.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "DELETE FROM customers",
        "INSERT INTO customers VALUES (3, 'x')",
        "SELECT 1; DROP TABLE customers",
        "PRAGMA database_list",
        "DESCRIBE customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits())


def test_a_batch_cannot_launder_a_statement_past_the_firewall(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """`explore query` takes several statements per call, and the guard is
    per statement or it is nothing.

    Two ways a batch could weaken it, both pinned here. Riding alongside a clean
    statement must not admit a PII projection, and a semicolon-joined argument
    must still be refused as the smuggled second statement it is: arguments are
    never joined into one string, so the multi-statement refusal above keeps
    exactly the reach it had.
    """

    import json

    from exmergo_dex_core.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    base = ["--path", str(duckdb_file), "--repo-root", str(repo)]
    assert main(["explore", "map", *base]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "explore",
                "query",
                "select count(*) as n from customers",
                "select email from customers",
                "select 1 as a; drop table customers",
                *base,
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error" and payload["reason"] == "guard"
    results = payload["data"]["results"]
    assert [r["status"] for r in results] == ["ok", "refused", "refused"]
    assert "PII-flagged" in results[1]["error"]
    assert "exactly one statement" in results[2]["error"]
    # No flagged value crossed the boundary anywhere in the payload, including in
    # the neighbour that was allowed to answer.
    assert "@" not in json.dumps(payload["data"])
    env.sanitize(env.ok(payload["data"]))


def test_an_install_that_cannot_validate_sql_refuses_rather_than_degrading():
    # The dialect engine ships with the connector extras, not the base install, so
    # an install that cannot parse SQL is reachable. The only safe answer is to
    # refuse: a query dex cannot parse is a query it cannot promise is read-only,
    # so there must be no weaker fallback path (a regex screen, a warn-and-run)
    # that would let an unvalidated statement reach a warehouse. This pins the
    # refusal itself; `tests/guards/test_dialect.py` covers the mechanics.
    import sys

    from exmergo_dex_core.guards import dialect

    original = sys.modules.get("sqlglot")
    sys.modules["sqlglot"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(dialect.DialectDependencyError):
            dialect.ensure_available()
    finally:
        if original is None:
            del sys.modules["sqlglot"]
        else:
            sys.modules["sqlglot"] = original


def test_prod_target_execution_is_refused():
    from exmergo_dex_core import transform

    # The refusal fires before the cost gate and before any project resolution:
    # confirmation cannot push a build at production.
    for target in ("prod", "production", "PROD", "live"):
        with pytest.raises(transform.ProdTargetRefusedError):
            transform.build(target=target, confirmed=True)
    # A misconfigured dbt_target cannot whitelist production either.
    with pytest.raises(transform.ProdTargetRefusedError):
        transform.build(target="prod", configured_target="prod", confirmed=True)
    # Nor does an arbitrary non-dev target slip through.
    with pytest.raises(transform.ProdTargetRefusedError):
        transform.build(target="staging", confirmed=True)


# --- Family 2: cost-guard binds ----------------------------------------------


def test_cost_guard_blocks_over_ceiling():
    from exmergo_dex_core.guards import cost_guard

    # Over-ceiling blocks first, before the confirmation check, so a blown budget
    # can never be pushed through with --confirm.
    with pytest.raises(cost_guard.OverCeilingError):
        cost_guard.preflight(estimate=10_000, ceiling=10, confirmed=True)
    with pytest.raises(cost_guard.OverCeilingError):
        cost_guard.preflight(estimate=10_000, ceiling=10)


@pytest.mark.parametrize(
    "connector",
    ["bigquery", "snowflake", "databricks", "redshift", "postgres", "clickhouse"],
)
def test_row_attribution_never_spends_unasked_on_a_metered_connector(
    connector, dbt_project_dir: Path, monkeypatch
):
    """Naming a row-affecting change is free; measuring one is a scan. A metered
    connector must therefore not be touched by planning unless the caller asked,
    which is what keeps `transform plan` free of a handshake for the edits that
    cost nothing. The edit here does move rows, so a connection would be opened
    if the gate were on the wrong side."""

    from exmergo_dex_core import transform
    from exmergo_dex_core.transform.row_attribution import attribute

    model = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    model.write_text("select * from raw_customers where id > 0\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise AssertionError("planning opened a connection without --attribute-rows")

    monkeypatch.setattr(DexEngine, "_adapter", refuse)
    engine = DexEngine(
        connector=connector,
        repo_root=str(dbt_project_dir.parent),
        store=FilesystemStore(dbt_project_dir.parent),
        config=DexConfig(connector=connector),
    )
    edits = [
        transform.PlanEdit(
            path="models/staging/stg_customers.sql",
            kind=transform.EditKind.MODEL_SQL,
            new_content="select * from raw_customers where id > 5\n",
        )
    ]
    # requested=None is the default: follow the connector, and every one of these
    # bills, so none of them may be opened.
    outcome = attribute(engine, edits, requested=None)
    assert outcome.models, "the change is still named, for free"
    changes = outcome.models[0].changes
    assert changes and all(not c.attributed for c in changes)
    assert all("--attribute-rows" in (c.reason or "") for c in changes)
    assert outcome.adapter is None and outcome.pending is None


@pytest.mark.parametrize(
    "paradigm",
    [env.Paradigm.BYTES_SCANNED, env.Paradigm.COMPUTE_TIME, env.Paradigm.DB_LOAD],
)
def test_a_refusal_never_reports_a_metered_connector_as_free(paradigm, monkeypatch):
    """The guard being right is not enough: the envelope has to say so.

    Every other cost assertion in this family stops at the exception, which is
    exactly how a metered over-ceiling refusal shipped for three releases
    reporting `cost.paradigm: free_local` in the envelope beside prose that
    named the real paradigm. `free_local` is a positive claim that the connector
    bills nothing, so a host branching on the structured field to ask whether a
    refusal was about money was told no. Assert it where the consumer reads it.
    """

    import argparse

    from exmergo_dex_core import cli
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=paradigm,
        ceiling=10.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="metered",
    )

    def refuse(estimate):
        # The two refusals confirmation cannot override, rendered the way the
        # CLI renders them: through dispatch, which is the only path from a
        # raised guard error to something an agent reads.
        def raiser(args, engine):
            gate.preflight_command(estimate)
            raise AssertionError("the gate admitted an unbudgeted or over-ceiling run")

        monkeypatch.setattr(cli, "_run", raiser)
        return cli.dispatch(argparse.Namespace(), None)

    over_ceiling = refuse(10_000.0)
    assert over_ceiling.status is env.Status.ERROR
    assert over_ceiling.cost.paradigm is paradigm
    assert over_ceiling.cost.estimate == 10_000.0 and over_ceiling.cost.ceiling == 10.0

    gate.ceiling = None
    no_ceiling = refuse(1.0)
    assert no_ceiling.status is env.Status.ERROR
    assert no_ceiling.cost.paradigm is paradigm


@pytest.mark.parametrize("backend", ["filesystem", "memory"])
def test_two_concurrent_billed_commands_cannot_both_pass_one_session_ceiling(
    tmp_path, backend
):
    """The cumulative ceiling binds across commands, not only within one.

    The guard used to read the day's spend once when a gate was built and decide
    the whole command from that reading, so two commands overlapping in time were
    admitted against the same headroom. Reproduced live on BigQuery: a sequential
    pair refused as designed, the same pair issued concurrently both ran, and the
    ledger finished 15.3% over a seeded `session_ceiling`. Nothing looked wrong
    afterwards, because every entry in that ledger was true.

    Here rather than only in the cost-guard suite because a ceiling that reports
    a number which did not bind is a safety regression, not a bug: it is the
    control the published cost claim rests on.
    """

    import threading

    from exmergo_dex_core.config import Budget, DexConfig
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.guards.cost_guard import CostGuardError, utc_day_start

    store = FilesystemStore(tmp_path) if backend == "filesystem" else MemoryStore()
    config = DexConfig(
        connector="bigquery", budget=Budget(ceiling=1_000.0, session_ceiling=1_000.0)
    )
    # Every gate is built before any of them admits, which is what makes this an
    # honest test of the race rather than of thread scheduling: all four read the
    # same empty ledger, and under the old design that reading was the whole basis
    # for every decision that followed.
    gates = [
        new_cost_gate(
            "bigquery", config, store, confirmed=True, command="explore profile"
        )
        for _ in range(4)
    ]
    admitted: list[bool] = []
    ready = threading.Barrier(len(gates))

    def run(gate):
        ready.wait()
        try:
            gate.preflight_command(600.0)
        except CostGuardError:
            return
        try:
            gate.record_billed(500.0, statement="select 1")
            admitted.append(True)
        finally:
            gate.settle()

    threads = [threading.Thread(target=run, args=(g,)) for g in gates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 1
    settled = store.spend_since(
        utc_day_start(), field="billed_bytes", connector="bigquery"
    )
    assert settled <= 1_000.0


def test_every_shipped_store_can_serialize_the_spend_admission(tmp_path):
    """A ceiling dex cannot enforce has to be one dex reports it cannot enforce.

    Both shipped backends carry the lock, so the CLI and the default library path
    are safe by default rather than safe by documentation. A backend without one
    still runs, and every billed command against it says the cumulative ceiling is
    advisory there, which is the same rule the rest of the guard follows: a
    control that is narrower than it looks is worse than one that is absent.
    """

    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.storage import SpendLock

    for store in (FilesystemStore(tmp_path), MemoryStore()):
        assert isinstance(store, SpendLock), type(store).__name__

    def gate(lock):
        return CostGate(
            paradigm=env.Paradigm.BYTES_SCANNED,
            ceiling=10.0,
            session_ceiling=1_000.0,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            record=lambda entry: None,
            lock=lock,
        )

    assert gate(FilesystemStore(tmp_path).spend_lock).warnings() == []
    unguarded = gate(None).warnings()
    assert len(unguarded) == 1 and "spend lock" in unguarded[0]


def test_a_scope_flag_cannot_widen_the_committed_allowlist():
    """The source allowlist in .dex/config.yml is a committed cost boundary. A
    per-command flag scopes work inside it and can never reach outside it, on any
    connector."""

    from exmergo_dex_core import config as config_mod
    from exmergo_dex_core.connect import ScopeError, narrow_target

    for connector, field, target in (
        ("bigquery", "datasets", config_mod.BigQueryTarget(datasets=["analytics"])),
        ("databricks", "catalogs", config_mod.DatabricksTarget(catalogs=["raw"])),
        ("postgres", "schemas", config_mod.PostgresTarget(schemas=["public"])),
        (
            "clickhouse",
            "databases",
            config_mod.ClickHouseTarget(databases=["app"]),
        ),
    ):
        narrowed = narrow_target(target, connector, [getattr(target, field)[0]])
        assert getattr(narrowed, field) == getattr(target, field)
        with pytest.raises(ScopeError):
            narrow_target(target, connector, ["somewhere_else"])


def test_the_grain_estimate_prices_every_scan_the_grain_run_executes():
    """Family 2: cost is surfaced before any spend, and on this axis that rests
    entirely on one structural claim.

    ``GrainPlan`` promises the estimate and the confirmed run "price and execute
    exactly the same statements", and the only thing making that true is that both
    read the same plan. Nothing asserted it. Every list on the plan is a scan, so
    a list the run iterates and the estimator does not is a table scanned outside
    the number the operator confirmed, and nothing about it would look wrong: the
    handshake still appears, the findings still arrive, the bill is just larger
    than the quote. The plan below carries all three kinds at once so a fourth
    added to one side and not the other fails here rather than on someone's
    invoice.
    """

    from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta
    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_drift, grain_estimate

    columns = ["order_key", "line_number", "shipped_on"]

    class RecordingAdapter:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.priced: list[str] = []
            self.scanned: list[tuple[str, tuple[str, ...]]] = []

        def query_estimate(self, sql: str) -> float:
            self.priced.append(sql)
            return 1.0

        def table_metadata(self, identifier):
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name="line_items",
                row_count=1000,
                byte_size=None,
                column_count=len(columns),
            )
            return meta, [
                ColumnMeta(name=n, data_type="INTEGER", nullable=False, ordinal=i)
                for i, n in enumerate(columns)
            ]

        def exact_distinct_counts(self, identifier, cols):
            self.scanned.append((identifier, tuple(cols)))
            return dict.fromkeys(cols, 1000)

        def distinct_combination_counts(self, identifier, combinations):
            for combo in combinations:
                self.scanned.append((identifier, tuple(combo)))
            return {tuple(c): 1000 for c in combinations}

    dataset = Dataset(identifier="db.s.line_items")
    plan = GrainPlan(
        key_checks=[(dataset, ["order_key"], 1000)],
        fanout_pairs=[],
        composite_checks=[(dataset, [["order_key", "line_number"]], 1000)],
        declared_composite_checks=[(dataset, [["line_number", "shipped_on"]], 1000)],
        notes=[],
    )

    priced_adapter, run_adapter = RecordingAdapter(), RecordingAdapter()
    total, per_table = grain_estimate(priced_adapter, plan)
    grain_drift(run_adapter, plan)

    assert total > 0 and per_table == {"db.s.line_items": total}
    assert run_adapter.scanned, "the plan executed nothing, so this proves nothing"
    priced_text = "\n".join(priced_adapter.priced)
    for identifier, scanned_columns in run_adapter.scanned:
        assert identifier in per_table, (
            f"{identifier} was scanned and never priced: the operator confirmed a "
            "figure that did not include it"
        )
        for column in scanned_columns:
            assert column in priced_text, (
                f"{identifier}.{column} was scanned and no priced statement names "
                "it, so the quote covered fewer columns than the run read"
            )


# --- Family 3: PII flagged, never surfaced -----------------------------------


def test_pii_flag_cannot_carry_an_example_value():
    # Structural guarantee: the flag type has no field for a sample value, so PII
    # can be recorded as (column, category, confidence) but never surfaced.
    assert set(PIIFlag.model_fields) == {"category", "confidence"}
    assert "value" not in ColumnProfile.model_fields


def test_pii_flag_lives_on_the_column_profile():
    col = ColumnProfile(
        name="email", data_type="VARCHAR", pii=PIIFlag(category="email", confidence=0.9)
    )
    assert col.pii is not None and col.pii.category.value == "email"


def test_query_firewall_enforces_pii_flagged_never_surfaced():
    # The flag is not just metadata: any expression that would carry a flagged
    # column's values into the projection is refused, including through
    # aggregates that return values (MIN) and through CTE laundering.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT email FROM customers",
        "SELECT MIN(email) FROM customers",
        "SELECT * FROM customers",
        "WITH x AS (SELECT email AS e FROM customers) SELECT e FROM x",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits())
    # Measuring the flagged column is fine: a statistic is not a value.
    inspect_query("SELECT COUNT(DISTINCT email) FROM customers", cache, QueryLimits())


def test_query_firewall_unnest_reshapes_but_never_smuggles():
    # Every dialect's native FROM-clause unnest idiom is admitted over a clear
    # column, refused when a subquery hides inside, and refused when it would
    # project a flagged column's values: the reshape inherits the taint.
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="db.main.events",
                columns=[
                    ColumnProfile(name="id", data_type="INTEGER"),
                    ColumnProfile(name="doc", data_type="JSON"),
                    ColumnProfile(
                        name="payload",
                        data_type="JSON",
                        pii=PIIFlag(category="email", confidence=0.9),
                    ),
                ],
            )
        ]
    )
    idioms = {
        "bigquery": "FROM events, UNNEST(JSON_KEYS({col})) AS k",
        "snowflake": "FROM events, LATERAL FLATTEN(input => {col}) AS k(k)",
        "databricks": (
            "FROM events LATERAL VIEW EXPLODE(json_object_keys({col})) x AS k"
        ),
        "postgres": "FROM events, jsonb_object_keys({col}) AS k",
        "redshift": "FROM events e, UNPIVOT e.{col} AS v AT k",
        "duckdb": "FROM events, UNNEST(json_keys({col})) AS u(k)",
        # ClickHouse has no lateral join: ARRAY JOIN is the expansion, and it
        # is a Join node rather than a FROM source, so the taint rule reaches
        # it by a different path than every other dialect here.
        "clickhouse": "FROM events ARRAY JOIN JSONExtractKeysAndValuesRaw({col}) AS k",
    }
    for dialect, idiom in idioms.items():
        allowed = "SELECT k " + idiom.format(col="doc")
        inspect_query(allowed, cache, QueryLimits(), dialect=dialect)
        smuggle = "SELECT k " + idiom.format(col="(SELECT doc FROM events)")  # noqa: S608
        with pytest.raises(QueryRefusedError):
            inspect_query(smuggle, cache, QueryLimits(), dialect=dialect)
        flagged = "SELECT k " + idiom.format(col="payload")
        with pytest.raises(QueryRefusedError):
            inspect_query(flagged, cache, QueryLimits(), dialect=dialect)


def test_firewall_block_threshold_is_a_hard_coded_engine_constant():
    # The threshold is engine policy, not configuration: a config edit must
    # never be able to widen the PII boundary. Its value is load-bearing (every
    # base confidence in the detector sits at or above it, so nothing unblocks
    # without value-shape evidence), so the number itself is pinned here.
    #
    # It is asserted at the guards package, which is where the policy lives, and
    # the firewall reads it from there. That indirection is what keeps the PII
    # gate on a SQL-free surface (the hosted semantic layer screens dimension
    # names) from dragging in a SQL parser: every gate still blocks at one number,
    # but only the surfaces that actually parse SQL depend on the parser.
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.guards import PII_BLOCK_CONFIDENCE

    assert PII_BLOCK_CONFIDENCE == 0.5
    assert not any("threshold" in name for name in DexConfig.model_fields)

    # One threshold, not a copy per surface: the firewall and the semantic PII
    # gate must read the same number, or they can drift apart silently.
    from exmergo_dex_core.explore import semantic
    from exmergo_dex_core.guards import query_firewall

    assert query_firewall.PII_BLOCK_CONFIDENCE == PII_BLOCK_CONFIDENCE
    assert semantic.PII_BLOCK_CONFIDENCE == PII_BLOCK_CONFIDENCE


def test_firewall_threshold_boundary_and_warning_carry_no_values():
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    def cache_at(confidence: float) -> DexCache:
        return DexCache(
            datasets=[
                Dataset(
                    identifier="db.main.region",
                    columns=[
                        ColumnProfile(
                            name="r_name",
                            data_type="VARCHAR",
                            pii=PIIFlag(category="name", confidence=confidence),
                        ),
                    ],
                )
            ]
        )

    with pytest.raises(QueryRefusedError):
        inspect_query("SELECT r_name FROM region", cache_at(0.5), QueryLimits())

    inspected = inspect_query(
        "SELECT r_name FROM region", cache_at(0.49), QueryLimits()
    )
    (warning,) = inspected.warnings
    # The warning is built from the column name, category, and numbers only:
    # nothing shaped like a cell value can appear in it by construction.
    assert "region.r_name" in warning and "(name)" in warning
    assert "AFRICA" not in warning and "@" not in warning


def test_pii_refusal_s_suggested_override_carries_no_value():
    # Issue #217: the refusal now names the exact pii_overrides entry that
    # would clear the column. That entry is built from the dataset identifier,
    # the column name, and the category only, the same structural guarantee
    # PIIFlag itself makes (no value field exists anywhere to leak one).
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="db.main.region",
                columns=[
                    ColumnProfile(
                        name="r_name",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="name", confidence=0.9),
                    ),
                ],
            )
        ]
    )
    with pytest.raises(QueryRefusedError) as excinfo:
        inspect_query("SELECT r_name FROM region", cache, QueryLimits())
    message = str(excinfo.value)
    assert "column: db.main.region.r_name" in message
    assert "AFRICA" not in message and "@" not in message


def test_a_seed_s_pii_refusal_fires_before_any_diff_is_built(dbt_project_dir: Path):
    """A seed is the first edit kind that puts **values** into a reviewable diff,
    and a diff goes into git and stays there.

    Two things have to hold, and only one of them is the refusal. The other is
    its position: ``plan`` validates every edit before it builds a single diff,
    because the envelope sanitizer walks ``data`` and never ``diffs``. If the
    order were the other way round, a refused seed's values would already be in
    the diff list, and from there in the transcript. So this asserts the
    ordering, not just the outcome: nothing was stored, nothing was written, and
    the message that comes back carries the column name and never a value.
    """

    from exmergo_dex_core import transform
    from exmergo_dex_core.transform.validate import EditValidationError

    store = FilesystemStore(dbt_project_dir.parent)
    a_person = "someone@example.com"
    with pytest.raises(EditValidationError) as excinfo:
        transform.plan(
            "a lookup built from customer data",
            [
                transform.PlanEdit(
                    path="seeds/contacts.csv",
                    kind=transform.EditKind.SEED_CSV,
                    new_content=f"id,email\n1,{a_person}\n",
                )
            ],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=store,
        )
    message = str(excinfo.value)
    assert "'email' looks like email" in message
    assert a_person not in message
    # Nothing was stored (the plan never got made) and nothing was written.
    assert store.list_plans() == []
    assert not (dbt_project_dir / "seeds" / "contacts.csv").exists()


def test_the_reference_index_reads_a_seed_s_header_and_never_its_rows(
    dbt_project_dir: Path,
):
    """A seed is project data, and the index walks every file in the project.

    The header names columns, which is what a reference index is for. The rows
    below it are values, and values never enter agent context. This is the one
    place in the read path where "scan every file" meets "a seed holds data", so
    the boundary is asserted here rather than left to the scanner's good manners.
    """

    from exmergo_dex_core.dbt_project import load
    from exmergo_dex_core.references import ReferenceIndex

    (dbt_project_dir / "seeds").mkdir(parents=True, exist_ok=True)
    a_person = "someone@example.com"
    (dbt_project_dir / "seeds" / "contacts.csv").write_text(
        f"id,email\n1,{a_person}\n2,other@example.com\n", encoding="utf-8"
    )
    index = ReferenceIndex(load(dbt_project_dir))

    header, _limits = index.references_to("email", "column")
    assert ("seeds/contacts.csv", "seed_header") in {
        (hit.path, hit.form) for hit in header
    }
    indexed = {
        reference.name
        for references in index._by_name.values()
        for reference in references
    }
    assert a_person not in indexed
    assert not any(name and "@example.com" in name for name in indexed)


def test_the_reference_index_reports_names_and_never_a_column_value(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Every occurrence carries a path, a line and a form, and no content.

    The command answers "where", so a line number is the whole payload it owes.
    Echoing the matching source line back would be a small convenience and a
    standing leak, because the line that names a column is routinely the line
    that also carries a literal.
    """

    from exmergo_dex_core.cli import main

    (dbt_project_dir / "models" / "staging" / "stg_secrets.sql").write_text(
        "select 'hunter2' as token, id from {{ ref('stg_customers') }}\n",
        encoding="utf-8",
    )
    assert main(["--repo-root", str(tmp_path), "transform", "references", "id"]) == 0
    payload = capsys.readouterr().out
    assert "hunter2" not in payload
    occurrences = [
        occurrence
        for target in json.loads(payload)["data"]["targets"]
        for file_entry in target["files"]
        for occurrence in file_entry["occurrences"]
    ]
    assert occurrences
    assert all(
        set(occurrence) <= {"line", "form", "resolution", "note"}
        for occurrence in occurrences
    )


def test_propagation_writes_nothing_to_the_project_before_apply(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Propose-don't-impose, on the one path where dex authors the content itself.

    `transform rename` is the first verb where the engine writes the SQL rather
    than validating what an agent wrote, so the guarantee that nothing reaches the
    project until `apply` is asserted here rather than inherited from the plan
    store's good behaviour.
    """

    from exmergo_dex_core.cli import main

    model = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    schema = dbt_project_dir / "models" / "staging" / "schema.yml"
    before = (model.read_text(), schema.read_text())

    assert (
        main(
            [
                "--repo-root",
                str(tmp_path),
                "transform",
                "rename",
                "column",
                "stg_customers.id",
                "customer_id",
            ]
        )
        == 0
    )
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["status"] == "ok"
    assert envelope["data"]["plan_id"]
    assert envelope["diffs"]
    assert (model.read_text(), schema.read_text()) == before


def test_propagation_never_proposes_an_edit_inside_an_installed_package(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """dex reads packages and does not write them.

    The reference index scans `dbt_packages/` on purpose, because a package that
    ships a model the project shadows is a reference on both sides. That makes an
    installed package reachable from the same walk that builds edits, so the write
    boundary is asserted rather than assumed.
    """

    from exmergo_dex_core.cli import main

    package = dbt_project_dir / "dbt_packages" / "utils"
    (package / "models").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        'name: utils\nversion: "1.0.0"\nprofile: utils\n', encoding="utf-8"
    )
    (package / "models" / "packaged.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    packaged_before = (package / "models" / "packaged.sql").read_text()

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "rename",
            "column",
            "stg_customers.id",
            "customer_id",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert not any(
        path.startswith("dbt_packages/") for path in envelope["data"]["paths"]
    )
    assert (package / "models" / "packaged.sql").read_text() == packaged_before


def test_propagation_refuses_rather_than_partially_applying(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The rule the whole propagation surface rests on.

    `transform references` may answer "here is what I found, and here is why I
    might be missing something", because a person reads that and compensates. A
    generated plan cannot, because it will be applied. So a reference dex could
    not resolve refuses the plan outright, and nothing is stored.

    Deliberately stricter than the delete guard, which warns on the same input:
    a dangling dynamic ref left by a delete is unsatisfiable, and one in a
    rename's path is fixable by hand.
    """

    from exmergo_dex_core.cli import main

    (dbt_project_dir / "models" / "staging" / "stg_dynamic.sql").write_text(
        "select * from {{ ref(var('which')) }}\n", encoding="utf-8"
    )

    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "rename",
            "model",
            "stg_customers",
            "stg_people",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert rc != 0
    assert envelope["status"] == "error"
    assert "stg_dynamic.sql:1" in json.dumps(envelope["errors"])
    assert FilesystemStore(tmp_path).list_plans() == []
    assert (dbt_project_dir / "models" / "staging" / "stg_customers.sql").exists()


def test_propagation_and_placement_open_no_connection(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Both verbs are repo-only, whatever the repo is configured to bill against.

    They rewrite files and read a `ref()` graph, and neither needs a warehouse.
    A connector opened here would price nothing and still authenticate, which is
    the quiet version of the failure the cost handshake exists to make loud.
    """

    from exmergo_dex_core.cli import main
    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    monkeypatch.setattr(
        DexEngine,
        "_adapter",
        lambda *a, **k: pytest.fail("a propagation command opened a connection"),
    )
    for connector in ("bigquery", "snowflake", "databricks", "duckdb"):
        save_config(DexConfig(connector=connector), tmp_path)
        assert (
            main(
                [
                    "--repo-root",
                    str(tmp_path),
                    "transform",
                    "rename",
                    "column",
                    "stg_customers.id",
                    f"id_{connector}",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_the_reference_index_opens_no_connection_on_any_connector(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Repo-only means repo-only, whatever the repo is configured to bill against.

    The command is advertised as free on every connector. A connector that got
    opened here would price nothing and still authenticate, which is the quiet
    version of the failure the cost handshake exists to make loud.
    """

    from exmergo_dex_core.cli import main
    from exmergo_dex_core.config import DexConfig, save_config
    from exmergo_dex_core.engine import DexEngine

    monkeypatch.setattr(
        DexEngine,
        "_adapter",
        lambda *a, **k: pytest.fail("`transform references` opened a connection"),
    )
    for connector in ("bigquery", "snowflake", "databricks", "redshift", "duckdb"):
        save_config(DexConfig(connector=connector), tmp_path)
        assert (
            main(["--repo-root", str(tmp_path), "transform", "references", "id"]) == 0
        )
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == "ok"
        assert envelope["cost"]["estimate"] is None


def test_a_refused_seed_never_reaches_a_dbt_subprocess(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    """The PII gate runs before the parse, not after it.

    `transform plan` hands snapshots, seeds and the config kinds to dbt's own
    parser before it stores anything, and that parser reads a *copy of the
    project with the edit written into it*. Parsing first would put a refused
    seed's values on disk and through a subprocess before the gate ever fired,
    which is the same failure the profiles secret-guard is ordered against.

    Found by dogfooding, not by the suite: every unit test called the validator
    directly and so could not see what the command layer did first.
    """

    import importlib
    import json as _json

    from exmergo_dex_core.cli import main

    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    def refuse_to_run(*args, **kwargs):
        raise AssertionError("dbt was handed the seed before it was refused")

    monkeypatch.setattr(build_mod, "shadow_parse", refuse_to_run)

    payload = tmp_path / "edits.json"
    payload.write_text(
        _json.dumps(
            {
                "edits": [
                    {
                        "path": "seeds/contacts.csv",
                        "kind": "seed_csv",
                        "content": "id,email\n1,someone@example.com\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "a lookup built from customer data",
            "--edits-file",
            str(payload),
        ]
    )
    envelope = _json.loads(capsys.readouterr().out)
    assert rc != 0
    assert "'email' looks like email" in envelope["errors"][0]
    assert "someone@example.com" not in _json.dumps(envelope)


def test_a_seed_over_the_size_cap_refuses_rather_than_writes(dbt_project_dir: Path):
    # The other half of "a seed is data entering git": a multi-megabyte CSV is
    # unreadable in review, which is the one thing a reviewable diff is for.
    from exmergo_dex_core import transform
    from exmergo_dex_core.transform.validate import MAX_SEED_ROWS, EditValidationError

    store = FilesystemStore(dbt_project_dir.parent)
    with pytest.raises(EditValidationError, match="row cap"):
        transform.plan(
            "far too much data",
            [
                transform.PlanEdit(
                    path="seeds/big.csv",
                    kind=transform.EditKind.SEED_CSV,
                    new_content="code\n" + "x\n" * (MAX_SEED_ROWS + 1),
                )
            ],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=store,
        )
    assert store.list_plans() == []
    assert not (dbt_project_dir / "seeds" / "big.csv").exists()


def test_pii_override_is_config_only_and_survives_reprofiling(tmp_path: Path):
    # A hand-edit to the cache is overwritten by the next profile; only the
    # committed config entry durably clears a reviewed column, and the clear is
    # recorded on the profile as an audit trail.
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.explore.profile import profile

    path = tmp_path / "override.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE region (r_name VARCHAR)")
    conn.execute("INSERT INTO region VALUES ('AFRICA')")
    conn.close()

    adapter = DuckDBAdapter(path)
    try:
        (without,) = profile(adapter, ["override.main.region"])
        (with_override,) = profile(
            adapter,
            ["override.main.region"],
            pii_overrides={"override.main.region.r_name"},
        )
    finally:
        adapter.close()

    assert without.columns[0].pii is not None, "no override: the flag stands"
    assert with_override.columns[0].pii is None
    assert with_override.columns[0].pii_overridden is not None, "the audit trail"


def test_an_on_demand_profile_is_a_real_profile_not_a_shortcut(tmp_path: Path):
    """Profiling on demand must not become a hole in the PII policy.

    The firewall may proceed on an object nobody profiled only because dex
    profiles it first, so the whole guarantee rests on that profile being the one
    a deliberate `explore profile` would have written. Two things are asserted:
    the flags block exactly as they do on a hand-profiled cache, and the cached
    result is indistinguishable from the deliberate one for the columns and flags
    the guard reads.
    """

    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.guards.query_firewall import QueryRefusedError
    from exmergo_dex_core.storage import FilesystemStore

    path = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE people (id INTEGER, email VARCHAR)")
    conn.execute("INSERT INTO people VALUES (1, 'a@example.com')")
    conn.close()

    implicit_root = tmp_path / "implicit"
    implicit_root.mkdir()
    config = DexConfig(connector="duckdb")
    with DexEngine(
        connector="duckdb",
        path=str(path),
        config=config,
        store=FilesystemStore(implicit_root),
    ) as engine:
        # Never mapped, never profiled: the object is reached for the first time
        # by a query, and the flag it has never seen still blocks.
        with pytest.raises(QueryRefusedError):
            engine.query("SELECT email FROM people")
        engine.query("SELECT COUNT(*) AS n FROM people")

    deliberate_root = tmp_path / "deliberate"
    deliberate_root.mkdir()
    with DexEngine(
        connector="duckdb",
        path=str(path),
        config=config,
        store=FilesystemStore(deliberate_root),
    ) as engine:
        engine.profile("wh.main.people")

    def flags(root: Path):
        cache = FilesystemStore(root).load_cache()
        (dataset,) = [d for d in cache.datasets if d.identifier == "wh.main.people"]
        return [
            (c.name, c.data_type, c.pii.category if c.pii else None)
            for c in dataset.columns
        ]

    assert flags(implicit_root) == flags(deliberate_root)
    assert any(category is not None for _n, _t, category in flags(implicit_root))


def test_an_implicit_profile_cannot_be_reached_unconfirmed_or_over_ceiling(
    fake_bq_client, monkeypatch, tmp_path: Path
):
    """The scan dex runs on the caller's behalf is still a scan, so the whole
    cost lifecycle binds on it: an unconfirmed run executes nothing, an
    over-ceiling estimate refuses and confirmation cannot override it, and the
    quoted number covers the profile as well as the query rather than the query
    alone."""

    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.guards.cost_guard import (
        ConfirmationRequiredError,
        OverCeilingError,
    )
    from exmergo_dex_core.storage import FilesystemStore

    sql = "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`"

    def run(*, ceiling, confirmed):
        monkeypatch.setattr(
            DexEngine,
            "_adapter",
            lambda self, command=None, **kw: _bq_adapter(
                fake_bq_client, ceiling=ceiling, confirmed=confirmed
            ),
        )
        engine = DexEngine(
            connector="bigquery",
            repo_root=str(tmp_path),
            store=FilesystemStore(tmp_path),
            config=DexConfig(connector="bigquery"),
        )
        return engine.query(sql)

    # Nothing is cached, so answering this means profiling first. Unconfirmed,
    # that ask is raised before anything executes, and the number it carries
    # covers the profile as well as the query.
    with pytest.raises(ConfirmationRequiredError) as caught:
        run(ceiling=None, confirmed=False)
    assert caught.value.cost.estimate > 10 * 1024 * 1024
    assert all(call.dry_run for call in fake_bq_client.query_calls)

    # And confirmation cannot buy through a ceiling the combined work exceeds.
    with pytest.raises(OverCeilingError):
        run(ceiling=1_000, confirmed=True)
    assert all(call.dry_run for call in fake_bq_client.query_calls)


def test_the_er_diagram_marks_pii_and_carries_no_column_value():
    """A rendered diagram is the most shareable artifact dex produces: it gets
    pasted into issues, committed, and dropped into chat, where none of the
    envelope's context travels with it. So the rule that holds for the envelope
    has to hold here in its strictest form. The cache retains min/max for every
    safe column, and the renderer must never reach for them; the PII flag is
    (category, confidence) and IS drawn, because flagged-not-hidden is the
    posture and a diagram that quietly omitted a sensitive column would be
    hiding rather than flagging.
    """

    from exmergo_dex_core.cache import (
        Dataset,
        DexCache,
        PIICategory,
        Relationship,
    )
    from exmergo_dex_core.explore.diagram import render_er_mermaid

    customers = Dataset(
        identifier="shop.main.customers",
        columns=[
            ColumnProfile(
                name="customer_id",
                data_type="INTEGER",
                is_unique=True,
                min_value=1,
                max_value=987654,
            ),
            ColumnProfile(
                name="email",
                data_type="VARCHAR",
                min_value="aaron@example.com",
                max_value="zoe@example.com",
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
            ),
            ColumnProfile(
                name="ssn",
                data_type="VARCHAR",
                pii=PIIFlag(category=PIICategory.GOVERNMENT_ID, confidence=0.9),
            ),
        ],
        candidate_keys=[["customer_id"]],
        grain=["customer_id"],
    )
    orders = Dataset(
        identifier="shop.main.orders",
        columns=[
            ColumnProfile(name="order_id", data_type="INTEGER", is_unique=True),
            ColumnProfile(name="customer_id", data_type="INTEGER"),
        ],
    )
    cache = DexCache(
        datasets=[customers, orders],
        relationships=[
            Relationship(
                from_dataset="shop.main.orders",
                from_columns=["customer_id"],
                to_dataset="shop.main.customers",
                to_columns=["customer_id"],
            )
        ],
    )

    for mermaid in (
        render_er_mermaid(cache).mermaid,
        render_er_mermaid(cache, full=True).mermaid,
    ):
        for value in ("987654", "aaron@example.com", "zoe@example.com"):
            assert value not in mermaid, "a column value reached the diagram"
        assert "pii:email 0.95" in mermaid
        assert "pii:government_id 0.90" in mermaid


def test_the_map_payload_marks_pii_and_carries_no_column_value():
    """`explore map` returns findings rather than a receipt (issue #202), which
    puts profile content into the envelope for the first time. The rule the
    diagram obeys has to hold here for the same reason and in the same strict
    form: the cache retains min/max and a value domain for every column that
    earned one, and this payload must never reach for them. The PII flag IS
    reported, because flagged-not-hidden is the posture, and it is (category,
    confidence) as it is everywhere.
    """

    import json as _json

    from exmergo_dex_core.cache import (
        Dataset,
        DexCache,
        PIICategory,
        Relationship,
        ValueCount,
        ValueDomain,
    )
    from exmergo_dex_core.explore.summary import summarize_map

    customers = Dataset(
        identifier="shop.main.customers",
        columns=[
            ColumnProfile(
                name="customer_id",
                data_type="INTEGER",
                is_unique=True,
                min_value=1,
                max_value=987654,
            ),
            ColumnProfile(
                name="email",
                data_type="VARCHAR",
                min_value="aaron@example.com",
                max_value="zoe@example.com",
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
            ),
            ColumnProfile(
                name="tier",
                data_type="VARCHAR",
                value_domain=ValueDomain(
                    values=[ValueCount(value="platinum", count=3)]
                ),
            ),
        ],
        candidate_keys=[["customer_id"]],
        grain=["customer_id"],
    )
    orders = Dataset(
        identifier="shop.main.orders",
        columns=[
            ColumnProfile(name="order_id", data_type="INTEGER", is_unique=True),
            ColumnProfile(name="customer_id", data_type="INTEGER"),
        ],
    )
    cache = DexCache(
        datasets=[customers, orders],
        relationships=[
            Relationship(
                from_dataset="shop.main.orders",
                from_columns=["customer_id"],
                to_dataset="shop.main.customers",
                to_columns=["customer_id"],
            )
        ],
    )

    for detail in (False, True):
        view = summarize_map(cache, detail=detail)
        blob = _json.dumps(
            [o.model_dump(mode="json") for o in view.objects], sort_keys=True
        )
        for value in ("987654", "aaron@example.com", "zoe@example.com", "platinum"):
            assert value not in blob, "a column value reached the map payload"
        for key in ("min_value", "max_value", "value_domain"):
            assert key not in blob
        flagged = next(c for o in view.objects for c in o.columns if c.name == "email")
        assert flagged.pii is not None
        assert set(flagged.pii.model_dump()) == {"category", "confidence"}


# --- Family 4: propose-don't-impose ------------------------------------------


def test_changes_are_diffs_not_silent_writes(dbt_project_dir: Path):
    from exmergo_dex_core import transform

    new_model = dbt_project_dir / "models" / "staging" / "stg_new.sql"
    edits = [
        transform.PlanEdit(
            path="models/staging/stg_new.sql",
            kind=transform.EditKind.MODEL_SQL,
            new_content="select 1 as id\n",
        )
    ]
    _plan, diffs, _warnings = transform.plan(
        "add stg_new",
        edits,
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    # Planning returns reviewable diffs and touches nothing in the project.
    assert diffs and diffs[0]["unified"]
    assert not new_model.exists()


def _attribution_repo(dbt_project_dir: Path, duckdb_file: Path, model_sql: str) -> Path:
    """A project whose one model reads a real warehouse table, cache and all.

    Row-population attribution is the only plan-time check that reaches the
    warehouse, so the spine exercises it against the real engine rather than a
    stand-in: a real cache, the real firewall, the real adapter.
    """

    repo = dbt_project_dir.parent
    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: customers\n      - name: orders\n",
        encoding="utf-8",
    )
    (dbt_project_dir / "models" / "staging" / "stg_orders.sql").write_text(
        model_sql, encoding="utf-8"
    )
    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        repo_root=str(repo),
        store=FilesystemStore(repo),
        config=DexConfig(connector="duckdb"),
    ) as engine:
        engine.map(full=True)
    return repo


_PRIOR_STG_ORDERS = "select * from {{ source('raw', 'customers') }}\nwhere id > 0\n"


def _attribute(repo: Path, duckdb_file: Path, authored: str):
    from exmergo_dex_core import transform

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        repo_root=str(repo),
        store=FilesystemStore(repo),
        config=DexConfig(connector="duckdb"),
    ) as engine:
        return engine.plan(
            "edit the filter",
            edits=[
                transform.PlanEdit(
                    path="models/staging/stg_orders.sql",
                    kind=transform.EditKind.MODEL_SQL,
                    new_content=authored,
                )
            ],
        )


def test_row_attribution_measures_and_still_writes_nothing(
    dbt_project_dir: Path, duckdb_file: Path
):
    """Attribution runs statements against the warehouse. It must remain a
    reader: the plan is still a proposal and the project is still untouched."""

    repo = _attribution_repo(dbt_project_dir, duckdb_file, _PRIOR_STG_ORDERS)
    model = dbt_project_dir / "models" / "staging" / "stg_orders.sql"
    before = model.read_text()

    result = _attribute(
        repo,
        duckdb_file,
        "select * from {{ source('raw', 'customers') }}\nwhere id > 1\n",
    )
    measured = [c for c in result.row_attribution[0]["changes"] if c["attributed"]]
    assert measured and all(c["delta"] is not None for c in measured)
    # Propose-don't-impose survives a check that reaches the warehouse.
    assert model.read_text() == before
    assert result.diffs and result.diffs[0]["unified"]


def test_row_attribution_over_a_pii_column_carries_no_value(
    dbt_project_dir: Path, duckdb_file: Path
):
    """A filter on a PII-flagged column is still attributable, because the
    statement projects COUNT(*) and nothing else. The count is a statistic; the
    values it counts never leave the engine."""

    repo = _attribution_repo(dbt_project_dir, duckdb_file, _PRIOR_STG_ORDERS)
    result = _attribute(
        repo,
        duckdb_file,
        "select * from {{ source('raw', 'customers') }}\nwhere email like 'a%'\n",
    )
    changes = result.row_attribution[0]["changes"]
    assert any(c["attributed"] and c["delta"] is not None for c in changes)
    # The PII column is named as SQL, never as data: no address appears anywhere.
    payload = to_envelope(result).model_dump(mode="json")
    assert "email" in str(payload), "the predicate itself is reported"
    assert "@example.com" not in str(payload)


def test_every_statement_row_attribution_issues_is_a_bare_count(
    dbt_project_dir: Path, duckdb_file: Path, monkeypatch
):
    """The generated SQL is SELECT-only and projects one aggregate. This is the
    property that lets attribution reach the warehouse at all."""

    from exmergo_dex_core.adapters import duckdb as duckdb_adapter
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    issued: list[str] = []
    original = duckdb_adapter.DuckDBAdapter.run_query

    def recording(self, sql, **kwargs):
        issued.append(sql)
        return original(self, sql, **kwargs)

    monkeypatch.setattr(duckdb_adapter.DuckDBAdapter, "run_query", recording)

    repo = _attribution_repo(dbt_project_dir, duckdb_file, _PRIOR_STG_ORDERS)
    issued.clear()
    _attribute(
        repo,
        duckdb_file,
        "select * from {{ source('raw', 'customers') }}\nwhere id > 1\n",
    )

    assert issued, "the attribution ran statements"
    for sql in issued:
        assert_select_only(sql, dialect="duckdb")
        upper = sql.upper()
        assert "COUNT(*)" in upper
        assert not any(
            forbidden in upper for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE")
        )


def _reconcile_fixtures(dbt_project_dir: Path) -> tuple[FilesystemStore, str]:
    """A repo primed so `maintain reconcile` would propose a mechanical edit.

    A dex-scaffolded staging model, a profiled baseline of the table behind it,
    and a `column_added` finding against that table: the three conditions the
    mechanical path requires. Everything below turns on whether the project
    format is asked before that path is taken.
    """

    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.drift import AxisResult, DriftFinding, DriftReport
    from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

    staging = dbt_project_dir / "models" / "staging"
    (staging / "stg_orders.sql").write_text(
        "select id, customer_id from {{ source('main', 'orders') }}\n", encoding="utf-8"
    )
    (staging / "stg_orders.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_orders\n    columns:\n      - name: id\n",
        encoding="utf-8",
    )

    created_at = datetime.now(UTC).isoformat()
    store = FilesystemStore(dbt_project_dir.parent)
    store.save_snapshot(
        Snapshot(
            created_at=created_at,
            connector="duckdb",
            warehouse=WarehouseBaseline(
                datasets=[
                    Dataset(
                        identifier="warehouse.main.orders",
                        row_count=3,
                        columns=[
                            ColumnProfile(name="id", data_type="INTEGER"),
                            ColumnProfile(name="customer_id", data_type="INTEGER"),
                        ],
                        profiled_at=created_at,
                    )
                ]
            ),
        )
    )
    store.save_drift(
        DriftReport(
            connector="duckdb",
            snapshot_created_at=created_at,
            axes={
                "schema": AxisResult(
                    run_at=created_at,
                    findings=[
                        DriftFinding(
                            axis="schema",
                            code="column_added",
                            identifier="warehouse.main.orders",
                            column="discount",
                            detail="a new column appeared on orders",
                            data={"data_type": "DOUBLE"},
                        )
                    ],
                )
            },
        )
    )
    return store, created_at


class _NotEditableProject:
    """Tier 2: a format reduced from a running graph, declining the write tier."""

    name = "graph"

    def definitions(self):
        from exmergo_dex_core.dbt_project import ProjectDefinitions

        return ProjectDefinitions(present=True)

    def transform_layer(self):
        from exmergo_dex_core.maintain.snapshot import TransformLayer

        return TransformLayer()

    def semantic_layer(self):
        from exmergo_dex_core.maintain.snapshot import SemanticLayer

        return SemanticLayer()


def test_a_format_that_declines_the_write_tier_gets_no_mechanical_edit(
    dbt_project_dir: Path,
):
    """Propose-don't-impose, made structural rather than coincidental.

    dex must not author an edit into a project whose format says it cannot
    receive one. A reduction of a running graph is the case: its source of truth
    is the code that produced the graph, so writing into the reduction edits an
    artifact regenerated on the next run, and the human reviewing that diff is
    reviewing something that will be overwritten.

    Until the write tier was asked, this held only by accident. Both mechanical
    paths key on the `models/staging/stg_<table>.*` scaffold convention and fail
    closed, so a generated tree was safe exactly as long as its own directory
    naming happened not to collide, and a format whose layers used that
    vocabulary would have been written into. The consumer who built the second
    format pinned that invariant with a test in their own repository, which is
    the wrong side of the boundary for an invariant this one owes.

    Paired deliberately: the fixtures are identical and the dbt format takes the
    mechanical path through them, so a regression that broke reconcile outright
    would fail the first half rather than pass the second by doing nothing.
    """

    store, _ = _reconcile_fixtures(dbt_project_dir)
    config = DexConfig(dbt_project_dir="analytics")

    editable = DexEngine(
        config=config, store=store, repo_root=str(dbt_project_dir.parent)
    ).reconcile()

    assert [p.kind for p in editable.proposals] == ["mechanical"], (
        "the non-degraded path is not reachable, so the degraded assertion below "
        "would pass for the wrong reason"
    )
    assert editable.plan_id is not None and editable.diffs

    declined = DexEngine(
        config=config,
        store=store,
        repo_root=str(dbt_project_dir.parent),
        project_format=_NotEditableProject(),
    ).reconcile()

    assert declined.proposals, "the findings must still be surfaced, only advisory"
    assert {p.kind for p in declined.proposals} == {"advisory"}
    assert declined.plan_id is None
    assert not declined.diffs
    assert store.latest_plan() is None or store.latest_plan().plan_id == (
        editable.plan_id
    ), "a format that declines the write tier stored a plan"
    assert any("does not implement the write tier" in w for w in declined.warnings)


def test_profiles_edit_never_carries_a_credential_into_a_diff(dbt_project_dir: Path):
    # profiles.yml is an editable surface, but a credential must never reach the
    # plan diff (and thus agent context). An inlined literal is refused whether
    # it is in the proposed content or already on disk (the diff's removed side).
    from exmergo_dex_core import transform

    (dbt_project_dir / "profiles.yml").write_text(
        "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n"
        "      host: localhost\n      user: u\n      password: s3cr3t-on-disk\n"
        "      dbname: d\n      schema: public\n",
        encoding="utf-8",
    )
    proposed_secret = transform.PlanEdit(
        path="profiles.yml",
        kind=transform.EditKind.PROFILES_YML,
        new_content=(
            "dex_test:\n  outputs:\n    dev:\n      type: postgres\n"
            "      password: s3cr3t-proposed\n"
        ),
    )
    with pytest.raises(Exception) as proposed_exc:
        transform.plan(
            "inline a secret",
            [proposed_secret],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=FilesystemStore(dbt_project_dir.parent),
        )
    assert "s3cr3t-proposed" not in str(proposed_exc.value)

    # Even a clean (env_var) proposal is refused while the on-disk file still
    # inlines a secret, since diffing it would surface the removed literal.
    clean_proposal = transform.PlanEdit(
        path="profiles.yml",
        kind=transform.EditKind.PROFILES_YML,
        new_content=(
            "dex_test:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n"
            "      host: localhost\n      user: u\n"
            "      password: \"{{ env_var('PGPASSWORD') }}\"\n"
            "      dbname: d\n      schema: public\n"
        ),
    )
    with pytest.raises(Exception) as current_exc:
        transform.plan(
            "env-var the password",
            [clean_proposal],
            dbt_project_dir,
            repo_root=dbt_project_dir.parent,
            store=FilesystemStore(dbt_project_dir.parent),
        )
    assert "s3cr3t-on-disk" not in str(current_exc.value)


def test_apply_refuses_to_overwrite_a_human_edit(dbt_project_dir: Path):
    from exmergo_dex_core import transform

    model = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    edits = [
        transform.PlanEdit(
            path="models/staging/stg_customers.sql",
            kind=transform.EditKind.MODEL_SQL,
            new_content="select 1 as id\n",
        )
    ]
    planned, _diffs, _warnings = transform.plan(
        "trim stg_customers",
        edits,
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    # A human edits the file after the plan was made; their edit is authoritative.
    model.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    result = transform.apply(
        planned.plan_id,
        dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert result.written == []
    assert result.conflicts
    assert model.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"


def test_one_conflict_refuses_the_whole_plan_not_the_conflicting_edit(
    dbt_project_dir: Path,
):
    """An apply is all-or-nothing across the plan, and the edit set is the unit.

    Landing the clean edits beside a refused one leaves the project matching
    neither the proposal nor what the human had, while the apply reports itself
    refused, so nothing records which half arrived. The single-file refusal above
    cannot see that: with one edit in the plan there is no clean half to land.
    """

    from exmergo_dex_core import transform

    touched = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    untouched = dbt_project_dir / "models" / "marts" / "fct_orders.sql"
    edits = [
        transform.PlanEdit(
            path="models/marts/fct_orders.sql",
            kind=transform.EditKind.MODEL_SQL,
            new_content="select 1 as order_id\n",
        ),
        transform.PlanEdit(
            path="models/staging/stg_customers.sql",
            kind=transform.EditKind.MODEL_SQL,
            new_content="select 1 as id\n",
        ),
    ]
    planned, _diffs, _warnings = transform.plan(
        "two edits, one of which a human will touch",
        edits,
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    model_existed = untouched.exists()
    touched.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    result = transform.apply(
        planned.plan_id,
        dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )

    assert result.written == []
    assert result.conflicts
    assert touched.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"
    assert untouched.exists() == model_existed, (
        "the clean edit landed while the conflicting one beside it was refused"
    )


def test_a_stored_plan_cannot_escape_the_surface_its_format_declares(
    dbt_project_dir: Path,
):
    """Containment is re-checked at apply, and confirmation does not override it.

    A plan is a stored artifact that sits through a human review, so what it was
    validated against at plan time is not what it is being written into. The
    hashes are re-checked for that reason and the surface now is too: a path
    outside what the format declares is refused before anything reaches the
    writer, and unlike a conflict there is nobody who can accept it.
    """

    import json as _json

    from exmergo_dex_core import transform
    from exmergo_dex_core.adapters.project import DbtProject

    store = FilesystemStore(dbt_project_dir.parent)
    planned, _diffs, _warnings = transform.plan(
        "a plan to tamper with",
        [
            transform.PlanEdit(
                path="models/staging/stg_customers.sql",
                kind=transform.EditKind.MODEL_SQL,
                new_content="select 1 as id\n",
            )
        ],
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=store,
    )
    # The plan is rewritten where it sits, which is the only way to reach apply
    # with a path plan-time containment would have refused.
    stored_path = dbt_project_dir.parent / ".dex" / "plans" / f"{planned.plan_id}.json"
    stored = _json.loads(stored_path.read_text(encoding="utf-8"))
    stored["edits"][0]["path"] = "models_backup/stg_customers.sql"
    stored_path.write_text(_json.dumps(stored), encoding="utf-8")
    escaped = dbt_project_dir / "models_backup" / "stg_customers.sql"

    for confirmed in (False, True):
        with pytest.raises(transform.PlanError, match="editing surface"):
            transform.apply(
                planned.plan_id,
                dbt_project_dir.parent,
                store=store,
                confirmed=confirmed,
                project_format=DbtProject(dbt_project_dir.parent, dbt_project_dir),
            )
    assert not escaped.exists()


def test_an_authored_kind_cannot_land_outside_its_own_path_family(
    dbt_project_dir: Path,
):
    """Writes are confined to the repo, and within the repo to the part of the
    dbt surface the kind belongs to.

    Containment alone is not enough once the surface has several families: a
    snapshot written into ``models/`` is inside the surface and still wrong,
    because dbt parses it as a model and the build fails. So the kind and its
    location have to agree, in both directions, and neither confirmation nor a
    re-plan can talk past it.
    """

    from exmergo_dex_core import transform

    store = FilesystemStore(dbt_project_dir.parent)
    misplaced = [
        (
            transform.EditKind.SNAPSHOT_SQL,
            "models/staging/snap_customers.sql",
            "snapshot paths",
        ),
        (transform.EditKind.SEED_CSV, "models/staging/lookup.csv", "seed paths"),
        (transform.EditKind.SEED_CSV, "macros/lookup.csv", "seed paths"),
        (transform.EditKind.SNAPSHOT_SQL, "seeds/snap.sql", "snapshot paths"),
        # The two families that hold nothing but `.sql` and sit beside each
        # other, which is where a misfiling is easiest to make and hardest to
        # notice: dbt would build a misfiled test as a model, and would never
        # look at a misfiled analysis at all.
        (
            transform.EditKind.TEST_SQL,
            "models/staging/assert_ids.sql",
            "test paths",
        ),
        (transform.EditKind.TEST_SQL, "analyses/assert_ids.sql", "test paths"),
        (
            transform.EditKind.ANALYSIS_SQL,
            "models/staging/scratch.sql",
            "analysis paths",
        ),
        (transform.EditKind.ANALYSIS_SQL, "tests/scratch.sql", "analysis paths"),
    ]
    for kind, path, named in misplaced:
        with pytest.raises(transform.PlanError, match=named):
            transform.plan(
                "misfiled",
                [transform.PlanEdit(path=path, kind=kind, new_content="id\n1\n")],
                dbt_project_dir,
                repo_root=dbt_project_dir.parent,
                store=store,
            )
        assert not (dbt_project_dir / path).exists()
    assert store.list_plans() == []


def test_apply_refuses_to_delete_a_human_edited_file(dbt_project_dir: Path):
    # Propose-don't-impose extends to removal: a delete never silently drops a
    # file a human touched after the plan was made.
    from exmergo_dex_core import transform

    model = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    edits = [
        transform.PlanEdit(
            path="models/staging/stg_customers.sql",
            kind=transform.EditKind.MODEL_SQL,
            op=transform.EditOp.DELETE,
        )
    ]
    planned, _diffs, _warnings = transform.plan(
        "drop stg_customers",
        edits,
        dbt_project_dir,
        repo_root=dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    # A human edits the file after the delete was planned.
    model.write_text("select 99 as id -- keep me\n", encoding="utf-8")

    result = transform.apply(
        planned.plan_id,
        dbt_project_dir.parent,
        store=FilesystemStore(dbt_project_dir.parent),
    )
    assert result.written == []
    assert result.conflicts
    # The file is still there: an unconfirmed delete against a diverged file is
    # refused, not carried out.
    assert model.read_text(encoding="utf-8") == "select 99 as id -- keep me\n"


def test_semantic_planning_writes_nothing_even_with_shadow_parse(
    dbt_project_dir: Path, capsys, monkeypatch
):
    """The plan-time dbt parse runs against a throwaway copy: after a semantic
    plan the project tree is byte-identical, so the only artifact is the plan."""

    import hashlib
    import importlib
    import json as json_mod
    import subprocess

    from exmergo_dex_core.cli import main

    # Give dbt a reason to parse (a time spine) and record what it saw.
    (dbt_project_dir / "models" / "spine.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: metricflow_time_spine\n"
        "    time_spine:\n"
        "      standard_granularity_column: date_day\n"
        "    columns:\n"
        "      - name: date_day\n"
        "        granularity: day\n",
        encoding="utf-8",
    )
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    seen_dirs: list[str] = []

    def recorder(timeout: float, cwd):
        def run(argv: list[str]):
            seen_dirs.append(argv[argv.index("--project-dir") + 1])
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", recorder)

    def tree(root: Path) -> dict[str, str]:
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = tree(dbt_project_dir)
    payload = dbt_project_dir.parent / "sem.json"
    payload.write_text(
        json_mod.dumps(
            {
                "edits": [
                    {
                        "path": "models/semantic/things.yml",
                        "content": "metrics:\n"
                        "  - name: thing_count\n"
                        "    type: simple\n"
                        "    type_params:\n"
                        "      measure: thing_count\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # The measure does not exist, so the plan is refused by reference checks;
    # the write-nothing property must hold on refusal paths too. Then a valid
    # payload exercises the parse path itself.
    main(
        [
            "--repo-root",
            str(dbt_project_dir.parent),
            "semantic",
            "plan",
            "x",
            "--edits-file",
            str(payload),
        ]
    )
    capsys.readouterr()
    payload.write_text(
        json_mod.dumps(
            {
                "edits": [
                    {
                        "path": "models/semantic/things.yml",
                        "content": "semantic_models:\n"
                        "  - name: things\n"
                        "    model: ref('stg_customers')\n"
                        "    entities:\n"
                        "      - name: thing\n"
                        "        type: primary\n"
                        "        expr: id\n"
                        "    measures:\n"
                        "      - name: thing_count\n"
                        "        agg: count\n"
                        "        expr: id\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "--repo-root",
            str(dbt_project_dir.parent),
            "semantic",
            "plan",
            "x",
            "--edits-file",
            str(payload),
        ]
    )
    capsys.readouterr()
    assert rc == 0
    assert seen_dirs, "the shadow parse ran"
    assert all(d != str(dbt_project_dir) for d in seen_dirs)
    assert tree(dbt_project_dir) == before


# `transform init` sits across families 1 and 4: the profile it generates is
# what the dev-target-only rule later reads, and bootstrap must stay strictly
# additive with no silent connector default.


def test_init_refuses_where_a_project_already_exists(dbt_project_dir: Path):
    # Bootstrap is strictly additive: anywhere find_project would discover a
    # project, init refuses, so it can never clobber hand-written work.
    from exmergo_dex_core import transform

    repo = dbt_project_dir.parent
    with pytest.raises(transform.InitError):
        transform.init_project(
            "fresh", "duckdb", path=str(repo / "warehouse.duckdb"), repo_root=repo
        )


def test_demo_refuses_to_overwrite_an_existing_warehouse(tmp_path: Path, capsys):
    """`dex demo` is create-only, and deliberately not confirmable.

    The sibling of the init refusal above, for the one verb that writes a data
    file rather than a project file. It is stricter than init on purpose: a
    `--confirm` that could talk past this would put a real warehouse one typo
    away from being replaced, and the fix (name another path) costs nothing.
    """

    import json

    pytest.importorskip("duckdb")
    from exmergo_dex_core.cli import main

    existing = tmp_path / "production.duckdb"
    existing.write_bytes(b"someone else's warehouse")

    for argv in (["demo", str(existing)], ["demo", str(existing), "--confirm"]):
        assert main(argv) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["reason"] == "guard"
        assert existing.read_bytes() == b"someone else's warehouse"


def test_demo_creates_only_what_it_names_and_never_a_second_config(
    tmp_path: Path, monkeypatch, capsys
):
    """Two artifacts, both reported, and never one that shadows a real project.

    A `.dex/config.yml` written into a subdirectory of someone's repo would
    silently capture every command run there, which is the same class of harm as
    overwriting a file: a change they did not ask for and would not see.
    """

    import json

    pytest.importorskip("duckdb")
    from exmergo_dex_core.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    on_disk = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    )
    assert on_disk == sorted(payload["data"]["created"])

    # Now with a project config already above the target: the warehouse is still
    # created, the config is not, and the envelope says which happened.
    (tmp_path / ".git").mkdir()
    (tmp_path / "scratch").mkdir()
    committed = tmp_path / ".dex" / "config.yml"
    before = committed.read_text(encoding="utf-8")

    assert main(["demo", "scratch/second.duckdb"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["created"] == ["scratch/second.duckdb"]
    assert not (tmp_path / "scratch" / ".dex").exists()
    assert committed.read_text(encoding="utf-8") == before
    assert any("left untouched" in w for w in payload["warnings"])


def test_init_never_falls_through_to_a_default_connector(tmp_path: Path, capsys):
    # Init bakes the connector into a durable artifact (the generated
    # profiles.yml), so the engine-wide DuckDB default does not apply: bare init
    # errors and creates nothing.
    import json

    from exmergo_dex_core.cli import main

    rc = main(["--repo-root", str(tmp_path), "transform", "init", "analytics"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == "error"
    assert "--connector" in payload["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_config_resolves_from_a_subdirectory_not_a_silent_default(tmp_path: Path):
    # The other half of "no silent connector default": a command run from a
    # subdirectory of a project must resolve the project's own .dex/config.yml
    # (walked up to the git root), never fall through to the engine-wide DuckDB
    # default and then surface as a phantom config/profiles connector mismatch.
    import argparse

    from exmergo_dex_core import command_args
    from exmergo_dex_core.config import DexConfig, save_config

    (tmp_path / ".git").mkdir()
    save_config(DexConfig(connector="bigquery"), tmp_path)
    sub = tmp_path / "analytics" / "models"
    sub.mkdir(parents=True)

    resolved = command_args.repo_root(argparse.Namespace(repo_root=str(sub)))
    assert resolved == str(tmp_path.resolve())


def test_no_config_anywhere_refuses_rather_than_defaulting_to_duckdb(tmp_path: Path):
    # With no config resolvable and nothing explicit to fall back on, the engine
    # refuses instead of reading a phantom DuckDB target. An explicit --connector
    # or --path still drives the bare ad-hoc read.
    from exmergo_dex_core.connect import open_adapter

    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match=r"no \.dex/config\.yml found"):
        open_adapter(repo_root=str(tmp_path))


def test_init_profile_is_dev_only_with_no_secrets(tmp_path: Path):
    # The generated profiles.yml is why bootstrap is engine-owned: a single dev
    # default target, nothing prod-named, and no secret-like keys anywhere.
    import yaml

    from exmergo_dex_core import transform

    transform.init_project(
        "analytics", "duckdb", path=str(tmp_path / "w.duckdb"), repo_root=tmp_path
    )
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    profile = profiles["analytics"]
    assert profile["target"] == "dev"
    assert set(profile["outputs"]) == {"dev"}
    # The envelope sanitizer doubles as the secret-key scanner here.
    env.sanitize(env.ok(profiles))


def test_init_project_round_trips_through_the_loader(tmp_path: Path):
    from exmergo_dex_core import dbt_project, transform

    transform.init_project(
        "analytics", "duckdb", path=str(tmp_path / "w.duckdb"), repo_root=tmp_path
    )
    view = dbt_project.load(dbt_project.find_project(tmp_path))
    assert view.project_name == "analytics"
    assert view.profile_name == "analytics"
    assert dbt_project.resolve_target(tmp_path / "analytics").name == "dev"


def test_layered_init_keeps_every_init_invariant(tmp_path: Path):
    # The layered variant adds files but changes no safety property: still one
    # dev-only target, still no secret-like keys anywhere in the generated set,
    # still credential-free (this test runs with no connection of any kind).
    import yaml

    from exmergo_dex_core import transform

    transform.init_project(
        "analytics",
        "duckdb",
        path=str(tmp_path / "w.duckdb"),
        repo_root=tmp_path,
        layered_schemas=True,
    )
    project = tmp_path / "analytics"
    profiles = yaml.safe_load((project / "profiles.yml").read_text(encoding="utf-8"))
    assert profiles["analytics"]["target"] == "dev"
    assert set(profiles["analytics"]["outputs"]) == {"dev"}
    generated = {
        rel: (project / rel).read_text(encoding="utf-8")
        for rel in (
            "dbt_project.yml",
            "profiles.yml",
            "macros/generate_schema_name.sql",
        )
    }
    env.sanitize(env.ok(generated))


def test_init_content_preflight_is_free_and_never_gates(tmp_path: Path, monkeypatch):
    # The init-time content check rides the free metadata door on a billed
    # connector: an unconfirmed zero-budget cost gate would refuse any spend,
    # and the probe must never ask it. Nothing may cross the data door either.
    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import (
        FakeSnowflakeConnection,
        FakeSnowflakeTable,
        FakeWarehouse,
    )

    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.config import DexConfig, SnowflakeTarget
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.transform import dev_target

    connection = FakeSnowflakeConnection(
        tables=[
            FakeSnowflakeTable(
                database="SCRATCH",
                schema="STAGING_DEV",
                name="LEFTOVER",
                columns=[("ID", "FIXED", False)],
            )
        ],
        warehouses=[FakeWarehouse(name="DEX_WH")],
    )
    adapter = SnowflakeAdapter(
        connection=connection,
        cost_gate=CostGate(
            paradigm=env.Paradigm.COMPUTE_TIME,
            ceiling=0.0,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="snowflake",
        ),
        target=SnowflakeTarget(warehouse="DEX_WH"),
        clock=connection.clock,
    )
    monkeypatch.setattr(
        "exmergo_dex_core.connect.open_adapter", lambda **_kwargs: adapter
    )
    config = DexConfig(
        connector="snowflake",
        dbt_target="dev",
        snowflake=SnowflakeTarget(warehouse="DEX_WH", dev_database="SCRATCH"),
    )
    warnings = dev_target.content_check(config, tmp_path, layered=True)
    assert any("SCRATCH.STAGING_DEV" in w for w in warnings)
    assert connection.data_statements == []
    # The warnings themselves must be message-clean: names and counts only.
    env.emit(env.ok({}, warnings=warnings))


def test_an_in_memory_store_writes_nothing_across_a_multi_step_flow(
    duckdb_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The store is the only thing standing between the engine and the user's disk.

    An in-process caller asked for no persistence, so a full flow (profile, then a
    query that reads the profile back) must leave the tree exactly as it found it:
    no `.dex/` directory, no stray file anywhere, not even in the working directory
    a relative path would resolve against.
    """

    from exmergo_dex_core import DexEngine

    repo = duckdb_file.parent
    monkeypatch.chdir(repo)

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(repo)): p.read_bytes()
            for p in sorted(repo.rglob("*"))
            if p.is_file()
        }

    before = snapshot()

    # The public API with its defaults, which is the shape that matters: a
    # consumer who imports this package and calls it must not acquire a `.dex/`
    # directory as a side effect of doing so.
    with DexEngine(connector="duckdb", path=str(duckdb_file)) as eng:
        profiled = eng.profile("customers")
        # Real work happened, so the silence below is not the silence of a no-op.
        assert profiled.profiled_count == 1
        # The locator is honest about there being no file to open.
        assert profiled.cache_path == "memory:cache"

        # The second step proves retention, not just silence: the query firewall
        # derives its PII policy from the cache the first step stored, so a store
        # that dropped writes would fail here rather than pass quietly.
        queried = eng.query("select count(*) as n from customers")
        assert queried.row_count == 1

        # The diagram renders an artifact a caller will very likely want on
        # disk, which is exactly why the engine hands back a string and writes
        # nothing: choosing where a file lands is the caller's decision, and a
        # renderer that helpfully dropped a `.mmd` somewhere would break the
        # no-persistence promise this whole test exists to hold.
        drawn = eng.diagram(full=True)
        assert drawn.entity_count == 1
        assert drawn.mermaid.startswith("erDiagram\n")

    assert snapshot() == before
    assert not (repo / ".dex").exists()


def test_an_in_memory_session_budget_still_binds(fake_bq_client):
    """A backend that forgets across processes must not forget within one.

    MemoryStore's ledger lives for the process, so the daily session ceiling
    resets with the next one (right for a library call, wrong for the CLI, which
    is why the CLI keeps the filesystem store). Within one process, though, the
    cumulative ceiling has to bind exactly as it does on disk.
    """

    from exmergo_dex_core.guards.cost_guard import CostGate, OverCeilingError

    store = MemoryStore()
    cutoff = "2026-07-03T00:00:00+00:00"
    store.append_spend_log(
        {
            "at": "2026-07-03T09:00:00+00:00",
            "connector": "bigquery",
            "billed_bytes": 900,
        }
    )
    gate = CostGate(
        paradigm=env.Paradigm.BYTES_SCANNED,
        ceiling=10_000,
        session_ceiling=1_000,
        session_spent=store.spend_since(cutoff, connector="bigquery"),
        confirmed=True,
        connector="bigquery",
        record=store.append_spend_log,
    )
    # 900 already spent against a 1_000 session ceiling: the per-command ceiling
    # would allow this, the session ceiling must not.
    with pytest.raises(OverCeilingError):
        gate.preflight_command(500)


# --- Family 5: credentials and raw rows never enter stdout data ---------------


def test_envelope_blocks_secrets_in_data():
    with pytest.raises(env.SanitizationError):
        env.emit(env.ok({"connection": {"password": "hunter2"}}))


def test_envelope_blocks_raw_rows_in_data():
    with pytest.raises(env.SanitizationError):
        env.emit(env.ok({"rows": [{"id": 1, "email": "a@example.com"}]}))


def test_query_results_are_columnar_and_pass_the_sanitizer(capsys):
    # The query path's list-of-lists shape crosses cleanly; the dict-row rule
    # above still guards every other command against accidental record dumps.
    env.emit(env.ok({"columns": ["id", "n"], "cells": [[1, 3], [2, 5]]}))
    assert capsys.readouterr().out


def test_no_payload_is_keyed_by_a_warehouse_object_name(capsys):
    """The sanitizer matches *key names* against secret-like substrings, so any
    payload that keys an object by user-controlled data hands a warehouse the
    power to fail the boundary check. A table called `access_tokens` is an
    entirely ordinary thing to own, and the diagram's entity legend is the one
    payload that was tempted to key by object name; it emits records instead.

    Stated as a rule rather than a diagram test because the next payload that
    wants a name-keyed map should meet this assertion first.
    """

    from exmergo_dex_core.cache import Dataset, DexCache
    from exmergo_dex_core.explore.results import DiagramResult

    hostile = ["access_tokens", "user_credentials", "api_key_rotation", "secrets"]
    cache = DexCache(
        datasets=[
            Dataset(
                identifier=f"vault.main.{name}",
                columns=[ColumnProfile(name="id", data_type="INTEGER", is_unique=True)],
            )
            for name in hostile
        ]
    )
    rendered = render_er_mermaid(cache, full=True)
    assert set(rendered.entities) == set(hostile), "the fixture must be hostile"

    envelope = to_envelope(
        DiagramResult(mermaid=rendered.mermaid, entities=rendered.entities)
    )
    env.emit(envelope)  # raises SanitizationError if any object name became a key
    assert capsys.readouterr().out


# --- BigQuery: the billed connector exercises every family ---------------------
#
# These run against the fake client (tests/fakes/bigquery.py): deterministic,
# offline, free. They importorskip on the [bigquery] extra, which CI and the
# release gate install, so trimming that extra from a workflow would silently
# skip release-blocking families; keep `--extra bigquery` in ci.yml and
# release.yml.


def _bq_adapter(fake_bq_client, *, ceiling=500 * 1024 * 1024, confirmed=True):
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.BYTES_SCANNED,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="bigquery",
    )
    return BigQueryAdapter(
        project="test-proj",
        cost_gate=gate,
        client=fake_bq_client,
        principal_type="user",
    )


def test_bigquery_generated_sql_is_select_only(fake_bq_client):
    # Family 1: every statement the adapter generates passes the SELECT-only
    # guard in the bigquery dialect (asserted at build time, no client needed).
    from exmergo_dex_core.adapters.base import (
        ColumnMeta,
        is_integer_type,
        is_string_type,
    )
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _bq_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.shop.customers")
    columns = [*columns, ColumnMeta("signup_ts", "TIMESTAMP", True, len(columns))]
    shape = {
        c.name
        for c in columns
        if "CHAR" in c.data_type.upper()
        or "STRING" in c.data_type.upper()
        or "TEXT" in c.data_type.upper()
    }
    type_req = {
        c.name
        for c in columns
        if is_string_type(c.data_type) or is_integer_type(c.data_type)
    }
    key_shape_req = {c.name for c in columns if is_string_type(c.data_type)}
    temporal_req = {"signup_ts"}
    sql, _plan = adapter._build_aggregate_sql(
        "test-proj.shop.customers",
        columns,
        {"id"},
        shape,
        type_req,
        key_shape_req,
        temporal_req,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    # Declared-type-vs-content statistics (#204) ride the same statement too.
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
    # ...with every cast in them total, so no dialect can raise on a
    # non-numeric string in a profiled column (#310).
    assert_every_cast_is_total(sql)
    # Heterogeneous-key-shape statistics (#205) ride the same statement too.
    assert "ks_uuid_" in sql and "ks_hex_" in sql
    # Temporal-continuity statistics (#206) ride the same statement too,
    # using TIMESTAMP_TRUNC/TIMESTAMP_DIFF (BigQuery's reversed argument
    # order and type-specific truncation family).
    assert "tc_da_" in sql and "tp_d_" in sql and "tg_h_" in sql
    assert "TIMESTAMP_TRUNC" in sql and "TIMESTAMP_DIFF" in sql
    assert assert_select_only(sql, dialect="bigquery") == sql


def test_select_only_guard_rejects_bigquery_writes_and_scripts():
    # Family 1: BigQuery scripting, DML/DDL, and multi-statement forms are all
    # refused when parsed in the bigquery dialect.
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "DECLARE x INT64; SELECT x",
        "CREATE TEMP TABLE t AS SELECT 1",
        "MERGE INTO d.t USING d.s ON FALSE WHEN NOT MATCHED THEN INSERT ROW",
        "TRUNCATE TABLE d.t",
        "SELECT 1; SELECT 2",
        "DELETE FROM d.t WHERE TRUE",
        "EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT 1",
        "CALL d.proc()",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad, dialect="bigquery")


def test_bigquery_unconfirmed_scan_never_executes(fake_bq_client):
    # Family 2: the strict handshake. Without --confirm only the free dry-run
    # happens; the refusal carries the estimate for the agent to surface.
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _bq_adapter(fake_bq_client, confirmed=False)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert exc_info.value.cost.estimate == 5_000
    assert [c.dry_run for c in fake_bq_client.query_calls] == [True]


def test_bigquery_confirmed_run_without_a_ceiling_is_refused(fake_bq_client):
    # Family 2: nothing executes unbudgeted, and confirmation cannot stand in
    # for a ceiling on a billed paradigm.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _bq_adapter(fake_bq_client, ceiling=None, confirmed=True)
    with pytest.raises(CostGuardError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_bigquery_over_ceiling_cannot_be_confirmed_through(fake_bq_client):
    # Family 2: over-ceiling blocks first, even fully confirmed.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _bq_adapter(fake_bq_client, ceiling=1_000, confirmed=True)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `test-proj`.`shop`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_bigquery_a_narrowed_reserve_still_bounds_what_profiling_bills():
    # Family 2: the estimate is a ceiling actual spend will not exceed (#107),
    # and that has to survive every reserve the estimator declines to hold
    # (#299). A view is the sharpest case: it has no row count, so all three
    # escalation probes bail and the estimator now reserves nothing at all for
    # it. If any of them could still run, this is where the quoted number would
    # turn out to be less than the bill.
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    from exmergo_dex_core.explore import profile as profile_mod

    bigquery = pytest.importorskip("google.cloud.bigquery")
    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="customers_v",
                schema=[
                    bigquery.SchemaField("id", "INTEGER"),
                    bigquery.SchemaField("tier", "STRING"),
                ],
                num_rows=100,  # nulled out for a view, which is the point
                num_bytes=5_000,
                table_type="VIEW",
            )
        ],
        row_resolver=lambda sql: [
            {
                "n_total": 100,
                "nn_0": 100,
                "nd_0": 100,
                "mn_0": 1,
                "mx_0": 100,
                "nn_1": 100,
                "nd_1": 3,
                "d_0": 100,
                "d_1": 3,
            }
        ],
    )
    adapter = _bq_adapter(client)
    estimate, _per_table = adapter.profile_estimate(["test-proj.shop.customers_v"])
    profile_mod.profile(adapter, ["test-proj.shop.customers_v"])
    billed = adapter.cost_gate.spend_summary()["bytes_billed"]
    assert billed <= estimate
    # And no escalation was attempted, which is why nothing was reserved.
    executed = [c for c in client.query_calls if not c.dry_run]
    assert len(executed) == 1


def test_bigquery_every_executed_job_is_server_capped(fake_bq_client):
    # Family 2: defense in depth past the client-side gate; a wrong estimate
    # cannot overrun the budget because the service enforces the cap.
    fake_bq_client.row_resolver = lambda sql: [{"n": 1}]
    adapter = _bq_adapter(fake_bq_client)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    executed = [c for c in fake_bq_client.query_calls if not c.dry_run]
    assert executed
    assert all(c.job_config.maximum_bytes_billed is not None for c in executed)


def test_query_firewall_blocks_bigquery_value_carrying_shapes():
    # Family 3: PII stays flagged-not-surfaced under the bigquery dialect,
    # including BigQuery's own value-carrying aggregates and JSON casts.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT ANY_VALUE(email) FROM db.main.customers",
        "SELECT ARRAY_AGG(email) FROM db.main.customers",
        "SELECT STRING_AGG(email) FROM db.main.customers",
        "SELECT TO_JSON_STRING(email) FROM db.main.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits(), dialect="bigquery")
    # Measuring stays allowed in the bigquery dialect too.
    inspect_query(
        "SELECT COUNT(DISTINCT email) FROM db.main.customers",
        cache,
        QueryLimits(),
        dialect="bigquery",
    )


def test_init_bigquery_profile_is_dev_only_with_no_secrets(tmp_path: Path):
    # Family 4: the generated BigQuery profile has a single dev target, ADC
    # auth (method: oauth), and no secret-shaped key anywhere.
    import yaml

    from exmergo_dex_core import transform
    from exmergo_dex_core.config import CONFIG_FILE
    from exmergo_dex_core.storage import DEX_DIR

    (tmp_path / DEX_DIR).mkdir()
    (tmp_path / DEX_DIR / CONFIG_FILE).write_text(
        "bigquery:\n  project: test-proj\n", encoding="utf-8"
    )
    transform.init_project("analytics", "bigquery", repo_root=tmp_path)
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    profile = profiles["analytics"]
    assert profile["target"] == "dev"
    assert set(profile["outputs"]) == {"dev"}
    assert profile["outputs"]["dev"]["method"] == "oauth"
    # The envelope sanitizer doubles as the secret-key scanner here.
    env.sanitize(env.ok(profiles))


def test_bigquery_capabilities_pass_the_sanitizer(fake_bq_client, capsys):
    # Family 5: the capabilities payload carries the principal's TYPE, never
    # an identity or key material, and survives the sanitizer end to end.
    adapter = _bq_adapter(fake_bq_client)
    caps = adapter.capabilities()
    env.emit(env.ok(caps))
    out = capsys.readouterr().out
    assert out
    assert "@" not in out  # no principal email
    assert caps["principal_type"] in {
        "user",
        "service_account",
        "external_account",
        "metadata",
        "unknown",
    }


def test_bigquery_spend_ledger_holds_no_sql_or_values(tmp_path: Path, fake_bq_client):
    # Family 5: the audit trail is byte counts and statement hashes only.
    import json

    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_bq_client.row_resolver = lambda sql: [{"n": 1}]
    adapter = _bq_adapter(fake_bq_client)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `test-proj`.`shop`.`customers`",
        max_rows=10,
        timeout_seconds=30,
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert "SELECT" not in json.dumps(entry)
    assert entry["billed_bytes"] == 5_000
    assert entry["statement_sha256"]


# --- Snowflake: the compute-time connector exercises every family ---------------
#
# These run against the fake connection (tests/fakes/snowflake.py):
# deterministic, offline, free. They importorskip on the [snowflake] extra,
# which CI and the release gate install, so trimming that extra from a
# workflow would silently skip release-blocking families; keep
# `--extra snowflake` in ci.yml and release.yml.


def _sf_adapter(
    fake_sf_connection,
    *,
    ceiling=600.0,
    confirmed=True,
    databases=None,
    scope_override=None,
):
    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="snowflake",
    )
    return SnowflakeAdapter(
        connection=fake_sf_connection,
        cost_gate=gate,
        target=SnowflakeTarget(warehouse="DEX_WH", databases=databases or []),
        account="TESTORG-TESTACCT",
        auth_method="named_connection:key_pair",
        scope_override=scope_override,
        clock=fake_sf_connection.clock,
    )


def test_snowflake_scope_flag_cannot_widen_the_committed_allowlist(fake_sf_connection):
    # Family 2: Snowflake resolves bare schema names against the account, so it
    # enforces the committed cost boundary inside the adapter, after resolution
    # and before anything is estimated or spent.
    from exmergo_dex_core.adapters.snowflake import SnowflakeConnectionError

    adapter = _sf_adapter(
        fake_sf_connection, databases=["SHOP.PUBLIC"], scope_override=["SHOP"]
    )
    with pytest.raises(SnowflakeConnectionError, match="never widens"):
        adapter.list_objects()
    assert fake_sf_connection.data_statements == []


def test_snowflake_unresolvable_scope_never_falls_back_to_the_whole_allowlist(
    fake_sf_connection,
):
    # Family 2: the cost-safety bug this guards. A scope that names nothing must
    # refuse, never silently widen to every table the allowlist permits.
    from exmergo_dex_core.adapters.snowflake import SnowflakeConnectionError

    adapter = _sf_adapter(
        fake_sf_connection, databases=["SHOP"], scope_override=["__NOT_A_SCHEMA__"]
    )
    with pytest.raises(SnowflakeConnectionError):
        adapter.profile_estimate(["SHOP.PUBLIC.EVENTS"])
    assert fake_sf_connection.data_statements == []


def test_an_unresolvable_scope_never_falls_back_on_any_connector(
    fake_bq_client, fake_databricks, fake_pg_connection, fake_redshift_connection
):
    # Family 2: the same cost-safety bug, on every warehouse connector. A source
    # scope that names nothing must refuse, and must do so on the free metadata
    # path: never an empty inventory the user was not told about, and never a
    # fallback to every table the allowlist permits. The estimate a user confirms
    # has to cover what they actually named.
    from exmergo_dex_core.adapters.bigquery import (
        BigQueryAdapter,
        BigQueryConnectionError,
    )
    from exmergo_dex_core.adapters.databricks import (
        DatabricksAdapter,
        DatabricksConnectionError,
    )
    from exmergo_dex_core.adapters.postgres import (
        PostgresAdapter,
        PostgresConnectionError,
    )
    from exmergo_dex_core.adapters.redshift import (
        RedshiftAdapter,
        RedshiftConnectionError,
    )
    from exmergo_dex_core.config import (
        BigQueryTarget,
        DatabricksTarget,
        PostgresTarget,
        RedshiftTarget,
    )
    from exmergo_dex_core.guards.cost_guard import CostGate

    def gate(paradigm, connector):
        return CostGate(
            paradigm=paradigm,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=True,
            connector=connector,
        )

    bigquery = BigQueryAdapter(
        project="test-proj",
        cost_gate=gate(env.Paradigm.BYTES_SCANNED, "bigquery"),
        target=BigQueryTarget(datasets=["__not_a_dataset__"]),
        client=fake_bq_client,
    )
    databricks = DatabricksAdapter(
        workspace=fake_databricks.workspace,
        sql_connect=fake_databricks.sql_connect,
        cost_gate=gate(env.Paradigm.COMPUTE_TIME, "databricks"),
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["__not_a_catalog__"]),
        clock=fake_databricks.clock,
    )
    postgres = PostgresAdapter(
        connection=fake_pg_connection,
        cost_gate=gate(env.Paradigm.DB_LOAD, "postgres"),
        target=PostgresTarget(schemas=["__not_a_schema__"]),
        clock=fake_pg_connection.clock,
    )
    redshift = RedshiftAdapter(
        connection=fake_redshift_connection,
        cost_gate=gate(env.Paradigm.COMPUTE_TIME, "redshift"),
        target=RedshiftTarget(schemas=["__not_a_schema__"]),
        clock=fake_redshift_connection.clock,
    )

    with pytest.raises(BigQueryConnectionError):
        bigquery.list_objects()
    with pytest.raises(DatabricksConnectionError):
        databricks.list_objects()
    with pytest.raises(PostgresConnectionError):
        postgres.list_objects()
    with pytest.raises(RedshiftConnectionError):
        redshift.list_objects()

    # Refused on the free path: nothing was queried, and no SQL session opened.
    assert fake_bq_client.query_calls == []
    assert fake_databricks.connection.data_statements == []
    assert fake_databricks.connect_count == 0
    assert fake_pg_connection.data_statements == []
    assert fake_redshift_connection.data_statements == []


def test_snowflake_generated_sql_is_select_only(fake_sf_connection):
    # Family 1: every data statement the adapter generates passes the
    # SELECT-only guard in the snowflake dialect (asserted at build time).
    from exmergo_dex_core.adapters.base import (
        ColumnMeta,
        is_integer_type,
        is_string_type,
    )
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _sf_adapter(fake_sf_connection)
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")
    columns = [*columns, ColumnMeta("SIGNUP_TS", "TIMESTAMP_NTZ", True, len(columns))]
    shape = {
        c.name
        for c in columns
        if "CHAR" in c.data_type.upper()
        or "STRING" in c.data_type.upper()
        or "TEXT" in c.data_type.upper()
    }
    type_req = {
        c.name
        for c in columns
        if is_string_type(c.data_type) or is_integer_type(c.data_type)
    }
    key_shape_req = {c.name for c in columns if is_string_type(c.data_type)}
    temporal_req = {"SIGNUP_TS"}
    sql, _plan = adapter._build_aggregate_sql(
        "SHOP.PUBLIC.CUSTOMERS",
        columns,
        {"ID"},
        shape,
        type_req,
        key_shape_req,
        temporal_req,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
    # ...with every cast in them total, so no dialect can raise on a
    # non-numeric string in a profiled column (#310).
    assert_every_cast_is_total(sql)
    assert "ks_uuid_" in sql and "ks_hex_" in sql
    # Temporal-continuity statistics (#206) ride the same statement too.
    assert "tc_da_" in sql and "tp_d_" in sql and "tg_h_" in sql
    assert "DATE_TRUNC" in sql and "DATEDIFF" in sql
    assert assert_select_only(sql, dialect="snowflake") == sql


def test_select_only_guard_rejects_snowflake_writes_and_ddl():
    # Family 1: Snowflake DML/DDL, stage/data movement, and multi-statement
    # forms are all refused when parsed in the snowflake dialect.
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "CREATE TABLE t AS SELECT 1",
        "MERGE INTO d.t USING d.s ON FALSE WHEN NOT MATCHED THEN INSERT VALUES (1)",
        "TRUNCATE TABLE d.t",
        "SELECT 1; SELECT 2",
        "DELETE FROM d.t WHERE TRUE",
        "COPY INTO @mystage/x FROM (SELECT 1)",
        "CALL d.proc()",
        "ALTER WAREHOUSE wh SET WAREHOUSE_SIZE = 'X-Large'",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad, dialect="snowflake")


def test_snowflake_unconfirmed_scan_never_executes(fake_sf_connection):
    # Family 2: the strict handshake. Without --confirm nothing runs on the
    # warehouse (estimation is free SHOW metadata, so there is nothing to bill).
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _sf_adapter(fake_sf_connection, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_sf_connection.data_statements == []


def test_snowflake_confirmed_run_without_a_ceiling_is_refused(fake_sf_connection):
    # Family 2: nothing executes unbudgeted; confirmation cannot stand in for
    # a ceiling on a billed paradigm.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _sf_adapter(fake_sf_connection, ceiling=None, confirmed=True)
    with pytest.raises(CostGuardError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."CUSTOMERS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_sf_connection.data_statements == []


def test_snowflake_over_ceiling_cannot_be_confirmed_through(fake_sf_connection):
    # Family 2: over-ceiling blocks first, even fully confirmed.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _sf_adapter(fake_sf_connection, ceiling=2.0, confirmed=True)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "SHOP"."PUBLIC"."EVENTS"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_sf_connection.data_statements == []


def test_snowflake_every_executed_statement_is_server_capped(fake_sf_connection):
    # Family 2: defense in depth past the client-side gate; a wrong heuristic
    # cannot overrun the budget because the statement timeout kills it.
    fake_sf_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _sf_adapter(fake_sf_connection)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
    )
    executed = fake_sf_connection.data_statements
    assert executed
    assert all(s.session_timeout is not None for s in executed)


def test_query_firewall_blocks_snowflake_value_carrying_shapes():
    # Family 3: PII stays flagged-not-surfaced under the snowflake dialect,
    # including Snowflake's own value-carrying aggregates and casts.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT ANY_VALUE(email) FROM db.main.customers",
        "SELECT ARRAY_AGG(email) FROM db.main.customers",
        "SELECT LISTAGG(email, ',') FROM db.main.customers",
        "SELECT TO_JSON(email) FROM db.main.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits(), dialect="snowflake")
    # Measuring stays allowed in the snowflake dialect too.
    inspect_query(
        "SELECT COUNT(DISTINCT email) FROM db.main.customers",
        cache,
        QueryLimits(),
        dialect="snowflake",
    )


def test_snowflake_capabilities_pass_the_sanitizer(fake_sf_connection, capsys):
    # Family 5: the capabilities payload carries a coarse auth method, never
    # an identity, password, or key, and survives the sanitizer end to end.
    adapter = _sf_adapter(fake_sf_connection)
    caps = adapter.capabilities()
    env.emit(env.ok(caps))
    out = capsys.readouterr().out
    assert out
    assert "@" not in out  # no user identity
    assert caps["auth_method"].split(":")[0] in {
        "named_connection",
        "default_connection",
        "environment",
        "dbt_profile",
        "unknown",
    }


def test_snowflake_spend_ledger_holds_no_sql_or_values(
    tmp_path: Path, fake_sf_connection
):
    # Family 5: the audit trail is second counts and statement hashes only.
    import json

    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_sf_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _sf_adapter(fake_sf_connection)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "SHOP"."PUBLIC"."CUSTOMERS"',
        max_rows=10,
        timeout_seconds=200,
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert "SELECT" not in json.dumps(entry)
    assert entry["billed_seconds"] > 0
    assert entry["statement_sha256"]


def test_ledgers_never_mix_paradigms(tmp_path: Path):
    # Family 2 (cross-connector): a bytes session budget must not absorb a
    # seconds entry and vice versa; each connector sums only its own unit.
    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    store.append_spend_log(
        {
            "at": "2026-07-05T00:00:01+00:00",
            "connector": "bigquery",
            "billed_bytes": 5000,
        }
    )
    store.append_spend_log(
        {
            "at": "2026-07-05T00:00:02+00:00",
            "connector": "snowflake",
            "billed_seconds": 42.0,
        }
    )
    store.append_spend_log(
        {
            "at": "2026-07-05T00:00:03+00:00",
            "connector": "postgres",
            "billed_seconds": 7.0,
        }
    )
    store.append_spend_log(
        {
            "at": "2026-07-05T00:00:04+00:00",
            "connector": "databricks",
            "billed_seconds": 11.0,
        }
    )
    store.append_spend_log(
        {
            "at": "2026-07-05T00:00:05+00:00",
            "connector": "redshift",
            "billed_seconds": 13.0,
        }
    )
    assert store.spend_since("2026-07-05T00:00:00+00:00", connector="bigquery") == 5000
    assert (
        store.spend_since(
            "2026-07-05T00:00:00+00:00", field="billed_seconds", connector="snowflake"
        )
        == 42.0
    )
    assert (
        store.spend_since(
            "2026-07-05T00:00:00+00:00", field="billed_seconds", connector="postgres"
        )
        == 7.0
    )
    assert (
        store.spend_since(
            "2026-07-05T00:00:00+00:00", field="billed_seconds", connector="databricks"
        )
        == 11.0
    )
    assert (
        store.spend_since(
            "2026-07-05T00:00:00+00:00", field="billed_seconds", connector="redshift"
        )
        == 13.0
    )


# --- Databricks: the lakehouse compute-time connector exercises every family -----
#
# These run against the fake pair (tests/fakes/databricks.py): deterministic,
# offline, free. They importorskip on the [databricks] extra (via the
# fake_databricks fixture), which CI and the release gate install, so trimming
# that extra from a workflow would silently skip release-blocking families;
# keep `--extra databricks` in ci.yml and release.yml.


def _dbx_adapter(fake_databricks, *, ceiling=600.0, confirmed=True):
    from exmergo_dex_core.adapters.databricks import DatabricksAdapter
    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="databricks",
    )
    return DatabricksAdapter(
        workspace=fake_databricks.workspace,
        sql_connect=fake_databricks.sql_connect,
        cost_gate=gate,
        target=DatabricksTarget(warehouse="fake-wh"),
        host="test.cloud.databricks.com",
        auth_method="default_profile:oauth_user",
        clock=fake_databricks.clock,
    )


def test_databricks_generated_sql_is_select_only(fake_databricks):
    # Family 1: every data statement the adapter generates passes the
    # SELECT-only guard in the databricks dialect (asserted at build time).
    from exmergo_dex_core.adapters.base import (
        ColumnMeta,
        is_integer_type,
        is_string_type,
    )
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _dbx_adapter(fake_databricks)
    _meta, columns = adapter.table_metadata("shop.core.customers")
    columns = [*columns, ColumnMeta("signup_ts", "TIMESTAMP", True, len(columns))]
    shape = {
        c.name
        for c in columns
        if "CHAR" in c.data_type.upper()
        or "STRING" in c.data_type.upper()
        or "TEXT" in c.data_type.upper()
    }
    type_req = {
        c.name
        for c in columns
        if is_string_type(c.data_type) or is_integer_type(c.data_type)
    }
    key_shape_req = {c.name for c in columns if is_string_type(c.data_type)}
    temporal_req = {"signup_ts"}
    sql, _plan = adapter._build_aggregate_sql(
        "shop.core.customers",
        columns,
        {"id"},
        shape,
        type_req,
        key_shape_req,
        temporal_req,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
    # ...with every cast in them total, so no dialect can raise on a
    # non-numeric string in a profiled column (#310).
    assert_every_cast_is_total(sql)
    assert "ks_uuid_" in sql and "ks_hex_" in sql
    # Temporal-continuity statistics (#206) ride the same statement too.
    assert "tc_da_" in sql and "tp_d_" in sql and "tg_h_" in sql
    assert "date_trunc" in sql and "TIMESTAMPDIFF" in sql
    assert assert_select_only(sql, dialect="databricks") == sql


def test_select_only_guard_rejects_databricks_writes_and_ddl():
    # Family 1: Databricks DML/DDL, Delta maintenance, and multi-statement
    # forms are all refused when parsed in the databricks dialect.
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "CREATE TABLE t AS SELECT 1",
        "MERGE INTO d.t USING d.s ON FALSE WHEN NOT MATCHED THEN INSERT VALUES (1)",
        "TRUNCATE TABLE d.t",
        "SELECT 1; SELECT 2",
        "DELETE FROM d.t WHERE TRUE",
        "INSERT INTO d.t VALUES (1)",
        "OPTIMIZE d.t",
        "VACUUM d.t",
        "COPY INTO d.t FROM '/mnt/x'",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad, dialect="databricks")


def test_databricks_unconfirmed_scan_never_executes(fake_databricks):
    # Family 2: the strict handshake. Without --confirm nothing runs on the
    # warehouse; estimation is free REST metadata, and the SQL session that
    # could wake the warehouse is never even opened.
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _dbx_adapter(fake_databricks, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0
    assert fake_databricks.connection.data_statements == []


def test_databricks_confirmed_run_without_a_ceiling_is_refused(fake_databricks):
    # Family 2: nothing executes unbudgeted; confirmation cannot stand in for
    # a ceiling on a billed paradigm.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _dbx_adapter(fake_databricks, ceiling=None, confirmed=True)
    with pytest.raises(CostGuardError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`customers`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0


def test_databricks_over_ceiling_cannot_be_confirmed_through(fake_databricks):
    # Family 2: over-ceiling blocks first, even fully confirmed (the floor
    # plus the wake charge exceeds a 2-second ceiling).
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _dbx_adapter(fake_databricks, ceiling=2.0, confirmed=True)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            "SELECT COUNT(*) FROM `shop`.`core`.`events`",
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_databricks.connect_count == 0


def test_databricks_every_executed_statement_is_server_capped(fake_databricks):
    # Family 2: defense in depth past the client-side gate; a wrong floor
    # cannot overrun the budget because STATEMENT_TIMEOUT kills the statement.
    fake_databricks.connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _dbx_adapter(fake_databricks)
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=200,
    )
    executed = fake_databricks.connection.data_statements
    assert executed
    assert all(s.session_timeout is not None for s in executed)


def test_query_firewall_blocks_databricks_value_carrying_shapes():
    # Family 3: PII stays flagged-not-surfaced under the databricks dialect,
    # including its own value-carrying aggregates and JSON casts.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT ANY_VALUE(email) FROM db.main.customers",
        "SELECT ARRAY_AGG(email) FROM db.main.customers",
        "SELECT COLLECT_LIST(email) FROM db.main.customers",
        "SELECT TO_JSON(struct(email)) FROM db.main.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits(), dialect="databricks")
    # Measuring stays allowed in the databricks dialect too.
    inspect_query(
        "SELECT COUNT(DISTINCT email) FROM db.main.customers",
        cache,
        QueryLimits(),
        dialect="databricks",
    )


def test_databricks_capabilities_pass_the_sanitizer(fake_databricks, capsys):
    # Family 5: the capabilities payload carries a coarse auth method, never
    # an identity or token, and survives the sanitizer end to end.
    adapter = _dbx_adapter(fake_databricks)
    caps = adapter.capabilities()
    env.emit(env.ok(caps))
    out = capsys.readouterr().out
    assert out
    assert "@" not in out  # no user identity
    assert caps["auth_method"].split(":")[0] in {
        "named_profile",
        "environment",
        "default_profile",
        "dbt_profile",
        "unknown",
    }


def test_databricks_spend_ledger_holds_no_sql_or_values(
    tmp_path: Path, fake_databricks
):
    # Family 5: the audit trail is second counts and statement hashes only.
    import json

    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_databricks.connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _dbx_adapter(fake_databricks)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        "SELECT COUNT(*) AS n FROM `shop`.`core`.`customers`",
        max_rows=10,
        timeout_seconds=200,
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert "SELECT" not in json.dumps(entry)
    assert entry["billed_seconds"] > 0
    assert entry["statement_sha256"]


# --- Postgres: the db-load connector exercises every family ---------------------
#
# These run against the fake connection (tests/fakes/postgres.py):
# deterministic, offline, free. They importorskip on the [postgres] extra,
# which CI and the release gate install, so trimming that extra from a
# workflow would silently skip release-blocking families; keep
# `--extra postgres` in ci.yml and release.yml.


def _pg_adapter(fake_pg_connection, *, ceiling=600.0, confirmed=True):
    from exmergo_dex_core.adapters.postgres import PostgresAdapter
    from exmergo_dex_core.config import PostgresTarget
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.DB_LOAD,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="postgres",
    )
    return PostgresAdapter(
        connection=fake_pg_connection,
        cost_gate=gate,
        target=PostgresTarget(),
        auth_method="database_url:password",
        clock=fake_pg_connection.clock,
    )


def test_postgres_generated_sql_is_select_only(fake_pg_connection):
    # Family 1: every data statement the adapter generates passes the
    # SELECT-only guard in the postgres dialect (asserted at build time).
    from exmergo_dex_core.adapters.base import (
        ColumnMeta,
        is_integer_type,
        is_string_type,
    )
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _pg_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    columns = [*columns, ColumnMeta("signup_ts", "TIMESTAMP", True, len(columns))]
    shape = {
        c.name
        for c in columns
        if "CHAR" in c.data_type.upper()
        or "STRING" in c.data_type.upper()
        or "TEXT" in c.data_type.upper()
    }
    type_req = {
        c.name
        for c in columns
        if is_string_type(c.data_type) or is_integer_type(c.data_type)
    }
    key_shape_req = {c.name for c in columns if is_string_type(c.data_type)}
    temporal_req = {"signup_ts"}
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers",
        columns,
        {"id"},
        shape,
        type_req,
        key_shape_req,
        temporal_req,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
    # ...with every cast in them total, so no dialect can raise on a
    # non-numeric string in a profiled column (#310).
    assert_every_cast_is_total(sql)
    assert "ks_uuid_" in sql and "ks_hex_" in sql
    # Temporal-continuity statistics (#206) ride the same statement too.
    assert "tc_da_" in sql and "tp_d_" in sql and "tg_h_" in sql
    assert "date_trunc" in sql and "EXTRACT(EPOCH" in sql
    assert assert_select_only(sql, dialect="postgres") == sql


def test_postgres_session_is_read_only_by_construction(fake_pg_connection):
    # Family 1: default_transaction_read_only is set before any statement, so
    # even a statement that slipped every guard would be refused server-side.
    adapter = _pg_adapter(fake_pg_connection)
    adapter.capabilities()
    first = fake_pg_connection.statements[0].sql.lower()
    assert "set default_transaction_read_only = on" in first


def test_select_only_guard_rejects_postgres_writes_ddl_and_copy():
    # Family 1: Postgres DML/DDL, COPY, and multi-statement forms are all
    # refused when parsed in the postgres dialect.
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "CREATE TABLE t AS SELECT 1",
        "TRUNCATE TABLE app.t",
        "SELECT 1; SELECT 2",
        "DELETE FROM app.t WHERE TRUE",
        "UPDATE app.t SET x = 1",
        "COPY app.t TO '/tmp/exfil.csv'",
        "ALTER TABLE app.t ADD COLUMN y text",
        "DROP TABLE app.t",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad, dialect="postgres")


def test_postgres_unconfirmed_scan_never_executes(fake_pg_connection):
    # Family 2: the strict handshake. Without --confirm nothing scans (the
    # estimate comes from the free planner, so there is nothing to load).
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _pg_adapter(fake_pg_connection, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."customers"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_pg_connection.data_statements == []


def test_postgres_confirmed_run_without_a_ceiling_is_refused(fake_pg_connection):
    # Family 2: nothing executes unbudgeted; confirmation cannot stand in for
    # a ceiling on a metered paradigm.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _pg_adapter(fake_pg_connection, ceiling=None, confirmed=True)
    with pytest.raises(CostGuardError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."customers"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_pg_connection.data_statements == []


def test_postgres_over_ceiling_cannot_be_confirmed_through(fake_pg_connection):
    # Family 2: over-ceiling blocks first, even fully confirmed.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _pg_adapter(fake_pg_connection, ceiling=2.0, confirmed=True)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."events"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_pg_connection.data_statements == []


def test_postgres_every_executed_statement_is_server_capped(fake_pg_connection):
    # Family 2: defense in depth past the client-side gate; a wrong heuristic
    # cannot overrun the budget because statement_timeout kills the statement.
    fake_pg_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _pg_adapter(fake_pg_connection)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "dexdb"."shop"."customers"',
        max_rows=10,
        timeout_seconds=200,
    )
    executed = fake_pg_connection.data_statements
    assert executed
    assert all(s.session_timeout_ms is not None for s in executed)


def test_query_firewall_blocks_postgres_value_carrying_shapes():
    # Family 3: PII stays flagged-not-surfaced under the postgres dialect,
    # including Postgres's own value-carrying aggregates and casts.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT STRING_AGG(email, ',') FROM db.main.customers",
        "SELECT ARRAY_AGG(email) FROM db.main.customers",
        "SELECT JSONB_AGG(email) FROM db.main.customers",
        "SELECT TO_JSON(email) FROM db.main.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits(), dialect="postgres")
    # Measuring stays allowed in the postgres dialect too.
    inspect_query(
        "SELECT COUNT(DISTINCT email) FROM db.main.customers",
        cache,
        QueryLimits(),
        dialect="postgres",
    )


def test_postgres_stats_reads_never_select_value_columns(fake_pg_connection):
    # Family 3: pg_stats is the planner's own statistics view and its
    # most_common_vals / histogram_bounds columns hold raw row values; the
    # adapter's stats reads must never touch them.
    fake_pg_connection.row_resolver = lambda sql: [
        {"n_total": 100, "nn_0": 100, "nn_1": 90, "nn_2": 80, "nn_3": 70}
    ]
    adapter = _pg_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    adapter.column_aggregates("dexdb.shop.customers", columns)
    stats_reads = [s.sql for s in fake_pg_connection.statements if "pg_stats" in s.sql]
    assert stats_reads
    for sql in stats_reads:
        assert "most_common_vals" not in sql
        assert "histogram_bounds" not in sql
        assert "most_common_elems" not in sql


def test_postgres_capabilities_pass_the_sanitizer(fake_pg_connection, capsys):
    # Family 5: the capabilities payload carries a coarse auth method, never
    # an identity, password, or DSN, and survives the sanitizer end to end.
    adapter = _pg_adapter(fake_pg_connection)
    caps = adapter.capabilities()
    env.emit(env.ok(caps))
    out = capsys.readouterr().out
    assert out
    assert "@" not in out  # no user identity or DSN
    assert caps["auth_method"].split(":")[0] in {
        "config_service",
        "database_url",
        "environment",
        "config_target",
        "dbt_profile",
        "unknown",
    }
    assert caps["auth_method"].split(":")[1] in {"password", "external", "service_file"}


def test_postgres_spend_ledger_holds_no_sql_or_values(
    tmp_path: Path, fake_pg_connection
):
    # Family 5: the audit trail is second counts and statement hashes only.
    import json

    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_pg_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _pg_adapter(fake_pg_connection)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "dexdb"."shop"."customers"',
        max_rows=10,
        timeout_seconds=200,
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert "SELECT" not in json.dumps(entry)
    assert entry["billed_seconds"] > 0
    assert entry["statement_sha256"]


# --- Redshift: the second compute-time connector exercises every family ---------
#
# These run against the fake connection (tests/fakes/redshift.py):
# deterministic, offline, free. They importorskip on the [redshift] extra,
# which CI and the release gate install, so trimming that extra from a
# workflow would silently skip release-blocking families; keep
# `--extra redshift` in ci.yml and release.yml.


def _redshift_adapter(fake_redshift_connection, *, ceiling=600.0, confirmed=True):
    from exmergo_dex_core.adapters.redshift import RedshiftAdapter
    from exmergo_dex_core.config import RedshiftTarget
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="redshift",
    )
    return RedshiftAdapter(
        connection=fake_redshift_connection,
        cost_gate=gate,
        target=RedshiftTarget(),
        compute={
            "kind": "serverless",
            "workgroup": "dex-wg",
            "base_capacity_rpus": 8.0,
        },
        auth_method="iam_serverless:default_chain",
        clock=fake_redshift_connection.clock,
    )


def test_redshift_generated_sql_is_select_only(fake_redshift_connection):
    # Family 1: every data statement the adapter generates passes the
    # SELECT-only guard in the redshift dialect (asserted at build time).
    from exmergo_dex_core.adapters.base import (
        ColumnMeta,
        is_integer_type,
        is_string_type,
    )
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _redshift_adapter(fake_redshift_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
    columns = [*columns, ColumnMeta("signup_ts", "TIMESTAMP", True, len(columns))]
    shape = {
        c.name
        for c in columns
        if "CHAR" in c.data_type.upper()
        or "STRING" in c.data_type.upper()
        or "TEXT" in c.data_type.upper()
    }
    type_req = {
        c.name
        for c in columns
        if is_string_type(c.data_type) or is_integer_type(c.data_type)
    }
    key_shape_req = {c.name for c in columns if is_string_type(c.data_type)}
    temporal_req = {"signup_ts"}
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers",
        columns,
        {"id"},
        shape,
        type_req,
        key_shape_req,
        temporal_req,
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
    # ...with every cast in them total, so no dialect can raise on a
    # non-numeric string in a profiled column (#310).
    assert_every_cast_is_total(sql)
    assert "ks_uuid_" in sql and "ks_hex_" in sql
    # Temporal-continuity statistics (#206) ride the same statement too.
    assert "tc_da_" in sql and "tp_d_" in sql and "tg_h_" in sql
    assert "DATE_TRUNC" in sql and "DATEDIFF" in sql
    assert assert_select_only(sql, dialect="redshift") == sql


def test_redshift_session_read_only_is_best_effort_and_honest(
    fake_redshift_connection,
):
    # Family 1: the session read-only mode is attempted before any statement;
    # when Redshift declines it, the adapter tolerates the refusal and
    # capabilities reports the truth rather than a comforting fiction.
    adapter = _redshift_adapter(fake_redshift_connection)
    adapter.capabilities()
    first = fake_redshift_connection.statements[0].sql.lower()
    assert "set default_transaction_read_only = on" in first

    from fakes.redshift import FakeRedshiftConnection

    declining = FakeRedshiftConnection(
        tables=fake_redshift_connection.tables, reject_read_only=True
    )
    declined = _redshift_adapter(declining)
    assert declined.capabilities()["session_read_only"] is False


def test_select_only_guard_rejects_redshift_writes_ddl_and_movement():
    # Family 2 of the dialect surface: Redshift DML/DDL, data movement
    # (COPY/UNLOAD), and multi-statement forms are all refused when parsed in
    # the redshift dialect.
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError, assert_select_only

    for bad in (
        "CREATE TABLE t AS SELECT 1",
        "TRUNCATE TABLE shop.t",
        "SELECT 1; SELECT 2",
        "DELETE FROM shop.t WHERE TRUE",
        "UPDATE shop.t SET x = 1",
        "COPY shop.t FROM 's3://bucket/exfil' IAM_ROLE 'arn:aws:iam::1:role/r'",
        "UNLOAD ('SELECT * FROM shop.t') TO 's3://bucket/exfil'",
        "ALTER TABLE shop.t ADD COLUMN y varchar",
        "DROP TABLE shop.t",
    ):
        with pytest.raises(NotSelectOnlyError):
            assert_select_only(bad, dialect="redshift")


def test_redshift_unconfirmed_scan_never_executes(fake_redshift_connection):
    # Family 2: the strict handshake. Without --confirm nothing scans.
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _redshift_adapter(fake_redshift_connection, confirmed=False)
    with pytest.raises(ConfirmationRequiredError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."customers"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_redshift_connection.data_statements == []


def test_redshift_confirmed_run_without_a_ceiling_is_refused(fake_redshift_connection):
    # Family 2: nothing executes unbudgeted; confirmation cannot stand in for
    # a ceiling on a billed paradigm.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _redshift_adapter(fake_redshift_connection, ceiling=None, confirmed=True)
    with pytest.raises(CostGuardError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."customers"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_redshift_connection.data_statements == []


def test_redshift_over_ceiling_cannot_be_confirmed_through(fake_redshift_connection):
    # Family 2: over-ceiling blocks first, even fully confirmed.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _redshift_adapter(fake_redshift_connection, ceiling=2.0, confirmed=True)
    with pytest.raises(OverCeilingError):
        adapter.run_query(
            'SELECT COUNT(*) FROM "dexdb"."shop"."events"',
            max_rows=10,
            timeout_seconds=30,
        )
    assert fake_redshift_connection.data_statements == []


def test_redshift_every_executed_statement_is_server_capped(fake_redshift_connection):
    # Family 2: defense in depth past the client-side gate; a wrong heuristic
    # cannot overrun the budget because statement_timeout kills the statement.
    fake_redshift_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _redshift_adapter(fake_redshift_connection)
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "dexdb"."shop"."customers"',
        max_rows=10,
        timeout_seconds=200,
    )
    executed = fake_redshift_connection.data_statements
    assert executed
    assert all(s.session_timeout_ms is not None for s in executed)


def test_query_firewall_blocks_redshift_value_carrying_shapes():
    # Family 3: PII stays flagged-not-surfaced under the redshift dialect,
    # including Redshift's own value-carrying aggregates.
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import (
        QueryRefusedError,
        inspect_query,
    )

    cache = _firewall_cache()
    for bad in (
        "SELECT LISTAGG(email, ',') FROM db.main.customers",
        "SELECT MIN(email) FROM db.main.customers",
        "SELECT ANY_VALUE(email) FROM db.main.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(bad, cache, QueryLimits(), dialect="redshift")
    # Measuring stays allowed in the redshift dialect too.
    inspect_query(
        "SELECT COUNT(DISTINCT email) FROM db.main.customers",
        cache,
        QueryLimits(),
        dialect="redshift",
    )


def test_init_redshift_profile_is_dev_only_with_no_secrets(tmp_path: Path, monkeypatch):
    # Family 4: the generated Redshift profile has a single dev target and, on
    # the IAM path, no secret-shaped key anywhere (temporary credentials are
    # minted by the dbt adapter at runtime).
    import yaml

    from exmergo_dex_core import transform
    from exmergo_dex_core.config import CONFIG_FILE
    from exmergo_dex_core.storage import DEX_DIR

    class _Client:
        def get_workgroup(self, workgroupName):  # noqa: N803 (boto3's spelling)
            return {
                "workgroup": {
                    "workgroupName": workgroupName,
                    "namespaceName": "ns",
                    "status": "AVAILABLE",
                    "baseCapacity": 8,
                    "endpoint": {
                        "address": "wg.1.eu.redshift-serverless.amazonaws.com",
                        "port": 5439,
                    },
                }
            }

        def get_namespace(self, namespaceName):  # noqa: N803 (boto3's spelling)
            return {"namespace": {"namespaceName": namespaceName, "dbName": "shop"}}

    class _Session:
        def __init__(self, **kwargs):
            pass

        def client(self, service):
            return _Client()

    import boto3

    monkeypatch.setattr(boto3, "Session", _Session)
    (tmp_path / DEX_DIR).mkdir()
    (tmp_path / DEX_DIR / CONFIG_FILE).write_text(
        "redshift:\n  workgroup: dex-wg\n", encoding="utf-8"
    )
    transform.init_project("analytics", "redshift", repo_root=tmp_path)
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    profile = profiles["analytics"]
    assert profile["target"] == "dev"
    assert set(profile["outputs"]) == {"dev"}
    assert profile["outputs"]["dev"]["method"] == "iam"
    # The envelope sanitizer doubles as the secret-key scanner here.
    env.sanitize(env.ok(profiles))


def test_redshift_capabilities_pass_the_sanitizer(fake_redshift_connection, capsys):
    # Family 5: the capabilities payload carries a coarse auth method, never
    # an identity, key, or password, and survives the sanitizer end to end.
    adapter = _redshift_adapter(fake_redshift_connection)
    caps = adapter.capabilities()
    env.emit(env.ok(caps))
    out = capsys.readouterr().out
    assert out
    assert "@" not in out  # no user identity
    assert caps["auth_method"].split(":")[0] in {
        "iam_serverless",
        "iam_cluster",
        "environment",
        "config_target",
        "dbt_profile",
        "unknown",
    }
    assert caps["auth_method"].split(":")[1] in {
        "profile",
        "environment",
        "default_chain",
        "password",
        "external",
        "unknown",
    }


def test_redshift_spend_ledger_holds_no_sql_or_values(
    tmp_path: Path, fake_redshift_connection
):
    # Family 5: the audit trail is second counts and statement hashes only.
    import json

    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_redshift_connection.row_resolver = lambda sql: [{"n": 1}]
    adapter = _redshift_adapter(fake_redshift_connection)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "dexdb"."shop"."customers"',
        max_rows=10,
        timeout_seconds=200,
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert "SELECT" not in json.dumps(entry)
    assert entry["billed_seconds"] > 0
    assert entry["statement_sha256"]


# --- ClickHouse: the second db-load connector exercises every family ------------
#
# These run against the fake client (tests/fakes/clickhouse.py): deterministic,
# offline, free. They importorskip on the [clickhouse] extra, which CI and the
# release gate install, so trimming that extra from a workflow would silently
# skip release-blocking families; keep `--extra clickhouse` in ci.yml and
# release.yml.
#
# Two of these guard hazards that are specific to this engine and fail *silently*
# rather than loudly, which is the failure mode a green suite is worst at
# catching: ClickHouse fills an unmatched LEFT JOIN row with the column type's
# default instead of NULL unless told otherwise, and it has no LAG at all.


def _clickhouse_adapter(fake_clickhouse_connection, *, ceiling=600.0, confirmed=True):
    from exmergo_dex_core.adapters.clickhouse import ClickHouseAdapter
    from exmergo_dex_core.config import ClickHouseTarget
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=env.Paradigm.DB_LOAD,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=confirmed,
        connector="clickhouse",
    )
    return ClickHouseAdapter(
        connection=fake_clickhouse_connection,
        cost_gate=gate,
        target=ClickHouseTarget(),
        auth_method="environment:password",
    )


def test_clickhouse_generated_sql_is_select_only(fake_clickhouse_connection):
    # Family 1: every data statement the adapter generates passes the
    # SELECT-only guard in its own dialect.
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.customers")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.customers",
        columns,
        {"created_at"},
        {"email"},
        {"email"},
        {"email"},
        {"created_at"},
    )
    assert_select_only(sql, dialect="clickhouse")
    for prefix in ("nn_", "d_", "su_", "sp_", "st_", "tp_d_", "tg_d_"):
        assert prefix in sql


def test_clickhouse_session_is_read_only_on_every_statement(
    fake_clickhouse_connection,
):
    # Family 1: read-only is not a property of how the client was built. The
    # settings ride each statement, so a host-supplied client cannot lose them.
    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    adapter.capabilities()
    _meta, columns = adapter.table_metadata("shop.customers")
    adapter.column_aggregates("shop.customers", columns)
    assert fake_clickhouse_connection.queries
    for query in fake_clickhouse_connection.queries:
        assert query.settings["readonly"] == 2
        assert query.settings["allow_ddl"] == 0


def test_clickhouse_orphan_probes_can_actually_find_orphans(
    fake_clickhouse_connection,
):
    # Family 1, and the sharpest ClickHouse-specific hazard in the connector.
    #
    # ClickHouse defaults join_use_nulls to 0, which fills an unmatched LEFT
    # JOIN row with the column type's default (0, '') rather than NULL. The
    # shared relationship overlap probe counts orphans with `IS NULL`, so with
    # the default every inferred join reports perfectly clean and maintain
    # grain's join-fanout half never fires. Measured live: 0 orphans under the
    # default, 40 with the setting, against a table seeded with exactly 40.
    #
    # This is a control that looks active while doing nothing, so it gets a
    # spine assertion rather than a unit test.
    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    adapter.capabilities()
    _meta, columns = adapter.table_metadata("shop.customers")
    adapter.column_aggregates("shop.customers", columns)
    assert fake_clickhouse_connection.queries
    for query in fake_clickhouse_connection.queries:
        assert query.settings["join_use_nulls"] == 1, (
            "without join_use_nulls=1 an unmatched LEFT JOIN row yields the "
            "type default instead of NULL, and every orphan probe reports zero"
        )


def test_clickhouse_temporal_gaps_are_measurable_at_all(fake_clickhouse_connection):
    # Family 1's honesty half: a detector that structurally cannot fire is
    # worse than an absent one, because a clean result reads as a pass.
    #
    # ClickHouse has no LAG. lagInFrame returns the *type default* rather than
    # NULL past the frame edge, so the naive rewrite makes the first row compare
    # against the epoch and report a ~20,000 day gap on every column, and it
    # respects the window frame, so it needs the explicit full frame. Measured
    # live against a column with a known 3-day hole: 20590 without both
    # corrections, 3 with them, which is what DuckDB reports for the same data.
    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    _meta, columns = adapter.table_metadata("shop.events")
    occurred = next(c for c in columns if c.name == "occurred_at")
    sql, _plan = adapter._build_aggregate_sql(
        "shop.events", [occurred], set(), set(), set(), set(), {"occurred_at"}
    )
    assert "lagInFrame(toNullable(period), 1, NULL)" in sql
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING" in sql


def test_select_only_guard_rejects_clickhouse_writes_ddl_and_movement(
    fake_clickhouse_connection,
):
    # Family 1: the guard refuses in this dialect, not merely in duckdb.
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    for sql in (
        "INSERT INTO shop.customers VALUES (1)",
        "ALTER TABLE shop.customers DROP COLUMN email",
        "DROP TABLE shop.customers",
        "TRUNCATE TABLE shop.customers",
        "OPTIMIZE TABLE shop.customers FINAL",
        "CREATE TABLE t (a UInt8) ENGINE = Memory",
        "SELECT 1; DROP TABLE shop.customers",
    ):
        with pytest.raises(Exception):
            assert_select_only(sql, dialect="clickhouse")


def test_clickhouse_unconfirmed_scan_never_executes(fake_clickhouse_connection):
    # Family 2: the gate binds before anything reaches the server.
    from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError

    adapter = _clickhouse_adapter(fake_clickhouse_connection, confirmed=False)
    _meta, columns = adapter.table_metadata("shop.customers")
    with pytest.raises(ConfirmationRequiredError):
        adapter.column_aggregates("shop.customers", columns)
    assert fake_clickhouse_connection.data_queries == []


def test_clickhouse_confirmed_run_without_a_ceiling_is_refused(
    fake_clickhouse_connection,
):
    # Family 2: nothing executes unbudgeted.
    from exmergo_dex_core.guards.cost_guard import CostGuardError

    adapter = _clickhouse_adapter(fake_clickhouse_connection, ceiling=None)
    _meta, columns = adapter.table_metadata("shop.customers")
    with pytest.raises(CostGuardError):
        adapter.column_aggregates("shop.customers", columns)
    assert fake_clickhouse_connection.data_queries == []


def test_clickhouse_over_ceiling_cannot_be_confirmed_through(
    fake_clickhouse_connection,
):
    # Family 2: an over-ceiling estimate refuses first, and confirmation
    # cannot override it.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    adapter = _clickhouse_adapter(
        fake_clickhouse_connection, ceiling=0.001, confirmed=True
    )
    _meta, columns = adapter.table_metadata("shop.events")
    with pytest.raises(OverCeilingError):
        adapter.column_aggregates("shop.events", columns)
    assert fake_clickhouse_connection.data_queries == []


def test_clickhouse_every_executed_statement_is_server_capped(
    fake_clickhouse_connection,
):
    # Family 2: the layer that binds when the estimate is wrong. Both caps are
    # asserted because max_execution_time is checked at block boundaries and a
    # single fast block can overshoot it; the byte cap is what binds on a scan.
    adapter = _clickhouse_adapter(fake_clickhouse_connection, ceiling=600.0)
    _meta, columns = adapter.table_metadata("shop.customers")
    adapter.column_aggregates("shop.customers", columns)
    billed = fake_clickhouse_connection.data_queries
    assert billed
    for query in billed:
        assert query.settings["max_execution_time"] > 0
        assert query.settings["timeout_overflow_mode"] == "throw"
        assert query.settings["max_bytes_to_read"] > 0
        assert query.settings["read_overflow_mode"] == "throw"


def test_query_firewall_blocks_clickhouse_value_carrying_shapes(
    fake_clickhouse_connection,
):
    # Family 3: PII is flagged and never surfaced, in this dialect's idioms.
    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        PIICategory,
        PIIFlag,
    )
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.guards.query_firewall import QueryRefusedError, inspect_query

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="shop.customers",
                object_type="table",
                row_count=100,
                columns=[
                    ColumnProfile(name="id", data_type="UInt64"),
                    ColumnProfile(
                        name="email",
                        data_type="String",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
                    ),
                    ColumnProfile(name="tags", data_type="Array(String)"),
                ],
            )
        ]
    )
    limits = QueryLimits()

    # Carrying shapes stay refused, including the ones ClickHouse spells its own
    # way: an -If combinator over a value-returning aggregate is still value
    # returning, and ARRAY JOIN reshapes but cannot launder.
    for sql in (
        "SELECT email FROM shop.customers",
        "SELECT max(email) FROM shop.customers",
        "SELECT anyIf(email, id > 1) FROM shop.customers",
        "SELECT groupArray(email) FROM shop.customers",
        "SELECT e FROM shop.customers ARRAY JOIN splitByChar(',', email) AS e",
        "SELECT email LIKE 'a%' FROM shop.customers",
    ):
        with pytest.raises(QueryRefusedError):
            inspect_query(sql, cache, limits, dialect="clickhouse")

    # Measuring shapes still pass, so the gate is a taint rule rather than a
    # column blocklist. Without this half the refusals above would also hold on
    # a firewall that refused everything.
    for sql in (
        "SELECT count() FROM shop.customers",
        "SELECT uniqExact(email) FROM shop.customers",
        "SELECT countIf(id > 1) FROM shop.customers",
        "SELECT id, count() FROM shop.customers FINAL GROUP BY id",
        "SELECT tag, count() FROM shop.customers ARRAY JOIN tags AS tag GROUP BY tag",
    ):
        inspect_query(sql, cache, limits, dialect="clickhouse")


def test_init_clickhouse_profile_is_dev_only_with_no_secrets(tmp_path, monkeypatch):
    # Family 4 and 5: the rendered profile writes only to the dev database and
    # carries no credential value.
    import yaml as _yaml

    from exmergo_dex_core.config import ClickHouseTarget, DexConfig
    from exmergo_dex_core.transform.init import _clickhouse_profile

    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.internal")
    monkeypatch.setenv("CLICKHOUSE_USER", "dbt_dev")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "super-secret")
    config = DexConfig(
        connector="clickhouse",
        clickhouse=ClickHouseTarget(databases=["app"], dev_database="dbt_dev"),
    )
    rendered = _clickhouse_profile("proj", None, config, tmp_path)

    assert "super-secret" not in rendered
    assert "env_var" in rendered
    output = _yaml.safe_load(rendered)["proj"]["outputs"]["dev"]
    assert output["type"] == "clickhouse"
    # dbt-clickhouse has no `database` key: its `schema` is the ClickHouse
    # database, and it must be the dev one, never a source.
    assert output["schema"] == "dbt_dev"
    assert "app" not in str(output["schema"])
    assert output["password"].startswith("{{ env_var(")

    # The cap references have to survive the yaml round trip intact. An f-string
    # would silently collapse the closing `}}` to one brace, rendering a profile
    # that parses, applies, and caps nothing.
    settings = output["custom_settings"]
    assert settings["max_execution_time"].endswith(") }}")
    assert settings["max_bytes_to_read"].endswith(") }}")
    assert settings["timeout_overflow_mode"] == "throw"


def test_clickhouse_capabilities_pass_the_sanitizer(fake_clickhouse_connection):
    # Family 5: nothing in the free probe trips the secret-key scan.
    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    env.sanitize(env.ok(adapter.capabilities()))


def test_clickhouse_spend_ledger_holds_no_sql_or_values(
    tmp_path, fake_clickhouse_connection
):
    # Family 5: the ledger records a hash and a number, never statement text.
    import json as _json

    from fakes.clickhouse import FakeResult

    from exmergo_dex_core.storage.filesystem import FilesystemStore

    store = FilesystemStore(tmp_path)
    fake_clickhouse_connection.row_resolver = lambda sql: FakeResult(
        rows=[{"n": 1}], seconds=0.4
    )
    adapter = _clickhouse_adapter(fake_clickhouse_connection)
    adapter.cost_gate._record = store.append_spend_log
    adapter.run_query(
        'SELECT COUNT(*) AS n FROM "shop"."customers"', max_rows=10, timeout_seconds=200
    )
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = _json.loads(lines[-1])
    assert "SELECT" not in _json.dumps(entry)
    assert entry["billed_seconds"] > 0
    assert entry["statement_sha256"]


# --- Maintain: drift detection and reconcile exercise every family -------------
#
# Detection is read-only against data and writes only to `.dex/`; only reconcile
# emits diffs, and those apply through the transform conflict handshake. These
# assertions guard those invariants on the DuckDB loop, where they are free.


def _maintain_setup(tmp_path: Path, capsys) -> tuple[Path, Path]:
    """A DuckDB warehouse (with a PII column and a key) plus a dbt project,
    mapped and snapshotted: the baseline the maintain families detect against."""

    import duckdb

    from exmergo_dex_core.cli import main

    root = tmp_path / "repo"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE customers (id INTEGER, email VARCHAR, status VARCHAR)")
    conn.execute(
        "INSERT INTO customers SELECT i, 'user' || i || '@example.com', "
        "(['active','churned'])[(i % 2) + 1] FROM range(1, 31) t(i)"
    )
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )
    (root / "models" / "staging").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        'name: spine_test\nversion: "1.0.0"\nprofile: spine_test\n'
        'model-paths: ["models"]\n',
        encoding="utf-8",
    )
    (root / "profiles.yml").write_text(
        "spine_test:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (root / "models" / "staging" / "_dex_sources.yml").write_text(
        "version: 2\nsources:\n  - name: main\n    schema: main\n    tables:\n"
        "      - name: customers\n        columns:\n"
        "          - name: id\n          - name: email\n          - name: status\n",
        encoding="utf-8",
    )
    assert main(["--repo-root", str(root), "explore", "map"]) == 0
    assert main(["--repo-root", str(root), "maintain", "snapshot"]) == 0
    capsys.readouterr()  # drain the setup commands' stdout
    return root, db_path


def _run(argv: list[str], capsys) -> dict:
    import json

    from exmergo_dex_core.cli import main

    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    payload = json.loads(out)
    assert rc in (0, 1)
    return payload


def test_maintain_detection_leaves_the_warehouse_read_only(tmp_path: Path, capsys):
    # Family 1: detection never mutates the warehouse. The DuckDB file is
    # byte-identical after a full check that scans it (grain runs aggregates).
    import hashlib

    root, db_path = _maintain_setup(tmp_path, capsys)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    _run(["--repo-root", str(root), "maintain", "check"], capsys)
    _run(["--repo-root", str(root), "maintain", "grain"], capsys)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_maintain_grain_findings_carry_no_example_values(tmp_path: Path, capsys):
    # Family 3: grain drift is established from aggregates; the finding reports
    # counts, never the duplicated key values or any PII.
    import duckdb

    from exmergo_dex_core.maintain.drift import DriftFinding

    # Structural: a finding has no field that could hold a row value.
    assert "value" not in DriftFinding.model_fields
    assert "values" not in DriftFinding.model_fields

    root, db_path = _maintain_setup(tmp_path, capsys)
    conn = duckdb.connect(str(db_path))
    conn.execute("INSERT INTO customers SELECT id, email, status FROM customers")
    conn.close()
    payload = _run(["--repo-root", str(root), "maintain", "check"], capsys)
    dumped = __import__("json").dumps(payload)
    assert "@example.com" not in dumped  # no PII value ever
    grain = [f for f in payload["data"]["findings"] if f["axis"] == "grain"]
    assert grain and all(
        set(f["data"]) <= {"distinct_count", "row_count", "was_grain"}
        or f["code"] != "key_lost_uniqueness"
        for f in grain
    )


def test_maintain_cardinality_reports_counts_not_the_new_value(tmp_path: Path, capsys):
    # Family 3: a widened categorical dimension is a count delta; the new value
    # itself never crosses the envelope.
    import duckdb
    import yaml

    root, db_path = _maintain_setup(tmp_path, capsys)
    (root / "models" / "staging" / "customers_semantic.yml").write_text(
        yaml.safe_dump(
            {
                "semantic_models": [
                    {
                        "name": "customers",
                        "model": "ref('customers')",
                        "entities": [{"name": "id", "type": "primary"}],
                        "dimensions": [{"name": "status", "type": "categorical"}],
                        "measures": [
                            {"name": "customer_count", "agg": "count", "expr": "id"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _run(["--repo-root", str(root), "explore", "map"], capsys)
    _run(["--repo-root", str(root), "maintain", "snapshot"], capsys)
    conn = duckdb.connect(str(db_path))
    conn.execute("INSERT INTO customers VALUES (999, 'x@example.com', 'refunded')")
    conn.close()
    payload = _run(["--repo-root", str(root), "maintain", "semantic"], capsys)
    assert "refunded" not in __import__("json").dumps(payload)
    card = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == "dimension_cardinality_changed"
    ]
    assert card and card[0]["data"]["distinct_after"] == 3


def test_maintain_reconcile_writes_nothing_to_the_project(tmp_path: Path, capsys):
    # Family 4: reconcile proposes a plan of diffs and touches no project file;
    # applying is a separate, hash-checked step.
    import hashlib

    import duckdb

    def tree(root: Path) -> dict[str, str]:
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "models").rglob("*"))
            if p.is_file()
        }

    root, db_path = _maintain_setup(tmp_path, capsys)
    before = tree(root)
    conn = duckdb.connect(str(db_path))
    conn.execute("ALTER TABLE customers ADD COLUMN phone VARCHAR")
    conn.close()
    _run(["--repo-root", str(root), "maintain", "check"], capsys)
    payload = _run(["--repo-root", str(root), "maintain", "reconcile"], capsys)
    assert payload["status"] == "ok"
    # Proposals and diffs exist as proposals only; the model tree is unchanged.
    assert tree(root) == before


def test_maintain_envelopes_pass_the_sanitizer(tmp_path: Path, capsys):
    # Family 5: every maintain command's payload survives env.emit's sanitizer
    # (it runs inside main), so no secret-like key or raw-row shape leaks.
    root, _db_path = _maintain_setup(tmp_path, capsys)
    for argv in (
        ["maintain", "snapshot"],
        ["maintain", "check"],
        ["maintain", "schema"],
        ["maintain", "reconcile"],
    ):
        payload = _run(["--repo-root", str(root), *argv], capsys)
        assert payload["status"] in {"ok", "needs_confirmation"}


# --- Family 5 + cost honesty: the hosted semantic-layer backend --------------
#
# The hosted dbt Cloud Semantic Layer executes server-side under its own warehouse
# credential, so dex's cost guard is structurally unavailable there. Two invariants
# guard that seam: the service token never crosses the stdout boundary, and the
# backend is honest about the missing guard rather than faking one.


def test_hosted_semantic_token_never_crosses_the_boundary():
    import json

    from fakes.semantic import SECRET_TOKEN, FakeHostedBackend, table_json_result

    from exmergo_dex_core.explore.semantic import SemanticQuery

    backend = FakeHostedBackend(
        result=table_json_result(["sessions"], ["string"], [[5.0]])
    )
    envelope = backend.query(SemanticQuery(metrics=["sessions"]))
    env.sanitize(envelope)  # a secret-like key would hard-fail here
    assert SECRET_TOKEN not in json.dumps(envelope.model_dump(mode="json"))


def test_hosted_semantic_is_warn_only_never_silently_priced():
    from fakes.semantic import FakeHostedBackend, table_json_result

    from exmergo_dex_core.explore.semantic import SemanticQuery

    backend = FakeHostedBackend(
        result=table_json_result(["sessions"], ["string"], [[5.0]])
    )
    result = backend.query(SemanticQuery(metrics=["sessions"]))
    # answered WITHOUT any confirmation (dex governs nothing on this path)...
    assert result.row_count == 1
    # ...but it never pretends to have priced or bounded the spend...
    assert result.cost.paradigm == env.Paradigm.HOSTED
    assert result.cost.estimate is None and result.cost.ceiling is None
    # ...and it says so, loudly, on every result, all the way to stdout.
    assert any("cost guard unavailable" in w for w in result.warnings)
    envelope = to_envelope(result)
    assert envelope.status == env.Status.OK
    assert any("cost guard unavailable" in w for w in envelope.warnings)


def test_local_semantic_pii_evidence_blocks_an_innocent_looking_dimension(
    tmp_path: Path,
):
    # Family 3: on the local backend the .dex cache's value-evidence flags decide,
    # so a dimension whose NAME reads clean is still refused when the physical
    # column it resolves to is flagged. The name heuristic alone would allow it.
    import json as _json

    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        PIICategory,
        PIIFlag,
    )
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.explore.semantic import screen_dimension_refs
    from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend

    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    (project / "target" / "semantic_manifest.json").write_text(
        _json.dumps(
            {
                "semantic_models": [
                    {
                        "name": "orders",
                        "node_relation": {
                            "alias": "orders",
                            "relation_name": "wh.main.orders",
                        },
                        "entities": [{"name": "order", "type": "primary"}],
                        "dimensions": [
                            {
                                "name": "contact",
                                "type": "categorical",
                                "expr": "contact_col",
                            }
                        ],
                        "measures": [{"name": "order_count", "agg": "count"}],
                    }
                ],
                "metrics": [],
            }
        )
    )
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.orders",
                columns=[
                    ColumnProfile(
                        name="contact_col",
                        data_type="VARCHAR",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
                    )
                ],
            )
        ]
    )
    # The project format is injected, because the gate resolves a dimension to its
    # physical column through the project seam now rather than by parsing the
    # compiled artifact itself. The seam is what must keep the evidence flowing.
    from exmergo_dex_core.adapters.project import DbtProject

    backend = LocalMetricFlowBackend(
        project,
        _memory_engine(),
        "duckdb",
        QueryLimits(),
        DbtProject(project.parent, project),
    )
    lookup = backend._cache_pii_lookup(cache)
    assert dict(screen_dimension_refs(["order__contact"], meta_lookup=lookup))


def test_local_semantic_pii_evidence_follows_a_join_resolved_dimension(
    tmp_path: Path,
):
    # Family 3, on the tokens the join resolution added. A metric can be grouped by
    # a dimension declared in a model it joins to, and resolving those paths puts
    # tokens in the catalog that a caller can now name. A token the gate cannot
    # resolve to a physical column is screened on its name alone, which is the
    # fail-closed floor rather than an equivalent, so every path the resolution
    # adds has to reach the same evidence a declared token reaches.
    import json as _json

    from exmergo_dex_core import dbt_project
    from exmergo_dex_core.adapters.project import DbtProject
    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        PIICategory,
        PIIFlag,
    )
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.explore.semantic import screen_dimension_refs
    from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend

    project = tmp_path / "joined"
    (project / "target").mkdir(parents=True)
    (project / "target" / "semantic_manifest.json").write_text(
        _json.dumps(
            {
                "semantic_models": [
                    {
                        "name": "users",
                        "node_relation": {
                            "alias": "dim_users",
                            "relation_name": "wh.main.dim_users",
                        },
                        "entities": [{"name": "user", "type": "primary"}],
                        "dimensions": [
                            {
                                "name": "contact",
                                "type": "categorical",
                                "expr": "contact_col",
                            }
                        ],
                        "measures": [],
                    },
                    {
                        "name": "sessions",
                        "node_relation": {
                            "alias": "fct_sessions",
                            "relation_name": "wh.main.fct_sessions",
                        },
                        "entities": [
                            {"name": "session", "type": "primary"},
                            {"name": "user", "type": "foreign"},
                        ],
                        "dimensions": [],
                        "measures": [{"name": "session_count", "agg": "count"}],
                    },
                ],
                "metrics": [
                    {
                        "name": "sessions",
                        "type": "simple",
                        "type_params": {"input_measures": [{"name": "session_count"}]},
                    }
                ],
            }
        )
    )

    def resolve(_manifest_text):
        # The resolver's answer, stated here rather than asked of MetricFlow: this
        # suite installs no [semantic] extra, and the claim under test is what the
        # gate does with a resolved path, not how the path was resolved.
        return {
            "sessions": [
                dbt_project.ResolvedPath(
                    "session__user__contact", "contact", "users", "categorical"
                )
            ]
        }

    class _Layer(DbtProject):
        def semantic_catalog(self):
            return dbt_project.semantic_catalog(self.project_dir, resolve_paths=resolve)

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.dim_users",
                columns=[
                    ColumnProfile(
                        name="contact_col",
                        data_type="VARCHAR",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
                    )
                ],
            )
        ]
    )
    backend = LocalMetricFlowBackend(
        project,
        _memory_engine(),
        "duckdb",
        QueryLimits(),
        _Layer(project.parent, project),
    )
    # The name says nothing: `session__user__contact` matches no PII pattern, so the
    # column's own evidence is the only thing that refuses this query.
    assert not screen_dimension_refs(["session__user__contact"])
    lookup = backend._cache_pii_lookup(cache)
    assert dict(screen_dimension_refs(["session__user__contact"], meta_lookup=lookup))


def test_local_semantic_refuses_a_foreign_namespace_before_spending(tmp_path: Path):
    # Family 2 + 1: rendered metric SQL bakes in the relations the project was
    # compiled against. Reading a namespace this connection does not have is
    # refused BEFORE the cost handshake, so a mismatch never bills a failed job.
    # The connection's own inventory is the authority, so the refusal holds with a
    # profiling cache and without one; what it must never do is refuse a relation
    # that is in the warehouse and merely absent from that cache.
    from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache
    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend

    backend = LocalMetricFlowBackend(
        tmp_path, _memory_engine(), "duckdb", QueryLimits()
    )
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.orders",
                columns=[ColumnProfile(name="status", data_type="VARCHAR")],
            )
        ]
    )
    live = ["wh.main.orders", "wh.staging.customers"]
    for held in (cache, None):
        verdict, _unprofiled = backend._relation_precheck(
            "SELECT status FROM other_db.main.orders", held, "duckdb", lambda: live
        )
        assert verdict is not None and "different namespace" in verdict
    # and the relation the connection does carry is not collateral damage
    assert (
        backend._relation_precheck(
            "SELECT status FROM wh.main.orders", None, "duckdb", lambda: live
        )[0]
        is None
    )


def test_hosted_semantic_pii_dimension_refused_not_surfaced():
    from fakes.semantic import FakeHostedBackend

    from exmergo_dex_core.explore.semantic import (
        SemanticQuery,
        SemanticQueryRefusedError,
    )

    backend = FakeHostedBackend()
    with pytest.raises(SemanticQueryRefusedError, match="PII"):
        backend.query(SemanticQuery(metrics=["sessions"], group_by=["user__email"]))
    # refused before the query was ever submitted for execution
    assert not any("createQuery" in posted for posted in backend.posted)


def test_the_hosted_pii_map_covers_every_metric_in_a_multi_metric_query():
    # Family 3, and the reason the gate asks one metric at a time. The API's
    # `dimensions(metrics: [a, b])` returns the dimensions common to ALL the
    # listed metrics, not their union, so asking once for a multi-metric query
    # shrinks the authoritative map instead of growing it: everything outside the
    # intersection falls through to the name heuristic. A dimension the dbt
    # project marked `meta: {pii: true}` whose name carries no PII signal then
    # passes the gate and is grouped and projected. "PII is flagged, never
    # surfaced" does not survive that, and a note after the fact is not the same
    # as a refusal.
    from fakes.semantic import FakeHostedBackend

    from exmergo_dex_core.explore.semantic import (
        SemanticQuery,
        SemanticQueryRefusedError,
        screen_dimension_refs,
    )

    clean = {"name": "user__pricing_tier", "config": {"meta": {}}}
    flagged = {"name": "agent__operator_handle", "config": {"meta": {"pii": True}}}
    # Two metrics from two semantic models: the flagged dimension is reachable
    # from one of them, so it is exactly what an intersection drops.
    backend = FakeHostedBackend(
        dimensions_meta={
            "active_users": [clean],
            "agent_runs": [clean, flagged],
        }
    )
    # Nothing about the name gives it away, which is what makes the layer's own
    # metadata the only thing standing between this dimension and stdout.
    assert not screen_dimension_refs(["agent__operator_handle"])

    with pytest.raises(SemanticQueryRefusedError, match="PII"):
        backend.query(
            SemanticQuery(
                metrics=["active_users", "agent_runs"],
                group_by=["agent__operator_handle"],
            )
        )
    assert not any("createQuery" in posted for posted in backend.posted)


def test_the_hosted_pii_map_adjudicates_rather_than_disclosing_a_gap():
    # The other half of the same invariant. Blocking is not enough: a dimension
    # the layer documented must be *adjudicated*, not cleared by the heuristic and
    # then disclosed as unscreened, because a note is the part of a payload a
    # caller is least likely to act on. So a ref the layer speaks to leaves
    # nothing unadjudicated, and a grain suffix (which no dimension name carries)
    # is not enough on its own to drop a ref back to the floor.
    from fakes.semantic import FakeHostedBackend, table_json_result

    from exmergo_dex_core.explore.semantic import SemanticQuery, unadjudicated_refs

    dims = {
        "active_users": [{"name": "user__pricing_tier", "config": {"meta": {}}}],
        "agent_runs": [
            {"name": "agent__mode", "config": {"meta": {"pii": False}}},
            {"name": "user__created_at", "config": {"meta": {"pii": False}}},
        ],
    }
    backend = FakeHostedBackend(
        dimensions_meta=dims,
        result=table_json_result(["active_users"], ["number"], [[5.0]]),
    )
    query = SemanticQuery(
        metrics=["active_users", "agent_runs"],
        group_by=["agent__mode", "user__created_at__month"],
    )
    meta, _ = backend._query_metadata(query.metrics)
    lookup = backend._meta_lookup(meta)
    assert unadjudicated_refs(query.group_by, meta_lookup=lookup) == []

    result = backend.query(query)
    assert not any("name heuristic alone" in note for note in result.notes)


def test_a_filter_a_backend_cannot_read_is_refused_rather_than_half_screened():
    # Family 3, and the gate's other structural fail-open. A metric query touches
    # dimensions two ways: the group_by tokens and the dimensions its filter
    # clauses name. The filter dialect belongs to the answering layer, so the
    # backend reads it; a backend that cannot has to refuse the query, because the
    # gate's disclosures can only report on refs the extraction found. An extractor
    # that matches nothing produces a successful query, no blocks and no notes,
    # with every filtered dimension grouped and projected and nothing saying it was
    # never examined. Both shipped backends read MetricFlow's dialect, so this is
    # the contract a third one inherits rather than a live path.
    from fakes.semantic import FakeHostedBackend

    from exmergo_dex_core.explore.semantic import (
        SemanticQuery,
        SemanticQueryRefusedError,
    )

    class _NoFilterDialect(FakeHostedBackend):
        def filter_refs(self, clauses):
            return None

    backend = _NoFilterDialect(
        dimensions_meta={"sessions": [{"name": "user__email", "config": None}]}
    )
    filtered = SemanticQuery(
        metrics=["sessions"],
        group_by=["session__mode"],
        where=['{"member": "users.email", "operator": "set"}'],
    )
    with pytest.raises(SemanticQueryRefusedError, match="filter dialect"):
        backend.query(filtered)
    assert not any("createQuery" in posted for posted in backend.posted)

    # The same backend still answers an unfiltered query: the refusal is scoped to
    # the input it cannot screen, not to the backend.
    unfiltered = SemanticQuery(metrics=["sessions"], group_by=["user__email"])
    with pytest.raises(SemanticQueryRefusedError, match="PII"):
        backend.query(unfiltered)


def test_hosted_semantic_pii_gate_still_binds_on_an_injected_token(monkeypatch):
    # Family 3, at the semantic credential seam. Supplying the token makes identity
    # the host's; it does not make the PII policy the host's. The gate has to refuse
    # the same dimension whether the token was discovered or handed over, or a host
    # would buy itself an unscreened path by supplying its own credential.
    from fakes.semantic import FakeHostedBackend

    from exmergo_dex_core import SemanticSource
    from exmergo_dex_core.config import SemanticConfig
    from exmergo_dex_core.explore.semantic import (
        SemanticQuery,
        SemanticQueryRefusedError,
        resolve_backend,
    )

    monkeypatch.delenv("DBT_SL_TOKEN", raising=False)
    config = DexConfig(
        semantic=SemanticConfig(
            backend="dbt_cloud", host="sl.example.com", environment_id="7"
        )
    )
    engine = DexEngine(
        config=config,
        store=MemoryStore(),
        semantic_source=SemanticSource(token=lambda: "host-token"),
    )
    # The real resolution path builds the real backend from the injected token; the
    # fake only stands in for the transport, so the gate under test is the shipped one.
    built = resolve_backend(engine)
    assert built._token == "host-token"  # noqa: S105 (a test fixture, not a secret)

    probe = FakeHostedBackend()
    monkeypatch.setattr(probe, "_token", built._token)
    with pytest.raises(SemanticQueryRefusedError, match="PII"):
        probe.query(SemanticQuery(metrics=["sessions"], group_by=["user__email"]))
    assert not any("createQuery" in posted for posted in probe.posted)


def test_a_host_supplied_semantic_token_never_crosses_the_boundary(monkeypatch):
    # Family 5, at the semantic credential seam: an injected token is a secret like
    # any other, so it must not reach an envelope. The sanitizer would hard-fail on a
    # secret-shaped key, and the value must not appear even under an innocent one.
    import json

    from fakes.semantic import FakeHostedBackend, table_json_result

    from exmergo_dex_core.explore.semantic import SemanticQuery

    injected = "dbts_INJECTED_token_must_not_leak"
    backend = FakeHostedBackend(
        result=table_json_result(["sessions"], ["string"], [[5.0]])
    )
    monkeypatch.setattr(backend, "_token", injected)
    result = backend.query(SemanticQuery(metrics=["sessions"]))
    envelope = to_envelope(result)
    env.sanitize(envelope)
    assert injected not in json.dumps(envelope.model_dump(mode="json"))


# --- The programmatic API is bound by the same spine as the CLI ----------------
#
# Every guard above was written when the only way in was a subcommand. The engine
# is a second door into the same house, and a guard that only holds on one side
# of a house is not a guard, so the load-bearing ones are re-asserted through it.


def _billed_engine(fake_bq_client, tmp_path: Path, **kwargs) -> DexEngine:
    """An engine whose one connection is the BigQuery fake, gated for real.

    The gate is built by ``connect.new_cost_gate``, not hand-rolled, so what is
    under test is the wiring the engine actually uses.
    """

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget
    from exmergo_dex_core.connect import new_cost_gate

    config = DexConfig(connector="bigquery", bigquery=BigQueryTarget(project="p"))
    store = FilesystemStore(tmp_path)

    def opener(**opened):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=new_cost_gate(
                "bigquery",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    engine = DexEngine(config=config, store=store, **kwargs)
    engine._open_for_test = opener
    return engine


@pytest.fixture
def api_engine(fake_bq_client, tmp_path, monkeypatch):
    """Engines built by ``_billed_engine``, with the real opener replaced."""

    import exmergo_dex_core.connect as connect_mod

    def build(**kwargs):
        engine = _billed_engine(fake_bq_client, tmp_path, **kwargs)
        monkeypatch.setattr(connect_mod, "open_adapter", engine._open_for_test)
        return engine

    return build


def test_api_confirmation_binds_and_spends_nothing(api_engine, fake_bq_client):
    # Family 2, through the API: cost is surfaced before any spend, and the
    # refusal carries what a caller needs to re-issue rather than a bare error.
    from exmergo_dex_core import ConfirmationRequiredError

    with api_engine() as engine, pytest.raises(ConfirmationRequiredError) as caught:
        engine.profile("customers")

    assert caught.value.request.cost.estimate > 0
    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_api_over_ceiling_cannot_be_confirmed_through(api_engine, fake_bq_client):
    # Family 2, through the API: confirmation is not an override. An estimate
    # past the ceiling refuses first, however confirmed the caller is.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    with (
        api_engine(confirmed=True, budget=1_000.0) as engine,
        pytest.raises(OverCeilingError),
    ):
        engine.profile("customers")

    assert all(c.dry_run for c in fake_bq_client.query_calls)


def test_api_no_ceiling_is_refused_even_when_confirmed(api_engine, fake_bq_client):
    # Family 2, through the API: nothing billed executes unbudgeted.
    from exmergo_dex_core.guards.cost_guard import CeilingRequiredError

    with api_engine(confirmed=True) as engine, pytest.raises(CeilingRequiredError):
        engine.profile("customers")

    assert all(c.dry_run for c in fake_bq_client.query_calls)


@pytest.fixture
def fk_bq_client():
    """A fake warehouse where inference finds a candidate join to probe: orders
    carries a customer_id foreign key into customers, whose id the resolver
    reports as unique. The shared fixture has no such join, and without one the
    verify phase has nothing to price and never checkpoints."""

    bigquery = pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    def table(table_id, fields):
        return FakeTable(
            project="test-proj",
            dataset_id="shop",
            table_id=table_id,
            schema=[bigquery.SchemaField(*f) for f in fields],
            num_rows=100,
            num_bytes=5_000,
        )

    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            table(
                "customers",
                [("id", "INTEGER"), ("email", "STRING"), ("plan_tier", "STRING")],
            ),
            table(
                "orders",
                [("id", "INTEGER"), ("customer_id", "INTEGER"), ("total", "NUMERIC")],
            ),
        ],
    )
    # A near-unique column that escalates without fully proving uniqueness
    # (d_0 of 99, not 100) leaves the composite-key probe un-short-circuited, so
    # every escalation the estimate reserved for actually runs and gets
    # charged. plan_tier/customer_id/total are also domain-eligible (small
    # cardinality, index 1/2), so the value-domain reserve is spent for real
    # too, not left as unused padding -- together, that is what leaves no
    # headroom for the verify probe below.
    values = {"n_total": 100, "nonnull_fk": 100, "orphans": 0}
    for i in range(10):
        values |= {
            f"nn_{i}": 100,
            f"nd_{i}": 100 if i == 0 else 5,
            f"mn_{i}": 1,
            f"mx_{i}": 100,
            f"d_{i}": 99 if i == 0 else 5,
        }

    def resolve(sql):
        row = dict(values)
        if "ARRAY_AGG" in sql:
            for i in range(3):
                row[f"d_{i}"] = [{"v": f"v{j}", "c": 1} for j in range(5)]
                row[f"n_{i}"] = 5
        return [row]

    client.row_resolver = resolve
    return client


def test_api_verify_checkpoint_keeps_what_it_already_paid_for(
    fk_bq_client, tmp_path, monkeypatch
):
    """Family 2 + 4: the mid-command checkpoint must not discard paid-for work.

    ``relationships(verify=True)`` profiles everything, then prices the overlap
    probes once inference knows what to probe. When that second phase does not
    fit the confirmed budget it comes back as a request on the result, not as an
    exception, because the profiles above it have already been billed and
    throwing them away would charge the user twice for one scan.
    """

    import exmergo_dex_core.connect as connect_mod

    # 80 MB is profile_estimate()'s worst-case total for two tables (each
    # floored aggregate batch plus all three possible escalation reserves: 40
    # MB apiece) -- the minimum that clears the confirm handshake at all.
    engine = _billed_engine(
        fk_bq_client, tmp_path, confirmed=True, budget=float(80 * 1024 * 1024)
    )
    monkeypatch.setattr(connect_mod, "open_adapter", engine._open_for_test)
    with engine:
        result = engine.relationships(verify=True)

    # The ask is present and priced...
    assert result.pending_confirmation is not None
    assert result.pending_confirmation.data["phase"] == "verify"
    # ...no probe ran...
    assert not any(
        not c.dry_run and "nonnull_fk" in c.sql for c in fk_bq_client.query_calls
    )
    # ...and the work already paid for is on the result and in the store, not lost.
    assert result.relationships and not result.relationships[0].verified
    cached = FilesystemStore(tmp_path).load_cache()
    assert cached is not None and cached.datasets
    assert any("saved unverified" in note for note in result.notes)


def test_api_pii_stays_flagged_and_never_surfaced(duckdb_file: Path):
    # Family 3, through the API: the firewall's verdict does not depend on which
    # door the query came in through.
    from exmergo_dex_core import QueryRefusedError

    with DexEngine(connector="duckdb", path=str(duckdb_file)) as engine:
        engine.map()
        with pytest.raises(QueryRefusedError):
            engine.query("select email from customers")


# --- A host-supplied connection buys identity, never a guard --------------------
#
# The seam that lets a host open the connection is the one place where an
# integrator could plausibly expect to hand dex a fully built adapter, cost gate
# included. It cannot, and these are the reasons why, asserted rather than argued.
# A host that fumbled `session_spent`, or passed 0.0, would silently disarm the
# cumulative ceiling in exactly the deployment where a runaway agent loop is most
# expensive, so dex keeps building the gate from its own store on both paths.


def _host_connected_engine(fake_pg_connection, store, **kwargs) -> DexEngine:
    """A billed engine whose connection came from outside, gated for real.

    Postgres because its fake is connection-shaped, so what is injected here is
    the same kind of object a host would hand over. The gate is built by
    connect.py from ``store``, not by this helper, so what is under test is the
    real wiring rather than a hand-rolled stand-in.
    """

    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import PostgresTarget

    config = DexConfig(connector="postgres", postgres=PostgresTarget(schemas=["shop"]))
    return DexEngine(
        config=config,
        store=store,
        connection=ConnectionSource(connect=lambda: fake_pg_connection),
        **kwargs,
    )


def test_the_session_ceiling_binds_on_a_host_supplied_connection(fake_pg_connection):
    # Family 2, at the connection seam. This is the property the whole design
    # rests on: the host owns authentication, dex still owns the brake. Spend
    # already in the ledger is settled against the ceiling even though dex never
    # opened the connection that would do the spending.
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    store = MemoryStore()
    store.append_spend_log(
        {
            "at": datetime.now(UTC).isoformat(),
            "connector": "postgres",
            "billed_seconds": 1_000.0,
        }
    )
    # The per-command budget would allow this comfortably; the session budget is
    # already spent, and it is the tighter of the two that has to bind.
    engine = _host_connected_engine(
        fake_pg_connection,
        store,
        confirmed=True,
        budget=10_000.0,
    )
    engine.config.budget.session_ceiling = 1_000.0

    with engine as eng, pytest.raises(OverCeilingError):
        eng.profile("shop.customers")

    # And nothing ran: the refusal landed before any statement, not after.
    assert fake_pg_connection.data_statements == []


def test_a_host_supplied_connection_still_needs_a_store(fake_pg_connection):
    # Family 2: supplying a connection makes identity the host's. It does not
    # make the budget the host's, so a billed connector with nowhere to settle
    # the session ledger is refused rather than opened unbudgeted.
    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import PostgresTarget

    config = DexConfig(connector="postgres", postgres=PostgresTarget())
    engine = DexEngine(
        config=config,
        connection=ConnectionSource(connect=lambda: fake_pg_connection),
    )
    engine.store = None  # a host that wired its own store badly

    with pytest.raises(ValueError, match="needs a store"):
        engine._adapter("explore profile")


def test_a_host_supplied_connection_cannot_widen_a_committed_scope(
    fake_pg_connection,
):
    # Family 2: the committed source allowlist is a cost boundary, and owning the
    # credential does not move it. A host narrows inside what the config commits,
    # exactly as a --scope flag does, and reaching outside is refused.
    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import PostgresTarget
    from exmergo_dex_core.connect import ScopeError

    config = DexConfig(connector="postgres", postgres=PostgresTarget(schemas=["shop"]))
    source = ConnectionSource(connect=lambda: fake_pg_connection)

    with DexEngine(
        config=config, store=MemoryStore(), connection=source, scopes=["shop"]
    ) as eng:
        assert eng.connect_test().capabilities["schema_count"] >= 1

    with (
        DexEngine(
            config=config,
            store=MemoryStore(),
            connection=source,
            scopes=["somewhere_else"],
        ) as eng,
        pytest.raises(ScopeError, match="never widens"),
    ):
        eng.connect_test()


def test_duckdb_cannot_be_reached_through_an_injected_connection(duckdb_file: Path):
    # Family 1: DuckDB is opened read-only by dex itself, and that is what makes
    # the read-only guarantee enforceable on a local file. An injected handle
    # could have been opened writable, so the seam refuses the connector outright
    # rather than trusting a caller's word about a file it did not open.
    from exmergo_dex_core import ConnectionSource
    from exmergo_dex_core.config import DuckDBTarget

    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path=str(duckdb_file)))
    writable = ConnectionSource(connect=lambda: pytest.fail("never called"))

    with pytest.raises(ValueError, match="read-only"):
        DexEngine(config=config, connection=writable)._adapter("explore inventory")
