"""Deployed Metric Views, read back from Unity Catalog and held to the file (#1363).

The unit half reads a deployment through :mod:`...metric_views.deployed` with a
reader standing in for ``tables.get``: what Unity Catalog does on the way back
(reserialized YAML, a name's case, the body wrapped in its ``CREATE VIEW``) must
never read as a difference, and every change to what a query relies on must.

The end-to-end half runs `explore semantic list --deployed` and governed
queries against the fake workspace, through the real adapter and cost gate, and
reads the statement log: a stale deployment is refused before any SQL session
opens, and nothing is read back unless it was asked for.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.metric_views.deployed import (
    FULL,
    LIMITED,
    REFUSED,
    compare,
    read_deployed,
    relied_on,
)
from exmergo_dex_core.metric_views.loader import validate_view_contents

from .workspace_fake import ORDERS

WHERE = "shop.metrics.orders"


def _repository(body: dict[str, Any] = ORDERS):
    loaded = validate_view_contents({"views/orders.yaml": yaml.safe_dump(body)})
    assert loaded.errors == [], loaded.notes()
    return loaded.views[0]


def _read(body: dict[str, Any] | str):
    text = body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False)
    return read_deployed("orders", WHERE, lambda _: text)


def _differences(
    deployed: dict[str, Any] | str,
    names: list[str],
    grained: list[str] = (),
    repository: dict[str, Any] = ORDERS,
):
    mine = _repository(repository)
    read = _read(deployed)
    assert read.view is not None, read.coverage
    reliance = relied_on(
        mine,
        [mine.member(n) for n in names],
        [mine.member(n) for n in grained],
    )
    assert reliance is not None
    return compare(mine, read.view, reliance, unresolved=read.unresolved)


def _edited(**changes: Any) -> dict[str, Any]:
    body = copy.deepcopy(ORDERS)
    for key, value in changes.items():
        body[key] = value
    return body


def _member(body: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
    return next(m for m in body[kind] if m["name"] == name)


# --- normalization is no difference -------------------------------------------------


def test_the_same_definition_read_back_is_full_and_no_difference():
    read = _read(ORDERS)

    assert read.coverage.status == FULL
    assert read.view.name == "orders"
    assert read.view.file == WHERE
    everything = [m.name for m in _repository().fields] + [
        m.name for m in _repository().measures
    ]
    assert _differences(ORDERS, everything) == []


def test_reserialized_yaml_spacing_and_case_are_no_difference():
    """Unity Catalog may hand the body back reserialized: other key order and
    flow style, upper-cased keywords and names, extra whitespace. None of it
    changes what the view answers."""

    body = copy.deepcopy(ORDERS)
    body = {key: body[key] for key in reversed(list(body))}
    body["source"] = "SHOP.CORE.ORDERS"
    body["filter"] = "O_ORDERSTATUS   !=  'X'"
    _member(body, "measures", "revenue")["expr"] = "sum( O_TotalPrice )"
    _member(body, "measures", "aov")["expr"] = "MEASURE(Revenue)/MEASURE(ORDER_COUNT)"
    _member(body, "measures", "revenue")["name"] = "Revenue"
    join = body["joins"][0]
    join["on"] = "SOURCE.o_custkey=Customer.C_CUSTKEY"
    join["name"] = "CUSTOMER"
    _member(body, "fields", "nation")["expr"] = "CUSTOMER.NATION.N_NAME"
    text = yaml.safe_dump(body, default_flow_style=True, width=60)

    assert _differences(text, ["aov", "nation", "segment"]) == []


def test_a_body_wrapped_in_its_create_view_is_read_between_the_delimiters():
    body = yaml.safe_dump(ORDERS, sort_keys=False)
    wrapped = (
        f"CREATE OR REPLACE VIEW {WHERE} WITH METRICS LANGUAGE YAML AS $$\n{body}$$"
    )

    read = _read(wrapped)

    assert read.coverage.status == FULL
    assert _differences(wrapped, ["revenue"]) == []


def test_fields_spelled_as_dimensions_are_no_difference():
    body = copy.deepcopy(ORDERS)
    body["dimensions"] = body.pop("fields")

    assert _differences(body, ["order_status", "revenue"]) == []


# --- the read-back ----------------------------------------------------------------


def test_a_deployment_re_saved_in_another_spec_version_is_refused_naming_it():
    read = _read(_edited(version=0.1))

    assert read.coverage.status == REFUSED
    assert read.view is None
    (reason,) = read.coverage.reasons
    assert reason.startswith(f"{WHERE}: (root): [version]")
    assert "version 0.1 is not one dex reads" in reason


def test_a_refused_read_is_coverage_not_a_crash():
    def refuse(_: str) -> str:
        raise PermissionError("PERMISSION_DENIED: User does not have SELECT\nmore")

    read = read_deployed("orders", WHERE, refuse)

    assert read.coverage.status == REFUSED
    assert read.coverage.reasons == ["PERMISSION_DENIED: User does not have SELECT"]
    assert read.coverage.as_dict() == {
        "view": "orders",
        "deployed_as": WHERE,
        "coverage": "refused",
        "reasons": ["PERMISSION_DENIED: User does not have SELECT"],
    }


def test_a_read_back_with_a_refused_element_is_limited_and_it_is_unreadable():
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "net")["expr"] = "MEASURE(no_such) - SUM(o_discount)"

    read = _read(body)

    assert read.coverage.status == LIMITED
    assert any("measures['net']" in reason for reason in read.coverage.reasons)
    (difference,) = _differences(body, ["net"])
    assert difference.field == "unreadable"
    assert "could not be compared" in difference.render()
    assert _differences(body, ["revenue"]) == [], "the rest still compares"


# --- each stale-definition refusal --------------------------------------------------


def _fields(differences) -> list[tuple[str, str]]:
    return [(d.element, d.field) for d in differences]


def test_an_edited_measure_expression_is_a_difference_naming_it():
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["expr"] = "SUM(o_totalprice * 2)"

    differences = _differences(body, ["revenue"])

    assert _fields(differences) == [("orders.revenue", "expr")]
    assert "SUM(o_totalprice * 2)" in differences[0].render()


def test_a_measure_read_through_measure_is_held_too():
    """`aov` is `MEASURE(revenue) / MEASURE(order_count)`: a query of `aov`
    relies on `revenue`'s expression, so editing it refuses that query."""

    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["expr"] = "SUM(o_totalprice * 2)"

    assert _fields(_differences(body, ["aov"])) == [("orders.revenue", "expr")]
    assert _differences(body, ["order_count"]) == [], "unrelated measures still run"


