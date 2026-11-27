"""``dex maintain incidents``: the incidents the sources hold, read and priced (#1472).

Two sources feed the one fold (:func:`~.incidents.fold_incidents`), in this
precedence, which is the fold's tie-break between sources failing in the run that
opens an incident:

1. **Elementary's tables** (:mod:`.elementary_incidents`, #1471), where the
   package is installed, enabled and uploading. The read is bounded by a run
   count: the newest ``invocations`` rows of ``dbt_invocations`` (20 by
   default, :data:`~.elementary_incidents.MAX_INVOCATIONS` at most), never a
   number of days, so its price does not depend on the schedule. Every statement
   goes through the runner ``explore query`` uses, under the firewall, the source
   allowlist, the profile on demand and the query ledger, and all five are priced
   before any runs: on a billed connector the first call answers
   ``needs_confirmation`` having sent no statement (it reads only the
   catalog metadata every ``explore`` read takes first).
2. **dbt's run results** (:mod:`.run_results_incidents`, #1468): files only, one
   invocation. Folded beside Elementary, its run takes the place Elementary gave
   the same invocation, or the place after the window where it started later; a run
   that is neither is reported and left out of the fold, since it has no place in
   the order.

Each source works alone and each is reported under ``sources`` as ``read``,
``missing`` or ``refused``, with a reason code for the last two. A schema
outside the committed source allowlist is ``refused`` before anything opens a
connection, with the ``dex config set`` that would add it: the allowlist is a cost
boundary, so this command never reads past it. An empty ``incidents`` list
means no failure only where a source reads ``read``.

Read-only: nothing here writes to the warehouse or the project, and no incident
state is kept between calls.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import command_args
from ..errors import RequestError
from ..transform.elementary_schema import Located, allowlist_command, entries
from ..values_free import require_allowed
from .commit_history import CommitHistory
from .coverage_runs import current_tests, parse_time
from .incidents import IncidentFold, SourceHistory, fold_incidents
from .observations import Observation, Subject
from .results import IncidentsResult

if TYPE_CHECKING:
    from ..engine import DexEngine

COMMAND = "maintain incidents"

#: The invocations one call reads by default, and the most ``--invocations``
#: takes: the reader's own bound (:data:`~.elementary_incidents.MAX_INVOCATIONS`).
DEFAULT_INVOCATIONS = 20
MAX_INVOCATIONS = 2000

ELEMENTARY = "elementary"
DBT_RUN_RESULTS = "dbt_run_results"

#: Every source, in the precedence the fold reads them in.
SOURCES: tuple[str, ...] = (ELEMENTARY, DBT_RUN_RESULTS)

SourceStatus = Literal["read", "missing", "refused"]

#: Why a source was not read. Elementary: ``no_project`` and ``no_manifest``
#: (nothing says whether the package is installed), ``not_installed``,
#: ``not_parsed`` (installed, but the manifest predates it), ``disabled`` (its
#: models are disabled for the target), ``not_uploading`` (its hooks or the
#: two uploads it needs are off), ``tables_not_declared``, ``tables_absent``
#: (declared, not built), ``outside_allowlist`` (with the ``dex config set``
#: that adds it), ``prerequisite`` (the ``sql`` extra is missing), a layout
#: code (:mod:`.elementary_layouts`), a reader code (:mod:`.elementary_refusals`),
#: ``connection_unavailable`` and ``catalog_unavailable`` (the connection or its
#: catalog could not be read), ``unpriced`` (part of the read could not be
#: priced, so it was not asked for), ``warehouse_refused`` (the warehouse
#: refused a statement of the read, its words masked) or ``firewall_refused``.
#: A cost refusal (a confirmation, a budget, a ceiling) is the command's and is
#: raised, never a status. dbt's run results: ``no_project``, ``no_manifest``
#: and the adapter's own codes (:mod:`.run_results_incidents`).

#: The two tables the reader reads, as the package's models name them.
_TABLES = ("dbt_invocations", "elementary_test_results")

_SPLIT = re.compile(r"[,\s]+")


@dataclass
class _Source:
    """One source's entry under ``sources``, and what it hands the fold."""

    status: SourceStatus
    reason_code: str | None = None
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    observations: tuple[Observation, ...] = ()
    history: SourceHistory | None = None

    def report(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"status": self.status}
        if self.reason_code is not None:
            entry["reason_code"] = self.reason_code
        if self.detail is not None:
            entry["detail"] = self.detail
        entry.update(self.extra)
        return entry


