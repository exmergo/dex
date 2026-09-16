"""The sweep `transform build --verify` folds onto a run, judged on its own.

Everything here is the orchestration and the suppression policy: which findings
a build's sweep is entitled to report, which it must decline to report and say
so, and what it does when the thing it needs is not there. The detectors
themselves are `maintain.verify`'s and are tested there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.errors import DexError
from exmergo_dex_core.guards.cost_guard import OverCeilingError
from exmergo_dex_core.transform.verify import (
    MAX_FINDINGS,
    built_models,
    dev_source_scope,
    verify_build,
)


@dataclass
class _Meta:
    identifier: str
    row_count: int | None = None


@dataclass
class _CountResult:
    """One row of ``dex_rows_<n>`` aliases, as the batched count returns it."""

    columns: list[str]
    cells: list[list[int]]


class _StubAdapter:
    """Enough adapter for the sweep: a dialect, an object list, no cost gate."""

    name = "duckdb"
    dialect = "duckdb"
    paradigm = Paradigm.FREE_LOCAL

    def __init__(self, objects: list[_Meta], counts: dict[str, int] | None = None):
        self._objects = objects
        self._counts = counts or {}
        self.queries: list[str] = []

    def list_objects(self, *, include_views: bool = True):
        return list(self._objects)

    def run_query(self, sql, max_rows=None, timeout_seconds=None):
        self.queries.append(sql)
        values = list(self._counts.values())
        return _CountResult(
            columns=[f"dex_rows_{i}" for i in range(len(values))], cells=[values]
        )


@dataclass
class _Query:
    timeout_seconds: float = 30.0


@dataclass
class _Config:
    query: _Query = field(default_factory=_Query)


class _Engine:
    """Only what the sweep reads off the engine, which is one timeout."""

    config = _Config()


def _artifacts(project: Path, nodes: dict, results: list[dict]) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    (target / "run_results.json").write_text(
        json.dumps({"results": results}), encoding="utf-8"
    )


def _summary(nodes: list[dict], *, success: bool = True) -> dict:
    return {"target": "dev", "success": success, "returncode": 0, "nodes": nodes}


def _node(unique_id: str, status: str = "success") -> dict:
    return {"unique_id": unique_id, "name": unique_id.split(".")[-1], "status": status}


def _sweep(project: Path, summary: dict, adapter=None, **kwargs):
    def opener():
        if adapter is None:
            raise DexError("no connection in this test")
        return adapter

    return verify_build(
        _Engine(),
        project,
        summary,
        resolve_adapter=kwargs.pop("resolve_adapter", opener),
    )


# --- scope --------------------------------------------------------------------


def test_the_scope_is_the_models_this_build_ran():
    summary = _summary(
        [
            _node("model.shop.stg_orders"),
            _node("model.shop.fct_orders"),
            _node("test.shop.not_null_stg_orders_id.3249b83c15", status="pass"),
            _node("snapshot.shop.snap_hosts"),
        ]
    )
    assert built_models(summary) == {"stg_orders", "fct_orders"}


def test_a_summary_without_unique_ids_scopes_to_nothing():
    """A build from before ids were carried is not a build of every model."""

    assert (
        built_models({"nodes": [{"name": "stg_orders", "status": "success"}]}) == set()
    )


# --- what always runs, and what never does ------------------------------------


def test_build_status_findings_are_free_and_need_no_connection(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"},
            "model.shop.dim_orders": {
                "name": "dim_orders",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.shop.fct_orders"]},
            },
        },
        results=[
            {
                "unique_id": "model.shop.fct_orders",
                "status": "error",
                "message": "boom",
            },
            {"unique_id": "model.shop.dim_orders", "status": "skipped"},
        ],
    )
    result = _sweep(
        tmp_path,
        _summary([_node("model.shop.fct_orders"), _node("model.shop.dim_orders")]),
    )
    codes = {f.code for f in result.findings}
    assert codes == {"node_failed", "node_skipped"}
    skipped = next(f for f in result.findings if f.code == "node_skipped")
    assert skipped.data["caused_by"] == "fct_orders"


def test_a_warning_node_is_reported_rather_than_dropped(tmp_path: Path):
    """The case the field report raised: seventeen warns and no way to name them."""

    _artifacts(
        tmp_path,
        nodes={
            "test.shop.relationships_orders_customer.abc123": {
                "name": "relationships_orders_customer",
                "resource_type": "test",
            }
        },
        results=[
            {
                "unique_id": "test.shop.relationships_orders_customer.abc123",
                "status": "warn",
                "message": "got 12 results, configured to warn if != 0",
            }
        ],
    )
    result = _sweep(tmp_path, _summary([]))
    warned = [f for f in result.findings if f.code == "node_warned"]
    assert len(warned) == 1
    assert warned[0].identifier == "relationships_orders_customer"
    assert warned[0].severity == "low"
    assert "12 results" in warned[0].detail


def test_no_relation_is_never_reported_from_a_build(tmp_path: Path):
    """dbt just said it built these; the catalog is not a better authority."""

    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"}
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )
    result = _sweep(tmp_path, _summary([_node("model.shop.fct_orders")]))
    assert not any(f.code == "no_relation" for f in result.findings)
    assert "run results are authoritative" in result.suppressed["no_relation"]


# --- suppression, each with its own reason ------------------------------------


def test_a_failed_build_suppresses_row_population_and_says_why(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"}
        },
        results=[
            {"unique_id": "model.shop.fct_orders", "status": "error", "message": "x"}
        ],
    )
    result = _sweep(
        tmp_path,
        _summary([_node("model.shop.fct_orders")], success=False),
    )
    assert "did not complete" in result.suppressed["row_population"]
    assert any(f.code == "node_failed" for f in result.findings)


def test_an_unreachable_warehouse_suppresses_rather_than_raises(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"}
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )
    result = _sweep(tmp_path, _summary([_node("model.shop.fct_orders")]))
    assert result.ran is True
    assert "warehouse unreachable" in result.suppressed["row_population"]


def test_a_build_that_ran_no_models_says_so(tmp_path: Path):
    _artifacts(tmp_path, nodes={}, results=[])
    result = _sweep(
        tmp_path, _summary([_node("test.shop.some_test.abc", status="pass")])
    )
    assert result.suppressed["row_population"] == "this build ran no models"


def test_a_dev_target_outside_the_read_scope_is_named_not_guessed_at(tmp_path: Path):
    """The metered default: dbt writes where dex is not looking.

    Without this the sweep would report every model as having no relation, or
    quietly compare nothing and look clean. Both are worse than saying it.
    """

    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."fct_orders"',
                "compiled_code": 'select * from "warehouse"."raw"."orders"',
                "config": {"materialized": "table"},
            }
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )
    adapter = _StubAdapter([_Meta("warehouse.raw.orders", 100)])
    result = _sweep(tmp_path, _summary([_node("model.shop.fct_orders")]), adapter)
    assert "outside dex's read scope" in result.suppressed["row_population"]
    assert not any(f.axis == "row_population" for f in result.findings)


# --- the cap, and the one thing that is allowed to escape ---------------------


def test_the_finding_cap_binds_and_says_that_it_did(tmp_path: Path):
    count = MAX_FINDINGS + 5
    nodes = {
        f"model.shop.m{i}": {"name": f"m{i}", "resource_type": "model"}
        for i in range(count)
    }
    results = [
        {"unique_id": f"model.shop.m{i}", "status": "error", "message": "boom"}
        for i in range(count)
    ]
    _artifacts(tmp_path, nodes=nodes, results=results)
    result = _sweep(
        tmp_path, _summary([_node(f"model.shop.m{i}") for i in range(count)])
    )
    assert len(result.findings) == MAX_FINDINGS
    assert any("further finding(s) are not listed" in w for w in result.warnings)


def test_a_cost_refusal_is_not_downgraded_to_a_warning(tmp_path: Path):
    """Every other failure here becomes a suppression reason. Not this one: a
    budget the caller set is theirs, and spending past it to finish a sweep is
    the one thing the sweep must not do."""

    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"}
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )

    def opener():
        raise OverCeilingError("estimated cost exceeds the ceiling")

    with pytest.raises(OverCeilingError):
        _sweep(
            tmp_path,
            _summary([_node("model.shop.fct_orders")]),
            resolve_adapter=opener,
        )


# --- the payload --------------------------------------------------------------


def test_a_sweep_that_did_not_run_says_so_positively():
    from exmergo_dex_core.transform.verify import BuildVerification

    payload = BuildVerification(ran=False, reason="not requested").payload()
    assert payload == {"ran": False, "reason": "not requested"}


def test_a_clean_sweep_is_distinguishable_from_one_that_never_ran(tmp_path: Path):
    _artifacts(tmp_path, nodes={}, results=[])
    payload = _sweep(tmp_path, _summary([])).payload()
    assert payload["ran"] is True
    assert payload["findings"] == []
    assert payload["finding_count"] == 0
    assert "row_population" in payload["suppressed"]


# --- the dev-namespace fold ---------------------------------------------------


def test_the_dev_scope_is_spelled_in_each_connector_s_own_vocabulary():
    """Snowflake and Databricks scope by the container above the schema, so a
    bare dev schema means nothing to them and the qualified pair is what goes
    into the allowlist."""

    from exmergo_dex_core.config import (
        BigQueryTarget,
        ClickHouseTarget,
        DatabricksTarget,
        DexConfig,
        PostgresTarget,
        SnowflakeTarget,
    )

    cases = {
        "bigquery": (
            DexConfig(bigquery=BigQueryTarget(dev_dataset="dbt_dev")),
            ("datasets", ["dbt_dev"]),
        ),
        "snowflake": (
            DexConfig(
                snowflake=SnowflakeTarget(
                    dev_database="ANALYTICS", dev_schema="DBT_DEV"
                )
            ),
            ("databases", ["ANALYTICS.DBT_DEV"]),
        ),
        "databricks": (
            DexConfig(
                databricks=DatabricksTarget(dev_catalog="main", dev_schema="dbt_dev")
            ),
            ("catalogs", ["main.dbt_dev"]),
        ),
        "postgres": (
            DexConfig(postgres=PostgresTarget(dev_schema="dbt_dev")),
            ("schemas", ["dbt_dev"]),
        ),
        "clickhouse": (
            DexConfig(clickhouse=ClickHouseTarget(dev_database="dbt_dev")),
            ("databases", ["dbt_dev"]),
        ),
    }
    for connector, (config, expected) in cases.items():
        assert dev_source_scope(config, connector) == expected, connector


def test_a_connector_with_no_dev_namespace_widens_nothing():
    from exmergo_dex_core.config import BigQueryTarget, DexConfig

    assert dev_source_scope(DexConfig(connector="duckdb"), "duckdb") is None
    assert dev_source_scope(DexConfig(bigquery=BigQueryTarget()), "bigquery") is None


def test_the_fold_extends_a_committed_allowlist_in_memory_only():
    """The committed allowlist is a cost boundary and a `--scope` flag may only
    narrow it. This is not a flag: it adds the one namespace the config itself
    names as the dev target, for one command, and writes nothing back."""

    from exmergo_dex_core.config import BigQueryTarget, DexConfig
    from exmergo_dex_core.transform.commands import _widen_scope_to_the_dev_target

    class _E:
        connector = "bigquery"
        config = DexConfig(
            connector="bigquery",
            bigquery=BigQueryTarget(
                project="p", datasets=["dex_ci"], dev_dataset="dbt_dev"
            ),
        )

    engine = _E()
    before = engine.config
    _widen_scope_to_the_dev_target(engine)
    assert engine.config.bigquery.datasets == ["dex_ci", "dbt_dev"]
    assert before.bigquery.datasets == ["dex_ci"], "the original config is untouched"


def test_the_fold_is_idempotent():
    from exmergo_dex_core.config import BigQueryTarget, DexConfig
    from exmergo_dex_core.transform.commands import _widen_scope_to_the_dev_target

    class _E:
        connector = "bigquery"
        config = DexConfig(
            connector="bigquery",
            bigquery=BigQueryTarget(
                project="p", datasets=["dex_ci", "dbt_dev"], dev_dataset="dbt_dev"
            ),
        )

    engine = _E()
    _widen_scope_to_the_dev_target(engine)
    assert engine.config.bigquery.datasets == ["dex_ci", "dbt_dev"]


def test_an_empty_allowlist_is_left_empty():
    """Empty already means every namespace this connection can see, so pinning
    it to the dev one would be a narrowing dressed as a widening."""

    from exmergo_dex_core.config import BigQueryTarget, DexConfig
    from exmergo_dex_core.transform.commands import _widen_scope_to_the_dev_target

    class _E:
        connector = "bigquery"
        config = DexConfig(
            connector="bigquery",
            bigquery=BigQueryTarget(project="p", dev_dataset="dbt_dev"),
        )

    engine = _E()
    _widen_scope_to_the_dev_target(engine)
    assert engine.config.bigquery.datasets == []


def test_counts_that_do_not_fit_the_ceiling_come_back_as_an_offer(monkeypatch):
    """The backstop, for the run where the upfront fold could not price them.

    The build is finished and billed by the time this fires, so the counts are
    offered rather than refused, and the envelope stays `ok`. Reporting
    `needs_confirmation` would tell a host the build had not run and invite it
    to pay for the whole thing again.
    """

    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.transform.verify import _count_handshake

    class _Adapter:
        name = "bigquery"
        dialect = "bigquery"
        paradigm = Paradigm.BYTES_SCANNED
        cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=1_000.0,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            command="transform build",
        )

    ask = _count_handshake(_Adapter())
    offer = ask(50_000.0, 3)
    assert offer is not None
    assert offer.data["phase"] == "row_population"
    assert offer.data["per_table_bytes"] == {"(row counts)": 50_000.0}
    assert offer.data["relation_count"] == 3
    assert "--verify --confirm --budget" in offer.data["hint"]
    assert any(
        "already run and billed" in n or "settled" in n for n in offer.data["notes"]
    )


def test_counts_that_fit_the_ceiling_are_taken_without_asking():
    """One confirmation covers both phases, which is the whole point of folding
    the count estimate into the build's own."""

    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.transform.verify import _count_handshake

    class _Adapter:
        name = "bigquery"
        dialect = "bigquery"
        paradigm = Paradigm.BYTES_SCANNED
        cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=1_000_000.0,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            command="transform build",
        )

    assert _count_handshake(_Adapter())(50_000.0, 3) is None


