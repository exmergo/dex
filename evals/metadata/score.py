"""Score what an agent left in a workspace: build gate, completeness, correctness.

Deliberately independent of the engine's own reader: the scorer reads the YAML
with PyYAML and merges ``meta`` with ``config.meta`` the way dbt does, so a bug
in how Dex reads or writes a declaration shows up here as a lower score rather
than being shared by the thing under test and the thing measuring it.

Truth (``truth.json``), per task::

    {"models": {
       "<model>": {
         "grain": ["season", "team_id"],                 # compared as a set
         "population": {"column": "status",              # the effective rule:
                        "domain": [...],                 # every value the column holds
                        "excluded": ["cancelled"]}       # the ones the model drops
                     | {"filters": "none"},              # or: nothing is filtered
         "columns": {"<col>": {"role": "measure", "aggregation": "max",
                               "additivity": "semi_additive", "unit": "count"}},
         "assumption_evidence": "precedent"},            # the tier the convention needs
     "checks": [{"name": "...", "sql": "select count(*) ...", "expect": 0}],
     "workspace_checks": [{"name": "...", "sql": "...", "expect": 0,
                           "live_only": true}],
     "report": {"names": ["task_data", "due_at"], "not_done": true},
     "incomplete_allowed": {"blocked_by": ["tasks_task"]},
     "procedure": {"build_after_apply": true, "cold_deps_after_apply": true,
                   "compare_before_apply": true, "rebuilt_before_compare": true,
                   "cold_deps_after_repair": true,
                   "incremental_build_after_apply": true,
                   "mutate_after_tests": true},
     "replays": [{"name": "next_batch", "select": "fct_x",
                  "setup": ["delete from raw.x"],
                  "steps": [["insert into raw.x ..."], []],
                  "checks": [{"name": "...", "sql": "...", "expect": 0}]}],
     "incremental_test": {"model": "fct_x"},
     "planted_defects": [{"name": "window one day wider",
                          "path": "models/marts/fct_x.sql",
                          "replace": ["'6 day'", "'7 day'"], "select": "fct_x"}]}

``checks`` run against the gate's own build of a fresh copy, on fresh data (the
fixture's ``grading_tables`` where it has them). ``workspace_checks`` run
read-only against the warehouse the agent left in its workspace, which is the
only place a change the agent made to data, or failed to make, can be seen; a
``live_only`` one is skipped on a reference run, which never builds there.
``report`` grades the agent's final message (see :func:`report_rubric`).
``replays`` grade an incremental model's next run, which a single cold build
never executes (#1111): each starts from its own fresh copy of the workspace and
grading warehouse, runs its ``setup`` statements, builds the project cold, then
for each step runs the step's statements against that scratch warehouse (new
source rows, never the agent's) and ``dbt run --select <select>`` without
``--full-refresh``, and finally its ``checks``. Their results join ``checks``,
named ``<replay>: <check>``. ``incremental_test`` grades, from the workspace's
YAML alone, whether the model has a unit test that overrides ``is_incremental``
to true: the verification step, graded apart from the data the replays judge.
``incomplete_allowed`` accepts a deliverable left blocked by the named
prerequisite, where the build failed on nothing else and the final message
reports it (status ``blocked_reported``): a request the agent cannot carry out
from Dex may be reported rather than worked around (#1083).
``planted_defects`` grade the tests the agent left, not the model (#1112): each
defect is planted in its own copy of the gate's build by an exact, single text
replacement in the model's SQL, and ``dbt build --select`` must then fail at
least one test, unit or data. A defect whose text is not found exactly once is
``planted: false`` and not caught. The grade is the share caught, apart from
every other score, and says only that these defects could not ship, never that
the model is right.

A ``unit`` ending in ``*`` matches by prefix (``currency:*`` accepts any ISO code),
and a list accepts any of its values, for a field where more than one answer is
defensible (a grain column that is a date is a ``key`` and a ``time``).
An ``includes`` filter is read through the column's domain, so declaring the kept
values and declaring the dropped ones score the same.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .fixtures import (
    WAREHOUSE_FILE,
    Fixture,
    Task,
    build_warehouse,
    starting_project,
)
from .privacy import privacy_checks

EVIDENCE_TIERS = ("request", "contract", "precedent", "data", "default")
MEASURE_FIELDS = ("aggregation", "additivity", "unit")
GATE_TIMEOUT_S = 600


# --- reading declarations ---------------------------------------------------


def _yaml_files(project: Path) -> list[Path]:
    models = project / "models"
    if not models.is_dir():
        return []
    return sorted(p for p in models.rglob("*") if p.suffix in (".yml", ".yaml"))


def model_entries(project: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every ``models:`` entry in the project's YAML, by model name, with where
    each one lives. Two entries for one model is what dbt refuses."""

    import yaml

    entries: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path in _yaml_files(project):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue  # the build gate reports an unparseable file
        if not isinstance(document, dict):
            continue
        for entry in document.get("models") or []:
            if isinstance(entry, dict) and entry.get("name"):
                rel = str(path.relative_to(project))
                entries.setdefault(str(entry["name"]), []).append((rel, entry))
    return entries


