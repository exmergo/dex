"""What a generated test costs per production run (#1462).

A plan that renders a generated test prices it from production's catalog sizes:
the relations the test reads, resolved into ``<connector>.prod``, their row
counts and bytes, and the connector's size-based estimate, grouped by cadence.
Where a size cannot be read the test's estimate is unknown, with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.adapters.base import ObjectMeta
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.transform import test_cost
from exmergo_dex_core.transform.declarations import GeneratedRow

FRESH = {
    "model": "fct_orders",
    "columns": {
        "loaded_at": {
            "role": "time",
            "freshness": {"max_lag": "6h", "basis": "capture"},
        }
    },
}
CONTINUOUS = {
    "model": "fct_orders",
    "columns": {"loaded_at": {"role": "time", "continuity": {"grain": "day"}}},
}
ORDERS_SQL = "select 1 as order_id, current_timestamp as loaded_at\n"


# --- the plan, end to end on DuckDB ---------------------------------------------


def _warehouse(tmp_path: Path, rows: int | None) -> Path:
    """The file dex reads, with ``rows`` rows in ``prod.fct_orders`` (none at
    all when ``rows`` is None)."""

    path = tmp_path / "dev.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute("create schema if not exists prod")
        if rows is not None:
            conn.execute(
                "create table prod.fct_orders as select range as order_id, "
                "current_timestamp as loaded_at from range(?)",
                [rows],
            )
    return path


def _repo(tmp_path: Path, *, prod: str | None = "prod") -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    lines = ["connector: duckdb", "duckdb:", f"  path: {tmp_path / 'dev.duckdb'}"]
    if prod:
        lines.append(f"  prod: {prod}")
    lines += ["declarations:", "  render_tests: true"]
    (tmp_path / ".dex" / "config.yml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return tmp_path


def _plan(tmp_path: Path, capsys, declaration: dict) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_orders.sql",
                        "kind": "model_sql",
                        "content": ORDERS_SQL,
                    }
                ],
                "declarations": [declaration],
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_a_rendered_test_over_a_production_table_is_priced_with_its_size(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _warehouse(tmp_path, rows=7)
    planned = _plan(_repo(tmp_path), capsys, FRESH)

    assert planned["status"] == "ok", planned["errors"]
    report = planned["data"]["generated_tests"]
    [row] = report["tests"]
    assert row["status"] == "added"
    assert row["cost"] == {
        "status": "estimated",
        "estimate": 0.0,
        "relations": ["dev.prod.fct_orders"],
    }
    cost = report["cost"]
    assert cost["prod"] == "prod"
    assert cost["paradigm"] == "free_local"
    assert cost["read_at"]
    [relation] = cost["relations"]
    assert relation["relation"] == "dev.prod.fct_orders"
    assert relation["rows"] == 7
    assert relation["object_type"] == "table"
    assert cost["per_run"] == {
        gt.EVERY_RUN: {
            "tests": 1,
            "estimated": 1,
            "unknown": 0,
            "estimate": 0.0,
            "complete": True,
        }
    }


def test_the_same_plan_with_the_table_unreadable_says_unknown_and_why(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _warehouse(tmp_path, rows=None)
    planned = _plan(_repo(tmp_path), capsys, FRESH)

    assert planned["status"] == "ok", planned["errors"]
    report = planned["data"]["generated_tests"]
    [row] = report["tests"]
    assert row["cost"]["status"] == "unknown"
    assert row["cost"]["reason"] == test_cost.RELATION_NOT_FOUND
    assert row["cost"]["relation"] == "dev.prod.fct_orders"
    assert report["cost"]["per_run"][gt.EVERY_RUN]["complete"] is False
    assert report["cost"]["per_run"][gt.EVERY_RUN]["estimate"] is None
    assert any("partial" in w for w in planned["warnings"])


def test_with_no_prod_every_test_is_unknown_and_the_warning_names_the_key(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _warehouse(tmp_path, rows=3)
    planned = _plan(_repo(tmp_path, prod=None), capsys, FRESH)

    assert planned["status"] == "ok", planned["errors"]
    [row] = planned["data"]["generated_tests"]["tests"]
    assert row["cost"]["reason"] == test_cost.PROD_UNSET
    assert any("duckdb.prod" in w for w in planned["warnings"])


def test_each_cadence_is_totalled_apart(dbt_project_dir: Path, tmp_path: Path, capsys):
    _warehouse(tmp_path, rows=3)
    both = {
        "model": "fct_orders",
        "columns": {
            "loaded_at": {
                "role": "time",
                "freshness": {"max_lag": "6h", "basis": "capture"},
                "continuity": {"grain": "day"},
            }
        },
    }
    planned = _plan(_repo(tmp_path), capsys, both)

    assert planned["status"] == "ok", planned["errors"]
    per_run = planned["data"]["generated_tests"]["cost"]["per_run"]
    assert set(per_run) == {gt.EVERY_RUN, gt.SCHEDULED}
    assert per_run[gt.EVERY_RUN]["tests"] == 1
    assert per_run[gt.SCHEDULED]["tests"] == 1


# --- pricing, with a fake connector ---------------------------------------------


class _Adapter:
    """A metered connector whose catalog knows ``sizes`` (identifier to
    ``(rows, bytes, object_type)``) and prices a statement at its summed bytes."""

    name = "bigquery"
    project = "proj"
    paradigm = Paradigm.BYTES_SCANNED

    def __init__(self, sizes: dict, billed: bool = False) -> None:
        self.sizes = sizes
        self.billed = billed
        self.asked: list[str] = []
        self.dev_reads: list = []
        self._dev_target = None

    def read_dev_target(self, scope) -> None:
        self.dev_reads.append(scope)
        self._dev_target = tuple(scope) if scope else None

    def table_metadata(self, identifier: str):
        self.asked.append(identifier)
        if identifier not in self.sizes:
            raise LookupError(identifier)
        rows, size, kind = self.sizes[identifier]
        meta = ObjectMeta(
            identifier=identifier,
            object_type=kind,
            schema=identifier.split(".")[-2],
            name=identifier.split(".")[-1],
            row_count=rows,
            byte_size=size,
            column_count=1,
        )
        return meta, []

    def size_estimate(self, byte_sizes) -> float:
        return float(sum(byte_sizes))

    def catalog_read_billed(self) -> bool:
        return self.billed


def _engine(connector: str = "bigquery", **target) -> SimpleNamespace:
    config = DexConfig(connector=connector, **{connector: target})
    return SimpleNamespace(config=config, connector=None)


def _node(name, *, schema="dev", database="proj", custom=None, kind="model", **extra):
    config = {"schema": custom, **extra.pop("config", {})}
    return {
        f"{kind}.p.{name}": {
            "name": name,
            "resource_type": kind,
            "package_name": "p",
            "database": database,
            "schema": schema,
            "alias": extra.pop("alias", name),
            "config": config,
            **extra,
        }
    }


def _view(tmp_path: Path, nodes: dict, sources: dict | None = None, macro: str = ""):
    if macro:
        (tmp_path / "macros").mkdir(exist_ok=True)
        (tmp_path / "macros" / "naming.sql").write_text(macro, encoding="utf-8")
    return SimpleNamespace(
        root=str(tmp_path),
        macro_paths=["macros"],
        manifest={
            "metadata": {"project_name": "p"},
            "nodes": nodes,
            "sources": sources or {},
        },
    )


def _row(model="fct_orders", cadence=gt.EVERY_RUN, reads=None, status="added"):
    row = GeneratedRow(
        {
            "model": model,
            "column": None,
            "test": "dex_x",
            "cadence": cadence,
            "claim": "c",
            "status": status,
        }
    )
    row.reads = reads
    return row


def _price(engine, rows, adapter, view):
    report = {"tests": rows}
    warnings = test_cost.price(
        engine, report, view=view, adapter_factory=lambda: adapter
    )
    return report, warnings


def test_a_test_is_priced_from_every_relation_it_reads(tmp_path: Path):
    adapter = _Adapter(
        {
            "proj.analytics.fct_orders": (100, 5_000, "table"),
            "proj.analytics.dim_customers": (10, 2_000, "table"),
        }
    )
    row = _row(
        reads=[{"model": "fct_orders"}, {"relation": "ref('dim_customers')"}],
        cadence=gt.SCHEDULED,
    )
    view = _view(tmp_path, {**_node("fct_orders"), **_node("dim_customers")})

    report, warnings = _price(
        _engine(prod="analytics", dev_dataset="dev"), [row], adapter, view
    )

    assert warnings == []
    assert row["cost"] == {
        "status": "estimated",
        "estimate": 7_000.0,
        "relations": ["proj.analytics.fct_orders", "proj.analytics.dim_customers"],
    }
    assert report["cost"]["unit"] == "bytes"
    assert report["cost"]["per_run"][gt.SCHEDULED]["estimate"] == 7_000.0
    # prod was read for this one call, then dropped.
    assert adapter.dev_reads == [["proj.analytics"], None]


def test_a_custom_schema_is_named_as_dbt_names_it_in_production(tmp_path: Path):
    adapter = _Adapter({"proj.analytics_marts.fct_orders": (1, 10, "table")})
    view = _view(tmp_path, _node("fct_orders", custom="marts", schema="dev_marts"))

    _price(_engine(prod="analytics"), [row := _row()], adapter, view)

    assert row["cost"]["relations"] == ["proj.analytics_marts.fct_orders"]


LAYERED = (
    Path(test_cost.__file__).parent / "assets" / "macros" / "generate_schema_name.sql"
).read_text(encoding="utf-8")


def test_the_layered_naming_needs_the_production_target_name(tmp_path: Path):
    adapter = _Adapter({"proj.marts_prd.fct_orders": (1, 10, "table")})
    view = _view(
        tmp_path, _node("fct_orders", custom="marts", schema="marts_dev"), macro=LAYERED
    )

    _price(_engine(prod="analytics"), [unset := _row()], adapter, view)
    engine = _engine(prod="analytics")
    engine.config.dbt_prod_target = "prd"
    _price(engine, [named := _row()], adapter, view)

    assert unset["cost"]["reason"] == test_cost.PROD_TARGET_UNSET
    assert named["cost"]["relations"] == ["proj.marts_prd.fct_orders"]


def test_a_naming_dex_cannot_reproduce_is_unknown_never_guessed(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (1, 10, "table")})
    override = (
        "{% macro generate_schema_name(custom_schema_name, node) %}"
        "{{ custom_schema_name or target.schema }}{% endmacro %}"
    )
    view = _view(tmp_path, _node("fct_orders"), macro=override)

    _price(_engine(prod="analytics"), [row := _row()], adapter, view)

    assert row["cost"]["reason"] == test_cost.CUSTOM_SCHEMA_NAMING
    assert adapter.asked == []


def test_a_source_is_read_only_inside_the_allowlist(tmp_path: Path):
    sources = {
        "source.p.raw.orders": {
            "source_name": "raw",
            "name": "orders",
            "database": "proj",
            "schema": "raw",
            "identifier": "orders",
        }
    }
    adapter = _Adapter(
        {"proj.raw.orders": (1, 50, "table"), "proj.analytics.fct": (1, 5, "table")}
    )
    row = _row(reads=[{"relation": "source('raw', 'orders')"}])
    view = _view(tmp_path, {}, sources)

    _price(
        _engine(prod="analytics", project="proj", datasets=["raw"]),
        [inside := _row(reads=row.reads)],
        adapter,
        view,
    )
    _price(
        _engine(prod="analytics", project="proj", datasets=["staging"]),
        [outside := _row(reads=row.reads)],
        adapter,
        view,
    )

    assert inside["cost"]["status"] == "estimated"
    assert outside["cost"]["reason"] == test_cost.OUTSIDE_ALLOWLIST


def test_a_view_has_no_size_and_the_test_is_unknown(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (None, None, "view")})
    _price(_engine(prod="analytics"), [row := _row()], adapter, _view(tmp_path, {}))
    assert row["cost"]["reason"] == test_cost.NO_SIZE_IN_METADATA
    assert "a view" in row["cost"]["detail"]


def test_a_billed_catalog_is_not_read_unasked(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (1, 1, "table")}, billed=True)
    _price(_engine(prod="analytics"), [row := _row()], adapter, _view(tmp_path, {}))
    assert row["cost"]["reason"] == test_cost.METADATA_READ_BILLED
    assert adapter.asked == []


def test_an_unnamed_input_is_unknown(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (1, 1, "table")})
    row = _row(reads=[{"relation": None}])
    _price(_engine(prod="analytics"), [row], adapter, _view(tmp_path, {}))
    assert row["cost"]["reason"] == test_cost.RELATION_UNNAMED


def test_a_snapshot_with_a_literal_schema_is_read_there(tmp_path: Path):
    adapter = _Adapter({"proj.snapshots.orders_snap": (9, 90, "table")})
    nodes = _node(
        "orders_snap",
        kind="snapshot",
        schema="snapshots",
        config={"target_schema": "snapshots"},
    )
    row = _row(reads=[{"relation": "ref('orders_snap')"}])
    _price(_engine(prod="analytics"), [row], adapter, _view(tmp_path, nodes))
    assert row["cost"]["relations"] == ["proj.snapshots.orders_snap"]


def test_only_tests_that_run_in_production_are_priced(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (1, 1, "table")})
    rows = [
        _row(status="not_rendered"),
        _row(status="stale"),
        _row(status="listed_while_off"),
    ]
    report, _ = _price(_engine(prod="analytics"), rows, adapter, _view(tmp_path, {}))
    assert ["cost" in row for row in rows] == [False, False, True]
    assert report["cost"]["per_run"][gt.EVERY_RUN]["tests"] == 1


def test_a_connection_that_cannot_open_leaves_every_test_unknown(tmp_path: Path):
    def boom():
        raise RuntimeError("secret text")

    report = {"tests": [row := _row()]}
    test_cost.price(
        _engine(prod="analytics"),
        report,
        view=_view(tmp_path, {}),
        adapter_factory=boom,
    )
    assert row["cost"]["reason"] == test_cost.CONNECTION_UNAVAILABLE
    assert "secret" not in json.dumps(report)


# --- each connector's price from sizes ------------------------------------------


def test_bigquery_raises_each_table_to_its_billed_minimum():
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter

    floor = 10 * 1024 * 1024
    estimate = BigQueryAdapter.size_estimate(object(), [1, 3 * floor])  # type: ignore[arg-type]
    assert estimate == 4 * floor


@pytest.mark.parametrize(
    ("module", "cls"),
    [
        ("postgres", "PostgresAdapter"),
        ("clickhouse", "ClickHouseAdapter"),
    ],
)
def test_the_db_load_connectors_price_bytes_over_their_scan_rate(module, cls):
    import importlib

    adapter_cls = getattr(
        importlib.import_module(f"exmergo_dex_core.adapters.{module}"), cls
    )
    fake = SimpleNamespace(_scan_seconds=lambda b: adapter_cls._scan_seconds(fake, b))
    assert adapter_cls.size_estimate(fake, [0]) > 0
    big = adapter_cls.size_estimate(fake, [100 * 1024**3])
    assert big > adapter_cls.size_estimate(fake, [1024**3])


def test_a_view_is_priced_on_the_tables_it_reads(tmp_path: Path):
    """dbt's default materialization keeps no size, and a test over a view
    scans what the view reads: the manifest's lineage is followed to tables."""

    adapter = _Adapter(
        {
            "proj.analytics.fct_orders": (None, None, "view"),
            "proj.analytics.raw_orders": (10, 4_000, "table"),
            "proj.raw.payments": (5, 1_000, "table"),
        }
    )
    nodes = {
        **_node("fct_orders", depends_on={"nodes": ["model.p.int_orders"]}),
        **_node(
            "int_orders",
            config={"materialized": "ephemeral"},
            depends_on={"nodes": ["seed.p.raw_orders", "source.p.raw.payments"]},
        ),
        **_node("raw_orders", kind="seed"),
    }
    sources = {
        "source.p.raw.payments": {
            "resource_type": "source",
            "source_name": "raw",
            "name": "payments",
            "database": "proj",
            "schema": "raw",
            "identifier": "payments",
        }
    }
    report, warnings = _price(
        _engine(prod="analytics"),
        [row := _row()],
        adapter,
        _view(tmp_path, nodes, sources),
    )

    assert warnings == []
    assert row["cost"]["status"] == "estimated"
    assert row["cost"]["estimate"] == 5_000.0
    assert row["cost"]["relations"] == [
        "proj.analytics.raw_orders",
        "proj.raw.payments",
    ]
    assert row["cost"]["through"] == ["proj.analytics.fct_orders", "int_orders"]
    [view] = [r for r in report["cost"]["relations"] if r["object_type"] == "view"]
    assert "reason" not in view


def test_a_view_with_no_lineage_has_no_size(tmp_path: Path):
    adapter = _Adapter({"proj.analytics.fct_orders": (None, None, "view")})
    _price(
        _engine(prod="analytics"),
        [row := _row()],
        adapter,
        _view(tmp_path, _node("fct_orders")),
    )
    assert row["cost"]["reason"] == test_cost.NO_SIZE_IN_METADATA
