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
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.errors import RequestError
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform import commands as transform_commands
from exmergo_dex_core.transform import mutation
from exmergo_dex_core.transform.test_scaffold import unit_test_scaffold_edits

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


def _write_project_yml(project: Path, **paths: list[str]) -> None:
    lines = ["name: dex_test", 'version: "1.0.0"', "profile: dex_test"]
    lines += [
        f"{key.replace('_', '-')}: {json.dumps(value)}" for key, value in paths.items()
    ]
    (project / "dbt_project.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def ghost(project: Path) -> Path:
    """An analysis named after a model that does not exist.

    `models_analysis` begins with the characters `models` and is not under it,
    so only a lookup that compares whole path segments keeps it out.
    """

    _write_project_yml(
        project, model_paths=["models"], analysis_paths=["models_analysis"]
    )
    (project / "models_analysis").mkdir()
    (project / "models_analysis" / "stg_ghost.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    return project


def test_an_analysis_beside_the_model_path_is_refused_before_dbt_runs(
    ghost, recorded_dbt, monkeypatch
):
    """Taken for the model, the analysis got the ephemeral header and a `dbt
    compile`, and the refusal came from dbt's answer instead of from the parse,
    where a name that is not a model costs nothing to ask about."""

    calls, _plan = recorded_dbt
    _skip_dev_target_check(monkeypatch)

    with pytest.raises(RequestError, match="no model named 'stg_ghost'"):
        transform_commands.test_mutations(_engine(ghost), "stg_ghost")
    assert calls == []


def test_scaffold_and_mutate_refuse_a_name_that_is_not_a_model_the_same_way(
    ghost, recorded_dbt, monkeypatch
):
    """The two halves of `transform test` read one lookup, so they cannot
    disagree about which file is a model: same refusal, same envelope reason."""

    _skip_dev_target_check(monkeypatch)

    with pytest.raises(RequestError) as scaffold:
        unit_test_scaffold_edits(load(ghost), None, "stg_ghost")
    with pytest.raises(RequestError) as mutate:
        transform_commands.test_mutations(_engine(ghost), "stg_ghost")

    assert str(mutate.value) == str(scaffold.value)
    assert reason_for(mutate.value) is Reason.REQUEST


@pytest.mark.parametrize("model_paths", [["src/models"], ["./src/models"]])
def test_a_model_under_a_nested_model_path_is_the_file_that_gets_mutated(
    project, recorded_dbt, monkeypatch, model_paths
):
    """`src/models` is one configured entry and two path segments. Compared as a
    string built with the platform's separator, it never matched the POSIX keys
    `load()` builds on Windows, so `--mutate` refused a model `--scaffold` found.
    Asserted on the file the ephemeral header lands in, because a run that
    patched any other file would still complete against the faked manifest."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)
    _write_project_yml(project, model_paths=model_paths)
    (project / "src").mkdir()
    shutil.move(project / "models", project / "src" / "models")

    shadow_run = importlib.import_module("exmergo_dex_core.transform.build").ShadowRun
    patched: list[str] = []
    original_write = shadow_run.write

    def record(self, rel_path, text):
        if mutation.EPHEMERAL_HEADER in text:
            patched.append(rel_path)
        return original_write(self, rel_path, text)

    monkeypatch.setattr(shadow_run, "write", record)

    result = transform_commands.test_mutations(_engine(project), "fct")

    # The first header is the one the lookup placed; every write after it is a
    # mutant or the baseline twin, filed beside the model it found.
    assert patched[0] == "src/models/staging/fct.sql"
    assert {path.rsplit("/", 1)[0] for path in patched} == {"src/models/staging"}
    assert result.model == "fct"
    assert any(c["argv"][1] == "test" for c in calls)


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


def test_shadow_run_strip_semantic_models(project):
    from exmergo_dex_core.transform.build import ShadowRun

    sem_yaml = project / "models" / "semantic.yml"
    sem_yaml.write_text(
        "version: 2\n"
        "models:\n"
        "  - name: fct\n"
        "semantic_models:\n"
        "  - name: fct_sem\n"
        "    model: ref('fct')\n"
        "metrics:\n"
        "  - name: m1\n",
        encoding="utf-8",
    )
    with ShadowRun(project, target="dev") as shadow:
        assert shadow.strip_semantic_models("other_model") is False
        assert shadow.strip_semantic_models("fct") is True
        assert shadow.strip_semantic_models("fct") is False
        shadow_sem = shadow._require_shadow() / "models" / "semantic.yml"
        assert shadow_sem.is_file()
        content = shadow_sem.read_text(encoding="utf-8")
        assert "models:" in content
        assert "semantic_models:" not in content
        assert "metrics:" not in content
    assert "semantic_models:" in sem_yaml.read_text(encoding="utf-8")


def test_mutation_coverage_warns_when_semantic_models_stripped(
    project, recorded_dbt, monkeypatch
):
    _calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    sem_path = project / "models" / "semantic.yml"
    sem_path.write_text(
        "version: 2\nsemantic_models:\n  - name: fct_sem\n    model: ref('fct')\n",
        encoding="utf-8",
    )
    _skip_dev_target_check(monkeypatch)
    result = transform_commands.test_mutations(_engine(project), "fct")
    assert any(
        "semantic models referencing 'fct' were omitted" in w for w in result.warnings
    )
    assert "semantic_models:" in sem_path.read_text()


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
        (None, "db_load", "postgres", "opt-in: re-run with --check-equivalence"),
        (True, "compute_time", "redshift", "no verified whole-row comparison"),
        (True, "compute_time", "databricks", "no verified whole-row comparison"),
        (True, "db_load", "trino", "no verified whole-row comparison"),
        # Unasked on a connector with no comparison: the reason is the missing
        # comparison, never an invitation to a flag that cannot help.
        (None, "db_load", "trino", "no verified whole-row comparison"),
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


class _UptimeStandIn:
    """Owes a fixed amount beyond the batch's statements until it settles, the
    way the Snowflake model owes a resume minimum and an idle tail."""

    def __init__(self, owed: float):
        self.owed = owed
        self.observed: list[tuple[float, float | None]] = []
        self.settled = 0

    def pending(self) -> float:
        return 0.0 if self.settled else self.owed

    def quote(self) -> float:
        return self.pending()

    def admit(self) -> None:
        pass

    def observe(self, seconds: float, *, span: float | None = None) -> None:
        self.observed.append((seconds, span))

    def settle(self) -> tuple[float, list[str]]:
        self.settled += 1
        if self.settled > 1:
            return 0.0, []
        return self.owed, ["the warehouse idled after the batch"]


def test_the_stop_rule_leaves_room_for_what_the_warehouse_bills_beyond_the_runs(
    project,
):
    """A warehouse that bills uptime owes its idle tail once the last run is
    done. A batch that spends the whole budget on runs leaves the tail over it,
    so the stop rule counts what is still owed before admitting the next run,
    and the batch settles that once, as one more row of its own."""

    import json

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.commands import _run_mutants

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1 and b > 2", dialect="duckdb")
    )
    assert len(batch.mutants) > 2
    shadow = _CopyStandIn()
    prices = {"(baseline)": 10.0, **{m.id: 10.0 for m in batch.mutants}}
    uptime = _UptimeStandIn(owed=75.0)
    store = FilesystemStore(project.parent)
    result = _run_mutants(
        shadow,
        "models/fct.sql",
        batch,
        model="fct",
        paradigm=Paradigm.COMPUTE_TIME,
        connector="snowflake",
        store=store,
        ceiling=100.0,
        estimate=None,
        mutation_mod=mutation,
        prices=prices,
        uptime=uptime,
    )
    _runs, spend, warnings = result.pop("_meta")

    # The baseline, then one mutant: 10 + 75 + 10 fits, 20 + 75 + 10 does not.
    assert shadow.invoked == ["test", "test"]
    assert result["counts"]["not_run"] == len(batch.mutants) - 1
    assert uptime.observed == [(10.0, None), (10.0, None)]
    assert uptime.settled == 1
    assert spend["seconds_billed"] == pytest.approx(95.0)
    assert "the warehouse idled after the batch" in warnings
    ledger = (project.parent / ".dex" / "spend.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in ledger]
    assert [r["billed_seconds"] for r in rows] == [10.0, 10.0, 75.0]
    assert {r["command"] for r in rows} == {"transform test"}
    assert all(r["estimate"] is None for r in rows)


def test_the_uptime_quote_is_a_line_of_its_own_and_never_a_run_price():
    from exmergo_dex_core.transform.build import UPTIME_LINE
    from exmergo_dex_core.transform.commands import _price_mutations

    class _Quoting(_TextPricedAdapter):
        def __init__(self):
            self.warehouse_uptime = _UptimeStandIn(owed=60.0)

    adapter = _Quoting()
    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1 and b > 2", dialect="duckdb")
    )
    estimate, per_table, prices, _notes = _price_mutations(
        adapter, _PricingCopy(None), "models/fct.sql", batch, {"name": "fct"}, mutation
    )
    assert per_table[UPTIME_LINE] == 60.0
    assert UPTIME_LINE not in prices
    assert estimate == pytest.approx(sum(per_table.values()))


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


def test_the_downstream_flags_need_a_mutation_run(project, capsys):
    from exmergo_dex_core.cli import main

    rc = main(
        [
            "--repo-root",
            str(project.parent),
            "transform",
            "test",
            "--scaffold",
            "fct",
            "--downstream",
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


def test_mutation_testing_on_model_referenced_by_semantic_model(
    measurable_project: Path,
):
    """Regression for #727: transform test --mutate fails on any model a
    semantic model refs because an ephemeral mutant has no relation name.
    The referencing semantic model must be dropped from the shadow copy so
    dbt can compile and run data tests on the mutant."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "metricflow_time_spine.sql").write_text(
        "select date '2020-01-01' + cast(i as integer) as date_day "
        "from range(365) t(i)",
        encoding="utf-8",
    )
    (measurable_project / "models" / "semantic.yml").write_text(
        "version: 2\n"
        "semantic_models:\n"
        "  - name: metricflow_time_spine\n"
        "    model: ref('metricflow_time_spine')\n"
        "    primary_entity: date_day\n"
        "    dimensions:\n"
        "      - name: date_day\n"
        "        type: time\n"
        "        type_params:\n"
        "          time_granularity: day\n"
        "  - name: fct_sem\n"
        "    model: ref('fct')\n"
        "    entities:\n"
        "      - name: id\n"
        "        type: primary\n"
        "    dimensions:\n"
        "      - name: region\n"
        "        type: categorical\n",
        encoding="utf-8",
    )
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    assert result.counts["generated"] >= 4
    assert any(
        "semantic models referencing 'fct' were omitted" in w for w in result.warnings
    )
    semantic_file = measurable_project / "models" / "semantic.yml"
    assert semantic_file.is_file()
    assert "fct_sem" in semantic_file.read_text(encoding="utf-8")


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


class _TranslatingAdapter(_TextPricedAdapter):
    """Stand-in compute-time adapter that translates settled seconds."""

    def __init__(self, translate_fn, *, dialect: str = "snowflake", uptime=None):
        from exmergo_dex_core.envelope import Paradigm
        from exmergo_dex_core.guards.cost_guard import CostGate

        self.dialect = dialect
        self.paradigm = Paradigm.COMPUTE_TIME
        self.cost_gate = CostGate(
            paradigm=Paradigm.COMPUTE_TIME,
            ceiling=10_000,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=True,
            connector=dialect,
        )
        self.warehouse_uptime = uptime
        self.translated_Args: list[float] = []
        self._translate_fn = translate_fn

    def compute_spend_translation(self, seconds: float) -> dict[str, float]:
        self.translated_Args.append(seconds)
        return self._translate_fn(seconds)


def test_snowflake_mutation_spend_reports_credits_billed_including_uptime(
    project, recorded_dbt, monkeypatch
):
    """On Snowflake the batch translates its total settled `seconds_billed`
    (runs plus settled warehouse uptime) once into `credits_billed`."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = [
        {
            "unique_id": "test.p.not_null_fct_a.abc123",
            "status": "pass",
            "execution_time": 15.0,
        }
    ]
    _skip_dev_target_check(monkeypatch)

    uptime = _UptimeStandIn(owed=60.0)
    adapter = _TranslatingAdapter(
        lambda seconds: {
            "credits_billed": round(seconds / 3600.0, 6),
            "usd_billed": round((seconds / 3600.0) * 3.0, 4),
        },
        dialect="snowflake",
        uptime=uptime,
    )
    monkeypatch.setattr(DexEngine, "_adapter", lambda self, command=None, **kw: adapter)

    engine = _engine(project, connector="snowflake", confirmed=True, budget=600.0)
    result = transform_commands.test_mutations(engine, "fct")

    test_runs = sum(1 for c in calls if c["argv"][1] == "test")
    expected_seconds = test_runs * 15.0 + 60.0
    assert result.spend["seconds_billed"] == pytest.approx(expected_seconds)
    assert result.spend["credits_billed"] == pytest.approx(
        round(expected_seconds / 3600.0, 6)
    )
    assert result.spend["usd_billed"] == pytest.approx(
        round((expected_seconds / 3600.0) * 3.0, 4)
    )
    # Called once at 0.0 during pricing, then once after runs + uptime settle.
    assert adapter.translated_Args == [0.0, pytest.approx(expected_seconds)]


def test_clickhouse_cloud_mutation_proves_capacity_before_runs_and_translates_spend(
    project, recorded_dbt, monkeypatch
):
    """ClickHouse Cloud proves live capacity at 0.0 seconds while pricing, before
    any `dbt test` runs, and reports `compute_unit_hours_billed` on settlement."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = [
        {
            "unique_id": "test.p.not_null_fct_a.abc123",
            "status": "pass",
            "execution_time": 18.0,
        }
    ]
    _skip_dev_target_check(monkeypatch)

    test_calls_at_translate: list[int] = []

    def translate(seconds: float) -> dict[str, float]:
        test_calls_at_translate.append(sum(1 for c in calls if c["argv"][1] == "test"))
        return {"compute_unit_hours_billed": round(seconds * 2.0 / 3600.0, 6)}

    adapter = _TranslatingAdapter(translate, dialect="clickhouse")
    monkeypatch.setattr(DexEngine, "_adapter", lambda self, command=None, **kw: adapter)

    engine = _engine(project, connector="clickhouse", confirmed=True, budget=600.0)
    result = transform_commands.test_mutations(engine, "fct")

    total_test_runs = sum(1 for c in calls if c["argv"][1] == "test")
    assert total_test_runs >= 2
    # First translation happened before any `dbt test` ran; second after all ran.
    assert test_calls_at_translate == [0, total_test_runs]
    expected_seconds = total_test_runs * 18.0
    assert result.spend["seconds_billed"] == pytest.approx(expected_seconds)
    assert result.spend["compute_unit_hours_billed"] == pytest.approx(
        round(expected_seconds * 2.0 / 3600.0, 6)
    )


