"""The Elementary fixture and the dev-schema census prove themselves (#1304).

The census tests run offline on plain DuckDB. The fixture tests install the
Elementary package from the dbt hub, so they run only with
``DEX_TEST_ELEMENTARY=1`` and collect as skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
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
    OLDEST_DUCKDB_VERSION,
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
        stderr=False,
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


def _executed(result) -> int:
    # `generated` is the batch size, set whether or not a mutant was ever tested.
    return sum(
        result.counts[outcome]
        for outcome in ("killed", "caught_downstream", "survived", "rejected")
    )


#: The two Elementary tests on ``fct``, which a mutation run keeps out (#1307).
MONITORING_TESTS = {
    "elementary_volume_anomalies_fct_ordered_at",
    "elementary_schema_changes_fct_",
}

#: Each variant at the current release, and variant a at the oldest release
#: DuckDB can build.
MUTATION_CASES = [(CURRENT_VERSION, v) for v in VARIANTS] + [
    (OLDEST_DUCKDB_VERSION, "a")
]


@requires_elementary
@pytest.mark.parametrize(("version", "variant"), MUTATION_CASES)
def test_a_mutation_run_leaves_the_dev_warehouse_as_it_found_it(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    version: str,
    variant: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """The holding arm (#1307): with Elementary installed, a mutation run changes
    nothing in the dev warehouse. Where nothing refers to the package's models
    the copy disables them (every variant but e); on e they stay enabled, dbt is
    made to pick its own test materialization, and the monitoring tests are left
    out for their history. Either way each left-out test is named with its
    reason code.

    Variant f's temp tables are session ``temporary`` on DuckDB and gone before
    the census reads, so an empty footprint there says nothing about them. Only
    the package's materialization creates them, so the record read before every
    run, which must say the tests get dbt's, is what covers f."""

    from exmergo_dex_core.transform.build import ShadowRun
    from exmergo_dex_core.transform.elementary import read

    picked: list[str] = []
    test = ShadowRun.test

    def recording(self, select, **kwargs):
        picked.append(read(self.shadow, self.manifest()).materialization)
        return test(self, select, **kwargs)

    monkeypatch.setattr(ShadowRun, "test", recording)
    project = elementary_project(tmp_path, variant, elementary_installs(version))
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    result = engine.test_mutations("fct")

    assert _executed(result) >= 1, "no mutant ran, so nothing was measured"
    assert footprint(before, duckdb_census(warehouse)) == {}
    code = "elementary_history" if variant == "e" else "elementary_disabled"
    left_out = {
        entry["name"]: entry["reason_code"]
        for entry in result.baseline["excluded"]
        if "reason_code" in entry
    }
    assert left_out == dict.fromkeys(MONITORING_TESTS, code)
    assert not MONITORING_TESTS & {t["name"] for t in result.baseline["tests"]}
    assert picked and set(picked) == {"dbt"}


@requires_elementary
def test_a_mutation_run_refuses_where_a_root_shim_keeps_elementary_s_materialization(
    tmp_path: Path, installed_elementary: Path
):
    """Variant c's shim with variant e's root model: the package must stay
    enabled, and the shim hands every test to Elementary's materialization
    whatever dbt's flag says, so no copy dex can make runs the tests quietly.
    The run is refused, and nothing in the warehouse changes."""

    from exmergo_dex_core.transform.mutation import MutationError

    from .elementary_fixture import READS_ELEMENTARY_SQL

    project = elementary_project(tmp_path, "c", installed_elementary)
    (project / "models" / "elementary_runs.sql").write_text(
        READS_ELEMENTARY_SQL, encoding="utf-8"
    )
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    with pytest.raises(MutationError, match="hands every test to Elementary's"):
        engine.test_mutations("fct")
    assert footprint(before, duckdb_census(warehouse)) == {}


@requires_elementary
def test_a_model_whose_only_tests_are_monitoring_tests_is_refused_as_untested(
    tmp_path: Path, installed_elementary: Path
):
    """With every test but the two Elementary ones removed from ``fct``, nothing
    left could catch a mutant, so the run is refused for that reason and not as
    a baseline that failed, which ``maintain coverage`` would retry (#1307)."""

    from exmergo_dex_core.transform.mutation import MutationError

    project = elementary_project(tmp_path, "a", installed_elementary)
    (project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: fct\n    data_tests:\n"
        "      - elementary.volume_anomalies:\n          timestamp_column: ordered_at\n"
        "      - elementary.schema_changes\n",
        encoding="utf-8",
    )
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    with pytest.raises(MutationError, match="every test of 'fct' is an Elementary"):
        engine.test_mutations("fct")
    assert footprint(before, duckdb_census(warehouse)) == {}


@requires_elementary
@pytest.mark.parametrize("version", VERSIONS)
def test_without_the_fix_a_mutation_run_leaves_rows_in_the_dev_elementary_schema(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    version: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """The failing arm, on variant a: with the copy left as it was before #1307
    (no package hooks stripped, nothing disabled, nothing left out), the
    package's hooks fire on the baseline and on every mutant and each uploads an
    invocation. This is what proves the census can see the footprint the
    holding arm finds empty."""

    from exmergo_dex_core.transform import commands
    from exmergo_dex_core.transform.build import ShadowRun

    monkeypatch.setattr(ShadowRun, "strip_run_hooks", lambda self: [])
    monkeypatch.setattr(commands, "_quiet_elementary", lambda shadow: ([], []))
    project = elementary_project(tmp_path, "a", elementary_installs(version))
    engine = _engine(project)
    engine.build(target="dev")
    warehouse = tmp_path / "dev.duckdb"

    before = duckdb_census(warehouse)
    result = engine.test_mutations("fct")
    change = footprint(before, duckdb_census(warehouse))

    assert result.counts["not_run"] == 0, "the budget stopped the run short"
    assert {schema for schema, _, _ in change} == {ELEMENTARY_SCHEMA}
    was, now = change[(ELEMENTARY_SCHEMA, "dbt_invocations", "BASE TABLE")]
    assert now - was == result.counts["generated"] + 1


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
    # dbt picks the package's materialization, which hands every test to dbt's
    # because the package is disabled, so the tests get dbt's behaviour.
    "d": (False, "disabled", "dbt"),
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
    debug log exactly where the record says the tests get its behaviour (the
    first column of ``BEHAVIOUR``, read from a run in
    ``test_each_variant_changes_what_elementary_does_in_a_run``)."""

    from exmergo_dex_core.transform.elementary import read

    project = elementary_project(tmp_path, variant, elementary_installs(version))
    record = read(project, _parsed(project, "dev"))

    assert record.installed
    assert record.release == version
    # Acceptance, first case (#1445): a release the fixture runs is supported.
    assert (record.release_status, record.release_reason_code) == ("supported", None)
    assert (record.enabled, record.hooks, record.materialization) == RESOLVED[variant]
    assert (record.materialization == "elementary") is BEHAVIOUR[variant][0]
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


#: The work a build's estimate leaves out on each variant (#1309): the hooks and
#: the monitoring tests where the package runs, the per-test extras only where
#: its materialization handles the tests.
UNPRICED = {
    "a": ["elementary_hooks", "elementary_monitoring_tests", "elementary_test_extras"],
    "b": ["elementary_hooks", "elementary_monitoring_tests"],
    "d": [],
}


@requires_elementary
@pytest.mark.parametrize("variant", sorted(UNPRICED))
def test_build_cost_names_what_each_variant_does_not_price(
    tmp_path: Path, installed_elementary: Path, variant: str
):
    from exmergo_dex_core.transform.elementary import read, unpriced_work

    project = elementary_project(tmp_path, variant, installed_elementary)
    manifest = _parsed(project, "dev")
    text = (project / "dbt_project.yml").read_text("utf-8")
    work = unpriced_work(read(project, manifest), manifest, text)

    assert work.codes == UNPRICED[variant]
    assert len(work.warnings()) == len(UNPRICED[variant])
    if variant != "d":
        # The fixture's one anomaly test, at the package's default window.
        anomaly = [t for t in work.gated_tests if t.days_back is not None]
        assert [(t.days_back, t.days_back_source) for t in anomaly] == [(14, "default")]


@requires_elementary
def test_build_cost_names_nothing_for_a_project_without_the_package(
    tmp_path: Path, installed_elementary: Path
):
    from exmergo_dex_core.transform.elementary import read, unpriced_work

    project = elementary_project(tmp_path, "b", installed_elementary)
    for name in ("packages.yml", "package-lock.yml"):
        (project / name).unlink()
    shutil.rmtree(project / "dbt_packages")
    (project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: fct\n    columns:\n      - name: id\n"
        "        data_tests: [unique, not_null]\n",
        encoding="utf-8",
    )
    project_yml = yaml.safe_load((project / "dbt_project.yml").read_text("utf-8"))
    project_yml["models"].pop("elementary")
    (project / "dbt_project.yml").write_text(yaml.safe_dump(project_yml), "utf-8")
    manifest = _parsed(project, "dev")

    record = read(project, manifest)
    assert not record.installed
    assert unpriced_work(record, manifest).codes == []


# --- a pass that checked nothing (#1308) -----------------------------------------

#: ``fct``'s two Elementary tests, and a second model whose only test is one.
ELEMENTARY_FCT_TESTS = {
    "elementary_volume_anomalies_fct_ordered_at",
    "elementary_schema_changes_fct_",
}
ONLY_VOLUME = "elementary_volume_anomalies_only_volume_ordered_at"


@requires_elementary
@pytest.mark.parametrize(
    ("variant", "code"), [("d", "package_disabled"), ("a", "history_unmeasured")]
)
def test_an_elementary_test_that_checked_nothing_asserts_no_model(
    tmp_path: Path, installed_elementary: Path, variant: str, code: str
):
    """Variant d disables the package's models on dev, so its tests compile to
    the no-results query; variant a enables them on a dev target with no
    history. Either way the Elementary tests pass and are listed unchecked, a
    model whose only test is one is ``build_only``, and ``fct``, which has dbt's
    own tests too, is still ``asserted`` with none of those listed."""

    project = elementary_project(tmp_path, variant, installed_elementary)
    (project / "models" / "only_volume.sql").write_text(
        "select * from {{ ref('fct') }}\n", encoding="utf-8"
    )
    (project / "models" / "only_volume.yml").write_text(
        "version: 2\nmodels:\n  - name: only_volume\n    data_tests:\n"
        "      - elementary.volume_anomalies:\n"
        "          timestamp_column: ordered_at\n",
        encoding="utf-8",
    )
    result = _engine(project).build(target="dev")

    assurance = result.completion.assurance
    listed = {t.test: (t.models, t.reason_code) for t in assurance.unchecked_tests}
    assert listed == {
        **{name: (["fct"], code) for name in ELEMENTARY_FCT_TESTS},
        ONLY_VOLUME: (["only_volume"], code),
    }
    # With the package enabled its own models crowd the capped build_only
    # names, so only_volume is told apart by not being asserted.
    assert assurance.asserted.names == ["fct"]
    assert not FCT_TESTS - ELEMENTARY_FCT_TESTS & set(listed)
    assert any("unchecked_tests" in line for line in result.warnings)


# --- a release Dex was not run against (#1445) -----------------------------------


def _installed_project_file(project: Path) -> Path:
    return project / "dbt_packages" / "elementary" / "dbt_project.yml"


def _patch_above_newest() -> str:
    """A patch release above the newest the fixture runs, which the fixture's own
    constants decide, so the case moves with them."""

    major, minor, patch = (int(part) for part in CURRENT_VERSION.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _set_installed_release(project: Path, release: str) -> None:
    """Edit the installed package's own ``version``, and nothing else, so dbt and
    the package's code are those of the release the fixture installed."""

    path = _installed_project_file(project)
    own = yaml.safe_load(path.read_text("utf-8"))
    own["version"] = release
    path.write_text(yaml.safe_dump(own, sort_keys=False), "utf-8")


@requires_elementary
def test_a_release_above_the_newest_tested_is_unchecked_and_the_copy_stays_inert(
    tmp_path: Path, installed_elementary: Path, monkeypatch: pytest.MonkeyPatch
):
    """Acceptance, second case: the package at a patch above the newest release
    the fixture runs. A build lists each Elementary test under
    ``elementary_release_untested`` and counts none toward a model, the estimate
    record carries the code, the warning names the release and the newest
    tested, and a mutation run still leaves the dev warehouse as it found it."""

    from exmergo_dex_core.transform.build import ShadowRun
    from exmergo_dex_core.transform.elementary import read, unpriced_work

    release = _patch_above_newest()
    project = elementary_project(tmp_path, "a", installed_elementary)
    _set_installed_release(project, release)
    engine = _engine(project)

    result = engine.build(target="dev")

    listed = {
        t.test: (t.models, t.reason_code)
        for t in result.completion.assurance.unchecked_tests or []
    }
    assert {name: listed.get(name) for name in ELEMENTARY_FCT_TESTS} == dict.fromkeys(
        ELEMENTARY_FCT_TESTS, (["fct"], "elementary_release_untested")
    )
    assert {code for _, code in listed.values()} == {"elementary_release_untested"}
    # dbt's own tests on fct still pass and still count.
    assert "fct" in result.completion.assurance.asserted.names
    assert any(
        release in w and CURRENT_VERSION in w and "elementary_release_untested" in w
        for w in result.warnings
    )
    manifest = _parsed(project, "dev")
    record = read(project, manifest)
    work = unpriced_work(record, manifest, (project / "dbt_project.yml").read_text())
    assert (record.release, record.hooks) == (release, "unknown")
    assert work.as_dict()["release_reason_code"] == "elementary_release_untested"

    picked: list[str] = []
    test = ShadowRun.test

    def recording(self, select, **kwargs):
        picked.append(read(self.shadow, self.manifest()).materialization)
        return test(self, select, **kwargs)

    monkeypatch.setattr(ShadowRun, "test", recording)
    warehouse = tmp_path / "dev.duckdb"
    before = duckdb_census(warehouse)
    mutated = engine.test_mutations("fct")

    assert _executed(mutated) >= 1, "no mutant ran, so nothing was measured"
    assert footprint(before, duckdb_census(warehouse)) == {}
    assert any("elementary_release_untested" in w for w in mutated.warnings)
    # The copy disables the package's models; what the package's materialization
    # then does is the release's to say, so the copy also pins dbt's own.
    assert picked and set(picked) == {"dbt"}


@requires_elementary
def test_an_unreadable_installed_project_file_reports_the_release_unknown(
    tmp_path: Path, installed_elementary: Path
):
    """Acceptance, third case: the manifest dbt parsed for ``dev``, read after
    the installed package's ``dbt_project.yml`` stopped being readable. Every
    result that rests on the release carries ``elementary_release_unknown``."""

    from exmergo_dex_core.transform.elementary import (
        read,
        unchecked_tests,
        unpriced_work,
    )

    project = elementary_project(tmp_path, "a", installed_elementary)
    manifest = _parsed(project, "dev")
    _installed_project_file(project).write_bytes(b"\xff\xfe version: [")

    record = read(project, manifest)
    nodes = manifest["nodes"]
    listed = {
        nodes[uid]["name"]: code
        for uid, code in unchecked_tests(manifest, record).items()
    }
    work = unpriced_work(record, manifest, (project / "dbt_project.yml").read_text())

    assert record.installed
    assert record.release is None
    assert record.release_reason_code == "elementary_release_unknown"
    assert {name: listed.get(name) for name in ELEMENTARY_FCT_TESTS} == dict.fromkeys(
        ELEMENTARY_FCT_TESTS, "elementary_release_unknown"
    )
    assert set(listed.values()) == {"elementary_release_unknown"}
    assert work.as_dict()["release_reason_code"] == "elementary_release_unknown"
    assert "elementary_release_unknown" in (record.release_warning() or "")


# --- explore ranking leaves the package's tables out (#1311) ---------------------


@requires_elementary
@pytest.mark.parametrize("include", [True, False], ids=["included", "default"])
def test_explore_map_on_the_fixture_ranks_no_elementary_table(
    tmp_path: Path, installed_elementary: Path, include: bool
):
    """#1311's acceptance on the real package. Variant e builds the package's
    models into ``main_elementary`` and a project model, ``elementary_runs``, that
    reads one of them. With ``include_elementary`` the package's tables rank and
    are profiled like any other, and crowd a project table out of the map (the
    arm built to break it); by default none of them ranks, ``notes`` says so, and
    every table the project owns, the one reading the package included, ranks."""

    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.storage import FilesystemStore

    project = elementary_project(tmp_path, "e", installed_elementary)
    _engine(project).build(target="dev")
    warehouse = tmp_path / "dev.duckdb"
    built = duckdb_census(warehouse)
    package_tables = {name for schema, name, _ in built if schema == ELEMENTARY_SCHEMA}
    own_tables = {name for schema, name, _ in built if schema == "main"}
    assert {"dbt_run_results", "elementary_test_results"} <= package_tables
    assert {"raw_orders", "fct", "elementary_runs"} <= own_tables

    explore_root = tmp_path / "explore"
    explore_root.mkdir()
    engine = DexEngine(
        connector="duckdb",
        path=str(warehouse),
        config=DexConfig(connector="duckdb"),
        store=FilesystemStore(explore_root),
        repo_root=explore_root,
    )
    result = engine.map(include_elementary=include, full=True)

    def schema_of(identifier: str) -> str:
        return identifier.split(".")[-2]

    described = {o.identifier for o in result.objects}
    described_package = {i for i in described if schema_of(i) == ELEMENTARY_SCHEMA}
    described_own = {i.split(".")[-1] for i in described if schema_of(i) == "main"}
    if include:
        # The package's thirty-odd tables fill the 25-object payload and push
        # out a table the project owns: the defect #1311 removes.
        assert described_package
        assert own_tables - described_own
        assert result.excluded.count == 0
        return
    assert described_own == own_tables
    assert not described_package
    assert all(schema_of(o.identifier) != ELEMENTARY_SCHEMA for o in result.top_objects)
    assert result.excluded.reason == "elementary"
    assert result.excluded.count == len(package_tables)
    assert [g.namespace.split(".")[-1] for g in result.excluded.by_namespace] == [
        ELEMENTARY_SCHEMA
    ]
    assert any("Elementary" in n and ELEMENTARY_SCHEMA in n for n in result.notes)
    assert result.profiled_count == len(own_tables)


@requires_elementary
@pytest.mark.parametrize("include", [True, False], ids=["included", "default"])
def test_explore_diagram_and_relationships_on_the_fixture_leave_the_package_out(
    tmp_path: Path, installed_elementary: Path, include: bool
):
    """#1494's acceptance on the real package, variant e. After an include map
    filled the cache with the package's profiles, a default `diagram --full`
    draws none of them and says so, and `relationships` profiles none of them;
    with `include_elementary` both take them with the rest. `elementary_runs`,
    the project's own model in the same warehouse, is drawn and profiled either
    way."""

    from exmergo_dex_core.config import DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.storage import FilesystemStore

    project = elementary_project(tmp_path, "e", installed_elementary)
    _engine(project).build(target="dev")
    warehouse = tmp_path / "dev.duckdb"
    built = duckdb_census(warehouse)
    package_tables = {name for schema, name, _ in built if schema == ELEMENTARY_SCHEMA}
    own_tables = {name for schema, name, _ in built if schema == "main"}
    assert "elementary_runs" in own_tables

    def engine(root: Path) -> DexEngine:
        root.mkdir()
        return DexEngine(
            connector="duckdb",
            path=str(warehouse),
            config=DexConfig(connector="duckdb"),
            store=FilesystemStore(root),
            repo_root=root,
        )

    def schema_of(identifier: str) -> str:
        return identifier.split(".")[-2]

    drawing = engine(tmp_path / "drawing")
    drawing.map(include_elementary=True, full=True)
    diagram = drawing.diagram(full=True, include_elementary=include)
    drawn = set(diagram.entities.values())
    drawn_package = {i for i in drawn if schema_of(i) == ELEMENTARY_SCHEMA}
    drawn_own = {i.split(".")[-1] for i in drawn if schema_of(i) == "main"}
    assert "elementary_runs" in drawn_own

    related = engine(tmp_path / "relating").relationships(include_elementary=include)
    endpoints = {r.from_dataset for r in related.relationships} | {
        r.to_dataset for r in related.relationships
    }
    if include:
        # The package's thirty-odd tables and the project's three fit under the
        # diagram's 40-entity cap, so every one of them is drawn.
        assert drawn_package == {
            f"dev.{ELEMENTARY_SCHEMA}.{name}" for name in package_tables
        }
        assert diagram.elided_entity_count == 0
        assert diagram.excluded.count == 0
        assert related.profiled_count == len(own_tables) + len(package_tables)
        assert any(schema_of(i) == ELEMENTARY_SCHEMA for i in endpoints)
        assert related.excluded.count == 0
        return
    assert not drawn_package
    assert diagram.excluded.reason == "elementary"
    assert diagram.excluded.count == len(package_tables)
    assert any(
        "Elementary" in n and ELEMENTARY_SCHEMA in n and "`explore diagram`" in n
        for n in diagram.notes
    )
    assert related.profiled_count == len(own_tables)
    assert not any(schema_of(i) == ELEMENTARY_SCHEMA for i in endpoints)
    assert related.excluded.reason == "elementary"
    assert related.excluded.count == len(package_tables)
    assert any(
        "Elementary" in n and ELEMENTARY_SCHEMA in n and "`explore relationships`" in n
        for n in related.notes
    )


# --- the install dex init offers (#1464) -------------------------------------------


def _with_install_applied(project: Path, installed: Path) -> Path:
    """``project`` stripped of the package, with the install ``dex init`` offers
    written in its place, then the package installed as ``transform deps``
    would from the lockfile the fixture's installer resolved."""

    from exmergo_dex_core.transform.elementary_install import install_edits

    (project / "packages.yml").unlink()
    (project / "package-lock.yml").unlink()
    shutil.rmtree(project / "dbt_packages")
    authored = yaml.safe_load((project / "dbt_project.yml").read_text("utf-8"))
    authored.pop("models")
    (project / "dbt_project.yml").write_text(
        yaml.safe_dump(authored, sort_keys=False), encoding="utf-8"
    )
    for edit in install_edits(project, "dev"):
        (project / edit.path).write_text(edit.new_content, encoding="utf-8")
    shutil.copy(installed / "package-lock.yml", project / "package-lock.yml")
    shutil.copytree(installed / "dbt_packages", project / "dbt_packages")
    return project


@requires_elementary
def test_the_offered_install_runs_nothing_of_the_package_on_dev(
    tmp_path: Path, installed_elementary: Path
):
    """Both dbt and the resolver read the rendered ``+enabled`` string as a
    boolean per target: parsed for ``dev`` the package and its hooks are
    disabled, and a dev build creates none of its relations; parsed for
    ``prod``, never built, they are back."""

    from exmergo_dex_core.transform.elementary import read
    from exmergo_dex_core.transform.elementary_install import INSTALL_RELEASE

    project = _with_install_applied(
        elementary_project(tmp_path, "a", installed_elementary), installed_elementary
    )
    dev = read(project, _parsed(project, "dev"))
    assert dev.installed and dev.release == INSTALL_RELEASE
    assert (dev.enabled, dev.hooks, dev.materialization) == (False, "disabled", "dbt")

    warehouse = tmp_path / "dev.duckdb"
    before = duckdb_census(warehouse)
    dbt(
        "build",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--target",
        "dev",
        cwd=project,
        check=False,
    )
    results = json.loads((project / "target" / "run_results.json").read_text("utf-8"))
    assert not [
        r["unique_id"] for r in results["results"] if ".elementary." in r["unique_id"]
    ]
    created = footprint(before, duckdb_census(warehouse))
    assert not [rel for rel in created if rel[0] == ELEMENTARY_SCHEMA]
    assert created  # the project's own models did build

    prod = read(project, _parsed(project, "prod"))
    assert (prod.enabled, prod.hooks) == (True, "run")
    assert not (tmp_path / "prod.duckdb").exists()


# --- the reader's table layouts (#1469) ---------------------------------------------


def _rows(warehouse: Path, columns: str, table: str, where: str = "") -> list[tuple]:
    """``columns`` of one of Elementary's tables, read-only. Every argument is a
    literal in this file, never a value read from the warehouse."""

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(warehouse), read_only=True)
    sql = f"select {columns} from {ELEMENTARY_SCHEMA}.{table} {where}"  # noqa: S608
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _built_columns(warehouse: Path, table: str) -> list[tuple]:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = ? and table_name = ? order by ordinal_position",
            [ELEMENTARY_SCHEMA, table],
        ).fetchall()
    finally:
        con.close()


