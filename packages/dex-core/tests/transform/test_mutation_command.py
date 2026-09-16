"""`transform test --mutate`: what it refuses for free, what it runs, and where.

Two halves. The first fakes the dbt subprocess, so the refusals, the argv, the
environment and the budget loop are asserted without a warehouse. The second
runs real dbt against DuckDB, because the properties that matter most here are
properties of dbt's own behaviour: that an ephemeral mutant still gets its tests
run, and that nothing is written to the project or the warehouse while it
happens. Faking dbt cannot establish either.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform import commands as transform_commands
from exmergo_dex_core.transform import mutation

MODEL_SQL = """select
    o.id,
    o.amount,
    c.region,
    case when o.amount > 100 then 'large' else 'small' end as band
from {{ ref('stg_orders') }} o
inner join {{ ref('stg_customers') }} c on o.cid = c.id
where o.status <> 'cancelled'
"""


def _engine(project: Path, connector: str = "duckdb", **kwargs) -> DexEngine:
    return DexEngine(
        connector=connector,
        repo_root=str(project.parent),
        store=FilesystemStore(project.parent),
        config=DexConfig(
            connector=connector, dbt_target="dev", dbt_project_dir=project.name
        ),
        **kwargs,
    )


def _skip_dev_target_check(monkeypatch) -> None:
    """The dev-target preflight opens a connection; these cases are about what
    dbt was asked to do, not about whether the target is deployable."""

    monkeypatch.setattr(
        importlib.import_module("exmergo_dex_core.transform.dev_target"),
        "check",
        lambda *a, **k: [],
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            part in {"target", "logs", ".git"} for part in path.parts
        ):
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --- the faked-dbt half --------------------------------------------------------


@pytest.fixture
def recorded_dbt(monkeypatch):
    """Record every dbt argv and answer with artifacts the caller asked for."""

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []
    plan: dict[str, object] = {"manifest": {}, "results": [], "returncode": 0}

    def fake(timeout, cwd, env=None):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": Path(cwd), "env": env or {}})
            target_path = Path(argv[argv.index("--target-path") + 1])
            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "manifest.json").write_text(json.dumps(plan["manifest"]))
            if argv[1] == "test":
                (target_path / "run_results.json").write_text(
                    json.dumps({"results": plan["results"]})
                )
            return subprocess.CompletedProcess(
                args=argv, returncode=plan["returncode"], stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    return calls, plan


def _manifest(materialized: str = "ephemeral", *, tests=True, **node_overrides):
    nodes = {
        "model.p.fct": {
            "name": "fct",
            "unique_id": "model.p.fct",
            "package_name": "p",
            "language": "sql",
            "config": {"materialized": materialized},
            "depends_on": {"nodes": ["model.p.stg_orders"]},
            "compiled_code": 'select a from "d"."m"."stg_orders" where a > 1',
            **node_overrides,
        },
        "model.p.stg_orders": {
            "name": "stg_orders",
            "unique_id": "model.p.stg_orders",
            "package_name": "p",
            "config": {"materialized": "view"},
            "relation_name": '"d"."m"."stg_orders"',
        },
    }
    if tests:
        nodes["test.p.not_null_fct_a.abc123"] = {
            "name": "not_null_fct_a",
            "unique_id": "test.p.not_null_fct_a.abc123",
            "resource_type": "test",
            "attached_node": "model.p.fct",
            "depends_on": {"nodes": ["model.p.fct"]},
            "compiled_code": "select 1",
        }
    return {"metadata": {"project_name": "p"}, "nodes": nodes, "unit_tests": {}}


def _results(status: str = "pass"):
    return [
        {
            "unique_id": "test.p.not_null_fct_a.abc123",
            "status": status,
            "execution_time": 0.1,
        }
    ]


@pytest.fixture
def project(dbt_project_dir: Path) -> Path:
    (dbt_project_dir / "models" / "staging" / "fct.sql").write_text(
        MODEL_SQL, encoding="utf-8"
    )
    return dbt_project_dir


def test_it_runs_dbt_test_and_never_build_or_run(project, recorded_dbt, monkeypatch):
    """A build runs a model's unit tests before the model, so one failing unit
    test skips the model and the skip cascades onto every data test attached to
    it. The run would then report tests as skipped with no way to tell which
    would have caught the defect. `dbt test` also executes no materialization,
    so nothing can write a relation."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    transform_commands.test_mutations(_engine(project), "fct")
    verbs = {call["argv"][1] for call in calls}
    assert verbs <= {"compile", "test", "parse", "deps"}
    assert "build" not in verbs and "run" not in verbs


