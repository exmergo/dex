"""Declarations for models that already exist (#501): ``transform declare``.

A declaration (#491) is written for the models a plan creates or edits, and the
draft (#493) measures most of one from the cache and the SQL. A project that
predates both has no declarations, and nothing adds them unasked: on a project
of thousands of models an unrequested backfill is a diff nobody reviews. This is
the opt-in: select existing models, and one plan proposes what dex already
measured about each of them.

Three rules hold throughout:

- **Only what is absent.** A field the model's YAML already states is never
  proposed, so a model a human declared keeps every word of it, and a second run
  after the plan is applied proposes nothing for the same model.
- **Only facts that hold as a declaration.** The grain, a ``key`` or
  ``foreign_key`` role, a ``references`` that resolves to something in this
  project, and ``semi_additive`` for a running total. A unit shape is a
  hypothesis from a value range, and a profile with no NULL is not a rule that
  NULL never occurs, so both stay in ``data.declarations.<model>.drafted`` as
  evidence and are never written. Intent (description, population, assumptions)
  is never drafted; it comes from the payload or it is listed as missing.
- **Named, not scanned.** Everything here reads the repository and the cache. A
  relation the cache has not profiled is named with the command that would
  profile it, and nothing is opened to find out more.

Every selected model is either proposed or skipped with a reason, and the plan is
capped so a project-wide selector stays reviewable. Skipped models do not count
against the cap, so applying a plan and running the same selector again moves on
to the next batch.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from . import declarations as declarations_mod
from .declarations import Declaration, DeclarationError
from .rewrite import RewriteError

#: Models proposed per plan. A plan is a diff somebody reads; past a couple of
#: dozen models it stops being one.
MAX_MODELS = 25


@dataclass
class Selection:
    """The models a selector names, in the order they are considered."""

    models: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Proposal:
    """What ``transform declare`` will write for one model, and why."""

    model: str
    path: str
    declaration: Declaration
    fields: dict[str, Any] = field(default_factory=dict)
    supplied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"model": self.model, "path": self.path}
        out["fields"] = self.fields
        if self.supplied:
            out["supplied"] = self.supplied
        return out


@dataclass
class Outcome:
    proposals: list[Proposal] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    elided: list[str] = field(default_factory=list)


# --- the selector -----------------------------------------------------------------


def select_models(
    view: Any, objects: Iterable[str] | None, root: Path | None = None
) -> Selection:
    """The models ``objects`` names, the way ``maintain verify`` reads them.

    Model names, repeatable and comma-separated, case-insensitive; none at all
    selects every model. Read from the repository alone, so no compile is
    needed. Unlike ``verify``, a name that matches nothing is reported rather
    than dropped: a declaration that silently did not happen reads like one that
    did.
    """

    files = {path: source.content for path, source in view.files.items()}
    sql = _model_files(files, view.model_paths)
    versioned = _versioned_models(files)
    by_name = {Path(path).stem.lower(): Path(path).stem for path in sql}

    wanted = [
        name.strip() for raw in objects or [] for name in raw.split(",") if name.strip()
    ]
    selection = Selection()
    if not wanted:
        names = [Path(path).stem for path in sql]
    else:
        names = []
        for name in dict.fromkeys(wanted):
            base = name.split(".", 1)[0].lower()
            if "." in name and base in versioned:
                names.append(name)
                continue
            found = by_name.get(name.lower())
            if found is None:
                selection.skipped.append(_unmatched(name, view, root))
            elif found not in names:
                names.append(found)

    for name in names:
        owner = _version_owner(name, versioned)
        if owner is not None:
            selection.skipped.append(
                {
                    "model": name,
                    "reason": "versioned",
                    "detail": (
                        f"'{owner}' is a versioned model; its declaration belongs "
                        "under each entry in `versions:`, which dex does not "
                        "write. Declare it by hand"
                    ),
                }
            )
            continue
        selection.models.append(name)
    return selection


def _model_files(files: Mapping[str, str], model_paths: Iterable[str]) -> list[str]:
    roots = [PurePosixPath(p) for p in model_paths]
    return sorted(
        path
        for path in files
        if PurePosixPath(path).suffix == ".sql"
        and any(root in PurePosixPath(path).parents for root in roots)
    )


def _versioned_models(files: Mapping[str, str]) -> set[str]:
    """Lowercased names of the models whose YAML entry carries ``versions:``."""

    out: set[str] = set()
    for path, content in files.items():
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry.get("versions")
            ):
                out.add(entry["name"].lower())
    return out


def _version_owner(name: str, versioned: set[str]) -> str | None:
    """The versioned model ``name`` is a version of (or is), if any.

    dbt's default file for version ``2`` of ``dim_customers`` is
    ``dim_customers_v2.sql``, and a selector may also say ``dim_customers.v2``.
    """

    lowered = name.lower()
    base = lowered.split(".", 1)[0]
    if base in versioned:
        return name.split(".", 1)[0]
    match = re.fullmatch(r"(.+)_v[^_]+", lowered)
    if match and match.group(1) in versioned:
        return name[: len(match.group(1))]
    return None


def _unmatched(name: str, view: Any, root: Path | None) -> dict[str, Any]:
    if root is not None:
        for configured in view.model_paths:
            base = root / configured
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if path.stem.lower() == name.lower():
                    return {
                        "model": name,
                        "reason": "python_model",
                        "detail": (
                            f"{path.relative_to(root).as_posix()} is a Python model; "
                            "dex drafts from SQL only"
                        ),
                    }
    where = ", ".join(view.model_paths)
    return {
        "model": name,
        "reason": "not_a_model",
        "detail": f"no model named '{name}' is defined under {where}",
    }


# --- what to propose --------------------------------------------------------------


def propose(
    view: Any,
    models: list[str],
    supplied: list[Declaration],
    *,
    draft: Callable[[str], tuple[dict[str, Any] | None, list[str]]],
    cap: int,
) -> Outcome:
    """Each model's proposal, or the reason it has none, up to ``cap`` proposals.

    ``draft`` returns one model's ``drafted``/``declared``/``missing`` entry for
    the project as it stands. ``supplied`` is the payload's own declarations,
    which win field by field over anything drafted and are considered first.
    """

    files = {path: source.content for path, source in view.files.items()}
    by_model = {d.model.lower(): d for d in supplied}
    order = [m for m in models if m.lower() in by_model] + [
        m for m in models if m.lower() not in by_model
    ]
    outcome = Outcome()
    # The project as each accepted proposal leaves it, so two models declared in
    # one shared file are rendered one after the other, the way the plan will.
    rendered = dict(files)

    for index, model in enumerate(order):
        given = by_model.get(model.lower())
        if len(outcome.proposals) >= cap and given is None:
            outcome.elided = order[index:]
            break
        entry, _warnings = draft(model)
        if entry is None and given is None:
            outcome.skipped.append(
                {
                    "model": model,
                    "reason": "sql_unreadable",
                    "detail": "dex could not read the model's SQL to draft from it",
                }
            )
            continue
        entry = entry or {"drafted": {}, "declared": {}, "missing": []}
        fields, payload = _absent_facts(entry, rendered, view)
        if given is not None:
            payload, supplied_fields = _merge(payload, given)
        else:
            supplied_fields = []
        if not fields and not supplied_fields:
            # Whether anything would have been proposed had nothing been declared
            # yet: that separates a model already complete from one dex has no
            # measured fact for.
            candidates, _ = _absent_facts({**entry, "declared": {}}, rendered, view)
            outcome.skipped.append(
                _why_nothing(model, entry, complete=bool(candidates))
            )
            continue

        declaration = Declaration.model_validate({"model": model, **payload})
        sql_path = declarations_mod.model_sql_path(rendered, view.model_paths, model)
        path = declarations_mod.schema_path(
            rendered, model
        ) or declarations_mod._default_schema_path(sql_path or "", model)
        try:
            if given is None:
                # A drafted fact the declaration check would refuse is a draft
                # that does not fit this model, not a reason to refuse the batch.
                declarations_mod._check(
                    declaration, rendered, view, cache=None, pii_overrides=None
                )
            text = declarations_mod.render(rendered.get(path), declaration)
        except (RewriteError, DeclarationError) as exc:
            if given is not None and isinstance(exc, DeclarationError):
                raise
            outcome.skipped.append(
                {
                    "model": model,
                    "reason": "not_writable",
                    "detail": f"{path}: {exc}",
                }
            )
            continue
        if text == rendered.get(path):
            outcome.skipped.append(_why_nothing(model, entry, complete=True))
            continue
        rendered[path] = text
        outcome.proposals.append(
            Proposal(
                model=model,
                path=path,
                declaration=declaration,
                fields=fields,
                supplied=supplied_fields,
            )
        )
    return outcome


def _absent_facts(
    entry: dict[str, Any], files: Mapping[str, str], view: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The drafted facts the model does not yet declare.

    Returns them twice: as evidence (``{value, evidence}`` per field, for the
    envelope) and as a declaration payload (plain values, for the plan).
    """

    drafted = entry.get("drafted") or {}
    declared = entry.get("declared") or {}
    stated = {
        name.lower(): (name, facts)
        for name, facts in (declared.get("columns") or {}).items()
    }
    evidence: dict[str, Any] = {}
    payload: dict[str, Any] = {}

    grain = drafted.get("grain") or {}
    if grain.get("value") and not declared.get("grain"):
        evidence["grain"] = {"value": grain["value"], "evidence": grain["evidence"]}
        payload["grain"] = list(grain["value"])

    columns_evidence: dict[str, Any] = {}
    columns_payload: dict[str, Any] = {}
    for name, facts in (drafted.get("columns") or {}).items():
        written_name, existing = stated.get(name.lower(), (name, {}))
        out_evidence: dict[str, Any] = {}
        out_payload: dict[str, Any] = {}
        role = (facts.get("role") or {}).get("value")
        if role and not existing.get("role"):
            out_evidence["role"] = facts["role"]
            out_payload["role"] = role
        effective_role = existing.get("role") or role
        reference = (facts.get("references") or {}).get("value")
        if (
            reference
            and not existing.get("references")
            and effective_role in (None, "foreign_key")
            and _resolves(reference, files, view)
        ):
            out_evidence["references"] = facts["references"]
            out_payload["references"] = reference
        additivity = (facts.get("additivity") or {}).get("value")
        if additivity and not existing.get("additivity"):
            out_evidence["additivity"] = facts["additivity"]
            out_payload["additivity"] = additivity
        if out_payload:
            columns_evidence[written_name] = out_evidence
            columns_payload[written_name] = out_payload
    if columns_payload:
        evidence["columns"] = columns_evidence
        payload["columns"] = columns_payload
    return evidence, payload


