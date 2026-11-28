"""The cause kinds beside the duplicate delivery, decided from evidence (#1476).

#1475 confirms the loader's duplicate delivery. This module decides the rest,
each from evidence Dex already has, read at the two runs that bound the break:

- **the loader retyped a source column**: the schema drift axis
  (``column_retyped``) on a source relation the tested model reads, between the
  snapshot that bounds the break and the warehouse after it;
- **the project changed a model in the range**: a file the failing test's
  lineage is built from changed between the bounding commits, and
  ``maintain verify`` on the changed model reports a defect;
- **a package was upgraded**: the version ``package-lock.yml`` pins for a
  package the lineage reads differs at the two bounding commits;
- **a seed was edited**: a seed in the lineage hashes differently at the two
  bounding commits;
- **the schedule**: a parent model was built before its child at the last
  passing run and after it at the first failing run, read from both models'
  build times (Elementary's ``dbt_run_results``);
- **a source's rows arrived after the run read them** (#1623): rows of the
  window the failing run judged, by the source's event time, whose capture time
  is after that run started, where none of the window the last passing run
  judged were (:mod:`.arrival` reads the counts).

The range's files are split between owners first (:func:`partition`), so one
edit is never claimed twice: a seed's file is the seed's, the package files are
the package's, and everything else under the project is the project's.

Pure logic: the caller reads git, the warehouse and Elementary's table, and
hands the answers here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

from .diagnosis import Candidate, DiagnosisRange
from .observations import UNKNOWN, utc

#: The checks each kind is confirmed by, as the answer names them.
SCHEMA_CHECK = "maintain check: schema axis"
VERIFY_CHECK = "maintain verify"
PACKAGE_CHECK = "package-lock.yml at the bounding commits"
SEED_CHECK = "seed file hash at the bounding commits"
SCHEDULE_CHECK = "dbt_run_results: build order at the bounding runs"
ARRIVAL_CHECK = "source capture time against the bounding runs' starts"

#: The files a package owns: what dbt reads to install packages.
PACKAGE_FILES = frozenset({"packages.yml", "dependencies.yml", "package-lock.yml"})
LOCK_FILE = "package-lock.yml"

#: The most changed files a candidate's evidence lists.
_MAX_FILES = 20

#: The most parent pairs one test's schedule is checked for.
MAX_PAIRS = 10

#: The packages every project reads that no project installs: dbt's own
#: macros, and each adapter's (``dbt_duckdb``, ``dbt_bigquery``...).
_BUILTIN_PACKAGES = frozenset({"dbt"})
_ADAPTER_MACROS = re.compile(
    r"dbt_(duckdb|postgres|bigquery|snowflake|redshift|databricks|spark|"
    r"clickhouse|trino|athena|fabric|sqlserver)"
)

Owner = Literal["seed", "package", "project"]

#: Files no owner changes: Dex's own state (``.dex/``, which a team commits
#: beside the project), and what dbt writes and a repository may still hold.
_NOBODYS = (".dex/", "target/", "dbt_packages/", "logs/")


# --- the range ---------------------------------------------------------------------


@dataclass(frozen=True)
class Bounds:
    """The commits a git-read kind compares: the failing run's, and the passing
    commits that bound the range (#1467). ``last_good`` is the bound the last
    passing run deployed, where it is one, else the first bound: the one the
    answer names as *before*."""

    through: str
    after: tuple[str, ...]
    last_good: str


def range_bounds(span: DiagnosisRange) -> Bounds | Candidate:
    """The bounding commits, or the ``not_checked`` reason they are not known.

    Returns a template candidate (owner ``project``) whose ``reason``,
    ``detail`` and ``evidence`` the caller copies onto each git-read kind.
    ``same_commit`` is returned as a bounds object with no ``after``: no file
    changed across the break.
    """

    commits = span.commits
    if commits is None:
        return Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            reason="no_commit_history",
            detail="the repository's history was not read, so the commits the "
            "break spans are not known",
        )
    if commits.status == "same_commit":
        return Bounds(
            through=commits.through.commit, after=(), last_good=commits.through.commit
        )
    if commits.status != "ordered" or not commits.after:
        evidence: dict[str, Any] = {"status": commits.status}
        if commits.reason is not None:
            evidence["reason"] = commits.reason
        if commits.side is not None:
            evidence["side"] = commits.side
        return Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            reason="commit_range_unknown",
            detail="the commits the break spans are not known "
            f"({commits.reason or commits.status}), so no file can be read at "
            "the runs that bound it",
            evidence=evidence,
        )
    after = tuple(ref.commit for ref in commits.after)
    last = commits.last_good_run
    last_good = last.commit if last != UNKNOWN and last.commit in after else after[0]
    return Bounds(through=commits.through.commit, after=after, last_good=last_good)


def unknown_range(template: Candidate, owner: str, kind: str) -> Candidate:
    """The range's ``not_checked`` answer, for one kind."""

    return template.model_copy(update={"owner": owner, "kind": kind})


def same_commit(owner: str, kind: str, commit: str) -> Candidate:
    """Ruled out: the failing commit itself passed earlier, so no file of this
    owner changed across the break."""

    return Candidate(
        owner=owner,  # type: ignore[arg-type]
        kind=kind,
        status="ruled_out",
        check="onset.commits",
        reason="same_commit",
        detail=f"commit {commit} passed in an earlier run and failed in this "
        f"one, so no {owner} file changed across the break",
        evidence={"commit": commit},
    )


def partition(
    files: Iterable[str], seed_paths: Iterable[str], seed_dirs: Iterable[str] = ()
) -> dict[Owner, tuple[str, ...]]:
    """The range's files, each given to one owner: a seed's file (or any CSV
    under a seed path) to the seed, the package files to the package, and the
    rest to the project. Dex's own ``.dex/`` and dbt's output are nobody's."""

    seeds = {_norm(p) for p in seed_paths}
    dirs = tuple(_norm(d).rstrip("/") + "/" for d in seed_dirs if d)
    owned: dict[Owner, list[str]] = {"seed": [], "package": [], "project": []}
    for path in dict.fromkeys(_norm(f) for f in files):
        if path.startswith(_NOBODYS):
            # Dex's own state, and what dbt writes: no owner's change.
            continue
        if path in seeds or (path.endswith(".csv") and path.startswith(dirs)):
            owned["seed"].append(path)
        elif PurePosixPath(path).name in PACKAGE_FILES and "/" not in path:
            owned["package"].append(path)
        else:
            owned["project"].append(path)
    return {owner: tuple(paths) for owner, paths in owned.items()}


def _norm(path: str) -> str:
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


# --- the lineage -------------------------------------------------------------------


@dataclass(frozen=True)
class Lineage:
    """What one failing test reads, from the manifest.

    ``nodes`` is the test, the model it is attached to and everything upstream
    of them (models, seeds, snapshots, sources). ``files`` maps each project
    file those nodes are built or patched from to the nodes it defines.
    ``packages`` names every package the lineage reads, through a node or a
    macro. ``pairs`` are the (child, parent) models a schedule can order.
    """

    test: str
    model: str | None
    project: str
    nodes: tuple[str, ...] = ()
    models: dict[str, str] = field(default_factory=dict)
    seeds: dict[str, str] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    packages: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    code: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    #: The code of the models that read each source directly, so a retyped
    #: column is matched against the models that read that relation.
    readers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: The models each project macro is reached from, directly or through
    #: another macro, so a changed macro is judged on the models that call it.
    macro_callers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Whether the walk stopped at its limit with nodes left unvisited: an
    #: absence (no seed, no package, no file) then proves nothing.
    truncated: bool = False


def _patch(path: Any) -> str | None:
    """A ``patch_path`` (``shop://models/schema.yml``) as a project path."""

    if not isinstance(path, str) or not path:
        return None
    return _norm(path.split("://", 1)[-1])


def _macro_package(uid: str) -> str | None:
    parts = uid.split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "macro" else None


def lineage(
    manifest: Mapping[str, Any],
    test: str,
    *,
    columns: Sequence[str] = (),
    limit: int = 200,
) -> Lineage | None:
    """The lineage of ``test``, or ``None`` where the manifest does not hold it."""

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    macros = manifest.get("macros") or {}
    node = nodes.get(test)
    if not isinstance(node, Mapping):
        return None
    project = str((manifest.get("metadata") or {}).get("project_name") or "")
    attached = node.get("attached_node")
    deps = [str(d) for d in (node.get("depends_on") or {}).get("nodes") or ()]
    model = (
        str(attached)
        if attached
        else next((d for d in deps if d.startswith("model.")), None)
    )

    seen: list[str] = [test]
    queue = list(deps)
    while queue and len(seen) < limit:
        uid = queue.pop(0)
        if uid in seen:
            continue
        seen.append(uid)
        held = nodes.get(uid)
        if isinstance(held, Mapping) and held.get("resource_type") in (
            "model",
            "snapshot",
        ):
            queue.extend(
                str(d) for d in (held.get("depends_on") or {}).get("nodes") or ()
            )
    truncated = any(uid not in seen for uid in queue)

    files: dict[str, list[str]] = {}
    packages: list[str] = []
    models: dict[str, str] = {}
    seeds: dict[str, str] = {}
    found_sources: list[str] = []
    code: list[str] = []
    readers: dict[str, list[str]] = {}
    callers: dict[str, list[str]] = {}

    def own(path: str | None, uid: str) -> None:
        if path:
            files.setdefault(_norm(path), []).append(uid)

    def macro_closure(start: Iterable[str]) -> list[str]:
        """Every macro reached from ``start``, through the macros each calls."""

        reached: list[str] = []
        pending = list(start)
        while pending and len(reached) < limit:
            uid = pending.pop(0)
            if uid in reached:
                continue
            reached.append(uid)
            macro = macros.get(uid)
            if isinstance(macro, Mapping):
                pending.extend(
                    str(m) for m in (macro.get("depends_on") or {}).get("macros") or ()
                )
        return reached

    namespace = (node.get("test_metadata") or {}).get("namespace")
    if namespace:
        packages.append(str(namespace))
    for uid in seen:
        if uid.startswith("source."):
            if uid in sources:
                found_sources.append(uid)
                held_source = sources[uid]
                package = str(held_source.get("package_name") or "")
                if package and package != project:
                    packages.append(package)
                elif package == project:
                    own(held_source.get("original_file_path"), uid)
            continue
        held = nodes.get(uid)
        if not isinstance(held, Mapping):
            continue
        package = str(held.get("package_name") or "")
        kind = held.get("resource_type")
        if package and package != project:
            packages.append(package)
        elif package == project:
            own(held.get("original_file_path"), uid)
            own(_patch(held.get("patch_path")), uid)
        if kind == "model":
            models[uid] = str(held.get("name") or uid.split(".")[-1])
            texts = [
                text
                for text in (held.get("raw_code"), held.get("compiled_code"))
                if isinstance(text, str) and text
            ]
            code.extend(texts)
            for dep in (held.get("depends_on") or {}).get("nodes") or ():
                if str(dep).startswith("source."):
                    readers.setdefault(str(dep), []).extend(texts or [""])
        elif kind == "seed":
            path = held.get("original_file_path")
            if isinstance(path, str) and path and package == project:
                seeds[uid] = _norm(path)
        direct = [str(m) for m in (held.get("depends_on") or {}).get("macros") or ()]
        for macro_uid in macro_closure(direct):
            callers.setdefault(macro_uid, []).append(uid)

    for uid in callers:
        owner = _macro_package(uid)
        if owner is None:
            continue
        package = owner
        if package == project:
            macro = macros.get(uid)
            if isinstance(macro, Mapping):
                own(macro.get("original_file_path"), uid)
        elif package not in _BUILTIN_PACKAGES and not _ADAPTER_MACROS.fullmatch(
            package
        ):
            # dbt_utils, dbt_expectations: a package, not dbt's or an adapter's.
            packages.append(package)

    # A schedule can only stale a model through a model it reads: each pair is
    # the tested model and a model parent it depends on directly. A model a
    # test reads beside it (a relationships test's other side) is a pair only
    # where the tested model reads it.
    pairs: list[tuple[str, str]] = []
    if model is not None:
        held = nodes.get(model)
        if isinstance(held, Mapping):
            parents = (held.get("depends_on") or {}).get("nodes") or ()
            pairs.extend(
                (model, str(p))
                for p in parents
                if str(p).startswith("model.")
                and (nodes.get(str(p)) or {}).get("config", {}).get("materialized")
                != "ephemeral"
            )

    return Lineage(
        test=test,
        model=model,
        project=project,
        nodes=tuple(seen),
        models=models,
        seeds=seeds,
        sources=tuple(found_sources),
        files={path: tuple(dict.fromkeys(uids)) for path, uids in files.items()},
        packages=tuple(dict.fromkeys(p for p in packages if p)),
        pairs=tuple(dict.fromkeys(pairs))[:MAX_PAIRS],
        code=tuple(code),
        columns=tuple(columns),
        readers={uid: tuple(texts) for uid, texts in readers.items()},
        macro_callers={
            uid: tuple(dict.fromkeys(c for c in calling if c in models or c == test))
            for uid, calling in callers.items()
        },
        truncated=truncated,
    )


# --- the project -------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectChange:
    """The project files the range changed that the lineage is built from."""

    commits: int
    files: tuple[str, ...]
    models: tuple[str, ...]
    test_changed: bool
    project_wide: tuple[str, ...] = ()


#: Project files every node reads, so a change to one changes the lineage.
_PROJECT_WIDE = frozenset({"dbt_project.yml"})


def project_change(
    found: Lineage, project_files: Sequence[str], commits: int
) -> ProjectChange:
    """Which of the lineage's files and models the range's project files are."""

    changed: list[str] = []
    models: list[str] = []
    test_changed = False
    wide = tuple(f for f in project_files if f in _PROJECT_WIDE)
    for path in project_files:
        uids = found.files.get(path)
        if not uids:
            continue
        changed.append(path)
        for uid in uids:
            if uid in found.models:
                models.append(uid)
            elif uid == found.test:
                test_changed = True
            elif uid.startswith("macro."):
                # A project macro the lineage calls, directly or through another
                # macro: judged on the models that call it.
                calling = found.macro_callers.get(uid, ())
                models.extend(c for c in calling if c in found.models)
                if found.test in calling:
                    test_changed = True
    if wide and found.model is not None:
        models.append(found.model)
    return ProjectChange(
        commits=commits,
        files=tuple(dict.fromkeys([*changed, *wide])),
        models=tuple(dict.fromkeys(models)),
        test_changed=test_changed,
        project_wide=wide,
    )


#: The rule-outs that rest on something being absent from the lineage or the
#: range: an incomplete walk or a cut listing cannot support them.
_ABSENCE = frozenset(
    {"not_in_lineage", "no_seed_in_lineage", "no_parent_model", "no_packages"}
)


def unproven(candidate: Candidate, reason: str, detail: str) -> Candidate:
    """``candidate`` as ``not_checked`` where its rule-out rests on an absence
    an incomplete read cannot show; any other answer as it stands."""

    if candidate.status != "ruled_out" or candidate.reason not in _ABSENCE:
        return candidate
    return candidate.model_copy(
        update={
            "status": "not_checked",
            "reason": reason,
            "detail": f"{detail}, so {candidate.reason} cannot be shown",
            "evidence": {**candidate.evidence, "would_rule_out": candidate.reason},
        }
    )


def project_not_in_lineage(change: ProjectChange, changed_files: int) -> Candidate:
    return Candidate(
        owner="project",
        kind="project_change",
        status="ruled_out",
        check="onset.commits",
        reason="not_in_lineage",
        detail=f"{change.commits} commit(s) in the break's range changed "
        f"{changed_files} project file(s), none of which the failing test or "
        "any model it reads is built from",
        evidence={"commits": change.commits, "changed_files": changed_files},
    )


def project_test_only(change: ProjectChange) -> Candidate:
    return Candidate(
        owner="project",
        kind="project_change",
        status="not_checked",
        reason="test_definition_changed",
        detail="the failing test's own definition changed in the break's range "
        "and no model it reads did: a changed claim is a project cause "
        "maintain verify cannot judge, so it is named and not confirmed",
        evidence=_change_evidence(change),
    )


def _change_evidence(change: ProjectChange) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "commits": change.commits,
        "files": list(change.files[:_MAX_FILES]),
    }
    if len(change.files) > _MAX_FILES:
        evidence["files_elided"] = len(change.files) - _MAX_FILES
    if change.test_changed:
        evidence["test_changed"] = True
    return evidence


