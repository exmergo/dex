"""The demo's dbt project: the models a test fails on, and the build that failed.

A warehouse alone shows what is wrong with the data. An incident needs more: a
test that checks the data, and a run that recorded the test failing. This module
renders both as files, beside the warehouse :mod:`.warehouse` writes:

- **A dbt project, small enough to read in one sitting.** One source over two of
  the warehouse's tables, a staging model over each, and a ``unique`` and a
  ``not_null`` test on each model's key, with a dev-only ``profiles.yml`` wired
  to the demo file, as ``dex transform init`` writes one.
- **What a build of it left behind**, seeded rather than run: ``target/manifest.json``
  (which tests exist, what each reads) and ``target/run_results.json`` (one
  ``dbt build`` invocation). In that build ``unique_stg_order_items_order_item_id``
  fails, for the reason the warehouse already plants: a batch of ``order_items``
  was loaded twice. Every other test passes. So ``dex maintain incidents`` reads
  one open incident from dbt's run results, with no build and no warehouse
  write first.

The artifacts are generated, never produced by dbt here: the demo runs no dbt,
installs no package and touches no network. They hold the fields dex's readers
read, spelled as dbt 1.12 writes them (the test ids carry the hashes dbt gives
those tests), and the failing test's ``failures`` count is computed from the
generated rows, so it agrees with what a real build of this project reports. A
real ``dex transform build`` writes its own artifacts in Dex's artifact
directory, and the readers take whichever is newer.

**Determinism is the same contract as the warehouse's.** No wall clock: the
build is dated from :data:`~.warehouse._ANCHOR`. The invocation id is derived,
not drawn. :func:`project_digest` covers every file, and a test pins it.

Nothing here writes a data file: :func:`build_project` is pure, and
:func:`write_project` writes text files only, create-only, so the warehouse
module stays the one place that opens DuckDB writable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from .warehouse import _ANCHOR, DemoTable, DemoTargetExistsError, _uuid_shaped

#: The directory the project is written into, beside the warehouse file, and
#: the project's dbt name: the one ``dex init <name> --mode demo`` documents.
PROJECT_DIR = "analytics"
PROJECT_NAME = "analytics"

#: The dbt source the staging models read, and the warehouse schema it names.
SOURCE_NAME = "shop"
SOURCE_SCHEMA = "main"

#: The release the artifacts are spelled as: the manifest and run-results
#: schema versions it writes, and the hashes in its generic test ids.
DBT_VERSION = "1.12.5"
_MANIFEST_SCHEMA = "https://schemas.getdbt.com/dbt/manifest/v12.json"
_RUN_RESULTS_SCHEMA = "https://schemas.getdbt.com/dbt/run-results/v6.json"

#: The seeded build: the morning after the last generated date.
BUILD_STARTED_AT = datetime.combine(_ANCHOR + timedelta(days=1), time(6, 0), tzinfo=UTC)
_BUILD_SECONDS = 42
BUILD_INVOCATION_ID = _uuid_shaped("dex-demo-dbt-build-1")

#: The test the seeded build fails, by the id dbt gives it.
FAILING_TEST = "test.analytics.unique_stg_order_items_order_item_id.90e333a108"

#: Each staging model: its source table, the columns it selects, its key.
_MODELS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "stg_orders",
        "orders",
        ("order_id", "customer_id", "status", "order_total", "placed_at"),
        "order_id",
    ),
    (
        "stg_order_items",
        "order_items",
        ("order_item_id", "order_id", "product_id", "quantity", "unit_price"),
        "order_item_id",
    ),
)

#: The generic tests on each model's key, with the hash dbt's id carries. dbt
#: derives the hash from the test's name and arguments, so it is fixed for
#: these four; a test that parses the project with dbt holds them to it.
_TEST_HASHES = {
    ("unique", "stg_orders"): "e3b841c71a",
    ("not_null", "stg_orders"): "81cfe2fe64",
    ("unique", "stg_order_items"): "90e333a108",
    ("not_null", "stg_order_items"): "26a7e2bc35",
}

#: The macros dbt records under each generic test, after the test's own.
_TEST_MACROS = (
    "macro.dbt.get_where_subquery",
    "macro.dbt.get_limit_subquery_sql",
    "macro.dbt.should_store_failures",
    "macro.dbt.statement",
)


@dataclass(frozen=True)
class DemoProject:
    """The project as files: each path under the project directory, and its text.

    ``sources`` are the hand-authored files (the reviewable ones the command
    reports as diffs); ``artifacts`` are what a build leaves under ``target/``.
    """

    sources: tuple[tuple[str, str], ...]
    artifacts: tuple[tuple[str, str], ...]

    @property
    def files(self) -> tuple[tuple[str, str], ...]:
        return self.sources + self.artifacts


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sql(model: str) -> str:
    _, table, columns, _ = next(m for m in _MODELS if m[0] == model)
    return (
        f"select {', '.join(columns)}\n"
        f"from {{{{ source('{SOURCE_NAME}', '{table}') }}}}\n"
    )


def _dump(document: Any) -> str:
    return yaml.safe_dump(document, sort_keys=False)


def _source_files(warehouse_name: str) -> list[tuple[str, str]]:
    """The hand-authored half: what a person reads and edits."""

    files = [
        (
            "dbt_project.yml",
            _dump(
                {
                    "name": PROJECT_NAME,
                    "version": "1.0.0",
                    "profile": PROJECT_NAME,
                    "model-paths": ["models"],
                }
            ),
        ),
        (
            "profiles.yml",
            _dump(
                {
                    PROJECT_NAME: {
                        "target": "dev",
                        "outputs": {
                            "dev": {"type": "duckdb", "path": f"../{warehouse_name}"}
                        },
                    }
                }
            ),
        ),
        (
            "models/staging/_sources.yml",
            "version: 2\n\n"
            + _dump(
                {
                    "sources": [
                        {
                            "name": SOURCE_NAME,
                            "description": "The shop's tables, as its loader writes.",
                            "schema": SOURCE_SCHEMA,
                            "tables": [{"name": table} for _, table, _, _ in _MODELS],
                        }
                    ]
                }
            ),
        ),
        (
            "models/staging/_staging.yml",
            "version: 2\n\n"
            + _dump(
                {
                    "models": [
                        {
                            "name": model,
                            "description": f"One row per row of {SOURCE_NAME}.{table}.",
                            "columns": [
                                {
                                    "name": key,
                                    "data_tests": ["unique", "not_null"],
                                }
                            ],
                        }
                        for model, table, _, key in _MODELS
                    ]
                }
            ),
        ),
    ]
    files.extend((f"models/staging/{model}.sql", _sql(model)) for model, *_ in _MODELS)
    return files


def _test_id(test: str, model: str) -> str:
    key = next(k for m, _, _, k in _MODELS if m == model)
    return f"test.{PROJECT_NAME}.{test}_{model}_{key}.{_TEST_HASHES[(test, model)]}"


def _manifest(database: str) -> dict[str, Any]:
    """The compiled manifest a ``dbt parse`` of the project writes, cut to what
    dex reads: the nodes, their lineage, and each test's metadata."""

    nodes: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    parent_map: dict[str, list[str]] = {}
    child_map: dict[str, list[str]] = {}
    for model, table, _, key in _MODELS:
        source_id = f"source.{PROJECT_NAME}.{SOURCE_NAME}.{table}"
        model_id = f"model.{PROJECT_NAME}.{model}"
        sources[source_id] = {
            "database": database,
            "schema": SOURCE_SCHEMA,
            "name": table,
            "resource_type": "source",
            "package_name": PROJECT_NAME,
            "path": "models/staging/_sources.yml",
            "original_file_path": "models/staging/_sources.yml",
            "unique_id": source_id,
            "fqn": [PROJECT_NAME, "staging", SOURCE_NAME, table],
            "source_name": SOURCE_NAME,
            "identifier": table,
            "relation_name": f'"{database}"."{SOURCE_SCHEMA}"."{table}"',
            "config": {"enabled": True},
            "columns": {},
            "meta": {},
            "tags": [],
        }
        sql = _sql(model)
        nodes[model_id] = {
            "database": database,
            "schema": SOURCE_SCHEMA,
            "name": model,
            "resource_type": "model",
            "package_name": PROJECT_NAME,
            "path": f"staging/{model}.sql",
            "original_file_path": f"models/staging/{model}.sql",
            "unique_id": model_id,
            "fqn": [PROJECT_NAME, "staging", model],
            "alias": model,
            "checksum": {
                "name": "sha256",
                "checksum": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            },
            "config": {"enabled": True, "materialized": "view", "meta": {}},
            "tags": [],
            "description": f"One row per row of {SOURCE_NAME}.{table}.",
            "columns": {key: {"name": key, "description": "", "meta": {}, "tags": []}},
            "meta": {},
            "patch_path": f"{PROJECT_NAME}://models/staging/_staging.yml",
            "relation_name": f'"{database}"."{SOURCE_SCHEMA}"."{model}"',
            "raw_code": sql.rstrip("\n"),
            "language": "sql",
            "refs": [],
            "sources": [[SOURCE_NAME, table]],
            "depends_on": {"macros": [], "nodes": [source_id]},
            "access": "protected",
            "version": None,
            "latest_version": None,
            "primary_key": [key],
        }
        parent_map[model_id] = [source_id]
        child_map[source_id] = [model_id]
        tests = []
        for test in ("unique", "not_null"):
            test_id = _test_id(test, model)
            tests.append(test_id)
            nodes[test_id] = {
                "database": database,
                "schema": f"{SOURCE_SCHEMA}_dbt_test__audit",
                "name": f"{test}_{model}_{key}",
                "resource_type": "test",
                "package_name": PROJECT_NAME,
                "path": f"{test}_{model}_{key}.sql",
                "original_file_path": "models/staging/_staging.yml",
                "unique_id": test_id,
                "fqn": [PROJECT_NAME, "staging", f"{test}_{model}_{key}"],
                "alias": f"{test}_{model}_{key}",
                "checksum": {"name": "none", "checksum": ""},
                "config": {
                    "enabled": True,
                    "schema": "dbt_test__audit",
                    "materialized": "test",
                    "severity": "ERROR",
                    "fail_calc": "count(*)",
                    "warn_if": "!= 0",
                    "error_if": "!= 0",
                    "meta": {},
                },
                "tags": [],
                "description": "",
                "columns": {},
                "meta": {},
                "relation_name": None,
                "raw_code": f"{{{{ test_{test}(**_dbt_generic_test_kwargs) }}}}",
                "language": "sql",
                "refs": [{"name": model, "package": None, "version": None}],
                "sources": [],
                "depends_on": {
                    "macros": [f"macro.dbt.test_{test}", *_TEST_MACROS],
                    "nodes": [model_id],
                },
                "column_name": key,
                "file_key_name": f"models.{model}",
                "attached_node": model_id,
                "test_metadata": {
                    "name": test,
                    "kwargs": {
                        "column_name": key,
                        "model": f"{{{{ get_where_subquery(ref('{model}')) }}}}",
                    },
                    "namespace": None,
                },
            }
            parent_map[test_id] = [model_id]
            child_map[test_id] = []
        child_map[model_id] = sorted(tests)
    for source_id in sources:
        parent_map[source_id] = []
    return {
        "metadata": {
            "dbt_schema_version": _MANIFEST_SCHEMA,
            "dbt_version": DBT_VERSION,
            "generated_at": _iso(BUILD_STARTED_AT),
            "invocation_id": BUILD_INVOCATION_ID,
            "invocation_started_at": _iso(BUILD_STARTED_AT),
            "env": {},
            "project_name": PROJECT_NAME,
            "adapter_type": "duckdb",
        },
        "nodes": nodes,
        "sources": sources,
        "macros": {},
        "docs": {},
        "exposures": {},
        "metrics": {},
        "groups": {},
        "selectors": {},
        "disabled": {},
        "parent_map": parent_map,
        "child_map": child_map,
        "group_map": {},
        "saved_queries": {},
        "semantic_models": {},
        "unit_tests": {},
    }