def test_a_field_read_by_another_field_is_held_too():
    base = copy.deepcopy(ORDERS)
    base["fields"].append(
        {"name": "status_label", "expr": "CONCAT('s-', order_status)"}
    )
    deployed = copy.deepcopy(base)
    _member(deployed, "fields", "order_status")["expr"] = "lower(o_orderstatus)"

    differences = _differences(deployed, ["status_label"], repository=base)

    assert _fields(differences) == [("orders.order_status", "expr")]


def test_the_source_and_the_filter_are_held():
    assert _fields(
        _differences(_edited(source="shop.core.orders_v2"), ["revenue"])
    ) == [("view orders", "source")]
    assert _fields(
        _differences(_edited(filter="o_orderstatus = 'F'"), ["revenue"])
    ) == [("view orders", "filter")]
    assert _fields(_differences(_edited(filter=None), ["revenue"])) == [
        ("view orders", "filter")
    ]


def test_a_materialization_or_parameters_gained_there_are_held():
    gained = _edited(
        materialization={
            "mode": "relaxed",
            "materialized_views": [{"name": "d", "type": "unaggregated"}],
        }
    )
    assert _fields(_differences(gained, ["revenue"])) == [
        ("view orders", "materialization")
    ]


def test_the_format_is_held_only_where_a_grain_reads_it():
    body = copy.deepcopy(ORDERS)
    _member(body, "fields", "order_date")["format"] = {"type": "number"}

    assert _differences(body, ["order_date", "revenue"]) == []
    assert _fields(_differences(body, ["order_date", "revenue"], ["order_date"])) == [
        ("orders.order_date", "format.type")
    ]


