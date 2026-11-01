"""Temporal contracts: a status-history model against its snapshot (#1027).

A snapshot can build and hold the right rows while a model over it disagrees
about what its history means: whether the first observation is a transition,
whether an unrelated edit counts, whether a timestamp is business or capture
time. A model's own tests adopt its own conventions, so they cannot settle this.

A ``history`` declaration states those choices, and this module derives what
each entity's values must be from the snapshot's versions and the declared
policy alone, without reading the model's SQL, then counts where the model's
columns disagree. One aggregate statement per contract: counts, never a row or
a value.

**What is supported.** One output row per entity, one snapshot, one status
column, and versions ordered by the snapshot's own validity timestamp. Anything
else is reported, never guessed:

- A policy the declaration leaves out is ``policy_missing``. None is assumed.
- Where a declared business timestamp runs backwards across a status change,
  business and capture order disagree about the sequence. Those entities are
  excluded from every comparison and counted (``business_order_conflict``).
- Where it stands still or is NULL at a status change, the transition counts
  still compare, since capture order is intact, and the timestamps do not
  (``business_timestamp_unreliable``). A source timestamp that merely equals a
  creation timestamp is not evidence either way, so it is not flagged.
- Hard-delete records are versions that are not observations of the status, so
  a snapshot writing them is not measured (``hard_delete_records``).

A run in which no entity made a transition is ``not_applicable``, not
``satisfied``: its counts agree and nothing about transitions was exercised.
Snapshot execution is reported beside the history claim and never stands in for
it: a snapshot that built says nothing about what a model made of its versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..cache import match_identifier
from ..dbt_project import strip_relation_quoting
from ..semantic_declarations import History
from .drift import DriftFinding

if TYPE_CHECKING:
    from .declared_facts import DeclaredFactsResult, Prepared, _Probe

_DEFAULT_VALID_FROM = "dbt_valid_from"
_DEFAULT_UPDATED_AT = "dbt_updated_at"

#: The output column each measure compares, and the finding that names a mismatch.
_MEASURE_FINDINGS = {
    "transition_count": "temporal_transition_count_mismatch",
    "latest_target_transition_at": "temporal_latest_transition_mismatch",
    "first_target_at": "temporal_first_attainment_mismatch",
}


@dataclass(frozen=True)
class SnapshotNode:
    name: str
    relation: str
    strategy: str | None
    hard_deletes: str | None
    valid_from: str
    updated_at: str
    #: dbt's status for this node in the last run; ``None`` when it did not run.
    execution: str | None


@dataclass(frozen=True)
class HistoryRule:
    #: The contract as declared, or ``None`` when the YAML is malformed.
    history: History | None
    node: SnapshotNode | None
    malformed: str | None = None


@dataclass(frozen=True)
class Written:
    """A contract's statement, and the snapshot relation it reads."""

    sql: str
    rule: HistoryRule
    snapshot_identifier: str


def snapshot_nodes(nodes: dict[str, Any], project_dir: Path) -> dict[str, SnapshotNode]:
    """Every snapshot the manifest holds, by lowered name, with its last status."""

    from .verify import _run_results

    results = _run_results(project_dir) or []
    status = {str(r.get("unique_id")): str(r.get("status")) for r in results}
    out: dict[str, SnapshotNode] = {}
    for uid, node in nodes.items():
        if (
            not isinstance(node, dict)
            or node.get("resource_type") != "snapshot"
            or not isinstance(node.get("name"), str)
            or not isinstance(node.get("relation_name"), str)
            or not node["relation_name"]
        ):
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        renamed = config.get("snapshot_meta_column_names")
        renamed = renamed if isinstance(renamed, dict) else {}
        out[node["name"].lower()] = SnapshotNode(
            name=node["name"],
            relation=strip_relation_quoting(node["relation_name"]),
            strategy=config.get("strategy"),
            hard_deletes=config.get("hard_deletes"),
            valid_from=renamed.get("dbt_valid_from") or _DEFAULT_VALID_FROM,
            updated_at=renamed.get("dbt_updated_at") or _DEFAULT_UPDATED_AT,
            execution=status.get(uid),
        )
    return out