#: The verify severities that confirm a project change.
_CONFIRMING = frozenset({"high", "medium"})


def project_verified(
    change: ProjectChange,
    names: Mapping[str, str],
    findings: Sequence[Any],
    *,
    offered: bool,
    suppressed: Mapping[str, str],
) -> Candidate:
    """The project candidate as ``maintain verify`` answered it on the changed
    models (``names`` maps each model's ``unique_id`` to the name verify takes).

    A finding of medium or high severity on a changed model confirms. A build
    finding on a test node never does: the incident's own failure would
    otherwise confirm itself.
    """

    wanted = {names[uid].casefold() for uid in change.models if uid in names}
    held = [
        f
        for f in findings
        if (getattr(f, "identifier", None) or "").casefold() in wanted
        and getattr(f, "severity", None) in _CONFIRMING
    ]
    evidence = _change_evidence(change)
    evidence["models"] = sorted(names[uid] for uid in change.models if uid in names)
    if held:
        evidence["findings"] = [
            {
                "model": f.identifier,
                "code": f.code,
                "axis": f.axis,
                "severity": f.severity,
                **({"column": f.column} if getattr(f, "column", None) else {}),
            }
            for f in held[:_MAX_FILES]
        ]
        codes = ", ".join(sorted({f.code for f in held}))
        return Candidate(
            owner="project",
            kind="project_change",
            status="confirmed",
            check=VERIFY_CHECK,
            finding=held[0].code,
            detail=f"{len(change.files)} file(s) the failing test's lineage is "
            f"built from changed in the break's range, and maintain verify on "
            f"{', '.join(evidence['models'])} reports {codes}",
            evidence=evidence,
        )
    if offered:
        return Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            check=VERIFY_CHECK,
            reason="scan_offered",
            detail="maintain verify on the changed model needs a scan this "
            "confirmation does not cover; it was offered, not run",
            evidence=evidence,
        )
    data_classes = {"grain", "row_population", "join_contract", "column_contract"}
    if data_classes and data_classes <= set(suppressed):
        return Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            check=VERIFY_CHECK,
            reason="verify_suppressed",
            detail="maintain verify ran none of its data checks on the changed "
            "model ("
            + "; ".join(f"{k}: {suppressed[k]}" for k in sorted(data_classes))
            + ")",
            evidence=evidence,
        )
    return Candidate(
        owner="project",
        kind="project_change",
        status="ruled_out",
        check=VERIFY_CHECK,
        reason="verify_clean",
        detail="files the failing test's lineage is built from changed in the "
        "break's range, and maintain verify on "
        f"{', '.join(evidence['models']) or 'the changed models'} reports no "
        "defect of medium or high severity",
        evidence=evidence,
    )