def test_a_display_only_change_is_no_difference():
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["comment"] = "Gross revenue, renamed."
    _member(body, "measures", "revenue")["display_name"] = "Gross"
    body["comment"] = "Another description."

    assert _differences(body, ["revenue"]) == []


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        (lambda j: j.update(on="source.o_custkey = customer.c_altkey"), "on"),
        (lambda j: j.update(source="shop.core.customer_v2"), "source"),
        (
            lambda j: j.update(rely={"at_most_one_match": False}),
            "rely.at_most_one_match",
        ),
        (lambda j: j.pop("rely"), "rely.at_most_one_match"),
    ],
)
def test_a_join_a_member_reads_through_is_held(edit, field):
    body = copy.deepcopy(ORDERS)
    edit(body["joins"][0])

    assert _fields(_differences(body, ["segment", "revenue"])) == [
        ("join customer", field)
    ]
    assert _differences(body, ["order_status", "revenue"]) == [], (
        "a join nothing in the query reads is not held"
    )


def test_a_nested_join_holds_its_parent_too():
    body = copy.deepcopy(ORDERS)
    body["joins"][0]["on"] = "source.o_custkey = customer.c_altkey"

    assert ("join customer", "on") in _fields(_differences(body, ["nation"]))


def test_a_join_written_with_using_is_held():
    base = copy.deepcopy(ORDERS)
    base["joins"][0] = {
        "name": "customer",
        "source": "shop.core.customer",
        "using": ["c_custkey"],
    }
    base["fields"] = [f for f in base["fields"] if "nation" not in f["expr"]]
    deployed = copy.deepcopy(base)
    deployed["joins"][0]["using"] = ["c_altkey"]

    assert _fields(_differences(deployed, ["segment"], repository=base)) == [
        ("join customer", "using")
    ]
    deployed["joins"][0]["using"] = ["C_CUSTKEY"]
    assert _differences(deployed, ["segment"], repository=base) == []


def test_a_member_missing_from_the_deployment_is_named():
    body = copy.deepcopy(ORDERS)
    body["measures"] = [m for m in body["measures"] if m["name"] != "biggest"]

    (difference,) = _differences(body, ["biggest"])
    assert (difference.element, difference.field) == ("orders.biggest", "missing")
    assert difference.render() == "orders.biggest is not in the deployed view"


def test_a_window_added_there_is_a_difference():
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["window"] = [
        {"order": "order_date", "range": "trailing 7 day"}
    ]

    assert _fields(_differences(body, ["revenue"])) == [("orders.revenue", "window")]


# --- end to end on the fake workspace ----------------------------------------------

pytest.importorskip("databricks.sql")

from exmergo_dex_core.cli import main  # noqa: E402

from .workspace_fake import Workspace, route_to_fake_workspace  # noqa: E402


@pytest.fixture
def workspace(monkeypatch):
    """The fake workspace behind dex's adapter funnel."""
    return route_to_fake_workspace(monkeypatch)


