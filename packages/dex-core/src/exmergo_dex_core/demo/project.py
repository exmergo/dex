"""The demo's dbt project: the models a test fails on, and the build that failed.

A warehouse alone shows what is wrong with the data. An incident needs more: a
test that checks the data, and a run that recorded the test failing. This module
renders both as files, beside the warehouse :mod:`.warehouse` writes:

- **A dbt project, small enough to read in one sitting.** One source over three
  of the warehouse's tables, a staging model over each, and a ``unique`` and a
  ``not_null`` test on each model's key, with a dev-only ``profiles.yml`` wired
  to the demo file, as ``dex transform init`` writes one.
- **A source that arrived late** (#1623): ``shipments``, which the loader lands
  with a ``loaded_at`` capture time the source names as its ``loaded_at_field``.
  ``stg_shipments`` declares ``shipped_at`` as its time column with a 12-hour
  freshness, which renders ``dex_freshness`` (its macro is copied into
  ``macros/``). The table's rows are dated from the newest seeded build, like
  Elementary's history: the last twelve hours' shipments landed minutes after
  that build started, so its freshness test failed.
- **The Elementary package, declared and recorded.** ``packages.yml`` declares
  it at the release the reader supports, ``dbt_project.yml`` names its schema
  and turns its row samples off, and ``dbt_packages/elementary/dbt_project.yml``
  records that release where the resolver reads it. That file is a record, not
  the package: the demo installs nothing (#1477).
- **A semantic layer that reads the failing column** (#1478), in
  ``models/semantic/``: the time spine dbt requires beside any semantic model,
  an ``orders`` semantic model with three metrics, and an ``order_items``
  semantic model whose primary entity is ``order_item_id``. So ``dex maintain
  diagnose`` names that entity beside the cause. ``order_items`` holds no
  measure: the shop's ``order_items`` has no time column, and MetricFlow gives
  every measure one.
- **What the latest build left behind**, seeded rather than run:
  ``target/manifest.json`` (which tests exist, what each reads, where the
  package builds its tables), ``target/run_results.json`` (one ``dbt build``
  invocation, the newest of the runs :mod:`.elementary` seeds) and
  ``target/semantic_manifest.json`` (the semantic layer as MetricFlow reads it,
  which ``explore semantic list --local`` needs). In that build
  ``unique_stg_order_items_order_item_id`` fails, for the reason the warehouse
  already plants: a batch of ``order_items`` was loaded twice, and
  ``dex_freshness_stg_shipments_shipped_at__43200`` fails, because the last
  day's shipments had not landed. Every other test passes. So ``dex maintain
  incidents`` reads two open incidents, with no build and no warehouse write
  first.

The artifacts are generated, never produced by dbt here: the demo runs no dbt,
installs no package and touches no network. They hold the fields dex's readers
read, spelled as dbt 1.12 writes them (the test ids carry the hashes dbt gives
those tests), and the failing test's ``failures`` count is computed from the
generated rows, so it agrees with what a real build of this project reports. A
real ``dex transform build`` writes its own artifacts in Dex's artifact
directory, and the readers take whichever is newer.

**Dated from generation, deterministic in every other way.** The build is the
newest of :mod:`.elementary`'s runs, so it is dated back from the time the demo
was generated, which the caller passes in; nothing here reads a clock. The
invocation id is derived, not drawn. :func:`project_digest` covers every file,
and a test pins it for a fixed generation time, so every offset is pinned.

Nothing here writes a data file: :func:`build_project` is pure, and
:func:`write_project` writes text files only, create-only, so the warehouse
module stays the one place that opens DuckDB writable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .elementary import (
    RELEASE,
    SCHEMA,
    SeededRun,
    SeededTest,
    elementary_tables,
    invocation_id,
)
from .warehouse import (
    DemoPathError,
    DemoTable,
    DemoTargetExistsError,
)

#: The directory the demo writes beside the warehouse file. It names itself as
#: the demo's, so ``analytics/``, the directory the docs give the user's own
#: project (``dex transform init analytics``), stays free.
DEMO_DIR = "demo_project"
#: The dbt project, one level inside it. ``transform init`` refuses wherever the
#: root or one of its immediate children holds a dbt project
#: (:func:`~exmergo_dex_core.dbt_project.discover_projects`), so a project
#: directly under the root would block the user's first ``transform init``.
#: Down here it is found through the config's ``dbt_project_dir`` pin, which
#: ``transform init`` moves to the project it creates.
PROJECT_DIR = f"{DEMO_DIR}/dbt"
#: From the project directory, where dbt runs, back up to the warehouse file.
_UP = "../" * len(PROJECT_DIR.split("/"))
#: The project's dbt name: the one ``dex init <name> --mode demo`` documents.
PROJECT_NAME = "analytics"

#: The dbt source the staging models read, and the warehouse schema it names.
SOURCE_NAME = "shop"
SOURCE_SCHEMA = "main"

#: The release the artifacts are spelled as: the manifest and run-results
#: schema versions it writes, and the hashes in its generic test ids.
DBT_VERSION = "1.12.5"
_MANIFEST_SCHEMA = "https://schemas.getdbt.com/dbt/manifest/v12.json"
_RUN_RESULTS_SCHEMA = "https://schemas.getdbt.com/dbt/run-results/v6.json"

#: The build ``run_results.json`` holds: the newest of the seeded runs.
BUILD_INVOCATION_ID = invocation_id(0)
#: The project's source files read as written this long before the oldest run,
#: so the manifest is newer than every source, as after a real build.
_SOURCES_BEFORE = timedelta(days=1)

#: The recorded release, where the resolver reads it. Not the package.
_RECORDED_RELEASE = f"""\
# The release of the Elementary package this project's history was recorded
# with. dex demo writes this record and nothing else here: it installs no
# package. `dex transform deps` installs the package itself, over this file.
name: elementary
version: '{RELEASE}'
config-version: 2
"""

#: The test the seeded build fails, by the id dbt gives it.
FAILING_TEST = "test.analytics.unique_stg_order_items_order_item_id.90e333a108"

#: The loader's late source (#1623): its table, its staging model, the columns
#: the model selects (the event time ``shipped_at``, the capture time
#: ``loaded_at``), and its key.
SHIPMENTS = "shipments"
_SHIPMENTS_MODEL = "stg_shipments"
_SHIPMENT_COLUMNS = ("shipment_id", "carrier", "parcels", "shipped_at", "loaded_at")
_SHIPMENT_KEY = "shipment_id"
#: The freshness the model declares on ``shipped_at``, and the test it renders,
#: by the id dbt gives it.
_MAX_LAG = "12h"
_MAX_LAG_SECONDS = 43200
LATE_TEST = (
    f"test.{PROJECT_NAME}.dex_freshness_{_SHIPMENTS_MODEL}_shipped_at"
    f"__{_MAX_LAG_SECONDS}.28abf0b4d3"
)
#: Each rendered test's claim id, as ``dex transform plan`` writes it for this
#: declaration (#1448): the incident is keyed by it.
_SHIPMENT_CLAIMS = {
    "unique": "0665fac67d6cc5d9",
    "not_null": "e3c401e72c13f248",
    "dex_freshness": "4e06c7a1957c63cf",
}
#: How often a shipment ships, how long the loader usually takes to land it,
#: how many it holds, and when the late ones landed after the newest build
#: started: before the demo ran, so they are in the table it writes.
_SHIPMENT_EVERY = timedelta(hours=3)
_LOAD_LAG = timedelta(minutes=20)
_SHIPMENT_COUNT = 168
_LATE_LANDED = timedelta(minutes=5)
#: The last shipment ships this long before the newest build starts.
_LAST_SHIPPED = timedelta(minutes=30)
_CARRIERS = ("north", "south", "east")
#: Where the declared test's macro is copied from: the one Dex ships.
_FRESHNESS_MACRO = "dex_freshness.sql"

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
    (_SHIPMENTS_MODEL, SHIPMENTS, _SHIPMENT_COLUMNS, _SHIPMENT_KEY),
)

#: The generic tests on each model's key, with the hash dbt's id carries. dbt
#: derives the hash from the test's name and arguments, so it is fixed for
#: these four; a test that parses the project with dbt holds them to it.
_TEST_HASHES = {
    ("unique", "stg_orders"): "e3b841c71a",
    ("not_null", "stg_orders"): "81cfe2fe64",
    ("unique", "stg_order_items"): "90e333a108",
    ("not_null", "stg_order_items"): "26a7e2bc35",
    ("unique", _SHIPMENTS_MODEL): "61ce1cfa9c",
    ("not_null", _SHIPMENTS_MODEL): "13d2043380",
}

#: The semantic layer's file, and the time spine dbt requires beside any
#: semantic model (#1478).
_SEMANTIC_YML = "models/semantic/_semantic.yml"
TIME_SPINE = "metricflow_time_spine"
_TIME_SPINE_SQL = (
    "select cast(range as date) as date_day\n"
    "from range(date '2025-01-01', date '2028-01-01', interval 1 day)\n"
)

#: One semantic model per staging model: its name, the model it sits on, its
#: description, its default time dimension, and its entities, dimensions and
#: measures as ``(name, type, expr)`` (a time dimension adds its granularity).
#: ``order_items`` is keyed on the column the seeded incident fails on, so
#: ``maintain diagnose`` names it as a reader. It holds no measure: the shop's
#: ``order_items`` has no time column, and MetricFlow gives every measure one.
_SEMANTIC_MODELS: tuple[dict[str, Any], ...] = (
    {
        "name": "orders",
        "model": "stg_orders",
        "description": "One row per order.",
        "agg_time_dimension": "ordered_at",
        "entities": (
            ("order", "primary", "order_id"),
            ("customer", "foreign", "customer_id"),
        ),
        "dimensions": (
            ("status", "categorical", None, None),
            ("ordered_at", "time", "cast(placed_at as timestamp)", "day"),
        ),
        "measures": (
            ("order_count", "count_distinct", "order_id"),
            ("order_revenue", "sum", "order_total"),
        ),
    },
    {
        "name": "order_items",
        "model": "stg_order_items",
        "description": "One row per order line item.",
        "agg_time_dimension": None,
        "entities": (
            ("order_item", "primary", "order_item_id"),
            ("order", "foreign", "order_id"),
            ("product", "foreign", "product_id"),
        ),
        "dimensions": (),
        "measures": (),
    },
)

#: The metrics: name, label, type, and the measure (simple) or the numerator
#: and denominator metrics (ratio) each is built on.
_METRICS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("orders", "Orders", "simple", ("order_count",)),
    ("revenue", "Revenue", "simple", ("order_revenue",)),
    ("average_order_value", "Average order value", "ratio", ("revenue", "orders")),
)

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
    #: When the sources read as written: before every seeded run.
    sources_written_at: datetime

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


def _freshness_macro() -> str:
    from importlib.resources import files

    return (
        files("exmergo_dex_core.transform")
        .joinpath("assets", "macros", _FRESHNESS_MACRO)
        .read_text(encoding="utf-8")
    )


def _test_config(test: str) -> dict[str, Any]:
    """A rendered test's ``config``, as ``dex transform plan`` writes it."""

    config: dict[str, Any] = {}
    if test == "dex_freshness":
        config["tags"] = ["dex_every_run"]
    config["meta"] = {
        "disable_test_samples": True,
        "dex": {"claim": _SHIPMENT_CLAIMS[test]},
    }
    return config