# --- the loader: a retyped source column ---------------------------------------------


#: A projection that reads every column: a retyped column is read through it
#: whether or not the code spells the column.
_WILDCARD = re.compile(r"(?:\bselect\s+(?:distinct\s+)?|,\s*|\.)\*", re.IGNORECASE)


def reads_column(found: Lineage, column: str, source: str | None = None) -> bool | None:
    """Whether the lineage reads ``column`` of ``source``: the tested column, or
    a word in the code of a model that reads that source directly. ``None``
    where it cannot be told: no code for those models in the manifest, or a
    wildcard projection that reads the column without spelling it."""

    wanted = column.casefold()
    if any(c.casefold() == wanted for c in found.columns):
        return True
    texts = found.readers.get(source, ()) if source is not None else found.code
    texts = tuple(t for t in texts if t)
    if not texts:
        return None
    pattern = re.compile(rf"(?<![\w$]){re.escape(wanted)}(?![\w$])", re.IGNORECASE)
    if any(pattern.search(text) for text in texts):
        return True
    if any(_WILDCARD.search(text) for text in texts):
        return None
    return False


def retype_candidates(
    found: Lineage,
    retyped: Mapping[str, Sequence[Any]],
    measured_against: dict[str, Any],
    *,
    checked: Sequence[str],
    unmeasured: Sequence[str] = (),
    sources: Mapping[str, str] | None = None,
) -> list[Candidate]:
    """The loader's retype candidates, from the schema axis's ``column_retyped``
    findings per relation (``retyped``), over the relations ``checked``.
    ``sources`` maps each relation to the manifest source it is, so a column is
    matched against the models that read that relation.

    A retype is confirmed only where the lineage is shown to read the column;
    where that cannot be told, it is named ``not_checked``, never a cause."""

    confirmed: list[Candidate] = []
    unknown: list[Candidate] = []
    unread: list[str] = []
    for relation in checked:
        source = (sources or {}).get(relation)
        for finding in retyped.get(relation, ()):
            column = str(finding.column or "")
            read = reads_column(found, column, source)
            if read is False:
                unread.append(f"{relation}.{column}")
                continue
            data = finding.data or {}
            evidence = {
                "type_before": data.get("type_before"),
                "type_after": data.get("type_after"),
                "measured_against": measured_against,
            }
            moved = (
                f"{relation}.{column} was retyped from {data.get('type_before')} "
                f"to {data.get('type_after')} since the snapshot that bounds the "
                "break"
            )
            if read is None:
                unknown.append(
                    Candidate(
                        owner="loader",
                        kind="source_retyped",
                        relation=relation,
                        columns=(column,),
                        status="not_checked",
                        check=SCHEMA_CHECK,
                        finding="column_retyped",
                        reason="column_read_unknown",
                        detail=moved + "; the manifest does not show whether the "
                        "models reading it read this column (no code, or a "
                        "wildcard projection), so it is named and not confirmed",
                        evidence=evidence,
                    )
                )
                continue
            confirmed.append(
                Candidate(
                    owner="loader",
                    kind="source_retyped",
                    relation=relation,
                    columns=(column,),
                    status="confirmed",
                    check=SCHEMA_CHECK,
                    finding="column_retyped",
                    detail=moved,
                    evidence=evidence,
                )
            )
    if confirmed:
        return [*confirmed, *unknown]
    held: list[Candidate] = list(unknown)
    if checked and not unknown:
        evidence_out: dict[str, Any] = {
            "relations": list(checked),
            "measured_against": measured_against,
        }
        if unread:
            evidence_out["retyped_not_read"] = unread[:_MAX_FILES]
        held.append(
            Candidate(
                owner="loader",
                kind="source_retyped",
                status="ruled_out",
                check=SCHEMA_CHECK,
                reason="no_retype",
                detail="no column the tested model reads was retyped on "
                f"{', '.join(checked)} since the snapshot that bounds the break",
                evidence=evidence_out,
            )
        )
    if unmeasured:
        held.append(
            Candidate(
                owner="loader",
                kind="source_retyped",
                status="not_checked",
                reason="not_in_snapshot",
                detail="the snapshot that bounds the break holds no columns for "
                f"{', '.join(unmeasured)}, so a retype there cannot be measured",
                evidence={"relations": list(unmeasured)},
            )
        )
    return held