def read_rule(raw: Any, snapshots: dict[str, SnapshotNode]) -> HistoryRule | None:
    """A model's ``history`` from repository text. A malformed one stays a rule,
    so an obligation someone wrote reads as unchecked rather than as absent."""

    if raw is None:
        return None
    try:
        history = History.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            ".".join(str(part) for part in error["loc"]) or "history"
            for error in exc.errors()
        )
        return HistoryRule(None, None, malformed=f"malformed at: {problems}")
    return HistoryRule(history, snapshots.get(history.snapshot.lower()))


# --- the statement ----------------------------------------------------------------


def _unwritten(prepared: Prepared, model: str, code: str, why: str) -> None:
    prepared.temporal_unwritten[model] = (code, why)
    prepared.dropped["temporal"].append(f"{model}: {why}")


def write_probe(
    adapter,
    probe: _Probe,
    live: dict[str, tuple[str, str]] | None,
    live_identifiers: list[str] | None,
    prepared: Prepared,
) -> None:
    """Write the statement that measures one contract onto ``probe``, or record
    why none could be written. Free: at most one metadata read."""

    from ..guards.cost_guard import CostGuardError
    from ..guards.sql_guard import assert_select_only
    from .declared_facts import _refusal, _transpile

    model = probe.model.model
    rule = probe.model.history
    if rule is None:  # pragma: no cover - callers only pass history models
        raise ValueError("no declared history")
    if rule.history is None:
        _unwritten(
            prepared,
            model,
            "malformed_declaration",
            f"the history declaration is {rule.malformed}",
        )
        return
    history = rule.history
    node = rule.node
    if node is None:
        _unwritten(
            prepared,
            model,
            "relation_not_found",
            f"the project has no built snapshot named '{history.snapshot}'",
        )
        return
    if node.execution in ("error", "fail", "skipped"):
        _unwritten(
            prepared,
            model,
            "snapshot_not_built",
            f"the snapshot '{node.name}' reported {node.execution} in the last run",
        )
        return
    if node.hard_deletes == "new_record":
        _unwritten(
            prepared,
            model,
            "hard_delete_records",
            f"the snapshot '{node.name}' writes hard-delete records, which are "
            "versions that are not observations of the status; this shape is not "
            "supported yet",
        )
        return

    snapshot_live: dict[str, tuple[str, str]] | None = None
    if live_identifiers is None:
        identifier = node.relation
    else:
        matches = match_identifier(node.relation, live_identifiers)
        if len(matches) != 1:
            _unwritten(
                prepared,
                model,
                "relation_not_found",
                "no single relation in the warehouse matched the snapshot "
                f"{node.relation}",
            )
            return
        identifier = matches[0]
        try:
            _meta, columns = adapter.table_metadata(identifier)
        except CostGuardError:
            raise
        except Exception as exc:
            _unwritten(
                prepared,
                model,
                "relation_not_found",
                f"the snapshot's columns could not be read ({_refusal(exc)})",
            )
            return
        snapshot_live = {c.name.lower(): (c.name, c.data_type) for c in columns} or None

    def resolved(
        column: str, within: dict[str, tuple[str, str]] | None
    ) -> tuple[str, str | None] | None:
        if within is None:
            return column, None
        return within.get(column.lower())

    measures = history.measures
    measure_columns = {
        "transition_count": measures.transition_count,
        "latest_target_transition_at": measures.latest_target_transition_at,
        "first_target_at": measures.first_target_at,
    }
    time_column = history.time.column if history.time else node.valid_from
    wanted_snapshot = [
        *history.entity.values(),
        history.status.column,
        node.valid_from,
        time_column,
    ]
    wanted_model = [
        *history.entity,
        *(c for c in measure_columns.values() if c is not None),
    ]
    absent = [
        f"{node.name}.{c}" for c in wanted_snapshot if not resolved(c, snapshot_live)
    ] + [f"{model}.{c}" for c in wanted_model if not resolved(c, live)]
    if absent:
        _unwritten(
            prepared,
            model,
            "column_missing",
            f"the history names column(s) a relation does not have: "
            f"{', '.join(dict.fromkeys(absent))}",
        )
        return

    def spell(column: str, within) -> tuple[str, str | None]:
        # Every column was resolved above, or the contract was left unwritten.
        return resolved(column, within) or (column, None)

    sql = _statement(
        history,
        relation=identifier,
        model_relation=probe.identifier,
        snapshot_keys=[spell(c, snapshot_live)[0] for c in history.entity.values()],
        model_keys=[spell(c, live)[0] for c in history.entity],
        status=spell(history.status.column, snapshot_live),
        order=spell(node.valid_from, snapshot_live)[0],
        time=spell(time_column, snapshot_live)[0],
        measures={
            name: spell(column, live)
            for name, column in measure_columns.items()
            if column is not None
        },
    )
    dialect = adapter.dialect
    probe.temporal = Written(
        sql=assert_select_only(_transpile(sql, dialect), dialect=dialect),
        rule=rule,
        snapshot_identifier=identifier,
    )


