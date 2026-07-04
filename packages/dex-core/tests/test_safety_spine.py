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
        )
    finally:
        adapter.close()
    assert sql.lstrip().upper().startswith("SELECT")
    # Idempotent: passing it through the guard again must not raise.
    assert assert_select_only(sql) == sql


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
    sql, _plan = adapter._build_aggregate_sql(
        "test-proj.shop.customers", columns, {"id"}
    )
    assert sql.lstrip().upper().startswith("SELECT")
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