def test_clickhouse_cloud_mutation_fails_before_first_run_when_capacity_unproved(
    project, recorded_dbt, monkeypatch
):
    """If proving live capacity fails during `_price_mutations`, no `dbt test`
    invocation may run."""

    calls, plan = recorded_dbt
    plan["manifest"] = _manifest()
    plan["results"] = _results()
    _skip_dev_target_check(monkeypatch)

    def missing_capacity(_seconds: float) -> dict[str, float]:
        raise RuntimeError("ClickHouse Cloud capacity could not be proved")

    adapter = _TranslatingAdapter(missing_capacity, dialect="clickhouse")
    monkeypatch.setattr(DexEngine, "_adapter", lambda self, command=None, **kw: adapter)

    engine = _engine(project, connector="clickhouse", confirmed=True, budget=600.0)
    with pytest.raises(RuntimeError, match="capacity could not be proved"):
        transform_commands.test_mutations(engine, "fct")

    assert not [c for c in calls if c["argv"][1] == "test"]


# --- downstream tests (#648) ---------------------------------------------------

#: A mart that only counts the model's rows per region. Its singular test pins
#: every count the mart publishes, which is the coverage the issue had to
#: duplicate one hop upstream because the mutation run never consulted it.
SUMMARY_SQL = "select region, count(*) as n from {{ ref('fct') }} group by 1"