def duplicated_keys(tables: tuple[DemoTable, ...], table: str, column: str) -> int:
    """How many values of ``table.column`` occur more than once: the row count a
    dbt ``unique`` test on it returns, which its ``failures`` reports."""

    found = next(t for t in tables if t.name == table)
    index = next(i for i, (name, _) in enumerate(found.columns) if name == column)
    counts = Counter(row[index] for row in found.rows)
    return sum(1 for n in counts.values() if n > 1)


def _run_results(tables: tuple[DemoTable, ...]) -> dict[str, Any]:
    """One ``dbt build`` of the project: both views build, and of the four
    tests only the ``unique`` test on ``stg_order_items`` fails."""

    results: list[dict[str, Any]] = []
    clock = BUILD_STARTED_AT + timedelta(seconds=2)

    def entry(uid: str, status: str, message: str, failures: int | None) -> None:
        nonlocal clock
        started, clock = clock, clock + timedelta(seconds=3)
        results.append(
            {
                "status": status,
                "timing": [
                    {
                        "name": "execute",
                        "started_at": _iso(started),
                        "completed_at": _iso(clock),
                    }
                ],
                "thread_id": "Thread-1",
                "execution_time": 3.0,
                "adapter_response": {"_message": "OK"},
                "message": message,
                "failures": failures,
                "unique_id": uid,
                "compiled": True,
                "relation_name": None,
            }
        )

    for model, table, _, key in _MODELS:
        entry(f"model.{PROJECT_NAME}.{model}", "success", "OK", None)
        for test in ("unique", "not_null"):
            failed = duplicated_keys(tables, table, key) if test == "unique" else 0
            if failed:
                entry(
                    _test_id(test, model),
                    "fail",
                    f"Got {failed} results, configured to fail if != 0",
                    failed,
                )
            else:
                entry(_test_id(test, model), "pass", "", 0)
    return {
        "metadata": {
            "dbt_schema_version": _RUN_RESULTS_SCHEMA,
            "dbt_version": DBT_VERSION,
            "generated_at": _iso(BUILD_STARTED_AT + timedelta(seconds=_BUILD_SECONDS)),
            "invocation_id": BUILD_INVOCATION_ID,
            "invocation_started_at": _iso(BUILD_STARTED_AT),
            "env": {},
        },
        "results": results,
        "elapsed_time": float(_BUILD_SECONDS),
        "args": {"which": "build", "target": "dev", "select": [], "exclude": []},
    }