def test_dbt_writes_into_the_copy_while_cwd_stays_at_the_real_project(
    project, recorded_dbt, monkeypatch
):
    """dbt resolves target/ and logs/ against --project-dir, and dbt-duckdb
    resolves a relative profile path against the process cwd. Only this split
    keeps the artifacts in the copy and the warehouse reachable."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    transform_commands.test_mutations(_engine(project), "fct")
    for call in calls:
        argv = call["argv"]
        assert call["cwd"] == project.resolve()
        assert Path(argv[argv.index("--project-dir") + 1]) != project.resolve()
        for flag in ("--target-path", "--log-path"):
            assert project.resolve() not in Path(argv[argv.index(flag) + 1]).parents


def test_the_selection_flags_are_pinned_rather_than_inherited(
    project, recorded_dbt, monkeypatch
):
    """`DBT_INDIRECT_SELECTION=cautious` in a caller's environment would drop the
    tests from the selection. Every mutant would then survive and the report
    would call the suite weak when it was never asked."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    monkeypatch.setenv("DBT_INDIRECT_SELECTION", "cautious")
    monkeypatch.setenv("DBT_TARGET_PATH", "/elsewhere/target")
    _skip_dev_target_check(monkeypatch)
    transform_commands.test_mutations(_engine(project), "fct")
    for call in (c for c in calls if c["argv"][1] == "test"):
        argv = call["argv"]
        assert argv[argv.index("--indirect-selection") + 1] == "eager"
        assert "--no-defer" in argv and "--no-fail-fast" in argv
        assert "DBT_INDIRECT_SELECTION" not in call["env"]
        assert "DBT_TARGET_PATH" not in call["env"]
        assert call["env"]["DO_NOT_TRACK"] == "1"


@pytest.mark.parametrize(
    "manifest,message",
    [
        (_manifest(tests=False), "has no tests"),
        (_manifest(language="python"), "Python model"),
        (_manifest("table"), "could not make"),
        (
            _manifest(config={"materialized": "ephemeral", "sql_header": "set x=1"}),
            "sql_header",
        ),
    ],
)
def test_a_model_dex_will_not_mutate_is_refused_before_any_test_runs(
    project, recorded_dbt, monkeypatch, manifest, message
):
    """Every one of these is answered from the parse, so a model dex cannot
    measure costs nothing to ask about."""

    calls, plan = recorded_dbt
    plan["manifest"] = manifest
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    with pytest.raises(mutation.MutationError, match=message):
        transform_commands.test_mutations(_engine(project), "fct")
    assert not [c for c in calls if c["argv"][1] == "test"]


def test_a_cap_above_the_ceiling_is_refused_without_touching_dbt(project, recorded_dbt):
    calls, _ = recorded_dbt
    with pytest.raises(ValueError, match="above the engine ceiling"):
        transform_commands.test_mutations(_engine(project), "fct", max_mutants=999)
    assert calls == []


def test_a_suite_with_nothing_passing_stops_rather_than_measuring_it(
    project, recorded_dbt, monkeypatch
):
    """Every verdict is relative to what passed before anything was mutated, so
    a suite with nothing passing has nothing that could catch a defect."""

    _calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results("fail")
    _skip_dev_target_check(monkeypatch)
    with pytest.raises(mutation.MutationError, match="passes against the unmutated"):
        transform_commands.test_mutations(_engine(project), "fct")


def test_run_hooks_are_stripped_from_the_copy_and_said_so(
    project, recorded_dbt, monkeypatch
):
    """A hook fires once per invocation and this invokes dbt once per mutant, so
    a hook that grants or audits would fire N+1 times for a command the caller
    thinks of as read only."""

    _calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    manifest_path = project / "dbt_project.yml"
    manifest_path.write_text(
        manifest_path.read_text() + 'on-run-start:\n  - "select 1"\n', encoding="utf-8"
    )
    _skip_dev_target_check(monkeypatch)
    result = transform_commands.test_mutations(_engine(project), "fct")
    assert any("hooks are not run" in w for w in result.warnings)
    assert "on-run-start" in manifest_path.read_text()


# --- the real-dbt half ---------------------------------------------------------