SUMMARY_TEST = """with s as (select * from {{ ref('fct_summary') }})
select 'unexpected' as problem from s where not (region = 'eu' and n = 2)
union all
select 'missing' from (select count(*) as c from s) t where c <> 1
"""


def _with_summary(project: Path) -> Path:
    (project / "models" / "schema.yml").write_text(_schema_yml(False), encoding="utf-8")
    (project / "models" / "fct_summary.sql").write_text(SUMMARY_SQL, encoding="utf-8")
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests" / "assert_summary_counts.sql").write_text(
        SUMMARY_TEST, encoding="utf-8"
    )
    return project


def _with_versioned_summary(project: Path) -> Path:
    """Same mart and count pin as `_with_summary`, declared as dbt model version 1.

    The SQL lives at `fct_summary_v1.sql` and `schema.yml` names it
    `fct_summary` with `versions: [{v: 1}]`. The singular test still refs the
    unversioned name; routing must report the version label `fct_summary.v1`.
    """

    schema = """version: 2
models:
  - name: fct
    columns:
      - name: id
        data_tests: [not_null]
  - name: fct_summary
    latest_version: 1
    versions:
      - v: 1
"""
    (project / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    (project / "models" / "fct_summary_v1.sql").write_text(
        SUMMARY_SQL, encoding="utf-8"
    )
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests" / "assert_summary_counts.sql").write_text(
        SUMMARY_TEST, encoding="utf-8"
    )
    return project


