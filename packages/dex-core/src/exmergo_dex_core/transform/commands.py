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
import contextlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .. import command_args
from .. import envelope as env
from ..config import pii_override_paths
from ..edits import ApplyResult as PlanApplyResult
from ..edits import EditOp, SemanticEditTarget
from ..errors import DexError
from ..results import to_envelope
from ..storage import Store, readable_cache
from . import plans as plans_mod

# `DependencyPolicy` alone, at module scope, because it is a default in
# `build`'s signature and therefore evaluated at import. It carries no
# dependency of its own; the rest of the build module stays deferred for
# the reason stated above.
from .build import DependencyPolicy
from .native_semantic import edits_from_payload, plan_hint, read_payload_file
from .plans import EditKind, PlanEdit, PlanError
from .results import (
    ApplyResult,
    BuildResult,
    ClassificationResult,
    DepsResult,
    GroundingResult,
    InitResult,
    MacroListResult,
    MacroResult,
    MutationCoverageResult,
    PlacementResult,
    PlanExportResult,
    PlanListResult,
    PlanResult,
    PreflightResult,
    PropagationResult,
    TestScaffoldResult,
)

if TYPE_CHECKING:
    from ..engine import DexEngine
    from . import semantic as semantic_mod
    from .verify import BuildVerification

# Two of this module's neighbours reach the dialect engine at import (`.semantic`
# for MetricFlow-shaped YAML, `.validate` for SQL), and importing either here
# would put sqlglot behind every verb this module serves. Most of them need it;
# `apply` does not, and `transform apply` is how a native semantic plan is
# written. An install carrying only a semantic reader has no sqlglot, so an
# eager import here would let that install author a plan it could never apply.
# Reached at the point of use instead, which is where the dependency is real.


def _semantic() -> Any:
    from . import semantic

    return semantic


# What actually caps a dbt statement server-side, per compute-time connector:
# a per-connector fact, kept out of the shared build arm so the next connector
# adds an entry instead of nesting a conditional. dex cannot inject a per-build
# cap through any of these dbt adapters.
_COMPUTE_TIME_CAP_NOTES = {
    "clickhouse": (
        "each statement was capped server-side by max_execution_time and "
        "max_bytes_to_read set from the ceiling (injected through the "
        "profile's custom_settings env_var references)"
    ),
    "redshift": (
        "a statement_timeout on the dbt dev user and a workgroup usage "
        "limit are the server-side caps (dex cannot inject one per build)"
    ),
}
_DEFAULT_COMPUTE_TIME_CAP_NOTE = (
    "the warehouse-level statement timeout and auto-suspend are the server-side caps"
)