def check_invocations(invocations: int | None) -> int:
    """The run count to read: the default where none is given, else ``invocations``
    held to 1 to :data:`MAX_INVOCATIONS`."""

    if invocations is None:
        return DEFAULT_INVOCATIONS
    if type(invocations) is not int or not 1 <= invocations <= MAX_INVOCATIONS:
        raise ValueError(
            f"--invocations must be a whole number from 1 to {MAX_INVOCATIONS}, "
            "the most one read takes; read a narrower selection rather than a "
            "wider window"
        )
    return invocations


def incidents(
    engine: DexEngine,
    objects: list[str] | None = None,
    *,
    invocations: int | None = None,
) -> IncidentsResult:
    """The incidents the sources hold for the selected models' tests.

    ``objects`` names models (repeated, comma- or space-separated); a test is
    selected when it depends on one. None selects every test. ``invocations``
    is the run count Elementary's tables are read over, newest first.
    """

    require_allowed(engine.result_mode, COMMAND)
    return read(engine, objects, invocations=invocations).result


@dataclass(frozen=True)
class IncidentRead:
    """What one read of the sources returns: the command's result, and for a
    caller that goes on from it (``maintain diagnose``, #1475) the fold itself,
    the manifest the tests were read from and the project it sits in."""

    result: IncidentsResult
    fold: IncidentFold | None = None
    manifest: dict[str, Any] | None = None
    project_dir: Path | None = None


def read(
    engine: DexEngine,
    objects: list[str] | None = None,
    *,
    invocations: int | None = None,
    history: CommitHistory | None = None,
    command: str = COMMAND,
) -> IncidentRead:
    """The read :func:`incidents` reports, with the fold kept.

    ``history`` is handed to the fold, so each incident's ``onset`` carries
    the commits it spans (#1467). ``command`` names the caller on the cost
    gate's ask, so a confirmation names the command that asked.
    """

    window_size = check_invocations(invocations)
    warnings: list[str] = []
    notes: list[str] = []
    steps: list[dict[str, str]] = []

    project_dir, manifest, why = _project(engine)
    if project_dir is None or manifest is None:
        code = "no_project" if project_dir is None else "no_manifest"
        if project_dir is not None:
            steps.append(_BUILD_STEP)
        return IncidentRead(
            IncidentsResult(
                sources={
                    name: _Source("missing", code, why).report() for name in SOURCES
                },
                window=_window(window_size, None, None),
                price=_price_report(None),
                next_steps=steps,
                warnings=[f"{command} read no source: {why}"],
            ),
            project_dir=project_dir,
        )

    claims, over_long = _bounded_claims(manifest)
    if over_long:
        warnings.append(
            f"{over_long} test claim(s) are longer than {_MAX_KEY_CHARS} "
            "characters, so those tests are keyed by their unique_id"
        )
    selected, unmatched = _selected_tests(manifest, objects)
    if unmatched:
        warnings.append(
            f"no model named {', '.join(unmatched)} is in the manifest, so it "
            "selects no test"
        )

    elementary = _elementary(
        engine, project_dir, manifest, claims, window_size, steps, command
    )
    priced: dict[str, Any] | None = elementary.extra.pop("_price", None)
    opened = elementary.extra.pop("_adapter", None)
    warnings.extend(elementary.extra.pop("_warnings", []))
    dbt = _dbt(project_dir, manifest, claims, elementary.history, steps, notes)
    warnings.extend(dbt.extra.pop("_warnings", []))
    held_at: int | None = dbt.extra.pop("_held_note_at", None)

    read = [source for source in (elementary, dbt) if source.history is not None]
    observations = [
        observation
        for source in read
        for observation in source.observations
        if selected is None or observation.test in selected
    ]
    current = [
        Subject.of(uid, claims.get(uid))
        for uid in sorted(current_tests(manifest))
        if selected is None or uid in selected
    ]
    histories = [source.history for source in read if source.history is not None]
    fold = fold_incidents(
        observations,
        sources=histories,
        current_subjects=current,
        now=datetime.now(UTC),
        history=history,
    )
    if held_at is not None:
        undated = sum(
            1
            for incident in fold.incidents
            if incident.opening_source == "dbt_run_results"
            and incident.first_observed == "unknown"
        )
        if undated:
            # Where the file's own note would have been, so the notes keep the
            # order every other placement gives them.
            notes.insert(held_at, _only_dbt_reports(undated))
    if dbt.history is not None and elementary.status != "read":
        notes.append(
            "dbt's run_results.json holds one invocation, so a failure it reports "
            "has no known start, and one fixed since is no longer in it: the next "
            "build overwrites the file, so this source reports an open incident "
            "and never a close"
        )

    result = IncidentsResult(
        incidents=[incident.model_dump(mode="json") for incident in fold.incidents],
        schedules={
            name: schedule.model_dump(mode="json")
            for name, schedule in fold.schedules.items()
        },
        window=_window(window_size, elementary.history, dbt),
        price=_price_report(priced),
        sources={ELEMENTARY: elementary.report(), DBT_RUN_RESULTS: dbt.report()},
        scope=None
        if selected is None
        else {"objects": _names(objects), "tests": len(selected)},
        next_steps=steps,
        notes=notes,
        warnings=warnings,
    )
    # Stamped wherever this call acquired the adapter, so a read refused after
    # part of it ran still reports what that part billed. Only then: an engine
    # held across commands keeps the last one's settled gate, which a call that
    # read files alone must not report as its own.
    if opened is not None:
        command_args.stamp_spend(result, opened)
        if priced is not None and result.price is not None:
            result.price["settled"] = result.spend
    return IncidentRead(result, fold=fold, manifest=manifest, project_dir=project_dir)


