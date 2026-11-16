"""Snowflake Semantic Views read from the account alone (#1357).

`semantic.deployment: snowflake_account` reads the catalog from every semantic view
the source allowlist holds (SHOW, then `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`), with
no repository file needed, and runs `query` and `values` through the same governed
spine as `local`: dex renders and executes, under the PII gate and the cost guard.
What changes is the trust: nothing is held to a reviewed file, and every result
says so. These run the real adapter and cost gate against the behavioural fake
connection, through the CLI, and read its statement log.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("snowflake.connector")

from exmergo_dex_core.cli import main

from .account_fake import SALES, Account
from .conftest import sv, table

DISCLOSURE = "not checked against one"

#: A second view only the account has: no repository file describes it.
RETURNS = sv(
    "returns",
    table(
        "returned",
        "SHOP.PUBLIC.ORDERS",
        ["o_orderkey"],
        dimensions=[{"name": "status", "expr": "o_orderstatus"}],
        metrics=[{"name": "returned_count", "expr": "COUNT(o_orderkey)"}],
    ),
)


def _repo(
    root: Path,
    *files: dict,
    databases: str = "[SHOP.PUBLIC]",
    deployment: str = "snowflake_account",
) -> Path:
    """A repository on the account deployment, with ``files`` configured beside
    it when given."""

    names = []
    for view in files:
        path = root / "views" / f"{view['name']}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(view, sort_keys=False), encoding="utf-8")
        names.append(f"views/{view['name']}.yaml")
    listed = (
        "  snowflake:\n    files:" + "".join(f"\n      - {n}" for n in names) + "\n"
        if names
        else ""
    )
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: snowflake\n"
        "snowflake:\n"
        "  warehouse: DEX_WH\n"
        f"  databases: {databases}\n"
        "budget:\n"
        "  ceiling: 3600\n"
        "  session_ceiling: 36000\n"
        "semantic:\n"
        "  vendor: snowflake\n"
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


# --- the catalog ---------------------------------------------------------------------


def test_list_reads_every_view_on_the_account_with_no_file(tmp_path, capsys, account):
    account["account"] = Account(views={"SHOP.PUBLIC.RETURNS": RETURNS})
    envelope = _list(capsys, _repo(tmp_path))

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert (data["deployment"], data["execution"]) == ("snowflake_account", "dex")
    assert data["source"] == "account"
    assert data["coverage"] == [
        {"view": "RETURNS", "deployed_as": "SHOP.PUBLIC.RETURNS", "coverage": "full"},
        {"view": "SALES", "deployed_as": "SHOP.PUBLIC.SALES", "coverage": "full"},
    ]
    metrics = {m["name"].lower() for m in data["metrics"]}
    assert {"sales.orders.order_count", "returns.returned.returned_count"} <= metrics
    # Snowflake's own spelling: no file lends this view another one.
    assert "SALES.ORDERS.ORDER_COUNT" in {m["name"] for m in data["metrics"]}
    # The relation each logical table sits on, read back.
    models = {m["name"].lower(): m for m in data["semantic_models"]}
    assert models["sales.orders"]["relation"].upper() == "SHOP.PUBLIC.ORDERS"


def test_the_account_read_spends_no_warehouse_time(tmp_path, capsys, account):
    """SHOW and the read-back only: both cloud-services reads."""
    _list(capsys, _repo(tmp_path))

    sent = account["account"].sent
    assert sent
    assert all(s.startswith("SHOW") or "READ_YAML" in s for s in sent), sent
    assert account["account"].queries == []


def test_a_view_this_role_cannot_read_back_is_refused_in_coverage(
    tmp_path, capsys, account
):
    account["account"] = Account(
        views={"SHOP.PUBLIC.RETURNS": RETURNS}, unreadable={"SHOP.PUBLIC.RETURNS"}
    )
    envelope = _list(capsys, _repo(tmp_path))

    assert envelope["status"] == "ok", envelope["errors"]
    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["RETURNS"]["coverage"] == "refused"
    assert "Insufficient privileges" in coverage["RETURNS"]["reasons"][0]
    assert coverage["SALES"]["coverage"] == "full"
    assert not any(
        m["name"].lower().startswith("returns.") for m in envelope["data"]["metrics"]
    )


def _hidden_private() -> dict:
    """SALES as a role without REFERENCES reads it back: the private
    `discount` fact gone, the public `gross` metric over it still there."""

    deployed = copy.deepcopy(SALES)
    orders = deployed["tables"][0]
    orders["facts"] = [f for f in orders["facts"] if f["name"] != "discount"]
    for metric in orders["metrics"]:
        if metric["name"] == "gross":
            metric.pop("access_modifier")
    return deployed


def test_a_read_back_reading_members_it_does_not_carry_is_limited(
    tmp_path, capsys, account
):
    account["account"] = Account(deployed=_hidden_private())
    envelope = _list(capsys, _repo(tmp_path))

    (coverage,) = envelope["data"]["coverage"]
    assert coverage["coverage"] == "limited"
    assert "does not carry" in coverage["reasons"][0]
    assert "gross" in coverage["reasons"][0].lower()


def test_a_query_relying_on_such_a_member_is_refused_before_spend(
    tmp_path, capsys, account
):
    account["account"] = Account(deployed=_hidden_private())
    envelope = _query(capsys, _repo(tmp_path), "sales.orders.gross")

    assert envelope["reason"] == "guard", envelope
    assert "does not carry" in envelope["errors"][0]
    assert account["account"].queries == []


def test_a_query_not_relying_on_it_runs_and_says_the_view_is_limited(
    tmp_path, capsys, account
):
    account["account"] = Account(deployed=_hidden_private())
    envelope = _query(capsys, _repo(tmp_path), "sales.orders.order_count")

    assert envelope["status"] == "ok", envelope["errors"]
    assert any("does not carry" in n for n in envelope["data"]["notes"])


def test_a_file_whose_private_member_is_not_read_back_is_limited(
    tmp_path, capsys, account
):
    """The `local` rule, kept where a configured file describes the view."""
    deployed = copy.deepcopy(SALES)
    deployed["tables"][0]["facts"].pop(1)  # the private discount, hidden
    deployed["tables"][0]["metrics"] = [
        m for m in deployed["tables"][0]["metrics"] if m["name"] != "gross"
    ]
    account["account"] = Account(deployed=deployed)
    envelope = _list(capsys, _repo(tmp_path, SALES))

    (coverage,) = envelope["data"]["coverage"]
    assert coverage["coverage"] == "limited"
    assert "orders.discount" in coverage["reasons"][0]


def test_an_empty_scope_is_an_empty_catalog_that_says_so(tmp_path, capsys, account):
    account["account"] = Account(deployed=None)
    envelope = _list(capsys, _repo(tmp_path))

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["metrics"] == []
    assert envelope["data"].get("coverage", []) == []
    assert any("listed nothing" in n for n in envelope["data"]["notes"])


def test_a_query_on_a_view_the_account_does_not_hold_is_refused_by_name(
    tmp_path, capsys, account
):
    account["account"] = Account(deployed=None)
    envelope = _query(capsys, _repo(tmp_path), "sales.orders.revenue")

    assert envelope["status"] == "error"
    assert "no semantic view named 'sales'" in envelope["errors"][0]
    assert account["account"].queries == []


def _two_scopes() -> Account:
    """SALES deployed twice: on SHOP.PUBLIC's tables, and on a staging copy of
    them in SHOP.STAGING, as a dev and a prod schema each hold their own."""

    from fakes.snowflake import FakeSnowflakeTable

    staging = copy.deepcopy(SALES)
    for logical in staging["tables"]:
        logical["base_table"]["schema"] = "STAGING"
    fake = Account(views={"SHOP.STAGING.SALES": staging})
    fake.connection.tables.extend(
        FakeSnowflakeTable("SHOP", "STAGING", name, [(key, "FIXED", False)], rows=10)
        for name, key in (
            ("ORDERS", "O_ORDERKEY"),
            ("CUSTOMERS", "C_CUSTKEY"),
            ("NATIONS", "N_NATIONKEY"),
        )
    )
    return fake


def test_a_name_in_two_scopes_is_refused_for_each(tmp_path, capsys, account):
    account["account"] = _two_scopes()
    root = _repo(tmp_path, databases="[SHOP]")
    envelope = _list(capsys, root)

    coverage = envelope["data"]["coverage"]
    assert [c["deployed_as"] for c in coverage] == [
        "SHOP.PUBLIC.SALES",
        "SHOP.STAGING.SALES",
    ]
    assert {c["coverage"] for c in coverage} == {"refused"}
    assert "--scope" in coverage[0]["reasons"][0]
    assert envelope["data"]["metrics"] == []
    assert account["account"].reads == []

    refused = _query(capsys, root, "sales.orders.revenue")
    assert refused["reason"] == "guard", refused
    assert "more than one allowlisted schema" in refused["errors"][0]
    assert account["account"].queries == []


def test_scope_narrows_a_name_in_two_scopes_to_one(tmp_path, capsys, account):
    account["account"] = _two_scopes()
    root = _repo(tmp_path, databases="[SHOP]")
    envelope = _query(
        capsys, root, "sales.orders.order_count", "--scope", "SHOP.STAGING"
    )

    assert envelope["status"] == "ok", envelope["errors"]
    (sent,) = account["account"].queries
    assert '"SHOP"."STAGING"."SALES"' in sent


# --- every result says where its definition came from ---------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["list", "--deployed"],
        ["query", "sales.orders.order_count", "--confirm", "--budget", "3000"],
        ["values", "sales.orders.status", "--confirm", "--budget", "3000"],
    ],
    ids=["list", "list-deployed", "query", "values"],
)
def test_every_result_carries_the_account_disclosure(tmp_path, capsys, account, argv):
    envelope = _run(capsys, _repo(tmp_path), "explore", "semantic", *argv)

    assert envelope["status"] == "ok", envelope["errors"]
    assert any(DISCLOSURE in w for w in envelope["warnings"]), envelope["warnings"]
    data = envelope["data"]
    assert data.get("definition_source", data.get("source")) == "account"
    assert data["deployment"] == "snowflake_account"


def test_list_deployed_says_it_changes_nothing(tmp_path, capsys, account):
    envelope = _list(capsys, _repo(tmp_path), "--deployed")

    assert any("--deployed changes nothing" in n for n in envelope["data"]["notes"])


# --- a governed query --------------------------------------------------------------


def test_a_query_runs_through_dex_s_connector_unchecked(tmp_path, capsys, account):
    envelope = _query(
        capsys,
        _repo(tmp_path),
        "sales.orders.order_count",
        "--group-by",
        "sales.orders.status",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["cells"] == [["F", 7]]
    assert (data["vendor"], data["execution"]) == ("snowflake", "dex")
    assert data["definition_source"] == "account"
    (sent,) = account["account"].queries
    assert sent.startswith('SELECT * FROM SEMANTIC_VIEW("SHOP"."PUBLIC"."SALES"')
    assert any("deployed as SHOP.PUBLIC.SALES" in n for n in data["notes"])


def test_the_statement_order_is_show_then_read_back_then_query(
    tmp_path, capsys, account
):
    _query(capsys, _repo(tmp_path), "sales.orders.revenue")

    sent = account["account"].sent
    show = next(i for i, s in enumerate(sent) if s.startswith("SHOW SEMANTIC VIEWS"))
    read = next(i for i, s in enumerate(sent) if "READ_YAML" in s)
    run = next(i for i, s in enumerate(sent) if "SEMANTIC_VIEW(" in s and i != read)
    assert show < read < run


def test_a_query_reads_back_only_the_view_it_names(tmp_path, capsys, account):
    account["account"] = Account(views={"SHOP.PUBLIC.RETURNS": RETURNS})
    _query(capsys, _repo(tmp_path), "sales.orders.order_count")

    assert account["account"].reads == ["SHOP.PUBLIC.SALES"]


def test_unconfirmed_it_asks_priced_from_the_base_tables(tmp_path, capsys, account):
    root = _repo(tmp_path)
    narrow = _query(capsys, root, "sales.customers.customer_count", confirm=False)
    wide = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.customers.segment",
        confirm=False,
    )

    assert narrow["status"] == "needs_confirmation", narrow
    assert narrow["cost"]["estimate"] < wide["cost"]["estimate"]
    assert account["account"].queries == []


def test_values_of_a_personal_dimension_are_refused_before_any_warehouse_statement(
    tmp_path, capsys, account
):
    """The token resolves only through the read-back, which is free; nothing
    billed is sent."""

    envelope = _values(capsys, _repo(tmp_path), "sales.customers.email")

    assert envelope["reason"] == "guard", envelope
    assert account["account"].queries == []
    assert all(
        s.startswith("SHOW") or "READ_YAML" in s for s in account["account"].sent
    )


def test_a_personal_group_by_is_refused_before_spend(tmp_path, capsys, account):
    envelope = _query(
        capsys,
        _repo(tmp_path),
        "sales.orders.order_count",
        "--group-by",
        "sales.customers.email",
    )

    assert envelope["reason"] == "guard", envelope
    assert account["account"].queries == []


def test_values_scoped_by_a_metric(tmp_path, capsys, account):
    envelope = _values(
        capsys,
        _repo(tmp_path),
        "sales.customers.segment",
        "--metric",
        "sales.orders.order_count",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["scoped_to"] == ["sales.orders.order_count"]


def test_api_is_refused_since_dex_executes(tmp_path, capsys, account):
    envelope = _query(capsys, _repo(tmp_path), "sales.orders.revenue", "--api")

    assert envelope["status"] == "error"
    assert account["account"].sent == []


# --- a configured file beside the account -------------------------------------------


def test_a_file_describing_a_deployed_view_is_named_never_silently_passed_over(
    tmp_path, capsys, account
):
    account["account"] = Account(views={"SHOP.PUBLIC.RETURNS": RETURNS})
    root = _repo(tmp_path, SALES)
    envelope = _list(capsys, root)

    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["sales"]["repository_file"] == "views/sales.yaml"
    assert "repository_file" not in coverage["RETURNS"]
    # The file lends the view its spelling.
    assert "sales.orders.order_count" in {
        m["name"] for m in envelope["data"]["metrics"]
    }

    queried = _query(capsys, root, "sales.orders.order_count")
    assert queried["status"] == "ok", queried["errors"]
    assert any(
        "views/sales.yaml" in n and "was not checked" in n
        for n in queried["data"]["notes"]
    )


def test_a_differing_file_is_not_a_refusal_under_the_account(tmp_path, capsys, account):
    """The account is the source of truth; `--local` is what holds it to the
    file."""

    edited = copy.deepcopy(SALES)
    edited["tables"][0]["metrics"][1]["expr"] = "SUM(orders.total_price) * 2"
    root = _repo(tmp_path, edited)

    assert _query(capsys, root, "sales.orders.revenue")["status"] == "ok"

    held = _query(capsys, root, "sales.orders.revenue", "--local")
    assert held["reason"] == "guard", held
    assert "differs from the repository" in held["errors"][0]
    assert held["data"].get("deployment") != "snowflake_account"


def test_local_with_no_file_configured_says_so(tmp_path, capsys, account):
    envelope = _list(capsys, _repo(tmp_path), "--local")

    assert envelope["status"] == "error"
    assert "no semantic.snowflake.files" in envelope["errors"][0]
    assert account["account"].sent == []


def test_a_local_configuration_never_contacts_the_account(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES, deployment="local")
    envelope = _list(capsys, root)

    assert envelope["status"] == "ok"
    assert envelope["data"]["deployment"] == "local"
    assert "source" not in envelope["data"]
    assert not any(DISCLOSURE in w for w in envelope["warnings"])
    assert account["account"].sent == []


# --- explore relationships, and the repository-only commands -------------------------


def test_relationships_fold_the_account_s_joins_under_the_hosted_flag(
    tmp_path, capsys, account
):
    """The edges `explore relationships|map --use-hosted-semantic-layer` fold
    in: the account's relationships, on the base tables they join. Read free,
    through SHOW and the read-back; the profiling probes are the command's own."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.commands import _native_semantic_edges

    engine = DexEngine.from_repo(_repo(tmp_path))
    identifiers = ["SHOP.PUBLIC.ORDERS", "SHOP.PUBLIC.CUSTOMERS", "SHOP.PUBLIC.NATIONS"]
    bare, _ = _native_semantic_edges(engine, False, identifiers)
    edges, _ = _native_semantic_edges(
        engine, False, identifiers, use_hosted_semantic_layer=True
    )

    assert bare == []
    joined = {
        tuple(sorted((e.from_dataset.upper(), e.to_dataset.upper()))) for e in edges
    }
    assert ("SHOP.PUBLIC.CUSTOMERS", "SHOP.PUBLIC.ORDERS") in joined
    assert ("SHOP.PUBLIC.CUSTOMERS", "SHOP.PUBLIC.NATIONS") in joined
    assert account["account"].queries == []