def _dex_meta(node: dict[str, Any]) -> dict[str, Any]:
    """``meta.dex`` merged under ``config.meta.dex``, the way dbt merges meta."""

    merged: dict[str, Any] = {}
    for meta in (node.get("meta"), (node.get("config") or {}).get("meta")):
        if isinstance(meta, dict) and isinstance(meta.get("dex"), dict):
            merged.update(meta["dex"])
    return merged


@dataclass
class Declared:
    description: str | None = None
    grain: list[str] | None = None
    population: dict[str, Any] | None = None
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)


def declared(entry: dict[str, Any] | None) -> Declared:
    if not entry:
        return Declared()
    dex = _dex_meta(entry)
    columns: dict[str, dict[str, Any]] = {}
    for column in entry.get("columns") or []:
        if not isinstance(column, dict) or not column.get("name"):
            continue
        facts = dict(_dex_meta(column))
        if column.get("description"):
            facts["description"] = column["description"]
        columns[str(column["name"]).lower()] = facts
    grain = dex.get("grain")
    return Declared(
        description=(entry.get("description") or None),
        grain=[str(g) for g in grain] if isinstance(grain, list) else None,
        population=dex.get("population")
        if isinstance(dex.get("population"), dict)
        else None,
        assumptions=[a for a in dex.get("assumptions") or [] if isinstance(a, dict)],
        columns=columns,
    )


# --- which models the agent touched -----------------------------------------


def _model_sql(project: Path) -> dict[str, str]:
    models = project / "models"
    if not models.is_dir():
        return {}
    return {
        p.stem: p.read_text(encoding="utf-8") for p in sorted(models.rglob("*.sql"))
    }


def unit_test_entries(project: Path) -> dict[str, list[dict[str, Any]]]:
    """Every ``unit_tests:`` entry in the project's YAML, by the model it tests."""

    import yaml

    entries: dict[str, list[dict[str, Any]]] = {}
    for path in _yaml_files(project):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        for entry in document.get("unit_tests") or []:
            if isinstance(entry, dict) and entry.get("model"):
                entries.setdefault(str(entry["model"]), []).append(entry)
    return entries


def touched_models(baseline: Path, workspace: Path) -> list[str]:
    """Models whose SQL, YAML entry or unit tests differ from the starting state.

    A unit test is part of what a model ships with: a task that asks for tests
    (#1112) changes nothing else, and must not read as untouched.
    """

    before_sql, after_sql = _model_sql(baseline), _model_sql(workspace)
    before_yml, after_yml = model_entries(baseline), model_entries(workspace)
    before_ut, after_ut = unit_test_entries(baseline), unit_test_entries(workspace)
    names = set(after_sql) | set(after_yml) | set(after_ut) | set(before_ut)
    return sorted(
        name
        for name in names
        if after_sql.get(name) != before_sql.get(name)
        or [e for _, e in after_yml.get(name, [])]
        != [e for _, e in before_yml.get(name, [])]
        or after_ut.get(name, []) != before_ut.get(name, [])
    )


# --- the build gate ---------------------------------------------------------


def _dbt() -> str:
    beside = Path(sys.executable).parent / "dbt"
    if beside.exists():
        return str(beside)
    found = shutil.which("dbt")
    if not found:
        raise RuntimeError("dbt is not installed in this environment")
    return found


@dataclass
class Gate:
    passed: bool
    stage: str  # "duplicates", "deps", "parse", "build" or "ok"
    detail: list[str] = field(default_factory=list)
    #: The nodes ``dbt build`` reported as ``error`` or ``fail``, by name for a
    #: model and by unique id otherwise; empty unless the build stage failed.
    failed_nodes: list[str] = field(default_factory=list)


def _failed_nodes(project: Path) -> list[str]:
    """Extract failed dbt nodes with their resource type and package owner."""

    import json

    try:
        results = json.loads((project / "target/run_results.json").read_text())
    except (OSError, ValueError):
        return []
    failed = []
    for result in results.get("results") or []:
        if result.get("status") not in ("error", "fail"):
            continue
        unique_id = str(result.get("unique_id") or "")
        failed.append(
            unique_id.rsplit(".", 1)[-1]
            if unique_id.startswith("model.")
            else unique_id
        )
    return sorted(failed)


def _tail(output: str, lines: int = 25) -> list[str]:
    return [line for line in output.strip().splitlines()[-lines:] if line.strip()]


_GATE_IGNORE = shutil.ignore_patterns(
    "target", "logs", "dbt_packages", "*.duckdb*", ".dex", ".git"
)


