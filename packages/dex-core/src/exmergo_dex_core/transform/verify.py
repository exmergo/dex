"""The correctness sweep folded onto a build, scoped to the nodes it just ran.

`transform build` reports what dbt reported: which nodes ran and what status
each returned. A caller who reads ``"success": true`` learns that dbt executed
without erroring, which is not the question they asked. Whether the relations it
produced hold the rows they should is a different question, and it is the one
that the expensive defects hide behind: an inner join where a left join was
meant passes every test dbt runs.

`maintain verify` answers that question, and this runs the same detectors over
the nodes of one build rather than over a whole project. The detectors are not
reimplemented here: they live in ``maintain.verify`` and are pure, so this
module is the orchestration and the policy, which is where a build differs from
a standalone sweep:

- **Scope.** The whole project is a different and more expensive question. This
  looks only at what dbt just ran, read out of the run results dbt just wrote.
- **Findings never fail the build.** A build dbt completed is a build that
  completed. Whether a finding should gate anything is a caller's policy, not a
  default, so findings ride the envelope beside the build result and the status
  is unchanged by them.
- **Cost.** The counts a row-population verdict needs are priced with the build
  and drawn against the reservation the build is already holding, so a caller
  confirms one number rather than two.
- **The dev target is the subject.** Every other command treats the namespace
  dbt writes to as off limits, which is why it is refused as a source. This one
  reads it, because the relations dbt just wrote are the whole point, and reads
  it after dbt has written: on a first build it did not exist before, and on a
  rebuild the counts listed before the build are the last build's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..adapters.base import Adapter
    from ..engine import DexEngine
    from ..maintain.drift import DriftFinding
    from ..results import ConfirmationRequest

# What a build's findings are worth reading in one sitting. A build that trips
# this many detectors has one upstream cause, and the tail is the same cause
# restated per model. Stated in a warning whenever it binds, because a truncated
# list that says nothing about being truncated reads as the whole answer.
MAX_FINDINGS = 50

#: The dbt resource type row population is about. Tests and snapshots run in the
#: same build and are covered by the build-status half instead.
_MODEL_PREFIX = "model."


@dataclass
class BuildVerification:
    """What the sweep found, and what its caller still has to settle.

    ``ran`` is a positive statement rather than an absent key. A build that did
    not verify and a build that verified and found nothing are different
    answers, and the second one is the only one that means the models are
    clean; leaving the key off would let the first read as the second.

    ``suppressed`` is the same rule one level down: it names each finding class
    that did not run and why, so an empty ``findings`` from a run where nothing
    could execute is never mistaken for a clean project.
    """

    ran: bool = False
    reason: str | None = None
    findings: list[DriftFinding] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    suppressed: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    offer: ConfirmationRequest | None = None

    def payload(self) -> dict[str, Any]:
        if not self.ran:
            return {"ran": False, "reason": self.reason}
        return {
            "ran": True,
            "scope": self.scope,
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "finding_count": len(self.findings),
            "suppressed": self.suppressed,
        }


def built_models(summary: dict[str, Any]) -> dict[str, str]:
    """The models this build ran, as unique id to label.

    Read from the nodes dbt reported rather than from the manifest, because the
    manifest is the whole project and this is one build's selection. Keyed by
    unique id, which is what scopes the sweep: two versions of one model share
    a name, and a build that ran one of them did not run the other. The label
    is the build summary's own node name, the one the envelope shows.
    """

    from ..dbt_project import manifest_node_label

    built: dict[str, str] = {}
    for node in summary.get("nodes") or []:
        unique_id = str(node.get("unique_id") or "")
        if unique_id.startswith(_MODEL_PREFIX):
            built[unique_id] = str(node.get("name") or manifest_node_label(unique_id))
    return built


def price_verification(
    adapter: Adapter, project_dir: Path, *, scope: set[str]
) -> tuple[float, list[str]]:
    """What the row counts this build will need would cost, priced before it runs.

    Called from the build's own pricing pass, so the caller confirms one number
    covering both phases rather than agreeing to a build and then being asked
    again about the counts that judge it.

    Which relations need a real count is decided from the manifest's declared
    materialization rather than from the catalog, and that is the whole trick:
    at pricing time the relations do not necessarily exist yet, so the catalog
    cannot be asked whether it keeps a count for them. What the materialization
    says is enough, because no warehouse maintains a row count for a view and
    every one of them does for a table.

    Best-effort, like the build estimate it joins. On the first build of a
    project the relations to count are not there to be dry-run priced, so this
    returns zero and a note, and the phase check after the build prices them
    against the confirmed budget instead.
    """

    from ..maintain import verify as verify_mod

    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0, []
    try:
        checks, _, _ = verify_mod.row_population_plan(
            project_dir, adapter.dialect, scope=scope
        )
        countable = _uncounted_by_materialization(project_dir, checks)
        if not countable:
            return 0.0, []
        sql = verify_mod.count_relations_sql(sorted(countable), adapter.dialect)
        return float(estimator(sql)), []
    except Exception as exc:
        from ..envelope import redact

        return 0.0, [
            redact(
                "could not price this build's verification upfront "
                f"({type(exc).__name__}: {exc}); the row counts are priced "
                "again after the build and offered if they do not fit the "
                "confirmed budget"
            )
        ]


def verify_build(
    engine: DexEngine,
    project_dir: Path,
    summary: dict[str, Any],
    *,
    resolve_adapter: Callable[[], Adapter],
) -> BuildVerification:
    """Sweep the nodes this build ran, and report what it could not sweep.

    ``resolve_adapter`` is injected rather than taken from the engine, and that
    is what keeps the spend honest: on a billed connector the build has already
    opened an adapter and its gate is holding this command's reservation, so
    the caller hands that same one back here. Reaching for the engine instead
    would rebuild the gate, and the counts would then be charged against a
    reservation nobody booked. Named for resolving rather than opening because
    this module opens nothing: :meth:`DexEngine._adapter` is the only opener in
    the tree, and that is asserted structurally by reading the source for the
    funnel's own name.

    Never raises except on a cost-guard refusal, which is re-raised untouched:
    a build that has already run must report what it found, so every other
    failure here becomes a suppression reason or a warning. An over-ceiling
    refusal is not a degraded finding class, it is the caller's own budget
    contradiction, and downgrading it to a warning would spend past a number
    they set.
    """

    from ..maintain import drift as drift_mod
    from ..maintain import verify as verify_mod

    built = built_models(summary)
    scope = set(built)
    result = BuildVerification(ran=True, scope=sorted(built.values()))

    # Both halves below need the adapter, and only the first may open it: a
    # second open would rebuild the gate holding this command's reservation.
    opened: list[Adapter] = []

    def adapter_once() -> Adapter:
        if not opened:
            opened.append(resolve_adapter())
        return opened[0]

    findings, notes = verify_mod.build_status_findings(project_dir)
    result.warnings.extend(notes)
    if notes:
        result.suppressed["build_status"] = notes[0]

    # dbt just told us which nodes it built, and it is a better authority on
    # that than the catalog: a node dbt reports as `success` has a relation,
    # whether or not it falls inside the source scope dex normally reads. The
    # no-relation check exists for a project nobody has built recently, which is
    # the one thing this caller can rule out.
    result.suppressed["no_relation"] = (
        "dbt's run results are authoritative for the nodes this build ran"
    )

    reason = _row_population(engine, project_dir, summary, scope, adapter_once, result)
    if reason is not None:
        result.suppressed["row_population"] = reason

    proven = _declared_facts(engine, project_dir, summary, scope, adapter_once, result)
    # A grain this build proved, by its own passing uniqueness test or by the
    # declared-fact count, ranks a fan-out on that model as intended (#841).
    if summary.get("success"):
        proven = {**verify_mod.passed_grain_tests(project_dir), **proven}
    result.findings = verify_mod.damp_fanout(result.findings, proven)

    result.findings = drift_mod.rank_findings(findings + result.findings)
    if len(result.findings) > MAX_FINDINGS:
        dropped = len(result.findings) - MAX_FINDINGS
        result.findings = result.findings[:MAX_FINDINGS]
        result.warnings.append(
            f"{dropped} further finding(s) are not listed: a build reporting "
            f"more than {MAX_FINDINGS} has one upstream cause restated per "
            "model, so fix the ranked ones and re-run"
        )
    return result


def _row_population(
    engine: DexEngine,
    project_dir: Path,
    summary: dict[str, Any],
    scope: set[str],
    resolve_adapter: Callable[[], Adapter],
    result: BuildVerification,
) -> str | None:
    """Row loss and fanout for the models this build ran, or why not.

    Returns the suppression reason, and writes findings, warnings and any
    deferred-count offer onto ``result``. Every step that can fail to run says
    so rather than contributing an empty finding list, because the two are
    indistinguishable to a reader and only one of them means the models were
    checked.
    """

    from ..cache import match_identifier
    from ..errors import DexError
    from ..guards import dialect as dialect_guard
    from ..guards.cost_guard import CostGuardError
    from ..maintain import verify as verify_mod

    if not summary.get("success"):
        return (
            "the build did not complete, so its relations are a mix of this "
            "run's output and the last one's; row counts over that say nothing"
        )
    if not scope:
        return "this build ran no models"
    try:
        dialect_guard.ensure_available()
    except DexError as exc:
        return str(exc)
    try:
        adapter = resolve_adapter()
    except CostGuardError:
        # A budget refusal is not a connection problem, and it is the one thing
        # here that must not become a suppression reason: it is the caller's own
        # ceiling, and swallowing it would let a later step spend past it.
        raise
    except DexError as exc:
        return f"warehouse unreachable: {exc}"

    checks, plan_notes, informational = verify_mod.row_population_plan(
        project_dir, adapter.dialect, scope=scope
    )
    result.notes.extend(informational)
    result.warnings.extend(note for note in plan_notes if note not in informational)
    if not checks:
        return (
            plan_notes[0]
            if plan_notes
            else "no model this build ran could be lined up against a driving parent"
        )

    try:
        live = adapter.list_objects()
    except CostGuardError:
        raise
    except Exception as exc:
        from ..envelope import redact

        return redact(
            "the relations this build wrote could not be listed "
            f"({type(exc).__name__}: {exc})"
        )
    identifiers = [meta.identifier for meta in live]
    if not any(match_identifier(check.relations[0], identifiers) for check in checks):
        return (
            f"the dev target this build wrote to is outside dex's read scope "
            f"(dex sees {len(identifiers)} object(s), none of them the "
            "relations this build produced); --verify reads the configured dev "
            "namespace, so a relation built anywhere else, such as a custom "
            "schema, is not judged"
        )

    wanted = sorted({relation for check in checks for relation in check.relations})
    try:
        measured = verify_mod.relation_counts(
            adapter,
            wanted,
            live,
            timeout_seconds=engine.config.query.timeout_seconds,
            handshake=_count_handshake(adapter),
        )
    except CostGuardError:
        raise
    except Exception as exc:
        return f"the row counts could not be read ({type(exc).__name__}: {exc})"

    findings, finding_notes = verify_mod.row_population_findings(
        checks,
        measured.counts,
        measured.counted,
        measured.absent,
        measured.deferred,
    )
    result.findings.extend(findings)
    result.warnings.extend(measured.notes + finding_notes)
    result.offer = measured.offer
    return None


def _declared_facts(
    engine: DexEngine,
    project_dir: Path,
    summary: dict[str, Any],
    scope: set[str],
    resolve_adapter: Callable[[], Adapter],
    result: BuildVerification,
) -> dict[str, dict]:
    """What the built models' declarations say, checked against what dbt wrote
    (#496): unit, additivity, population, and a declared grain no ``unique``
    test in this build covers.

    Writes findings, warnings, suppressions and any deferred offer onto
    ``result``, and returns the declared grains the count proved unique. The
    statements are gated as a phase of the build, like the row counts, so a
    caller who confirmed the build's estimate (which priced these upfront, see
    :func:`price_declared_facts`) is not asked again.
    """

    from ..errors import DexError
    from ..guards.cost_guard import CostGuardError
    from ..maintain import declared_facts as facts_mod

    def suppress(reason: str, *, grain: bool = True) -> None:
        for finding_class in facts_mod.CLASSES:
            result.suppressed.setdefault(finding_class, reason)
        if grain:
            result.suppressed.setdefault("grain", reason)

    if not summary.get("success"):
        suppress(
            "the build did not complete, so its relations are a mix of this "
            "run's output and the last one's; a declaration checked against "
            "that says nothing"
        )
        return {}
    if not scope:
        suppress("this build ran no models")
        return {}

    models, plan_notes = facts_mod.plan(project_dir, scope=scope)
    result.warnings.extend(plan_notes)
    if plan_notes:
        suppress(plan_notes[0])
        return {}
    grain_models = [m for m in models if facts_mod.grain_untested(m)]
    if not grain_models:
        result.suppressed["grain"] = (
            "no model this build ran declares a grain that a unique test in this "
            "build does not already check"
        )
    if not models:
        for finding_class in facts_mod.CLASSES:
            result.suppressed[finding_class] = facts_mod.nothing_declared(
                finding_class, "this build ran"
            )
        return {}

    try:
        adapter = resolve_adapter()
    except CostGuardError:
        raise
    except DexError as exc:
        suppress(f"warehouse unreachable: {exc}")
        return {}
    try:
        live = adapter.list_objects()
    except CostGuardError:
        raise
    except Exception as exc:
        from ..envelope import redact

        suppress(
            redact(
                "the relations this build wrote could not be listed "
                f"({type(exc).__name__}: {exc})"
            )
        )
        return {}

    try:
        prepared = facts_mod.prepare(
            adapter, models, [meta.identifier for meta in live], check_grain=True
        )
        facts = facts_mod.findings(
            adapter,
            prepared,
            where="this build ran",
            timeout_seconds=engine.config.query.timeout_seconds,
            handshake=_facts_handshake(adapter),
            pii_overrides=_pii_overrides(engine),
            min_rows=engine.config.maintain.grain_min_rows,
        )
    except CostGuardError:
        raise
    except Exception as exc:
        from ..envelope import redact

        suppress(
            redact(
                f"the declared facts could not be checked ({type(exc).__name__}: {exc})"
            )
        )
        return {}

    result.findings.extend(facts.findings)
    result.warnings.extend(facts.notes)
    for finding_class, reason in facts.suppressed.items():
        result.suppressed[finding_class] = reason
    if grain_models and facts.offer is not None:
        result.suppressed["grain"] = "checking it is a scan, offered above"
    if facts.offer is not None and result.offer is None:
        result.offer = facts.offer
    return facts.proven_grains


def _pii_overrides(engine: DexEngine):
    from ..config import pii_override_paths

    return pii_override_paths(engine.config.pii_overrides)


def price_declared_facts(
    adapter: Adapter, project_dir: Path, *, scope: set[str]
) -> tuple[float, list[str]]:
    """What the declared-fact checks this build will need would cost, priced
    before it runs and folded into the build's estimate the way
    :func:`price_verification` folds the row counts, so one confirmed budget
    covers the build and every check that judges it.

    Best-effort for the same reason: on the first build the relations are not
    there to dry-run against, so this returns zero and a note, and the phase
    check after the build prices the statements against the confirmed budget.
    """

    from ..maintain import declared_facts as facts_mod

    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0, []
    try:
        models, _ = facts_mod.plan(project_dir, scope=scope)
        if not models:
            return 0.0, []
        prepared = facts_mod.prepare(adapter, models, None, check_grain=True)
        estimate, _count = facts_mod.cost(adapter, prepared)
        return estimate, []
    except Exception as exc:
        from ..envelope import redact

        return 0.0, [
            redact(
                "could not price this build's declared-fact checks upfront "
                f"({type(exc).__name__}: {exc}); they are priced again after the "
                "build and offered if they do not fit the confirmed budget"
            )
        ]


def _facts_handshake(adapter: Adapter):
    """The declared-fact statements as a phase of the build, exactly as
    :func:`_count_handshake` gates the row counts: extend the reservation the
    command already holds, and offer rather than refuse when they do not fit."""

    from .. import command_args

    def ask(estimate: float, statement_count: int):
        return command_args.phase_handshake(
            "transform build",
            adapter,
            estimate,
            phase="declared_facts",
            per_table_key="(declared facts)",
            extra={"statement_count": statement_count},
            skip=statement_count == 0,
            notes=[
                "the build is complete and its spend is settled; this estimate "
                "buys only the checks of what its models declare"
            ],
            hint=lambda cost, unit, budget: (
                f"{statement_count} aggregate statement(s) check what this build's "
                f"models declare, estimated at {cost:.0f} {unit} beyond what remains "
                "of the confirmed budget. The build itself is done and billed; "
                f"re-run with --verify --confirm --budget {budget} to build and "
                "judge in one pass"
            ),
        )

    return ask


def _count_handshake(adapter: Adapter):
    """Gate the row counts as a phase of the build, not as a command of their own.

    The build has already spent by the time this runs, so pricing the counts as
    a fresh whole-command handshake would rebook the gate and could refuse a
    caller who has already agreed to exactly this spend. A phase check extends
    the reservation the command is holding instead, and asks only when the
    counts would not fit under the confirmed ceiling.

    Note what the phase is measured against: dbt bills outside the gate
    entirely, so the only spend the gate has accumulated by now is dex's own,
    which on this command is nothing. The check therefore asks whether the
    counts alone fit the ceiling, not whether the build plus the counts do.
    That is the right question, because the build is finished and its money is
    spent either way, and it means the ordinary outcome is that nothing is
    asked at all. The upfront fold is what makes the caller's budget big enough
    to have covered both; this is the backstop for the run where it was not,
    and it returns an offer rather than a refusal.
    """

    from .. import command_args

    def ask(estimate: float, relation_count: int):
        return command_args.phase_handshake(
            "transform build",
            adapter,
            estimate,
            phase="row_population",
            per_table_key="(row counts)",
            extra={"relation_count": relation_count},
            skip=relation_count == 0,
            notes=[
                "the build is complete and its spend is settled; this estimate "
                "buys only the row counts that judge it"
            ],
            hint=lambda cost, unit, budget: (
                f"{relation_count} relation(s) this build wrote have no row "
                "count the warehouse maintains, and counting them is a scan "
                f"estimated at {cost:.0f} {unit} beyond what remains of the "
                "confirmed budget. The build itself is done and billed; re-run "
                f"with --verify --confirm --budget {budget} to build and judge "
                "in one pass"
            ),
        )

    return ask


def _uncounted_by_materialization(project_dir: Path, checks: list) -> set[str]:
    """The planned relations no warehouse will keep a row count for.

    A view is the case that matters, and it is dbt's default materialization,
    so on a project nobody has configured otherwise this is every model in it.
    Read from the manifest rather than the catalog because at pricing time the
    relations may not exist yet, which is exactly when an upfront number is
    most useful.
    """

    from ..dbt_project import strip_relation_quoting
    from ..maintain.verify import _manifest_nodes

    nodes = _manifest_nodes(project_dir) or {}
    counted_by_catalog = {"table", "incremental"}
    uncounted: set[str] = set()
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        relation = node.get("relation_name")
        config = node.get("config")
        materialized = (
            str(config.get("materialized", "")).lower()
            if isinstance(config, dict)
            else ""
        )
        if (
            isinstance(relation, str)
            and relation
            and materialized
            and materialized not in counted_by_catalog
        ):
            uncounted.add(strip_relation_quoting(relation).lower())
    wanted = {relation for check in checks for relation in check.relations}
    return uncounted & wanted