#: ``stg_shipments``' YAML: the declaration ``dex transform plan`` renders for
#: it, its grain's tests and the freshness test on ``shipped_at``. ``loaded_at``
#: is the source's ``loaded_at_field``: the capture time diagnosis reads.
_SHIPMENTS_YML = "version: 2\n\n" + yaml.safe_dump(
    {
        "models": [
            {
                "name": _SHIPMENTS_MODEL,
                "description": "One row per shipment the carrier's loader lands.",
                "config": {
                    "meta": {
                        "dex": {
                            "grain": [_SHIPMENT_KEY],
                            "population": {
                                "rule": "Every shipment the loader has landed."
                            },
                        }
                    }
                },
                "columns": [
                    {
                        "name": _SHIPMENT_KEY,
                        "config": {"meta": {"dex": {"role": "key"}}},
                        "data_tests": [
                            {"unique": {"config": _test_config("unique")}},
                            {"not_null": {"config": _test_config("not_null")}},
                        ],
                    },
                    {
                        "name": "shipped_at",
                        "config": {
                            "meta": {
                                "dex": {
                                    "role": "time",
                                    "freshness": {
                                        "max_lag": _MAX_LAG,
                                        "basis": "business",
                                    },
                                }
                            }
                        },
                        "data_tests": [
                            {
                                "dex_freshness": {
                                    "arguments": {"max_lag_seconds": _MAX_LAG_SECONDS},
                                    "config": _test_config("dex_freshness"),
                                }
                            }
                        ],
                    },
                    {
                        "name": "loaded_at",
                        "config": {"meta": {"dex": {"role": "time"}}},
                    },
                ],
            }
        ]
    },
    sort_keys=False,
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
                    "models": {"elementary": {"+schema": "elementary"}},
                    # Elementary keeps no sample of a failing test's rows.
                    "vars": {"test_sample_row_count": 0},
                }
            ),
        ),
        (
            "packages.yml",
            _dump(
                {
                    "packages": [
                        {"package": "elementary-data/elementary", "version": RELEASE}
                    ]
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
                            "dev": {"type": "duckdb", "path": _UP + warehouse_name}
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
                            "tables": [
                                {"name": table, "loaded_at_field": "loaded_at"}
                                if table == SHIPMENTS
                                else {"name": table}
                                for _, table, _, _ in _MODELS
                            ],
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
                        if model != _SHIPMENTS_MODEL
                    ]
                }
            ),
        ),
        (f"models/staging/{_SHIPMENTS_MODEL}.yml", _SHIPMENTS_YML),
        (f"macros/{_FRESHNESS_MACRO}", _freshness_macro()),
    ]
    files.extend((f"models/staging/{model}.sql", _sql(model)) for model, *_ in _MODELS)
    files.append((f"models/semantic/{TIME_SPINE}.sql", _TIME_SPINE_SQL))
    files.append((_SEMANTIC_YML, _semantic_yaml()))
    return files