def _repo(root: Path, **views: dict) -> Path:
    names = []
    for name, body in views.items():
        path = root / "views" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
        names.append(f"views/{name}.yaml")
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    listed = "".join(f"\n      - {name}" for name in names)
    (root / ".dex" / "config.yml").write_text(
        "connector: databricks\n"
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


def _query(capsys, root: Path, *argv: str) -> dict:
    return _run(
        capsys,
        root,
        "explore",
        "semantic",
        "query",
        *argv,
        "--confirm",
        "--budget",
        "3000",
    )


def _no_sql_session(found: Workspace) -> None:
    assert found.fake.connect_count == 0, found.statements
    assert found.statements == []


def _diverged() -> dict[str, Any]:
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["expr"] = "SUM(o_totalprice * 2)"
    return body


RETURNS = {
    "version": 1.1,
    "source": "shop.core.orders",
    "fields": [{"name": "order_status", "expr": "o_orderstatus"}],
    "measures": [{"name": "order_count", "expr": "COUNT(1)"}],
}


def test_list_deployed_reads_unity_catalog_and_names_its_coverage(
    tmp_path, capsys, workspace
):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _run(capsys, root, "explore", "semantic", "list", "--deployed")

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["source"] == "deployed"
    assert data["coverage"] == [
        {"view": "orders", "deployed_as": WHERE, "coverage": "full"}
    ]
    assert "orders.revenue" in {m["name"] for m in data["metrics"]}
    found = workspace["workspace"]
    assert f"tables.get:{WHERE}" in found.workspace.metadata_calls
    _no_sql_session(found)


def test_list_deployed_answers_for_an_unchanged_and_a_diverged_view(
    tmp_path, capsys, workspace
):
    """The dogfood's first step: both answer, since a list holds nothing to the
    file; the diverged one's catalog is what is deployed."""

    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={WHERE: _diverged(), "shop.metrics.returns": RETURNS},
    )
    root = _repo(tmp_path, orders=ORDERS, returns=RETURNS)
    data = _run(capsys, root, "explore", "semantic", "list", "--deployed")["data"]

    assert {c["view"]: c["coverage"] for c in data["coverage"]} == {
        "orders": "full",
        "returns": "full",
    }
    revenue = next(m for m in data["measures"] if m["name"] == "revenue")
    assert revenue["expr"] == "SUM(o_totalprice * 2)"


def test_list_deployed_names_a_refused_a_browse_only_and_a_missing_view(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={"shop.metrics.returns": None},
        refused={WHERE: "PERMISSION_DENIED: User does not have USE SCHEMA"},
    )
    missing = copy.deepcopy(RETURNS)
    root = _repo(tmp_path, orders=ORDERS, returns=RETURNS, refunds=missing)
    envelope = _run(capsys, root, "explore", "semantic", "list", "--deployed")

    assert envelope["status"] == "ok", envelope["errors"]
    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["orders"]["coverage"] == "refused"
    assert coverage["orders"]["reasons"] == [
        "PERMISSION_DENIED: User does not have USE SCHEMA"
    ]
    assert coverage["returns"]["coverage"] == "refused"
    assert "not shown its definition" in coverage["returns"]["reasons"][0]
    assert coverage["refunds"]["coverage"] == "refused"
    assert coverage["refunds"]["deployed_as"] is None
    assert "defined only in the repository" in coverage["refunds"]["reasons"][0]
    assert envelope["data"]["metrics"] == [], "nothing refused is presented as read"
    assert any("coverage is not full" in n for n in envelope["data"]["notes"])


def test_list_deployed_names_a_limited_view(tmp_path, capsys, workspace):
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "net")["expr"] = "MEASURE(no_such) - SUM(o_discount)"
    workspace["workspace"] = Workspace(definitions={WHERE: body})
    root = _repo(tmp_path, orders=ORDERS)
    data = _run(capsys, root, "explore", "semantic", "list", "--deployed")["data"]

    (coverage,) = data["coverage"]
    assert coverage["coverage"] == "limited"
    assert "measures['net']" in coverage["reasons"][0]
    names = {m["name"] for m in data["metrics"]}
    assert "orders.revenue" in names
    assert "orders.net" not in names


