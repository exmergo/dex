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
    assert built_models(summary) == {
        "model.shop.stg_orders": "stg_orders",
        "model.shop.fct_orders": "fct_orders",
    }


def test_a_summary_without_unique_ids_scopes_to_nothing():
    """A build from before ids were carried is not a build of every model."""

    assert built_models({"nodes": [{"name": "stg_orders", "status": "success"}]}) == {}


# --- versioned models ---------------------------------------------------------
#
# dbt writes a versioned model's id as `model.<package>.<name>.v<N>` and keeps
# its `name` across versions. The sweep's scope once read `v2` off the id, so a
# versioned model dropped out of row population and nothing said so.


def _versioned_project(project: Path) -> None:
    nodes = {
        "model.shop.fct_orders": {
            "name": "fct_orders",
            "resource_type": "model",
            "relation_name": '"warehouse"."dbt_dev"."fct_orders"',
            "compiled_code": 'select * from "warehouse"."raw"."orders"',
            "config": {"materialized": "table"},
        },
        **{
            f"model.shop.dim_customers.v{v}": {
                "name": "dim_customers",
                "resource_type": "model",
                "version": v,
                "latest_version": 2,
                "relation_name": f'"warehouse"."dbt_dev"."dim_customers_v{v}"',
                "compiled_code": 'select * from "warehouse"."raw"."customers"',
                "config": {"materialized": "table"},
            }
            for v in (1, 2)
        },
    }
    _artifacts(project, nodes=nodes, results=[])


def _versioned_warehouse(counts: dict[str, int]) -> _StubAdapter:
    """A free connector counts with one batched query, answered in the order
    the batch names its relations, which is sorted."""

    relations = [
        "warehouse.raw.orders",
        "warehouse.raw.customers",
        "warehouse.dbt_dev.fct_orders",
        "warehouse.dbt_dev.dim_customers_v1",
        "warehouse.dbt_dev.dim_customers_v2",
    ]
    return _StubAdapter(
        [_Meta(relation) for relation in relations],
        counts=dict(sorted(counts.items())),
    )


def test_a_versioned_model_the_build_ran_is_in_the_scope(tmp_path: Path):
    _versioned_project(tmp_path)
    summary = _summary(
        [
            {"unique_id": "model.shop.fct_orders", "status": "success"},
            {"unique_id": "model.shop.dim_customers.v2", "status": "success"},
        ]
    )
    adapter = _versioned_warehouse(
        {
            "warehouse.dbt_dev.dim_customers_v2": 40,
            "warehouse.dbt_dev.fct_orders": 100,
            "warehouse.raw.customers": 100,
            "warehouse.raw.orders": 100,
        }
    )
    result = _sweep(tmp_path, summary, adapter)
    assert result.scope == ["dim_customers.v2", "fct_orders"]
    assert "row_population" not in result.suppressed, result.suppressed
    assert [(f.code, f.identifier) for f in result.findings] == [
        ("row_loss", "dim_customers.v2")
    ]


def test_a_build_of_one_version_does_not_judge_the_other(tmp_path: Path):
    """Both versions share a name, which is why the scope is keyed by id: a
    build that ran v2 neither counts v1 nor names it."""

    _versioned_project(tmp_path)
    adapter = _versioned_warehouse(
        {"warehouse.dbt_dev.dim_customers_v2": 40, "warehouse.raw.customers": 100}
    )
    summary = _summary(
        [{"unique_id": "model.shop.dim_customers.v2", "status": "success"}]
    )
    result = _sweep(tmp_path, summary, adapter)
    assert result.scope == ["dim_customers.v2"]
    assert [f.identifier for f in result.findings] == ["dim_customers.v2"]
    assert not any("dim_customers_v1" in q for q in adapter.queries)
    assert not any("dim_customers.v1" in w for w in result.warnings)


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


def test_generated_sql_is_a_note_and_a_missing_parent_still_warns(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.metricflow_time_spine": {
                "name": "metricflow_time_spine",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."metricflow_time_spine"',
                "compiled_code": (
                    "select cast(range as date) as date_day "
                    "from range(date '2020-01-01', date '2030-01-01', interval 1 day)"
                ),
            },
            "model.shop.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."fct_orders"',
                "compiled_code": 'select * from "warehouse"."raw"."orders"',
            },
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )
    adapter = _StubAdapter(
        [_Meta("warehouse.dbt_dev.fct_orders")],
        counts={"warehouse.dbt_dev.fct_orders": 10},
    )
    result = _sweep(
        tmp_path,
        _summary(
            [
                _node("model.shop.metricflow_time_spine"),
                _node("model.shop.fct_orders"),
            ]
        ),
        adapter,
    )
    generated = (
        "row population was not compared for metricflow_time_spine "
        "(generated in SQL; no parent relation to compare against)"
    )
    assert result.notes == [generated]
    assert generated not in result.warnings
    assert any(
        "fct_orders (nothing built at warehouse.raw.orders)" in warning
        for warning in result.warnings
    )
    joined = " ".join(result.warnings)
    assert "nothing built at )" not in joined
    assert "nothing built at :" not in joined


