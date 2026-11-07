"""Which behavior survives when a feature flag goes (#858).

"Retire this feature" and "make it permanent" both remove a flag's spelling, so
a reference search cannot tell them apart: a retirement that kept the enabled
branch reports no remaining reads, builds, and still outputs the retired column.
Row comparison cannot catch it either where the output table is empty.

So the author states the outcome, in a contract on ``transform remove var``:
which flag value's behavior survives and what each output must then hold. Dex
never picks the branch, and never reads one off a default. It holds the proposal
to what was stated, three times over:

- at plan time, from compiled SQL alone (:func:`analyze`), against the baseline
  compiled at the retained value and at the other one;
- in ``transform compare``, whose baseline compiles at the retained value, so
  ``equivalent`` means the proposal equals the chosen branch;
- after a build, from the built relation's metadata
  (:func:`verify_outputs`), which judges an empty table as well as a full one.

A check that cannot run is ``unchecked`` with a ``reason_code``, never clean.
"""

# ruff: noqa: PERF401
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from ..dbt_project import DbtProjectView, jinja_calls
from ..errors import PrerequisiteError
from ..semantic_declarations import Assumption, _Strict
from .plans import EditKind, EditOp, PlanEdit

if TYPE_CHECKING:
    from ..engine import DexEngine

MAX_READERS = 25


class RetirementOutput(_Strict):
    """What one model outputs once the flag is gone."""

    expected: list[str] = Field(min_length=1)
    removed: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disjoint(self) -> RetirementOutput:
        both = {c.lower() for c in self.expected} & {c.lower() for c in self.removed}
        if both:
            raise ValueError(
                f"{', '.join(sorted(both))} cannot be both expected and removed"
            )
        return self


class RetirementMetrics(_Strict):
    retained: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class RetirementContract(_Strict):
    """The outcome the author states. Nothing here is inferred."""

    behavior: Literal["retire", "permanent"]
    #: The flag value whose behavior survives. Required, because a default says
    #: what the project did and not what the request wants.
    retained_value: bool | int | float | str | None
    #: The owner-approved scope. Dex never edits an installed package, so the
    #: one accepted value states what that means for this change.
    packages: Literal["out_of_scope"] = "out_of_scope"
    outputs: dict[str, RetirementOutput] = Field(min_length=1)
    metrics: RetirementMetrics = Field(default_factory=RetirementMetrics)
    assumptions: list[Assumption] = Field(default_factory=list)
    #: ``complete`` (the default) needs a reference scan Dex can finish.
    #: ``authored`` checks the outputs and metrics named here and reports global
    #: reference discovery as unchecked, for a project the scan cannot finish
    #: (#1086). Never inferred: the author opts in.
    scope: Literal["complete", "authored"] = "complete"

    @field_validator("retained_value", mode="before")
    @classmethod
    def _scalar(cls, value: Any) -> Any:
        if isinstance(value, (list, dict)):
            raise ValueError("retained_value is one scalar value of the flag")
        return value


def parse_contract(raw: Any) -> RetirementContract:
    """The payload's ``retirement``, or a refusal naming what to fix."""

    if isinstance(raw, dict) and "retained_value" not in raw:
        raise ValueError(
            "retirement needs retained_value: the flag value whose behavior "
            "survives. Dex does not infer it, since a default does not say which "
            "behavior the request keeps"
        )
    try:
        return RetirementContract.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'retirement'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(
            f"the retirement contract is not valid: {problems}. It takes behavior "
            "(retire or permanent), retained_value, packages (out_of_scope), "
            "outputs ({model: {expected: [...], removed: [...]}}), metrics "
            "({retained: [...], removed: [...]}), assumptions and scope (complete "
            "or authored)"
        ) from exc


