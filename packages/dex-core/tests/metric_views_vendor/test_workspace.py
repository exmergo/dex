"""Databricks Metric Views read from Unity Catalog alone (#1364).

`semantic.deployment: databricks_workspace` reads the catalog from every Metric
View the source allowlist holds (`tables.list`, then `tables.get`), with no
repository file needed, and runs `query` and `values` through the same governed
spine as `local`: dex renders and executes, under the PII gate and the cost
guard. What changes is the trust: nothing is held to a reviewed file, and every
result says so. These run the real adapter and cost gate against the fake
workspace, through the CLI, and read its call and statement logs.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("databricks.sql")

from exmergo_dex_core.cli import main

from .workspace_fake import ORDERS, Workspace, route_to_fake_workspace

DISCLOSURE = "not checked against one"
WHERE = "shop.metrics.orders"

#: A second view only the workspace has: no repository file describes it.
RETURNS = {
    "version": 1.1,
    "source": "shop.core.orders",
    "fields": [{"name": "order_status", "expr": "o_orderstatus"}],
    "measures": [{"name": "returned_count", "expr": "COUNT(1)"}],
}


@pytest.fixture
def workspace(monkeypatch):
    """The fake workspace behind dex's adapter funnel."""
    return route_to_fake_workspace(monkeypatch)


def _repo(
    root: Path,
    files: dict[str, dict[str, Any]] | None = None,
    *,
    catalogs: str = "[shop.core, shop.metrics]",
    deployment: str = "databricks_workspace",
) -> Path:
    """A repository on the workspace deployment, with ``files`` (view name to
    body) configured beside it when given."""

    names = []
    for name, body in (files or {}).items():
        path = root / "views" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
        names.append(f"views/{name}.yaml")
    listed = (
        "  databricks:\n    files:" + "".join(f"\n      - {n}" for n in names) + "\n"
        if names
        else ""
    )
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: databricks\n"
        "databricks:\n"
        "  warehouse: fake-wh\n"
        f"  catalogs: {catalogs}\n"
        "budget:\n"
        "  ceiling: 3600\n"
        "  session_ceiling: 36000\n"
        "semantic:\n"
        "  vendor: databricks\n"
        f"  deployment: {deployment}\n" + listed,
        encoding="utf-8",
    )
    return root


def _run(capsys, root: Path, *argv: str) -> dict:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _list(capsys, root: Path, *argv: str) -> dict:
    return _run(capsys, root, "explore", "semantic", "list", *argv)


def _query(capsys, root: Path, *argv: str, confirm: bool = True) -> dict:
    flags = ["--confirm", "--budget", "3000"] if confirm else []
    return _run(capsys, root, "explore", "semantic", "query", *argv, *flags)


def _values(capsys, root: Path, *argv: str) -> dict:
    return _run(
        capsys,
        root,
        "explore",
        "semantic",
        "values",
        *argv,
        "--confirm",
        "--budget",
        "3000",
    )


def _no_sql_session(found: Workspace) -> None:
    assert found.fake.connect_count == 0, found.statements
    assert found.statements == []


def _reads(found: Workspace) -> list[str]:
    """Each view read back with `tables.get`, in order."""
    return [
        call.split(":", 1)[1]
        for call in found.workspace.metadata_calls
        if call.startswith("tables.get:")
    ]