def test_a_time_spine_alone_is_a_note(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.metricflow_time_spine": {
                "name": "metricflow_time_spine",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."metricflow_time_spine"',
                "compiled_code": (
                    "select cast(range as date) as date_day "
                    "from range(date '2020-01-01', date '2030-01-01', interval 1 day)"
                ),
            }
        },
        results=[
            {"unique_id": "model.shop.metricflow_time_spine", "status": "success"}
        ],
    )
    result = _sweep(
        tmp_path,
        _summary([_node("model.shop.metricflow_time_spine")]),
        _StubAdapter([]),
    )
    generated = (
        "row population was not compared for metricflow_time_spine "
        "(generated in SQL; no parent relation to compare against)"
    )
    assert result.notes == [generated]
    assert generated not in result.warnings
    assert result.suppressed["row_population"] == generated


def test_a_model_whose_final_cte_is_a_union_is_suppressed_with_reason(tmp_path: Path):
    sql = (
        "with named as (select 1 as id), "
        "residual as (select 2 as id), "
        "combined as ( "
        "    select id from named "
        "    union all "
        "    select id from residual "
        ") "
        "select * from combined"
    )
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_daily": {
                "name": "fct_daily",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."fct_daily"',
                "compiled_code": sql,
            }
        },
        results=[{"unique_id": "model.shop.fct_daily", "status": "success"}],
    )
    result = _sweep(
        tmp_path,
        _summary([_node("model.shop.fct_daily")]),
        _StubAdapter([]),
    )
    assert "row_population" in result.suppressed
    assert (
        "it is driven by a set operation, so it has no single parent"
        in result.suppressed["row_population"]
    )
    assert not any("nothing built at combined" in w for w in result.warnings)


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


# --- a listing that fails after the build -------------------------------------


def _built_with_a_warning(project: Path) -> dict:
    """One model the sweep can line up against its parent, and a test that
    warned, so a build-status finding exists whatever row population meets."""

    _artifacts(
        project,
        nodes={
            "model.shop.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."dbt_dev"."fct_orders"',
                "compiled_code": 'select * from "warehouse"."raw"."orders"',
                "config": {"materialized": "table"},
            },
            "test.shop.unique_fct_orders_id.abc123": {
                "name": "unique_fct_orders_id",
                "resource_type": "test",
            },
        },
        results=[
            {"unique_id": "model.shop.fct_orders", "status": "success"},
            {
                "unique_id": "test.shop.unique_fct_orders_id.abc123",
                "status": "warn",
                "message": "got 1 result, configured to warn if != 0",
            },
        ],
    )
    return _summary([_node("model.shop.fct_orders")])


class _ListingFails(_StubAdapter):
    def __init__(self, error: Exception):
        super().__init__([])
        self._error = error

    def list_objects(self, *, include_views: bool = True):
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        DexError("scope 'ANALYTICS.DBT_DEV' does not exist"),
        RuntimeError("the metadata service went away"),
    ],
    ids=["dex-error", "unexpected"],
)
def test_a_listing_that_fails_after_the_build_suppresses_rather_than_raises(
    tmp_path: Path, error: Exception
):
    """By the time the sweep lists, dbt has built and billed. A raise here would
    lose the build's own result, and its spend, to a failure in judging it."""

    summary = _built_with_a_warning(tmp_path)
    result = _sweep(tmp_path, summary, _ListingFails(error))
    assert result.ran is True
    reason = result.suppressed["row_population"]
    assert reason.startswith("the relations this build wrote could not be listed")
    assert type(error).__name__ in reason
    assert [f.code for f in result.findings] == ["node_warned"]