def _resolves(reference: str, files: Mapping[str, str], view: Any) -> bool:
    """Whether ``references`` names something a relationships test can ``ref()``.

    A join to a raw source table is verified evidence, but a declaration's
    ``references`` renders ``ref('<parent>')``, so it is proposed only once a
    model, seed or snapshot of this project stands for the parent.
    """

    parent, _, column = reference.rpartition(".")
    if not parent or not column:
        return False
    if declarations_mod.model_sql_path(files, view.model_paths, parent) is not None:
        return True
    return declarations_mod._is_seed_or_snapshot(files, view, parent)


def _merge(
    payload: dict[str, Any], given: Declaration
) -> tuple[dict[str, Any], list[str]]:
    """``payload`` with the supplied declaration laid over it, field by field."""

    stated = given.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    stated.pop("model", None)
    merged = dict(payload)
    names: list[str] = []
    for key, value in stated.items():
        if key == "columns":
            columns = {k: dict(v) for k, v in (merged.get("columns") or {}).items()}
            lowered = {k.lower(): k for k in columns}
            for column, facts in value.items():
                target = lowered.get(column.lower(), column)
                columns.setdefault(target, {}).update(facts)
                names.extend(f"columns.{column}.{fact}" for fact in facts)
            merged["columns"] = columns
        else:
            merged[key] = value
            names.append(key)
    return merged, names