def _member(body: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
    return next(m for m in body[kind] if m["name"] == name)


# --- the catalog ---------------------------------------------------------------------


def test_list_reads_every_view_in_unity_catalog_with_no_file(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={"shop.metrics.returns": RETURNS},
    )
    envelope = _list(capsys, _repo(tmp_path))

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert (data["deployment"], data["execution"]) == ("databricks_workspace", "dex")
    assert data["source"] == "account"
    assert data["coverage"] == [
        {"view": "orders", "deployed_as": WHERE, "coverage": "full"},
        {"view": "returns", "deployed_as": "shop.metrics.returns", "coverage": "full"},
    ]
    metrics = {m["name"] for m in data["metrics"]}
    assert {"orders.revenue", "returns.returned_count"} <= metrics
    # The relation each view sits on, read back.
    models = {m["name"]: m for m in data["semantic_models"]}
    assert models["orders"]["relation"] == "shop.core.orders"


def test_the_workspace_read_wakes_no_sql_warehouse(tmp_path, capsys, workspace):
    """`tables.list` and `tables.get` only: both Unity Catalog REST reads."""
    _list(capsys, _repo(tmp_path))

    found = workspace["workspace"]
    assert _reads(found) == [WHERE]
    _no_sql_session(found)


def test_a_view_this_identity_cannot_read_back_is_refused_in_coverage(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={"shop.metrics.returns": RETURNS},
        refused={"shop.metrics.returns": "PERMISSION_DENIED: no SELECT"},
    )
    envelope = _list(capsys, _repo(tmp_path))

    assert envelope["status"] == "ok", envelope["errors"]
    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["returns"]["coverage"] == "refused"
    assert coverage["returns"]["reasons"] == ["PERMISSION_DENIED: no SELECT"]
    assert coverage["orders"]["coverage"] == "full"
    assert not any(
        m["name"].startswith("returns.") for m in envelope["data"]["metrics"]
    )
    assert any("coverage is not full" in n for n in envelope["data"]["notes"])


def test_a_browse_only_view_is_refused_in_coverage(tmp_path, capsys, workspace):
    """An identity shown the view and not its definition reads nothing back."""
    workspace["workspace"] = Workspace(definitions={WHERE: None})
    envelope = _list(capsys, _repo(tmp_path))

    (coverage,) = envelope["data"]["coverage"]
    assert coverage["coverage"] == "refused"
    assert "not shown its definition" in coverage["reasons"][0]
    assert envelope["data"]["metrics"] == []


def _limited() -> dict[str, Any]:
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "net")["expr"] = "MEASURE(no_such) - SUM(o_discount)"
    return body


def test_a_permission_limited_read_back_is_limited(tmp_path, capsys, workspace):
    workspace["workspace"] = Workspace(definitions={WHERE: _limited()})
    data = _list(capsys, _repo(tmp_path))["data"]

    (coverage,) = data["coverage"]
    assert coverage["coverage"] == "limited"
    assert "measures['net']" in coverage["reasons"][0]
    names = {m["name"] for m in data["metrics"]}
    assert "orders.revenue" in names
    assert "orders.net" not in names


def test_a_query_on_a_limited_view_runs_and_says_so(tmp_path, capsys, workspace):
    workspace["workspace"] = Workspace(definitions={WHERE: _limited()})
    root = _repo(tmp_path)

    ran = _query(capsys, root, "orders.order_count")
    assert ran["status"] == "ok", ran["errors"]
    assert any("measures['net']" in n for n in ran["data"]["notes"])

    refused = _query(capsys, root, "orders.net")
    assert refused["status"] == "error"
    assert workspace["workspace"].queries == [workspace["workspace"].queries[0]]


def test_an_empty_scope_is_an_empty_catalog_that_says_so(tmp_path, capsys, workspace):
    """The allowlist's one schema holds tables and no Metric View."""
    workspace["workspace"] = Workspace(deployed=())
    envelope = _list(capsys, _repo(tmp_path, catalogs="[shop.core]"))

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["metrics"] == []
    assert envelope["data"].get("coverage", []) == []
    assert any("listed nothing" in n for n in envelope["data"]["notes"])
    _no_sql_session(workspace["workspace"])