# The same shape for db-load, and for the same reason. This one is not
# decoration: the note asserts that a specific server-side cap was applied, and
# the mechanism differs per connector, so a shared sentence would claim a cap
# that was never injected on every connector but the one it was written for.
# `_cap_note` refuses to claim anything for a connector with no entry.
_DB_LOAD_CAP_NOTES = {
    "postgres": (
        "each statement was capped server-side by a statement_timeout set to "
        "the ceiling (injected via PGOPTIONS)"
    ),
    "clickhouse": (
        "each statement was capped server-side by max_execution_time and "
        "max_bytes_to_read set from the ceiling (injected through the "
        "profile's custom_settings env_var references)"
    ),
}
_UNCAPPED_BUILD_NOTE = (
    "this build ran without a dex-injected server-side cap: the confirmed "
    "budget bounded the estimate, but nothing bounded a statement that "
    "outran it"
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
    in_place: bool = False,
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
        in_place=in_place,
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
            in_place=bool(getattr(args, "in_place", False)),
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
    attribute_rows: bool | None = None,
) -> PlanResult:
    """Turn authored edits into a stored plan of reviewable diffs.

    ``scaffold`` prepends staging skeletons built from the exploration cache.
    Deletes, project and profile edits, snapshots and seeds are gated by dbt's
    own parser here rather than at build time, because a broken
    ``dbt_project.yml`` breaks everything, an orphaning delete is cheaper to
    catch before it is stored, and a snapshot's config and a seed's CSV are
    shapes dbt's parser knows and a regex only approximates.

    ``attribute_rows`` controls the row-population report (see
    :mod:`.row_attribution`), which runs after the plan is stored so that nothing
    it finds, and no way it fails, can stop a plan from existing.
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
    # dbt's own parser is a far better gate than a regex on the two kinds whose
    # shape it fully owns: it resolves a snapshot's config against the project
    # and reads a seed's CSV the way it will at build time. It is the
    # authoritative check behind the structural one in `validate`, and it
    # degrades to a warning where dbt is not installed.
    parser_owned = any(
        e.kind
        in (
            EditKind.PROJECT_YML,
            EditKind.PROFILES_YML,
            EditKind.SNAPSHOT_SQL,
            EditKind.SEED_CSV,
        )
        for e in edits
    )
    if has_delete or parser_owned:
        # The secret-guard runs first, so an inlined credential is never handed
        # to the dbt subprocess.
        from ..dbt_project import load as load_project
        from .build import shadow_parse
        from .validate import assert_profiles_safe

        project = engine.project_dir()
        view = load_project(project)
        assert_profiles_safe(view, edits)
        # dex's own refusals run before the subprocess, for the same reason the
        # secret-guard above does: the parse copies the project with these edits
        # written into it, so a seed refused for carrying personal data must not
        # reach disk or a subprocess first, and dbt's message for a misfiled
        # snapshot ("Encountered unknown tag 'snapshot'") must not stand in for
        # the one that names the fix. Warnings are dropped here on purpose;
        # `plans.plan` produces them again and is what the caller sees.
        overrides = pii_override_paths(engine.config.pii_overrides)
        cache = readable_cache(engine.store) if _has_seed(edits) else None
        for edit in edits:
            plans_mod.admit_edit(
                edit,
                view,
                project,
                cache=cache if edit.kind is EditKind.SEED_CSV else None,
                pii_overrides=overrides,
            )
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
    _attribute_rows(engine, result, edits, requested=attribute_rows)
    return result


def _attribute_rows(
    engine: DexEngine,
    result: PlanResult,
    edits: list[PlanEdit],
    *,
    requested: bool | None,
) -> None:
    """Fold the row-population report onto a plan that is already stored.

    Deliberately total: an edit that cannot move rows, a project dex cannot read,
    a warehouse it cannot reach, all return quietly, because the plan is the
    command's product and this is commentary on it. The one thing that does
    propagate is the priced ask on a metered connector, which rides back on the
    result beside the plan rather than replacing it.

    The broad ``except`` is the backstop behind that: the plan is stored by the
    time this runs, so a defect in the analysis must not turn a successful plan
    into an error envelope the caller cannot read a plan id out of. It names the
    exception type rather than swallowing it, so a bug still surfaces.

    A cost-guard refusal is deliberately **not** caught. An over-ceiling estimate
    and a missing ceiling are the two things confirmation cannot override, and a
    backstop that downgraded either to a warning would leave the guard reporting
    a number that did not bind, which is the failure mode the whole gate exists
    to prevent.
    """

    from ..guards.cost_guard import CostGuardError
    from .row_attribution import attribute

    try:
        outcome = attribute(engine, edits, requested=requested)
    except CostGuardError:
        raise
    except Exception as exc:
        result.warnings.append(
            "could not analyse this edit's effect on the row population "
            f"({type(exc).__name__}: {exc}); the plan itself is unaffected"
        )
        return
    result.warnings.extend(outcome.warnings)
    if not outcome:
        return

    result.row_attribution = [model.model_dump(mode="json") for model in outcome.models]
    if outcome.pending is not None:
        result.pending_confirmation = outcome.pending
    if outcome.adapter is not None:
        command_args.stamp_spend(result, outcome.adapter)


def cmd_plan(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = plan(
            engine,
            getattr(args, "argument", None) or "",
            edits=edits_from_payload(getattr(args, "edits_file", None)),
            scaffold=getattr(args, "scaffold", None),
            attribute_rows=getattr(args, "attribute_rows", None),
        )
        return to_envelope(result, hints=plan_hint(result))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)


def rename(
    engine: DexEngine,
    kind: str,
    old: str,
    new: str,
    *,
    edits_file: str | None = None,
) -> PropagationResult:
    """``transform rename``: every edit the rename needs, as one plan.

    Routed through :func:`plan` rather than straight to the plan store, so a
    generated plan passes exactly the gates a hand-authored one does: containment,
    structural validation, the profiles secret guard, the dangling-reference guard
    on the delete half of a model rename, and dbt's own parser where it owns the
    shape. A generated edit is not more trustworthy than an authored one; it is
    only faster to produce.

    Row attribution is off. A rename changes what a column is called and not which
    rows a model returns, so measuring it would put a warehouse scan and a cost
    handshake in front of a change that is free and repo-only.
    """

    return _propagate(engine, kind, old, new, edits_file=edits_file)


def remove(
    engine: DexEngine, kind: str, name: str, *, edits_file: str | None = None
) -> PropagationResult:
    """``transform remove``: the definition removed, and the reads verified gone.

    dex authors the removal of the *definition* and refuses while any read of it
    survives, naming each. It never rewrites a read: `{% if var('flag') %}` can be
    dropped or unguarded and only the caller knows which, and `{{ var('x') }}` in
    an expression has no value dex may invent.

    ``edits_file`` is how the caller supplies those read edits. They are validated
    and stored in this same plan, so the removal stays atomic without dex guessing
    at semantics.
    """

    return _propagate(engine, kind, name, None, edits_file=edits_file)


def _propagate(
    engine: DexEngine,
    kind: str,
    old: str,
    new: str | None,
    *,
    edits_file: str | None,
) -> PropagationResult:
    from ..dbt_project import load as load_project
    from .propagate import propagate

    project = engine.project_dir()
    outcome = propagate(
        load_project(project),
        project,
        kind,
        old,
        new,
        extra_edits=edits_from_payload(edits_file),
    )
    planned = plan(engine, outcome.intent, edits=outcome.edits, attribute_rows=False)
    return PropagationResult(
        change=f"{old} -> {new}" if new else f"{old} removed",
        kind=kind,
        sites=outcome.sites,
        plan_id=planned.plan_id,
        intent=planned.intent,
        paths=planned.paths,
        plan_path=planned.plan_path,
        diffs=planned.diffs,
        warnings=planned.warnings,
        notes=outcome.notes,
    )


def cmd_rename(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = rename(
            engine,
            args.kind,
            args.old,
            args.new,
            edits_file=getattr(args, "edits_file", None),
        )
        return to_envelope(result, hints=plan_hint(result))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)


def cmd_remove(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = remove(
            engine, args.kind, args.name, edits_file=getattr(args, "edits_file", None)
        )
        return to_envelope(result, hints=plan_hint(result))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)


def place(
    engine: DexEngine,
    column: str,
    targets: list[str],
    expression: str,
    *,
    explain: bool = False,
) -> PlacementResult:
    """``transform place``: where a shared derived column belongs, and why.

    ``explain`` answers the question and stores nothing, which is what makes the
    proposal something a caller can take up cheaply. The reasoning is identical
    either way: the plan is the same answer with the edits attached.

    Placement is asked of the project format where one is configured, the way
    ``maintain reconcile`` asks it. The ``ref()`` graph and the SQL stay dbt's,
    which is what they are: a per-format graph protocol with one implementation
    would be a seam with nothing on the other side of it.
    """

    from ..adapters.project import PlacingProject as _Placing
    from ..dbt_project import load as load_project
    from .place import place as compute

    project = engine.project_dir()
    editable = engine.editable_project()
    outcome = compute(
        load_project(project),
        project,
        column,
        targets,
        expression,
        placement=editable if isinstance(editable, _Placing) else None,
    )
    common = {
        "column": outcome.column,
        "strategy": outcome.strategy,
        "ancestor": outcome.ancestor,
        "inputs": outcome.inputs,
        "targets": outcome.targets,
        "reasoning": outcome.reasoning,
        "chain": outcome.chain,
        "notes": outcome.notes,
    }
    if explain:
        return PlacementResult(explained=True, **common)
    if not outcome.edits:
        return PlacementResult(
            explained=True,
            **common,
            warnings=[
                "every model in the chain already carries this column, so there "
                "was nothing to plan"
            ],
        )
    planned = plan(engine, outcome.intent, edits=outcome.edits, attribute_rows=False)
    return PlacementResult(
        plan_id=planned.plan_id,
        intent=planned.intent,
        paths=planned.paths,
        plan_path=planned.plan_path,
        diffs=planned.diffs,
        warnings=planned.warnings,
        **common,
    )


def cmd_place(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = place(
            engine,
            args.argument or "",
            _split_targets(getattr(args, "targets", None)),
            getattr(args, "expr", None) or "",
            explain=getattr(args, "explain", False),
        )
        hints = (
            {"next": "apply it with `transform apply`, or argue with the reasoning"}
            if result.plan_id
            else None
        )
        return to_envelope(result, hints=hints)
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)


def _split_targets(raw: list[str] | None) -> list[str]:
    """Target models from a repeatable, comma-splittable flag.

    Both spellings, because `explore semantic query` already accepts both for its
    own lists and a caller should not have to remember which commands take which.
    """

    return [
        name.strip()
        for entry in (raw or [])
        for name in entry.split(",")
        if name.strip()
    ]


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
    return to_envelope(result, hints=plan_hint(result))


def test_scaffold(engine: DexEngine, model_name: str | None) -> TestScaffoldResult:
    """Plan a ``unit_tests:`` skeleton scaffolded from a model's own
    ref()/source() inputs: a ``given`` block per input holding only the
    columns the model actually reads, typed from the exploration cache.

    Never invents the expected output: the ``expect:`` block is a deliberate,
    empty stub that fails until a human fills it in. dbt's own parser is the
    gate (same as ``macro()``), so a malformed fixture is caught before the
    plan is ever stored, not left to `transform build`.
    """

    if not model_name:
        raise ValueError(
            "transform test needs a model: `transform test --scaffold <model>`"
        )

    from ..dbt_project import load as load_project
    from . import test_scaffold as test_scaffold_mod

    project = engine.project_dir()
    view = load_project(project)
    cache = readable_cache(engine.store)
    edits, inputs = test_scaffold_mod.unit_test_scaffold_edits(view, cache, model_name)

    from .build import shadow_parse

    parse_result = shadow_parse(project, edits, target=engine.config.dbt_target)
    warnings: list[str] = []
    if not parse_result["available"]:
        warnings.append(parse_result["reason"])
    elif not parse_result["success"]:
        raise DbtParseError(
            _failure_message("dbt parse failed", parse_result["messages"]),
            warnings=parse_result["messages"][1:],
        )

    planned = _make_plan(engine, f"scaffold unit test for {model_name}", edits)
    planned.warnings.extend(warnings)
    planned.warnings.append(
        "expect: is a stub with no rows; this unit test fails until you fill "
        "in the model's actual expected output for the given fixtures above"
    )
    return TestScaffoldResult(**planned.model_dump(), model=model_name, inputs=inputs)


def test_mutations(
    engine: DexEngine,
    model: str | None,
    *,
    max_mutants: int | None = None,
    target: str | None = None,
) -> MutationCoverageResult:
    """Plant standard analytics defects in a model and report which tests miss them.

    The question this answers is the one a passing suite cannot: whether the
    tests would notice if the model were wrong. Each mutant is one defect, built
    in a throwaway copy as an ephemeral model so nothing is materialized, and run
    through the model's own tests.

    The order mirrors `transform build`, and for the same reasons: the free
    refusals come first, so a model dex cannot mutate costs nothing to find out
    about; the dev-target check runs next, because a broken target makes every
    run impossible and the caller should learn that before weighing a budget;
    then the whole batch is priced and confirmed once. N runs behind one
    handshake is the only shape that works here, since a per-mutant ask would
    make the caller answer twenty times for one question.
    """

    from ..adapters import get_dialect
    from ..connect import paradigm_for
    from ..envelope import Paradigm
    from ..guards.cost_guard import skipped_handshake_warning
    from . import dev_target
    from . import mutation as mutation_mod
    from .build import ShadowRun, assert_dev_target

    if not model:
        raise ValueError(
            "transform test needs a model: `transform test --mutate <model>`"
        )
    if max_mutants is not None and max_mutants > mutation_mod.MAX_MUTANTS:
        raise ValueError(
            f"--max-mutants {max_mutants} is above the engine ceiling of "
            f"{mutation_mod.MAX_MUTANTS}; every mutant is a dbt run, so the "
            "ceiling is what keeps this command's cost predictable. Lower it "
            "to narrow the run"
        )
    cap = mutation_mod.MAX_MUTANTS if max_mutants is None else max(0, max_mutants)

    config = engine.config
    store = engine.store
    repo_root = engine.require_repo_root("running mutation coverage")
    target = target or config.dbt_target or "dev"
    assert_dev_target(target, config.dbt_target)
    connector = engine.connector or config.connector
    effective = config.model_copy(update={"connector": connector})
    paradigm = paradigm_for(connector, effective)
    ceiling = engine.budget if engine.budget is not None else config.budget.ceiling
    project = engine.project_dir()

    dev_warnings = dev_target.check(
        project,
        target,
        effective,
        repo_root,
        store=store,
        connection=engine.connection,
    )

    warnings: list[str] = list(dev_warnings)
    adapter = None
    gate = None
    runs = 0
    spend: dict[str, float | None] | None = None
    try:
        with ShadowRun(
            project,
            target=target,
            connector=connector,
            paradigm=paradigm,
            ceiling=ceiling,
        ) as shadow:
            if shadow.strip_run_hooks():
                warnings.append(
                    "this project's on-run-start/on-run-end hooks are not run: "
                    "mutation coverage invokes dbt once per mutant, and a hook "
                    "that grants or audits would otherwise fire once per run"
                )
            node, model_path = _mutable_model(engine, shadow, model, mutation_mod)
            prepared, batch = _plan_mutants(
                shadow, node, model_path, cap, get_dialect(connector), mutation_mod
            )
            warnings.extend(prepared.notes)
            if batch.unparsed:
                warnings.append(
                    f"{batch.unparsed} mutant(s) did not survive a re-read and "
                    "were dropped rather than run"
                )
            if not batch.mutants:
                raise mutation_mod.MutationError(
                    f"'{model}' has no SQL dex knows how to plant a defect in "
                    "(no comparison, filter, join, CASE, division, window frame "
                    "or aggregate), so there is nothing to measure its tests against"
                )

            estimate = None
            if paradigm is not Paradigm.FREE_LOCAL:
                adapter = engine._adapter("transform test")
                estimate, per_mutant, price_notes = _price_mutations(
                    adapter, shadow, model_path, batch, node, mutation_mod
                )
                warnings.extend(price_notes)
                if estimate is not None:
                    command_args.billed_handshake(
                        "transform test",
                        adapter,
                        estimate,
                        per_table=per_mutant or None,
                        notes=price_notes or None,
                    )
                gate = command_args.cost_gate(adapter)

            result = _run_mutants(
                shadow,
                model_path,
                batch,
                model=node["name"],
                paradigm=paradigm,
                connector=connector,
                store=store,
                ceiling=ceiling,
                estimate=estimate,
                mutation_mod=mutation_mod,
            )
            runs, spend, run_warnings = result.pop("_meta")
            warnings.extend(run_warnings)
            # Released before the day's total is read back, exactly as
            # `_shape_build_result` releases before reading. Every run settles
            # while this command still holds its whole-batch reservation, so a
            # total read during the loop counts that reservation on top of what
            # the runs actually billed and overstates the day by the estimate.
            spend = _refresh_session_total(spend, gate, store, paradigm, connector)
    finally:
        if gate is not None:
            gate.settle()

    if paradigm is Paradigm.FREE_LOCAL:
        warnings.extend(skipped_handshake_warning(paradigm, engine.confirmed))

    survivors = result["counts"].get("survived", 0)
    if survivors:
        warnings.append(
            f"{survivors} planted defect(s) survived this model's tests: no test "
            "told the mutant apart, on the dev data or on the unit test fixtures. "
            "They are in data.mutants, each with the test that would catch it"
        )
    if batch.elided_total:
        warnings.append(
            f"{batch.elided_total} further mutant(s) were not run: the cap is "
            f"{batch.cap}, and what it cut is in data.cap.elided, per defect class"
        )

    return MutationCoverageResult(
        model=node["name"],
        target=target,
        baseline=result["baseline"],
        mutants=result["mutants"],
        counts=result["counts"],
        score=result["score"],
        cap={
            "limit": batch.cap,
            "generated": len(batch.mutants),
            "considered": batch.considered,
            "elided": batch.elided,
        },
        runs=runs,
        spend=spend,
        cost=_mutation_cost(paradigm, estimate, ceiling, adapter),
        warnings=warnings,
    )


def _mutation_cost(paradigm, estimate, ceiling, adapter):
    from ..envelope import Cost
    from ..guards.cost_guard import estimate_quality_of

    return Cost(
        paradigm=paradigm,
        estimate=estimate,
        ceiling=ceiling,
        estimate_quality=estimate_quality_of(adapter, estimate, paradigm=paradigm),
    )


def _mutable_model(engine, shadow, model: str, mutation_mod):
    """The node to mutate, or the free refusal saying why this model is not one.

    Every check here is answered from the parse alone, before a connection is
    opened or a mutant is priced, because a model dex cannot mutate should cost
    nothing to ask about.
    """

    from ..dbt_project import load as load_project

    view = load_project(shadow.project)
    original = _model_file(view, model)
    if original is None:
        raise mutation_mod.MutationError(
            f"no model named '{model}' in this project's model paths"
        )
    path, content = original
    # The header is appended rather than prepended: dbt gives a scalar config key
    # to the last `config()` call in a file, so a model declaring its own
    # `materialized` would otherwise win and the mutant would build a relation.
    shadow.write(path, content + "\n" + mutation_mod.EPHEMERAL_HEADER + "\n")
    manifest = shadow.compile(model)

    matches = [
        node
        for uid, node in manifest.get("nodes", {}).items()
        if uid.startswith("model.") and node.get("name") == model
    ]
    if not matches:
        raise mutation_mod.MutationError(f"dbt compiled no model named '{model}'")
    node = matches[0]
    if node.get("language") == "python":
        raise mutation_mod.MutationError(
            f"'{model}' is a Python model; mutation coverage plants defects in SQL"
        )
    if node.get("package_name") != manifest.get("metadata", {}).get("project_name"):
        raise mutation_mod.MutationError(
            f"'{model}' belongs to an installed package rather than this project, "
            "and dex does not mutate a package's own models"
        )
    if (node.get("config") or {}).get("sql_header"):
        raise mutation_mod.MutationError(
            f"'{model}' declares a sql_header, which only a materialization emits; "
            "an ephemeral mutant would run without it and the tests would fail for "
            "that reason rather than for the defect"
        )
    if (node.get("config") or {}).get("materialized") != "ephemeral":
        # The override is what keeps every mutant from writing a relation, so a
        # run must not proceed on a project where it silently did not apply.
        raise mutation_mod.MutationError(
            f"dex could not make '{model}' ephemeral for the run (it compiled as "
            f"{(node.get('config') or {}).get('materialized')}), and it will not "
            "run mutants that would materialize into your dev target"
        )
    if not _attached_tests(manifest, node):
        # Free, and the more useful answer than "nothing to mutate": with no test
        # to catch anything, every mutant survives by construction and the run
        # would spend N dbt invocations to say so.
        raise mutation_mod.MutationError(
            f"'{model}' has no tests, so there is nothing to measure: every "
            "planted defect would survive by construction. Write one first "
            f"(`transform test --scaffold {model}` scaffolds a unit test), "
            "then measure it"
        )
    return node, path


def _attached_tests(manifest: dict, node: dict) -> list[str]:
    """Every test dbt would run for this model, generic, singular and unit alike.

    Read from the manifest rather than from a run, because it is the free answer
    and it is what makes "this model has no tests" a refusal instead of N dbt
    invocations that all report a survivor.
    """

    unique_id = node.get("unique_id")
    attached = [
        uid
        for uid, other in manifest.get("nodes", {}).items()
        if other.get("resource_type") == "test"
        and (
            other.get("attached_node") == unique_id
            or unique_id in (other.get("depends_on") or {}).get("nodes", [])
        )
    ]
    attached += [
        uid
        for uid, unit in (manifest.get("unit_tests") or {}).items()
        if unique_id in (unit.get("depends_on") or {}).get("nodes", [])
        or unit.get("model") == node.get("name")
    ]
    return attached


def _model_file(view, model: str):
    """The model's path and content, read off the project view."""

    for path, file in view.files.items():
        if not path.endswith(".sql"):
            continue
        if Path(path).stem == model and any(
            path.startswith(str(Path(root))) for root in view.model_paths
        ):
            return path, file.content
    return None


