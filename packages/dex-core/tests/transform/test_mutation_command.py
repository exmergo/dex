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


LEAKED = "someone@example.com"


def _show_line(rows) -> str:
    return json.dumps(
        {
            "info": {"name": "ShowNode", "level": "info", "msg": "Previewing"},
            "data": {"preview": json.dumps(rows)},
        }
    )


@pytest.fixture
def recorded_dbt(monkeypatch):
    """Record every dbt argv and answer with artifacts the caller asked for.

    ``plan["test_results"]``, when set, is a queue of result lists, one per
    `dbt test` in order; otherwise every test run answers ``plan["results"]``.
    ``plan["show_rows"]`` is a queue of previews, one per `dbt show`: a list of
    rows is printed as dbt's ShowNode event, and ``None`` is a failed show
    whose error line quotes a data value, the way a warehouse error can.
    """

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []
    plan: dict[str, object] = {
        "manifest": {},
        "results": [],
        "returncode": 0,
        "show_rows": [],
    }

    def fake(timeout, cwd, env=None):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": Path(cwd), "env": env or {}})
            target_path = Path(argv[argv.index("--target-path") + 1])
            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "manifest.json").write_text(json.dumps(plan["manifest"]))
            returncode, stdout = plan["returncode"], ""
            if argv[1] == "test":
                queue = plan.get("test_results")
                results = queue.pop(0) if queue else plan["results"]
                (target_path / "run_results.json").write_text(
                    json.dumps({"results": results})
                )
            if argv[1] == "show":
                (target_path / "run_results.json").write_text(
                    json.dumps(
                        {
                            "results": [
                                {
                                    "unique_id": "model.p.dex_mutation_equivalence",
                                    "status": "success",
                                    "execution_time": 0.1,
                                }
                            ]
                        }
                    )
                )
                queue = plan["show_rows"]
                rows = queue.pop(0) if queue else None
                if rows is None:
                    returncode = 1
                    stdout = json.dumps(
                        {
                            "info": {
                                "name": "RunResultError",
                                "level": "error",
                                "msg": f"Bad int64 value: '{LEAKED}'",
                            }
                        }
                    )
                else:
                    stdout = _show_line(rows)
            return subprocess.CompletedProcess(
                args=argv, returncode=returncode, stdout=stdout, stderr=""
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
    so nothing can write a relation, and neither does `dbt show`, which the
    equivalence check reads its counts back through."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    transform_commands.test_mutations(_engine(project), "fct")
    verbs = {call["argv"][1] for call in calls}
    assert verbs <= {"compile", "test", "parse", "deps", "show"}
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


# --- equivalence on the dev data, faked --------------------------------------


def _counts(mutant_rows, baseline_rows, only_in_mutant, only_in_baseline):
    return [
        {
            "mutant_rows": mutant_rows,
            "baseline_rows": baseline_rows,
            "only_in_mutant": only_in_mutant,
            "only_in_baseline": only_in_baseline,
        }
    ]


def _queue_runs(plan, *statuses: str) -> None:
    """One `dbt test` answer per run: the baseline first, then each mutant."""

    plan["test_results"] = [_results(status) for status in statuses]


def test_only_survivors_are_compared_after_the_self_check(
    project, recorded_dbt, monkeypatch
):
    """The issue scopes the comparison to survivors, and the self-check runs
    first, while the model file still holds the unmutated model, so a model
    that cannot reproduce itself is found before anything is spent on labels.

    The mutants of `where a > 1` are, in order, a boundary flip, a dropped
    filter and a negated one. The first is killed; the other two survive, one
    changing the output on the dev data and one not.
    """

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    _queue_runs(plan, "pass", "fail", "pass", "pass")
    plan["show_rows"] = [_counts(4, 4, 0, 0), _counts(5, 4, 1, 0), _counts(4, 4, 0, 0)]
    _skip_dev_target_check(monkeypatch)

    result = transform_commands.test_mutations(_engine(project), "fct")

    verbs = [call["argv"][1] for call in calls]
    assert verbs[verbs.index("test") :] == [
        "test", "show", "test", "test", "show", "test", "show",
    ]  # fmt: skip
    by_id = {m["id"]: m for m in result.mutants}
    assert by_id["m01"]["status"] == "killed"
    assert by_id["m01"]["equivalence"] is None
    assert by_id["m02"]["equivalence"]["status"] == "distinguishable"
    assert by_id["m02"]["equivalence"]["only_in_mutant"] == 1
    assert by_id["m03"]["equivalence"]["status"] == "equivalent"
    assert by_id["m03"]["equivalence"]["baseline_rows"] == 4
    assert result.equivalence == {
        "checked": True,
        "self_check": "reproducible",
        "distinguishable": 1,
        "equivalent": 1,
        "not_checked": 0,
        "reason": None,
    }
    assert [m["id"] for m in result.mutants] == ["m02", "m03", "m01"]
    assert "unit test" in by_id["m03"]["suggested_test"]
    assert by_id["m02"]["suggested_test"] != by_id["m03"]["suggested_test"]
    assert any("1 change the model's output" in w for w in result.warnings)
    assert result.counts["survived"] == 2 and result.score == pytest.approx(1 / 3)


def test_a_model_that_does_not_reproduce_itself_labels_no_survivor(
    project, recorded_dbt, monkeypatch
):
    """Two evaluations of the unmutated model that disagree mean every mutant
    would look different from it, so a label would not be evidence of anything.
    One show is spent learning that, and none after it."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    plan["show_rows"] = [_counts(4, 4, 2, 2)]
    _skip_dev_target_check(monkeypatch)

    result = transform_commands.test_mutations(_engine(project), "fct")

    assert [c["argv"][1] for c in calls].count("show") == 1
    assert result.equivalence["self_check"] == "not_reproducible"
    assert "does not reproduce its own output" in result.equivalence["reason"]
    survivors = [m for m in result.mutants if m["status"] == "survived"]
    assert survivors
    assert all(m["equivalence"]["status"] == "not_checked" for m in survivors)
    assert any("not labelled" in w for w in result.warnings)


def test_a_comparison_that_fails_is_unlabelled_and_carries_no_dbt_text(
    project, recorded_dbt, monkeypatch
):
    """A warehouse error can quote the value it failed on, so a failed show
    leaves the survivor unlabelled and none of dbt's text in the envelope."""

    _calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    plan["show_rows"] = [_counts(4, 4, 0, 0), None, _counts(4, 4, 0, 0), None]
    _skip_dev_target_check(monkeypatch)

    result = transform_commands.test_mutations(_engine(project), "fct")

    labels = [m["equivalence"]["status"] for m in result.mutants]
    assert labels.count("not_checked") == 2
    assert LEAKED not in json.dumps(result.model_dump(mode="json"))


def test_the_twin_and_the_comparison_live_only_in_the_copy(
    project, recorded_dbt, monkeypatch
):
    """Both are written beside the model so they inherit its folder's configs,
    and compiled together with it, which is the parse check and what leaves the
    tests' compiled SQL in the manifest for pricing."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    seen: dict[str, str] = {}
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    original_write = build_module.ShadowRun.write

    def spy(self, rel_path, text):
        seen[rel_path] = text
        original_write(self, rel_path, text)

    monkeypatch.setattr(build_module.ShadowRun, "write", spy)
    before = _tree_digest(project)

    transform_commands.test_mutations(_engine(project), "fct")

    assert "models/staging/dex_mutation_baseline.sql" in seen
    assert "models/staging/dex_mutation_equivalence.sql" in seen
    selects = [
        c["argv"][c["argv"].index("--select") + 1]
        for c in calls
        if c["argv"][1] == "compile"
    ]
    assert "fct dex_mutation_equivalence" in selects
    assert _tree_digest(project) == before
    assert not list(project.rglob("dex_mutation_*"))


def test_a_name_the_project_already_uses_turns_the_check_off(
    project, recorded_dbt, monkeypatch
):
    calls, plan = recorded_dbt
    manifest = _manifest()
    manifest["nodes"]["model.p.dex_mutation_baseline"] = {
        "name": "dex_mutation_baseline"
    }
    plan["manifest"] = manifest
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)

    result = transform_commands.test_mutations(_engine(project), "fct")

    assert (
        "already has a node named dex_mutation_baseline"
        in (result.equivalence["reason"])
    )
    assert "show" not in {c["argv"][1] for c in calls}


@pytest.mark.parametrize(
    "requested,paradigm,connector,reason",
    [
        (False, "free_local", "duckdb", "--no-check-equivalence"),
        (None, "bytes_scanned", "bigquery", "opt-in: re-run with --check-equivalence"),
        (None, "compute_time", "snowflake", "opt-in"),
        (True, "compute_time", "redshift", "no verified whole-row comparison"),
        (True, "db_load", "postgres", "no verified whole-row comparison"),
    ],
)
def test_the_check_is_off_with_a_reason_rather_than_absent(
    requested, paradigm, connector, reason
):
    """Off is a state that says why: a metered connector without the flag, a
    warehouse dex has no verified fingerprint for, or the caller's own `--no-`.
    None of these writes anything into the copy or runs anything."""

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.commands import _EquivalenceCheck

    class _NoShadow:
        def __getattr__(self, name):
            raise AssertionError(f"an off check touched the copy ({name})")

    check = _EquivalenceCheck.prepare(
        _NoShadow(),
        {"name": "fct", "unique_id": "model.p.fct"},
        "models/fct.sql",
        mutation.enumerate_mutants(
            mutation.prepare("select a from t", dialect="duckdb")
        ),
        select="fct",
        requested=requested,
        paradigm=Paradigm(paradigm),
        connector=connector,
        mutation_mod=mutation,
    )
    assert not check.enabled
    assert reason in check.reason
    label = check.check(None, "m01", mutation)
    assert label.status == "not_checked" and reason in label.reason
    assert check.summary()["checked"] is False


class _CopyStandIn:
    """A shadow copy whose tests all pass and whose shows answer from a queue."""

    def __init__(self, shows=()):
        self.shows = list(shows)
        self.invoked: list[str] = []

    def write(self, *args, **kwargs):
        pass

    def test(self, model, **kwargs):
        self.invoked.append("test")
        return {
            "nodes": [
                {
                    "unique_id": "test.p.not_null.abc",
                    "name": "not_null",
                    "status": "pass",
                    "execution_time": 10.0,
                }
            ]
        }

    def show(self, select):
        self.invoked.append("show")
        summary = {
            "nodes": [{"unique_id": "model.p.x", "name": "x", "execution_time": 10.0}]
        }
        return summary, (self.shows.pop(0) if self.shows else None)


def test_the_budget_can_stop_the_batch_before_a_survivors_comparison(project):
    """Each comparison is checked against the confirmed budget like a test run
    is. One that does not fit leaves its survivor unlabelled, and the rest of
    the batch is reported not_run rather than run past the budget."""

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.commands import _EquivalenceCheck, _run_mutants

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1 and b > 2", dialect="duckdb")
    )
    shadow = _CopyStandIn(shows=[_counts(1, 1, 0, 0)])
    prices = {"(baseline)": 10.0, "(equivalence self-check)": 10.0}
    prices.update({m.id: 10.0 for m in batch.mutants})
    prices.update({f"{m.id} equivalence": 500.0 for m in batch.mutants})
    result = _run_mutants(
        shadow,
        "models/fct.sql",
        batch,
        model="fct",
        paradigm=Paradigm.COMPUTE_TIME,
        connector="snowflake",
        store=FilesystemStore(project.parent),
        ceiling=100.0,
        estimate=None,
        mutation_mod=mutation,
        prices=prices,
        equivalence=_EquivalenceCheck(shadow),
    )
    _runs, _spend, warnings = result.pop("_meta")

    assert shadow.invoked == ["test", "show", "test"]
    first = next(m for m in result["mutants"] if m["id"] == "m01")
    assert first["equivalence"]["status"] == "not_checked"
    assert "budget ran out" in first["equivalence"]["reason"]
    assert result["counts"]["not_run"] == len(batch.mutants) - 1
    assert any("budget covered 1 of" in w for w in warnings)


class _TextPricedAdapter:
    """Prices a statement by its length, so different SQL prices differently."""

    dialect = "duckdb"

    def query_estimate(self, sql: str) -> float:
        return float(len(sql))


class _PricingCopy:
    def __init__(self, comparison: str | None):
        self.comparison = comparison

    def manifest(self):
        return {
            "nodes": {
                "test.p.not_null_fct_a.abc": {
                    "resource_type": "test",
                    "compiled_code": (
                        "with __dbt__cte__fct as (select a from t where a > 1 "
                        "and b > 2) select count(*) from __dbt__cte__fct"
                    ),
                }
            }
        }


def _priced(equivalence=None):
    from exmergo_dex_core.transform.commands import _price_mutations

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1 and b > 2", dialect="duckdb")
    )
    return batch, _price_mutations(
        _TextPricedAdapter(),
        _PricingCopy(None),
        "models/fct.sql",
        batch,
        {"name": "fct"},
        mutation,
        equivalence=equivalence,
    )


def test_each_mutant_is_priced_as_its_own_statement():
    """Every mutant used to be priced at the baseline's cost, because its model
    file was spliced in as text no parser reads. A dropped filter is a shorter
    statement here, standing in for a scan that reads more."""

    batch, (estimate, per_table, prices, _notes) = _priced()
    dropped = next(m for m in batch.mutants if m.operator == "predicate_drop")
    assert per_table[f"{dropped.id} predicate_drop"] != per_table["(baseline)"]
    assert prices[dropped.id] == per_table[f"{dropped.id} predicate_drop"]
    assert estimate == sum(per_table.values())


def test_the_comparisons_are_priced_into_the_same_batch_as_if_all_survive():
    from exmergo_dex_core.transform.commands import _EquivalenceCheck

    check = _EquivalenceCheck(object())
    check.compiled = (
        "with __dbt__cte__fct as (select a from t where a > 1 and b > 2), "
        "__dbt__cte__dex_mutation_baseline as (select a from t where a > 1 "
        "and b > 2) select count(*) from __dbt__cte__fct"
    )
    batch, (estimate, per_table, prices, notes) = _priced(check)

    assert per_table["(equivalence self-check)"] == len(check.compiled)
    line = "(equivalence checks, if every mutant survives)"
    assert per_table[line] == sum(prices[f"{m.id} equivalence"] for m in batch.mutants)
    assert estimate == sum(per_table.values())
    assert any("as if every mutant survives" in note for note in notes)
    assert check.enabled


def test_a_comparison_that_cannot_be_priced_turns_the_check_off():
    """Unpriced spend is never confirmed, so the check comes off rather than
    running outside the number the caller agreed to."""

    from exmergo_dex_core.transform.commands import _EquivalenceCheck

    check = _EquivalenceCheck(object())
    _batch, (_estimate, per_table, _prices, _notes) = _priced(check)
    assert not check.enabled
    assert "(equivalence self-check)" not in per_table


def test_the_equivalence_flags_need_a_mutation_run(project, capsys):
    from exmergo_dex_core.cli import main

    rc = main(
        [
            "--repo-root",
            str(project.parent),
            "transform",
            "test",
            "--scaffold",
            "fct",
            "--check-equivalence",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc != 0 and envelope["status"] == "error"
    assert "need --mutate" in envelope["errors"][0]


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
    result = engine.test_mutations("fct")

    assert result.equivalence["self_check"] == "reproducible", "nothing was compared"
    assert _tree_digest(measurable_project) == before_tree
    assert relations() == before_relations


def test_survivors_are_labelled_by_what_the_dev_data_can_tell_apart(
    measurable_project: Path,
):
    """The issue's acceptance, on real dbt and real DuckDB, under a `not_null`
    suite that lets every defect through.

    Every order's customer exists, so the inner join turned left produces the
    same rows, and no amount is above 100, so the dropped `large` branch changes
    nothing either: both are equivalent on this data and need a fixture. The
    dropped status filter brings the cancelled order back and the boundary flip
    re-bands the order sitting on 100, so the data already tells those apart.
    """

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")

    labels = {m["operator"]: m["equivalence"] for m in result.mutants}
    assert labels["join_type"]["status"] == "equivalent"
    assert labels["join_type"]["only_in_mutant"] == 0
    assert labels["join_type"]["baseline_rows"] == 2
    assert labels["case_branch"]["status"] == "equivalent"
    assert labels["predicate_drop"]["status"] == "distinguishable"
    assert labels["predicate_drop"]["only_in_mutant"] == 1
    assert labels["predicate_drop"]["only_in_baseline"] == 0
    assert labels["comparison"]["status"] == "distinguishable"
    assert labels["comparison"]["only_in_mutant"] == 1
    assert labels["comparison"]["only_in_baseline"] == 1

    ranked = [m["equivalence"]["status"] for m in result.mutants]
    assert ranked == sorted(ranked, key=["distinguishable", "equivalent"].index)
    assert result.equivalence["self_check"] == "reproducible"
    assert result.counts["survived"] == result.counts["generated"]
    assert result.score == 0.0, "an equivalent survivor still counts against the suite"


def test_a_model_that_does_not_reproduce_itself_is_caught_by_the_self_check(
    measurable_project: Path,
):
    """`random()` differs between two evaluations of the same model, so any
    mutant would look different from it. The self-check is what notices."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "fct_noisy.sql").write_text(
        "select id, random() as jitter from {{ ref('stg_orders') }} where amount > 5",
        encoding="utf-8",
    )
    (measurable_project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: fct_noisy\n    columns:\n"
        "      - name: id\n        data_tests: [not_null]\n",
        encoding="utf-8",
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct_noisy")

    assert result.equivalence["self_check"] == "not_reproducible"
    assert all(
        m["equivalence"]["status"] == "not_checked"
        for m in result.mutants
        if m["status"] == "survived"
    )


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