def test_a_query_on_a_view_unity_catalog_does_not_hold_is_refused_by_name(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(deployed=())
    envelope = _query(capsys, _repo(tmp_path, catalogs="[shop.core]"), "orders.revenue")

    assert envelope["status"] == "error"
    assert "no metric view named 'orders'" in envelope["errors"][0]
    _no_sql_session(workspace["workspace"])


def _two_scopes() -> Workspace:
    """`orders` deployed twice, as a dev and a prod schema each hold their own."""
    return Workspace(deployed=(WHERE, "shop.staging.orders"))


def test_a_name_in_two_scopes_is_refused_for_each(tmp_path, capsys, workspace):
    workspace["workspace"] = _two_scopes()
    root = _repo(tmp_path, catalogs="[shop.core, shop.metrics, shop.staging]")
    envelope = _list(capsys, root)

    coverage = envelope["data"]["coverage"]
    assert [c["deployed_as"] for c in coverage] == [WHERE, "shop.staging.orders"]
    assert {c["coverage"] for c in coverage} == {"refused"}
    assert "--scope" in coverage[0]["reasons"][0]
    assert envelope["data"]["metrics"] == []
    assert _reads(workspace["workspace"]) == []

    refused = _query(capsys, root, "orders.revenue")
    assert refused["reason"] == "guard", refused
    assert "more than one allowlisted schema" in refused["errors"][0]
    _no_sql_session(workspace["workspace"])


def test_scope_narrows_a_name_in_two_scopes_to_one(tmp_path, capsys, workspace):
    workspace["workspace"] = _two_scopes()
    root = _repo(tmp_path, catalogs="[shop.core, shop.metrics, shop.staging]")
    envelope = _query(
        capsys,
        root,
        "orders.order_count",
        "--scope",
        "shop.staging",
        "--scope",
        "shop.core",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    (sent,) = workspace["workspace"].queries
    assert "`shop`.`staging`.`orders`" in sent


# --- every result says where its definition came from ---------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["list", "--deployed"],
        ["query", "orders.order_count", "--confirm", "--budget", "3000"],
        ["values", "orders.order_status", "--confirm", "--budget", "3000"],
    ],
    ids=["list", "list-deployed", "query", "values"],
)
def test_every_result_carries_the_account_source_disclosure(
    tmp_path, capsys, workspace, argv
):
    envelope = _run(capsys, _repo(tmp_path), "explore", "semantic", *argv)

    assert envelope["status"] == "ok", envelope["errors"]
    assert any(DISCLOSURE in w for w in envelope["warnings"]), envelope["warnings"]
    assert any("Unity Catalog" in w for w in envelope["warnings"])
    data = envelope["data"]
    assert data.get("definition_source", data.get("source")) == "account"
    assert data["deployment"] == "databricks_workspace"


def test_list_deployed_says_it_changes_nothing(tmp_path, capsys, workspace):
    envelope = _list(capsys, _repo(tmp_path), "--deployed")

    assert any("--deployed changes nothing" in n for n in envelope["data"]["notes"])


# --- a governed query --------------------------------------------------------------


def test_a_query_runs_through_dex_s_connector_unchecked(tmp_path, capsys, workspace):
    envelope = _query(
        capsys, _repo(tmp_path), "orders.order_count", "--group-by", "orders.segment"
    )

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert (data["vendor"], data["execution"]) == ("databricks", "dex")
    assert data["definition_source"] == "account"
    (sent,) = workspace["workspace"].queries
    assert "MEASURE(`order_count`)" in sent
    assert "FROM `shop`.`metrics`.`orders`" in sent
    assert any(f"deployed as {WHERE}" in n for n in data["notes"])


def test_the_order_is_list_then_read_back_then_the_statement(
    tmp_path, capsys, workspace
):
    found = workspace["workspace"]
    _query(capsys, _repo(tmp_path), "orders.revenue")

    calls = found.workspace.metadata_calls
    listed = next(i for i, c in enumerate(calls) if c.startswith("tables.list"))
    read = next(i for i, c in enumerate(calls) if c.startswith("tables.get:"))
    assert listed < read
    assert len(found.queries) == 1


def test_a_query_reads_back_only_the_view_it_names(tmp_path, capsys, workspace):
    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={"shop.metrics.returns": RETURNS},
    )
    _query(capsys, _repo(tmp_path), "orders.order_count")

    assert _reads(workspace["workspace"]) == [WHERE]


def test_unconfirmed_it_asks_and_opens_no_sql_session(tmp_path, capsys, workspace):
    """The cost handshake comes after the free Unity Catalog reads and before
    any statement."""

    envelope = _query(capsys, _repo(tmp_path), "orders.revenue", confirm=False)

    assert envelope["status"] == "needs_confirmation", envelope
    assert envelope["cost"]["estimate"] > 0
    assert _reads(workspace["workspace"]) == [WHERE]
    _no_sql_session(workspace["workspace"])