def _same(left: str, right: str) -> str:
    """A NULL-safe equality that is itself never NULL, so negating it is safe.

    Written out because not every dialect has IS NOT DISTINCT FROM, and as a CASE
    because ``NOT (a = b OR (a IS NULL AND b IS NULL))`` is NULL when one side
    alone is NULL, which would pass a value set against an expected NULL.
    """

    return (
        f"(CASE WHEN {left} = {right} THEN 1 "
        f"WHEN {left} IS NULL AND {right} IS NULL THEN 1 ELSE 0 END = 1)"
    )


def _statement(
    history: History,
    *,
    relation: str,
    model_relation: str,
    snapshot_keys: list[str],
    model_keys: list[str],
    status: tuple[str, str | None],
    order: str,
    time: str,
    measures: dict[str, tuple[str, str | None]],
) -> str:
    """One aggregate statement: the policy's expected values per entity, compared
    with the model's columns. Every output is a count."""

    from .declared_facts import _comparison, _quote, _quote_relation

    status_name, status_type = status
    subject, literal = _comparison(status_name, status_type, (history.status.target,))
    q_status, q_order, q_time = _quote(status_name), _quote(order), _quote(time)
    keys = [f"k{i}" for i in range(len(snapshot_keys))]
    key_list = ", ".join(keys)
    partition = f"OVER (PARTITION BY {key_list} ORDER BY dex_o)"

    def on(left: str, right: str) -> str:
        return " AND ".join(f"{left}.{k} = {right}.{k}" for k in keys)

    snap_rel = _quote_relation(relation)
    snap_keys = ", ".join(
        f"{_quote(c)} AS {k}" for c, k in zip(snapshot_keys, keys, strict=True)
    )
    not_null = " AND ".join(f"{_quote(c)} IS NOT NULL" for c in snapshot_keys)
    keep = (
        not_null
        if history.null_status is not None and history.null_status.value == "state"
        else f"{not_null} AND {q_status} IS NOT NULL"
    )
    counts_first = (
        history.initial_observation is not None
        and history.initial_observation.value == "transition"
    )
    business = (
        history.time is not None
        and history.time.provenance is not None
        and history.time.provenance.value == "business"
    )
    # A first observation at the target counts only under the declared policy.
    entered_first = "1" if counts_first else "0"

    def typed(name: str) -> str:
        data_type = (measures.get(name, (None, None))[1] or "").lower()
        return "DATE" if data_type == "date" else "TIMESTAMP"

    conflict = (
        "CASE WHEN dex_changed = 1 AND dex_t < dex_prev_t THEN 1 ELSE 0 END"
        if business
        else "0"
    )
    unreliable = (
        "CASE WHEN (dex_changed = 1 OR dex_target = 1) AND dex_t IS NULL THEN 1 "
        "WHEN dex_changed = 1 AND dex_t = dex_prev_t THEN 1 ELSE 0 END"
        if business
        else "0"
    )
    out_columns = [f"{_quote(c)} AS {k}" for c, k in zip(model_keys, keys, strict=True)]
    for name, (column, _type) in measures.items():
        expression = _quote(column)
        if name != "transition_count":
            expression = f"CAST({expression} AS {typed(name)})"
        out_columns.append(f"{expression} AS dex_m_{name}")
    out_not_null = " AND ".join(f"{_quote(c)} IS NOT NULL" for c in model_keys)
    model_rel = _quote_relation(model_relation)

    parts = [
        "COUNT(*) AS dex_entities",
        "COUNT(CASE WHEN dex_eligible = 1 THEN 1 END) AS dex_eligible_entities",
        "COUNT(CASE WHEN dex_eligible = 1 AND dex_hit = 1 THEN 1 END) AS dex_present",
        "COUNT(CASE WHEN dex_n > 0 THEN 1 END) AS dex_with_transition",
        "COUNT(CASE WHEN dex_conflict > 0 AND dex_ambiguous = 0 THEN 1 END) "
        "AS dex_conflicted",
        "COUNT(CASE WHEN dex_ambiguous > 0 THEN 1 END) AS dex_ambiguous_entities",
        "COUNT(CASE WHEN dex_clean = 1 AND dex_unreliable > 0 THEN 1 END) "
        "AS dex_unreliable_entities",
        "COUNT(CASE WHEN dex_hit = 1 AND dex_clean = 1 THEN 1 END) AS dex_compared",
        "COUNT(CASE WHEN dex_hit = 1 AND dex_clean = 1 AND dex_unreliable = 0 THEN 1 "
        "END) AS dex_compared_timed",
    ]
    expected_of = {
        "transition_count": "dex_n",
        "latest_target_transition_at": "dex_latest",
        "first_target_at": "dex_first",
    }
    for name in measures:
        gate = "dex_unreliable = 0 AND " if name != "transition_count" else ""
        left, right = f"dex_m_{name}", expected_of[name]
        if name != "transition_count":
            right = f"CAST({right} AS {typed(name)})"
        parts.append(
            f"COUNT(CASE WHEN dex_hit = 1 AND dex_clean = 1 AND {gate}NOT "
            f"{_same(left, right)} THEN 1 END) AS dex_bad_{name}"
        )
    parts += [
        "(SELECT COUNT(*) FROM dex_out) AS dex_out_rows",
        f"(SELECT COUNT(*) FROM (SELECT DISTINCT {key_list} FROM dex_out) d) "  # noqa: S608
        "AS dex_out_keys",
        f"(SELECT COUNT(CASE WHEN b.dex_flag = 1 THEN 1 END) FROM (SELECT DISTINCT "  # noqa: S608
        f"{key_list} FROM dex_out) a LEFT JOIN (SELECT {key_list}, 1 AS dex_flag FROM "
        f"dex_base WHERE dex_eligible = 1) b ON {on('a', 'b')}) AS dex_out_known",
    ]
    ever_target = history.population is not None and (
        history.population.value == "ever_target"
    )
    base_keys = ", ".join(f"e.{k}" for k in keys)
    eligible = "CASE WHEN x.dex_ever = 1 THEN 1 ELSE 0 END" if ever_target else "1"
    compared_measures = "".join(f"o.dex_m_{name}, " for name in measures)
    ctes = [
        f"dex_versions AS (SELECT {snap_keys}, {q_status} AS dex_s, "  # noqa: S608
        f"{q_order} AS dex_o, "
        f"{q_time} AS dex_t, CASE WHEN {subject} = {literal} THEN 1 ELSE 0 END "
        f"AS dex_target FROM {snap_rel} WHERE {keep})",
        f"dex_lagged AS (SELECT {key_list}, dex_s, dex_o, dex_t, dex_target, "  # noqa: S608
        f"LAG(dex_s) {partition} AS dex_prev_s, LAG(dex_t) {partition} AS dex_prev_t, "
        f"ROW_NUMBER() {partition} AS dex_rn FROM dex_versions)",
        f"dex_steps AS (SELECT {key_list}, dex_o, dex_t, dex_target, "  # noqa: S608
        "dex_prev_t, "
        "CASE "
        f"WHEN dex_rn = 1 THEN 0 WHEN {_same('dex_s', 'dex_prev_s')} THEN 0 "
        "ELSE 1 END AS dex_changed, "
        f"CASE WHEN dex_rn = 1 AND dex_target = 1 THEN {entered_first} ELSE 0 END "
        "AS dex_entered_first FROM dex_lagged)",
        f"dex_expected AS (SELECT {key_list}, SUM(dex_changed + dex_entered_first) "  # noqa: S608
        "AS dex_n, MAX(CASE WHEN dex_target = 1 AND (dex_changed = 1 OR "
        "dex_entered_first = 1) THEN CAST(dex_t AS TIMESTAMP) END) AS dex_latest, "
        "MIN(CASE WHEN dex_target = 1 THEN CAST(dex_t AS TIMESTAMP) END) AS dex_first, "
        f"MAX(dex_target) AS dex_ever, SUM({conflict}) AS dex_conflict, "
        f"SUM({unreliable}) AS dex_unreliable, "
        "COUNT(*) - COUNT(DISTINCT dex_o) AS dex_ambiguous "
        f"FROM dex_steps GROUP BY {key_list})",
        f"dex_entities AS (SELECT DISTINCT {snap_keys} FROM {snap_rel} "  # noqa: S608
        f"WHERE {not_null})",
        f"dex_base AS (SELECT {base_keys}, COALESCE(x.dex_n, 0) AS dex_n, "  # noqa: S608
        "x.dex_latest, x.dex_first, COALESCE(x.dex_conflict, 0) AS dex_conflict, "
        "COALESCE(x.dex_unreliable, 0) AS dex_unreliable, "
        "COALESCE(x.dex_ambiguous, 0) AS dex_ambiguous, "
        "CASE WHEN COALESCE(x.dex_conflict, 0) = 0 "
        "AND COALESCE(x.dex_ambiguous, 0) = 0 "
        f"THEN 1 ELSE 0 END AS dex_clean, {eligible} AS dex_eligible "
        f"FROM dex_entities e LEFT JOIN dex_expected x ON {on('e', 'x')})",
        f"dex_out AS (SELECT {', '.join(out_columns)}, 1 AS dex_hit FROM {model_rel} "  # noqa: S608
        f"WHERE {out_not_null})",
        f"dex_cmp AS (SELECT b.*, {compared_measures}o.dex_hit "  # noqa: S608
        f"FROM dex_base b LEFT JOIN dex_out o ON {on('b', 'o')})",
    ]
    return " ".join(
        ["WITH", ", ".join(ctes), "SELECT", ", ".join(parts), "FROM dex_cmp"]
    )