def _measure_input(metric: tuple[str, str, str, tuple[str, ...]]) -> list[str]:
    """The measures a metric reads, through the metrics it is built on."""

    _, _, kind, inputs = metric
    if kind == "simple":
        return list(inputs)
    by_name = {m[0]: m for m in _METRICS}
    return [measure for name in inputs for measure in _measure_input(by_name[name])]


def _semantic_yaml() -> str:
    """The semantic layer as its author writes it: the time spine's entry, the
    semantic models and the metrics."""

    def semantic_model(spec: dict[str, Any]) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": spec["name"],
            "description": spec["description"],
            "model": f"ref('{spec['model']}')",
        }
        if spec["agg_time_dimension"]:
            entry["defaults"] = {"agg_time_dimension": spec["agg_time_dimension"]}
        entry["entities"] = [
            {"name": name, "type": kind, "expr": expr}
            for name, kind, expr in spec["entities"]
        ]
        if spec["dimensions"]:
            entry["dimensions"] = []
            for name, kind, expr, grain in spec["dimensions"]:
                dimension: dict[str, Any] = {"name": name, "type": kind}
                if expr is not None:
                    dimension["expr"] = expr
                if grain is not None:
                    dimension["type_params"] = {"time_granularity": grain}
                entry["dimensions"].append(dimension)
        if spec["measures"]:
            entry["measures"] = [
                {"name": name, "agg": agg, "expr": expr}
                for name, agg, expr in spec["measures"]
            ]
        return entry

    def metric(spec: tuple[str, str, str, tuple[str, ...]]) -> dict[str, Any]:
        name, label, kind, inputs = spec
        params: dict[str, Any] = (
            {"measure": inputs[0]}
            if kind == "simple"
            else {"numerator": inputs[0], "denominator": inputs[1]}
        )
        return {"name": name, "label": label, "type": kind, "type_params": params}

    return "version: 2\n\n" + _dump(
        {
            "models": [
                {
                    "name": TIME_SPINE,
                    "description": "One row per day: the time spine MetricFlow "
                    "joins metrics to.",
                    "time_spine": {"standard_granularity_column": "date_day"},
                    "columns": [{"name": "date_day", "granularity": "day"}],
                }
            ],
            "semantic_models": [semantic_model(s) for s in _SEMANTIC_MODELS],
            "metrics": [metric(m) for m in _METRICS],
        }
    )


