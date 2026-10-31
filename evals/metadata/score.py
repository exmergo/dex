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
     "checks": [{"name": "...", "sql": "select count(*) ...", "expect": 0}]}

A ``unit`` ending in ``*`` matches by prefix (``currency:*`` accepts any ISO code),
and a list accepts any of its values, for a field where more than one answer is
defensible (a grain column that is a date is a ``key`` and a ``time``).
An ``includes`` filter is read through the column's domain, so declaring the kept
values and declaring the dropped ones score the same.
"""

from __future__ import annotations

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


def touched_models(baseline: Path, workspace: Path) -> list[str]:
    """Models whose SQL or YAML entry differs from the task's starting state."""

    before_sql, after_sql = _model_sql(baseline), _model_sql(workspace)
    before_yml, after_yml = model_entries(baseline), model_entries(workspace)
    names = set(after_sql) | set(after_yml)
    return sorted(
        name
        for name in names
        if after_sql.get(name) != before_sql.get(name)
        or [e for _, e in after_yml.get(name, [])]
        != [e for _, e in before_yml.get(name, [])]
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
    stage: str  # "duplicates", "parse", "build" or "ok"
    detail: list[str] = field(default_factory=list)


def _tail(output: str, lines: int = 25) -> list[str]:
    return [line for line in output.strip().splitlines()[-lines:] if line.strip()]


def build_gate(workspace: Path, fixture: Fixture, scratch: Path) -> Gate:
    """A cold ``dbt parse`` then ``dbt build`` in a fresh copy of the workspace,
    against a freshly generated warehouse, with no ``target/`` left over.

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
    shutil.copytree(
        workspace,
        scratch,
        ignore=shutil.ignore_patterns(
            "target", "logs", "dbt_packages", "*.duckdb*", ".dex", ".git"
        ),
    )
    build_warehouse(scratch / WAREHOUSE_FILE, fixture.tables())
    for stage, args in (("parse", ["parse"]), ("build", ["build"])):
        try:
            proc = subprocess.run(
                [_dbt(), *args, "--profiles-dir", ".", "--no-use-colors"],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return Gate(
                False, stage, [f"dbt {stage} timed out after {GATE_TIMEOUT_S}s"]
            )
        if proc.returncode != 0:
            return Gate(False, stage, _tail(proc.stdout + "\n" + proc.stderr))
    return Gate(True, "ok")


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
    import duckdb

    results = []
    con = duckdb.connect(str(scratch / WAREHOUSE_FILE), read_only=True)
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


# --- the task score ----------------------------------------------------------


@dataclass
class TaskScore:
    task: str
    status: str  # "scored", "gate_failed" or "no_models"
    gate: dict[str, Any]
    touched: list[str]
    models: dict[str, Any]
    checks: list[dict[str, Any]]
    completeness: float
    correctness: float
    checks_passed: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_workspace(task: Task, fixture: Fixture, workspace: Path) -> TaskScore:
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
            return TaskScore(
                task.key, "gate_failed", asdict(gate), touched, {}, [], 0.0, 0.0, 0.0
            )
        entries = model_entries(workspace)
        models: dict[str, Any] = {}
        complete_scores: list[float] = []
        correct_fields: list[bool] = []
        for model in task.models:
            found = entries.get(model)
            decl = declared(found[0][1] if found else None)
            columns = relation_columns(tmp_path / "built", model)
            comp = completeness(decl, columns)
            corr = correctness(decl, (task.truth.get("models") or {}).get(model, {}))
            complete_scores.append(comp["score"])
            correct_fields.extend(corr.values())
            models[model] = {
                "touched": model in touched,
                "columns": columns,
                "completeness": comp,
                "correctness": corr,
                "description": decl.description,
            }
        checks = run_checks(tmp_path / "built", task.truth.get("checks") or [])
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
    )
