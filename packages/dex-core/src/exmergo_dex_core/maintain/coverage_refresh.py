"""``maintain coverage --mutate``: refresh stale and missing strength (#1284).

The read half of strength says which recorded ``transform test --mutate``
scores still describe the project. This is the other half: run the mutation
command again for the models whose score is ``stale`` or ``missing``, in one
bounded batch behind one cost handshake, and record each result exactly as
``transform test --mutate`` records it, so the next read sees it ``fresh``.

Four rules shape a batch.

- **Bounded.** At most :data:`REFRESH_CAP` models are attempted per run, and
  ``--max-models`` can narrow that and never widen it. The rest are counted and
  named, and running the command again continues with them: a refreshed model
  reads ``fresh`` and leaves the candidates, and a model refused for a reason the
  project alone decides is recorded as such and reads ``untestable`` until it
  changes.
- **Ordered.** The selector's own order when one was given; otherwise models
  never measured before those measured against an older tree, upstream before
  downstream, then by name.
- **One handshake.** On a metered connector every attempted model is planned
  and priced first, for free, and the total is confirmed once; the runs then
  draw on that one reservation, each stopping where the batch's budget ends, and
  a model the budget did not reach is reported ``not_run``. Free on DuckDB.
- **Free refusals cost nothing.** A Python model, a model with a
  ``sql_header`` and a versioned model are read off the manifest and listed as
  ``skipped``, never planned and never counted against the cap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: Models attempted per run, and the ceiling ``--max-models`` cannot raise.
REFRESH_CAP = 25
#: Names listed per refresh list before the cut; every list keeps its count.
NAME_CAP = 25


@dataclass
class Candidate:
    """One model whose recorded strength no longer describes it."""

    unique_id: str
    label: str
    name: str
    status: str  # stale or missing
    #: Whether the model has tests of its own, not only downstream ones.
    own_tests: bool = True
    #: Whether its last run was refused for a reason the data decided, so it
    #: is retried after the models a refresh has not tried yet.
    retry: bool = False


@dataclass
class RefreshBatch:
    """The state one batch shares across its ``test_mutations`` calls.

    Duck-typed by :func:`~..transform.commands.test_mutations`: ``pricing`` says
    which phase a call is in, ``add_price`` takes a model's price in the first,
    and ``remaining`` and ``record_spend`` bound and book the second.
    """

    adapter: Any = None
    ceiling: float | None = None
    pricing: bool = False
    prices: dict[str, float] = field(default_factory=dict)
    #: Each model's baseline run price: the statement that runs before any
    #: affordability check inside the model, so the batch checks it first.
    baselines: dict[str, float] = field(default_factory=dict)
    price_notes: list[str] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    spent: float = 0.0
    spend: dict[str, Any] | None = None

    def add_price(self, node: dict, estimate, per_table, notes) -> None:
        from ..guards.cost_guard import UPTIME_LINE

        label = str(node.get("name"))
        if estimate is None:
            self.unpriced.append(label)
            return
        # The warehouse's uptime overhead belongs to the command, not to any one
        # model: the batch handshake adds it once.
        self.prices[label] = max(
            0.0, float(estimate) - float((per_table or {}).get(UPTIME_LINE, 0.0))
        )
        self.baselines[label] = float((per_table or {}).get("(baseline)", 0.0))
        self.price_notes.extend(n for n in notes or [] if n not in self.price_notes)

    def remaining(self) -> float | None:
        if self.ceiling is None:
            return None
        return max(0.0, self.ceiling - self.spent)

    def record_spend(self, spent: float, spend: dict | None) -> None:
        from ..transform.commands import _merge_spend

        self.spent += float(spent or 0.0)
        self.spend = _merge_spend(self.spend, spend)


def skip_reason(node: dict[str, Any]) -> str | None:
    """Why a model is never attempted, read off the manifest for free."""

    if node.get("language") == "python":
        return "python_model"
    if (node.get("config") or {}).get("sql_header"):
        return "sql_header"
    if node.get("version") not in (None, ""):
        # `transform test --mutate` names a model, and every version shares it.
        return "versioned_model"
    return None


def order(
    candidates: list[Candidate],
    manifest: dict[str, Any],
    selector: Iterable[str] | None,
) -> list[Candidate]:
    """The selector's order when given, else missing first, upstream first."""

    wanted = [name.lower() for name in selector or []]
    if wanted:
        position = {name: index for index, name in enumerate(wanted)}

        def rank(candidate: Candidate) -> int:
            names = {candidate.label.lower(), candidate.name.lower()}
            return min(
                (position[n] for n in names if n in position), default=len(wanted)
            )

        # The selector's order, with a model a run already refused after the
        # rest, so it never holds the first slots run after run.
        return sorted(candidates, key=lambda c: (c.retry, rank(c), c.label))

    nodes = manifest.get("nodes") or {}
    depth: dict[str, int] = {}

    def level(unique_id: str, trail: frozenset[str] = frozenset()) -> int:
        if unique_id in depth:
            return depth[unique_id]
        if unique_id in trail:
            return 0
        parents = [
            parent
            for parent in ((nodes.get(unique_id) or {}).get("depends_on") or {}).get(
                "nodes"
            )
            or []
            if parent.startswith("model.")
        ]
        value = 1 + max((level(p, trail | {unique_id}) for p in parents), default=-1)
        depth[unique_id] = value
        return value

    return sorted(
        candidates,
        key=lambda c: (
            c.retry,
            c.status != "missing",
            level(c.unique_id),
            c.label,
        ),
    )