def _test_id(test: str, model: str) -> str:
    key = next(k for m, _, _, k in _MODELS if m == model)
    return f"test.{PROJECT_NAME}.{test}_{model}_{key}.{_TEST_HASHES[(test, model)]}"


_LATE_KWARGS = {
    "column_name": "shipped_at",
    "max_lag_seconds": _MAX_LAG_SECONDS,
    "model": f"{{{{ get_where_subquery(ref('{_SHIPMENTS_MODEL}')) }}}}",
}


def seeded_tests() -> tuple[SeededTest, ...]:
    """The project's seven tests, as Elementary records them."""

    return (
        *(
            SeededTest(
                unique_id=_test_id(test, model),
                model=model,
                column=key,
                short_name=test,
            )
            for model, _, _, key in _MODELS
            for test in ("unique", "not_null")
        ),
        SeededTest(
            unique_id=LATE_TEST,
            model=_SHIPMENTS_MODEL,
            column="shipped_at",
            short_name="dex_freshness",
            params=json.dumps(_LATE_KWARGS),
        ),
    )


def history_tables(
    tables: tuple[DemoTable, ...],
    warehouse_name: str,
    runs: tuple[SeededRun, ...],
) -> tuple[DemoTable, ...]:
    """Elementary's two tables for ``runs`` of this project, beside a warehouse
    file named ``warehouse_name`` whose shop rows are ``tables``: the failing
    test reports the same ``failures`` as in ``run_results.json``."""

    return elementary_tables(
        runs,
        seeded_tests(),
        failing={
            FAILING_TEST: (
                "outcome",
                duplicated_keys(tables, "order_items", "order_item_id"),
            ),
            # dex_freshness returns one row when the column is stale.
            LATE_TEST: ("late", 1),
        },
        database=Path(warehouse_name).stem,
        project=PROJECT_NAME,
        dbt_version=DBT_VERSION,
    )


def shipments_table(runs: tuple[SeededRun, ...]) -> tuple[DemoTable, ...]:
    """The loader's ``shipments``, dated back from the newest of ``runs``.

    One shipment every three hours, the newest half an hour before that build
    started, each landed twenty minutes after it shipped, except the four that
    shipped within the freshness test's twelve hours of the build: those landed
    five minutes after it started (and before the demo was generated, ten
    minutes after it), so the build read the table without them, and its
    newest shipment was twelve and a half hours old. Empty of runs, there is no
    table.
    """

    if not runs:
        return ()
    newest = runs[-1].started_at
    late_from = newest - timedelta(seconds=_MAX_LAG_SECONDS)
    rows: list[tuple] = []
    for step in range(_SHIPMENT_COUNT - 1, -1, -1):
        shipped = newest - _LAST_SHIPPED - step * _SHIPMENT_EVERY
        landed = newest + _LATE_LANDED if shipped > late_from else shipped + _LOAD_LAG
        number = _SHIPMENT_COUNT - step
        rows.append(
            (
                f"shp_{number:04d}",
                _CARRIERS[number % len(_CARRIERS)],
                1 + (number * 7) % 9,
                shipped.astimezone(UTC).replace(tzinfo=None),
                landed.astimezone(UTC).replace(tzinfo=None),
            )
        )
    return (
        DemoTable(
            name=SHIPMENTS,
            columns=(
                ("shipment_id", "VARCHAR"),
                ("carrier", "VARCHAR"),
                ("parcels", "INTEGER"),
                ("shipped_at", "TIMESTAMP"),
                ("loaded_at", "TIMESTAMP"),
            ),
            rows=tuple(rows),
            note=(
                "a carrier's shipments, each with when it shipped and when the "
                "loader landed it: the last twelve hours' landed minutes after "
                "the newest build read the table"
            ),
        ),
    )