#: The longest key a :class:`~.observations.Subject` holds.
_MAX_KEY_CHARS = 512


def _bounded_claims(manifest: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    """The manifest's claims, less those too long to key a subject, and how many
    were dropped: both readers and the current subjects read this one map, so a
    test is keyed alike everywhere (by its ``unique_id`` where its claim is
    dropped, as dbt's reader already keys it)."""

    from ..transform.claims import manifest_claims

    kept: dict[str, str] = {}
    dropped = 0
    for uid, claim in manifest_claims(manifest).items():
        if len(claim) > _MAX_KEY_CHARS:
            dropped += 1
        else:
            kept[uid] = claim
    return kept, dropped


# --- the project ---------------------------------------------------------------


_BUILD_STEP = {
    "command": "dex transform build --target dev",
    "purpose": (
        "build the project, which writes the manifest this command reads tests "
        "from, and dbt's run results"
    ),
}


def _project(
    engine: DexEngine,
) -> tuple[Path | None, dict[str, Any] | None, str]:
    """The dbt project and its newest manifest on disk, or why either is absent.

    The manifest is the one beside the run results: which tests exist, their
    claims, and where the package builds its tables.
    """

    from ..errors import DexError
    from ..transform.artifact_runtime import readable_artifact_dir

    try:
        project_dir = Path(engine.project_dir())
    except (DexError, ValueError) as exc:
        return None, None, f"the dbt project could not be located ({exc})"
    path = readable_artifact_dir(project_dir) / "manifest.json"
    loaded: Any = None
    if path.is_file():
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return (
            project_dir,
            None,
            "no manifest.json on disk says which tests the project has; a build "
            "or a dbt parse writes one",
        )
    return project_dir, loaded, ""


def _names(objects: Sequence[str] | None) -> list[str]:
    return [part for obj in objects or () for part in _SPLIT.split(obj) if part]


def _selected_tests(
    manifest: Mapping[str, Any], objects: Sequence[str] | None
) -> tuple[set[str] | None, list[str]]:
    """The tests that depend on a named model, and the names no model matched.

    ``None`` with no names: every test is selected. A versioned model is named
    by its base name (every version) or by ``<name>.v<N>``.
    """

    names = _names(objects)
    if not names:
        return None, []
    wanted = {name.casefold() for name in names}
    models: set[str] = set()
    matched: set[str] = set()
    for uid, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, Mapping) or node.get("resource_type") != "model":
            continue
        base = str(node.get("name") or "").casefold()
        spellings = {base}
        if node.get("version") is not None:
            spellings.add(f"{base}.v{node['version']}".casefold())
        hit = spellings & wanted
        if hit:
            models.add(str(uid))
            matched.update(hit)
    tests: set[str] = set()
    for section in ("nodes", "unit_tests"):
        for uid, node in (manifest.get(section) or {}).items():
            if not isinstance(node, Mapping):
                continue
            if section == "nodes" and node.get("resource_type") != "test":
                continue
            reads = set((node.get("depends_on") or {}).get("nodes") or ())
            attached = node.get("attached_node")
            if attached:
                reads.add(str(attached))
            if reads & models:
                tests.add(str(uid))
    unmatched = [name for name in names if name.casefold() not in matched]
    return tests, unmatched


