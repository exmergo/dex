"""What the provider enforces on a guarded build, and what it cannot.

`transform build` refuses a prod target, prices the run, and takes the confirm
handshake, all in this process. Once the work is handed to a dbt subprocess the
question is different: what does the warehouse itself enforce. These assert the
report answers that from the project's rendered profile rather than from what
dex would have written, and that a connector with nothing to enforce says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.adapters import adapter_declarations
from exmergo_dex_core.config import DexConfig, GuardConfig
from exmergo_dex_core.envelope import EstimateQuality, Paradigm
from exmergo_dex_core.guards.execution import (
    guarded_execution_preflight,
    guarded_statement_verdict,
)
from exmergo_dex_core.guards.sql_guard import (
    NotSelectOnlyError,
    RefusalReason,
    assert_select_only,
)


def _profile(project: Path, connector: str, dev: dict) -> None:
    import yaml

    (project / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "dex_test": {
                    "target": "dev",
                    "outputs": {"dev": {"type": connector, **dev}},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_duckdb_names_the_absence_of_a_provider_side_control(dbt_project_dir: Path):
    """A target called `dev` is not by itself evidence of anything.

    DuckDB has no service to enforce a limit, and reporting a healthy preflight
    for it would be the exact false safety claim this report exists to prevent.
    """

    report = guarded_execution_preflight(
        adapter_declarations("duckdb"), project_dir=dbt_project_dir
    )
    assert report.connector == "duckdb"
    assert report.paradigm is Paradigm.FREE_LOCAL
    assert report.binding is False
    assert any("no provider-side spend control" in u for u in report.unsupported)
    # Read-only is a real guarantee and is reported as one, with what it does
    # and does not bound spelled out.
    (control,) = report.controls
    assert control.name == "read_only"
    assert "bounds nothing a dbt build writes" in control.detail


def test_bigquery_reports_the_cap_the_profile_actually_carries(dbt_project_dir: Path):
    _profile(
        dbt_project_dir,
        "bigquery",
        {"project": "p", "dataset": "d", "maximum_bytes_billed": 1_000_000},
    )
    report = guarded_execution_preflight(
        adapter_declarations("bigquery", Paradigm.BYTES_SCANNED),
        project_dir=dbt_project_dir,
    )

    assert report.binding is True
    assert report.estimate_quality is EstimateQuality.EXACT
    (control,) = [c for c in report.controls if c.name == "maximum_bytes_billed"]
    assert control.value == 1_000_000
    assert control.binds == "statement"
    assert control.unit == "bytes"
    # The provenance is the point: it was written when the project was
    # initialized, from the ceiling configured then, and a later --budget does
    # not move it.
    assert control.source == "profile"
    assert "--budget does not move it" in control.detail
    assert any("wall-clock" in u for u in report.unsupported)


def test_a_profile_with_no_cap_says_nothing_binds_rather_than_staying_quiet(
    dbt_project_dir: Path,
):
    _profile(dbt_project_dir, "bigquery", {"project": "p", "dataset": "d"})
    report = guarded_execution_preflight(
        adapter_declarations("bigquery", Paradigm.BYTES_SCANNED),
        project_dir=dbt_project_dir,
    )

    assert report.binding is False
    assert report.controls == []
    assert any("nothing bounds a statement that outruns it" in n for n in report.notes)


def test_reading_no_project_is_reported_as_such_rather_than_nothing_binds(
    dbt_project_dir: Path,
):
    report = guarded_execution_preflight(
        adapter_declarations("bigquery", Paradigm.BYTES_SCANNED)
    )
    assert report.binding is False
    assert any("no project was read" in n for n in report.notes)


def test_a_missing_dev_output_is_named_rather_than_read_as_uncapped(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "profiles.yml").unlink()
    report = guarded_execution_preflight(
        adapter_declarations("bigquery", Paradigm.BYTES_SCANNED),
        project_dir=dbt_project_dir,
    )
    assert report.binding is False
    assert any("could not read what binds" in n for n in report.notes)


def test_postgres_reports_the_cap_that_follows_this_runs_budget(dbt_project_dir: Path):
    """The one class of connector where the cap follows the budget of the run
    rather than whatever was configured when the profile was written."""

    _profile(dbt_project_dir, "postgres", {"host": "h", "dbname": "d"})
    config = DexConfig()
    config.budget.ceiling = 45.0
    report = guarded_execution_preflight(
        adapter_declarations("postgres", Paradigm.DB_LOAD),
        project_dir=dbt_project_dir,
        config=config,
    )
    (control,) = [c for c in report.controls if c.source == "budget"]
    assert control.value == 45.0
    assert control.binds == "statement"
    assert "follows the budget" in control.detail


def test_clickhouse_without_the_env_references_reports_nothing_binding(
    dbt_project_dir: Path,
):
    """A hand-written profile builds fine and cannot be capped, and saying so is
    the difference between a build that was capped and one that reports it was."""

    _profile(dbt_project_dir, "clickhouse", {"host": "h", "database": "d"})
    config = DexConfig()
    config.budget.ceiling = 30.0
    report = guarded_execution_preflight(
        adapter_declarations("clickhouse", Paradigm.DB_LOAD),
        project_dir=dbt_project_dir,
        config=config,
    )
    assert report.binding is False
    assert any("custom_settings do not reference" in n for n in report.notes)


@pytest.mark.parametrize(
    ("sql", "dialect", "reason"),
    [
        ("CALL my_proc(1)", "snowflake", RefusalReason.STORED_PROCEDURE_OR_CALL),
        ("EXECUTE IMMEDIATE 'select 1'", "bigquery", RefusalReason.DYNAMIC_SQL),
        ("drop table t", "duckdb", RefusalReason.DDL),
        ("create function f() as 1", "duckdb", RefusalReason.DDL),
        ("insert into t values (1)", "duckdb", RefusalReason.WRITE),
        ("select 1; select 2", "duckdb", RefusalReason.MULTI_STATEMENT),
    ],
)
def test_a_refusal_carries_a_name_rather_than_a_parser_node_type(sql, dialect, reason):
    """sqlglot funnels every procedural and dynamic form into `Command`, so a
    message quoting that class tells a host nothing it can branch on."""

    with pytest.raises(NotSelectOnlyError) as caught:
        assert_select_only(sql, dialect=dialect)
    assert caught.value.reason is reason


def test_the_function_allowlist_is_off_until_a_caller_supplies_one():
    """On by default would refuse working builds on every warehouse with local
    functions the engine has never heard of."""

    sql = "select my_udf(x) as v from t"
    assert guarded_statement_verdict(sql, node="m", dialect="duckdb").allowed is True
    assert (
        guarded_statement_verdict(
            sql, node="m", dialect="duckdb", approved_functions=set()
        ).allowed
        is True
    )


def test_an_unapproved_function_is_refused_by_name_when_the_allowlist_is_on():
    verdict = guarded_statement_verdict(
        "select my_udf(x) as v from t",
        node="fct_orders",
        dialect="duckdb",
        approved_functions={"other_udf"},
    )
    assert verdict.allowed is False
    assert verdict.reason == "unapproved_function"
    assert "my_udf" in verdict.detail
    assert verdict.node == "fct_orders"


def test_a_dialect_builtin_is_never_an_unapproved_function():
    """sqlglot models a builtin, so only what it could not model reaches the
    allowlist; a project's own UDF does and `count` does not."""

    verdict = guarded_statement_verdict(
        "select count(*) as n, sum(x) as s from t",
        node="m",
        dialect="duckdb",
        approved_functions={"nothing"},
    )
    assert verdict.allowed is True


def test_a_statement_that_cannot_be_parsed_is_refused_rather_than_passed():
    verdict = guarded_statement_verdict("select from where", node="m", dialect="duckdb")
    assert verdict.allowed is False


def test_the_allowlist_config_is_empty_by_default():
    assert GuardConfig().approved_functions == []
    assert DexConfig().guards.approved_functions == []