def _plan_mutants(shadow, node, model_path, cap, dialect, mutation_mod):
    """The compiled model, references restored, and the defects to plant in it."""

    manifest = shadow.manifest()
    parents = []
    for uid in node.get("depends_on", {}).get("nodes", []):
        parent = manifest.get("nodes", {}).get(uid) or manifest.get("sources", {}).get(
            uid
        )
        if parent is None:
            continue
        ephemeral = (parent.get("config") or {}).get("materialized") == "ephemeral"
        rendered = (
            f"{mutation_mod.DBT_CTE_PREFIX}{parent['name']}"
            if ephemeral
            else parent.get("relation_name")
        )
        if not rendered:
            continue
        parents.append(
            mutation_mod.ParentRelation(
                rendered=rendered,
                jinja=_reference_jinja(parent, uid),
                ephemeral=ephemeral,
            )
        )
    prepared = mutation_mod.prepare(
        node.get("compiled_code") or "", dialect=dialect, parents=parents
    )
    return prepared, mutation_mod.enumerate_mutants(prepared, cap=cap)


def _reference_jinja(parent: dict, unique_id: str) -> str:
    """How a dbt file has to name this input for a unit test fixture to bind."""

    if unique_id.startswith("source."):
        return f"{{{{ source('{parent['source_name']}', '{parent['name']}') }}}}"
    if parent.get("version") is not None:
        return f"{{{{ ref('{parent['name']}', v={parent['version']}) }}}}"
    return f"{{{{ ref('{parent['name']}') }}}}"


def _price_mutations(adapter, shadow, model_path, batch, node, mutation_mod):
    """Price the whole batch upfront, per mutant, as one number to confirm.

    Each mutant is priced as the statements the warehouse will actually run,
    which means splicing it into each test's compiled SQL rather than pricing the
    model alone: a mutant that drops a partition predicate scans more than the
    model it came from, and pricing the batch at the baseline's cost would
    under-report it. Under-reporting is the one direction a cost guard must never
    round.
    """

    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return None, {}, ["connector exposes no estimator; the batch is not priced"]

    manifest = shadow.manifest()
    tests = [
        node.get("compiled_code")
        for node in manifest.get("nodes", {}).values()
        if node.get("resource_type") == "test" and node.get("compiled_code")
    ]
    notes: list[str] = []
    if not tests:
        notes.append(
            "only unit tests read this model, and a unit test runs against "
            "fixtures rather than the warehouse, so the batch prices at zero"
        )

    def price(body: str) -> float:
        total = 0.0
        for test_sql in tests:
            spliced = mutation_mod.inline_into_test(
                test_sql, model_name=node["name"], body=body, dialect=adapter.dialect
            )
            with contextlib.suppress(Exception):
                total += estimator(spliced if spliced is not None else test_sql)
        return total

    per_mutant: dict[str, float] = {}
    with contextlib.suppress(Exception):
        per_mutant["(baseline)"] = price(batch.identity)
    for mutant in batch.mutants:
        with contextlib.suppress(Exception):
            per_mutant[f"{mutant.id} {mutant.operator}"] = price(mutant.body)
    if not per_mutant:
        return None, {}, [*notes, "the batch could not be priced upfront"]
    return sum(per_mutant.values()), per_mutant, notes


def _run_mutants(
    shadow,
    model_path,
    batch,
    *,
    model: str,
    paradigm,
    connector: str,
    store,
    ceiling: float | None,
    estimate: float | None,
    mutation_mod,
):
    """The baseline, then one run per mutant, stopping if the budget runs out."""

    from ..envelope import Paradigm

    warnings: list[str] = []
    runs = 0
    spent = 0.0
    spend: dict[str, float | None] | None = None

    shadow.write(model_path, batch.identity)
    baseline_summary = shadow.test(model)
    runs += 1
    spend, _ = _settle_dbt_spend(
        baseline_summary or {},
        [],
        paradigm=paradigm,
        connector=connector,
        store=store,
        estimate=None,
        command="transform test",
    )
    baseline = _test_statuses(baseline_summary)
    excluded = [
        {"name": name, "status": status, "reason": _exclusion_reason(status)}
        for name, status in sorted(baseline.items())
        if status != "pass"
    ]
    if not any(status == "pass" for status in baseline.values()):
        raise mutation_mod.MutationError(
            f"no test of '{model}' passes against the unmutated model, so there is "
            "nothing that could catch a defect. Build its parents first "
            f"(`transform build --select +{model}`) and fix the failing tests, "
            "then measure them"
        )

    mutants: list[dict] = []
    counts = {"killed": 0, "survived": 0, "rejected": 0, "not_run": 0}
    per_run = (estimate or 0.0) / max(len(batch.mutants) + 1, 1)
    for mutant in batch.mutants:
        if (
            paradigm is not Paradigm.FREE_LOCAL
            and ceiling is not None
            and spent + per_run > ceiling
        ):
            # An unknown settlement counts at its estimate, so the guard never
            # rounds spend down on the way to deciding it can afford another run.
            for remaining in batch.mutants[len(mutants) :]:
                mutants.append({**remaining.payload(), "status": "not_run"})
                counts["not_run"] += 1
            warnings.append(
                f"the confirmed budget covered {runs - 1} of {len(batch.mutants)} "
                "mutants; the rest are reported as not_run. Re-run with a larger "
                "--budget, or narrow the run with --max-mutants"
            )
            break
        shadow.write(model_path, mutant.body)
        summary = shadow.test(model)
        runs += 1
        run_spend, _ = _settle_dbt_spend(
            summary or {},
            [],
            paradigm=paradigm,
            connector=connector,
            store=store,
            estimate=None,
            command="transform test",
        )
        spent += _spent_in_run(run_spend, paradigm, fallback=per_run)
        spend = _merge_spend(spend, run_spend)
        verdict = mutation_mod.classify(baseline, _test_statuses(summary))
        counts[verdict.outcome] = counts.get(verdict.outcome, 0) + 1
        mutants.append(
            {
                **mutant.payload(),
                "status": verdict.outcome,
                "caught_by": verdict.caught_by,
                "warn_only": verdict.warn_only,
            }
        )

    # Survivors first: the reader is deciding which test to write next, and the
    # mutants that were caught are the ones they need to read least.
    order = {"survived": 0, "not_run": 1, "rejected": 2, "killed": 3}
    mutants.sort(key=lambda m: (order.get(m["status"], 9), m["id"]))
    decided = counts["killed"] + counts["survived"]
    return {
        "baseline": {
            "tests": [
                {"name": name, "status": status}
                for name, status in sorted(baseline.items())
            ],
            "excluded": excluded,
        },
        "mutants": mutants,
        "counts": {"generated": len(batch.mutants), **counts},
        "score": (counts["killed"] / decided) if decided else None,
        "_meta": (runs, spend, warnings),
    }