def _named(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return entries[:NAME_CAP]


def refresh(
    engine,
    manifest: dict[str, Any],
    candidates: list[Candidate],
    *,
    selector: Iterable[str] | None = None,
    limit: int | None = None,
    target: str | None = None,
) -> tuple[dict[str, Any], list[str], dict | None, Any]:
    """Run ``transform test --mutate`` for the candidates, in one batch.

    Returns the ``refresh`` block, warnings, the batch's spend and its cost.
    Confirmation and ceiling refusals propagate from the one handshake, before
    any model ran.
    """

    from .. import command_args
    from ..connect import paradigm_for
    from ..envelope import Cost, Paradigm
    from ..guards.cost_guard import skipped_handshake_warning
    from ..transform import mutation as mutation_mod
    from ..transform.build import assert_dev_target
    from ..transform.commands import _refresh_session_total, test_mutations

    if limit is not None and limit > REFRESH_CAP:
        raise ValueError(
            f"--max-models {limit} is above the ceiling of {REFRESH_CAP}; every "
            "model is a mutation run of its own, so the ceiling is what keeps one "
            "refresh's cost and time predictable. Lower it, and run the command "
            "again for the next batch"
        )
    cap = REFRESH_CAP if limit is None else max(0, limit)
    # This command's spend, and only this command's: a refresh that runs
    # nothing must not report the last one's.
    engine._last_spend = None

    config = engine.config
    connector = engine.connector or config.connector
    effective = config.model_copy(update={"connector": connector})
    paradigm = paradigm_for(connector, effective)

    nodes = manifest.get("nodes") or {}
    skipped: list[dict[str, Any]] = []
    eligible: list[Candidate] = []
    for candidate in order(candidates, manifest, selector):
        reason = skip_reason(nodes.get(candidate.unique_id) or {})
        if (
            reason is None
            and not candidate.own_tests
            and paradigm is not Paradigm.FREE_LOCAL
        ):
            # Only downstream tests read it, and routing a mutant through them
            # is opt-in where it bills, so the run would refuse it as having no
            # tests and hold a slot on every refresh.
            reason = "downstream_only"
        if reason is not None:
            skipped.append({"model": candidate.label, "reason_code": reason})
        else:
            eligible.append(candidate)
    attempted = eligible[:cap]
    elided = eligible[cap:]

    target = target or config.dbt_target or "dev"
    block: dict[str, Any] = {
        "ran": True,
        "limit": cap,
        "candidates": len(candidates),
        "attempted": len(attempted),
        "refreshed": [],
        "refused": [],
        "skipped": skipped,
        "not_run": [],
        "elided": {
            "count": len(elided),
            "models": [c.label for c in elided[:NAME_CAP]],
        },
        "target": target,
    }
    warnings: list[str] = []
    if not attempted:
        block["ran"] = False
        return block, warnings, None, None

    assert_dev_target(target, config.dbt_target)
    ceiling = engine.budget if engine.budget is not None else config.budget.ceiling

    batch = RefreshBatch(ceiling=None if paradigm is Paradigm.FREE_LOCAL else ceiling)
    refused: dict[str, dict[str, Any]] = {}

    def attempt(candidate: Candidate):
        return test_mutations(
            engine,
            candidate.name,
            target=target,
            refresh_batch=batch,
        )

    gate = None
    estimate: float | None = None
    try:
        if paradigm is not Paradigm.FREE_LOCAL:
            batch.adapter = engine._adapter("maintain coverage")
            batch.pricing = True
            for candidate in attempted:
                try:
                    attempt(candidate)
                except mutation_mod.MutationError as exc:
                    refused[candidate.unique_id] = {
                        "model": candidate.label,
                        "reason_code": _reason_code(str(exc)),
                        "reason": str(exc),
                    }
            batch.pricing = False
            priced = dict(batch.prices)
            if batch.unpriced:
                warnings.append(
                    f"{len(batch.unpriced)} model(s) could not be priced upfront "
                    f"({', '.join(batch.unpriced[:NAME_CAP])}); with no price "
                    "for their baselines nothing says they fit the budget, so "
                    "they are not run (refresh.not_run, reason_code unpriced)"
                )
            if len(refused) < len(attempted):
                # Asked even at zero: a run nothing could price still bills, so
                # it never goes ahead unconfirmed or unbudgeted.
                estimate = sum(priced.values())
                command_args.billed_handshake(
                    "maintain coverage",
                    batch.adapter,
                    estimate,
                    per_table=priced or None,
                    notes=[
                        "each model is priced as its own transform test --mutate "
                        "batch; the confirmed total covers every one",
                        *(
                            [
                                f"{len(refused)} model(s) refused while being "
                                "priced and are not in the estimate: "
                                + ", ".join(
                                    f"{entry['model']} ({entry['reason_code']})"
                                    for entry in list(refused.values())[:NAME_CAP]
                                )
                            ]
                            if refused
                            else []
                        ),
                        *batch.price_notes,
                    ],
                )
            gate = command_args.cost_gate(batch.adapter)

        exhausted = False
        for candidate in attempted:
            if candidate.unique_id in refused:
                block["refused"].append(refused[candidate.unique_id])
                continue
            if exhausted:
                block["not_run"].append(
                    {"model": candidate.label, "reason_code": "budget"}
                )
                continue
            if batch.ceiling is not None and candidate.name in batch.unpriced:
                # Its baseline has no price, so nothing says it fits: a model
                # never starts on a guess where the batch bills.
                block["not_run"].append(
                    {"model": candidate.label, "reason_code": "unpriced"}
                )
                continue
            left = batch.remaining()
            # The baseline runs before any check inside the model, so it has
            # to fit in what is left before the model starts.
            if left is not None and (
                left <= 0 or left < batch.baselines.get(candidate.name, 0.0)
            ):
                exhausted = True
                block["not_run"].append(
                    {"model": candidate.label, "reason_code": "budget"}
                )
                continue
            try:
                result = attempt(candidate)
            except mutation_mod.MutationError as exc:
                block["refused"].append(
                    {
                        "model": candidate.label,
                        "reason_code": _reason_code(str(exc)),
                        "reason": str(exc),
                    }
                )
                continue
            if result is None:  # a pricing return, never in this phase
                continue
            not_run = int(result.counts.get("not_run", 0)) + int(
                ((result.incremental or {}).get("counts") or {}).get("not_run", 0)
            )
            entry = {
                "model": candidate.label,
                "was": candidate.status,
                "score": result.score,
                "score_with_downstream": result.score_with_downstream,
                "incremental_score": (result.incremental or {}).get("score"),
                "survivors": int(result.counts.get("survived", 0)),
                "complete": not_run == 0,
                "recorded": result.record is not None,
            }
            if not_run:
                entry["not_run"] = not_run
                # Only the budget stops the batch: a branch whose own baseline
                # passes no test leaves its mutants not_run with money to spare.
                exhausted = batch.ceiling is not None and any(
                    "the confirmed budget" in w for w in result.warnings
                )
            block["refreshed"].append(entry)
            warnings.extend(
                w
                for w in result.warnings
                if w not in warnings and not w.startswith("the confirmed budget")
            )
        if exhausted:
            warnings.append(
                "the confirmed budget ran out part way through the batch: the "
                "models it did not reach are under refresh.not_run. Re-run with a "
                "larger --budget, or narrow the batch with --max-models"
            )
        if batch.adapter is not None:
            # The uptime overhead and the compute-unit translation, once for
            # the batch: the per-model runs left both unsettled.
            from ..transform.commands import _BatchSpend

            meter = _BatchSpend(
                paradigm=paradigm,
                connector=connector,
                store=engine.store,
                ceiling=None,
                adapter=batch.adapter,
            )
            meter.spend = batch.spend
            warnings.extend(meter.settle_uptime())
            batch.spend = meter.spend
        batch.spend = _refresh_session_total(
            batch.spend, gate, engine.store, paradigm, connector
        )
    finally:
        if gate is not None:
            gate.settle()

    if paradigm is Paradigm.FREE_LOCAL:
        # Said once for the batch, never once per model run.
        unasked = skipped_handshake_warning(paradigm, engine.confirmed)
        warnings = [w for w in warnings if w not in unasked] + unasked
    for key in ("refused", "not_run", "skipped", "refreshed"):
        block[f"{key}_count"] = len(block[key])
        block[key] = _named(block[key])
    cost = Cost(paradigm=paradigm, estimate=estimate, ceiling=batch.ceiling)
    engine._last_spend = batch.spend
    return block, warnings, batch.spend, cost


def _reason_code(message: str) -> str:
    """A stable code for a ``transform test --mutate`` refusal, from its words.

    The mutation command raises one error class with a sentence naming the
    property that stopped it; the batch reports each with a code a caller can
    branch on, and keeps the sentence beside it.
    """

    text = message.lower()
    table = (
        ("has no tests", "no_tests"),
        ("no sql dex knows how to plant", "no_mutable_sql"),
        ("passes against the unmutated model", "baseline_not_passing"),
        ("python model", "python_model"),
        ("installed package", "package_model"),
        ("sql_header", "sql_header"),
        ("could not make", "not_ephemeral"),
        ("compiled no model", "not_compiled"),
    )
    for needle, code in table:
        if needle in text:
            return code
    return "refused"
