"""Rendering a SLayer query without letting SLayer execute anything (#677).

What the renderer promises: SLayer plans over the models dex already validated,
held in a storage that only reads; the dialect is chosen from dex's connector by
an explicit table and never by SLayer's Postgres fallback; the datasource it sees
carries a name and a type and nothing a client could connect with; and a render
is a dry run that never builds a SQL client, never writes, and never imports
SLayer's file or SQLite storage.

Needs the real `[slayer]` extra, so it skips without it; the `tests` CI job
is where it runs.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from slayer.core.models import DatasourceConfig
from slayer.engine.query_engine import SlayerQueryEngine
from slayer.sql.client import SlayerSQLClient

from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.local_runtime import _RENDERERS
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.slayer import loader, render
from exmergo_dex_core.slayer.loader import FORMAT_VERSION, validate_model_contents
from exmergo_dex_core.slayer.project import SlayerSemanticLayer
from exmergo_dex_core.slayer.render import (
    EXCLUDED,
    SLAYER_TYPES,
    SlayerRenderer,
    ValidatedModelStorage,
)

BASE = "semantics/slayer"
NOW = datetime(2026, 1, 15, 12)


def model(name: str = "orders", **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": "wh",
        "default_time_dimension": "ordered_at",
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "status", "type": "TEXT"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "measures": [{"name": "revenue", "formula": "amount:sum"}],
        **fields,
    }


def loaded(*models: dict[str, Any]) -> list[loader.LoadedModel]:
    contents = {f"models/wh/{m['name']}.yaml": yaml.safe_dump(m) for m in models}
    result = validate_model_contents(contents)
    assert not result.errors, result.notes()
    return result.models


QUERY = {"source_model": "orders", "measures": ["revenue"], "dimensions": ["status"]}


def renderer(connector: str = "duckdb", **kwargs: Any) -> SlayerRenderer:
    return SlayerRenderer(loaded(model()), connector=connector, now=NOW, **kwargs)


# --- rendering ---------------------------------------------------------------------


def test_a_saved_measure_renders_to_sql():
    sql = renderer().render(QUERY).sql

    assert "SUM(orders.amount)" in sql
    assert "FROM demo.main.orders" in sql
    assert "GROUP BY" in sql


def test_the_dialect_follows_dex_s_connector():
    """BigQuery quotes with backticks and DuckDB with double quotes: the type in
    the table, not SLayer's Postgres fallback, chose each."""

    assert "`" in renderer("bigquery").render(QUERY).sql
    assert "`" not in renderer("duckdb").render(QUERY).sql


def test_a_relative_date_resolves_against_the_fixed_clock():
    query = {
        "source_model": "orders",
        "measures": ["revenue"],
        "time_dimensions": [
            {
                "dimension": "ordered_at",
                "granularity": "day",
                "date_range": ["last 7 days", "today"],
            }
        ],
    }
    pinned = renderer()

    first = pinned.render(query).sql
    assert "2026-01-08" in first and "2026-01-16" in first
    assert pinned.render(query).sql == first
    later = (
        SlayerRenderer(loaded(model()), connector="duckdb", now=datetime(2026, 3, 1))
        .render(query)
        .sql
    )
    assert later != first


# --- nothing past rendering --------------------------------------------------------


