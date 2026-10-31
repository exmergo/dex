"""Does a stored plan keep a model's results? (#856)

A refactor can build green and keep its row count while it changes a measure.
:mod:`.multiset_compare` answers that for two compiled SELECTs. This module makes
the two SELECTs from one plan: the model as the project defines it now, and the
model as the plan would define it.

Each side compiles in its own throwaway copy of the project, so an intermediate
the same plan extracts is seen by the proposed side and does not exist on the
baseline side. Every model the plan touches, and the compared model itself, is
made ephemeral in both copies. dbt then inlines them, so the only relations either
statement reads are ones the plan does not change, and both sides read the same
ones. Nothing is built.

A verdict is about the inputs compared at the time compared. The baseline is
re-executed over today's dev relations, which says nothing about what an earlier
warehouse state held or what a future input would do.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
import sqlglot.expressions as exp

from .. import command_args
from ..adapters import get_dialect
from ..cache import DexCache, match_identifier
from ..dbt_project import EditOp, content_hash, model_file
from ..dbt_project import load as load_project
from ..errors import DexError
from ..guards.sql_guard import referenced_relations
from ..storage import readable_cache
from .build import DbtRunError, ShadowRun, assert_dev_target
from .multiset_compare import (
    ReasonCode,
    RefactorCompareResult,
    _NotCheckedError,
    _raw_estimate,
    compare_refactors,
)
from .mutation import EPHEMERAL_HEADER
from .plans import EditKind, PlanEdit, TransformPlan
from .results import RefactorComparisonResult

if TYPE_CHECKING:
    from ..engine import DexEngine

COMMAND = "transform compare"

#: Edits that change a relation both sides read, which no inlining can undo: the
#: dev relation still holds what the last build wrote.
_UNSYNCHRONIZING_KINDS = frozenset({EditKind.SEED_CSV, EditKind.SNAPSHOT_SQL})

#: Edits that can change how an untouched parent compiles. Its dev relation keeps
#: what it was built with, so both graphs are compiled through their ancestors and
#: any parent that compiles differently makes the inputs unsynchronized.
_GLOBAL_KINDS = frozenset(
    {EditKind.MACRO_SQL, EditKind.PROJECT_YML, EditKind.PACKAGES_YML}
)

_NONDETERMINISTIC_NODES: tuple[type[exp.Expression], ...] = (
    exp.Rand,
    exp.Randn,
    exp.Uuid,
    exp.CurrentTimestamp,
    exp.CurrentDate,
    exp.CurrentTime,
    exp.CurrentDatetime,
    exp.TableSample,
)

_NONDETERMINISTIC_NAMES = frozenset(
    {
        "random",
        "rand",
        "randn",
        "uniform",
        "uuid",
        "uuid_string",
        "generate_uuid",
        "gen_random_uuid",
        "generateuuidv4",
        "newid",
        "now",
        "today",
        "getdate",
        "sysdate",
        "systimestamp",
        "localtimestamp",
        "localtime",
        "transaction_timestamp",
        "statement_timestamp",
        "clock_timestamp",
        "timeofday",
        "current_timestamp",
        "current_date",
        "current_time",
    }
)


class _NotComparableError(DexError):
    """This plan cannot be compared, for a reason the result reports."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class _Side:
    sql: str
    digest: str
    grain: list[str] = field(default_factory=list)
    #: Each upstream model read from the warehouse, by a digest of its compiled SQL.
    #: Filled only when the plan can change how an untouched parent compiles.
    parents: dict[str, str] = field(default_factory=dict)


def compare(
    engine: DexEngine,
    plan_id: str | None = None,
    *,
    model: str | None = None,
    column_mapping: Mapping[str, str] | None = None,
    key_columns: Sequence[str] | None = None,
    target: str | None = None,
    accepted_type_changes: Sequence[str] | None = None,
) -> RefactorComparisonResult:
    """Compare one model before and after a stored, unapplied plan.

    DuckDB allows one writer or many readers, and dbt opens the file to write. So
    a handle dex holds is released before dbt compiles and again afterwards,
    leaving the engine able to run dbt or compare again.
    """

    local = (engine.connector or engine.config.connector) == "duckdb"
    if local:
        engine.close()
    try:
        return _compare(
            engine,
            plan_id,
            model=model,
            column_mapping=column_mapping,
            key_columns=key_columns,
            target=target,
            accepted_type_changes=accepted_type_changes,
        )
    finally:
        if local:
            engine.close()