def test_values_of_a_personal_field_are_refused_before_any_sql(
    tmp_path, capsys, workspace
):
    """The token resolves only through the read-back, which is free REST
    metadata; no SQL warehouse is woken."""

    envelope = _values(capsys, _repo(tmp_path), "orders.customer_email")

    assert envelope["reason"] == "guard", envelope
    assert _reads(workspace["workspace"]) == [WHERE]
    _no_sql_session(workspace["workspace"])


def test_a_personal_group_by_is_refused_before_any_sql(tmp_path, capsys, workspace):
    envelope = _query(
        capsys,
        _repo(tmp_path),
        "orders.order_count",
        "--group-by",
        "orders.customer_email",
    )

    assert envelope["reason"] == "guard", envelope
    _no_sql_session(workspace["workspace"])


def test_values_scoped_by_a_metric(tmp_path, capsys, workspace):
    envelope = _values(
        capsys,
        _repo(tmp_path),
        "orders.order_status",
        "--metric",
        "orders.order_count",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["scoped_to"] == ["orders.order_count"]


def test_api_is_refused_since_dex_executes(tmp_path, capsys, workspace):
    envelope = _query(capsys, _repo(tmp_path), "orders.revenue", "--api")

    assert envelope["status"] == "error"
    assert workspace["workspace"].workspace.metadata_calls == []
    _no_sql_session(workspace["workspace"])


def test_another_connector_is_refused_by_name(tmp_path, capsys, workspace):
    root = _repo(tmp_path)
    config = root / ".dex" / "config.yml"
    config.write_text(
        config.read_text().replace("connector: databricks", "connector: duckdb"),
        encoding="utf-8",
    )
    envelope = _list(capsys, root)

    assert envelope["status"] == "error"
    assert "databricks_workspace" in envelope["errors"][0]
    assert workspace["workspace"].workspace.metadata_calls == []


# --- a configured file beside the workspace -------------------------------------------


def _diverged() -> dict[str, Any]:
    body = copy.deepcopy(ORDERS)
    _member(body, "measures", "revenue")["expr"] = "SUM(o_totalprice * 2)"
    return body


def test_a_file_describing_a_deployed_view_is_named_never_silently_passed_over(
    tmp_path, capsys, workspace
):
    workspace["workspace"] = Workspace(
        deployed=(WHERE, "shop.metrics.returns"),
        definitions={"shop.metrics.returns": RETURNS},
    )
    root = _repo(tmp_path, {"Orders": ORDERS})
    envelope = _list(capsys, root)

    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["orders"]["repository_file"] == "views/Orders.yaml"
    assert "repository_file" not in coverage["returns"]
    assert any("views/Orders.yaml" in n for n in envelope["data"]["notes"])

    queried = _query(capsys, root, "orders.order_count")
    assert queried["status"] == "ok", queried["errors"]
    assert any(
        "views/Orders.yaml" in n and "was not checked" in n
        for n in queried["data"]["notes"]
    )


def test_a_diverged_file_is_not_a_refusal_and_local_holds_it(
    tmp_path, capsys, workspace
):
    """Unity Catalog is the source of truth; `--local` is what holds it to the
    file."""

    workspace["workspace"] = Workspace(definitions={WHERE: _diverged()})
    root = _repo(tmp_path, {"orders": ORDERS})

    ran = _query(capsys, root, "orders.revenue")
    assert ran["status"] == "ok", ran["errors"]
    assert any("views/orders.yaml" in n for n in ran["data"]["notes"])

    held = _query(capsys, root, "orders.revenue", "--local")
    assert held["reason"] == "guard", held
    assert "differs from the repository" in held["errors"][0]
    assert len(workspace["workspace"].queries) == 1


def test_local_with_no_file_configured_says_so(tmp_path, capsys, workspace):
    envelope = _list(capsys, _repo(tmp_path), "--local")

    assert envelope["status"] == "error"
    assert "no semantic.databricks.files" in envelope["errors"][0]
    assert workspace["workspace"].workspace.metadata_calls == []


def test_a_local_configuration_never_contacts_unity_catalog(
    tmp_path, capsys, workspace
):
    root = _repo(tmp_path, {"orders": ORDERS}, deployment="local")
    envelope = _list(capsys, root)

    assert envelope["status"] == "ok"
    assert envelope["data"]["deployment"] == "local"
    assert "source" not in envelope["data"]
    assert not any(DISCLOSURE in w for w in envelope["warnings"])
    assert workspace["workspace"].workspace.metadata_calls == []
    _no_sql_session(workspace["workspace"])


# --- explore relationships, and the repository-only commands -------------------------


def test_relationships_fold_the_workspace_s_joins_under_the_hosted_flag(
    tmp_path, capsys, workspace
):
    """The edges `explore relationships|map --use-hosted-semantic-layer` fold
    in: the deployed views' joins, on the tables they join. Read free, through
    the listing and the read-back."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.commands import _native_semantic_edges

    engine = DexEngine.from_repo(_repo(tmp_path))
    identifiers = ["shop.core.orders", "shop.core.customer", "shop.core.nation"]
    bare, _ = _native_semantic_edges(engine, False, identifiers)
    edges, _ = _native_semantic_edges(
        engine, False, identifiers, use_hosted_semantic_layer=True
    )

    assert bare == []
    joined = {
        tuple(sorted((e.from_dataset.lower(), e.to_dataset.lower()))) for e in edges
    }
    assert ("shop.core.customer", "shop.core.orders") in joined
    assert ("shop.core.customer", "shop.core.nation") in joined
    _no_sql_session(workspace["workspace"])


def test_the_hosted_flag_folds_the_rely_keys(tmp_path, capsys, workspace):
    """A join's `rely.at_most_one_match` is a key on the joined table."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.semantic.backend import (
        resolve_hosted_semantic_layer,
    )

    engine = DexEngine.from_repo(_repo(tmp_path))
    keys, _ = resolve_hosted_semantic_layer(engine).declared_keys()

    assert any(
        (k.relation or "").lower() == "shop.core.customer"
        and k.column.lower() == "c_custkey"
        for k in keys
    ), keys