# --- running and judging ----------------------------------------------------------


def run_probe(
    adapter,
    probe: _Probe,
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
    timeout_seconds: float,
) -> None:
    from ..guards.cost_guard import CostGuardError
    from .declared_facts import _refusal

    written = probe.temporal
    if written is None:  # pragma: no cover - callers check first
        raise ValueError("no temporal statement")
    model = probe.model.model
    try:
        answer = adapter.run_query(
            written.sql, max_rows=1, timeout_seconds=timeout_seconds
        )
    except CostGuardError:
        raise
    except Exception as exc:
        detail = _refusal(exc)
        result.notes.append(
            f"the declared history on {model} could not be measured: the warehouse "
            f"refused the statement ({detail})"
        )
        prepared.dropped["temporal"].append(
            f"{model}: the warehouse refused the check ({detail})"
        )
        result.temporal_contracts.append(
            _record(
                model, written.rule, written, None, "unchecked", "probe_refused", detail
            )
        )
        return
    row = {
        str(k).lower(): int(v or 0)
        for k, v in zip(answer.columns, answer.cells[0], strict=True)
    }
    _judge(model, written, row, result, checked, prepared)


def record_unmeasured(
    prepared: Prepared,
    result: DeclaredFactsResult,
    reason_code: str,
    reason: str | None,
) -> None:
    """A record for every declared contract that has none, so an obligation
    nothing measured reads as unchecked rather than as absent."""

    recorded = {r["model"] for r in result.temporal_contracts}
    for declared in prepared.temporal_models:
        if declared.model in recorded or declared.history is None:
            continue
        code, why = prepared.temporal_unwritten.get(
            declared.model, (reason_code, reason)
        )
        if why is None:
            code, why = (
                "relation_not_found",
                next(
                    (
                        d.partition(": ")[2]
                        for d in prepared.dropped.get("temporal", [])
                        if d.startswith(f"{declared.model}: ")
                    ),
                    "the check could not be written",
                ),
            )
        result.temporal_contracts.append(
            _record(
                declared.model, declared.history, None, None, "unchecked", code, why
            )
        )