def _compare(
    engine: DexEngine,
    plan_id: str | None,
    *,
    model: str | None,
    column_mapping: Mapping[str, str] | None,
    key_columns: Sequence[str] | None,
    target: str | None,
    accepted_type_changes: Sequence[str] | None,
) -> RefactorComparisonResult:
    """The comparison itself.

    The order follows the rest of the transform group: the free refusals first,
    then the dev-target check, then both compiles, and only then a connection.
    On DuckDB dbt has to hold the file while it compiles, so dex's own read-only
    handle waits until dbt is done. Nothing runs before the handshake.
    """

    from . import dev_target
    from .commands import _resolve_plan_id

    store, plan_id = _resolve_plan_id(engine, plan_id, "comparing a plan")
    stored = store.load_plan(plan_id)
    if stored.edit_target != "project":
        raise ValueError(
            "this plan edits a semantic layer's documents rather than a "
            "transformation project, so it has no compiled model to compare"
        )
    repo_root = Path(engine.require_repo_root("comparing a plan"))
    project = repo_root / stored.project_dir
    config = engine.config
    target = target or config.dbt_target or "dev"
    assert_dev_target(target, config.dbt_target)
    connector = engine.connector or config.connector
    if not connector:
        raise ValueError(
            "transform compare needs a connector: set `connector:` in "
            ".dex/config.yml or pass --connector"
        )
    warnings: list[str] = []
    # Ahead of choosing a model: once applied, the models this plan created exist
    # on disk and would read as rewritten, which asks for a --model that cannot help.
    if stored.applied_at:
        return _not_checked_result(
            {"plan_id": plan_id, "model": model or "", "target": target},
            "plan_applied",
            "this plan is already applied, so the tree no longer holds the "
            "baseline; plan the change again from the prior commit to compare it",
            warnings,
        )
    chosen = _chosen_model(stored, project, model)
    identity: dict[str, Any] = {"plan_id": plan_id, "model": chosen, "target": target}

    def not_checked(code: ReasonCode, detail: str | None) -> RefactorComparisonResult:
        return _not_checked_result(identity, code, detail, warnings)

    stale = _stale_paths(stored, project)
    if stale:
        return not_checked(
            "plan_stale",
            "changed since planning: " + ", ".join(stale),
        )
    unsynchronized = [
        edit.path for edit in stored.edits if edit.kind in _UNSYNCHRONIZING_KINDS
    ]
    if unsynchronized:
        return not_checked(
            "inputs_unsynchronized",
            "this plan changes a seed or a snapshot, and the dev relation still "
            "holds what the last build wrote: " + ", ".join(unsynchronized),
        )
    through_ancestors = any(edit.kind in _GLOBAL_KINDS for edit in stored.edits)

    effective = config.model_copy(update={"connector": connector})
    warnings.extend(
        dev_target.check(
            project,
            target,
            effective,
            repo_root,
            store=store,
            connection=engine.connection,
        )
    )

    try:
        baseline = _compile_baseline(
            engine, stored, project, chosen, target, connector, through_ancestors
        )
    except _NotComparableError as exc:
        return not_checked(exc.code, exc.detail)
    try:
        proposed = _compile_proposed(
            engine, stored, project, chosen, target, connector, through_ancestors
        )
    except _NotComparableError as exc:
        return not_checked(exc.code, exc.detail)

    moved = sorted(
        name
        for name in baseline.parents.keys() & proposed.parents.keys()
        if baseline.parents[name] != proposed.parents[name]
    )
    if moved:
        return not_checked(
            "inputs_unsynchronized",
            "this plan changes how these untouched parents compile, and their dev "
            "relations hold what the last build wrote: " + ", ".join(moved),
        )

    dialect = get_dialect(connector)
    for side, sql in (("baseline", baseline.sql), ("proposed", proposed.sql)):
        found = nondeterministic_expression(sql, dialect)
        if found is not None:
            return not_checked(
                "nondeterministic_expression",
                f"the {side} calls {found}, so two runs can differ with no "
                "change at all",
            )

    keys = list(key_columns) if key_columns else None
    key_source = "request" if keys else None
    if keys is None and baseline.grain:
        keys, key_source = list(baseline.grain), "declaration"

    identity.update(
        baseline_compiled=baseline.digest,
        proposed_compiled=proposed.digest,
        key_columns=keys,
        key_source=key_source,
    )

    adapter = engine._adapter(COMMAND, reads_dev_target=True)
    names = list(
        dict.fromkeys(
            referenced_relations(baseline.sql, dialect=dialect)
            + referenced_relations(proposed.sql, dialect=dialect)
        )
    )
    identity["input_scope"] = sorted(names)

    from ..explore import commands as explore

    cache = readable_cache(store)
    missing = _missing_relations(adapter, names)
    if missing:
        return not_checked(
            "inputs_unsynchronized",
            "this connection cannot see: "
            + ", ".join(missing)
            + "; build them with `transform build --target dev`, or add a source "
            "namespace to the allowlist",
        )
    gap = explore._object_gap(adapter, cache, names)

    prepriced = False
    profiled: list[str] = []
    if gap.to_profile:
        try:
            estimate = _profile_handshake(
                adapter, gap.to_profile, baseline.sql, proposed.sql, keys, config
            )
        except _NotCheckedError as exc:
            return not_checked(exc.reason_code, exc.detail)
        if estimate is not None:
            result = _not_checked_result(
                identity,
                "awaiting_confirmation",
                None,
                warnings,
                pending=estimate,
            )
            return command_args.stamp_spend(result, adapter)
        prepriced = True
        _profiled, cache, _locator, _note = explore._profile_into_cache(
            store,
            adapter,
            config,
            explore._project_definitions(engine, False),
            gap.to_profile,
            cache,
            datetime.now(UTC),
        )
        profiled = list(gap.to_profile)
        warnings.extend(explore._auto_profile_warning(profiled, adapter))

    verdict = compare_refactors(
        baseline.sql,
        proposed.sql,
        adapter=adapter,
        cache=cache if cache is not None else DexCache(),
        limits=config.query,
        command=COMMAND,
        baseline_id=baseline.digest,
        proposed_id=proposed.digest,
        input_scope=", ".join(sorted(names)),
        column_mapping=column_mapping,
        key_columns=keys,
        prepriced=prepriced,
        accepted_type_changes=accepted_type_changes,
    )
    result = _result(identity, verdict, warnings, profiled)
    return command_args.stamp_spend(result, adapter)


