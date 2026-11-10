"""Validate authored control-flow models against fresh, governed dbt compilation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from .. import command_args
from ..adapters import get_dialect
from ..dbt_project import profiles_dir
from ..envelope import EstimateQuality
from ..guards.cost_guard import ConfirmationRequiredError
from .build import _child_environ, _collect_messages, assert_dev_target, shadow_project
from .plans import EditKind, PlanEdit
from .validate import CompiledModel, EditValidationError, needs_compilation

TIMEOUT_SECONDS = 120


class CompileQueries:
    """One adapter, cost gate, and reservation for all dynamically discovered SQL."""

    def __init__(self, engine):
        self.engine = engine
        self.adapter = None
        self.phases = 0
        self.queries = 0
        self.estimate = 0.0
        self.warnings: list[str] = []
        self.inventory = None
        self.introspected: list[str] = []

    def _resolve(self, request: dict) -> tuple[str, str | None]:
        """The relation dbt named, matched against the scoped free inventory.

        Returns the dotted name dbt asked for and the one inventory identifier
        it matches, or ``None`` where nothing in scope matches. Two matches are
        refused: answering either would let dbt compile against a guess.
        """

        from ..cache import match_identifier

        if self.adapter is None:
            self.adapter = self.engine._adapter("transform plan", reads_dev_target=True)
        if self.inventory is None:
            self.inventory = {
                obj.identifier: obj for obj in self.adapter.list_objects()
            }
        name = ".".join(
            str(request[key])
            for key in ("database", "schema", "identifier")
            if request.get(key) is not None
        )
        matches = match_identifier(name, list(self.inventory))
        if len(matches) > 1:
            raise EditValidationError(f"dbt relation lookup is ambiguous: {name}")
        return name, matches[0] if matches else None

    def relation(self, request: dict) -> dict:
        """Resolve dbt's get_relation/is_incremental through scoped free metadata."""

        _name, match = self._resolve(request)
        return {
            "relation_type": self.inventory[match].object_type.lower()
            if match is not None
            else None
        }

    def columns(self, request: dict) -> dict:
        """Answer dbt's ``get_columns_in_relation`` from scoped metadata (#1154).

        Column names and types are a schema read, never a row: they come from
        ``table_metadata``, the free metadata read every other command uses for
        a relation's columns, never through the query firewall, which keeps
        refusing ``DESCRIBE`` and every other non-SELECT shape. Each lookup
        counts toward ``query.max_statements`` like a compile-time query, and
        is named once in the plan's warnings. A relation the scoped inventory
        does not hold is answered as dbt answers a missing relation, with no
        columns, and the warning says so rather than letting it pass quietly.
        """

        if self.queries >= self.engine.config.query.max_statements:
            raise EditValidationError("compile-time SQL exceeds query.max_statements")
        self.queries += 1
        name, match = self._resolve(request)
        if match is None:
            note = (
                f"dbt compilation asked for the columns of {name}, which is not in "
                "the scoped inventory (outside the source scope, or not built "
                "yet); it was answered with no columns, as dbt answers a missing "
                "relation"
            )
            if note not in self.warnings:
                self.warnings.append(note)
            return {"columns": []}
        # Columns alone where the adapter can read them without counting rows
        # (DuckDB's table_metadata adds a COUNT(*), which scans a view); every
        # other connector answers table_metadata from its metadata inventory.
        columns_only = getattr(self.adapter, "table_columns", None)
        if callable(columns_only):
            found = columns_only(match)
        else:
            _meta, found = self.adapter.table_metadata(match)
        if match not in self.introspected:
            self.introspected.append(match)
            self.warnings.append(
                f"dbt compilation listed the columns of {match} from metadata "
                "(names and types; no rows read)"
            )
        answer: dict[str, Any] = {
            "columns": [[column.name, column.data_type] for column in found]
        }
        # A connector whose dbt lists columns with DESCRIBE (Snowflake) hands
        # over the type as DESCRIBE prints it, precision and scale included, and
        # the worker builds each column with ``from_description`` as dbt does.
        described = getattr(self.adapter, "dbt_column_types", None)
        if callable(described):
            answer["columns"] = described(match)
            answer["described"] = True
        # A connector whose dbt column class needs more than a name and a type
        # (BigQuery's nested fields and mode) hands over its column tree too, so
        # a macro's `data_type` is the one dbt itself would render.
        schema = getattr(self.adapter, "column_schema", None)
        if callable(schema):
            answer["schema"] = schema(match)
        return answer

    def handshake(self, command, adapter, estimate, **kwargs):
        note = (
            "dbt compilation discovers queries dynamically; this estimate covers "
            "only work discovered so far, and later queries share the command ceiling"
        )
        try:
            if self.phases == 0:
                command_args.billed_handshake(
                    "transform plan",
                    adapter,
                    estimate,
                    **{**kwargs, "notes": [*kwargs.get("notes", []), note]},
                )
            else:
                pending = command_args.phase_handshake(
                    "transform plan",
                    adapter,
                    estimate,
                    phase="dbt compilation",
                    hint=lambda *args, **kw: (
                        "re-run transform plan with --confirm and a budget covering "
                        "all compile-time queries; queries already run will run again"
                    ),
                    notes=[note],
                )
                if pending is not None:
                    exc = ConfirmationRequiredError(pending.cost)
                    exc.request = pending
                    raise exc
        except ConfirmationRequiredError as exc:
            exc.cost.estimate_quality = EstimateQuality.APPROXIMATE
            raise
        if self.phases == 0:
            gate = command_args.cost_gate(adapter)
            self.estimate = gate.cost().estimate if gate is not None else estimate
        else:
            self.estimate += estimate
        self.phases += 1
        if note not in self.warnings:
            self.warnings.append(note)

    def query(self, sql: str) -> dict[str, Any]:
        from ..explore.commands import _run_statements
        from ..guards.query_firewall import QueryRefusedError, assert_query_shape

        dialect = get_dialect(self.engine.connector or self.engine.config.connector)
        # A raw schema read is refused before the generic shape check can point
        # the caller at `explore profile`, which cannot help a macro (#1154).
        shape = introspection_shape(sql, dialect)
        if shape is not None:
            raise QueryRefusedError(
                f"compile-time {shape} is not run as SQL; dbt compilation lists a "
                "relation's columns only through adapter.get_columns_in_relation"
                "(relation), which Dex answers from scoped metadata (names and "
                "types, no rows). A macro that issues the statement itself, such "
                "as a direct call to the dispatched get_columns_in_relation macro "
                "or a hand-written describe, is unsupported during planning: call "
                "adapter.get_columns_in_relation instead"
            )
        # Before opening even a read-only adapter. Direct macro DML never
        # reaches connection discovery or warehouse metadata.
        assert_query_shape(sql, dialect=dialect)
        if self.queries >= self.engine.config.query.max_statements:
            raise EditValidationError("compile-time SQL exceeds query.max_statements")
        self.queries += 1
        if self.adapter is None:
            self.adapter = self.engine._adapter("transform plan", reads_dev_target=True)
        batch, shared = _run_statements(
            self.engine,
            [(sql, None)],
            adapter=self.adapter,
            handshake=self.handshake,
            reads_dev_target=True,
        )
        item = batch[0]
        self.warnings.extend(shared.warnings)
        if item.error is not None:
            raise item.error
        result = item.result
        self.warnings.extend(result.warnings)
        if result.truncated:
            raise EditValidationError(
                "compile-time query result was truncated; narrow the SELECT "
                "so dbt never compiles a model from an incomplete result"
            )
        return {"columns": result.columns, "types": result.types, "cells": result.cells}

    def settle(self, result=None):
        if self.adapter is not None:
            if result is not None:
                command_args.stamp_spend(result, self.adapter)
                if command_args.cost_gate(self.adapter) is not None:
                    result.cost.estimate = self.estimate
                    result.cost.estimate_quality = EstimateQuality.APPROXIMATE
            else:
                self.engine._last_spend = command_args.settled_spend(self.adapter)