def retype_unmeasured(reason: str, detail: str) -> Candidate:
    return Candidate(
        owner="loader",
        kind="source_retyped",
        status="not_checked",
        reason=reason,
        detail=detail,
    )


# --- a package -----------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """One package as ``package-lock.yml`` pins it."""

    key: str
    named: bool
    version: str


def _slug(entry: Mapping[str, Any]) -> str | None:
    for field_name in ("package", "git", "local", "tarball", "private"):
        value = entry.get(field_name)
        if isinstance(value, str) and value.strip():
            tail = value.strip().rstrip("/").split("/")[-1]
            if tail.endswith(".git"):
                tail = tail[:-4]
            return tail.replace("-", "_").casefold() or None
    return None


def parse_lock(text: str | None) -> dict[str, Pin] | None:
    """The pins of a ``package-lock.yml``, keyed by the package's name (dbt
    1.7 writes it) or, where none is written, its slug. ``None`` where the
    file is absent or not a lock."""

    if text is None:
        return None
    try:
        held = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(held, Mapping) or not isinstance(held.get("packages"), list):
        return None
    pins: dict[str, Pin] = {}
    for entry in held["packages"]:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        named = isinstance(name, str) and bool(name.strip())
        key = str(name).strip().casefold() if named else _slug(entry)
        if not key:
            continue
        version = entry.get("version", entry.get("revision"))
        if version is None and "local" in entry:
            version = f"local:{entry['local']}"
        pins[key] = Pin(key=key, named=named, version=str(version))
    return pins