def _refresh_session_total(spend, gate, store, paradigm, connector):
    """Settle the gate, then re-read the day's total the envelope will report.

    A build settles its gate before reading the ledger back for the same reason:
    the reservation is headroom the command is holding, not spend it has
    incurred, so a total read while it stands reports work nobody did. This
    command holds one reservation across N runs, which makes the overstatement
    exactly the whole batch estimate rather than a rounding difference.
    """

    from ..envelope import Paradigm
    from ..guards.cost_guard import ledger_field, utc_day_start

    if gate is None or spend is None or paradigm is Paradigm.FREE_LOCAL:
        return spend
    gate.settle()
    with contextlib.suppress(Exception):
        spend = {
            **spend,
            "session_spent_today": store.spend_since(
                utc_day_start(), field=ledger_field(paradigm), connector=connector
            ),
        }
    return spend


def _test_statuses(summary: dict | None) -> dict[str, str]:
    """Each test node's status, keyed by the name a caller would recognize."""

    if not summary:
        return {}
    return {
        node["name"]: node["status"]
        for node in summary.get("nodes", [])
        if str(node.get("unique_id", "")).startswith(("test.", "unit_test."))
    }


def _exclusion_reason(status: str) -> str:
    if status == "error":
        return (
            "this test could not run against the unmutated model, so it cannot "
            "testify about a mutant either"
        )
    return (
        "this test was already failing before anything was mutated; counting it "
        "as a catch would report the suite as strong because it is broken"
    )


def _spent_in_run(spend: dict | None, paradigm, *, fallback: float) -> float:
    from ..guards.cost_guard import spend_field

    if not spend:
        return fallback
    value = spend.get(spend_field(paradigm))
    # Unknown settlement counts at the estimate rather than at zero.
    return fallback if value is None else float(value)


#: Spend keys that are a reading of the world rather than this command's own
#: contribution to it, so the newest answer replaces the previous one instead of
#: being added to it. Summing `session_spent_today` across nine runs reported a
#: day's total nine times over, which is the one direction a spend report must
#: not err in.
_SPEND_LATEST_WINS = {"session_spent_today", "reserved"}


def _merge_spend(running: dict | None, latest: dict | None) -> dict | None:
    """Accumulate what a sequence of dbt runs billed into one command's spend.

    Three kinds of key and three rules, because a spend payload is not uniformly
    additive. What each run billed sums. A reading of the day's cumulative total
    is already cumulative, so the latest one wins. The two flags are claims about
    the whole command: it settled only if every run did, and its settlement is
    unknown if any run's was, which is why they are combined rather than added.
    Booleans are integers in Python, so an additive rule silently turned
    ``settled: true`` into ``settled: 9``.
    """

    if latest is None:
        return running
    if running is None:
        return dict(latest)
    merged = dict(running)
    for key, value in latest.items():
        previous = merged.get(key)
        if isinstance(value, bool) or isinstance(previous, bool):
            if key == "unknown_settlement":
                merged[key] = bool(previous) or bool(value)
            else:
                merged[key] = bool(previous) and bool(value)
        elif key in _SPEND_LATEST_WINS:
            merged[key] = value if value is not None else previous
        elif isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            merged[key] = previous + value
        elif previous is None:
            merged[key] = value
    return merged