# --- Elementary ----------------------------------------------------------------


def _elementary(
    engine: DexEngine,
    project_dir: Path,
    manifest: Mapping[str, Any],
    claims: Mapping[str, str],
    window_size: int,
    steps: list[dict[str, str]],
    command: str = COMMAND,
) -> _Source:
    from ..transform import elementary as elementary_mod
    from ..transform import elementary_install as install

    found = install.detect(project_dir)
    if not found["declared"] and not found["installed"]:
        steps.append(
            {
                "command": "dex init",
                "purpose": (
                    "offers the Elementary package install as a stored plan; once "
                    "the package uploads its test results, this command reads "
                    "their run history rather than one run"
                ),
            }
        )
        return _Source(
            "missing",
            "not_installed",
            "the project does not install the Elementary package",
        )
    if not found["installed"]:
        steps.append(
            {
                "command": "dex transform deps",
                "purpose": (
                    "install the declared dbt packages: the project declares the "
                    "Elementary package and it is not installed"
                ),
            }
        )
        return _Source(
            "missing",
            "not_installed",
            "the project declares the Elementary package and it is not installed",
        )
    runtime = elementary_mod.read(project_dir, manifest)
    warned = [w for w in (runtime.release_warning(),) if w]
    if not runtime.installed:
        steps.append(_BUILD_STEP)
        return _Source(
            "missing",
            "not_parsed",
            "the Elementary package is installed, and the manifest on disk was "
            "written before it was, so it names none of its tables",
            {"_warnings": warned},
        )
    if not runtime.enabled:
        return _Source(
            "missing",
            "disabled",
            "the Elementary package's models are disabled for the target the "
            "manifest was parsed for, so it writes no table",
            {"_warnings": warned, "release": runtime.release},
        )
    off = [
        upload
        for upload in ("invocations", "test_results")
        if runtime.uploads.get(upload) is False
    ]
    if off:
        return _Source(
            "missing",
            "not_uploading",
            "the Elementary package does not upload "
            + " or ".join(off)
            + f" in a run (its hooks read {runtime.hooks}), so its tables hold no "
            "current history",
            {"_warnings": warned, "release": runtime.release, "hooks": runtime.hooks},
        )
    unknown = [
        upload
        for upload in ("invocations", "test_results")
        if runtime.uploads.get(upload) is None
    ]
    if unknown:
        warned.append(
            "whether the Elementary package uploads "
            + " and ".join(unknown)
            + " in a run is not known, so its tables are read as found and may "
            "stop short of the latest run"
        )
    located = _declared_namespace(manifest)
    if located is None:
        return _Source(
            "missing",
            "tables_not_declared",
            "the manifest declares neither of the package's tables "
            f"({', '.join(_TABLES)}), so nothing says where it writes them",
            {"_warnings": warned, "release": runtime.release},
        )
    refusal = _allowlist_refusal(engine, located, steps)
    if refusal is not None:
        refusal.extra["_warnings"] = warned
        refusal.extra["release"] = runtime.release
        return refusal
    return _read_elementary(
        engine, located, runtime.release, claims, window_size, warned, command
    )


#: Where the manifest says the package builds its tables, spelled as the
#: allowlist entries ``dex init`` proposes (#1473).
_Located = Located