def package_candidate(
    found: Lineage,
    before: dict[str, Pin] | None,
    after: dict[str, Pin] | None,
    *,
    declared: bool,
    bounds: Bounds,
    commits: Sequence[str] = (),
) -> Candidate:
    """The package candidate from the locks at the bounding commits.

    ``declared`` says whether the project declares any package at all
    (``packages.yml`` or ``dependencies.yml`` at the failing commit).
    """

    base = {"owner": "package", "kind": "package_upgraded", "check": PACKAGE_CHECK}
    read = {p.casefold() for p in found.packages}
    if before is None or after is None:
        if not declared:
            return Candidate(
                **base,
                status="ruled_out",
                reason="no_packages",
                detail="the project declares no package at the bounding commits",
            )
        return Candidate(
            **base,
            status="not_checked",
            reason="unpinned",
            detail="no package-lock.yml is committed at "
            + (
                "both bounding commits"
                if before is None and after is None
                else "one of the bounding commits"
            )
            + ", so the versions installed at each run are not recorded: "
            "packages.yml's ranges can resolve differently on the same commit",
            evidence={
                "lock_before": before is not None,
                "lock_after": after is not None,
            },
        )
    changed = sorted(
        key
        for key in set(before) | set(after)
        if (before[key].version if key in before else None)
        != (after[key].version if key in after else None)
    )
    if not changed:
        return Candidate(
            **base,
            status="ruled_out",
            reason="same_versions",
            detail="package-lock.yml pins the same version of every package at "
            "the two bounding commits",
            evidence={
                "commit_before": bounds.last_good,
                "commit_after": bounds.through,
            },
        )
    hits = [key for key in changed if key in read]
    evidence: dict[str, Any] = {
        "commit_before": bounds.last_good,
        "commit_after": bounds.through,
        "changed": [
            {
                "package": key,
                "version_before": before[key].version if key in before else None,
                "version_after": after[key].version if key in after else None,
            }
            for key in changed[:_MAX_FILES]
        ],
    }
    if commits:
        evidence["commits"] = list(commits[:_MAX_FILES])
    if hits:
        first = {
            "version_before": before[hits[0]].version if hits[0] in before else None,
            "version_after": after[hits[0]].version if hits[0] in after else None,
        }
        return Candidate(
            **base,
            status="confirmed",
            relation=None,
            detail=f"package-lock.yml moved {', '.join(hits)} the failing test's "
            f"lineage reads ({hits[0]} {first['version_before']} to "
            f"{first['version_after']}) between the last passing run's commit and "
            "the failing one",
            evidence={**evidence, "packages": hits},
        )
    unnamed = [
        key
        for key in changed
        if not (after.get(key) or before.get(key) or Pin(key, True, "")).named
    ]
    unmatched = [p for p in read if p not in set(before) | set(after)]
    if unnamed and unmatched:
        return Candidate(
            **base,
            status="not_checked",
            reason="package_unmapped",
            detail="package-lock.yml changed packages it names by their source "
            f"({', '.join(unnamed)}) and the lineage reads packages it does not "
            f"name ({', '.join(sorted(unmatched))}), so they cannot be matched",
            evidence=evidence,
        )
    return Candidate(
        **base,
        status="ruled_out",
        reason="not_in_lineage",
        detail=f"package-lock.yml moved {', '.join(changed)} between the bounding "
        "commits, and the failing test's lineage reads "
        + (", ".join(sorted(read)) if read else "no package")
        + ", none of them",
        evidence={**evidence, "lineage_packages": sorted(read)},
    )


