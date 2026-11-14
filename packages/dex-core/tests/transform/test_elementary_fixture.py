"""The Elementary fixture and the dev-schema census prove themselves (#1304).

The census tests run offline on plain DuckDB. The fixture tests install the
Elementary package from the dbt hub, so they run only with
``DEX_TEST_ELEMENTARY=1`` and collect as skipped otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from .elementary_fixture import (
    ELEMENTARY_SCHEMA,
    VARIANTS,
    VERSIONS,
    dbt,
    duckdb_census,
    elementary_project,
    footprint,
    requires_elementary,
)
from .elementary_fixture import elementary_installs as elementary_installs
from .elementary_fixture import installed_elementary as installed_elementary
from .mutation_helpers import _engine

#: Printed by Elementary's ``test`` materialization, with its debug logs on, once
#: per test it handles and once more per test it routes through a temp table.
MATERIALIZATION_LINE = "starting test materialization hook"
TEMP_TABLE_LINE = "created test temp table"

#: What each variant's row promises: whether dbt picks Elementary's ``test``
#: materialization, and whether that materialization uses temp tables.
BEHAVIOUR = {
    "a": (True, False),
    "b": (False, False),
    "c": (True, False),
    "d": (False, False),
    "e": (True, False),
    "f": (True, True),
}


# --- the census, offline --------------------------------------------------------


def _execute(warehouse: Path, *statements: str) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(warehouse))
    try:
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


def test_a_command_that_touches_nothing_leaves_no_footprint(duckdb_file: Path):
    before = duckdb_census(duckdb_file)

    assert before, "the census listed nothing, so an empty footprint proves nothing"
    assert footprint(before, duckdb_census(duckdb_file)) == {}


def test_the_census_sees_new_rows_a_new_relation_and_a_dropped_one(duckdb_file: Path):
    before = duckdb_census(duckdb_file)
    _execute(
        duckdb_file,
        "insert into customers values (3, 'c@example.com')",
        "create schema audit",
        "create table audit.runs as select 1 as id",
        "create view audit.latest as select * from audit.runs",
        "drop table orders",
    )

    assert footprint(before, duckdb_census(duckdb_file)) == {
        ("audit", "latest", "VIEW"): (None, 1),
        ("audit", "runs", "BASE TABLE"): (None, 1),
        ("main", "customers", "BASE TABLE"): (2, 3),
        ("main", "orders", "BASE TABLE"): (3, None),
    }


def test_a_relation_that_cannot_be_counted_is_reported_not_raised(duckdb_file: Path):
    _execute(duckdb_file, "create view order_ids as select id from orders")
    before = duckdb_census(duckdb_file)
    _execute(duckdb_file, "drop table orders")

    change = footprint(before, duckdb_census(duckdb_file))

    assert change[("main", "order_ids", "VIEW")] == (3, None)


# --- the fixture, against the installed package ---------------------------------


@requires_elementary
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_parses_builds_and_carries_the_four_kinds_of_test(
    tmp_path: Path, installed_elementary: Path, variant: str
):
    project = elementary_project(tmp_path, variant, installed_elementary)
    [fct] = yaml.safe_load((project / "models" / "schema.yml").read_text())["models"]
    by_column = {column["name"]: column["data_tests"] for column in fct["columns"]}

    assert [next(iter(test)) for test in fct["data_tests"][:1]] == [
        "elementary.volume_anomalies"
    ]
    assert "elementary.schema_changes" in fct["data_tests"]
    assert "accepted_values" in by_column["band"][0]
    # Rendered by Dex from the declared grain, not written by hand.
    assert by_column["id"] == ["unique", "not_null"]
    assert fct["config"]["meta"]["dex"]["grain"] == ["id"]

    _engine(project).build(target="dev")

    schemas = {schema for schema, _, _ in duckdb_census(tmp_path / "dev.duckdb")}
    assert "main" in schemas, "the project's own models did not build"
    # Variant d disables the package's models for dev, so its schema never appears.
    assert (ELEMENTARY_SCHEMA in schemas) is (variant != "d")


@requires_elementary
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_changes_what_elementary_does_in_a_run(
    tmp_path: Path, installed_elementary: Path, variant: str
):
    """The six rows would be one fixture under six names if nothing told them
    apart. Elementary's own debug log does: its materialization announces every
    test it handles, and every temp table it routes one through."""

    project = elementary_project(tmp_path, variant, installed_elementary)
    _engine(project).build(target="dev")

    log = dbt(
        "test",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--target",
        "dev",
        "--vars",
        "{debug_logs: true}",
        cwd=project,
    )

    assert "Done. PASS=" in log, log[-2000:]
    assert (MATERIALIZATION_LINE in log, TEMP_TABLE_LINE in log) == BEHAVIOUR[variant]


@requires_elementary
@pytest.mark.parametrize("version", VERSIONS)
def test_a_mutation_run_leaves_rows_in_the_dev_elementary_schema(
    tmp_path: Path, elementary_installs: Callable[[str], Path], version: str
):
    """The failing arm (#1307), on variant a: a mutation run is meant to leave the
    dev warehouse as it found it, and with Elementary installed it does not. The
    package's run hooks fire on the baseline and on every mutant, so each one
    uploads an invocation. This asserts the defect, which is what proves the
    census can see one; the fix inverts it. Run on the current release and on the
    oldest one DuckDB can build."""

    project = elementary_project(tmp_path, "a", elementary_installs(version))
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    result = engine.test_mutations("fct")
    change = footprint(before, duckdb_census(warehouse))

    assert result.counts["generated"] >= 1, "no mutant ran, so nothing was measured"
    assert {schema for schema, _, _ in change} == {ELEMENTARY_SCHEMA}
    was, now = change[(ELEMENTARY_SCHEMA, "dbt_invocations", "BASE TABLE")]
    assert now - was == result.counts["generated"] + 1


@requires_elementary
def test_a_mutation_run_with_elementary_disabled_leaves_no_footprint(
    tmp_path: Path, installed_elementary: Path
):
    """The holding arm, on variant d: the same command over the same project with
    the package's models disabled for dev changes nothing, so the footprint on
    variant a is Elementary's and not the fixture's."""

    project = elementary_project(tmp_path, "d", installed_elementary)
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    result = engine.test_mutations("fct")

    assert result.counts["generated"] >= 1, "no mutant ran, so nothing was measured"
    assert footprint(before, duckdb_census(warehouse)) == {}