def test_rendering_never_constructs_a_sql_client(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("rendering reached for a SQL client")

    monkeypatch.setattr(SlayerQueryEngine, "client_for", refuse)
    monkeypatch.setattr(SlayerSQLClient, "__init__", refuse)

    assert renderer().render(QUERY).sql


def test_rendering_never_calls_a_storage_write(monkeypatch):
    """Every write raises, which a planning path could swallow; so each one is
    also recorded, and a render leaves the record empty."""

    attempted: list[str] = []

    def record(what: str):
        attempted.append(what)
        raise RuntimeError(what)

    monkeypatch.setattr(render, "_read_only", record)

    renderer().render(QUERY)
    assert attempted == []


_DATASOURCE = DatasourceConfig(name="wh", type="duckdb")

#: Every method that would persist, as the store's own overrides spell it.
_PERSISTING = [
    ("_save_model_impl", (None,), {}),
    ("_delete_model_row", (), {"data_source": "wh", "name": "orders"}),
    ("update_column_sampled", (), {"data_source": "wh", "model_name": "orders"}),
    ("_save_datasource_impl", (_DATASOURCE,), {}),
    ("_delete_datasource_row", ("wh",), {}),
    ("_set_datasource_priority_raw", ([],), {}),
    ("_save_memory_row", (None,), {}),
    ("_get_memory_row", ("1",), {}),
    ("_list_memories_rows", (), {}),
    ("_delete_memory_row", ("1",), {}),
    ("_next_memory_seq", (), {}),
    ("save_embedding", (None,), {}),
    ("get_embedding", (), {"canonical_id": "x", "embedding_model_name": "m"}),
    ("list_embeddings", (), {"embedding_model_name": "m"}),
    ("delete_embeddings_for_canonical", (), {"canonical_id_prefix": "wh.orders"}),
]


@pytest.mark.parametrize(("method", "args", "kwargs"), _PERSISTING)
def test_every_write_memory_and_embedding_method_raises(method, args, kwargs):
    storage = ValidatedModelStorage(loaded(model()), connector="duckdb")

    with pytest.raises(RuntimeError, match="read-only"):
        asyncio.run(getattr(storage, method)(*args, **kwargs))


def test_the_public_writes_reach_the_refusal_too():
    """SLayer's public wrappers validate and then call the overrides above."""

    storage = ValidatedModelStorage(loaded(model()), connector="duckdb")

    with pytest.raises(RuntimeError, match="read-only"):
        asyncio.run(storage.save_datasource(_DATASOURCE))
    with pytest.raises(RuntimeError, match="read-only"):
        asyncio.run(storage.delete_model("orders", data_source="wh"))


def test_the_read_only_store_overrides_every_abstract_persisting_method():
    """A write SLayer adds in a later release surfaces as an abstract method this
    class does not implement, and construction fails, rather than inheriting a
    default that persists."""

    assert ValidatedModelStorage.__abstractmethods__ == frozenset()


def test_rendering_never_opens_slayer_s_file_or_sqlite_storage(tmp_path):
    """In a fresh interpreter, so no other test's storage counts.

    SLayer's query engine imports its YAML storage at module load (since 1.6),
    so the property held is that rendering never constructs either backend,
    which is what would read or write a storage directory.
    """

    script = textwrap.dedent(
        """
        import yaml
        from slayer.storage.sqlite_storage import SQLiteStorage
        from slayer.storage.yaml_storage import YAMLStorage

        opened = []

        def record(name):
            def init(self, *args, **kwargs):
                opened.append(name)
                raise AssertionError(f"rendering opened {name}")
            return init

        YAMLStorage.__init__ = record("YAMLStorage")
        SQLiteStorage.__init__ = record("SQLiteStorage")

        from exmergo_dex_core.slayer import loader
        from exmergo_dex_core.slayer.render import SlayerRenderer

        model = {
            "version": loader.FORMAT_VERSION, "name": "orders", "data_source": "wh",
            "sql_table": "demo.main.orders",
            "columns": [
                {"name": "status", "type": "TEXT"},
                {"name": "amount", "type": "DOUBLE"},
            ],
            "measures": [{"name": "revenue", "formula": "amount:sum"}],
        }
        result = loader.validate_model_contents(
            {"models/wh/orders.yaml": yaml.safe_dump(model)}
        )
        query = {"source_model": "orders", "measures": ["revenue"]}
        SlayerRenderer(result.models, connector="duckdb").render(query)
        print(sorted(opened))
        """
    )
    run = subprocess.run(  # noqa: S603 - a fixed script, run by this interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert run.stdout.strip().splitlines()[-1] == "[]"


# --- the datasource is a dialect ---------------------------------------------------


def test_the_datasource_carries_a_name_and_a_type_and_nothing_else():
    storage = ValidatedModelStorage(loaded(model()), connector="snowflake")
    datasource = asyncio.run(storage.get_datasource("wh"))

    assert datasource.name == "wh"
    assert datasource.type == "snowflake"
    assert set(datasource.model_dump(exclude_defaults=True)) == {"name", "type"}
    assert asyncio.run(storage.get_datasource("elsewhere")) is None


def test_the_store_answers_only_for_the_models_it_was_given():
    storage = ValidatedModelStorage(loaded(model()), connector="duckdb")

    assert asyncio.run(storage.list_datasources()) == ["wh"]
    assert asyncio.run(storage._list_all_model_identities()) == [("wh", "orders")]
    assert asyncio.run(storage.get_datasource_priority()) == []
    assert asyncio.run(storage.get_model("missing")) is None
    found = asyncio.run(storage.get_model("orders"))
    found.columns.clear()
    assert asyncio.run(storage.get_model("orders", data_source="wh")).columns


def test_the_type_table_is_explicit_and_within_local_metricflow():
    """Every connector the table names renders in its own dialect, and every
    connector local MetricFlow renders for is either in it or excluded with a
    reason a refusal can quote."""

    assert all(SLAYER_TYPES[connector] == connector for connector in SLAYER_TYPES)
    assert set(SLAYER_TYPES) <= set(_RENDERERS)
    assert set(_RENDERERS) - set(SLAYER_TYPES) <= set(EXCLUDED)
    assert not set(SLAYER_TYPES) & set(EXCLUDED)


@pytest.mark.parametrize("connector", ["mysql", "", None])
def test_a_connector_outside_the_table_is_refused_by_name(connector):
    with pytest.raises(SemanticBackendError) as raised:
        SlayerRenderer(loaded(model()), connector=connector)

    assert f"connector '{connector}'" in str(raised.value)
    assert "Postgres fallback" in str(raised.value)
    assert reason_for(raised.value) is Reason.CONFIGURATION


@pytest.mark.parametrize(
    ("connector", "why"),
    [("clickhouse", "live client"), ("databricks", "TRUNC")],
)
def test_an_excluded_connector_is_refused_with_its_reason(connector, why):
    with pytest.raises(SemanticBackendError) as raised:
        SlayerRenderer(loaded(model()), connector=connector)

    assert f"connector '{connector}'" in str(raised.value)
    assert why in str(raised.value)
    assert "duckdb" in str(raised.value)
    assert reason_for(raised.value) is Reason.CONFIGURATION


def test_a_model_with_no_data_source_is_refused_naming_it_at_load():
    """SLayer's own validator is the first refusal, before a renderer exists."""

    orphan = model("refunds")
    del orphan["data_source"]

    result = validate_model_contents({"models/refunds.yaml": yaml.safe_dump(orphan)})

    assert result.models == []
    (error,) = result.errors
    assert error.file == "models/refunds.yaml"
    assert "'refunds'" in error.message and "data_source" in error.message


def test_a_model_with_no_data_source_is_refused_naming_it_by_the_renderer():
    """And the renderer's, for a model that reached it some other way: it has
    no dialect to give one, and SLayer keys a model by its data_source."""

    (orders,) = loaded(model())
    orphan = loader.LoadedModel(
        file="models/refunds.yaml",
        model=loaded(model("refunds"))[0].model.model_copy(
            update={"data_source": None}
        ),
    )

    with pytest.raises(SemanticBackendError) as raised:
        SlayerRenderer([orders, orphan], connector="duckdb")

    assert "'refunds' (models/refunds.yaml)" in str(raised.value)
    assert "`data_source`" in str(raised.value)
    assert "'orders'" not in str(raised.value)


# --- the source's one load ---------------------------------------------------------


def test_the_source_hands_the_renderer_the_models_it_already_loaded(
    tmp_path: Path, monkeypatch
):
    name = "models/wh/orders.yaml"
    path = tmp_path / BASE / name
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(model()), encoding="utf-8")
    source = SlayerSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector="duckdb",
            options={"base_dir": BASE, "models": [name]},
        )
    )
    reads: list[str] = []
    original = loader.load_models
    monkeypatch.setattr(
        loader, "load_models", lambda *a, **k: reads.append("load") or original(*a, **k)
    )

    source.semantic_catalog()
    models = source.validated_models()

    assert reads == ["load"]
    assert [m.model.name for m in models] == ["orders"]
    assert (
        "FROM demo.main.orders"
        in SlayerRenderer(models, connector="duckdb", now=NOW).render(QUERY).sql
    )
