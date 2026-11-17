"""Governed `explore semantic query|values` under `semantic.vendor: databricks` (#1362).

Dex renders `SELECT ... MEASURE(...) FROM <deployed view> GROUP BY ALL` from the
repository's catalog, finds the deployed view with a warehouse-free Unity Catalog
read, and executes through its own Databricks adapter. These run the real adapter
and cost gate against the behavioural fakes (`tests/fakes/databricks.py`),
through the CLI and through Python, and read the statement log: what was sent,
in what order, and that nothing reached the warehouse before a refusal.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("databricks.sql")

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine

from .conftest import view
from .workspace_fake import (
    ELSEWHERE,
    MATERIALIZED,
    ORDERS,
    PARAMS,
    Workspace,
    route_to_fake_workspace,
)


@pytest.fixture
def workspace(monkeypatch):
    """The fake workspace behind dex's adapter funnel."""
    return route_to_fake_workspace(monkeypatch)


def _repo(root: Path, connector: str = "databricks", **views: dict) -> Path:
    names = []
    for name, body in views.items():
        path = root / "views" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
        names.append(f"views/{name}.yaml")
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    listed = "".join(f"\n      - {name}" for name in names)
    (root / ".dex" / "config.yml").write_text(
        f"connector: {connector}\n"
        "databricks:\n"
        "  warehouse: fake-wh\n"
        "  catalogs: [shop.core, shop.metrics]\n"
        "budget:\n"
        "  ceiling: 3600\n"
        "  session_ceiling: 36000\n"
        "semantic:\n"
        "  vendor: databricks\n"
        "  databricks:\n"
        f"    files:{listed}\n",
        encoding="utf-8",
    )
    return root


def _run(capsys, root: Path, *argv: str) -> dict:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _query(capsys, root: Path, *argv: str, confirm: bool = True) -> dict:
    flags = ["--confirm", "--budget", "3000"] if confirm else []
    return _run(capsys, root, "explore", "semantic", "query", *argv, *flags)


def _values(capsys, root: Path, *argv: str, confirm: bool = True) -> dict:
    flags = ["--confirm", "--budget", "3000"] if confirm else []
    return _run(capsys, root, "explore", "semantic", "values", *argv, *flags)


def _nothing_reached_the_warehouse(found: Workspace) -> None:
    assert found.fake.connect_count == 0, found.statements
    assert found.statements == []


# --- a governed query, end to end ---------------------------------------------------