def test_a_defect_a_downstream_test_catches_is_reported_apart(
    measurable_project: Path,
):
    """The issue's acceptance on real dbt and DuckDB: `fct` carries only a
    `not_null`, so every defect survives its own suite, but the dropped status
    filter brings a cancelled order back and the mart's count test sees it.
    That defect is `caught_downstream`, and `score` reads exactly what it reads
    without the downstream tests."""

    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_summary(measurable_project))
    engine.build(target="dev")

    routed = engine.test_mutations("fct")
    alone = engine.test_mutations("fct", downstream=False)

    assert routed.downstream["enabled"] is True, routed.downstream
    assert routed.downstream["models"] == ["fct_summary"]
    assert routed.downstream["tests"] == ["assert_summary_counts"]
    statuses = {m["operator"]: m for m in routed.mutants}
    drop = statuses["predicate_drop"]
    assert drop["status"] == "caught_downstream"
    assert drop["caught_by"] == []
    assert drop["caught_downstream_by"] == ["assert_summary_counts"]
    # The inner join where every key resolves changes no count in the mart.
    assert statuses["join_type"]["status"] == "survived"

    assert routed.counts["caught_downstream"] >= 1
    assert routed.score == alone.score == 0.0
    assert routed.score_with_downstream > 0.0
    assert alone.score_with_downstream is None
    assert alone.downstream["enabled"] is False
    assert "--no-downstream" in alone.downstream["reason"]
    assert alone.counts["caught_downstream"] == 0
    assert any("caught by tests on the models downstream" in w for w in routed.warnings)