def _dbt_command(project: Path, command: str) -> dict:
    """Run ``command`` and return the run results it wrote."""

    run_results = project / "target" / "run_results.json"
    run_results.unlink(missing_ok=True)
    project_args = ("--project-dir", str(project), "--profiles-dir", str(project))
    log = dbt(command, *project_args, cwd=project, check=False)
    assert run_results.exists(), f"dbt {command} wrote no run results:\n{log}"
    return json.loads(run_results.read_text(encoding="utf-8"))


def _layout_check(warehouse: Path, release: str | None):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.maintain.elementary_layouts import (
        adapter_catalog,
        check_layouts,
    )

    adapter = DuckDBAdapter(warehouse)
    try:
        catalog = adapter_catalog(adapter, f"dev.{ELEMENTARY_SCHEMA}")
        return check_layouts(release, catalog)
    finally:
        adapter.close()


def _built_layout_is_the_recorded_one(warehouse: Path) -> None:
    """Every column the package built, in order and family, is the recorded one."""

    from exmergo_dex_core.maintain.elementary_layouts import layout_for, type_family

    for table, layout in layout_for(CURRENT_VERSION).tables.items():
        built = _built_columns(warehouse, table)
        assert [name for name, _ in built] == [name for name, _ in layout.columns]
        for name, data_type in built:
            assert type_family(data_type) == layout.family(name), (table, name)