def _missing_relations(adapter: Any, names: Sequence[str]) -> list[str]:
    """Names the statements read that the connection's own listing does not hold.

    A listing that cannot be read settles nothing: the statements then fail on
    their own, as ``not_executable``.
    """

    try:
        live = [meta.identifier for meta in adapter.list_objects()]
    except Exception:
        return []
    return [name for name in names if not match_identifier(name, live)]


def nondeterministic_expression(sql: str, dialect: str) -> str | None:
    """The first expression that can differ between two runs, or ``None``.

    Reads the compiled statement. A parse failure says nothing here: the firewall
    owns that refusal.
    """

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None
    for node in tree.walk():
        if isinstance(node, _NONDETERMINISTIC_NODES):
            return type(node).__name__.lower()
        if isinstance(node, exp.Anonymous):
            name = str(node.name).lower()
            if name in _NONDETERMINISTIC_NAMES:
                return name
    return None


def edited_existing_models(
    stored_edits: Sequence[PlanEdit], project: Path
) -> list[str]:
    """The models a plan rewrites that already exist: what a comparison can cover."""

    names = []
    for edit in stored_edits:
        if edit.kind is not EditKind.MODEL_SQL or edit.op is not EditOp.UPSERT:
            continue
        if not (project / edit.path).is_file():
            continue
        names.append(Path(edit.path).stem)
    return names


def _chosen_model(stored: TransformPlan, project: Path, model: str | None) -> str:
    if model:
        return model
    candidates = edited_existing_models(stored.edits, project)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(
            "this plan rewrites no existing model, so there is no baseline to "
            "compare; name the model the refactor should preserve with --model"
        )
    raise ValueError(
        "this plan rewrites several models: "
        + ", ".join(sorted(candidates))
        + "; name the one whose results should not change with --model"
    )


def _stale_paths(stored: TransformPlan, project: Path) -> list[str]:
    stale = []
    for edit in stored.edits:
        path = project / edit.path
        found = (
            content_hash(path.read_text(encoding="utf-8")) if path.is_file() else None
        )
        if found != edit.old_content_hash:
            stale.append(edit.path)
    return stale