def build_project(tables: tuple[DemoTable, ...], warehouse_name: str) -> DemoProject:
    """The project for a warehouse file named ``warehouse_name``, as files.

    Pure: no I/O. ``tables`` are the warehouse's generated rows, which decide
    the failing test's ``failures`` count. The database dbt names is the file's
    stem, as dbt-duckdb names it.
    """

    database = Path(warehouse_name).stem
    artifacts = (
        ("target/manifest.json", json.dumps(_manifest(database), indent=2) + "\n"),
        (
            "target/run_results.json",
            json.dumps(_run_results(tables), indent=2) + "\n",
        ),
    )
    return DemoProject(
        sources=tuple(_source_files(warehouse_name)), artifacts=artifacts
    )


def project_digest(project: DemoProject) -> str:
    """A sha256 over every file's path and text, in order: the project's half of
    the determinism promise, pinned by a test as :func:`~.warehouse.row_digest`
    is."""

    digest = hashlib.sha256()
    for path, text in project.files:
        digest.update(path.encode("utf-8") + b"\x00" + text.encode("utf-8") + b"\x00")
    return digest.hexdigest()


def project_collision(root: Path) -> Path | None:
    """The project directory under ``root`` if anything already sits there."""

    target = root / PROJECT_DIR
    return target if target.exists() or target.is_symlink() else None