# The statement shapes that read a catalog rather than rows: a column listing
# by any other spelling than the adapter method Dex brokers (#1154).
_INTROSPECTION_NODES = {
    "Describe": "DESCRIBE",
    "Summarize": "SUMMARIZE",
    "Show": "SHOW",
    "Pragma": "PRAGMA",
}


def introspection_shape(sql: str, dialect: str) -> str | None:
    """Name the catalog read a compile-time statement makes, or ``None``.

    Covers a top-level or nested ``DESCRIBE``/``SUMMARIZE``/``SHOW``/``PRAGMA``
    and a read of ``information_schema``. Only the refusal's wording depends on
    it: every one of these is refused either way, and a statement that does not
    parse is left to the firewall, which refuses it on its own terms.
    """

    import sqlglot
    from sqlglot import exp

    try:
        root = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    if root is None:
        return None
    for node in root.walk():
        label = _INTROSPECTION_NODES.get(type(node).__name__)
        if label is not None:
            return label
        # BigQuery keeps `INFORMATION_SCHEMA.COLUMNS` as one dotted table name,
        # so every dotted part is looked at, not only the schema slot.
        if isinstance(node, exp.Table) and "information_schema" in {
            part.lower()
            for slot in (node.catalog, node.db, node.name)
            for part in (slot or "").split(".")
        }:
            return "information_schema read"
    return None