def test_routing_through_a_downstream_model_writes_and_materializes_nothing(
    measurable_project: Path,
):
    """The mart is made ephemeral in the copy, not rebuilt: the project is a
    byte for byte match afterwards and the dev warehouse gained no relation and
    lost none, the mart's own view included."""

    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_summary(measurable_project))
    engine.build(target="dev")
    warehouse = measurable_project.parent / "dev.duckdb"

    def relations():
        con = duckdb.connect(str(warehouse), read_only=True)
        try:
            return set(
                con.execute(
                    "select table_schema, table_name, table_type "
                    "from information_schema.tables"
                ).fetchall()
            )
        finally:
            con.close()

    before_tree, before_relations = _tree_digest(measurable_project), relations()
    result = engine.test_mutations("fct")

    assert result.downstream["enabled"] is True
    assert _tree_digest(measurable_project) == before_tree
    assert relations() == before_relations


def test_a_model_with_only_downstream_tests_is_measured(measurable_project: Path):
    """With no test of its own, every defect used to be refused as unmeasurable.
    A mart test that reads it is a test that can catch it."""

    pytest.importorskip("dbt.adapters.duckdb")
    project = _with_summary(measurable_project)
    (project / "models" / "schema.yml").unlink()
    engine = _engine(project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    assert result.counts["killed"] == 0
    assert result.counts["caught_downstream"] >= 1
    assert result.score == 0.0

    with pytest.raises(mutation.MutationError, match="has no tests"):
        engine.test_mutations("fct", downstream=False)


def test_a_versioned_downstream_model_is_routed_by_its_version_label(
    measurable_project: Path,
):
    """A versioned mart is selected by its dbt label, not the bare name.

    `--downstream` walks the child map and feeds `--select` the labels
    `manifest_node_label` produces. For a model with `versions:`, that label is
    `name.vN`. Pinning it here keeps a refactor of the selector from dropping
    versioned projects silently.
    """

    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_versioned_summary(measurable_project))
    engine.build(target="dev")

    result = engine.test_mutations("fct")

    assert result.downstream["enabled"] is True, result.downstream
    assert result.downstream["models"] == ["fct_summary.v1"]
    statuses = {m["operator"]: m for m in result.mutants}
    drop = statuses["predicate_drop"]
    assert drop["status"] == "caught_downstream"
    assert drop["caught_downstream_by"] == ["assert_summary_counts"]