def test_a_measure_by_field_runs_through_dex_s_connector(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(
        capsys,
        root,
        "orders.order_count",
        "--group-by",
        "orders.order_status",
        "--order-by=-orders.order_count",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["columns"] == ["orders.order_status", "orders.order_count"]
    assert data["cells"] == [["F", 7]]
    assert (data["vendor"], data["execution"]) == ("databricks", "dex")
    (sent,) = workspace["workspace"].queries
    assert sent == (
        "SELECT `order_status` AS `orders.order_status`, "
        "MEASURE(`order_count`) AS `orders.order_count` "
        "FROM `shop`.`metrics`.`orders` GROUP BY ALL "
        "ORDER BY `orders.order_count` DESC LIMIT 51"
    )
    notes = " ".join(data["notes"])
    assert "deployed as shop.metrics.orders" in notes
    assert "#1363" in notes, "the unmade definition check is said, never implied"


def test_the_view_filter_is_databricks_and_not_repeated(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, "orders.revenue")

    assert envelope["status"] == "ok", envelope["errors"]
    (sent,) = workspace["workspace"].queries
    assert "o_orderstatus" not in sent and "WHERE" not in sent
    assert any("`filter`" in n for n in envelope["data"]["notes"])


def test_the_statement_runs_under_a_statement_timeout(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    _query(capsys, root, "orders.revenue")

    found = workspace["workspace"]
    sent = found.statements
    query = next(i for i, s in enumerate(sent) if "MEASURE(" in s)
    assert sent[query - 1].startswith("SET STATEMENT_TIMEOUT = ")
    assert found.connection.statements[query].session_timeout is not None


def test_unconfirmed_it_asks_and_opens_no_session(tmp_path, capsys, workspace):
    """The cost handshake comes before any statement, and finding the view is a
    Unity Catalog REST read: no SQL session is opened to ask."""

    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, "orders.revenue", confirm=False)

    assert envelope["status"] == "needs_confirmation", envelope
    found = workspace["workspace"]
    _nothing_reached_the_warehouse(found)
    listed = found.workspace.metadata_calls
    assert any(call.startswith("tables.list:shop.metrics") for call in listed)


def test_a_stopped_warehouse_is_priced_with_its_startup(tmp_path, capsys, monkeypatch):
    holder = route_to_fake_workspace(monkeypatch)
    root = _repo(tmp_path, orders=ORDERS)

    holder["workspace"] = Workspace(running=False)
    cold = _query(capsys, root, "orders.revenue", confirm=False)
    holder["workspace"] = Workspace(running=True)
    warm = _query(capsys, root, "orders.revenue", confirm=False)

    assert cold["cost"]["estimate"] > warm["cost"]["estimate"] > 0


def test_it_is_priced_by_the_base_tables_the_request_reaches(monkeypatch):
    """Databricks keeps no size for a Metric View, so the layer names the
    view's source and the join sources the members reach, and the adapter
    prices them, sharpened by any size learned in-budget."""

    holder = route_to_fake_workspace(monkeypatch)
    from exmergo_dex_core.adapters.databricks import DatabricksAdapter
    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    found = holder["workspace"]
    adapter = DatabricksAdapter(
        workspace=found.workspace,
        sql_connect=found.fake.sql_connect,
        cost_gate=CostGate(
            paradigm=Paradigm.COMPUTE_TIME,
            ceiling=600.0,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=True,
            connector="databricks",
        ),
        target=DatabricksTarget(warehouse="fake-wh", catalogs=["shop"]),
        clock=found.connection.clock,
    )
    floor = adapter.relations_estimate(["shop.core.orders"])
    adapter._entry("shop.core.orders")["byte_size"] = 400 * 1_000_000_000
    sized = adapter.relations_estimate(["SHOP.core.orders", "shop.core.nation"])

    assert sized > floor
    assert adapter.list_metric_views() == ["shop.metrics.orders"]
    assert "shop.metrics.orders" not in {m.identifier for m in adapter.list_objects()}
    assert found.fake.connect_count == 0


def test_the_base_tables_follow_the_joins_a_member_reads(tmp_path, workspace):
    from exmergo_dex_core.metric_views.query import base_tables, translate_query

    root = _repo(tmp_path, orders=ORDERS)
    loaded = DexEngine.from_repo(root).semantic_catalog_source().loaded()

    def reached(*group_by: str, measure: str = "orders.order_count") -> list[str]:
        plan = translate_query(loaded, metrics=[measure], group_by=list(group_by))
        return base_tables(plan.view, plan) or []

    assert reached() == ["shop.core.orders"]
    # A field read through the snowflake chain reaches every table on it.
    assert reached("orders.nation") == [
        "shop.core.orders",
        "shop.core.customer",
        "shop.core.nation",
    ]
    assert reached(measure="orders.last_email") == [
        "shop.core.orders",
        "shop.core.customer",
    ]


def test_a_grain_a_filter_and_a_joined_field(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    workspace["workspace"].rows = [
        {
            "orders.order_date__month": "2026-01-01",
            "orders.nation": "FR",
            "orders.revenue": 3,
        }
    ]
    envelope = _query(
        capsys,
        root,
        "orders.revenue",
        "--group-by",
        "orders.order_date__month,orders.nation",
        "--where",
        "order_status = 'F'",
        "--where",
        "orders.segment IN ('BUILDING', 'MACHINERY')",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["columns"] == [
        "orders.order_date__month",
        "orders.nation",
        "orders.revenue",
    ]
    (sent,) = workspace["workspace"].queries
    assert sent == (
        "SELECT DATE_TRUNC('MONTH', `order_date`) AS `orders.order_date__month`, "
        "`nation` AS `orders.nation`, MEASURE(`revenue`) AS `orders.revenue` "
        "FROM `shop`.`metrics`.`orders` "
        "WHERE (`order_status` = 'F') AND (`segment` IN ('BUILDING', 'MACHINERY')) "
        "GROUP BY ALL LIMIT 51"
    )


def test_a_ratio_and_a_derived_measure_are_composed_by_databricks(
    tmp_path, capsys, workspace
):
    root = _repo(tmp_path, orders=ORDERS)
    workspace["workspace"].rows = [{"orders.aov": 1.5, "orders.net": 9}]
    envelope = _query(capsys, root, "orders.aov,orders.net")

    assert envelope["status"] == "ok", envelope["errors"]
    (sent,) = workspace["workspace"].queries
    assert sent == (
        "SELECT MEASURE(`aov`) AS `orders.aov`, MEASURE(`net`) AS `orders.net` "
        "FROM `shop`.`metrics`.`orders` GROUP BY ALL LIMIT 51"
    )


def test_values_with_and_without_a_measure(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    workspace["workspace"].rows = [{"orders.priority": "1-URGENT"}]

    plain = _values(capsys, root, "orders.priority")
    scoped = _values(capsys, root, "orders.priority", "--metric", "orders.revenue")

    assert plain["status"] == "ok", plain["errors"]
    assert scoped["status"] == "ok", scoped["errors"]
    assert plain["data"]["columns"] == ["orders.priority"]
    assert plain["data"]["scoped_to"] == []
    assert scoped["data"]["scoped_to"] == ["orders.revenue"]
    first, second = workspace["workspace"].queries
    assert first == (
        "SELECT `priority` AS `orders.priority` FROM `shop`.`metrics`.`orders` "
        "GROUP BY ALL ORDER BY `orders.priority` LIMIT 51"
    )
    assert second == (
        "SELECT `orders.priority` FROM (SELECT `priority` AS `orders.priority`, "
        "MEASURE(`revenue`) AS `__m0` FROM `shop`.`metrics`.`orders` GROUP BY ALL) "
        "ORDER BY `orders.priority` LIMIT 51"
    )


def test_python_and_the_cli_give_the_same_answer(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    cli = _query(
        capsys, root, "orders.order_count", "--group-by", "orders.order_status"
    )

    engine = DexEngine.from_repo(root, budget=3000, confirmed=True)
    record = engine.semantic_query(
        "orders.order_count", group_by=["orders.order_status"]
    )

    assert record.columns == cli["data"]["columns"]
    assert record.cells == cli["data"]["cells"]
    first, second = workspace["workspace"].queries
    assert first == second


# --- refused before anything reaches the warehouse ----------------------------------


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["orders.running"], "window measure"),
        (["orders.running_share"], "reads running, which is a window measure"),
        (["orders.revenue", "--grain", "month"], "bare --grain"),
        (["orders.revenue", "--group-by", "orders.priority__month"], "date"),
        (["orders.revenue__month"], "orders.revenue is a measure"),
        (["orders.revenue", "--where", "revenue > 10"], "a measure"),
        (["orders.revenue", "--where", "MEASURE(revenue) > 10"], "MEASURE(revenue)"),
        (["orders.revenue", "--where", "o_custkey = 1"], "no such field"),
        (["orders.revenue", "--where", "order_status = :status"], "could not read"),
        (["orders.revenue", "--group-by", "orders.revenue"], "is a measure"),
        (["orders.order_status"], "is a field, not a measure"),
        (["orders.revenue", "--group-by", "orders.customer_email"], "PII"),
        (["orders.last_email"], "PII"),
        (["orders.latest_email"], "PII"),
        (["orders.revenue", "--where", "customer_email = 'x'"], "PII"),
        (["orders.revenue", "--group-by", "orders.contact"], "PII"),
        (["orders.revenue", "--where", "contact LIKE 'a%'"], "PII"),
        (["orders.revenue", "--order-by", "orders.priority"], "not a member"),
    ],
)
def test_a_refusal_comes_before_the_warehouse(tmp_path, capsys, workspace, argv, says):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, *argv)

    assert envelope["status"] == "error", envelope
    assert says in " ".join(envelope["errors"]), envelope["errors"]
    _nothing_reached_the_warehouse(workspace["workspace"])


@pytest.mark.parametrize(
    ("body", "says"),
    [(PARAMS, "declares parameters (region)"), (MATERIALIZED, "`materialization`")],
)
def test_a_view_v1_does_not_query_is_refused(tmp_path, capsys, workspace, body, says):
    root = _repo(tmp_path, orders=body)
    envelope = _query(capsys, root, "orders.order_count")

    assert envelope["status"] == "error"
    assert says in envelope["errors"][0]
    _nothing_reached_the_warehouse(workspace["workspace"])


def test_two_views_in_one_query_are_refused(tmp_path, capsys, workspace):
    second = view(
        "shop.core.customer",
        fields=[{"name": "segment", "expr": "c_mktsegment"}],
        measures=[{"name": "customers", "expr": "COUNT(1)"}],
    )
    root = _repo(tmp_path, orders=ORDERS, customers=second)
    envelope = _query(capsys, root, "orders.revenue", "--group-by", "customers.segment")

    assert "reads one Metric View" in envelope["errors"][0]
    _nothing_reached_the_warehouse(workspace["workspace"])


def test_a_query_with_no_measure_is_refused(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, "--group-by", "orders.order_status")

    assert envelope["status"] == "error"
    _nothing_reached_the_warehouse(workspace["workspace"])


def test_a_view_defined_only_in_the_repository_is_refused(
    tmp_path, capsys, monkeypatch
):
    holder = route_to_fake_workspace(monkeypatch)
    # The schema holds a Metric View, just not one of this name.
    holder["workspace"] = Workspace(deployed=("shop.metrics.returns",))
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, "orders.revenue")

    assert "defined only in the repository" in envelope["errors"][0]
    _nothing_reached_the_warehouse(holder["workspace"])


def test_a_view_deployed_twice_is_refused_and_scope_narrows_it(
    tmp_path, capsys, monkeypatch
):
    holder = route_to_fake_workspace(monkeypatch)
    holder["workspace"] = Workspace(
        deployed=("shop.metrics.orders", "shop.staging.orders")
    )
    root = _repo(tmp_path, orders=ORDERS)
    config = root / ".dex" / "config.yml"
    config.write_text(
        config.read_text().replace(
            "[shop.core, shop.metrics]", "[shop.core, shop.metrics, shop.staging]"
        ),
        encoding="utf-8",
    )

    ambiguous = _query(capsys, root, "orders.revenue")
    assert "more than one allowlisted schema" in ambiguous["errors"][0]
    assert "--scope <catalog>.<schema>" in ambiguous["errors"][0]
    _nothing_reached_the_warehouse(holder["workspace"])

    narrowed = _query(
        capsys,
        root,
        "orders.revenue",
        "--scope",
        "shop.metrics",
        "--scope",
        "shop.core",
    )
    assert narrowed["status"] == "ok", narrowed["errors"]


def test_a_base_table_outside_the_allowlist_is_refused(tmp_path, capsys, monkeypatch):
    holder = route_to_fake_workspace(monkeypatch)
    root = _repo(tmp_path, orders=ELSEWHERE)
    envelope = _query(capsys, root, "orders.entries")

    assert "outside the source allowlist" in envelope["errors"][0]
    assert "other.core.ledger" in envelope["errors"][0]
    _nothing_reached_the_warehouse(holder["workspace"])


def test_another_connector_is_refused_by_name(tmp_path, capsys, workspace):
    root = _repo(tmp_path, connector="duckdb", orders=ORDERS)
    envelope = _query(capsys, root, "orders.revenue")

    assert envelope["status"] == "error"
    assert "Databricks connector" in envelope["errors"][0]
    assert "duckdb" in envelope["errors"][0]


def test_values_of_a_personal_field_are_refused_before_the_warehouse(
    tmp_path, capsys, workspace
):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _values(capsys, root, "orders.customer_email")

    assert envelope["status"] == "error"
    assert envelope["reason"] == "guard", envelope
    _nothing_reached_the_warehouse(workspace["workspace"])


def test_a_field_over_two_tables_is_screened_through_both(tmp_path, capsys, workspace):
    """The catalog's lineage traces neither column of `place`; the gate reads
    them from its expression, and a neutral pair passes."""

    root = _repo(tmp_path, orders=ORDERS)
    workspace["workspace"].rows = [{"orders.place": "BUILDING/FR"}]

    passed = _values(capsys, root, "orders.place")
    refused = _values(capsys, root, "orders.contact")

    assert passed["status"] == "ok", passed["errors"]
    assert refused["status"] == "error"
    assert refused["reason"] == "guard", refused
    assert len(workspace["workspace"].queries) == 1


def _with_measures(**exprs: str) -> dict:
    body = copy.deepcopy(ORDERS)
    body["measures"].extend({"name": n, "expr": e} for n, e in exprs.items())
    return body


def test_a_measure_over_a_field_is_screened_through_its_columns(
    tmp_path, capsys, workspace
):
    """`MAX(contact)` returns the values of `contact`, a field over two tables
    the catalog's lineage traces neither column of: the gate reads its columns
    as it does when `contact` is grouped, so the personal one is refused."""

    root = _repo(
        tmp_path,
        orders=_with_measures(top_contact="MAX(contact)", top_place="MAX(place)"),
    )
    refused = _query(capsys, root, "orders.top_contact")
    assert refused["status"] == "error"
    assert refused["reason"] == "guard", refused
    assert "customer.c_email" in refused["errors"][0], refused["errors"]
    _nothing_reached_the_warehouse(workspace["workspace"])

    workspace["workspace"].rows = [{"orders.top_place": "BUILDING/FR"}]
    passed = _query(capsys, root, "orders.top_place")
    assert passed["status"] == "ok", passed["errors"]


def test_a_source_named_partially_is_priced_by_its_inventory_entry(
    tmp_path, capsys, workspace, monkeypatch
):
    """`core.orders` names `shop.core.orders`: the statement is priced by the
    inventory's identifier, which is what the adapter holds a size under."""

    from exmergo_dex_core.adapters.databricks import DatabricksAdapter

    priced: list[list[str]] = []
    estimate = DatabricksAdapter.relations_estimate

    def spy(self, identifiers):
        priced.append(list(identifiers))
        return estimate(self, identifiers)

    monkeypatch.setattr(DatabricksAdapter, "relations_estimate", spy)
    root = _repo(tmp_path, orders={**ORDERS, "source": "core.orders"})
    envelope = _query(capsys, root, "orders.order_count", confirm=False)

    assert envelope["status"] == "needs_confirmation", envelope["errors"]
    assert priced and priced[-1] == ["shop.core.orders"], priced