def test_no_deployed_read_happens_unless_it_was_asked_for(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _run(capsys, root, "explore", "semantic", "list")

    assert envelope["status"] == "ok"
    assert "source" not in envelope["data"]
    assert "coverage" not in envelope["data"]
    found = workspace["workspace"]
    assert found.workspace.metadata_calls == []
    _no_sql_session(found)


def test_list_deployed_on_another_connector_is_refused(tmp_path, capsys):
    root = _repo(tmp_path, orders=ORDERS)
    config = root / ".dex" / "config.yml"
    config.write_text(
        config.read_text().replace("connector: databricks", "connector: duckdb"),
        encoding="utf-8",
    )
    envelope = _run(capsys, root, "explore", "semantic", "list", "--deployed")

    assert envelope["status"] == "error"
    assert "without --deployed" in envelope["errors"][0]


def test_a_query_on_the_unchanged_view_runs(tmp_path, capsys, workspace):
    root = _repo(tmp_path, orders=ORDERS)
    envelope = _query(capsys, root, "orders.aov", "--group-by", "orders.segment")

    assert envelope["status"] == "ok", envelope["errors"]
    assert len(workspace["workspace"].queries) == 1


def test_a_query_naming_the_diverged_measure_is_refused_before_the_warehouse(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(definitions={WHERE: _diverged()})
    root = _repo(tmp_path, orders=ORDERS)

    envelope = _query(capsys, root, "orders.revenue")

    assert envelope["status"] == "error"
    assert envelope["reason"] == "guard", envelope
    (error,) = envelope["errors"]
    assert (
        f"deployed as {WHERE} differs from the repository's views/orders.yaml" in error
    )
    assert "orders.revenue has a different expr there" in error
    _no_sql_session(workspace["workspace"])

    # A measure built on it through MEASURE() is refused for the same reason;
    # one that reads nothing diverged still runs on the same deployment.
    assert "orders.revenue" in _query(capsys, root, "orders.aov")["errors"][0]
    _no_sql_session(workspace["workspace"])
    ran = _query(capsys, root, "orders.order_count")
    assert ran["status"] == "ok", ran["errors"]


def test_values_are_held_to_the_file_too(tmp_path, capsys, workspace):
    body = copy.deepcopy(ORDERS)
    _member(body, "fields", "order_status")["expr"] = "lower(o_orderstatus)"
    workspace["workspace"] = Workspace(definitions={WHERE: body})
    root = _repo(tmp_path, orders=ORDERS)

    envelope = _run(
        capsys,
        root,
        "explore",
        "semantic",
        "values",
        "orders.order_status",
        "--confirm",
        "--budget",
        "3000",
    )

    assert envelope["reason"] == "guard", envelope
    assert "orders.order_status has a different expr" in envelope["errors"][0]
    _no_sql_session(workspace["workspace"])


@pytest.mark.parametrize(
    ("definitions", "refused", "says"),
    [
        ({WHERE: None}, None, "not shown its definition"),
        (None, {WHERE: "PERMISSION_DENIED: no"}, "PERMISSION_DENIED: no"),
        ({WHERE: _edited(version=0.1)}, None, "version 0.1 is not one dex reads"),
    ],
    ids=["browse_only", "refused", "another_spec_version"],
)
def test_a_deployment_that_cannot_be_read_back_refuses_the_query(
    tmp_path, capsys, workspace, definitions, refused, says
):
    workspace["workspace"] = Workspace(definitions=definitions, refused=refused)
    root = _repo(tmp_path, orders=ORDERS)

    envelope = _query(capsys, root, "orders.revenue")

    assert envelope["reason"] == "guard", envelope
    (error,) = envelope["errors"]
    assert "could not be read back" in error
    assert says in error
    _no_sql_session(workspace["workspace"])


def test_a_grain_holds_the_field_s_format(tmp_path, capsys, workspace):
    body = copy.deepcopy(ORDERS)
    _member(body, "fields", "order_date")["format"] = {"type": "number"}
    workspace["workspace"] = Workspace(definitions={WHERE: body})
    root = _repo(tmp_path, orders=ORDERS)

    grained = _query(
        capsys, root, "orders.revenue", "--group-by", "orders.order_date__month"
    )
    assert "orders.order_date has a different format.type" in grained["errors"][0]
    plain = _query(capsys, root, "orders.revenue", "--group-by", "orders.order_date")
    assert plain["status"] == "ok", plain["errors"]