class _DownstreamCopy:
    """The three things `_DownstreamTests.prepare` asks of a shadow project."""

    def __init__(self, root: Path, manifest: dict, *, compiled=None, fails=False):
        self.shadow = root
        self._manifest = manifest
        self._compiled = compiled if compiled is not None else manifest
        self._fails = fails
        self.compiled_selections: list[str] = []

    def manifest(self) -> dict:
        return self._manifest

    def write(self, rel_path: str, text: str) -> None:
        (self.shadow / rel_path).write_text(text, encoding="utf-8")

    def compile(self, select: str) -> dict:
        from exmergo_dex_core.transform.build import DbtRunError

        self.compiled_selections.append(select)
        if self._fails and " " in select:
            raise DbtRunError("dbt would not compile it")
        return self._compiled


def _downstream_manifest(mart_materialized: str = "ephemeral") -> dict:
    manifest = _manifest()
    nodes = manifest["nodes"]
    nodes["model.p.mart"] = {
        "name": "mart",
        "unique_id": "model.p.mart",
        "resource_type": "model",
        "package_name": "p",
        "language": "sql",
        "original_file_path": "models/mart.sql",
        "config": {"materialized": mart_materialized},
        "depends_on": {"nodes": ["model.p.fct"]},
    }
    nodes["model.p.fct"]["resource_type"] = "model"
    nodes["snapshot.p.snap"] = {
        "name": "snap",
        "unique_id": "snapshot.p.snap",
        "resource_type": "snapshot",
        "package_name": "p",
        "depends_on": {"nodes": ["model.p.fct"]},
    }
    nodes["test.p.mart_counts"] = {
        "name": "mart_counts",
        "unique_id": "test.p.mart_counts",
        "resource_type": "test",
        "depends_on": {"nodes": ["model.p.mart"]},
        "compiled_code": "select 1",
    }
    nodes["test.p.mart_vs_snap"] = {
        "name": "mart_vs_snap",
        "unique_id": "test.p.mart_vs_snap",
        "resource_type": "test",
        "depends_on": {"nodes": ["model.p.mart", "snapshot.p.snap"]},
        "compiled_code": "select 1",
    }
    manifest["unit_tests"] = {
        "unit_test.p.mart.mart_rules": {
            "name": "mart_rules",
            "model": "mart",
            "depends_on": {"nodes": ["model.p.mart"]},
        }
    }
    manifest["child_map"] = {
        "model.p.fct": [
            "model.p.mart",
            "snapshot.p.snap",
            "test.p.not_null_fct_a.abc123",
        ],
        "model.p.mart": ["test.p.mart_counts", "test.p.mart_vs_snap"],
        "snapshot.p.snap": ["test.p.mart_vs_snap"],
    }
    return manifest


