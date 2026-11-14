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
    ABSENT,
    BEHAVIOUR,
    ELEMENTARY_SCHEMA,
    MATERIALIZATION_LINE,
    TEMP_TABLE_LINE,
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
        ("audit", "latest", "VIEW"): (ABSENT, 1),
        ("audit", "runs", "BASE TABLE"): (ABSENT, 1),
        ("main", "customers", "BASE TABLE"): (2, 3),
        ("main", "orders", "BASE TABLE"): (3, ABSENT),
    }


def test_a_relation_that_cannot_be_counted_is_kept_and_told_from_a_dropped_one(
    duckdb_file: Path,
):
    view = ("main", "order_ids", "VIEW")
    _execute(duckdb_file, "create view order_ids as select id from orders")
    before = duckdb_census(duckdb_file)
    _execute(duckdb_file, "drop table orders")

    after = duckdb_census(duckdb_file)

    assert view in after, "the census dropped a relation it could not count"
    assert after[view] is None
    assert footprint(before, after)[view] == (3, None)
    assert footprint(before, {})[view] == (3, ABSENT)


def test_a_relation_whose_name_holds_a_double_quote_is_still_counted(
    duckdb_file: Path,
):
    table = ("main", 'order"items', "BASE TABLE")
    _execute(duckdb_file, 'create table "order""items" as select 1 as id')
    before = duckdb_census(duckdb_file)
    _execute(duckdb_file, 'insert into "order""items" values (2)')

    assert before[table] == 1, "the census could not count a validly named relation"
    assert footprint(before, duckdb_census(duckdb_file)) == {table: (1, 2)}


# --- the fixture, against the installed package ---------------------------------


def _listed(project: Path, *argv: str) -> set[str]:
    """What ``dbt ls`` selects in ``project``, by name. It opens no connection."""

    out = dbt(
        "ls",
        "--quiet",
        "--output",
        "name",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        *argv,
        cwd=project,
    )
    return {line.strip() for line in out.splitlines() if line.strip()}


#: The four kinds of test, as dbt names them: an anomaly test, ``schema_changes``,
#: a dbt built-in, and the two Dex renders from the declared grain.
FCT_TESTS = {
    "elementary_volume_anomalies_fct_ordered_at",
    "elementary_schema_changes_fct_",
    "accepted_values_fct_band__large__small",
    "unique_fct_id",
    "not_null_fct_id",
}


@requires_elementary
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_parses_builds_and_carries_the_four_kinds_of_test(
    tmp_path: Path, installed_elementary: Path, variant: str
):
    project = elementary_project(tmp_path, variant, installed_elementary)
    [fct] = yaml.safe_load((project / "models" / "schema.yml").read_text())["models"]
    # The grain's tests are rendered by Dex from a declaration, not written here.
    assert fct["config"]["meta"]["dex"]["grain"] == ["id"]

    _engine(project).build(target="dev")

    built = duckdb_census(tmp_path / "dev.duckdb")
    assert ("main", "fct", "BASE TABLE") in built, (
        "the project's own mart did not build"
    )
    tests = ("--resource-type", "test", "--select", "fct", "--target", "dev")
    assert _listed(project, *tests) == FCT_TESTS
    # Variant d disables the package's models for dev, and only for dev: a
    # resolver asked about `prod` must find them enabled there. Listing reads the
    # graph, so `prod` is named without ever being built.
    elementary = ("--resource-type", "model", "--select", "package:elementary")
    assert bool(_listed(project, *elementary, "--target", "dev")) is (variant != "d")
    assert bool(_listed(project, *elementary, "--target", "prod"))
    assert ({schema for schema, _, _ in built} >= {ELEMENTARY_SCHEMA}) is (
        variant != "d"
    )
    assert not (tmp_path / "prod.duckdb").exists()


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
    """The holding arm, on variant d: the same command over a project that differs
    from variant a only in disabling the package's models for dev changes
    nothing, so the footprint on variant a is Elementary's and not the
    fixture's."""

    project = elementary_project(tmp_path, "d", installed_elementary)
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    result = engine.test_mutations("fct")

    # `generated` is the batch size, set whether or not a mutant was ever tested.
    executed = sum(
        result.counts[outcome]
        for outcome in ("killed", "caught_downstream", "survived", "rejected")
    )
    assert executed >= 1, "no mutant ran, so nothing was measured"
    assert footprint(before, duckdb_census(warehouse)) == {}