@pytest.fixture
def measurable_project(tmp_path: Path, duckdb_file: Path) -> Path:
    """A project whose one mart has a suite worth measuring, on real DuckDB."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "dev.duckdb"
    shutil.copy(duckdb_file, warehouse)
    con = duckdb.connect(str(warehouse))
    con.execute(
        "create or replace table main.raw_orders as select * from (values "
        "(1, 7, 100.0::double, 'placed'), (2, 7, 10.0::double, 'placed'), "
        "(3, 8, 20.0::double, 'cancelled')) t(id, cid, amount, status)"
    )
    con.execute(
        "create or replace table main.raw_customers as select * from (values "
        "(7, 'eu'), (8, 'us')) t(id, region)"
    )
    con.close()

    project = tmp_path / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        f"p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {warehouse}\n",
        encoding="utf-8",
    )
    (project / "models" / "stg_orders.sql").write_text(
        "select id, cid, amount, status from main.raw_orders", encoding="utf-8"
    )
    (project / "models" / "stg_customers.sql").write_text(
        "select id, region from main.raw_customers", encoding="utf-8"
    )
    (project / "models" / "fct.sql").write_text(MODEL_SQL, encoding="utf-8")
    return project


def _schema_yml(unit_test: bool) -> str:
    base = """version: 2
models:
  - name: fct
    columns:
      - name: id
        data_tests: [not_null]
"""
    if not unit_test:
        return base
    return (
        base
        + """
unit_tests:
  - name: fct_pins_its_rules
    model: fct
    given:
      - input: ref('stg_orders')
        rows:
          - {id: 1, cid: 7, amount: 100.0, status: 'placed'}
          - {id: 2, cid: 7, amount: 10.0, status: 'placed'}
          - {id: 3, cid: 8, amount: 20.0, status: 'cancelled'}
          - {id: 4, cid: 99, amount: 30.0, status: 'placed'}
      - input: ref('stg_customers')
        rows:
          - {id: 7, region: 'eu'}
    expect:
      rows:
        - {id: 1, region: 'eu', band: 'small'}
        - {id: 2, region: 'eu', band: 'small'}
"""
    )


@pytest.mark.parametrize("with_unit_test", [False, True])
def test_a_stronger_suite_catches_more_planted_defects(
    measurable_project: Path, with_unit_test: bool
):
    """The acceptance the issue asks for, both directions in one test: a model
    carrying only a `not_null` lets the defects through, and adding one unit test
    that pins the model's actual rules catches most of them."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(with_unit_test), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    counts = result.counts
    assert counts["generated"] >= 4
    if with_unit_test:
        assert counts["killed"] > counts["survived"], result.mutants
        assert result.score > 0.5
    else:
        assert counts["killed"] == 0
        assert counts["survived"] == counts["generated"]
        assert result.score == 0.0