def test_a_cost_refusal_while_listing_is_still_raised(tmp_path: Path):
    summary = _built_with_a_warning(tmp_path)
    with pytest.raises(OverCeilingError):
        _sweep(
            tmp_path,
            summary,
            _ListingFails(OverCeilingError("estimated cost exceeds the ceiling")),
        )


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
    price, notes = price_verification(adapter, tmp_path, scope={"model.p.fct_orders"})
    assert price == 1024.0 and notes == []
    assert '"wh"."dbt_dev"."fct_orders"' in adapter.sql[0]
    assert adapter.sql[0].lstrip().upper().startswith("SELECT"), "aggregate only"

    _priceable_manifest(tmp_path, materialized="table")
    adapter = _Estimator()
    assert price_verification(adapter, tmp_path, scope={"model.p.fct_orders"}) == (
        0.0,
        [],
    )
    assert adapter.sql == [], "a table's count is free metadata, so nothing is priced"


def test_a_cold_dev_target_degrades_to_a_note_rather_than_failing(tmp_path: Path):
    """The first build of a project: the relations to count are not there yet to
    be dry-run against. The build must still be priced and still run."""

    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")
    adapter = _Estimator(raises=RuntimeError("Not found: Dataset wh:dbt_dev"))
    price, notes = price_verification(adapter, tmp_path, scope={"model.p.fct_orders"})
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

    assert price_verification(
        _NoEstimator(), tmp_path, scope={"model.p.fct_orders"}
    ) == (
        0.0,
        [],
    )


def test_a_scope_naming_nothing_prices_nothing(tmp_path: Path):
    from exmergo_dex_core.transform.verify import price_verification

    _priceable_manifest(tmp_path, materialized="view")
    adapter = _Estimator()
    assert price_verification(adapter, tmp_path, scope=set()) == (0.0, [])
    assert adapter.sql == []


# --- declared facts (#496) ----------------------------------------------------


def _declared_manifest(project: Path, *, relation: str = "wh.dbt_dev.fct_orders"):
    _artifacts(
        project,
        nodes={
            "model.p.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": relation,
                "config": {
                    "materialized": "table",
                    "meta": {
                        "dex": {
                            "grain": ["order_id", "line"],
                            "population": {
                                "filters": [
                                    {"column": "status", "excludes": ["cancelled"]}
                                ]
                            },
                        }
                    },
                },
                "columns": {
                    "share": {
                        "name": "share",
                        "config": {"meta": {"dex": {"unit": "fraction"}}},
                    }
                },
            }
        },
        results=[{"unique_id": "model.p.fct_orders", "status": "success"}],
    )


@dataclass
class _Maintain:
    grain_min_rows: int = 100


@dataclass
class _FactsConfig:
    query: _Query = field(default_factory=_Query)
    maintain: _Maintain = field(default_factory=_Maintain)
    pii_overrides: list = field(default_factory=list)


class _FactsEngine:
    config = _FactsConfig()


def test_a_build_checks_what_its_models_declare(tmp_path: Path):
    """End to end on a real relation: the declared exclusion is missing from the
    SQL, the share is a percentage, and the composite grain has a duplicate."""

    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    path = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA dbt_dev")
    conn.execute(
        "CREATE TABLE dbt_dev.fct_orders AS SELECT i AS order_id, 1 AS line, "
        "CASE WHEN i < 3 THEN 'cancelled' ELSE 'paid' END AS status, "
        "i * 10.0 AS share FROM range(10) t(i) UNION ALL SELECT 0, 1, 'paid', 0.5"
    )
    conn.close()
    _declared_manifest(tmp_path)
    adapter = DuckDBAdapter(path)
    try:
        result = verify_build(
            _FactsEngine(),
            tmp_path,
            _summary([_node("model.p.fct_orders")]),
            resolve_adapter=lambda: adapter,
        )
    finally:
        adapter.close()
    codes = sorted(f.code for f in result.findings)
    assert codes == ["grain_broken", "population_contradicted", "unit_out_of_range"]
    population = next(f for f in result.findings if f.code == "population_contradicted")
    assert population.data["contradicting_rows"] == 3
    grain = next(f for f in result.findings if f.code == "grain_broken")
    assert grain.data["source"] == "declaration"
    assert "grain" not in result.suppressed
    assert "unit" not in result.suppressed
    assert "population" not in result.suppressed
    assert "declares a column additive" in result.suppressed["additivity"]


def test_a_failed_build_suppresses_the_declared_facts_and_says_why(tmp_path: Path):
    _declared_manifest(tmp_path)
    result = _sweep(tmp_path, _summary([_node("model.p.fct_orders")], success=False))
    for finding_class in ("grain", "unit", "additivity", "population"):
        assert "did not complete" in result.suppressed[finding_class]


