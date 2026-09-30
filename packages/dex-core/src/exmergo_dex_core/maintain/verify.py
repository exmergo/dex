"""maintain verify: a baseline-free sweep answering "is this project correct
right now", as opposed to drift's "what changed since the baseline" (#224).

Five finding classes live here. Build-status gaps (#225) read the compiled
manifest and the last run's ``run_results.json``: failed nodes, nodes skipped
by a failed parent, models with no relation, and a project that does not
compile. Column contract (#230) compares a built relation's actual columns
against what its schema.yml declares, in both directions, plus a type check
where a type is declared. Row population (#226) reads each model's compiled
SQL to find the relation it is built *from*, and compares the two row counts:
a model holding materially fewer rows than its driving parent, with nothing
in its SQL that would account for the shortfall, has lost rows silently, and
one holding materially more has fanned out on a join. Grain (#229) checks
that a model's intended grain -- a declared ``unique`` test, a semantic
model's declared primary entity, or (absent either) a free naming heuristic
-- still holds one row per key in the built relation. Join contract (#228)
reads the same compiled SQL for every equality join a model writes, and
measures the value overlap on each: a join whose keys find almost nothing on
the other side is silently wrong in a way no dbt test catches, and is the
most damaging defect this sweep can report.

Detection is pure and reads only artifacts already on disk, with four
exceptions, all metadata-only or bounded-and-deliberate by the adapter's own
contract rather than an open-ended scan: the column contract's own
``adapter.table_metadata`` read, the grain check's ``adapter.
exact_distinct_counts``/``distinct_combination_counts`` (called only on a
declared or already-near-unique key, never speculatively across a table), the
join contract's own overlap probe (:func:`join_contract_findings`, priced and
offered exactly like :func:`relation_counts` below rather than taken), and
:func:`relation_counts` itself, which is where an open-ended warehouse *scan*
can happen at all and is separate for that reason: it decides between the
catalog's free metadata, a free count, and a count that has to be paid for
and is therefore offered rather than taken. Keeping that decision in one
place is what lets every other function here be judged on artifacts (or free,
bounded metadata) alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..cache import match_identifier
from ..dbt_project import strip_relation_quoting
from ..explore.profile import NEAR_UNIQUE_RATIO
from ..explore.relationships import entity_of, is_id_shaped
from ..transform.build import shadow_parse
from .drift import DriftFinding, _grain_severity

#: dbt's own run_results.json status strings that mean the node did not build.
_FAILURE_STATUSES = frozenset({"error", "fail"})

# A test dbt ran at `severity: warn` passed the run and failed the assertion.
# Neither a failure nor a skip, so it fell through both branches and was
# reported nowhere: a build whose seventeen warnings are normal and whose
# eighteenth is a regression had no way to say which seventeen they were.
_WARNING_STATUSES = frozenset({"warn"})

# Deliberately the same numbers `volume_drift` uses, because it is the same
# judgement asked of a different pair of counts: below the first, a row-count
# difference is chatter rather than a defect, and past the second it is not a
# discrepancy any more but a collapse. Kept as their own constants rather than
# imported, since the two axes are free to diverge on field evidence without
# dragging the other with them.
_ROW_DIFFERENCE_REPORT_FRACTION = 0.10
_ROW_LOSS_HIGH_FRACTION = 0.50

# A model that more than doubles its driving parent is not a join picking up a
# few extra matches; the key it joins on is wrong.
_ROW_FANOUT_HIGH_FACTOR = 2.0

# Materializations whose row count is not a function of this run's parent, so
# comparing the two says nothing. An incremental model holds whatever previous
# runs loaded into it, which is the point of the materialization.
_UNCOMPARABLE_MATERIALIZATIONS = frozenset({"incremental"})


def compile_check(project_dir: Path) -> tuple[DriftFinding | None, list[str]]:
    """Whether the project parses at all.

    A project that does not compile invalidates every finding computed from
    its manifest (a stale or absent ``target/manifest.json`` looks identical
    to one from a project that simply has not been built yet), so this is
    meant to run first, and the caller suppresses the manifest-derived checks
    on failure (#172's inertness, #225's third acceptance bullet).

    Returns ``(finding, notes)``: a finding only on a proven parse failure.
    ``notes`` carries a reason instead when the check could not run at all
    (no dbt installed, no ``profiles.yml``) rather than silently reporting
    nothing, reusing :func:`~..transform.build.shadow_parse`'s own degrade
    path so this and `transform plan` never disagree about when dbt is
    reachable.
    """

    result = shadow_parse(project_dir, [])
    if not result["available"]:
        return None, [f"compile check skipped ({result['reason']})"]
    if result["success"]:
        return None, []
    detail = result["messages"][0] if result["messages"] else "dbt parse failed"
    finding = DriftFinding(
        axis="build",
        code="project_does_not_compile",
        severity="high",
        detail=detail,
        data={"messages": result["messages"]},
    )
    return finding, []


def _manifest_nodes(project_dir: Path) -> dict[str, dict] | None:
    path = project_dir / "target" / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("nodes", {})


def _run_results(project_dir: Path) -> list[dict] | None:
    path = project_dir / "target" / "run_results.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("results", [])


def build_status_findings(project_dir: Path) -> tuple[list[DriftFinding], list[str]]:
    """Nodes that failed, were skipped because a parent failed, or warned, from
    the last run's ``run_results.json`` joined to the compiled manifest for
    names and the dependency graph. Free: reads artifacts already on disk,
    opens no connection.

    A skip is walked back through however many transitively-skipped parents
    it takes to name the node that actually failed, since a two-layer partial
    build (a grandparent failure skipping both its child and grandchild)
    would otherwise only ever name the immediate, also-skipped parent.

    A warning ranks low on purpose. A project that runs relationship tests at
    ``severity: warn`` over documented gaps has warnings by design, so this is
    a list to compare against last run's rather than a defect on its own; what
    it must not be is absent, which is what leaves a caller counting statuses
    in the raw node list to find out which ones warned.
    """

    nodes = _manifest_nodes(project_dir) or {}
    results = _run_results(project_dir)
    if results is None:
        return (
            [],
            [
                "no dbt run results found; run `dbt run` or `dbt build` for "
                "build-status findings"
            ],
        )

    def name_of(uid: str) -> str:
        node = nodes.get(uid)
        if node and node.get("name"):
            return str(node["name"])
        return uid.rsplit(".", 1)[-1]

    status_by_uid = {
        r["unique_id"]: str(r.get("status", "unknown"))
        for r in results
        if r.get("unique_id")
    }

    def failed_ancestor(uid: str, seen: set[str]) -> str | None:
        if uid in seen:
            return None
        seen.add(uid)
        node = nodes.get(uid) or {}
        for dep in (node.get("depends_on") or {}).get("nodes", []):
            status = status_by_uid.get(dep)
            if status in _FAILURE_STATUSES:
                return name_of(dep)
            if status == "skipped":
                found = failed_ancestor(dep, seen)
                if found:
                    return found
        return None

    findings: list[DriftFinding] = []
    for result in results:
        uid = result.get("unique_id")
        status = str(result.get("status", "unknown"))
        if not uid or status not in _FAILURE_STATUSES | _WARNING_STATUSES | {"skipped"}:
            continue
        name = name_of(uid)
        if status in _WARNING_STATUSES:
            message = result.get("message") or status
            findings.append(
                DriftFinding(
                    axis="build",
                    code="node_warned",
                    identifier=name,
                    severity="low",
                    detail=f"'{name}' warned rather than failed: {message}",
                    data={"status": status},
                )
            )
            continue
        if status in _FAILURE_STATUSES:
            message = result.get("message") or status
            findings.append(
                DriftFinding(
                    axis="build",
                    code="node_failed",
                    identifier=name,
                    severity="high",
                    detail=f"'{name}' failed to build: {message}",
                    data={"status": status},
                )
            )
            continue
        cause = failed_ancestor(uid, set())
        findings.append(
            DriftFinding(
                axis="build",
                code="node_skipped",
                identifier=name,
                # A named cause is a definite causal chain; an unnamed one
                # (a selector exclusion, an upstream error dbt did not
                # attribute) is real but less actionable, so it ranks lower.
                severity="medium" if cause else "low",
                detail=(
                    f"'{name}' was skipped because '{cause}' failed to build"
                    if cause
                    else f"'{name}' was skipped"
                ),
                data={"caused_by": cause} if cause else {},
            )
        )
    return findings, []


def missing_relation_findings(
    model_relations: dict[str, str],
    live_identifiers: list[str],
    already_reported: set[str],
) -> list[DriftFinding]:
    """Manifest models with no corresponding relation in the warehouse.

    ``already_reported`` names models a build-status finding already
    explained (a node that failed or was skipped never produced a relation
    either, and reporting that twice under a different code would say the
    same thing about the same node in two places). ``model_relations`` is
    expected pre-filtered to model names (no ``.``): a source's own
    "declared but absent" case belongs to the schema axis, which already
    reports dangling sources against a baseline.
    """

    findings: list[DriftFinding] = []
    for name, relation in sorted(model_relations.items()):
        if name in already_reported:
            continue
        if match_identifier(relation, live_identifiers):
            continue
        findings.append(
            DriftFinding(
                axis="build",
                code="no_relation",
                identifier=name,
                severity="high",
                detail=(
                    f"'{name}' is declared in the project but has no relation "
                    f"in the warehouse ({relation})"
                ),
                data={"relation_name": relation},
            )
        )
    return findings


# --- column contract: a built relation against its declared schema.yml ---------


def column_contract_plan(
    project_dir: Path, *, scope: set[str] | None = None
) -> tuple[dict[str, dict[str, str | None]], int, list[str]]:
    """Every selected model's declared column contract, from the compiled
    manifest, plus how many considered models declare none at all (#230).

    Read from the manifest rather than by re-parsing schema.yml, the way
    #214's plan-time comparison has to: dbt already reduces every declared
    column (and its ``data_type``, when one is declared) into each model
    node's own ``columns``, and a compiled manifest is exactly what this
    command needs a build to have produced already for its other two finding
    classes. ``scope`` narrows which models are even considered, so a model
    outside it is neither checked nor counted toward the undeclared total,
    the same rule :func:`row_population_plan` follows. Compared lowercase on
    both sides: the caller here is `maintain verify`'s own ``objects``
    argument, already lowered before it reaches this function, and a model
    name is conventionally lowercase already, so this never narrows past
    what an exact-case match would.

    Returns ``(declared_by_model, undeclared_count, notes)``. A model with no
    ``columns:`` entry contributes to the count and nothing to the mapping:
    it has no contract to compare, which is a fact about the project rather
    than a finding about any one model, so it is named once at the summary
    level (#230's acceptance) rather than repeated per model. ``notes`` names
    the one condition that stops this from running at all, the same way
    :func:`row_population_plan` does for its own manifest read.
    """

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return (
            {},
            0,
            [
                "no compiled manifest found; run `dbt compile` or `dbt build` "
                "for column-contract findings"
            ],
        )

    declared_by_model: dict[str, dict[str, str | None]] = {}
    undeclared = 0
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = node.get("name")
        if not (isinstance(name, str) and name):
            continue
        if scope is not None and name.lower() not in scope:
            continue
        columns = node.get("columns")
        if not isinstance(columns, dict) or not columns:
            undeclared += 1
            continue
        declared_by_model[name] = {
            str(column_name): (
                column["data_type"]
                if isinstance(column, dict) and isinstance(column.get("data_type"), str)
                else None
            )
            for column_name, column in columns.items()
            if isinstance(column_name, str)
        }
    return declared_by_model, undeclared, []


def column_contract_findings(
    adapter,
    declared_by_model: dict[str, dict[str, str | None]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    undeclared: int,
) -> tuple[list[DriftFinding], list[str]]:
    """A built relation's actual columns against what its schema.yml declares,
    in both directions, plus a type check where a type is declared (#230).

    Two-directional and deliberately asymmetric in severity, per the issue's
    own framing: a documented column the relation does not have is a real
    defect (``high``, someone reading the docs to write a query gets a
    column that is not there), and an undocumented column is a
    documentation gap (``low``). A type disagreement ranks between the two:
    the column exists on both sides, so nothing is missing, but a reader
    trusting the declared type gets the wrong one.

    Free on every connector: both sides come from metadata dex already has
    to read for this command's other checks (the manifest) or reads no
    differently than :func:`missing_relation_findings` does
    (``adapter.table_metadata``, a schema lookup, never a scan).

    A model with no relation in the warehouse, or an ambiguous one, is named
    in the notes rather than silently skipped: the no-relation finding
    already explains the missing-entirely case, so naming it again here
    would only be additive if the model resolved ambiguously, which is worth
    knowing about but does not deserve its own high-severity finding.
    """

    from ..dbt_project import column_contract_divergence

    findings: list[DriftFinding] = []
    unchecked: list[str] = []
    for model, declared in sorted(declared_by_model.items()):
        relation = model_relations.get(model)
        if relation is None:
            continue
        matches = match_identifier(relation, live_identifiers)
        if len(matches) != 1:
            unchecked.append(model)
            continue

        declared_lower = {name.lower(): dtype for name, dtype in declared.items()}
        _meta, columns = adapter.table_metadata(matches[0])
        actual_lower = {column.name.lower(): column.data_type for column in columns}
        missing, extra, mismatched = column_contract_divergence(
            declared_lower, actual_lower
        )

        if missing:
            findings.append(
                DriftFinding(
                    axis="build",
                    code="column_missing",
                    identifier=model,
                    severity="high",
                    detail=(
                        f"'{model}' declares column(s) {', '.join(missing)} in "
                        "schema.yml that the built relation does not have"
                    ),
                    data={"model": model, "columns": missing},
                )
            )
        if extra:
            findings.append(
                DriftFinding(
                    axis="build",
                    code="column_undeclared",
                    identifier=model,
                    severity="low",
                    detail=(
                        f"'{model}' builds column(s) {', '.join(extra)} that "
                        "schema.yml does not declare"
                    ),
                    data={"model": model, "columns": extra},
                )
            )
        if mismatched:
            findings.append(
                DriftFinding(
                    axis="build",
                    code="column_type_mismatch",
                    identifier=model,
                    severity="medium",
                    detail=(
                        f"'{model}' declares "
                        + ", ".join(
                            f"{name} as {declared_type}"
                            for name, declared_type, _ in mismatched
                        )
                        + " in schema.yml, but the built relation has "
                        + ", ".join(
                            f"{name} as {actual_type}"
                            for name, _, actual_type in mismatched
                        )
                    ),
                    data={
                        "model": model,
                        "mismatches": [
                            {"column": name, "declared": d, "actual": a}
                            for name, d, a in mismatched
                        ],
                    },
                )
            )

    notes: list[str] = []
    if unchecked:
        notes.append(
            "the column contract was not checked for "
            + ", ".join(sorted(unchecked))
            + ": no single relation in the warehouse matched"
        )
    if undeclared:
        notes.append(
            f"{undeclared} model(s) considered declare no columns in "
            "schema.yml, so the column contract was not checked for them"
        )
    return findings, notes


# --- grain: a built relation against its intended one-row-per-entity key -------


@dataclass(frozen=True)
class GrainCandidate:
    """One model's intended grain, and where the claim came from (#229).

    ``source`` is one of ``"unique_test"`` (a single column dbt's own
    ``unique``/``data_tests: unique`` declares), ``"unique_test_composite"``
    (``unique_combination_of_columns``, checked only when no single-column
    test exists for the same model: a column-level test is the narrower,
    more specific claim), ``"primary_entity"`` (a semantic model's declared
    grain, checked only when neither dbt test exists: a real declaration, but
    one MetricFlow's modeling layer makes rather than a test dbt itself
    verifies), or ``"heuristic"`` (no declaration at all; a free naming guess,
    see :func:`_heuristic_candidate`).
    """

    columns: list[str]
    source: str


def grain_plan(
    definitions, *, scope: set[str] | None = None
) -> dict[str, list[GrainCandidate]]:
    """Every selected model's declared grain, straight from ``ProjectDefinitions``
    (#229) -- no manifest walk of its own, unlike every other plan function
    here: a declared unique test, a declared composite unique combination, and
    a semantic model's primary entity are already fully resolved there (the
    same object :func:`~exmergo_dex_core.maintain.commands.verify` already
    reads for ``model_relations``), so re-deriving any of it from
    ``target/manifest.json`` directly would only duplicate what
    ``dbt_project.py``'s own readers already did.

    A model absent from the returned mapping declares no grain at all by any
    of the three sources; the findings step's own free naming heuristic and
    "grain unknown" reporting cover it from there, since both need a live
    relation's actual columns, which ``ProjectDefinitions`` does not carry.

    Priority is per model, not per candidate: every column independently
    declared ``unique`` is checked (a model can have more than one, each its
    own claim); a declared composite is checked only when the model has no
    single-column unique test; the primary entity is checked only when the
    model has neither. A model can therefore only ever produce candidates
    from one of the three sources, never a mix.

    ``scope`` is compared lowercase, the same convention
    :func:`column_contract_plan` follows and for the same reason: the caller
    is `maintain verify`'s own ``objects`` argument, already lowered.
    """

    by_model: dict[str, list[GrainCandidate]] = {}
    for key in definitions.declared_keys:
        if not key.unique:
            continue
        if scope is not None and key.model.lower() not in scope:
            continue
        by_model.setdefault(key.model, []).append(
            GrainCandidate(columns=[key.column], source="unique_test")
        )
    for combo in definitions.declared_composite_keys:
        if combo.model in by_model:
            continue
        if scope is not None and combo.model.lower() not in scope:
            continue
        by_model.setdefault(combo.model, []).append(
            GrainCandidate(columns=list(combo.columns), source="unique_test_composite")
        )
    for model, column in definitions.primary_entities.items():
        if model in by_model:
            continue
        if scope is not None and model.lower() not in scope:
            continue
        by_model[model] = [GrainCandidate(columns=[column], source="primary_entity")]
    return by_model


_GRAIN_SOURCE_PHRASE = {
    "unique_test": "declares",
    "unique_test_composite": "declares the combination of",
    "primary_entity": "declares as its semantic layer's primary entity",
    "heuristic": "has no declared grain, but appears (by column name) to intend",
}


def _grain_broken_finding(
    model: str,
    columns: list[str],
    row_count: int,
    distinct_count: int,
    *,
    exact: bool,
    source: str,
    min_rows: int,
) -> DriftFinding:
    duplicates = row_count - distinct_count
    severity, extra_data, note_suffix = _grain_severity(row_count, min_rows)
    approx = "" if exact else "approximately "
    columns_text = " and ".join(columns) if len(columns) < 3 else ", ".join(columns)
    unique_word = "unique" if len(columns) == 1 else "jointly unique"
    return DriftFinding(
        axis="grain",
        code="grain_broken",
        identifier=model,
        column=columns[0] if len(columns) == 1 else None,
        severity=severity,
        exact=exact,
        detail=(
            f"'{model}' {_GRAIN_SOURCE_PHRASE[source]} {columns_text} "
            f"{unique_word}, but the built relation has {approx}{duplicates} "
            f"duplicate row(s){note_suffix}"
        ),
        data={
            "model": model,
            "columns": columns,
            "duplicate_count": duplicates,
            "source": source,
            **extra_data,
        },
    )


def _declared_grain_findings(
    adapter,
    model: str,
    identifier: str,
    candidates: list[GrainCandidate],
    min_rows: int,
) -> tuple[list[DriftFinding], bool]:
    """Check every declared candidate for one model. Returns ``(findings,
    unchecked)``: ``unchecked`` is True when a composite candidate could not
    be measured at all (a metered adapter declining an unaffordable scan,
    per :meth:`Adapter.distinct_combination_counts`'s own contract), which is
    a caveat for the caller's notes, not a clean bill of health."""

    meta, _columns = adapter.table_metadata(identifier)
    row_count = meta.row_count
    if row_count is None:
        return [], True

    findings: list[DriftFinding] = []
    unchecked = False

    singles = [c for c in candidates if len(c.columns) == 1]
    if singles:
        counts = adapter.exact_distinct_counts(
            identifier, [c.columns[0] for c in singles]
        )
        for c in singles:
            distinct = counts.get(c.columns[0])
            if distinct is None:
                unchecked = True
                continue
            if distinct >= row_count:
                continue
            findings.append(
                _grain_broken_finding(
                    model,
                    c.columns,
                    row_count,
                    distinct,
                    exact=True,
                    source=c.source,
                    min_rows=min_rows,
                )
            )

    for c in candidates:
        if len(c.columns) == 1:
            continue
        result = adapter.distinct_combination_counts(identifier, [c.columns])
        distinct = result.get(tuple(c.columns))
        if distinct is None:
            unchecked = True
            continue
        if distinct >= row_count:
            continue
        findings.append(
            _grain_broken_finding(
                model,
                c.columns,
                row_count,
                distinct,
                exact=True,
                source=c.source,
                min_rows=min_rows,
            )
        )

    return findings, unchecked


def _heuristic_candidate(adapter, model: str, identifier: str, min_rows: int):
    """The free naming heuristic (#229): no declared grain at all, so this
    looks for a column named ``id``/``<entity>_id`` or otherwise
    :func:`~exmergo_dex_core.explore.relationships.is_id_shaped`, the same
    shape :func:`~exmergo_dex_core.explore.relationships.detect_grain` prefers
    -- but unlike that function, never trusts a naming match as proof by
    itself. A guess from a name alone is the weakest of the three sources,
    so it earns the cheapest possible check first: an approximate distinct
    count, already free metadata this command reads for column contract.
    Only a column that already looks near-unique in that approximate count
    is worth a real (still cheap, single-column) exact scan to turn a guess
    into a proof; a column nowhere close to unique is reported straight from
    the approximate count instead, honestly marked ``exact=False``, since
    escalating would only confirm what the approximation already made clear.

    Returns ``(finding_or_None, reason)`` where ``reason`` is ``None`` (clean
    or a finding was produced), ``"unknown"`` (no column even looked like a
    key), or ``"unsupported"`` (the adapter cannot run the check this needs).
    """

    meta, columns = adapter.table_metadata(identifier)
    row_count = meta.row_count
    entity = entity_of(identifier.rsplit(".", 1)[-1])
    named = (f"{entity}_id", f"{entity}id", "id")
    candidate = next((c for c in columns if c.name.lower() in named), None)
    if candidate is None:
        candidate = next((c for c in columns if is_id_shaped(c.name)), None)
    if candidate is None:
        return None, "unknown"
    if row_count is None:
        return None, "unsupported"

    aggregates_fn = getattr(adapter, "column_aggregates", None)
    if aggregates_fn is None:
        return None, "unsupported"
    aggregates = aggregates_fn(identifier, [candidate])
    agg = next((a for a in aggregates if a.name == candidate.name), None)
    if agg is None or agg.distinct_count is None:
        return None, "unsupported"
    if agg.null_fraction not in (0.0, None):
        # A key candidate with nulls is not a key: disqualified, not proven.
        return None, "unknown"

    non_null = round((1 - (agg.null_fraction or 0.0)) * row_count)
    if non_null and agg.distinct_count >= NEAR_UNIQUE_RATIO * non_null:
        exact_fn = getattr(adapter, "exact_distinct_counts", None)
        if exact_fn is not None:
            exact_counts = exact_fn(identifier, [candidate.name])
            distinct = exact_counts.get(candidate.name)
            if distinct is not None:
                if distinct >= row_count:
                    return None, None
                return (
                    _grain_broken_finding(
                        model,
                        [candidate.name],
                        row_count,
                        distinct,
                        exact=True,
                        source="heuristic",
                        min_rows=min_rows,
                    ),
                    None,
                )
    if agg.distinct_count >= row_count:
        return None, None
    return (
        _grain_broken_finding(
            model,
            [candidate.name],
            row_count,
            agg.distinct_count,
            exact=False,
            source="heuristic",
            min_rows=min_rows,
        ),
        None,
    )


def grain_findings(
    adapter,
    declared: dict[str, list[GrainCandidate]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    all_models: set[str],
    *,
    min_rows: int = 100,
) -> tuple[list[DriftFinding], list[str]]:
    """A built relation's grain against what the project declares (or, absent
    any declaration, a free naming guess), for every selected model (#229).

    A declared source (unique test, composite, or primary entity) is checked
    directly with an exact scan: the declaration itself is the justification
    for spending on verification, the same reasoning ``maintain grain``'s own
    ``declared_composite_checks`` already rests on. Only a model with no
    declaration at all falls to the heuristic, which is free-first and
    escalates to exact only when already close (see
    :func:`_heuristic_candidate`) -- so a ``grain_broken`` finding's ``exact``
    flag is always True except in exactly that one approximate-verdict case,
    honestly naming the one place this check ever reports from a guess.

    An unknown grain and a broken one are reported through different
    channels on purpose (the issue's own framing): a broken grain is a
    ``DriftFinding``; an unknown one is named once in ``notes`` rather than
    given a finding of its own, since "dex could not tell" is not itself a
    defect in the project.
    """

    findings: list[DriftFinding] = []
    ambiguous: list[str] = []
    unchecked: list[str] = []
    unknown: list[str] = []

    for model in sorted(all_models):
        relation = model_relations.get(model)
        if relation is None:
            # missing_relation_findings already reports this model; nothing
            # about its grain belongs in a second, unrelated finding class.
            continue
        matches = match_identifier(relation, live_identifiers)
        if len(matches) != 1:
            ambiguous.append(model)
            continue
        identifier = matches[0]

        candidates = declared.get(model)
        if candidates:
            model_findings, was_unchecked = _declared_grain_findings(
                adapter, model, identifier, candidates, min_rows
            )
            findings.extend(model_findings)
            if was_unchecked:
                unchecked.append(model)
            continue

        finding, reason = _heuristic_candidate(adapter, model, identifier, min_rows)
        if finding is not None:
            findings.append(finding)
        elif reason == "unknown":
            unknown.append(model)
        elif reason == "unsupported":
            unchecked.append(model)

    notes: list[str] = []
    if ambiguous:
        notes.append(
            "the grain was not checked for "
            + ", ".join(sorted(ambiguous))
            + ": no single relation in the warehouse matched"
        )
    if unchecked:
        notes.append(
            "the grain could not be measured for "
            + ", ".join(sorted(unchecked))
            + ": the connector could not run the distinct-count scan this needs"
        )
    if unknown:
        notes.append(
            "the grain could not be determined for "
            + ", ".join(sorted(unknown))
            + ": no declared unique test, no declared primary entity, and no "
            "id-shaped column was found"
        )
    return findings, notes


# --- row population: a model against the relation it is built from -------------


@dataclass(frozen=True)
class RowPopulationCheck:
    """One model lined up against its driving parent, ready to be counted.

    Everything static about the comparison is settled here, before any count is
    read, so the caller can decide what it can afford to measure while knowing
    exactly what each measurement would buy. ``reducers`` and ``multiplies``
    carry the SQL's own explanation for a row-count difference: a check holding
    either is not a defect waiting for a number, it is a model that was written
    to hold a different number of rows than its parent.
    """

    model: str
    relation: str
    parent: str
    parent_relation: str
    joins: tuple[str, ...] = ()
    join_keys: tuple[str, ...] = ()
    reducers: tuple[str, ...] = ()
    multiplies: bool = False

    @property
    def relations(self) -> tuple[str, str]:
        """Both sides, as the keys a caller's count map must carry."""

        return (self.relation.lower(), self.parent_relation.lower())


def _manifest_sources(project_dir: Path) -> dict[str, dict]:
    path = project_dir / "target" / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("sources", {})


def _table_name(node) -> str:
    """A parsed table reference as a plain dotted name, quoting removed.

    Built from the node's own parts rather than from its rendered SQL, because
    the rendering carries whatever quoting the dialect uses and the manifest's
    relation names carry their own.
    """

    parts = [str(part) for part in (node.catalog, node.db, node.name) if part]
    return ".".join(parts)


def _cte_name(node, cte_scopes: dict) -> str | None:
    """``node``'s own CTE name, if it is one -- and only when it is written
    unqualified (``orders``, never ``main.orders`` or ``db.main.orders``).

    A CTE has no schema of its own to be qualified with; a reference that
    carries one is a real, physical table that merely shares a short name
    with a CTE the query also declares (``with orders as (select * from
    main.orders) ...``), and matching it against ``cte_scopes`` by bare name
    alone would treat that physical table as a self-reference back to the
    CTE itself, exactly the mistake this guards against.
    """

    from sqlglot import expressions as exp

    if not isinstance(node, exp.Table) or node.catalog or node.db:
        return None
    name = node.name.lower()
    return name if name in cte_scopes else None


def _through_ctes(node, cte_scopes: dict, sql_shape, seen: set[str] | None = None):
    """Follow a relation through this query's CTEs to the table behind it.

    A compiled dbt model joins CTEs, not tables: the join in the final select
    reads ``lines``, which reads ``stg_order_items``. Naming the CTE in a
    finding would hand the reader an internal alias they cannot look up, so
    every relation dex reports is resolved to the relation it stands for.
    """

    from sqlglot import expressions as exp

    seen = seen or set()
    if isinstance(node, exp.Subquery):
        return _through_ctes(
            sql_shape.from_relation(node.this)
            if isinstance(node.this, exp.Select)
            else None,
            cte_scopes,
            sql_shape,
            seen,
        )
    if not isinstance(node, exp.Table):
        return None
    name = node.name.lower()
    if name not in cte_scopes or name in seen:
        return node if name not in cte_scopes else None
    seen.add(name)
    return _through_ctes(
        sql_shape.from_relation(cte_scopes[name]), cte_scopes, sql_shape, seen
    )


def _join_label(join, resolved, name_of) -> str:
    """One join named the way a reader would name it: how it joins, and what to.

    The project's own name for the joined relation where there is one, since
    that is what the reader will go and open; the physical relation otherwise.
    The ON condition is deliberately not here, because it travels separately as
    the join keys, which is the part that identifies a wrong grain.
    """

    if resolved is None:
        return f"{join.side} join to a subquery"
    return f"{join.side} join to '{name_of(_table_name(resolved))[1]}'"


def _driving_parent(tree, sql_shape):
    """Walk from the outermost select down to the physical relation it drives off.

    dbt models are written as chains of CTEs, so the FROM of the final select
    almost never names a table: it names the last CTE, which names the one
    before it. Following that chain is what makes the parent this reports the
    relation a person would call the model's source, rather than an internal
    name that means nothing outside the file.

    Everything the walk passes through counts, not only where it ends. A filter
    three CTEs down explains the model's row count just as well as one in the
    final select, and a join anywhere on the path can fan it out, so the
    reducers and joins accumulate the whole way.

    Returns ``(table, reducers, joins, multiplies, reason)``: ``table`` is None
    when the chain cannot be followed to one relation, and ``reason`` then says
    why in words a caller can put in a note.
    """

    from sqlglot import expressions as exp

    if sql_shape.SET_OPERATIONS and isinstance(tree, sql_shape.SET_OPERATIONS):
        return (
            None,
            [],
            [],
            False,
            "it is a set operation, so it has no single parent",
            {},
        )
    if not isinstance(tree, exp.Select):
        return None, [], [], False, "its compiled SQL is not a SELECT", {}

    cte_scopes = {
        name: scope
        for name, scope in sql_shape.scopes(tree).items()
        if name != sql_shape.MAIN_SCOPE
    }
    reducers: list[str] = []
    found_joins: list = []
    multiplies = sql_shape.multiplies_rows(tree)

    current = tree
    visited: set[str] = set()
    while True:
        reducers.extend(sql_shape.reducing_clauses(current))
        found_joins.extend(sql_shape.joins(current))
        relation = sql_shape.from_relation(current)
        if relation is None:
            return (
                None,
                reducers,
                found_joins,
                multiplies,
                "it selects from nothing",
                cte_scopes,
            )
        if isinstance(relation, exp.Subquery):
            inner = relation.this
            if sql_shape.SET_OPERATIONS and isinstance(inner, sql_shape.SET_OPERATIONS):
                return (
                    None,
                    reducers,
                    found_joins,
                    multiplies,
                    "it is driven by a set operation, so it has no single parent",
                    cte_scopes,
                )
            if not isinstance(inner, exp.Select):
                return (
                    None,
                    reducers,
                    found_joins,
                    multiplies,
                    "its driving relation is not a table or a subquery dex can follow",
                    cte_scopes,
                )
            cte_scopes.update(
                {
                    name: scope
                    for name, scope in sql_shape.scopes(inner).items()
                    if name != sql_shape.MAIN_SCOPE
                }
            )
            current = inner
            continue
        if not isinstance(relation, exp.Table):
            return (
                None,
                reducers,
                found_joins,
                multiplies,
                "its driving relation is not a table or a subquery dex can follow",
                cte_scopes,
            )
        name = relation.name.lower()
        if name in cte_scopes:
            if name in visited:  # pragma: no cover - a cycle needs invalid SQL
                return (
                    None,
                    reducers,
                    found_joins,
                    multiplies,
                    "its CTE chain refers back to itself",
                    cte_scopes,
                )
            visited.add(name)
            current = cte_scopes[name]
            continue
        return relation, reducers, found_joins, multiplies, None, cte_scopes


def row_population_plan(
    project_dir: Path,
    dialect: str,
    *,
    scope: set[str] | None = None,
) -> tuple[list[RowPopulationCheck], list[str]]:
    """Line every buildable model up against the relation it is built from.

    Free and connectionless: the compiled SQL and the relation names are both
    already in ``target/manifest.json``, so this settles the whole static half
    of row population before anything decides what a count would cost.

    A model this cannot line up is named in the notes rather than dropped. An
    unfollowable chain and a clean model are the same empty result otherwise,
    and only one of them means the model was checked.

    ``scope`` narrows to a set of model names, which is what a caller verifying
    one build rather than a whole project has: the notes narrow with it, so a
    model outside the scope is neither checked nor reported as unchecked. Both
    halves matter, because a note naming models this caller never asked about
    reads as a gap in the answer it did ask for.
    """

    import sqlglot

    from .. import sql_shape

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return (
            [],
            [
                "no compiled manifest found; run `dbt compile` or `dbt build` "
                "for row-population findings"
            ],
        )

    relations: dict[str, str] = {}
    for node in list(nodes.values()) + list(_manifest_sources(project_dir).values()):
        if not isinstance(node, dict):
            continue
        relation = node.get("relation_name")
        name = node.get("name")
        if isinstance(relation, str) and relation and isinstance(name, str):
            relations[strip_relation_quoting(relation)] = name

    known = sorted(relations)

    def name_of(relation: str) -> tuple[str, str]:
        """A relation from the SQL as ``(physical, what to call it)``.

        Compiled SQL does not always spell a relation the way the manifest
        does, so the physical name is settled through the same suffix-tolerant
        match the no-relation check uses; an ambiguous one keeps what the SQL
        said rather than guessing between two candidates.
        """

        matched = match_identifier(relation, known)
        resolved = matched[0] if len(matched) == 1 else relation
        return resolved, relations.get(resolved, resolved)

    checks: list[RowPopulationCheck] = []
    notes: list[str] = []
    incremental: list[str] = []
    unfollowable: list[str] = []
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = node.get("name")
        relation = node.get("relation_name")
        code = node.get("compiled_code")
        if not (isinstance(name, str) and name):
            continue
        if scope is not None and name not in scope:
            continue
        # An ephemeral model compiles with no relation of its own and is inlined
        # into whatever reads it, so it has no row count to compare; a model
        # with no compiled SQL was never compiled and says nothing either.
        if not (isinstance(relation, str) and relation):
            continue
        if not (isinstance(code, str) and code.strip()):
            continue
        config = node.get("config")
        materialized = (
            str(config.get("materialized", "")).lower()
            if isinstance(config, dict)
            else ""
        )
        if materialized in _UNCOMPARABLE_MATERIALIZATIONS:
            incremental.append(name)
            continue

        try:
            tree = sqlglot.parse_one(code, read=dialect)
        except Exception:
            unfollowable.append(f"{name} (dex could not parse its compiled SQL)")
            continue
        if tree is None:  # pragma: no cover - an empty parse needs empty SQL
            continue
        table, reducers, found_joins, multiplies, reason, cte_scopes = _driving_parent(
            tree, sql_shape
        )
        if table is None:
            unfollowable.append(f"{name} ({reason})")
            continue

        parent_relation, parent = name_of(_table_name(table))
        checks.append(
            RowPopulationCheck(
                model=name,
                relation=strip_relation_quoting(relation),
                parent=parent,
                parent_relation=parent_relation,
                joins=tuple(
                    _join_label(
                        join,
                        _through_ctes(join.relation, cte_scopes, sql_shape),
                        name_of,
                    )
                    for join in found_joins
                ),
                join_keys=tuple(
                    key
                    for join in found_joins
                    for key in sql_shape.equality_columns(join.on)
                ),
                reducers=tuple(dict.fromkeys(reducers)),
                multiplies=multiplies,
            )
        )

    if incremental:
        notes.append(
            f"row population was not compared for {', '.join(sorted(incremental))}: "
            "an incremental model holds what previous runs loaded into it, so its "
            "row count is not a function of its parent's"
        )
    if unfollowable:
        notes.append(
            "no single driving parent could be identified for "
            + ", ".join(sorted(unfollowable))
        )
    return checks, notes


def row_population_findings(
    checks: list[RowPopulationCheck],
    counts: dict[str, int],
    counted: set[str],
    absent: set[str] | None = None,
    deferred: set[str] | None = None,
) -> tuple[list[DriftFinding], list[str]]:
    """Row loss and fanout, judged from the counts the caller could get.

    ``counts`` is keyed by relation, lowered, and ``counted`` names the ones
    measured on this run rather than read from the catalog; a verdict resting on
    a catalog estimate is reported with ``exact`` False rather than withheld,
    the same way the volume axis reports one.

    Conservative by construction, because the alternative is worse than silence.
    A model with a filter or an aggregate has an explicit reason to hold fewer
    rows than its parent, and there is no way to bound from static SQL how many
    rows a filter "should" remove, so those are never reported for loss at all.
    A detector that fires on every aggregate gets switched off, and then it
    catches nothing.
    """

    findings: list[DriftFinding] = []
    uncomparable: list[str] = []
    unbuilt: list[str] = []
    absent, deferred = absent or set(), deferred or set()
    for check in checks:
        relation, parent_relation = check.relations
        # Silence where something else in the same envelope already explained
        # it: a model with no relation is reported under its own code, and a
        # count nobody has paid for yet is named beside the offer that prices
        # it. Saying it twice in two wordings reads as two problems.
        if relation in absent or {relation, parent_relation} & deferred:
            continue
        if parent_relation in absent:
            unbuilt.append(f"{check.model} (nothing built at {check.parent_relation})")
            continue
        rows, parent_rows = counts.get(relation), counts.get(parent_relation)
        if rows is None or parent_rows is None:
            missing = check.relation if rows is None else check.parent_relation
            uncomparable.append(f"{check.model} (no row count for {missing})")
            continue
        if parent_rows == 0:
            continue
        exact = relation in counted and parent_relation in counted
        fraction = (rows - parent_rows) / parent_rows
        if abs(fraction) < _ROW_DIFFERENCE_REPORT_FRACTION:
            continue

        against = (
            f"{rows} rows against its driving parent '{check.parent}''s "
            f"{parent_rows} ({fraction:+.0%})"
        )
        data = {
            "driving_parent": check.parent,
            "driving_parent_relation": check.parent_relation,
            "row_count": rows,
            "parent_row_count": parent_rows,
            "change_fraction": round(fraction, 4),
        }
        if check.joins:
            data["joins"] = list(check.joins)
        if check.join_keys:
            data["join_keys"] = list(check.join_keys)

        if fraction < 0:
            if check.reducers:
                continue
            cause = (
                f"; the {check.joins[0]} is the only thing in it that can drop rows"
                if check.joins
                else ""
            )
            findings.append(
                DriftFinding(
                    axis="row_population",
                    code="row_loss",
                    identifier=check.model,
                    severity=(
                        "high" if -fraction >= _ROW_LOSS_HIGH_FRACTION else "medium"
                    ),
                    detail=(
                        f"'{check.model}' has {against}, and nothing in its SQL "
                        f"filters, aggregates, or de-duplicates{cause}"
                    ),
                    exact=exact,
                    data=data,
                )
            )
            continue

        if check.multiplies or not check.joins:
            continue
        key = (
            f" on {', '.join(check.join_keys)}"
            if check.join_keys
            else " (dex could not read the join key)"
        )
        findings.append(
            DriftFinding(
                axis="row_population",
                code="row_fanout",
                identifier=check.model,
                severity=(
                    "high"
                    if rows >= parent_rows * _ROW_FANOUT_HIGH_FACTOR
                    else "medium"
                ),
                detail=(
                    f"'{check.model}' has {against}: the {check.joins[0]}{key} "
                    "matches more than one row per parent row"
                ),
                exact=exact,
                data=data,
            )
        )

    notes = []
    if unbuilt:
        notes.append(
            "row population was not compared for "
            + ", ".join(sorted(unbuilt))
            + ": the driving relation has no object in the warehouse"
        )
    if uncomparable:
        notes.append(
            "row population was not compared for "
            + ", ".join(sorted(uncomparable))
            + " (the warehouse maintains no count for this kind of object, and "
            "counting it was not free)"
        )
    return findings, notes


# --- join contract: a model's own joins against measured value overlap ---------

#: A verified join's orphan fraction at or above this is a complete or
#: near-complete miss: the join predicate connects to almost nothing on the
#: other side. Deliberately the same value `explore relationships --verify`
#: uses for its own catastrophic-orphan-rate finding (#207's
#: `_ORPHAN_FINDING_THRESHOLD`), because it is the same judgement -- "this
#: join found almost nothing" -- asked of a join the project wrote instead of
#: one dex inferred. Kept as its own constant rather than imported, the same
#: reasoning `_ROW_LOSS_HIGH_FRACTION` above already gives: the two axes are
#: free to diverge on field evidence without dragging each other along.
_JOIN_ZERO_OVERLAP_THRESHOLD = 0.9

#: The floor a join's orphan fraction must clear to be worth reporting at
#: all, below `_JOIN_ZERO_OVERLAP_THRESHOLD`. A left join whose right side is
#: legitimately sparse sits here: real orphans, honestly measured, but not
#: the catastrophic case `_JOIN_ZERO_OVERLAP_THRESHOLD` names. Below this, a
#: join is healthy and reports nothing.
_JOIN_ORPHANS_THRESHOLD = 0.2


@dataclass(frozen=True)
class JoinCandidate:
    """One equality join a model's compiled SQL writes, both sides resolved
    past their CTE aliases to the physical relation each reads.

    ``from_relation``/``from_columns`` is the side already in scope when the
    join is written (the FROM relation, or an earlier join in the same
    chain) -- the "child" side whose key is expected to find a match.
    ``to_relation``/``to_columns`` is the relation this join introduces --
    the "parent" side the key is checked against. This is the same from/to
    split :class:`~exmergo_dex_core.cache.Relationship` already uses, so a
    candidate here converts to one with no re-derivation. The columns are
    tuples, not one each: a join whose ON equates more than one column pair
    between the same two relations (``a.order_id = b.order_id AND a.line_no
    = b.line_no``) is one composite key, not two independent single-column
    ones -- probing them separately would measure the overlap of each column
    alone, a different and generally wrong question from whether the
    combination matches.
    """

    from_relation: str
    from_columns: tuple[str, ...]
    to_relation: str
    to_columns: tuple[str, ...]
    side: str


def _scope_relations(select, sql_shape) -> dict[str, object]:
    """One scope's own alias -> raw, unresolved relation map: its FROM plus
    every one of its own JOINs. The same map :func:`_model_joins` builds for
    the outer scope it is walking, needed again here because resolving a
    column projected from *inside* a CTE has to know that CTE's own
    relations too -- a projection can read from any of them, not only the
    CTE's FROM (see :func:`_resolve_within_scope`)."""

    relations: dict[str, object] = {}
    from_relation = sql_shape.from_relation(select)
    if from_relation is not None:
        relations[from_relation.alias_or_name.lower()] = from_relation
    for join in sql_shape.joins(select):
        relations[join.relation.alias_or_name.lower()] = join.relation
    return relations


def _qualified_source(column, scope_relations: dict[str, object]):
    """Which of ``scope_relations`` a column reference names: its own table
    qualifier, looked up directly, when it has one; the sole relation in
    scope when it has none and there is only one candidate (an unqualified
    column can only mean that one); ``None`` when it has none and more than
    one relation could have written it -- unresolvable without a schema
    read dex does not have at parse time, not a guess to make."""

    qualifier = (column.table or "").lower()
    if qualifier:
        return scope_relations.get(qualifier)
    if len(scope_relations) == 1:
        return next(iter(scope_relations.values()))
    return None


def _resolve_within_scope(select, column_name: str, cte_scopes: dict, sql_shape, seen):
    """Find ``column_name``'s own projection inside ``select``, and recurse
    into *whichever of that scope's own relations it actually reads from*
    -- its FROM, or one of its own JOINs, determined by
    :func:`_qualified_source` exactly the way an outer join's own column
    resolution is. A CTE that projects a joined relation's column
    (``select o.order_id, c.region_id from orders o join customers c on
    ...``) must resolve ``region_id`` through ``customers``, not blindly
    through the CTE's FROM (``orders``, which does not have it) -- the
    fix this function exists for.

    A bare, unqualified ``SELECT *`` passes every name through unchanged,
    but only when this scope has exactly one relation to have come from;
    with more than one in scope, which table a star's column belongs to is
    the same schema question a qualifier's absence already cannot answer,
    so it is left unresolved rather than assumed.

    Refuses -- rather than resolving through -- any scope that reduces rows
    (:func:`~..sql_shape.reducing_clauses`: a WHERE, a HAVING, a GROUP BY, a
    DISTINCT, a LIMIT). ``select * from customers where active = true``
    projects the same column names the physical ``customers`` table has,
    but not the same rows: probing a join against the whole table when the
    project's own SQL joins against only the active subset can report a
    join healthy that has zero real overlap once the filter is honored. The
    join is skipped instead, not measured against semantics it does not
    have.
    """

    from sqlglot import expressions as exp

    if sql_shape.reducing_clauses(select):
        return None

    scope_relations = _scope_relations(select, sql_shape)
    saw_unqualified_star = False
    for proj in select.expressions:
        if isinstance(proj, exp.Star):
            saw_unqualified_star = True
            continue
        if isinstance(proj, exp.Alias):
            if proj.alias.lower() != column_name.lower():
                continue
            inner = proj.this
            if not isinstance(inner, exp.Column):
                return None
            source = _qualified_source(inner, scope_relations)
            return (
                None
                if source is None
                else _resolve_join_column(
                    source, inner.name, cte_scopes, sql_shape, seen
                )
            )
        if (
            isinstance(proj, exp.Column)
            and proj.alias_or_name.lower() == column_name.lower()
        ):
            source = _qualified_source(proj, scope_relations)
            return (
                None
                if source is None
                else _resolve_join_column(
                    source, proj.name, cte_scopes, sql_shape, seen
                )
            )
    if saw_unqualified_star and len(scope_relations) == 1:
        (only_source,) = scope_relations.values()
        return _resolve_join_column(
            only_source, column_name, cte_scopes, sql_shape, seen
        )
    return None


def _resolve_join_column(
    node, column_name: str, cte_scopes: dict, sql_shape, seen=None
):
    """Follow one column from ``node`` (the raw relation a join predicate
    qualified it with) through as many CTE or inline-subquery hops as it
    takes to the physical relation and column it actually reads.

    A compiled dbt model's own CTE can rename a column in its SELECT list
    (``select customer_id as cid from orders``), or project one from a
    relation it joins rather than its FROM; a join further downstream then
    writes the CTE's own name for it (``cid``), which the physical table
    underneath may not have, or may have under a different name. Probed as
    written, that sends the adapter a column the relation does not carry,
    and the probe fails outright. Resolved here instead, one hop at a time
    via :func:`_resolve_within_scope`; the first hop that computes the
    column, or that a star cannot disambiguate, returns ``None``, and the
    caller treats the whole pair as unresolvable rather than guessing.

    ``seen`` guards against a CTE cycle (a cycle needs invalid SQL, but the
    guard costs nothing); an inline subquery needs no such guard, since
    descending into one is always a strictly smaller, genuinely different
    node -- there is no way back to one already visited.

    Returns ``(physical_relation, physical_column_name)``, or ``None``.
    """

    from sqlglot import expressions as exp

    seen = seen if seen is not None else set()

    if isinstance(node, exp.Subquery):
        inner = node.this
        if not isinstance(inner, exp.Select):
            return None
        return _resolve_within_scope(inner, column_name, cte_scopes, sql_shape, seen)

    if not isinstance(node, exp.Table):
        return None
    cte_name = _cte_name(node, cte_scopes)
    if cte_name is None:
        return node, column_name
    if cte_name in seen:  # pragma: no cover - a cycle needs invalid SQL
        return None
    definition = cte_scopes[cte_name]
    if not isinstance(definition, exp.Select):
        # A set-operation CTE (a UNION and friends): its output column has
        # no single physical source to resolve to -- each branch can read
        # from an entirely different relation for the "same" column -- so
        # this is unresolvable rather than a guess at which branch answers.
        return None
    return _resolve_within_scope(
        definition, column_name, cte_scopes, sql_shape, seen | {cte_name}
    )


def _join_equality_pairs(join, in_scope_relations: list, sql_shape):
    """The column-to-column equalities one join's condition requires
    together, however it wrote them -- an ``ON``, or ``USING`` (each named
    column implies ``left.col = right.col`` with no explicit qualifier on
    either side, so this builds that pair itself).

    A ``USING`` join is only resolved when exactly one relation is already
    in scope: with a single preceding relation, that is unambiguously the
    "left" side the using-column must belong to, but with two or more (a
    third-plus join in a chain, ``... JOIN b USING (x) JOIN c USING (y)``
    where ``y`` was ``b``'s column, not the original FROM's), which one
    actually carries it is a schema question dex cannot answer from the SQL
    text alone, so guessing the original FROM relation the way an earlier
    version of this function did is exactly the bug this guards against.

    Returns ``(pairs, skip_reason)``: ``skip_reason`` is ``None`` with empty
    ``pairs`` for a join that carries neither (a CROSS JOIN, an UNNEST --
    not a candidate for this check at all, not something to note either),
    and a prose reason with empty ``pairs`` for one that carries a real
    condition this could not turn into a composite key (see
    :func:`~..sql_shape.conjunctive_equality_pairs` for why "some of the
    equalities, dropping the rest" is never an acceptable partial answer).
    """

    from sqlglot import expressions as exp

    if join.using:
        if len(in_scope_relations) != 1:
            reason = (
                "a USING join with nothing already in scope to pair against"
                if not in_scope_relations
                else "a USING join with more than one relation already in scope: "
                "which one carries the column cannot be determined from the "
                "SQL text alone"
            )
            return [], reason
        (from_relation,) = in_scope_relations
        from_alias = from_relation.alias_or_name
        to_alias = join.relation.alias_or_name
        return [
            (exp.column(col, table=from_alias), exp.column(col, table=to_alias))
            for col in join.using
        ], None
    if join.on is None:
        return [], None  # a CROSS JOIN/UNNEST: not this check's business
    pairs = sql_shape.conjunctive_equality_pairs(join.on)
    if pairs is None:
        return [], "no conjunctive column-to-column equality"
    return pairs, None


def _select_branches(tree, sql_shape) -> list:
    """Every leaf SELECT under a top-level set operation, flattened.

    ``UNION``/``UNION ALL``/``INTERSECT``/``EXCEPT`` parse as a left-deep
    binary tree (``A UNION B UNION C`` is ``Union(this=Union(this=A,
    expression=B), expression=C)``), so a caller that only looks at the
    outermost node's own FROM/JOINs (as :func:`_driving_parent` does, for
    its own narrower purpose of finding one single driving relation) never
    sees any branch's own joins at all -- a real join a branch writes,
    orphans and all, is invisible from the top. Flattened here instead, so
    every branch is walked for its own joins independently. A single
    top-level SELECT is already its own one-element answer; anything else
    (no FROM at all, a VALUES list) has no join to find and returns empty.
    """

    from sqlglot import expressions as exp

    if isinstance(tree, exp.Select):
        return [tree]
    if sql_shape.SET_OPERATIONS and isinstance(tree, sql_shape.SET_OPERATIONS):
        left = _select_branches(tree.this, sql_shape) if tree.this is not None else []
        right_side = tree.args.get("expression")
        right = (
            _select_branches(right_side, sql_shape) if right_side is not None else []
        )
        return left + right
    return []


def _model_joins(tree, sql_shape) -> tuple[list[JoinCandidate], list[str]]:
    """Every equality join reachable from a model's compiled SQL (#228),
    both sides resolved past their CTE aliases to the physical relation and
    column each reads.

    Visits every scope the query reaches, not only the FROM chain a single
    driving parent walks (:func:`_driving_parent`'s own narrower purpose):
    a join naming a CTE that itself joins two other relations has that inner
    join visited too, an inline subquery (``select * from (select ... join
    ...) x``) is descended into the same way, queued the moment either kind
    is named in a FROM or a JOIN, and a top-level set operation
    (``UNION``/``UNION ALL``/``INTERSECT``/``EXCEPT``) has every one of its
    branches walked independently (:func:`_select_branches`), sharing the
    one ``WITH`` clause a compound statement's CTEs are scoped to as a
    whole. A CTE or an inline subquery whose own body is a set operation
    (rather than a plain SELECT) gets the same treatment: every branch is
    still walked for its own joins (a CTE naming a real, written join in one
    of its branches has that join checked, not silently skipped because the
    CTE as a whole is a UNION), even though a *column* reference through
    such a CTE is unresolvable (:func:`_resolve_join_column`) -- finding a
    join and resolving a column through one are different questions, and a
    UNION only makes the second one ill-posed, not the first. A visited set
    (by CTE name) keeps a CTE referenced from more than one place from being
    processed twice; an inline subquery needs no such guard, since it can
    never be reached a second time by construction.

    Returns ``(candidates, skipped)``. ``skipped`` names, in prose, every
    join dex saw carrying a real condition it could not turn into a
    candidate -- a non-conjunctive-equality condition (an OR, a range
    predicate), a ``USING`` join ambiguous about which relation it pairs
    against, or a column (a CTE's own computed expression, or one a star
    could not disambiguate) that could not be resolved to a physical one --
    so a non-equality join is stated rather than silently dropped (#228's
    acceptance). If *any* equality pair the join's own condition requires
    together is unresolvable, the whole join is skipped rather than probed
    on the pairs that did resolve: a composite key with one component
    guessed away is a narrower, wrong question, the same reasoning
    :func:`~..sql_shape.conjunctive_equality_pairs` already applies to a
    non-equality conjunct. A join with neither an ON nor a USING (a CROSS
    JOIN, an UNNEST) is not a candidate for this check by construction and
    is excluded without a note, the same as row population's own fanout
    check already treats it (`sql_shape.ROW_MULTIPLIERS`).
    """

    from sqlglot import expressions as exp

    branches = _select_branches(tree, sql_shape)
    if not branches:
        return [], []

    # A local scan rather than sql_shape.scopes(): that function is typed
    # (and used by its other callers, row population and transform plan) as
    # SELECT-only CTEs, so a CTE whose own body is a set operation is
    # dropped from its result entirely -- exactly the join-finding gap this
    # fixes. #228's own traversal needs the raw body, Select or set
    # operation alike, so it is read directly here instead of widening a
    # shared primitive's contract for every other caller's sake.
    with_clause = tree.args.get("with_") or tree.args.get("with")
    cte_scopes: dict[str, exp.Expression] = (
        {cte.alias_or_name.lower(): cte.this for cte in with_clause.expressions}
        if with_clause is not None
        else {}
    )
    candidates: list[JoinCandidate] = []
    skipped: list[str] = []
    visited_ctes: set[str] = set()
    queue: list = list(branches)

    def _enqueue(relation) -> None:
        if isinstance(relation, exp.Subquery):
            queue.extend(_select_branches(relation.this, sql_shape))
            return
        cte_name = _cte_name(relation, cte_scopes)
        if cte_name is not None and cte_name not in visited_ctes:
            visited_ctes.add(cte_name)
            queue.extend(_select_branches(cte_scopes[cte_name], sql_shape))

    while queue:
        current = queue.pop()
        from_relation = sql_shape.from_relation(current)
        # alias -> the *raw*, unresolved relation node, so a column's own
        # resolution (_resolve_join_column) can walk the same CTE/subquery
        # chain a relation's own resolution does, rather than trusting a
        # column name survived every hop unchanged.
        alias_map: dict[str, exp.Expression] = {}
        if from_relation is not None:
            alias_map[from_relation.alias_or_name.lower()] = from_relation
            _enqueue(from_relation)

        for join in sql_shape.joins(current):
            # Every relation already in scope *before* this join's own is
            # added -- what a USING join's ambiguity check needs, and what
            # an ON join's alias lookup already used implicitly.
            in_scope_relations = list(alias_map.values())
            join_alias = join.relation.alias_or_name.lower()
            alias_map[join_alias] = join.relation
            _enqueue(join.relation)

            pairs, skip_reason = _join_equality_pairs(
                join, in_scope_relations, sql_shape
            )
            if skip_reason is not None:
                skipped.append(f"{join.label}: {skip_reason}")
                continue
            if not pairs:
                continue  # a CROSS JOIN/UNNEST: not this check's business

            # Grouped by (from relation, to relation): every equality pair
            # between the same two relations is one composite key, not one
            # candidate per pair (see JoinCandidate's own docstring).
            grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
            unresolved = False
            for left, right in pairs:
                left_relation = alias_map.get((left.table or "").lower())
                right_relation = alias_map.get((right.table or "").lower())
                if left_relation is None or right_relation is None:
                    unresolved = True
                    break
                left_resolved = _resolve_join_column(
                    left_relation, left.name, cte_scopes, sql_shape
                )
                right_resolved = _resolve_join_column(
                    right_relation, right.name, cte_scopes, sql_shape
                )
                if left_resolved is None or right_resolved is None:
                    unresolved = True
                    break
                left_table, left_col = left_resolved
                right_table, right_col = right_resolved
                if (left.table or "").lower() == join_alias:
                    to_table, to_col = left_table, left_col
                    from_table, from_col = right_table, right_col
                elif (right.table or "").lower() == join_alias:
                    to_table, to_col = right_table, right_col
                    from_table, from_col = left_table, left_col
                else:
                    unresolved = True
                    break
                from_name, to_name = _table_name(from_table), _table_name(to_table)
                grouped.setdefault((from_name, to_name), []).append((from_col, to_col))
            if unresolved:
                # Any unresolvable component drops the whole join, not just
                # that pair: a composite key probed on only the pairs that
                # happened to resolve is a narrower question than the join
                # itself asks, and can read a genuinely broken join as
                # healthy (#228's own review).
                skipped.append(
                    f"{join.label}: a join column could not be resolved to a "
                    "physical relation and column"
                )
                continue
            for (from_name, to_name), column_pairs in grouped.items():
                from_cols = tuple(c for c, _ in column_pairs)
                to_cols = tuple(c for _, c in column_pairs)
                if from_name == to_name and from_cols == to_cols:
                    # The literal same key read twice off the same relation
                    # (t.col = t.col): every row matches itself, always --
                    # nothing to orphan-check. A self-join on *different*
                    # columns (employees.manager_id = managers.id, both
                    # reading `employees`) is a real, checkable join and is
                    # not excluded here.
                    continue
                candidates.append(
                    JoinCandidate(
                        from_relation=from_name,
                        from_columns=from_cols,
                        to_relation=to_name,
                        to_columns=to_cols,
                        side=join.side,
                    )
                )

    return candidates, skipped


def join_contract_plan(
    project_dir: Path, dialect: str, *, scope: set[str] | None = None
) -> tuple[dict[str, list[JoinCandidate]], list[str]]:
    """Every selected model's own equality joins, read from its compiled SQL
    (#228). Free: parses artifacts already on disk, opens no connection.

    ``dialect`` is the connector's own dialect, the same parameter
    :func:`row_population_plan` already takes for the same reason: compiled
    SQL is written in the project's actual warehouse's dialect, and parsing
    it as DuckDB regardless would misread dialect-specific syntax on every
    other connector -- silently wrong candidates at best, a parse failure
    that drops the model's joins entirely at worst.

    ``scope`` is compared lowercase, the same convention
    :func:`column_contract_plan` and :func:`grain_plan` follow.

    Returns ``(candidates, notes)``. A model absent from ``candidates``
    either declares no joins at all, or every join it writes was skipped
    (named in ``notes``, prefixed by the model); the caller cannot tell
    those apart from this return alone, which is fine, since neither is a
    finding -- only a measured orphan fraction is.
    """

    from .. import sql_shape

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return (
            {},
            [
                "no compiled manifest found; run `dbt compile` or `dbt build` "
                "for join-contract findings"
            ],
        )

    import sqlglot

    by_model: dict[str, list[JoinCandidate]] = {}
    notes: list[str] = []
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = node.get("name")
        if not (isinstance(name, str) and name):
            continue
        if scope is not None and name.lower() not in scope:
            continue
        sql = node.get("compiled_code")
        if not isinstance(sql, str) or not sql.strip():
            continue
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: S112 -- unparseable compiled SQL, not our defect
            continue
        candidates, skipped = _model_joins(tree, sql_shape)
        if candidates:
            by_model[name] = candidates
        notes.extend(f"{name}: {reason}" for reason in skipped)
    return by_model, notes


def _resolve_join_relationships(
    candidates_by_model: dict[str, list[JoinCandidate]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
):
    """Every candidate turned into a :class:`~..cache.Relationship`, both
    sides resolved to exactly one live identifier. Pure and adapter-free:
    ``live_identifiers`` is already-read metadata, matching every other
    resolution step in this module. Returns ``(pairs, unresolved)``, where
    ``pairs`` keeps each relationship's owning model alongside it (a
    ``Relationship`` itself carries no model name) and ``unresolved`` names
    the models a join could not be resolved for."""

    from ..cache import Relationship, RelationshipKind

    pairs: list[tuple[str, Relationship]] = []
    unresolved: list[str] = []
    for model, candidates in candidates_by_model.items():
        for candidate in candidates:
            from_matches = match_identifier(candidate.from_relation, live_identifiers)
            to_matches = match_identifier(candidate.to_relation, live_identifiers)
            if len(from_matches) != 1 or len(to_matches) != 1:
                unresolved.append(model)
                continue
            pairs.append(
                (
                    model,
                    Relationship(
                        from_dataset=from_matches[0],
                        from_columns=list(candidate.from_columns),
                        to_dataset=to_matches[0],
                        to_columns=list(candidate.to_columns),
                        kind=RelationshipKind.DECLARED,
                    ),
                )
            )
    return pairs, unresolved


def join_probe_cost(adapter, relationships: list) -> tuple[float, int]:
    """``(estimate, probe_count)`` for measuring every relationship here dex
    has not yet measured, without running the probe.

    Pure pricing, split out from :func:`join_contract_findings` so a caller
    combining this estimate with another axis's own (row population's row
    counts, in `maintain/commands.py`'s shared handshake) can price both
    before asking once, rather than each axis asking on its own and
    ``VerifyResult.pending_offer`` -- one slot -- silently keeping only
    whichever asked last.
    """

    from .. import command_args
    from ..explore import relationships as rel_mod

    to_probe = rel_mod.probe_candidates(relationships)
    if not to_probe or command_args.cost_gate(adapter) is None:
        return 0.0, len(to_probe)
    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0, len(to_probe)
    statements = rel_mod.probe_statements(relationships, adapter.dialect)
    return sum(estimator(stmt) for stmt in statements), len(to_probe)


def join_contract_cost(
    project_dir: Path,
    adapter,
    model_relations: dict[str, str],
    live_identifiers: list[str],
    *,
    scope: set[str] | None = None,
) -> tuple[float, int]:
    """``(estimate, probe_count)`` for #228's whole axis, end to end but
    without running anything: the plan (parse every selected model's
    compiled SQL), the resolution (each join's relations against the live
    warehouse), and the pricing (:func:`join_probe_cost`), composed into one
    call. Exists so `maintain/commands.py`'s combined handshake can price
    this axis lazily, alongside row population's own estimate, before either
    axis asks once -- see that handshake's own docstring.
    """

    candidates, _plan_notes = join_contract_plan(
        project_dir, adapter.dialect, scope=scope
    )
    pairs, _unresolved = _resolve_join_relationships(
        candidates, model_relations, live_identifiers
    )
    return join_probe_cost(adapter, [rel for _model, rel in pairs])


def _standalone_join_handshake(adapter) -> Callable[[float, int], object | None]:
    """The ask this axis makes on its own, when the caller has not supplied a
    shared one -- mirrors :func:`_standalone_handshake`'s reasoning exactly,
    for the same reason: free findings elsewhere in the envelope are already
    final, so this returns a request rather than raising."""

    from .. import command_args

    def ask(estimate: float, probe_count: int):
        return command_args.confirmation_request(
            "maintain verify",
            adapter,
            estimate,
            per_table={"(join overlap probes)": estimate},
            axes=["join_contract"],
            notes=[
                f"the estimate buys {probe_count} join overlap probe(s), which "
                "is what a broken or sparse join is judged from"
            ],
        )

    return ask


def join_contract_findings(
    adapter,
    candidates_by_model: dict[str, list[JoinCandidate]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    *,
    timeout_seconds: float = 30.0,
    handshake: Callable[[float, int], object | None] | None = None,
) -> tuple[list[DriftFinding], list[str], object | None]:
    """A model's own joins measured against real value overlap (#228).

    Reuses the exact probe `explore relationships --verify` already runs
    (:func:`~..explore.relationships.verify_relationships`) rather than
    authoring a second one: the question is identical (how many of this
    side's non-null keys have no match on the other side), only the source
    of the join differs (the project wrote this one; that command infers or
    reads a declaration). Aggregate counts only, by that function's own
    contract -- no key value ever leaves the engine.

    A join at or above `_JOIN_ZERO_OVERLAP_THRESHOLD` orphan fraction is
    `join_zero_overlap` (``high``): the join predicate connects to almost
    nothing on the other side, which is the most damaging and least visible
    defect this sweep can catch. One from `_JOIN_ORPHANS_THRESHOLD` up to
    that is `join_orphans` (``medium``): real, measured orphans -- a left
    join whose right side is legitimately sparse lands here, reported at its
    fraction rather than characterized as broken. Below `_JOIN_ORPHANS_
    THRESHOLD` a join is healthy and reports nothing.

    Returns ``(findings, notes, offer)``. ``offer`` is non-``None`` only when
    ``handshake`` deferred the probe; the caller folds it into
    ``VerifyResult.pending_offer`` the same way row population's own scan
    already does.
    """

    from ..explore import relationships as rel_mod

    pairs, unresolved = _resolve_join_relationships(
        candidates_by_model, model_relations, live_identifiers
    )
    notes: list[str] = []
    if unresolved:
        notes.append(
            "the join contract was not checked for "
            + ", ".join(sorted(set(unresolved)))
            + ": a joined relation did not match exactly one live identifier"
        )
    if not pairs:
        return [], notes, None

    relationships = [rel for _model, rel in pairs]
    estimate, probe_count = join_probe_cost(adapter, relationships)
    if probe_count == 0:
        return [], notes, None

    ask = handshake if handshake is not None else _standalone_join_handshake(adapter)
    offer = ask(estimate, probe_count)
    if offer is not None:
        notes.append(
            f"the join contract was not measured: {probe_count} join(s) need "
            "an overlap probe, offered above"
        )
        return [], notes, offer

    rel_mod.verify_relationships(
        adapter, relationships, timeout_seconds=timeout_seconds
    )

    findings: list[DriftFinding] = []
    for model, rel in pairs:
        if not rel.verified or rel.orphan_fraction is None:
            continue
        fraction = rel.orphan_fraction
        if fraction >= _JOIN_ZERO_OVERLAP_THRESHOLD:
            code, severity = "join_zero_overlap", "high"
        elif fraction >= _JOIN_ORPHANS_THRESHOLD:
            code, severity = "join_orphans", "medium"
        else:
            continue
        from_key = ", ".join(f"{rel.from_dataset}.{c}" for c in rel.from_columns)
        to_key = ", ".join(f"{rel.to_dataset}.{c}" for c in rel.to_columns)
        findings.append(
            DriftFinding(
                axis="join_contract",
                code=code,
                identifier=model,
                column=rel.from_columns[0] if len(rel.from_columns) == 1 else None,
                severity=severity,
                detail=(
                    f"'{model}' joins ({from_key}) to ({to_key}), but "
                    f"{fraction:.0%} of {rel.from_dataset} rows have no match "
                    f"in {rel.to_dataset}"
                ),
                data={
                    "model": model,
                    "from_relation": rel.from_dataset,
                    "from_columns": list(rel.from_columns),
                    "to_relation": rel.to_dataset,
                    "to_columns": list(rel.to_columns),
                    "orphan_fraction": fraction,
                },
            )
        )
    return findings, notes, None


# --- getting the counts the comparison needs -----------------------------------


def count_relations_sql(identifiers: list[str], dialect: str) -> str:
    """One statement counting every named relation, aliased by position.

    Aggregate-only by construction: ``COUNT(*)`` projects no column, so the
    statement can carry a row count and nothing else, which is what keeps a
    measurement inside the same read-only and PII guarantees the profiling
    aggregates hold. Authored once in DuckDB SQL and transpiled, the way the
    join-overlap probe is, so a connector gets its own quoting without a second
    author of the same statement.

    One statement rather than many because a metered connector prices and bills
    per query: counting twenty relations in twenty statements would pay the
    per-query floor twenty times over for work that is one scan of metadata-free
    relations either way.
    """

    import sqlglot

    def quote(identifier: str) -> str:
        return ".".join(
            '"' + part.replace('"', '""') + '"' for part in identifier.split(".")
        )

    # The only interpolation is a quoted and escaped identifier, never a value,
    # and the whole statement goes through `assert_select_only` before it runs.
    counts = ", ".join(
        f"(SELECT COUNT(*) FROM {quote(identifier)}) AS dex_rows_{index}"  # noqa: S608
        for index, identifier in enumerate(identifiers)
    )
    sql = f"SELECT {counts}"
    if dialect == "duckdb":
        return sql
    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


@dataclass(frozen=True)
class RelationCounts:
    """What could be counted, what could not, and why not.

    Returned as one value rather than a row of positionals because the four
    outcomes are read together: a relation with a count, one the warehouse has
    no object for, one deferred behind an offer, and the subset measured rather
    than estimated. A caller that sees only ``counts`` cannot tell an absent
    finding from an unaffordable one.
    """

    counts: dict[str, int]
    #: Relations measured on this run. A verdict is exact only if both of its
    #: sides are here; a catalog count is an estimate for this purpose.
    counted: set[str]
    #: Relations the warehouse has no object for at all.
    absent: set[str]
    #: Relations left uncounted because counting them is a scan nobody has
    #: agreed to yet. Named in ``notes`` and priced in ``offer``.
    deferred: set[str]
    offer: object | None
    notes: list[str]


def _standalone_handshake(adapter) -> Callable[[float, int], object | None]:
    """The ask `maintain verify` makes: a whole-command handshake, priced before
    anything on this axis has spent, returned rather than raised because the
    free findings beside it are already final."""

    from .. import command_args

    def ask(estimate: float, relation_count: int):
        return command_args.confirmation_request(
            "maintain verify",
            adapter,
            estimate,
            per_table={"(row counts)": estimate},
            axes=["row_population"],
            notes=[
                "the build-status and no-relation findings in this envelope are "
                f"final; the estimate buys {relation_count} row count(s) the "
                "warehouse keeps no metadata for, which is what row loss and "
                "fanout are judged from"
            ],
        )

    return ask


def relation_counts(
    adapter,
    wanted: list[str],
    live: list,
    timeout_seconds: float,
    *,
    handshake: Callable[[float, int], object | None] | None = None,
) -> RelationCounts:
    """The row counts a row-population verdict needs, cheapest source first.

    Three tiers, and which one answers decides both the spend and the honesty
    flag on the finding:

    - the catalog, free everywhere, but an estimate for the axis's purposes and
      absent entirely for a view, a materialized view, or an external table,
      which is exactly what dbt's default materialization produces;
    - a real ``COUNT(*)``, which on a connector with no cost gate is free, so
      every relation is counted and every verdict is exact rather than resting
      on the catalog's estimate;
    - the same count on a metered connector, which is a scan and therefore
      offered through the ordinary handshake rather than taken.

    ``handshake`` is how the caller gates that third tier, given the estimate
    and how many relations it buys; it returns a request to defer behind, or
    ``None`` to proceed. It is injected because the gate primitive differs by
    caller and nothing else does: a standalone sweep prices a whole command and
    asks up front, while a sweep folded onto a build that has already spent
    prices a phase against the reservation that build is holding. Default is
    the standalone ask, so the caller that has no opinion gets the safe one.
    """

    from .. import command_args
    from ..guards.sql_guard import assert_select_only

    identifiers = [meta.identifier for meta in live]
    by_identifier = {meta.identifier.lower(): meta for meta in live}

    resolved: dict[str, str] = {}
    for relation in wanted:
        matched = match_identifier(relation, identifiers)
        if len(matched) == 1:
            resolved[relation] = matched[0]
    absent = {relation for relation in wanted if relation not in resolved}

    free = command_args.cost_gate(adapter) is None
    counts: dict[str, int] = {}
    to_count: list[str] = []
    for relation, identifier in resolved.items():
        meta = by_identifier.get(identifier.lower())
        row_count = meta.row_count if meta is not None else None
        # On a free connector the catalog's estimate is never good enough when
        # an exact count costs nothing: DuckDB's `estimated_size` is what it
        # says it is, and a verdict on a 10% difference cannot rest on it.
        if row_count is not None and not free:
            counts[relation] = row_count
        else:
            to_count.append(relation)

    if not to_count:
        return RelationCounts(counts, set(), absent, set(), None, [])

    sql = assert_select_only(
        count_relations_sql([resolved[r] for r in to_count], adapter.dialect),
        dialect=adapter.dialect,
    )
    estimator = getattr(adapter, "query_estimate", None)
    estimate = estimator(sql) if estimator is not None and not free else 0.0
    ask = handshake if handshake is not None else _standalone_handshake(adapter)
    offer = ask(estimate, len(to_count))
    if offer is not None:
        return RelationCounts(
            counts,
            set(),
            absent,
            set(to_count),
            offer,
            [
                "row population was not judged for "
                + ", ".join(sorted(to_count))
                + ": the warehouse maintains no row count for them and counting "
                "them is a scan, offered above"
            ],
        )

    try:
        result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return RelationCounts(
            counts,
            set(),
            absent,
            set(to_count),
            None,
            [f"the row counts could not be read ({type(exc).__name__}: {exc})"],
        )
    row = dict(zip(result.columns, result.cells[0], strict=True))
    counted = set()
    for index, relation in enumerate(to_count):
        value = row.get(f"dex_rows_{index}")
        if value is not None:
            counts[relation] = int(value)
            counted.add(relation)
    return RelationCounts(counts, counted, absent, set(to_count) - counted, None, [])