def _invocations_read_back(warehouse: Path, ran: dict, tested: dict) -> None:
    invocations = _rows(
        warehouse,
        "invocation_id, command, elementary_version, run_started_at, created_at",
        "dbt_invocations",
    )
    assert {(i, c) for i, c, *_ in invocations} == {
        (ran["metadata"]["invocation_id"], "run"),
        (tested["metadata"]["invocation_id"], "test"),
    }
    for _, _, version, started, created in invocations:
        assert version == CURRENT_VERSION
        # A string at 0.26.1, as the recorded layout says, and an ISO time.
        assert isinstance(started, str)
        assert datetime.fromisoformat(started)
        assert isinstance(created, datetime)


def _results_read_back(warehouse: Path, tested: dict) -> dict[str, str]:
    """Each test's rows against dbt's own result; ``{test_unique_id: id}``."""

    # Run results also hold the package's on-run-start and on-run-end
    # operations, which write no test result.
    statuses = {
        r["unique_id"]: (r["status"], r["failures"])
        for r in tested["results"]
        if r["unique_id"].startswith("test.")
    }
    results = _rows(
        warehouse,
        "id, test_unique_id, invocation_id, status, failures, detected_at",
        "elementary_test_results",
    )
    assert len({row[0] for row in results}) == len(results)
    assert {row[2] for row in results} == {tested["metadata"]["invocation_id"]}
    assert {row[1] for row in results} == set(statuses)
    for _, uid, _, status, failures, detected in results:
        assert isinstance(detected, datetime)
        # Elementary counts its own tests' failures, and marks an anomaly row
        # with none `pass` whatever dbt said (get_normalized_test_status,
        # handle_tests_results.sql:62-76); dbt's status and count hold for the
        # rest. The tests are the root project's, so the name says whose.
        if _node_name(uid).startswith("elementary_"):
            assert status in {statuses[uid][0], "pass"}, uid
        else:
            assert (status, failures) == statuses[uid], uid
    return {uid: result_id for result_id, uid, *_ in results}