def _prepare_downstream(tmp_path: Path, copy=None, **kwargs):
    from exmergo_dex_core.envelope import Paradigm

    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "mart.sql").write_text("select 1", encoding="utf-8")
    copy = copy or _DownstreamCopy(tmp_path, _downstream_manifest())
    options = {
        "requested": None,
        "paradigm": Paradigm.FREE_LOCAL,
        "connector": "duckdb",
    }
    options.update(kwargs)
    check = transform_commands._DownstreamTests.prepare(
        copy,
        _downstream_manifest()["nodes"]["model.p.fct"],
        model="fct",
        mutation_mod=mutation,
        **options,
    )
    return check, copy


def test_the_selection_routes_the_chain_and_excludes_what_cannot_see_the_mutant(
    tmp_path: Path,
):
    """A unit test on the mart reads fixtures, and a test that also reads the
    snapshot compares the mutant with a table holding the unmutated rows. The
    model's own tests are never excluded."""

    check, copy = _prepare_downstream(tmp_path)
    assert check.enabled, check.reason
    assert check.select == "fct mart"
    assert check.tests == ["mart_counts"]
    assert sorted(check.exclude) == ["mart_vs_snap", "unit_test:mart_rules"]
    assert "not_null_fct_a" in check.direct
    assert "not_null_fct_a" not in check.exclude
    assert (
        "snap" in check.excluded[0]["reason"] or "snap" in check.excluded[1]["reason"]
    )
    assert check.summary()["cut"][0]["name"] == "snap"
    assert mutation.EPHEMERAL_HEADER in (tmp_path / "models" / "mart.sql").read_text()
    assert copy.compiled_selections == ["fct mart"]


def test_downstream_is_opt_in_on_a_billed_connector(tmp_path: Path):
    from exmergo_dex_core.envelope import Paradigm

    check, copy = _prepare_downstream(
        tmp_path, paradigm=Paradigm.BYTES_SCANNED, connector="bigquery"
    )
    assert not check.enabled
    assert "--downstream" in check.reason and "opt-in" in check.reason
    assert check.select == "fct"
    assert copy.compiled_selections == []
    assert "with --downstream" in check.no_tests_hint()

    asked, _ = _prepare_downstream(
        tmp_path, requested=True, paradigm=Paradigm.BYTES_SCANNED, connector="bigquery"
    )
    assert asked.enabled


def test_no_downstream_is_honoured_on_duckdb(tmp_path: Path):
    check, copy = _prepare_downstream(tmp_path, requested=False)
    assert not check.enabled and "--no-downstream" in check.reason
    assert copy.compiled_selections == []
    assert check.summary()["tests"] == []


def test_a_chain_that_will_not_compile_falls_back_and_puts_every_file_back(
    tmp_path: Path,
):
    (tmp_path / "models").mkdir()
    copy = _DownstreamCopy(tmp_path, _downstream_manifest(), fails=True)
    check, _ = _prepare_downstream(tmp_path, copy=copy)
    assert not check.enabled
    assert "would not compile as ephemeral" in check.reason
    assert (tmp_path / "models" / "mart.sql").read_text() == "select 1"
    assert copy.compiled_selections == ["fct mart", "fct"]
    assert check.exclude == [] and check.excluded_names == set()
    # A model with no tests of its own must still be refused, not run against
    # downstream tests that no longer run.
    assert check.tests == []


def test_a_downstream_model_that_would_still_materialize_turns_routing_off(
    tmp_path: Path,
):
    """The override is what keeps the chain from writing a relation, so a run
    must not route through a model where it silently did not apply."""

    (tmp_path / "models").mkdir()
    copy = _DownstreamCopy(
        tmp_path, _downstream_manifest(), compiled=_downstream_manifest("table")
    )
    check, _ = _prepare_downstream(tmp_path, copy=copy)
    assert not check.enabled
    assert "could not make 'mart' ephemeral" in check.reason
    assert (tmp_path / "models" / "mart.sql").read_text() == "select 1"