def test_every_kind_of_test_gets_to_answer_for_the_mutant(measurable_project: Path):
    """Generic, singular and unit tests all run against an ephemeral mutant. If
    any kind were silently dropped, its defects would all read as survivors."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(True), encoding="utf-8"
    )
    (measurable_project / "tests").mkdir()
    (measurable_project / "tests" / "no_null_region.sql").write_text(
        "select id from {{ ref('fct') }} where region is null", encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    names = {test["name"] for test in result.baseline["tests"]}
    assert "not_null_fct_id" in names
    assert "no_null_region" in names
    assert "fct_pins_its_rules" in names


def test_a_mutation_run_writes_nothing_and_materializes_nothing(
    measurable_project: Path,
):
    """The two guarantees the safety spine turns on: the project is a byte for
    byte match afterwards, and the dev warehouse gained no relation."""

    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(True), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    warehouse = measurable_project.parent / "dev.duckdb"

    def relations():
        con = duckdb.connect(str(warehouse), read_only=True)
        try:
            return set(
                con.execute(
                    "select table_schema, table_name from information_schema.tables"
                ).fetchall()
            )
        finally:
            con.close()

    before_tree, before_relations = _tree_digest(measurable_project), relations()
    engine.test_mutations("fct")

    assert _tree_digest(measurable_project) == before_tree
    assert relations() == before_relations


def test_the_cap_narrows_the_run_and_reports_what_it_cut(measurable_project: Path):
    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct", max_mutants=2)
    assert result.counts["generated"] == 2
    assert result.cap["limit"] == 2
    assert sum(result.cap["elided"].values()) > 0
    assert any("not run" in w and "cap" in w for w in result.warnings)


def test_the_batch_reports_one_spend_rather_than_a_sum_of_flags():
    """A spend payload is not uniformly additive, and treating it that way is
    how nine runs reported `settled: 9`.

    What each run billed sums. The day's cumulative total is already cumulative,
    so summing it reports the day nine times over. The two flags are claims about
    the command as a whole: it settled only if every run did, and its settlement
    is unknown if any run's was.
    """

    from exmergo_dex_core.transform.commands import _merge_spend

    first = {
        "bytes_billed": 100.0,
        "session_spent_today": 1_000.0,
        "settled": True,
        "unknown_settlement": False,
        "reserved": None,
    }
    second = {
        "bytes_billed": 50.0,
        "session_spent_today": 1_050.0,
        "settled": True,
        "unknown_settlement": False,
        "reserved": None,
    }
    merged = _merge_spend(_merge_spend(None, first), second)
    assert merged["bytes_billed"] == 150.0
    assert merged["session_spent_today"] == 1_050.0
    assert merged["settled"] is True
    assert merged["unknown_settlement"] is False


def test_one_run_with_unknown_spend_makes_the_batch_unknown():
    """Known plus unknown is unknown, so the flags cannot be averaged away by a
    majority of well-behaved runs."""

    from exmergo_dex_core.transform.commands import _merge_spend

    known = {"bytes_billed": 100.0, "settled": True, "unknown_settlement": False}
    unknown = {"bytes_billed": None, "settled": False, "unknown_settlement": True}
    merged = _merge_spend(_merge_spend(None, known), unknown)
    assert merged["settled"] is False
    assert merged["unknown_settlement"] is True


def test_the_run_stops_when_the_confirmed_budget_runs_out(
    project, recorded_dbt, monkeypatch
):
    """The batch is priced upfront, but a run can still outrun its estimate, and
    the guard has to bind on what was actually spent rather than on what was
    predicted.

    Stopping and reporting the remainder as `not_run` is the honest outcome: a
    shorter list of survivors read as a clean bill would be the one way this
    command could mislead about cost and coverage at once.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform import mutation
    from exmergo_dex_core.transform.commands import _run_mutants

    class _Shadow:
        """Answers every run with spend that overshoots the per-run estimate."""

        def __init__(self):
            self.runs = 0

        def write(self, *args, **kwargs):
            pass

        def test(self, model, **kwargs):
            self.runs += 1
            return {
                "success": True,
                "nodes": [
                    {
                        "unique_id": "test.p.not_null.abc",
                        "name": "not_null",
                        "status": "pass",
                        "execution_time": 40.0,
                    }
                ],
            }

    prepared = mutation.prepare(
        "select a from t where a > 1 and b > 2 and c > 3", dialect="duckdb"
    )
    batch = mutation.enumerate_mutants(prepared)
    shadow = _Shadow()
    result = _run_mutants(
        shadow,
        "models/staging/fct.sql",
        batch,
        model="fct",
        paradigm=Paradigm.COMPUTE_TIME,
        connector="snowflake",
        store=FilesystemStore(project.parent),
        ceiling=100.0,
        estimate=90.0,
        mutation_mod=mutation,
    )
    _runs, _spend, warnings = result.pop("_meta")

    assert result["counts"]["not_run"] > 0
    assert shadow.runs < len(batch.mutants) + 1, "the run kept going past the budget"
    assert any("budget covered" in w for w in warnings)
    statuses = {m["id"]: m["status"] for m in result["mutants"]}
    assert "not_run" in statuses.values()


def test_the_days_total_excludes_the_reservation_the_command_is_still_holding():
    """A reservation is headroom, not spend, and this command holds one across
    every run in the batch.

    Each run settles its own row while that reservation still stands, so a day's
    total read during the loop counts the whole batch estimate on top of what the
    runs actually billed. `transform build` releases before it reads for the same
    reason; the difference here is that the overstatement is the entire estimate
    rather than a rounding error.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.commands import _refresh_session_total

    class _Gate:
        def __init__(self):
            self.settled = False

        def settle(self):
            self.settled = True

    class _Store:
        def spend_since(self, cutoff, *, field, connector):
            return 6_000.0

    gate = _Gate()
    spend = {"bytes_billed": 6_000.0, "session_spent_today": 2_103_152.0}
    refreshed = _refresh_session_total(
        spend, gate, _Store(), Paradigm.BYTES_SCANNED, "bigquery"
    )
    assert gate.settled, "the reservation has to be released before the read"
    assert refreshed["session_spent_today"] == 6_000.0
    assert refreshed["bytes_billed"] == 6_000.0, "what the runs billed is unchanged"