def test_the_fold_rides_the_outer_command_s_adapter_and_its_cost_gate(
    tmp_path, capsys, workspace
):
    """Opening an adapter through the engine rebuilds its cost gate, which
    would settle `explore relationships`' own gate early; the free read rides
    the open adapter."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.commands import _native_semantic_edges

    engine = DexEngine.from_repo(_repo(tmp_path))
    adapter = engine._adapter("explore relationships")
    gate = adapter.cost_gate
    edges, _ = _native_semantic_edges(
        engine,
        False,
        ["shop.core.orders", "shop.core.customer"],
        use_hosted_semantic_layer=True,
    )

    assert edges
    assert engine._adapter_instance is adapter
    assert adapter.cost_gate is gate


def test_maintain_reads_no_workspace_layer_and_says_so(tmp_path, capsys, workspace):
    """Like dbt Cloud, Unity Catalog is explore's alone: maintain reads the
    repository's files, and with none it compares nothing and says why."""

    from exmergo_dex_core.engine import DexEngine

    engine = DexEngine.from_repo(_repo(tmp_path))
    layer = engine.semantic_catalog_source().semantic_layer()

    assert layer.semantic_models == []
    assert any("databricks_workspace" in n for n in layer.notes)
    assert workspace["workspace"].workspace.metadata_calls == []


def test_authoring_with_no_file_configured_is_refused_by_name(
    tmp_path, capsys, workspace
):
    root = _repo(tmp_path)
    edits = tmp_path / "edits.json"
    edits.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "views/orders.yaml",
                        "kind": "semantic_document",
                        "content": yaml.safe_dump(ORDERS),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    envelope = _run(
        capsys, root, "semantic", "databricks", "define", "--edits-file", str(edits)
    )

    assert envelope["status"] == "error"
    assert "semantic.databricks.files" in " ".join(envelope["errors"])
    assert workspace["workspace"].workspace.metadata_calls == []