def test_a_leaf_model_says_nothing_reads_it(tmp_path: Path):
    manifest = _manifest()
    manifest["child_map"] = {"model.p.fct": ["test.p.not_null_fct_a.abc123"]}
    check, copy = _prepare_downstream(
        tmp_path, copy=_DownstreamCopy(tmp_path, manifest)
    )
    assert not check.enabled
    assert check.reason == "no model in this project reads 'fct'"
    assert copy.compiled_selections == []


def test_a_downstream_catch_leaves_the_models_own_score_where_it_was(
    project, recorded_dbt, monkeypatch
):
    """Same suite, same mutants: the only thing downstream tests add is a
    second score. The model's own score is what it would be without them."""

    calls, plan = recorded_dbt
    manifest = _downstream_manifest()
    plan["manifest"] = manifest
    (project / "models" / "mart.sql").write_text("select 1", encoding="utf-8")
    manifest["nodes"]["model.p.mart"]["original_file_path"] = "models/mart.sql"
    own = {"unique_id": "test.p.not_null_fct_a.abc123", "status": "pass"}
    plan["test_results"] = [
        [own, {"unique_id": "test.p.mart_counts", "status": "pass"}]
    ]
    plan["results"] = [own, {"unique_id": "test.p.mart_counts", "status": "fail"}]
    _skip_dev_target_check(monkeypatch)

    result = transform_commands.test_mutations(_engine(project), "fct")

    assert result.counts["killed"] == 0
    assert result.counts["caught_downstream"] == result.counts["generated"]
    assert result.score == 0.0
    assert result.score_with_downstream == 1.0
    assert all(m["caught_downstream_by"] == ["mart_counts"] for m in result.mutants)
    test_calls = [c["argv"] for c in calls if c["argv"][1] == "test"]
    for argv in test_calls:
        assert argv[argv.index("--select") + 1] == "fct mart"
        assert "unit_test:mart_rules" in argv


class _DownstreamPricingCopy:
    """The model's own test, a mart test that reads the mutant through the
    mart, and a mart test the runs exclude."""

    _CHAIN = (
        "with __dbt__cte__fct as (select a from t where a > 1 and b > 2), "
        "__dbt__cte__mart as (select count(*) as n from __dbt__cte__fct) "
        "select n from __dbt__cte__mart"
    )

    def manifest(self):
        return {
            "nodes": {
                "test.p.own": {
                    "name": "own",
                    "resource_type": "test",
                    "compiled_code": (
                        "with __dbt__cte__fct as (select a from t where a > 1 "
                        "and b > 2) select count(*) from __dbt__cte__fct"
                    ),
                },
                "test.p.mart_counts": {
                    "name": "mart_counts",
                    "resource_type": "test",
                    "compiled_code": self._CHAIN,
                },
                "test.p.mart_vs_snap": {
                    "name": "mart_vs_snap",
                    "resource_type": "test",
                    "compiled_code": f"{self._CHAIN}, s",
                },
            }
        }


def test_downstream_tests_are_priced_with_the_mutant_spliced_beneath_the_mart():
    """A downstream test recomputes the chain, mutant at the bottom, so each
    mutant is priced through it; a test the runs exclude never runs, so it is
    priced at nothing rather than inflating the confirmed batch."""

    from exmergo_dex_core.transform.commands import _price_mutations

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1 and b > 2", dialect="duckdb")
    )
    copy = _DownstreamPricingCopy()

    def priced(skip):
        return _price_mutations(
            _TextPricedAdapter(),
            copy,
            "models/fct.sql",
            batch,
            {"name": "fct"},
            mutation,
            skip=skip,
        )

    _, every, _, _ = priced(frozenset())
    _, kept, _, _ = priced({"mart_vs_snap"})
    nodes = copy.manifest()["nodes"]
    own = len(nodes["test.p.own"]["compiled_code"])
    mart = len(nodes["test.p.mart_counts"]["compiled_code"])
    assert kept["(baseline)"] == own + mart
    assert every["(baseline)"] > kept["(baseline)"]
    dropped = next(m for m in batch.mutants if m.operator == "predicate_drop")
    assert kept[f"{dropped.id} predicate_drop"] < kept["(baseline)"]
