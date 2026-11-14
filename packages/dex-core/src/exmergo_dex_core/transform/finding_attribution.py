"""Which of a build's findings the plan introduced, and which were there before (#1230).

A ``--verify`` build of a real project reports every finding on every model it
swept, and most of them predate the change under review. An agent reading the
list sees one new high-severity finding among dozens of inherited ones and,
reasonably, dismisses the whole list as inherited. The defect the plan caused
then ships looking like a success.

This module keeps the two apart. Each finding is classified as one of:

* ``introduced``: the plan's doing, as far as the evidence goes. Either the
  model is one the plan created (a new model has no history to inherit), or the
  last sweep recorded before the plan was applied looked at the same model, ran
  the same class of check, and did not report it.
* ``existing``: not the plan's doing. Either that earlier sweep reported the same
  finding, or the model is outside everything the plan's edits can reach in the
  manifest's lineage, so nothing the plan changed is upstream of it.
* ``unattributed``: neither could be shown. No sweep before the plan looked at
  the model, or one did and suppressed that class of check.

``basis`` says which rule decided, so a reader can weigh ``existing`` by lineage
differently from ``existing`` by baseline.

The baseline is a short history of sweeps written beside the build's own
artifacts (:data:`HISTORY`), one entry per ``--verify`` build: the target, when
it ran, the models it judged, the finding classes it suppressed, and one
fingerprint per finding. A fingerprint names the check and where it fired
(axis, code, model, column, join), never a count or a value, so it survives the
counts moving and carries nothing the envelope would withhold.

Nothing here changes a status. Reading or writing the history is best effort:
a history that cannot be read is no baseline, never an error.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

#: The sweep history, written beside ``dex_previous_build.json``.
HISTORY = "dex_verify_history.json"

#: How many sweeps the history keeps. A plan is normally applied within a few
#: builds of the sweep before it; the cap keeps the file small.
MAX_SWEEPS = 10

INTRODUCED = "introduced"
UNATTRIBUTED = "unattributed"
EXISTING = "existing"

#: Reading order: what the plan did, what nothing can place, what was there.
ORDER = {INTRODUCED: 0, UNATTRIBUTED: 1, EXISTING: 2}

#: The suppression keys (``data.verification.suppressed``) that stand for each
#: finding axis. A baseline that suppressed one of them did not look, so its
#: silence on that axis is not evidence the finding is new.
_AXIS_CLASSES: dict[str, frozenset[str]] = {
    "build": frozenset({"build_status", "column_contract"}),
    "row_population": frozenset({"row_population"}),
    "join_contract": frozenset({"join_contract"}),
    "grain": frozenset({"grain"}),
    "model_versions": frozenset({"model_versions"}),
    "retirement": frozenset({"retirement"}),
    "toggle": frozenset({"toggle"}),
    # Read per code below: one axis, several classes, and a sweep routinely
    # suppresses some of them ("nothing declares a unit") while running others.
    "declaration": frozenset(
        {"unit", "additivity", "population", "parent_population", "temporal"}
    ),
}

#: The suppression keys that stand for each declared-fact finding, by code or
#: code prefix. ``population_delta`` is never named in ``suppressed`` (#1228),
#: so a baseline that swept the model ran it.
_CODE_CLASSES: tuple[tuple[str, frozenset[str]], ...] = (
    ("unit_out_of_range", frozenset({"unit"})),
    ("additivity_contradicted", frozenset({"additivity"})),
    ("population_contradicted", frozenset({"population"})),
    ("population_delta", frozenset()),
    ("parent_population_", frozenset({"parent_population"})),
    ("temporal_", frozenset({"temporal"})),
    ("no_entity_comparable", frozenset({"temporal"})),
    ("policy_missing", frozenset({"temporal"})),
)


def _classes(axis: str, code: str) -> frozenset[str] | None:
    """The suppression keys a finding's check runs under, or ``None`` when
    unknown (which never counts as having run)."""

    if axis == "declaration":
        for prefix, classes in _CODE_CLASSES:
            if code.startswith(prefix):
                return classes
    return _AXIS_CLASSES.get(axis)


#: Edit kinds whose reach the manifest's lineage cannot bound: a macro, the
#: project config or the packages can change how any model compiles.
_UNBOUNDED_KINDS = frozenset(
    {"macro_sql", "project_yml", "packages_yml", "profiles_yml", "semantic_yml"}
)
#: Edit kinds whose own path is a node's file.
_NODE_KINDS = frozenset({"model_sql", "snapshot_sql", "seed_csv", "test_sql"})


def _value(finding: Any, key: str) -> Any:
    if isinstance(finding, Mapping):
        return finding.get(key)
    return getattr(finding, key, None)


def fingerprint(finding: Any) -> str:
    """Where a finding fired and which check raised it, with no count or value.

    The join label is part of it, so two joins on one model that both orphan
    rows stay two findings.
    """

    data = _value(finding, "data") or {}
    join = data.get("join") if isinstance(data, Mapping) else None
    return "|".join(
        str(part or "")
        for part in (
            _value(finding, "axis"),
            _value(finding, "code"),
            _value(finding, "identifier"),
            _value(finding, "column"),
            join if isinstance(join, str) else None,
        )
    )


# --- the history ---------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """The last sweep recorded before the plan was applied."""

    recorded_at: str
    invocation_id: str | None
    scope: frozenset[str]
    suppressed: frozenset[str]
    fingerprints: frozenset[str]
    #: ``axis|model|join`` keys of checks the baseline left ``unchecked`` on one
    #: model (a join it could not probe, a column contract it could not read),
    #: which a model-wide suppression does not name.
    unchecked: frozenset[str] = frozenset()

    def describe(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "invocation_id": self.invocation_id,
            "models_swept": len(self.scope),
            "findings": len(self.fingerprints),
        }


def _read(directory: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads((directory / HISTORY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sweeps = document.get("sweeps") if isinstance(document, dict) else None
    return [s for s in sweeps or [] if isinstance(s, dict)]


def record_sweep(
    directory: Path | None,
    target: str,
    *,
    scope: Iterable[str],
    suppressed: Iterable[str],
    fingerprints: Iterable[str],
    unchecked: Iterable[str] = (),
    invocation_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Append one sweep to the history, keeping the latest :data:`MAX_SWEEPS`.

    Written atomically, so a reader never sees half a file. Never raises.
    """

    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "target": target,
            "recorded_at": (now or datetime.now(UTC)).isoformat(),
            "invocation_id": invocation_id,
            "scope": sorted(set(scope)),
            "suppressed": sorted(set(suppressed)),
            "fingerprints": sorted(set(fingerprints)),
            "unchecked": sorted(set(unchecked)),
        }
        sweeps = [*_read(directory), entry][-MAX_SWEEPS:]
        body = json.dumps({"schema_version": 1, "sweeps": sweeps}, indent=1)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            handle.write(body)
        Path(handle.name).replace(directory / HISTORY)
    except (OSError, TypeError, ValueError):
        return