# --- a seed --------------------------------------------------------------------------


def seed_candidates(
    found: Lineage,
    hashes: Mapping[str, tuple[Sequence[str | None], str | None]],
    bounds: Bounds,
    commits: Mapping[str, Sequence[str]],
) -> list[Candidate]:
    """One candidate per seed in the lineage, from its file hash at each
    bounding commit and at the failing one (``hashes`` maps a seed's
    ``unique_id`` to ``(hashes at the bounds, hash at the failing commit)``).

    A seed is edited where its hash at the failing commit differs from its
    hash at every bound: content one of the passing commits already held did
    not enter in the range.
    """

    base = {"owner": "seed", "kind": "seed_edited", "check": SEED_CHECK}
    if not found.seeds:
        return [
            Candidate(
                **base,
                status="ruled_out",
                reason="no_seed_in_lineage",
                detail="the failing test and the models it reads read no seed of "
                "this project",
            )
        ]
    held: list[Candidate] = []
    for uid, path in sorted(found.seeds.items()):
        at_bounds, at_through = hashes.get(uid, ((), None))
        name = uid.split(".")[-1]
        evidence: dict[str, Any] = {
            "seed": name,
            "path": path,
            "commit_before": bounds.last_good,
            "commit_after": bounds.through,
        }
        before = dict(zip(bounds.after, at_bounds, strict=False)).get(bounds.last_good)
        evidence["hash_before"] = before
        evidence["hash_after"] = at_through
        if at_through is None or not at_bounds or any(h is None for h in at_bounds):
            held.append(
                Candidate(
                    **base,
                    relation=name,
                    status="not_checked",
                    reason="seed_file_absent",
                    detail=f"{path} is not in every commit that bounds the break, so "
                    "its hash cannot be compared",
                    evidence=evidence,
                )
            )
            continue
        if all(h != at_through for h in at_bounds):
            touched = list(commits.get(path, ()))[:_MAX_FILES]
            if touched:
                evidence["commits"] = touched
            held.append(
                Candidate(
                    **base,
                    relation=name,
                    status="confirmed",
                    detail=f"seed {name} ({path}) hashes differently at the failing "
                    "run's commit than at the last passing run's: it was edited in "
                    "the break's range",
                    evidence=evidence,
                )
            )
        else:
            held.append(
                Candidate(
                    **base,
                    relation=name,
                    status="ruled_out",
                    reason="same_hash",
                    detail=f"seed {name} ({path}) hashes the same at the failing "
                    "run's commit as at a passing one",
                    evidence=evidence,
                )
            )
    return held


# --- the schedule --------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    """One successful build of a model, from ``dbt_run_results``."""

    model: str
    invocation: str
    started: datetime | None
    completed: datetime | None


def last_build(
    builds: Sequence[Build], model: str, run: str, at: datetime | None
) -> Build | None:
    """The build of ``model`` a test in ``run`` read: the one ``run`` itself
    made, else the newest that completed before ``run`` started."""

    own = [b for b in builds if b.model == model and b.invocation == run]
    if own:
        return max(own, key=lambda b: utc(b.completed) if b.completed else _EPOCH)
    if at is None:
        return None
    earlier = [
        b
        for b in builds
        if b.model == model
        and b.invocation != run
        and b.completed is not None
        and utc(b.completed) <= utc(at)
    ]
    return max(earlier, key=lambda b: utc(b.completed)) if earlier else None  # type: ignore[arg-type]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _flipped(child: Build, parent: Build) -> bool | None:
    """Whether the parent's build completed after the child's started: the
    child read a parent older than the one the test reads."""

    if parent.completed is None or child.started is None:
        return None
    return utc(parent.completed) > utc(child.started)


def _build(build: Build) -> dict[str, Any]:
    return {
        "invocation_id": build.invocation,
        "started_at": build.started.isoformat() if build.started else None,
        "completed_at": build.completed.isoformat() if build.completed else None,
    }


