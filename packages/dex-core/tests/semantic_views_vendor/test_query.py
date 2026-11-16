"""Governed `explore semantic query|values` under `semantic.vendor: snowflake` (#1290).

Dex renders `SELECT ... FROM SEMANTIC_VIEW(<deployed view> ...)` from the
repository's catalog, reads the deployed view back and holds it to the
repository, and executes through its own Snowflake adapter. These run the real
adapter and cost gate against the behavioural fake connection
(`tests/fakes/snowflake.py`), through the CLI, and read its statement log: what
was sent, in what order, and that nothing was sent before a refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("snowflake.connector")

from fakes.snowflake import (
    FakeSnowflakeTable,
)

from exmergo_dex_core.cli import main

from .account_fake import SALES, Account
from .conftest import sv, table


def _repo(root: Path, *views: dict, extra: str = "") -> Path:
    names = []
    for view in views:
        path = root / "views" / f"{view['name']}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(view, sort_keys=False), encoding="utf-8")
        names.append(f"views/{view['name']}.yaml")
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    listed = "".join(f"\n      - {name}" for name in names)
    (root / ".dex" / "config.yml").write_text(
        "connector: snowflake\n"
        "snowflake:\n"
        "  warehouse: DEX_WH\n"
        "  databases: [SHOP.PUBLIC]\n"
        "budget:\n"
        "  ceiling: 3600\n"
        "  session_ceiling: 36000\n"
        "semantic:\n"
        "  vendor: snowflake\n"
        "  snowflake:\n"
        f"    files:{listed}\n" + extra,
        encoding="utf-8",
    )
    return root


def _run(capsys, root: Path, *argv: str) -> dict:
    main(["--repo-root", str(root), *argv])
    return json.loads(capsys.readouterr().out)


def _query(capsys, root: Path, *argv: str, confirm: bool = True) -> dict:
    flags = ["--confirm", "--budget", "3000"] if confirm else []
    return _run(capsys, root, "explore", "semantic", "query", *argv, *flags)


# --- a governed query, end to end ---------------------------------------------------


def test_a_metric_by_dimension_runs_through_dex_s_connector(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    envelope = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.orders.status",
        "--order-by=-sales.orders.order_count",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["columns"] == ["sales.orders.status", "sales.orders.order_count"]
    assert data["cells"] == [["F", 7]]
    assert (data["vendor"], data["execution"]) == ("snowflake", "dex")
    (sent,) = account["account"].queries
    assert sent == (
        'SELECT * FROM SEMANTIC_VIEW("SHOP"."PUBLIC"."SALES" DIMENSIONS '
        'orders.status AS "c0" METRICS orders.order_count AS "c1") '
        'ORDER BY "c1" DESC LIMIT 51'
    )
    assert any("deployed as SHOP.PUBLIC.SALES" in n for n in data["notes"])


def test_the_statement_order_is_the_safety_contract(tmp_path, capsys, account):
    """SHOW and the read-back (no warehouse), then the one billed
    statement: the definition check precedes any spend."""

    root = _repo(tmp_path, SALES)
    _query(capsys, root, "sales.orders.revenue")

    sent = account["account"].sent
    show = next(i for i, s in enumerate(sent) if s.startswith("SHOW SEMANTIC VIEWS"))
    read = next(i for i, s in enumerate(sent) if "READ_YAML" in s)
    run = next(i for i, s in enumerate(sent) if "SEMANTIC_VIEW(" in s and i != read)
    assert show < read < run


def test_unconfirmed_it_asks_and_sends_no_query(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.revenue", confirm=False)

    assert envelope["status"] == "needs_confirmation", envelope
    assert account["account"].queries == []


def test_it_is_priced_by_the_base_tables_the_members_reach(tmp_path, capsys, account):
    """Snowflake keeps no bytes for a semantic view; the floor would quote a
    42 GB scan as one second."""

    root = _repo(tmp_path, SALES)
    narrow = _query(capsys, root, "sales.customers.customer_count", confirm=False)
    wide = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.customers.segment",
        confirm=False,
    )

    assert narrow["cost"]["estimate"] < wide["cost"]["estimate"]
    assert wide["cost"]["estimate"] > 30


def test_a_grain_and_a_filter(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    account["account"].rows = [{"c0": "2026-01-01", "c1": 3}]
    envelope = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.orders.order_date__month",
        "--where",
        "orders.status = 'F'",
        "--where",
        "sales.customers.segment IN ('BUILDING', 'MACHINERY')",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["columns"] == [
        "sales.orders.order_date__month",
        "sales.orders.order_count",
    ]
    (sent,) = account["account"].queries
    assert "DIMENSIONS DATE_TRUNC('month', orders.order_date) AS \"c0\"" in sent
    assert (
        "WHERE (orders.status = 'F') AND "
        "(customers.segment IN ('BUILDING', 'MACHINERY'))"
    ) in sent


def test_a_derived_metric_groups_by_what_its_inputs_share(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    account["account"].rows = [{"c0": "BUILDING", "c1": 1.5}]
    envelope = _query(
        capsys,
        root,
        "sales.orders_per_customer",
        "--group-by",
        "sales.customers.segment",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    (sent,) = account["account"].queries
    assert 'METRICS orders_per_customer AS "c1"' in sent

    # customer_count sits on customers, which reaches no orders column.
    refused = _query(
        capsys,
        root,
        "sales.orders_per_customer",
        "--group-by",
        "sales.orders.order_date__month",
    )
    assert "cannot group sales.orders_per_customer" in refused["errors"][0]


def test_facts_alone_are_a_query(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    account["account"].rows = [{"total_price": 10.5}]
    envelope = _query(capsys, root, "--group-by", "sales.orders.total_price")

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["columns"] == ["sales.orders.total_price"]
    (sent,) = account["account"].queries
    assert "FACTS orders.total_price)" in sent


# --- values --------------------------------------------------------------------------


def test_values_of_a_joined_dimension(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    account["account"].rows = [{"c0": "BUILDING"}]
    envelope = _run(
        capsys,
        root,
        "explore",
        "semantic",
        "values",
        "sales.customers.segment",
        "--confirm",
        "--budget",
        "3000",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["columns"] == ["sales.customers.segment"]
    assert envelope["data"]["scoped_to"] == []
    (sent,) = account["account"].queries
    assert sent.startswith(
        'SELECT * FROM SEMANTIC_VIEW("SHOP"."PUBLIC"."SALES" DIMENSIONS '
        'customers.segment AS "c0")'
    )


def test_values_scoped_by_a_metric(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    account["account"].rows = [{"c0": "BUILDING"}]
    envelope = _run(
        capsys,
        root,
        "explore",
        "semantic",
        "values",
        "sales.customers.segment",
        "--metric",
        "sales.orders.order_count",
        "--confirm",
        "--budget",
        "3000",
    )

    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["scoped_to"] == ["sales.orders.order_count"]
    (sent,) = account["account"].queries
    assert sent.startswith('SELECT "c0" FROM SEMANTIC_VIEW(')
    assert 'METRICS orders.order_count AS "c1"' in sent


def test_values_of_a_personal_dimension_are_refused_before_anything_is_sent(
    tmp_path, capsys, account
):
    root = _repo(tmp_path, SALES)
    envelope = _run(
        capsys, root, "explore", "semantic", "values", "sales.customers.email"
    )

    assert envelope["reason"] == "guard", envelope
    assert account["account"].sent == []


# --- refusals before anything is sent ------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (
            ["sales.orders.revenue", "--group-by", "sales.orders.total_price"],
            "never reads facts in the same query as metrics",
        ),
        (
            ["--group-by", "sales.orders.total_price,sales.orders.status"],
            "2026_07",
        ),
        (["sales.orders.gross"], "is private"),
        (["--group-by", "sales.orders.discount"], "is private"),
        (["sales.orders.running"], "window function metric"),
        (
            ["sales.customers.customer_count", "--group-by", "sales.orders.status"],
            "cannot group sales.customers.customer_count",
        ),
        (
            ["sales.orders.order_count", "--group-by", "sales.orders.status__month"],
            "is categorical",
        ),
        (["sales.orders.order_count", "--grain", "month"], "no `metric_time`"),
        (
            ["sales.orders.order_count", "--where", "orders.revenue > 1"],
            "a metric",
        ),
        (
            ["sales.orders.order_count", "--where", "orders.discount > 1"],
            "is private",
        ),
        (
            [
                "sales.orders.order_count",
                "--where",
                "orders.status IN (SELECT 1)",
            ],
            "could not read which members",
        ),
        (["sales.orders.nope"], "no such member"),
        (
            ["sales.orders.order_count", "--group-by", "sales.customers.email"],
            "would surface PII",
        ),
        (["sales.customers.last_email"], "would surface PII"),
        (
            ["sales.orders.order_count", "--where", "customers.email = 'x'"],
            "would surface PII",
        ),
    ],
)
def test_refused_before_anything_is_sent(tmp_path, capsys, account, argv, says):
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, *argv)

    assert envelope["status"] == "error", envelope
    assert says in " ".join(envelope["errors"])
    assert account["account"].sent == []


def test_tokens_from_two_views_are_refused(tmp_path, capsys, account):
    other = sv(
        "other",
        table(
            "t",
            "SHOP.PUBLIC.ORDERS",
            ["o_orderkey"],
            metrics=[{"name": "n", "expr": "COUNT(o_orderkey)"}],
        ),
    )
    root = _repo(tmp_path, SALES, other)
    envelope = _query(
        capsys, root, "sales.orders.order_count", "--group-by", "other.t.n"
    )

    assert "one semantic view" in " ".join(envelope["errors"])
    assert account["account"].sent == []


def test_a_view_declaring_variables_is_refused(tmp_path, capsys, account):
    view = dict(SALES, variables=[{"name": "x", "data_type": "NUMBER"}])
    root = _repo(tmp_path, view)
    envelope = _query(capsys, root, "sales.orders.order_count")

    assert "`variables`" in " ".join(envelope["errors"])


# --- the deployed view ----------------------------------------------------------------


def test_a_view_defined_only_in_the_repository_is_refused_before_spend(
    tmp_path, capsys, account
):
    account["account"] = Account(deployed=None)
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.revenue")

    assert envelope["reason"] == "guard", envelope
    assert "defined only in the repository" in envelope["errors"][0]
    assert account["account"].queries == []
    assert not any("READ_YAML" in s for s in account["account"].sent)


def test_a_view_in_two_allowlisted_schemas_is_ambiguous(tmp_path, capsys, account):
    fake = account["account"]
    fake.connection.tables.append(
        FakeSnowflakeTable("SHOP", "STAGING", "SALES", [], kind="semantic_view")
    )
    fake.connection.databases = sorted({*fake.connection.databases})
    root = _repo(tmp_path, SALES)
    (root / ".dex" / "config.yml").write_text(
        (root / ".dex" / "config.yml").read_text().replace("[SHOP.PUBLIC]", "[SHOP]"),
        encoding="utf-8",
    )
    envelope = _query(capsys, root, "sales.orders.revenue")

    assert "more than one allowlisted schema" in envelope["errors"][0]
    assert "--scope" in envelope["errors"][0]
    assert fake.queries == []


@pytest.mark.parametrize(
    ("edit", "says"),
    [
        (
            lambda v: v["tables"][0]["metrics"][1].update(
                expr="SUM(orders.total_price) * 2"
            ),
            "orders.revenue has a different expr",
        ),
        (
            lambda v: v["tables"][1]["dimensions"][0].update(expr="c_name"),
            "customers.segment has a different expr",
        ),
        (
            lambda v: v["tables"][0]["facts"][0].update(
                access_modifier="private_access"
            ),
            "orders.total_price has a different access_modifier",
        ),
        (
            lambda v: v["tables"][0]["metrics"].pop(1),
            "orders.revenue is not in the deployed view",
        ),
        (
            lambda v: v["relationships"][0]["relationship_columns"][0].update(
                left_column="o_orderkey"
            ),
            "relationship orders_customers has a different columns",
        ),
        (
            lambda v: v["tables"][1].update(primary_key={"columns": ["c_email"]}),
            "table customers has a different primary_key",
        ),
    ],
)
def test_a_stale_deployment_is_refused_before_spend(
    tmp_path, capsys, account, edit, says
):
    deployed = json.loads(json.dumps(SALES))
    edit(deployed)
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, SALES)
    envelope = _query(
        capsys,
        root,
        "sales.orders.revenue",
        "--group-by",
        "sales.customers.segment",
        "--where",
        "orders.total_price > 1",
    )

    assert envelope["reason"] == "guard", envelope
    assert says in envelope["errors"][0], envelope["errors"]
    assert account["account"].queries == []


def test_a_member_this_query_does_not_rely_on_may_differ(tmp_path, capsys, account):
    deployed = json.loads(json.dumps(SALES))
    deployed["tables"][0]["dimensions"][1]["expr"] = "o_orderstatus"  # priority
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.order_count")

    assert envelope["status"] == "ok", envelope["errors"]


def test_a_view_this_role_cannot_read_back_is_refused(tmp_path, capsys, account):
    account["account"] = Account(readable=False)
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.revenue")

    assert envelope["reason"] == "guard"
    assert "could not be read back" in envelope["errors"][0]
    assert account["account"].queries == []


def test_a_private_input_this_role_cannot_read_refuses_rather_than_runs(
    tmp_path, capsys, account
):
    """`orders_per_customer` reads nothing private, but a metric reading a
    private one that the read-back hides cannot be compared."""

    view = json.loads(json.dumps(SALES))
    view["metrics"].append({"name": "gross_x2", "expr": "orders.gross * 2"})
    deployed = json.loads(json.dumps(view))
    deployed["tables"][0]["metrics"].pop(3)  # gross, hidden from this role
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, view)
    envelope = _query(capsys, root, "sales.gross_x2")

    assert envelope["reason"] == "guard", envelope
    assert "this role cannot read it back" in envelope["errors"][0]


# --- list --deployed -----------------------------------------------------------------


def test_list_deployed_reads_the_account_and_names_its_coverage(
    tmp_path, capsys, account
):
    root = _repo(tmp_path, SALES)
    envelope = _run(capsys, root, "explore", "semantic", "list", "--deployed")

    assert envelope["status"] == "ok", envelope["errors"]
    data = envelope["data"]
    assert data["source"] == "deployed"
    assert data["coverage"] == [
        {"view": "sales", "deployed_as": "SHOP.PUBLIC.SALES", "coverage": "full"}
    ]
    assert "sales.orders.order_count" in {m["name"] for m in data["metrics"]}


def test_list_deployed_names_a_limited_and_a_missing_view(tmp_path, capsys, account):
    deployed = json.loads(json.dumps(SALES))
    deployed["tables"][0]["facts"].pop(1)  # the private discount, hidden
    account["account"] = Account(deployed=deployed)
    missing = sv(
        "returns",
        table(
            "r",
            "SHOP.PUBLIC.ORDERS",
            ["o_orderkey"],
            dimensions=[{"name": "s", "expr": "o_orderstatus"}],
        ),
    )
    root = _repo(tmp_path, SALES, missing)
    envelope = _run(capsys, root, "explore", "semantic", "list", "--deployed")

    coverage = {c["view"]: c for c in envelope["data"]["coverage"]}
    assert coverage["sales"]["coverage"] == "limited"
    assert "orders.discount" in coverage["sales"]["reasons"][0]
    assert coverage["returns"]["coverage"] == "refused"
    assert "defined only in the repository" in coverage["returns"]["reasons"][0]


def test_a_plain_list_contacts_no_account(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    envelope = _run(capsys, root, "explore", "semantic", "list")

    assert envelope["status"] == "ok"
    assert "source" not in envelope["data"]
    assert account["account"].sent == []


def test_deployed_is_refused_where_the_layer_has_no_deployment(tmp_path, capsys):
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: dbt\n", encoding="utf-8"
    )
    envelope = _run(capsys, tmp_path, "explore", "semantic", "list", "--deployed")

    assert envelope["status"] == "error"


def test_deployed_is_a_list_flag(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.revenue", "--deployed")

    assert "`list`" in " ".join(envelope["errors"])


# --- review round 1 ----------------------------------------------------------------


def _flag(root: Path, column: str) -> None:
    """Profile CUSTOMERS with ``column`` flagged as email, as `explore profile`
    would have cached it."""

    from exmergo_dex_core.cache import (
        ColumnProfile,
        Dataset,
        DexCache,
        PIICategory,
        PIIFlag,
    )
    from exmergo_dex_core.storage import FilesystemStore

    FilesystemStore(root).save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="SHOP.PUBLIC.CUSTOMERS",
                    columns=[
                        ColumnProfile(
                            name=column,
                            data_type="TEXT",
                            pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
                        ),
                        ColumnProfile(name="C_MKTSEGMENT", data_type="TEXT"),
                    ],
                )
            ]
        )
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["sales.orders.order_count", "--group-by", "SALES.CUSTOMERS.CONTACT"],
        ["sales.orders.order_count", "--where", "CUSTOMERS.CONTACT = 'x'"],
        ["sales.customers.top_ref"],
    ],
    ids=["another-case", "filter-another-case", "max-of-a-base-column"],
)
def test_a_profiled_flag_binds_whatever_the_spelling(tmp_path, capsys, account, argv):
    """A neutral name over a flagged column: the case a name heuristic misses,
    so the cached flag has to be found however the token is spelled, and a
    bare base column inside MAX() is screened as that column."""

    root = _repo(tmp_path, SALES)
    _flag(root, "C_REF")
    envelope = _query(capsys, root, *argv)

    assert "would surface PII" in " ".join(envelope["errors"]), envelope
    assert account["account"].sent == []


def test_values_in_another_case_meet_the_same_flag(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    _flag(root, "C_REF")
    envelope = _run(
        capsys, root, "explore", "semantic", "values", "Sales.Customers.Contact"
    )

    assert envelope["reason"] == "guard", envelope
    assert account["account"].sent == []


def test_a_changed_fact_a_metric_sums_is_stale(tmp_path, capsys, account):
    deployed = json.loads(json.dumps(SALES))
    deployed["tables"][0]["facts"][0]["expr"] = "o_totalprice * 2"
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, SALES)
    envelope = _query(capsys, root, "sales.orders.revenue")

    assert "orders.total_price has a different expr" in envelope["errors"][0]
    assert account["account"].queries == []


def test_the_table_a_dimension_is_joined_through_is_held_too(tmp_path, capsys, account):
    """`orders` reaches `nations` through `customers`: a changed key on the
    intermediate table changes the join, so it is compared and priced."""

    deployed = json.loads(json.dumps(SALES))
    deployed["tables"][1]["primary_key"] = {"columns": ["c_ref"]}
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, SALES)
    envelope = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.nations.country",
    )

    assert "table customers has a different primary_key" in envelope["errors"][0]


def test_facts_sharing_a_name_are_refused(tmp_path, capsys, account):
    root = _repo(tmp_path, SALES)
    envelope = _query(
        capsys,
        root,
        "--group-by",
        "sales.orders.total_price,sales.customers.total_price",
    )

    assert "share the name" in " ".join(envelope["errors"]), envelope
    assert account["account"].queries == []


def test_each_operation_opens_its_own_adapter(tmp_path):
    """A gate must not carry one operation's confirmation or charges into the
    next: the adapter is released when the operation ends, on a refusal too."""

    from exmergo_dex_core.config import QueryLimits
    from exmergo_dex_core.explore.semantic.governed import GovernedSql

    opened: list[str] = []

    class Engine:
        def _adapter(self, command):
            opened.append(command)
            return object()

    governed = GovernedSql(
        Engine(), "snowflake", QueryLimits(), dict, recompile_hint="x"
    )
    first = governed.adapter("explore semantic query")
    assert governed.adapter("explore semantic query") is first
    governed.release()
    assert governed.adapter("explore semantic query") is not first
    assert opened == ["explore semantic query", "explore semantic query"]


def test_array_union_agg_returns_its_inputs():
    from exmergo_dex_core.semantic_views.lineage import value_reads

    reads = value_reads("ARRAY_UNION_AGG(customers.tags)")
    assert [(r.qualifier, r.name) for r in reads or []] == [("customers", "tags")]


def test_a_base_table_outside_the_allowlist_is_refused_not_priced_as_nothing(
    tmp_path, capsys, account
):
    """The statement is priced from its base tables' sizes, which only the
    allowlisted inventory carries: a table outside it would be quoted as
    nothing while Snowflake scans it."""

    view = json.loads(json.dumps(SALES))
    view["tables"][2]["base_table"]["schema"] = "ELSEWHERE"
    deployed = json.loads(json.dumps(view))
    account["account"] = Account(deployed=deployed)
    root = _repo(tmp_path, view)
    envelope = _query(
        capsys,
        root,
        "sales.orders.order_count",
        "--group-by",
        "sales.nations.country",
    )

    assert "outside the source allowlist" in " ".join(envelope["errors"]), envelope
    assert "SHOP.ELSEWHERE.NATIONS" in envelope["errors"][0]
    assert account["account"].queries == []