def _touched(stored: TransformPlan) -> list[PlanEdit]:
    return [edit for edit in stored.edits if edit.kind is EditKind.MODEL_SQL]


def _shadow(engine: DexEngine, project: Path, target: str, connector: str) -> ShadowRun:
    from ..connect import paradigm_for

    config = engine.config
    effective = config.model_copy(update={"connector": connector})
    return ShadowRun(
        project,
        target=target,
        connector=connector,
        paradigm=paradigm_for(connector, effective),
        ceiling=engine.budget if engine.budget is not None else config.budget.ceiling,
    )


def _compile_baseline(
    engine: DexEngine,
    stored: TransformPlan,
    project: Path,
    model: str,
    target: str,
    connector: str,
    through_ancestors: bool = False,
) -> _Side:
    view = load_project(project)
    try:
        chosen_path = model_file(view, model)
    except DexError as exc:
        raise _NotComparableError(
            "baseline_not_compilable", f"the project defines no model '{model}'"
        ) from exc
    paths = {chosen_path: view.files[chosen_path].content}
    for edit in _touched(stored):
        existing = view.files.get(edit.path)
        if existing is not None:
            paths[edit.path] = existing.content
    with _shadow(engine, project, target, connector) as shadow:
        return _compile_side(
            shadow, paths, model, "baseline_not_compilable", through_ancestors
        )


def _compile_proposed(
    engine: DexEngine,
    stored: TransformPlan,
    project: Path,
    model: str,
    target: str,
    connector: str,
    through_ancestors: bool = False,
) -> _Side:
    view = load_project(project)
    paths: dict[str, str] = {}
    with _shadow(engine, project, target, connector) as shadow:
        for edit in stored.edits:
            if edit.op is EditOp.DELETE:
                shadow.remove(edit.path)
            else:
                shadow.write(edit.path, edit.new_content or "")
        for edit in _touched(stored):
            if edit.op is EditOp.UPSERT:
                paths[edit.path] = edit.new_content or ""
        if not any(Path(path).stem == model for path in paths):
            deleted = any(
                edit.op is EditOp.DELETE and Path(edit.path).stem == model
                for edit in stored.edits
            )
            if deleted:
                raise _NotComparableError(
                    "proposed_not_compilable", f"this plan deletes '{model}'"
                )
            try:
                chosen_path = model_file(view, model)
            except DexError as exc:
                raise _NotComparableError(
                    "proposed_not_compilable",
                    f"neither the project nor this plan defines '{model}'",
                ) from exc
            paths[chosen_path] = view.files[chosen_path].content
        return _compile_side(
            shadow, paths, model, "proposed_not_compilable", through_ancestors
        )


def _compile_side(
    shadow: ShadowRun,
    paths: Mapping[str, str],
    model: str,
    code: ReasonCode,
    through_ancestors: bool = False,
) -> _Side:
    """Inline the given models and compile ``model`` in this copy of the project."""

    shadow.strip_run_hooks()
    for path, content in paths.items():
        if not path.endswith(".sql"):
            raise _NotComparableError(code, f"'{path}' is not a SQL model")
        # Appended, because dbt gives a scalar config key to the last config()
        # call in a file, so a model's own `materialized` would otherwise win.
        shadow.write(path, content + "\n" + EPHEMERAL_HEADER + "\n")
        shadow.strip_semantic_models(Path(path).stem)
    try:
        manifest = shadow.compile(f"+{model}" if through_ancestors else model)
    except DbtRunError as exc:
        raise _NotComparableError(code, _compile_detail(str(exc))) from exc
    matches = [
        node
        for uid, node in manifest.get("nodes", {}).items()
        if uid.startswith("model.") and node.get("name") == model
    ]
    if not matches:
        raise _NotComparableError(code, f"dbt compiled no model named '{model}'")
    node = matches[0]
    if node.get("language") == "python":
        raise _NotComparableError(code, f"'{model}' is a Python model")
    if (node.get("config") or {}).get("sql_header"):
        raise _NotComparableError(
            code,
            f"'{model}' declares a sql_header, which only a materialization emits",
        )
    sql = (node.get("compiled_code") or "").strip().rstrip(";")
    if not sql:
        raise _NotComparableError(code, f"dbt wrote no compiled SQL for '{model}'")
    meta = (node.get("config") or {}).get("meta") or {}
    dex = meta.get("dex") if isinstance(meta, dict) else None
    grain = dex.get("grain") if isinstance(dex, dict) else None
    return _Side(
        sql=sql,
        digest=hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
        grain=[str(column) for column in grain] if isinstance(grain, list) else [],
        parents=_built_parents(manifest, node) if through_ancestors else {},
    )