def test_a_build_with_nothing_declared_names_each_class(tmp_path: Path):
    _artifacts(
        tmp_path,
        nodes={
            "model.shop.fct_orders": {"name": "fct_orders", "resource_type": "model"}
        },
        results=[{"unique_id": "model.shop.fct_orders", "status": "success"}],
    )
    result = _sweep(tmp_path, _summary([_node("model.shop.fct_orders")]))
    assert "this build ran" in result.suppressed["unit"]
    assert "this build ran" in result.suppressed["population"]
    assert "unique test" in result.suppressed["grain"]


def test_declared_facts_are_priced_with_the_build(tmp_path: Path):
    from exmergo_dex_core.transform.verify import price_declared_facts

    _declared_manifest(tmp_path)
    adapter = _Estimator()
    adapter.cost_gate = object()
    price, notes = price_declared_facts(adapter, tmp_path, scope={"model.p.fct_orders"})
    assert price == 1024.0 and notes == []
    (sql,) = adapter.sql
    assert sql.startswith("SELECT COUNT(*) AS dex_rows")
    assert '"wh"."dbt_dev"."fct_orders"' in sql


def test_a_cold_dev_target_prices_the_declared_facts_later(tmp_path: Path):
    from exmergo_dex_core.transform.verify import price_declared_facts

    _declared_manifest(tmp_path)
    adapter = _Estimator(raises=RuntimeError("Not found: Dataset wh:dbt_dev"))
    adapter.cost_gate = object()
    price, notes = price_declared_facts(adapter, tmp_path, scope={"model.p.fct_orders"})
    assert price == 0.0
    assert "declared-fact checks upfront" in notes[0]
    assert "priced again after the build" in notes[0]


def test_declared_facts_that_do_not_fit_come_back_as_an_offer():
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.transform.verify import _facts_handshake

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

    offer = _facts_handshake(_Adapter())(50_000.0, 2)
    assert offer is not None
    assert offer.data["phase"] == "declared_facts"
    assert offer.data["per_table_bytes"] == {"(declared facts)": 50_000.0}
    assert offer.data["statement_count"] == 2
    assert "--verify --confirm --budget" in offer.data["hint"]


# --- #841: a build's own passing uniqueness test speaks for its fan-out ------------


def _fanned_project(project: Path, test_status: str) -> tuple[dict, _StubAdapter]:
    nodes = {
        "model.shop.fct_lines": {
            "name": "fct_lines",
            "resource_type": "model",
            "relation_name": '"warehouse"."dbt_dev"."fct_lines"',
            "compiled_code": (
                'select o.id, i.sku from "warehouse"."raw"."orders" o '
                'inner join "warehouse"."raw"."items" i on o.id = i.order_id'
            ),
            "config": {"materialized": "table"},
        },
        "test.shop.unique_fct_lines_line_id.1a2b": {
            "resource_type": "test",
            "attached_node": "model.shop.fct_lines",
            "test_metadata": {"name": "unique", "kwargs": {"column_name": "line_id"}},
        },
    }
    _artifacts(
        project,
        nodes=nodes,
        results=[
            {"unique_id": "model.shop.fct_lines", "status": "success"},
            {
                "unique_id": "test.shop.unique_fct_lines_line_id.1a2b",
                "status": test_status,
            },
        ],
    )
    summary = _summary(
        [
            _node("model.shop.fct_lines"),
            _node("test.shop.unique_fct_lines_line_id.1a2b", status=test_status),
        ]
    )
    adapter = _StubAdapter(
        [
            _Meta("warehouse.raw.orders"),
            _Meta("warehouse.raw.items"),
            _Meta("warehouse.dbt_dev.fct_lines"),
        ],
        counts={"warehouse.dbt_dev.fct_lines": 500, "warehouse.raw.orders": 100},
    )
    return summary, adapter


def test_a_passing_unique_test_caps_the_build_s_fanout_at_low(tmp_path: Path):
    summary, adapter = _fanned_project(tmp_path, "pass")
    result = _sweep(tmp_path, summary, adapter)
    (finding,) = [f for f in result.findings if f.code == "row_fanout"]
    assert finding.severity == "low"
    assert finding.data["grain_proven"] == {
        "columns": ["line_id"],
        "source": "unique_test",
    }


def test_a_failing_unique_test_proves_nothing(tmp_path: Path):
    summary, adapter = _fanned_project(tmp_path, "fail")
    result = _sweep(tmp_path, summary, adapter)
    (finding,) = [f for f in result.findings if f.code == "row_fanout"]
    assert finding.severity == "high"
    assert "grain_proven" not in finding.data