def _declared_namespace(manifest: Mapping[str, Any]) -> _Located | None:
    for node in (manifest.get("nodes") or {}).values():
        if (
            isinstance(node, Mapping)
            and node.get("package_name") == "elementary"
            and node.get("resource_type") == "model"
            and node.get("name") == _TABLES[0]
            and node.get("schema")
        ):
            database = node.get("database")
            return _Located(str(database) if database else None, str(node["schema"]))
    return None


def _entries(connector: str, located: _Located) -> list[str]:
    return entries(connector, located)


def _allowlist_refusal(
    engine: DexEngine, located: _Located, steps: list[dict[str, str]]
) -> _Source | None:
    """``refused`` where the source allowlist does not reach the package's
    schema, decided from the config alone, before any connection opens."""

    from ..adapters.base import scope_within
    from ..connect import _SCOPE_FIELDS, source_scope

    connector = engine.connector or engine.config.connector
    if connector is None or connector not in _SCOPE_FIELDS:
        return None
    target = getattr(engine.config, connector, None)
    # The opener's precedence: on BigQuery `datasets` (`--dataset`) narrows
    # ahead of `scopes` (`--scope`), as `connect.open_adapter` applies them.
    datasets = list(getattr(engine, "datasets", None) or [])
    flag = "--dataset" if connector == "bigquery" and datasets else "--scope"
    narrowed = datasets if flag == "--dataset" else []
    narrowed = narrowed or list(getattr(engine, "scopes", None) or [])
    allowlist = source_scope(connector, target, narrowed or None)
    entries = _entries(connector, located)
    if not allowlist or any(scope_within(entry, allowlist) for entry in entries):
        return None
    field_name = _SCOPE_FIELDS[connector]
    committed = source_scope(connector, target, None)
    reached = not committed or any(scope_within(e, committed) for e in entries)
    if narrowed and reached:
        return _Source(
            "refused",
            "outside_allowlist",
            f"{flag} narrows this command to {', '.join(narrowed)}, which leaves "
            f"out {located.spelled()}, the schema the Elementary package writes",
            {"namespace": located.spelled()},
        )
    command = allowlist_command(connector, committed, entries[0])
    steps.append(
        {
            "command": command,
            "purpose": (
                "add the schema the Elementary package writes to the committed "
                "source allowlist; the allowlist is a cost boundary, so this "
                "command never reads past it"
            ),
        }
    )
    return _Source(
        "refused",
        "outside_allowlist",
        f"{located.spelled()}, the schema the Elementary package writes, is "
        f"outside the source allowlist ({connector}.{field_name}: "
        f"{', '.join(committed)}), so it is not read",
        {"namespace": located.spelled(), "config_set": command},
    )


def _adapter_namespace(adapter: Any, located: _Located) -> str | None:
    """The namespace as the adapter spells relations, where it lists the table."""

    schema = located.schema.casefold()
    database = (located.database or "").casefold()
    for meta in adapter.list_objects():
        parts = str(meta.identifier).split(".")
        if len(parts) < 2 or parts[-1].casefold() != _TABLES[0]:
            continue
        if parts[-2].casefold() != schema:
            continue
        if len(parts) >= 3 and database and parts[-3].casefold() != database:
            continue
        return ".".join(parts[:-1])
    return None