def stored_form(
    variable: str,
    contract: RetirementContract,
    discovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What a stored plan and a plan document carry.

    ``scope`` is written only when it is ``authored``, so a complete retirement
    stores, exports and digests exactly as it did before #1086. A scoped one
    also carries the discovery summary it was planned under, so a build and a
    plan document can say what stayed unchecked without scanning again.
    """

    form = {"variable": variable, **contract.model_dump(mode="json")}
    if contract.scope == "complete":
        form.pop("scope")
    if discovery is not None:
        form["discovery"] = dict(discovery)
    return form


def contract_of(stored: Mapping[str, Any]) -> RetirementContract:
    """The contract a stored form carries, without its bookkeeping keys."""

    return RetirementContract.model_validate(
        {
            k: v
            for k, v in stored.items()
            if k not in ("variable", "discovery", "column_reads")
        }
    )


def decisions(stored: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The contract's assumptions, shaped as ``data.decisions`` entries."""

    if not stored:
        return []
    return [
        {
            "retirement": stored.get("variable"),
            "decision": entry.get("decision"),
            "chosen": entry.get("chosen"),
            "evidence": entry.get("evidence"),
            **({"column": entry["column"]} if entry.get("column") else {}),
        }
        for entry in stored.get("assumptions") or []
    ]


# --- what the repository says about the flag ---------------------------------


def _vars_block(view: DbtProjectView) -> dict[str, Any]:
    source = view.files.get("dbt_project.yml")
    if source is None:
        return {}
    try:
        project = yaml.safe_load(source.content)
    except yaml.YAMLError:
        return {}
    if not isinstance(project, dict):
        return {}
    block = project.get("vars")
    return block if isinstance(block, dict) else {}


def scoped_definitions(view: DbtProjectView, name: str) -> list[str]:
    """The ``vars: {<scope>: {<name>: ...}}`` keys that set this flag.

    ``yaml_blocks`` reads top-level vars only, so these are never in a removal's
    spans. Each is left alone: a scope naming a package configures that package,
    and one naming this project is the author's to delete in the payload.
    """

    return sorted(
        str(scope)
        for scope, value in _vars_block(view).items()
        if isinstance(value, dict) and name in value
    )


def _project_name(view: DbtProjectView) -> str | None:
    source = view.files.get("dbt_project.yml")
    if source is None:
        return None
    try:
        project = yaml.safe_load(source.content)
    except yaml.YAMLError:
        return None
    return project.get("name") if isinstance(project, dict) else None


def yaml_reads(files: Mapping[str, str], name: str) -> list[str]:
    """``{{ var('x') }}`` written in YAML, where the reference index reads no var.

    ``+enabled: "{{ var('x') }}"`` is a read like any other, and it survives a
    retirement silently because no index form records it.
    """

    found = []
    for path, content in sorted(files.items()):
        if not path.endswith((".yml", ".yaml")):
            continue
        for call in jinja_calls(content):
            if call.callee == "var" and call.args and call.args[0] == name:
                found.append(f"{path}:{call.line}")
    return found


def is_already_complete(
    view: DbtProjectView,
    name: str,
    edits: Iterable[PlanEdit],
    *,
    allow_dynamic: bool = False,
) -> bool:
    """Nothing local names the flag, and the payload would change no file.

    ``allow_dynamic`` is for a scoped contract (#1086), which reports a
    dynamic var read as unchecked rather than resolving it.
    """

    from ..references import ReferenceIndex

    for edit in edits:
        existing = view.files.get(edit.path)
        if edit.op is EditOp.DELETE:
            if existing is not None:
                return False
        elif existing is None or existing.content != edit.new_content:
            return False
    index = ReferenceIndex(view, scan_packages=False)
    if index.references_to(name, "var")[0] or index.definitions_of(name, "var"):
        return False
    if index.indeterminate_for("var") and not allow_dynamic:
        return False
    if _project_name(view) in scoped_definitions(view, name):
        return False
    return not yaml_reads(
        {path: source.content for path, source in view.files.items()}, name
    )


def package_scope(view: DbtProjectView, project: Path, name: str) -> dict[str, Any]:
    """What sits outside this change: never edited, always reported."""

    from ..references import ReferenceIndex

    index = ReferenceIndex(view)
    readers: list[dict[str, Any]] = []
    for hit in index.references_to(name, "var")[0]:
        if hit.package and hit.form == "var_call":
            readers.append(
                {
                    "package": hit.package,
                    "path": hit.path,
                    "line": hit.line,
                    "has_default": _has_default(project, hit.path, hit.line, name),
                }
            )
    dynamic = [
        {"package": hit.package, "path": hit.path, "line": hit.line}
        for hit in index.indeterminate_for("var")
        if hit.package
    ]
    defined = sorted(
        {str(d.package) for d in index.definitions_of(name, "var") if d.package}
    )
    own = _project_name(view)
    scoped = scoped_definitions(view, name)
    report: dict[str, Any] = {
        "owner_scope": "out_of_scope",
        "readers": readers[:MAX_READERS],
        "readers_elided": max(0, len(readers) - MAX_READERS),
        "dynamic_readers": dynamic[:MAX_READERS],
        "dynamic_readers_elided": max(0, len(dynamic) - MAX_READERS),
        "defined_by": defined,
        "scoped_definitions": [scope for scope in scoped if scope != own],
    }
    return report


#: How many project-local limits a discovery summary names; the rest are counted.
MAX_LIMITS = 10


def discovery_report(
    view: DbtProjectView, project: Path, name: str, edits: list[PlanEdit]
) -> dict[str, Any]:
    """Why a complete reference scan cannot be promised for this flag (#1086).

    The same reasons ``transform remove var`` refuses on, judged the same way:
    the scan's limits and unread YAML on the project as it stands, and dynamic
    var reads on the project after the payload, so a dynamic read the payload
    rewrites is not counted. ``status`` is ``complete`` only when none of them
    stands, in which case the full check can run and a scoped contract is
    refused. Package limits are counted per package rather than listed, since
    a package is outside the change and its internals are not the author's to
    resolve.
    """

    from ..references import ReferenceIndex, column_parse_limit
    from .build import packages_dir

    index = ReferenceIndex(view)
    _, query_limits = index.references_to(name, "var")
    limits = list(dict.fromkeys([*index.limits, *query_limits]))
    unread = sorted(dict.fromkeys(index.unread))
    try:
        install = (
            packages_dir(project).resolve().relative_to(project.resolve()).as_posix()
        )
    except (OSError, ValueError):
        install = "dbt_packages"
    package_limits: dict[str, int] = {}
    local: list[str] = []
    for limit in limits:
        if limit.startswith(f"{install}/"):
            package = limit[len(install) + 1 :].split("/", 1)[0]
            package_limits[package] = package_limits.get(package, 0) + 1
        elif limit.startswith("package '"):
            package = limit.split("'")[1]
            package_limits[package] = package_limits.get(package, 0) + 1
        else:
            local.append(limit)
    after = ReferenceIndex(_project_after_edits(view, edits), scan_packages=False)
    dynamic = sorted(f"{ref.path}:{ref.line}" for ref in after.indeterminate_for("var"))
    incomplete = bool(limits or unread or dynamic)
    # Column-parse limits alone no longer refuse the full contract (#1172); they
    # are counted apart so a reader can tell the scoped route is optional there.
    column = {column_parse_limit(path) for path in index.column_unread}
    report: dict[str, Any] = {
        "status": "unchecked" if incomplete else "complete",
        "column_only": bool(limits)
        and all(limit in column for limit in limits)
        and not (unread or dynamic),
        "limits": local[:MAX_LIMITS],
        "limits_elided": max(0, len(local) - MAX_LIMITS),
        "package_limits": dict(sorted(package_limits.items())),
        "unread": unread[:MAX_LIMITS],
        "unread_elided": max(0, len(unread) - MAX_LIMITS),
        "dynamic_reads": dynamic[:MAX_READERS],
        "dynamic_reads_elided": max(0, len(dynamic) - MAX_READERS),
    }
    if incomplete:
        report["reason_code"] = "discovery_incomplete"
    return report


def validate_scope(
    view: DbtProjectView,
    edits: list[PlanEdit],
    contract: RetirementContract,
    discovery: Mapping[str, Any],
) -> None:
    """Refuse a scoped contract whose scope Dex cannot hold it to (#1086).

    A scoped retirement is admitted only where the full one cannot run, names
    only models this project defines, and covers every model the payload
    rewrites, so its assertions are bounded by what the author wrote and never
    silently narrower.
    """

    from ..references import ReferenceIndex

    if discovery.get("status") == "complete":
        raise ValueError(
            "this retirement's reference discovery is complete, so the full check "
            "can run: drop `scope` from the contract (or set it to `complete`). "
            "`scope: authored` is for a project whose scan Dex cannot finish"
        )
    index = ReferenceIndex(_project_after_edits(view, edits), scan_packages=False)
    outside = sorted(
        model
        for model in contract.outputs
        if not any(not d.package for d in index.definitions_of(model, "model"))
    )
    if outside:
        raise ValueError(
            f"a scoped retirement names only models this project defines, and "
            f"{', '.join(outside)} is not one after this payload (a package's "
            "model is out of scope, since Dex never edits it). Name the project "
            "model that outputs the feature's columns, or fix the spelling"
        )
    # A removed metric is judged absent after the payload, so one that never
    # existed here (a misspelling, a package's) would pass without a check.
    before = ReferenceIndex(view, scan_packages=False)
    unknown = sorted(
        name
        for name in contract.metrics.removed
        if not before.definitions_of(name, "metric")
    )
    if unknown:
        raise ValueError(
            f"a scoped retirement removes only metrics this project defines, and "
            f"{', '.join(unknown)} is not one before this payload (a package's "
            "metric is out of scope). Name the project metric, or fix the spelling"
        )
    written = sorted(
        {
            Path(edit.path).stem
            for edit in edits
            if edit.kind is EditKind.MODEL_SQL and edit.op is EditOp.UPSERT
        }
        - set(contract.outputs)
    )
    if written:
        raise ValueError(
            f"a scoped retirement checks only the outputs it names, and this "
            f"payload rewrites {', '.join(written)} without an `outputs` entry. "
            "State what each one outputs afterwards (`expected`, and `removed` "
            "for what goes), so no authored model goes unchecked"
        )


def _has_default(project: Path, path: str, line: int, name: str) -> bool | None:
    """Whether a package's read carries a default; ``None`` where unread."""

    try:
        content = (project / path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for call in jinja_calls(content):
        if (
            call.callee == "var"
            and call.line == line
            and call.args
            and call.args[0] == name
        ):
            if len(call.args) > 1:
                return True
            # A keyword default is dropped from args, so the line decides.
            lines = content.splitlines()
            return line <= len(lines) and "default" in lines[line - 1]
    return None


def package_warnings(name: str, scope: Mapping[str, Any]) -> list[str]:
    """The consequences for package models, which the owner scoped out."""

    out = []
    for reader in scope.get("readers") or []:
        if reader.get("has_default") is False:
            out.append(
                f"{reader['path']}:{reader['line']} in package {reader['package']} "
                f"reads var('{name}') with no default, so it stops compiling once "
                "the definition goes. Installed packages are not edited: keep a "
                f"`vars: {{{reader['package']}: {{{name}: ...}}}}` entry for it, or "
                "disable that model from this project"
            )
    if scope.get("readers") or scope.get("defined_by"):
        out.append(
            f"package models that read '{name}' keep their own behavior and now "
            "resolve it from their default or the package's own definition; "
            "`data.retirement.package_scope` names them"
        )
    for reader in scope.get("dynamic_readers") or []:
        out.append(
            f"{reader['path']}:{reader['line']} in package {reader['package']} reads "
            f"a var dex could not name, so it may or may not read '{name}'. "
            "Installed packages are not edited: read that line, and if it reads "
            "this flag, pin the value under a package-scoped `vars:` entry"
        )
    return out


# --- plan time: compile each branch ------------------------------------------


def _record(model: str) -> dict[str, Any]:
    return {"model": model, "status": "satisfied", "checks": []}


def _contradict(record: dict[str, Any], check: str, detail: str) -> None:
    record["status"] = "contradicted"
    record["finding"] = record.get("finding") or check
    record["checks"].append({"check": check, "detail": detail})


def _uncheck(record: dict[str, Any], code: str) -> None:
    if record["status"] != "contradicted":
        record["status"] = "unchecked"
    record.setdefault("reason_code", code)


def _project_after_edits(view: DbtProjectView, edits: list[PlanEdit]) -> DbtProjectView:
    from .propagate import _project_after_edits as after

    return after(view, edits)


def analyze(
    engine: DexEngine,
    stored: Any,
    project: Path,
    view: DbtProjectView,
    contract: RetirementContract,
    variable: str,
) -> dict[str, Any]:
    """Hold the proposal to the contract from compiled SQL alone.

    Free: dbt compiles in a throwaway copy, as ``transform compare`` does, and no
    statement reads a row. So this catches the kept branch on an empty table.
    """

    edits = list(stored.edits)
    sql_edits = [e for e in edits if e.kind is EditKind.MODEL_SQL]
    written = {Path(e.path).stem for e in sql_edits if e.op is EditOp.UPSERT}
    models = sorted(set(contract.outputs) | written)
    connector = engine.connector or engine.config.connector
    records = {model: _record(model) for model in models}

    for model in sorted(written - set(contract.outputs)):
        _uncheck(records[model], "no_declared_outputs")

    boolean = isinstance(contract.retained_value, bool)
    compiled: dict[str, dict[str, _Compiled]] = {}
    trees: dict[str, Any] = {}
    failure: str | None = None
    cached: list[Any] = []

    def cache() -> Any:
        """the exploration cache, read once and only when a star needs it."""

        if not cached:
            from ..storage import readable_cache

            cached.append(readable_cache(engine.store))
        return cached[0]

    try:
        compiled["retained"] = _compile_tree(
            engine,
            stored,
            project,
            view,
            models,
            connector,
            vars_={variable: contract.retained_value},
            cache=cache,
            trees=trees,
        )
        if boolean:
            compiled["other"] = _compile_tree(
                engine,
                stored,
                project,
                view,
                models,
                connector,
                vars_={variable: not contract.retained_value},
                cache=cache,
                trees=trees,
            )
        compiled["proposed"] = _compile_tree(
            engine,
            stored,
            project,
            view,
            models,
            connector,
            proposed=True,
            cache=cache,
            trees=trees,
        )
    except Exception as exc:
        failure = type(exc).__name__

    if failure is None and not boolean:
        for record in records.values():
            record["other_branch"] = "non_boolean_flag"
    if failure is not None:
        for record in records.values():
            _uncheck(record, "compile_unavailable")
    else:
        for model, record in records.items():
            if _disabled_everywhere(model, trees):
                # dbt compiles no SQL for a disabled model, so there is nothing
                # to hold to its outputs: owed, never missing (#1156).
                _uncheck(record, "not_compiled")
                record["reason"] = (
                    f"dbt leaves '{model}' disabled, so its output was never "
                    "compiled; enable it to have it judged, or delete it if it is "
                    "retired"
                )
                continue
            _judge(model, record, contract, compiled, connector)

    after = _files_after(view, edits)
    surviving, orphaned = _references_after(view, edits, contract, compiled)
    column_reads = _column_reads(
        view, edits, contract, trees.get("proposed"), connector, surviving
    )
    for model, columns in surviving.items():
        record = records.setdefault(model, _record(model))
        for column, where in columns.items():
            _contradict(
                record,
                "removed_column_still_read",
                f"'{model}.{column}' is declared removed and is still read at "
                f"{', '.join(where[:5])}",
            )

    metrics = metric_records(_index_after(view, edits), contract)
    yaml_left = yaml_reads(after, variable)
    after_view = _project_after_edits(view, edits)
    own = _project_name(view)
    definition_left = own in scoped_definitions(after_view, variable)
    records_list = [records[m] for m in sorted(records)] + metrics
    verdict = (
        "contradicted"
        if any(r["status"] == "contradicted" for r in records_list)
        else "unchecked"
        if any(r["status"] == "unchecked" for r in records_list)
        or column_reads["coverage"] == "partial"
        else "satisfied"
    )
    return {
        "status": "planned",
        "verdict": verdict,
        "models": [records[m] for m in sorted(records)],
        "metrics": metrics,
        "orphaned_models": orphaned,
        "column_reads": column_reads,
        "yaml_reads": yaml_left,
        "project_scoped_definition_left": definition_left,
    }


class _Compiled:
    """One judged model's compiled form in one tree."""

    def __init__(
        self,
        columns: list[str] | None,
        reason: str | None,
        reads: set[str],
        sql: str,
        *,
        failed: bool = False,
    ) -> None:
        """Its columns (or why they are unknown), its parents and its SQL."""

        self.columns = columns
        self.reason = reason
        self.reads = reads
        self.sql = sql
        self.failed = failed


class CompileUnavailableError(PrerequisiteError):
    """Nothing in one tree compiled: dbt absent, no profile, or a parse error.

    Never reaches a caller: :func:`analyze` turns it into ``compile_unavailable``
    records.
    """


def _compile_tree(
    engine: DexEngine,
    stored: Any,
    project: Path,
    view: DbtProjectView,
    models: list[str],
    connector: str | None,
    *,
    vars_: Mapping[str, Any] | None = None,
    proposed: bool = False,
    cache: Callable[[], Any] | None = None,
    trees: dict[str, Any] | None = None,
) -> dict[str, _Compiled]:
    """Compile ``models`` from one tree, resolving stars through the catalog.

    Shared with the toggle check (:mod:`.compiled_tree`, #1170): a ``*`` over
    a model compiled in this tree, a seed or a profiled source is read, and an
    unprofiled source names the profile that would supply it. The judged models
    and their ancestors are compiled (``+model``), and the proposal also their
    descendants, which is where a removed column can still be read (#1172). A
    model whose own compile fails is set aside, so it never takes the rest of
    the tree with it. ``trees`` receives each tree's branch, keyed
    ``proposed``, ``retained`` or ``other``.
    """

    from ..adapters import get_dialect
    from .compiled_tree import Catalog, compile_branch

    dialect = get_dialect(connector) if connector else None
    selector = (
        " ".join(f"+{m}+" if proposed else f"+{m}" for m in models) if models else None
    )
    branch = compile_branch(
        engine,
        stored,
        project,
        view,
        connector,
        vars_=vars_,
        proposed=proposed,
        select=selector,
    )
    if branch.failure is not None:
        raise CompileUnavailableError(branch.failure)
    if trees is not None:
        side = "proposed" if proposed else "retained"
        if not proposed and "retained" in trees:
            side = "other"
        trees[side] = branch
    # A proposed tree reads the plan's own seeds; the others the project's.
    seen = _project_after_edits(view, list(stored.edits)) if proposed else view
    catalog = Catalog(branch, seen, dialect, cache or (lambda: None))
    out: dict[str, _Compiled] = {}
    for label, model in branch.models.items():
        base = label.rsplit(".v", 1)[0]
        if label not in models and base not in models:
            continue
        catalog.resolve(label)
        out.setdefault(
            base,
            _Compiled(
                model.columns,
                model.reason,
                {parent.split(".")[-1] for parent in model.parents},
                model.sql,
            ),
        )
    for label, reason in branch.failed.items():
        base = label.rsplit(".v", 1)[0]
        if (label in models or base in models) and base not in out:
            out[base] = _Compiled(
                None, f"dbt could not compile it: {reason}", set(), "", failed=True
            )
    return out


def _disabled_everywhere(model: str, trees: Mapping[str, Any]) -> bool:
    """Whether ``model`` is disabled, and compiled, in no tree."""

    branches = [b for b in trees.values() if b is not None]
    if not branches:
        return False
    for branch in branches:
        names = {label.rsplit(".v", 1)[0] for label in branch.disabled}
        compiled = {label.rsplit(".v", 1)[0] for label in branch.models}
        if model not in names or model in compiled:
            return False
    return True


def _judge(
    model: str,
    record: dict[str, Any],
    contract: RetirementContract,
    compiled: Mapping[str, Mapping[str, _Compiled]],
    connector: str | None,
) -> None:
    proposed = compiled["proposed"].get(model)
    retained = compiled["retained"].get(model)
    other = compiled.get("other", {}).get(model)
    if proposed is None:
        declared = contract.outputs.get(model)
        if declared is not None:
            _contradict(
                record,
                "output_model_missing",
                f"'{model}' has declared outputs and the proposal defines no such "
                "model",
            )
        return
    record["branches"] = {
        "retained": retained.columns if retained else None,
        "other": other.columns if other else None,
        "proposed": proposed.columns,
    }
    if proposed.columns is None:
        _uncheck(record, "compile_failed" if proposed.failed else "unreadable_select")
        record["reason"] = proposed.reason
    else:
        columns = set(proposed.columns)
        if retained is not None and retained.columns is not None:
            same = columns == set(retained.columns)
            record["matches_retained"] = same
            if not same:
                kept = sorted(columns - set(retained.columns))
                lost = sorted(set(retained.columns) - columns)
                _contradict(
                    record,
                    "proposal_differs_from_retained_branch",
                    f"with the flag at {contract.retained_value!r}, '{model}' outputs "
                    "different columns than this proposal"
                    + (f"; only the proposal has {', '.join(kept)}" if kept else "")
                    + (f"; only the branch has {', '.join(lost)}" if lost else "")
                    + ". Fix the SQL, or correct retained_value if the contract "
                    "named the wrong branch",
                )
        elif retained is not None:
            _uncheck(
                record, "compile_failed" if retained.failed else "unreadable_select"
            )
            if retained.reason and "reason" not in record:
                record["reason"] = retained.reason
        declared = contract.outputs.get(model)
        if declared is not None:
            expected = {c.lower() for c in declared.expected}
            removed = {c.lower() for c in declared.removed}
            still = sorted(columns & removed)
            missing = sorted(expected - columns)
            extra = sorted(columns - expected - removed)
            if still:
                _contradict(
                    record,
                    "removed_output_present",
                    f"'{model}' still outputs {', '.join(still)}, which the "
                    "contract says goes",
                )
            if missing:
                _contradict(
                    record,
                    "expected_output_missing",
                    f"'{model}' no longer outputs {', '.join(missing)}, which the "
                    "contract expects",
                )
            if extra:
                _contradict(
                    record,
                    "unexpected_output",
                    f"'{model}' outputs {', '.join(extra)}, which the contract "
                    "neither expects nor removes",
                )
    if (
        retained is not None
        and not (retained.failed or proposed.failed)
        and proposed.reads != retained.reads
    ):
        kept = sorted(proposed.reads - retained.reads)
        lost = sorted(retained.reads - proposed.reads)
        if kept:
            _contradict(
                record,
                "reads_outside_retained_branch",
                f"'{model}' still reads {', '.join(kept)}, which the retained "
                "branch does not read. A join kept with no output column still "
                "moves rows",
            )
        if lost:
            _contradict(
                record,
                "dropped_retained_read",
                f"'{model}' no longer reads {', '.join(lost)}, which the retained "
                "branch reads",
            )
    unused = _unused_ctes(proposed.sql, connector)
    if retained is not None:
        unused -= _unused_ctes(retained.sql, connector)
    if unused:
        _contradict(
            record,
            "unused_cte",
            f"'{model}' defines {', '.join(sorted(unused))} and never reads it; the "
            "retained branch has no such leftover",
        )


def _unused_ctes(sql: str, connector: str | None) -> set[str]:
    """CTEs nothing reads; dbt's own inlined ones are not the author's."""

    import sqlglot
    from sqlglot import exp

    from ..adapters import get_dialect

    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(
            sql, read=get_dialect(connector) if connector else None
        )
    except Exception:
        return set()
    defined = {
        cte.alias
        for cte in tree.find_all(exp.CTE)
        if cte.alias and not cte.alias.startswith("__dbt__cte__")
    }
    read = {table.name for table in tree.find_all(exp.Table) if not table.db}
    return defined - read


def _files_after(view: DbtProjectView, edits: list[PlanEdit]) -> dict[str, str]:
    from .propagate import _project_after_edits

    return {
        path: source.content
        for path, source in _project_after_edits(view, edits).files.items()
    }


def _index_after(view: DbtProjectView, edits: list[PlanEdit]) -> Any:
    from ..references import ReferenceIndex
    from .propagate import _project_after_edits

    return ReferenceIndex(_project_after_edits(view, edits), scan_packages=False)


def _references_after(
    view: DbtProjectView,
    edits: list[PlanEdit],
    contract: RetirementContract,
    compiled: Mapping[str, Mapping[str, _Compiled]],
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Removed columns something still reads, and models nothing reads any more."""

    from .propagate import _READING_FORMS

    index = _index_after(view, edits)
    surviving: dict[str, dict[str, list[str]]] = {}
    for model, declared in contract.outputs.items():
        for column in declared.removed:
            where = sorted(
                f"{hit.path}:{hit.line}"
                for hit in index.references_to(f"{model}.{column}", "column")[0]
                if hit.form in _READING_FORMS["column"]
                and hit.note != "same_name_elsewhere"
                # The model's own SELECT is its output, which `outputs` judges.
                and not (hit.form == "select_column" and Path(hit.path).stem == model)
            )
            if where:
                surviving.setdefault(model, {})[column] = where

    dropped: set[str] = set()
    for model, proposed in (compiled.get("proposed") or {}).items():
        if proposed.failed:
            continue
        before: set[str] = set()
        for side in ("retained", "other"):
            entry = (compiled.get(side) or {}).get(model)
            if entry is not None:
                before |= entry.reads
        dropped |= before - proposed.reads
    deleted = {Path(e.path).stem for e in edits if e.op is EditOp.DELETE}
    orphaned = sorted(
        name
        for name in dropped - deleted
        if index.definitions_of(name, "model")
        and not [
            hit
            for hit in index.references_to(name, "model")[0]
            if hit.form in ("ref_call", "python_ref")
        ]
    )
    return surviving, orphaned


def _column_reads(
    view: DbtProjectView,
    edits: list[PlanEdit],
    contract: RetirementContract,
    branch: Any,
    connector: str | None,
    surviving: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    """How far ``removed_column_still_read`` reached, and the reads it adds (#1172).

    The source scan blanks Jinja before parsing, and a file it cannot parse
    names no column. Each such file is read again from what dbt compiled for
    it in the proposal: a model downstream of a declared model whose compiled
    SQL names a removed column is a surviving read, added to ``surviving`` by
    its path. A macro's columns land in the models that call it, so it needs
    no read of its own. Any other file (a model that did not compile, a
    disabled one, a test or an analysis) is ``unchecked``, never clean.
    """

    from ..adapters import get_dialect
    from .compiled_tree import identifiers, macro_callers, project_name

    removed = {
        (model, column.lower())
        for model, declared in contract.outputs.items()
        for column in declared.removed
    }
    report: dict[str, Any] = {
        "coverage": "not_applicable",
        "unchecked": [],
        "unchecked_elided": 0,
        "read_from_compiled": [],
    }
    if not removed:
        return report
    index = _index_after(view, edits)
    unread = sorted(set(index.column_unread))
    macro_bases = [p.rstrip("/") + "/" for p in view.macro_paths]
    models = dict(branch.models) if branch is not None else {}
    by_path = {model.path: (label, model) for label, model in models.items()}
    dialect = get_dialect(connector) if connector else None
    unchecked: list[str] = []
    compiled_from: list[str] = []

    def read(label: str, model: Any) -> bool:
        """Hold one compiled model's SQL to the removed columns; False if unread."""

        names = identifiers(model.sql, dialect)
        if names is None:
            return False
        if model.path not in compiled_from:
            compiled_from.append(model.path)
        parents = {
            uid.split(".")[2]
            for uid in model.parents
            if uid.startswith("model.") and len(uid.split(".")) > 2
        }
        for declared, column in sorted(removed):
            if label.rsplit(".v", 1)[0] == declared or declared not in parents:
                continue
            if column not in names:
                continue
            where = surviving.setdefault(declared, {}).setdefault(column, [])
            if not any(
                site.startswith(f"{model.path}:") or site == model.path
                for site in where
            ):
                where.append(model.path)
        return True

    callers = macro_callers(branch, project_name(view)) if branch is not None else {}
    for path in unread:
        if any(path.startswith(base) for base in macro_bases):
            # A macro's columns land in the models that call it, whose own
            # source scan sees only the call: read each caller's compiled SQL.
            if branch is None:
                unchecked.append(path)
                continue
            for label in callers.get(path, []):
                model = models[label]
                if model.sql and not read(label, model) and model.path not in unchecked:
                    unchecked.append(model.path)
            continue
        found = by_path.get(path)
        if found is not None and not found[1].sql:
            # The proposal compiles the declared models and their descendants,
            # so an enabled model it left uncompiled reads none of them.
            continue
        if found is None or not read(*found):
            unchecked.append(path)
    report["coverage"] = "partial" if unchecked else "complete"
    report["unchecked"] = unchecked[:MAX_LIMITS]
    report["unchecked_elided"] = max(0, len(unchecked) - MAX_LIMITS)
    report["read_from_compiled"] = compiled_from[:MAX_LIMITS]
    return report


def metric_records(index: Any, contract: RetirementContract) -> list[dict[str, Any]]:
    """Each named metric against the definitions the project holds."""

    records = []
    for name, wanted in (
        *((n, True) for n in contract.metrics.retained),
        *((n, False) for n in contract.metrics.removed),
    ):
        present = bool(index.definitions_of(name, "metric"))
        satisfied = present is wanted
        record: dict[str, Any] = {
            "model": name,
            "metric": True,
            "expected": "retained" if wanted else "removed",
            "status": "satisfied" if satisfied else "contradicted",
        }
        if not satisfied:
            record["finding"] = (
                "retirement_metric_missing" if wanted else "retirement_metric_present"
            )
        records.append(record)
    return records


def plan_warnings(variable: str, analysis: Mapping[str, Any]) -> list[str]:
    """One warning per contradiction, and one for everything not judged."""

    out = []
    for record in analysis.get("models") or []:
        for check in record.get("checks") or []:
            out.append(f"retirement of '{variable}': {check['detail']}")
        if record.get("reason_code"):
            out.append(
                f"retirement of '{variable}': '{record['model']}' is not checked "
                f"({record['reason_code']}), which is not clean"
            )
    for record in analysis.get("metrics") or []:
        if record["status"] == "contradicted":
            out.append(
                f"retirement of '{variable}': metric '{record['model']}' is declared "
                f"{record['expected']} and the proposal disagrees"
            )
    for where in analysis.get("yaml_reads") or []:
        out.append(f"retirement of '{variable}': {where} still reads the flag")
    if analysis.get("project_scoped_definition_left"):
        out.append(
            f"retirement of '{variable}': dbt_project.yml still sets it under this "
            "project's own `vars:` scope, which `transform remove` does not edit; "
            "remove that entry with a project_yml edit in this payload"
        )
    reads = analysis.get("column_reads") or {}
    if reads.get("coverage") == "partial":
        files = list(reads.get("unchecked") or [])
        more = int(reads.get("unchecked_elided") or 0)
        out.append(
            f"retirement of '{variable}': whether a removed column is still read "
            f"is unchecked in {', '.join(files)}"
            + (f" and {more} more" if more else "")
            + ": Dex could not read their columns from source or from compiled "
            "SQL. `data.retirement.column_reads` names them; read each one"
        )
    for model in analysis.get("orphaned_models") or []:
        out.append(
            f"retirement of '{variable}': nothing reads '{model}' any more; remove "
            "it in this payload if the feature was its only reason to exist"
        )
    return out


def scope_warnings(
    variable: str, contract: RetirementContract, discovery: Mapping[str, Any]
) -> list[str]:
    """What a scoped retirement checked, and what it leaves unchecked."""

    reasons = []
    metrics = [*contract.metrics.retained, *contract.metrics.removed]
    local = len(discovery.get("limits") or []) + int(
        discovery.get("limits_elided") or 0
    )
    if local:
        reasons.append(f"{local} project file(s) the scan could not read")
    packages = discovery.get("package_limits") or {}
    if packages:
        reasons.append(
            f"{sum(packages.values())} limit(s) in package(s) "
            + ", ".join(sorted(packages))
        )
    unread = len(discovery.get("unread") or []) + int(
        discovery.get("unread_elided") or 0
    )
    if unread:
        reasons.append(f"{unread} unread file(s) or package(s)")
    dynamic = list(discovery.get("dynamic_reads") or [])
    if dynamic:
        reasons.append(
            f"{len(dynamic) + int(discovery.get('dynamic_reads_elided') or 0)} "
            f"dynamic var read(s) that may read it ({', '.join(dynamic[:5])})"
        )
    return [
        f"retirement of '{variable}' is scoped to the outputs it names "
        f"({', '.join(sorted(contract.outputs))}"
        + (f"; metrics {', '.join(metrics)}" if metrics else "")
        + "). Reference discovery is unchecked ("
        + "; ".join(reasons or ["discovery_incomplete"])
        + "), so passing these checks does not show every reader was found or "
        "the feature retired everywhere: `data.retirement.discovery` lists what "
        "was not read"
    ]


# --- after a build -----------------------------------------------------------


def verify_outputs(
    stored: Mapping[str, Any],
    built: Mapping[str, str],
    scope: set[str],
    relations: Mapping[str, str],
    live_identifiers: list[str],
    adapter_once: Callable[[], Any],
    index: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """The built relations' columns against the contract.

    From metadata, the read the column contract already makes, so it costs no
    statement and judges an empty table the same as a full one. A model this run
    did not build keeps an earlier run's relation, which proves nothing here.
    """

    from ..cache import match_identifier
    from ..envelope import redact
    from ..guards.cost_guard import CostGuardError
    from ..maintain.drift import DriftFinding

    contract = contract_of(stored)
    labels = {built[uid] for uid in scope}
    findings: list[Any] = []
    records: list[dict[str, Any]] = []
    for model, declared in sorted(contract.outputs.items()):
        record: dict[str, Any] = {"model": model, "status": "unchecked"}
        records.append(record)
        if model not in labels:
            record["reason_code"] = "not_built"
            continue
        relation = relations.get(model)
        if relation is None:
            record["reason_code"] = "no_relation"
            continue
        matches = match_identifier(relation, live_identifiers)
        if len(matches) != 1:
            record["reason_code"] = "relation_not_found"
            continue
        try:
            _meta, columns = adapter_once().table_metadata(matches[0])
        except CostGuardError:
            raise
        except Exception as exc:
            record["reason_code"] = "metadata_unreadable"
            record["reason"] = redact(type(exc).__name__)
            continue
        actual = {column.name.lower() for column in columns}
        expected = {c.lower() for c in declared.expected}
        removed = {c.lower() for c in declared.removed}
        present = sorted(actual & removed)
        extra = sorted(actual - expected - removed)
        missing = sorted(expected - actual)
        record["actual"] = sorted(actual)
        record["status"] = "satisfied"
        for code, names, what in (
            ("retirement_output_present", present, "the contract removes"),
            ("retirement_output_unexpected", extra, "the contract does not list"),
        ):
            if names:
                record["status"] = "contradicted"
                record["finding"] = record.get("finding") or code
                findings.append(
                    DriftFinding(
                        axis="build",
                        code=code,
                        identifier=model,
                        severity="high",
                        detail=(
                            f"'{model}' was built with {', '.join(names)}, which "
                            f"{what}: the flag's other behavior survived"
                        ),
                        data={"model": model, "columns": names},
                    )
                )
        if missing:
            record["status"] = "contradicted"
            record["finding"] = record.get("finding") or "retirement_output_missing"
            findings.append(
                DriftFinding(
                    axis="build",
                    code="retirement_output_missing",
                    identifier=model,
                    severity="high",
                    detail=(
                        f"'{model}' was built without {', '.join(missing)}, which "
                        "the contract expects"
                    ),
                    data={"model": model, "columns": missing},
                )
            )
    records.extend(metric_records(index, contract))
    reads = stored.get("column_reads") or {}
    if reads.get("coverage") == "partial":
        records.append(
            {
                "model": str(stored.get("variable")),
                "check": "removed_column_reads",
                "status": "unchecked",
                "reason_code": "column_reads_partial",
                "unchecked": list(reads.get("unchecked") or []),
            }
        )
    if contract.scope == "authored":
        records.append(discovery_record(str(stored.get("variable")), stored))
    return findings, records


def discovery_record(variable: str, stored: Mapping[str, Any]) -> dict[str, Any]:
    """Global reference discovery for a scoped retirement: never passed.

    The outputs above say what the authored models hold; this says that nothing
    established every reader was found and retired, so a completion summary
    keeps it among its unchecked obligations however the outputs turned out.
    """

    discovery = stored.get("discovery") or {}
    return {
        "model": variable,
        "check": "reference_discovery",
        "status": "unchecked",
        "reason_code": discovery.get("reason_code") or "discovery_incomplete",
    }
