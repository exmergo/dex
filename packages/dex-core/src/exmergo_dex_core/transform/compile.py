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

    def relation(self, request: dict) -> dict:
        """Resolve dbt's get_relation/is_incremental through scoped free metadata."""

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
        return {
            "relation_type": self.inventory[matches[0]].object_type.lower()
            if matches
            else None
        }

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
        from ..guards.query_firewall import assert_query_shape

        # Before opening even a read-only adapter. Direct macro DML never
        # reaches connection discovery or warehouse metadata.
        assert_query_shape(
            sql,
            dialect=get_dialect(self.engine.connector or self.engine.config.connector),
        )
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


def compile_models(
    engine, edits: list[PlanEdit]
) -> tuple[dict[str, CompiledModel], CompileQueries]:
    """Compile the complete proposed project, admitting SQL through the engine."""

    from . import dev_target

    wanted = {
        edit.path: edit
        for edit in edits
        if edit.kind is EditKind.MODEL_SQL
        and edit.new_content is not None
        and needs_compilation(edit.new_content)
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