def refuse_collision(root: Path) -> None:
    """Refuse, before anything is written, a root that already holds the
    project directory: the demo creates and never replaces, as for the file."""

    found = project_collision(root)
    if found is not None:
        raise DemoTargetExistsError(
            f"'{found.as_posix()}' already exists; dex demo writes its dbt project "
            "there and never opens or replaces one you already have. Run it in an "
            "empty directory"
        )


def write_project(root: Path, project: DemoProject) -> list[str]:
    """Write every file under ``root/PROJECT_DIR``, create-only, and return the
    paths written relative to ``root``. On a failure the files written so far are
    removed again, so a half-written project is never left behind."""

    refuse_collision(root)
    base = root / PROJECT_DIR
    try:
        for relative, text in project.files:
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # "x": a file that appeared since the check is refused, not replaced.
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
    except Exception:
        # The directory did not exist a moment ago, so all of it is ours.
        shutil.rmtree(base, ignore_errors=True)
        raise
    return [f"{PROJECT_DIR}/{relative}" for relative, _ in project.files]


__all__ = [
    "BUILD_INVOCATION_ID",
    "BUILD_STARTED_AT",
    "DBT_VERSION",
    "FAILING_TEST",
    "PROJECT_DIR",
    "PROJECT_NAME",
    "DemoProject",
    "build_project",
    "duplicated_keys",
    "project_collision",
    "project_digest",
    "refuse_collision",
    "write_project",
]