def test_a_free_connector_never_asks_about_counts():
    class _Adapter:
        name = "duckdb"
        dialect = "duckdb"
        paradigm = Paradigm.FREE_LOCAL

    from exmergo_dex_core.transform.verify import _count_handshake

    assert _count_handshake(_Adapter())(50_000.0, 3) is None


# --- the upfront price ---------------------------------------------------------


def _priceable_manifest(project: Path, *, materialized: str) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "sources": {
                    "source.p.raw.orders": {
                        "name": "orders",
                        "relation_name": "wh.raw.orders",
                    }
                },
                "nodes": {
                    "model.p.fct_orders": {
                        "name": "fct_orders",
                        "resource_type": "model",
                        "relation_name": "wh.dbt_dev.fct_orders",
                        "config": {"materialized": materialized},
                        "compiled_code": "select * from wh.raw.orders",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


class _Estimator:
    dialect = "duckdb"

    def __init__(self, price=1024.0, raises=None):
        self._price, self._raises, self.sql = price, raises, []

    def query_estimate(self, sql):
        self.sql.append(sql)
        if self._raises is not None:
            raise self._raises
        return self._price


def test_only_relations_the_warehouse_keeps_no_count_for_are_priced(tmp_path: Path):
    """Read from the declared materialization, not the catalog: at pricing time
    the relations may not exist yet, which is exactly when an upfront number is
    most useful."""

    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")
    adapter = _Estimator()
    price, notes = price_verification(adapter, tmp_path, scope={"fct_orders"})
    assert price == 1024.0 and notes == []
    assert '"wh"."dbt_dev"."fct_orders"' in adapter.sql[0]
    assert adapter.sql[0].lstrip().upper().startswith("SELECT"), "aggregate only"

    _priceable_manifest(tmp_path, materialized="table")
    adapter = _Estimator()
    assert price_verification(adapter, tmp_path, scope={"fct_orders"}) == (0.0, [])
    assert adapter.sql == [], "a table's count is free metadata, so nothing is priced"


def test_a_cold_dev_target_degrades_to_a_note_rather_than_failing(tmp_path: Path):
    """The first build of a project: the relations to count are not there yet to
    be dry-run against. The build must still be priced and still run."""

    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")
    adapter = _Estimator(raises=RuntimeError("Not found: Dataset wh:dbt_dev"))
    price, notes = price_verification(adapter, tmp_path, scope={"fct_orders"})
    assert price == 0.0
    assert len(notes) == 1
    assert "could not price this build's verification upfront" in notes[0]
    assert "priced again after the build" in notes[0]


def test_a_connector_with_no_dry_run_prices_nothing_and_says_nothing(tmp_path: Path):
    """Postgres has no estimator at all. Absent is not a degrade: there was
    never an upfront number to lose, and the phase check still gates it."""

    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")

    class _NoEstimator:
        dialect = "postgres"

    assert price_verification(_NoEstimator(), tmp_path, scope={"fct_orders"}) == (
        0.0,
        [],
    )


def test_a_scope_naming_nothing_prices_nothing(tmp_path: Path):
    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")
    adapter = _Estimator()
    assert price_verification(adapter, tmp_path, scope=set()) == (0.0, [])
    assert adapter.sql == []
