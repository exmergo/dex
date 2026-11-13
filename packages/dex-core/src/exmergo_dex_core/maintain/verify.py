"""maintain verify: a baseline-free sweep answering "is this project correct
right now", as opposed to drift's "what changed since the baseline" (#224).

Five finding classes live here (a sixth, declared facts, lives in
:mod:`.declared_facts`, sharing the grain finding below). Build-status gaps
(#225) read the compiled manifest and the last run's ``run_results.json``:
failed nodes, nodes skipped by a failed parent, models with no relation, and a
project that does not compile. Column contract (#230) compares a built
relation's actual columns against what its schema.yml declares, in both
directions, plus a type check where a type is declared. Row population (#226)
reads each model's compiled SQL to find the relation it is built *from*, and
compares the two row counts: a model holding materially fewer rows than its
driving parent, with nothing in its SQL that would account for the shortfall,
has lost rows silently, and one holding materially more has fanned out on a
join. Grain (#229) checks that a model's intended grain -- the grain its
declaration states, a declared ``unique`` test, a semantic model's declared
primary entity, or (absent all of them) a free naming heuristic -- still holds
one row per key in the built relation. Join contract (#228) reads the same
compiled SQL for every equality join a model writes, and measures the value
overlap on each: a join whose keys find almost nothing on the other side is
silently wrong in a way no dbt test catches, and is the most damaging defect
this sweep can report.

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

import contextlib
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cache import match_identifier, relation_verdict
from ..dbt_project import manifest_node_label, strip_relation_quoting
from ..envelope import failure_text
from ..errors import WarehouseQueryError
from ..explore.profile import NEAR_UNIQUE_RATIO
from ..explore.relationships import entity_of, is_id_shaped
from ..transform.build import shadow_parse
from ..value_mask import mask_values
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

# Said once when a fan-out with no readable key is capped, and dropped again
# when a proven grain caps it further: one finding states one cap.
_UNREAD_KEY_CAP = (
    " (severity capped at medium: with no join key to read, dex cannot tell an "
    "intended expansion from a wrong key)"
)

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


def _artifact(project_dir: Path, name: str) -> Path:
    """``name`` in the directory holding the manifest this sweep reads.

    The manifest and the run results are read from one directory, the newest
    one on disk (issue #1014): after a Dex build that is Dex's own artifact
    directory, after the user's own dbt run it is the project's target path, and
    a run results file is never paired with another run's manifest.
    """

    from ..transform.artifact_runtime import readable_artifact_dir

    return readable_artifact_dir(project_dir) / name


def _manifest_nodes(project_dir: Path) -> dict[str, dict] | None:
    path = _artifact(project_dir, "manifest.json")
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("nodes", {})


def _run_results(project_dir: Path) -> list[dict] | None:
    from ..transform.artifact_runtime import pinned_invocation_id

    path = _artifact(project_dir, "run_results.json")
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    # A build's own sweep reads only its own invocation's results; another
    # run's passing tests must not vouch for this build's rows.
    expected = pinned_invocation_id()
    written = (document.get("metadata") or {}).get("invocation_id")
    if expected is not None and written is not None and str(written) != expected:
        return None
    return document.get("results", [])


def read_manifest_target(project_dir: Path) -> str | None:
    """The target dbt compiled the manifest against, from its manifest.json."""

    path = _artifact(project_dir, "manifest.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            target_name = metadata.get("target_name")
            if isinstance(target_name, str) and target_name:
                return target_name
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return None


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
        return manifest_node_label(uid, nodes.get(uid))

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
            message = _dbt_message(result, status)
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
            message = _dbt_message(result, status)
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


def _dbt_message(result: dict, status: str) -> str:
    """dbt's message on one node, with any value a warehouse conversion error
    quotes masked: a model that fails on a cast fails in the warehouse's words,
    and those quote the row's value (#1070)."""

    return mask_values(str(result.get("message") or status))


def missing_relation_findings(
    model_relations: dict[str, str],
    live_identifiers: list[str],
    already_reported: set[str],
    *,
    dev_scope: str | None = None,
    judged_namespace: str | None = None,
    manifest_target: str | None = None,
    reads_dev_target: bool = False,
) -> tuple[list[DriftFinding], list[str]]:
    """Manifest models with no corresponding relation in the warehouse.

    ``already_reported`` names models a build-status finding already
    explained (a node that failed or was skipped never produced a relation
    either, and reporting that twice under a different code would say the
    same thing about the same node in two places). ``model_relations`` holds
    models only: a source's own "declared but absent" case belongs to the
    schema axis, which already reports dangling sources against a baseline.

    When ``reads_dev_target`` is False, models whose relations resolve into
    the dev namespace (or outside the judged namespace when the manifest was
    compiled against dev) are reported as unchecked in notes with a pointer
    to ``maintain verify --target dev``, rather than as high-severity
    ``no_relation`` findings (#715).
    """

    findings: list[DriftFinding] = []
    unchecked: list[str] = []
    judged_schemas = (
        {s.strip().lower() for s in judged_namespace.split(",")}
        if judged_namespace
        else set()
    )

    for name, relation in sorted(model_relations.items()):
        if name in already_reported:
            continue
        if match_identifier(relation, live_identifiers):
            continue

        if not reads_dev_target:
            clean_rel = strip_relation_quoting(relation).lower()
            rel_parts = clean_rel.split(".")
            rel_ns = clean_rel.rsplit(".", 1)[0] if len(rel_parts) > 1 else ""
            rel_schema = rel_parts[-2] if len(rel_parts) > 1 else ""
            is_dev = False

            if dev_scope and (
                dev_scope.lower() in clean_rel
                or rel_schema == dev_scope.lower().split(".")[-1]
                or rel_ns == dev_scope.lower()
            ):
                is_dev = True
            elif manifest_target == "dev":
                in_judged = (
                    rel_ns in judged_schemas
                    or rel_schema in judged_schemas
                    or any(clean_rel.startswith(js + ".") for js in judged_schemas)
                )
                if not in_judged:
                    is_dev = True
            elif (
                live_identifiers
                and relation_verdict(relation, live_identifiers) == "foreign"
            ):
                is_dev = True

            if is_dev:
                unchecked.append(name)
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

    notes: list[str] = []
    if unchecked:
        target_hint = "maintain verify --target dev"
        models_str = ", ".join(sorted(unchecked))
        notes.append(
            f"the relation check was not run for {models_str}: relation resolves into "
            "the dev namespace, which is outside the read scope; re-run with "
            f"`{target_hint}` to verify dev models"
        )
    return findings, notes


# --- column contract: a built relation against its declared schema.yml ---------


class ColumnContractPlan(tuple[dict[str, dict[str, str | None]], int, list[str]]):
    """The plan for checking column contracts against the compiled manifest.

    A 3-tuple of ``(declared_by_model, undeclared, notes)`` for backwards
    compatibility with callers that unpack it, plus ``package_entries`` naming
    the package YAML that documents any project override among them (#1205).
    """

    declared_by_model: dict[str, dict[str, str | None]]
    undeclared: int
    notes: list[str]
    package_entries: dict[str, str]

    def __new__(
        cls,
        declared_by_model: dict[str, dict[str, str | None]],
        undeclared: int,
        notes: list[str],
        package_entries: dict[str, str] | None = None,
    ) -> ColumnContractPlan:
        """Create a new column contract plan."""
        obj = super().__new__(cls, (declared_by_model, undeclared, notes))
        obj.declared_by_model = declared_by_model
        obj.undeclared = undeclared
        obj.notes = notes
        obj.package_entries = package_entries or {}
        return obj


def column_contract_plan(
    project_dir: Path, *, scope: set[str] | None = None
) -> ColumnContractPlan:
    """Every selected model's declared column contract, from the compiled
    manifest, plus how many considered models declare none at all (#230).

    Read from the manifest rather than by re-parsing schema.yml, the way
    #214's plan-time comparison has to: dbt already reduces every declared
    column (and its ``data_type``, when one is declared) into each model
    node's own ``columns``, and a compiled manifest is exactly what this
    command needs a build to have produced already for its other two finding
    classes. ``scope`` narrows which models are even considered, so a model
    outside it is neither checked nor counted toward the undeclared total,
    the same rule :func:`row_population_plan` follows. It holds unique ids, which
    is what a build has and what keeps two versions of one model apart, or
    lowered labels, which is what `maintain verify`'s own ``objects`` argument
    has: a model name is conventionally lowercase already, so this never narrows
    past what an exact-case match would.

    Returns a :class:`ColumnContractPlan` unpacking as ``(declared_by_model,
    undeclared_count, notes)``. A model with no ``columns:`` entry contributes to
    the count and nothing to the mapping: it has no contract to compare, which is a
    fact about the project rather than a finding about any one model, so it is named
    once at the summary level (#230's acceptance) rather than repeated per model.
    ``notes`` names the one condition that stops this from running at all, the same
    way :func:`row_population_plan` does for its own manifest read.
    """

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return ColumnContractPlan(
            {},
            0,
            [
                "no compiled manifest found; run `dbt compile` or `dbt build` "
                "for column-contract findings"
            ],
            {},
        )

    from ..transform.build import packages_dir

    try:
        install_dir = (
            packages_dir(project_dir).relative_to(Path(project_dir)).as_posix()
        )
    except Exception:
        install_dir = "dbt_packages"

    declared_by_model: dict[str, dict[str, str | None]] = {}
    package_entries: dict[str, str] = {}
    undeclared = 0
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        if not (isinstance(node.get("name"), str) and node["name"]):
            continue
        name = manifest_node_label(uid, node)
        if scope is not None and uid not in scope and name.lower() not in scope:
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
        patch = node.get("patch_path")
        if isinstance(patch, str) and "://" in patch:
            pkg, path = patch.split("://", 1)
            if pkg and path and pkg != node.get("package_name"):
                package_entries[name] = f"{install_dir.rstrip('/')}/{pkg}/{path}"

    with contextlib.suppress(Exception):
        from ..transform.package_overrides import documented_overrides

        project_file = Path(project_dir) / "dbt_project.yml"
        if project_file.is_file():
            project_yml = project_file.read_text(encoding="utf-8")
            overrides = documented_overrides(
                project_dir, declared_by_model.keys(), project_yml
            )
            for model_name, override in overrides.items():
                package_entries[model_name] = override.package_entry

    return ColumnContractPlan(declared_by_model, undeclared, [], package_entries)


@dataclass
class ColumnContractCheck:
    """What the column-contract comparison found, and which models it reached.

    ``unchecked`` maps a model to why it was not compared: ``no_relation``,
    ``relation_not_found`` or ``metadata_unreadable``. A caller reporting
    coverage needs these apart from ``checked``, because no findings over no
    comparison is not a clean contract.
    """

    findings: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    unchecked: dict[str, str] = field(default_factory=dict)


def built_model_relations(project_dir: Path, scope: set[str]) -> dict[str, str]:
    """Each in-scope model's label to its relation, from the compiled manifest.

    ``scope`` holds unique ids. An ephemeral model has no relation and no entry.
    """

    return {
        manifest_node_label(uid, node): strip_relation_quoting(node["relation_name"])
        for uid, node in (_manifest_nodes(project_dir) or {}).items()
        if uid in scope
        and isinstance(node, dict)
        and isinstance(node.get("relation_name"), str)
        and node["relation_name"]
    }


def column_contract_findings(
    adapter,
    declared_by_model: dict[str, dict[str, str | None]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    undeclared: int,
    *,
    package_entries: Mapping[str, str] | None = None,
) -> tuple[list[DriftFinding], list[str]]:
    """:func:`column_contract_check`, as findings and notes alone."""

    check = column_contract_check(
        adapter,
        declared_by_model,
        model_relations,
        live_identifiers,
        undeclared,
        package_entries=package_entries,
    )
    return check.findings, check.notes


def column_contract_check(
    adapter,
    declared_by_model: dict[str, dict[str, str | None]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    undeclared: int,
    *,
    package_entries: Mapping[str, str] | None = None,
) -> ColumnContractCheck:
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

    A relation whose metadata cannot be read is named and the rest still
    compared. A cost-guard refusal is re-raised: it is the caller's own limit
    rather than a gap in this check.

    An override of a package model documented by the package names the package's
    YAML entry in its findings (``package_entries``) and explains that the project
    cannot declare the override (#1205).
    """

    from ..dbt_project import column_contract_divergence
    from ..guards.cost_guard import CostGuardError

    check = ColumnContractCheck()
    findings = check.findings
    unchecked: list[str] = []
    unreadable: list[str] = []
    for model, declared in sorted(declared_by_model.items()):
        relation = model_relations.get(model)
        if relation is None:
            check.unchecked[model] = "no_relation"
            continue
        matches = match_identifier(relation, live_identifiers)
        if len(matches) != 1:
            unchecked.append(model)
            check.unchecked[model] = "relation_not_found"
            continue

        declared_lower = {name.lower(): dtype for name, dtype in declared.items()}
        try:
            _meta, columns = adapter.table_metadata(matches[0])
        except CostGuardError:
            raise
        except Exception as exc:
            unreadable.append(
                f"the column contract was not checked for {model}: its "
                f"metadata could not be read ({failure_text(exc)})"
            )
            check.unchecked[model] = "metadata_unreadable"
            continue
        check.checked.append(model)
        actual_lower = {column.name.lower(): column.data_type for column in columns}
        try:
            connector = getattr(adapter, "name", None) or getattr(
                adapter, "dialect", None
            )
        except Exception:
            connector = None
        missing, extra, mismatched = column_contract_divergence(
            declared_lower, actual_lower, connector=connector
        )

        package_entry = (package_entries or {}).get(model)
        source = f"`{package_entry}`" if package_entry else "schema.yml"

        if missing:
            findings.append(
                DriftFinding(
                    axis="build",
                    code="column_missing",
                    identifier=model,
                    severity="high",
                    detail=(
                        f"'{model}' declares column(s) {', '.join(missing)} in "
                        f"{source} that the built relation does not have"
                    ),
                    data={"model": model, "columns": missing},
                )
            )
        if extra:
            if package_entry:
                detail = (
                    f"'{model}' builds column(s) {', '.join(extra)} that "
                    f"`{package_entry}` does not declare; the project cannot "
                    "declare the override"
                )
            else:
                detail = (
                    f"'{model}' builds column(s) {', '.join(extra)} that "
                    "schema.yml does not declare"
                )
            extra_data: dict[str, Any] = {"model": model, "columns": extra}
            if package_entry:
                extra_data["package_entry"] = package_entry
            findings.append(
                DriftFinding(
                    axis="build",
                    code="column_undeclared",
                    identifier=model,
                    severity="low",
                    detail=detail,
                    data=extra_data,
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
                        + f" in {source}, but the built relation has "
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

    notes = check.notes
    notes.extend(unreadable)
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
    return check


# --- grain: a built relation against its intended one-row-per-entity key -------


@dataclass(frozen=True)
class GrainCandidate:
    """One model's intended grain, and where the claim came from (#229).

    ``source`` is one of ``"declaration"`` (the grain the model's own
    declaration states, ``config.meta.dex.grain``, #496: ranked first, because
    it is the one statement of what a row *is* rather than a test of one
    column, and it is the only source a composite grain has in a project with
    no ``dbt_utils``), ``"unique_test"`` (a single column dbt's own
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
    of the four sources; the findings step's own free naming heuristic and
    "grain unknown" reporting cover it from there, since both need a live
    relation's actual columns, which ``ProjectDefinitions`` does not carry.

    Priority is per model, not per candidate: a model whose declaration
    states a grain is checked on that grain alone (#496); otherwise every
    column independently declared ``unique`` is checked (a model can have more
    than one, each its own claim); every declared composite is checked only
    when the model has no single-column unique test (#1223: a second composite
    on the same model was once dropped); the primary entity is checked
    only when the model has none of those. A model can therefore only ever
    produce candidates from one source, never a mix, so one duplicate is never
    reported twice under two sources.

    ``scope`` is compared lowercase, the same convention
    :func:`column_contract_plan` follows and for the same reason: the caller
    is `maintain verify`'s own ``objects`` argument, already lowered.
    """

    by_model: dict[str, list[GrainCandidate]] = {}
    for model, candidates in declared_grain_candidates(
        definitions, scope=scope
    ).items():
        for source in _GRAIN_SOURCE_ORDER:
            chosen = [c for c in candidates if c.source == source]
            if chosen:
                by_model[model] = chosen
                break
    return by_model


#: The order :func:`grain_plan` ranks the declared sources in, first wins.
_GRAIN_SOURCE_ORDER = (
    "declaration",
    "unique_test",
    "unique_test_composite",
    "primary_entity",
)


def declared_grain_candidates(
    definitions, *, scope: set[str] | None = None
) -> dict[str, list[GrainCandidate]]:
    """Every grain each selected model declares, from every source, before
    :func:`grain_plan`'s priority rule picks the ones it checks (#1223).

    The difference between the two is what ``grain_coverage`` reports as
    ``not_planned``: a key the project states that this command leaves to a
    higher-ranked one. Each candidate appears once per model and source; the
    order within a model is the ranking, then declaration order.
    """

    def wanted(model: str) -> bool:
        return scope is None or model.lower() in scope

    by_model: dict[str, list[GrainCandidate]] = {}
    for model, columns in definitions.declared_grains.items():
        if wanted(model):
            by_model.setdefault(model, []).append(
                GrainCandidate(columns=list(columns), source="declaration")
            )
    for key in definitions.declared_keys:
        if key.unique and wanted(key.model):
            by_model.setdefault(key.model, []).append(
                GrainCandidate(columns=[key.column], source="unique_test")
            )
    for combo in definitions.declared_composite_keys:
        if wanted(combo.model):
            by_model.setdefault(combo.model, []).append(
                GrainCandidate(
                    columns=list(combo.columns), source="unique_test_composite"
                )
            )
    for model, column in definitions.primary_entities.items():
        if wanted(model):
            by_model.setdefault(model, []).append(
                GrainCandidate(columns=[column], source="primary_entity")
            )
    for model, candidates in by_model.items():
        seen: set[tuple[str, tuple[str, ...]]] = set()
        unique: list[GrainCandidate] = []
        for c in sorted(candidates, key=lambda c: _GRAIN_SOURCE_ORDER.index(c.source)):
            marker = (c.source, tuple(column.lower() for column in c.columns))
            if marker not in seen:
                seen.add(marker)
                unique.append(c)
        by_model[model] = unique
    return by_model


def grain_record(
    model: str,
    candidate: GrainCandidate,
    status: str,
    reason: str | None = None,
    *,
    rows: int | None = None,
    distinct: int | None = None,
) -> dict:
    """One ``grain_coverage`` record (#1223): a declared key and its verdict.

    ``status`` is ``proven`` (the exact distinct count reached the row count),
    ``broken`` (it did not, and a ``grain_broken`` finding says by how much),
    ``not_applicable`` (the relation has no rows, so uniqueness says nothing),
    ``unchecked`` (planned, not measured; ``reason`` says why) or
    ``not_planned`` (left to a higher-ranked key, ``reason`` ``priority``).
    ``measured`` carries the two counts wherever both were read, and never a
    value: the warehouse's own words stay in ``notes``.
    """

    return {
        "model": model,
        "columns": list(candidate.columns),
        "source": candidate.source,
        "status": status,
        "reason": reason,
        "measured": (
            {"rows": rows, "distinct": distinct}
            if rows is not None and distinct is not None
            else None
        ),
    }


def measured_grain_record(
    model: str, candidate: GrainCandidate, rows: int, distinct: int
) -> dict:
    if rows == 0:
        return grain_record(
            model,
            candidate,
            "not_applicable",
            "empty_relation",
            rows=rows,
            distinct=distinct,
        )
    status = "proven" if distinct >= rows else "broken"
    return grain_record(model, candidate, status, rows=rows, distinct=distinct)


def not_planned_grain_records(
    declared: dict[str, list[GrainCandidate]],
    planned: dict[str, list[GrainCandidate]],
) -> list[dict]:
    """A ``not_planned`` record for each declared key the priority rule left
    out, unless a planned key on the same model names the same columns: that
    key is judged, under the source that ranked first (#1223)."""

    records: list[dict] = []
    for model, candidates in declared.items():
        chosen = planned.get(model, [])
        judged = {frozenset(column.lower() for column in c.columns) for c in chosen}
        for candidate in candidates:
            if candidate in chosen:
                continue
            if frozenset(column.lower() for column in candidate.columns) in judged:
                continue
            records.append(grain_record(model, candidate, "not_planned", "priority"))
    return records


def sort_grain_records(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda r: (
            r["model"],
            _GRAIN_SOURCE_ORDER.index(r["source"]),
            [column.lower() for column in r["columns"]],
        ),
    )


_GRAIN_SOURCE_PHRASE = {
    "declaration": "declares one row per",
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
    if source == "heuristic" and severity == "high":
        severity = "medium"
        extra_data = {**extra_data, "severity_cap_applied": "heuristic_grain"}
        note_suffix = (
            " (severity capped at medium: guessed from column name; "
            "not a declared grain)"
        )
    approx = "" if exact else "approximately "
    columns_text = " and ".join(columns) if len(columns) < 3 else ", ".join(columns)
    unique_word = "unique" if len(columns) == 1 else "jointly unique"
    # A declared grain already says what one row is; "unique" after it would
    # say the same thing twice.
    claim = (
        f"{_GRAIN_SOURCE_PHRASE[source]} {columns_text}"
        if source == "declaration"
        else f"{_GRAIN_SOURCE_PHRASE[source]} {columns_text} {unique_word}"
    )
    return DriftFinding(
        axis="grain",
        code="grain_broken",
        identifier=model,
        column=columns[0] if len(columns) == 1 else None,
        severity=severity,
        exact=exact,
        detail=(
            f"'{model}' {claim}, but the built relation has {approx}{duplicates} "
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
    proven: dict[str, dict] | None = None,
    records: list[dict] | None = None,
) -> tuple[list[DriftFinding], str | None, list[str]]:
    """Check every declared candidate for one model. Returns ``(findings,
    unchecked_reason, unresolved)``, and records in ``proven`` the first
    candidate the scans showed unique (see :func:`damp_fanout`). ``records``,
    when given, gains one ``grain_coverage`` record per candidate (#1223).
    ``unchecked_reason`` is None when every candidate was measured, ``"scan"``
    when a candidate could not be measured at all (a metered adapter declining
    an unaffordable scan, per
    :meth:`Adapter.distinct_combination_counts`'s own contract), and
    ``"no_count"`` when the scans ran but no exact row count was available
    to compare them against. Either non-None value is a caveat for the
    caller's notes, not a clean bill of health; ``unresolved`` names every
    declared column the live relation does not have, which is skipped
    rather than sent to the warehouse to fail there.

    Each declared column is resolved against the relation's own columns
    case-insensitively before it reaches the adapter (#483). ``schema.yml``
    spells a column the way dbt wrote it, which is ordinarily lowercase;
    the relation spells it the way the warehouse folded it, which on
    Snowflake is uppercase. An adapter quotes the name it is handed, so
    handing it the declared spelling produced ``COUNT(DISTINCT "id")`` against
    a relation holding ``ID``, which Snowflake refuses as an invalid
    identifier. The same lowered comparison :func:`column_contract_findings`
    already makes. The finding keeps the declared spelling, because it speaks
    about what the model declares; only the statement uses the relation's.

    A statement the warehouse refuses (:class:`WarehouseQueryError`) is left
    to propagate: :func:`grain_findings` catches it per model, so one refused
    scan is reported against that model's grain and nothing else.
    """

    meta, columns = adapter.table_metadata(identifier)

    findings: list[DriftFinding] = []
    unresolved: list[str] = []

    # Declared spelling -> the relation's spelling. With no column metadata to
    # resolve against (an adapter that reports none) the declared spelling is
    # the only one there is, so it is used as it was before this map existed.
    live = {column.name.lower(): column.name for column in columns}

    def resolve(c: GrainCandidate) -> list[str] | None:
        if not live:
            return list(c.columns)
        names = [live.get(column.lower()) for column in c.columns]
        missing = [
            col for col, name in zip(c.columns, names, strict=True) if name is None
        ]
        if missing:
            unresolved.extend(missing)
            return None
        return [name for name in names if name is not None]

    resolved = [(c, resolve(c)) for c in candidates]

    singles = [
        (c, names) for c, names in resolved if names is not None and len(names) == 1
    ]
    # Collect every distinct count first, compare second. The count the
    # verdicts compare against must not be read before the scans ran: on
    # an adapter whose catalog holds only an estimate (Postgres reltuples)
    # or nothing at all (an unanalyzed table, reltuples -1, or a view),
    # the scan itself is what supplies the exact total -- the distinct
    # count statement rides COUNT(*) along -- so reading it up front
    # would either compare against a stale estimate or bail out before
    # the scan that would have measured the grain (#849).
    single_counts: dict[str, int] = {}
    scan_failed = False
    if singles:
        counts = adapter.exact_distinct_counts(
            identifier, [names[0] for _, names in singles]
        )
        for _, names in singles:
            distinct = counts.get(names[0])
            if distinct is None:
                scan_failed = True
                continue
            single_counts[names[0]] = distinct

    combo_counts: dict[tuple[str, ...], int] = {}
    combos = [
        (c, names) for c, names in resolved if names is not None and len(names) > 1
    ]
    for _, combo_names in combos:
        result = adapter.distinct_combination_counts(identifier, [combo_names])
        distinct = result.get(tuple(combo_names))
        if distinct is None:
            scan_failed = True
            continue
        combo_counts[tuple(combo_names)] = distinct

    # A scan that ran may have upgraded the catalog count to the exact
    # total; only fall back to the pre-scan count when it did not.
    row_count = adapter.table_metadata(identifier)[0].row_count
    if row_count is None:
        row_count = meta.row_count

    def counted(names: list[str] | None) -> int | None:
        if names is None:
            return None
        if len(names) == 1:
            return single_counts.get(names[0])
        return combo_counts.get(tuple(names))

    if records is not None:
        for c, spelled in resolved:
            distinct = counted(spelled)
            if spelled is None:
                records.append(grain_record(model, c, "unchecked", "missing_columns"))
            elif distinct is None:
                records.append(grain_record(model, c, "unchecked", "scan"))
            elif row_count is None:
                records.append(grain_record(model, c, "unchecked", "no_count"))
            else:
                records.append(measured_grain_record(model, c, row_count, distinct))

    if row_count is None:
        if single_counts or combo_counts:
            return [], "no_count", unresolved
        return [], "scan", unresolved

    for c, names in singles:
        distinct = single_counts.get(names[0])
        if distinct is None:
            continue
        if distinct >= row_count:
            _record_proven(proven, model, c)
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

    for c, combo_names in combos:
        distinct = combo_counts.get(tuple(combo_names))
        if distinct is None:
            continue
        if distinct >= row_count:
            _record_proven(proven, model, c)
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

    return findings, ("scan" if scan_failed else None), unresolved


def _record_proven(
    proven: dict[str, dict] | None, model: str, candidate: GrainCandidate
) -> None:
    if proven is not None:
        proven.setdefault(
            model, {"columns": list(candidate.columns), "source": candidate.source}
        )


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
    key), ``"unsupported"`` (the adapter cannot run the check this needs,
    including a priced scan the cost gate refuses: an unconfirmed run with
    no ceiling, or a spent budget), or ``"no_count"`` (the aggregates ran
    but no exact row count was available to compare them against).
    """

    meta, columns = adapter.table_metadata(identifier)
    entity = entity_of(identifier.rsplit(".", 1)[-1])
    named = (f"{entity}_id", f"{entity}id", "id")
    candidate = next((c for c in columns if c.name.lower() in named), None)
    if candidate is None:
        candidate = next((c for c in columns if is_id_shaped(c.name)), None)
    if candidate is None:
        return None, "unknown"

    from ..guards.cost_guard import CostGuardError

    aggregates_fn = getattr(adapter, "column_aggregates", None)
    if aggregates_fn is None:
        return None, "unsupported"
    try:
        aggregates = aggregates_fn(identifier, [candidate])
    except CostGuardError:
        # A priced scan the cost gate refuses is this model's grain going
        # unmeasured, not the command failing: the declared path declines
        # the same way via try_charge, and grain_findings already treats a
        # refused statement as a note rather than a failure (#483).
        return None, "unsupported"
    agg = next((a for a in aggregates if a.name == candidate.name), None)
    if agg is None or agg.distinct_count is None:
        return None, "unsupported"
    if agg.null_fraction not in (0.0, None):
        # A key candidate with nulls is not a key: disqualified, not proven.
        return None, "unknown"

    # As with the declared path, the count is read only after the scan
    # ran: the aggregate statement itself is what supplies the exact total
    # on an adapter whose catalog holds only an estimate or nothing (#849).
    row_count = adapter.table_metadata(identifier)[0].row_count
    if row_count is None:
        row_count = meta.row_count
    if row_count is None:
        return None, "no_count"

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
    proven: dict[str, dict] | None = None,
    records: list[dict] | None = None,
    spine_or_generated: set[str] | None = None,
) -> tuple[list[DriftFinding], list[str]]:
    """A built relation's grain against what the project declares (or, absent
    any declaration, a free naming guess), for every selected model (#229).

    ``proven`` collects, per model, the declared grain the scans showed unique
    (#841). Only a declaration counts: the naming guess is never recorded,
    because a guess proven unique says nothing about what the model intends.

    ``records``, when given, gains one ``grain_coverage`` record per declared
    candidate in ``declared`` (#1223), measured or not, so a key that held is
    stated rather than left to an absent finding. Each reason a candidate went
    unmeasured has a code there that the notes below spell out in prose:
    ``no_relation`` (nothing in the warehouse matched, or the model has no
    relation), ``ambiguous`` (more than one matched), ``refused``,
    ``missing_columns``, ``scan`` and ``no_count``. The naming guess has no
    record, since it is not a declared key.

    A declared source (unique test, composite, or primary entity) is checked
    directly with an exact scan: the declaration itself is the justification
    for spending on verification, the same reasoning ``maintain grain``'s own
    ``declared_composite_checks`` already rests on. Only a model with no
    declaration at all falls to the heuristic, which is free-first and
    escalates to exact only when already close (see
    :func:`_heuristic_candidate`), unless the model is a time spine,
    generated in SQL, or driven off one (#1202). A ``grain_broken`` finding's
    ``exact`` flag is always True except in exactly that one
    approximate-verdict case, honestly naming the one place this check ever
    reports from a guess.

    An unknown grain and a broken one are reported through different
    channels on purpose (the issue's own framing): a broken grain is a
    ``DriftFinding``; an unknown one is named once in ``notes`` rather than
    given a finding of its own, since "dex could not tell" is not itself a
    defect in the project.
    """

    findings: list[DriftFinding] = []
    ambiguous: list[str] = []
    unchecked: list[str] = []
    no_count: list[str] = []
    unknown: list[str] = []
    unresolved: dict[str, list[str]] = {}
    refused: dict[str, str] = {}

    def unmeasured(model: str, reason: str) -> None:
        if records is not None:
            for candidate in declared.get(model) or []:
                records.append(grain_record(model, candidate, "unchecked", reason))

    for model in sorted(all_models | set(declared)):
        relation = model_relations.get(model)
        if relation is None:
            # missing_relation_findings already reports this model; nothing
            # about its grain belongs in a second, unrelated finding class.
            unmeasured(model, "no_relation")
            continue
        if model not in all_models:
            continue
        matches = match_identifier(relation, live_identifiers)
        if len(matches) != 1:
            ambiguous.append(model)
            unmeasured(model, "ambiguous" if matches else "no_relation")
            continue
        identifier = matches[0]

        # A statement the warehouse refuses is this model's grain going
        # unmeasured, not the command failing (#483): the other models' grains
        # are still checked, and the other finding classes still run. The
        # server's own words go to the notes, because they are the diagnosis.
        try:
            candidates = declared.get(model)
            if candidates:
                model_records: list[dict] | None = [] if records is not None else None
                try:
                    model_findings, unchecked_reason, missing = (
                        _declared_grain_findings(
                            adapter,
                            model,
                            identifier,
                            candidates,
                            min_rows,
                            proven,
                            model_records,
                        )
                    )
                except WarehouseQueryError:
                    unmeasured(model, "refused")
                    raise
                if records is not None and model_records is not None:
                    records.extend(model_records)
                findings.extend(model_findings)
                if unchecked_reason == "scan":
                    unchecked.append(model)
                elif unchecked_reason == "no_count":
                    no_count.append(model)
                if missing:
                    unresolved[model] = missing
                continue

            if spine_or_generated and (
                model in spine_or_generated
                or model.lower() in {m.lower() for m in spine_or_generated}
            ):
                continue

            finding, reason = _heuristic_candidate(adapter, model, identifier, min_rows)
        except WarehouseQueryError as exc:
            refused[model] = failure_text(exc, detail_only=True)
            continue
        if finding is not None:
            findings.append(finding)
        elif reason == "unknown":
            unknown.append(model)
        elif reason == "unsupported":
            unchecked.append(model)
        elif reason == "no_count":
            no_count.append(model)

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
    if no_count:
        notes.append(
            "the grain could not be measured for "
            + ", ".join(sorted(no_count))
            + ": no exact row count was available"
        )
    if unknown:
        notes.append(
            "the grain could not be determined for "
            + ", ".join(sorted(unknown))
            + ": no declared grain, no declared unique test, no declared primary "
            "entity, and no id-shaped column was found"
        )
    for model, missing in sorted(unresolved.items()):
        notes.append(
            f"the declared grain on {model} names a column the relation does "
            "not have, so it was not checked: " + ", ".join(missing)
        )
    for model, detail in sorted(refused.items()):
        notes.append(
            f"the grain could not be measured for {model}: the warehouse "
            f"refused the distinct-count statement ({detail})"
        )
    return findings, notes


# --- row population: a model against the relation it is built from -------------

_GENERATED_SQL = "generated in SQL; no parent relation to compare against"
#: Why a driving parent is not a population (#988), as the note words it.
_TIME_SPINE = "the project's time spine"
_GENERATED_PARENT = "generated in SQL"
#: The time spine's required name before dbt 1.9 added the `time_spine:` config.
_LEGACY_TIME_SPINE = "metricflow_time_spine"


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
    keys_by_join: tuple[tuple[str, ...], ...] = ()
    reducers: tuple[str, ...] = ()
    multiplies: bool = False

    @property
    def relations(self) -> tuple[str, str]:
        """Both sides, as the keys a caller's count map must carry."""

        return (self.relation.lower(), self.parent_relation.lower())

    @property
    def join_keys(self) -> tuple[str, ...]:
        """Every join's equality keys, in join order."""

        return tuple(key for keys in self.keys_by_join for key in keys)

    def describe_joins(self) -> str:
        """Each join beside its own keys: a key read off one join is never
        shown next to another join's label (#841)."""

        return " or the ".join(
            f"{label} on {', '.join(keys)}"
            if keys
            else f"{label} (dex could not read the join key)"
            for label, keys in zip(
                self.joins,
                self.keys_by_join or ((),) * len(self.joins),
                strict=True,
            )
        )


def _manifest_sources(project_dir: Path) -> dict[str, dict]:
    path = _artifact(project_dir, "manifest.json")
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
    target_scope = cte_scopes[name]
    if sql_shape.SET_OPERATIONS and isinstance(target_scope, sql_shape.SET_OPERATIONS):
        return node
    return _through_ctes(
        sql_shape.from_relation(target_scope)
        if isinstance(target_scope, exp.Select)
        else None,
        cte_scopes,
        sql_shape,
        seen,
    )


def _join_label(
    join,
    resolved,
    name_of,
    cte_scopes: dict | None = None,
    sql_shape=None,
) -> str:
    """One join named the way a reader would name it: how it joins, and what to.

    The project's own name for the joined relation where there is one, since
    that is what the reader will go and open; the physical relation otherwise.
    When a joined CTE holds a set operation, it has no underlying physical table,
    so the join is named after the CTE itself. A join to an anonymous set operation
    names it as a set operation rather than a generic subquery.
    The ON condition is deliberately not here, because it travels separately as
    the join keys, which is the part that identifies a wrong grain.
    """

    if resolved is not None:
        return f"{join.side} join to '{name_of(_table_name(resolved))[1]}'"

    if cte_scopes and _cte_name(join.relation, cte_scopes) is not None:
        return f"{join.side} join to '{join.relation.name}'"

    inner = (
        join.relation.this.unnest()
        if hasattr(join.relation, "this") and hasattr(join.relation.this, "unnest")
        else getattr(join.relation, "this", None)
    )
    if (
        sql_shape is not None
        and sql_shape.SET_OPERATIONS
        and isinstance(inner, sql_shape.SET_OPERATIONS)
    ):
        return f"{join.side} join to a set operation"

    return f"{join.side} join to a subquery"


def _generates_rows(relation) -> bool:
    """An ``UNNEST`` of generator calls or literal arrays: BigQuery's
    ``unnest(generate_date_array(...))``, the time spine's usual shape there,
    generates its rows in SQL just as DuckDB's ``range(...)`` does (#988)."""

    from sqlglot import expressions as exp

    if not isinstance(relation, exp.Unnest) or not relation.expressions:
        return False
    generators = tuple(
        kind
        for kind in (
            getattr(exp, name, None)
            for name in (
                "GenerateDateArray",
                "GenerateTimestampArray",
                "GenerateSeries",
                "ExplodingGenerateSeries",
            )
        )
        if kind is not None
    )
    return all(
        isinstance(argument, generators)
        or (
            isinstance(argument, exp.Array)
            and all(isinstance(item, exp.Literal) for item in argument.expressions)
        )
        for argument in relation.expressions
    )


_DRIVEN_BY_SET_OPERATION = "it is driven by a set operation, so it has no single parent"


def _set_operation_reason(node, sql_shape, reason: str) -> str:
    """Why a set operation has no parent: ``reason``, unless every branch
    selects from nothing or from VALUES, which generates its rows in SQL
    (``select 0 union all select 1``, the seed of ``dbt_utils.date_spine``)."""

    from sqlglot import expressions as exp

    def generates(branch) -> bool:
        if sql_shape.SET_OPERATIONS and isinstance(branch, sql_shape.SET_OPERATIONS):
            return generates(branch.left) and generates(branch.right)
        if isinstance(branch, exp.Subquery):
            return generates(branch.this)
        if not isinstance(branch, exp.Select) or sql_shape.joins(branch):
            return False
        relation = sql_shape.from_relation(branch)
        return relation is None or isinstance(relation, exp.Values)

    return _GENERATED_SQL if generates(node) else reason


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
            _set_operation_reason(
                tree, sql_shape, "it is a set operation, so it has no single parent"
            ),
            {},
        )
    if not isinstance(tree, exp.Select):
        return None, [], [], False, "its compiled SQL is not a SELECT", {}

    with_clause = tree.args.get("with_") or tree.args.get("with")
    cte_scopes = (
        {cte.alias_or_name.lower(): cte.this for cte in with_clause.expressions}
        if with_clause is not None
        else {}
    )
    reducers: list[str] = []
    found_joins: list = []
    multiplies = sql_shape.multiplies_rows(tree)

    current = tree
    visited: set[str] = set()
    while True:
        if sql_shape.SET_OPERATIONS and isinstance(current, sql_shape.SET_OPERATIONS):
            return (
                None,
                reducers,
                found_joins,
                multiplies,
                _set_operation_reason(current, sql_shape, _DRIVEN_BY_SET_OPERATION),
                cte_scopes,
            )
        if not isinstance(current, exp.Select):
            return (
                None,
                reducers,
                found_joins,
                multiplies,
                "its driving relation is not a table or a subquery dex can follow",
                cte_scopes,
            )
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
                    _set_operation_reason(inner, sql_shape, _DRIVEN_BY_SET_OPERATION),
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
            with_inner = inner.args.get("with_") or inner.args.get("with")
            if with_inner is not None:
                cte_scopes.update(
                    {
                        cte.alias_or_name.lower(): cte.this
                        for cte in with_inner.expressions
                    }
                )
            current = inner
            continue
        if isinstance(relation, exp.Values) or _generates_rows(relation):
            return (
                None,
                reducers,
                found_joins,
                multiplies,
                _GENERATED_SQL,
                cte_scopes,
            )
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
        if not name:
            reason = (
                _GENERATED_SQL
                if isinstance(relation.this, exp.GenerateSeries)
                else "it is a table function with no warehouse parent"
            )
            return None, reducers, found_joins, multiplies, reason, cte_scopes
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
            # A CTE can carry a WITH of its own (dbt_utils.date_spine compiles
            # to one): its names are in scope from here down, and a nested name
            # is never a physical table.
            if isinstance(current, exp.Select):
                cte_scopes.update(
                    {
                        nested: scope
                        for nested, scope in sql_shape.scopes(current).items()
                        if nested != sql_shape.MAIN_SCOPE
                    }
                )
            continue
        return relation, reducers, found_joins, multiplies, None, cte_scopes


def spine_or_generated_models(
    project_dir: Path | str | None,
    dialect: str = "duckdb",
    nodes: dict[str, dict] | None = None,
) -> set[str]:
    """Models that are a time spine, generated in SQL, or driven off one (#1202).

    Such models supply anchors or cross products rather than preserving a
    parent's population or holding a natural single-column primary key. Absent
    a declared grain, the naming heuristic is skipped on them so a cross
    product (like a calendar crossed with customers) is not flagged as a
    broken grain.
    """

    if nodes is None:
        if project_dir is None:
            return set()
        project_path = (
            Path(project_dir) if isinstance(project_dir, str) else project_dir
        )
        nodes = _manifest_nodes(project_path) or {}
    if not nodes:
        return set()

    import sqlglot

    from .. import sql_shape

    relations: dict[str, str] = {}
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        relation = node.get("relation_name")
        if isinstance(relation, str) and relation and isinstance(node.get("name"), str):
            relations[strip_relation_quoting(relation)] = manifest_node_label(uid, node)

    known = sorted(relations)

    def name_of(relation: str) -> tuple[str, str]:
        matched = match_identifier(relation, known)
        resolved = matched[0] if len(matched) == 1 else relation
        return resolved, relations.get(resolved, resolved)

    non_population: dict[str, str] = {}
    model_by_relation: dict[str, dict] = {}
    result: set[str] = set()

    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        relation = node.get("relation_name")
        if not (isinstance(relation, str) and relation):
            continue
        label = manifest_node_label(uid, node)
        stripped = strip_relation_quoting(relation)
        model_by_relation[stripped] = node
        if node.get("time_spine") or str(node.get("name", "")).lower() == (
            _LEGACY_TIME_SPINE
        ):
            non_population[stripped] = _TIME_SPINE
            result.add(label)

    checks: list[tuple[str, str, list[str]]] = []
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        relation = node.get("relation_name")
        code = node.get("compiled_code")
        if not (isinstance(node.get("name"), str) and node["name"]):
            continue
        name = manifest_node_label(uid, node)
        if not (isinstance(relation, str) and relation):
            continue
        stripped = strip_relation_quoting(relation)
        if not (isinstance(code, str) and code.strip()):
            continue
        try:
            tree = sqlglot.parse_one(code, read=dialect)
        except Exception:
            tree = None
        if tree is None:
            continue
        table, _, found_joins, _, reason, cte_scopes = _driving_parent(tree, sql_shape)
        model_by_relation.pop(stripped, None)
        if table is None:
            if reason == _GENERATED_SQL:
                result.add(name)
                non_population.setdefault(stripped, _GENERATED_PARENT)
            continue
        parent_relation, _ = name_of(_table_name(table))
        join_rels: list[str] = []
        for join in found_joins:
            resolved = _through_ctes(join.relation, cte_scopes, sql_shape)
            if resolved is not None:
                join_rels.append(name_of(_table_name(resolved))[0])
        checks.append((name, parent_relation, join_rels))

    for _name, parent_relation, _ in checks:
        parent_node = model_by_relation.get(parent_relation)
        if parent_relation in non_population or parent_node is None:
            continue
        parent_code = parent_node.get("compiled_code")
        if not (isinstance(parent_code, str) and parent_code.strip()):
            continue
        try:
            parent_tree = sqlglot.parse_one(parent_code, read=dialect)
        except Exception:
            parent_tree = None
        if parent_tree is not None and (
            _driving_parent(parent_tree, sql_shape)[4] == _GENERATED_SQL
        ):
            non_population[parent_relation] = _GENERATED_PARENT
        else:
            model_by_relation.pop(parent_relation, None)

    for name, parent_relation, join_rels in checks:
        if parent_relation in non_population or any(
            j in non_population for j in join_rels
        ):
            result.add(name)

    return result


def row_population_plan(
    project_dir: Path,
    dialect: str,
    *,
    scope: set[str] | None = None,
    recomputed: Collection[str] = (),
) -> tuple[list[RowPopulationCheck], list[str], list[str]]:
    """Line every buildable model up against the relation it is built from.

    Free and connectionless: the compiled SQL and the relation names are both
    already in ``target/manifest.json``, so this settles the whole static half
    of row population before anything decides what a count would cost.

    A model this cannot line up is named in the notes rather than dropped. An
    unfollowable chain and a clean model are the same empty result otherwise,
    and only one of them means the model was checked. The third list is the
    subset of those notes that name a model generated in SQL.

    ``scope`` narrows to a set of model unique ids, which is what a caller
    verifying one build rather than a whole project has: the notes narrow with
    it, so a model outside the scope is neither checked nor reported as
    unchecked. Both halves matter, because a note naming models this caller
    never asked about reads as a gap in the answer it did ask for. Ids rather
    than names, because two versions of one model share a name and a build
    that ran one of them did not run the other.

    ``recomputed`` names, by label, the incremental models this run rebuilt from
    their parents with ``--full-refresh``, which is the one time an incremental
    model's row count is a function of its parent's. Only a build knows that, so
    a caller with no build behind it leaves it empty. A microbatch model stays
    skipped even so: it rebuilds only from its own ``begin``, so what it holds is
    still not its whole parent, and nothing in its SQL would say so.

    A model whose driving parent supplies anchors rather than a population
    (#988) is not compared either, and an informational note says why: the
    project's time spine (a `time_spine:` config, or the `metricflow_time_spine`
    name dbt required before it), or a model generated in SQL. A model that only
    joins one is unaffected.
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
            [],
        )

    relations: dict[str, str] = {}
    for uid, node in list(nodes.items()) + list(_manifest_sources(project_dir).items()):
        if not isinstance(node, dict):
            continue
        relation = node.get("relation_name")
        if isinstance(relation, str) and relation and isinstance(node.get("name"), str):
            relations[strip_relation_quoting(relation)] = manifest_node_label(uid, node)

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

    # Relations that supply rows without being a population (#988): the
    # project's time spine, by its dbt `time_spine:` config or the name dbt
    # before 1.9 required, and, filled in below, any model generated in SQL.
    # A model driven off one is not compared to its row count, because the
    # spine's dates are anchors, not rows the model is expected to preserve.
    non_population: dict[str, str] = {}
    model_by_relation: dict[str, dict] = {}
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        relation = node.get("relation_name")
        if not (isinstance(relation, str) and relation):
            continue
        model_by_relation[strip_relation_quoting(relation)] = node
        if node.get("time_spine") or str(node.get("name", "")).lower() == (
            _LEGACY_TIME_SPINE
        ):
            non_population[strip_relation_quoting(relation)] = _TIME_SPINE

    checks: list[RowPopulationCheck] = []
    notes: list[str] = []
    uncompiled: list[str] = []
    informational: list[str] = []
    incremental: list[str] = []
    microbatch: list[str] = []
    generated: list[str] = []
    unfollowable: list[str] = []
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        relation = node.get("relation_name")
        code = node.get("compiled_code")
        if not (isinstance(node.get("name"), str) and node["name"]):
            continue
        name = manifest_node_label(uid, node)
        if scope is not None and uid not in scope and name.lower() not in scope:
            continue
        # An ephemeral model compiles with no relation of its own and is inlined
        # into whatever reads it, so it has no row count to compare; a model
        # with no compiled SQL was never compiled, so it is collected for the
        # note rather than checked.
        if not (isinstance(relation, str) and relation):
            continue
        config = node.get("config")
        materialized = (
            str(config.get("materialized", "")).lower()
            if isinstance(config, dict)
            else ""
        )
        if materialized == "ephemeral":
            continue
        # Ahead of the compiled-SQL check, because dbt's manifest holds no compiled
        # SQL for a microbatch model however often it built: asking for a compile
        # would send the caller after an answer that would not change.
        if materialized in _UNCOMPARABLE_MATERIALIZATIONS:
            # A label can repeat across packages, and one copy's own setting
            # decides whether dbt rebuilt that copy.
            if name not in recomputed or (
                isinstance(config, dict) and config.get("full_refresh") is False
            ):
                incremental.append(name)
                continue
            if isinstance(config, dict) and (
                config.get("incremental_strategy") == "microbatch"
            ):
                microbatch.append(name)
                continue
        if not (isinstance(code, str) and code.strip()):
            uncompiled.append(name)
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
        # Read here, so the generated-parent pass below need not read it again.
        model_by_relation.pop(strip_relation_quoting(relation), None)
        if table is None:
            if reason == _GENERATED_SQL:
                generated.append(name)
                non_population.setdefault(
                    strip_relation_quoting(relation), _GENERATED_PARENT
                )
            else:
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
                        cte_scopes=cte_scopes,
                        sql_shape=sql_shape,
                    )
                    for join in found_joins
                ),
                keys_by_join=tuple(
                    tuple(sql_shape.equality_columns(join.on)) for join in found_joins
                ),
                reducers=tuple(dict.fromkeys(reducers)),
                multiplies=multiplies,
            )
        )

    if uncompiled:
        notes.append(
            f"no compiled SQL found for {', '.join(sorted(uncompiled))}; "
            "run `dbt compile` or `dbt build` for row-population findings"
        )
    if incremental:
        notes.append(
            f"row population was not compared for {', '.join(sorted(incremental))}: "
            "an incremental model holds what previous runs loaded into it, so its "
            "row count is not a function of its parent's"
        )
    if microbatch:
        notes.append(
            f"row population was not compared for {', '.join(sorted(microbatch))}: "
            "a full refresh rebuilds a microbatch model only from its `begin`, so "
            "its row count is not a function of its whole parent's"
        )
    if generated:
        generated_note = "row population was not compared for " + ", ".join(
            f"{model} ({_GENERATED_SQL})" for model in sorted(generated)
        )
        notes.append(generated_note)
        informational.append(generated_note)
    # Settled only now, since a generated parent is known once every model has
    # been read. A parent outside a scoped run was never read above, so it is
    # read here, once. A spine reached only through a join is not the driving
    # parent, so a model that joins one keeps its check.
    for check in checks:
        parent_node = model_by_relation.get(check.parent_relation)
        if check.parent_relation in non_population or parent_node is None:
            continue
        parent_code = parent_node.get("compiled_code")
        if not (isinstance(parent_code, str) and parent_code.strip()):
            continue
        try:
            parent_tree = sqlglot.parse_one(parent_code, read=dialect)
        except Exception:
            parent_tree = None
        if parent_tree is not None and (
            _driving_parent(parent_tree, sql_shape)[4] == _GENERATED_SQL
        ):
            non_population[check.parent_relation] = _GENERATED_PARENT
        else:
            model_by_relation.pop(check.parent_relation, None)
    anchored = sorted(
        (check for check in checks if check.parent_relation in non_population),
        key=lambda check: check.model,
    )
    if anchored:
        checks = [
            check for check in checks if check.parent_relation not in non_population
        ]
        anchored_note = "row population was not compared for " + ", ".join(
            f"{check.model} (its driving parent '{check.parent}' is "
            f"{non_population[check.parent_relation]}, which supplies anchors "
            "rather than a population the model is expected to preserve)"
            for check in anchored
        )
        notes.append(anchored_note)
        informational.append(anchored_note)
    if unfollowable:
        notes.append(
            "no single driving parent could be identified for "
            + ", ".join(sorted(unfollowable))
        )
    return checks, notes, informational


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
        severity = "high" if rows >= parent_rows * _ROW_FANOUT_HIGH_FACTOR else "medium"
        cap = ""
        # A join with no key dex can read is usually a range or inequality join,
        # and those expand on purpose as often as by mistake (#841). Without a
        # key there is nothing to call wrong, so the size of the expansion alone
        # never ranks it with a wrong key.
        if not check.join_keys:
            data["join_key_read"] = False
            if severity == "high":
                severity = "medium"
                data["severity_cap_applied"] = "join_key_unread"
                cap = _UNREAD_KEY_CAP
        findings.append(
            DriftFinding(
                axis="row_population",
                code="row_fanout",
                identifier=check.model,
                severity=severity,
                detail=(
                    f"'{check.model}' has {against}: the {check.describe_joins()} "
                    f"matches more than one row per parent row{cap}"
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


def damp_fanout(
    findings: list[DriftFinding], proven: dict[str, dict]
) -> list[DriftFinding]:
    """Cap a ``row_fanout`` at low on a model whose declared grain this run
    proved unique (#841).

    A model that holds more rows than its driving parent and is still unique on
    the grain it declares is holding the rows it says it holds: the expansion
    is what the model is for (anchors times windows, a date spine times
    entities), not a key matching twice. Only a declaration counts, never the
    naming guess, and the finding stays listed rather than dropped, the way
    ``grain_min_rows`` damps a grain finding, so a reader still sees the counts
    and a gate on high or medium no longer needs an allowlist where a later,
    real fan-out could hide. A fan-out on a model whose grain broke is left
    alone, even where another of its declared grains held: the
    ``grain_broken`` beside it is the duplicate it explains.
    """

    broken = {f.identifier for f in findings if f.code == "grain_broken"}
    damped: list[DriftFinding] = []
    for finding in findings:
        grain = proven.get(finding.identifier or "")
        if (
            finding.code != "row_fanout"
            or grain is None
            or finding.identifier in broken
            or finding.severity == "low"
        ):
            damped.append(finding)
            continue
        columns = ", ".join(grain["columns"])
        damped.append(
            finding.model_copy(
                update={
                    "severity": "low",
                    "detail": (
                        f"{finding.detail.replace(_UNREAD_KEY_CAP, '')} "
                        "(severity capped at low: the declared "
                        f"grain [{columns}] is proven unique, so the extra rows are "
                        "the grain the model declares rather than duplicates)"
                    ),
                    "data": {
                        **finding.data,
                        "grain_proven": {
                            "columns": list(grain["columns"]),
                            "source": grain["source"],
                        },
                        "severity_cap_applied": "grain_proven",
                    },
                }
            )
        )
    return damped


def passed_grain_tests(project_dir: Path) -> dict[str, dict]:
    """The grains the last run's own uniqueness tests proved, per model label.

    A ``unique`` or ``unique_combination_of_columns`` test that passed in
    ``run_results.json`` proved its columns unique on the relation that run
    built, which is the relation a build's verification judges. Read only by a
    build, whose run results are this run's; a standalone sweep measures the
    grain itself, since the last run may be older than the relation.

    A test scoped by a ``where`` or ``limit`` config checked a subset of the
    rows and proves nothing about the relation, so it is not read; and a model
    one of whose uniqueness tests failed or warned has no proven grain, whatever
    its others say, since that result is the duplicate a fan-out explains.
    """

    from ..dbt_project import manifest_node_label

    nodes = _manifest_nodes(project_dir) or {}
    results = _run_results(project_dir) or []
    status = {str(r.get("unique_id")): str(r.get("status")) for r in results}
    proven: dict[str, dict] = {}
    unproven: set[str] = set()
    for unique_id in sorted(status):
        node = nodes.get(unique_id)
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        attached = node.get("attached_node")
        if not isinstance(meta, dict) or not isinstance(attached, str):
            continue
        test_config = node.get("config")
        if isinstance(test_config, dict) and (
            test_config.get("where") or test_config.get("limit") is not None
        ):
            continue
        raw_kwargs = meta.get("kwargs")
        kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
        if meta.get("name") == "unique" and isinstance(kwargs.get("column_name"), str):
            columns, source = [kwargs["column_name"]], "unique_test"
        elif meta.get("name") == "unique_combination_of_columns" and isinstance(
            kwargs.get("combination_of_columns"), list
        ):
            columns = [str(c) for c in kwargs["combination_of_columns"]]
            source = "unique_test_composite"
        else:
            continue
        parent = nodes.get(attached)
        model = manifest_node_label(
            attached, parent if isinstance(parent, dict) else None
        )
        if status[unique_id] in _FAILURE_STATUSES | _WARNING_STATUSES:
            unproven.add(model)
        elif status[unique_id] == "pass":
            proven.setdefault(model, {"columns": columns, "source": source})
    return {model: grain for model, grain in proven.items() if model not in unproven}


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
    #: The join as the model writes it (``left join subscriptions on ...``), so
    #: a coverage record names the join a reader can find in the SQL (#857).
    #: Not part of equality: two candidates are the same join by their keys.
    label: str = field(default="", compare=False)
    #: Per key column, the conversion the join applies to it before comparing
    #: (``TRY_CAST({} AS INT)``), or None for a bare column (#861). Empty when
    #: neither side converts anything, which is every join written before this.
    from_keys: tuple[str | None, ...] = ()
    to_keys: tuple[str | None, ...] = ()
    #: Per side, the row filters a CTE or subquery applies before the join reads
    #: it, carried down to the physical relation in DuckDB SQL (#1085). Part of
    #: equality: the same keys read through a different filter are a different
    #: input. Empty where the side reads its relation unfiltered.
    from_filters: tuple[str, ...] = ()
    to_filters: tuple[str, ...] = ()
    #: The filtering scopes behind each side, as the model writes them.
    from_scopes: tuple[str, ...] = field(default=(), compare=False)
    to_scopes: tuple[str, ...] = field(default=(), compare=False)

    def scope_filters(self) -> tuple[str | None, str | None] | None:
        """Each side's filters joined into one predicate, or None where neither
        side is filtered (#1085)."""

        if not self.from_filters and not self.to_filters:
            return None

        def joined(filters: tuple[str, ...]) -> str | None:
            return " AND ".join(f"({f})" for f in filters) if filters else None

        return joined(self.from_filters), joined(self.to_filters)

    def key_exprs(self) -> tuple[tuple[str | None, ...], tuple[str | None, ...]] | None:
        """The conversion templates applied to each side's keys, or None where the join
        compares its keys as they are (#861).
        """
        if not any(self.from_keys) and not any(self.to_keys):
            return None
        return self.from_keys, self.to_keys


@dataclass(frozen=True)
class JoinSkip:
    """A join the contract saw and could not measure, and why (#857).

    ``code`` is stable for a caller to branch on; ``reason`` is the prose. The
    string form is the warning line the axis has always emitted, so a reader
    of ``warnings`` alone still learns of every skip."""

    join: str
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.join}: {self.reason}" if self.join else self.reason


@dataclass
class _FilterWalk:
    """What one join side's column resolution collected on its way to the
    physical relation (#1085): the filters of every scope it resolved through,
    carried down to that relation, or the reason it could not carry one.

    ``filters`` holds ``(physical relation name, predicate)`` pairs, the
    predicate in DuckDB SQL over the physical column names, unqualified and
    quoted, the form the coverage probe is authored in before it is transpiled
    to the connector's dialect. ``scopes`` names each filtering scope as the
    model writes it, for the record. ``code``/``reason`` is the first failure.

    ``keep_rows`` is set by the join contract, which measures the rows a model
    joins: a scope whose own join can drop rows of the relation a column reads
    is then refused like any other reducer (#1159). Lineage leaves it unset,
    since a join that drops rows does not change which physical column an
    output is.
    """

    dialect: str | None = None
    keep_rows: bool = False
    filters: list[tuple[str, str]] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    code: str | None = None
    reason: str | None = None

    def fail(self, code: str, reason: str) -> None:
        if self.code is None:
            self.code, self.reason = code, reason

    def carry(self, select, cte_scopes: dict, sql_shape, seen) -> bool:
        """Collect a reducing scope's filters if it has the one supported shape,
        or record why not; return whether resolution may continue through it."""

        name = _scope_label(select)
        reducing = sql_shape.reducing_clauses(select)
        if reducing != ["a WHERE filter"]:
            others = [r for r in reducing if r != "a WHERE filter"]
            return self._refuse(name, ", ".join(others) or "its shape")
        if sql_shape.joins(select):
            return self._refuse(name, "a join beside its filter")
        source = sql_shape.from_relation(select)
        if source is None:
            return self._refuse(name, "no relation to filter")
        for predicate in sql_shape.predicates(select, "where"):
            why = sql_shape.simple_row_predicate(predicate, self.dialect)
            if why is not None:
                return self._refuse(name, why)
            lowered = _lower_predicate(predicate, source, cte_scopes, sql_shape, seen)
            if lowered is None:
                return self._refuse(
                    name,
                    "the filter reads a column the CTE chain computes, aggregates "
                    "or cannot trace to one relation",
                )
            table, condition = lowered
            if sql_shape.simple_row_predicate(condition, self.dialect) is not None:
                return self._refuse(
                    name,
                    "the filter reads a column the CTE chain computes with an "
                    "expression dex does not carry",
                )
            self.filters.append((_table_name(table), condition.sql(dialect="duckdb")))
        if name not in self.scopes:
            self.scopes.append(name)
        return True

    def narrows(self, select, source, sql_shape) -> bool:
        """Whether one of ``select``'s own joins can drop rows of ``source``,
        the relation in it a column is read from; record why if so (#1159).

        Joins associate left to right, so ``source`` keeps every row only when
        the join that brings it in (if any) keeps its right side, a RIGHT or
        FULL join, and every join after that keeps its left side, a LEFT or
        FULL join. An inner join, a bare JOIN, a comma join and a CROSS JOIN
        can all drop rows (a CROSS JOIN with an empty side, or an UNNEST of an
        empty array, keeps none), so probing the whole relation underneath
        would count keys the model never joins. Semi and anti joins never
        reach here: :func:`~..sql_shape.reducing_clauses` already names them.
        """

        if not self.keep_rows:
            return False
        joins = sql_shape.joins(select)
        start = next((i for i, j in enumerate(joins) if j.relation is source), None)
        for i, join in enumerate(joins):
            if start is not None and i < start:
                continue
            side = join.side.split(" ")[0]
            kept = ("right", "full") if i == start else ("left", "full")
            if side not in kept:
                self._refuse(
                    _scope_label(select),
                    f"its {join.label}, which can drop rows of "
                    f"`{source.alias_or_name}`",
                )
                return True
        return False

    def _refuse(self, name: str, why: str) -> bool:
        self.fail(
            "filtered_cte",
            f"a join column is read through {name}, which reduces its rows with "
            f"a shape dex does not carry into a probe ({why}), so probing the "
            "physical relation underneath would measure rows the model never "
            "joins",
        )
        return False


def _scope_label(select) -> str:
    """How a filtering scope reads in a record: ``cte `name``` or ``a subquery``."""

    from sqlglot import expressions as exp

    parent = select.parent
    if isinstance(parent, exp.CTE):
        return f"CTE `{parent.alias_or_name}`"
    return "a subquery"


def _lower_predicate(predicate, relation, cte_scopes: dict, sql_shape, seen):
    """``predicate``, written over ``relation``'s columns, rewritten over the
    physical relation underneath it: ``(physical table node, condition)`` with
    every column unqualified and quoted, or ``None`` where a hop computes a
    column it reads, or its columns come from more than one relation.

    Each CTE or subquery hop is crossed with
    :func:`~..sql_shape.substitute_columns`, which keeps the rows a filter
    keeps when the hop is row-by-row. That hop's own filters are not collected
    here: the join column's resolution passes through the same hop and
    collects them (:meth:`_FilterWalk.carry`).
    """

    from sqlglot import expressions as exp

    current, condition, visited = relation, predicate.copy(), set(seen)
    for _ in range(16):
        if isinstance(current, exp.Subquery):
            body = current.this
        elif isinstance(current, exp.Table):
            name = _cte_name(current, cte_scopes)
            if name is None:
                bare = condition.copy()
                for column in list(bare.find_all(exp.Column)):
                    replacement = exp.column(column.name, quoted=True)
                    if column is bare:
                        bare = replacement
                    else:
                        column.replace(replacement)
                return current, bare
            if name in visited:  # pragma: no cover - a cycle needs invalid SQL
                return None
            visited.add(name)
            body = cte_scopes[name]
        else:
            return None
        if not isinstance(body, exp.Select):
            return None
        condition = sql_shape.substitute_columns(condition, body)
        if condition is None:
            return None
        relations = _scope_relations(body, sql_shape)
        sources = {
            id(src): src
            for column in condition.find_all(exp.Column)
            if (src := _qualified_source(column, relations)) is not None
        }
        if len(sources) != 1 or any(
            _qualified_source(column, relations) is None
            for column in condition.find_all(exp.Column)
        ):
            return None
        (current,) = sources.values()
    return None  # pragma: no cover - a chain deeper than any compiled model


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


def _resolve_within_scope(
    select, column_name: str, cte_scopes: dict, sql_shape, seen, walk=None
):
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
    scalar aggregate, a DISTINCT, a LIMIT, a semi or anti join; for the join
    contract, also a join that can drop rows of the relation the column is
    read from, :meth:`_FilterWalk.narrows`, #1159). ``select * from customers where
    active = true``
    projects the same column names the physical ``customers`` table has,
    but not the same rows: probing a join against the whole table when the
    project's own SQL joins against only the active subset can report a
    join healthy that has zero real overlap once the filter is honored. The
    join is skipped instead, not measured against semantics it does not
    have.

    With a ``walk`` (:class:`_FilterWalk`, the join contract's own resolution,
    #1085), one reducing shape is resolved through instead: a scope reading one
    relation whose only reducing clause is a WHERE of simple row predicates
    (:func:`~..sql_shape.simple_row_predicate`). Each predicate is carried down
    to the physical relation and collected on the walk, so the probe can
    measure the rows the model actually joins rather than the whole table.
    Every other shape is recorded on the walk as the reason it stays unchecked.
    """

    from sqlglot import expressions as exp

    if sql_shape.reducing_clauses(select) and (
        walk is None or not walk.carry(select, cte_scopes, sql_shape, seen)
    ):
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
                if source is None or (walk and walk.narrows(select, source, sql_shape))
                else _resolve_join_column(
                    source, inner.name, cte_scopes, sql_shape, seen, walk
                )
            )
        if (
            isinstance(proj, exp.Column)
            and proj.alias_or_name.lower() == column_name.lower()
        ):
            source = _qualified_source(proj, scope_relations)
            return (
                None
                if source is None or (walk and walk.narrows(select, source, sql_shape))
                else _resolve_join_column(
                    source, proj.name, cte_scopes, sql_shape, seen, walk
                )
            )
    if saw_unqualified_star and len(scope_relations) == 1:
        (only_source,) = scope_relations.values()
        return _resolve_join_column(
            only_source, column_name, cte_scopes, sql_shape, seen, walk
        )
    return None


def _resolve_join_column(
    node, column_name: str, cte_scopes: dict, sql_shape, seen=None, walk=None
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
        return _resolve_within_scope(
            inner, column_name, cte_scopes, sql_shape, seen, walk
        )

    if not isinstance(node, exp.Table):
        return None
    cte_name = _cte_name(node, cte_scopes)
    if cte_name is None:
        return node, column_name
    if cte_name in seen:  # pragma: no cover - a cycle needs invalid SQL
        return None
    definition = cte_scopes[cte_name]
    if not isinstance(definition, exp.Select):
        if walk is not None:
            walk.fail("set_operation_cte", _UNRESOLVED_REASONS["set_operation_cte"])
        # A set-operation CTE (a UNION and friends): its output column has
        # no single physical source to resolve to -- each branch can read
        # from an entirely different relation for the "same" column -- so
        # this is unresolvable rather than a guess at which branch answers.
        return None
    return _resolve_within_scope(
        definition, column_name, cte_scopes, sql_shape, seen | {cte_name}, walk
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

    Returns ``(pairs, skip)``: ``skip`` is ``None`` with empty
    ``pairs`` for a join that carries neither (a CROSS JOIN, an UNNEST --
    not a candidate for this check at all, not something to note either),
    and a ``(code, reason)`` pair with empty ``pairs`` for one that carries a real
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
            return [], ("ambiguous_using", reason)
        (from_relation,) = in_scope_relations
        from_alias = from_relation.alias_or_name
        to_alias = join.relation.alias_or_name
        return [
            (
                exp.column(col, table=from_alias),
                exp.column(col, table=to_alias),
                None,
                None,
            )
            for col in join.using
        ], None
    if join.on is None:
        return [], None  # a CROSS JOIN/UNNEST: not this check's business
    plain = sql_shape.conjunctive_equality_pairs(join.on)
    if plain is not None:
        return [(left, right, None, None) for left, right in plain], None
    # A key compared through a cast or a lexical normalization (#861) is still
    # a key: the probe applies the same conversion, and counts the values it
    # fails on apart, rather than leaving the join unchecked.
    converted = sql_shape.conjunctive_conversion_pairs(join.on)
    if converted is not None:
        return [
            (
                left,
                right,
                sql_shape.measuring_key_template(left_side, left),
                sql_shape.measuring_key_template(right_side, right),
            )
            for left, right, left_side, right_side in converted
        ], None
    if _pairs_keys_and_filters(join.on):
        # `on o.customer_id = c.customer_id and o.status <> 'cancelled'`:
        # the keys are there, but the filter beside them narrows which rows
        # can match. A probe of the keys alone counts the rows that filter
        # excludes as matches, so a parent whose only matches are excluded
        # (every order cancelled) would read as covered when it is not.
        return [], (
            "key_and_filter_condition",
            "its condition filters as well as pairing keys (a conjunct "
            "that is not a column-to-column equality), and an overlap "
            "probe on the keys alone would count the rows that filter "
            "excludes as matches, hiding the rows left with none",
        )
    if sql_shape.rewrites_a_key(join.on):
        return [], (
            "unsupported_key_expression",
            "a key is rewritten by an expression dex does not measure (only "
            "casts and lower, upper and trim are), so its coverage cannot be "
            "probed",
        )
    return [], ("non_equality_join", "no conjunctive column-to-column equality")


def _pairs_keys_and_filters(on) -> bool:
    """Whether an ON's top-level conjuncts include a column-to-column equality
    beside something else, as opposed to holding no key equality at all."""

    from sqlglot import expressions as exp

    stack, conjuncts = [on], []
    while stack:
        node = stack.pop()
        if isinstance(node, exp.And):
            stack.extend((node.this, node.expression))
        elif isinstance(node, exp.Paren):
            stack.append(node.this)
        else:
            conjuncts.append(node)
    return any(
        isinstance(c, exp.EQ)
        and isinstance(c.this, exp.Column)
        and isinstance(c.expression, exp.Column)
        for c in conjuncts
    )


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
    return sql_shape.select_branches(tree)


def _model_joins(
    tree, sql_shape, dialect: str | None = None
) -> tuple[list[JoinCandidate], list[JoinSkip]]:
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
    skipped: list[JoinSkip] = []
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

            pairs, skip = _join_equality_pairs(join, in_scope_relations, sql_shape)
            if skip is not None:
                skipped.append(JoinSkip(join.label, *skip))
                continue
            if not pairs:
                continue  # a CROSS JOIN/UNNEST: not this check's business

            # Grouped by (from relation, to relation): every equality pair
            # between the same two relations is one composite key, not one
            # candidate per pair (see JoinCandidate's own docstring).
            grouped: dict[
                tuple[str, str, tuple, tuple, tuple, tuple],
                list[tuple[str, str, str | None, str | None]],
            ] = {}
            unresolved = False
            unresolved_code = "unresolved_column"
            unresolved_reason: str | None = None
            for left, right, left_tpl, right_tpl in pairs:
                left_relation = alias_map.get((left.table or "").lower())
                right_relation = alias_map.get((right.table or "").lower())
                if left_relation is None or right_relation is None:
                    unresolved = True
                    break
                # Only a set operation is decided by the first hop's shape. A
                # filtered first hop may now be carried (#1085), so whether it
                # is the reason is the walk's to record, at any depth; a column
                # that fails for another reason (a computed one) stays
                # `unresolved_column`.
                for relation in (left_relation, right_relation):
                    if (
                        _cte_shape(relation, cte_scopes, sql_shape)
                        == "set_operation_cte"
                    ):
                        unresolved_code = "set_operation_cte"
                # Each side resolves with its own walk, which carries a
                # supported filter down to the physical relation (#1085) and
                # records why an unsupported one stays unchecked.
                left_walk = _FilterWalk(dialect, keep_rows=True)
                right_walk = _FilterWalk(dialect, keep_rows=True)
                left_resolved = _resolve_join_column(
                    left_relation, left.name, cte_scopes, sql_shape, walk=left_walk
                )
                right_resolved = _resolve_join_column(
                    right_relation, right.name, cte_scopes, sql_shape, walk=right_walk
                )
                if left_resolved is None or right_resolved is None:
                    failed = left_walk if left_resolved is None else right_walk
                    if failed.code is not None:
                        unresolved_code, unresolved_reason = failed.code, failed.reason
                    unresolved = True
                    break
                mismatch = next(
                    (
                        walk
                        for walk, (table, _c) in (
                            (left_walk, left_resolved),
                            (right_walk, right_resolved),
                        )
                        if any(name != _table_name(table) for name, _f in walk.filters)
                    ),
                    None,
                )
                if mismatch is not None:
                    unresolved = True
                    unresolved_code = "filtered_cte"
                    unresolved_reason = (
                        "a join column is read through "
                        + " and ".join(mismatch.scopes)
                        + ", whose filter reads a different relation than the "
                        "join key, so it cannot be carried onto the relation the "
                        "probe reads"
                    )
                    break
                left_table, left_col = left_resolved
                right_table, right_col = right_resolved
                left_side = (left_walk, left_table, left_col, left_tpl)
                right_side = (right_walk, right_table, right_col, right_tpl)
                if (left.table or "").lower() == join_alias:
                    to_side, from_side = left_side, right_side
                elif (right.table or "").lower() == join_alias:
                    to_side, from_side = right_side, left_side
                else:
                    unresolved = True
                    break
                from_walk, from_table, from_col, from_tpl = from_side
                to_walk, to_table, to_col, to_tpl = to_side
                from_name, to_name = _table_name(from_table), _table_name(to_table)
                group_key = (
                    from_name,
                    to_name,
                    _distinct(f for _n, f in from_walk.filters),
                    _distinct(f for _n, f in to_walk.filters),
                    tuple(from_walk.scopes),
                    tuple(to_walk.scopes),
                )
                grouped.setdefault(group_key, []).append(
                    (from_col, to_col, from_tpl, to_tpl)
                )
            if unresolved:
                # Any unresolvable component drops the whole join, not just
                # that pair: a composite key probed on only the pairs that
                # happened to resolve is a narrower question than the join
                # itself asks, and can read a genuinely broken join as
                # healthy (#228's own review).
                skipped.append(
                    JoinSkip(
                        join.label,
                        unresolved_code,
                        unresolved_reason
                        or _UNRESOLVED_REASONS.get(
                            unresolved_code,
                            "a join column could not be resolved to a physical "
                            "relation and column",
                        ),
                    )
                )
                continue
            for group_key, column_pairs in grouped.items():
                from_name, to_name, from_filters, to_filters = group_key[:4]
                from_scopes, to_scopes = group_key[4:]
                from_cols = tuple(c for c, _t, _f, _g in column_pairs)
                to_cols = tuple(c for _c, c, _f, _g in column_pairs)
                from_keys = tuple(f for _c, _t, f, _g in column_pairs)
                to_keys = tuple(g for _c, _t, _f, g in column_pairs)
                converts = any(from_keys) or any(to_keys)
                if (
                    from_name == to_name
                    and from_cols == to_cols
                    and not converts
                    and from_filters == to_filters
                ):
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
                        label=join.label,
                        from_keys=from_keys if converts else (),
                        to_keys=to_keys if converts else (),
                        from_filters=from_filters,
                        to_filters=to_filters,
                        from_scopes=from_scopes,
                        to_scopes=to_scopes,
                    )
                )

    return candidates, skipped


def _distinct(items) -> tuple[str, ...]:
    """``items`` in first-seen order with repeats dropped."""

    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return tuple(out)


#: Why a join's columns did not resolve, by the shape of the CTE it reads.
_UNRESOLVED_REASONS = {
    "set_operation_cte": (
        "a join column is read through a CTE whose body is a UNION or another "
        "set operation, which is not a warehouse relation and has no single "
        "physical column to probe"
    ),
    "filtered_cte": (
        "a join column is read through a CTE that filters, groups or limits its "
        "rows in a shape dex does not carry into a probe, so probing the "
        "physical relation underneath would measure rows the model never joins"
    ),
}


def _cte_shape(relation, cte_scopes: dict, sql_shape) -> str | None:
    """Why a join side that names a CTE cannot be probed physically, if the
    CTE's own shape is the reason: a set operation (#840: never mistaken for
    a warehouse table) or a scope that reduces rows. First hop only. The join
    contract's own resolution (:class:`_FilterWalk`) overrides this with a more
    precise answer: a supported filter is carried rather than refused (#1085),
    and an unsupported one at any depth is named with its shape."""

    from sqlglot import expressions as exp

    name = _cte_name(relation, cte_scopes)
    if name is None:
        return None
    body = cte_scopes[name]
    if not isinstance(body, exp.Select):
        return "set_operation_cte"
    if sql_shape.reducing_clauses(body):
        return "filtered_cte"
    return None


@dataclass
class ModelJoinContext:
    """What a model's own SQL and declaration say about its joins (#857).

    ``lineage`` maps each output column (lowered) to the physical relation and
    column it reads, as far as the compiled SQL lets that be followed; a
    computed column has no entry. ``columns`` is each output column's declared
    ``config.meta.dex`` (lowered keys). ``parents`` maps every project node
    name (lowered) to its relation, so a ``references`` can be lined up with
    the relation a join reads. ``assumptions`` is the model's declared
    ``assumptions`` entries and ``population_filters`` the columns its
    ``population.filters`` entries name (lowered), which say an INNER join's
    unmatched rows are the population the model means to drop (#1162).
    """

    relation: str | None = None
    lineage: dict[str, tuple[str, str]] = field(default_factory=dict)
    columns: dict[str, dict] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    assumptions: list[dict] = field(default_factory=list)
    population_filters: tuple[str, ...] = ()


@dataclass
class JoinContractPlan:
    """Every selected model's equality joins, the joins that could not be
    turned into a probe (with a typed reason), and what the model declares."""

    candidates: dict[str, list[JoinCandidate]] = field(default_factory=dict)
    skips: dict[str, list[JoinSkip]] = field(default_factory=dict)
    context: dict[str, ModelJoinContext] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def plan_join_contract(
    project_dir: Path, dialect: str, *, scope: set[str] | None = None
) -> JoinContractPlan:
    """Every selected model's own equality joins, read from its compiled SQL
    (#228, #857). Free: parses artifacts already on disk, opens no connection.

    ``dialect`` is the connector's own: compiled SQL is written in the project's
    warehouse dialect, and parsing it as DuckDB regardless would misread
    dialect-specific syntax on every other connector.

    ``scope`` holds model unique ids or lowered names, the convention
    :func:`row_population_plan` follows, so a build (ids) and a `maintain
    verify` call (names) narrow this the same way.

    Compiled SQL that does not parse is a typed skip (``unparseable_sql``), not
    a silent absence: its joins are an obligation nothing checked.
    """

    from .. import sql_shape
    from ..guards.pii_meta import entry_meta

    nodes = _manifest_nodes(project_dir)
    if nodes is None:
        return JoinContractPlan(
            notes=[
                "no compiled manifest found; run `dbt compile` or `dbt build` "
                "for join-contract findings"
            ]
        )

    import sqlglot

    parents: dict[str, str] = {}
    for node in nodes.values():
        if (
            isinstance(node, dict)
            and node.get("resource_type") in ("model", "seed", "snapshot")
            and isinstance(node.get("name"), str)
            and isinstance(node.get("relation_name"), str)
            and node["relation_name"]
        ):
            parents.setdefault(
                node["name"].lower(), strip_relation_quoting(node["relation_name"])
            )

    plan = JoinContractPlan()
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = node.get("name")
        if not (isinstance(name, str) and name):
            continue
        if scope is not None and uid not in scope and name.lower() not in scope:
            continue
        sql = node.get("compiled_code")
        if not isinstance(sql, str) or not sql.strip():
            continue
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # unparseable compiled SQL, not our defect
            plan.skips[name] = [
                JoinSkip(
                    "",
                    "unparseable_sql",
                    "the compiled SQL could not be parsed (a macro or dynamic SQL "
                    "dex cannot read), so none of its joins were checked",
                )
            ]
            continue
        candidates, skipped = _model_joins(tree, sql_shape, dialect)
        if candidates:
            plan.candidates[name] = candidates
        if skipped:
            plan.skips[name] = skipped
        if candidates or skipped:
            relation = node.get("relation_name")
            declared: dict[str, dict] = {}
            columns = node.get("columns")
            for key, column in columns.items() if isinstance(columns, dict) else []:
                if not isinstance(column, dict):
                    continue
                dex = entry_meta(column).get("dex")
                column_name = column.get("name") or key
                if isinstance(dex, dict) and isinstance(column_name, str):
                    declared[column_name.lower()] = dex
            assumptions, population_filters = _model_population_intent(
                entry_meta(node).get("dex")
            )
            plan.context[name] = ModelJoinContext(
                relation=strip_relation_quoting(relation)
                if isinstance(relation, str) and relation
                else None,
                lineage=_output_lineage(tree, sql_shape),
                columns=declared,
                parents=parents,
                assumptions=assumptions,
                population_filters=population_filters,
            )
    return plan


def _model_population_intent(dex) -> tuple[list[dict], tuple[str, ...]]:
    """The model-level ``assumptions`` entries that name a column, and the
    columns its ``population.filters`` entries name (lowered), from its
    ``config.meta.dex`` (#1162)."""

    if not isinstance(dex, dict):
        return [], ()
    stated = dex.get("assumptions")
    assumptions = [
        entry
        for entry in (stated if isinstance(stated, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("column"), str)
        and entry["column"].strip()
    ]
    population = dex.get("population")
    entries = population.get("filters") if isinstance(population, dict) else None
    filters = tuple(
        entry["column"].strip().lower()
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("column"), str)
        and entry["column"].strip()
    )
    return assumptions, filters


def join_contract_plan(
    project_dir: Path, dialect: str, *, scope: set[str] | None = None
) -> tuple[dict[str, list[JoinCandidate]], list[str]]:
    """``(candidates, notes)``: :func:`plan_join_contract`'s candidates, with
    every typed skip rendered as the ``model: join: reason`` line the axis has
    always emitted."""

    plan = plan_join_contract(project_dir, dialect, scope=scope)
    notes = list(plan.notes)
    notes.extend(
        f"{model}: {skip}" for model, skips in plan.skips.items() for skip in skips
    )
    return plan.candidates, notes


def _output_lineage(tree, sql_shape, depth: int = 0) -> dict[str, tuple[str, str]]:
    """Each output column (lowered) mapped to the physical ``(relation, column)``
    it reads, through the CTE chain, where the SQL lets it be followed.

    A computed column (``coalesce(x, 0)``, an aggregate) has no entry: it is not
    a value the join supplies unchanged, so it is not judged as one. The common
    dbt closing ``select * from final`` follows the star into ``final``.
    """

    from sqlglot import expressions as exp

    if depth > 8 or not isinstance(tree, exp.Select):
        return {}
    with_clause = tree.args.get("with_") or tree.args.get("with")
    root = tree if depth == 0 else None
    cte_scopes = (
        {cte.alias_or_name.lower(): cte.this for cte in with_clause.expressions}
        if with_clause is not None
        else {}
    )
    return _select_lineage(tree, cte_scopes, sql_shape, depth, root)


def _select_lineage(select, cte_scopes, sql_shape, depth, root) -> dict:
    from sqlglot import expressions as exp

    if depth > 8:
        return {}
    scope_relations = _scope_relations(select, sql_shape)
    lineage: dict[str, tuple[str, str]] = {}
    for proj in select.expressions:
        if isinstance(proj, exp.Star) and len(scope_relations) == 1:
            (only,) = scope_relations.values()
            cte = _cte_name(only, cte_scopes)
            if cte is not None and isinstance(cte_scopes[cte], exp.Select):
                lineage.update(
                    _select_lineage(
                        cte_scopes[cte], cte_scopes, sql_shape, depth + 1, root
                    )
                )
            continue
        if isinstance(proj, exp.Alias):
            name, inner = proj.alias, proj.this
        elif isinstance(proj, exp.Column):
            name, inner = proj.name, proj
        else:
            continue
        if not isinstance(inner, exp.Column) or not name:
            continue
        source = _qualified_source(inner, scope_relations)
        if source is None:
            continue
        # Through a filter the join contract carries (#1085): an output read
        # through a filtered CTE is still that physical column, so a declared
        # `null_rule: never` on it still makes the join required.
        resolved = _resolve_join_column(
            source, inner.name, cte_scopes, sql_shape, walk=_FilterWalk()
        )
        if resolved is None:
            continue
        table, column = resolved
        lineage[name.lower()] = (_table_name(table), column)
    return lineage


def _same_relation(a: str, b: str) -> bool:
    """Whether two dotted relation names are one relation, compared on as many
    trailing parts as both carry (a compiled SQL name may be fully qualified
    where the manifest's is too, or not)."""

    left = [p for p in a.lower().replace('"', "").replace("`", "").split(".") if p]
    right = [p for p in b.lower().replace('"', "").replace("`", "").split(".") if p]
    n = min(len(left), len(right))
    return n > 0 and left[-n:] == right[-n:]


def _never_null(meta: dict | None) -> bool:
    rule = (meta or {}).get("null_rule")
    return isinstance(rule, str) and rule.strip().lower() == "never"


def _null_allowed(meta: dict | None) -> bool:
    rule = (meta or {}).get("null_rule")
    return isinstance(rule, str) and bool(rule.strip()) and not _never_null(meta)


@dataclass(frozen=True)
class JoinPolicy:
    """What a model's declaration says about one of its joins (#857)."""

    #: The join's columns the model passes through unchanged.
    derived: tuple[str, ...]
    #: The output columns that carry the driving key.
    driving: tuple[str, ...]
    #: Why every eligible driving row must match, or None when nothing says so.
    required_by: str | None
    #: Every column the join supplies is declared as allowed to be NULL.
    declared_optional: bool
    #: NULL driving keys count as unmatched rather than ineligible.
    null_keys_unmatched_by: str | None
    #: On an INNER join the declaration does not require, what in it says the
    #: unmatched driving rows are meant to be dropped: an ``assumptions`` entry
    #: or a ``population.filters`` entry naming the driving key column (#1162).
    unmatched_accounted_for: dict | None = None


def join_policy(candidate: JoinCandidate, ctx: ModelJoinContext | None) -> JoinPolicy:
    """Read a join's obligation from the model's declaration, never from data.

    A join is required when the model declares a column it supplies
    ``null_rule: never`` on a LEFT join (an unmatched row would put a NULL
    there), or declares the column carrying its driving key a ``foreign_key``
    whose ``references`` names the joined relation (every key must exist in the
    parent). NULL driving keys count against coverage only when the column
    carrying them is declared ``null_rule: never``.

    An INNER join that is not required drops its unmatched driving rows, which
    is a filter when the model means it: an ``assumptions`` entry whose
    ``column`` names the driving key column, or a ``population.filters`` entry
    on it, says so, and is returned as what accounts for those rows (#1162).
    """

    if ctx is None:
        return JoinPolicy((), (), None, False, None)
    from_columns = {c.lower() for c in candidate.from_columns}
    derived = tuple(
        sorted(
            col
            for col, (relation, _c) in ctx.lineage.items()
            if _same_relation(relation, candidate.to_relation)
        )
    )
    driving = tuple(
        sorted(
            col
            for col, (relation, column) in ctx.lineage.items()
            if _same_relation(relation, candidate.from_relation)
            and column.lower() in from_columns
        )
    )
    left = candidate.side.split(" ")[0] == "left" if candidate.side else False
    required_by = None
    if left:
        never = [c for c in derived if _never_null(ctx.columns.get(c))]
        if never:
            required_by = (
                f"{never[0]} comes from the joined relation and is declared "
                "null_rule: never"
            )
    if required_by is None:
        for col in driving:
            meta = ctx.columns.get(col) or {}
            target = meta.get("references")
            if meta.get("role") != "foreign_key" or not isinstance(target, str):
                continue
            parent = ctx.parents.get(target.rpartition(".")[0].lower())
            if parent is not None and _same_relation(parent, candidate.to_relation):
                required_by = f"{col} is declared a foreign_key referencing {target}"
                break
    declared_optional = bool(derived) and all(
        _null_allowed(ctx.columns.get(c)) for c in derived
    )
    counted = next((c for c in driving if _never_null(ctx.columns.get(c))), None)
    inner = candidate.side.split(" ")[0] == "inner" if candidate.side else False
    return JoinPolicy(
        derived,
        driving,
        required_by,
        declared_optional,
        f"{counted} is declared null_rule: never" if counted else None,
        _unmatched_accounted_for(from_columns | set(driving), ctx)
        if inner and required_by is None
        else None,
    )


def _unmatched_accounted_for(keys: set[str], ctx: ModelJoinContext) -> dict | None:
    """The declaration entry that names one of ``keys`` (the driving key's
    physical columns and the output columns carrying it, lowered): the first
    such ``assumptions`` entry, else the first ``population.filters`` entry,
    or None (#1162)."""

    for assumption in ctx.assumptions:
        column = assumption.get("column")
        if isinstance(column, str) and column.strip().lower() in keys:
            return {
                "by": "assumption",
                **{
                    k: assumption[k]
                    for k in ("column", "decision", "chosen", "evidence")
                    if assumption.get(k) is not None
                },
            }
    for column in ctx.population_filters:
        if column in keys:
            return {"by": "population_filter", "column": column}
    return None


def _resolve_join_relationships(
    candidates_by_model: dict[str, list[JoinCandidate]],
    model_relations: dict[str, str],
    live_identifiers: list[str] | None,
):
    """Every candidate turned into a :class:`~..cache.Relationship`, both sides
    resolved to exactly one live identifier (or taken as written when
    ``live_identifiers`` is ``None``, at pricing time before a build). Pure and
    adapter-free. Returns ``(pairs, unresolved)``: ``pairs`` keeps each
    relationship's model and candidate beside it, and ``unresolved`` is the
    ``(model, candidate)`` pairs that matched no single live identifier."""

    from ..cache import Relationship, RelationshipKind

    pairs: list[tuple[str, JoinCandidate, Relationship]] = []
    unresolved: list[tuple[str, JoinCandidate]] = []
    for model, candidates in candidates_by_model.items():
        for candidate in candidates:
            if live_identifiers is None:
                from_matches = [candidate.from_relation]
                to_matches = [candidate.to_relation]
            else:
                from_matches = match_identifier(
                    candidate.from_relation, live_identifiers
                )
                to_matches = match_identifier(candidate.to_relation, live_identifiers)
            if len(from_matches) != 1 or len(to_matches) != 1:
                unresolved.append((model, candidate))
                continue
            pairs.append(
                (
                    model,
                    candidate,
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


@dataclass
class _NullCheck:
    """One model's statement counting the non-NULLs of the columns its joins
    supply, on the relation it built (#857)."""

    model: str
    identifier: str
    #: (lowered output column, the relation's spelling of it)
    columns: list[tuple[str, str]]
    sql: str


def _null_checks(
    adapter,
    plan: JoinContractPlan,
    live_identifiers: list[str] | None,
) -> tuple[list[_NullCheck], list[str]]:
    """The NULL-count statements for every model whose joins supply columns.

    Column spellings come from the built relation's metadata where it can be
    listed (a warehouse may have folded the case), and as the SQL wrote them
    before the build has run. Aggregate-only: ``COUNT(column)`` per column.
    """

    from ..explore.relationships import _quote_identifier, _quote_part, _transpile_probe
    from ..guards.cost_guard import CostGuardError
    from ..guards.sql_guard import assert_select_only

    checks: list[_NullCheck] = []
    notes: list[str] = []
    for model, candidates in plan.candidates.items():
        ctx = plan.context.get(model)
        if ctx is None or ctx.relation is None:
            continue
        derived = sorted(
            {c for cand in candidates for c in join_policy(cand, ctx).derived}
        )
        if not derived:
            continue
        if live_identifiers is None:
            identifier = ctx.relation
            spelled = [(c, c) for c in derived]
        else:
            matches = match_identifier(ctx.relation, live_identifiers)
            if len(matches) != 1:
                notes.append(
                    f"{model}: the columns its joins supply were not NULL-checked: "
                    f"no single relation in the warehouse matched {ctx.relation}"
                )
                continue
            identifier = matches[0]
            try:
                _meta, columns = adapter.table_metadata(identifier)
            except CostGuardError:
                raise
            except Exception as exc:  # a metadata read is a note
                notes.append(
                    f"{model}: the columns its joins supply were not NULL-checked: "
                    f"the relation's columns could not be read ({type(exc).__name__})"
                )
                continue
            live = {c.name.lower(): c.name for c in columns}
            spelled = [(c, live[c]) for c in derived if c in live]
        if not spelled:
            continue
        counts = ", ".join(
            f"COUNT({_quote_part(name)}) AS dex_nn_{i}"
            for i, (_c, name) in enumerate(spelled)
        )
        relation = _quote_identifier(identifier)
        sql = f"SELECT COUNT(*) AS dex_rows, {counts} FROM {relation}"  # noqa: S608
        checks.append(
            _NullCheck(
                model,
                identifier,
                spelled,
                assert_select_only(
                    _transpile_probe(sql, adapter.dialect), dialect=adapter.dialect
                ),
            )
        )
    return checks, notes


def _split_unsafe(pairs: list[tuple], dialect: str) -> tuple[list[tuple], list[tuple]]:
    """The pairs a probe can measure on ``dialect``, and those it cannot: a key
    conversion that needs a safe cast where the dialect has none and cannot
    guard one either (#861, #1075)."""

    import sqlglot

    from .. import sql_shape

    if dialect not in sql_shape.NO_SAFE_CAST:
        return pairs, []

    def unsafe_template(template: str | None) -> bool:
        if template is None or "TRY_CAST" not in template.upper():
            return False
        tree = sqlglot.parse_one(template.format("dex_key"), read="duckdb")
        try:
            sql_shape.with_safe_casts(tree, dialect)
        except sql_shape.NoSafeCastError:
            return True
        return False

    keep: list[tuple] = []
    unsafe: list[tuple] = []
    for item in pairs:
        exprs = item[1].key_exprs()
        needs = exprs is not None and any(
            unsafe_template(template) for side in exprs for template in side
        )
        (unsafe if needs else keep).append(item)
    return keep, unsafe


def _key_exprs(pairs) -> dict[int, tuple]:
    """The conversions each probed relationship applies to its keys (#861),
    by the relationship's identity, which is what the probe batches carry."""

    return {
        id(rel): exprs
        for _m, candidate, rel in pairs
        if (exprs := candidate.key_exprs()) is not None
    }


def _scope_filters(pairs) -> dict[int, tuple]:
    """The filters each probed relationship reads its sides through (#1085), by
    the relationship's identity, the twin of :func:`_key_exprs`."""

    return {
        id(rel): filters
        for _m, candidate, rel in pairs
        if (filters := candidate.scope_filters()) is not None
    }


def join_probe_cost(
    adapter,
    relationships: list,
    key_exprs: dict | None = None,
    scope_filters: dict | None = None,
) -> tuple[float, int]:
    """``(estimate, probe_count)`` for measuring every relationship's coverage,
    without running it. Pure pricing, split out so a caller combining this axis
    with another can price both before asking once."""

    from .. import command_args
    from ..explore import relationships as rel_mod

    to_probe = rel_mod.probe_candidates(relationships)
    if not to_probe or command_args.cost_gate(adapter) is None:
        return 0.0, len(to_probe)
    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0, len(to_probe)
    statements = rel_mod.coverage_statements(
        relationships, adapter.dialect, key_exprs=key_exprs, scope_filters=scope_filters
    )
    return sum(estimator(stmt) for stmt in statements), len(to_probe)


def _statements_cost(adapter, statements: list[str]) -> float:
    from .. import command_args

    if not statements or command_args.cost_gate(adapter) is None:
        return 0.0
    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0
    return float(sum(estimator(sql) for sql in statements))


def join_contract_cost(
    project_dir: Path,
    adapter,
    model_relations: dict[str, str],
    live_identifiers: list[str] | None,
    *,
    scope: set[str] | None = None,
) -> tuple[float, int]:
    """``(estimate, statement_count)`` for the whole axis, end to end without
    running anything: the plan, the resolution against the warehouse, the
    coverage probes and the NULL counts. Exists so a combined handshake (or a
    build's upfront estimate, with ``live_identifiers`` ``None``) can price this
    axis beside the others before anything asks."""

    plan = plan_join_contract(project_dir, adapter.dialect, scope=scope)
    pairs, _unresolved = _resolve_join_relationships(
        plan.candidates, model_relations, live_identifiers
    )
    pairs, _unsafe = _split_unsafe(pairs, adapter.dialect)
    relationships = [rel for _m, _c, rel in pairs]
    exprs = _key_exprs(pairs)
    filters = _scope_filters(pairs)
    estimate, _joins = join_probe_cost(adapter, relationships, exprs, filters)
    checks, _notes = (
        _null_checks(adapter, plan, live_identifiers) if pairs else ([], [])
    )
    estimate += _statements_cost(adapter, [c.sql for c in checks])
    return estimate, _statement_count(adapter, relationships, checks, exprs, filters)


def _statement_count(
    adapter,
    relationships: list,
    checks: list,
    key_exprs: dict | None = None,
    scope_filters: dict | None = None,
) -> int:
    """How many statements the axis would issue: the batched coverage probes
    and one NULL count per model, which is what a handshake states it buys."""

    from ..explore import relationships as rel_mod

    probes = rel_mod.coverage_statements(
        relationships, adapter.dialect, key_exprs=key_exprs, scope_filters=scope_filters
    )
    return len(probes) + len(checks) if probes else 0


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
                f"the estimate buys {probe_count} join coverage statement(s), "
                "which is what a broken, sparse or unmatched join is judged from"
            ],
        )

    return ask


@dataclass
class JoinContractResult:
    """The join contract's answer (#857): findings for contradictions, and one
    record per join for everything else, so a satisfied join, an unmatched
    obligation and an empty input are each visible rather than absent."""

    findings: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    offer: object | None = None
    records: list[dict] = field(default_factory=list)


def _skip_record(model: str, skip: JoinSkip, **extra) -> dict:
    return {
        "model": model,
        "join": skip.join or None,
        "status": "unchecked",
        "reason_code": skip.code,
        "reason": skip.reason,
        **extra,
    }


def _candidate_record(model: str, candidate: JoinCandidate, policy: JoinPolicy) -> dict:
    """The join coverage record's declared half: the join, its sides and what the
    declaration says about it, plus any key conversion.
    """
    return {
        "model": model,
        "join": candidate.label or None,
        "side": candidate.side,
        "from_relation": candidate.from_relation,
        "from_columns": list(candidate.from_columns),
        "to_relation": candidate.to_relation,
        "to_columns": list(candidate.to_columns),
        "declared": {
            "required": policy.required_by is not None,
            "required_by": policy.required_by,
            "optional": policy.declared_optional,
            "null_keys": "unmatched" if policy.null_keys_unmatched_by else "excluded",
            "null_keys_by": policy.null_keys_unmatched_by,
            "derived_columns": list(policy.derived),
        },
        **(
            {"key_conversion": _key_conversion(candidate)}
            if candidate.key_exprs() is not None
            else {}
        ),
        **(
            {"effective_input": _effective_input(candidate)}
            if candidate.scope_filters() is not None
            else {}
        ),
    }


def _effective_input(candidate: JoinCandidate) -> dict:
    """Per filtered side, the scopes the join reads it through and the filters
    the probe applied to the physical relation, as the probe ran them (#1085).
    The measured counts are over these rows, not the whole relation."""

    out: dict[str, dict] = {}
    for side, relation, scopes, filters in (
        (
            "from",
            candidate.from_relation,
            candidate.from_scopes,
            candidate.from_filters,
        ),
        ("to", candidate.to_relation, candidate.to_scopes, candidate.to_filters),
    ):
        if filters:
            out[side] = {
                "relation": relation,
                "through": list(scopes),
                "filters": list(filters),
            }
    return out


def _side_label(dataset: str, filters: tuple[str, ...]) -> str:
    """A relation as a finding names it: with the filter the probe read it
    through, so a count is never mistaken for the whole table's (#1085)."""

    if not filters:
        return dataset
    # Parenthesized when there are several, as the probe joins them: a filter
    # may hold an OR, and `a OR b AND c` reads as a different population.
    joined = (
        filters[0] if len(filters) == 1 else " AND ".join(f"({f})" for f in filters)
    )
    return f"{dataset} (rows where {joined})"


def _key_conversion(candidate: JoinCandidate) -> dict:
    """The conversions a join applies to its keys, as the probe applied them
    (a hard cast measured as its safe form), per side (#861)."""

    def side(columns, templates):
        """Each column rendered through its side's conversion template, if any."""
        return [
            template.format(column) if template else column
            for column, template in zip(columns, templates, strict=False)
        ]

    return {
        "from": side(candidate.from_columns, candidate.from_keys),
        "to": side(candidate.to_columns, candidate.to_keys),
    }


def join_coverage(
    adapter,
    plan: JoinContractPlan,
    model_relations: dict[str, str],
    live_identifiers: list[str],
    *,
    timeout_seconds: float = 30.0,
    handshake: Callable[[float, int], object | None] | None = None,
    baseline: dict[str, tuple[str | None, dict[str, float | None]]] | None = None,
) -> JoinContractResult:
    """A model's own joins measured for coverage against real data (#228, #857).

    Each supported equality join gets one record: what the model declares about
    it (:func:`join_policy`), what the coverage probe measured (rows and
    distinct keys, NULL keys apart), and a ``status`` of ``satisfied``,
    ``contradicted``, ``not_applicable`` (nothing eligible: an empty input has
    no coverage, which is not full coverage) or ``unchecked`` with a
    ``reason_code``. Findings, aggregate counts only:

    - ``join_no_match`` (high): a nonempty eligible population matched nothing.
      No threshold: zero is zero. ``low`` where the model declares every column
      the join supplies as allowed to be NULL.
    - ``join_orphans`` / ``join_zero_overlap``: some unmatched rows. Where the
      declaration makes the join required, any unmatched row contradicts it
      (``threshold_source: contract``). Otherwise the default thresholds apply
      (``threshold_source: default``, stated in ``data``), and on a LEFT join
      unmatched rows are the optional relationship a LEFT join writes: recorded
      as evidence, with ``join_orphans`` withheld and ``join_zero_overlap``
      ranked medium. On an INNER join whose unmatched rows the declaration
      accounts for (an ``assumptions`` entry or a ``population.filters`` entry
      naming the driving key column), the join is the model's filter: the
      record is ``satisfied``, keeps its counts and names that entry under
      ``unmatched_accounted_for`` (#1162).
    - ``join_field_all_null`` (high): a column the join supplies, declared
      ``null_rule: never``, is NULL on every row of the built relation.
      ``join_field_newly_null`` (medium): one with no declared rule that is
      NULL on every row now, where the exploration cache profiled the same
      relation with values in it.
    """

    from ..explore import relationships as rel_mod

    result = JoinContractResult(notes=list(plan.notes))
    for model, skips in plan.skips.items():
        for skip in skips:
            result.notes.append(f"{model}: {skip}")
            result.records.append(_skip_record(model, skip))

    pairs, unresolved = _resolve_join_relationships(
        plan.candidates, model_relations, live_identifiers
    )
    for model, candidate in unresolved:
        policy = join_policy(candidate, plan.context.get(model))
        result.records.append(
            {
                **_candidate_record(model, candidate, policy),
                "status": "unchecked",
                "reason_code": "relation_not_found",
                "reason": "a joined relation did not match exactly one relation in "
                "the warehouse",
            }
        )
    pairs, unsafe = _split_unsafe(pairs, getattr(adapter, "dialect", ""))
    for model, candidate, _rel in unsafe:
        policy = join_policy(candidate, plan.context.get(model))
        result.records.append(
            {
                **_candidate_record(model, candidate, policy),
                "status": "unchecked",
                "reason_code": "no_safe_cast_on_connector",
                "reason": "the join converts its key with a cast, and this "
                "connector has no cast that returns NULL instead of failing, so "
                "the values it rejects cannot be counted",
            }
        )
    if unresolved:
        result.notes.append(
            "the join contract was not checked for "
            + ", ".join(sorted({m for m, _c in unresolved}))
            + ": a joined relation did not match exactly one live identifier"
        )
    if not pairs:
        return result

    relationships = [rel for _m, _c, rel in pairs]
    exprs = _key_exprs(pairs)
    filters = _scope_filters(pairs)
    checks, check_notes = _null_checks(adapter, plan, live_identifiers)
    result.notes.extend(check_notes)
    estimate, probe_count = join_probe_cost(adapter, relationships, exprs, filters)
    estimate += _statements_cost(adapter, [c.sql for c in checks])
    statement_count = _statement_count(adapter, relationships, checks, exprs, filters)
    if statement_count == 0:
        return result

    ask = handshake if handshake is not None else _standalone_join_handshake(adapter)
    offer = ask(estimate, statement_count)
    if offer is not None:
        result.notes.append(
            f"the join contract was not measured: {probe_count} join(s) need a "
            "coverage probe, offered above"
        )
        for model, candidate, _rel in pairs:
            policy = join_policy(candidate, plan.context.get(model))
            result.records.append(
                {
                    **_candidate_record(model, candidate, policy),
                    "status": "unchecked",
                    "reason_code": "scan_offered",
                    "reason": "measuring it is a scan, offered above",
                }
            )
        result.offer = offer
        return result

    # Only a join that converts a key passes the conversions, and only one
    # read through a filter passes the filters (#1085), so the call is the one
    # every other join has always made.
    measured, refused = rel_mod.measure_coverage(
        adapter,
        relationships,
        timeout_seconds=timeout_seconds,
        **({"key_exprs": exprs} if exprs else {}),
        **({"scope_filters": filters} if filters else {}),
    )
    counts_of = {id(rel): counts for rel, counts in measured}
    refused_of = {id(rel): text for rel, text in refused}

    nulls = _run_null_checks(adapter, checks, timeout_seconds, result)

    for model, candidate, rel in pairs:
        ctx = plan.context.get(model)
        policy = join_policy(candidate, ctx)
        record = _candidate_record(model, candidate, policy)
        counts = counts_of.get(id(rel))
        if counts is None:
            reason = refused_of.get(id(rel), "the coverage probe returned no row")
            record.update(
                status="unchecked", reason_code="probe_refused", reason=reason
            )
            result.notes.append(f"{model}: {reason}")
            result.records.append(record)
            continue
        finding = _judge_coverage(model, candidate, rel, counts, policy, record)
        if finding is not None:
            result.findings.append(finding)
        _judge_derived_nulls(
            model, candidate, policy, ctx, nulls.get(model), record, result, baseline
        )
        result.records.append(record)
    return result


def _run_null_checks(adapter, checks, timeout_seconds, result) -> dict:
    from ..guards.cost_guard import CostGuardError

    out: dict[str, tuple[str, int, dict[str, int]]] = {}
    for check in checks:
        try:
            answer = adapter.run_query(
                check.sql, max_rows=1, timeout_seconds=timeout_seconds
            )
        except CostGuardError:
            raise
        except Exception as exc:  # one refused count is a note
            result.notes.append(
                f"{check.model}: the columns its joins supply were not NULL-checked: "
                f"the warehouse refused the statement ({type(exc).__name__})"
            )
            continue
        row = {
            str(k).lower(): v
            for k, v in zip(answer.columns, answer.cells[0], strict=True)
        }
        out[check.model] = (
            check.identifier,
            int(row.get("dex_rows") or 0),
            {
                column: int(row.get(f"dex_nn_{i}") or 0)
                for i, (column, _spelled) in enumerate(check.columns)
            },
        )
    return out


def _judge_coverage(
    model: str,
    candidate: JoinCandidate,
    rel,
    counts,
    policy: JoinPolicy,
    record: dict,
) -> DriftFinding | None:
    """Fill ``record`` with the measured counts and verdict; return the finding
    a contradiction earns, or ``None``."""

    counted = policy.null_keys_unmatched_by is not None
    eligible_rows = counts.eligible_rows + (counts.null_key_rows if counted else 0)
    matched_rows = counts.matched_rows
    unmatched_rows = eligible_rows - matched_rows
    eligible_keys, matched_keys = counts.eligible_keys, counts.matched_keys
    coverage_rows = round(matched_rows / eligible_rows, 4) if eligible_rows else None
    coverage_keys = round(matched_keys / eligible_keys, 4) if eligible_keys else None
    required = policy.required_by is not None
    left = candidate.side.split(" ")[0] == "left" if candidate.side else False
    threshold_source = "contract" if required else "default"
    record["measured"] = {
        "rows": counts.rows,
        "null_key_rows": counts.null_key_rows,
        "eligible_rows": eligible_rows,
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "eligible_keys": eligible_keys,
        "matched_keys": matched_keys,
        "unmatched_keys": eligible_keys - matched_keys,
    }
    if getattr(counts, "conversion_failed_rows", None) is not None:
        # Non-NULL keys the join's conversion turns into NULL: they stay in the
        # eligible population as unmatched, and are counted apart so a reader
        # can tell a failed cast from a missing parent (#861).
        record["measured"]["conversion_failed_rows"] = counts.conversion_failed_rows
        record["measured"]["conversion_failed_keys"] = counts.conversion_failed_keys
    record["coverage"] = {"rows": coverage_rows, "keys": coverage_keys}
    record["threshold_source"] = threshold_source
    record["thresholds"] = (
        {"unmatched_rows": 0}
        if required
        else {
            "join_zero_overlap": _JOIN_ZERO_OVERLAP_THRESHOLD,
            "join_orphans": _JOIN_ORPHANS_THRESHOLD,
        }
    )
    child = _side_label(rel.from_dataset, candidate.from_filters)
    parent = _side_label(rel.to_dataset, candidate.to_filters)
    if eligible_rows == 0:
        record.update(
            status="not_applicable",
            reason_code="empty_input" if counts.rows == 0 else "all_keys_null",
            reason=(
                f"{child} has no rows, so the join has no coverage to "
                "judge (undefined, not complete)"
                if counts.rows == 0
                else f"every {child} row has a NULL key, and the "
                "declaration does not count NULL keys, so nothing is eligible"
            ),
        )
        return None

    orphan_fraction = round(unmatched_rows / eligible_rows, 4)
    from_key = ", ".join(f"{rel.from_dataset}.{c}" for c in rel.from_columns)
    to_key = ", ".join(f"{rel.to_dataset}.{c}" for c in rel.to_columns)
    measured = (
        f"{unmatched_rows} of {eligible_rows} eligible {child} row(s) "
        f"({orphan_fraction:.0%}) and {eligible_keys - matched_keys} of "
        f"{eligible_keys} distinct key(s) have no match in {parent}"
    )
    if counts.null_key_rows:
        measured += f"; {counts.null_key_rows} row(s) with a NULL key are " + (
            "counted as unmatched" if counted else "excluded"
        )
    data = {
        "model": model,
        "join": candidate.label or None,
        "side": candidate.side,
        "from_relation": rel.from_dataset,
        "from_columns": list(rel.from_columns),
        "to_relation": rel.to_dataset,
        "to_columns": list(rel.to_columns),
        "orphan_fraction": orphan_fraction,
        **record["measured"],
        "coverage_rows": coverage_rows,
        "coverage_keys": coverage_keys,
        "threshold_source": threshold_source,
        "required_by": policy.required_by,
        **(
            {"effective_input": record["effective_input"]}
            if "effective_input" in record
            else {}
        ),
    }
    scoped = (
        f" (counted over {child} against {parent})"
        if candidate.scope_filters() is not None
        else ""
    )

    accounted = policy.unmatched_accounted_for if unmatched_rows else None
    if matched_rows == 0:
        code = "join_no_match"
        severity = "low" if policy.declared_optional and not required else "high"
        detail = (
            f"'{model}' joins ({from_key}) to ({to_key}), and none of its "
            f"{eligible_rows} eligible row(s) ({eligible_keys} distinct key(s)) "
            f"match{scoped}: every column the join supplies is NULL or every row "
            "is dropped. The keys share no value; check the join columns and the "
            "relation the model is driven from"
        )
        if policy.declared_optional and not required:
            data["declared_optional"] = True
    elif required and unmatched_rows > 0:
        code = (
            "join_zero_overlap"
            if orphan_fraction >= _JOIN_ZERO_OVERLAP_THRESHOLD
            else "join_orphans"
        )
        severity = "high"
        detail = (
            f"'{model}' joins ({from_key}) to ({to_key}) and requires every "
            f"eligible row to match ({policy.required_by}), but {measured}"
        )
        data["threshold"] = 0
    elif orphan_fraction >= _JOIN_ZERO_OVERLAP_THRESHOLD:
        code = "join_zero_overlap"
        severity = "medium" if left else "high"
        detail = (
            f"'{model}' joins ({from_key}) to ({to_key}), but {measured}, at or "
            f"above the default {_JOIN_ZERO_OVERLAP_THRESHOLD:.0%}"
            + (
                "; a LEFT join keeps those rows with NULLs, which nothing in the "
                "declaration rules out"
                if left
                else ""
            )
        )
        data["threshold"] = _JOIN_ZERO_OVERLAP_THRESHOLD
    elif orphan_fraction >= _JOIN_ORPHANS_THRESHOLD and not left:
        code, severity = "join_orphans", "medium"
        detail = (
            f"'{model}' joins ({from_key}) to ({to_key}), but {measured}, at or "
            f"above the default {_JOIN_ORPHANS_THRESHOLD:.0%}"
        )
        data["threshold"] = _JOIN_ORPHANS_THRESHOLD
    else:
        record["status"] = "satisfied"
        if left and unmatched_rows:
            record["optional_unmatched"] = True
        if accounted is not None:
            record["unmatched_accounted_for"] = dict(accounted)
        return None

    if accounted is not None and code != "join_no_match":
        # An INNER join used as a filter, and the declaration says so: the
        # rows it drops are the population the model means (#1162). The counts
        # stay on the record, with the finding the default would have raised.
        record["status"] = "satisfied"
        record["unmatched_accounted_for"] = {**accounted, "withheld_finding": code}
        return None

    record["status"] = "contradicted"
    record["finding"] = code
    return DriftFinding(
        axis="join_contract",
        code=code,
        identifier=model,
        column=rel.from_columns[0] if len(rel.from_columns) == 1 else None,
        severity=severity,
        detail=detail,
        data=data,
    )


def _judge_derived_nulls(
    model: str,
    candidate: JoinCandidate,
    policy: JoinPolicy,
    ctx: ModelJoinContext | None,
    measured: tuple[str, int, dict[str, int]] | None,
    record: dict,
    result: JoinContractResult,
    baseline: dict[str, tuple[str | None, dict[str, float | None]]] | None,
) -> None:
    """Columns the join supplies that are NULL on every row of the built model.

    A finding only where something says the column should hold values: its
    declared ``null_rule: never``, or a cached profile of the same relation
    that saw values in it. Otherwise the column is listed on the record and
    left there, since an optional field that is empty is not by itself a defect.
    """

    if measured is None or not policy.derived:
        return
    identifier, rows, non_null = measured
    if rows == 0:
        return
    empty = [c for c in policy.derived if non_null.get(c) == 0]
    if not empty:
        return
    record["derived_columns_all_null"] = empty
    profile = None
    profiled_at = None
    if baseline:
        keys = match_identifier(identifier, list(baseline))
        if len(keys) == 1:
            profiled_at, profile = baseline[keys[0]]
    record["null_baseline"] = "cache" if profile is not None else "none"
    already = {
        (f.identifier, f.column)
        for f in result.findings
        if f.code in ("join_field_all_null", "join_field_newly_null")
    }
    for column in empty:
        if (model, column) in already:
            continue
        meta = (ctx.columns.get(column) if ctx else None) or {}
        data = {
            "model": model,
            "column": column,
            "join": candidate.label or None,
            "to_relation": candidate.to_relation,
            "rows": rows,
            "coverage_rows": (record.get("coverage") or {}).get("rows"),
        }
        if _never_null(meta):
            result.findings.append(
                DriftFinding(
                    axis="join_contract",
                    code="join_field_all_null",
                    identifier=model,
                    column=column,
                    severity="high",
                    detail=(
                        f"'{model}.{column}' is declared null_rule: never and comes "
                        f"from {candidate.to_relation}, but it is NULL on all {rows} "
                        "row(s): the join supplying it matched nothing the model "
                        "kept. See the join's coverage record for the counts"
                    ),
                    data=data,
                )
            )
            continue
        before = (profile or {}).get(column)
        if before is not None and before < 1:
            result.findings.append(
                DriftFinding(
                    axis="join_contract",
                    code="join_field_newly_null",
                    identifier=model,
                    column=column,
                    severity="medium",
                    detail=(
                        f"'{model}.{column}' comes from {candidate.to_relation} and "
                        f"is NULL on all {rows} row(s) now, where the last profile "
                        f"of {identifier} found {1 - before:.0%} of it filled: the "
                        "join supplying it stopped matching"
                    ),
                    data={
                        **data,
                        "baseline": "cache",
                        "baseline_null_fraction": before,
                        "baseline_profiled_at": profiled_at,
                    },
                )
            )


def join_contract_findings(
    adapter,
    candidates_by_model: dict[str, list[JoinCandidate]],
    model_relations: dict[str, str],
    live_identifiers: list[str],
    *,
    timeout_seconds: float = 30.0,
    handshake: Callable[[float, int], object | None] | None = None,
) -> tuple[list[DriftFinding], list[str], object | None]:
    """``(findings, notes, offer)`` for candidates with no declaration context:
    :func:`join_coverage` over a plan holding only ``candidates_by_model``."""

    result = join_coverage(
        adapter,
        JoinContractPlan(candidates=candidates_by_model),
        model_relations,
        live_identifiers,
        timeout_seconds=timeout_seconds,
        handshake=handshake,
    )
    return result.findings, result.notes, result.offer


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
            [f"the row counts could not be read ({failure_text(exc)})"],
        )
    row = dict(zip(result.columns, result.cells[0], strict=True))
    counted = set()
    for index, relation in enumerate(to_count):
        value = row.get(f"dex_rows_{index}")
        if value is not None:
            counts[relation] = int(value)
            counted.add(relation)
    return RelationCounts(counts, counted, absent, set(to_count) - counted, None, [])
