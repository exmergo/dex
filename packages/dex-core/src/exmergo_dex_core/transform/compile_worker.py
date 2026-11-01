"""Private dbt compilation child. Database SQL is brokered by the parent engine.

This module is only imported by its subprocess entrypoint: dbt and agate remain
optional. stdout is a private JSON-lines protocol; dbt's output goes to stderr.
There is no direct warehouse handle in this process.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path


def guard_unopened_connections(manager, lazy, states) -> None:
    """Release a connection that was never opened without opening it.

    Releasing such a connection is not database access. dbt-bigquery's
    ``close()`` reads ``connection.handle`` only to close it, and reading the
    property resolves the lazy handle, which this worker refuses. So every
    compile on BigQuery failed in ``before_run``'s ``connection_named("master")``
    before any model compiled (#1001, found dogfooding #1015). A connection
    whose handle is still lazy is marked closed instead. One that was opened
    cannot exist here, since opening is what the worker refuses.

    ``manager``, ``lazy`` and ``states`` are dbt's ``BaseConnectionManager``,
    ``LazyHandle`` and ``ConnectionState``, passed in so this module still
    imports without dbt.
    """

    def never_opened(connection) -> bool:
        return isinstance(getattr(connection, "_handle", None), lazy)

    original_release = manager.release
    original_cleanup = manager.cleanup_all

    def release(self):
        with self.lock:
            connection = self.get_if_exists()
        if connection is not None and never_opened(connection):
            connection.state = states.CLOSED
            return None
        return original_release(self)

    def cleanup_all(self):
        with self.lock:
            unopened = [c for c in self.thread_connections.values() if never_opened(c)]
            for connection in unopened:
                connection.state = states.CLOSED
            if len(unopened) == len(self.thread_connections):
                self.thread_connections.clear()
                return None
        return original_cleanup(self)

    manager.release = release
    manager.cleanup_all = cleanup_all


def main() -> int:
    import agate
    from dbt.adapters.base.connections import BaseConnectionManager
    from dbt.adapters.contracts.connection import (
        AdapterResponse,
        ConnectionState,
        LazyHandle,
    )
    from dbt.cli.main import dbtRunner
    from dbt.context.providers import ParseDatabaseWrapper, RuntimeDatabaseWrapper
    from dbt_common.exceptions import CompilationError

    wire = sys.stdout
    lock = threading.Lock()

    def send(value):
        wire.write(json.dumps(value) + "\n")
        wire.flush()

    def execute(sql, auto_begin=False, fetch=False, **kwargs):
        # A dbt compile may use worker threads; serialize the request/response
        # pair so a result can never reach another model's macro.
        with lock:
            send({"type": "query", "sql": sql})
            answer = json.loads(sys.stdin.readline())

        def column_type(name):
            name = name.upper()
            if "BOOL" in name:
                return agate.Boolean()
            if any(
                part in name
                for part in (
                    "INT",
                    "DECIMAL",
                    "NUMERIC",
                    "FLOAT",
                    "DOUBLE",
                    "REAL",
                    "NUMBER",
                )
            ):
                return agate.Number()
            if "TIMESTAMP" in name or "DATETIME" in name:
                return agate.DateTime()
            if name == "DATE":
                return agate.Date()
            return agate.Text(cast_nulls=False)

        table = agate.Table(
            answer["cells"],
            answer["columns"],
            column_types=[column_type(t) for t in answer["types"]],
        )
        return AdapterResponse("SELECT", rows_affected=len(table.rows)), table

    def guarded_getattr(original):
        def get(self, name):
            if name == "execute":
                return execute
            if name == "get_relation" and isinstance(self, RuntimeDatabaseWrapper):

                def relation(database=None, schema=None, identifier=None):
                    with lock:
                        send(
                            {
                                "type": "relation",
                                "database": database,
                                "schema": schema,
                                "identifier": identifier,
                            }
                        )
                        answer = json.loads(sys.stdin.readline())
                    if answer["relation_type"] is None:
                        return None
                    return self.Relation.create(
                        database=database,
                        schema=schema,
                        identifier=identifier,
                        type=answer["relation_type"],
                    )

                return relation
            return original(self, name)

        return get

    def no_handle(self, connection):
        raise CompilationError(
            "plan-time dbt attempted direct database access; use run_query() or "
            "a statement block with a SELECT so Dex can govern it. Adapter "
            "introspection requiring a direct handle is unsupported during planning"
        )

    # Both parse and execute contexts must be covered: a project can invoke
    # adapter.execute directly without an `if execute` guard during parsing.
    ParseDatabaseWrapper.__getattr__ = guarded_getattr(ParseDatabaseWrapper.__getattr__)
    RuntimeDatabaseWrapper.__getattr__ = guarded_getattr(
        RuntimeDatabaseWrapper.__getattr__
    )
    LazyHandle.resolve = no_handle

    guard_unopened_connections(BaseConnectionManager, LazyHandle, ConnectionState)

    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    argv = [
        "compile",
        "--project-dir",
        spec["project"],
        "--profiles-dir",
        spec["profiles"],
        "--target",
        spec["target"],
        "--target-path",
        str(Path(spec["project"]) / "target"),
        "--log-path",
        str(Path(spec["project"]) / "logs"),
        "--log-format",
        "json",
        "--no-populate-cache",
        "--no-introspect",
        "--no-partial-parse",
        "--threads",
        "1",
        "--no-defer",
        "--select",
        *[f"path:{path}" for path in spec["paths"]],
    ]
    with contextlib.redirect_stdout(sys.stderr):
        result = dbtRunner().invoke(argv)
    nodes = {}
    manifest = Path(spec["project"]) / "target" / "manifest.json"
    if result.success and manifest.is_file():
        content = json.loads(manifest.read_text(encoding="utf-8"))
        for node in content.get("nodes", {}).values():
            path = node.get("original_file_path")
            if (
                path in spec["paths"]
                and node.get("resource_type") == "model"
                and node.get("package_name")
                == content.get("metadata", {}).get("project_name")
            ):
                nodes[path] = node.get("compiled_code")
    send({"type": "complete", "success": result.success, "nodes": nodes})
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