def _read_elementary(
    engine: DexEngine,
    located: _Located,
    release: str | None,
    claims: Mapping[str, str],
    window_size: int,
    warned: list[str],
    command: str = COMMAND,
) -> _Source:
    try:
        from . import elementary_incidents as reader
        from .elementary_layouts import ElementaryLayoutError
        from .elementary_refusals import NO_INVOCATIONS, ElementaryReadError
    except ImportError as exc:
        return _Source(
            "refused",
            "prerequisite",
            f"reading Elementary's tables needs the sql extra ({exc.name} is not "
            "installed)",
            {"_warnings": warned, "release": release},
        )
    from ..envelope import failure_text
    from ..errors import DexError, WarehouseQueryError
    from ..guards.query_firewall import QueryRefusedError

    base: dict[str, Any] = {"_warnings": warned, "release": release}
    # One acquisition for the whole read: the engine rebuilds the cost gate on
    # every call to `_adapter`, so acquiring again would settle the gate the
    # whole-read reservation sits on before any statement ran on it.
    try:
        adapter = engine._adapter(command)
    except DexError as exc:
        _reraise_cost(exc)
        return _Source("refused", "connection_unavailable", str(exc), base)
    base["_adapter"] = adapter
    try:
        namespace = _adapter_namespace(adapter, located)
    except DexError as exc:
        _reraise_cost(exc)
        return _Source("refused", "catalog_unavailable", str(exc), base)
    if namespace is None:
        return _Source(
            "missing",
            "tables_absent",
            f"the connection lists no {_TABLES[0]} table in {located.spelled()}: "
            "the package declares its tables there and has not built them yet",
            {**base, "namespace": located.spelled()},
        )
    base["namespace"] = namespace
    try:
        # Catalog metadata only: the layout check and the relations' spelling.
        plan = reader.plan_read(
            engine, namespace, release, last=window_size, adapter=adapter
        )
    except ElementaryLayoutError as exc:
        return _Source("refused", exc.code, str(exc), base)
    except DexError as exc:
        _reraise_cost(exc)
        return _Source("refused", "catalog_unavailable", str(exc), base)
    try:
        priced = _price(engine, adapter, plan, command)
        # From here a refusal may follow spend, so it keeps what was priced.
        base["_price"] = priced
        read = reader.read_elementary(engine, plan, claims=claims, adapter=adapter)
    except ElementaryReadError as exc:
        # No run in the window is nothing to read rather than a refusal of what
        # was there: the package has not uploaded a run yet.
        status: SourceStatus = "missing" if exc.code == NO_INVOCATIONS else "refused"
        detail = _command_advice(str(exc))
        return _Source(status, exc.code, detail, {**base, "counts": dict(exc.counts)})
    except QueryRefusedError as exc:
        return _Source("refused", "firewall_refused", str(exc), base)
    except _UnpricedError as exc:
        return _Source("refused", "unpriced", str(exc), base)
    except WarehouseQueryError as exc:
        # A server-side failure mid-read (a timeout, a refused statement) is
        # this source's, so dbt's run results are still read and the price is
        # kept. No cost refusal is this class. The words are masked as every
        # folded failure is: a conversion error can quote a row's value.
        return _Source("refused", "warehouse_refused", failure_text(exc), base)
    priced["first_read"] = read.first_read
    priced["profiled"] = list(read.profiled)
    return _Source(
        "read",
        extra={
            **base,
            "_price": priced,
            "first_read": read.first_read,
            "windowed": read.windowed,
            "measure": {
                "invocations": read.measure.invocations,
                "result_rows": read.measure.result_rows,
                "results_without_invocation": read.measure.results_without_invocation,
            },
            "result_rows_read": read.result_rows_read,
            "duplicate_ids": read.duplicate_ids,
            "merged_rows": read.merged_rows,
            "unattributed": read.unattributed,
            "unplaced": read.unplaced,
            "commits_unread": read.commits_unread,
            "severities_unread": read.severities_unread,
        },
        observations=read.observations,
        history=read.history,
    )


#: What each of the read's statements does, in the order it sends them
#: (:meth:`~.elementary_incidents.ReadPlan.statements`), as its priced line.
_STATEMENT_LABELS = (
    "count invocations",
    "count result rows",
    "count result rows with no invocation",
    "read invocations",
    "read result rows",
)


class _UnpricedError(RequestError):
    """Part of the read could not be priced, so it is not asked for at all:
    asking at a lower price than the read spends would let a confirmation
    authorise less than it pays for. Never leaves :func:`_read_elementary`,
    which reports it as the ``unpriced`` refusal of ``sources.elementary``."""


def _reraise_cost(exc: Exception) -> None:
    """Keep the cost guard's own refusals on the command's path: a confirmation
    ask, a budget or a ceiling is the caller's decision, never a source's
    status."""

    from ..guards.cost_guard import CostGuardError
    from ..results import BudgetExhaustedError

    if isinstance(exc, CostGuardError | BudgetExhaustedError):
        raise exc


#: The reader's retry advice names ``since``, which this command does not take:
#: each such refusal is told the command's own way to read fewer runs.
_ADVICE = {
    "so read from after the upgrade with since": (
        "so read fewer runs with --invocations, so the window holds only runs "
        "written since the upgrade"
    ),
    "read a later window with since, nothing was read": (
        "nothing was read; read fewer runs with --invocations"
    ),
}