def late_shipments(tables: tuple[DemoTable, ...]) -> int:
    """How many shipments landed after the newest build started: the rows
    ``maintain diagnose`` counts late."""

    found = next((t for t in tables if t.name == SHIPMENTS), None)
    if found is None or not found.rows:
        return 0
    newest = max(row[3] for row in found.rows) + _LAST_SHIPPED
    return sum(1 for row in found.rows if row[4] > newest)


#: The package's models the demo seeds a table for: those the reader's
#: recorded layout lists.
_PACKAGE_TABLES = (
    "dbt_invocations",
    "elementary_test_results",
    "test_result_rows",
    "data_monitoring_metrics",
)


def _elementary_nodes(database: str) -> dict[str, Any]:
    """The package's tables the demo seeds, where the project builds them,
    and the run hooks that upload them: what a parse with the package
    installed and enabled puts in the manifest, cut to what dex reads."""

    nodes: dict[str, Any] = {}
    for table in _PACKAGE_TABLES:
        uid = f"model.elementary.{table}"
        nodes[uid] = {
            "database": database,
            "schema": SCHEMA,
            "name": table,
            "resource_type": "model",
            "package_name": "elementary",
            "unique_id": uid,
            "alias": table,
            "config": {"enabled": True, "materialized": "incremental"},
            "relation_name": f'"{database}"."{SCHEMA}"."{table}"',
            "depends_on": {"macros": [], "nodes": []},
        }
    for hook in ("on-run-start", "on-run-end"):
        uid = f"operation.elementary.elementary-{hook}-0"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "operation",
            "package_name": "elementary",
            "name": f"elementary-{hook}-0",
        }
    return nodes


def _time_spine_node(database: str) -> dict[str, Any]:
    uid = f"model.{PROJECT_NAME}.{TIME_SPINE}"
    return {
        "database": database,
        "schema": SOURCE_SCHEMA,
        "name": TIME_SPINE,
        "resource_type": "model",
        "package_name": PROJECT_NAME,
        "path": f"semantic/{TIME_SPINE}.sql",
        "original_file_path": f"models/semantic/{TIME_SPINE}.sql",
        "unique_id": uid,
        "fqn": [PROJECT_NAME, "semantic", TIME_SPINE],
        "alias": TIME_SPINE,
        "checksum": {
            "name": "sha256",
            "checksum": hashlib.sha256(_TIME_SPINE_SQL.encode("utf-8")).hexdigest(),
        },
        "config": {"enabled": True, "materialized": "view", "meta": {}},
        "tags": [],
        "description": "One row per day: the time spine MetricFlow joins metrics to.",
        "columns": {
            "date_day": {
                "name": "date_day",
                "description": "",
                "meta": {},
                "tags": [],
                "granularity": "day",
            }
        },
        "meta": {},
        "patch_path": f"{PROJECT_NAME}://{_SEMANTIC_YML}",
        "relation_name": f'"{database}"."{SOURCE_SCHEMA}"."{TIME_SPINE}"',
        "raw_code": _TIME_SPINE_SQL.rstrip("\n"),
        "language": "sql",
        "refs": [],
        "sources": [],
        "depends_on": {"macros": [], "nodes": []},
        "access": "protected",
        "version": None,
        "latest_version": None,
        "time_spine": {
            "standard_granularity_column": "date_day",
            "custom_granularities": [],
        },
    }


#: The version of dbt-semantic-interfaces dbt 1.12.5 records in the semantic
#: manifest it writes. The dbt-parse test holds the seeded file equal to a real
#: parse, so a lock bump that moves dbt-core fails it: update this and the
#: entries' shape to what the new release writes, then the project digest.
_DSI_VERSION = {"major_version": "0", "minor_version": "213", "patch_version": "0"}


def _relation(database: str, alias: str) -> dict[str, Any]:
    return {
        "alias": alias,
        "schema_name": SOURCE_SCHEMA,
        "database": database,
        "relation_name": f'"{database}"."{SOURCE_SCHEMA}"."{alias}"',
    }


def _measure_ref(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "filter": None,
        "alias": None,
        "join_to_timespine": False,
        "fill_nulls_with": None,
    }


def _metric_ref(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "filter": None,
        "alias": None,
        "offset_window": None,
        "offset_to_grain": None,
    }


