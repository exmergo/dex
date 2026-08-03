"""Transform orchestration, in two layers.

The lower layer is the run functions: they take an :class:`~..engine.DexEngine`,
resolve the dbt project, drive the plan/apply/build engine, and return a record
from :mod:`.results`. The upper layer is the ``cmd_*`` shims: argparse in,
envelope out. The transform skill fronts both authoring CLI groups (``transform``
and ``semantic``); they share one plan store and one write path, which is why
they live in one package.

The caller is the author: model SQL and semantic YAML arrive as edit payloads.
The engine validates, diffs, and stores; nothing touches the dbt project until an
explicit apply.

This is the most filesystem-entangled group, and deliberately so. The dbt project
is the source of truth and stays a git-reviewable filesystem artifact, so every
command here needs a repo root and refuses without one, naming what needed it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

import yaml

from .. import command_args
from .. import envelope as env
from ..dbt_project import ApplyResult as PlanApplyResult
from ..dbt_project import EditOp
from ..errors import DexError
from ..results import to_envelope
from ..storage import Store
from . import plans as plans_mod
from . import semantic as semantic_mod
from .plans import EditKind, PlanEdit
from .results import (
    ApplyResult,
    BuildResult,
    DepsResult,
    InitResult,
    MacroListResult,
    MacroResult,
    PlanListResult,
    PlanResult,
)

if TYPE_CHECKING:
    from ..engine import DexEngine

# What actually caps a dbt statement server-side, per compute-time connector:
# a per-connector fact, kept out of the shared build arm so the next connector
# adds an entry instead of nesting a conditional. dex cannot inject a per-build
# cap through any of these dbt adapters.
_COMPUTE_TIME_CAP_NOTES = {
    "redshift": (
        "a statement_timeout on the dbt dev user and a workgroup usage "
        "limit are the server-side caps (dex cannot inject one per build)"
    ),
}
_DEFAULT_COMPUTE_TIME_CAP_NOTE = (
    "the warehouse-level statement timeout and auto-suspend are the server-side caps"
)


class BuildFailedError(DexError):
    """A dbt run that started and did not succeed.

    Carries the run summary and the cost, because a failed build still consumed
    warehouse time and a caller triaging it needs both dbt's first message and
    what the attempt cost.
    """

    def __init__(self, message: str, *, result: BuildResult):
        super().__init__(message)
        self.result = result


def init_project(
    engine: DexEngine,
    name: str,
    *,
    connector: str | None = None,
    path: str | None = None,
    layered_schemas: bool = False,
) -> InitResult:
    """Bootstrap a dbt project in the repo, rendering profiles from dex config.

    The one command that writes files without a plan first, because there is no
    project yet to plan against. It also needs a repo root more than any other:
    it creates a directory tree and a ``.dex/config.yml`` next to it.

    The engine-wide connector fall-through is deliberately not used here. An
    explicit ``connector`` wins, a ``connector:`` committed in config is accepted
    and attributed, and a bare init is an error. Init bakes the connector into
    the generated ``profiles.yml``, and misconfiguration that works is the worst
    kind.
    """

    from . import dev_target
    from . import init as init_mod

    repo_root = engine.require_repo_root("scaffolding a dbt project")
    source = "flag"
    if not connector:
        declared = engine.config if engine.has_declared_config else None
        if declared is not None and "connector" in declared.model_fields_set:
            connector, source = declared.connector, "config"
    if not connector:
        raise ValueError(
            "transform init needs an explicit connector and never defaults: pass "
            "--connector <" + "|".join(init_mod.VALID_CONNECTORS) + "> or declare "
            "connector: in .dex/config.yml"
        )

    result = init_mod.init_project(
        name,
        connector,
        path=path,
        repo_root=repo_root,
        layered_schemas=layered_schemas,
    )

    # The renderers persisted the resolved dev namespaces into .dex/config.yml,
    # so a fresh load has exactly the names the profile was rendered from. The
    # check is advisory and files are already written: existing content in a
    # namespace the project will build into is a warning, never a refusal.
    from ..config import DexConfig, load_config

    fresh = load_config(repo_root) or DexConfig()
    preflight = dev_target.content_check(
        fresh,
        repo_root,
        layered=layered_schemas,
        store=engine.store,
        connection=engine.connection,
    )

    return InitResult(
        project_name=result.project_name,
        project_dir=result.project_dir,
        connector=result.connector,
        connector_source=source,
        created=result.created,
        diffs=result.diffs,
        warnings=preflight,
    )


def cmd_init(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = init_project(
            engine,
            getattr(args, "argument", None) or "",
            connector=getattr(args, "connector", None),
            path=getattr(args, "path", None),
            layered_schemas=bool(getattr(args, "layered_schemas", False)),
        )
    except ValueError as exc:
        return env.error_for(exc)
    return to_envelope(
        result,
        hints={
            "next": "run `explore map` if you have not yet, then propose staging "
            "models with `transform plan --scaffold <table>`"
        },
    )


def plan(
    engine: DexEngine,
    intent: str,
    *,
    edits: list[PlanEdit] | None = None,
    scaffold: list[str] | None = None,
) -> PlanResult:
    """Turn authored edits into a stored plan of reviewable diffs.

    ``scaffold`` prepends staging skeletons built from the exploration cache.
    Deletes and project/profile edits are gated by dbt's own parser here rather
    than at build time, because a broken ``dbt_project.yml`` breaks everything
    and an orphaning delete is cheaper to catch before it is stored.
    """

    edits = list(edits or [])
    if scaffold:
        from . import scaffold as scaffold_mod

        edits = scaffold_mod.scaffold_edits(scaffold, engine.store) + edits

    if not edits:
        raise ValueError(
            "transform plan needs content: pass --edits-file <path|-> with the "
            "authored edits, or --scaffold <table> for a staging skeleton"
        )

    parse_notes: list[str] = []
    has_delete = any(e.op is EditOp.DELETE for e in edits)
    has_config = any(
        e.kind in (EditKind.PROJECT_YML, EditKind.PROFILES_YML) for e in edits
    )
    if has_delete or has_config:
        # The secret-guard runs first, so an inlined credential is never handed
        # to the dbt subprocess.
        from ..dbt_project import load as load_project
        from .build import shadow_parse
        from .validate import assert_profiles_safe

        project = engine.project_dir()
        view = load_project(project)
        assert_profiles_safe(view, edits)
        # Refuse an orphaning delete with a precise, dbt-independent message
        # before the subprocess runs (which would otherwise report the same
        # danglers as a lower-level parse error). This is the always-available
        # hard gate; the parse below is the authoritative backstop.
        if has_delete:
            plans_mod.validate_deletions(view, edits)
        parse_result = shadow_parse(project, edits, target=engine.config.dbt_target)
        if not parse_result["available"]:
            parse_notes.append(parse_result["reason"])
        elif not parse_result["success"]:
            raise DbtParseError(
                _failure_message("dbt parse failed", parse_result["messages"]),
                warnings=parse_result["messages"][1:],
            )
        elif parse_result["messages"]:
            parse_notes = [f"dbt: {m}" for m in parse_result["messages"]]

    result = _make_plan(engine, intent, edits)
    result.warnings.extend(parse_notes)
    return result


def cmd_plan(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = plan(
            engine,
            getattr(args, "argument", None) or "",
            edits=_edits_from_payload(getattr(args, "edits_file", None)),
            scaffold=getattr(args, "scaffold", None),
        )
        return to_envelope(result, hints=_plan_hint(result))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)


def macro(engine: DexEngine, name: str | None = None) -> MacroListResult | MacroResult:
    """List the shipped macros, or plan scaffolding one into the project."""

    from ..dbt_project import load as load_project
    from . import scaffold as scaffold_mod

    if not name:
        return MacroListResult(
            macros=[
                {"name": macro_name, "description": description}
                for macro_name, description in sorted(scaffold_mod.MACRO_ASSETS.items())
            ]
        )

    project = engine.project_dir()
    view = load_project(project)
    edit = scaffold_mod.macro_edit(name, view.macro_paths[0])

    warnings: list[str] = []
    existing = view.files.get(edit.path)
    if existing is not None and existing.content == edit.new_content:
        return MacroResult(
            macro=name,
            path=edit.path,
            up_to_date=True,
            warnings=[f"{edit.path} already matches the shipped version"],
        )
    if existing is not None:
        warnings.append(
            f"{edit.path} differs from the shipped version (customized or "
            "stale); the diff below reconciles them, and applying it "
            "overwrites the project's copy"
        )

    # The authoritative gate, same layering as semantic plans: dbt's own
    # parser sees the macro in a shadow copy of the project, which also
    # catches a name collision with a macro defined elsewhere in the project.
    from .build import shadow_parse

    parse_result = shadow_parse(project, [edit], target=engine.config.dbt_target)
    if not parse_result["available"]:
        warnings.append(parse_result["reason"])
    elif not parse_result["success"]:
        raise DbtParseError(
            _failure_message("dbt parse failed", parse_result["messages"]),
            warnings=parse_result["messages"][1:],
        )

    planned = _make_plan(engine, f"scaffold macro {name}", [edit])
    planned.warnings.extend(warnings)
    return MacroResult(**planned.model_dump(), macro=name, path=edit.path)


def cmd_macro(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = macro(engine, getattr(args, "argument", None))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    if isinstance(result, MacroListResult):
        return to_envelope(
            result, hints={"next": "scaffold one with `transform macro <name>`"}
        )
    if result.up_to_date:
        return to_envelope(result)
    return to_envelope(result, hints=_plan_hint(result))


def apply(engine: DexEngine, plan_id: str | None = None) -> ApplyResult:
    """Write a stored plan's edits into the dbt project.

    A file that changed after the plan was made is a conflict, not an overwrite:
    human edits are authoritative, so the result comes back with conflicts and
    nothing written until the caller confirms deliberately.
    """

    store = engine.require_full_store("applying a plan")
    if not plan_id:
        # No id means the latest unapplied plan of any kind: apply does not
        # dispatch on kind, a plan is a plan (a semantic plan applies the same
        # way a model plan does).
        latest = store.latest_plan(None)
        if latest is None:
            raise ValueError(
                "no unapplied plan found; run `transform plan` or `semantic "
                "define|update|plan` first, or pass a plan id"
            )
        plan_id = latest.plan_id

    repo_root = engine.require_repo_root("applying a plan to the dbt project")
    outcome: PlanApplyResult = plans_mod.apply(
        plan_id, repo_root, store=store, confirmed=engine.confirmed
    )
    conflicts = [c.model_dump(mode="json") for c in outcome.conflicts]
    if outcome.conflicts and not outcome.written:
        from ..results import ConfirmationRequest

        return ApplyResult(
            plan_id=plan_id,
            conflicts=conflicts,
            diffs=outcome.diffs,
            # Not a spend confirmation but the same shape: a priced-out ask the
            # caller has to accept before anything is overwritten.
            pending_confirmation=ConfirmationRequest(
                data={
                    "hint": (
                        "these files changed after the plan was made (human edits "
                        "are authoritative); re-plan against current state, or "
                        "re-run with --confirm to overwrite deliberately"
                    )
                }
            ),
        )
    return ApplyResult(
        plan_id=plan_id,
        written=outcome.written,
        conflicts_overridden=[c.path for c in outcome.conflicts],
        diffs=outcome.diffs,
    )


def cmd_apply(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(apply(engine, getattr(args, "argument", None)))
    except ValueError as exc:
        return env.error_for(exc)


def plans(engine: DexEngine) -> PlanListResult:
    """Stored plans (pending and applied), newest first."""

    return PlanListResult(
        plans=[
            {
                "plan_id": p.plan_id,
                "intent": p.intent,
                "kinds": sorted({e.kind.value for e in p.edits}),
                "paths": [e.path for e in p.edits],
                "created_at": p.created_at,
                "applied_at": p.applied_at,
                "pending": p.applied_at is None,
            }
            for p in engine.require_full_store("listing plans").list_plans()
        ]
    )


def cmd_plans(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(plans(engine))


def build(
    engine: DexEngine, *, target: str | None = None, select: str | None = None
) -> BuildResult:
    """Run ``dbt build`` against the dev target, cost-surfaced first.

    The order mirrors the documented gate: the dev-target check runs first (free,
    and it must refuse a broken or undeployable target before anyone weighs a
    budget), then a billed build is priced by a free ``dbt compile`` dry-run, then
    the same confirm handshake ``explore`` uses gates the spend.

    Pricing is best-effort by design: dex discovers its own connection while dbt
    reads ``profiles.yml``, and the two can legitimately differ, so a connection
    dex cannot open (or a compile that fails) must never break a build dbt could
    have run. It degrades to no-estimate with a note; the ceiling and the
    server-side per-statement cap still bind.
    """

    from ..connect import paradigm_for
    from ..envelope import Paradigm
    from ..guards.cost_guard import (
        ConfirmationRequiredError,
        no_session_ceiling_warning,
    )

    # `from .build import ...` rather than `from . import build`: the package
    # re-exports the build *function* under the same name as the module, and the
    # submodule-path form resolves the module unambiguously.
    from . import dev_target
    from .build import build as run_build

    store = engine.store
    config = engine.config
    repo_root = engine.require_repo_root("building the dbt project")
    target = target or config.dbt_target or "dev"
    ceiling = engine.budget if engine.budget is not None else config.budget.ceiling
    connector = engine.connector or config.connector
    paradigm = paradigm_for(connector)

    project = engine.project_dir()
    # A --connector flag governs this build, so the drift check must compare the
    # profile against that connector's config block, not the committed default.
    effective = config.model_copy(update={"connector": connector})
    dev_check = lambda: dev_target.check(  # noqa: E731
        project,
        target,
        effective,
        repo_root,
        store=store,
        connection=engine.connection,
    )

    # Free/local (DuckDB): nothing bills, so there is nothing to price. The
    # engine runs the dev-target check and the confirm handshake itself.
    if paradigm is Paradigm.FREE_LOCAL:
        try:
            summary, cost = run_build(
                project,
                target=target,
                configured_target=config.dbt_target,
                select=select,
                ceiling=ceiling,
                confirmed=engine.confirmed,
                paradigm=paradigm,
                dev_target_check=dev_check,
            )
        except ConfirmationRequiredError as exc:
            exc.request = _build_confirmation(target, exc.cost)
            raise
        return _shape_build_result(summary, cost, paradigm, connector, store)

    dev_warnings = dev_check()
    estimate, per_node, price_notes, adapter = _price_build(
        engine, project, target, select
    )
    if estimate is not None:
        command_args.billed_handshake(
            "transform build",
            adapter,
            estimate,
            per_table=per_node or None,
            notes=price_notes or None,
        )
    try:
        summary, cost = run_build(
            project,
            target=target,
            configured_target=config.dbt_target,
            select=select,
            ceiling=ceiling,
            confirmed=engine.confirmed,
            paradigm=paradigm,
            estimate=estimate,
            # The dev-target check already ran above; passing None keeps the
            # engine from opening its own connection a second time.
            dev_target_check=None,
        )
    except ConfirmationRequiredError as exc:
        # Reached only when pricing degraded to no estimate; the note explains
        # why the confirm ask carries no number. `billed_handshake` never ran on
        # this path, so the guard's own warnings have to be raised here instead.
        exc.request = _build_confirmation(
            target,
            exc.cost,
            notes=[
                *price_notes,
                *no_session_ceiling_warning(paradigm, config.budget.session_ceiling),
            ],
        )
        raise

    return _shape_build_result(
        summary,
        cost,
        paradigm,
        connector,
        store,
        extra_notes=[*price_notes, *dev_warnings],
        session_ceiling=config.budget.session_ceiling,
    )


def cmd_build(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(
            build(
                engine,
                target=getattr(args, "target", None),
                select=getattr(args, "select", None),
            )
        )
    except BuildFailedError as exc:
        # A failed build still bills for the statements dbt ran before it
        # stopped, and a caller sizing the re-run needs that number more than a
        # successful one does. `to_envelope` is what normally lifts `spend` into
        # `data`, and this path does not go through it.
        data = exc.result.data()
        if exc.result.spend is not None:
            data["spend"] = exc.result.spend
        return env.error_for(
            exc,
            data=data,
            cost=exc.result.cost,
            warnings=exc.result.warnings,
        )


def deps(engine: DexEngine) -> DepsResult:
    """Install the project's dbt package dependencies. Writes only
    ``dbt_packages/``, never the warehouse, so it needs no handshake."""

    from .build import deps as run_deps
    from .build import has_package_spec

    project = engine.project_dir()
    if not has_package_spec(project):
        return DepsResult(
            ran=False,
            reason="no packages.yml (or dependencies.yml with packages) in the project",
        )
    # An explicit invocation is a refresh: run even when dbt_packages/ exists.
    summary = run_deps(project)
    messages = summary.pop("messages", [])
    if summary["success"]:
        return DepsResult(ran=True, summary=summary, warnings=messages)
    raise BuildFailedError(
        _failure_message("dbt deps failed", messages),
        result=BuildResult(
            success=False,
            summary={"ran": True, **summary},
            warnings=messages[1:] if messages else [],
        ),
    )


def cmd_deps(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(deps(engine))
    except BuildFailedError as exc:
        return env.error_for(exc, data=exc.result.data(), warnings=exc.result.warnings)


def semantic_define(
    engine: DexEngine, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
) -> PlanResult:
    """Author new semantic definitions (entities, dimensions, measures, metrics).

    Refuses a name the project already defines, so an accidental redefinition is
    caught rather than applied; use :func:`semantic_update` to evolve one.
    """

    return _semantic_plan(engine, intent, edits, mode="define", no_parse=no_parse)


def semantic_update(
    engine: DexEngine, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
) -> PlanResult:
    """Evolve existing semantic definitions.

    The mirror of :func:`semantic_define`: refuses a name the project does not
    already have, so a typo does not silently create a second definition.
    """

    return _semantic_plan(engine, intent, edits, mode="update", no_parse=no_parse)


def semantic_plan(
    engine: DexEngine, intent: str, edits: list[PlanEdit], *, no_parse: bool = False
) -> PlanResult:
    """Mixed-intent semantic authoring: one payload may evolve existing
    definitions and add the new ones they depend on; each name is classified
    as defined or updated instead of the whole payload being refused."""

    return _semantic_plan(engine, intent, edits, mode="plan", no_parse=no_parse)


# Mode to the public function that owns it. The shims dispatch through this
# rather than reaching past it into `_semantic_plan`, so the CLI really does
# consume the same surface a library caller does.
_SEMANTIC_AUTHORING = {
    "define": semantic_define,
    "update": semantic_update,
    "plan": semantic_plan,
}


def cmd_semantic_define(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _semantic_envelope(args, engine, "define")


def cmd_semantic_update(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _semantic_envelope(args, engine, "update")


def cmd_semantic_plan(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _semantic_envelope(args, engine, "plan")


def _semantic_envelope(
    args: argparse.Namespace, engine: DexEngine, mode: str
) -> env.Envelope:
    try:
        result = _SEMANTIC_AUTHORING[mode](
            engine,
            getattr(args, "argument", None) or "",
            _edits_from_payload(
                getattr(args, "edits_file", None), default_kind=EditKind.SEMANTIC_YML
            ),
            no_parse=bool(getattr(args, "no_parse", False)),
        )
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)
    return to_envelope(result, hints=_plan_hint(result))


# --- helpers -----------------------------------------------------------------


class DbtParseError(DexError):
    """dbt's own parser refused the authored edits, so nothing was stored.

    The authoritative gate: dex's checks are precise and dbt's is final, and a
    plan dbt cannot parse would only fail later at build time, further from the
    edit that caused it. ``warnings`` carries dbt's remaining messages.
    """

    def __init__(self, message: str, *, warnings: list[str] | None = None):
        super().__init__(message)
        self.warnings = warnings or []


def _plan_hint(result: PlanResult) -> dict[str, str]:
    return {"next": f"review the diffs, then `transform apply {result.plan_id}`"}


def _record_build_spend(
    store: Store, connector: str, billed: float, paradigm
) -> dict[str, float]:
    """Account a billed dbt build in the spend ledger and report what it cost.

    The paradigm names both units, so a build draws against the same session
    budget as an explore scan and reports spend under the key every other
    command reports it under.

    Returns the summary in :meth:`CostGate.spend_summary`'s shape, because a
    build settles outside any gate: dbt executes the statements, so
    ``record_billed`` never fires and the gate's own total stays zero for the
    run. The day's total is read back from the ledger this write just landed in,
    which is the number the next command's gate will start from.
    """

    from datetime import UTC, datetime

    from ..guards.cost_guard import ledger_field, spend_field, utc_day_start

    field = ledger_field(paradigm)
    store.append_spend_log(
        {
            "at": datetime.now(UTC).isoformat(),
            "connector": connector,
            "command": "transform build",
            field: float(billed),
            "job_id": None,
            "statement_sha256": None,
        }
    )
    return {
        spend_field(paradigm): float(billed),
        "session_spent_today": store.spend_since(
            utc_day_start(), field=field, connector=connector
        ),
    }


def _price_build(engine: DexEngine, project, target: str, select: str | None):
    """Price a billed build upfront with a free ``dbt compile`` dry-run.

    Returns ``(estimate, per_node, notes, adapter)``. ``estimate`` is ``None``
    when pricing could not be produced (no reachable dex connection, a compile
    failure): the build still runs, gated by the ceiling and the server-side cap,
    with a note saying why no number was shown.
    """

    from .build import compile_estimate, needs_deps
    from .build import deps as run_deps

    adapter = None
    try:
        adapter = engine._adapter("transform build")
        # dbt compile refuses to run with declared-but-uninstalled packages, and
        # deps writes only dbt_packages/ (never the warehouse), so installing it
        # here during the free preflight is consistent and idempotent on the build.
        if needs_deps(project):
            run_deps(project)
        estimate, per_node, notes = compile_estimate(
            project, adapter, target=target, select=select
        )
        return estimate, per_node, notes, adapter
    except Exception as exc:
        note = (
            f"could not price this build upfront ({type(exc).__name__}: {exc}); "
            "the budget and the server-side per-statement cap still bind"
        )
        return None, {}, [env.redact(note)], adapter


def _build_confirmation(target: str, cost, notes=()):
    from ..results import ConfirmationRequest

    return ConfirmationRequest(
        cost=cost,
        data={
            "command": "transform build",
            "target": target,
            "hint": "review the cost, then re-run with --confirm (and --budget on "
            "billed connectors)",
        },
        warnings=list(notes),
    )


def _shape_build_result(
    summary: dict,
    cost,
    paradigm,
    connector: str,
    store: Store,
    extra_notes=(),
    session_ceiling: float | None = None,
) -> BuildResult:
    """Shape a finished dbt run per paradigm, and ledger what it actually cost.

    ``extra_notes`` carries anything learned before the run (a degraded-pricing
    note, dev-target warnings); the per-paradigm note explains the server-side cap
    that binds the spend, and actual billed magnitude is recorded to the ledger
    *and* reported on the result, so a host summing settled spend from envelopes
    sees builds rather than silently counting them as free.

    A failed run reports its spend too: dbt bills for the statements it ran
    before it stopped, and a caller sizing the re-run needs that number more
    than a successful one does.
    """

    from ..envelope import Paradigm
    from ..guards.cost_guard import no_session_ceiling_warning

    messages = summary.pop("messages", [])
    notes = [*extra_notes, *summary.pop("notes", [])]
    spend: dict[str, float] | None = None
    if paradigm is Paradigm.BYTES_SCANNED:
        notes = [
            "each statement was capped server-side by the profile's "
            "maximum_bytes_billed (a per-statement cap, not per run)",
            *notes,
        ]
        billed = summary.get("bytes_billed")
        if billed:
            spend = _record_build_spend(store, connector, billed, paradigm)
    elif paradigm is Paradigm.COMPUTE_TIME:
        cap_note = _COMPUTE_TIME_CAP_NOTES.get(
            connector, _DEFAULT_COMPUTE_TIME_CAP_NOTE
        )
        notes = [cap_note, *notes]
        # dbt-snowflake, dbt-databricks, and dbt-redshift report no billing figure;
        # per-node execution time is the honest compute-seconds actual.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        if seconds:
            summary["seconds_billed"] = seconds
            spend = _record_build_spend(store, connector, seconds, paradigm)
    elif paradigm is Paradigm.DB_LOAD:
        notes = [
            "each statement was capped server-side by a statement_timeout set to "
            "the ceiling (injected via PGOPTIONS)",
            *notes,
        ]
        # dbt-postgres reports no billing figure; per-node execution time is the
        # honest database-seconds actual.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        if seconds:
            summary["seconds_billed"] = seconds
            spend = _record_build_spend(store, connector, seconds, paradigm)
    notes = [*notes, *no_session_ceiling_warning(paradigm, session_ceiling)]
    if summary["success"]:
        return BuildResult(
            success=True,
            summary=summary,
            cost=cost,
            spend=spend,
            warnings=[*notes, *messages],
        )
    # Agents triage from `errors` first, so the first real dbt message rides there;
    # the rest stay in warnings.
    deps_info = summary.get("deps")
    prefix = (
        "dbt deps failed"
        if deps_info and not deps_info.get("success", True)
        else "dbt build failed"
    )
    raise BuildFailedError(
        _failure_message(prefix, messages),
        result=BuildResult(
            success=False,
            summary=summary,
            cost=cost,
            spend=spend,
            warnings=[*notes, *(messages[1:] if messages else [])],
        ),
    )


def _failure_message(prefix: str, messages: list[str]) -> str:
    return f"{prefix}: {messages[0]}" if messages else prefix


def _make_plan(engine: DexEngine, intent: str, edits: list[PlanEdit]) -> PlanResult:
    repo_root = engine.require_repo_root("storing a transform plan")
    stored, diffs, warnings = plans_mod.plan(
        intent,
        edits,
        engine.project_dir(),
        repo_root,
        store=engine.require_full_store("storing a semantic plan"),
    )
    return PlanResult(
        plan_id=stored.plan_id,
        intent=stored.intent,
        paths=[e.path for e in stored.edits],
        plan_path=engine.require_full_store("locating a plan").plan_locator(
            stored.plan_id
        ),
        diffs=diffs,
        warnings=warnings,
    )


def _semantic_plan(
    engine: DexEngine,
    intent: str,
    edits: list[PlanEdit],
    *,
    mode: str,
    no_parse: bool = False,
) -> PlanResult:
    if not edits:
        raise ValueError(
            f"semantic {mode} needs content: pass --edits-file <path|-> with the "
            "authored dbt semantic YAML"
        )
    non_semantic = [e.path for e in edits if e.kind is not EditKind.SEMANTIC_YML]
    if non_semantic:
        raise ValueError(
            f"semantic {mode} takes only semantic_yml edits; got other kinds for: "
            + ", ".join(non_semantic)
        )
    deletions = [e.path for e in edits if e.op is EditOp.DELETE]
    if deletions:
        raise ValueError(
            f"semantic {mode} authors content and does not delete; remove the "
            "semantic YAML with `transform plan` instead, for: " + ", ".join(deletions)
        )

    from ..dbt_project import load as load_project

    project = engine.project_dir()
    view = load_project(project)
    parsed_edits = [yaml.safe_load(e.new_content) for e in edits]
    classification = semantic_mod.check_mode(mode, parsed_edits, view)
    semantic_mod.check_references(parsed_edits, view)
    spine_warning = semantic_mod.time_spine_warning(view, parsed_edits)

    # The authoritative gate: a plan that dbt cannot parse is never stored.
    # Skipped when the time-spine warning fires (dbt would refuse to parse for
    # that already-surfaced reason, and authoring the spine comes next), or
    # when the caller opts out.
    parse_warning: str | None = None
    parse_deprecations: list[str] = []
    if no_parse:
        pass
    elif spine_warning:
        parse_warning = "dbt parse skipped until the project has a time spine"
    else:
        from .build import shadow_parse

        parse_result = shadow_parse(project, edits, target=engine.config.dbt_target)
        if not parse_result["available"]:
            parse_warning = parse_result["reason"]
        elif not parse_result["success"]:
            raise DbtParseError(
                _failure_message("dbt parse failed", parse_result["messages"]),
                warnings=parse_result["messages"][1:],
            )
        elif parse_result["messages"]:
            # The parse passed, but dbt logged deprecation notices against this
            # exact YAML: surface them now, at plan time, rather than let the
            # author discover them for the first time at `transform build`
            # (where they also poison the failure-error channel).
            parse_deprecations = [f"dbt: {m}" for m in parse_result["messages"]]

    result = _make_plan(engine, intent, edits)
    result.defined = classification["defined"]
    result.updated = classification["updated"]
    if spine_warning:
        result.warnings.append(spine_warning)
    if parse_warning:
        result.warnings.append(parse_warning)
    result.warnings.extend(parse_deprecations)
    return result


def _edits_from_payload(
    edits_file: str | None, default_kind: EditKind | None = None
) -> list[PlanEdit]:
    """Read the agent-authored edits payload (a file path, or ``-`` for stdin).

    Shape: ``{"edits": [{"path": ..., "kind": ..., "op": ..., "content": ...},
    ...]}``. ``op`` defaults to ``"upsert"`` (create or update): those carry
    ``content``. An ``op`` of ``"delete"`` removes the file and carries no
    ``content``. ``kind`` may be omitted when the command implies it (semantic
    define/update).
    """

    if edits_file is None:
        return []
    raw = sys.stdin.read() if edits_file == "-" else _read_file(edits_file)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"edits payload is not valid JSON: {exc}") from exc
    entries = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError('edits payload must be {"edits": [...]}')

    edits: list[PlanEdit] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(f"edits[{i}] needs at least a path")
        try:
            op = EditOp(entry.get("op") or EditOp.UPSERT.value)
        except ValueError as exc:
            raise ValueError(
                f"edits[{i}] has an unknown op '{entry.get('op')}': one of "
                + ", ".join(o.value for o in EditOp)
            ) from exc
        kind = entry.get("kind") or default_kind
        if kind is None:
            raise ValueError(
                f"edits[{i}] needs a kind: one of "
                + ", ".join(k.value for k in EditKind)
            )
        has_content = "content" in entry
        if op is EditOp.UPSERT and not has_content:
            raise ValueError(f"edits[{i}] is an upsert and needs content")
        if op is EditOp.DELETE and has_content:
            raise ValueError(f"edits[{i}] is a delete and must not carry content")
        edits.append(
            PlanEdit(
                path=entry["path"],
                kind=EditKind(kind),
                op=op,
                new_content=entry.get("content"),
            )
        )
    return edits


def _read_file(path: str) -> str:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise ValueError(f"edits file not found: {path}")
    return p.read_text(encoding="utf-8")