def _command_advice(message: str) -> str:
    for theirs, ours in _ADVICE.items():
        if theirs in message:
            return message.replace(theirs, ours)
    return message


def _price(
    engine: DexEngine, adapter: Any, plan: Any, command: str = COMMAND
) -> dict[str, Any]:
    """Price every statement the read sends, and any profile the firewall needs
    first, as one handshake on the read's one adapter: on a billed connector an
    unconfirmed call raises the ask here, before any statement runs. A part
    that cannot be priced raises :class:`_UnpricedError`, never a lower ask."""

    from ..explore.commands import _object_gap, _profile_estimate
    from ..storage import readable_cache

    estimate_one = getattr(adapter, "query_estimate", None)
    named = [f"{plan.namespace}.{table}" for table in _TABLES]
    profile_estimate = 0.0
    per_table: dict[str, float] = {}
    try:
        per_statement = {
            label: float(estimate_one(sql)) if estimate_one else 0.0
            for label, sql in zip(_STATEMENT_LABELS, plan.statements(), strict=True)
        }
        to_profile = list(
            _object_gap(adapter, readable_cache(engine.store), named).to_profile
        )
        if to_profile:
            profile_estimate, per_table = _profile_estimate(adapter, to_profile)
    except Exception as exc:
        _reraise_cost(exc)
        # The type alone: an estimator's message can carry a warehouse's words.
        raise _UnpricedError(
            f"the read could not be priced ({type(exc).__name__}), so it was not "
            "asked for and nothing was read"
        ) from exc
    total = profile_estimate + sum(per_statement.values())
    notes = [
        f"{len(per_statement)} statements read Elementary's tables over the newest "
        f"{plan.last} invocations"
    ]
    if to_profile:
        notes.append(
            f"{len(to_profile)} of its tables have no usable profile, and the "
            "firewall reads PII flags from the cache: this estimate covers "
            "profiling them first"
        )
    command_args.billed_handshake(
        command,
        adapter,
        total,
        per_table={**per_table, **per_statement},
        notes=notes,
    )
    return {
        "estimate": total,
        "per_statement": per_statement,
        "profile_estimate": profile_estimate if to_profile else None,
    }


# --- dbt's run results ---------------------------------------------------------


def _dbt(
    project_dir: Path,
    manifest: Mapping[str, Any],
    claims: Mapping[str, str],
    elementary: SourceHistory | None,
    steps: list[dict[str, str]],
    notes: list[str],
) -> _Source:
    from .run_results_incidents import read_run_results

    found = read_run_results(project_dir, manifest, claims=claims)
    extra: dict[str, Any] = {"_warnings": list(found.warnings)}
    if found.run is not None:
        extra["run"] = found.run
    if found.status != "read" or found.history is None:
        if found.status == "missing":
            steps.append(_BUILD_STEP)
        detail = found.notes[0] if found.notes else None
        return _Source(found.status, found.reason_code, detail, extra)
    started = parse_time(str((found.run or {}).get("invocation_started_at") or ""))
    sequence, placed_as = _place(found.history, started, elementary)
    extra["placed"] = placed_as
    if sequence is None:
        extra["folded"] = False
        notes.extend(found.notes)
        return _Source(
            "read",
            detail=(
                "the run is not in Elementary's window and did not start after "
                "it, so it has no place in the order and is left out of the fold"
            ),
            extra=extra,
        )
    # Re-sequenced in memory, from the one read: reading the file again could
    # find another invocation a build wrote in between.
    observations = tuple(
        observation.model_copy(update={"sequence": sequence})
        for observation in found.observations
    )
    history = found.history.model_copy(
        update={
            "runs": tuple(
                ref.model_copy(update={"sequence": sequence})
                for ref in found.history.runs
            )
        }
    )
    counts = found.counts
    extra.update(
        folded=True,
        counts={
            "dropped": counts.dropped,
            "unrecognized": counts.unrecognized,
            "unattributed": counts.unattributed,
            "merged": counts.merged,
            "claims_unread": counts.claims_unread,
        },
    )
    if placed_as in ("in_window", "after_window"):
        # The run has a place in Elementary's order, as one of its invocations
        # or after its newest one, so a failure Elementary saw pass before it is
        # dated: the file's "no known start" note is left out here. :func:`read`
        # puts a note in its place only for the incidents this run opened with
        # no known start (a test Elementary holds no result for), and that note
        # names those incidents alone, since others in the same fold can be
        # dated. The run itself is still named, under this source's ``run`` and
        # ``placed``. Read alone, the run keeps the file's note: nothing dates it.
        extra["_held_note_at"] = len(notes)
        notes.extend(n for n in found.notes if n != found.unknown_start_note)
    else:
        notes.extend(found.notes)
    return _Source("read", extra=extra, observations=observations, history=history)


