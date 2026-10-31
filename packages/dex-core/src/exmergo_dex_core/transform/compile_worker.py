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


def main() -> int:
    import agate
    from dbt.adapters.contracts.connection import AdapterResponse, LazyHandle
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