#: The verification record lists whose ``unchecked`` records leave one model's
#: check unrun, and the finding axis each stands for.
_RECORD_AXES = (
    ("join_coverage", "join_contract"),
    ("parent_population", "declaration"),
    ("temporal_contracts", "declaration"),
    ("population_delta", "declaration"),
)


def unchecked_keys(verification: Mapping[str, Any]) -> list[str]:
    """``axis|model|join`` for every per-model check a sweep left unchecked.

    ``verification`` is a sweep's payload. A join record keys on its join, so
    another join on the same model that was measured still counts as checked;
    the other records key on the model.
    """

    keys: list[str] = []
    for kind, axis in _RECORD_AXES:
        for record in verification.get(kind) or []:
            if not isinstance(record, Mapping) or record.get("status") != "unchecked":
                continue
            join = record.get("join") if kind == "join_coverage" else None
            keys.append(f"{axis}|{record.get('model') or ''}|{join or ''}")
    contract = verification.get("column_contract") or {}
    keys.extend(
        f"build|{item.get('model') or ''}|"
        for item in contract.get("unchecked") or []
        if isinstance(item, Mapping)
    )
    return keys


def load_baseline(
    directory: Path | None, target: str, *, before: str | None
) -> Baseline | None:
    """The latest sweep of ``target`` recorded strictly before ``before``.

    ``before`` is the plan's ``applied_at``, or its ``created_at`` while it is
    unapplied, so a build of the plan never takes its own earlier sweep as the
    baseline. ``None`` takes the latest sweep.
    """

    if directory is None:
        return None
    cutoff = _instant(before) if before else None
    if before and cutoff is None:
        return None
    best: tuple[datetime, dict[str, Any]] | None = None
    for sweep in _read(directory):
        if sweep.get("target") != target:
            continue
        when = _instant(sweep.get("recorded_at"))
        if when is None or (cutoff is not None and when >= cutoff):
            continue
        if best is None or when > best[0]:
            best = (when, sweep)
    if best is None:
        return None
    sweep = best[1]
    return Baseline(
        recorded_at=str(sweep.get("recorded_at")),
        invocation_id=sweep.get("invocation_id"),
        scope=frozenset(str(s) for s in sweep.get("scope") or ()),
        suppressed=frozenset(str(s) for s in sweep.get("suppressed") or ()),
        fingerprints=frozenset(str(s) for s in sweep.get("fingerprints") or ()),
        unchecked=frozenset(str(s) for s in sweep.get("unchecked") or ()),
    )