def schedule_candidate(
    pair: tuple[str, str],
    names: Mapping[str, str],
    builds: Sequence[Build],
    span: DiagnosisRange,
) -> Candidate:
    """The schedule candidate for one (child, parent) pair: the order of the
    two models' last builds at the last passing run and at the first failing
    one."""

    child, parent = pair
    child_name = names.get(child, child.split(".")[-1])
    parent_name = names.get(parent, parent.split(".")[-1])
    base = {
        "owner": "schedule",
        "kind": "built_before_parent",
        "relation": child_name,
        "check": SCHEDULE_CHECK,
    }
    pair_evidence: dict[str, Any] = {"child": child_name, "parent": parent_name}
    if span.after == UNKNOWN:
        return Candidate(
            **base,
            status="not_checked",
            reason="after_unknown",
            detail="no passing run precedes the failure in what the sources hold, "
            "so the build order before the break cannot be read",
            evidence=pair_evidence,
        )
    good, bad = span.after, span.through
    at_bad = (
        last_build(builds, child, bad.run, bad.at),
        last_build(builds, parent, bad.run, bad.at),
    )
    at_good = (
        last_build(builds, child, good.run, good.at),
        last_build(builds, parent, good.run, good.at),
    )
    if None in at_bad or None in at_good:
        missing = [
            f"{name} at the {when} run"
            for when, held in (("failing", at_bad), ("last passing", at_good))
            for name, build in zip((child_name, parent_name), held, strict=True)
            if build is None
        ]
        return Candidate(
            **base,
            status="not_checked",
            reason="no_build_recorded",
            detail="dbt_run_results records no successful build of "
            + "; ".join(missing),
            evidence=pair_evidence,
        )
    bad_child, bad_parent = at_bad
    good_child, good_parent = at_good
    assert bad_child and bad_parent and good_child and good_parent  # noqa: S101
    flipped_bad = _flipped(bad_child, bad_parent)
    flipped_good = _flipped(good_child, good_parent)
    evidence = {
        **pair_evidence,
        "failing_run": {
            "run": bad.run,
            "child": _build(bad_child),
            "parent": _build(bad_parent),
        },
        "last_passing_run": {
            "run": good.run,
            "child": _build(good_child),
            "parent": _build(good_parent),
        },
    }
    if flipped_bad is None or flipped_good is None:
        return Candidate(
            **base,
            status="not_checked",
            reason="no_build_times",
            detail="dbt_run_results records a build with no execution time, so "
            "the order of the two builds cannot be read",
            evidence=evidence,
        )
    if not flipped_bad:
        return Candidate(
            **base,
            status="ruled_out",
            reason="parent_first",
            detail=f"at the failing run {parent_name} was built before "
            f"{child_name} started, in dependency order",
            evidence=evidence,
        )
    if flipped_good:
        return Candidate(
            **base,
            status="ruled_out",
            reason="order_unchanged",
            detail=f"{parent_name} was already built after {child_name} at the last "
            "passing run, so the order did not change across the break",
            evidence=evidence,
        )
    return Candidate(
        **base,
        status="confirmed",
        detail=f"at the last passing run {parent_name} was built before "
        f"{child_name}; at the failing run it was built after {child_name} "
        f"started, so {child_name} read a {parent_name} older than the one the "
        "test reads",
        evidence=evidence,
    )


def schedule_unread(reason: str, detail: str) -> Candidate:
    return Candidate(
        owner="schedule",
        kind="built_before_parent",
        status="not_checked",
        check=SCHEDULE_CHECK,
        reason=reason,
        detail=detail,
    )


def no_pairs() -> Candidate:
    return Candidate(
        owner="schedule",
        kind="built_before_parent",
        status="ruled_out",
        check=SCHEDULE_CHECK,
        reason="no_parent_model",
        detail="the failing test reads no model built from another model, so no "
        "build order can break it",
    )


# --- the loader: rows that arrived after the run read them (#1623) -------------------


@dataclass(frozen=True)
class ArrivalColumns:
    """The two columns one source's arrival is read from: when a row landed
    (``capture``) and when its event happened (``event``), with where each
    was named."""

    capture: str
    capture_source: str
    event: str
    event_source: str


@dataclass(frozen=True)
class ArrivalCounts:
    """The counts one statement read over a source, never a value.

    ``bad_*`` are over the window the failing run judged, ``good_*`` over the
    window the last passing run judged (:func:`arrival_window`). ``bad_late``
    counts the rows whose capture time is after the failing run's start;
    ``good_late`` the rows captured after the last pass started and by the
    failing run's start, the lateness that pass already ran with (a row still
    missing at the failing run is that failure's own). ``*_null`` counts the
    rows with no capture time.
    """

    bad_window: int
    bad_late: int
    bad_null: int
    good_window: int
    good_late: int
    good_null: int = 0


@dataclass(frozen=True)
class ArrivalWindows:
    """The event-time windows the two runs judged, each ``(after, through]``.

    With no lag, the failing run judged the rows whose event falls after the
    last pass started, through its own start, and the last pass the window of
    the same width before its start. With a lag (a freshness test's
    ``max_lag_seconds``), each run judged the rows within that lag of its own
    start: the rows the test itself could count."""

    good_from: datetime
    good_at: datetime
    bad_from: datetime
    bad_at: datetime
    lag_seconds: int | None = None


def arrival_window(
    span: DiagnosisRange, lag_seconds: int | None = None
) -> ArrivalWindows | Candidate:
    """The windows the bounding runs judged, or the ``no_run_times`` answer
    where a start is not recorded."""

    if span.after == UNKNOWN:
        return arrival_unread(
            "no_run_times",
            "no passing run precedes the failure in what the sources hold, so "
            "there is no run whose window can show the rows landed in time",
        )
    good_at, bad_at = span.after.at, span.through.at
    if good_at is None or bad_at is None:
        missing = [
            name
            for name, at in (("last passing", good_at), ("failing", bad_at))
            if at is None
        ]
        return arrival_unread(
            "no_run_times",
            "the "
            + " and ".join(missing)
            + " run's start is not recorded (dbt_invocations' run_started_at), "
            "so no capture time can be held to it",
        )
    good, bad = utc(good_at), utc(bad_at)
    if bad <= good:
        return arrival_unread(
            "no_run_times",
            "the failing run's recorded start is not after the last passing "
            "run's, so the two runs' windows cannot be told apart",
        )
    if lag_seconds is not None and lag_seconds > 0:
        lag = timedelta(seconds=lag_seconds)
        return ArrivalWindows(good - lag, good, bad - lag, bad, lag_seconds)
    return ArrivalWindows(good - (bad - good), good, good, bad)