def _semantic_entries(
    database: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The semantic models and metrics as dbt 1.12.5 writes them to
    ``semantic_manifest.json``: the shape MetricFlow reads."""

    return (
        [_semantic_model_entry(database, spec) for spec in _SEMANTIC_MODELS],
        [_metric_entry(metric) for metric in _METRICS],
    )


def _semantic_model_entry(database: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "defaults": {"agg_time_dimension": spec["agg_time_dimension"]}
        if spec["agg_time_dimension"]
        else None,
        "description": spec["description"],
        "node_relation": _relation(database, spec["model"]),
        "primary_entity": None,
        "entities": [
            {
                "name": name,
                "description": None,
                "type": kind,
                "role": None,
                "expr": expr,
                "metadata": None,
                "label": None,
                "config": {"meta": {}},
            }
            for name, kind, expr in spec["entities"]
        ],
        "measures": [
            {
                "name": name,
                "agg": agg,
                "description": None,
                "create_metric": False,
                "expr": expr,
                "agg_params": None,
                "metadata": None,
                "non_additive_dimension": None,
                "agg_time_dimension": None,
                "label": None,
                "config": {"meta": {}},
            }
            for name, agg, expr in spec["measures"]
        ],
        "dimensions": [
            {
                "name": name,
                "description": None,
                "type": kind,
                "datatype": None,
                "is_partition": False,
                "type_params": {
                    "time_granularity": grain,
                    "validity_params": None,
                }
                if grain
                else None,
                "expr": expr,
                "metadata": None,
                "label": None,
                "config": {"meta": {}},
            }
            for name, kind, expr, grain in spec["dimensions"]
        ],
        "label": None,
        "metadata": None,
        "config": {"meta": {}},
    }


def _metric_entry(metric: tuple[str, str, str, tuple[str, ...]]) -> dict[str, Any]:
    name, label, kind, inputs = metric
    return {
        "name": name,
        "description": "",
        "type": kind,
        "datatype": None,
        "type_params": {
            "measure": _measure_ref(inputs[0]) if kind == "simple" else None,
            "numerator": _metric_ref(inputs[0]) if kind == "ratio" else None,
            "denominator": _metric_ref(inputs[1]) if kind == "ratio" else None,
            "expr": None,
            "window": None,
            "grain_to_date": None,
            "metrics": [],
            "conversion_type_params": None,
            "cumulative_type_params": None,
            "input_measures": [_measure_ref(m) for m in _measure_input(metric)],
            "metric_aggregation_params": None,
            "join_to_timespine": False,
            "fill_nulls_with": None,
            "is_private": None,
        },
        "filter": None,
        "metadata": None,
        "label": label,
        "config": {"meta": {}},
        "time_granularity": None,
    }


def _semantic_manifest_document(database: str) -> dict[str, Any]:
    """``target/semantic_manifest.json``, as a ``dbt parse`` of the project
    writes it: what ``explore semantic list --local`` reads."""

    models, metrics = _semantic_entries(database)
    return {
        "semantic_models": models,
        "metrics": metrics,
        "project_configuration": {
            "time_spine_table_configurations": [],
            "metadata": None,
            "dsi_package_version": _DSI_VERSION,
            "time_spines": [
                {
                    "node_relation": _relation(database, TIME_SPINE),
                    "primary_column": {"name": "date_day", "time_granularity": "day"},
                    "custom_granularities": [],
                }
            ],
        },
        "saved_queries": [],
    }


def _semantic_manifest(
    database: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[str]]]:
    """The semantic models and metrics as ``manifest.json`` records them, and
    each one's parents: the semantic manifest's entries, with the fields only
    the manifest carries."""

    path = _SEMANTIC_YML.removeprefix("models/")
    entries, metric_entries = _semantic_entries(database)
    models: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    parents: dict[str, list[str]] = {}
    owner: dict[str, str] = {}

    def without_metadata(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in m.items() if k != "metadata"} for m in members]

    for spec, entry in zip(_SEMANTIC_MODELS, entries, strict=True):
        uid = f"semantic_model.{PROJECT_NAME}.{spec['name']}"
        model_id = f"model.{PROJECT_NAME}.{spec['model']}"
        models[uid] = {
            **entry,
            "resource_type": "semantic_model",
            "package_name": PROJECT_NAME,
            "path": path,
            "original_file_path": _SEMANTIC_YML,
            "unique_id": uid,
            "fqn": [PROJECT_NAME, "semantic", spec["name"]],
            "model": f"ref('{spec['model']}')",
            "entities": without_metadata(entry["entities"]),
            "measures": without_metadata(entry["measures"]),
            "depends_on": {"macros": [], "nodes": [model_id]},
            "refs": [{"name": spec["model"], "package": None, "version": None}],
            "config": {"enabled": True, "group": None, "meta": {}},
            "unrendered_config": {},
            "group": None,
        }
        parents[uid] = [model_id]
        for measure, *_ in spec["measures"]:
            owner[measure] = uid
    for (name, _, kind, inputs), entry in zip(_METRICS, metric_entries, strict=True):
        uid = f"metric.{PROJECT_NAME}.{name}"
        depends = (
            [owner[inputs[0]]]
            if kind == "simple"
            else [f"metric.{PROJECT_NAME}.{i}" for i in inputs]
        )
        metrics[uid] = {
            **{k: v for k, v in entry.items() if k != "datatype"},
            "resource_type": "metric",
            "package_name": PROJECT_NAME,
            "path": path,
            "original_file_path": _SEMANTIC_YML,
            "unique_id": uid,
            "fqn": [PROJECT_NAME, "semantic", name],
            "config": {"enabled": True, "group": None, "meta": {}},
            "unrendered_config": {},
            "sources": [],
            "depends_on": {"macros": [], "nodes": depends},
            "refs": [],
            "metrics": [],
            "group": None,
            "meta": {},
            "tags": [],
        }
        parents[uid] = depends
    return models, metrics, parents


def _manifest(database: str, build: SeededRun) -> dict[str, Any]:
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
            "path": "staging/_sources.yml",
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
        if table == SHIPMENTS:
            # dbt 1.10+ writes the field at both levels.
            sources[source_id]["loaded_at_field"] = "loaded_at"
            sources[source_id]["config"]["loaded_at_field"] = "loaded_at"
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
        if model == _SHIPMENTS_MODEL:
            nodes[model_id].update(_shipments_entry())
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
                    "meta": _test_config(test)["meta"]
                    if model == _SHIPMENTS_MODEL
                    else {},
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
        if model == _SHIPMENTS_MODEL:
            nodes[LATE_TEST] = _late_test_node(database, model_id)
            tests.append(LATE_TEST)
            parent_map[LATE_TEST] = [model_id]
            child_map[LATE_TEST] = []
        child_map[model_id] = sorted(tests)
    for source_id in sources:
        parent_map[source_id] = []
    spine = _time_spine_node(database)
    nodes[spine["unique_id"]] = spine
    parent_map[spine["unique_id"]] = []
    child_map[spine["unique_id"]] = []
    semantic_models, metrics, semantic_parents = _semantic_manifest(database)
    for uid, held in semantic_parents.items():
        parent_map[uid] = held
        child_map.setdefault(uid, [])
        for parent in held:
            child_map[parent] = sorted([*child_map.get(parent, []), uid])
    for uid in (package := _elementary_nodes(database)):
        nodes[uid] = package[uid]
        parent_map[uid] = []
        child_map[uid] = []
    return {
        "metadata": {
            "dbt_schema_version": _MANIFEST_SCHEMA,
            "dbt_version": DBT_VERSION,
            "generated_at": _iso(build.started_at),
            "invocation_id": build.invocation_id,
            "invocation_started_at": _iso(build.started_at),
            "env": {},
            "project_name": PROJECT_NAME,
            "adapter_type": "duckdb",
        },
        "nodes": nodes,
        "sources": sources,
        "macros": {},
        "docs": {},
        "exposures": {},
        "metrics": metrics,
        "groups": {},
        "selectors": {},
        "disabled": {},
        "parent_map": parent_map,
        "child_map": child_map,
        "group_map": {},
        "saved_queries": {},
        "semantic_models": semantic_models,
        "unit_tests": {},
    }


def _shipments_entry() -> dict[str, Any]:
    """What ``stg_shipments``' own YAML adds to its manifest node: its
    description, its declaration and its columns."""

    document = yaml.safe_load(_SHIPMENTS_YML)["models"][0]
    columns: dict[str, Any] = {}
    for column in document["columns"]:
        meta = column.get("config", {}).get("meta", {})
        columns[column["name"]] = {
            "name": column["name"],
            "description": "",
            "meta": meta,
            "config": {"meta": meta, "tags": []},
            "tags": [],
        }
    meta = document["config"]["meta"]
    return {
        "description": document["description"],
        "columns": columns,
        "meta": meta,
        "config": {"enabled": True, "materialized": "view", "meta": meta},
        "patch_path": f"{PROJECT_NAME}://models/staging/{_SHIPMENTS_MODEL}.yml",
    }


def _late_test_node(database: str, model_id: str) -> dict[str, Any]:
    """The freshness test on ``stg_shipments.shipped_at``, as dbt parses it."""

    name = LATE_TEST.split(".")[2]
    config = _test_config("dex_freshness")
    return {
        "database": database,
        "schema": f"{SOURCE_SCHEMA}_dbt_test__audit",
        "name": name,
        "resource_type": "test",
        "package_name": PROJECT_NAME,
        "path": f"{name}.sql",
        "original_file_path": f"models/staging/{_SHIPMENTS_MODEL}.yml",
        "unique_id": LATE_TEST,
        "fqn": [PROJECT_NAME, "staging", name],
        "alias": name,
        "checksum": {"name": "none", "checksum": ""},
        "config": {
            "enabled": True,
            "schema": "dbt_test__audit",
            "materialized": "test",
            "severity": "ERROR",
            "fail_calc": "count(*)",
            "warn_if": "!= 0",
            "error_if": "!= 0",
            "tags": config["tags"],
            "meta": config["meta"],
        },
        "tags": config["tags"],
        "description": "",
        "columns": {},
        "meta": config["meta"],
        "relation_name": None,
        "raw_code": "{{ test_dex_freshness(**_dbt_generic_test_kwargs) }}",
        "language": "sql",
        "refs": [{"name": _SHIPMENTS_MODEL, "package": None, "version": None}],
        "sources": [],
        "depends_on": {
            "macros": [f"macro.{PROJECT_NAME}.test_dex_freshness", *_TEST_MACROS],
            "nodes": [model_id],
        },
        "column_name": "shipped_at",
        "file_key_name": f"models.{_SHIPMENTS_MODEL}",
        "attached_node": model_id,
        "test_metadata": {
            "name": "dex_freshness",
            "kwargs": dict(_LATE_KWARGS),
            "namespace": None,
        },
    }


def duplicated_keys(tables: tuple[DemoTable, ...], table: str, column: str) -> int:
    """How many values of ``table.column`` occur more than once: the row count a
    dbt ``unique`` test on it returns, which its ``failures`` reports."""

    found = next(t for t in tables if t.name == table)
    index = next(i for i, (name, _) in enumerate(found.columns) if name == column)
    counts = Counter(row[index] for row in found.rows)
    return sum(1 for n in counts.values() if n > 1)


def _run_results(tables: tuple[DemoTable, ...], build: SeededRun) -> dict[str, Any]:
    """One ``dbt build`` of the project: the three views build, and of the
    seven tests only the ``unique`` test on ``stg_order_items`` and the
    freshness test on ``stg_shipments`` can fail, where the seeded run says
    each did."""

    results: list[dict[str, Any]] = []
    clock = build.started_at + timedelta(seconds=2)

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
        if model == _SHIPMENTS_MODEL:
            if build.late == "fail":
                entry(LATE_TEST, "fail", "Got 1 result, configured to fail if != 0", 1)
            else:
                entry(LATE_TEST, "pass", "", 0)
        for test in ("unique", "not_null"):
            # Only the batch of order_items loaded twice breaks a key.
            failed = (
                duplicated_keys(tables, table, key)
                if test == "unique" and build.outcome == "fail" and table != SHIPMENTS
                else 0
            )
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
            "generated_at": _iso(build.completed_at),
            "invocation_id": build.invocation_id,
            "invocation_started_at": _iso(build.started_at),
            "env": {},
        },
        "results": results,
        "elapsed_time": (build.completed_at - build.started_at).total_seconds(),
        "args": {"which": "build", "target": "dev", "select": [], "exclude": []},
    }


def build_project(
    tables: tuple[DemoTable, ...],
    warehouse_name: str,
    runs: tuple[SeededRun, ...],
) -> DemoProject:
    """The project for a warehouse file named ``warehouse_name``, as files.

    Pure: no I/O. ``tables`` are the warehouse's generated rows, which decide
    the failing test's ``failures`` count, and ``runs`` the seeded history
    (:func:`~.elementary.seeded_runs`), whose newest is the build the artifacts
    record. The database dbt names is the file's stem, as dbt-duckdb names it.
    """

    database = Path(warehouse_name).stem
    build = runs[-1]
    artifacts = (
        ("dbt_packages/elementary/dbt_project.yml", _RECORDED_RELEASE),
        (
            "target/manifest.json",
            json.dumps(_manifest(database, build), indent=2) + "\n",
        ),
        (
            "target/run_results.json",
            json.dumps(_run_results(tables, build), indent=2) + "\n",
        ),
        (
            "target/semantic_manifest.json",
            json.dumps(_semantic_manifest_document(database), indent=2) + "\n",
        ),
    )
    return DemoProject(
        sources=tuple(_source_files(warehouse_name)),
        artifacts=artifacts,
        sources_written_at=runs[0].started_at - _SOURCES_BEFORE,
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
    """The demo's directory under ``root`` if anything already sits there."""

    target = root / DEMO_DIR
    return target if target.exists() or target.is_symlink() else None


def refuse_target_name(target: Path) -> None:
    """Refuse a warehouse file named like the demo's directory beside it,
    before anything is written: the two would be the same path."""

    if target.name == DEMO_DIR:
        raise DemoPathError(
            f"'{target.as_posix()}' is where dex demo writes its dbt project "
            f"({PROJECT_DIR}/), so it cannot also be the warehouse file. Name "
            "the file something else, for example `dex demo shop.duckdb`"
        )


def refuse_collision(root: Path) -> None:
    """Refuse, before anything is written, a root that already holds the
    demo's directory: the demo creates and never replaces, as for the file."""

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
    owned = root / DEMO_DIR
    base = root / PROJECT_DIR
    try:
        # Exclusive: a directory that appeared since the check is refused here,
        # before anything is written, so the rollback below removes only ours.
        owned.mkdir()
    except FileExistsError:
        refuse_collision(root)
        raise
    try:
        for relative, text in project.files:
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # "x": a file that appeared since the check is refused, not replaced.
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        # The sources were written before the builds that read them, so a
        # reader comparing their times with the manifest's does not call the
        # seeded artifacts stale. Dated from the oldest seeded run, so this
        # time moves with generation time as the runs do.
        written_at = project.sources_written_at.timestamp()
        for relative, _ in project.sources:
            os.utime(base / relative, (written_at, written_at))
    except BaseException:
        shutil.rmtree(owned, ignore_errors=True)
        raise
    return [f"{PROJECT_DIR}/{relative}" for relative, _ in project.files]


__all__ = [
    "BUILD_INVOCATION_ID",
    "DBT_VERSION",
    "DEMO_DIR",
    "FAILING_TEST",
    "LATE_TEST",
    "PROJECT_DIR",
    "PROJECT_NAME",
    "SHIPMENTS",
    "DemoProject",
    "build_project",
    "duplicated_keys",
    "history_tables",
    "late_shipments",
    "project_collision",
    "project_digest",
    "refuse_collision",
    "refuse_target_name",
    "seeded_tests",
    "shipments_table",
    "write_project",
]