def _node_name(unique_id: str) -> str:
    """A test's name: ``test.<project>.<name>.<hash>`` without the rest."""

    return unique_id.split(".")[2]


def _samples_and_metrics_read_back(
    warehouse: Path, result_ids: dict[str, str], sampled_result: str
) -> None:
    # The anomaly test writes every scored bucket here too, unsampled, so only
    # the failing test's rows are counted; each row points at a result.
    samples = _rows(
        warehouse, "elementary_test_results_id, row_index", "test_result_rows"
    )
    assert {result_id for result_id, _ in samples} <= set(result_ids.values())
    assert [i for r, i in samples if r == sampled_result] == [1]

    metrics = _rows(
        warehouse,
        "id, bucket_start, bucket_end, bucket_duration_hours, metric_value",
        "data_monitoring_metrics",
        "where metric_name = 'row_count' and lower(full_table_name) like '%.fct'",
    )
    assert metrics
    assert len({m[0] for m in metrics}) == len(metrics)
    for _, start, end, hours, value in metrics:
        assert hours == 24
        assert end - start == timedelta(hours=24)
        assert value >= 0


def _changed_layout_refuses(tmp_path: Path, warehouse: Path) -> None:
    """The other arm, on a copy of the same warehouse: a layout it does not know."""

    from exmergo_dex_core.maintain.elementary_layouts import ElementaryLayoutError

    changed = tmp_path / "changed" / "dev.duckdb"
    changed.parent.mkdir()
    shutil.copy(warehouse, changed)
    _execute(
        changed,
        f"alter table {ELEMENTARY_SCHEMA}.dbt_invocations drop column created_at",
        f"alter table {ELEMENTARY_SCHEMA}.elementary_test_results "
        "alter detected_at type varchar",
    )
    with pytest.raises(ElementaryLayoutError) as caught:
        _layout_check(changed, CURRENT_VERSION)
    assert [(p.code, p.table, p.column) for p in caught.value.problems] == [
        ("elementary_column_retyped", "elementary_test_results", "detected_at"),
        ("elementary_column_missing", "dbt_invocations", "created_at"),
    ]


