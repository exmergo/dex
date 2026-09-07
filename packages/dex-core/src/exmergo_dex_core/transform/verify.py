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
  reads it, because the relations dbt just wrote are the whole point.
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


def built_models(summary: dict[str, Any]) -> set[str]:
    """The models this build ran, by dbt's own name for each.

    Read from the node ids dbt wrote rather than from the manifest, because the
    manifest is the whole project and this is one build's selection. A model's
    unique id is ``model.<package>.<name>``, so the last segment is already
    the name the project calls it; nothing else in the run results is needed.
    """

    names: set[str] = set()
    for node in summary.get("nodes") or []:
        unique_id = str(node.get("unique_id") or "")
        if unique_id.startswith(_MODEL_PREFIX):
            names.add(unique_id.rsplit(".", 1)[-1])
    return names


def dev_source_scope(config, connector: str) -> tuple[str, list[str]] | None:
    """The allowlist field and entry that let this command read its own output.

    A build's output is normally outside what dex reads: every connector's dev
    namespace is refused as a source, so exploration can never mistake a built
    model for a source table, and `transform init` enforces it. Verification is
    the one command whose subject *is* that output, so it adds exactly that one
    namespace to its own source scope and nothing else.

    Returned as ``(field, [entry])`` rather than applied here, because the entry
    has to be spelled in the allowlist's own vocabulary and the two differ per
    connector: Snowflake and Databricks scope by the container above the schema,
    so a bare dev schema means nothing to them and the qualified pair is what
    goes in.

    ``None`` for DuckDB, which declares no dev namespace at all: its dev target
    is a database file, and the dev-target preflight already refuses a build
    whose profile and config disagree about which file that is, so dex is
    connected to the right one before this question arises.
    """

    def qualified(container: str | None, schema: str | None) -> str | None:
        if not schema:
            return None
        return f"{container}.{schema}" if container else schema

    target = getattr(config, connector, None)
    if target is None:
        return None
    if connector == "bigquery":
        return ("datasets", [target.dev_dataset]) if target.dev_dataset else None
    if connector == "snowflake":
        entry = qualified(target.dev_database, target.dev_schema)
        return ("databases", [entry]) if entry else None
    if connector == "databricks":
        entry = qualified(target.dev_catalog, target.dev_schema)
        return ("catalogs", [entry]) if entry else None
    if connector in {"postgres", "redshift"}:
        return ("schemas", [target.dev_schema]) if target.dev_schema else None
    if connector == "clickhouse":
        return ("databases", [target.dev_database]) if target.dev_database else None
    return None


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
        checks, _ = verify_mod.row_population_plan(
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

    scope = built_models(summary)
    result = BuildVerification(ran=True, scope=sorted(scope))

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

    reason = _row_population(
        engine, project_dir, summary, scope, resolve_adapter, result
    )
    if reason is not None:
        result.suppressed["row_population"] = reason

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

    checks, plan_notes = verify_mod.row_population_plan(
        project_dir, adapter.dialect, scope=scope
    )
    result.warnings.extend(plan_notes)
    if not checks:
        return (
            plan_notes[0]
            if plan_notes
            else "no model this build ran could be lined up against a driving parent"
        )

    live = adapter.list_objects()
    identifiers = [meta.identifier for meta in live]
    if not any(match_identifier(check.relations[0], identifiers) for check in checks):
        return (
            f"the dev target this build wrote to is outside dex's read scope "
            f"(dex sees {len(identifiers)} object(s), none of them the "
            "relations this build produced); name the dev namespace in the "
            "connector's source scope to judge row loss and fanout on it"
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