def _built_parents(
    manifest: Mapping[str, Any], node: Mapping[str, Any]
) -> dict[str, str]:
    """Every model upstream of ``node`` that is read as built, by compiled digest."""

    nodes = manifest.get("nodes", {})
    found: dict[str, str] = {}
    pending = list((node.get("depends_on") or {}).get("nodes") or [])
    seen: set[str] = set()
    while pending:
        unique_id = pending.pop()
        if unique_id in seen or not unique_id.startswith("model."):
            continue
        seen.add(unique_id)
        parent = nodes.get(unique_id) or {}
        pending.extend((parent.get("depends_on") or {}).get("nodes") or [])
        if (parent.get("config") or {}).get("materialized") == "ephemeral":
            continue
        compiled = (parent.get("compiled_code") or "").strip()
        found[str(parent.get("name"))] = hashlib.sha256(
            compiled.encode("utf-8")
        ).hexdigest()[:16]
    return found


def _compile_detail(message: str) -> str:
    """What a failed compile may say.

    dbt's compilation and parsing errors quote the project. Anything else came
    from the warehouse or the connection, whose words stay out of the result.
    """

    lowered = message.lower()
    if "compilation error" in lowered or "parsing error" in lowered:
        return _clip(message)
    return (
        "dbt could not compile this model against the dev target; "
        "run `dbt compile` there to read why"
    )


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _profile_handshake(adapter, to_profile, left, right, keys, config):
    """One ask for profiling the inputs and running the comparison."""

    from ..explore import commands as explore
    from ..explore.commands import blob_override_paths

    if command_args.cost_gate(adapter) is None:
        return None
    profile_estimate, per_table = explore._profile_estimate(
        adapter, to_profile, include_blobs=blob_override_paths(config.blob_overrides)
    )
    # Raises _NotCheckedError with the exception's class name alone, so a warehouse
    # refusal here reads as it does when every input already has a profile.
    comparison = _raw_estimate(left, right, adapter, keyed=bool(keys))
    return command_args.confirmation_request(
        COMMAND,
        adapter,
        profile_estimate + comparison,
        per_table={**per_table, "(refactor comparison)": comparison},
        notes=[
            f"{len(to_profile)} relation(s) this comparison reads have no usable "
            "profile, and the firewall reads PII flags from the cache; this "
            "estimate covers profiling them and running the comparison"
        ],
    )


def _result(
    identity: dict[str, Any],
    verdict: RefactorCompareResult,
    warnings: list[str],
    profiled: list[str],
) -> RefactorComparisonResult:
    return RefactorComparisonResult(
        **_identity_fields(identity),
        status=verdict.status,
        reason_code=verdict.reason_code,
        detail=verdict.detail,
        compared_at=verdict.compared_at,
        schema_diff=_jsonable(verdict.schema),
        multiset=_jsonable(verdict.multiset),
        keyed=_jsonable(verdict.keyed),
        keyed_reason=verdict.keyed_reason,
        profiled_on_demand=profiled,
        pending_confirmation=verdict.pending,
        warnings=warnings,
    )


def _not_checked_result(
    identity: dict[str, Any],
    code: ReasonCode,
    detail: str | None,
    warnings: list[str],
    *,
    pending: Any = None,
) -> RefactorComparisonResult:
    return RefactorComparisonResult(
        **_identity_fields(identity),
        status="not_checked",
        reason_code=code,
        detail=detail,
        compared_at=datetime.now(UTC).isoformat(),
        pending_confirmation=pending,
        warnings=warnings,
    )


def _identity_fields(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": identity["plan_id"],
        "model": identity["model"],
        "target": identity["target"],
        "baseline_compiled": identity.get("baseline_compiled"),
        "proposed_compiled": identity.get("proposed_compiled"),
        "input_scope": identity.get("input_scope") or [],
        "key_columns": identity.get("key_columns"),
        "key_source": identity.get("key_source"),
    }


def _jsonable(value: Any) -> dict[str, Any] | None:
    from dataclasses import asdict

    return None if value is None else asdict(value)
