"""The Elementary fixture and the dev-schema census prove themselves (#1304).

The census tests run offline on plain DuckDB. The fixture tests install the
Elementary package from the dbt hub, so they run only with
``DEX_TEST_ELEMENTARY=1`` and collect as skipped otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from .elementary_fixture import (
    ABSENT,
    BEHAVIOUR,
    CURRENT_VERSION,
    ELEMENTARY_SCHEMA,
    LEGACY_VERSIONS,
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


def _strip_sampling_switch(project: Path) -> None:
    """Strip the sampling switch from the grain's rendered ``unique`` test."""

    path = project / "models" / "schema.yml"
    schema = yaml.safe_load(path.read_text(encoding="utf-8"))
    [column] = [c for c in schema["models"][0]["columns"] if c["name"] == "id"]
    column["data_tests"] = [
        "unique" if isinstance(t, dict) and "unique" in t else t
        for t in column["data_tests"]
    ]
    path.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")


@requires_elementary
def test_a_failing_rendered_test_leaves_no_sample_of_its_rows(
    tmp_path: Path, installed_elementary: Path
):
    """#1306, on variant a, where Elementary's materialization handles the tests.
    A failing ``unique`` test returns the duplicated key, and Elementary copies
    failing rows into ``test_result_rows``. The test Dex renders carries
    ``disable_test_samples``, so it adds no row there; the same test with the
    switch removed adds one, so the census can see a sample when there is one."""

    project = elementary_project(tmp_path, "a", installed_elementary)
    warehouse = tmp_path / "dev.duckdb"
    # One duplicated key, so the grain's `unique` test fails on exactly one value.
    _execute(
        warehouse,
        "insert into main.raw_orders select * from main.raw_orders where id = 1",
    )
    project_args = ("--project-dir", str(project), "--profiles-dir", str(project))
    dbt("run", *project_args, cwd=project)
    rows = (ELEMENTARY_SCHEMA, "test_result_rows", "BASE TABLE")
    run_results = project / "target" / "run_results.json"

    def sampled() -> int:
        before = duckdb_census(warehouse)
        # Removed first, so a run that wrote no results cannot be read as the
        # previous arm's.
        run_results.unlink(missing_ok=True)
        log = dbt(
            "test", *project_args, "--select", "unique_fct_id", cwd=project, check=False
        )
        # dbt prints ERROR=1 for a test that errored as well as for one that
        # failed, and an errored test also leaves no sample. Only a completed
        # failure on the one duplicated key measures the switch.
        [result] = [
            r
            for r in json.loads(run_results.read_text(encoding="utf-8"))["results"]
            if r["unique_id"].startswith("test.")
        ]
        assert (result["status"], result["failures"]) == ("fail", 1), (
            f"the rendered unique test is meant to fail on one key:\n{log}"
        )
        was, now = footprint(before, duckdb_census(warehouse)).get(rows, (0, 0))
        return now - was

    assert sampled() == 0
    _strip_sampling_switch(project)
    assert sampled() == 1


# --- the resolver (#1305) ---------------------------------------------------------

#: What the resolver reads for each variant on ``dev``: whether the package is
#: enabled, what its run hooks do, and whose ``test`` materialization dbt picks.
RESOLVED = {
    "a": (True, "run", "elementary"),
    "b": (True, "run", "dbt"),
    "c": (True, "run", "elementary"),
    "d": (False, "disabled", "elementary"),
    "e": (True, "run", "elementary"),
    "f": (True, "run", "elementary"),
}

#: Each variant at the current release, and three at the oldest release most
#: installs pin. DuckDB parses 0.16.4, though it cannot build it.
RESOLVER_CASES = [(CURRENT_VERSION, v) for v in VARIANTS] + [
    (LEGACY_VERSIONS[0], v) for v in ("a", "b", "d")
]


def _parsed(project: Path, target: str) -> dict:
    dbt(
        "parse",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--target",
        target,
        cwd=project,
    )
    return json.loads((project / "target" / "manifest.json").read_text("utf-8"))


@requires_elementary
@pytest.mark.parametrize(("version", "variant"), RESOLVER_CASES)
def test_the_resolver_reads_what_each_variant_does_on_dev(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    version: str,
    variant: str,
):
    """The record for ``dev`` matches the variant's row, and agrees with what
    the run itself shows: Elementary's materialization announces itself in the
    debug log exactly where the record says it is picked and the package is
    enabled (the first column of ``BEHAVIOUR``, read from a run in
    ``test_each_variant_changes_what_elementary_does_in_a_run``)."""

    from exmergo_dex_core.transform.elementary import read

    project = elementary_project(tmp_path, variant, elementary_installs(version))
    record = read(project, _parsed(project, "dev"))

    assert record.installed
    assert record.release == version
    assert (record.enabled, record.hooks, record.materialization) == RESOLVED[variant]
    announced = record.enabled and record.materialization == "elementary"
    assert announced is BEHAVIOUR[variant][0]
    assert not (tmp_path / "prod.duckdb").exists()


@requires_elementary
@pytest.mark.parametrize("version", [CURRENT_VERSION, LEGACY_VERSIONS[0]])
def test_the_resolver_reads_variant_d_as_enabled_where_the_project_enables_it(
    tmp_path: Path, elementary_installs: Callable[[str], Path], version: str
):
    """Variant d disables the package for ``dev`` only, with a Jinja expression on
    the target name that only a parse can settle. Parsed for ``prod``, which is
    never built, the package and its hooks are back."""

    from exmergo_dex_core.transform.elementary import read

    project = elementary_project(tmp_path, "d", elementary_installs(version))
    record = read(project, _parsed(project, "prod"))

    assert (record.enabled, record.hooks) == (True, "run")
    assert all(record.uploads.values())
    assert not (tmp_path / "prod.duckdb").exists()