def _record(
    model: str,
    rule: HistoryRule,
    written: Written | None,
    measured: dict[str, int] | None,
    status: str,
    reason_code: str | None = None,
    reason: str | None = None,
    assertions: list[dict[str, Any]] | None = None,
    unchecked: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history = rule.history
    node = rule.node
    record: dict[str, Any] = {
        "model": model,
        "declared": _declared(history),
        "snapshot_relation": (
            written.snapshot_identifier if written else node.relation if node else None
        ),
        "snapshot_execution": {
            "status": (node.execution or "not_in_last_run") if node else "unknown",
            "strategy": node.strategy if node else None,
        },
        "status": status,
        "assertions": assertions or [],
        "unchecked": unchecked or [],
    }
    if measured is not None:
        record["measured"] = measured
    if reason_code is not None:
        record["reason_code"] = reason_code
    if reason is not None:
        record["reason"] = reason
    return record


def _declared(history: History | None) -> dict[str, Any] | None:
    """The policy, with the status target left out: every other field is a name
    or an enum, and a record has no need to repeat a literal."""

    if history is None:
        return None
    dumped = history.model_dump(mode="json", exclude_none=True)
    dumped["status"] = {"column": history.status.column}
    return dumped


def _judge(
    model: str,
    written: Written,
    row: dict[str, int],
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
) -> None:
    rule = written.rule
    history = rule.history
    if history is None:  # pragma: no cover - a written contract parsed
        raise ValueError("no declared history")
    node = rule.node
    entities = row.get("dex_entities", 0)
    out_rows = row.get("dex_out_rows", 0)
    eligible = row.get("dex_eligible_entities", 0)
    missing = eligible - row.get("dex_present", 0)
    unexpected = row.get("dex_out_keys", 0) - row.get("dex_out_known", 0)
    measured = {
        "snapshot_entities": entities,
        "eligible_entities": eligible,
        "entities_with_transition": row.get("dex_with_transition", 0),
        "output_rows": out_rows,
        "output_keys": row.get("dex_out_keys", 0),
        "missing_entities": missing,
        "unexpected_entities": unexpected,
    }
    if entities == 0 and out_rows == 0:
        reason = (
            "the snapshot and the model hold no rows, so there is no history to judge"
        )
        prepared.dropped["temporal"].append(f"{model}: {reason}")
        result.temporal_contracts.append(
            _record(
                model, rule, written, measured, "not_applicable", "empty_input", reason
            )
        )
        return
    checked["temporal"] += 1

    assertions: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    contradicted = False

    def finding(code: str, detail: str, severity: str = "high", **data: Any) -> None:
        result.findings.append(
            DriftFinding(
                axis="declaration",
                code=code,
                identifier=model,
                severity=severity,
                detail=detail,
                data={"model": model, "snapshot": history.snapshot, **data},
            )
        )

    conflicted = row.get("dex_conflicted", 0)
    ambiguous = row.get("dex_ambiguous_entities", 0)
    unreliable = row.get("dex_unreliable_entities", 0)
    if conflicted:
        unchecked.append(
            {
                "reason_code": "business_order_conflict",
                "entities": conflicted,
                "detail": (
                    f"for {conflicted} entit(ies) the declared business timestamp runs "
                    "backwards across a status change, so business and capture order "
                    "give different sequences; they were excluded rather than guessed"
                ),
            }
        )
    if ambiguous:
        unchecked.append(
            {
                "reason_code": "ambiguous_version_order",
                "entities": ambiguous,
                "detail": (
                    f"{ambiguous} entit(ies) hold versions sharing one validity "
                    "timestamp, so their order is undefined; they were excluded"
                ),
            }
        )

    policy_owed: dict[str, list[str]] = {
        "transition_count": _owed(history, "initial_observation", "null_status"),
        "latest_target_transition_at": _owed(
            history, "initial_observation", "null_status", "time"
        ),
        "first_target_at": _owed(history, "time"),
    }
    measure_columns = {
        "transition_count": history.measures.transition_count,
        "latest_target_transition_at": history.measures.latest_target_transition_at,
        "first_target_at": history.measures.first_target_at,
    }
    for name, column in measure_columns.items():
        if column is None:
            continue
        timed = name != "transition_count"
        compared = row.get("dex_compared_timed" if timed else "dex_compared", 0)
        bad = row.get(f"dex_bad_{name}", 0)
        entry: dict[str, Any] = {
            "assertion": name,
            "column": column,
            "compared": compared,
            "mismatched_entities": bad,
        }
        owed = policy_owed[name]
        if owed:
            entry.update(
                status="unchecked",
                reason_code="policy_missing",
                mismatched_entities=None,
                owed=owed,
            )
        elif bad:
            contradicted = True
            entry["status"] = "contradicted"
            finding(
                _MEASURE_FINDINGS[name],
                f"'{model}.{column}' disagrees with the {name} its declared history "
                f"policy gives from '{history.snapshot}' for {bad} of {compared} "
                f"entit(ies) compared ({_policy_text(history)}). Correct the SQL, or "
                "correct the declaration if the policy is not what was meant",
                column=column,
                assertion=name,
                compared=compared,
                mismatched_entities=bad,
                initial_observation=_value(history.initial_observation),
                null_status=_value(history.null_status),
            )
        elif compared == 0:
            entry.update(status="unchecked", reason_code="no_entity_comparable")
        else:
            entry["status"] = "satisfied"
        assertions.append(entry)

    if unreliable and any(
        measure_columns[name] is not None
        for name in ("latest_target_transition_at", "first_target_at")
    ):
        unchecked.append(
            {
                "reason_code": "business_timestamp_unreliable",
                "entities": unreliable,
                "detail": (
                    f"for {unreliable} entit(ies) the declared business timestamp is "
                    "NULL or does not advance at a status change, so their time "
                    "fields were not compared; their transition counts were, since "
                    "capture order is intact"
                ),
            }
        )

    if history.population is None:
        assertions.append(
            {
                "assertion": "population",
                "status": "unchecked",
                "reason_code": "policy_missing",
                "owed": ["population"],
            }
        )
    else:
        wrong = bool(missing or unexpected)
        assertions.append(
            {
                "assertion": "population",
                "status": "contradicted" if wrong else "satisfied",
                "compared": eligible,
                "missing_entities": missing,
                "unexpected_entities": unexpected,
            }
        )
        rule_text = history.population.value
        if missing:
            contradicted = True
            finding(
                "temporal_population_missing",
                f"'{model}' declares the history population {rule_text}, but "
                f"{missing} of {eligible} eligible entit(ies) in '{history.snapshot}' "
                "have no row in it",
                missing_entities=missing,
                eligible_entities=eligible,
            )
        if unexpected:
            contradicted = True
            finding(
                "temporal_population_unexpected",
                f"'{model}' declares the history population {rule_text}, but "
                f"{unexpected} of its {row.get('dex_out_keys', 0)} key(s) are not in "
                f"that population of '{history.snapshot}'",
                unexpected_entities=unexpected,
                eligible_entities=eligible,
            )

    if history.time is not None and history.time.provenance is not None and node:
        problem = _provenance_problem(history, node)
        if problem is None:
            assertions.append({"assertion": "time_provenance", "status": "satisfied"})
        else:
            contradicted = True
            assertions.append(
                {"assertion": "time_provenance", "status": "contradicted"}
            )
            finding(
                "temporal_provenance_contradicted",
                f"'{model}' declares its time column '{history.time.column}' as "
                f"{history.time.provenance.value} time, but {problem}",
                severity="medium",
                column=history.time.column,
                provenance=history.time.provenance.value,
                strategy=node.strategy,
            )

    if not assertions:
        unchecked.append(
            {
                "reason_code": "nothing_to_compare",
                "detail": "the history declares no measure and no population",
            }
        )

    unchecked_codes = [item["reason_code"] for item in unchecked] + [
        item["reason_code"] for item in assertions if item["status"] == "unchecked"
    ]
    status, reason_code, reason = "satisfied", None, None
    if contradicted:
        status = "contradicted"
    elif unchecked_codes:
        status, reason_code = "unchecked", unchecked_codes[0]
        reason = "what the record lists under `unchecked` was not established"
    elif row.get("dex_with_transition", 0) == 0:
        status, reason_code = "not_applicable", "no_history_observed"
        reason = (
            "no entity made a transition under the declared policy, so the columns "
            "agree without transitions having been exercised"
        )
    result.temporal_contracts.append(
        _record(
            model,
            rule,
            written,
            measured,
            status,
            reason_code,
            reason,
            assertions=assertions,
            unchecked=unchecked,
        )
    )


def _value(item: Any) -> str | None:
    return item.value if item is not None else None


def _owed(history: History, *fields: str) -> list[str]:
    owed = []
    for name in fields:
        if name == "time":
            if history.time is None or history.time.provenance is None:
                owed.append("time.provenance")
        elif getattr(history, name) is None:
            owed.append(name)
    return owed


def _policy_text(history: History) -> str:
    return (
        f"initial_observation: {_value(history.initial_observation)}, "
        f"null_status: {_value(history.null_status)}"
    )


def _provenance_problem(history: History, node: SnapshotNode) -> str | None:
    """Why a declared provenance cannot be true of this column, or None.

    Under the ``timestamp`` strategy dbt copies the source's ``updated_at`` into
    its validity column, so that column is source time; under ``check`` it is
    the snapshot's clock. A column dbt does not write is not capture time.
    """

    time = history.time
    if time is None or time.provenance is None:
        return None
    written_by_dbt = time.column.lower() in {
        node.valid_from.lower(),
        node.updated_at.lower(),
    }
    if time.provenance.value == "capture":
        if not written_by_dbt:
            return (
                "the snapshot does not write that column, so it comes from the "
                "source rather than from the snapshot's clock"
            )
        if node.strategy == "timestamp":
            return (
                f"'{node.name}' uses the timestamp strategy, under which dbt copies "
                "that value from the source's updated_at"
            )
        return None
    if written_by_dbt and node.strategy == "check":
        return (
            f"'{node.name}' uses the check strategy, under which that column holds "
            "the time the snapshot ran"
        )
    return None