def _dbt_stage(
    scratch: Path, args: list[str]
) -> subprocess.CompletedProcess[str] | None:
    """One dbt invocation in ``scratch``; ``None`` when it timed out."""

    try:
        return subprocess.run(
            [_dbt(), *args, "--profiles-dir", ".", "--no-use-colors"],
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=GATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None


def _declares_packages(project: Path) -> bool:
    return (project / "packages.yml").is_file() or (
        project / "dependencies.yml"
    ).is_file()


def build_gate(workspace: Path, fixture: Fixture, scratch: Path) -> Gate:
    """A cold ``dbt parse`` then ``dbt build`` in a fresh copy of the workspace,
    against a freshly generated warehouse, with no ``target/`` left over. A
    project that declares packages has them reinstalled first (``dbt deps``),
    since the copy leaves ``dbt_packages/`` behind.

    ``scratch`` keeps the built copy so the caller can read the relations it built.
    """

    duplicates = {
        name: [path for path, _ in found]
        for name, found in model_entries(workspace).items()
        if len(found) > 1
    }
    if duplicates:
        return Gate(
            False,
            "duplicates",
            [
                f"{name} has {len(paths)} YAML entries: {paths}"
                for name, paths in duplicates.items()
            ],
        )
    shutil.copytree(workspace, scratch, ignore=_GATE_IGNORE)
    build_warehouse(scratch / WAREHOUSE_FILE, fixture.grading_tables())
    stages = [("parse", ["parse"]), ("build", ["build"])]
    if _declares_packages(scratch):
        # `dbt_packages/` was left behind above, so this is a reinstall: an edit
        # made only under the installed packages is gone, as after `dbt deps`.
        stages.insert(0, ("deps", ["deps"]))
    for stage, args in stages:
        proc = _dbt_stage(scratch, args)
        if proc is None:
            return Gate(
                False, stage, [f"dbt {stage} timed out after {GATE_TIMEOUT_S}s"]
            )
        if proc.returncode != 0:
            return Gate(
                False,
                stage,
                _tail(proc.stdout + "\n" + proc.stderr),
                _failed_nodes(scratch) if stage == "build" else [],
            )
    return Gate(True, "ok")


def _execute(database: Path, statements: list[str]) -> None:
    import duckdb

    con = duckdb.connect(str(database))
    try:
        for statement in statements:
            con.execute(statement)
        con.execute("checkpoint")
    finally:
        con.close()


def run_replays(
    workspace: Path, fixture: Fixture, root: Path, replays: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run each replay (see the module docstring) and return its checks' results,
    named ``<replay>: <check>``. A replay whose dbt run fails fails every check
    it carries, with the stage that failed as the error."""

    results: list[dict[str, Any]] = []
    for replay in replays:
        name = replay["name"]
        checks = replay.get("checks") or []
        scratch = root / f"replay-{name}"
        shutil.copytree(workspace, scratch, ignore=_GATE_IGNORE)
        database = scratch / WAREHOUSE_FILE
        build_warehouse(database, fixture.grading_tables())
        _execute(database, replay.get("setup") or [])
        stages: list[tuple[str, list[str], list[str]]] = []
        if _declares_packages(scratch):
            stages.append(("deps", ["deps"], []))
        stages.append(("cold run", ["run"], []))
        for index, statements in enumerate(replay.get("steps") or [], start=1):
            stages.append(
                (
                    f"incremental run {index}",
                    ["run", "--select", replay["select"]],
                    list(statements),
                )
            )
        failed = None
        for stage, args, statements in stages:
            _execute(database, statements)
            proc = _dbt_stage(scratch, args)
            if proc is None or proc.returncode != 0:
                failed = f"dbt {stage} failed"
                break
        if failed is not None:
            results += [
                {"name": f"{name}: {c['name']}", "passed": False, "error": failed}
                for c in checks
            ]
            continue
        results += [
            {**result, "name": f"{name}: {result['name']}"}
            for result in run_checks(scratch, checks)
        ]
    return results


def incremental_unit_tests(project: Path, model: str) -> list[str]:
    """The unit tests on ``model`` that override ``is_incremental`` to true, read
    from the project's YAML: the test that runs the branch a full build never
    does (#1111)."""

    import yaml

    found = []
    for path in _yaml_files(project):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        for test in document.get("unit_tests") or []:
            if not isinstance(test, dict) or test.get("model") != model:
                continue
            macros = (test.get("overrides") or {}).get("macros") or {}
            if isinstance(macros, dict) and macros.get("is_incremental") is True:
                found.append(str(test.get("name")))
    return sorted(found)


def planted_defects(
    built: Path, defects: list[dict[str, Any]], scratch: Path
) -> list[dict[str, Any]]:
    """Whether the project's own tests catch each planted defect (#1112).

    ``built`` is a project the gate already built, warehouse included. Each
    defect gets its own copy of it, so defects never compound, and the copy is
    built with ``dbt build --select <select>``: unit tests read the defective
    SQL, the model is rebuilt from it, and its data tests read the result. A
    test reported ``fail`` is a catch. A model that errors is not a catch, since
    the defect was meant to build and a refusal says nothing about the tests.
    """

    import json

    results: list[dict[str, Any]] = []
    for index, defect in enumerate(defects):
        copy = scratch / f"defect_{index}"
        shutil.copytree(
            built, copy, ignore=shutil.ignore_patterns("target", "logs", ".dex")
        )
        path = copy / defect["path"]
        old, new = defect["replace"]
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        record: dict[str, Any] = {"name": defect["name"], "planted": False}
        if text.count(old) != 1:
            record.update(caught=False, error="defect text not found exactly once")
            results.append(record)
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        record["planted"] = True
        try:
            subprocess.run(
                [
                    _dbt(),
                    "build",
                    "--select",
                    defect["select"],
                    "--profiles-dir",
                    ".",
                    "--no-use-colors",
                ],
                cwd=copy,
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            record.update(caught=False, error="dbt build timed out")
            results.append(record)
            continue
        run_results = copy / "target" / "run_results.json"
        statuses: dict[str, str] = {}
        if run_results.is_file():
            for result in json.loads(run_results.read_text(encoding="utf-8")).get(
                "results", []
            ):
                statuses[str(result.get("unique_id"))] = str(result.get("status"))
        caught_by = sorted(
            uid.split(".")[-1] if uid.startswith("unit_test.") else uid
            for uid, status in statuses.items()
            if uid.startswith(("test.", "unit_test.")) and status == "fail"
        )
        record.update(caught=bool(caught_by), caught_by=caught_by)
        results.append(record)
    return results


def relation_columns(scratch: Path, model: str) -> list[str] | None:
    import duckdb

    con = duckdb.connect(str(scratch / WAREHOUSE_FILE), read_only=True)
    try:
        rows = con.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'main' and table_name = ? order by ordinal_position",
            [model],
        ).fetchall()
    finally:
        con.close()
    return [r[0].lower() for r in rows] or None


def run_checks(scratch: Path, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate row, schema and workspace assertions against the candidate."""

    import duckdb

    results = []
    database = scratch / WAREHOUSE_FILE
    if checks and not database.is_file():
        return [
            {"name": check["name"], "passed": False, "error": "no warehouse"}
            for check in checks
        ]
    con = duckdb.connect(str(database), read_only=True)
    try:
        for check in checks:
            try:
                got = con.execute(check["sql"]).fetchone()[0]
                passed = got == check["expect"]
                results.append({"name": check["name"], "passed": passed, "got": got})
            except Exception as exc:  # a missing column or relation is a failed check
                results.append(
                    {
                        "name": check["name"],
                        "passed": False,
                        "error": type(exc).__name__,
                    }
                )
    finally:
        con.close()
    return results


# --- completeness and correctness -------------------------------------------


def completeness(decl: Declared, columns: list[str] | None) -> dict[str, Any]:
    """Which declaration fields are present, item by item."""

    items: dict[str, bool] = {
        "description": bool(decl.description and decl.description.strip()),
        "grain": bool(decl.grain),
        "population": bool(
            decl.population
            and (decl.population.get("rule") or decl.population.get("filters"))
        ),
        "assumption": any(
            a.get("evidence") in EVIDENCE_TIERS for a in decl.assumptions
        ),
    }
    if columns is None:
        # Nothing to judge column roles against: the model was not built (a task
        # that asked for it and got only YAML). Counted, so it cannot score 1.0.
        items["built_relation"] = False
    for column in columns or []:
        facts = decl.columns.get(column, {})
        items[f"role:{column}"] = bool(facts.get("role"))
        if facts.get("role") == "measure":
            for name in MEASURE_FIELDS:
                items[f"{name}:{column}"] = bool(facts.get(name))
    present = sum(items.values())
    return {
        "score": present / len(items) if items else 0.0,
        "missing": sorted(k for k, ok in items.items() if not ok),
    }


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _effective_excluded(
    population: dict[str, Any] | None, column: str, domain: list[str]
) -> set[str] | None:
    """The values of ``column`` a population drops, or None where no filter on
    that column is declared."""

    if not population:
        return None
    domain_set = {_norm(v) for v in domain}
    excluded: set[str] | None = None
    for f in population.get("filters") or []:
        if not isinstance(f, dict) or _norm(f.get("column", "")) != _norm(column):
            continue
        dropped: set[str] = set()
        if f.get("excludes"):
            dropped |= {_norm(v) for v in f["excludes"]}
        if f.get("includes"):
            dropped |= domain_set - {_norm(v) for v in f["includes"]}
        excluded = (excluded or set()) | dropped
    return excluded


def _unit_matches(expected: str, got: Any) -> bool:
    if got is None:
        return False
    if expected.endswith("*"):
        return _norm(got).startswith(_norm(expected[:-1]))
    return _norm(got) == _norm(expected)


def correctness(decl: Declared, truth: dict[str, Any]) -> dict[str, bool]:
    fields: dict[str, bool] = {}
    if "grain" in truth:
        fields["grain"] = {_norm(g) for g in decl.grain or []} == {
            _norm(g) for g in truth["grain"]
        }
    population = truth.get("population")
    if population is not None:
        if population.get("filters") == "none":
            fields["population"] = not any(
                (f.get("excludes") or f.get("includes"))
                for f in (decl.population or {}).get("filters") or []
                if isinstance(f, dict)
            )
        else:
            got = _effective_excluded(
                decl.population, population["column"], population["domain"]
            )
            fields["population"] = got == {_norm(v) for v in population["excluded"]}
    for column, expected in (truth.get("columns") or {}).items():
        facts = decl.columns.get(column.lower(), {})
        for name, value in expected.items():
            if name == "unit":
                fields[f"unit:{column}"] = _unit_matches(value, facts.get("unit"))
            elif isinstance(value, list):
                # More than one answer is defensible: a grain column that is also
                # a date is a `key` and a `time`.
                fields[f"{name}:{column}"] = _norm(facts.get(name)) in {
                    _norm(v) for v in value
                }
            else:
                fields[f"{name}:{column}"] = _norm(facts.get(name)) == _norm(value)
    if "assumption_evidence" in truth:
        fields["assumption_evidence"] = any(
            a.get("evidence") == truth["assumption_evidence"] for a in decl.assumptions
        )
    return fields


# --- attribution (#1026) -----------------------------------------------------


def attribution(decl: Declared, truth: dict[str, Any] | None) -> dict[str, Any] | None:
    """Whether the decision the task turns on is labelled with the tier that
    actually settled it, graded apart from whether the SQL is preferred.

    The tier is the only thing read: the ``detail`` is reported and never earns
    credit, so quoting ambiguous wording does not turn a ``default`` into a
    ``request``. Which population was chosen is the checks' business. Every
    assumption matching the decision must carry an accepted tier, so one honest
    entry cannot outweigh another that claims too much.
    """

    if not truth:
        return None
    pattern = re.compile(truth["decision_pattern"], re.IGNORECASE)
    matched = [
        a
        for a in decl.assumptions
        if pattern.search(f"{a.get('decision') or ''} {a.get('chosen') or ''}")
    ]
    tiers = [str(a.get("evidence")) for a in matched]
    forbidden = set(truth.get("forbidden") or [])
    accepted = set(truth["evidence"])
    limit = truth.get("max_assumptions")
    noisy = limit is not None and len(decl.assumptions) > limit
    labelled = bool(tiers) and all(t in accepted and t not in forbidden for t in tiers)
    return {
        "passed": labelled and not noisy,
        "matched": len(matched),
        "tiers": tiers,
        "expected": sorted(accepted),
        "assumption_count": len(decl.assumptions),
        "over_recorded": noisy,
    }


# --- procedure steps ---------------------------------------------------------


#: Procedure steps graded from the order of the agent's Dex calls (#1049, #855).
#: Each reads ``Session.dex_calls``, in the order the agent made them.
PROCEDURE_STEPS = (
    "compare_before_apply",
    "build_after_apply",
    "cold_deps_after_apply",
    "rebuilt_before_compare",
    "cold_deps_after_repair",
    "incremental_build_after_apply",
    "mutate_after_tests",
)

#: Build outcomes that establish nothing was built (#1114). ``None`` (the
#: agent's own pipe hid the envelope) is given the benefit of the doubt, as a
#: hidden comparison verdict is.
#: ``unrelated`` ran nodes, none of them one the change required, so the repair
#: it was meant to build was not built.
_UNBUILT_OUTCOMES = frozenset(
    {"failed", "skipped", "not_run", "empty_selection", "unrelated"}
)


def _is_mutation(call: dict[str, Any]) -> bool:
    return call["label"] == "transform test" and "mutate" in (call.get("flags") or [])


def _compared_the_apply(
    calls: list[dict[str, Any]], compare: int, applies: list[int]
) -> bool:
    """Whether the comparison at ``compare`` is a usable one of the last apply."""

    last = applies[-1]
    if compare > last or calls[compare].get("verdict") in ("not_checked", "not_run"):
        return False
    compared, applied = calls[compare].get("plan"), calls[last].get("plan")
    if compared and applied:
        return compared == applied
    return not any(compare < i < last for i in applies)


def _rebuilt_before(
    calls: list[dict[str, Any]], compare: int, applies: list[int]
) -> bool:
    """Whether an earlier apply was built, successfully, before ``compare``."""

    earlier = [i for i in applies if i < compare]
    # After the latest apply before the comparison: an apply changes the tree,
    # so a build of an earlier one does not build what the comparison reads.
    return bool(earlier) and any(
        call["label"] == "transform build"
        and earlier[-1] < i < compare
        and call.get("outcome") not in _UNBUILT_OUTCOMES
        for i, call in enumerate(calls)
    )


def procedure_steps(
    truth: dict[str, Any] | None, calls: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Whether the agent took the procedure steps the task requires, in order.

    ``truth`` is the task's ``procedure`` block, ``{"<step>": true, ...}``; a step
    set to false or absent is not graded, so a task where a comparison is not
    needed (an intentional behavior change) never penalizes one. ``None`` when
    the task grades no step, or when there is no session to read (a reference
    run), so neither is averaged in as a pass.

    - ``compare_before_apply``: a comparison of the plan the last
      ``transform apply`` wrote precedes that apply and came back usable. The
      last, not the first: repairing a prerequisite is its own plan and apply,
      and the refactor's comparison rightly follows it. A comparison after every
      apply is a miss, since the baseline is gone. Where both calls name a plan
      the ids must match; where either does not (no id, ``$PLAN``), no other
      apply may sit between them, since an apply changes what the comparison
      read. ``not_checked`` and a comparison that never ran are not usable; a
      verdict the agent's own pipe hid is given the benefit of the doubt.
    - ``build_after_apply``: a ``transform build`` follows the last
      ``transform apply``, so the applied change is what was built.
    - ``cold_deps_after_apply``: a ``transform build --cold-deps`` follows the
      last ``transform apply``, so the repair that went in is the one shown to
      survive a reinstall of the packages (#860, #1057). Read from the call's
      ``flags``; a session recorded before flags were has none, so it is a miss.
    - ``rebuilt_before_compare``: the usable comparison
      ``compare_before_apply`` looks for comes after an earlier apply (the
      prerequisite repair, its own plan) and a build after the latest such
      apply that did not fail (#1114). A repair bundled into the refactor's own
      plan has no such apply, so its comparison could only ever read the
      blocked baseline: a miss. A build whose envelope the agent's pipe hid
      gets the benefit of the doubt.
    - ``cold_deps_after_repair``: a ``transform build --cold-deps`` that did not
      fail follows any apply, so a package repair is shown to survive a
      reinstall whether that build ran before the refactor's comparison or after
      its apply (#1114).
    - ``incremental_build_after_apply``: a ``transform build`` without
      ``--full-refresh`` follows the last ``transform apply`` and every
      ``--full-refresh`` build after it, so an incremental model's next run, not
      only its full build, ran on the applied change (#1111).
    - ``mutate_after_tests``: a ``transform test --mutate`` follows the last
      ``transform apply``, so the tests that were measured are the ones that
      ship (#1112). Measuring, strengthening and stopping leaves the strengthened
      tests unmeasured, so it is a miss.
    """

    required = [step for step in PROCEDURE_STEPS if (truth or {}).get(step)]
    if not required or calls is None:
        return None
    labels = [call["label"] for call in calls]
    applies = [i for i, label in enumerate(labels) if label == "transform apply"]
    compares = [i for i, label in enumerate(labels) if label == "transform compare"]
    builds = [i for i, label in enumerate(labels) if label == "transform build"]
    results: dict[str, bool] = {}
    if "compare_before_apply" in required:
        results["compare_before_apply"] = bool(applies) and any(
            _compared_the_apply(calls, i, applies) for i in compares
        )
    if "build_after_apply" in required:
        results["build_after_apply"] = bool(applies) and any(
            i > applies[-1] for i in builds
        )
    if "cold_deps_after_apply" in required:
        results["cold_deps_after_apply"] = bool(applies) and any(
            i > applies[-1] and "cold-deps" in (calls[i].get("flags") or [])
            for i in builds
        )
    if "rebuilt_before_compare" in required:
        results["rebuilt_before_compare"] = bool(applies) and any(
            _compared_the_apply(calls, i, applies)
            and _rebuilt_before(calls, i, applies)
            for i in compares
        )
    if "cold_deps_after_repair" in required:
        results["cold_deps_after_repair"] = bool(applies) and any(
            i > applies[0]
            and "cold-deps" in (calls[i].get("flags") or [])
            and calls[i].get("outcome") not in _UNBUILT_OUTCOMES
            for i in builds
        )
    if "incremental_build_after_apply" in required:
        after = [i for i in builds if applies and i > applies[-1]]
        refreshes = [
            i for i in after if "full-refresh" in (calls[i].get("flags") or [])
        ]
        results["incremental_build_after_apply"] = any(
            i not in refreshes and all(i > r for r in refreshes) for i in after
        )
    if "mutate_after_tests" in required:
        results["mutate_after_tests"] = bool(applies) and any(
            i > applies[-1] and _is_mutation(call) for i, call in enumerate(calls)
        )
    return {
        "passed": all(results.values()),
        "steps": results,
        "score": sum(results.values()) / len(results),
    }


def mutation_record(calls: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """What the agent did with mutation testing, recorded and never graded.

    ``survived`` is each run's survivor count as its envelope reported it
    (``None`` where the agent's pipe hid it). ``strengthened`` is whether a
    ``transform apply`` followed a run with survivors, and ``remeasured``
    whether another run followed that apply. ``None`` with no session to read.
    """

    if calls is None:
        return None
    runs = [i for i, call in enumerate(calls) if _is_mutation(call)]
    applies = [i for i, call in enumerate(calls) if call["label"] == "transform apply"]
    with_survivors = [i for i in runs if calls[i].get("survived")]
    strengthened = [a for a in applies if any(r < a for r in with_survivors)]
    return {
        "runs": len(runs),
        "survived": [calls[i].get("survived") for i in runs],
        "strengthened": bool(strengthened),
        "remeasured": any(r > strengthened[0] for r in runs) if strengthened else False,
    }


# --- the final report ---------------------------------------------------------

_NOT_DONE = re.compile(
    r"n['\u2019]t\b|\b(?:not|never|cannot|unable|incomplete|unchanged|outside|remains?|"
    r"left (?:for|to)|owned by|up to)\b"
)
_CHANGE = re.compile(
    r"\b(?:modif|chang|alter|migrat|writ|updat|perform|run|ran|execut|appl|"
    r"convert|retyp|fix)\w*"
)
_SOURCE = re.compile(
    r"\b(?:source|raw|underlying|warehouse|input|loader|loaded)\b|"
    r"\braw\.\w+|\bthe data\b|\bour data\b"
)
# A change, not a state: "is now a timestamp", "has been retyped", "was changed".
# A sentence that only describes how the source is ("due_on is a timestamp") is
# not a claim that anything was done to it.
_CLAIM = re.compile(
    r"\b(?:now|has been|have been|was|were)\b[^.]{0,40}?\b(?:changed|modified|"
    r"updated|migrated|altered|converted|retyped|fixed|match(?:es)?|a timestamp|"
    r"timestamps?)\b|\bnow\s+(?:matches|has|holds|is|are)\b"
)


def _sentences(text: str) -> list[str]:
    """Split final-report prose into units for claims and refusal checks."""

    return [
        part.strip().lower()
        for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if part.strip()
    ]


def report_rubric(
    truth: dict[str, Any] | None, final: str | None
) -> dict[str, Any] | None:
    """Whether the agent's final message reports the part it could not do (#1083).

    A deterministic, bounded heuristic over plain sentences, so a run is graded
    the same way twice, and not a judge of prose. ``truth`` is the task's
    ``report`` block: ``names`` it must mention (the relation and column whose
    data change was asked for) and ``not_done``, that a change to the source is
    stated as not performed. A sentence that says the source or the data is now
    changed without a negation fails it, however the rest reads. ``None`` when
    the task grades no report, or when there is no session to read (a reference
    run), so neither is averaged in as a pass.
    """

    if not truth or final is None:
        return None
    text = final.lower()
    checks: dict[str, bool] = {}
    names = [str(name).lower() for name in truth.get("names") or []]
    if names:
        checks["names"] = all(name in text for name in names)
    sentences = _sentences(final)
    if truth.get("not_done"):
        checks["states_not_done"] = (
            any(
                _NOT_DONE.search(sentence) and _CHANGE.search(sentence)
                for sentence in sentences
            )
            or "incomplete" in text
        )
        checks["no_overclaim"] = not any(
            _SOURCE.search(sentence)
            and _CLAIM.search(sentence)
            and not _NOT_DONE.search(sentence)
            for sentence in sentences
        )
    return {"passed": all(checks.values()), "checks": checks}


# --- the task score ----------------------------------------------------------


@dataclass
class TaskScore:
    task: str
    #: "scored", "gate_failed", "no_models", or "blocked_reported": the gate
    #: failed only on a prerequisite the task allows to stay blocked, and the
    #: final message reports it (``truth.incomplete_allowed``, #1083).
    status: str
    gate: dict[str, Any]
    touched: list[str]
    models: dict[str, Any]
    checks: list[dict[str, Any]]
    completeness: float
    correctness: float
    checks_passed: float
    #: Share of the task's models whose attribution passed; ``None`` when the
    #: task grades no attribution, so it is never averaged in as a pass.
    attribution: float | None = None
    #: Share of the required procedure steps the agent took in order; ``None``
    #: when the task grades none or there was no session (see procedure_steps).
    procedure: float | None = None
    procedure_steps: dict[str, bool] | None = None
    #: Independent synthetic-value leakage/config checks; not a schema score.
    privacy: dict[str, Any] | None = None
    #: Checks of the warehouse the agent left behind (see the module docstring);
    #: ``None`` when the task has none.
    workspace_checks: list[dict[str, Any]] | None = None
    #: Whether the final message reports what was not done (``report_rubric``).
    report: dict[str, Any] | None = None
    #: ``{"passed", "tests"}``: whether the task's incremental model has a unit
    #: test overriding ``is_incremental`` (``truth.incremental_test``, #1111);
    #: ``None`` when the task grades none.
    incremental_test: dict[str, Any] | None = None
    #: Each planted defect and whether the tests caught it (#1112); ``None``
    #: when the task plants none.
    planted_defects: list[dict[str, Any]] | None = None
    #: Share of the planted defects the tests caught; ``None`` when none planted.
    defects_caught: float | None = None
    #: What the agent did with ``transform test --mutate`` (``mutation_record``).
    mutation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blocked_as_allowed(task: Task, gate: Gate) -> bool:
    """Whether the build failed only on the prerequisite the task allows to stay
    blocked (``truth.incomplete_allowed.blocked_by``), so everything else built
    or was skipped behind it."""

    allowed = set((task.truth.get("incomplete_allowed") or {}).get("blocked_by") or [])
    return (
        bool(allowed)
        and gate.stage == "build"
        and bool(gate.failed_nodes)
        and set(gate.failed_nodes) <= allowed
    )


def _score_blocked(
    task: Task, workspace: Path, gate: Gate, touched: list[str]
) -> TaskScore:
    """A deliverable left blocked by the prerequisite the task allows (#1083): the
    declarations are scored against the columns they declare, since nothing was
    built to read, and no data check runs. :func:`score_workspace` keeps this
    only where the final message reports it; otherwise it is a failed gate."""

    entries = model_entries(workspace)
    models: dict[str, Any] = {}
    complete_scores: list[float] = []
    correct_fields: list[bool] = []
    for model in task.models:
        found = entries.get(model)
        decl = declared(found[0][1] if found else None)
        columns = sorted(decl.columns) or None
        comp = completeness(decl, columns)
        corr = correctness(decl, (task.truth.get("models") or {}).get(model, {}))
        complete_scores.append(comp["score"])
        correct_fields.extend(corr.values())
        models[model] = {
            "touched": model in touched,
            "columns": columns,
            "completeness": comp,
            "correctness": corr,
            "attribution": None,
            "description": decl.description,
        }
    return TaskScore(
        task=task.key,
        status="blocked_reported",
        gate=asdict(gate),
        touched=touched,
        models=models,
        checks=[],
        completeness=sum(complete_scores) / len(complete_scores),
        correctness=(sum(correct_fields) / len(correct_fields))
        if correct_fields
        else 1.0,
        checks_passed=1.0,
    )


def score_workspace(
    task: Task,
    fixture: Fixture,
    workspace: Path,
    calls: list[dict[str, Any]] | None = None,
    transcript: Path | None = None,
    *,
    final: str | None = None,
    reference: bool = False,
) -> TaskScore:
    """Combine build and row scores with workspace, privacy and report checks."""

    score = _score_workspace(task, fixture, workspace)
    wanted = [
        check
        for check in task.truth.get("workspace_checks") or []
        if not (reference and check.get("live_only"))
    ]
    if wanted:
        score.workspace_checks = run_checks(workspace, wanted)
    score.report = report_rubric(task.truth.get("report"), final)
    if score.status == "blocked_reported" and not (score.report or {}).get("passed"):
        # Left blocked and not reported as such, or no message to read: the
        # request was neither done nor accurately reported.
        score.status = "gate_failed"
        score.models, score.checks = {}, []
        score.completeness = score.correctness = score.checks_passed = 0.0
    score.privacy = privacy_checks(
        task.truth.get("privacy"), fixture, workspace, transcript
    )
    wanted_test = task.truth.get("incremental_test")
    if wanted_test:
        tests = incremental_unit_tests(workspace, wanted_test["model"])
        score.incremental_test = {"passed": bool(tests), "tests": tests}
    graded = procedure_steps(task.truth.get("procedure"), calls)
    if graded is not None:
        score.procedure = graded["score"]
        score.procedure_steps = graded["steps"]
    if task.truth.get("planted_defects"):
        score.mutation = mutation_record(calls)
    return score


def _score_workspace(task: Task, fixture: Fixture, workspace: Path) -> TaskScore:
    """Build and score changed deliverables against the grading warehouse."""

    with tempfile.TemporaryDirectory(prefix="dex-metadata-") as tmp:
        tmp_path = Path(tmp)
        baseline = starting_project(task, fixture, tmp_path / "baseline")
        touched = touched_models(baseline, workspace)
        if not set(task.models) & set(touched):
            # An authoring task that changed nothing earns nothing: no vacuous
            # perfect score for a declaration nobody wrote.
            return TaskScore(task.key, "no_models", {}, touched, {}, [], 0.0, 0.0, 0.0)
        gate = build_gate(workspace, fixture, tmp_path / "built")
        if not gate.passed:
            if _blocked_as_allowed(task, gate):
                return _score_blocked(task, workspace, gate, touched)
            return TaskScore(
                task.key, "gate_failed", asdict(gate), touched, {}, [], 0.0, 0.0, 0.0
            )
        entries = model_entries(workspace)
        models: dict[str, Any] = {}
        complete_scores: list[float] = []
        correct_fields: list[bool] = []
        attributed: list[bool] = []
        for model in task.models:
            found = entries.get(model)
            decl = declared(found[0][1] if found else None)
            columns = relation_columns(tmp_path / "built", model)
            comp = completeness(decl, columns)
            corr = correctness(decl, (task.truth.get("models") or {}).get(model, {}))
            complete_scores.append(comp["score"])
            correct_fields.extend(corr.values())
            graded = attribution(decl, task.truth.get("attribution"))
            if graded is not None:
                attributed.append(graded["passed"])
            models[model] = {
                "touched": model in touched,
                "columns": columns,
                "completeness": comp,
                "correctness": corr,
                "attribution": graded,
                "description": decl.description,
            }
        checks = run_checks(tmp_path / "built", task.truth.get("checks") or [])
        checks += run_replays(
            workspace, fixture, tmp_path, task.truth.get("replays") or []
        )
        defects = (
            planted_defects(tmp_path / "built", task.truth["planted_defects"], tmp_path)
            if task.truth.get("planted_defects")
            else None
        )
    return TaskScore(
        task=task.key,
        status="scored",
        gate=asdict(gate),
        touched=touched,
        models=models,
        checks=checks,
        completeness=sum(complete_scores) / len(complete_scores),
        correctness=(sum(correct_fields) / len(correct_fields))
        if correct_fields
        else 1.0,
        checks_passed=(
            sum(c["passed"] for c in checks) / len(checks) if checks else 1.0
        ),
        attribution=(sum(attributed) / len(attributed)) if attributed else None,
        planted_defects=defects,
        defects_caught=(
            sum(d["caught"] for d in defects) / len(defects) if defects else None
        ),
    )