def test_the_fold_rides_the_outer_command_s_adapter_and_its_cost_gate(
    tmp_path, capsys, account
):
    """Found dogfooding: opening an adapter through the engine rebuilds its cost
    gate, which settled `explore relationships`' own gate early and stamped its
    result with the account read's empty one (`seconds_billed: 0.0` beside a
    ledger that recorded the profiling). The free read rides the open adapter."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.explore.commands import _native_semantic_edges

    engine = DexEngine.from_repo(_repo(tmp_path))
    adapter = engine._adapter("explore relationships")
    gate = adapter.cost_gate
    edges, _ = _native_semantic_edges(
        engine,
        False,
        ["SHOP.PUBLIC.ORDERS", "SHOP.PUBLIC.CUSTOMERS"],
        use_hosted_semantic_layer=True,
    )

    assert edges
    assert engine._adapter_instance is adapter
    assert adapter.cost_gate is gate


def test_maintain_reads_no_account_layer_and_says_so(tmp_path, capsys, account):
    """Like dbt Cloud, the account is explore's alone: maintain reads the
    repository's files, and with none it compares nothing and says why."""

    from exmergo_dex_core.engine import DexEngine

    engine = DexEngine.from_repo(_repo(tmp_path))
    layer = engine.semantic_catalog_source().semantic_layer()

    assert layer.semantic_models == []
    assert any("snowflake_account" in n for n in layer.notes)
    assert account["account"].sent == []


def test_authoring_with_no_file_configured_is_refused_by_name(
    tmp_path, capsys, account
):
    root = _repo(tmp_path)
    edits = tmp_path / "edits.json"
    edits.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "views/sales.yaml",
                        "kind": "semantic_document",
                        "content": yaml.safe_dump(SALES),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    envelope = _run(
        capsys, root, "semantic", "snowflake", "define", "--edits-file", str(edits)
    )

    assert envelope["status"] == "error"
    assert "semantic.snowflake.files" in " ".join(envelope["errors"])
    assert account["account"].sent == []