def _at(moment: datetime) -> str:
    return utc(moment).isoformat()


def arrival_candidate(
    relation: str,
    columns: ArrivalColumns,
    counts: ArrivalCounts,
    span: DiagnosisRange,
    corroborated_by: Sequence[Mapping[str, Any]] = (),
    lag_seconds: int | None = None,
) -> Candidate:
    """The late-arrival candidate for one source, from its counts.

    Confirmed where some rows of the failing run's window landed after that
    run started and none of the last passing run's window landed after it
    started. ``corroborated_by`` (freshness failures in the same range) is
    listed as evidence and never decides the answer.
    """

    window = arrival_window(span, lag_seconds)
    if isinstance(window, Candidate):
        return window.model_copy(update={"relation": relation})
    good_from, good_at = window.good_from, window.good_at
    bad_from, bad_at = window.bad_from, window.bad_at
    good, bad = span.after, span.through
    assert good != UNKNOWN  # noqa: S101
    evidence: dict[str, Any] = {
        "capture_column": columns.capture,
        "capture_source": columns.capture_source,
        "event_column": columns.event,
        "event_source": columns.event_source,
        "failing_run": {
            "run": bad.run,
            "started_at": _at(bad_at),
            "window": {"after": _at(bad_from), "through": _at(bad_at)},
            "window_rows": counts.bad_window,
            "late_rows": counts.bad_late,
            "null_capture_rows": counts.bad_null,
        },
        "last_passing_run": {
            "run": good.run,  # type: ignore[union-attr]
            "started_at": _at(good_at),
            "window": {"after": _at(good_from), "through": _at(good_at)},
            "window_rows": counts.good_window,
            "late_rows": counts.good_late,
            "null_capture_rows": counts.good_null,
        },
        "window_basis": (
            f"max_lag ({window.lag_seconds}s)"
            if window.lag_seconds is not None
            else "run_to_run"
        ),
        "late_rows": counts.bad_late,
        "read": "the source as it stands now",
    }
    if corroborated_by:
        evidence["corroborated_by"] = [dict(c) for c in corroborated_by]
    base = {
        "owner": "loader",
        "kind": "source_arrived_late",
        "relation": relation,
        "columns": (columns.event, columns.capture),
        "check": ARRIVAL_CHECK,
        "evidence": evidence,
    }
    if counts.bad_late == 0 and counts.bad_null > 0:
        return Candidate(
            **base,
            status="not_checked",
            reason="no_capture_time",
            detail=f"{counts.bad_null} of the {counts.bad_window} row(s) of the "
            f"window the failing run judged have no {columns.capture}, and none "
            "of the rest landed after the run started, so when those rows landed "
            "cannot be told",
        )
    if counts.bad_late == 0:
        held = (
            "the window the failing run judged holds no row today"
            if counts.bad_window == 0
            else f"every one of the {counts.bad_window} row(s) whose "
            f"{columns.event} falls in the window the failing run judged has a "
            f"{columns.capture} before that run started"
        )
        return Candidate(
            **base,
            status="ruled_out",
            reason="arrived_before_run",
            detail=f"{held}, so no row of {relation} arrived after the run read them",
        )
    if counts.good_late == 0 and counts.good_null > 0:
        return Candidate(
            **base,
            status="not_checked",
            reason="no_capture_time",
            detail=f"{counts.bad_late} row(s) of the failing run's window landed "
            f"after it started, but {counts.good_null} row(s) of the last passing "
            f"run's window have no {columns.capture}, so whether that window had "
            "all landed in time cannot be told",
        )
    if counts.good_late > 0:
        return Candidate(
            **base,
            status="ruled_out",
            reason="lateness_unchanged",
            detail=f"{counts.good_late} row(s) of the window the last passing run "
            f"judged also landed after that run started, so the lateness of "
            f"{relation} was already there while the test passed",
        )
    return Candidate(
        **base,
        status="confirmed",
        detail=f"{counts.bad_late} of the {counts.bad_window} row(s) of {relation} "
        f"whose {columns.event} falls in the window the failing run judged have a "
        f"{columns.capture} after that run started ({_at(bad_at)}), while every row "
        "of the last passing run's window had landed before it started: the "
        "loader delivered them after the run read the source",
    )


def arrival_unread(reason: str, detail: str, relation: str | None = None) -> Candidate:
    return Candidate(
        owner="loader",
        kind="source_arrived_late",
        relation=relation,
        status="not_checked",
        check=ARRIVAL_CHECK,
        reason=reason,
        detail=detail,
    )


__all__ = [
    "ARRIVAL_CHECK",
    "LOCK_FILE",
    "PACKAGE_FILES",
    "ArrivalColumns",
    "ArrivalCounts",
    "ArrivalWindows",
    "Bounds",
    "Build",
    "Lineage",
    "Pin",
    "ProjectChange",
    "arrival_candidate",
    "arrival_unread",
    "arrival_window",
    "last_build",
    "lineage",
    "no_pairs",
    "package_candidate",
    "parse_lock",
    "partition",
    "project_change",
    "project_not_in_lineage",
    "project_test_only",
    "project_verified",
    "range_bounds",
    "retype_candidates",
    "retype_unmeasured",
    "same_commit",
    "schedule_candidate",
    "schedule_unread",
    "seed_candidates",
    "unknown_range",
    "unproven",
]
