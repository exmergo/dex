"""Every producer of a live feature toggle's columns, on both values (#1113).

A flag that stays live has two behaviors, and a change to it has to hold in
both. "Hide the fee columns when ``enable_fees`` is false" is done when no model
of the project outputs a fee column with the flag off, which a search of the
models the author gated cannot show: the run behind #1113 gated the marts and
left thirteen ledger models still exposing the column, and on an empty table a
row comparison cannot see it either.

So the author states the toggle in a ``toggle`` contract on ``transform plan``:
the variable, the value that turns the feature on and the one that turns it off,
and the columns the feature governs. Dex then finds the producers itself, from
compiled SQL, never from the models the edits happen to touch:

- at plan time (:func:`analyze`), every model of this project is compiled in a
  throwaway copy at the on value and at the off value, and the tree before the
  plan at the on value. No statement reads a row.
- in ``transform compare``, both sides compile at the on value, so
  ``equivalent`` means "on keeps today's results".
- after a build (:func:`verify_outputs`), the built relations' columns are read
  from metadata, which judges an empty table the same as a full one, against the
  value the build ran with. The other value was not built, and says so.

Distinct from :mod:`.retirement`, which removes the flag and keeps one behavior.
A check that cannot run is ``unchecked`` with a ``reason_code``, never clean, and
an incomplete reading of the project is never reported as ``satisfied``.
"""

# ruff: noqa: PERF401
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ..dbt_project import DbtProjectView, jinja_calls
from ..references import column_parse_limit
from ..semantic_declarations import Assumption, _Strict
from .compiled_tree import (
    _QUOTES,
    Branch,
    Catalog,
    compile_branch,
    identifiers,
    macro_callers,
    macro_closure,
)
from .plans import PlanEdit

if TYPE_CHECKING:
    from ..engine import DexEngine

#: The most records of one kind a plan or build lists before counting the rest.
MAX_MODELS = 200

_Scalar = bool | int | float | str | None


class ToggleContract(_Strict):
    """What the author states about a flag that stays. Nothing is inferred."""

    #: The value that turns the feature on, and the one that turns it off. Both
    #: required, since a default says what the project does today, not which
    #: value means "on".
    on_value: _Scalar
    off_value: _Scalar
    #: The output columns the feature governs: present with it on, absent with
    #: it off, in every model of the project but the exempt ones.
    columns: list[str] = Field(min_length=1)
    #: Models allowed to keep a governed column with the feature off (a staging
    #: model passing a source column through, say). Each still has to keep it
    #: with the feature on. Name the reason in ``assumptions``.
    exempt_models: list[str] = Field(default_factory=list)
    #: The owner-approved scope. Installed packages are never edited.
    packages: Literal["out_of_scope"] = "out_of_scope"
    assumptions: list[Assumption] = Field(default_factory=list)

    @field_validator("on_value", "off_value", mode="before")
    @classmethod
    def _scalar(cls, value: Any) -> Any:
        """Refuse a list or a mapping where one flag value is expected."""

        if isinstance(value, (list, dict)):
            raise ValueError("a toggle value is one scalar value of the flag")
        return value

    @model_validator(mode="after")
    def _distinct(self) -> ToggleContract:
        """Refuse equal on and off values and a column named twice."""

        if self.on_value == self.off_value and type(self.on_value) is type(
            self.off_value
        ):
            raise ValueError("on_value and off_value must differ")
        lowered = [c.lower() for c in self.columns]
        if len(set(lowered)) != len(lowered):
            raise ValueError("columns names a column twice")
        return self