_NOT_PROFILED = "the cache has not profiled it"


def _why_nothing(
    model: str, entry: dict[str, Any], *, complete: bool = False
) -> dict[str, Any]:
    drafted = entry.get("drafted") or {}
    missing = list(entry.get("missing") or [])
    unprofiled = [
        i
        for i in drafted.get("inputs") or []
        if not i.get("profiled") and i.get("relation")
    ]
    out: dict[str, Any] = {"model": model}
    if complete:
        out["reason"] = "complete"
        out["detail"] = "the model already declares every fact dex can draft for it" + (
            "; intent nobody has supplied is listed under `missing`" if missing else ""
        )
    elif unprofiled:
        out["reason"] = "unprofiled_inputs"
        # The drafter's own reason where it says more than "not profiled": no
        # cache at all, or a name matching several relations.
        reasons = list(
            dict.fromkeys(i.get("reason") or _NOT_PROFILED for i in unprofiled)
        )
        # An input no command can profile (#893) is stated with its reason and
        # left out of the commands, rather than named with one that fails.
        commands = [i["command"] for i in unprofiled if i.get("command")]
        out["detail"] = (
            "nothing was drafted from "
            + ", ".join(i["relation"] for i in unprofiled)
            + ": "
            + "; ".join(reasons)
            + (
                ". Run " + ", ".join(f"`{c}`" for c in commands) + " and declare again"
                if commands
                else ""
            )
        )
        out["commands"] = commands
    elif drafted.get("incomplete"):
        out["reason"] = "sql_unreadable"
        out["detail"] = "; ".join(drafted["incomplete"])
    else:
        out["reason"] = "nothing_drafted"
        out["detail"] = (
            "the cache proves no key, verified join or running total for this "
            "model's columns, so there is no measured fact to propose"
        )
    if missing:
        out["missing"] = missing
    return out
