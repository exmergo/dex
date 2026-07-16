"""The safety spine: the five safety-critical assertion families.

A regression on any of these is a release blocker regardless of benchmark score.
The harness is wired in full now: families whose engine already exists are real
tests; families whose engine is not yet built are explicit ``xfail`` placeholders
so the spine is visible and complete in CI from day one and turns green as the
logic arrives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import ColumnProfile, PIIFlag

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
        )
    finally:
        adapter.close()
    assert sql.lstrip().upper().startswith("SELECT")
    # Shape statistics ride the same guarded statement (regex predicates inside
    # measuring aggregates, never a raw value in the projection).
    assert "su_1" in sql and "sp_1" in sql and "st_1" in sql
    # Idempotent: passing it through the guard again must not raise.
    assert assert_select_only(sql) == sql


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


def test_firewall_block_threshold_is_a_hard_coded_engine_constant():
    # The threshold is engine policy, not configuration: a config edit must
    # never be able to widen the PII boundary. Its value is load-bearing (every
    # base confidence in the detector sits at or above it, so nothing unblocks
    # without value-shape evidence), so the number itself is pinned here.
    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.guards.query_firewall import PII_BLOCK_CONFIDENCE

    assert PII_BLOCK_CONFIDENCE == 0.5
    assert not any("threshold" in name for name in DexConfig.model_fields)


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
        "add stg_new", edits, dbt_project_dir, repo_root=dbt_project_dir.parent
    )
    # Planning returns reviewable diffs and touches nothing in the project.
    assert diffs and diffs[0]["unified"]
    assert not new_model.exists()


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
        "trim stg_customers", edits, dbt_project_dir, repo_root=dbt_project_dir.parent
    )
    # A human edits the file after the plan was made; their edit is authoritative.
    model.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    result = transform.apply(planned.plan_id, dbt_project_dir.parent)
    assert result.written == []
    assert result.conflicts
    assert model.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"


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
    sql, _plan = adapter._build_aggregate_sql(
        "test-proj.shop.customers", columns, {"id"}, shape
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
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
    from exmergo_dex_core.cache import DEX_DIR
    from exmergo_dex_core.config import CONFIG_FILE

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

    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
    sql, _plan = adapter._build_aggregate_sql(
        "SHOP.PUBLIC.CUSTOMERS", columns, {"ID"}, shape
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
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

    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
    sql, _plan = adapter._build_aggregate_sql(
        "shop.core.customers", columns, {"id"}, shape
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
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

    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers", columns, {"id"}, shape
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
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

    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
    sql, _plan = adapter._build_aggregate_sql(
        "dexdb.shop.customers", columns, {"id"}, shape
    )
    assert sql.lstrip().upper().startswith("SELECT")
    assert "su_" in sql and "sp_" in sql and "st_" in sql
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
    from exmergo_dex_core.cache import DEX_DIR
    from exmergo_dex_core.config import CONFIG_FILE

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

    from exmergo_dex_core.cache import DexStore

    store = DexStore(tmp_path)
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