def parse_contract(raw: Any) -> tuple[str, ToggleContract]:
    """The payload's ``toggle``, as its variable and contract, or a refusal."""

    if not isinstance(raw, dict):
        raise ValueError(
            "toggle must be an object: {variable, on_value, off_value, columns}"
        )
    variable = raw.get("variable")
    if not isinstance(variable, str) or not variable.strip():
        raise ValueError("toggle needs variable: the name of the flag that stays")
    missing = [key for key in ("on_value", "off_value") if key not in raw]
    if missing:
        raise ValueError(
            f"toggle needs {' and '.join(missing)}: the flag value that turns the "
            "feature on and the one that turns it off. Dex does not infer either "
            "from a default, which says what the project does today and not what "
            "each value means"
        )
    try:
        contract = ToggleContract.model_validate(
            {k: v for k, v in raw.items() if k != "variable"}
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'toggle'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(
            f"the toggle contract is not valid: {problems}. It takes variable, "
            "on_value, off_value, columns, exempt_models, packages (out_of_scope) "
            "and assumptions"
        ) from exc
    return variable.strip(), contract


def stored_form(variable: str, contract: ToggleContract) -> dict[str, Any]:
    """What a stored plan and a plan document carry, before :func:`analyze` adds
    the producers it found."""

    return {"variable": variable, **contract.model_dump(mode="json")}


def contract_of(stored: Mapping[str, Any]) -> ToggleContract:
    """The contract back out of its stored form."""

    return ToggleContract.model_validate(
        {
            k: v
            for k, v in stored.items()
            if k
            not in (
                "variable",
                "producers",
                "judged_models",
                "analysis_failed",
                "plan_gaps",
            )
        }
    )


def decisions(stored: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The contract's assumptions, shaped as ``data.decisions`` entries."""

    if not stored:
        return []
    return [
        {
            "toggle": stored.get("variable"),
            "decision": entry.get("decision"),
            "chosen": entry.get("chosen"),
            "evidence": entry.get("evidence"),
            **({"column": entry["column"]} if entry.get("column") else {}),
        }
        for entry in stored.get("assumptions") or []
    ]


# --- what the repository says about the flag ---------------------------------


def _files_after(view: DbtProjectView, edits: list[PlanEdit]) -> DbtProjectView:
    """The project as it reads once ``edits`` land."""

    from .propagate import _project_after_edits

    return _project_after_edits(view, edits)


def local_reads(view: DbtProjectView, variable: str) -> tuple[list[str], list[str]]:
    """Where this project reads the flag, and where it reads a var it cannot name.

    SQL reads come from the reference index; reads written in YAML (a
    ``+enabled: "{{ var('x') }}"``) are found separately, since the index records
    none. Both are ``path:line``.
    """

    from ..references import ReferenceIndex
    from .retirement import yaml_reads

    index = ReferenceIndex(view, scan_packages=False)
    reads = sorted(
        f"{hit.path}:{hit.line}"
        for hit in index.references_to(variable, "var")[0]
        if hit.form == "var_call" and not hit.package
    )
    reads += yaml_reads(
        {path: source.content for path, source in view.files.items()}, variable
    )
    dynamic = sorted(
        f"{hit.path}:{hit.line}"
        for hit in index.indeterminate_for("var")
        if not hit.package
    )
    return reads, dynamic


def require_read(view: DbtProjectView, edits: list[PlanEdit], variable: str) -> None:
    """Refuse a toggle on a variable the plan's tree never reads."""

    reads, _dynamic = local_reads(_files_after(view, edits), variable)
    if not reads:
        raise ValueError(
            f"nothing in this project reads var('{variable}') once the plan lands, "
            "so there is no toggle to check. Gate the feature's columns on it in "
            "the same payload"
        )


def built_value(view: DbtProjectView, variable: str) -> tuple[bool, Any]:
    """The value a plain build of this tree gives the flag, if it is static.

    ``vars:`` in ``dbt_project.yml`` first; else the default every local read
    states, where they all state the same one. ``(False, None)`` where neither
    settles it, which a caller reports as ``value_unknown``.
    """

    from ..references import project_vars

    declared = project_vars(view)
    if variable in declared and not isinstance(declared[variable], (dict, list)):
        return True, declared[variable]
    defaults: list[Any] = []
    for path, source in view.files.items():
        if not path.endswith(".sql"):
            continue
        for call in jinja_calls(source.content):
            if call.callee != "var" or not call.args or call.args[0] != variable:
                continue
            if len(call.args) < 2:
                return False, None
            defaults.append(_literal(call.args[1]))
    if defaults and all(d == defaults[0] for d in defaults):
        return True, defaults[0]
    return False, None


def _literal(value: Any) -> Any:
    """A jinja literal as YAML reads it: ``false`` and ``False`` are False."""

    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    return value


def matches(value: Any, wanted: Any) -> bool:
    """Whether a flag value is the contract's value, tolerating YAML spelling."""

    return _literal(value) == _literal(wanted) and isinstance(
        _literal(value), bool
    ) == isinstance(_literal(wanted), bool)


# --- plan time: compile every model on both values ---------------------------


def _record(model: str) -> dict[str, Any]:
    """A fresh record for ``model``, satisfied until a check says otherwise."""

    return {"model": model, "status": "satisfied", "checks": []}


def _contradict(record: dict[str, Any], check: str, detail: str, **where: Any) -> None:
    """Mark ``record`` contradicted by ``check``, keeping every check's detail
    and, where a check names a site (a file and line), that site."""

    record["status"] = "contradicted"
    record["finding"] = record.get("finding") or check
    record["checks"].append({"check": check, "detail": detail, **where})


def _uncheck(record: dict[str, Any], code: str, reason: str | None = None) -> None:
    """Mark ``record`` unchecked with ``code``, never downgrading a contradiction."""

    if record["status"] != "contradicted":
        record["status"] = "unchecked"
    record.setdefault("reason_code", code)
    if reason:
        record.setdefault("reason", reason)


def analyze(
    engine: DexEngine,
    stored: Any,
    project: Path,
    view: DbtProjectView,
    variable: str,
    contract: ToggleContract,
) -> dict[str, Any]:
    """Hold every model of the project to the toggle, from compiled SQL alone.

    Free: three compiles in throwaway copies (the proposal at each value, and
    the tree before the plan at the on value), and no statement reads a row.
    """

    from ..adapters import get_dialect
    from ..storage import readable_cache
    from .retirement import package_scope

    connector = engine.connector or engine.config.connector
    dialect = get_dialect(connector) if connector else None
    branches = {
        side: compile_branch(
            engine,
            stored,
            project,
            view,
            connector,
            vars_={variable: value},
            proposed=proposed,
        )
        for side, value, proposed in (
            ("on", contract.on_value, True),
            ("off", contract.off_value, True),
            ("before", contract.on_value, False),
        )
    }
    cached: list[Any] = []

    def cache() -> Any:
        """The exploration cache, read once and only when a star needs it."""

        if not cached:
            cached.append(readable_cache(engine.store))
        return cached[0]

    after = _files_after(view, list(stored.edits))
    for side, branch in branches.items():
        if branch.failure is None:
            # A proposed branch reads the plan's own seeds, the tree before it
            # the project's.
            catalog = Catalog(
                branch, after if side != "before" else view, dialect, cache
            )
            for label in branch.models:
                catalog.resolve(label)

    governed = {c.lower() for c in contract.columns}
    exempt = {m.lower() for m in contract.exempt_models}
    reads, dynamic = local_reads(after, variable)
    from ..references import ReferenceIndex

    index = ReferenceIndex(after, scan_packages=False)
    # The compiled SQL is the judge (#1156): a file whose source the column scan
    # could not parse once its Jinja was blanked is either a model judged below
    # from what dbt compiled, or a macro whose effect is inside those models.
    # Every other reason the index is short still stands.
    unparsed = set(index.column_unread)
    limits: list[str] = [
        limit
        for limit in index.limits
        if not any(limit == column_parse_limit(path) for path in unparsed)
    ]

    on, off, before = branches["on"], branches["off"], branches["before"]
    if before.failure and not (on.failure and off.failure):
        limits.append(
            "the tree before this plan did not compile, so columns the plan "
            f"drops were not checked: {before.failure}"
        )
    if on.failure and off.failure:
        names = sorted({*before.models})
        unavailable = []
        for name in names[:MAX_MODELS]:
            entry = _record(name)
            _uncheck(entry, "compile_unavailable", on.failure)
            unavailable.append(entry)
        return _report(
            unavailable,
            [*limits, f"dbt could not compile: {on.failure}"],
            reads,
            len(names),
        )

    records: list[dict[str, Any]] = []
    producers: dict[str, list[str]] = {}
    compiled = (
        set(on.models)
        | set(off.models)
        | set(before.models)
        | set(on.failed)
        | set(off.failed)
    )
    labels = sorted(
        set(on.models)
        | set(off.models)
        | set(before.models)
        | set(on.failed)
        | set(off.failed)
    )
    for label in labels:
        record = _record(label)
        records.append(record)
        _judge_model(
            label, record, branches, governed, exempt, dialect, contract, producers
        )

    for side in ("on", "off"):
        if branches[side].failure:
            value = contract.on_value if side == "on" else contract.off_value
            for record in records:
                _contradict(
                    record,
                    "branch_compile_failed",
                    f"the project does not compile with {variable}={value!r}: "
                    f"{branches[side].failure}",
                )
            break

    project_name = view.project_name or _manifest_project(branches)
    callers = _callers(branches, project_name)
    macro_reads = _attributed(reads, callers, view)
    dynamic_reads = _attributed(dynamic, callers, view, keep_models=True)
    not_compiled = _not_compiled(branches, reads, view)
    for entry in not_compiled:
        record = _record(entry["model"])
        _uncheck(
            record,
            "not_compiled",
            f"dbt leaves {entry['path']} disabled at both values, so its output "
            "was never compiled; enable it, or delete it if it is retired",
        )
        record["path"] = entry["path"]
        records.append(record)
    if not_compiled:
        limits.append(
            f"{len(not_compiled)} disabled model(s) read this flag and were not "
            "compiled, so the toggle is unchecked for them only: "
            + ", ".join(e["model"] for e in not_compiled[:10])
        )

    # What else reads a governed column with the flag off: a test that still
    # runs, and a semantic definition that stops resolving (#1169).
    by_label = {record["model"]: record for record in records}
    sites, reader_limits = governed_readers(
        index,
        after,
        off,
        {
            label: cols
            for label, cols in producers.items()
            if label.lower() not in exempt
        },
        name=project_name,
        variable=variable,
        on_value=contract.on_value,
        off_value=contract.off_value,
    )
    limits.extend(reader_limits)
    for label, found in sites.items():
        for site in found:
            check = site.pop("check")
            detail = site.pop("detail")
            _contradict(by_label[label], check, detail, **site)

    judged = [r for r in records if r["status"] != "satisfied" or r.get("produces")]
    report = _report(
        judged,
        limits,
        reads,
        len(labels),
        clean=len(labels) - len([r for r in judged if r["model"] in compiled]),
    )
    report["producers"] = producers
    report["exempt_models"] = sorted(exempt)
    report["macro_reads"] = macro_reads
    report["dynamic_reads"] = dynamic_reads
    report["not_compiled"] = not_compiled
    report["package_scope"] = package_scope(view, project, variable)
    return report


def _manifest_project(branches: Mapping[str, Branch]) -> str:
    """The project's name as dbt wrote it, where ``dbt_project.yml`` left it out."""

    for branch in branches.values():
        for model in branch.models.values():
            if model.package:
                return model.package
    return ""


def _callers(branches: Mapping[str, Branch], project: str) -> dict[str, list[str]]:
    """Per macro file, the models calling it on either proposed branch."""

    merged: dict[str, set[str]] = {}
    for side in ("on", "off"):
        if branches[side].failure is None:
            for path, labels in macro_callers(branches[side], project).items():
                merged.setdefault(path, set()).update(labels)
    return {path: sorted(labels) for path, labels in sorted(merged.items())}


def _attributed(
    sites: list[str],
    callers: Mapping[str, list[str]],
    view: DbtProjectView,
    *,
    keep_models: bool = False,
) -> list[dict[str, Any]]:
    """Each read in a macro, with the models whose compiled SQL it reaches.

    ``sites`` are ``path:line``. A read in a model file is the model's own,
    already judged from its compiled SQL; it is listed only with
    ``keep_models`` (a dynamic read), naming its model. A macro nothing calls
    changes no model.
    """

    out: list[dict[str, Any]] = []
    for site in sites:
        path, _, line = site.rpartition(":")
        if not path:
            continue
        entry: dict[str, Any] = {
            "path": path,
            "line": int(line) if line.isdigit() else None,
        }
        if path in callers or _is_macro(path, view):
            entry["macro"] = True
            entry["models"] = list(callers.get(path, []))[:MAX_MODELS]
        elif keep_models and path.endswith(".sql"):
            entry["models"] = [Path(path).stem]
        else:
            continue
        out.append(entry)
    return out[:MAX_MODELS]


def _is_macro(path: str, view: DbtProjectView) -> bool:
    """Whether ``path`` sits under one of the project's macro paths."""

    return any(
        path == base or path.startswith(base.rstrip("/") + "/")
        for base in (getattr(view, "macro_paths", None) or ["macros"])
    )


def _not_compiled(
    branches: Mapping[str, Branch], reads: list[str], view: DbtProjectView
) -> list[dict[str, Any]]:
    """Disabled models that read the flag, which no branch compiled (#1156).

    Read the flag means the model's own file has a ``var()`` read of it, or its
    disabled node calls a project macro that does, directly or through another
    macro. A macro read counts whether or not any enabled model calls it. A
    model enabled at either value is judged instead, with its disabled side
    reported on its record.
    """

    on, off = branches["on"], branches["off"]
    read_paths = {site.rpartition(":")[0] for site in reads}
    macro_paths = {path for path in read_paths if _is_macro(path, view)}
    disabled = {
        label: path
        for label, path in {**off.disabled, **on.disabled}.items()
        if label not in on.models
        and label not in off.models
        and label not in on.failed
        and label not in off.failed
    }
    macros_of: dict[str, set[str]] = {}
    for branch in (on, off):
        for uid, entries in (branch.manifest.get("disabled") or {}).items():
            if not uid.startswith("model."):
                continue
            for node in entries if isinstance(entries, list) else [entries]:
                depends = (node.get("depends_on") or {}).get("macros") or ()
                macros_of.setdefault(str(node.get("name")), set()).update(depends)
    manifest_macros = on.manifest.get("macros") or off.manifest.get("macros") or {}
    out = []
    for label, path in sorted(disabled.items()):
        called = {
            str((manifest_macros.get(uid) or {}).get("original_file_path") or "")
            for uid in macro_closure(
                manifest_macros, macros_of.get(label.split(".v")[0], set())
            )
        }
        if path in read_paths or called & macro_paths:
            out.append({"model": label, "path": path})
    return out[:MAX_MODELS]


def _judge_model(
    label: str,
    record: dict[str, Any],
    branches: Mapping[str, Branch],
    governed: set[str],
    exempt: set[str],
    dialect: str | None,
    contract: ToggleContract,
    producers: dict[str, list[str]],
) -> None:
    """Run every toggle check on one model across the compiled branches."""

    on = branches["on"].models.get(label)
    off = branches["off"].models.get(label)
    before = branches["before"].models.get(label)
    on_cols = set(on.columns) if on and on.columns is not None else None
    off_cols = set(off.columns) if off and off.columns is not None else None
    before_cols = set(before.columns) if before and before.columns is not None else None
    record["branches"] = {
        "on": sorted(on_cols) if on_cols is not None else None,
        "off": sorted(off_cols) if off_cols is not None else None,
    }
    if label in branches["off"].disabled and off is None:
        record["branches"]["off"] = "disabled"
    if label in branches["on"].disabled and on is None:
        record["branches"]["on"] = "disabled"
    if label.lower() in exempt:
        record["exempt"] = True

    # One model that does not compile is that model's finding alone (#1156).
    failed = {
        side: branches[side].failed[label]
        for side in ("on", "off")
        if label in branches[side].failed
    }
    if len(failed) == 2:
        _uncheck(record, "compile_failed", f"dbt could not compile it: {failed['on']}")
    else:
        for side, reason in failed.items():
            value = contract.on_value if side == "on" else contract.off_value
            _contradict(
                record,
                "branch_compile_failed",
                f"'{label}' does not compile with the flag {side} ({value!r}): "
                f"{reason}",
            )
    for side in failed:
        record["branches"][side] = "compile_failed"

    for side, model in (("on", on), ("off", off)):
        if model is not None and model.columns is None:
            _uncheck(record, "unreadable_select", f"{side}: {model.reason}")
            record["branches"][side] = None

    if on_cols is not None:
        produced = sorted(on_cols & governed)
        if produced:
            record["produces"] = produced
            producers[label] = produced
    # Off: the governed columns are gone, unless the model is exempt.
    if off_cols is not None and label.lower() not in exempt:
        present = sorted(off_cols & governed)
        if present:
            _contradict(
                record,
                "governed_column_present_when_off",
                f"with the flag off ({contract.off_value!r}), '{label}' still "
                f"outputs {', '.join(present)}",
            )
        names = identifiers(off.sql, dialect) if off is not None else None
        read = sorted((names or set()) & governed - set(present))
        if read:
            _contradict(
                record,
                "governed_column_read_when_off",
                f"with the flag off, '{label}' still reads {', '.join(read)}, so a "
                "value it outputs under another name still depends on the feature",
            )
    # On: today's governed and ungoverned columns are all still there.
    if before_cols is not None and on_cols is not None:
        lost = sorted((before_cols & governed) - on_cols)
        if lost:
            _contradict(
                record,
                "governed_column_lost_when_on",
                f"with the flag on ({contract.on_value!r}), '{label}' no longer "
                f"outputs {', '.join(lost)}, which it outputs today",
            )
    if before_cols is not None:
        for side, cols in (("on", on_cols), ("off", off_cols)):
            if cols is None:
                continue
            gone = sorted((before_cols - governed) - cols)
            if gone:
                _contradict(
                    record,
                    "ungoverned_column_lost",
                    f"with the flag {side}, '{label}' no longer outputs "
                    f"{', '.join(gone)}, which the toggle does not govern",
                )
    if on_cols is not None and off_cols is not None:
        only_on = sorted((on_cols - governed) - off_cols)
        only_off = sorted((off_cols - governed) - on_cols)
        if only_on or only_off:
            _contradict(
                record,
                "ungoverned_columns_differ",
                f"'{label}' outputs different ungoverned columns on and off"
                + (f"; only on: {', '.join(only_on)}" if only_on else "")
                + (f"; only off: {', '.join(only_off)}" if only_off else "")
                + ". Add them to `columns` if the feature governs them",
            )


# --- what else reads a governed column (#1169) ---------------------------------

#: The test arguments that name a column of the model the test is attached to.
#: ``field`` is apart: a ``relationships`` test names a column of its ``to``.
_TEST_COLUMN_KEYS = ("column_name", "combination_of_columns", "compare_columns")


def _names(value: Any) -> set[str]:
    """Lowercased column names from one test argument, quoting removed."""

    values = value if isinstance(value, list) else [value]
    return {
        _QUOTES.sub("", item).strip().lower()
        for item in values
        if isinstance(item, str)
    }


def _test_columns(node: Mapping[str, Any], uid: str) -> set[str]:
    """The columns of model ``uid`` a compiled test node reads."""

    from ..references import _relation_from

    kwargs = (node.get("test_metadata") or {}).get("kwargs") or {}
    names: set[str] = set()
    if node.get("attached_node") == uid:
        names |= _names(node.get("column_name"))
        for key in _TEST_COLUMN_KEYS:
            names |= _names(kwargs.get(key))
    parts = uid.split(".")
    if (
        len(parts) > 2
        and uid in ((node.get("depends_on") or {}).get("nodes") or [])
        and _relation_from(kwargs.get("to")) == parts[2]
    ):
        names |= _names(kwargs.get("field"))
    return names


def _argument_columns(node: Mapping[str, Any]) -> set[str]:
    """The columns a test names in its arguments, beyond the column it sits on.

    The index records these as ``yaml_test_column``; a column test's own
    ``column_name`` is the column entry it is written under instead.
    """

    kwargs = (node.get("test_metadata") or {}).get("kwargs") or {}
    names: set[str] = set()
    for key in ("combination_of_columns", "compare_columns", "field"):
        names |= _names(kwargs.get(key))
    if not node.get("column_name"):
        names |= _names(kwargs.get("column_name"))
    return names


def _project_tests(
    manifest: Mapping[str, Any], project: str
) -> tuple[list[tuple[str, Mapping[str, Any]]], list[tuple[str, Mapping[str, Any]]]]:
    """This project's test nodes in one compiled branch: enabled, then disabled."""

    enabled = [
        (uid, node)
        for uid, node in (manifest.get("nodes") or {}).items()
        if uid.startswith(f"test.{project}.")
    ]
    disabled = []
    for uid, entries in (manifest.get("disabled") or {}).items():
        if uid.startswith(f"test.{project}."):
            for node in entries if isinstance(entries, list) else [entries]:
                disabled.append((uid, node))
    return enabled, disabled


def _indent(text: str) -> int:
    """The column a YAML line's content starts at."""

    return len(text) - len(text.lstrip(" -"))


def _test_line(content: str, column_line: int, test: str) -> int | None:
    """The line under a column's YAML entry that invokes ``test``, if any.

    The index places the column's entry, and its tests sit inside it: the first
    line naming the test before the entry ends is the one dbt reads.
    """

    lines = content.splitlines()
    if not 0 < column_line <= len(lines):
        return None
    depth = _indent(lines[column_line - 1])
    pattern = re.compile(rf"(?<!\w){re.escape(test)}(?!\w)")
    for number in range(column_line - 1, len(lines)):
        text = lines[number]
        stripped = text.lstrip()
        if number >= column_line and stripped and not stripped.startswith("#"):
            # The next column, or the end of the entry this column sits in.
            indent = _indent(text)
            if indent < depth or (indent == depth and stripped.startswith("- ")):
                return None
        if pattern.search(text.split("#", 1)[0]):
            return number + 1
    return None


def governed_readers(
    index: Any,
    view: DbtProjectView,
    off: Branch,
    producers: Mapping[str, list[str]],
    *,
    name: str,
    variable: str,
    on_value: Any,
    off_value: Any,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """The tests and semantic definitions over a producer's governed column.

    ``producers`` are the models that output a governed column with the flag
    on, exempt ones left out (they keep the column off). The sites come from
    :meth:`ReferenceIndex.references_to` on ``model.column``, as retirement's
    ``removed_column_still_read`` does, and each carries its file and line:

    - ``governed_column_tested_when_off``: a test that dbt still runs with the
      flag off, read from the off branch's manifest, so a test disabled at the
      off value (``enabled: "{{ var('flag') }}"``) is not reported;
    - ``governed_column_in_semantic_layer``: a dimension, measure or entity
      whose column is gone with the flag off, with the metrics drawing on it.

    A reference the index cannot resolve, and a test it names that the manifest
    holds no node for, are returned as limits rather than dropped.
    """

    sites: dict[str, list[dict[str, Any]]] = {}
    limits: list[str] = []
    if not producers:
        return sites, limits
    hits_of: dict[tuple[str, str], list[Any]] = {}
    for label, columns in sorted(producers.items()):
        base = label.split(".")[0]
        for column in sorted(set(columns)):
            hits, query_limits = index.references_to(f"{base}.{column}", "column")
            limits.extend(query_limits)
            hits_of[(label, column)] = [hit for hit in hits if not hit.package]

    if off.failure is None:
        advice = _enabled_expression(variable, on_value, off_value)
        _tested_when_off(view, off, hits_of, sites, limits, name, advice, off_value)
    elif any(
        hit.form in ("yaml_column", "yaml_test_column")
        for hits in hits_of.values()
        for hit in hits
    ):
        limits.append(
            "the tests over governed columns were not judged: the project does "
            "not compile with the flag off"
        )
    _in_semantic_layer(index, view, off, producers, hits_of, sites, limits, name)
    return sites, limits


def _tested_when_off(
    view: DbtProjectView,
    off: Branch,
    hits_of: Mapping[tuple[str, str], list[Any]],
    sites: dict[str, list[dict[str, Any]]],
    limits: list[str],
    project: str,
    advice: str,
    off_value: Any,
) -> None:
    """Each test still enabled with the flag off over a producer's governed column."""

    enabled, disabled = _project_tests(off.manifest, project)
    seen: set[str] = set()
    taken: set[tuple[str, int]] = set()
    for (label, column), hits in sorted(hits_of.items()):
        uid = f"model.{project}.{label}"
        base = label.split(".")[0]
        reading = [
            (test_id, node)
            for test_id, node in enabled
            if column in _test_columns(node, uid)
        ]
        compiled = [
            node
            for _test_id, node in (*enabled, *disabled)
            if column in _test_columns(node, uid)
        ]
        for test_id, node in reading:
            if test_id in seen:
                continue
            path = str(node.get("original_file_path") or "")
            test = str(
                (node.get("test_metadata") or {}).get("name") or node.get("name")
            )
            line = _locate_test(view, hits, path, base, column, node, test, taken)
            seen.add(test_id)
            where = f"{path}:{line}" if line else path
            sites.setdefault(label, []).append(
                {
                    "check": "governed_column_tested_when_off",
                    "detail": (
                        f"the {test} test at {where} reads '{label}.{column}' and "
                        f"still runs with the flag off ({off_value!r}), where the "
                        "column is gone, so the off build fails it. Disable it at "
                        f'the off value (`enabled: "{advice}"` in its config) or '
                        "remove it"
                    ),
                    "path": path,
                    "line": line,
                    "column": column,
                    "test": test,
                }
            )
        # A test the index places on this column that dbt compiled into no node,
        # enabled or not, is one this check could not judge. Each site is held
        # to the nodes of its own file and kind, so one compiled test on the
        # column does not stand for another written elsewhere.
        for hit in hits:
            if hit.owner != base or hit.form not in ("yaml_test_column", "yaml_column"):
                continue
            in_file = [
                node
                for node in compiled
                if node.get("original_file_path") == hit.path
                and (
                    column in _argument_columns(node)
                    if hit.form == "yaml_test_column"
                    else str(node.get("column_name") or "").lower() == column
                )
            ]
            if in_file or (
                hit.form == "yaml_column"
                and not _declares_tests(view, hit.path, base, column)
            ):
                continue
            limits.append(
                f"{hit.path}:{hit.line} names '{label}.{column}' in a test "
                "that matched no compiled test node, so whether it runs with "
                "the flag off is unknown"
            )


def _enabled_expression(variable: str, on_value: Any, off_value: Any) -> str:
    """A test ``enabled`` config that is true at ``on_value`` and false at
    ``off_value``.

    A bare ``var()`` only works for a boolean flag that is on when true: a
    ``disable_x`` flag turns the feature on with ``false``, and a string or
    integer flag renders a value dbt does not read as a boolean. A contract may
    pair values Jinja holds equal across types (``0`` and ``false``), so the
    test then also checks the type, never truthiness alone.
    """

    read = f"var('{variable}')"
    if isinstance(on_value, bool) and isinstance(off_value, bool):
        expression = read if on_value else f"not {read}"
    elif isinstance(on_value, bool):
        expression = f"{read} is sameas {'true' if on_value else 'false'}"
    elif isinstance(off_value, bool) and on_value == off_value:
        expression = f"({read} == {on_value!r} and {read} is not boolean)"
    else:
        expression = f"{read} == {on_value!r}"
    return "{{ " + expression + " }}"


def _declares_tests(view: DbtProjectView, path: str, model: str, column: str) -> bool:
    """Whether the YAML at ``path`` declares a test under ``model.column``."""

    from ..dbt_project import yaml_documents
    from ..references import _tests_of

    for parsed, where in yaml_documents(view):
        if where != path:
            continue
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or entry.get("name") != model:
                continue
            for item in entry.get("columns") or []:
                if (
                    isinstance(item, dict)
                    and str(item.get("name") or "").lower() == column
                    and _tests_of(item)
                ):
                    return True
    return False


def _locate_test(
    view: DbtProjectView,
    hits: list[Any],
    path: str,
    base: str,
    column: str,
    node: Mapping[str, Any],
    test: str,
    taken: set[tuple[str, int]],
) -> int | None:
    """The line of one compiled test in its YAML file, from the index's sites."""

    in_file = [hit for hit in hits if hit.path == path]
    source = view.files.get(path)
    if str(node.get("column_name") or "").lower() == column and source is not None:
        for hit in in_file:
            if hit.form == "yaml_column" and hit.owner == base:
                line = _test_line(source.content, hit.line, test)
                if line:
                    return line
    # A test naming the column in its arguments: the argument's own line, on
    # the model the test is declared under (a relationships test sits on the
    # model that points at this one).
    attached = str(node.get("attached_node") or "").split(".")
    owner = attached[2] if len(attached) > 2 else None
    for hit in in_file:
        if (
            hit.form == "yaml_test_column"
            and hit.owner == owner
            and (path, hit.line) not in taken
        ):
            taken.add((path, hit.line))
            return int(hit.line)
    for hit in in_file:
        if hit.form == "yaml_column" and hit.owner == base:
            return int(hit.line)
    return None


def _in_semantic_layer(
    index: Any,
    view: DbtProjectView,
    off: Branch,
    producers: Mapping[str, list[str]],
    hits_of: Mapping[tuple[str, str], list[Any]],
    sites: dict[str, list[dict[str, Any]]],
    limits: list[str],
    project: str,
) -> None:
    """Each semantic definition over a producer's governed column, and the
    metrics drawing on it; an unresolved semantic expression is a limit."""

    from ..dbt_project import metric_inputs, physical_column, semantic_yaml_entries
    from ..references import _relation_from

    entries = semantic_yaml_entries(view)
    feeds: dict[str, set[str]] = {}
    for kind, entry, _path in entries:
        if kind == "metric":
            measures, metrics = metric_inputs(entry)
            for name in (*measures, *metrics):
                feeds.setdefault(name, set()).add(str(entry["name"]))

    def metrics_on(measure: str) -> list[str]:
        """Every metric drawing on ``measure``, through other metrics too."""

        found: set[str] = set()
        pending = [measure]
        while pending:
            for metric in feeds.get(pending.pop(), set()):
                if metric not in found:
                    found.add(metric)
                    pending.append(metric)
        return sorted(found)

    disabled_off = (
        set(off.manifest.get("disabled") or {})
        - set(off.manifest.get("semantic_models") or {})
        if off.failure is None
        else set()
    )
    roles = (
        ("dimensions", "dimension"),
        ("measures", "measure"),
        ("entities", "entity"),
    )
    for label, columns in sorted(producers.items()):
        base = label.split(".")[0]
        governed = {c.lower() for c in columns}
        paths = {
            hit.path
            for column in governed
            for hit in hits_of.get((label, column), [])
            if hit.form == "semantic_expr" and hit.owner == base
        }
        for kind, entry, path in entries:
            if kind != "semantic_model" or path not in paths:
                continue
            if _relation_from(entry.get("model")) != base:
                continue
            if f"semantic_model.{project}.{entry['name']}" in disabled_off:
                continue
            for role, item_kind in roles:
                for item in entry.get(role) or []:
                    column = physical_column(item)
                    if not column or column.lower() not in governed:
                        continue
                    item_name = str(item.get("name"))
                    line = next(
                        (
                            int(hit.line)
                            for hit in index.references_to(item_name, item_kind)[0]
                            if hit.path == path
                            and hit.form
                            in ("semantic_definition", "semantic_entity_ref")
                        ),
                        None,
                    )
                    metrics = metrics_on(item_name) if item_kind == "measure" else []
                    where = f"{path}:{line}" if line else path
                    sites.setdefault(label, []).append(
                        {
                            "check": "governed_column_in_semantic_layer",
                            "detail": (
                                f"the {item_kind} '{item_name}' of semantic model "
                                f"'{entry['name']}' at {where} is defined over "
                                f"'{label}.{column.lower()}', which is gone with the "
                                "flag off, so it stops resolving"
                                + (
                                    f", and with it the metric(s) {', '.join(metrics)}"
                                    if metrics
                                    else ""
                                )
                            ),
                            "path": path,
                            "line": line,
                            "column": column.lower(),
                            "kind": item_kind,
                            "definition": item_name,
                            "semantic_model": str(entry["name"]),
                            "metrics": metrics,
                        }
                    )
        for hit in index.indeterminate_for("column"):
            if hit.form == "semantic_expr" and hit.owner == base and not hit.package:
                limits.append(
                    f"{hit.path}:{hit.line}: the {hit.note or 'expression'} over "
                    f"'{label}' could not be resolved to a column, so whether it "
                    "reads a governed column is unknown"
                )


def _report(
    records: list[dict[str, Any]],
    limits: list[str],
    reads: list[str],
    total: int,
    *,
    clean: int = 0,
) -> dict[str, Any]:
    """The ``data.toggle`` report, its verdict computed from records and limits."""

    limits = list(dict.fromkeys(limits))
    verdict = (
        "contradicted"
        if any(r["status"] == "contradicted" for r in records)
        else "unchecked"
        if limits or any(r["status"] == "unchecked" for r in records)
        else "satisfied"
    )
    listed = records[:MAX_MODELS]
    return {
        "status": "planned",
        "verdict": verdict,
        "completeness": "complete" if not limits else "incomplete",
        "limits": limits,
        "reads": reads,
        "models_compiled": total,
        "models_clean": clean,
        "models": listed,
        "models_elided": max(0, len(records) - len(listed)),
    }


def analysis_failed(reason: str) -> dict[str, Any]:
    """The report for an analysis that raised: unchecked, never clean.

    The plan is stored by then, so a failure here is reported as a limit on an
    empty reading rather than as an error that would hide the stored plan.
    """

    report = _report([], [f"the toggle could not be analysed ({reason})"], [], 0)
    # Stored with the contract, so a later build reports the producers it never
    # learned as an unchecked obligation instead of judging none and saying
    # nothing (an empty producer map alone reads as "nothing produces it").
    report["analysis_failed"] = reason
    report["producers"] = {}
    report["exempt_models"] = []
    report["package_scope"] = {}
    return report


def plan_gaps(analysis: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the plan's analysis could not judge, kept with the stored contract.

    A model whose SELECT could not be read is never a known producer, so a later
    build would hold nothing to it and say nothing; the same goes for a limit on
    the whole reading (both branches failing to compile, a dynamic ``var()``
    read). Stored, they come back as unchecked records on every build.
    """

    models = {
        str(record["model"]): str(record.get("reason_code") or "unchecked")
        for record in analysis.get("models") or []
        if record.get("status") == "unchecked"
    }
    limits = [str(limit) for limit in analysis.get("limits") or []]
    if not models and not limits:
        return None
    return {"models": models, "limits": limits}


def gap_records(stored: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The unchecked records a build owes for what the plan could not judge."""

    variable = str(stored.get("variable"))
    records: list[dict[str, Any]] = []
    if stored.get("analysis_failed"):
        records.append(
            {
                "model": f"var:{variable}",
                "status": "unchecked",
                "reason_code": "analysis_failed",
                "reason": (
                    "the plan's toggle analysis failed "
                    f"({stored['analysis_failed']}), so the producers of the "
                    "governed columns are unknown; plan again to check them"
                ),
            }
        )
    gaps = stored.get("plan_gaps") or {}
    for model, code in sorted((gaps.get("models") or {}).items())[:MAX_MODELS]:
        records.append(
            {
                "model": model,
                "status": "unchecked",
                "reason_code": "unchecked_at_plan",
                "reason": (
                    f"the plan could not judge this model ({code}), so whether it "
                    "produces a governed column is unknown"
                ),
            }
        )
    if gaps.get("limits") and not stored.get("analysis_failed"):
        records.append(
            {
                "model": f"var:{variable}",
                "status": "unchecked",
                "reason_code": "plan_incomplete",
                "reason": "; ".join(gaps["limits"][:3]),
            }
        )
    return records


def plan_warnings(variable: str, analysis: Mapping[str, Any]) -> list[str]:
    """One warning per contradiction, and one for everything not judged."""

    out = []
    for record in analysis.get("models") or []:
        for check in record.get("checks") or []:
            out.append(f"toggle '{variable}': {check['detail']}")
        if record.get("reason_code"):
            out.append(
                f"toggle '{variable}': '{record['model']}' is not checked "
                f"({record['reason_code']}), which is not clean"
            )
    for limit in analysis.get("limits") or []:
        out.append(f"toggle '{variable}': the project was not read in full: {limit}")
    scope = analysis.get("package_scope") or {}
    if scope.get("readers") or scope.get("dynamic_readers"):
        out.append(
            f"toggle '{variable}': package models read it too; they are out of "
            "scope and not judged (`data.toggle.package_scope` names them)"
        )
    return out


# --- after a build -----------------------------------------------------------


def verify_outputs(
    stored: Mapping[str, Any],
    built: Mapping[str, str],
    scope: set[str],
    relations: Mapping[str, str],
    live_identifiers: list[str],
    adapter_once: Callable[[], Any],
    value: tuple[bool, Any],
    project_name: str | None,
) -> tuple[list[Any], list[dict[str, Any]], str | None]:
    """The built relations' columns against the toggle, at the value built.

    From metadata, as the column contract reads it, so an empty table is judged
    like a full one. Returns the findings, the records and the branch built
    (``on``, ``off`` or ``None`` when the value is not one of the contract's).
    """

    from ..cache import match_identifier
    from ..envelope import redact
    from ..guards.cost_guard import CostGuardError
    from ..maintain.drift import DriftFinding

    contract = contract_of(stored)
    variable = str(stored.get("variable"))
    governed = {c.lower() for c in contract.columns}
    exempt = {m.lower() for m in contract.exempt_models}
    producers: dict[str, list[str]] = {
        str(k): [str(c).lower() for c in v]
        for k, v in (stored.get("producers") or {}).items()
    }
    known, current = value
    branch = None
    if known and matches(current, contract.on_value):
        branch = "on"
    elif known and matches(current, contract.off_value):
        branch = "off"

    own = {
        uid: label
        for uid, label in built.items()
        if project_name is None or uid.split(".")[1:2] == [project_name]
    }
    labels = {own[uid] for uid in scope if uid in own}
    findings: list[Any] = []
    records: list[dict[str, Any]] = []
    # What the plan could not judge is owed whatever this build judges below:
    # a model it never read is no known producer, so nothing else names it.
    records.extend(gap_records(stored))
    if branch is None:
        why = (
            f"this build gave '{variable}' the value {current!r}, which is neither "
            "the contract's on_value nor its off_value"
            if known
            else f"the value this build gave '{variable}' could not be read from "
            "dbt_project.yml or a default in the SQL"
        )
        for model in sorted(set(producers) | labels)[:MAX_MODELS]:
            records.append(
                {
                    "model": model,
                    "status": "unchecked",
                    "reason_code": "value_unknown",
                    "reason": why,
                }
            )
        return findings, records, None

    other = "off" if branch == "on" else "on"
    candidates = sorted(set(producers) | (labels if branch == "off" else set()))
    for model in candidates:
        record: dict[str, Any] = {
            "model": model,
            "branch": branch,
            "status": "unchecked",
        }
        if model not in labels:
            if model in producers:
                record["reason_code"] = "not_built"
                records.append(record)
            continue
        relation = relations.get(model)
        if relation is None:
            record["reason_code"] = "no_relation"
            records.append(record)
            continue
        found = match_identifier(relation, live_identifiers)
        if len(found) != 1:
            record["reason_code"] = "relation_not_found"
            records.append(record)
            continue
        try:
            _meta, columns = adapter_once().table_metadata(found[0])
        except CostGuardError:
            raise
        except Exception as exc:
            record["reason_code"] = "metadata_unreadable"
            record["reason"] = redact(type(exc).__name__)
            records.append(record)
            continue
        actual = {column.name.lower() for column in columns}
        record["status"] = "satisfied"
        if branch == "off":
            present = sorted(actual & governed)
            if model.lower() in exempt:
                record["exempt"] = True
                present = []
            if present:
                record["status"] = "contradicted"
                record["finding"] = "toggle_column_present_when_off"
                record["columns"] = present
                findings.append(
                    DriftFinding(
                        axis="build",
                        code="toggle_column_present_when_off",
                        identifier=model,
                        severity="high",
                        detail=(
                            f"'{model}' was built with {variable} off and still has "
                            f"{', '.join(present)}, which the toggle hides"
                        ),
                        data={"model": model, "columns": present},
                    )
                )
            elif model not in producers:
                # A clean model that produces nothing governed: counted, not listed.
                continue
        else:
            missing = sorted(set(producers.get(model, [])) - actual)
            if missing:
                record["status"] = "contradicted"
                record["finding"] = "toggle_column_missing_when_on"
                record["columns"] = missing
                findings.append(
                    DriftFinding(
                        axis="build",
                        code="toggle_column_missing_when_on",
                        identifier=model,
                        severity="high",
                        detail=(
                            f"'{model}' was built with {variable} on and lacks "
                            f"{', '.join(missing)}, which the plan's on branch "
                            "outputs"
                        ),
                        data={"model": model, "columns": missing},
                    )
                )
        records.append(record)
    for model in sorted(producers):
        records.append(
            {
                "model": model,
                "branch": other,
                "status": "unchecked",
                "reason_code": "branch_not_built",
                "reason": (
                    f"this build ran with the flag {branch}; the {other} branch was "
                    "checked from compiled SQL when planning (`data.toggle`), not "
                    "built"
                ),
            }
        )
    return findings, records[: 2 * MAX_MODELS], branch


def absorb_column_contract(
    findings: list[Any], stored: Mapping[str, Any], branch: str | None
) -> tuple[list[Any], list[str]]:
    """Drop the governed columns from ``column_missing`` on an off build.

    A model's schema.yml documents its columns with the feature on, and dbt
    has no way to make a column's entry conditional, so an off build missing a
    governed column is the toggle working, not a broken contract. Every other
    column a finding names stays.
    """

    if branch != "off":
        return findings, []
    governed = {c.lower() for c in contract_of(stored).columns}
    kept: list[Any] = []
    absorbed: list[str] = []
    for finding in findings:
        if getattr(finding, "code", None) != "column_missing":
            kept.append(finding)
            continue
        data = dict(getattr(finding, "data", None) or {})
        columns = [str(c) for c in data.get("columns") or []]
        left = [c for c in columns if c.lower() not in governed]
        if len(left) == len(columns):
            kept.append(finding)
            continue
        absorbed.append(str(data.get("model") or finding.identifier))
        if left:
            data["columns"] = left
            kept.append(
                finding.model_copy(
                    update={
                        "data": data,
                        "detail": (
                            f"'{finding.identifier}' declares column(s) "
                            f"{', '.join(left)} in schema.yml that the built "
                            "relation does not have"
                        ),
                    }
                )
            )
    notes = (
        [
            "schema.yml documents governed column(s) the toggle hides on this off "
            "build, in " + ", ".join(sorted(set(absorbed))) + "; they are judged by "
            "`data.verification.toggle`, not reported as missing"
        ]
        if absorbed
        else []
    )
    return kept, notes