#: Appended to a disabled model in the throwaway copy, so dbt compiles it.
_FORCE_ENABLED = "{{ config(enabled=true) }}"


def compile_models(
    engine, edits: list[PlanEdit], *, force: set[str] | None = None
) -> tuple[dict[str, CompiledModel], CompileQueries]:
    """Compile the complete proposed project, admitting SQL through the engine.

    ``force`` names model paths compiled even with no control flow: a drafted
    package override (#1229) is checked by dbt's compile, macros and governed
    introspection included, before it is stored.
    """

    from . import dev_target

    forced = force or set()
    wanted = {
        edit.path: edit
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL
        and edit.new_content is not None
        and (edit.path in forced or needs_compilation(edit.new_content))
    }
    broker = CompileQueries(engine)
    if not wanted:
        return {}, broker
    try:
        available = importlib.util.find_spec("dbt.cli.main") is not None
    except ModuleNotFoundError:
        available = False
    if not available:
        raise EditValidationError(
            "control-flow models require dbt; install the connector extra "
            "(for example exmergo-dex-core[duckdb])"
        )
    project = Path(engine.project_dir()).resolve()
    target = engine.config.dbt_target or "dev"
    assert_dev_target(target, engine.config.dbt_target)
    connector = engine.connector or engine.config.connector
    dialect = get_dialect(connector)
    profiles = profiles_dir(project).resolve()
    try:
        with shadow_project(project, edits) as shadow:
            effective = engine.config.model_copy(update={"connector": connector})
            if connector == "duckdb" and engine.path is not None:
                from ..config import DuckDBTarget

                effective.duckdb = DuckDBTarget(
                    path=str(Path(engine.path).expanduser().resolve())
                )
            if profiles == project:
                profiles = shadow
                # Relative DuckDB paths are relative to the real dbt cwd, not
                # the disposable project. Normalize only the copied profile.
                profile_file = shadow / "profiles.yml"
                if not profile_file.is_file():
                    raise EditValidationError(
                        "control-flow compilation requires the post-edit profiles.yml"
                    )
                raw = yaml.safe_load(profile_file.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise EditValidationError("profiles.yml must be a YAML mapping")
                for profile in raw.values():
                    if not isinstance(profile, dict):
                        continue
                    outputs = profile.get("outputs")
                    if not isinstance(outputs, dict):
                        continue
                    for output in outputs.values():
                        if not isinstance(output, dict):
                            continue
                        path = output.get("path")
                        if isinstance(path, str) and "{{" in path:
                            from ..connect import _profile_scalar

                            path = _profile_scalar(path, os.environ)
                        if (
                            output.get("type") == "duckdb"
                            and isinstance(path, str)
                            and path != ":memory:"
                        ):
                            output["path"] = str(
                                (project / Path(path).expanduser()).resolve()
                            )
                profile_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
            # The edited config and profile are checked together, before dbt
            # gets to render a model or request any warehouse work.
            broker.warnings.extend(
                dev_target.check(
                    shadow,
                    target,
                    effective,
                    engine.repo_root,
                    store=engine.store,
                    connection=engine.connection,
                )
            )
            spec = shadow.parent / "compile.json"
            spec.write_text(
                json.dumps(
                    {
                        "project": str(shadow),
                        "profiles": str(profiles),
                        "target": target,
                        "paths": list(wanted),
                    }
                ),
                encoding="utf-8",
            )
            reply, messages = _worker(project, spec, broker)
            if not reply.get("success"):
                raise EditValidationError(
                    "dbt compile failed: "
                    + (messages[0] if messages else "no compiled models")
                )
            # dbt compiles no SQL for a disabled model, and a model disabled
            # today can be enabled by the next build (a flag, a config change),
            # so its SELECT is checked anyway: compiled once more in this copy
            # with an in-file `enabled=true`, which outranks a YAML or project
            # setting (#1156). The project's own file is never changed.
            disabled = sorted(set(reply.get("disabled") or ()) & set(wanted))
            if disabled:
                for path in disabled:
                    (shadow / path).write_text(
                        wanted[path].new_content + "\n" + _FORCE_ENABLED + "\n",
                        encoding="utf-8",
                    )
                reply, more = _worker(project, spec, broker)
                if not reply.get("success"):
                    raise EditValidationError(
                        "dbt compile failed with the disabled model(s) "
                        f"{', '.join(disabled)} enabled to check them: "
                        + (more[0] if more else "no compiled models")
                    )
                messages = [*messages, *more]
                broker.warnings.extend(
                    f"{path}: dbt leaves this model disabled, so it is not built; "
                    "Dex compiled it enabled to check its SQL"
                    for path in disabled
                )
            compiled = {}
            for path, edit in wanted.items():
                code = reply.get("nodes", {}).get(path)
                if not isinstance(code, str) or not code.strip():
                    raise EditValidationError(
                        f"{path}: dbt returned no fresh compiled SQL"
                    )
                evidence = CompiledModel(
                    path,
                    hashlib.sha256(edit.new_content.encode()).hexdigest(),
                    code,
                    target,
                    dialect,
                )
                evidence.check(edit)
                compiled[path] = evidence
            broker.warnings.extend(f"dbt: {message}" for message in messages)
            return compiled, broker
    except BaseException:
        broker.settle()
        raise


def _worker(
    project: Path, spec: Path, broker: CompileQueries
) -> tuple[dict, list[str]]:
    """Run dbt out of process, with a bounded lifetime and private query channel."""

    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as logs,
        subprocess.Popen(  # noqa: S603 (fixed interpreter/module, no shell)
            [
                sys.executable,
                "-m",
                "exmergo_dex_core.transform.compile_worker",
                str(spec),
            ],
            cwd=project,
            env=_child_environ(isolated=True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=logs,
            text=True,
        ) as child,
    ):
        timer = threading.Timer(TIMEOUT_SECONDS, child.kill)
        timer.start()
        reply = {}
        try:
            for line in child.stdout:
                request = json.loads(line)
                if request.get("type") == "query":
                    response = broker.query(request["sql"])
                elif request.get("type") == "relation":
                    response = broker.relation(request)
                elif request.get("type") == "columns":
                    response = broker.columns(request)
                elif request.get("type") == "complete":
                    reply = request
                    break
                else:
                    raise EditValidationError("invalid dbt compilation worker response")
                if response is not None:
                    child.stdin.write(json.dumps(response) + "\n")
                    child.stdin.flush()
            child.wait(timeout=5)
        finally:
            timer.cancel()
            if child.poll() is None:
                child.kill()
                child.wait()
        logs.seek(0)
        output = logs.read()
        messages = _collect_messages(
            subprocess.CompletedProcess([], child.returncode, output, "")
        )
        if not reply:
            raise EditValidationError(
                "dbt compilation worker failed or timed out: "
                + (messages[0] if messages else output[-1000:])
            )
        return reply, messages