@requires_elementary
def test_the_reader_s_layouts_hold_on_the_fixture_and_its_rows_read_back(
    tmp_path: Path, installed_elementary: Path
):
    """#1469 at 0.26.1, on variant a, where Elementary's materialization handles
    the tests. One duplicated key makes the grain's ``unique`` test fail on one
    value, with its sampling switch removed so the sample is written. The
    catalog check passes on what the package built, the built layout is the
    recorded one column for column, and each table reads back to what the two
    runs did. A copy with one column dropped and one retyped refuses."""

    # Imported before any dbt run, so a reader that is absent fails here.
    from exmergo_dex_core.maintain.elementary_layouts import READER_RELEASES
    from exmergo_dex_core.transform.elementary import read

    assert CURRENT_VERSION in READER_RELEASES
    project = elementary_project(tmp_path, "a", installed_elementary)
    warehouse = tmp_path / "dev.duckdb"
    _execute(
        warehouse,
        "insert into main.raw_orders select * from main.raw_orders where id = 1",
    )
    _strip_sampling_switch(project)
    ran = _dbt_command(project, "run")
    tested = _dbt_command(project, "test")
    [unique] = [
        r for r in tested["results"] if _node_name(r["unique_id"]) == "unique_fct_id"
    ]
    assert (unique["status"], unique["failures"]) == ("fail", 1)

    manifest = json.loads((project / "target" / "manifest.json").read_text("utf-8"))
    check = _layout_check(warehouse, read(project, manifest).release)
    assert check.release == CURRENT_VERSION
    _built_layout_is_the_recorded_one(warehouse)
    _invocations_read_back(warehouse, ran, tested)
    result_ids = _results_read_back(warehouse, tested)
    _samples_and_metrics_read_back(
        warehouse, result_ids, result_ids[unique["unique_id"]]
    )
    _changed_layout_refuses(tmp_path, warehouse)
    assert not (tmp_path / "prod.duckdb").exists()