def _instant(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- the plan's reach ----------------------------------------------------------


@dataclass
class Reach:
    """What the plan's edits can reach, read from the manifest's lineage.

    ``labels`` is every node label the manifest knows; ``reached`` the labels
    downstream of an edit (the edited nodes included), or ``None`` where an edit
    can reach anything; ``created`` the models the plan created.
    """

    labels: set[str] = field(default_factory=set)
    reached: set[str] | None = None
    created: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def _kind(edit: Any) -> str:
    kind = getattr(edit, "kind", None)
    return str(getattr(kind, "value", kind) or "")


def _op(edit: Any) -> str:
    op = getattr(edit, "op", None)
    return str(getattr(op, "value", op) or "upsert")


def plan_reach(edits: Iterable[Any], manifest: Mapping[str, Any] | None) -> Reach:
    """The nodes the plan edits and everything downstream of them.

    A model, snapshot, seed or singular test is matched on the file it is
    defined in; a ``schema.yml`` on the nodes it documents (dbt's
    ``patch_path``). An edit the lineage cannot bound (a macro, the project
    config, packages, a semantic YAML, a delete) leaves ``reached`` ``None``.
    """

    from ..dbt_project import manifest_node_label

    reach = Reach()
    nodes = (manifest or {}).get("nodes") or {}
    if not isinstance(nodes, Mapping):
        return reach
    label_of: dict[str, str] = {}
    by_file: dict[str, list[str]] = {}
    by_patch: dict[str, list[str]] = {}
    # Sources too: a `schema.yml` that declares one is that source's file, and
    # its consumers are what an edit to it reaches.
    sources = (manifest or {}).get("sources") or {}
    defined = [
        *nodes.items(),
        *(sources.items() if isinstance(sources, Mapping) else ()),
    ]
    for unique_id, node in defined:
        if not isinstance(node, Mapping):
            continue
        label = manifest_node_label(str(unique_id), dict(node))
        label_of[str(unique_id)] = label
        path = node.get("original_file_path")
        if isinstance(path, str):
            by_file.setdefault(PurePosixPath(path).as_posix(), []).append(unique_id)
        patch = node.get("patch_path")
        if isinstance(patch, str):
            # `p://models/schema.yml`: the package, then the file.
            by_patch.setdefault(patch.split("://", 1)[-1], []).append(unique_id)
    reach.labels = set(label_of.values())

    roots: set[str] = set()
    bounded = True
    for edit in edits:
        kind = _kind(edit)
        path = PurePosixPath(str(getattr(edit, "path", ""))).as_posix()
        if _op(edit) == "delete" or kind in _UNBOUNDED_KINDS:
            bounded = False
            continue
        if kind in _NODE_KINDS:
            matched = by_file.get(path, [])
            roots.update(matched)
            if kind == "model_sql" and getattr(edit, "old_content_hash", 1) is None:
                reach.created.update(label_of[uid] for uid in matched)
            if not matched and kind != "test_sql":
                bounded = False
        elif kind == "schema_yml":
            documented = by_patch.get(path, [])
            # A source is defined in the YAML itself, so its file is the path.
            declared = [
                uid for uid in by_file.get(path, []) if uid.startswith("source.")
            ]
            roots.update(documented)
            roots.update(declared)
            # A YAML that matches no node (an older manifest with no patch_path)
            # changes something the lineage cannot name.
            if not documented and not declared:
                bounded = False
    reach.edited = {label_of[uid] for uid in roots if uid in label_of}
    if not bounded:
        return reach

    children = _children(manifest or {}, nodes)
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(children.get(current, ()))
    reach.reached = {label_of[uid] for uid in seen if uid in label_of}
    return reach


def _children(
    manifest: Mapping[str, Any], nodes: Mapping[str, Any]
) -> dict[str, list[str]]:
    child_map = manifest.get("child_map")
    if isinstance(child_map, Mapping) and child_map:
        return {str(k): [str(c) for c in v or ()] for k, v in child_map.items()}
    children: dict[str, list[str]] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        for parent in (node.get("depends_on") or {}).get("nodes") or ():
            children.setdefault(str(parent), []).append(str(unique_id))
    return children


@dataclass
class AttributionInput:
    """What a ``--for-plan`` build hands the sweep to attribute its findings.

    ``baseline_reason`` says why ``baseline`` is ``None`` where it is.
    """

    edits: list[Any]
    baseline: Baseline | None = None
    baseline_reason: str | None = None


# --- the classification --------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    """One finding's class, and the rule that decided it."""

    attribution: str
    basis: str


def classify(
    finding: Any, baseline: Baseline | None, reach: Reach | None
) -> Attribution:
    """``introduced``, ``unattributed`` or ``existing``, with its ``basis``.

    In order: a finding the baseline reported is ``existing`` (``baseline``); a
    finding on a model the plan created is ``introduced`` (``new_model``); one
    on a node the plan's lineage cannot reach is ``existing`` (``lineage``);
    one on a model the baseline swept with that check running is
    ``introduced`` (``baseline``); anything else is ``unattributed``
    (``no_baseline``, or ``baseline_suppressed`` where the baseline swept the
    model and did not run that check).
    """

    identifier = str(_value(finding, "identifier") or "")
    if baseline is not None and fingerprint(finding) in baseline.fingerprints:
        return Attribution(EXISTING, "baseline")
    if reach is not None:
        if identifier in reach.created:
            return Attribution(INTRODUCED, "new_model")
        if (
            reach.reached is not None
            and identifier in reach.labels
            and identifier not in reach.reached
        ):
            return Attribution(EXISTING, "lineage")
    if baseline is not None and identifier in baseline.scope:
        classes = _classes(
            str(_value(finding, "axis") or ""), str(_value(finding, "code") or "")
        )
        if (
            classes is not None
            and not classes & baseline.suppressed
            and not _left_unchecked(finding, identifier, baseline)
        ):
            return Attribution(INTRODUCED, "baseline")
        return Attribution(UNATTRIBUTED, "baseline_suppressed")
    return Attribution(UNATTRIBUTED, "no_baseline")


def _left_unchecked(finding: Any, identifier: str, baseline: Baseline) -> bool:
    """Whether the baseline swept the model and left this one check unrun.

    A column-contract finding is a ``build`` finding with a ``column_`` code; a
    join finding matches its own join, or a record that named none.
    """

    axis = str(_value(finding, "axis") or "")
    code = str(_value(finding, "code") or "")
    if axis == "build" and not code.startswith("column_"):
        return False
    data = _value(finding, "data") or {}
    join = data.get("join") if isinstance(data, Mapping) else None
    keys = {f"{axis}|{identifier}|"}
    if isinstance(join, str):
        keys.add(f"{axis}|{identifier}|{join}")
    return bool(keys & baseline.unchecked)


def attribute(
    findings: list[Any], baseline: Baseline | None, reach: Reach | None
) -> tuple[list[Any], list[Attribution]]:
    """The findings in reading order (introduced, unattributed, existing),
    keeping each class's own ranking, and each one's attribution."""

    judged = [(f, classify(f, baseline, reach)) for f in findings]
    judged.sort(key=lambda pair: ORDER[pair[1].attribution])
    return [f for f, _ in judged], [a for _, a in judged]


#: How many introduced findings ``data.verification.introduced`` names; the
#: count beside it is never cut.
MAX_INTRODUCED = 50


def summary(
    findings: list[Any],
    attributions: list[Attribution],
    given: AttributionInput,
    reach: Reach | None,
) -> dict[str, Any]:
    """``data.verification.attribution``: what the classification rested on."""

    counts = {INTRODUCED: 0, UNATTRIBUTED: 0, EXISTING: 0}
    for item in attributions:
        counts[item.attribution] += 1
    out: dict[str, Any] = {
        "ran": True,
        "counts": counts,
        "baseline": given.baseline.describe() if given.baseline else None,
        "lineage": (
            "unbounded" if reach is None or reach.reached is None else "bounded"
        ),
    }
    if given.baseline is None and given.baseline_reason:
        out["baseline_reason"] = given.baseline_reason
    if reach is not None and reach.created:
        out["created_models"] = sorted(reach.created)
    return out


def introduced_refs(
    findings: list[Any], attributions: list[Attribution]
) -> list[dict[str, Any]]:
    """The introduced findings, as references into ``findings``."""

    return [
        {
            k: v
            for k, v in (
                ("code", _value(f, "code")),
                ("identifier", _value(f, "identifier")),
                ("column", _value(f, "column")),
                ("severity", _value(f, "severity")),
                ("basis", a.basis),
            )
            if v is not None
        }
        for f, a in zip(findings, attributions, strict=True)
        if a.attribution == INTRODUCED
    ][:MAX_INTRODUCED]


def unattributed_high_refs(
    findings: list[Any], attributions: list[Attribution]
) -> list[dict[str, Any]]:
    """The high-severity findings nothing could place, as ``{code, identifier,
    column}``, taken before the findings cap so none is lost to it."""

    return [
        {
            k: v
            for k, v in (
                ("code", _value(f, "code")),
                ("identifier", _value(f, "identifier")),
                ("column", _value(f, "column")),
            )
            if v is not None
        }
        for f, a in zip(findings, attributions, strict=True)
        if a.attribution == UNATTRIBUTED and _value(f, "severity") == "high"
    ][:MAX_INTRODUCED]