def _only_dbt_reports(count: int) -> str:
    """The "no known start" note for a run placed in Elementary's order. It speaks
    of the incidents dbt's run opened with no pass before them, never of every
    failure, since the fold can date the others from Elementary's runs. Such an
    incident can still carry Elementary's later failures (a run from an older
    invocation Elementary recorded), so the note claims no single invocation."""

    return (
        f"{count} incident(s) opened by dbt_run_results have no known start "
        "(first_observed: unknown): no passing run is observed before their "
        "first failure, so every interval before it is unobserved"
    )


#: How dbt's run was placed in the one invocation order: ``alone`` (the only
#: run read), ``in_window`` (Elementary recorded the same invocation),
#: ``after_window`` (it started after Elementary's newest run) or ``unplaced``.
Placed = Literal["alone", "in_window", "after_window", "unplaced"]


def _place(
    run: SourceHistory, started: datetime | None, elementary: SourceHistory | None
) -> tuple[int | None, Placed]:
    """The run's place in the one invocation order, and how it was found.

    Elementary's place for the same invocation; else the place after
    Elementary's window, where the run *started* after Elementary's newest run
    started, both being starts (``run_started_at`` and dbt's
    ``invocation_started_at``, never ``generated_at``, which is the run's end);
    else no place. Alone, it is the only run."""

    [ref] = run.runs
    if elementary is None or not elementary.runs:
        return 1, "alone"
    placed = {held.run: held.sequence for held in elementary.runs}
    if ref.run in placed:
        return placed[ref.run], "in_window"
    newest = elementary.runs[-1]
    if started is not None and newest.at is not None and started > newest.at:
        return newest.sequence + 1, "after_window"
    return None, "unplaced"


# --- the envelope's own fields -------------------------------------------------


def _point(ref: Any) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {
        "invocation_id": ref.run,
        "sequence": ref.sequence,
        "at": ref.at.isoformat() if ref.at is not None else None,
    }


def _window(
    size: int, elementary: SourceHistory | None, dbt: _Source | None
) -> dict[str, Any]:
    """The invocations the read covered: first, last and how many.

    Elementary's read where it ran, which ``invocations`` bounds, so ``count``
    never exceeds it. A dbt run placed after it is folded too and named apart
    under ``after_window``, outside the count. With no Elementary read, the
    window is dbt's one run and ``invocations`` is ``None``: no run count was
    asked of anything.
    """

    history = dbt.history if dbt is not None else None
    after: list[dict[str, Any] | None] = []
    bound: int | None = None
    if elementary is not None and elementary.runs:
        runs = list(elementary.runs)
        bound = size
        placed = dbt.extra.get("placed") if dbt is not None else None
        if history is not None and placed == "after_window":
            after = [_point(ref) for ref in history.runs]
    else:
        runs = list(history.runs) if history is not None else []
    return {
        "invocations": bound,
        "count": len(runs),
        "first": _point(runs[0] if runs else None),
        "last": _point(runs[-1] if runs else None),
        "after_window": after,
    }


def _price_report(priced: dict[str, Any] | None) -> dict[str, Any]:
    """What the read was priced at and what it settled at. ``None`` for both
    where no warehouse was read: dbt's run results are files."""

    if priced is None:
        return {"estimate": None, "settled": None}
    return {**priced, "settled": None}


__all__ = [
    "COMMAND",
    "DEFAULT_INVOCATIONS",
    "MAX_INVOCATIONS",
    "SOURCES",
    "IncidentRead",
    "check_invocations",
    "incidents",
    "read",
]
