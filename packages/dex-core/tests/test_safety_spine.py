"""The safety spine: the five safety-critical assertion families.

A regression on any of these is a release blocker regardless of benchmark score.
The harness is wired in full now: families whose engine already exists are real
tests; families whose engine is not yet built are explicit ``xfail`` placeholders
so the spine is visible and complete in CI from day one and turns green as the
logic arrives.
"""

from __future__ import annotations

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
            ],
            safe={"id"},
            shape={"email"},
            type_req={"id", "email"},
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
    # Idempotent: passing it through the guard again must not raise.
    assert assert_select_only(sql) == sql


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
    dialects = ("duckdb", "bigquery", "snowflake", "databricks", "postgres", "redshift")
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
    ):
        narrowed = narrow_target(target, connector, [getattr(target, field)[0]])
        assert getattr(narrowed, field) == getattr(target, field)
        with pytest.raises(ScopeError):
            narrow_target(target, connector, ["somewhere_else"])


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
    from exmergo_dex_core.adapters.base import is_integer_type, is_string_type
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _bq_adapter(fake_bq_client)
    _meta, columns = adapter.table_metadata("test-proj.shop.customers")
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
    sql, _plan = adapter._build_aggregate_sql(
        "test-proj.shop.customers", columns, {"id"}, shape, type_req
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    # Declared-type-vs-content statistics (#204) ride the same statement too.
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
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
    from exmergo_dex_core.adapters.base import is_integer_type, is_string_type
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _sf_adapter(fake_sf_connection)
    _meta, columns = adapter.table_metadata("SHOP.PUBLIC.CUSTOMERS")
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
    sql, _plan = adapter._build_aggregate_sql(
        "SHOP.PUBLIC.CUSTOMERS", columns, {"ID"}, shape, type_req
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
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
    from exmergo_dex_core.adapters.base import is_integer_type, is_string_type
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _dbx_adapter(fake_databricks)
    _meta, columns = adapter.table_metadata("shop.core.customers")
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
    sql, _plan = adapter._build_aggregate_sql(
        "shop.core.customers", columns, {"id"}, shape, type_req
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
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
    from exmergo_dex_core.adapters.base import is_integer_type, is_string_type
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _pg_adapter(fake_pg_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
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
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers", columns, {"id"}, shape, type_req
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
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
    from exmergo_dex_core.adapters.base import is_integer_type, is_string_type
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    adapter = _redshift_adapter(fake_redshift_connection)
    _meta, columns = adapter.table_metadata("dexdb.shop.customers")
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
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers", columns, {"id"}, shape, type_req
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
    assert "ts_ns_" in sql and "ts_ep_s_" in sql
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
    backend = LocalMetricFlowBackend(project, _memory_engine(), "duckdb", QueryLimits())
    lookup = backend._cache_pii_lookup(cache)
    assert dict(screen_dimension_refs(["order__contact"], meta_lookup=lookup))


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