def cmd_test(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    from .mutation import MutationError
    from .test_scaffold import TestScaffoldError

    scaffold = getattr(args, "scaffold", None)
    mutate = getattr(args, "mutate", None)
    try:
        if scaffold and mutate:
            raise ValueError(
                "--scaffold writes a unit test and --mutate measures the tests "
                "that exist; run one, then the other"
            )
        if mutate:
            return to_envelope(
                test_mutations(
                    engine,
                    mutate,
                    max_mutants=getattr(args, "max_mutants", None),
                    target=getattr(args, "target", None),
                )
            )
        result = test_scaffold(engine, scaffold)
        return to_envelope(result, hints=plan_hint(result))
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except (ValueError, TestScaffoldError, MutationError) as exc:
        return env.error_for(exc)


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

    repo_root = engine.require_repo_root("applying a plan")
    stored = store.load_plan(plan_id)
    semantic_layer = None
    if stored.edit_target == "semantic":
        candidate = engine.semantic_catalog_source()
        if isinstance(candidate, SemanticEditTarget):
            semantic_layer = candidate
    outcome: PlanApplyResult = plans_mod.apply(
        plan_id,
        repo_root,
        store=store,
        confirmed=engine.confirmed,
        # A plan is applied through the format it was planned against, so a
        # format that placed an edit into its own keyspace is the one that writes
        # it. `None` here is a format declining the write tier, and falls back to
        # dbt's writer, which is where every plan went before the seam.
        project_format=(
            None if stored.edit_target == "semantic" else engine.editable_project()
        ),
        semantic_layer=semantic_layer,
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
    except (ValueError, PlanError) as exc:
        # `PlanError` is the apply-time containment refusal, which is a refusal
        # the caller can act on rather than a failure, so it gets the same named
        # envelope every other refusal on this path gets.
        return env.error_for(exc)


def _resolve_plan_id(
    engine: DexEngine, plan_id: str | None, what: str
) -> tuple[Store, str]:
    """The store and the plan id, resolving "the latest unapplied plan" once.

    Shared by every verb that takes an optional plan id, so "no id" means the
    same thing on `export`, `ground`, and `classify` as it already does on
    `apply`, rather than each verb inventing its own default.
    """

    store = engine.require_full_store(what)
    if plan_id:
        return store, plan_id
    latest = store.latest_plan(None)
    if latest is None:
        raise ValueError(
            "no unapplied plan found; run `transform plan` or `semantic "
            "define|update|plan` first, or pass a plan id"
        )
    return store, latest.plan_id


def export_plan(engine: DexEngine, plan_id: str | None = None) -> PlanExportResult:
    """A stored plan as a portable document, for a process that has no store.

    Reads the plan store and the project's current files, opens no connection,
    and spends nothing on any connector. The project read is only so a delete can
    be classified from the file it removes; a plan of pure upserts needs it for
    nothing and still gets it, because a project that cannot be read is a fact
    worth failing on here rather than at apply time in another process.
    """

    from .portable import plan_document

    store, plan_id = _resolve_plan_id(engine, plan_id, "exporting a plan")
    repo_root = Path(engine.require_repo_root("exporting a plan"))
    stored = store.load_plan(plan_id)
    project = repo_root / stored.project_dir

    def read_existing(rel_path: str) -> str | None:
        candidate = project / rel_path
        if not candidate.is_file():
            return None
        with contextlib.suppress(OSError, UnicodeDecodeError):
            return candidate.read_text(encoding="utf-8")
        return None

    document = plan_document(stored, read_existing=read_existing)
    return PlanExportResult(
        plan_id=document.plan_id,
        digest=document.digest,
        plan=document.model_dump(mode="json"),
    )


def cmd_export(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(export_plan(engine, getattr(args, "argument", None)))
    except (ValueError, PlanError) as exc:
        return env.error_for(exc)


def apply_document(
    engine: DexEngine,
    document: Any,
    *,
    expect_digest: str | None = None,
) -> ApplyResult:
    """Apply a plan document in this checkout, with no plan store involved.

    The offline half of the lifecycle. It needs a repo root and nothing else: no
    connector, no cache, no snapshot, no dbt, no network. A conflict comes back
    the way it does from `apply`, because a human edit in the applying checkout is
    authoritative there exactly as it is in the authoring one.
    """

    from .portable import apply_document as apply_portable

    repo_root = engine.require_repo_root("applying a plan document")
    semantic_layer = None
    candidate = None
    with contextlib.suppress(Exception):
        candidate = engine.semantic_catalog_source()
    if isinstance(candidate, SemanticEditTarget):
        semantic_layer = candidate

    plan, outcome = apply_portable(
        document,
        repo_root,
        expect_digest=expect_digest,
        confirmed=engine.confirmed,
        semantic_layer=semantic_layer,
    )
    conflicts = [c.model_dump(mode="json") for c in outcome.conflicts]
    if outcome.conflicts and not outcome.written:
        from ..results import ConfirmationRequest

        return ApplyResult(
            plan_id=plan.plan_id,
            conflicts=conflicts,
            diffs=outcome.diffs,
            pending_confirmation=ConfirmationRequest(
                data={
                    "hint": (
                        "these files differ from what the plan was authored "
                        "against (human edits are authoritative); re-plan against "
                        "this checkout, or re-run with --confirm to overwrite "
                        "deliberately"
                    )
                }
            ),
        )
    return ApplyResult(
        plan_id=plan.plan_id,
        written=outcome.written,
        conflicts_overridden=[c.path for c in outcome.conflicts],
        diffs=outcome.diffs,
    )


def cmd_apply_document(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    """``transform apply --plan-file``: the offline half of the lifecycle."""

    try:
        path = getattr(args, "plan_file", None)
        raw = sys.stdin.read() if path == "-" else read_payload_file(path)
        return to_envelope(
            apply_document(
                engine, raw, expect_digest=getattr(args, "expect_digest", None)
            )
        )
    except (ValueError, PlanError) as exc:
        return env.error_for(exc)


def preflight(engine: DexEngine, target: str | None = None) -> PreflightResult:
    """What the warehouse itself will enforce on this project's next build.

    Free and connectionless on every connector: it reads the project's rendered
    profile and the connector's own declarations, and opens nothing. That is why
    it is worth having separately from `connect test`, which proves a credential
    works and says nothing about what binds a dbt subprocess.
    """

    from ..adapters import adapter_declarations
    from ..connect import paradigm_for
    from ..guards.execution import guarded_execution_preflight

    config = engine.config
    connector = engine.connector or config.connector
    effective = config.model_copy(update={"connector": connector})

    # Declarations rather than an open adapter: this command must cost nothing
    # and open nothing, and everything it reads is a declaration rather than a
    # live fact. Building a real adapter would need a credential to report what
    # a rendered profile already says.
    declared = adapter_declarations(connector, paradigm_for(connector, effective))

    project = None
    with contextlib.suppress(Exception):
        project = engine.project_dir()

    report = guarded_execution_preflight(
        declared,
        project_dir=project,
        target=target or config.dbt_target or "dev",
        config=effective,
    )
    return PreflightResult(
        preflight=report.data(), notes=list(report.notes), warnings=[]
    )


def cmd_preflight(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(preflight(engine, getattr(args, "target", None)))
    except (ValueError, PlanError, DexError) as exc:
        return env.error_for(exc)


def ground(engine: DexEngine, plan_id: str | None = None) -> GroundingResult:
    """What a stored plan depends on, and whether resolution finished.

    Repo-only and free on every connector: it reads the project's files and the
    compiled artifacts, opens no connection, and needs no extra beyond whatever
    the project format already needs. The semantic half degrades to a named limit
    rather than an error when the project has no compiled semantic manifest,
    because a plan that touches no metric is fully grounded without one and
    refusing would make the common case pay for the rare one.
    """

    from .grounding import ground_plan
    from .portable import plan_document

    store, plan_id = _resolve_plan_id(engine, plan_id, "grounding a plan")
    repo_root = Path(engine.require_repo_root("grounding a plan"))
    stored = store.load_plan(plan_id)
    project = repo_root / stored.project_dir

    from ..dbt_project import load

    view = load(project)
    catalog = None
    catalog_error = None
    try:
        catalog = engine.semantic_catalog_format().semantic_catalog()
    except Exception as exc:
        catalog_error = (
            "the semantic catalog could not be read, so semantic references in "
            f"this plan were not resolved to the models behind them: {exc}"
        )

    grounding = ground_plan(
        view,
        list(stored.edits),
        project=project,
        config=engine.config,
        catalog=catalog,
        catalog_error=catalog_error,
        plan_digest=plan_document(stored).digest,
    )
    return GroundingResult(plan_id=plan_id, grounding=grounding.data())


def cmd_ground(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(ground(engine, getattr(args, "argument", None)))
    except (ValueError, PlanError) as exc:
        return env.error_for(exc)


def classify(
    engine: DexEngine,
    plan_id: str | None = None,
    *,
    edits: list[PlanEdit] | None = None,
) -> ClassificationResult:
    """What each edit's content contains, read from the content.

    Either a stored plan (by id, or the latest unapplied one) or a payload of
    authored edits, so a caller can classify a change before it is ever planned.
    Repo-only and free on every connector.
    """

    from .classify import classify_edit

    if edits is not None:
        classifications = [classify_edit(edit).data() for edit in edits]
        return ClassificationResult(classifications=classifications)

    store, plan_id = _resolve_plan_id(engine, plan_id, "classifying a plan")
    repo_root = Path(engine.require_repo_root("classifying a plan"))
    stored = store.load_plan(plan_id)
    project = repo_root / stored.project_dir
    classifications = []
    for edit in stored.edits:
        existing = None
        candidate = project / edit.path
        if edit.op is EditOp.DELETE and candidate.is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                existing = candidate.read_text(encoding="utf-8")
        classifications.append(classify_edit(edit, existing_content=existing).data())
    return ClassificationResult(plan_id=plan_id, classifications=classifications)


def cmd_classify(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        payload = getattr(args, "edits_file", None)
        edits = edits_from_payload(payload) if payload else None
        return to_envelope(
            classify(engine, getattr(args, "argument", None), edits=edits)
        )
    except (ValueError, PlanError) as exc:
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
    engine: DexEngine,
    *,
    target: str | None = None,
    select: str | None = None,
    verify: bool = False,
    for_plan: str | None = None,
    for_plan_document: Any = None,
    dependencies: DependencyPolicy = DependencyPolicy.INSTALL,
) -> BuildResult:
    """Run ``dbt build`` against the dev target, cost-surfaced first.

    ``verify`` folds the correctness sweep onto the run, scoped to the nodes it
    touched: dbt reports that it executed, and this reports whether what it
    produced holds the rows it should. Opt-in on every connector, including the
    free ones, so one flag means one thing everywhere. Its findings never
    change the status, and on a billed connector its scan is priced into the
    same estimate the build is priced into, so the caller confirms one number.

    ``for_plan`` and ``for_plan_document`` name the change this build is meant to
    validate, which is what turns dbt's exit code into a verdict: the plan's
    edits say which nodes the change required, and the evidence reports which of
    them actually ran. Without either there is no coverage to report and the
    field is absent rather than empty.

    Two parameters rather than one that takes either, because a plan id and a
    serialized document are both strings and telling them apart by inspection is
    a guess dex should not be making about which one a caller meant. They are
    also what the two ends of the lifecycle actually hold: the process that
    authored the change has a store, and the sandbox that validates it has the
    document and nothing else.

    ``dependencies`` is the sandbox switch. The default installs missing packages
    post-gate as it always has; ``REFUSE`` names them and stops, which is the
    right outcome where there is no network to install from.

    The order mirrors the documented gate: the dev-target check runs first (free,
    and it must refuse a broken or undeployable target before anyone weighs a
    budget), then a billed build is priced by a free ``dbt compile`` dry-run, then
    the same confirm handshake ``explore`` uses gates the spend.

    Pricing is normally best-effort: dex discovers its own connection while dbt
    reads ``profiles.yml``, and the two can legitimately differ, so a connection
    dex cannot open (or a compile that fails) need not break a build dbt could
    have run. ClickHouse Cloud is the exception: its live capacity is required
    to translate and report settled spend, so that fact must be proved before
    dbt runs. After it is cached, a compile failure can still degrade to a
    no-estimate note; the ceiling and server-side per-statement cap bind.
    """

    from ..connect import paradigm_for
    from ..envelope import Paradigm
    from ..guards.cost_guard import (
        ConfirmationRequiredError,
        estimate_quality_of,
        no_session_ceiling_warning,
        skipped_handshake_warning,
        unserialized_ledger_warning,
    )

    # `from .build import ...` rather than `from . import build`: the package
    # re-exports the build *function* under the same name as the module, and the
    # submodule-path form resolves the module unambiguously.
    from . import dev_target
    from .build import assert_dev_target, assert_packages_installed
    from .build import (
        build as run_build,
    )

    store = engine.store
    config = engine.config
    repo_root = engine.require_repo_root("building the dbt project")
    target = target or config.dbt_target or "dev"
    assert_dev_target(target, config.dbt_target)
    ceiling = engine.budget if engine.budget is not None else config.budget.ceiling
    connector = engine.connector or config.connector

    project = engine.project_dir()
    if verify:
        _widen_scope_to_the_dev_target(engine)
    # A --connector flag governs this build, so the drift check must compare the
    # profile against that connector's config block, not the committed default.
    effective = config.model_copy(update={"connector": connector})
    paradigm = paradigm_for(connector, effective)
    dev_check = lambda: dev_target.check(  # noqa: E731
        project,
        target,
        effective,
        repo_root,
        store=store,
        connection=engine.connection,
    )

    # Free/local (DuckDB): nothing bills, so there is nothing to price and no
    # confirmation to ask for (issue #197); the engine runs the dev-target
    # check and gates the ceiling/ready-to-run checks that still apply.
    if paradigm is Paradigm.FREE_LOCAL:
        summary, cost = run_build(
            project,
            target=target,
            configured_target=config.dbt_target,
            select=select,
            ceiling=ceiling,
            confirmed=engine.confirmed,
            paradigm=paradigm,
            connector=connector,
            dev_target_check=dev_check,
            dependencies=dependencies,
            approved_functions=frozenset(config.guards.approved_functions),
        )
        # Before the shaping on both paths, so the two read the same way; here
        # there is no gate to outlive, on the billed path below there is.
        verification = _verify_build(engine, project, summary, verify)
        return _shape_build_result(
            summary,
            cost,
            paradigm,
            connector,
            store,
            extra_notes=skipped_handshake_warning(paradigm, engine.confirmed),
            verification=verification,
            evidence=_build_evidence(
                engine, project, target, select, for_plan, for_plan_document
            ),
        )

    dev_warnings = dev_check()
    # Ahead of pricing, for the reason the dev-target check runs ahead of it: a
    # build that may not install its dependencies and does not have them cannot
    # succeed, so the caller should learn that rather than be handed an estimate
    # to weigh for a run that will not happen. It also keeps the refusal free,
    # which is the whole point in a sandbox: pricing opens a connection and
    # dry-runs every node.
    if dependencies is DependencyPolicy.REFUSE:
        assert_packages_installed(project)

    estimate, per_node, price_notes, adapter = _price_build(
        engine, project, target, select, verify=verify
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
            connector=connector,
            estimate=estimate,
            estimate_quality=estimate_quality_of(adapter, estimate, paradigm=paradigm),
            # The dev-target check already ran above; passing None keeps the
            # engine from opening its own connection a second time.
            dev_target_check=None,
            dependencies=dependencies,
            approved_functions=frozenset(config.guards.approved_functions),
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
                *no_session_ceiling_warning(
                    paradigm,
                    config.budget.session_ceiling,
                    declined=config.budget.session_ceiling_declined,
                ),
                *unserialized_ledger_warning(
                    paradigm,
                    config.budget.session_ceiling,
                    callable(getattr(store, "spend_lock", None)),
                ),
            ],
        )
        raise

    # Between the run and the shaping, deliberately: `_shape_build_result`
    # settles the gate, and a statement issued after that is charged against a
    # reservation that has already been released.
    verification = _verify_build(engine, project, summary, verify, adapter=adapter)
    return _shape_build_result(
        summary,
        cost,
        paradigm,
        connector,
        store,
        extra_notes=[*price_notes, *dev_warnings],
        session_ceiling=config.budget.session_ceiling,
        session_ceiling_declined=config.budget.session_ceiling_declined,
        gate=command_args.cost_gate(adapter) if adapter is not None else None,
        adapter=adapter,
        verification=verification,
        evidence=_build_evidence(
            engine, project, target, select, for_plan, for_plan_document
        ),
    )


def _verify_build(
    engine: DexEngine,
    project,
    summary: dict,
    requested: bool,
    *,
    adapter=None,
) -> BuildVerification:
    """The sweep, or the statement that it did not run.

    Opt-in on every connector, free ones included. Verification on DuckDB costs
    nothing, so making it automatic there was tempting; one flag that means one
    thing on every connector is worth more than saving a caller the flag on one
    of them, and a build whose payload changes shape with the connector is a
    worse contract than a build that always says what it did.

    ``adapter`` is the one the pricing pass already opened, passed through
    rather than re-derived: opening again would settle the gate holding this
    command's reservation and rebuild it, and the counts would then be charged
    against nothing.
    """

    from .verify import BuildVerification, verify_build

    if not requested:
        return BuildVerification(
            ran=False, reason="not requested; re-run with --verify to sweep"
        )
    opener = (
        (lambda: adapter)
        if adapter is not None
        else (lambda: engine._adapter("transform build"))
    )
    return verify_build(engine, Path(project), summary, resolve_adapter=opener)


def _widen_scope_to_the_dev_target(engine: DexEngine) -> None:
    """Let this command read the namespace dbt is about to write into.

    Every other command refuses that namespace as a source, so exploration can
    never mistake a built model for a source table. Verification is the one
    whose subject *is* that output: without this, a metered connector reports
    that it can see nothing to judge, because dbt writes to a namespace the
    source allowlist deliberately excludes.

    Applied to this command's own copy of the config, not through the
    ``--scope`` override. That override may only narrow, by design: a committed
    allowlist is a cost boundary and a flag must not reach past it. This is not
    a flag. It is dex adding the one namespace its own config already names as
    the dev target, for the length of one command, and the spend that namespace
    can attract is still bound by the budget and the handshake like any other.

    Called before anything opens a connection, because the adapter resolves its
    scope once on the first open and caches it for the command. Nothing is
    written back to `.dex/config.yml`, and the widened scope is what the
    envelope's connection block reports, so it is visible rather than silent.
    """

    from .verify import dev_source_scope

    connector = engine.connector or engine.config.connector
    widening = dev_source_scope(engine.config, connector)
    if widening is None:
        return
    field, entries = widening
    target = getattr(engine.config, connector)
    committed = [str(entry) for entry in getattr(target, field)]
    # An empty allowlist already means "everything this connection can see", so
    # narrowing it to the dev namespace would be a widening in name and a
    # narrowing in fact.
    if not committed:
        return
    widened = [*committed, *(e for e in entries if e not in committed)]
    if widened == committed:
        return
    engine.config = engine.config.model_copy(
        update={connector: target.model_copy(update={field: widened})}
    )


def _for_plan_document(args: argparse.Namespace) -> Any:
    """The plan document `--for-plan-file` names, or None."""

    path = getattr(args, "for_plan_file", None)
    if not path:
        return None
    return sys.stdin.read() if path == "-" else read_payload_file(path)


def cmd_build(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(
            build(
                engine,
                target=getattr(args, "target", None),
                select=getattr(args, "select", None),
                verify=getattr(args, "verify", False),
                for_plan=getattr(args, "for_plan", None),
                for_plan_document=_for_plan_document(args),
                dependencies=(
                    DependencyPolicy.REFUSE
                    if getattr(args, "no_install_deps", False)
                    else DependencyPolicy.INSTALL
                ),
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
    engine: DexEngine,
    intent: str,
    edits: list[PlanEdit],
    *,
    definitions: list[semantic_mod.DefinitionEdit] | None = None,
    no_parse: bool = False,
) -> PlanResult:
    """Author new semantic definitions (entities, dimensions, measures, metrics).

    Refuses a name the project already defines, so an accidental redefinition is
    caught rather than applied; use :func:`semantic_update` to evolve one.
    """

    return _semantic_plan(
        engine, intent, edits, mode="define", definitions=definitions, no_parse=no_parse
    )


def semantic_update(
    engine: DexEngine,
    intent: str,
    edits: list[PlanEdit],
    *,
    definitions: list[semantic_mod.DefinitionEdit] | None = None,
    no_parse: bool = False,
) -> PlanResult:
    """Evolve existing semantic definitions.

    The mirror of :func:`semantic_define`: refuses a name the project does not
    already have, so a typo does not silently create a second definition.
    """

    return _semantic_plan(
        engine, intent, edits, mode="update", definitions=definitions, no_parse=no_parse
    )


def semantic_plan(
    engine: DexEngine,
    intent: str,
    edits: list[PlanEdit],
    *,
    definitions: list[semantic_mod.DefinitionEdit] | None = None,
    no_parse: bool = False,
) -> PlanResult:
    """Mixed-intent semantic authoring: one payload may evolve existing
    definitions and add the new ones they depend on; each name is classified
    as defined, updated, or unchanged instead of the whole payload being
    refused."""

    return _semantic_plan(
        engine, intent, edits, mode="plan", definitions=definitions, no_parse=no_parse
    )


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


def _validation_error() -> type[Exception]:
    """`.validate`'s error, reached lazily for the reason above it."""

    from .validate import EditValidationError

    return EditValidationError


def _semantic_envelope(
    args: argparse.Namespace, engine: DexEngine, mode: str
) -> env.Envelope:
    try:
        result = _SEMANTIC_AUTHORING[mode](
            engine,
            getattr(args, "argument", None) or "",
            edits_from_payload(
                getattr(args, "edits_file", None), default_kind=EditKind.SEMANTIC_YML
            ),
            definitions=_definitions_from_payload(
                getattr(args, "definitions_file", None)
            ),
            no_parse=bool(getattr(args, "no_parse", False)),
        )
    except _validation_error() as exc:
        return env.error_for(exc)
    except DbtParseError as exc:
        return env.error_for(exc, warnings=exc.warnings)
    except ValueError as exc:
        return env.error_for(exc)
    return to_envelope(result, hints=plan_hint(result))


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


def _record_build_spend(
    store: Store,
    connector: str,
    billed: float | None,
    paradigm,
    estimate: float | None = None,
    *,
    gate_billed: float = 0.0,
    command: str = "transform build",
) -> dict[str, float | None]:
    """Account a billed dbt run in the spend ledger and report what it cost.

    The paradigm names both units, so a run draws against the same session
    budget as an explore scan and reports spend under the key every other
    command reports it under. ``command`` is what the ledger row is filed under,
    which matters because more than one command now runs dbt: a reader summing
    what `transform build` spent must not be handed another command's rows.

    ``billed`` is ``None`` only where dbt executed statements and reported no
    billing figure for any of them, which is unknown spend rather than none.
    Nothing is ledgered then, because there is no figure to ledger, and the unit
    key still reports ``None`` so the caller reads "unavailable" instead of the
    zero that would under-report it.

    Returns the summary in :meth:`CostGate.spend_summary`'s shape, because a
    build settles outside any gate: dbt executes the statements, so
    ``record_billed`` never fires and the gate's own total stays zero for the
    run. The day's total is read back from the ledger this write just landed in,
    which is the number the next command's gate will start from.

    ``estimate`` is what the handshake priced this build at, written beside what
    it billed exactly as :meth:`CostGate.record_billed` writes it, so a build
    calibrates a later refusal like any other command. It matters more here than
    anywhere else: a build is the largest billed command dex has, so it is both
    the one most likely to be refused over a ceiling and the one whose estimate
    an operator most needs the history of. ``None`` where pricing degraded and
    there was no estimate to record, which :func:`settled_ratios` skips rather
    than counting as a ratio of nothing.
    """

    from ..guards.cost_guard import (
        ledger_field,
        ledger_row,
        spend_field,
        utc_day_start,
    )

    field = ledger_field(paradigm)
    if billed is not None:
        store.append_spend_log(
            # Through the shared builder rather than a dict of its own, which is
            # what keeps a build's row the same row a gate writes. It carries the
            # kind every gate-written settlement carries, because that is what it
            # is, and declares `reservation_id` as null rather than omitting it:
            # a reader joining settlements on that key reads an absent key and a
            # null one as different claims, and only the null one says "this
            # settled outside any reservation".
            ledger_row(
                connector=connector,
                command=command,
                entry="settlement",
                field=field,
                amount=billed,
                estimate=estimate,
            )
        )
    # Read back best-effort, exactly as gate settlement reports the day's total:
    # what this build billed came from dbt and is exact either way, so a ledger
    # that cannot be read must not turn a build that already spent into a failure
    # reporting nothing. `None` is the documented "day's total unavailable".
    session_spent: float | None = None
    with contextlib.suppress(Exception):
        session_spent = store.spend_since(
            utc_day_start(), field=field, connector=connector
        )
    return {
        # `gate_billed` is what statements dex issued through the gate charged
        # (the folded verification), already in the ledger under this command's
        # own settlement, so it is added to what is reported and not appended
        # again. Reporting dbt's figure alone would under-report the command,
        # which is the one direction a cost guard must never round.
        spend_field(paradigm): None
        if billed is None
        else float(billed) + float(gate_billed),
        "session_spent_today": session_spent,
        # The one command where settlement can genuinely be unknown. dbt runs the
        # statements, and dbt-snowflake, dbt-databricks and dbt-redshift report no
        # billing figure at all, so `billed is None` here means statements ran and
        # nobody said what they cost. Stated as its own flag rather than left to be
        # inferred from a null, because a caller summing spend across envelopes has
        # to be able to tell an unknown apart from a zero without knowing which
        # adapters report figures.
        "settled": billed is not None,
        "unknown_settlement": billed is None,
        "reserved": None,
    }


def _settle_dbt_spend(
    summary: dict,
    notes: list[str],
    *,
    paradigm,
    connector: str,
    store: Store,
    estimate: float | None,
    adapter=None,
    gate_billed: float = 0.0,
    command: str = "transform build",
) -> tuple[dict[str, float | None] | None, list[str]]:
    """What one finished dbt run cost, ledgered, with the note that explains it.

    One function per paradigm's accounting rather than three scattered branches,
    because every caller that runs dbt owes the same three things: read the
    actual out of the artifact in that paradigm's unit, append a ledger row, and
    say which server-side cap bound the run. Returns the spend payload and the
    notes with the cap note in front, since the cap is context for the number
    rather than an afterthought to it.

    ``command`` names the ledger row's command, so a caller that is not a build
    settles under its own name and a reader summing one command's spend is not
    handed another's.
    """

    from ..envelope import Paradigm

    spend: dict[str, float | None] | None = None
    if paradigm is Paradigm.BYTES_SCANNED:
        notes = [
            "each statement was capped server-side by the profile's "
            "maximum_bytes_billed (a per-statement cap, not per run)",
            *notes,
        ]
        # Popped, not read: `data.spend` is the one place any command reports
        # what it billed, and a second copy at the top of `data` that only this
        # command carried was worse than no copy at all (issue #276). A caller
        # reading `data.bytes_billed` saw a build's spend and nothing for a
        # `maintain check` that had just scanned 0.89 GB, and an absent key
        # defaulted to zero reads as free.
        billed = summary.pop("bytes_billed", None)
        if billed is None and summary.get("nodes"):
            # Statements ran and not one of them reported a billing figure, so
            # what this run cost is unknown rather than nothing. Reporting the
            # key as null says exactly that; zero would under-report spend, which
            # is the one direction a cost guard must never round. A run that
            # died before executing anything has an empty `nodes` and really did
            # bill nothing, so it settles at zero like any other free run.
            notes = [
                *notes,
                "dbt reported no billing figure for any statement in this run, "
                "so spend is unknown rather than zero and nothing was appended "
                "to the spend ledger",
            ]
            if gate_billed:
                notes = [
                    *notes,
                    "the verification row counts did bill, and are in the "
                    "ledger and in session_spent_today; an unknown build "
                    "figure plus a known one is still unknown, so they are "
                    "not added into the reported spend",
                ]
        else:
            billed = float(billed or 0.0)
        spend = _record_build_spend(
            store,
            connector,
            billed,
            paradigm,
            estimate,
            gate_billed=gate_billed,
            command=command,
        )
    elif paradigm is Paradigm.COMPUTE_TIME:
        cap_note = _COMPUTE_TIME_CAP_NOTES.get(
            connector, _DEFAULT_COMPUTE_TIME_CAP_NOTE
        )
        notes = [cap_note, *notes]
        # dbt-snowflake, dbt-databricks, and dbt-redshift report no billing figure;
        # per-node execution time is the honest compute-seconds actual. It is
        # always known (a run with no nodes burned no seconds), so unlike the
        # bytes path there is no unknown case to distinguish from zero.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        spend = _record_build_spend(
            store,
            connector,
            seconds,
            paradigm,
            estimate,
            gate_billed=gate_billed,
            command=command,
        )
        translate = getattr(adapter, "compute_spend_translation", None)
        if translate is not None:
            # Unconditional, including at zero seconds: the translated keys
            # (compute-unit-hours, USD) are part of this connector's spend shape,
            # and a run that omitted them would be the same present-sometimes key
            # this issue is about, one level down.
            spend.update(translate(seconds))
    elif paradigm is Paradigm.DB_LOAD:
        notes = [_DB_LOAD_CAP_NOTES.get(connector, _UNCAPPED_BUILD_NOTE), *notes]
        # The db-load dbt adapters report no billing figure; per-node execution
        # time is the honest database-seconds actual.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        spend = _record_build_spend(
            store,
            connector,
            seconds,
            paradigm,
            estimate,
            gate_billed=gate_billed,
            command=command,
        )
    return spend, notes


def _price_build(
    engine: DexEngine,
    project,
    target: str,
    select: str | None,
    *,
    verify: bool = False,
):
    """Price a billed build upfront with a free ``dbt compile`` dry-run.

    Returns ``(estimate, per_node, notes, adapter)``. ``estimate`` is ``None``
    when pricing could not be produced (no reachable dex connection, a compile
    failure): the build normally still runs, gated by the ceiling and the
    server-side cap, with a note saying why no number was shown. ClickHouse
    Cloud first proves and caches its mandatory live capacity; failure there is
    re-raised before dbt can spend.
    """

    from .build import _build_env, compile_estimate, needs_deps
    from .build import deps as run_deps

    connector = engine.connector or engine.config.connector
    clickhouse_target = engine.config.clickhouse
    cloud_capacity_required = (
        connector == "clickhouse"
        and clickhouse_target is not None
        and clickhouse_target.deployment == "cloud"
    )
    cloud_capacity_proved = False
    adapter = None
    try:
        adapter = engine._adapter("transform build")
        if cloud_capacity_required:
            # Unlike the other build translations, ClickHouse Cloud's rate is
            # discovered live rather than configured. Prove and cache it before
            # dbt can spend; a compile failure may still degrade safely after
            # this point because settlement can use the cached rate.
            translate = getattr(adapter, "compute_spend_translation", None)
            if translate is None:
                raise RuntimeError(
                    "ClickHouse Cloud adapter cannot prove live compute capacity"
                )
            translate(0.0)
            cloud_capacity_proved = True
        # dbt compile refuses to run with declared-but-uninstalled packages, and
        # deps writes only dbt_packages/ (never the warehouse), so installing it
        # here during the free preflight is consistent and idempotent on the build.
        if needs_deps(project):
            run_deps(project)
        ceiling = (
            engine.budget if engine.budget is not None else engine.config.budget.ceiling
        )
        compile_env = _build_env(connector, adapter.paradigm, ceiling)
        guard_options = (
            {"approved_functions": frozenset(engine.config.guards.approved_functions)}
            if engine.config.guards.approved_functions
            else {}
        )
        estimate, per_node, notes = compile_estimate(
            project,
            adapter,
            target=target,
            select=select,
            env=compile_env,
            **guard_options,
        )
        if verify:
            # The compile above wrote the manifest this reads, so the sweep's
            # scan can be priced here and confirmed with the build rather than
            # asked about again once the build has already spent.
            from .build import compiled_model_names
            from .verify import price_verification

            scan, scan_notes = price_verification(
                adapter,
                Path(project),
                scope=compiled_model_names(Path(project)),
            )
            notes = [*notes, *scan_notes]
            if scan:
                estimate += scan
                per_node = {**per_node, "(row counts)": scan}
        return estimate, per_node, notes, adapter
    except Exception as exc:
        if engine.config.guards.approved_functions:
            # An enabled execution check cannot degrade to an unchecked build.
            raise
        if cloud_capacity_required and not cloud_capacity_proved:
            raise
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


def _build_evidence(
    engine: DexEngine,
    project,
    target: str,
    select: str | None,
    for_plan: str | None,
    for_plan_document: Any = None,
) -> dict | None:
    """Read dbt's artifacts and say what the build established, or nothing.

    ``None`` rather than a partial block wherever the evidence cannot be built,
    because an evidence payload that could not read the run is worse than no
    evidence: it reports an outcome nobody measured. The build's own result is
    unaffected either way.
    """

    from .evidence import build_evidence

    edits = None
    plan_digest = None
    if for_plan:
        from .portable import plan_document

        store = engine.require_full_store("checking what a build validated")
        stored = store.load_plan(for_plan)
        edits = list(stored.edits)
        plan_digest = plan_document(stored).digest
    elif for_plan_document is not None:
        from .portable import verify_plan_document

        document = verify_plan_document(for_plan_document)
        edits = [edit.as_edit() for edit in document.edits]
        plan_digest = document.digest

    source_digest = None
    with contextlib.suppress(Exception):
        from ..dbt_project import load
        from .grounding import source_digest as digest_of

        source_digest = digest_of(load(project))

    try:
        return build_evidence(
            project,
            target=target,
            select=select,
            edits=edits,
            plan_digest=plan_digest,
            source_digest=source_digest,
        ).data()
    except Exception:
        return None


def _shape_build_result(
    summary: dict,
    cost,
    paradigm,
    connector: str,
    store: Store,
    extra_notes=(),
    session_ceiling: float | None = None,
    session_ceiling_declined: bool = False,
    gate=None,
    adapter=None,
    evidence: dict | None = None,
    verification: BuildVerification | None = None,
) -> BuildResult:
    """Shape a finished dbt run per paradigm, and ledger what it actually cost.

    ``extra_notes`` carries anything learned before the run (a degraded-pricing
    note, dev-target warnings); the per-paradigm note explains the server-side cap
    that binds the spend, and actual billed magnitude is recorded to the ledger
    *and* reported on the result, so a host summing settled spend from envelopes
    sees builds rather than silently counting them as free.

    It is reported in exactly one place, ``data.spend``, which every billed
    command reports under. Anything a caller has to look for in a second key on
    some commands and not others is a key whose absence reads as a value, and the
    value it read as here was zero (issue #276). For the same reason a billed
    paradigm always reports the key, whatever it settled at: a spend of zero and
    "this command does not report spend" have to be distinguishable.

    A failed run reports its spend too: dbt bills for the statements it ran
    before it stopped, and a caller sizing the re-run needs that number more
    than a successful one does.

    dbt has returned by the time this runs, so the headroom the handshake booked
    is released here, before the ledger is read back. A build is the longest
    billed command dex has, which makes it both the one whose hold matters most
    to a concurrent command and the one where reporting the day's total without
    releasing first would overstate it by the whole estimate.
    """

    from ..guards.cost_guard import (
        no_session_ceiling_warning,
        unserialized_ledger_warning,
    )

    # Read before the settle releases it: on this command the gate bills for the
    # folded verification and nothing else, because dbt spends outside the gate
    # entirely and the compile that priced the run only dry-ran.
    gate_billed = gate.billed if gate is not None else 0.0
    if gate is not None:
        gate.settle()
    messages = summary.pop("messages", [])
    notes = [*extra_notes, *summary.pop("notes", [])]
    if verification is not None:
        notes = [*notes, *verification.warnings]
        if verification.findings:
            notes = [
                *notes,
                f"verification found {len(verification.findings)} issue(s) in the "
                "nodes this build touched; they are reported in "
                "data.verification.findings and do not change the build's status",
            ]
    # What the handshake priced this build at, ledgered beside what it billed so
    # a later over-ceiling refusal on this connector can say how far the two
    # have run apart. `None` on the degraded-pricing path, where there was no
    # estimate to compare against and inventing one would be worse than none.
    estimate = getattr(cost, "estimate", None)
    spend, notes = _settle_dbt_spend(
        summary,
        notes,
        paradigm=paradigm,
        connector=connector,
        store=store,
        estimate=estimate,
        adapter=adapter,
        gate_billed=gate_billed,
    )
    notes = [
        *notes,
        *no_session_ceiling_warning(
            paradigm, session_ceiling, declined=session_ceiling_declined
        ),
        *unserialized_ledger_warning(
            paradigm,
            session_ceiling,
            callable(getattr(store, "spend_lock", None)),
        ),
    ]
    if summary["success"]:
        result = BuildResult(
            success=True,
            summary=summary,
            evidence=evidence,
            cost=cost,
            spend=spend,
            warnings=[*notes, *messages],
        )
        if verification is not None:
            result.verification = verification.payload()
            # An offer, never a pending confirmation, and the departure from
            # `Result`'s usual rule is deliberate: the build the caller asked
            # for is finished and billed. Reporting `needs_confirmation` would
            # tell a host nothing had run and invite it to pay for the whole
            # build a second time to get the counts.
            result.pending_offer = verification.offer
        return result
    # Agents triage from `errors` first, so the first real dbt message rides there;
    # the rest stay in warnings.
    deps_info = summary.get("deps")
    prefix = (
        "dbt deps failed"
        if deps_info and not deps_info.get("success", True)
        else "dbt build failed"
    )
    failed = BuildResult(
        success=False,
        summary=summary,
        # A failed build is exactly where the evidence is worth most: which
        # node failed, which were skipped behind it, and whether any of the
        # nodes the change required ran at all.
        evidence=evidence,
        cost=cost,
        spend=spend,
        warnings=[*notes, *(messages[1:] if messages else [])],
    )
    if verification is not None:
        # A failed build is when "which node failed, and which were skipped
        # because of it" is worth most, and `cmd_build` builds that envelope by
        # hand from `result.data()`, so the payload reaches it from here.
        failed.verification = verification.payload()
    raise BuildFailedError(_failure_message(prefix, messages), result=failed)


def _failure_message(prefix: str, messages: list[str]) -> str:
    return f"{prefix}: {messages[0]}" if messages else prefix


def _make_plan(engine: DexEngine, intent: str, edits: list[PlanEdit]) -> PlanResult:
    from ..adapters.project import PlacingProject, placement_gap

    repo_root = engine.require_repo_root("storing a transform plan")
    editable = engine.editable_project()
    # The directory these edits are pinned against has to name the same project
    # as the surface they are checked in, or an existing file hashes as absent
    # and the apply that follows conflicts on a file nobody edited. A format
    # declaring a surface answers both from its own view, so the directory is
    # left to `plans.plan` to read there rather than asserted from dbt's here.
    # Everything else predates the seam and keeps dbt's configured pin, which
    # that function's own fallback (discovery from the repo root) would not
    # honor. For dbt the two agree by construction: its view loads the same
    # resolved directory `project_dir()` returns.
    #
    # Asked structurally: the branch turns on the format having a whole keyspace
    # (a view to pin against, and a surface to check in), and a format holding
    # one without the other has neither. `placement_gap` names the member it is
    # missing, on the plan path as well as on reconcile's, because this is the
    # other command that would otherwise fall silently back to dbt's discovery
    # and refuse with "no dbt project found" in a repository that has none.
    places = isinstance(editable, PlacingProject)
    gap = placement_gap(editable)
    try:
        stored, diffs, warnings = plans_mod.plan(
            intent,
            edits,
            None if places else engine.project_dir(),
            repo_root,
            store=engine.require_full_store("storing a semantic plan"),
            # The reviewed columns a human has already cleared, so the seed
            # gate's refusal can be lifted the same documented way every other
            # PII refusal is.
            pii_overrides=pii_override_paths(engine.config.pii_overrides),
            # Which house-convention warnings this project has left on. A style
            # judgment is the one kind of check a repo gets to decline, and the
            # decision belongs in the committed config rather than in a flag,
            # so it holds for every caller rather than for whoever remembered.
            conventions=engine.config.conventions,
            # Agent-authored edits, which is the caller `editing_surface` exists
            # for: there is no placement to compare a path against here, only the
            # surface the format admits to owning. A format declining the write
            # tier is `None` and validates against dbt's surface as before.
            project_format=editable,
        )
    except DexError as exc:
        # A format with a placement gap fell back to dbt's project and dbt's
        # surface, so a refusal here names dbt's paths for an edit the format
        # placed in its own keyspace, which reads as dex refusing the format's
        # own file. The gap is what explains it, and it is the reason there was a
        # fallback at all.
        if gap is None:
            raise
        raise type(exc)(f"{exc}. {gap}") from exc
    return PlanResult(
        plan_id=stored.plan_id,
        intent=stored.intent,
        paths=[e.path for e in stored.edits],
        plan_path=engine.require_full_store("locating a plan").plan_locator(
            stored.plan_id
        ),
        diffs=diffs,
        warnings=[*warnings, gap] if gap else warnings,
    )


def _semantic_plan(
    engine: DexEngine,
    intent: str,
    edits: list[PlanEdit],
    *,
    mode: str,
    definitions: list[semantic_mod.DefinitionEdit] | None = None,
    no_parse: bool = False,
) -> PlanResult:
    from ..dbt_project import load as load_project

    project = engine.project_dir()
    view = load_project(project)

    if definitions and edits:
        raise ValueError(
            f"semantic {mode} takes one payload: whole files (--edits-file) or "
            "definitions (--definitions-file), not both"
        )
    # What the caller named, when they named definitions rather than files. The
    # classification is narrowed to it: a spliced file carries every definition
    # it already held, and those were not part of this change.
    scope: set[semantic_mod.DefinitionKey] | None = None
    # The removals in this payload, carried past the lowering rather than read
    # back out of it: a removal's effect is the absence of something, which the
    # content it produces cannot state.
    removed: list[semantic_mod.DefinitionEdit] = []
    if definitions:
        # Lowered to the whole-file unit before anything else looks at them, so
        # the validation, classification, parse gate, and plan store below stay
        # on one code path regardless of how the caller expressed the change.
        edits = [
            PlanEdit(path=path, kind=EditKind.SEMANTIC_YML, new_content=content)
            for path, content in _semantic().splice_definitions(definitions, view)
        ]
        scope = {(d.kind, d.name) for d in definitions}
        removed = [d for d in definitions if d.op is EditOp.DELETE]

    if not edits:
        raise ValueError(
            f"semantic {mode} needs content: pass --edits-file <path|-> with whole "
            "semantic YAML files, or --definitions-file <path|-> with the "
            "individual definitions to write"
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

    parsed_by_path = [(e.path, yaml.safe_load(e.new_content)) for e in edits]
    parsed_edits = [parsed for _path, parsed in parsed_by_path]
    classification = _semantic().check_mode(
        mode,
        parsed_by_path,
        view,
        scope=scope,
        removed=[(d.kind, d.name) for d in removed],
    )
    # The two reference directions: what this payload's own definitions read,
    # then what the surviving project reads out of what it removes.
    _semantic().check_references(parsed_edits, view)
    _semantic().check_removals(
        removed, [(e.path, e.new_content or "") for e in edits], view
    )
    spine_warning = _semantic().time_spine_warning(view, parsed_edits)

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
    result.unchanged = classification["unchanged"]
    result.removed = classification["removed"]
    # `plans.plan` emits one diff per edit whether or not the content moved, so
    # a no-op is an all-empty diff set rather than an absent one.
    if result.unchanged and all(
        d["additions"] == 0 and d["deletions"] == 0 for d in result.diffs
    ):
        result.warnings.append(
            "every definition in this payload is identical to the project's "
            "current content, so this plan changes nothing"
        )
    if spine_warning:
        result.warnings.append(spine_warning)
    if parse_warning:
        result.warnings.append(parse_warning)
    result.warnings.extend(parse_deprecations)
    return result


def _has_seed(edits: list[PlanEdit]) -> bool:
    """Whether the exploration cache is worth reading for this payload.

    The cache is a stored document and only the seed gate has any use for it, so
    a plan with no seed in it never pays to load one.
    """

    return any(e.kind is EditKind.SEED_CSV and e.op is EditOp.UPSERT for e in edits)


def _definitions_from_payload(
    definitions_file: str | None,
) -> list[semantic_mod.DefinitionEdit]:
    """Read the per-definition payload (a file path, or ``-`` for stdin).

    Shape: ``{"definitions": [{"kind": ..., "path": ..., "content": ...}, ...]}``.
    ``kind`` is ``semantic_model`` or ``metric``; ``content`` is that one
    definition's YAML body; ``path`` may be omitted for a definition the project
    already declares, which is then rewritten where it already lives.

    An entry's ``op`` is ``upsert`` (the default, carrying ``content``) or
    ``delete``, which carries ``name`` instead and removes that one definition
    from the file that declares it. An omitted definition is never removed: only
    a declared delete removes anything.
    """

    if definitions_file is None:
        return []
    raw = (
        sys.stdin.read()
        if definitions_file == "-"
        else read_payload_file(definitions_file)
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"definitions payload is not valid JSON: {exc}") from exc
    entries = payload.get("definitions") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError('definitions payload must be {"definitions": [...]}')
    return _semantic().parse_definition_payload(entries)
